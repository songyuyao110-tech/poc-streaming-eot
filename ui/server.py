"""Loopback-only PoC UI; reuse the existing single-utterance pipeline."""
import json
import os
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
bootstrap.install()          # 上游仓库根（model_clients/gateway/registry）

from flask import Flask, Response, jsonify, request, send_file, stream_with_context
from simple_websocket import Server as WsServer
from core_pipeline_run import run
from model_clients import warm_slow_model

app = Flask(__name__)
app.json.ensure_ascii = False


@app.get("/")
def index():
    return send_file(Path(__file__).with_name("index.html"))


@app.post("/api/run")
def run_query():
    body = request.get_json()
    result = run(body["utterance"], tool_set=body.get("tool_set", "navigation"))
    return jsonify(result), 200 if result["outcome"] == "PASS" else 502


@app.post("/api/run/stream")
def stream_query():
    body = request.get_json()
    utterance = body["utterance"]
    tool_set = body.get("tool_set", "navigation")
    queue = Queue()
    began = time.monotonic()

    def emit(event, data):
        queue.put((event, {**data, "event_elapsed_ms": (time.monotonic() - began) * 1000}))

    def produce():
        try:
            # Callbacks fire at the real KEV/LLM boundaries, not after run() returns.
            result = run(utterance, on_stage=emit, tool_set=tool_set)
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
        emit("accepted", {"utterance": utterance, "tool_set": tool_set, "asr": "text_standin", "mock_only": True})
        # One producer bridges the synchronous stage callbacks into this SSE response.
        Thread(target=produce, daemon=True).start()
        while True:
            event, data = queue.get()
            yield f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False, allow_nan=False)}\n\n"
            if event in {"done", "error"}:
                break

    return Response(stream_with_context(events()), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


@app.post("/api/session/stream")
def session_stream():
    """Replay scripted ASR updates in real time through TurnSession; one event per SSE message."""
    from turn_session import TurnConfig, TurnSession
    body = request.get_json()
    updates = body.get("updates") or []
    if not isinstance(updates, list) or not updates:
        return jsonify({"error": "updates required"}), 400
    config = dict(body.get("config") or {})
    if "tool_set" in body:
        config["tool_set"] = body["tool_set"]
    try:
        cfg = TurnConfig.from_dict(config)
    except (TypeError, ValueError) as error:
        return jsonify({"error": type(error).__name__ + ": " + str(error)}), 400
    if cfg.tool_set not in {"navigation", "multidomain"}:
        return jsonify({"error": "Unknown tool_set"}), 400
    timeline = sorted([{"t_ms": item["t_ms"], "type": "asr", "text": item["text"],
                        "final": item.get("final", False)} for item in updates] +
                      [{"t_ms": item["t_ms"], "type": "vad", "vad_kind": item["kind"]}
                       for item in (body.get("vad") or [])],
                      key=lambda item: item["t_ms"])
    queue = Queue()
    session_id = "ui-" + str(int(time.time() * 1000))
    session = TurnSession(cfg, queue.put, session_id=session_id, warm_fn=warm_slow_model)

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
    control = {"stop": False, "closed": False, "asr_done": False, "stop_ack": False}
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
                cfg = TurnConfig.from_dict(overrides)
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
