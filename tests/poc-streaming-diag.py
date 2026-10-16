"""Diagnose replay-time vs real-time behaviour of TurnSession against the real KEV/LLM."""
import json
import re
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import bootstrap
bootstrap.install()          # 上游仓库根
sys.dont_write_bytecode = True

from turn_session import TurnConfig, TurnSession
from turn_eot import score_eot
from core_pipeline_run import select_tools, decide

PAUSE = re.compile(r"\|(\d+)\|")
SPEED = 6.0
T0 = time.monotonic() * 1000


def real_ms():
    return time.monotonic() * 1000 - T0


def build(text):
    updates, vad = [], [{"t_ms": 0, "kind": "speech_start"}]
    plain, t = "", 0.0
    for chunk in PAUSE.split(text):
        if chunk.isdigit():
            vad += [{"t_ms": int(t), "kind": "speech_end"}]
            t += float(chunk)
            vad += [{"t_ms": int(t), "kind": "speech_start"}]
            continue
        for char in chunk or "":
            t += 1000.0 / SPEED
            plain += char
            updates.append({"t_ms": int(round(t)), "text": plain, "final": False})
    if updates:
        updates[-1]["final"] = True
        vad.append({"t_ms": updates[-1]["t_ms"] + 20, "kind": "speech_end"})
    return updates, vad


def timed(label, fn):
    def wrapper(*args, **kwargs):
        started = real_ms()
        try:
            return fn(*args, **kwargs)
        finally:
            print(f"    [worker {label}] real {started:.0f} -> {real_ms():.0f} ms ({real_ms()-started:.0f} ms)", flush=True)
    return wrapper


def run(text, config=None):
    print("=" * 100)
    print(f"input {text!r} config={config}")
    events = []

    def on_event(event):
        events.append(event)
        name = event["event"]
        detail = ""
        if name == "eot_scores":
            detail = f"label={event['label']} margin={event['margin']:.3f} wall={event['wall_ms']:.0f}"
        if name == "eot_skipped":
            detail = f"reason={event['reason']} text={event.get('text')!r}"
        if name == "state_change":
            detail = f"{event['from']}->{event['to']} ({event['why']})"
        if name == "llm_decision":
            detail = event["decision"].get("tool")
        if name == "turn_done":
            detail = json.dumps({k: event[k] for k in ("t_last_word", "t_confirmed", "t_llm_start", "t_decision",
                                                       "eot_calls", "eot_skipped", "tool_calls", "llm_calls", "llm_wasted")})
        print(f"  replay {event['t_ms']:>7.0f} | real {real_ms():>7.0f} | v{event['text_version']:<3} {name:<14} {detail}", flush=True)

    session = TurnSession(TurnConfig(**(config or {})), on_event,
                          eot_fn=timed("eot", score_eot),
                          select_tools_fn=timed("tools", select_tools),
                          decide_fn=timed("llm", decide),
                          session_id="diag", evidence_root=None)
    updates, vad = build(text)
    timeline = sorted([{**u, "kind": "asr"} for u in updates] + [{**v, "kind": "vad"} for v in vad],
                      key=lambda item: item["t_ms"])
    for item in timeline:
        while True:
            now = real_ms()
            if now >= item["t_ms"] or session.turn_done:
                break
            session.tick(min(now, item["t_ms"]))
            time.sleep(0.005)
        if session.turn_done:
            break
        if item["kind"] == "asr":
            session.on_asr(item["t_ms"], item["text"], item.get("final", False))
        else:
            session.on_vad(item["t_ms"], item["kind"])
    deadline = real_ms() + 20000
    while not session.turn_done and real_ms() < deadline:
        session.tick(real_ms())
        time.sleep(0.005)
    print("  summary:", json.dumps(session.summary(), ensure_ascii=False))
    session.close()


if __name__ == "__main__":
    run("导航回家")
    run("等一下我想想|2500|回家")