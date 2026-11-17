"""A3 acceptance: drive the four V2 scripts through /api/session/stream and check the events."""
import codecs
import json
import re
import sys
import urllib.request

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 8767
SPEED = 6.0
PAUSE = re.compile(r"\|(\d+)\|")


def build_script(text, speed=SPEED):
    """`|600|` = a 600 ms pause that also closes the ASR segment; the next chunk starts a fresh one."""
    updates, vad = [], [{"t_ms": 0, "kind": "speech_start"}]
    plain, t = "", 0.0
    for chunk in PAUSE.split(text):
        if chunk.isdigit():
            vad.append({"t_ms": int(t), "kind": "speech_end"})
            t += float(chunk)
            vad.append({"t_ms": int(t), "kind": "speech_start"})
            plain = ""
            continue
        if not chunk:
            continue
        for char in chunk:
            t += 1000.0 / speed
            plain += char
            updates.append({"t_ms": int(round(t)), "text": plain, "final": False})
    if updates:
        updates[-1]["final"] = True
        vad.append({"t_ms": updates[-1]["t_ms"] + 20, "kind": "speech_end"})
    return updates, vad


def replay(text, config=None, tool_set="core_score"):
    updates, vad = build_script(text)
    body = {"updates": updates, "vad": vad, "tool_set": tool_set, "config": config or {}}
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
                name = next(line[7:] for line in lines if line.startswith("event: "))
                events.append(json.loads(next(line[6:] for line in lines if line.startswith("data: "))))
    return updates, vad, events


def show(text, events, keep_asr=False):
    print("=" * 100)
    print(f"input: {text!r}")
    for event in events:
        name = event["event"]
        if name in {"asr_update", "vad"} and not keep_asr:
            continue
        detail = {key: value for key, value in event.items()
                  if key not in {"event", "t_ms", "text_version", "session_id", "turns", "timeline_path"}}
        if name == "eot_scores":
            detail = {key: round(value, 4) if isinstance(value, float) else value
                      for key, value in detail.items() if key in {"complete", "cut", "wait", "label", "margin", "wall_ms", "kev_ms"}}
        if name == "tools_scores":
            detail = {"reused": detail.get("reused"), "selected": detail.get("selected"),
                      "top": [f"{item['name']}:{item['score']:.3f}" for item in detail.get("top", [])]}
        if name == "llm_decision":
            detail = {"tool": detail["decision"].get("tool"), "head": detail.get("decision_head")}
        print(f"  {event['t_ms']:>6}ms v{event['text_version']:<3} {name:<14} {json.dumps(detail, ensure_ascii=False)}")
    return events


def turn_done(events):
    return next((event for event in events if event["event"] == "turn_done"), None)


