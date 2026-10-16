"""M4 acceptance: feed a wav as 20 ms/16 kHz PCM frames into /api/session/mic and check the turn."""
import json
from pathlib import Path
import sys
import time
import wave

from simple_websocket import Client

URL = sys.argv[1] if len(sys.argv) > 1 else "ws://127.0.0.1:8765/api/session/mic"
WAV = sys.argv[2] if len(sys.argv) > 2 else "/tmp/asr-zh.wav"
FRAME_MS, RATE = 20, 16000
FRAME = int(RATE * 2 * FRAME_MS / 1000)


def main():
    with wave.open(WAV, "rb") as handle:
        pcm = handle.readframes(handle.getnframes())
    ws = Client.connect(URL, receive_bytes=65536, max_message_size=1 << 22)
    messages = []

    def drain(budget=0.0):
        while True:
            try:
                message = ws.receive(timeout=budget)
            except TimeoutError:
                return
            except Exception:
                return
            if message is None:
                return
            messages.append(json.loads(message))

    ws.send(json.dumps({"cmd": "config", "tool_set": "navigation", "config": {"speculative_llm": True}}))
    drain(1.0)
    print("ready:", json.dumps(messages[0] if messages else None, ensure_ascii=False))

    started = time.monotonic()
    for offset in range(0, len(pcm), FRAME):
        ws.send(pcm[offset:offset + FRAME])
        target = started + (offset + FRAME) / (RATE * 2)
        while time.monotonic() < target:
            drain(0.001)
            time.sleep(0.002)
    print(f"sent {len(pcm)} bytes in {(time.monotonic()-started)*1000:.0f} ms")
    ws.send(json.dumps({"cmd": "stop"}))
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        drain(0.2)
        if any(item["kind"] == "event" and item["data"].get("event") == "turn_done" for item in messages):
            break
    ws.send(json.dumps({"cmd": "save", "label": True}))
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        drain(0.2)
        if any(item["kind"] == "saved" for item in messages):
            break
    ws.close()

    asr = [item["data"] for item in messages if item["kind"] == "asr"]
    events = [item["data"] for item in messages if item["kind"] == "event"]
    saved = next((item["data"] for item in messages if item["kind"] == "saved"), None)
    errors = [item["data"] for item in messages if item["kind"] == "error"]
    partials = [item for item in asr if not item["final"]]
    finals = [item for item in asr if item["final"]]
    done = next((event for event in events if event["event"] == "turn_done"), None)
    decisions = [event for event in events if event["event"] == "llm_decision"]

    print(f"asr updates: {len(asr)} (partials={len(partials)} finals={len(finals)})")
    for item in asr:
        print(f"   t={item['t_ms']:>6}ms final={item['final']} text={item['text']!r}")
    print("events:", ", ".join(event["event"] for event in events))
    if done:
        print("turn_done:", json.dumps({key: done[key] for key in
              ("t_last_word", "t_confirmed", "t_llm_start", "t_decision", "eot_calls", "eot_skipped",
               "tool_calls", "llm_calls", "llm_wasted", "timeline_path")}, ensure_ascii=False))
    print("errors:", errors)
    print("saved:", json.dumps(saved, ensure_ascii=False))
    if saved:
        record = json.loads(Path(saved["path"]).read_text().strip())
        print("case file keys:", sorted(record))
        print(f"case file: asr_updates={len(record['asr_updates'])} events={len(record['events'])} "
              f"label_complete={record['label_complete']}")

    checks = [
        ("≥2 ASR partials", len(partials) >= 2, len(partials)),
        ("≥1 ASR final", len(finals) >= 1, len(finals)),
        ("turn_done reached", done is not None, ""),
        ("exactly 1 applied decision", len(decisions) == 1, len(decisions)),
        ("case file written with ASR updates + label", bool(saved) and Path(saved["path"]).is_file()
         and json.loads(Path(saved["path"]).read_text().strip())["label_complete"] is True, saved),
        ("no protocol errors", not errors, errors),
    ]
    for name, ok, note in checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name} {note}")
    passed = all(ok for _, ok, _ in checks)
    print(f"M4 RESULT: {'PASS' if passed else 'FAIL'} ({sum(ok for _, ok, _ in checks)}/{len(checks)})")
    return 0 if passed else 1


if __name__ == "__main__":
    raise SystemExit(main())