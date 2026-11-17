"""期四 · 冷场三选离线验收（不连 8009 / 不连 LLM / 不连 TTS）。

钉住 2026-11-16 已拍板的口径（验收卡 `poc-hub-jev-absorb-p4-20261116-验收.md`）：

1. **三条出口各一条能复现**：同一段安静、只换冷场结论 → 再等 / 主动接一句 / 问一句各自跑得出来。
2. **刻度到点只问一次、不重复**：约 4 秒那一拍只问一次；不会连着问；同一句接话不会反复说。
3. **有人开口立刻清零**：安静还没到点 / 刚到点还没开口 / 判断还在飞，人又说话 → 不出冷场那句，
   计时清掉；不会在有人说话时抢答。
4. **「问一句」只在约 10 秒之后才出现**：约 4 秒那一拍只能是「再等」或「主动接一句」。
5. **只接上文，不出现新话题 / 新任务**：台词带「刚才那句」的短片段；不选工具、不执行、不开闸。
6. **冷场开口不进 Agent**：记住（`hub_said`），但不选工具、不执行、不开闸；同一段稿开 / 关冷场
   的工具与决策条数一样。
7. **判断没回来 / 失败 / 超时**：不出声（再等）；收口与开闸不变。
8. **收住时刻与开闸结论不被带偏**：同一段稿「冷场开 vs 关」逐字段一致（去掉冷场自己的事件 / 字段）。
9. **调用次数**：不是「每版多一路」，只在「安静刻度」这一拍多 1 次；每版仍然是原来的那几路。

运行：python3 -B tests/gate/test_hub_quiet.py
"""
from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from gate.hub_quiet import (QUIET_BACKOFF_MS, QUIET_CHECK, QUIET_CHECK_AFTER_MS,  # noqa: E402
                            QUIET_FIRST_MS, QUIET_FOLLOW, QUIET_LABELS, QUIET_ORDER, QUIET_SAFE,
                            QUIET_STEP_MS, QUIET_WAIT, build_quiet_fn, decide_quiet,
                            quiet_chars, quiet_meta, quiet_question, quiet_state, quiet_text,
                            rule_quiet)
from gate.hub_gate import GateKeeper  # noqa: E402
from gate.turn_session import TurnConfig, TurnSession  # noqa: E402
from shared.text import normalize  # noqa: E402


def eot_scores(label, *, complete=None, cut=None, wait=None, confidence=0.9, latency=10.0):
    if label == "complete":
        return {"label": "complete", "complete": complete if complete is not None else 0.95,
                "cut": cut if cut is not None else 0.02, "wait": wait if wait is not None else 0.03,
                "confidence": confidence, "wall_ms": latency}
    if label == "cut":
        return {"label": "cut", "complete": 0.08, "cut": cut if cut is not None else 0.86,
                "wait": 0.06, "confidence": confidence, "wall_ms": latency}
    return {"label": "wait", "complete": 0.06, "cut": 0.04,
            "wait": wait if wait is not None else 0.90, "confidence": confidence, "wall_ms": latency}


STRONG_COMPLETE = eot_scores("complete", complete=0.95, confidence=0.95)
WEAK_COMPLETE = eot_scores("complete", complete=0.86, confidence=0.40)   # 可能说完：0.7s 等待档
CUT = eot_scores("cut")
WAIT = eot_scores("wait")


def accept_outcome(accept, *, source="测试接不接", model="fake/accept"):
    return {"accept": accept, "state": "ok", "confidence": 0.9 if accept else 0.2,
            "score": 0.9 if accept else 0.2, "source": source, "model": model,
            "reason": "测试用接不接结论"}


def ack_outcome(say=True, *, source="测试应声判断", model="fake/ack"):
    return {"say": say, "state": "ok", "score": 0.8 if say else 0.2, "confidence": 0.9,
            "source": source, "model": model, "reason": "测试用应声判断"}


