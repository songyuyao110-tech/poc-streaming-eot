"""C1-C4 acceptance for streaming multi-segment consumption (spec-streaming-multi-llm §5).

Deterministic on purpose: TurnSession is driven with a virtual clock and injected callables
(EOT always says "complete" with margin 0.9, tools and decide return immediately), so the expected
LLM call count and the exact text handed to each decide() are not exposed to KEV/LLM variance.

    python tests/poc-streaming-c1c4.py
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import bootstrap
bootstrap.install()          # 上游仓库根
sys.dont_write_bytecode = True
from turn_session import EVIDENCE_ROOT, TurnConfig, TurnSession, normalize

# Acceptance-only overrides (spec §5); TurnConfig defaults are untouched.
BASE = {"speculative_llm": False, "confirm_silence_ms": 300, "max_silence_ms": 1500,
        "debounce_ms": 0, "shortlist_reissue": False, "warm_llm": False, "eot_max_inflight": 1,
        "tools_max_inflight": 1, "eot_policy": "argmax", "tool_set": "core_score"}
SPEC = {**BASE, "speculative_llm": True, "spec_margin": 0.0}
EOT_STUB = {"complete": 0.9, "cut": 0.05, "wait": 0.05, "label": "complete", "margin": 0.9,
            "wall_ms": 1.0, "kev_ms": 1.0}
STEP = 20


class Harness:
    def __init__(self, config, session_id, evidence_root=None):
        self.now = 0.0
        self.events = []
        self.decide_inputs = []
        self.tool_inputs = []
        self.session = TurnSession(TurnConfig.from_dict(config), self.events.append,
                                   eot_fn=lambda text: dict(EOT_STUB), select_tools_fn=self._tools,
                                   decide_fn=self._decide, session_id=session_id,
                                   evidence_root=evidence_root)

    def _tools(self, text, tool_set, context):
        self.tool_inputs.append(text)
        return {"scores": {"tool:car_navi_to_destination": 0.9}, "selected": ["car_navi_to_destination"],
                "schemas": [{"name": "car_navi_to_destination", "description": "d",
                             "parameters": {"type": "object"}}],
                "native_schemas": None, "domains": None, "timing": {"total_ms": 1.0}}

    def _decide(self, utterance, selected, schemas, tool_set, context, **kwargs):
        self.decide_inputs.append(utterance)
        return {"decision": {"kind": "tool_call", "tool": "car_navi_to_destination", "arguments": {}},
                "llm_ms": 1.0, "log_fields": {"decision_head": "stub"}}

    # -- driver (virtual clock; only this thread touches TurnSession state) --
    # The 2 ms real sleep is not part of the timing under test: worker threads need to be scheduled
    # before the next tick can poll their result (same trick as the a4 deterministic harness).

    def pump(self, t_ms):
        while self.now < t_ms and not self.session.turn_done:
            self.now = min(float(t_ms), self.now + STEP)
            self.session.tick(self.now)
            time.sleep(0.002)

    def pump_until(self, predicate, deadline_ms):
        while not predicate() and self.now < deadline_ms and not self.session.turn_done:
            self.now += STEP
            self.session.tick(self.now)
            time.sleep(0.002)

    def named(self, name):
        return [event for event in self.events if event["event"] == name]

    def summary(self):
        return self.session.summary()

    def close(self):
        self.session.close()


def check(name, ok, note=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {note}")
    return bool(ok)


def invariant(h):
    summary = h.summary()
    total = summary["llm_calls"]
    consumed = summary["consumed_count"]
    wasted = summary["llm_wasted"]
    return (total == consumed + wasted and len(summary["consumptions"]) == consumed,
            f"llm_calls={total} consumed={consumed} wasted={wasted}")


def c1():
    print("=" * 100)
    print("C1 · single consumption baseline: 1 segment -> 1 decision, then silence closes the turn")
    h = Harness(BASE, "c1-baseline")
    h.session.on_vad(0, "speech_start")
    h.session.on_asr(100, "导航回家", True)
    h.session.on_vad(120, "speech_end")
    h.pump(4000)
    done = h.named("turn_done")
    starts, decisions, consumed = h.named("llm_start"), h.named("llm_decision"), h.named("segment_consumed")
    order = [event["event"] for event in h.events if event["event"] in ("llm_start", "llm_decision", "segment_consumed")]
    ok_inv, note = invariant(h)
    done_at = done[0]["t_ms"] if done else None
    consumed_at = consumed[0]["t_ms"] if consumed else None
    results = [
        check("C1 exactly 1 llm_start / 1 llm_decision", len(starts) == 1 and len(decisions) == 1,
              f"starts={len(starts)} decisions={len(decisions)}"),
        check("C1 decide() input text", h.decide_inputs == ["导航回家"], f"{h.decide_inputs}"),
        check("C1 llm_start.utterance", bool(starts) and starts[0].get("utterance") == "导航回家",
              f"{starts[0].get('utterance') if starts else None!r}"),
        check("C1 llm_decision.utterance + consumed_index=1",
              bool(decisions) and decisions[0].get("utterance") == "导航回家"
              and decisions[0].get("consumed_index") == 1,
              json.dumps({key: decisions[0].get(key) for key in ("utterance", "consumed_index", "consumed_version")},
                         ensure_ascii=False) if decisions else ""),
        check("C1 event order llm_decision -> segment_consumed (spec §5)",
              order == ["llm_start", "llm_decision", "segment_consumed"], f"{order}"),
        check("C1 segment_consumed text + consumptions", bool(consumed) and consumed[0].get("text") == "导航回家"
              and h.summary()["consumptions"][0]["segments"] == 1,
              json.dumps(consumed[0], ensure_ascii=False) if consumed else ""),
        check("C1 consumed_count=1 llm_calls=1 llm_wasted=0",
              h.summary()["consumed_count"] == 1 and h.summary()["llm_calls"] == 1 and h.summary()["llm_wasted"] == 0,
              f"consumed={h.summary()['consumed_count']} calls={h.summary()['llm_calls']} "
              f"wasted={h.summary()['llm_wasted']}"),
        check("C1 invariant llm_calls == consumed + wasted", ok_inv, note),
        check("C1 turn_done a full max_silence_ms after the last consumption, state LISTENING",
              bool(done) and done_at is not None and consumed_at is not None
              and done_at - consumed_at >= BASE["max_silence_ms"] and h.session.state == "LISTENING",
              f"consumed@{consumed_at} done@{done_at} state={h.session.state}"),
        check("C1 summary exposes t_last_llm_start / t_last_decision",
              done and "t_last_llm_start" in done[0] and "t_last_decision" in done[0]
              and done[0]["t_last_decision"] is not None,
              f"t_llm_start={done[0].get('t_llm_start')} t_last_llm_start={done[0].get('t_last_llm_start')} "
              f"t_last_decision={done[0].get('t_last_decision')}" if done else "no turn_done"),
    ]
    h.close()
    return results


def c2():
    print("=" * 100)
    print("C2 · two consecutive segments: 2 decisions, second input has no consumed prefix")
    h = Harness(BASE, "c2-two-segments", evidence_root=EVIDENCE_ROOT)
    h.session.on_vad(0, "speech_start")
    h.session.on_asr(100, "导航回家", True)
    h.session.on_vad(120, "speech_end")
    h.pump_until(lambda: h.session.consumed_count == 1, 800)
    first_consumed = h.named("segment_consumed")
    consume1_at = first_consumed[0]["t_ms"] if first_consumed else None
    h.pump(800)
    h.session.on_vad(800, "speech_start")
    h.session.on_asr(900, "顺便看看路况", True)
    h.session.on_vad(920, "speech_end")
    h.pump(5000)
    consumed, decisions = h.named("segment_consumed"), h.named("llm_decision")
    done = h.named("turn_done")
    done_at = done[0]["t_ms"] if done else None
    between = [event for event in h.events
               if event["event"] == "turn_done" and len(consumed) >= 2 and event["t_ms"] < consumed[1]["t_ms"]]
    ok_inv, note = invariant(h)
    timeline = json.loads(Path(h.session.timeline_path).read_text()) if h.session.timeline_path else {}
    results = [
        check("C2 exactly 2 llm_calls / 2 llm_decision / 2 segment_consumed",
              h.summary()["llm_calls"] == 2 and len(decisions) == 2 and len(consumed) == 2,
              f"calls={h.summary()['llm_calls']} decisions={len(decisions)} consumed={len(consumed)}"),
        check("C2 decide() inputs are the per-segment suffix",
              h.decide_inputs == ["导航回家", "顺便看看路况"], f"{h.decide_inputs}"),
        check("C2 segment 1 consumed before speech_start@800", consume1_at is not None and consume1_at < 800,
              f"consumed@ {consume1_at} < 800"),
        check("C2 no turn_done between the two consumptions", not between,
              f"turn_done@{done_at} consumed2@{consumed[1]['t_ms'] if len(consumed) >= 2 else None}"),
        check("C2 no barge_in / no turn reset", not h.named("barge_in") and h.summary()["turns"] == 1,
              f"barge_in={len(h.named('barge_in'))} turns={h.summary()['turns']}"),
        check("C2 consumption cursor advanced per segment",
              [item["segments"] for item in h.summary()["consumptions"]] == [1, 2],
              f"{[item['segments'] for item in h.summary()['consumptions']]}"),
        check("C2 consumed_count=2 llm_wasted=0", h.summary()["consumed_count"] == 2
              and h.summary()["llm_wasted"] == 0, f"consumed={h.summary()['consumed_count']} "
              f"wasted={h.summary()['llm_wasted']}"),
        check("C2 invariant llm_calls == consumed + wasted", ok_inv, note),
        check("C2 final state LISTENING + turn_done emitted", bool(done) and h.session.state == "LISTENING",
              f"state={h.session.state} done@{done_at}"),
        check("C2 timeline.json has consumptions[] + segment_consumed",
              bool(timeline) and len(timeline.get("summary", {}).get("consumptions", [])) == 2
              and sum(event["event"] == "segment_consumed" for event in timeline.get("events", [])) == 2,
              f"timeline={h.session.timeline_path}"),
    ]
    h.close()
    return results


def c3():
    print("=" * 100)
    print("C3 · continue after consumption: the speculative call on the suffix is invalidated")
    h = Harness(SPEC, "c3-spec-invalidated")
    h.session.on_vad(0, "speech_start")
    h.session.on_asr(100, "导航回家", True)
    h.session.on_vad(120, "speech_end")
    h.pump_until(lambda: h.session.consumed_count == 1, 1000)
    h.pump(400)
    h.session.on_asr(400, "顺便看看", False)
    h.pump_until(lambda: any(event["event"] == "llm_start" and event.get("utterance") == "顺便看看"
                             for event in h.events), 1200)
    job = h.session._jobs["llm"]
    inflight = job is not None and not job["canceled"] and job.get("utterance") == "顺便看看"
    h.session.on_asr(600, "顺便看看路况", True)
    h.pump(6000)
    consumed, decisions = h.named("segment_consumed"), h.named("llm_decision")
    cancels = h.named("llm_cancel")
    spec_cancels = [event for event in cancels if event.get("speculative")]
    ok_inv, note = invariant(h)
    results = [
        check("C3 speculative call on the unconsumed suffix really started (and was not applied yet)", inflight,
              f"job={job.get('utterance') if job else None!r}"),
        check("C3 exactly 3 llm_calls", h.summary()["llm_calls"] == 3, f"calls={h.summary()['llm_calls']}"),
        check("C3 decide() inputs #1 #2 #3", h.decide_inputs == ["导航回家", "顺便看看", "顺便看看路况"],
              f"{h.decide_inputs}"),
        check("C3 the in-flight speculative call was canceled as speculative",
              bool(spec_cancels) and spec_cancels[0].get("reason") in {"new_text", "speech_start"},
              json.dumps([{key: event.get(key) for key in ("t_ms", "reason", "speculative", "prepared")}
                          for event in cancels], ensure_ascii=False)),
        check("C3 exactly 2 segment_consumed with the right texts",
              [event["text"] for event in consumed] == ["导航回家", "顺便看看路况"],
              f"{[event['text'] for event in consumed]}"),
        check("C3 consumed_count=2 llm_wasted=1", h.summary()["consumed_count"] == 2
              and h.summary()["llm_wasted"] == 1,
              f"consumed={h.summary()['consumed_count']} wasted={h.summary()['llm_wasted']}"),
        check("C3 invariant llm_calls == consumed + wasted (3 == 2 + 1)", ok_inv, note),
        check("C3 exactly 1 applied decision in the end (the canceled one was not applied)",
              len(decisions) == 2 and h.session.state == "LISTENING",
              f"decisions={len(decisions)} state={h.session.state}"),
    ]
    h.close()
    return results


def c4():
    print("=" * 100)
    print("C4 · ASR re-sends the consumed sentence: duplicate ignored, prefix stripped")
    h = Harness(BASE, "c4-resend-dedup")
    h.session.on_vad(0, "speech_start")
    h.session.on_asr(100, "导航回家", True)
    h.session.on_vad(120, "speech_end")
    h.pump_until(lambda: h.session.consumed_count == 1, 1000)
    h.pump(600)
    before_dup = len(h.events)
    h.session.on_asr(600, "导航回家。", True)
    dup_events = h.events[before_dup:]
    h.pump(900)
    h.session.on_vad(900, "speech_start")
    h.session.on_asr(1000, "导航回家附近有加油站吗", True)
    h.pump(6000)
    consumed = h.named("segment_consumed")
    skipped = [event for event in h.events if event["event"] == "eot_skipped"
               and event.get("reason") == "consumed_duplicate"]
    after_dup = [event for event in dup_events
                 if event["event"] in ("eot_start", "tools_start", "llm_start")]
    ok_inv, note = invariant(h)
    turn_text = normalize(h.session.turn_text())
    results = [
        check("C4 the exact re-send is reported as consumed_duplicate", bool(skipped),
              json.dumps([{key: event.get(key) for key in ("t_ms", "reason", "text")} for event in skipped],
                         ensure_ascii=False)),
        check("C4 the duplicate triggers no eot_start / tools_start / llm_start", not after_dup,
              f"{[event['event'] for event in after_dup]}"),
        check("C4 exactly 2 llm_calls and inputs are the deduped suffix",
              h.summary()["llm_calls"] == 2 and h.decide_inputs == ["导航回家", "附近有加油站吗"],
              f"calls={h.summary()['llm_calls']} inputs={h.decide_inputs}"),
        check("C4 2 consumptions, second text is the stripped tail",
              [event["text"] for event in consumed] == ["导航回家", "附近有加油站吗"],
              f"{[event['text'] for event in consumed]}"),
        check("C4 turn_text() does not repeat the consumed sentence", turn_text.count("导航回家") == 1,
              f"{turn_text!r}"),
        check("C4 consumed_count=2 llm_wasted=0", h.summary()["consumed_count"] == 2
              and h.summary()["llm_wasted"] == 0,
              f"consumed={h.summary()['consumed_count']} wasted={h.summary()['llm_wasted']}"),
        check("C4 invariant llm_calls == consumed + wasted", ok_inv, note),
        check("C4 turn_done emitted, state LISTENING", bool(h.named("turn_done")) and h.session.state == "LISTENING",
              f"state={h.session.state}"),
    ]
    h.close()
    return results


if __name__ == "__main__":
    results = {"C1": c1(), "C2": c2(), "C3": c3(), "C4": c4()}
    print("=" * 100)
    for name, items in results.items():
        print(f"{name}: {'PASS' if all(items) else 'FAIL'} ({sum(items)}/{len(items)})")
    passed = all(all(items) for items in results.values())
    total = sum(len(items) for items in results.values())
    print(f"C1-C4 RESULT: {'PASS' if passed else 'FAIL'} "
          f"({sum(sum(items) for items in results.values())}/{total})")
    raise SystemExit(0 if passed else 1)