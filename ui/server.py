"""Loopback-only PoC UI; reuse the existing single-utterance pipeline."""
import json
import os
import re
from pathlib import Path
from queue import Empty, Queue
import sys
from threading import Thread
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
import bootstrap
bootstrap.install()          # 上游仓库根（model_clients/registry）

from flask import Flask, Response, jsonify, request, send_file, stream_with_context
from simple_websocket import Server as WsServer
from core_pipeline_run import TOOL_SET, run
from model_clients import warm_slow_model

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
    from turn_session import TurnConfig, TurnSession
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
    from turn_session import normalize

    wanted = {normalize(text) for text in (complete_texts or []) if normalize(text)}

    def eot(text):
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
        return {"decision": {"kind": "tool_call", "tool": tool, "arguments": {}},
                "llm_ms": 5.0, "log_fields": {"decision_head": "mock_dual_demo"}}

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
    from dual_seat import DualSeatSession
    from turn_session import EVIDENCE_ROOT, TurnConfig

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
    kwargs = {"seat_cfg": cfg, "on_event": queue.put, "session_id": session_id,
              "evidence_root": EVIDENCE_ROOT}
    if mock:
        full_texts = [rows[-1]["text"] for seat in plan
                      for rows in [[row for row in timeline
                                    if row["seat"] == seat and row["type"] == "asr"]] if rows]
        eot, tools, decide, gate = _dual_mock_chain(full_texts)
        kwargs.update(eot_fn=eot, select_tools_fn=tools, decide_fn=decide, confirm_gate=gate)
    else:
        kwargs.update(warm_fn=warm_slow_model)
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
            queue.put({"event": "duo_done", "t_ms": round(time.monotonic() * 1000 - began, 1),
                       "timed_out": timed_out, "mock": mock, "plan": plan,
                       "summary": session.summary()})
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
    from turn_session import TurnConfig, TurnSession
    from asr_stream import ASRStream

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


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=PORT, debug=False, use_reloader=False)
