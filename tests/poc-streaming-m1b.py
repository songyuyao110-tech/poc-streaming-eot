"""M1b acceptance: real streaming ASR (Volcengine, wav replay) driving TurnSession end to end."""
import json
import sys
import threading
import time
from pathlib import Path
from queue import Empty, Queue

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import bootstrap
bootstrap.install()          # 上游仓库根
sys.dont_write_bytecode = True

from asr_stream import stream_asr
from turn_session import TurnConfig, TurnSession

WAV = sys.argv[1] if len(sys.argv) > 1 else "/tmp/asr-zh.wav"
updates = Queue()
errors = []


def produce():
    try:
        for update in stream_asr(WAV):
            updates.put(update)
    except Exception as error:
        errors.append(error)
    finally:
        updates.put(None)


events = []
session = TurnSession(TurnConfig(), events.append, session_id="asr-" + str(int(time.time())))
threading.Thread(target=produce, daemon=True).start()
started = time.monotonic() * 1000
partials = finals = 0
deadline = started + 30000
while not session.turn_done and time.monotonic() * 1000 < deadline:
    try:
        item = updates.get(timeout=0.005)
    except Empty:
        item = "idle"
    if item is None:
        break
    if item != "idle":
        t_ms, text, final = item
        print(f"  asr  t={t_ms:>5}ms final={final} text={text!r}")
        partials += 0 if final else 1
        finals += 1 if final else 0
        session.on_asr(t_ms, text, final)
    session.tick(time.monotonic() * 1000 - started)

while not session.turn_done and time.monotonic() * 1000 < started + 30000:
    session.tick(time.monotonic() * 1000 - started)
    time.sleep(0.005)

print(f"  partials={partials} finals={finals} errors={[str(e) for e in errors]}")
for event in events:
    if event["event"] in {"vad"}:
        continue
    detail = {key: value for key, value in event.items() if key not in {"event", "t_ms", "text_version", "session_id"}}
    if event["event"] == "eot_scores":
        detail = {key: detail[key] for key in ("label", "margin")}
    if event["event"] == "llm_decision":
        detail = detail["decision"]
    if event["event"] == "turn_done":
        detail = {key: detail[key] for key in ("t_last_word", "t_confirmed", "t_llm_start", "t_decision",
                                               "eot_calls", "eot_skipped", "tool_calls", "llm_calls", "llm_wasted",
                                               "timeline_path")}
    print(f"  {event['event']:<14} {json.dumps(detail, ensure_ascii=False)[:200]}")

applied = sum(event["event"] == "llm_decision" for event in events)
summary = session.summary()
ok = session.turn_done and partials >= 2 and finals >= 1 and applied == 1
print(f"  note: premature LLM trigger then cancel is expected while the real partial cadence exceeds "
      f"confirm_silence_ms={session.cfg.confirm_silence_ms}ms (llm_calls={summary['llm_calls']}, llm_wasted={summary['llm_wasted']})")
print(f"M1b RESULT: {'PASS' if ok else 'FAIL'} (partials={partials} >=2, finals={finals} >=1, "
      f"applied_decisions={applied} ==1, turn_done={session.turn_done})")
session.close()
raise SystemExit(0 if ok else 1)