def quiet_outcome(choice, *, source="测试冷场判断", model="fake/quiet"):
    probabilities = {name: (0.82 if name == choice else round(0.18 / 2, 3)) for name in QUIET_ORDER}
    return {"choice": choice, "outlet": choice, "label": QUIET_LABELS[choice],
            "action": "测试用出口", "probabilities": probabilities,
            "score": probabilities[choice], "confidence": 0.9, "state": "ok",
            "source": source, "model": model, "reason": "测试用冷场结论"}


def tools_fn(text, tool_set=None, context=None):
    name = "car_navi_to_destination"
    return {"scores": {"tool:" + name: 0.9}, "selected": [name],
            "schemas": [{"name": name, "description": "d", "parameters": {"type": "object"}}],
            "native_schemas": None, "domains": None,
            "timing": {"stage1_ms": 1.0, "stage2_ms": 0.0, "total_ms": 1.0}}


def decide_fn(utterance, selected, schemas, tool_set, context, **kwargs):
    return {"decision": {"kind": "tool_call",
                         "tool": (selected or ["car_navi_to_destination"])[0],
                         "arguments": {}}, "llm_ms": 1.0,
            "log_fields": {"decision_head": "quiet-test"}}


class Bench:
    """一段固定稿 + 一个虚拟时钟：同一段稿只换冷场结论 / 开关冷场。

    `quiet` 可以是一条结论，也可以是一串（按第几次问取：第一拍、第二拍……各判各的）；
    取值：wait / follow / check，`fail`（判断失败），`hold_follow` / `hold_check`（卡在闸上，
    等 `("qrelease", t)` 放行——用来验「判断还在飞时人开口」）。
    """

    def __init__(self, script, *, quiet=QUIET_WAIT, quiet_enabled=True, quiet_fn=None,
                 quiet_timeout_ms=None, accept=True, ack=False, seat="D"):
        self.script = {normalize(key): value for key, value in script.items()}
        self.accept = accept
        self.quiet_asked_n = 0
        self.quiet_release = threading.Event()
        self.quiet_release.set()
        plan = list(quiet) if isinstance(quiet, (list, tuple)) else [quiet]
        self.quiet_plan = plan
        if quiet_fn is None:
            def _quiet_fn(*, quiet_ms, hub_last="", recent="", scene=None, seat="D",
                          check_allowed=None):
                index = min(len(plan) - 1, self.quiet_asked_n)
                self.quiet_asked_n += 1
                spec = plan[index]
                if spec == "fail":
                    raise RuntimeError("测试：冷场判断失败")
                if spec.startswith("hold_"):
                    self.quiet_release.clear()
                    if not self.quiet_release.wait(timeout=5.0):
                        raise RuntimeError("测试：冷场判断没人放行")
                    spec = spec[len("hold_"):]
                return quiet_outcome(spec)
            quiet_fn = _quiet_fn
        self.quiet_fn = quiet_fn
        self.gate = GateKeeper(
            accept_fn=lambda text, scene, seat: accept_outcome(self.accept),
            ack_fn=(lambda text, scene, seat: ack_outcome(True)) if ack else None,
            quiet_fn=(quiet_fn if quiet_enabled else None),
            quiet_timeout_ms=quiet_timeout_ms,
            scene_fn=lambda: None, seat=seat, accept_timeout_ms=3000)
        cfg = TurnConfig(quiet_enabled=quiet_enabled, ack_enabled=ack, cushion_enabled=False,
                         floor_enabled=False, confirm_silence_ms=700, cut_wait_ms=1500,
                         think_wait_ms=3000, warm_llm=False)
        self.session = TurnSession(cfg, None, gate=self.gate, session_id="quiet-" + seat,
                                   evidence_root=None, eot_fn=self._eot,
                                   select_tools_fn=tools_fn, decide_fn=decide_fn,
                                   seat_id=seat)
        self.now = 0

    def _eot(self, text):
        key = normalize(text)
        if key not in self.script:
            raise AssertionError("脚本里没有这一版字：" + text)
        return dict(self.script[key])

    def drive(self, commands, *, step_ms=10, tail_ms=1500):
        """`("asr", t, text, final)` / `("vad", t, kind)` / `("qrelease", t)` / `("end", t)`。"""
        index = 0
        end = max(item[1] for item in commands) + tail_ms
        while self.now <= end:
            while index < len(commands) and commands[index][1] <= self.now:
                item = commands[index]
                kind = item[0]
                index += 1
                if kind == "asr":
                    self.session.on_asr(item[1], item[2], item[3])
                elif kind == "vad":
                    self.session.on_vad(item[1], item[2])
                elif kind == "qrelease":
                    self.quiet_release.set()
                elif kind == "end":
                    return self
            self.session.tick(self.now)
            time.sleep(0.001)
            self.now += step_ms
        return self

    def events_of(self, name):
        return [event for event in self.session.events if event.get("event") == name]

    def confirm_times(self):
        return [(event["t_ms"], event["why"]) for event in self.events_of("state_change")
                if event.get("to") == "CONFIRMED"]

    def gate_routes(self):
        return [event.get("route") for event in self.events_of("gate_decision")]

    def turn_done_times(self):
        return [event["t_ms"] for event in self.events_of("turn_done")]

    def decisions(self):
        return [(event["t_ms"], event.get("utterance")) for event in self.events_of("llm_decision")]

    def asked(self):
        return [(event["t_ms"], event.get("episode"), event.get("quiet_ms"),
                 event.get("check_allowed")) for event in self.events_of("quiet_asked")]

    def spoken(self):
        return [(event["t_ms"], event.get("text"), event.get("outlet"), event.get("quiet_ms"))
                for event in self.events_of("quiet_spoken")]

    def close(self):
        self.session.close()
        self.gate.close()