def check(name, ok, note=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {note}")
    return bool(ok)


if __name__ == "__main__":
    results = []

    # 1) 导航回家 → exactly one applied decision.
    #    期望已因工具集更换而改写（旧的 navigation 14 工具要求 llm_wasted == 0）：
    #    core_score 是 242/17 两阶段 KEV，第一个 partial（1 个字）的工具轮会先返回一个空短名单，
    #    M4 的 allow_stale_shortlist 会先按这个旧短名单发起决策，短名单落地变化后按 shortlist_changed
    #    重发一次 —— 这正是 M4 设计的行为，浪费被记进 llm_wasted。见 docs/spec-core-score-tools.md §5 S4。
    _, _, events = replay("导航回家")
    show("导航回家", events)
    done = turn_done(events)
    decisions = [event for event in events if event["event"] == "llm_decision"]
    reissues = [event for event in events if event["event"] == "llm_cancel"
                and event.get("reason") == "shortlist_changed"]
    print(f"  turn_done: {json.dumps({k: done[k] for k in ('t_last_word','t_confirmed','t_llm_start','t_decision','eot_calls','eot_skipped','tool_calls','llm_calls','llm_wasted','timeline_path')}, ensure_ascii=False)}")
    results.append(check("A3-1 exactly 1 applied llm_decision", len(decisions) == 1, f"got {len(decisions)}"))
    results.append(check("A3-1 decision used the final shortlist (fresh)",
                         bool(decisions) and decisions[-1].get("shortlist_source") in {None, "fresh"},
                         f"{decisions[-1].get('shortlist_source') if decisions else None}"))
    results.append(check("A3-1 waste is only the M4 shortlist reissue (<= 1)", done["llm_wasted"] <= 1,
                         f"got {done['llm_wasted']} reissues={len(reissues)}"))

    # 2) 我去买菜|600|对了还要买酱油 → one valid decision on the merged text.
    _, _, events = replay("我去买菜|600|对了还要买酱油")
    show("我去买菜|600|对了还要买酱油", events)
    done = turn_done(events)
    decisions = [event for event in events if event["event"] == "llm_decision"]
    cancels = [event for event in events if event["event"] == "llm_cancel"]
    first_decision = decisions[0] if decisions else {}
    print(f"  decision text input: {first_decision.get('decision')}")
    print(f"  cancels: {json.dumps([{'t_ms': e['t_ms'], 'reason': e['reason']} for e in cancels], ensure_ascii=False)}")
    asr = [event for event in events if event["event"] == "asr_update"]
    print(f"  final asr text: {asr[-1]['text'] if asr else None!r}")
    results.append(check("A3-2 exactly 1 llm_decision", len(decisions) == 1, f"got {len(decisions)}"))
    results.append(check("A3-2 decision used the merged text",
                         decisions and asr and decisions[0]["t_ms"] >= asr[-1]["t_ms"]))
    results.append(check("A3-2 premature LLM call was canceled (if any)",
                         not any(event["event"] == "llm_start" and event["t_ms"] < asr[-1]["t_ms"] for event in events)
                         or bool(cancels)))


    # 3) 帮我导航到…|1500|…人民广场 → no LLM call inside the 1500 ms pause.
    _, _, events = replay("帮我导航到|1500|人民广场")
    show("帮我导航到|1500|人民广场", events)
    done = turn_done(events)
    asr = [event for event in events if event["event"] == "asr_update"]
    pause_start = [event for event in events if event["event"] == "vad" and event["kind"] == "speech_end"]
    decisions = [event for event in events if event["event"] == "llm_decision"]
    llm_starts = [event for event in events if event["event"] == "llm_start"]
    if pause_start:
        t_pause, deadline = pause_start[0]["t_ms"], pause_start[0]["t_ms"] + 1500
        early = [event for event in llm_starts if event["t_ms"] < deadline]
        results.append(check("A3-3 no LLM start inside the 1500ms pause", not early,
                             f"llm_starts={[e['t_ms'] for e in llm_starts]} pause@{t_pause} deadline={deadline}"))
    results.append(check("A3-3 exactly 1 llm_decision", len(decisions) == 1, f"got {len(decisions)}"))
    print(f"  final asr text: {asr[-1]['text'] if asr else None!r}")

    # 4) 等一下我想想|2500|回家 → no LLM during THINK_WAIT.
    _, _, events = replay("等一下我想想|2500|回家")
    show("等一下我想想|2500|回家", events)
    states = [event for event in events if event["event"] == "state_change"]
    think = [event for event in states if event["to"] == "THINK_WAIT"]
    llm_starts = [event for event in events if event["event"] == "llm_start"]
    if think:
        enter, leave = think[0]["t_ms"], next((event["t_ms"] for event in states
                                              if event["t_ms"] > think[0]["t_ms"] and event["to"] != "THINK_WAIT"), None)
        during = [event for event in llm_starts if leave and enter <= event["t_ms"] < leave]
        results.append(check("A3-4 no LLM start during THINK_WAIT", not during,
                             f"THINK_WAIT {enter}..{leave} llm_starts={[e['t_ms'] for e in llm_starts]}"))
    else:
        results.append(check("A3-4 reached THINK_WAIT", False, "no THINK_WAIT state"))
    decisions = [event for event in events if event["event"] == "llm_decision"]
    results.append(check("A3-4 exactly 1 llm_decision", len(decisions) == 1, f"got {len(decisions)}"))

    print("=" * 100)
    print(f"A3 RESULT: {'PASS' if all(results) else 'FAIL'} ({sum(results)}/{len(results)})")
    raise SystemExit(0 if all(results) else 1)