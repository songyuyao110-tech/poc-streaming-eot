"""Loopback-only PoC UI; reuse the existing single-utterance pipeline."""
from collections import deque
import json
import math
import os
import re
from pathlib import Path
from queue import Empty, Queue
import sys
from threading import Lock, RLock, Thread
import time
import traceback
import uuid

POC_ROOT = Path(__file__).resolve().parents[1]
SRC = Path(os.environ.get("POC_SRC", POC_ROOT / "src"))
PORT = int(os.environ.get("POC_PORT", "8765"))
MIC_CASES = Path(os.environ.get("POC_MIC_CASES", POC_ROOT / "cases"))
MIC_SAMPLE_RATE = 16000
MIC_FRAME_MS = 20
sys.dont_write_bytecode = True
sys.path.insert(0, str(SRC))
from app import bootstrap
bootstrap.install()          # 上游仓库根（model_clients/registry）

from flask import Flask, Response, jsonify, request, send_file, stream_with_context
from simple_websocket import Server as WsServer
from core_pipeline_run import TOOL_SET, run
from model_clients import warm_slow_model
from shared.text import normalize

# 云端 PoC 的运行时注入口：这台机器没有 corp LiteLLM，只有本机 127.0.0.1:4000 的代理。
# 主链原先把「必须是 corp 地址 + deepseek-v4.1-flash」写死在 `existing_slow_configuration()` 里；
# 这里只在显式设了 `POC_ALLOW_LOCAL_LITELLM=1` 时放行本机代理，不动判停/闸/工具/阈值，
# 也不打印任何凭据。没设这个开关时仍走原校验（默认行为不变）。
if os.environ.get("POC_ALLOW_LOCAL_LITELLM") == "1":
    import core_pipeline_run as _cpr

    def _poc_existing_slow_configuration():
        base = str(os.environ.get("LITELLM_BASE_URL") or "")
        if not (base.startswith("http://127.0.0.1:") or base.startswith("http://localhost:")):
            raise ValueError("POC_ALLOW_LOCAL_LITELLM only allows a loopback LiteLLM base")
        if not os.environ.get("LITELLM_API_KEY"):
            raise FileNotFoundError("Local LiteLLM credential unavailable")
        if not os.environ.get("SLOW_MODEL"):
            raise FileNotFoundError("Local LiteLLM model unavailable")

    _cpr.existing_slow_configuration = _poc_existing_slow_configuration
from hub_speech import HubSpeech          # 中枢出声接线（判断全走 gate/ 现成那批，不新判）
from neixinxi_log import NeixinxiLogReader, NeixinxiLogWriter

app = Flask(__name__)
app.json.ensure_ascii = False


@app.get("/")
def index():
    return send_file(Path(__file__).with_name("index.html"))


@app.post("/api/run")
def run_query():
    body = request.get_json()
    # The PoC serves exactly one tool set; a supplied `tool_set` is ignored, never honoured.
    result = run(body["utterance"])
    return jsonify(result), 200 if result["outcome"] == "PASS" else 502