# ---- 同一段稿：第一轮「导航回家」答完（中枢最后一句），之后没人说话 ----
SCRIPT = {"导航回家": STRONG_COMPLETE}
BASE = [("asr", 0, "导航回家", True)]


def run(commands=None, script=None, **kwargs):
    bench = Bench(script or SCRIPT, **kwargs)
    try:
        bench.drive(commands if commands is not None else BASE + [("end", 8000)])
        return bench
    except Exception:
        bench.close()
        raise


QUIET_EVENTS = {"quiet_asked", "quiet_cleared", "quiet_gate", "quiet_spoken", "hub_quiet"}


def strip_quiet(events):
    """去掉冷场自己那几条事件与新增的 `quiet_*` / `hub_last_*` 字段：剩下的就是「改动前那一份」。"""
    out = []
    for event in events:
        if event.get("event") in QUIET_EVENTS:
            continue
        row = {key: value for key, value in event.items()
               if key != "event" and key != "hub_policy" and not str(key).startswith(("quiet", "hub_last"))}
        cerebrum = row.get("cerebrum")
        if isinstance(cerebrum, dict):
            cerebrum = {key: value for key, value in cerebrum.items()
                        if key not in ("quiet", "quiet_source")}
            if isinstance(cerebrum.get("counters"), dict):
                cerebrum["counters"] = {key: value for key, value in cerebrum["counters"].items()
                                        if not str(key).startswith("quiet")}
            row["cerebrum"] = cerebrum
        out.append(row)
    return out


# 三条并发结论（接不接 / 应声 / 节奏 / 冷场）同一瞬间回来的先后本来不定；同一瞬间只按名字排。
GATE_NOISE = {"hub_accept", "hub_ack", "hub_pace", "hub_quiet", "hub_cushion"}


def canon_gate_noise(events):
    groups, order = {}, []
    for event in events:
        t_ms = event.get("t_ms")
        if t_ms not in groups:
            groups[t_ms] = []
            order.append(t_ms)
        groups[t_ms].append(event)
    out = []
    for t_ms in order:
        items = groups[t_ms]
        out.extend([item for item in items if item.get("event") not in GATE_NOISE])
        out.extend(sorted([item for item in items if item.get("event") in GATE_NOISE],
                          key=lambda item: item.get("event")))
    return out


