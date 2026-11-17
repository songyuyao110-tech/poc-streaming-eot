"""A5: speculative LLM start must be canceled/discarded when the user continues (design §3.4)."""
import codecs
import json
import sys
import urllib.request

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8765

# A long enough hold after a "complete" prefix that the shortlist is ready and the decision
# actually starts, then the user continues with more words.
# The gap before the continuation must exceed the KEV shortlist round so the decision really starts
# (measured ~0.4-0.9 s); 3.2 s keeps the check about cancellation, not about timing.
# 2026-11-04 去窗：强 complete 判完当刻进 CONFIRMED 并起决策，不再靠「长确认窗把 MAYBE_DONE 撑到短名单
# 就绪」那个技巧；这条用例要钉的是「在途决策被续句作废」，`speculative` 标记不再适用。
updates = [{"t_ms": 300, "text": "导航回家", "final": False},
           {"t_ms": 2800, "text": "吧", "final": False},
           {"t_ms": 3100, "text": "吧我想想", "final": True}]
vad = [{"t_ms": 0, "kind": "speech_start"}, {"t_ms": 350, "kind": "speech_end"},
       {"t_ms": 2500, "kind": "speech_start"}, {"t_ms": 3150, "kind": "speech_end"}]


def run():
    body = {"updates": updates, "vad": vad, "tool_set": "core_score",
            # max_silence must stay above the (weak-complete) settle window, otherwise it force-confirms
            # before the reversal. `confirm_silence_ms` 现在只挂「可能说完」那一档（去窗后强 complete
            # 不等它），留着只为弱 complete 的样本；spec_margin 只在 argmax/margin 策略下生效。
            "config": {"speculative_llm": True, "spec_margin": 0.0, "confirm_silence_ms": 4000,
                       "max_silence_ms": 6000}}
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


if __name__ == "__main__":
    events = run()
    for event in events:
        name = event["event"]
        if name in {"asr_update", "vad"}:
            continue
        detail = {key: value for key, value in event.items()
                  if key not in {"event", "t_ms", "text_version", "session_id", "turns", "timeline_path"}}
        if name == "eot_scores":
            detail = {key: detail[key] for key in ("label", "margin", "stale") if key in detail}
        if name == "llm_decision":
            detail = {"tool": detail["decision"].get("tool")}
        print(f"  {event['t_ms']:>7}ms v{event['text_version']:<3} {name:<14} {json.dumps(detail, ensure_ascii=False)[:150]}")
    last_asr = max(event["t_ms"] for event in events if event["event"] == "asr_update")
    # 去窗后强 complete 在 CONFIRMED 当刻起决策（speculative=False），所以这里只认「续句之前已经起了
    # 决策」，不再要求它带投机标记——用例钉的是「作废」，不是「何时抢跑」。
    spec_starts = [event for event in events if event["event"] == "llm_start"
                   and event["t_ms"] < last_asr]
    cancels = [event for event in events if event["event"] == "llm_cancel"]
    decisions = [event for event in events if event["event"] == "llm_decision"]
    done = next(event for event in events if event["event"] == "turn_done")
    results = [
        check("A5 a decision started before the user continued", bool(spec_starts),
              f"{[e['t_ms'] for e in spec_starts]}"),
        check("A5 it was canceled/discarded on continuation", bool(cancels),
              json.dumps([{"t_ms": e["t_ms"], "reason": e.get("reason"), "prepared": e.get("prepared")} for e in cancels], ensure_ascii=False)),
        check("A5 exactly one valid decision was applied", len(decisions) == 1, f"decisions={len(decisions)}"),
        check("A5 the waste is recorded", done["llm_wasted"] >= 1,
              f"llm_calls={done['llm_calls']} llm_wasted={done['llm_wasted']}"),
    ]
    print(f"A5 RESULT: {'PASS' if all(results) else 'FAIL'} ({sum(results)}/{len(results)})")
    raise SystemExit(0 if all(results) else 1)