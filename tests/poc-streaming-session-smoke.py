"""Scratch smoke test for TurnSession with fake EOT/tools/decide (no KEV, no LLM)."""
import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import bootstrap
bootstrap.install()          # 上游仓库根
sys.dont_write_bytecode = True

from turn_session import TurnConfig, TurnSession

SPEED = 6.0  # chars per second in the simulated timeline
PAUSE = re.compile(r"\|(\d+)\|")


def build_script(text, *, speech_end_at_pause=True):
    """Return (updates, vad) in simulated ms; |600| is a pause."""
    updates, vad = [], []
    plain, t = "", 0.0
    vad.append({"t_ms": 0, "kind": "speech_start"})
    for chunk in PAUSE.split(text):
        if chunk.isdigit():
            pause = float(chunk)
            if speech_end_at_pause:
                vad.append({"t_ms": int(t), "kind": "speech_end"})
            t += pause
            if speech_end_at_pause:
                vad.append({"t_ms": int(t), "kind": "speech_start"})
            continue
        if not chunk:
            continue
        buffer = plain
        for char in chunk:
            t += 1000.0 / SPEED
            buffer += char
            updates.append({"t_ms": int(t), "text": buffer, "final": False})
        plain = buffer
    if updates:
        updates[-1]["final"] = True
        vad.append({"t_ms": updates[-1]["t_ms"] + 20, "kind": "speech_end"})
    return updates, vad


def fake_eot(text):
    time.sleep(0.02)
    if text.endswith(("到", "要", "还要")) or len(text) < 3:
        return {"complete": 0.2, "cut": 0.7, "wait": 0.1, "label": "cut", "margin": -0.5, "wall_ms": 20.0, "kev_ms": 10.0}
    return {"complete": 0.8, "cut": 0.15, "wait": 0.05, "label": "complete", "margin": 0.65, "wall_ms": 20.0, "kev_ms": 10.0}


def fake_tools(text, tool_set, context):
    time.sleep(0.05)
    return {"scores": {"tool:car_navi_to_destination": 0.9, "tool:car_navi_add_waypoint": 0.7, "tool:car_navi_cancel": 0.1},
            "selected": ["car_navi_to_destination", "car_navi_add_waypoint"],
            "schemas": [{"name": "car_navi_to_destination", "description": "d", "parameters": {"type": "object"}}],
            "native_schemas": None, "domains": None,
            "timing": {"stage1_ms": 50.0, "stage2_ms": 0.0, "total_ms": 50.0}}


def fake_decide(utterance, selected, schemas, tool_set, context, **kwargs):
    time.sleep(0.1)
    return {"decision": {"kind": "tool_call", "tool": "car_navi_to_destination", "arguments": {"poi_type": "home"}},
            "llm_ms": 100.0, "log_fields": {"decision_head": "mock"}}


def run_case(text, config=None, **kwargs):
    cfg = TurnConfig(**(config or {}))
    events = []
    session = TurnSession(cfg, events.append, eot_fn=fake_eot, select_tools_fn=fake_tools, decide_fn=fake_decide,
                          session_id="smoke", evidence_root=None)
    updates, vad = build_script(text, **kwargs)
    timeline = sorted([{**u, "kind": "asr"} for u in updates] + [{**v, "kind": "vad"} for v in vad],
                      key=lambda item: item["t_ms"])
    start = time.monotonic()
    budget = 12000.0
    index = 0
    while index < len(timeline):
        item = timeline[index]
        while (time.monotonic() - start) * 1000 < item["t_ms"] and not session.turn_done:
            now = int((time.monotonic() - start) * 1000)
            session.tick(now)
            if not session.idle():
                time.sleep(0.005)
            elif (time.monotonic() - start) * 1000 < item["t_ms"] - 5:
                time.sleep(0.005)
        if session.turn_done:
            break
        if item["kind"] == "asr":
            session.on_asr(item["t_ms"], item["text"], item["final"])
        else:
            session.on_vad(item["t_ms"], item["kind"])
        index += 1
    while not session.turn_done and (time.monotonic() - start) * 1000 < budget:
        session.tick(int((time.monotonic() - start) * 1000))
        time.sleep(0.005)
    print("=" * 70)
    print(f"input={text!r} cfg={{'eot_policy': cfg.eot_policy, 'speculative_llm': cfg.speculative_llm}}")
    for event in events:
        name = event["event"]
        if name in {"asr_update", "vad"}:
            continue
        detail = {k: v for k, v in event.items() if k not in {"event", "t_ms", "text_version"}}
        if name == "eot_scores":
            detail = {k: detail[k] for k in ("complete", "cut", "wait", "label", "margin")}
        if name == "llm_decision":
            detail = {"decision": detail["decision"].get("tool")}
        print(f"  {event['t_ms']:>6} v{event['text_version']:<3} {name:<14} {json.dumps(detail, ensure_ascii=False)}")
    summary = session.summary()
    print("  summary:", json.dumps({k: summary[k] for k in
                                   ("t_last_word", "t_confirmed", "t_llm_start", "t_decision", "eot_calls",
                                    "eot_skipped", "tool_calls", "llm_calls", "llm_wasted")}, ensure_ascii=False))
    session.close()
    return events


if __name__ == "__main__":
    run_case("导航回家")
    run_case("我去买菜|600|对了还要买酱油")
    run_case("帮我导航到|1500|人民广场")
    run_case("等一下我想想|2500|回家")
    run_case("导航回家", {"speculative_llm": True})