class QuietContract(unittest.TestCase):
    """冷场这一问的请求形状、三条出口、刻度闸与兜底（纯函数）。"""

    def test_question_is_a_three_way_choice_with_the_pinned_outlets(self):
        question = quiet_question()
        self.assertEqual(question["type"], "choice")
        self.assertEqual(set(question["criteria"]), set(QUIET_ORDER))
        for label in ("再等", "主动接一句", "问一句"):
            self.assertIn(label, question["instructions"], "三条出口都要写进问法：" + label)
        self.assertIn("只接上文", question["instructions"])
        self.assertIn("不发起新任务", question["instructions"])
        self.assertEqual([QUIET_LABELS[name] for name in QUIET_ORDER],
                         ["再等", "主动接一句", "问一句"])

    def test_state_carries_how_long_quiet_and_the_topic_in_play(self):
        state = quiet_state(quiet_ms=4010, hub_last="好的，正在为您导航",
                            recent="导航回家", scene={"summary": "主驾在路口等灯"},
                            check_allowed=False)
        self.assertIn('"quiet_ms": 4010', state)
        self.assertIn('"hub_last": "好的，正在为您导航"', state)
        self.assertIn('"recent": "导航回家"', state)
        self.assertIn('"scene": "主驾在路口等灯"', state)
        self.assertIn('"check_allowed": false', state)
        blank = quiet_state(quiet_ms=0)
        self.assertIn('"hub_last": ""', blank)
        self.assertIn('"recent": ""', blank)

    def test_every_outlet_is_a_valid_answer_not_a_threshold_question(self):
        for outlet in QUIET_ORDER:
            choice, reason = decide_quiet(quiet=quiet_outcome(outlet), quiet_ms=12000)
            self.assertEqual(choice, outlet, reason)

    def test_missing_or_failed_judgement_waits_silently(self):
        for verdict in ({}, None, {"state": "pending"}, {"state": "failed"},
                        {"state": "timeout"}, {"state": "ok", "outlet": "hangup"},
                        {"state": "ok", "choice": None}):
            choice, reason = decide_quiet(quiet=verdict, quiet_ms=12000)
            self.assertEqual(choice, QUIET_SAFE, reason)
            self.assertEqual(choice, QUIET_WAIT)
            self.assertIn("不出声", reason)

    def test_check_is_not_allowed_at_the_first_tick(self):
        choice, reason = decide_quiet(quiet=quiet_outcome(QUIET_CHECK),
                                      quiet_ms=QUIET_FIRST_MS)
        self.assertEqual(choice, QUIET_WAIT)
        self.assertIn("还不能问「问一句」", reason)
        choice, _ = decide_quiet(quiet=quiet_outcome(QUIET_CHECK),
                                 quiet_ms=QUIET_CHECK_AFTER_MS)
        self.assertEqual(choice, QUIET_CHECK)

    def test_follow_and_wait_are_allowed_at_the_first_tick(self):
        for outlet in (QUIET_FOLLOW, QUIET_WAIT):
            choice, reason = decide_quiet(quiet=quiet_outcome(outlet), quiet_ms=QUIET_FIRST_MS)
            self.assertEqual(choice, outlet, reason)

    def test_before_the_first_tick_nothing_is_allowed(self):
        for outlet in QUIET_ORDER:
            choice, reason = decide_quiet(quiet=quiet_outcome(outlet), quiet_ms=QUIET_FIRST_MS - 10)
            self.assertEqual(choice, QUIET_WAIT, reason)
            self.assertIn("还没到首问刻度", reason)

    def test_rule_stand_in_is_labelled_and_honours_the_env(self):
        old = os.environ.get("HUB_QUIET_RULE")
        try:
            os.environ["HUB_QUIET_RULE"] = "check"
            verdict = rule_quiet(quiet_ms=12000)
            self.assertEqual(verdict["outlet"], QUIET_CHECK)
            self.assertEqual(verdict["source"], "离线替身（仅供离线演示）")
            self.assertIn("未拍板", verdict["model"])
            os.environ["HUB_QUIET_RULE"] = "not-an-outlet"
            self.assertEqual(rule_quiet(quiet_ms=12000)["outlet"], QUIET_WAIT)
        finally:
            if old is None:
                os.environ.pop("HUB_QUIET_RULE", None)
            else:
                os.environ["HUB_QUIET_RULE"] = old
        meta = quiet_meta()
        self.assertTrue(meta["decided"])
        self.assertEqual(meta["safe_outlet"], QUIET_WAIT)
        self.assertEqual(meta["first_ms"], QUIET_FIRST_MS)
        self.assertEqual(meta["check_after_ms"], QUIET_CHECK_AFTER_MS)

    def test_rule_fn_meta_is_not_impersonating_the_model(self):
        fn = build_quiet_fn("rule")
        self.assertIn("离线替身", fn.meta["source"])
        self.assertIn("未拍板", fn.meta["model"])
        with self.assertRaises(ValueError):
            build_quiet_fn("nope")

    def test_quiet_text_only_refers_back_and_never_starts_a_topic(self):
        topic = "导航回家"
        follow = quiet_text(QUIET_FOLLOW, topic, index=0)
        self.assertIn("导航回家", follow)
        self.assertNotIn("？", follow)
        check = quiet_text(QUIET_CHECK, topic, index=0)
        self.assertIn("导航回家", check)
        self.assertIn("？", check)
        self.assertGreater(quiet_chars(follow), 0)
        self.assertEqual(quiet_text(QUIET_FOLLOW, ""), "刚才那几句，我接着记着呢。")
        self.assertEqual(quiet_text(QUIET_CHECK, ""), "还在吗？要不要接着说刚才的？")
        # 长句只带一个短片段，不把整段念一遍
        self.assertIn("…", quiet_text(QUIET_FOLLOW, "我想问一下今天下午会不会下雨以及气温"))

    def test_same_sentence_is_not_repeated(self):
        first = quiet_text(QUIET_FOLLOW, "导航回家", index=0)
        second = quiet_text(QUIET_FOLLOW, "导航回家", index=0, avoid=first)
        self.assertNotEqual(first, second)
        self.assertIn("导航回家", second)


