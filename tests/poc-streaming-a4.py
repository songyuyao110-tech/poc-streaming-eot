"""A4 acceptance: timeline.json + turn_done fields + tool-shortlist reuse on unchanged text."""
import codecs
import json
import re
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import bootstrap
bootstrap.install()          # 上游仓库根
sys.dont_write_bytecode = True
from turn_session import TurnConfig, TurnSession

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8767
PUNCT = re.compile(r"[\s，。！？、；：,.!?;:\"'“”‘’()（）\[\]【】\-—…~]+")


def normalise(text):
    return PUNCT.sub("", text or "")
REQUIRED_TURN_DONE = ["t_last_word", "t_confirmed", "t_llm_start", "t_decision",
                      "eot_calls", "eot_skipped", "tool_calls", "llm_calls", "llm_wasted"]


def post(body):
    request = urllib.request.Request(f"http://127.0.0.1:{PORT}/api/session/stream",
                                     data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    events = []
    with urllib.request.urlopen(request, timeout=180) as response:
        buffer, decoder = "", codecs.getincrementaldecoder("utf-8")()
        while True:
            chunk = response.read(1)
            if not chunk:
                break
            buffer += decoder.decode(chunk)
            while "\n\n" in buffer:
                packet, buffer = buffer.split("\n\n", 1)
                lines = packet.split("\n")
                events.append(json.loads(next(line[6:] for line in lines if line.startswith("data: "))))
    return events


def check(name, ok, note=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {note}")
    return bool(ok)


# "导航回家" + a final update that only adds punctuation: normalised text is unchanged.
updates = [{"t_ms": 300, "text": "导", "final": False}, {"t_ms": 500, "text": "导航", "final": False},
           {"t_ms": 700, "text": "导航回", "final": False}, {"t_ms": 900, "text": "导航回家", "final": False},
           {"t_ms": 1050, "text": "导航回家。", "final": True}]
vad = [{"t_ms": 0, "kind": "speech_start"}, {"t_ms": 1070, "kind": "speech_end"}]
events = post({"updates": updates, "vad": vad, "tool_set": "navigation"})

print("=" * 100)
for event in events:
    if event["event"] in {"asr_update", "vad"}:
        continue
    detail = {key: value for key, value in event.items() if key not in {"event", "t_ms", "text_version", "session_id"}}
    if event["event"] == "eot_scores":
        detail = {key: round(value, 4) if isinstance(value, float) else value for key, value in detail.items()
                  if key in {"label", "margin", "wall_ms", "kev_ms"}}
    if event["event"] == "llm_decision":
        detail = {"tool": detail["decision"].get("tool")}
    print(f"  {event['t_ms']:>7}ms v{event['text_version']:<3} {event['event']:<14} {json.dumps(detail, ensure_ascii=False)[:180]}")

results = []
skipped = [event for event in events if event["event"] == "eot_skipped" and event.get("reason") == "unchanged"]
results.append(check("A4 unchanged final update skipped EOT", bool(skipped),
                     f"{[(e['t_ms'], e.get('text')) for e in skipped]}"))

last_changed = max(event["t_ms"] for event in events if event["event"] == "asr_update" and event.get("changed"))
scored, duplicates = [], []
for event in events:
    if event["event"] == "tools_start" and normalise(event.get("text")) in scored:
        duplicates.append((event["t_ms"], event.get("text")))
    if event["event"] == "tools_scores":
        scored.append(normalise(event.get("text")))
results.append(check("A4 no tool round for an already-scored text", not duplicates,
                     f"scored={scored} duplicates={duplicates}"))

done = next(event for event in events if event["event"] == "turn_done")
missing = [key for key in REQUIRED_TURN_DONE if key not in done]
results.append(check("A4 turn_done has every summary field", not missing, f"missing={missing}"))
results.append(check("A4 exactly 1 llm_decision", sum(event["event"] == "llm_decision" for event in events) == 1))

path = Path(done["timeline_path"])
results.append(check("A4 timeline.json written", path.is_file(), str(path)))
if path.is_file():
    timeline = json.loads(path.read_text())
    keys_ok = all(key in timeline for key in ("session_id", "config", "events", "turns", "summary"))
    results.append(check("A4 timeline.json structure", keys_ok, f"keys={sorted(timeline)}"))
    results.append(check("A4 timeline.json event count matches SSE", len(timeline["events"]) == len(events),
                         f"{len(timeline['events'])} vs {len(events)}"))
    results.append(check("A4 timeline.json config round-trips", timeline["config"].get("tool_set") == "navigation"))

def deterministic_reuse():
    """The same normalised text must not trigger a second tool-scoring round (injected fast callables)."""
    print("=" * 100)
    print("deterministic reuse check (stub EOT/tools/decide, no KEV/LLM)")
    scored, events = [], []

    def eot(text):
        return {"complete": 0.8, "cut": 0.1, "wait": 0.1, "label": "complete", "margin": 0.7,
                "wall_ms": 1.0, "kev_ms": 1.0}

    def tools(text, tool_set, context):
        scored.append(text)
        return {"scores": {"tool:navi_to_special_poi": 0.9}, "selected": ["navi_to_special_poi"],
                "schemas": [{"name": "navi_to_special_poi", "description": "d", "parameters": {"type": "object"}}],
                "native_schemas": None, "domains": None, "timing": {"total_ms": 1.0}}

    def decide(utterance, selected, schemas, tool_set, context, **kwargs):
        return {"decision": {"kind": "tool_call", "tool": "navi_to_special_poi", "arguments": {}},
                "llm_ms": 1.0, "log_fields": {"decision_head": "stub"}}

    session = TurnSession(TurnConfig(debounce_ms=0), events.append, eot_fn=eot, select_tools_fn=tools,
                          decide_fn=decide, session_id="a4-reuse", evidence_root=None)
    started = time.monotonic() * 1000
    now = lambda: time.monotonic() * 1000 - started

    def pump(until_ms):
        while now() < until_ms or not session.idle():
            session.tick(now())
            time.sleep(0.002)
            if session.turn_done:
                return

    session.on_vad(0, "speech_start")
    session.on_asr(100, "导航回家", False)
    pump(180)
    session.on_asr(200, "导航回家。", True)      # same normalised text
    session.on_vad(250, "speech_end")
    limit = now() + 3000
    while not session.turn_done and now() < limit:
        session.tick(now())
        time.sleep(0.002)
    counts = session.summary()
    print(f"  tools calls: {scored} · eot_calls={counts['eot_calls']} eot_skipped={counts['eot_skipped']} "
          f"tool_calls={counts['tool_calls']} llm_calls={counts['llm_calls']}")
    print(f"  events: {[event['event'] for event in events]}")
    session.close()
    return [check("A4 reuse: only one tool round for unchanged text", scored == ["导航回家"], f"{scored}"),
            check("A4 reuse: unchanged update skipped EOT", counts["eot_skipped"] == 1, f"{counts['eot_skipped']}"),
            check("A4 reuse: exactly one decision", counts["llm_calls"] == 1, f"{counts['llm_calls']}")]


print("=" * 100)
print(f"A4 RESULT: {'PASS' if all(results) else 'FAIL'} ({sum(results)}/{len(results)})")
if "--with-reuse" in sys.argv:
    results += deterministic_reuse()
    print(f"A4(+reuse) RESULT: {'PASS' if all(results) else 'FAIL'} ({sum(results)}/{len(results)})")
raise SystemExit(0 if all(results) else 1)