@app.post("/api/run/stream")
def stream_query():
    body = request.get_json()
    utterance = body["utterance"]
    queue = Queue()
    began = time.monotonic()

    def emit(event, data):
        queue.put((event, {**data, "event_elapsed_ms": (time.monotonic() - began) * 1000}))

    def produce():
        try:
            # Callbacks fire at the real KEV/LLM boundaries, not after run() returns.
            result = run(utterance, on_stage=emit)
            if result["outcome"] != "PASS":
                emit("error", result)
                return
            decision = result["decision"]
            execute = decision["kind"] == "tool_call"
            emit("exec_start", {"mock_only": True, "skipped": not execute,
                                "message": "开始 mock 执行（不会调用业务工具）" if execute else "跳过执行：追问或结束动作"})
            if execute:
                emit("exec_step", {"step": 1, "mock_only": True, "tool": decision["tool"], "arguments": decision["arguments"],
                                   "message": "调用 " + decision["tool"] + "…（仅模拟，不发送）"})
                emit("exec_step", {"step": 2, "mock_only": True, "tool": decision["tool"], "message": "mock 成功（不代表真实任务完成）"})
            execution = {"mock_only": True, "skipped": not execute, "executed_tools": 0,
                         "message": "mock 执行展示完成" if execute else "ask_user：等待用户回答，不执行工具" if decision["kind"] == "ask_user" else "done：无需执行工具"}
            emit("exec_done", execution)
            emit("done", {**result, "mock_execution": execution, "ui_total_latency_ms": (time.monotonic() - began) * 1000})
        except Exception as error:
            emit("error", {"outcome": "FAIL", "error_type": type(error).__name__, "error": "流式请求未能完成，请查看服务日志；未执行真实工具。"})

    def events():
        emit("accepted", {"utterance": utterance, "tool_set": TOOL_SET, "asr": "text_standin", "mock_only": True})
        # One producer bridges the synchronous stage callbacks into this SSE response.
        Thread(target=produce, daemon=True).start()
        while True:
            event, data = queue.get()
            yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, allow_nan=False)}\n\n"
            if event in {"done", "error"}:
                break

    return Response(stream_with_context(events()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def _build_confirm_timeline(confirm_rounds):
    """把"第一轮 + 二轮回"展开成两段 ASR 时间线（确认轮演示用）。

    第一轮立即吐出并判停确认，二轮回在确认等待窗内到达。返回 (updates, vad)。
    """
    round1 = (confirm_rounds.get("round1") or "").strip()
    round2 = (confirm_rounds.get("round2") or "").strip()
    if not round1:
        raise ValueError("confirm_rounds.round1 需要一句第一轮指令")

    def chunk(text, base):
        updates = [{"t_ms": base + 120, "text": text, "final": True}]
        vad = [{"t_ms": base, "kind": "speech_start"},
               {"t_ms": base + 140, "kind": "speech_end"}]
        return updates, vad, base

    u1, v1, base = chunk(round1, 0)
    updates = list(u1)
    vad = list(v1)
    if round2:
        base2 = base + 900          # 确认请求发出后，二轮回在 CONFIRM_WAIT 内到达
        u2, v2, _ = chunk(round2, base2)
        updates += u2
        vad += v2
    return updates, vad


def _confirm_mock_chain(round1_text, round2_text):
    """确认轮演示的离线 mock 注入链（不连真实 KEV/LLM/工具）。

    返回 (eot_fn, tools_fn, decide_fn, confirm_gate)：
      - 用关键词判断第一轮是否"高危"：是 -> 池内工具 car_drive_ESPSwitch 触发确认；
        否（如"导航回家"）-> 池外导航工具，直接执行、永不确认。
      - 二轮回若含转折词（空调/导航等）则接管为池外 hvac；否则仍回 ESP。
      - 确认打分/裁决走 confirm_orchestrator 的 mock 通道（复用 confirm_gate 稳定 mock）。
    """
    from confirm_orchestrator import TurnConfirmGate

    def eot(text):
        # 形状对齐 eot-judge-api（complete/cut/wait + confidence + label + margin）：
        # score 口径下 complete≥0.80 才算说完，confidence≥0.70 才抢跑。
        return {"complete": 0.9, "cut": 0.08, "wait": 0.02, "label": "complete",
                "confidence": 0.95, "margin": 0.65, "wall_ms": 5.0, "kev_ms": 5.0}

    def tools(text, tool_set, context):
        if tool_set != "core_score":
            raise ValueError("confirm demo uses " + TOOL_SET)
        takeover = bool(round2_text) and any(k in text for k in ("空调", "导航", "音乐", "媒体"))
        if takeover:
            return {"scores": {"tool:car_hvac_set_temp": 0.9},
                    "selected": ["car_hvac_set_temp"],
                    "schemas": [{"name": "car_hvac_set_temp", "description": "d", "parameters": {"type": "object"}}],
                    "native_schemas": None, "domains": None,
                    "timing": {"stage1_ms": 5.0, "stage2_ms": 0.0, "total_ms": 5.0}}
        risk = any(k in text for k in ("ESP", "关", "引擎盖", "防滑", "充电", "刹车"))
        if risk:
            return {"scores": {"tool:car_drive_ESPSwitch": 0.9, "tool:car_hvac_set_temp": 0.2},
                    "selected": ["car_drive_ESPSwitch", "car_hvac_set_temp"],
                    "schemas": [{"name": "car_drive_ESPSwitch", "description": "d", "parameters": {"type": "object"}}],
                    "native_schemas": None, "domains": None,
                    "timing": {"stage1_ms": 5.0, "stage2_ms": 0.0, "total_ms": 5.0}}
        # 池外：导航/普通指令直接执行
        return {"scores": {"tool:car_navi_to_destination": 0.9},
                "selected": ["car_navi_to_destination"],
                "schemas": [{"name": "car_navi_to_destination", "description": "d", "parameters": {"type": "object"}}],
                "native_schemas": None, "domains": None,
                "timing": {"stage1_ms": 5.0, "stage2_ms": 0.0, "total_ms": 5.0}}

    def decide(utterance, selected, schemas, tool_set, context, **kwargs):
        # tools() 已决定候选；直接把第一个入选作为决策（池内或接管后的池外都由此表达）。
        tool = selected[0] if selected else "car_drive_ESPSwitch"
        return {"decision": {"kind": "tool_call", "tool": tool, "arguments": {}},
                "llm_ms": 5.0, "log_fields": {"decision_head": "mock_confirm_demo"}}

    return eot, tools, decide, TurnConfirmGate(mock=True)


@app.post("/api/session/stream")
def session_stream():
    """Replay scripted ASR updates in real time through TurnSession; one event per SSE message.

    也接受 `confirm_rounds={"round1": "...", "round2": "..."}`（对话式二次确认演示用）：
    在服务端把它展开成"第一轮 + 二轮回"的 ASR 时间线回放，二轮回在 CONFIRM_WAIT 内到达。
    """
    from gate.turn_session import TurnConfig, TurnSession
    body = request.get_json()
    updates = body.get("updates") or []
    if not isinstance(updates, list) or not updates:
        updates = []
    confirm_rounds = body.get("confirm_rounds")
    if confirm_rounds:
        updates, vad = _build_confirm_timeline(confirm_rounds)
    else:
        vad = body.get("vad") or []
    if not updates:
        return jsonify({"error": "updates or confirm_rounds required"}), 400
    config = dict(body.get("config") or {})
    if "tool_set" in body:
        config["tool_set"] = body["tool_set"]
    if confirm_rounds:
        # 确认轮演示走离线 mock 链，判停后要留足等二轮回的窗口，避免超时兜底抢跑。
        config.setdefault("think_wait_ms", 20000)
        config.setdefault("max_silence_ms", 800)
        config.setdefault("confirm_silence_ms", 200)
    try:
        cfg = TurnConfig.from_dict(config)
    except (TypeError, ValueError) as error:
        return jsonify({"error": type(error).__name__ + ": " + str(error)}), 400
    timeline = sorted([{"t_ms": item["t_ms"], "type": "asr", "text": item["text"],
                        "final": item.get("final", False)} for item in updates] +
                      [{"t_ms": item["t_ms"], "type": "vad", "vad_kind": item["kind"]}
                       for item in vad],
                      key=lambda item: item["t_ms"])
    queue = Queue()
    session_id = "ui-" + str(int(time.time() * 1000))
    kwargs = {"session_id": session_id, "warm_fn": warm_slow_model}
    if confirm_rounds:
        # 确认轮演示用离线 mock 注入链：判定稳定、不依赖真实 KEV/LLM
        round1_text = (confirm_rounds.get("round1") or "").strip()
        round2_text = (confirm_rounds.get("round2") or "").strip()
        eot, tools, decide, gate = _confirm_mock_chain(round1_text, round2_text)
        kwargs.update(eot_fn=eot, select_tools_fn=tools, decide_fn=decide, confirm_gate=gate,
                      warm_fn=None)
    session = TurnSession(cfg, queue.put, **kwargs)

    def produce():
        try:
            began = time.monotonic() * 1000
            for item in timeline:
                while True:
                    now = time.monotonic() * 1000 - began
                    if now >= item["t_ms"] or session.turn_done:
                        break
                    session.tick(min(now, item["t_ms"]))
                    time.sleep(0.005)
                if session.turn_done:
                    break
                if item["type"] == "asr":
                    session.on_asr(item["t_ms"], item["text"], item["final"])
                else:
                    session.on_vad(item["t_ms"], item["vad_kind"])
            deadline = time.monotonic() * 1000 - began + 20000
            while not session.turn_done and time.monotonic() * 1000 - began < deadline:
                session.tick(time.monotonic() * 1000 - began)
                time.sleep(0.005)
            if not session.turn_done:
                queue.put({"event": "error", "t_ms": time.monotonic() * 1000 - began,
                           "text_version": session.version, "error": "turn did not confirm before the deadline"})
        except Exception as error:
            queue.put({"event": "error", "t_ms": 0, "text_version": 0,
                       "error_type": type(error).__name__, "error": str(error)})
        finally:
            queue.put(None)

    def events():
        Thread(target=produce, daemon=True).start()
        while True:
            event = queue.get()
            if event is None:
                break
            yield f"event: {event['event']}\ndata: {json.dumps(event, ensure_ascii=False, allow_nan=False)}\n\n"

    return Response(stream_with_context(events()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


MIC_CASE_HELP = "整轮（ASR 文本更新 + 时间戳 + 是否说完标注）保存到 " + str(MIC_CASES)
MIC_SESSIONS = {}          # session_id -> {session, asr_log, tool_set}: keeps save working after close
MIC_SESSION_KEEP = 8


# ---------- 双席（C1：两路干净 ASR → 两席各自判停/状态机 → 各自一次决策） ----------

DUAL_SEATS = ("D", "P")
DUAL_MIC_OF_SEAT = {"D": "mic-D", "P": "mic-P"}
DUAL_SEAT_NAME = {"D": "席 D · 主驾", "P": "席 P · 副驾"}


def _script_timeline(text, speed=6.0):
    """`文本|600|文本` → (ASR 更新, VAD)；停顿标记同时切一个 ASR 段。

    与前端旧 buildScript 同一规则，但只保留这一份实现（服务端展开脚本，UI 只发原文）。
    """
    updates, vad = [], [{"t_ms": 0, "kind": "speech_start"}]
    plain, t = "", 0.0
    for chunk in re.split(r"\|(\d+)\|", text):
        if re.fullmatch(r"\d+", chunk or ""):
            vad.append({"t_ms": round(t), "kind": "speech_end"})
            t += float(chunk)
            vad.append({"t_ms": round(t), "kind": "speech_start"})
            plain = ""
            continue
        for char in chunk:
            t += 1000.0 / speed
            plain += char
            updates.append({"t_ms": round(t), "text": plain, "final": False})
    if updates:
        updates[-1]["final"] = True
        vad.append({"t_ms": updates[-1]["t_ms"] + 20, "kind": "speech_end"})
    return updates, vad


def _dual_timeline(payload):
    """把 `seats` 归一成合并时间线（同刻 D 在前）+ 每席计划，供 /api/session/dual/stream 回放。

    seats 支持三种写法（可混用）：
      {"D": "导航回家"}                                  # 脚本简写
      {"D": {"script": "导航回家", "speed": 6}}           # 脚本 + 语速
      {"D": {"updates": [...], "vad": [...]}}            # 与单席 /api/session/stream 同形状
    """
    seats = payload.get("seats")
    if not isinstance(seats, dict) or not seats:
        raise ValueError('seats required: {"D": {"script": "…"}, "P": {"script": "…"}}')
    unknown = sorted(key for key in seats if key not in DUAL_SEATS)
    if unknown:
        raise ValueError("unknown seat id: " + ", ".join(unknown) + "（只有 D / P 两席）")
    timeline, plan = [], {}
    for seat in DUAL_SEATS:
        item = seats.get(seat)
        if item is None:
            continue
        if isinstance(item, str):
            item = {"script": item}
        if not isinstance(item, dict):
            raise ValueError("seat " + seat + " 需要是字符串或对象")
        if "script" in item:
            speed = float(item.get("speed") or payload.get("speed") or 6.0)
            if not 1 <= speed <= 20:
                raise ValueError("speed 需要在 1–20 字/秒")
            script = str(item.get("script") or "").strip()
            if not script:
                continue                       # 空文本 = 本席没有输入（不是「说了一句空话」）
            updates, vad = _script_timeline(script, speed)
        else:
            updates, vad = item.get("updates") or [], item.get("vad") or []
            if not isinstance(updates, list) or not isinstance(vad, list):
                raise ValueError("seat " + seat + " 的 updates/vad 需要是数组")
        rows = []
        for update in updates:
            rows.append({"t_ms": int(update["t_ms"]), "type": "asr", "seat": seat,
                         "text": str(update.get("text") or ""), "final": bool(update.get("final"))})
        for mark in vad:
            rows.append({"t_ms": int(mark["t_ms"]), "type": "vad", "seat": seat,
                         "kind": str(mark["kind"])})
        if not rows:
            continue
        timeline.extend(rows)
        plan[seat] = {"name": DUAL_SEAT_NAME[seat], "mic_id": DUAL_MIC_OF_SEAT[seat],
                      "script": item.get("script").strip() if isinstance(item.get("script"), str) else item.get("script"),
                      "updates": len(updates), "vad": len(vad)}
    if not plan:
        raise ValueError("seats 里没有任何 ASR 更新：至少一路要有文本")
    # 同刻（t_ms 相同）按 D→P 顺序派发：单写者队列里主驾席先入席（与 C1「同刻取主驾席」一致）
    timeline.sort(key=lambda row: (row["t_ms"], DUAL_SEATS.index(row["seat"])))
    return timeline, plan


def _dual_mock_chain(complete_texts=None):
    """双席演示的离线注入口：整句到齐才判 complete、选工具按关键词、决策恒 tool_call。

    与确认轮 demo 同一条思路（不连真实 KEV/LLM），只为把「两席并行且互不串席」演示稳定。

    2026-11-04 去窗：确认窗不再替判停吸收前缀误判，假判停再对任何文本都报 complete，第一个字
    （如「把」）就会让决策当刻落地，演示与用例都会失真。所以假判停只在拿到该席的整句时判
    complete，其余判 wait（继续听）——形状与真判停「说到整句才收口」一致。`complete_texts`
    由调用方按回放脚本给出（去窗后最短可执行输入就是整句）。
    """
    from confirm_orchestrator import TurnConfirmGate
    from shared.text import normalize

    wanted = {normalize(text) for text in (complete_texts or []) if normalize(text)}

    def eot(text):
        if normalize(text).endswith(("那个", "那")):
            # 演示「还没说完」这一去处：判停说被截断（不能确认）→ 闸门先不开；
            # 主链里静音窗到上限（默认 1.5s）才把这一版收口当说完。
            return {"complete": 0.06, "cut": 0.90, "wait": 0.04, "label": "cut",
                    "confidence": 0.5, "margin": -0.8, "wall_ms": 5.0, "kev_ms": 5.0}
        if wanted and normalize(text) not in wanted:
            # 还没说到整句：判「没说 yet」继续听（wait 档），不抢跑也不落地。
            return {"complete": 0.05, "cut": 0.10, "wait": 0.85, "label": "wait",
                    "confidence": 0.95, "margin": -0.80, "wall_ms": 5.0, "kev_ms": 5.0}
        return {"complete": 0.9, "cut": 0.08, "wait": 0.02, "label": "complete",
                "confidence": 0.95, "margin": 0.65, "wall_ms": 5.0, "kev_ms": 5.0}

    def tools(text, tool_set, context):
        if tool_set != TOOL_SET:
            raise ValueError("dual demo uses " + TOOL_SET)
        if any(key in text for key in ("空调", "温度", "冷", "热")):
            name = "car_hvac_set_temp"
        elif any(key in text for key in ("音乐", "歌", "媒体", "音量")):
            name = "car_sound_media_volume"
        else:
            name = "car_navi_to_destination"
        return {"scores": {"tool:" + name: 0.9}, "selected": [name],
                "schemas": [{"name": name, "description": "d", "parameters": {"type": "object"}}],
                "native_schemas": None, "domains": None,
                "timing": {"stage1_ms": 5.0, "stage2_ms": 0.0, "total_ms": 5.0}}

    def decide(utterance, selected, schemas, tool_set, context, **kwargs):
        tool = selected[0] if selected else "car_navi_to_destination"
        # 期三（2026-11-16）离线替身：把「这边正在说」这一段拉长到可观察（默认 0 = 与改动前一样）。
        # 只在离线演示链上生效；`HUB_MOCK_DECIDE_MS=1200` 时插话才落得进「开闸之后正在准备说」的窗口
        # （验收卡：需要「这边正在说」时用台面出字 + 播报桩模拟）。不接真模型、不改真链。
        delay_ms = float(os.environ.get("HUB_MOCK_DECIDE_MS", "0") or 0)
        if delay_ms > 0:
            time.sleep(delay_ms / 1000.0)
        return {"decision": {"kind": "tool_call", "tool": tool, "arguments": {}},
                "llm_ms": 5.0 + delay_ms, "log_fields": {"decision_head": "mock_dual_demo"}}

    return eot, tools, decide, TurnConfirmGate(mock=True)


@app.post("/api/session/dual/stream")
def dual_stream():
    """双席回放：两路 ASR 各进各席，单写者派发，事件逐条 SSE 吐回（带 seat_id / mic_id）。

    请求体：
      seats   同 _dual_timeline（必填，至少一路有内容）
      config  TurnConfig 覆盖（与单席同字段）
      mock    true = 离线判停/选工具/决策（不连 KEV/LLM）
      deadline_ms  等两席收口的上限（默认 30000；超时也发 duo_done，timed_out=true）
    """
    from asr.dual_seat import DualSeatSession
    from gate.turn_session import EVIDENCE_ROOT, TurnConfig
    # 三泳道最小可见：双席链路挂会话脑（任务判断 / 单一决策 / 执行落地），编排层事件才发得出来。
    from agent.orchestra import SessionBrain
    from agent.task_entity import TaskBoard
    from agent.relevance import build_relevance_fn
    body = request.get_json(silent=True) or {}
    try:
        timeline, plan = _dual_timeline(body)
        config = dict(body.get("config") or {})
        if "tool_set" in body:
            config["tool_set"] = body["tool_set"]
        cfg = TurnConfig.from_dict(config)
        mock = bool(body.get("mock"))
        deadline_ms = int(body.get("deadline_ms") or 30000)
    except (TypeError, ValueError) as error:
        return jsonify({"error": type(error).__name__ + ": " + str(error)}), 400
    session_id = "duo-ui-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    queue = Queue()
    mind = _hub_mind(session_id)
    gates = _hub_gates(mind)
    ledger = _hub_ledger()
    # 记忆：只有**真链**才写真云端（回放/离线 mock 不把演示语料灌进同一份用户记忆）。
    memory = None if mock else _hub_memory(session_id)
    if memory is not None:      # 日志只报「开没开 / 挂在谁 / 凭据来源」，不带任何密钥值
        app.logger.warning("memory(memos): %s", memory.status())
    kwargs = {"seat_cfg": cfg, "on_event": queue.put, "session_id": session_id,
              "evidence_root": EVIDENCE_ROOT,
              "seat_extra": _hub_seat_extra(gates, ledger, mind, memory)}
    if mock:
        full_texts = [rows[-1]["text"] for seat in plan
                      for rows in [[row for row in timeline
                                    if row["seat"] == seat and row["type"] == "asr"]] if rows]
        eot, tools, decide, gate = _dual_mock_chain(full_texts)
        kwargs.update(eot_fn=eot, select_tools_fn=tools, confirm_gate=gate)
        # 离线 mock：相关打分也走离线通道（首候选命中），不连 8009，任何机器都能复现完整编排流。
        brain = SessionBrain(decide, task_board=TaskBoard(relevance_fn=build_relevance_fn(mock=True)))
        kwargs["brain"] = brain
    else:
        from core_pipeline_run import decide as real_decide
        kwargs.update(warm_fn=warm_slow_model)
        # 真链：真 decide + 真相关打分（判停那个模型 8009）；8009 未接/打分出错由 TaskBoard.match
        # 兜住记 scorer_error → 新开任务，不阻塞决策。
        brain = SessionBrain(real_decide, task_board=TaskBoard(relevance_fn=build_relevance_fn(mock=None)))
        kwargs["brain"] = brain
    session = DualSeatSession(**kwargs)
    active = tuple(plan)

    def all_done():
        return all(session.seats[seat].turn_done for seat in active)

    def produce():
        began = time.monotonic() * 1000
        timed_out = False
        try:
            for row in timeline:
                while True:
                    now = time.monotonic() * 1000 - began
                    if now >= row["t_ms"] or all_done():
                        break
                    session.tick(int(now))
                    time.sleep(0.005)
                if all_done():
                    break
                mic_id = DUAL_MIC_OF_SEAT[row["seat"]]
                if row["type"] == "asr":
                    # 这一份读心先刷新场景（两路一起，不依赖中枢）；中枢在出字那一刻取一次已有的那份。
                    mind.ingest(text=row["text"], final=row["final"], seat=row["seat"])
                    session.on_asr(row["t_ms"], mic_id, row["text"], row["final"])
                else:
                    session.on_vad(row["t_ms"], mic_id, row["kind"])
            deadline = began + deadline_ms
            while not all_done() and time.monotonic() * 1000 < deadline:
                session.tick(int(time.monotonic() * 1000 - began))
                time.sleep(0.005)
            timed_out = not all_done()
        except Exception as error:
            queue.put({"event": "error", "t_ms": time.monotonic() * 1000 - began, "seat_id": None,
                       "error_type": type(error).__name__, "error": str(error)})
        finally:
            # 会话收口：先把脑里还卡在归并窗/在途的 AU 结算掉，summary.brain 的 task_eq/iu_eq 才平，
            # 前端「任务账 / 落地账」对账读到的才是收口后的真值（settle 幂等，再 close 一次无副作用）。
            if getattr(session, "brain", None) is not None:
                session.brain.settle()
            summary = session.summary()
            queue.put({"event": "duo_done", "t_ms": round(time.monotonic() * 1000 - began, 1),
                       "timed_out": timed_out, "mock": mock, "plan": plan,
                       "summary": summary})
            # 合并层落盘（§4.3-2）：duo_done.summary（含 brain 任务账）→ evidence/turn-session/<session_id>/duo_summary.json。
            # 单席 timeline.json 各只有一席，这是唯一持久化的合并副本；关页面/进程后仍可取。
            try:
                duo_dir = Path(EVIDENCE_ROOT) / session_id
                duo_dir.mkdir(parents=True, exist_ok=True)
                (duo_dir / "duo_summary.json").write_text(
                    json.dumps(summary, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
            except Exception:
                pass
            try:
                session.close()
            except Exception:
                pass
            queue.put(None)

    def events():
        yield "event: duo_accepted\ndata: " + json.dumps(
            {"session_id": session_id, "mock": mock, "tool_set": cfg.tool_set, "plan": plan},
            ensure_ascii=False, allow_nan=False) + "\n\n"
        Thread(target=produce, daemon=True).start()
        while True:
            event = queue.get()
            if event is None:
                break
            yield f"event: {event.get('event', 'seat_event')}\ndata: " + json.dumps(
                event, ensure_ascii=False, allow_nan=False) + "\n\n"

    return Response(stream_with_context(events()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


def register_mic_session(session_id, session, asr_log, tool_set):
    MIC_SESSIONS[session_id] = {"session": session, "asr_log": asr_log, "tool_set": tool_set}
    while len(MIC_SESSIONS) > MIC_SESSION_KEEP:
        MIC_SESSIONS.pop(next(iter(MIC_SESSIONS)))


def write_mic_case(holder, label_complete):
    session = holder["session"]
    MIC_CASES.mkdir(parents=True, exist_ok=True)
    path = MIC_CASES / (session.session_id + ".jsonl")
    record = {"id": session.session_id, "created_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
              "session_id": session.session_id, "sample_rate": MIC_SAMPLE_RATE, "frame_ms": MIC_FRAME_MS,
              "label_complete": bool(label_complete), "tool_set": holder["tool_set"],
              "config": session.cfg.__dict__, "asr_updates": holder["asr_log"], "events": session.events,
              "summary": session.summary(), "decision": session.decision,
              "timeline_path": str(session.timeline_path) if session.timeline_path else None}
    with path.open("w") as stream:
        stream.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")
    return path


@app.post("/api/session/mic/save")
def mic_save():
    """Save the last microphone turn as a jsonl case; works even after the WebSocket closed."""
    body = request.get_json() or {}
    holder = MIC_SESSIONS.get(body.get("session_id") or "")
    if holder is None:
        return jsonify({"error": "unknown or expired mic session; press 开始说话 first"}), 404
    path = write_mic_case(holder, body.get("label"))
    return jsonify({"path": str(path), "label_complete": bool(body.get("label"))})


@app.route("/api/session/mic", websocket=True)
def mic_session():
    """Live microphone mode: browser pushes 16 kHz/20 ms PCM frames, we run ASR -> TurnSession."""
    from gate.turn_session import TurnConfig, TurnSession
    from asr.asr_stream import ASRStream
    ws = WsServer.accept(request.environ)
    out = Queue()                      # (kind, payload) -> sender thread
    pcm_in = Queue()                   # raw PCM frames from the browser
    pending = []                       # ASR updates awaiting the driver thread
    asr_log = []                       # every ASR update, for "save as case"
    control = {"stop": False, "closed": False, "asr_done": False, "stop_ack": False, "stop_sent": False}
    session_id = "mic-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    cfg = TurnConfig()
    tool_set = {"value": cfg.tool_set}
    started = time.monotonic() * 1000
    now_ms = lambda: time.monotonic() * 1000 - started
    session = None

    def sender():
        # Only this thread writes to the socket.
        while not control["closed"]:
            try:
                kind, payload = out.get(timeout=0.2)
            except Empty:
                continue
            try:
                ws.send(json.dumps({"kind": kind, "data": payload}, ensure_ascii=False, allow_nan=False))
            except Exception:
                break

    def asr_worker():
        stream = None
        try:
            stream = ASRStream(MIC_SAMPLE_RATE)
            while True:
                try:
                    frame = pcm_in.get(timeout=0.05)
                except Empty:
                    frame = None
                if frame:
                    stream.push(frame)
                for text, final, end_ms in stream.poll():
                    update = {"t_ms": int(now_ms()), "text": text, "final": bool(final), "vad_end_ms": end_ms}
                    pending.append(update)
                    asr_log.append(update)
                    out.put(("asr", update))
                if control["stop"] and pcm_in.empty():
                    break
            for text, final, end_ms in stream.finish():
                update = {"t_ms": int(now_ms()), "text": text, "final": bool(final), "vad_end_ms": end_ms}
                pending.append(update)
                asr_log.append(update)
                out.put(("asr", update))
        except Exception as error:
            out.put(("error", {"stage": "asr", "error_type": type(error).__name__, "error": str(error)}))
        finally:
            if stream is not None:
                stream.close()
            control["asr_done"] = True

    def driver():
        # All TurnSession state changes happen on this thread; ASR/driver hand off through `pending`.
        while not session.turn_done and not control["closed"]:
            while pending:
                update = pending.pop(0)
                session.on_asr(update["t_ms"], update["text"], update["final"])
            if control["stop"] and control["asr_done"] and not control["stop_sent"]:
                # 「停止」= 显式收尾（multi-llm spec §3.5 stop(now_ms)）。等 ASR 把最后一句冲完再收，
                # 否则 final 文本会被丢掉：先让它进 suffix，再由 stop() 走完最后一次决策。
                control["stop_sent"] = True
                session.stop(now_ms())
            session.tick(now_ms())
            time.sleep(0.005)

    holder = {"session": None, "asr_log": asr_log, "tool_set": tool_set["value"]}

    Thread(target=sender, daemon=True).start()
    out.put(("ready", {"session_id": session_id, "sample_rate": MIC_SAMPLE_RATE, "frame_ms": MIC_FRAME_MS,
                       "help": MIC_CASE_HELP}))
    try:
        while True:
            message = ws.receive(timeout=0.5)
            if message is None:
                continue
            if isinstance(message, (bytes, bytearray)):
                if session is None:
                    session = TurnSession(cfg, lambda event: out.put(("event", event)), session_id=session_id,
                                          warm_fn=warm_slow_model)
                    holder["session"] = session
                    register_mic_session(session_id, session, asr_log, tool_set["value"])
                    Thread(target=driver, daemon=True).start()
                    Thread(target=asr_worker, daemon=True).start()
                pcm_in.put(bytes(message))
                continue
            command = json.loads(message)
            if command.get("cmd") == "config":
                overrides = dict(command.get("config") or {})
                if command.get("tool_set"):
                    overrides["tool_set"] = command["tool_set"]
                try:
                    cfg = TurnConfig.from_dict(overrides)
                except (TypeError, ValueError) as error:
                    out.put(("error", {"stage": "config", "error_type": type(error).__name__, "error": str(error)}))
                else:
                    tool_set["value"] = cfg.tool_set
                    holder["tool_set"] = cfg.tool_set
                    out.put(("config", {"config": {key: value for key, value in cfg.__dict__.items()}}))
            elif command.get("cmd") == "stop":
                control["stop"] = True
                out.put(("stopping", {"asr_done": control["asr_done"]}))
            elif command.get("cmd") == "save":
                if holder["session"] is None:
                    out.put(("error", {"stage": "save", "error_type": "ValueError",
                                       "error": "本轮还没有音频，没有可保存的用例"}))
                else:
                    path = write_mic_case(holder, command.get("label"))
                    out.put(("saved", {"path": str(path), "label_complete": bool(command.get("label"))}))
    except Exception as error:
        out.put(("error", {"stage": "socket", "error_type": type(error).__name__, "error": str(error),
                           "trace": traceback.format_exc().splitlines()[-3:]}))
    finally:
        control["closed"] = True
        try:
            ws.close()
        except Exception:
            pass
    return ""


# ---- 中枢并发闸门（wiki [[中枢并发闸门]]，2026-11-10）：**一个会话一份读心** + 每席一份闸门 -----
# 一次出字：这一份读心（同时带主驾 / 副驾两路出字 + 画面）给「此刻已有的场景」→ 中枢取一次 →
# 判停（本席 TurnSession）与接不接（`hub_accept.build_accept_fn()`，默认本机临时规则、未拍板）并发
# → 只在「判定说完」那一刻开闸。两席的闸门取的是**同一份**场景（2026-11-11 拍「两路 ASR 输入」）。
# 开闸前不进 Agent（工具披露 / 决策都被 TurnSession 的 `_gate_open_version` 挡住）。
# 接不接要换模型/服务时只换 `build_accept_fn()` 的实现，页面照旧读 `accept_source` 显示实际用了什么。
def _hub_mind(prefix, model_id=None):
    """一个会话一份读心（两路共用）。`model_id` = 页面选的读心档：给了就建真模型引擎。

    不传时仍然是离线规则替身 → 文本回放路径（/api/session/dual/stream）行为一点不变。
    不再「每席一份」：那样两路互相看不见对方的话，读出来的场景只有一半（本卡要改的就是这条）。
    """
    from mind.mind_reading import MindReading
    engine = _mind_engine(model_id) if model_id else None
    return MindReading(session_id=prefix + "-mind", engine=engine)


from app.assembly import (hub_gates as _hub_gates, hub_ledger as _hub_ledger,
                          hub_memory as _hub_memory, hub_seat_extra as _hub_seat_extra)


# ---- 读心模块（原拒答 / 上不上场）：ASR + 视频帧进 → 场景理解 + 场景快照 ----------------
# 只有「读当前场景」这一件事：不判停、不判接不接、不进 Agent、不发起握手。产出的场景由中枢
# 在开闸前取一次（`scene_snapshot()`，不调模型、不等待）。
MIND_SESSIONS = None
# 页面可选引擎：目录在 mind_llm.MODEL_CHOICES（三选一：`cc/glm-5.2` / `cc/glm-5.3-flash` /
# `deepseek-v4.1-flash` 关思考），默认 `deepseek-v4.1-flash`。每档引擎按 id 懒建并缓存 → 页面上切模型
# 不用重启 8765；请求带 `model`，响应 `engine.vision.capability` 标出该档能不能读图。
# 凭据只从进程环境 / ~/.dsh 私有凭据位置读，不打印、不落盘（见 src/mind_llm.py）。
# 测试 seam / 强制离线：`MIND_FORCE_OFFLINE=True`（或 `MIND_ENGINE=rule`）→ 所有档走离线规则。
MIND_FORCE_OFFLINE = False
MIND_ENGINE = None            # 默认档引擎（缓存里的一份，便于日志/兼容旧引用）
MIND_ENGINE_READY = False     # 默认档是否已建过
MIND_ENGINES: dict[str, object] = {}
MIND_RULE_MODES = {"rule", "offline", "none", "0", "false"}


def _mind_rule_forced() -> bool:
    return MIND_FORCE_OFFLINE or os.environ.get("MIND_ENGINE", "auto").strip().lower() in MIND_RULE_MODES


def _mind_engine(model_id: str | None = None):
    """按页面选的模型 id 取引擎（缓存）；未知 id 抛 ValueError（tick 返回 400）。

    缺凭据 / `MIND_ENGINE=rule` → 该档返回 None，上层回落离线规则（页面不白屏、不静默换模型）。
    每档独立缓存：先被页面选中的那一档不会把其它档一起拖成离线。
    """
    global MIND_ENGINE, MIND_ENGINE_READY
    from mind.mind_llm import MODEL_BY_ID, MODEL_DEFAULT_ID, engine_for_model
    model_id = (model_id or "").strip() or MODEL_DEFAULT_ID
    if model_id not in MODEL_BY_ID:
        raise ValueError("model 不在读心三选一内：" + model_id)
    if _mind_rule_forced():
        return None
    if model_id not in MIND_ENGINES:
        try:
            MIND_ENGINES[model_id] = engine_for_model(model_id)
        except Exception as error:  # 显式要求真模型但配置缺失：页面仍可开，engine 里写 offline
            app.logger.warning("mind engine %s unavailable: %s", model_id, type(error).__name__)
            MIND_ENGINES[model_id] = None
        if model_id == MODEL_DEFAULT_ID:
            MIND_ENGINE = MIND_ENGINES[model_id]
            MIND_ENGINE_READY = True
    return MIND_ENGINES[model_id]


def _mind_registry():
    global MIND_SESSIONS
    from mind.mind_reading import MindRegistry
    if MIND_SESSIONS is None:
        # registry 不钉死引擎：每拍按页面选的 model 传进来（同一引擎幂等，不重置本席）
        MIND_SESSIONS = MindRegistry(engine=None)
    return MIND_SESSIONS


def _mind_engine_info(model_id: str | None = None):
    from mind.mind_llm import MODEL_BY_ID, MODEL_DEFAULT_ID
    engine = _mind_engine(model_id)
    if engine is None:
        mid = (model_id or "").strip() or MODEL_DEFAULT_ID
        spec = MODEL_BY_ID.get(mid) or {}
        return {"name": "fast_multimodal_stub", "model": mid, "mode": "offline",
                "label": spec.get("label", mid), "vision_label": "离线规则不读图",
                "vision": {"capability": "unknown", "status": "unknown",
                           "note": "离线规则替身；换真模型时协议不变"},
                "source": "rules_offline",
                "note": "离线规则替身（未配置真模型或 MIND_ENGINE=rule）"}
    try:
        info = engine.describe()
    except Exception:
        info = {"name": model_id or MODEL_DEFAULT_ID, "mode": "litellm_vl", "base": ""}
    info["source"] = "llm"
    return info


@app.get("/mind")
def mind_page():
    return send_file(Path(__file__).with_name("mind.html"))


@app.get("/api/mind/demos")
def mind_demos():
    from mind.mind_llm import MODEL_CHOICES, MODEL_DEFAULT_ID
    from mind.mind_reading import DEMOS
    return jsonify({"demos": DEMOS, "models": [dict(item) for item in MODEL_CHOICES],
                    "default_model": MODEL_DEFAULT_ID, "engine": _mind_engine_info()})


@app.post("/api/mind/tick")
def mind_tick():
    """一次感知节拍：可同时带 ASR 增量与一帧视频（彩色 data URI），返回最新理解 / 判断。

    请求可选 `model`：页面选择器当前选中的引擎 id（缺省 = 线上默认 `deepseek-v4.1-flash`）。
    """
    registry = _mind_registry()

    body = request.get_json(silent=True) or {}
    session_id = str(body.get("session_id") or "")
    if not session_id:
        return jsonify({"error": "session_id required"}), 400
    try:
        engine = _mind_engine(str(body.get("model") or ""))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    frame, frame_error = _frame_body(body.get("frame"))
    if frame_error:
        return jsonify({"error": frame_error}), 400
    asr = body.get("asr") or {}
    text = asr.get("text")
    if text is not None and not isinstance(text, str):
        return jsonify({"error": "asr.text must be a string"}), 400
    session = registry.get(session_id, seat=body.get("seat") or "D", engine=engine)
    snapshot = _mind_ingest(session, frame, text, asr.get("final"))
    return jsonify(snapshot)


# 调试页的探针闸门按 session 缓存：同一 session 的「这一版字」连着几拍读同一份结论。
HUB_PROBES: dict = {}
HUB_EOT_MODES = {"offline", "real", "complete", "cut", "wait"}


def _hub_probe_eot(mode):
    """调试页判停这一路的来源：真 8009（`real`）/ 离线替身（默认）/ 三态强制（演示三去处）。

    `real` 不能直接回 `score_eot` 的原始分数：它还缺 `may_confirm`，而闸门只认「能确认的说完」。
    这里按与 TurnSession 同一条 `score` 策略（`EOT_COMPLETE_MIN_SCORE` / `EOT_CONFIDENCE_MIN`）
    把原始分数压成判停结论，避免「页面选真 8009 却永远不开闸」。
    """
    if mode == "real":
        from gate.turn_eot import score_eot
        def real(text):
            scores = score_eot(text)
            from gate.turn_session import TurnConfig
            cfg = TurnConfig()
            complete = scores.get("complete") or 0.0
            if complete >= cfg.eot_complete_min:
                label, may_confirm = "complete", True
                may_start_early = (scores.get("confidence") or 0.0) >= cfg.eot_confidence_min
            else:
                label = "cut" if (scores.get("cut") or 0.0) >= (scores.get("wait") or 0.0) else "wait"
                may_confirm, may_start_early = False, False
            return {**scores, "label": label, "may_confirm": may_confirm,
                    "may_start_early": may_start_early}
        return real

    def offline(text):
        t = (text or "").strip()
        if mode in ("complete", "cut", "wait"):
            label = {"complete": "complete", "cut": "cut", "wait": "wait"}[mode]
        elif t.endswith(("吗", "呢", "？", "?")) or not t:
            label = "wait"
        elif t.endswith(("那个", "这个", "然后", "就是", "那个…")):
            label = "cut"
        else:
            label = "complete"
        scores = {"complete": 0.94 if label == "complete" else 0.06,
                  "cut": 0.9 if label == "cut" else 0.05,
                  "wait": 0.9 if label == "wait" else 0.04}
        # 与 TurnSession 的 `_eot_verdict` 同口径：只有「说完」能确认；截断/没说完都不能开闸。
        return {**scores, "label": label,
                "may_confirm": label == "complete", "may_start_early": label == "complete",
                "confidence": 0.9 if label == "complete" else 0.5,
                "wall_ms": 1.0, "source": "offline"}

    return offline


@app.post("/api/hub/tick")
def hub_tick():
    """调试页一拍：读心刷新场景 → 中枢同一版字并发「判停 + 接不接」→ 闸门结论。

    请求：{session_id, seat, model, asr:{text,final}, frame:{image,w,h,brightness,motion},
           eot:"offline|real|complete|cut|wait"}（frame.image = 页面采好的彩色帧，原样透传给模型）
    返回：{mind, hub, gate, accept_source, eot_source}
    同一 session 里文字一变就算新一版（版本 +1），每个版本只取一次场景、只判一次闸门。
    """
    from gate.hub_accept import accept_meta, build_accept_fn
    from gate.hub_gate import ROUTE_WAIT, GateKeeper
    body = request.get_json(silent=True) or {}
    session_id = str(body.get("session_id") or "")
    if not session_id:
        return jsonify({"error": "session_id required"}), 400
    seat = str(body.get("seat") or "D")
    if seat not in DUAL_SEATS:
        return jsonify({"error": "seat must be D or P"}), 400
    eot_mode = str(body.get("eot") or "offline")
    if eot_mode not in HUB_EOT_MODES:
        return jsonify({"error": "eot must be one of " + ", ".join(sorted(HUB_EOT_MODES))}), 400
    try:
        engine = _mind_engine(str(body.get("model") or ""))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    frame, frame_error = _frame_body(body.get("frame"))
    if frame_error:
        return jsonify({"error": frame_error}), 400
    asr = body.get("asr") or {}
    text = asr.get("text")
    if text is not None and not isinstance(text, str):
        return jsonify({"error": "asr.text must be a string"}), 400

    mind = _mind_registry().get(session_id, seat=seat, engine=engine)
    mind_snap = _mind_ingest(mind, frame, text, asr.get("final"), seat=seat)

    probe = HUB_PROBES.get(session_id)
    if probe is None or probe["seat"] != seat or probe["eot_mode"] != eot_mode:
        if probe is not None:
            probe["gate"].close()
        probe = {"gate": GateKeeper(accept_fn=build_accept_fn(), scene_fn=mind.scene_snapshot,
                                    seat=seat, eot_fn=_hub_probe_eot(eot_mode)),
                 "version": 0, "text": "", "seat": seat, "eot_mode": eot_mode,
                 "started": time.monotonic() * 1000}
        HUB_PROBES[session_id] = probe
    now = int(time.monotonic() * 1000 - probe["started"])
    current = (text or "").strip()
    if current and current != probe["text"]:
        probe["version"] += 1
        probe["text"] = current
        probe["gate"].on_text(probe["version"], text, now, final=bool(asr.get("final")))
    probe["gate"].poll(now)
    verdict = probe["gate"].gate(probe["version"], now) if probe["version"] else None
    # 调试页这一拍的中枢也照同一条口径：闸门判定「这句收住」的那一刻（判停说完 / 静音窗当说完），
    # 读心用这句最完整的字 + 当时画面读一次。收住与「接不接」是不是拿到了无关（那种情况闸门给
    # accept_unavailable，但这句已经收住），只有「还没说完 / 判停没回来」（wait）不算收住。
    # 读心按同一份输入只读一次，闸门每拍取结论不会重复发。
    if verdict and verdict.get("route") != ROUTE_WAIT:
        mind.on_utterance_close(seat, why=str(verdict.get("state") or ""))
    return jsonify({"mind": mind_snap, "hub": probe["gate"].snapshot(), "gate": verdict,
                    "accept_source": accept_meta(),
                    "eot_source": {"mode": eot_mode,
                                   "label": "真判停 8009" if eot_mode == "real" else "离线替身"}})


@app.post("/api/hub/reset")
def hub_reset():
    body = request.get_json(silent=True) or {}
    session_id = str(body.get("session_id") or "")
    probe = HUB_PROBES.pop(session_id, None) if session_id else None
    if probe is not None:
        probe["gate"].close()
    return jsonify({"ok": True, "dropped": bool(probe)})


@app.post("/api/mind/reset")
def mind_reset():
    registry = _mind_registry()
    body = request.get_json(silent=True) or {}
    session_id = str(body.get("session_id") or "")
    if session_id:
        registry.drop(session_id)
    return jsonify({"ok": True})


# ---- 读心页真麦：只做「音频 → ASR 文本」，不建 TurnSession -------------------------------
# 与主 UI mic 模式同一条识别后端（asr_stream.ASRStream / CLOUD_ASR_*，凭据只从环境读、不打印）：
# 浏览器推 16 kHz / 20 ms PCM，本端点只回 partial/final，字再由页面按 tick 送进读心模块。
# 读心只吃字，所以这里不建 TurnSession、不走判停/选工具/决策，也不往 Agent 递东西。
MIND_ASR_FACTORY = None      # 测试 seam：注入 (rate) -> {push, poll, finish, close}；None = 真 ASRStream


def _mind_asr_factory():
    from asr.asr_stream import ASRStream
    if MIND_ASR_FACTORY is not None:
        return MIND_ASR_FACTORY
    return lambda rate: ASRStream(rate)


@app.route("/api/mind/asr", websocket=True)
def mind_asr_session():
    """读心页真麦通道：PCM 进 → 流式 ASR 文本出（partial 逐字 / final 落段）。"""
    ws = WsServer.accept(request.environ)
    out = Queue()
    pcm_in = Queue()
    control = {"stop": False, "closed": False, "started": False}
    session_id = "mind-asr-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    started = time.monotonic() * 1000
    now_ms = lambda: int(time.monotonic() * 1000 - started)

    def sender():
        # 只有这个线程写 socket。
        while not control["closed"]:
            try:
                kind, payload = out.get(timeout=0.2)
            except Empty:
                continue
            try:
                ws.send(json.dumps({"kind": kind, "data": payload}, ensure_ascii=False, allow_nan=False))
            except Exception:
                break

    def asr_worker():
        stream = None
        try:
            stream = _mind_asr_factory()(MIC_SAMPLE_RATE)
            while True:
                try:
                    frame = pcm_in.get(timeout=0.05)
                except Empty:
                    frame = None
                if frame:
                    stream.push(frame)
                for text, final, end_ms in stream.poll():
                    out.put(("asr", {"t_ms": now_ms(), "text": text, "final": bool(final),
                                     "vad_end_ms": end_ms}))
                if control["stop"] and pcm_in.empty():
                    break
            for text, final, end_ms in stream.finish():
                out.put(("asr", {"t_ms": now_ms(), "text": text, "final": bool(final),
                                 "vad_end_ms": end_ms}))
            out.put(("stopped", {"asr_done": True}))
        except Exception as error:
            out.put(("error", {"stage": "asr", "error_type": type(error).__name__, "error": str(error)}))
        finally:
            if stream is not None:
                try:
                    stream.close()
                except Exception:
                    pass

    Thread(target=sender, daemon=True).start()
    out.put(("ready", {"session_id": session_id, "sample_rate": MIC_SAMPLE_RATE,
                       "frame_ms": MIC_FRAME_MS, "engine": _mind_engine_info()}))
    try:
        while True:
            message = ws.receive(timeout=0.5)
            if message is None:
                continue
            if isinstance(message, (bytes, bytearray)):
                if not control["stop"]:
                    if not control["started"]:
                        control["started"] = True
                        Thread(target=asr_worker, daemon=True).start()
                    pcm_in.put(bytes(message))
                continue
            try:
                command = json.loads(message)
            except (TypeError, ValueError):
                continue
            if isinstance(command, dict) and command.get("cmd") == "stop":
                control["stop"] = True
    except Exception:
        pass
    finally:
        control["closed"] = True
        try:
            ws.close()
        except Exception:
            pass
    return ""


# ---------- 双席 · 真实麦克风（两路独立 ASR → DualSeatSession） ----------
#
# 新增路径（真链），不拆既有的「文本回放双席」 /api/session/dual/stream。
# 每条连接 = 一个通道先验席（mic-D → 席 D / mic-P → 席 P），各自推 16k/20ms PCM：
#
#     browser seat D ──WS──▶ ASRStream-D ──partial/final──▶ DualSeatSession.on_asr(mic-D,…)
#     browser seat P ──WS──▶ ASRStream-P ──partial/final──▶ DualSeatSession.on_asr(mic-P,…)
#                                    （单写者：只由 driver 线程调 session 方法，不串席）
#
# 两路 WS 共用同一个 `session_id`（浏览器打开时生成）即可配成一对；事件广播到两路连接，
# 前端按 `seat_id` 过滤（与文本回放同一套 `dualApply`）。单路连接也能单独收口（=那路没说话）。

# ASR 工厂注入点（测试 seam）：默认真实 `ASRStream`（火山豆包流式 ASR / CLOUD_ASR_*），
# 契约/冒烟测可替换成脚本假 ASR `(sample_rate, seat) -> {push, poll, finish, close}`，离线可复现。
DUAL_MIC_ASR_FACTORY = None      # None → 用 real ASRStream


def _dual_mic_asr_factory():
    from asr.asr_stream import ASRStream
    if DUAL_MIC_ASR_FACTORY is not None:
        return DUAL_MIC_ASR_FACTORY
    return lambda rate, seat: ASRStream(rate)


# 同屏画面：浏览器推**采好的彩色帧**（`frame` = {image: data URI, w, h, brightness, motion}，
# 与 `/api/mind/tick` 同一口径）。送进模型的就是这段 data URI，不重编码、不转灰阶。
# 最近一帧多久内还算「画面在送」——超时按无画面，不拿旧帧冒充当前画面。
DUAL_FRAME_FRESH_MS = 2000


def _frame_body(value):
    """校验并规整一帧：`{image, w, h, brightness, motion}`；不合法返回 (None, 错误文案)。"""
    from mind.mind_llm import FRAME_MAX_SIDE, image_ok
    if value is None:
        return None, None
    if not isinstance(value, dict):
        return None, "frame must be an object {image, w, h, brightness, motion}"
    image = value.get("image")
    if not image_ok(image):
        return None, "frame.image must be a jpeg/png data URI (<= 600KB)"
    width = value.get("w")
    height = value.get("h")
    for side in (width, height):
        if side is not None and (not isinstance(side, (int, float)) or not 0 < int(side) <= FRAME_MAX_SIDE):
            return None, "frame.w/frame.h must be 1..%d" % FRAME_MAX_SIDE
    brightness = value.get("brightness")
    motion = value.get("motion")
    for number in (brightness, motion):
        if number is not None and not isinstance(number, (int, float)):
            return None, "frame.brightness/motion must be numbers"
    source = str(value.get("source") or "").strip()
    if not source:
        source = "test_asset" if bool(value.get("test_asset")) else "camera"
    return {"image": image,
            "w": int(width) if width is not None else None,
            "h": int(height) if height is not None else None,
            "brightness": float(brightness) if brightness is not None else None,
            "motion": float(motion) if motion is not None else None,
            "source": source}, None


def _mind_ingest(session, frame, text, final, seat=None):
    """把一帧 + 一次出字喂进读心（两处入口共用，帧口径只写这一份）。

    `seat` = 这一路是哪一席在出字（两路真麦必须显式给；调试页一次只推一路时用默认席）。
    """
    body = frame or {}
    return session.ingest(text=text, final=bool(final), seat=seat, image=body.get("image"),
                          width=body.get("w"), height=body.get("h"),
                          brightness=body.get("brightness"), motion=body.get("motion"))


# ---------- 开麦路由（两页共用）：选了哪支就必须开哪支 ----------
#
# 2026-11-12 用户实测（内心戏页 17:43–17:51）：主驾选小米耳夹蓝牙、副驾选 Poly Blackwire 有线，
# 人离有线耳机很远，两路音量却一直完全一样、识别也是同一段话 —— 浏览器把两路都开成了同一支
# （系统默认那支）。页面按各席下拉分别要了设备（`getUserMedia + deviceId:{exact}`），服务端
# 也没把一路拷给另一路；问题在「要了 A 却拿到 A 的声音了吗」：设备名 / deviceId 都是浏览器自己
# 报的，可能和实际声音对不上，**不能当证据**。是不是两支只能按实际收到的 PCM 判：
# 每席留最近几秒的 20ms 电平走势，两路是同一份声音时，对齐后走势几乎一模一样（两支不同的麦
# 不会这么齐）。判出「同一份」就当场说清并停下，不静默按一路继续听。
MIC_ROUTE_WINDOW = 150             # 每席留最近 3s（150 × 20ms 帧）
MIC_ROUTE_MIN_FRAMES = 30          # 对齐后至少 0.6s 才判（不够 = 还没听到能分辨的声音）
MIC_ROUTE_CORR_SAME = 0.99         # 同一份声音：对齐后电平走势几乎一模一样
MIC_ROUTE_GAP_SAME = 1.5           # 同一份声音：逐帧电平差也很小（两支不同麦不会这么齐）
MIC_ROUTE_GAP_SPLIT = 4.0          # 差出这个数就是两条不同采集链（同一支麦被开两路差不出 4 dB）
MIC_ROUTE_FLOOR = -70.0            # 低于这个就当安静：安静帧不参与「像不像」的比对（静音对静音比不出什么）
MIC_ROUTE_CORR_SPLIT = 0.90        # 明显不是同一份：走势对不上
MIC_ROUTE_MAX_LAG = 8              # 对齐搜索 ±8 帧（±160ms：两路各自 buffering 的差）
MIC_ROUTE_EVAL_MS = 500            # 多久看一次


def _mic_route_pearson(left, right):
    """两段电平走势的皮尔逊相关；长度不足、或有一路几乎没变化（判不出形状）时给 None。"""
    count = len(left)
    if count < 3:
        return None
    mean_left = sum(left) / count
    mean_right = sum(right) / count
    var_left = sum((value - mean_left) ** 2 for value in left)
    var_right = sum((value - mean_right) ** 2 for value in right)
    if var_left <= 1e-9 or var_right <= 1e-9:
        return None
    cov = sum((a - mean_left) * (b - mean_right) for a, b in zip(left, right))
    return cov / math.sqrt(var_left * var_right)


def mic_route_verdict(left, right):
    """两路最近的电平走势（dBFS/20ms）比一比：同一份声音 / 真好分开 / 还判不了。

    返回 `{status: same_source|separate|unknown, corr, lag, gap_db, frames, reason}`。
    只比「对齐后走势像不像」：不做音频分离、不碰任何门槛；判不了就说判不了（不冒充结论）。
    例外：两路逐帧电平差 ≥ 4 dB 时直接算「分开」——同一支麦被浏览器开成两路是同一份数字信号，
    不可能系统性差出 4 dB；差得出来就是两条不同的采集链。
    """
    count = min(len(left), len(right))
    if count < MIC_ROUTE_MIN_FRAMES:
        return {"status": "unknown", "reason": "not_enough_audio", "frames": count,
                "corr": None, "lag": 0, "gap_db": None}
    a = list(left)[-count:]
    b = list(right)[-count:]
    best = None
    for lag in range(-MIC_ROUTE_MAX_LAG, MIC_ROUTE_MAX_LAG + 1):
        if lag >= 0:
            part_a, part_b = a[lag:], b[:count - lag]
        else:
            part_a, part_b = a[:count + lag], b[-lag:]
        # 只比「两路都真有声音」的帧：安静（<= -70 dBFS）时两路都贴在底噪上，
        # 拿静音对静音比出来的「一样」不算证据（停顿时/刚静音那几帧最容易骗人）。
        pairs = [(x, y) for x, y in zip(part_a, part_b) if x >= MIC_ROUTE_FLOOR and y >= MIC_ROUTE_FLOOR]
        frames = len(pairs)
        if frames < MIC_ROUTE_MIN_FRAMES:
            continue
        corr = _mic_route_pearson([x for x, _ in pairs], [y for _, y in pairs])
        if corr is None:
            continue
        gap = sum(abs(x - y) for x, y in pairs) / frames
        if best is None or corr > best[0]:
            best = (corr, lag, gap, frames)
    if best is None:
        return {"status": "unknown", "reason": "flat_or_silent", "frames": count,
                "corr": None, "lag": 0, "gap_db": None}
    corr, lag, gap, frames = best
    if corr >= MIC_ROUTE_CORR_SAME and gap <= MIC_ROUTE_GAP_SAME:
        status = "same_source"
    elif gap >= MIC_ROUTE_GAP_SPLIT:
        # 走势再像也没关系：同一支麦被开成两路是同一份数字信号，差不出 4 dB；
        # 差得出来就说明是两条不同的采集链（两支真麦同时听到同一个人说话就长这样）。
        status = "separate"
    elif corr <= MIC_ROUTE_CORR_SPLIT:
        status = "separate"
    else:
        status = "unknown"      # 半像不像：不冒充任何一边
    return {"status": status, "reason": None, "corr": round(corr, 4), "lag": lag,
            "gap_db": round(gap, 2), "frames": frames}


class MicRouteGuard:
    """一条会话的开麦路由账：每席最近电平走势 + 页面报的「要哪支 / 实际给哪支」+ 判定结果。

    `note_level` 由各席自己的 ASR worker 写（只写自己那一路）；`poll` 只在 driver（单写者）里调，
    结论变了才返回一条 `mic_route` 事件（没变化返回 None）。页面据此写清「这一路真正在用的是哪支 /
    两路是不是真的分开」；判出「同一份声音」或「浏览器给的不是所选」时页面必须当场说清并停下。
    """

    def __init__(self):
        self.levels = {seat: deque(maxlen=MIC_ROUTE_WINDOW) for seat in DUAL_SEATS}
        self.claims = {seat: None for seat in DUAL_SEATS}
        self.lock = Lock()
        self.status = ""
        self.evaluated_at = -10 ** 9

    def note_level(self, seat, dbfs):
        if seat not in self.levels:
            return
        with self.lock:
            self.levels[seat].append(float(dbfs))

    def set_claim(self, seat, payload):
        """页面报一次「这一席向浏览器要的是哪支、实际给的是哪支」（只收标签与是否一致，不存设备号）。"""
        if seat not in DUAL_SEATS or not isinstance(payload, dict):
            return
        match = payload.get("match")
        with self.lock:
            self.claims[seat] = {
                "selected_label": str(payload.get("selected_label") or "")[:80],
                "actual_label": str(payload.get("actual_label") or "")[:80],
                "match": match if isinstance(match, bool) else None,
                "requested_exact": payload.get("requested_exact") is True,
            }

    def snapshot(self):
        with self.lock:
            return ({seat: list(self.levels[seat]) for seat in DUAL_SEATS},
                    {seat: (dict(self.claims[seat]) if self.claims[seat] else None)
                     for seat in DUAL_SEATS})

    @staticmethod
    def _seats_payload(active, claims, levels):
        return {seat: {"selected_label": (claims.get(seat) or {}).get("selected_label") or "",
                       "actual_label": (claims.get(seat) or {}).get("actual_label") or "",
                       "match": (claims.get(seat) or {}).get("match"),
                       "dbfs": round(levels[seat][-1], 1) if levels.get(seat) else None}
                for seat in active}

    def poll(self, now_ms, active):
        """到点判一次；结论变了给一条 `mic_route` 事件，否则 None。`active` = 这一轮真接进来的席。"""
        if now_ms - self.evaluated_at < MIC_ROUTE_EVAL_MS:
            return None
        self.evaluated_at = now_ms
        active = [seat for seat in DUAL_SEATS if seat in (active or ())]
        if len(active) < 2:
            return None
        levels, claims = self.snapshot()
        mismatch = [seat for seat in active
                    if claims.get(seat) and claims[seat].get("match") is False]
        detail = {"corr": None, "lag": 0, "gap_db": None, "frames": 0}
        if mismatch:
            status = "device_mismatch"
            parts = []
            for seat in mismatch:
                claim = claims.get(seat) or {}
                parts.append("席 %s 选的是「%s」，浏览器实际给的是「%s」"
                             % (seat, claim.get("selected_label") or "所选设备",
                                claim.get("actual_label") or "另一支"))
            message = ("浏览器给的不是所选那支麦（%s）：这一轮没开成两支 —— 已停下，"
                       "重选设备或先停止再开始" % "；".join(parts))
        else:
            verdict = mic_route_verdict(levels[active[0]], levels[active[1]])
            detail = {key: verdict.get(key) for key in ("corr", "lag", "gap_db", "frames")}
            if verdict["status"] == "same_source":
                status = "same_source"
                message = ("两路其实是同一份声音：浏览器把主驾和副驾开成了同一支麦"
                           "（回报的设备名可能不一样），这一轮没开成两支 —— 已停下；"
                           "换一支麦，或先「停止」再「开始」重新开")
            elif verdict["status"] == "separate":
                status = "ok"
                message = ("两路已确认分开：主驾 / 副驾各自是所选那支，两路电平走势对不上"
                           "（不是同一份声音），音量按各自那支走")
            else:
                if self.status in ("ok", "same_source"):
                    return None          # 已经确认过：安静下来不撤回结论
                status = "pending"
                message = ("两路已按所选打开；还没听到能分辨的声音，还不能确认是不是真的两支 ——"
                           "说句话试试（一路近讲、另一路远离，两路电平应该分开走）")
        if status == self.status:
            return None
        self.status = status
        return {"event": "mic_route", "t_ms": now_ms, "shared": True, "status": status,
                "message": message, "corr": detail.get("corr"), "lag": detail.get("lag"),
                "gap_db": detail.get("gap_db"), "frames": detail.get("frames"),
                "seats": self._seats_payload(active, claims, levels)}


def _mic_route_frame_level(pcm):
    """一帧 16-bit 单声道 PCM 的 RMS 电平（dBFS，-90 起底）。路由判定与音量门槛共用这一份。"""
    import array
    if not pcm:
        return -90.0
    samples = array.array("h")
    samples.frombytes(bytes(pcm[: len(pcm) - (len(pcm) % 2)]))
    if not samples:
        return -90.0
    total = 0
    for value in samples:
        total += value * value
    rms = math.sqrt(total / len(samples))
    if rms <= 1e-6:
        return -90.0
    return max(-90.0, 20.0 * math.log10(min(1.0, rms / 32768.0)))


class _DualMicConn:
    """一条席连接：自己的发送队列/线程，接收广播事件并序列化写 socket。"""
    def __init__(self, ws):
        self.ws = ws
        self.out = Queue()
        self.closed = False
        self.closing = False               # 交给唯一的写线程收尾（不在收件线程里抢着关 socket）
        self.stop = False                  # 该席收到 stop
        self.asr_done = False              # 该席 ASR worker 已冲完 final
        self.connected = True              # WS 还开着（数据可进）
        self.worker = Thread(target=self._send, daemon=True)
        self.worker.start()

    def _send(self):
        """唯一的写者：排队出站 → 队列空了且要关时才关 socket。

        画面回执现在带着那帧的 data URI（几 KB），如果 `close()` 在别的线程里直接关 socket，
        可能正好把一帧写一半就断掉——浏览器会报 `Invalid frame header`，看起来像送帧坏了。
        所以关连接也只由本线程做。
        """
        while True:
            try:
                kind, payload = self.out.get(timeout=0.2)
            except Empty:
                if self.closing:
                    break
                continue
            if kind is None:                 # 收尾 sentinel
                break
            try:
                self.ws.send(json.dumps({"kind": kind, "data": payload}, ensure_ascii=False, allow_nan=False))
            except Exception:
                break
        self.closed = True
        try:
            self.ws.close()
        except Exception:
            pass

    def send(self, kind, payload):
        if not self.closed and not self.closing:
            self.out.put((kind, payload))

    def close(self):
        if self.closing:
            return
        self.closing = True
        self.out.put((None, None))


class DualMicZoom:
    """一个 session 的两路真实麦克风会话：DualSeatSession + 每席独立 ASR worker + 单写者 driver。"""

    def __init__(self, session_id, cfg, mock, model=None):
        from asr.dual_seat import DualSeatSession
        from mind.mind_llm import MODEL_DEFAULT_ID
        from gate.turn_session import EVIDENCE_ROOT
        self.session_id = session_id
        self.cfg = cfg
        self.mock = bool(mock)
        # 页面选的读心档：只作用于本会话的两席读心（不改 8765 默认档；不选 = 现网默认）。
        self.model = model or MODEL_DEFAULT_ID
        self.started = time.monotonic() * 1000
        self.conns = {}                    # seat -> _DualMicConn
        self.pcm_in = {seat: Queue() for seat in DUAL_SEATS}
        # 开麦路由：两路是不是真的各自开成了所选那支（按实际收到的声音判；见 MicRouteGuard）
        self.mic_route = MicRouteGuard()
        # 文字输入：页面在这一席的输入框里打的整句原话（未经 ASR，发送即收住），仅 driver 消费。
        self.text_in = {seat: Queue() for seat in DUAL_SEATS}
        self.pending = {seat: [] for seat in DUAL_SEATS}   # 每个 seat 的 ASR 更新，仅 driver 消费
        self.asr_log = {seat: [] for seat in DUAL_SEATS}
        self.final_payloads = {}           # 每席最后一次 final（收口时兜底用，不落盘）
        self.done = False
        self.driver_started = False
        self.stop_sent = {seat: False for seat in DUAL_SEATS}   # 该席「显式收尾」只发一次
        # 画面：最近一帧彩色 data URI + 收到时间；只由 driver（单写者）喂给两席读心。
        self.frame_latest = None
        self.frame_at_ms = -1
        self.frame_count = 0
        self.frame_dirty = False
        self.frame_lock = Lock()
        # 这一个会话的**一份**读心（两路共用）+ 每席一份闸门；「上一次推出的场景」也只有一个指纹。
        self.mind_pushed = None
        self.mind = _hub_mind(session_id, self.model)
        self.gates = _hub_gates(self.mind)
        self.ledger = _hub_ledger()
        # 记忆：真麦链挂真记忆（两席写同一份用户记忆空间；没 key 自动退化成空实现）。
        self.memory = _hub_memory(session_id)
        app.logger.warning("memory(memos) session=%s: %s", session_id, self.memory.status())
        kwargs = {"seat_cfg": cfg, "on_event": self._on_event, "session_id": session_id,
                  "evidence_root": EVIDENCE_ROOT,
                  "seat_extra": _hub_seat_extra(self.gates, self.ledger, self.mind, self.memory)}
        if mock:
            # 与文本回放同一套离线判停/选工具/决策（不连 KEV/LLM，任何机器能复现双席编排）。
            eot, tools, decide, gate = _dual_mock_chain()
            kwargs.update(eot_fn=eot, select_tools_fn=tools, decide_fn=decide, confirm_gate=gate)
        else:
            kwargs.update(warm_fn=warm_slow_model)
        self.session = DualSeatSession(**kwargs)

    def _on_event(self, event):
        # 广播到所有连接；前端按 seat_id 过滤（无 seat_id 的编排层事件走中间泳道）。
        for conn in list(self.conns.values()):
            conn.send("event", event)

    def now_ms(self):
        return int(time.monotonic() * 1000 - self.started)

    def asr_factory(self):
        return _dual_mic_asr_factory()

    # ---- 画面：同一路摄像头帧，两席读心共用（不前装成某一席私有） ----
    def put_frame(self, frame):
        """浏览器送来一帧（彩色 data URI + 统计）：只记最新一帧，由 driver 单写者 ingest。"""
        with self.frame_lock:
            self.frame_latest = frame
            self.frame_at_ms = self.now_ms()
            self.frame_count += 1
            self.frame_dirty = True

    def take_frame(self):
        """driver 取「还没喂过的新帧」；没新帧返回 None（不重复喂同一帧）。"""
        with self.frame_lock:
            if not self.frame_dirty:
                return None
            self.frame_dirty = False
            return self.frame_latest, self.frame_at_ms, self.frame_count

    def frame_for_mind(self):
        """本拍出字时随文本一起送进读心的帧：只有最近 DUAL_FRAME_FRESH_MS 内收到过才算画面在送。"""
        frame = self.frame_latest
        if frame is None or self.frame_at_ms < 0:
            return None
        return frame if self.now_ms() - self.frame_at_ms <= DUAL_FRAME_FRESH_MS else None

    def feed_mind(self, seat, text, final=False):
        """这一路出字进本会话那份读心（两席共用）：把「最近这一帧」一起送进去。

        真麦与文字输入走同一个口子：画面和字走同一条真链，读心不按输入方式分两套。
        """
        mind_frame = self.frame_for_mind() or {}
        self.mind.ingest(text=text, final=bool(final), seat=seat,
                         image=mind_frame.get("image"), width=mind_frame.get("w"),
                         height=mind_frame.get("h"),
                         brightness=mind_frame.get("brightness"),
                         motion=mind_frame.get("motion"))

    # ---- 文字输入：每席一条通道，发送即收住（不经过 ASR，也不走回放） ----
    def send_text(self, seat, text):
        """页面在某一席输入框里点发送：整句原话排队，由 driver（单写者）送进真链。

        不在这里调 session：driver 是唯一写者（和 ASR 同一口径），两边都往队列里放、只由它派。
        """
        if self.done:
            raise ValueError("会话已结束")
        if seat not in DUAL_SEATS:
            raise ValueError("seat must be D or P: " + str(seat))
        text = (text or "").strip()
        if not text:
            raise ValueError("空文本不发：发出去的就是这一句完整原话")
        self.text_in[seat].put(text)

    def push_mind_scenes(self):
        """读心一有结果就推进对话流（不等下一句出字、不等下一帧画面）。

        拍板口径（2026-11-11）：只要有结果就需要推送到对话流。`scene_snapshot()` 只读缓存、
        不调模型；真模型还在读时它给 None（「读场景中…」占位句不是场景，不推）。
        同一句话（summary + source 不变）不重复推；内容一变立刻推一条。

        **一个会话只推一份场景**（`shared: true`，带两路出字与各自席别）：真麦同屏和分屏预览都是这一份，
        不再每席各推一条——那样两个泳道会各拿一半，互相看不见对方在说什么。
        """
        if not self.conns:
            return
        mind = self.mind
        scene = mind.scene_snapshot()
        if not scene:
            return
        fingerprint = (scene.get("summary"), scene.get("source"))
        if fingerprint == self.mind_pushed:
            return
        self.mind_pushed = fingerprint
        # `frame` 是产出这份场景的那一次请求里真正送给模型的图（同一段 data URI、同压缩、同尺寸）：
        # 页面照它显示，点开读心消息看到的就是模型当时看到的那一张（2026-11-11 追加口径）。
        frame = (mind.snapshot() or {}).get("frame")
        self._on_event({"event": "mind_scene", "t_ms": int(self.now_ms()),
                        "shared": True, "scene": scene, "frame": frame,
                        "model": self.model})

    def set_model(self, model_id):
        """会话中途换读心档：换这一份读心的引擎（不动 ASR / 已出字），并广播给页面确认。"""
        from mind.mind_llm import model_choice
        if not model_choice(model_id):
            raise ValueError("model 不在读心三选一内：" + str(model_id))
        self.model = model_id
        self.mind.set_engine(_mind_engine(model_id))
        self._on_event({"event": "model_seen", "t_ms": self.now_ms(), "seat_id": DUAL_SEATS[0],
                        "model": model_id, "engine": _mind_engine_info(model_id)})

    def start_asr(self, seat):
        """为该席起一个 ASR worker：读自己的 pcm_in → 独立 ASRStream → 追加到 seat pending。

        循环结束条件（三选一起就收送）：
          1. 该席连接被移除（WS 断开 / 会话 done）——
          2. 该席收到 stop 且自己的 PCM 队列已空（把最后一句冲完再收）——
          3. 会话已 done。
        """
        def worker():
            stream = None
            try:
                make_asr = self.asr_factory()
                stream = make_asr(MIC_SAMPLE_RATE, seat)
                while not self.done:
                    conn = self.conns.get(seat)
                    if conn is None:
                        break
                    if conn.stop and self.pcm_in[seat].empty():
                        break
                    try:
                        frame = self.pcm_in[seat].get(timeout=0.05)
                    except Empty:
                        frame = None
                    if frame:
                        # 路由看护记这一路自己的电平（只判「两路是不是同一份声音」，不改门槛/不碰识别）
                        self.mic_route.note_level(seat, _mic_route_frame_level(frame))
                        stream.push(frame)
                    for text, final, end_ms in stream.poll():
                        update = {"t_ms": int(self.now_ms()), "text": text,
                                  "final": bool(final), "vad_end_ms": end_ms}
                        self.pending[seat].append(update)
                        self.asr_log[seat].append(update)
                if not self.done:
                    for text, final, end_ms in stream.finish():
                        update = {"t_ms": int(self.now_ms()), "text": text, "final": bool(final),
                                  "vad_end_ms": end_ms}
                        self.pending[seat].append(update)
                        self.asr_log[seat].append(update)
            except Exception as error:
                self._on_event({"event": "error", "t_ms": int(self.now_ms()), "seat_id": seat,
                                "error_type": type(error).__name__, "error": str(error)})
            finally:
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
                if seat in self.conns:
                    self.conns[seat].asr_done = True

        Thread(target=worker, daemon=True).start()

    def driver(self):
        while not self.done:
            active = [seat for seat in DUAL_SEATS if seat in self.conns]
            # 画面：浏览器按约 1 帧/秒推来；由本线程（单写者）喂给两席读心，喂完立刻广播回执。
            frame = self.take_frame()
            if frame:
                latest, at_ms, count = frame
                snap = _mind_ingest(self.mind, latest, None, False)
                # 回执里顺带「此刻已有的那一份场景」（真模型还在读就给 null，不拿占位句当场景）：
                # 画面一变读心就更新，页面据此在不下一次出字时也能看到最新场景。
                scene = self.mind.scene_snapshot()
                self._on_event({"event": "frame_seen", "t_ms": int(self.now_ms()),
                                "seat_id": DUAL_SEATS[0], "shared": True,
                                "count": count, "frame_at_ms": int(at_ms),
                                "w": (latest or {}).get("w"), "h": (latest or {}).get("h"),
                                "bytes": (latest or {}).get("bytes"),
                                "visual": (snap or {}).get("scene", {}).get("visual") or {},
                                "scene": scene,
                                "seats": (snap or {}).get("scene", {}).get("seats") or [],
                                "model": self.model})
                # 这条回执已经把这一版场景带上了：记进指纹，下一拍 `push_mind_scenes` 不再重复推同一版。
                if scene:
                    self.mind_pushed = (scene.get("summary"), scene.get("source"))
            # 结算在途 ASR 更新（单写者：只有本线程调 session 方法，天然不串席）。
            for seat in active:
                while self.pending[seat]:
                    update = self.pending[seat].pop(0)
                    # 读心按这一路出字自己刷新场景（中枢开闸那一刻取一次，不反过来驱动读心）。
                    # 出字这一拍把「最近这一帧」一起送进去：画面和字走同一条真链；两路进同一份读心。
                    self.feed_mind(seat, update["text"], final=update["final"])
                    self.session.on_asr(update["t_ms"], DUAL_MIC_OF_SEAT[seat], update["text"], update["final"])
                    if update["final"]:
                        self.final_payloads[seat] = update
                # 文字输入（发送即收住）：整句原话先喂本会话那份读心，再交给该席 TurnSession。
                # 不经过 ASR、不判停；接不接、开闸、读心收住、记忆写入都走和真麦同一条真链。
                while True:
                    try:
                        typed = self.text_in[seat].get_nowait()
                    except Empty:
                        break
                    self.feed_mind(seat, typed, final=True)
                    self.session.on_text_send(int(self.now_ms()), seat, typed)
            # 读心出结果就推：真模型回话（后台线程落结果）/ 规则场景刷新都在这里被看见，
            # 不用等下一句字或下一帧画面（`push_mind_scenes` 只读缓存、同版不重复推）。
            self.push_mind_scenes()
            self.session.tick(int(self.now_ms()))
            conns = dict(self.conns)
            # 显式收尾（与主 UI mic 模式 control["stop"] 同一口径）：该席 stop + ASR 冲完 + 队列已空，
            # 对该席 TurnSession 发一次 stop(now_ms())，把最后一句强制落判。不发这一步时，最后一段
            # final 可能开出一个没人收的新 turn，turn_done 一直不成立 → 会话挂住（单路尤其容易）。
            for seat in active:
                conn = conns.get(seat)
                if (conn is not None and conn.stop and conn.asr_done and not self.pending[seat]
                        and not self.stop_sent[seat]):
                    self.stop_sent[seat] = True
                    self.session.seats[seat].stop(int(self.now_ms()))
            # 全部在柜席都已停 + ASR 冲完 + 该席 turn_done → 会话收口。
            if active and all(seat in conns and conns[seat].stop and conns[seat].asr_done
                              and self.session.seats[seat].turn_done for seat in active):
                self._finish()
                return
            # 开麦路由：两路是不是真的分开（同一份声音 / 浏览器给的不是所选那支 → 页面当场说清并停下）
            route_event = self.mic_route.poll(int(self.now_ms()), active)
            if route_event:
                self._on_event(route_event)
            time.sleep(0.005)

    def _finish(self):
        if self.done:
            return
        self.done = True
        # 收口前把「这一轮最后读出来的场景」也推掉：不让最后一版结果埋在第 100 条事件里没露过面。
        try:
            self.push_mind_scenes()
        except Exception:
            pass
        try:
            self.session.close()
        except Exception:
            pass
        summary = self.session.summary()
        plan = {}
        for seat in DUAL_SEATS:
            conn = self.conns.get(seat)
            if conn is None:
                continue
            final_text = (self.final_payloads.get(seat) or {}).get("text", "")
            plan[seat] = {"name": DUAL_SEAT_NAME[seat], "mic_id": DUAL_MIC_OF_SEAT[seat],
                          # `script` 沿用文本回放 plan 的键名，让前端「串席文本」对账同一套逻辑可直接复用。
                          "script": final_text, "updates": len(self.asr_log[seat]),
                          "asr_updates": len(self.asr_log[seat]), "final_text": final_text}
        self._on_event({"event": "duo_done", "t_ms": self.now_ms(), "mock": self.mock,
                        "session_id": self.session_id, "plan": plan, "summary": summary, "timed_out": False})
        DUAL_MIC_ZOOM.pop(self.session_id, None)


DUAL_MIC_ZOOM = {}       # session_id -> DualMicZoom（浏览器先打一个 session，两路 WS 汇合）
DUAL_MIC_KEEP = 8


def _join_dual_mic(session_id):
    """取或创建一个双席真麦会话（创建时顺带起单写者 driver 线程）。"""
    zoom = DUAL_MIC_ZOOM.get(session_id)
    if zoom is None:
        raise ValueError("unknown or expired dual-mic session: " + str(session_id))
    if not zoom.driver_started:
        zoom.driver_started = True
        Thread(target=zoom.driver, daemon=True).start()
    return zoom


@app.route("/api/session/dual/mic", websocket=True)
def dual_mic_session():
    """双席 · 真实麦克风：一路 = 一席。查询参数 `session_id`（两路共用）+ `seat`(D/P)。

    - 打开第一路（创建会话？不 — 会话在 /api/session/dual/mic/open 预创建）。见下。
    - 浏览器用 `POST /api/session/dual/mic/open` 拿 session_id 后，开两条 WS 到本端点
      `?session_id=…&seat=D` 与 `?session_id=…&seat=P`；各自推 16k/20ms PCM。
    - 每席一个独立 ASRStream（真实火山豆包），partial/final 由 driver 单写者喂给 DualSeatSession。
    - `{"cmd":"stop"}` 停该席；两席都停且各自 turn_done → 会话发 duo_done 收口。
    - `{"cmd":"text","text":"…"}` 文字输入：这一席的**整句原话**（不经过 ASR、不走回放），
      「发送即收住」后和真麦走同一条链（要不要回答 / 开闸 / 读心 / 记忆）；只进这一席，不串席。
    """
    from gate.turn_session import TurnConfig
    qs = request.args
    session_id = (qs.get("session_id") or "").strip()
    seat = (qs.get("seat") or "").strip()
    if seat not in DUAL_SEATS:
        ws = WsServer.accept(request.environ)
        try:
            ws.send(json.dumps({"kind": "error", "data": {"stage": "join",
                                                          "error": "seat must be D or P"}}, ensure_ascii=False))
        finally:
            try:
                ws.close()
            except Exception:
                pass
        return ""
    if not session_id:
        ws = WsServer.accept(request.environ)
        try:
            ws.send(json.dumps({"kind": "error", "data": {"stage": "join",
                                                          "error": "session_id required"}}, ensure_ascii=False))
        finally:
            try:
                ws.close()
            except Exception:
                pass
        return ""
    try:
        zoom = _join_dual_mic(session_id)
    except ValueError as error:
        ws = WsServer.accept(request.environ)
        try:
            ws.send(json.dumps({"kind": "error", "data": {"stage": "join", "error": str(error)}}, ensure_ascii=False))
        finally:
            try:
                ws.close()
            except Exception:
                pass
        return ""
    if seat in zoom.conns:
        ws = WsServer.accept(request.environ)
        try:
            ws.send(json.dumps({"kind": "error", "data": {"stage": "join",
                                                          "error": "seat " + seat + " already joined"}}, ensure_ascii=False))
        finally:
            try:
                ws.close()
            except Exception:
                pass
        return ""
    ws = WsServer.accept(request.environ)
    conn = _DualMicConn(ws)
    zoom.conns[seat] = conn
    zoom.start_asr(seat)
    conn.send("ready", {"session_id": session_id, "seat": seat, "sample_rate": MIC_SAMPLE_RATE,
                        "frame_ms": MIC_FRAME_MS,
                        "help": "双席真麦：席 %s 独立 ASR(mic-%s)，推 16k/20ms PCM；两路共用 session_id=%s"
                        % (DUAL_SEAT_NAME[seat], seat, session_id)})
    try:
        while True:
            message = ws.receive(timeout=0.5)
            if message is None:
                continue
            if isinstance(message, (bytes, bytearray)):
                conn.send("pcm_seen", {"frame_bytes": len(message)})
                zoom.pcm_in[seat].put(bytes(message))
                continue
            command = json.loads(message)
            if command.get("cmd") == "stop":
                conn.stop = True
                conn.send("stopping", {"seat": seat, "asr_done": conn.asr_done})
            elif command.get("cmd") == "frame":
                # 同屏摄像头/合成帧：页面采好的**彩色帧**（data URI），两席读心共用；由 driver 单写者 ingest。
                frame, frame_error = _frame_body(command)
                if frame_error:
                    conn.send("error", {"stage": "frame", "error_type": "ValueError", "error": frame_error})
                else:
                    zoom.put_frame(frame)
            elif command.get("cmd") == "route":
                # 页面报「这一席向浏览器要的是哪支、实际给的是哪支」（只有标签与是否一致）。
                # 与设备号对不上的当场算没开成；设备号对得上但两路实际是同一份声音，由声音判定兜住。
                zoom.mic_route.set_claim(seat, command)
            elif command.get("cmd") == "model":
                try:
                    zoom.set_model(str(command.get("model") or ""))
                except ValueError as error:
                    conn.send("error", {"stage": "model", "error_type": "ValueError", "error": str(error)})
            elif command.get("cmd") == "text":
                # 文字输入：这一席输入框里的一句话（整句原话），发送即收住。
                # 只管放进该席的 text 队列：派发仍由 driver（单写者）做，和 ASR 同一口径、不串席。
                try:
                    zoom.send_text(seat, str(command.get("text") or ""))
                except ValueError as error:
                    conn.send("error", {"stage": "text", "error_type": "ValueError", "error": str(error)})
    except Exception as error:
        conn.send("error", {"stage": "socket", "error_type": type(error).__name__, "error": str(error),
                            "trace": traceback.format_exc().splitlines()[-3:]})
    finally:
        # 由写线程把队列发完再关 socket：这里直接关会把在途的一帧写一半，浏览器报 invalid frame header。
        zoom.conns.pop(seat, None)
        conn.close()
        # 等写线程把队列发完、自己关掉 socket，再交给请求线程收尾：不然收尾的 close 帧可能和在途数据帧打架。
        conn.worker.join(timeout=0.8)
        # 若会话只靠这一路、且它中途断掉（没 stop），不等 driver 空转，直接收口。
        if not zoom.done and not any(zoom.conns.get(s) is not None for s in DUAL_SEATS):
            zoom.done = True
    return ""


@app.post("/api/session/dual/mic/open")
def dual_mic_open():
    """预创建一个双席真麦会话，返回 session_id 与每个座可选的输入设备契约。

    请求体可选：`mock`（默认 true = 离线判停/选工具/决策），`config`（TurnConfig 覆盖），
    `model`（读心三选一里的档，缺省 = 现网默认 `deepseek-v4.1-flash`；未知档直接 400，不偷偷换模型）。
    前端先调用本接口拿 session_id + mock/tool_set + 实际使用的读心档，再开两条 WS 汇合。
    """
    from mind.mind_llm import MODEL_DEFAULT_ID, model_choice
    from gate.turn_session import TurnConfig
    body = request.get_json(silent=True) or {}
    model_id = str(body.get("model") or "").strip() or MODEL_DEFAULT_ID
    if not model_choice(model_id):
        return jsonify({"error": "model 不在读心三选一内：" + model_id}), 400
    cfg = TurnConfig()
    try:
        overrides = dict(body.get("config") or {})
        if body.get("tool_set"):
            overrides["tool_set"] = body["tool_set"]
        cfg = TurnConfig.from_dict(overrides)
    except (TypeError, ValueError) as error:
        return jsonify({"error": type(error).__name__ + ": " + str(error)}), 400
    # 默认走真链（真实 ASR + 判停/选工具/决策）；勾 mock = 离线判停/选工具/决策（不连 KEV/LLM）。
    mock = bool(body.get("mock", False))
    session_id = "duo-mic-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    zoom = DualMicZoom(session_id, cfg, mock, model=model_id)
    while len(DUAL_MIC_ZOOM) > DUAL_MIC_KEEP:
        DUAL_MIC_ZOOM.pop(next(iter(DUAL_MIC_ZOOM)))
    DUAL_MIC_ZOOM[session_id] = zoom
    return jsonify({"session_id": session_id, "mock": mock, "tool_set": cfg.tool_set,
                    "sample_rate": MIC_SAMPLE_RATE, "frame_ms": MIC_FRAME_MS,
                    "model": model_id, "mind_engine": _mind_engine_info(model_id),
                    "mic_of_seat": DUAL_MIC_OF_SEAT, "seats": list(DUAL_SEATS)}), 200


# ---------- 内心戏页（独立页 /neixinxi）：两路麦 → 两路流式识别 → 音量门槛 → 判停 → 读心 ----------
#
# 与现有对话流页分开的**一条独立小链**，只做到通路可见：
#
#   浏览器席 D 设备 ──WS──▶ 音量门槛(-45dBFS 默认) ──▶ ASRStream-D ──▶ 本席出字
#   浏览器席 P 设备 ──WS──▶ 音量门槛(-45dBFS 默认) ──▶ ASRStream-P ──▶ 本席出字
#       两路出字 ──▶ 本席判停（`gate.turn_eot` 同一条口径，只判「这句说完没说完」）
#       两路出字 + 当时画面 ──▶ 一份读心（只出一句场景旁白，不判停、不判接不接）
#
# 本页**不建 TurnSession**：页面那一拍「要不要回答」只记录；收住后交主链跑闸 + 工具 + Agent，
# 回执上屏；记忆只写不查进 Agent。
# 两席单写者纪律沿用 `DualMicZoom`：worker 只往自己队列放更新，driver 线程统一派（不串席）。

NEIXINXI_KEEP = 8
NEIXINXI_SESSIONS: dict = {}          # session_id -> NeixinxiZoom
NEIXINXI_ASR_FACTORY = None           # 测试 seam：`(rate, seat) -> {push, poll, finish, close}`
# 音量线只做**降噪**（2026-11-13 用户拍板）：低于这条线的底噪滤掉，不判「人还在不在说」；
# 判停只走语义（`gate.turn_eot` + 其后的静音窗）。这条线不再参与收口、落账、另起一句。
NEIXINXI_DEFAULT_THRESHOLD = -45.0    # dBFS：降噪线（页面默认值，可调）
NEIXINXI_THRESHOLD_MIN, NEIXINXI_THRESHOLD_MAX = -70.0, -10.0
NEIXINXI_GATE_HOLD_MS = 400           # 降噪线余留：过线后 400ms 内仍原样送，词头/词尾不被这条线切掉
NEIXINXI_EOT_MIN_GAP_MS = 700         # 出字一路在变：判停最多这么久问一次（final 立刻问）
NEIXINXI_LEVEL_EVERY_MS = 200         # 电平回执节拍（页面看门槛是否过线）
# 这一席这一句已经落账（语义上不在说）时，低于降噪线的帧送**静音**：底噪不进识别，也保住会话。
# 识别服务是按「还有没有音频包」判会话生死的，停顿几秒不送包就整段会话被收掉（45000081），
# 再开口就永远不出字。送静音既保住会话，也不把底噪送进识别。
NEIXINXI_SILENCE_FRAME = b"\x00\x00" * (MIC_SAMPLE_RATE * MIC_FRAME_MS // 1000)
NEIXINXI_ASR_RETRY_GAP_MS = (250, 600, 1200)   # 识别流还是断了：自动接回来（不打断这一轮收听）
# 音量线与判停的职责（2026-11-13 用户拍板「音量线只做降噪，判停只走语义」）：
#   - 判停（`gate.turn_eot` 三态 + 其后的静音窗）是**唯一**判「这句说完没说完 / 人还在不在说」的地方；
#     本页不拿音量过不过线、也不拿连续安静多久当音频 VAD —— 那会把句中的轻声当成「已经停住」。
#   - 降噪线（`self.threshold`）只决定一帧要不要原样送进识别：
#       · 低于线、且这一席这一句**已经落账**（`utterance_open=False`，语义上不在说）→ 送静音（滤底噪）；
#       · 低于线、但这一句**还在说**（`utterance_open=True`）→ 照原样送。句中的轻声、字间起伏、
#         半秒内的短停都走这一路，不会被这条线切成静音、把字丢掉。
#   - `utterance_open` 只由语义事件开关：出到新字 → 开；收住落到句边界（ASR 报段结束 / 这席不再出新字）
#     或静音窗到点收口 → 关。与电平无关，也不参与判停。
# 出字另起一句（2026-11-11 21:39 用户真麦实测：一句被判停收住、交给读心之后，用过的字没清掉，
# 识别把同一段话整段叠着往回传，下一句就接在上一句后面越来越长）。口径：
#   - 判停说「说完」→ 这句交给读心，同时记成「这一句要另起一行」；
#   - 等一个**真的句边界**再落账：ASR 自己报这一段结束（definite）／或这席停住不再出字一小段；
#   - 落账之后：页面只留新字、判停只判新字、读心只读新字（旧字不再拼进下一句）。
# **不是**判停一说「说完」就当场清字幕——一句还没说完时字幕仍按当前这句往上长（验收 1）：识别还在
# 往下出字就说明这句没到边界（只是判停抢跑），这时不清、不剪，等它停住/落定再说。
NEIXINXI_SETTLE_QUIET_MS = 1200   # 收住后这席不再出新字，这么久算真的收在这里（不拿音量线当「不在说」）
# 记忆（2026-11-12 10:29–10:31 用户拍板，本卡的口径；取代 2026-11-11「收住那一刻就写一刀」）：
#   - **读心出了结果才有资格写**：从「每一次收住就写一刀」改为「这句收住之后落地的那一次读心，
#     场面和这句话对得上」之后才可能写；
#   - 说话还在进行中的读心（这席又出了新字）只更新旁白，**不送判断、不写记忆**；只有画面变化、
#     没有新的收住，这一刀默认不写（不新做 10 秒兜底）；
#   - 送给判断和写入的是「上一刀之后这一席攒下来的话」（后面是前面的续上/补全就并成一句），
#     再加这一刻的读心；人名以这一刻读心为准；
#   - 写之前用**判停同一套模型**做一次很快的「有没有必要写」（另准备一份短稿，`memory/write_judge.py`），
#     通过了才落一刀；判断失败 / 超时 / 分不清 → 宁可不写（不拿失败回落编一刀记忆）；
#   - 两席几乎是同一句（像漏音）：只把更像本席的那一路写入，另一路不写；分不清是谁说的，不写。
#   - 页面显示的必须是 `recall` 查回来的内容，不是把读心旁白再贴一次冒充「记下了」。
NEIXINXI_MEMORY_CLOSE_WAIT_S = 8.0    # 等「这次收住触发的那份读心」落地的上限（等不到不写）
NEIXINXI_MEMORY_LEAK_WINDOW_MS = 5000  # 两席几乎同一句的串音对照窗（临时本地规则，不是音频分离）
NEIXINXI_MEMORY_MAX_CHARS = 120       # 攒句上限：超了留最近这些字（只作兜底，正常一两段）
# 纯应声（「哦」「嗯」「好的」这类）：不往攒句里塞、不构成有效信息（离线快判断也按这条）。
NEIXINXI_MEMORY_ACK_RE = re.compile(
    r"^(?:好的|好|行|对|是|嗯|哦|啊|哎|呀|哈|知道了|明白了|懂了|收到|谢谢|不客气|"
    r"没事|没关系|ok|OK|行吧|算了|随便|再见|回头见|嗯嗯|哦哦)[，,。.!！?？、… ]*$")
NEIXINXI_MEMORY_RECALL_AT_S = (4, 8, 15, 25, 40, 60, 90, 120)   # memos 结构化是异步的：按这几个时刻查回
NEIXINXI_MEMORY_RECALL_LIMIT = 3
# 判停两套静音窗（2026-11-12 用户电话：「停住后没有新字就不再问判停、最后一句悬很久」）。
# 阈值**直接沿用主链现成的**（`gate.turn_session` 的 `TurnConfig.cut_wait_ms` / `think_wait_ms`），
# 本页不另定一套；触发路径也与主链 `_on_eot_result` + `tick()` 同一口径：
#   - 判停说「截断」→ 挂 1.5s（`cut_wait_ms`，主链 CUT_OFF_WAIT）；
#   - 判停说「还在想 / 没说完」→ 挂 3s（`think_wait_ms`，主链 THINK_WAIT）；
#   - **「还在想」允许超过 2s**：主链 `max_silence_ms` 那道 2s 上限不拿来把这一档提前收掉；
#   - 计时从「这一席最后一次真出字 / 最后一次过线出声」算起；到点且这段时间没有新字 → 收口交读心；
#   - 等待期间又出了新字（或又出声）：窗跟着最新那一刻重新算，不按旧窗提前收口，下一次判停回来再换窗；
#   - 判停说「说完」那条路径不变：当刻收住，不加确认窗。
def _neixinxi_wait_windows():
    """两套静音窗的阈值：从主链 `TurnConfig` 直接读，不在本页另写数字。"""
    from gate.turn_session import TurnConfig
    cfg = TurnConfig()
    return int(cfg.cut_wait_ms), int(cfg.think_wait_ms)


def _neixinxi_asr_factory():
    """两路各一份独立 ASRStream（与主 UI 真麦同一条识别后端，凭据只从环境读、不打印）。"""
    from asr.asr_stream import ASRStream
    if NEIXINXI_ASR_FACTORY is not None:
        return NEIXINXI_ASR_FACTORY
    return lambda rate, seat: ASRStream(rate)


def _neixinxi_dbfs(pcm):
    """一段 16-bit 单声道 PCM 的 RMS 电平（dBFS，-90 起底）。只用来判门槛，不处理音频。"""
    return _mic_route_frame_level(pcm)


def _neixinxi_error_text(error, limit=160):
    """识别失败给页面看的那一句：去掉像凭据/密钥的键值与长串，只留人能读懂的原因。"""
    text = " ".join(str(error or "").split())
    text = re.sub(r"(?i)(authorization|access[_-]?key|app[_-]?key|token|secret|cookie)\s*[=:]\s*\S+",
                  r"\1=<redacted>", text)
    text = re.sub(r"[A-Za-z0-9+/=_\-]{32,}", "<redacted>", text)
    return text[:limit]


class NeixinxiZoom:
    """一个「内心戏」会话：两路麦/两路识别/两路门槛/两路判停 + 一份读心。不建 TurnSession。"""

    def __init__(self, session_id, model=None, eot_mode="offline", thresholds=None):
        from mind.mind_llm import MODEL_DEFAULT_ID
        self.session_id = session_id
        self.model = model or MODEL_DEFAULT_ID
        self.eot_mode = eot_mode if eot_mode in HUB_EOT_MODES else "offline"
        self.started = time.monotonic() * 1000
        self.done = False
        # 运行日志：只写这一会话真实发生的事（判停 / 原话 / 读心 / 主链事件）。
        # 失败不影响产品页（日志是旁路，不参与判断，也不回写主画面）。
        try:
            self.log = NeixinxiLogWriter(session_id, {
                "model": self.model, "eot_mode": self.eot_mode,
                "seats": [{"seat": seat, "label": DUAL_SEAT_NAME[seat]} for seat in DUAL_SEATS],
                "source": "内心戏产品页（文字验证 / 双路麦）",
            })
        except Exception as error:  # noqa: BLE001
            app.logger.warning("neixinxi log disabled: %s", type(error).__name__)
            self.log = None
        self.driver_started = False
        self.conns = {}                                          # seat -> _DualMicConn
        self.pcm_in = {seat: Queue() for seat in DUAL_SEATS}
        # 开麦路由：两路是不是真的各自开成了所选那支（按实际收到的声音判；见 MicRouteGuard）
        self.mic_route = MicRouteGuard()
        self.pending = {seat: [] for seat in DUAL_SEATS}         # ASR 更新，只由 driver 消费
        self.threshold = {seat: float((thresholds or {}).get(seat, NEIXINXI_DEFAULT_THRESHOLD))
                          for seat in DUAL_SEATS}
        self.level = {seat: -90.0 for seat in DUAL_SEATS}        # 最近一帧电平
        self.quiet = {seat: True for seat in DUAL_SEATS}         # True = 这一帧在降噪线以下（只给页面看这条线）
        self.speaking_until = {seat: 0 for seat in DUAL_SEATS}   # 降噪线余留（不切词头/词尾）
        # 这一席这一句**在语义上还在说**（`utterance_open`）：只由出字 / 收口 / 落账开关，与电平无关。
        # 开着时低于降噪线的帧也照原样送（句中的轻声、字间起伏、短停不丢字）；关着时低于线的帧送静音滤底噪。
        self.utterance_open = {seat: False for seat in DUAL_SEATS}
        self.text = {seat: "" for seat in DUAL_SEATS}            # 这一路最近一版字
        self.eot = {seat: None for seat in DUAL_SEATS}           # 这一路最近一次判停结论
        self.eot_text = {seat: "" for seat in DUAL_SEATS}
        self.eot_at = {seat: -10 ** 9 for seat in DUAL_SEATS}
        self.eot_busy = {seat: False for seat in DUAL_SEATS}
        self.finals = {seat: [] for seat in DUAL_SEATS}
        # 判停两套静音窗：`wait` = 这一席当前挂着的那个窗（截断 1.5s / 还在想 3s），到点收口。
        # `silence_closed` = 已由静音窗收住的那一版字（同一版字不再二次收住；出新字就清）。
        self.wait = {seat: None for seat in DUAL_SEATS}
        self.silence_closed = {seat: "" for seat in DUAL_SEATS}
        self.wait_cut_ms, self.wait_think_ms = _neixinxi_wait_windows()
        # 出字另起一句：这一席这一路 ASR 的「整段累积」原文 + 已经收住（用掉）的字 + 还在等边界的这一句。
        #   raw       = 本席这一路最近一版原文（火山在同一段语音里会整段重发，不是增量）
        #   consumed  = 这段语音里已经收住、已经交给读心的那一截（normalize 后）；下一句只认它后面的新字
        #   settle    = 判停刚收住、还没落到边界的这一句（等 ASR final / 停顿才落账）
        #   asr_final = 最近一次 ASR definite 报的整段字；final_seq 用来判「收住之后有没有新的段落边界」
        #   last_word = 本席最近一次真出字的时刻（落账判「停住没」用）
        self.raw = {seat: "" for seat in DUAL_SEATS}
        self.consumed = {seat: "" for seat in DUAL_SEATS}
        self.settle = {seat: None for seat in DUAL_SEATS}
        # 正式答复（期四）：本席刚收住的那一句原话 + 它的版本 + 已发出的台账。
        # 只把主链「闸 + 工具 + Agent」给的那句回执接到中间大字，不另造主链、不另写台词。
        self.formal_lock = Lock()
        # 一句一办：按收住顺序排队（最多留 4 句），一句一趟主链；连续几句都收住时不粘、也不丢。
        self.formal_pending = {seat: [] for seat in DUAL_SEATS}
        self.formal_done = {seat: None for seat in DUAL_SEATS}
        # 一句一办：这一席同一时刻只跑一趟主链；`formal_sent` 记这一席刚交付过的那句原话，
        # 用来把「页面把上一句也挂在前面」的多余前缀切掉（只交出当轮那一句）。
        self.formal_busy = {seat: False for seat in DUAL_SEATS}
        self.formal_sent = {seat: "" for seat in DUAL_SEATS}
        # 这一席最近几句已收住的原话：后一句把前一句又粘在开头时，按它切掉（一句一办的第二道兜底）
        self.formal_settled = {seat: [] for seat in DUAL_SEATS}
        self.asr_final = {seat: "" for seat in DUAL_SEATS}
        self.final_seq = {seat: 0 for seat in DUAL_SEATS}
        self.last_word_ms = {seat: -10 ** 9 for seat in DUAL_SEATS}
        self.consume_lock = RLock()
        # 画面：最近一帧彩色 data URI（页面采好的原样），只由 driver 喂给读心。
        self.frame_latest = None
        self.frame_at_ms = -1
        self.frame_count = 0
        self.frame_dirty = False
        self.frame_lock = Lock()
        # 这一份读心（两路共用）+ 判停来源；`_hub_probe_eot` 与调试页同一条判定口径。
        self.mind = _hub_mind(session_id, self.model)
        self.mind_pushed = None
        self.mind_trace = []        # 最近推给页面的读心事件（逐拍追溯用，只读；不参与判断）
        self.eot_fn = _hub_probe_eot(self.eot_mode)
        # 记忆：一份会话一份真记忆（两席写同一份用户记忆空间；没 key 自动退化成空实现）。
        # 本页「收住之后 → 读心落地 → 写前快判断通过 → 才落一刀」（与对话流页共用
        # `memory.memos._write_payload` 的送法；判断见 `memory.write_judge`）。
        self.memory = _hub_memory(session_id)
        app.logger.warning("memory(memos) session=%s: %s", session_id, self.memory.status())
        self.memory_lock = RLock()
        # 这一席「上一刀」真正写下去的文本（归一化后）：同句 / 前缀补全的下一版不再写第二刀。
        # 句边界落账后**不清空**（同句重发也不重写；不相同的下一句照常能写）。
        self.memory_written = {seat: "" for seat in DUAL_SEATS}
        self.memory_written_at = {seat: -10 ** 9 for seat in DUAL_SEATS}
        # 这一席「上一刀之后攒下来的话」：收住的碎片先并进这一版，等这次读心落地 + 快判断通过才落一刀；
        # 判断为不写时**留着继续攒**（下一句若续上/补全就并成完整那版）。
        self.memory_ledger = {seat: "" for seat in DUAL_SEATS}
        self.memory_parts = {seat: [] for seat in DUAL_SEATS}   # 这一次攒句由哪几段并成（回执 / 调试用）
        # 每次收住 +1：又收住了新的一句时，旧那一刀的等待 / 写入作废（只认最新这一次）。
        self.memory_gen = {seat: 0 for seat in DUAL_SEATS}
        # 最近一次收住（给两席「几乎是同一句」的串音对照用；窗口 `NEIXINXI_MEMORY_LEAK_WINDOW_MS`）。
        self.memory_close = {seat: None for seat in DUAL_SEATS}
        # 写前快判断：默认按页面 `eot` 档走（real = 8009 同一套模型那份新稿；否则本地规则替身）。
        # 测试 / 联调可替换（签名为位置参数 `(content, seat, scene, prev)`）。
        self.memory_judge = self._default_memory_judge
        self.memory_panel_state = {seat: {"write": None, "recall": None, "skip": None}
                                   for seat in DUAL_SEATS}
        # 中枢出声（应声 / 垫句 / 冷场接话）：本页不建 TurnSession，但出不出声仍走主链那一套
        # `GateKeeper` 编排 + 同一批判据/台词；`real` 档问 8009 真判断，默认 `offline` 走同一套
        # rule 分支（详情里照实标「离线替身」）。判断只进详情，主画面中间大字就是这儿出的话。
        self.speech = HubSpeech(self.mind, DUAL_SEATS,
                                mode="model" if self.eot_mode == "real" else "rule")
        self.hub_version = {seat: None for seat in DUAL_SEATS}   # 最近一版：判停读完按它回填
        self.car_log: list = []                                  # 车说过的最近几句（详情对照）

    # ---- 派发 / 时钟 ----
    def _on_event(self, event):
        """本席的事件只发给本席那条连接；共用的（读心场景 / 画面回执 / 编排层）广播给两路。

        这样「主驾的字只进主驾」是结构性的：另一席的连接上根本收不到这一席的出字。
        """
        seat = event.get("seat_id")
        if seat in self.conns and not event.get("shared"):
            self.conns[seat].send("event", event)
            return
        for conn in list(self.conns.values()):
            conn.send("event", event)

    def now_ms(self):
        return int(time.monotonic() * 1000 - self.started)

    def asr_factory(self):
        return _neixinxi_asr_factory()

    # ---- 降噪线：每路一个，只决定这一帧送不送进识别（不判「人还在不在说」） ----
    def set_threshold(self, seat, value):
        try:
            value = float(value)
        except (TypeError, ValueError):
            raise ValueError("threshold must be a number (dBFS)")
        if not NEIXINXI_THRESHOLD_MIN <= value <= NEIXINXI_THRESHOLD_MAX:
            raise ValueError("threshold must be %g..%g dBFS"
                             % (NEIXINXI_THRESHOLD_MIN, NEIXINXI_THRESHOLD_MAX))
        self.threshold[seat] = value
        return value

    def gate_open(self, seat, dbfs, now_ms):
        """这一帧要不要**原样**送进识别（降噪线的唯一职责）。

        这一句还在说（`utterance_open`）→ 一律原样送：句中轻声、字间起伏、短停不被这条线切断；
        否则只送过线（或刚过线不久、还在余留窗里）的帧，其余送静音滤掉底噪。
        """
        if self.utterance_open[seat]:
            return True
        if dbfs >= self.threshold[seat]:
            self.speaking_until[seat] = now_ms + NEIXINXI_GATE_HOLD_MS
            return True
        return now_ms <= self.speaking_until[seat]

    # ---- 画面：同一路摄像头，两席读心共用 ----
    def put_frame(self, frame):
        with self.frame_lock:
            self.frame_latest = frame
            self.frame_at_ms = self.now_ms()
            self.frame_count += 1
            self.frame_dirty = True

    def take_frame(self):
        with self.frame_lock:
            if not self.frame_dirty:
                return None
            self.frame_dirty = False
            return self.frame_latest

    def frame_for_mind(self):
        """出字这一拍随字送进读心的帧：只有最近 DUAL_FRAME_FRESH_MS 内收到过才算画面在送。"""
        with self.frame_lock:
            frame = self.frame_latest
            if frame is None or self.frame_at_ms < 0:
                return None
            return frame if self.now_ms() - self.frame_at_ms <= DUAL_FRAME_FRESH_MS else None

    def feed_mind(self, seat, text, final=False):
        """这一路出字进本会话那份读心：把「最近这一帧」一起送进去（读心自己不判停）。"""
        mind_frame = self.frame_for_mind() or {}
        self.mind.ingest(text=text, final=bool(final), seat=seat,
                         image=mind_frame.get("image"), width=mind_frame.get("w"),
                         height=mind_frame.get("h"),
                         brightness=mind_frame.get("brightness"),
                         motion=mind_frame.get("motion"))

    def push_mind_scenes(self):
        """读心一有结果就推给页面（同一版不重复推）。一份场景两路都在，只出旁白。"""
        scene = self.mind.scene_snapshot()
        if not scene:
            return
        fingerprint = (scene.get("summary"), scene.get("source"))
        if fingerprint == self.mind_pushed:
            return
        self.mind_pushed = fingerprint
        # 这份场景配的那张图：用「最近一次真的落地了」的读心请求帧（不受下一版 pending 影响）
        frame = self.mind.last_frame()
        event = {"event": "mind_scene", "t_ms": self.now_ms(), "shared": True,
                 "scene": scene, "frame": frame, "model": self.model}
        self._on_event(event)
        # 逐拍留痕：输入（帧尺寸/字节、两路字、前几次结果）+ 触发原因 + 输出（场景、来源、引擎），
        # 页面同一个 `t_ms` 就是这一次推给它的那一条；图片本体不入留痕（只记字节数）。
        self.mind_trace.append({
            "t_ms": event["t_ms"], "trigger": scene.get("trigger"), "source": scene.get("source"),
            "summary": scene.get("summary"), "at_ms": scene.get("at_ms"), "engine": scene.get("engine"),
            "model": self.model,
            "frame": ({"w": frame.get("w"), "h": frame.get("h"), "bytes": frame.get("bytes"),
                       "format": frame.get("format"), "has_image": bool(frame.get("image"))}
                      if frame else None),
            "seats": [{"label": item.get("label"), "seat": item.get("seat"),
                       "text": item.get("text") or item.get("settled") or "",
                       "settled": item.get("settled"), "speaking": item.get("speaking")}
                      for item in (scene.get("seats") or [])],
            "prior": [{"scene": (item or {}).get("scene"), "ago_ms": (item or {}).get("ago_ms")}
                      for item in (scene.get("prior_results") or [])],
            "prior_used": scene.get("prior_used"),
        })
        del self.mind_trace[:-8]
        if self.log is not None:
            try:
                self.log.record_mind(scene, frame, self.model, event["t_ms"])
            except Exception as error:  # noqa: BLE001 - 日志故障不回写产品页
                app.logger.warning("neixinxi log mind failed: %s", type(error).__name__)

    # ---- 出字另起一句：用过的字不再叠到下一句 ----
    @staticmethod
    def _cut_index(text, prefix_len):
        """「已收住那一截」在原文里结束的位置：按去掉标点空白后的字数对齐。

        识别重发时不一定逐字一致（会重新断句/换标点），所以不能拿字符串直接 startswith 切；
        数到已收住那一截的长度再把原文字符让出去（与 `turn_session._tail_after_prefix` 同一口径）。
        """
        from shared.text import PUNCT
        seen = 0
        for index, char in enumerate(text):
            if PUNCT.fullmatch(char):
                continue
            seen += 1
            if seen == prefix_len:
                return index + 1
        return len(text)

    @classmethod
    def _tail_after_prefix(cls, text, prefix_len):
        return text[cls._cut_index(text, prefix_len):]

    def trim_settled(self, seat, text):
        """这段语音里已经收住的字不再算这一句：只返回它后面的新字（整句都是旧字时返回空串）。

        - 重发的整段还是「已收住前缀 + 新尾巴」→ 只留尾巴（下一句开头不再叠上一句），
        - 整段就是已收住的那一截 → 空串（调用方按「这一版没有新字」跳过，不重判、不重读），
        - 不以已收住前缀开头 → 识别那边换了一段话（重连 / 重开口），前缀记忆归零，不拿旧前缀误剪新话。
        """
        from shared.text import normalize
        with self.consume_lock:
            base = self.consumed[seat]
            if not base:
                return text
            norm = normalize(text)
            if not norm:
                return text
            if norm == base:
                return ""
            if norm.startswith(base):
                return self._tail_after_prefix(text, len(base))
            self.consumed[seat] = ""
            return text

    def note_settle(self, seat, text):
        """判停把这一句收住、交给读心：记成「这一句要另起一行」，等边界（ASR final / 停顿）落账。"""
        with self.consume_lock:
            self.settle[seat] = {"text": text or "", "at_ms": self.now_ms(),
                                 "final_seq": self.final_seq[seat]}

    def _commit_settled(self, seat):
        """这一句真的收在边界上了：把用过的字记成已收住，页面/判停/读心从下一版起只看新字。

        **交付给主链的是这一句，不是这一段原文**：火山在同一段语音里会整段重发（会带上前面
        已经收住的那句），所以「已收住前缀 + 这一句」不能当成一句交出去（2026-11-17 实测：
        「今天的天气怎么样」+「打开座椅按摩」被粘成一句送进主链，只办了前一句）。
        """
        with self.consume_lock:
            pending = self.settle.get(seat)
            if not pending:
                return
            from shared.text import normalize
            boundary = self.asr_final[seat] if self.final_seq[seat] > pending["final_seq"] else ""
            base = normalize(boundary) or normalize(self.raw[seat])
            if not base:
                return
            # 这一句 = 判停 / 静音窗刚收住的那一版（driver 交下来时已经切掉上一句的前缀）
            sentence = (pending.get("text") or "").strip()
            prev = self.consumed[seat]
            self.consumed[seat] = base
            self.settle[seat] = None
            # 落到句边界 = 这一句在语义上收在这里：降噪线从下一帧起恢复滤底噪（不是拿音量判断的）
            self.utterance_open[seat] = False
            raw = self.raw[seat]
            if sentence:
                # 这一句在原文里结束的位置 = 上一句已经用掉的字 + 这一句自己的字；后面才是新字
                cut = self._cut_index(raw, min(len(prev) + len(normalize(sentence)), len(base)))
                used, tail = sentence, raw[cut:]
            else:
                cut = self._cut_index(raw, len(base))
                used, tail = raw[:cut], raw[cut:]
            self.text[seat] = tail
            self.last_word_ms[seat] = self.now_ms() if tail else self.last_word_ms[seat]
        # 这一句真的收在边界上了：下一句是新的一句（同句去重不靠这句清空，靠 `memory_written`
        # 与攒句合并；这里不再把「上一刀写过的文本」抹掉，避免同一句在边界后又写第二刀）。
        # 页面：这一席的字幕归零，只留这一句之后的新字（旧字不再挂在前面）
        self._on_event({"event": "asr_settled", "t_ms": self.now_ms(), "seat_id": seat,
                        "text": tail, "settled": used or (pending.get("text") or ""),
                        "note": "这句已收住并交给读心：字幕另起一句，旧字不再叠到下一句"})
        # 这一句已收住的原话（正式答复用：人说完 + 判断要回答才开闸）
        with self.formal_lock:
            self._formal_enqueue(seat, used.strip())
        # 读心这一路也归零（没有新字就明确「这一刻没出字」）：下一句的读心只看这一句新话
        self.feed_mind(seat, tail, final=False)
        if tail:
            self.maybe_eot(seat, tail, final=False)

    def _maybe_commit(self, seat):
        """判停收住之后等一个真句边界：ASR 报这段结束，或这席不再出新字一小段 → 落账（另起一句）。

        **不拿「没过音量线」当「已经停住」**：低于线不等于没在说（句中轻声就是这样被切走的）。
        这里只认「这一席这一句真的不再出新字 / ASR 报了段结束」这一条语义边界。
        """
        pending = self.settle.get(seat)
        if not pending:
            return
        now = self.now_ms()
        if self.final_seq[seat] > pending["final_seq"]:
            self._commit_settled(seat)
            return
        if (now - self.last_word_ms[seat] >= NEIXINXI_SETTLE_QUIET_MS
                and now - pending["at_ms"] >= NEIXINXI_SETTLE_QUIET_MS):
            self._commit_settled(seat)

    # ---- 正式答复（期四）：复用现网双席主链的闸 + 工具 + Agent 回执，只把那一句接到中间大字 ----
    def _maybe_formal(self, seat):
        """这一句已收住且中枢判「要回答」→ 交给主链跑一趟（闸 / 工具 / Agent 都还是主链那套），取回执原句。"""
        with self.formal_lock:
            if self.formal_busy.get(seat):
                return                              # 这一席已经有一趟在跑：不并发、不抢跑
            queue = self.formal_pending.get(seat) or []
            # 队首若是已经办过的那句（ASR 整段重发又把同一句排回来），先清掉它：
            # 不清就会一直堵在队首，后面收住的句子永远轮不到（2026-11-17 实测：第二句一直没进主链）。
            while queue and self.formal_done.get(seat) == queue[0]:
                queue.pop(0)
            text = queue[0] if queue else None
            if not text:
                return
        # 这一句已经收住了：**必须交给主链**，页面上那一拍「要不要回答」只作记录带着走。
        # 两个吞句的坑（2026-11-17 真页面实测）：①判断结论是「按版」的，前一趟还在跑时
        # 这句要等 3~5 秒，等回来结论已过期 → 判「不回答」被永久丢掉；②判断自己回话说不了。
        # 决定性的闸在主链那一趟（同一套 8009 判断 + 主链的闸），页面不再替它吞句。
        page_accept = self._formal_wait_accept(seat, timeout_ms=900)
        with self.formal_lock:
            if self.formal_done.get(seat) == text or self.formal_busy.get(seat):
                return
            sentence = self._formal_sentence(seat, text)
            if not sentence:
                # 整段就是已经收住 / 已经办过的那句：不重发，也不留在队首空转
                self.formal_done[seat] = text
                queue = self.formal_pending.get(seat) or []
                if queue and queue[0] == text:
                    queue.pop(0)
                return
            self.formal_done[seat] = text
            queue = self.formal_pending.get(seat) or []
            if queue and queue[0] == text:
                queue.pop(0)                      # 只出队这一句，后面收住的句子还在队里
            self.formal_busy[seat] = True
            self.formal_sent[seat] = text          # 记「这次收到的整段原话」，下一次才好切前缀
        Thread(target=self._formal_worker, args=(seat, sentence, page_accept), daemon=True).start()

    def _formal_enqueue(self, seat, text):
        """收住一句就排队（同一句不重复）：连续几句都收住时，一句一趟，不粘成一团。"""
        text = (text or "").strip()
        if not text:
            return
        queue = self.formal_pending.setdefault(seat, [])
        if text in queue:
            return
        queue.append(text)
        del queue[:-4]
        # 这一席最近几句已收住的原话：后一句若把前面那句又粘在开头，交付时按它切掉
        settled = self.formal_settled.setdefault(seat, [])
        if text not in settled:
            settled.append(text)
            del settled[:-4]

    def _formal_sentence(self, seat, text):
        """只留当轮收住的那一句：前句若被粘在前面，就把已经收住 / 已经交付过的那截切掉。

        「各句各自进主链」靠这里兜底：上一句不管当时有没有开闸、有没有办过，只要它已经收住，
        后一句都不得把它再拼进来一起送。
        """
        text = (text or "").strip()
        previous = [self.formal_sent.get(seat) or ""]
        previous += list(self.formal_settled.get(seat) or [])
        for prev in sorted({item.strip() for item in previous if item and item.strip()},
                           key=len, reverse=True):
            if text != prev and text.startswith(prev):
                return text[len(prev):].strip()     # 可能为空：整段就是上一句（调用方按没新句处理）
        return text

    def _formal_wait_accept(self, seat, timeout_ms=2600):
        """等这一版的「要不要回答」结论回来（最多 2.6 秒，与主链那一路的判断时延同量级）。

        返回原样快照（含 state / accept / at），只用于页面侧记录；不拿它替主链吞句。
        """
        began = time.monotonic() * 1000
        while True:
            try:
                snap = self.speech.snapshot(seat) or {}
                accept = snap.get("accept") or {}
                state = accept.get("state")
                if state == "ok" and accept.get("accept") is not None:
                    return {"state": "ok", "accept": bool(accept.get("accept")),
                            "accept_source": accept.get("source"),
                            "at": int(time.time() * 1000),
                            "truth": "页面这一拍的判断，只记录，不决定主链"}
                if state in ("failed", "timeout"):
                    return {"state": state, "accept": None,
                            "error": accept.get("error"),
                            "at": int(time.time() * 1000),
                            "truth": "页面判断没回来；主链照常自己决定"}
            except Exception:  # noqa: BLE001 - 判断没回来就按「不回答」处置，不出声
                return {"state": "error", "accept": None,
                        "at": int(time.time() * 1000),
                        "truth": "页面判断读取失败；主链照常自己决定"}
            if time.monotonic() * 1000 - began >= timeout_ms:
                return {"state": "timeout", "accept": None,
                        "at": int(time.time() * 1000),
                        "truth": "页面判断超时；主链照常自己决定"}
            time.sleep(0.05)

    def _formal_worker(self, seat, text, page_accept=None):
        """把这一句交给主链（`/api/session/dual/stream`，与测试台同一个入口）：真实判断 + 主链闸 + Agent。

        只把主链「对人说的话」接到中间大字：
        - 回问（`kind=ask_user/done` 的 question）→ 上屏；
        - 办完（`kind=tool_call` 的 spoken，例如「已打开主驾座椅按摩」）→ 上屏，这是这一轮对用户的回复；
        - 办理记录（「（已执行：某工具…）」那个 receipt）→ 只进台账，不上屏；
        - 主链一句都没有 → 中间保持空，也不编台词。
        `page_accept` 是页面上那一拍「要不要回答」的结论（只记录，不拿它吞句）。
        这一趟的 SSE 事件原样落进运行日志（带 `run_id` / 主链 `duo_session_id`），
        并记下缺口（判答 / 工具披露 / Agent 对话没回来的照实列出）。
        """
        began = time.monotonic()
        run_id = "run-" + uuid.uuid4().hex[:10]
        run_t0 = self.now_ms()
        captured = []
        duo_session_id = None
        said, receipt, why, said_kind = "", "", "主链这一趟没有开闸", "ask_user"
        error_text = None
        try:
            import urllib.request
            # 一句一趟：只送这一句的**一版终稿**（不再用逐字脚本回放）。
            # 逐字回放会让主链按每个中间版本反复决策（实测 4000+ 次），既不是「一句一办」，
            # 也会把日志灌满中间事件。`updates` 是主链接口本来就支持的入参。
            body = json.dumps({"seats": {seat: {
                "updates": [{"t_ms": 0, "text": text, "final": True}],
                "vad": [{"t_ms": 0, "kind": "speech_start"},
                        {"t_ms": 1, "kind": "speech_end"}],
            }}}, ensure_ascii=False).encode()
            req = urllib.request.Request(
                "http://127.0.0.1:%d/api/session/dual/stream" % PORT, data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=120) as resp:
                for raw in resp:
                    line = raw.decode("utf-8", "replace").strip()
                    if not line.startswith("data:"):
                        continue
                    try:
                        packet = json.loads(line[5:].strip())
                    except ValueError:
                        continue
                    kind = packet.get("event")
                    if not kind and packet.get("session_id"):
                        # `duo_accepted` 这一条 data 里没有 event 字段，事件名在 SSE 的 event: 行。
                        kind = "duo_accepted"
                    if not kind:
                        continue
                    if kind in ("duo_accepted", "gate_decision", "gate_open",
                                "gate_context_only", "tools_start", "tools_scores",
                                "llm_decision", "au_decided", "au_executed",
                                "receipt", "error", "duo_done"):
                        captured.append({"event": kind, "at": int(time.time() * 1000),
                                         "data": packet})
                    if kind == "duo_accepted":
                        duo_session_id = str(packet.get("session_id") or "") or None
                    elif kind == "au_executed" and str(packet.get("kind") or "") in ("ask_user", "done"):
                        question = str((packet.get("arguments") or {}).get("question") or "").strip()
                        if not question:
                            head = "已回问："
                            got = str(packet.get("receipt") or "")
                            question = got.split(head, 1)[1].strip() if head in got else ""
                        if question and not said:
                            said, why, said_kind = question, "", "ask_user"
                    elif kind == "au_executed":
                        # 办完了：主链给的那句「对人说的话」就是这一轮的回复（办理记录不上屏）
                        spoken = str(packet.get("spoken") or "").strip()
                        receipt = str(packet.get("receipt") or "").strip() or receipt
                        if spoken and not said:
                            said, why, said_kind = spoken, "", "done"
                        elif not spoken and not why:
                            why = "主链办完了但没给出一句对人说的话"
                    elif kind == "receipt":
                        receipt = str(packet.get("text") or "").strip() or receipt
                        if not said and not why:
                            why = "主链只给了办理回执，没有一句对人说的话"
                    elif kind == "duo_done":
                        if not said and not why:
                            why = "主链跑完但没开闸 / 没有对人说的话"
                        break
        except Exception as error:  # noqa: BLE001 - 主链这一趟没跑完：不出声，页面可见
            error_text = type(error).__name__ + ": " + str(error)[:160]
            why = "主链这一趟没跑完：" + error_text
        finally:
            with self.formal_lock:
                self.formal_busy[seat] = False
        if self.log is not None:
            try:
                self.log.record_run(
                    run_id, seat, text, page_accept, captured,
                    duo_session_id=duo_session_id, said=said, said_kind=said_kind,
                    receipt=receipt, error=error_text, t_ms=run_t0)
            except Exception as error:  # noqa: BLE001 - 日志故障不影响回执上屏
                app.logger.warning("neixinxi log run failed: %s", type(error).__name__)
        if not said:
            self._on_event({"event": "formal_skip", "t_ms": self.now_ms(), "seat_id": seat,
                            "utterance": text, "reason": why, "receipt": receipt or None,
                            "page_accept": page_accept, "run_id": run_id,
                            "duo_session_id": duo_session_id,
                            "note": "主链这一趟没有一句对人说的话：中间保持空（回执只在台账里，不上屏）"})
            return
        event = {"event": "car_speech", "slot": "formal", "text": said, "t_ms": self.now_ms(),
                 "seat_id": seat, "shared": True, "utterance": text,
                 "kind": said_kind, "receipt": receipt or None, "page_accept": page_accept,
                 "run_id": run_id, "duo_session_id": duo_session_id,
                 "reason": ("人说完、判断要回答、闸开了、主链办完后给出这一句：这就是这一轮的回复"
                            if said_kind == "done" else
                            "人说完、判断要回答、闸开了、主链给出回问：回问原文上屏"),
                 "source": "主链 /api/session/dual/stream（闸 + 工具 + Agent）",
                 "model": "core_pipeline_run.decide（真判断）+ 主链执行器（mock 执行，无副作用）",
                 "mode": self.eot_mode, "latency_ms": round((time.monotonic() - began) * 1000, 1)}
        self.car_log.append(event)
        del self.car_log[:-8]
        self._on_event(event)

    # ---- 文字验证通道（不靠真麦）：发送即收住，走同一条读心 + 记忆 ----
    def say_text(self, seat, text):
        """页面在这一席输入框里点发送：整句原话**发送即收住**（不经过 ASR、不判停）。

        与真麦走同一条链：发送即收住 → 这句进读心读一次 → 这次读心落地后再做写前快判断，
        通过了才写一刀记忆；只是不需要麦。要不要回答 / 开闸 / 进 Agent 一概不接（本页本来就不做这三件事）。
        """
        if seat not in DUAL_SEATS:
            raise ValueError("seat must be D or P: " + str(seat))
        text = (text or "").strip()
        if not text:
            raise ValueError("空文本不发：发出去的就是这一句完整原话")
        from shared.text import normalize
        now = self.now_ms()
        with self.consume_lock:
            # 这一句就是这一段的全部字（后面没有 ASR 再接字）：已收住前缀 = 整句，
            # 下一句从新的一段开始（文字路径每句独立，不把上一句叠进来）。
            self.raw[seat] = text
            self.consumed[seat] = normalize(text)
            self.settle[seat] = None
            self.text[seat] = text
            self.last_word_ms[seat] = now
            # 文字这一句发送即收住：麦那一路还挂着的静音窗一并作废（不拿它再收一次）。
            self.wait[seat] = None
            self.silence_closed[seat] = text
        self._on_event({"event": "asr_update", "t_ms": now, "seat_id": seat, "text": text,
                        "final": True, "source": "text",
                        "note": "文字输入：发送即收住（不经过 ASR）"})
        verdict = {"label": "complete", "may_confirm": True, "may_start_early": True,
                   "complete": 0.99, "cut": 0.01, "wait": 0.01, "confidence": 0.99,
                   "source": "text", "wall_ms": 0.0, "error": None}
        version = self.hub_text(seat, text, True)
        self.speech.on_eot(seat, version, now, dict(verdict))
        self.eot[seat] = verdict
        self.eot_text[seat] = text
        self.eot_at[seat] = now
        eot_event = {"event": "eot", "t_ms": now, "seat_id": seat, "text": text,
                     "final": True, "label": "complete", "label_cn": "说完",
                     "may_confirm": True,
                     "scores": {key: verdict[key] for key in ("complete", "cut", "wait")},
                     "source": "text", "source_mode": "text", "error": None,
                     "latency_ms": 0.0,
                     "note": "文字输入发送即收住：这句交给读心读一次（不判接不接）"}
        self._on_event(eot_event)
        if self.log is not None:
            try:
                self.log.record_utterance(seat, text, "text_send", now)
                self.log.record_eot(seat, text, dict(verdict, label_cn="说完", source="text"),
                                    "text", 0.0, now)
            except Exception as error:  # noqa: BLE001
                app.logger.warning("neixinxi log text failed: %s", type(error).__name__)
        # 读心：与判停「说完」那一刻同一条入口（用这一句完整原话 + 当时的画面读一次）
        self.feed_mind(seat, text, final=True)
        try:
            self.mind.on_utterance_close(seat, why="text_send")
        except Exception:
            pass
        self.push_mind_scenes()
        self.settle_memory(seat, text, now)
        self.speech.note_turn_closed(seat)
        with self.formal_lock:
            self._formal_enqueue(seat, text)
        Thread(target=self._maybe_formal, args=(seat,), daemon=True).start()

    # ---- 记忆：收住之后 → 读心落地 → 写前快判断 → 通过才落一刀；页面显示「记忆里查回来」的内容 ----
    @staticmethod
    def _merge_speech(prev, new):
        """把「上一刀之后攒下来的话」与新收住的这一段并成一句（碎片先攒再并）。

        - 后面是前面的续上 / 补全（`new` 以 `prev` 开头，或 `prev` 只是 `new` 里的一截）→ 留**完整那版**
          （例：「搞」+「搞好了还蛮有意思」→「搞好了还蛮有意思」，只留完整那版）；
        - 完全不重叠（识别另起了一段）→ 直接接成一句（不猜标点、不改原文）。
        """
        from shared.text import normalize
        prev = (prev or "").strip()
        new = (new or "").strip()
        if not prev:
            return new
        if not new:
            return prev
        p, n = normalize(prev), normalize(new)
        if not n:
            return prev
        if not p or n == p:
            return new if len(new) >= len(prev) else prev
        if n.startswith(p):
            return new                     # 后者是前者的补全：留完整那版
        if p.startswith(n):
            return prev                    # 后者只是前者的一截：留完整那版
        if p in n:
            return new
        if n in p:
            return prev
        return prev + new                  # 续上：接成一句

    def _ledger_add(self, seat, text):
        """这一席收住的这一段先进「攒句」：纯应声不往攒句里塞，只作这一次的判断内容。

        返回这一次要送去写前判断的那一版（`None` = 这一下没有新内容可判，不重复送判断）。
        """
        from shared.text import normalize
        body = (text or "").strip()
        norm = normalize(body)
        if not norm:
            return None
        with self.memory_lock:
            ledger = self.memory_ledger.get(seat) or ""
            if NEIXINXI_MEMORY_ACK_RE.match(norm):
                if ledger:
                    return None        # 应声不添新信息：攒着的那一版已经判过，不重复判
                return body            # 单独一句应声：照样送判断（判断会说不写，页上看得见）
            merged = self._merge_speech(ledger, body)
            if len(normalize(merged)) > NEIXINXI_MEMORY_MAX_CHARS:
                merged = self._merge_speech("", merged[-NEIXINXI_MEMORY_MAX_CHARS:]).lstrip(
                    "，,。；;、… ")
            self.memory_ledger[seat] = merged
            parts = self.memory_parts[seat]
            parts.append(body)
            del parts[:-6]
            return merged

    def settle_memory(self, seat, text, t_ms):
        """这句**收住**了：先攒句，等这次收住触发的读心落地，再做写前快判断，通过了才写一刀。

        - 时机：只在收住时调用（判停给「说完」/ 静音窗收口 / 文字发送）；**写**不在收住当刻，
          而在「这句收住触发的那份读心落地」之后（不是每一次读心都写）；
        - 说话还在进行中的读心只更新旁白；只有画面变化、没有新的收住：不写（不新做 10 秒兜底）；
        - 同一份读心结果只写一刀；同一句（归一化后相同或前缀补全）不重写；判断失败 / 超时 → 不写；
        - 送法与其他页共用（`memory.memos._write_payload`）：`主驾说：“…”` + `当时场景：…`。
        """
        if self.memory is None or not getattr(self.memory, "enabled", True):
            return None
        from shared.text import normalize
        body = (text or "").strip()
        if not normalize(body):
            return None
        merged = self._ledger_add(seat, body)
        if merged is None:
            return None
        with self.memory_lock:
            self.memory_gen[seat] += 1
            gen = self.memory_gen[seat]
            since_seq = self.mind.result_seq()
            self.memory_close[seat] = {"text": body, "norm": normalize(body),
                                       "at_ms": int(t_ms), "gen": gen}
        Thread(target=self._memory_worker,
               args=(seat, body, merged, int(t_ms), gen, since_seq), daemon=True).start()
        return True

    def _wait_close_scene(self, seat, fragment, gen, since_seq):
        """等「这次收住触发的那份读心」落地，返回 `(scene, skip_reason)`。

        - 落地时这席**还在说话**（又出了新字）→ 只更新旁白，不送判断、不写（`still_speaking`）；
        - 等不到（超时）/ 这份场景还没出来 → 不写（`mind_timeout` / `no_scene`）；
        - 离线规则替身没有「在途读心」可等：此刻这份规则场景就是这次读心的结果（照实标 source）。
        """
        from shared.text import normalize
        closed_norm = normalize(fragment)

        def same(text):
            other = normalize(text or "")
            return bool(other) and (other == closed_norm or other.startswith(closed_norm)
                                    or closed_norm.startswith(other))

        if self.mind.engine is None:
            snap = self.mind.snapshot()
            if snap["seats"][seat]["speaking"]:
                return None, "still_speaking"
            scene = self.mind.scene_snapshot()
            return (scene, None) if scene else (None, "no_scene")
        deadline = time.monotonic() + NEIXINXI_MEMORY_CLOSE_WAIT_S
        while time.monotonic() < deadline:
            if self.done or self.memory_gen[seat] != gen:
                return None, "superseded"
            for item in self.mind.close_results():
                if (int(item.get("seq") or 0) > since_seq and item.get("seat") == seat
                        and same(item.get("text"))):
                    snap = self.mind.snapshot()
                    if snap["seats"][seat]["speaking"]:
                        return None, "still_speaking"
                    scene = self.mind.scene_snapshot()
                    return (scene, None) if scene else (None, "no_scene")
            time.sleep(0.05)
        return None, "mind_timeout"

    @staticmethod
    def _duplicate_reason(text, prev):
        """同一句（归一化后相同或前缀补全）不重写：返回原因，否则 `None`。"""
        from shared.text import normalize
        body, last = normalize(text), normalize(prev or "")
        if not last:
            return None
        if body == last or body.startswith(last) or last.startswith(body):
            return "same_as_last_cut"
        return None

    def _leak_reason(self, seat, merged):
        """两席几乎是同一句（像漏音）：只写更像本席的那一路；分不清是谁说的，不写。

        临时本地规则（不是完整音频分离模块）：窗口 `NEIXINXI_MEMORY_LEAK_WINDOW_MS` 内另一席
        收住过 / 刚写过的内容与这一版——一模一样或足够接近 → 分不清，不写；另一席那版只是这一版的
        前缀 → 这一版更完整，写这一路；反过来 → 这一路像漏音，不写。
        """
        from shared.text import normalize
        other = "P" if seat == "D" else "D"
        now = self.now_ms()
        body = normalize(merged)
        with self.memory_lock:
            peers = []
            closed = self.memory_close.get(other)
            at_ms = closed.get("at_ms") if closed else None
            if closed and now - int(at_ms if at_ms is not None else -10 ** 9) \
                    <= NEIXINXI_MEMORY_LEAK_WINDOW_MS:
                peers.append(normalize(closed.get("text") or ""))
            written = self.memory_written.get(other) or ""
            if (written and now - int(self.memory_written_at.get(other, -10 ** 9))
                    <= NEIXINXI_MEMORY_LEAK_WINDOW_MS):
                peers.append(written)
        for peer in peers:
            if not peer:
                continue
            if body == peer or self._near_same(body, peer):
                return "leak_indistinguishable"          # 分不清是谁说的：不写
            if body.startswith(peer):
                continue                                  # 本席这版更完整：更像本席
            if peer.startswith(body):
                return "leak_other_more_complete"         # 另一席那版更完整：这一路像漏音
        return None

    @staticmethod
    def _near_same(a, b):
        """足够接近（像同一段话漏到两席）：长度差不大且相似度 ≥ 0.85。"""
        if not a or not b:
            return False
        if abs(len(a) - len(b)) > max(len(a), len(b)) * 0.3:
            return False
        import difflib
        return difflib.SequenceMatcher(None, a, b).ratio() >= 0.85

    def _default_memory_judge(self, content, seat, scene, prev):
        """写前快判断：`?eot=real` 走判停同一套模型那份新稿；否则走离线本地规则替身。"""
        from memory import write_judge
        if self.eot_mode == "real":
            return write_judge.judge_memory_write(content, seat=seat, scene=scene, prev=prev)
        return write_judge.rule_memory_write(content, seat=seat, scene=scene, prev=prev)

    def _memory_skip(self, seat, text, reason, verdict=None):
        verdict = verdict or {}
        payload = {"event": "memory_skip", "t_ms": self.now_ms(), "seat_id": seat,
                   "text": (text or "")[:200], "reason": reason,
                   "write_score": verdict.get("score"), "threshold": verdict.get("threshold"),
                   "judge_source": verdict.get("source"), "judge_error": verdict.get("error"),
                   "note": "这次没写记忆：" + str(reason)}
        self._memory_panel(seat, "skip", payload)
        self._on_event(payload)

    def _memory_worker(self, seat, fragment, merged, t_ms, gen, since_seq):
        from shared.text import normalize
        scene, skip_reason = self._wait_close_scene(seat, fragment, gen, since_seq)
        if scene is None:
            if skip_reason != "superseded":
                self._memory_skip(seat, merged, skip_reason or "no_mind_result")
            return
        if self.memory_gen[seat] != gen:
            return                          # 又收住了新的一句：这一刀作废，由新的那一次处理合并后的话
        prev = self.memory_written[seat]
        reason = self._duplicate_reason(merged, prev)
        if reason:
            self._memory_skip(seat, merged, reason)
            return
        leak = self._leak_reason(seat, merged)
        if leak:
            self._memory_skip(seat, merged, leak)
            return
        # 写前快判断（同一套判停模型 / 离线本地规则）：可能慢，不持锁。
        try:
            verdict = self.memory_judge(merged, seat, scene, prev) or {}
        except Exception as error:          # noqa: BLE001 - 判断失败：宁可不写，不拿回落编一刀
            verdict = {"write": False, "reason": "judge_error",
                       "error": "%s: %s" % (type(error).__name__, str(error)[:120])}
        if not verdict.get("write"):
            reason = str(verdict.get("reason") or "judged_no_write")
            self._memory_skip(seat, merged, reason, verdict)
            return
        # 快判断跑完可能又收住了新的一句 / 另一席刚写了同一句：写前在锁里再查一遍。
        block_reason = None
        receipt = None
        parts = []
        with self.memory_lock:
            if self.memory_gen[seat] != gen:
                return
            prev = self.memory_written[seat]
            block_reason = self._duplicate_reason(merged, prev) or self._leak_reason(seat, merged)
            if not block_reason:
                parts = list(self.memory_parts[seat])
                self.memory_written[seat] = normalize(merged)
                self.memory_written_at[seat] = self.now_ms()
                self.memory_ledger[seat] = ""
                self.memory_parts[seat] = []
                turn = {"t_ms": int(t_ms), "seat": seat, "version": self.now_ms(),
                        "route": "neixinxi", "text": merged, "scene": scene, "gate": None}
                try:
                    receipt = self.memory.write(turn) or {}
                except Exception as error:      # noqa: BLE001 - 记忆失败不许拖垮这一页
                    receipt = {"status": "error",
                               "error": {"kind": type(error).__name__,
                                         "detail": str(error)[:120]}}
        if block_reason:
            self._memory_skip(seat, merged, block_reason)
            return
        summary = ((scene or {}).get("summary") or "").strip() or None
        payload = {"event": "memory_write", "t_ms": self.now_ms(), "seat_id": seat,
                   "status": receipt.get("status"), "reason": receipt.get("reason"),
                   "text": receipt.get("text") or merged[:160],
                   "merged_text": merged, "parts": parts or None, "scene": summary,
                   "judge": {"source": verdict.get("source"), "score": verdict.get("score"),
                             "threshold": verdict.get("threshold"),
                             "wall_ms": verdict.get("wall_ms")},
                   "latency_ms": receipt.get("latency_ms"), "error": receipt.get("error"),
                   "note": "收住后读心落地 + 写前快判断通过：这一刀是攒并后的话（主驾/副驾说：“…”）"
                           " + 这一刻的场景"}
        self._memory_panel(seat, "write", payload)
        self._on_event(payload)
        self._recall_back(seat, merged, scene)

    @staticmethod
    def _structured_items(items):
        """memos 在结构化出来之前会先把**原话记录**（`user: …`）当一条记忆返回；这里只挑抽好的那条。"""
        return [item for item in items if not str(item).lstrip().startswith("user:")]

    def _recall_back(self, seat, text, scene):
        """按这句原话把记忆查回来（查不到再按这一刻的场景）：页面显示的就是查回来的内容。

        memos 的结构化是异步的：刚开始只能查到原话记录（`user: …`）。这时先照样查、继续等，
        等结构化那条（「副驾是张先生」这类）出来再推给页面；整轮都只出原话就如实报原话。
        """
        queries = [text]
        summary = ((scene or {}).get("summary") or "").strip()
        if summary and summary != text:
            queries.append(summary)
        began = time.monotonic()
        first = True
        for index, at_s in enumerate(NEIXINXI_MEMORY_RECALL_AT_S, start=1):
            gap = at_s - (time.monotonic() - began)
            if gap > 0:
                time.sleep(gap)
            if self.done:
                return
            items, receipt = [], {}
            for query in queries:
                try:
                    hits = self.memory.recall(None, limit=NEIXINXI_MEMORY_RECALL_LIMIT,
                                              query=query) or []
                    receipt = getattr(self.memory, "last_recall", None) or {}
                except Exception as error:      # noqa: BLE001 - 查不到就下一轮再查，不抛
                    hits = []
                    receipt = {"status": "error",
                               "error": {"kind": type(error).__name__, "detail": str(error)[:120]}}
                if hits:
                    items, text_query = list(hits), query
                    break
            else:
                text_query = queries[0]
            structured = self._structured_items(items) if items else []
            if structured:
                payload = {"event": "memory_recall", "t_ms": self.now_ms(), "seat_id": seat,
                           "query": text_query, "count": len(structured),
                           "items": [item[:160] for item in structured],
                           "status": receipt.get("status"), "latency_ms": receipt.get("latency_ms"),
                           "attempt": index, "structured": True,
                           "note": "记忆里查回来的内容（不是把读心旁白再贴一次）"}
                self._memory_panel(seat, "recall", payload)
                self._on_event(payload)
                return
            if items and (first or index == len(NEIXINXI_MEMORY_RECALL_AT_S)):
                payload = {"event": "memory_recall", "t_ms": self.now_ms(), "seat_id": seat,
                           "query": text_query, "count": len(items),
                           "items": [item[:160] for item in items],
                           "status": receipt.get("status"), "latency_ms": receipt.get("latency_ms"),
                           "attempt": index, "structured": False,
                           "note": "记忆里先返回了原话记录（结构化还没出来）；继续等结构化那条"}
                self._memory_panel(seat, "recall", payload)
                self._on_event(payload)
            if first or index == len(NEIXINXI_MEMORY_RECALL_AT_S):
                payload = {"event": "memory_recall", "t_ms": self.now_ms(), "seat_id": seat,
                           "query": text_query, "count": 0, "items": [],
                           "status": receipt.get("status") or "pending",
                           "latency_ms": receipt.get("latency_ms"), "attempt": index,
                           "note": ("记忆已经交上去，正在结构化（memos 是异步的）：过一会儿再查"
                                    if index < len(NEIXINXI_MEMORY_RECALL_AT_S)
                                    else "这一轮还没查到（记忆可能还在结构化）")}
                self._memory_panel(seat, "recall", payload)
                self._on_event(payload)
            first = False

    def _memory_panel(self, seat, kind, payload):
        with self.memory_lock:
            self.memory_panel_state.setdefault(
                seat, {"write": None, "recall": None, "skip": None})[kind] = payload

    def memory_panel_snapshot(self, seat=None):
        with self.memory_lock:
            if seat:
                return dict(self.memory_panel_state.get(seat) or {})
            return {key: dict(value) for key, value in self.memory_panel_state.items()}

    def recall_now(self, query):
        """页面「再查一次」/ 脚本自查：按给的问法真查一次（同步、有上限，失败就空）。"""
        if self.memory is None or not (query or "").strip():
            return {"status": "skipped", "query": query, "items": []}
        items = self.memory.recall(None, limit=NEIXINXI_MEMORY_RECALL_LIMIT, query=query) or []
        receipt = getattr(self.memory, "last_recall", None) or {}
        return {"status": receipt.get("status"), "query": query,
                "items": [item[:160] for item in items], "count": len(items),
                "latency_ms": receipt.get("latency_ms"), "error": receipt.get("error")}

    def _reset_consumed(self, seat):
        """换了一段识别（首次接上 / 中断后重连）：新的一段话从头出字，旧的前缀记忆不再适用。"""
        with self.consume_lock:
            self.raw[seat] = ""
            self.consumed[seat] = ""
            self.settle[seat] = None
            self.asr_final[seat] = ""
            self.final_seq[seat] = 0
            self.utterance_open[seat] = False

    # ---- 每一路的识别 worker（只碰自己这一席的队列） ----
    def _say(self, seat, kind, payload):
        conn = self.conns.get(seat)
        if conn is not None:
            conn.send(kind, payload)

    def _asr_ready(self, seat, recovered=False, attempt=0):
        # 每一次「识别真接上」都是这一段语音的开头（首次接入 / 中断后重连）：前缀记忆从头来
        self._reset_consumed(seat)
        self._say(seat, "asr_ready", {"seat": seat, "sample_rate": MIC_SAMPLE_RATE,
                                      "frame_ms": MIC_FRAME_MS,
                                      "recovered": bool(recovered), "attempt": int(attempt)})

    def _reconnect_asr(self, seat, error, stream=None):
        """识别流断了（服务端收掉 / 网络 / 报错）：不打断这一轮收听，自动接回来。

        页面会先看到 `asr_interrupted`（「识别中断，正在重新接上」），接回来后再看到
        `asr_ready{recovered:true}`（「识别中断，已重新接上」），这期间采到的音频仍在队列里，
        接上后照旧送识别（声音不丢）。重连全失败才报错，绝不无声失败。
        """
        if stream is not None:
            try:
                stream.close()
            except Exception:
                pass
        error_type = type(error).__name__
        # 日志只记错误类型（不记原文：原文可能带 URL / 凭据）
        app.logger.warning("neixinxi seat %s asr interrupted: %s", seat, error_type)
        self._on_event({"event": "asr_interrupted", "t_ms": self.now_ms(), "seat_id": seat,
                        "error_type": error_type, "error": _neixinxi_error_text(error)})
        for attempt, gap_ms in enumerate(NEIXINXI_ASR_RETRY_GAP_MS, start=1):
            time.sleep(gap_ms / 1000.0)
            conn = self.conns.get(seat)
            if self.done or conn is None or conn.stop:
                return None
            try:
                stream = self.asr_factory()(MIC_SAMPLE_RATE, seat)
            except Exception as retry_error:
                app.logger.warning("neixinxi seat %s asr reconnect %d failed: %s",
                                   seat, attempt, type(retry_error).__name__)
                continue
            self._asr_ready(seat, recovered=True, attempt=attempt)
            app.logger.warning("neixinxi seat %s asr reconnected (attempt %d)", seat, attempt)
            return stream
        app.logger.warning("neixinxi seat %s asr reconnect gave up", seat)
        self._say(seat, "error", {"seat": seat, "stage": "asr", "error_type": "ReconnectError",
                                  "error": "识别中断，重连 %d 次都没接上（这一席暂时不会出字）"
                                           % len(NEIXINXI_ASR_RETRY_GAP_MS)})
        return None

    @staticmethod
    def _asr_dead(stream):
        return bool(getattr(stream, "closed", False) or getattr(stream, "_closed", False))

    def start_seat(self, seat):
        def worker():
            stream = None
            try:
                stream = self.asr_factory()(MIC_SAMPLE_RATE, seat)
                self._asr_ready(seat)
                while not self.done:
                    conn = self.conns.get(seat)
                    if conn is None:
                        break
                    if conn.stop and self.pcm_in[seat].empty():
                        break
                    try:
                        frame = self.pcm_in[seat].get(timeout=0.05)
                    except Empty:
                        frame = None
                    opened = False
                    if frame:
                        dbfs = _neixinxi_dbfs(frame)
                        now = self.now_ms()
                        # 开麦路由看护：记这一路自己的电平（只判「两路是不是同一份声音」，不改门槛）
                        self.mic_route.note_level(seat, dbfs)
                        opened = self.gate_open(seat, dbfs, now)
                        self.level[seat] = dbfs
                        # 只给页面看这条降噪线，不参与判停 / 收口 / 落账（没过线 ≠ 没在说）
                        self.quiet[seat] = dbfs < self.threshold[seat]
                    try:
                        if frame:
                            # 没过线的一路送静音保活（识别会话不会因停顿被收掉，这一席的话也不进识别）
                            stream.push(frame if opened else NEIXINXI_SILENCE_FRAME)
                        for text, final, end_ms in stream.poll():
                            self.pending[seat].append({"t_ms": self.now_ms(), "text": text,
                                                       "final": bool(final), "vad_end_ms": end_ms})
                    except Exception as error:
                        stream = self._reconnect_asr(seat, error, stream)
                        if stream is None:
                            break
                        continue
                    if self._asr_dead(stream):
                        stream = self._reconnect_asr(seat, RuntimeError("recognition stream closed"),
                                                     stream)
                        if stream is None:
                            break
                if stream is not None:
                    for text, final, end_ms in stream.finish():
                        self.pending[seat].append({"t_ms": self.now_ms(), "text": text,
                                                   "final": bool(final), "vad_end_ms": end_ms})
            except Exception as error:
                app.logger.warning("neixinxi seat %s asr error: %s", seat, type(error).__name__)
                self._say(seat, "error", {"seat": seat, "stage": "asr",
                                          "error_type": type(error).__name__,
                                          "error": _neixinxi_error_text(error)})
            finally:
                if stream is not None:
                    try:
                        stream.close()
                    except Exception:
                        pass
                conn = self.conns.get(seat)
                if conn is not None:
                    conn.asr_done = True
                    conn.send("asr_stopped", {"seat": seat})
        Thread(target=worker, daemon=True).start()

    # ---- 判停：每一路各自判「这句说完没说完」（页面不替主链决定接不接） ----
    def maybe_eot(self, seat, text, final=False):
        text = (text or "").strip()
        if not text:
            return
        now = self.now_ms()
        if self.eot_busy[seat]:
            return
        if not final and now - self.eot_at[seat] < NEIXINXI_EOT_MIN_GAP_MS:
            return
        self.eot_at[seat] = now
        self.eot_busy[seat] = True
        version = self.hub_version[seat]
        Thread(target=self._eot_worker, args=(seat, text, bool(final), version),
               daemon=True).start()

    def _eot_worker(self, seat, text, final, hub_version=None):
        began = time.monotonic()
        try:
            verdict = self.eot_fn(text)
        except Exception as error:
            verdict = {"label": "wait", "source": "error",
                       "error": "%s: %s" % (type(error).__name__, error)}
        finally:
            self.eot_busy[seat] = False
        latency = (time.monotonic() - began) * 1000
        self.eot[seat] = verdict
        self.eot_text[seat] = text
        labels = {"complete": "说完", "cut": "截断", "wait": "没说完"}
        event = {"event": "eot", "t_ms": self.now_ms(), "seat_id": seat,
                 "text": text, "final": final,
                 "label": verdict.get("label"),
                 "label_cn": labels.get(verdict.get("label"), verdict.get("label")),
                 "may_confirm": bool(verdict.get("may_confirm")),
                 "scores": {key: verdict.get(key)
                            for key in ("complete", "cut", "wait")},
                 "source": verdict.get("source") or self.eot_mode,
                 "source_mode": self.eot_mode,
                 "error": verdict.get("error"),
                 "latency_ms": round(latency, 1)}
        self._on_event(event)
        if self.log is not None:
            try:
                self.log.record_eot(seat, text, dict(verdict, **{
                    "label_cn": event["label_cn"], "source": event["source"],
                }), self.eot_mode, event["latency_ms"], event["t_ms"])
            except Exception as error:  # noqa: BLE001
                app.logger.warning("neixinxi log eot failed: %s", type(error).__name__)
        # 判停结论回填给闸门（应声 / 垫句要用「说完没说」；与主链 `_on_eot_result` 同一条）
        self.speech.on_eot(seat, hub_version, self.now_ms(),
                           dict(verdict, latency_ms=round(latency, 1)))
        # 判停说「说完」= 这一路这句收住：由本页把这一路交给读心读一次
        # （读心不自己判停；这里只拿来决定「什么时候读」）。要不要回答／开闸／Agent 一概不接。
        if verdict.get("may_confirm"):
            # 当刻收住（不收不慢：与改动前完全一样，不加确认窗）；静音窗这一档一并拆掉。
            self.close_utterance(seat, text, why="eot_" + str(verdict.get("label")))
        else:
            # 判停给「截断 / 还在想·没说完」：挂上主链那套静音窗。停住之后不再有新字时，
            # 到点由 driver 收口（见 `maybe_silence_close`），不再空等到下一句开口才出旁白。
            self.arm_silence_wait(seat, verdict.get("label"), text)

    # ---- 判停两套静音窗：截断 1.5s（cut_wait_ms）/ 还在想 3s（think_wait_ms） ----
    def wait_window_ms(self, label):
        """这一档判停对应的静音窗：截断 → `cut_wait_ms`；还在想 / 没说完 → `think_wait_ms`。"""
        return self.wait_cut_ms if label == "cut" else self.wait_think_ms

    def silence_anchor_ms(self, seat):
        """这一席静音的起算点：最后一次真出字（语义判据）。

        不拿「最后一个过降噪线的帧」当锚点：低于线不等于没在说（句中轻声就是那样被提前收口的）。
        """
        return self.last_word_ms[seat]

    def arm_silence_wait(self, seat, label, text):
        """判停给了「截断 / 还在想」之后挂静音窗；到点没有新字就收口交给读心。

        同一版字已经由静音窗收住过的不再挂（避免在途判停结论把刚收住的这句又收一次）。
        """
        from shared.text import normalize
        norm = normalize(text)
        if not norm or norm == normalize(self.silence_closed[seat]):
            return
        window_ms = self.wait_window_ms(label)
        self.wait[seat] = {"label": label, "window_ms": window_ms, "text": text}
        self._on_event({"event": "silence_wait", "t_ms": self.now_ms(), "seat_id": seat,
                        "label": label, "label_cn": {"cut": "截断", "wait": "没说完"}.get(label, label),
                        "window_ms": window_ms, "text": text,
                        "note": "判停给了「%s」：这席停住、这段时间没有新字，满 %dms 就收口交读心"
                                % ({"cut": "截断"}.get(label, "还在想 / 没说完"), window_ms)})

    def maybe_silence_close(self, seat, now=None):
        """driver 每拍调一次：静音窗到点且这段时间没有新字 → 收口（主链 tick() 的 cut_wait / think_wait）。"""
        from shared.text import normalize
        wait = self.wait.get(seat)
        if not wait or self.done:
            return False
        now = self.now_ms() if now is None else int(now)
        silence = now - self.silence_anchor_ms(seat)
        if silence < wait["window_ms"]:
            return False
        text = (self.text[seat] or wait.get("text") or "").strip()
        norm = normalize(text)
        self.wait[seat] = None
        if not norm or norm == normalize(self.silence_closed[seat]):
            return False          # 没有可收的新字（或这版已经收过）：这个窗作废，不重复读心
        self.silence_closed[seat] = text
        # 静音窗到点 = 语义上这一句收在这里（降噪线恢复滤底噪；与电平无关）
        self.utterance_open[seat] = False
        closed = self.close_utterance(seat, text, why="silence_" + str(wait.get("label")))
        if closed:
            self._on_event({"event": "silence_close", "t_ms": self.now_ms(), "seat_id": seat,
                            "label": wait.get("label"), "window_ms": wait["window_ms"],
                            "silence_ms": int(silence), "text": text,
                            "note": "静音窗到点收口（%dms 没新字）：这句交给读心出旁白"
                                    % wait["window_ms"]})
        return closed

    # ---- 中枢出声：出字 / 判停回填 / 收住 / 每一拍没人说话 ----
    def hub_text(self, seat, text, final=False):
        """这一席出了新字：版本号 +1 后在同一版字上问五路（接不接 / 应声 / 节奏 / 垫句）。

        与主链 `TurnSession.on_asr` 同一件事；判停结论晚到，由 `_eot_worker` 按版本号回填。
        """
        version = self.speech.on_text(seat, text, self.now_ms(), final=bool(final))
        if version is not None:
            self.hub_version[seat] = version
        return version

    def hub_idle(self, seat=None):
        """这一刻真的没人说话：两席都没在途判停 / 没挂静音窗 / 没待读的字 / 电平都在降噪线下。"""
        now = self.now_ms()
        for other in DUAL_SEATS:
            if self.pending[other] or self.eot_busy[other] or self.wait.get(other):
                return False
            if not self.quiet[other]:
                return False          # 有人还在出声（电平在线上）：不算冷场
            if now - self.last_word_ms[other] < NEIXINXI_SETTLE_QUIET_MS:
                return False
        return True

    def hub_poll(self, seat):
        """这一拍问一次「现在该不该出声」：垫句 → 应声 → 冷场（顺序与主链 tick 一致）。"""
        self._maybe_formal(seat)
        for event in self.speech.poll(seat, self.now_ms(), idle=self.hub_idle(seat)):
            self.car_log.append(event)
            del self.car_log[:-8]
            self._on_event(event)

    def feed_text(self, seat, text, final=False):
        """回放通道：把「人这一侧的一版字」放进同一条 driver（与 ASR 出字同一个入口）。

        只排进 `pending`（driver 单写者照旧消费）：判停、读心、中枢出声全部走真链，
        这里不造任何结论；没麦 / 不方便开口时用它复现同一拍。
        """
        if seat not in DUAL_SEATS:
            raise ValueError("seat must be D or P: " + str(seat))
        text = (text or "").strip()
        if not text:
            raise ValueError("空文本不喂：喂进来的就是这一版字")
        now = self.now_ms()
        self.pending[seat].append({"t_ms": now, "text": text, "final": bool(final),
                                   "vad_end_ms": None, "source": "feed"})
        return {"seat": seat, "text": text, "final": bool(final), "t_ms": now}

    def close_utterance(self, seat, text, why):
        """这一句收住：交给读心读一次，并把这句排进记忆的「收住后等读心 + 快判断」（本页唯一收住入口）。

        两条路径共用：判停当刻说「说完」（why=eot_complete），以及静音窗到点（why=silence_cut_wait /
        silence_think_wait）。第三条是文字发送（`say_text` 自己那条，本来就发送即收住）。
        """
        text = (text or "").strip()
        self.wait[seat] = None
        if not text:
            return False
        from shared.text import normalize
        if (str(why).startswith("eot_") and normalize(text)
                and normalize(text) == normalize(self.silence_closed[seat])):
            # 这版字已经由静音窗收住过（在途的判停结论晚到了）：不重复读心、不重复写记忆。
            # （只有「静音窗先收、判停结论后到」这一种才走到这里；判停先说说完那条路径行为不变。）
            return False
        # 记成「这一句要另起一行」：等 ASR 报这段结束 / 这席停住，就把用过的字清掉
        # （页面、判停、读心从下一版起都只认这一句之后的新字）。
        self.note_settle(seat, text)
        if self.log is not None:
            try:
                self.log.record_utterance(seat, text, str(why), self.now_ms())
            except Exception as error:  # noqa: BLE001
                app.logger.warning("neixinxi log utterance failed: %s", type(error).__name__)
        try:
            self.mind.on_utterance_close(seat, why=str(why))
            self.push_mind_scenes()
        except Exception:
            pass
        # 记忆：收住只是「有资格」——先攒句，等这次收住触发的读心落地，再做写前快判断；
        # 通过才写一刀，写完把记忆查回来推页面（同一条写入路径 / 同一套送法；不收住不写）。
        self.settle_memory(seat, text, self.now_ms())
        # 这一句收住：轮次 +1，冷场从「中枢最后一句」或「人最后一句」起算（与主链同一条）
        self.speech.note_turn_closed(seat)
        # 正式答复：这一句原话交给中枢那一路判断（要回答才开闸，由主链那一趟决定）
        with self.formal_lock:
            self._formal_enqueue(seat, text)
        Thread(target=self._maybe_formal, args=(seat,), daemon=True).start()
        return True

    # ---- driver（单写者）：画面 → 读心；两席出字 → 判停 + 读心；电平回执 ----
    def driver(self):
        level_at = -10 ** 9
        while not self.done:
            # 读心循环第 ④ 种：没人说话、距上一份场景满 10 秒 → 兜底再出一份（与冷场无关）
            if self.mind.maybe_idle_fallback():
                self.push_mind_scenes()
            frame = self.take_frame()
            if frame:
                snap = _mind_ingest(self.mind, frame, None, False)
                scene = self.mind.scene_snapshot()
                self._on_event({"event": "frame_seen", "t_ms": self.now_ms(),
                                "seat_id": DUAL_SEATS[0], "shared": True,
                                "count": self.frame_count, "w": frame.get("w"), "h": frame.get("h"),
                                "scene": scene,
                                "seats": (snap or {}).get("scene", {}).get("seats") or [],
                                "model": self.model})
                if scene:
                    self.mind_pushed = (scene.get("summary"), scene.get("source"))
            for seat in [s for s in DUAL_SEATS if s in self.conns]:
                while self.pending[seat]:
                    update = self.pending[seat].pop(0)
                    raw = update["text"] or ""
                    self.raw[seat] = raw
                    if update["final"]:
                        # ASR 自己报了这一段结束：这是「这一句真的收在这儿」的硬边界
                        self.asr_final[seat] = raw
                        self.final_seq[seat] += 1
                    text = self.trim_settled(seat, raw)
                    if text and normalize(text) != normalize(self.text[seat]):
                        # 真的出了**新字**才重置静音计时。识别在停顿期间会把同一版字反复重发，
                        # 那不算「又出了新字」——否则静音窗永远等不到 1.5s / 3s，停住后又悬回去。
                        self.last_word_ms[seat] = self.now_ms()
                        # 出到新字 = 这一席这一句在语义上还在说：降噪线从此刻起一律原样送（轻声不丢字）
                        self.utterance_open[seat] = True
                        if text != self.silence_closed[seat]:
                            # 等待期间又出了新字：静音窗不按旧窗收口，跟着这版字重新算；
                            # 新字照常上屏、照常问判停（下一次判停回来会再换一次窗）。
                            self.silence_closed[seat] = ""
                    if not text and not update["final"]:
                        continue     # 这一版整段都是已经用掉的字：不上屏、不重判、不进读心
                    if text != self.text[seat] or update["final"]:
                        self.text[seat] = text
                        if update["final"]:
                            self.finals[seat].append(text)
                    self.feed_mind(seat, text, final=update["final"])
                    self._on_event({"event": "asr_update", "t_ms": update["t_ms"], "seat_id": seat,
                                    "text": text, "final": update["final"],
                                    "vad_end_ms": update.get("vad_end_ms"),
                                    "threshold": self.threshold[seat]})
                    self.hub_text(seat, text, update["final"])
                    self.maybe_eot(seat, text, final=update["final"])
                self._maybe_commit(seat)
                self.maybe_silence_close(seat)
                self.hub_poll(seat)
            # 换页后可能暂时没有页面连接：已排队的正式办理仍要跑完（只有点停止才结束场次）
            for seat in DUAL_SEATS:
                if seat not in self.conns:
                    self._maybe_formal(seat)
            self.push_mind_scenes()
            now = self.now_ms()
            if now - level_at >= NEIXINXI_LEVEL_EVERY_MS:
                level_at = now
                for seat in [s for s in DUAL_SEATS if s in self.conns]:
                    self._on_event({"event": "level", "t_ms": now, "seat_id": seat,
                                    "dbfs": round(self.level[seat], 1),
                                    "threshold": self.threshold[seat],
                                    "quiet": bool(self.quiet[seat])})
                # 开麦路由：两路是不是真的分开（同一份声音 / 浏览器给的不是所选那支 → 页面当场说清并停下）
                route_event = self.mic_route.poll(now, [s for s in DUAL_SEATS if s in self.conns])
                if route_event:
                    self._on_event(route_event)
            time.sleep(0.01)
        try:
            self.mind.close()
        except Exception:
            pass

    def close(self):
        self.done = True
        try:
            self.speech.close()
        except Exception:
            pass


def _join_neixinxi(session_id):
    zoom = NEIXINXI_SESSIONS.get(session_id)
    if zoom is None or zoom.done:
        raise ValueError("unknown or expired neixinxi session: " + str(session_id))
    if not zoom.driver_started:
        zoom.driver_started = True
        Thread(target=zoom.driver, daemon=True).start()
    return zoom


def _neixinxi_live_ids():
    return {sid for sid, zoom in NEIXINXI_SESSIONS.items() if not zoom.done}


def _neixinxi_live_rows():
    rows = []
    for zoom in NEIXINXI_SESSIONS.values():
        if zoom.done:
            continue
        rows.append({
            "session_id": zoom.session_id,
            "model": zoom.model,
            "eot_mode": zoom.eot_mode,
            "connected_seats": [seat for seat in DUAL_SEATS if seat in zoom.conns],
            "_started": zoom.started,
        })
    rows.sort(key=lambda item: item["_started"], reverse=True)
    for item in rows:
        item.pop("_started", None)
    return rows


def _neixinxi_replace_conn(zoom, seat, ws):
    """同一席只保留最新这一条连接：换页重连时顶掉旧连接，但不结束会话。"""
    old = zoom.conns.get(seat)
    if old is not None:
        try:
            old.close()
        except Exception:
            pass
        if zoom.conns.get(seat) is old:
            zoom.conns.pop(seat, None)
    conn = _DualMicConn(ws)
    zoom.conns[seat] = conn
    return conn


def _neixinxi_release_conn(zoom, seat, conn):
    """只拆这一条页面连接；没有连接时会话仍保活，等明确 reset 才结束。"""
    if zoom is not None and conn is not None and zoom.conns.get(seat) is conn:
        zoom.conns.pop(seat, None)
    if conn is not None:
        conn.close()
        conn.worker.join(timeout=0.8)


@app.get("/neixinxi")
def neixinxi_page():
    return send_file(Path(__file__).with_name("neixinxi.html"))


# ---------- 运行日志（只读；与产品主画面分开） ----------
@app.get("/fonts/<name>")
def neixinxi_font(name):
    """本页自带的中文字体（只列这两个文件，避免任意路径读取）。"""
    if name not in ("NotoSansSC-Regular.ttf", "NotoSansSC-Bold.ttf"):
        return jsonify({"error": "font not found"}), 404
    path = Path(__file__).with_name("fonts") / name
    if not path.is_file():
        return jsonify({"error": "font not found"}), 404
    return send_file(path, mimetype="font/ttf")


@app.get("/log")
def neixinxi_log_page():
    """内心戏运行日志：中轴时间线，左=对话中枢/Agent，右=读心/画面。只读真实落盘记录。"""
    return send_file(Path(__file__).with_name("log.html"))


@app.get("/log.css")
def neixinxi_log_css():
    return send_file(Path(__file__).with_name("log.css"), mimetype="text/css")


@app.get("/log.js")
def neixinxi_log_js():
    return send_file(Path(__file__).with_name("log.js"), mimetype="application/javascript")


@app.get("/api/neixinxi/log/sessions")
def neixinxi_log_sessions():
    """最近会话列表（只列真实落盘的会话；没有跨会话检索，也不假装有）。"""
    try:
        limit = max(1, min(200, int(request.args.get("limit") or 100)))
    except (TypeError, ValueError):
        limit = 100
    sessions = NeixinxiLogReader().sessions(limit=limit)
    live_ids = _neixinxi_live_ids()
    for item in sessions:
        item["live"] = item.get("session_id") in live_ids
    return jsonify({"sessions": sessions, "live": _neixinxi_live_rows(),
                    "limits": {"search": "未建设", "retention": "未建设",
                               "permission": "未建设"}}), 200


@app.get("/api/neixinxi/log/media/<session_id>/<name>")
def neixinxi_log_media(session_id, name):
    """日志里保存的那一帧。只允许固定目录下的 png/jpg，不允许路径穿越。"""
    if not re.match(r"^[A-Za-z0-9_.-]{1,120}$", session_id or ""):
        return jsonify({"error": "bad session id"}), 400
    if not re.match(r"^[A-Za-z0-9_.-]{1,80}\.(?:png|jpg|jpeg)$", name or ""):
        return jsonify({"error": "bad media name"}), 400
    root = Path(os.environ.get("NEIXINXI_LOG_DIR")
                or Path(__file__).resolve().parents[1] / "var" / "neixinxi_log")
    path = root / "media" / session_id / name
    if not path.is_file():
        return jsonify({"error": "media not found"}), 404
    return send_file(path)


@app.get("/api/neixinxi/log/<session_id>")
def neixinxi_log_session(session_id):
    """一条会话的完整时间线事件（原话 / 判停 / 主链 / 读心 / 画面）。"""
    data = NeixinxiLogReader().get(session_id)
    if data is None:
        return jsonify({"error": "unknown log session: " + str(session_id)}), 404
    return jsonify({"session": data["session"], "events": data["events"],
                    "live": session_id in _neixinxi_live_ids(),
                    "limits": {"search": "未建设", "retention": "未建设",
                               "permission": "未建设"}}), 200


@app.get("/mic_route.js")
def mic_route_js():
    """开麦路由共用脚本（内心戏页 / 对话流页同一份纯函数，见文件头注释）。"""
    return send_file(Path(__file__).with_name("mic_route.js"),
                     mimetype="application/javascript")


@app.get("/island.js")
def island_js():
    """中间那个球（与示意稿同一份 Jev 岛实现，原样搬进本仓，不改视觉）。"""
    return send_file(Path(__file__).with_name("island.js"),
                     mimetype="application/javascript")


@app.post("/api/neixinxi/open")
def neixinxi_open():
    """开一个「内心戏」会话：返回 session_id + 两席默认门槛 + 读心档/判停来源。

    请求体可选：`model`（读心三选一，缺省 = 现网默认档）、`eot`（`offline|real`，默认 offline）、
    `thresholds`（{D: dBFS, P: dBFS}，默认 -45）。
    """
    from mind.mind_llm import MODEL_DEFAULT_ID, model_choice
    body = request.get_json(silent=True) or {}
    model_id = str(body.get("model") or "").strip() or MODEL_DEFAULT_ID
    if not model_choice(model_id):
        return jsonify({"error": "model 不在读心三选一内：" + model_id}), 400
    eot_mode = str(body.get("eot") or "offline")
    if eot_mode not in ("offline", "real"):
        return jsonify({"error": "eot must be offline or real"}), 400
    thresholds = body.get("thresholds") or {}
    if not isinstance(thresholds, dict):
        return jsonify({"error": "thresholds must be an object {D, P}"}), 400
    for seat, value in thresholds.items():
        if seat not in DUAL_SEATS:
            return jsonify({"error": "thresholds key must be D or P"}), 400
        try:
            float(value)
        except (TypeError, ValueError):
            return jsonify({"error": "thresholds values must be numbers (dBFS)"}), 400
    session_id = "neixinxi-" + time.strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6]
    zoom = NeixinxiZoom(session_id, model=model_id, eot_mode=eot_mode, thresholds=thresholds)
    while len(NEIXINXI_SESSIONS) > NEIXINXI_KEEP:
        old = NEIXINXI_SESSIONS.pop(next(iter(NEIXINXI_SESSIONS)))
        old.close()
    NEIXINXI_SESSIONS[session_id] = zoom
    return jsonify({"session_id": session_id, "sample_rate": MIC_SAMPLE_RATE,
                    "frame_ms": MIC_FRAME_MS, "model": model_id,
                    "mind_engine": _mind_engine_info(model_id),
                    "eot": {"mode": eot_mode,
                            "label": "真判停 8009" if eot_mode == "real" else "离线替身（与调试页同一判定口径）",
                            "endpoint": "score_eot → EOT_JUDGE_BASE"},
                    "thresholds": dict(zoom.threshold),
                    "threshold_range": [NEIXINXI_THRESHOLD_MIN, NEIXINXI_THRESHOLD_MAX],
                    "seats": [{"seat": seat, "label": DUAL_SEAT_NAME[seat]} for seat in DUAL_SEATS],
                    # 记忆：只报「开没开 / 挂在谁 / 凭据来源标签」，不含任何密钥信息（见 memory.memos.status）。
                    "memory": zoom.memory.status(),
                    "text_channel": "/api/neixinxi/text?session_id=…&seat=D|P",
                    "limits": ("判停只判说完没说完；页面那一拍「要不要回答」只记录，"
                               "收住后交主链跑闸 + 工具 + Agent，回执上屏；"
                               "读心 / 判停 / 主链事件写入运行日志；"
                               "记忆写入仍需读心落地 + 写前快判断通过")}), 200


@app.route("/api/neixinxi/asr", websocket=True)
def neixinxi_asr():
    """内心戏页一路：`?session_id=…&seat=D|P`，推 16k/20ms PCM，收本席出字/电平/判停/读心。

    只做「音频 → 本席识别 → 本席判停」，不建 TurnSession；两路共用 session_id，各自独立 ASR。
    """
    qs = request.args
    session_id = (qs.get("session_id") or "").strip()
    seat = (qs.get("seat") or "").strip().upper()
    zoom = None
    closed_error = None
    if seat not in DUAL_SEATS:
        closed_error = "seat must be D or P"
    elif not session_id:
        closed_error = "session_id required"
    else:
        try:
            zoom = _join_neixinxi(session_id)
        except ValueError as error:
            closed_error = str(error)
    if closed_error is not None:
        ws = WsServer.accept(request.environ)
        try:
            ws.send(json.dumps({"kind": "error", "data": {"stage": "join", "error": closed_error}},
                               ensure_ascii=False))
        finally:
            try:
                ws.close()
            except Exception:
                pass
        return ""
    ws = WsServer.accept(request.environ)
    conn = _neixinxi_replace_conn(zoom, seat, ws)
    zoom.start_seat(seat)
    conn.send("ready", {"session_id": session_id, "seat": seat,
                       "label": DUAL_SEAT_NAME[seat],
                       "sample_rate": MIC_SAMPLE_RATE, "frame_ms": MIC_FRAME_MS,
                       "threshold": zoom.threshold[seat], "eot_mode": zoom.eot_mode,
                       "help": "本席独立设备推 16k/20ms PCM；过门槛才进识别"})
    try:
        while True:
            message = ws.receive(timeout=0.5)
            if message is None:
                continue
            if isinstance(message, (bytes, bytearray)):
                zoom.pcm_in[seat].put(bytes(message))
                continue
            try:
                command = json.loads(message)
            except (TypeError, ValueError):
                continue
            if not isinstance(command, dict):
                continue
            if command.get("cmd") == "stop":
                conn.stop = True
                conn.send("stopping", {"seat": seat})
            elif command.get("cmd") == "threshold":
                try:
                    value = zoom.set_threshold(seat, command.get("value"))
                except ValueError as error:
                    conn.send("error", {"stage": "threshold", "error_type": "ValueError",
                                        "error": str(error)})
                else:
                    conn.send("threshold", {"seat": seat, "value": value,
                                            "range": [NEIXINXI_THRESHOLD_MIN, NEIXINXI_THRESHOLD_MAX]})
            elif command.get("cmd") == "frame":
                frame, frame_error = _frame_body(command)
                if frame_error:
                    conn.send("error", {"stage": "frame", "error_type": "ValueError",
                                        "error": frame_error})
                else:
                    zoom.put_frame(frame)
            elif command.get("cmd") == "route":
                # 页面报「这一席向浏览器要的是哪支、实际给的是哪支」（只有标签与是否一致）。
                # 与所选对不上的当场算没开成；设备号对得上但两路实际是同一份声音，由声音判定兜住。
                zoom.mic_route.set_claim(seat, command)
    except Exception as error:
        conn.send("error", {"stage": "socket", "error_type": type(error).__name__,
                            "error": str(error)})
    finally:
        _neixinxi_release_conn(zoom, seat, conn)
    return ""


@app.route("/api/neixinxi/text", websocket=True)
def neixinxi_text():
    """内心戏页 · 文字验证通道（不靠真麦）：一条 WS 只服务这一席的文字输入 + 同一份事件。

    `{"cmd":"text","text":"…"}` = 这一席的整句原话，**发送即收住**（走真读心 + 真记忆，
    与真麦那条链同一份读心 / 判停口径 / 记忆写入；不另开第二条链）。
    不开 ASR（没有麦）；页面那一拍「要不要回答」只记录，收住后仍交主链跑闸 + 工具 + Agent。
    """
    qs = request.args
    session_id = (qs.get("session_id") or "").strip()
    seat = (qs.get("seat") or "").strip().upper()
    zoom = None
    closed_error = None
    if seat not in DUAL_SEATS:
        closed_error = "seat must be D or P"
    elif not session_id:
        closed_error = "session_id required"
    else:
        try:
            zoom = _join_neixinxi(session_id)
        except ValueError as error:
            closed_error = str(error)
    if closed_error is not None:
        ws = WsServer.accept(request.environ)
        try:
            ws.send(json.dumps({"kind": "error", "data": {"stage": "join", "error": closed_error}},
                               ensure_ascii=False))
        finally:
            try:
                ws.close()
            except Exception:
                pass
        return ""
    ws = WsServer.accept(request.environ)
    conn = _neixinxi_replace_conn(zoom, seat, ws)
    # 文字通道不启 ASR worker：这一席只有「整句原话」这一种输入（发送即收住）。
    conn.send("ready", {"session_id": session_id, "seat": seat,
                        "label": DUAL_SEAT_NAME[seat], "mode": "text",
                        "help": "文字输入：整句原话，发送即收住（走真读心 + 真记忆）"})
    try:
        while True:
            message = ws.receive(timeout=0.5)
            if message is None:
                continue
            if isinstance(message, (bytes, bytearray)):
                continue      # 这条通道不收音频
            try:
                command = json.loads(message)
            except (TypeError, ValueError):
                continue
            if not isinstance(command, dict):
                continue
            if command.get("cmd") == "text":
                try:
                    zoom.say_text(seat, command.get("text") or "")
                except ValueError as error:
                    conn.send("error", {"stage": "text", "seat": seat,
                                        "error_type": "ValueError", "error": str(error)})
            elif command.get("cmd") == "frame":
                # 文字验证时也能送画面：画面一变只读旁白（不写记忆，除非这句真被收住）。
                frame, frame_error = _frame_body(command)
                if frame_error:
                    conn.send("error", {"stage": "frame", "error_type": "ValueError",
                                        "error": frame_error})
                else:
                    zoom.put_frame(frame)
            elif command.get("cmd") == "stop":
                conn.stop = True
                conn.send("stopping", {"seat": seat})
                break
    except Exception as error:
        conn.send("error", {"stage": "socket", "error_type": type(error).__name__,
                            "error": str(error)})
    finally:
        _neixinxi_release_conn(zoom, seat, conn)
    return ""


@app.get("/api/neixinxi/memory")
def neixinxi_memory():
    """页面 / 脚本自查：这一会话「收住 → 写记忆 → 查回来」的最新状态（不含任何密钥）。

    可选 `q`（问法）：带上就按它真查一次（页面「再查一次」按钮与脚本自查同一条）。
    """
    session_id = (request.args.get("session_id") or "").strip()
    seat = (request.args.get("seat") or "").strip().upper() or None
    query = (request.args.get("q") or "").strip()
    zoom = NEIXINXI_SESSIONS.get(session_id) if session_id else None
    if zoom is None:
        return jsonify({"error": "unknown or expired neixinxi session: " + session_id}), 404
    body = {"session_id": session_id, "memory": zoom.memory_panel_snapshot(seat),
            "store": zoom.memory.status()}
    if query:
        body["query"] = zoom.recall_now(query)
    return jsonify(body), 200


def _neixinxi_zoom_pick(session_id=""):
    """按 session_id 取会话；没给就取最近开的那个（喂字 / 自查脚本用，省一次握手）。"""
    if session_id:
        return NEIXINXI_SESSIONS.get(session_id)
    if not NEIXINXI_SESSIONS:
        return None
    return max(NEIXINXI_SESSIONS.values(), key=lambda item: item.started)


@app.get("/api/neixinxi/mind")
def neixinxi_mind_trace():
    """最近几份「推给页面的读心事件」（只读，逐拍追溯用：同一个 t_ms 就是页面收到的那一条）。"""
    zoom = _neixinxi_zoom_pick(str(request.args.get("session_id") or "").strip())
    if zoom is None:
        return jsonify({"error": "先开一个内心戏会话"}), 404
    return jsonify({"session_id": zoom.session_id, "model": zoom.model,
                    "eot_mode": zoom.eot_mode, "events": list(zoom.mind_trace)}), 200


@app.post("/api/neixinxi/frame")
def neixinxi_frame():
    """无麦验收的测试接缝：把一张**已标注的测试彩色图**当「最近一帧」送进本会话。

    与页面摄像头走的是同一个入口（`put_frame` → `frame_for_mind` → 读心），所以读心收到的图
    与页面「详情」里显示的是同一张；只用于没有麦克风 / 摄像头的验收，不冒充真车画面。
    请求体：`session_id`（缺省 = 最近开的那个）、`image`（jpeg/png data URI）、`w`/`h`、
    可选 `brightness`/`motion`、`test_asset`（true = 测试图，回执照实标明）。
    """
    body = request.get_json(silent=True) or {}
    zoom = _neixinxi_zoom_pick(str(body.get("session_id") or "").strip())
    if zoom is None:
        return jsonify({"error": "先开一个内心戏会话"}), 404
    frame, frame_error = _frame_body(body.get("frame") if isinstance(body.get("frame"), dict) else body)
    if frame_error:
        return jsonify({"error": frame_error}), 400
    if not frame:
        return jsonify({"error": "image required (jpeg/png data URI)"}), 400
    zoom.put_frame(frame)
    return jsonify({"session_id": zoom.session_id, "w": frame.get("w"), "h": frame.get("h"),
                    "test_asset": bool(body.get("test_asset")),
                    "note": "测试彩色图；与摄像头同一入口送进读心"}), 200


@app.get("/api/neixinxi/hub")
def neixinxi_hub():
    """这一拍的判断（只给「详情」）：两席各自 判停 / 要不要回答 / 应声 / 节奏 / 垫句 / 冷场。

    与主链同一批判断（`GateKeeper` + `build_*_fn()`），本页只读结论、不改判据；
    `sources` 照实写这套判断是「真判断 8009」还是「离线替身」。
    """
    session_id = (request.args.get("session_id") or "").strip()
    seat = (request.args.get("seat") or "").strip().upper()
    zoom = _neixinxi_zoom_pick(session_id)
    if zoom is None:
        return jsonify({"error": "unknown or expired neixinxi session: " + session_id}), 404
    seats = [seat] if seat in DUAL_SEATS else list(DUAL_SEATS)
    return jsonify({"session_id": zoom.session_id, "eot_mode": zoom.eot_mode,
                    "hub": {item: zoom.speech.snapshot(item) for item in seats},
                    "car": zoom.car_log[-6:],
                    "seat_text": {item: zoom.text.get(item, "") for item in seats}})


@app.post("/api/neixinxi/feed")
def neixinxi_feed():
    """回放通道：把「人这一侧的一版字」喂进同一条 driver（没有麦 / 不方便开口时复现同一拍）。

    请求体：`text`（这一版字，必填）、`seat`（D/P，默认 D）、`final`（这一段结束，默认 false）、
    `session_id`（缺省 = 最近开的那个）。只排进 `pending`，判停 / 读心 / 出声全部照真链跑。
    """
    body = request.get_json(silent=True) or {}
    text = str(body.get("text") or "")
    seat = str(body.get("seat") or "D").strip().upper()
    zoom = _neixinxi_zoom_pick(str(body.get("session_id") or "").strip())
    if zoom is None:
        return jsonify({"error": "先开一个内心戏会话（页面上点「开始双路」或文字验证）"}), 404
    if seat not in DUAL_SEATS:
        return jsonify({"error": "seat must be D or P"}), 400
    if seat not in zoom.conns:
        return jsonify({"error": "这一席还没连上（页面上这一席要先接上）"}), 409
    try:
        fed = zoom.feed_text(seat, text, final=bool(body.get("final")))
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    return jsonify({"session_id": zoom.session_id, "fed": fed}), 200


@app.get("/api/neixinxi/live")
def neixinxi_live():
    """进行中的内心戏会话。换页只断开本页连接，场次仍在；只有点停止 / reset 才结束。"""
    rows = _neixinxi_live_rows()
    return jsonify({"sessions": rows, "current": rows[0] if rows else None}), 200


@app.post("/api/neixinxi/say")
def neixinxi_say():
    """文字验证同一口径：发送即收住，走真读心 + 主链。不要求本席此时占着 WebSocket。"""
    body = request.get_json(silent=True) or {}
    session_id = str(body.get("session_id") or "").strip()
    seat = str(body.get("seat") or "D").strip().upper()
    text = str(body.get("text") or "")
    if seat not in DUAL_SEATS:
        return jsonify({"error": "seat must be D or P"}), 400
    zoom = None
    if session_id:
        try:
            zoom = _join_neixinxi(session_id)
        except ValueError as error:
            return jsonify({"error": str(error)}), 404
    else:
        picked = _neixinxi_zoom_pick("")
        if picked is not None and not picked.done:
            zoom = _join_neixinxi(picked.session_id)
    if zoom is None:
        return jsonify({"error": "先开一个内心戏会话（日志页点「开始本场」，或内心戏页开文字验证）"}), 404
    try:
        zoom.say_text(seat, text)
    except ValueError as error:
        return jsonify({"error": str(error)}), 400
    return jsonify({"ok": True, "session_id": zoom.session_id, "seat": seat,
                    "text": text.strip()}), 200


@app.post("/api/neixinxi/reset")
def neixinxi_reset():
    body = request.get_json(silent=True) or {}
    session_id = str(body.get("session_id") or "")
    zoom = NEIXINXI_SESSIONS.pop(session_id, None) if session_id else None
    if zoom is not None:
        zoom.close()
    return jsonify({"ok": True, "dropped": bool(zoom)})


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)