class QuietSession(unittest.TestCase):
    """真状态机 + 虚拟时钟：三条出口、刻度、清零、只接上文、不进 Agent、收口 / 开闸不变。"""

    def test_the_same_silence_runs_each_of_the_three_outlets(self):
        waited = run(quiet=[QUIET_WAIT], commands=BASE + [("end", 8000)])
        try:
            self.assertEqual(waited.asked()[0][2] >= QUIET_FIRST_MS, True)
            self.assertEqual(waited.spoken(), [])
        finally:
            waited.close()
        followed = run(quiet=[QUIET_FOLLOW], commands=BASE + [("end", 8000)])
        try:
            spoken = followed.spoken()
            self.assertEqual(len(spoken), 1)
            offset = spoken[0][0] - followed.session._quiet_anchor_ms
            self.assertGreaterEqual(offset, QUIET_FIRST_MS)
            self.assertLess(offset, QUIET_FIRST_MS + 300)
            self.assertEqual(spoken[0][2], QUIET_FOLLOW)
            self.assertIn("导航回家", spoken[0][1])
        finally:
            followed.close()
        checked = run(quiet=[QUIET_CHECK, QUIET_CHECK], commands=BASE + [("end", 11000)])
        try:
            spoken = checked.spoken()
            self.assertEqual(len(spoken), 1, "4 秒那一拍判 check 也只能等到 10 秒那一拍")
            offset = spoken[0][0] - checked.session._quiet_anchor_ms
            self.assertGreaterEqual(offset, QUIET_CHECK_AFTER_MS)
            self.assertLess(offset, QUIET_CHECK_AFTER_MS + 300)
            self.assertEqual(spoken[0][2], QUIET_CHECK)
            self.assertIn("？", spoken[0][1])
        finally:
            checked.close()

    def test_one_ask_per_tick_and_never_back_to_back(self):
        bench = run(quiet=[QUIET_WAIT], commands=BASE + [("end", 28000)])
        try:
            asked = bench.asked()
            anchors = bench.session._quiet_anchor_ms
            offsets = [event[2] for event in asked]
            self.assertEqual(len(asked), 4, "4s / 10s / 18s / 26s 各一拍：%s" % (asked,))
            self.assertEqual([round(offset / 1000.0) for offset in offsets], [4, 10, 18, 26])
            times = [event[0] for event in asked]
            for earlier, later in zip(times, times[1:]):
                self.assertGreaterEqual(later - earlier, QUIET_FIRST_MS)
            self.assertEqual(bench.spoken(), [])
            self.assertEqual(bench.session._quiet_asks_total, 4)
            self.assertEqual(bench.gate.counters["quiet_calls"], 4)
            self.assertGreater(anchors, 0)
        finally:
            bench.close()

    def test_check_never_appears_at_four_seconds(self):
        bench = run(quiet=[QUIET_CHECK], commands=BASE + [("end", 7000)])
        try:
            self.assertEqual(bench.spoken(), [])
            gates = bench.events_of("quiet_gate")
            self.assertTrue(gates)
            self.assertIn("还不能问「问一句」", gates[0]["reason"])
            self.assertFalse([event for event in bench.events_of("quiet_asked")
                              if event["quiet_ms"] < QUIET_CHECK_AFTER_MS
                              and event.get("check_allowed")])
        finally:
            bench.close()

    def test_a_human_opening_clears_the_clock_before_the_tick(self):
        script = {"导航回家": STRONG_COMPLETE, "谢谢": STRONG_COMPLETE}
        bench = run(quiet=[QUIET_FOLLOW],
                    commands=[("asr", 0, "导航回家", True), ("release", 0),
                              ("asr", 3900, "谢谢", True), ("end", 10000)], script=script)
        try:
            cleared = bench.events_of("quiet_cleared")
            self.assertTrue(cleared)
            self.assertEqual(cleared[0]["t_ms"], 3900)
            self.assertEqual(cleared[0]["why"], "human_speech")
            spoken = bench.spoken()
            self.assertEqual(len(spoken), 1, "人开过口之后才重新起算：%s" % (spoken,))
            self.assertGreaterEqual(spoken[0][0], 6000, "不是刚被清掉就抢答")
            self.assertIn("谢谢", spoken[0][1])
        finally:
            bench.close()

    def test_a_human_opening_while_the_judgement_is_in_flight_drops_it(self):
        script = {"导航回家": STRONG_COMPLETE, "谢谢": STRONG_COMPLETE}
        bench = run(quiet=["hold_follow"],
                    commands=[("asr", 0, "导航回家", True), ("release", 0),
                              ("asr", 4200, "谢谢", True), ("qrelease", 4300),
                              ("end", 6000)], script=script)
        try:
            self.assertTrue(bench.events_of("quiet_asked"))
            self.assertTrue(bench.events_of("quiet_cleared"))
            self.assertEqual(bench.spoken(), [], "人开口以后，那一回的结论迟到也不许出声")
            self.assertEqual(bench.gate.counters["quiet_calls"], 1)
        finally:
            bench.close()

    def test_vad_speech_start_clears_the_clock(self):
        bench = run(quiet=[QUIET_FOLLOW],
                    commands=BASE + [("vad", 3900, "speech_start"), ("vad", 4100, "speech_end"),
                                     ("end", 9000)])
        try:
            self.assertTrue(bench.events_of("quiet_cleared"))
            self.assertEqual(bench.spoken(), [])
        finally:
            bench.close()

    def test_quiet_line_only_refers_back_and_is_not_answered(self):
        bench = run(quiet=[QUIET_FOLLOW], commands=BASE + [("end", 7000)])
        try:
            spoken = bench.spoken()
            self.assertEqual(len(spoken), 1)
            self.assertIn("导航回家", spoken[0][1])
            self.assertEqual(spoken[0][2], QUIET_FOLLOW)
            events = bench.session.events
            index = next(i for i, event in enumerate(events) if event.get("event") == "quiet_spoken")
            after = [event for event in events[index + 1:] if event.get("event") not in QUIET_EVENTS]
            self.assertEqual([event for event in after if event.get("event") == "llm_decision"], [])
            self.assertEqual([event for event in after if event.get("event") == "tools_start"], [])
            self.assertEqual([event for event in after if event.get("event") == "gate_open"], [])
            row = bench.session.hub_said[-1]
            self.assertIn("导航回家", row["text"])
            self.assertEqual(row["kind"], "quiet_follow")
            self.assertEqual(bench.session.counters["llm_calls"], 1, "只有原来那一轮的回答")
            self.assertEqual(bench.session.counters["tool_calls"], 1)
        finally:
            bench.close()

    def test_quiet_opening_does_not_enter_the_agent(self):
        quiet_on = run(quiet=[QUIET_WAIT], commands=BASE + [("end", 8000)])
        quiet_off = run(quiet_enabled=False, commands=BASE + [("end", 8000)])
        try:
            self.assertEqual(quiet_on.session.counters["llm_calls"],
                             quiet_off.session.counters["llm_calls"])
            self.assertEqual(quiet_on.session.counters["tool_calls"],
                             quiet_off.session.counters["tool_calls"])
            self.assertEqual(quiet_on.gate_routes(), quiet_off.gate_routes())
            self.assertEqual(quiet_on.confirm_times(), quiet_off.confirm_times())
            self.assertEqual(quiet_on.turn_done_times(), quiet_off.turn_done_times())
        finally:
            quiet_on.close()
            quiet_off.close()

    def test_judgement_failure_and_timeout_stay_silent(self):
        failed = run(quiet=["fail"], commands=BASE + [("end", 7000)])
        try:
            self.assertEqual(failed.spoken(), [])
            self.assertEqual(failed.events_of("hub_quiet")[0]["state"], "failed")
            self.assertEqual(failed.events_of("quiet_gate")[0]["spoken"], False)
        finally:
            failed.close()
        held = run(quiet=["hold_follow"], quiet_timeout_ms=600,
                   commands=BASE + [("end", 7000)])
        try:
            self.assertEqual(held.spoken(), [])
            self.assertEqual(held.session.counters["quiet_timeouts"], 1)
            self.assertEqual(held.events_of("hub_quiet")[-1]["state"], "timeout")
            self.assertGreaterEqual(held.session.counters["quiet_wait"], 1)
        finally:
            held.close()

    def test_closing_and_gate_are_not_disturbed(self):
        """同一段稿（还没到冷场刻度）：「冷场开 vs 关」去掉冷场事件 / 字段后逐字段相同。"""
        on = run(quiet=[QUIET_WAIT], commands=BASE + [("end", 5000)])
        off = run(quiet_enabled=False, commands=BASE + [("end", 5000)])
        try:
            self.assertEqual(on.confirm_times(), off.confirm_times())
            self.assertEqual(on.gate_routes(), off.gate_routes())
            self.assertEqual(on.turn_done_times(), off.turn_done_times())
            self.assertEqual(on.decisions(), off.decisions())
            on_events = strip_quiet(canon_gate_noise(on.session.events))
            off_events = strip_quiet(canon_gate_noise(off.session.events))
            self.assertEqual(on_events, off_events,
                             "没有冷场需要开口的那一段，事件流要与关掉冷场时逐字段一致")
        finally:
            on.close()
            off.close()

    def test_quiet_adds_one_call_per_tick_not_per_text_version(self):
        on = run(quiet=[QUIET_WAIT], commands=BASE + [("end", 11000)])
        off = run(quiet_enabled=False, commands=BASE + [("end", 11000)])
        try:
            self.assertEqual(on.session.counters["eot_calls"], off.session.counters["eot_calls"])
            self.assertEqual(on.gate.counters["accept_calls"], off.gate.counters["accept_calls"])
            self.assertEqual(len(on.events_of("hub_text")), len(off.events_of("hub_text")))
            self.assertEqual(on.gate.counters["quiet_calls"], 2, "4s / 10s 各一拍")
            self.assertEqual(sum(1 for event in on.events_of("quiet_asked")), 2)
            self.assertEqual(off.gate.counters.get("quiet_calls", 0), 0)
        finally:
            on.close()
            off.close()

    def test_back_off_between_two_openings_and_texts_rotate(self):
        bench = run(quiet=[QUIET_FOLLOW, QUIET_FOLLOW, QUIET_FOLLOW],
                    commands=BASE + [("end", 20000)])
        try:
            spoken = bench.spoken()
            self.assertEqual(len(spoken), 3, "4s / 10s / 18s 各开口一次：%s" % (spoken,))
            offsets = [event[0] for event in spoken]
            for earlier, later in zip(offsets, offsets[1:]):
                self.assertGreaterEqual(later - earlier, QUIET_BACKOFF_MS)
            self.assertEqual(len({event[1] for event in spoken}), 3, "同一句接话不反复说")
            for event in spoken:
                self.assertIn("导航回家", event[1])
        finally:
            bench.close()

    def test_quiet_does_not_fire_while_someone_is_still_speaking(self):
        """有人说话时走应声 / 垫句，没人说话才走冷场：说话这段一次冷场都不问。"""
        bench = run(quiet=[QUIET_FOLLOW], ack=True,
                    commands=[("asr", 0, "导航回家", True),
                              ("asr", 2000, "导航", False), ("asr", 3000, "导航回家", False),
                              ("asr", 4000, "导航回家吧", False), ("asr", 5000, "导航回家吧谢谢", True),
                              ("end", 7000)])
        try:
            self.assertEqual(bench.asked(), [], "人还在说的时候不问冷场")
            self.assertEqual(bench.spoken(), [])
            self.assertTrue(bench.events_of("quiet_cleared"), "每次开口都清零")
            self.assertTrue(bench.session.counters["quiet_blocked"] >= 0)
        finally:
            bench.close()

    def test_summary_surfaces_the_quiet_decision(self):
        bench = run(quiet=[QUIET_FOLLOW], commands=BASE + [("end", 7000)])
        try:
            summary = bench.session.summary()
            self.assertTrue(summary["quiet_enabled"])
            self.assertEqual(summary["quiet_first_ms"], QUIET_FIRST_MS)
            self.assertEqual(summary["quiet_check_after_ms"], QUIET_CHECK_AFTER_MS)
            self.assertEqual(summary["quiet_backoff_ms"], QUIET_BACKOFF_MS)
            self.assertEqual(summary["quiet_spoken_total"], 1)
            self.assertEqual(summary["quiet_asks_total"], 1)
            self.assertEqual(summary["hub_last_text"], "")
            self.assertTrue(summary["hub_last_said_ms"] is not None)
            self.assertEqual(summary["quiet_log"][0]["outlet"], QUIET_FOLLOW)
            self.assertIn("导航回家", summary["quiet_log"][0]["text"])
            self.assertIn("quiet_episode", summary)
        finally:
            bench.close()

    def test_silence_ticks_do_not_add_quiet_calls_before_four_seconds(self):
        bench = run(quiet=[QUIET_FOLLOW], commands=BASE + [("end", 3900)])
        try:
            self.assertEqual(bench.asked(), [])
            self.assertEqual(bench.spoken(), [])
            self.assertEqual(bench.gate.counters["quiet_calls"], 0)
        finally:
            bench.close()


class QuietSurface(unittest.TestCase):
    """台面：两席各一行「冷场判断」+ 一行「冷场开口」，事件有中文话术（静态契约，不起服务）。"""

    def test_both_seats_show_the_quiet_rows(self):
        html = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
        self.assertEqual(html.count('data-hub-quiet-judge>—'), 2)
        self.assertEqual(html.count('data-hub-quiet>—'), 2)
        self.assertIn("冷场判断", html)
        self.assertIn("冷场开口", html)
        self.assertIn("冷场只在安静到刻度才问", html)
        for event in ("quiet_asked", "hub_quiet", "quiet_gate", "quiet_spoken", "quiet_cleared"):
            self.assertIn(event + ":", html, "台面要有这一条事件的话术：" + event)
        self.assertIn("要不要开口", html)


if __name__ == "__main__":
    unittest.main(verbosity=2)
