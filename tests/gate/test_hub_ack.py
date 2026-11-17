"""期一 · 应一声离线验收（不连 8009 / 不连 LLM / 不连 TTS）。

钉住 2026-11-16 已拍板的三道闸与红线：

1. **应声只出现在「还没说完」的三个等待档**（弱 complete 0.7s / 话断了 1.5s / 还在想 3s）；
   「说完了当刻」的轮次里**一次应声都没有**。
2. **同版字上「不要回答」→ 不应声**；应声**只作用于本席**（两席各自算、互不压制）。
3. **同一轮两次至少隔 3 秒**、**一句 3–6 个字**；应声**不延长也不缩短**任何等待档
   ——同一段脚本在 `ack_enabled=False` 下收住时刻与开闸结论**逐毫秒一致**。
4. **判断没回来 / 超时 / 失败 → 不出声**，且**不改收口、不改开闸**。
5. **回声**：与本机刚说过的那句高度重合的字当回声丢掉，不停听、不改计时。
6. **调用次数**：同一版字上应声判断多发**一次**调用（2 路 → 3 路；8009 两问探针见
   `tests/gate/probe_hub_multi_question.py`，本期未通隧道未跑）。

运行：python3 -B tests/gate/test_hub_ack.py
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from gate.hub_ack import (ACK_MAX_CHARS, ACK_MIN_CHARS, ACK_WORDS, ack_chars, ack_question,  # noqa: E402
                          ack_state, ack_text, build_ack_fn, decide_ack)
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


def ack_outcome(say, *, source="测试应声判断", model="fake/ack"):
    return {"say": say, "state": "ok", "score": 0.8 if say else 0.2, "confidence": 0.9,
            "source": source, "model": model, "reason": "测试用应声判断"}


def tools_fn(text, tool_set=None, context=None):
    name = "car_navi_to_destination"
    return {"scores": {"tool:" + name: 0.9}, "selected": [name],
            "schemas": [{"name": name, "description": "d", "parameters": {"type": "object"}}],
            "native_schemas": None, "domains": None,
            "timing": {"stage1_ms": 1.0, "stage2_ms": 0.0, "total_ms": 1.0}}


def decide_fn(utterance, selected, schemas, tool_set, context, **kwargs):
    return {"decision": {"kind": "tool_call", "tool": (selected or ["car_navi_to_destination"])[0],
                         "arguments": {}}, "llm_ms": 1.0, "log_fields": {"decision_head": "ack-test"}}


class Bench:
    """一段固定稿 + 一个虚拟时钟：同一脚本可以带 / 不带应声各跑一遍，逐毫秒对照。"""

    def __init__(self, script, *, accept=True, ack=True, accept_fn=None, ack_fn=None,
                 ack_enabled=True, ack_gap_ms=3000, ack_timeout_ms=None, seat="D",
                 confirm_silence_ms=700, cut_wait_ms=1500, think_wait_ms=3000):
        self.script = {normalize(key): value for key, value in script.items()}
        self.accept = accept
        self.ack = ack
        self.seat = seat
        self._accept_fn = accept_fn
        self._ack_fn = ack_fn
        self.gate = GateKeeper(
            accept_fn=accept_fn or (lambda text, scene, seat: accept_outcome(self.accept)),
            ack_fn=None if ack_fn is False else (ack_fn or (lambda text, scene, seat: ack_outcome(self.ack))),
            scene_fn=lambda: None, seat=seat, ack_timeout_ms=ack_timeout_ms,
            accept_timeout_ms=3000)
        cfg = TurnConfig(ack_enabled=ack_enabled, ack_gap_ms=ack_gap_ms,
                         confirm_silence_ms=confirm_silence_ms, cut_wait_ms=cut_wait_ms,
                         think_wait_ms=think_wait_ms, warm_llm=False)
        self.session = TurnSession(cfg, None, gate=self.gate, session_id="ack-" + seat,
                                   evidence_root=None, eot_fn=self._eot,
                                   select_tools_fn=tools_fn, decide_fn=decide_fn, seat_id=seat)
        self.now = 0

    def _eot(self, text):
        key = normalize(text)
        if key not in self.script:
            raise AssertionError("脚本里没有这一版字：" + text)
        return dict(self.script[key])

    def pump(self, until_ms, *, step_ms=10):
        """不再喂新字，只把虚拟时钟往前推（用来看「判断还没回来 / 超时」这几拍）。"""
        while self.now <= until_ms:
            self.session.tick(self.now)
            time.sleep(0.001)
            self.now += step_ms
        return self.session

    def drive(self, events, *, step_ms=10, tail_ms=2500):
        """按脚本喂 ASR，再按虚拟时钟一路推进（收口后再多推 `tail_ms`，让在途判断回来）。"""
        index = 0
        end = events[-1][0] + 20000
        while self.now <= end:
            while index < len(events) and events[index][0] <= self.now:
                t_ms, text, final = events[index]
                self.session.on_asr(t_ms, text, final)
                index += 1
            self.session.tick(self.now)
            time.sleep(0.001)
            if self.session.turn_done and index >= len(events) and self.now >= events[-1][0] + tail_ms:
                break
            self.now += step_ms
        return self.session

    def events_of(self, name):
        return [event for event in self.session.events if event.get("event") == name]

    def acks(self):
        return self.events_of("ack_spoken")

    def ack_gates(self):
        return self.events_of("ack_gate")

    def version_label(self, version):
        for event in self.events_of("eot_scores"):
            if event.get("text_version") == version:
                return event.get("applied_label")
        return None

    def confirm_times(self):
        return [ (event["t_ms"], event["why"]) for event in self.events_of("state_change")
                 if event.get("to") == "CONFIRMED"]

    def gate_routes(self):
        return [event.get("route") for event in self.events_of("gate_decision")]

    def close(self):
        self.session.close()
        self.gate.close()


class AckJudgement(unittest.TestCase):
    """应声这一问的请求 / 响应形状（与判停、要不要回答同一套服务）。"""

    def test_question_is_a_two_choice_ask(self):
        question = ack_question()
        self.assertEqual(question["type"], "choice")
        self.assertEqual(set(question["criteria"]), {"ack", "quiet"})
        self.assertIn("应一声", question["instructions"])
        self.assertIn("不回答", question["instructions"], "问的是陪着说，不是回答")

    def test_state_shape_is_the_same_as_accept(self):
        self.assertEqual(ack_state("帮我看看前面那个"),
                         '{"utterance": "帮我看看前面那个", "scene": ""}')
        self.assertIn("scene", ack_state("导航", {"summary": "主驾在看前面", "revision": 3}))

    def test_words_are_three_to_six_characters(self):
        self.assertEqual(len(ACK_WORDS), len(set(ACK_WORDS)), "固定稿不重复")
        for word in ACK_WORDS:
            chars = ack_chars(word)
            self.assertGreaterEqual(chars, ACK_MIN_CHARS, word)
            self.assertLessEqual(chars, ACK_MAX_CHARS, word)
        self.assertEqual([ack_text(i) for i in range(len(ACK_WORDS))], list(ACK_WORDS))
        self.assertEqual(ack_text(len(ACK_WORDS)), ACK_WORDS[0], "按下标轮着说，不随机")

    def test_default_is_real_judge_and_rule_is_labelled_offline(self):
        meta = build_ack_fn().meta
        self.assertIn("8009", meta["source"])
        self.assertEqual(meta["mode"], "kev_choice")
        self.assertFalse(meta["decided"], "应声起判线未拍，不得写成已拍板")
        self.assertIn("未拍", meta["threshold_note"])
        rule = build_ack_fn(mode="rule")
        self.assertEqual(rule.meta["mode"], "rule")
        self.assertIn("离线", rule.meta["source"])
        self.assertTrue(rule("嗯，那个", None, "D")["say"], "离线替身一律判可以应一声")
        with self.assertRaises(ValueError):
            build_ack_fn(mode="jev-omni")


class AckPolicy(unittest.TestCase):
    """三道闸的唯一判据（纯函数，逐条对照拍板）。"""

    def ok_ack(self, **kwargs):
        base = {"say": True, "state": "ok", "score": 0.8, "confidence": 0.9}
        base.update(kwargs)
        return base

    def test_all_three_gates_pass(self):
        say, reason = decide_ack(eot={"label": "cut", "may_confirm": False, "may_start_early": False},
                                 accept=accept_outcome(True), ack=self.ok_ack(),
                                 last_spoken_ms=None, now_ms=1000, text=ack_text(0))
        self.assertTrue(say, reason)

    def test_strong_complete_never_speaks(self):
        say, reason = decide_ack(eot={"label": "complete", "may_confirm": True, "may_start_early": True},
                                 accept=accept_outcome(True), ack=self.ok_ack(),
                                 last_spoken_ms=None, now_ms=1000, text=ack_text(0))
        self.assertFalse(say)
        self.assertIn("说完了", reason)

    def test_weak_complete_is_a_waiting_tier(self):
        say, reason = decide_ack(eot={"label": "complete", "may_confirm": True, "may_start_early": False},
                                 accept=accept_outcome(True), ack=self.ok_ack(),
                                 last_spoken_ms=None, now_ms=1000, text=ack_text(0))
        self.assertTrue(say, reason)

    def test_not_for_car_never_speaks(self):
        say, reason = decide_ack(eot={"label": "wait", "may_confirm": False, "may_start_early": False},
                                 accept=accept_outcome(False), ack=self.ok_ack(),
                                 last_spoken_ms=None, now_ms=1000, text=ack_text(0))
        self.assertFalse(say)
        self.assertIn("不要回答", reason)

    def test_accept_unavailable_never_speaks(self):
        for accept in ({}, {"state": "pending"}, {"state": "timeout", "accept": None},
                       {"state": "failed", "accept": None}):
            say, reason = decide_ack(eot={"label": "cut", "may_confirm": False, "may_start_early": False},
                                     accept=accept, ack=self.ok_ack(),
                                     last_spoken_ms=None, now_ms=1000, text=ack_text(0))
            self.assertFalse(say, reason)
            self.assertIn("要不要回答", reason)

    def test_eot_unavailable_never_speaks(self):
        for eot in (None, {}, {"label": "error", "may_confirm": False, "error": {"kind": "ValueError"}}):
            say, reason = decide_ack(eot=eot, accept=accept_outcome(True), ack=self.ok_ack(),
                                     last_spoken_ms=None, now_ms=1000, text=ack_text(0))
            self.assertFalse(say, reason)
            self.assertIn("判停", reason)

    def test_ack_unavailable_or_failure_never_speaks(self):
        for ack in (None, {}, {"state": "pending"}, {"state": "timeout"},
                    {"state": "failed", "say": False}, self.ok_ack(say=False)):
            say, reason = decide_ack(eot={"label": "cut", "may_confirm": False, "may_start_early": False},
                                     accept=accept_outcome(True), ack=ack,
                                     last_spoken_ms=None, now_ms=1000, text=ack_text(0))
            self.assertFalse(say, reason)
            self.assertIn("应声判断", reason)

    def test_two_acks_in_a_turn_are_at_least_three_seconds_apart(self):
        say, reason = decide_ack(eot={"label": "cut", "may_confirm": False, "may_start_early": False},
                                 accept=accept_outcome(True), ack=self.ok_ack(),
                                 last_spoken_ms=1000, now_ms=2500, text=ack_text(1))
        self.assertFalse(say)
        self.assertIn("不到 3.0 秒", reason)
        say, _ = decide_ack(eot={"label": "cut", "may_confirm": False, "may_start_early": False},
                            accept=accept_outcome(True), ack=self.ok_ack(),
                            last_spoken_ms=1000, now_ms=4000, text=ack_text(1))
        self.assertTrue(say, "隔够 3 秒就能再应一声")

    def test_one_second_hold_after_speaking(self):
        say, reason = decide_ack(eot={"label": "cut", "may_confirm": False, "may_start_early": False},
                                 accept=accept_outcome(True), ack=self.ok_ack(),
                                 last_spoken_ms=0, now_ms=900, gap_ms=300,
                                 text=ack_text(0))
        self.assertFalse(say, "出声后 1 秒内不再出声（哪怕把 3 秒闸调小）")
        self.assertIn("不到 1.0 秒", reason)

    def test_wording_must_be_three_to_six_characters(self):
        say, reason = decide_ack(eot={"label": "cut", "may_confirm": False, "may_start_early": False},
                                 accept=accept_outcome(True), ack=self.ok_ack(),
                                 last_spoken_ms=None, now_ms=1000, text="嗯")
        self.assertFalse(say)
        self.assertIn("不在 3–6 字", reason)
        say, _ = decide_ack(eot={"label": "cut", "may_confirm": False, "may_start_early": False},
                            accept=accept_outcome(True), ack=self.ok_ack(),
                            last_spoken_ms=None, now_ms=1000, text="嗯我在这儿听着呢你说吧")
        self.assertFalse(say)


class AckInTurnSession(unittest.TestCase):
    """主链：应声什么时候出、什么时候不出；收口与开闸有没有被带偏。"""

    SCRIPT_DONE = {
        "导航回家": STRONG_COMPLETE,
    }
    EVENTS_DONE = [(0, "导航回家", True)]

    # 每一版都还在「没说完」这一侧；相邻两版间隔 < 1.5 秒（不触发静音窗收口），
    # 而应声之间隔 ≥ 3 秒（第 1 版与第 4 版）。
    SCRIPT_WAIT = {
        "帮我看看": WAIT,
        "帮我看看前面": CUT,
        "帮我看看前面那": WAIT,
        "帮我看看前面那台": CUT,
        "帮我看看前面那台车": WAIT,
        "导航去天安门广场": STRONG_COMPLETE,
    }
    EVENTS_WAIT = [(0, "帮我看看", False), (1000, "帮我看看前面", False),
                   (2200, "帮我看看前面那", False), (3400, "帮我看看前面那台", False),
                   (4600, "帮我看看前面那台车", False), (5800, "导航去天安门广场", True)]

    SCRIPT_CHAT = {
        "咱晚上去哪吃": CUT,
    }
    EVENTS_CHAT = [(0, "咱晚上去哪吃", True)]

    SCRIPT_CUT = {"帮我看看": CUT}
    EVENTS_CUT = [(0, "帮我看看", False)]

    SCRIPT_THINK = {"嗯那个": WAIT}
    EVENTS_THINK = [(0, "嗯那个", False)]

    SCRIPT_WEAK = {
        "把空调调到": WEAK_COMPLETE,
        "把空调调到24度": WEAK_COMPLETE,
    }
    EVENTS_WEAK = [(0, "把空调调到", False), (300, "把空调调到24度", True)]

    def run_bench(self, script, events, **kwargs):
        bench = Bench(script, **kwargs)
        bench.drive(events)
        self.addCleanup(bench.close)
        return bench

    # ---- ① 「说完了」的轮次里一次应声都没有 ----
    def test_done_round_has_no_ack_at_all(self):
        bench = self.run_bench(self.SCRIPT_DONE, self.EVENTS_DONE)
        self.assertEqual(bench.acks(), [], "说完了当刻收住：这一轮一次应声都没有")
        self.assertTrue(bench.session.turn_done, "这一轮照旧收住（决策落地即关回合）")
        self.assertIsNotNone(bench.session.t_confirmed)
        self.assertIn("agent", bench.gate_routes(), "开闸照旧（进 Agent）")

    # ---- ② 三个等待档里才允许应声；应声不改变收住时刻 ----
    def test_acks_only_in_waiting_tiers_and_round_still_closes_identically(self):
        with_ack = self.run_bench(self.SCRIPT_WAIT, self.EVENTS_WAIT)
        without = self.run_bench(self.SCRIPT_WAIT, self.EVENTS_WAIT, ack_enabled=False)
        self.assertEqual(with_ack.confirm_times(), without.confirm_times(),
                         "应声不延长也不缩短收住时刻：CONFIRMED 时刻逐毫秒一致")
        self.assertEqual(with_ack.gate_routes(), without.gate_routes(),
                         "开闸结论与改动前一致")
        acks = with_ack.acks()
        self.assertGreaterEqual(len(acks), 2, "两个以上的等待档里能应出声（同一轮至少隔 3 秒）")
        for event in acks:
            label = with_ack.version_label(event["text_version"])
            self.assertIn(label, ("cut", "wait", "complete"), "应声只出现在等待档这一版字上")
            self.assertIsNotNone(label)
            self.assertIn(ack_chars(event["text"]), range(ACK_MIN_CHARS, ACK_MAX_CHARS + 1))
        gaps = [event["gap_ms"] for event in acks[1:]]
        self.assertTrue(gaps, "第二次以后都有间隔")
        self.assertGreaterEqual(min(gaps), 3000, "同一轮两次至少隔 3 秒：%s" % gaps)
        self.assertEqual([event["spoken_total"] for event in acks], list(range(1, len(acks) + 1)))
        self.assertEqual(without.acks(), [], "ack_enabled=False 时一次都不出（A/B 对照）")

    # ---- ②b 话断了 1.5 秒收口：应声不把这一档拉长 / 提前 ----
    def test_cut_tier_still_closes_at_1500ms(self):
        with_ack = self.run_bench(self.SCRIPT_CUT, self.EVENTS_CUT)
        without = self.run_bench(self.SCRIPT_CUT, self.EVENTS_CUT, ack_enabled=False)
        self.assertEqual(with_ack.confirm_times(), without.confirm_times())
        self.assertEqual(with_ack.confirm_times()[0][0], 1500, "话断了仍是静音 1.5 秒后收口")
        self.assertGreaterEqual(len(with_ack.acks()), 1, "1.5 秒等待档里可以应一声")

    # ---- ②c 还在想 3 秒后收口（不被原来的 2 秒上限提前收）：应声不碰这两条 ----
    def test_think_tier_still_closes_at_3000ms(self):
        with_ack = self.run_bench(self.SCRIPT_THINK, self.EVENTS_THINK)
        without = self.run_bench(self.SCRIPT_THINK, self.EVENTS_THINK, ack_enabled=False)
        self.assertEqual(with_ack.confirm_times(), without.confirm_times())
        self.assertEqual(with_ack.confirm_times()[0][0], 3000,
                         "还在想仍 3 秒收口，且没被 2 秒上限提前收")
        self.assertGreaterEqual(len(with_ack.acks()), 1, "3 秒等待档里可以应一声")

    # ---- ③ 弱 complete（可能说完）也是等待档，且它在 0.7s 收敛后仍照旧收住 ----
    def test_weak_complete_may_ack_and_still_confirms_on_the_same_millisecond(self):
        with_ack = self.run_bench(self.SCRIPT_WEAK, self.EVENTS_WEAK)
        without = self.run_bench(self.SCRIPT_WEAK, self.EVENTS_WEAK, ack_enabled=False)
        self.assertEqual(with_ack.confirm_times(), without.confirm_times(),
                         "弱 complete 的收敛窗到点仍按原时刻收住（应声不掺一脚）")
        self.assertGreaterEqual(len(with_ack.acks()), 1, "弱 complete 的 0.7s 等待档里可以应一声")
        states = [event.get("to") for event in with_ack.events_of("state_change")]
        self.assertIn("MAYBE_DONE", states, "弱 complete 走的是「可能说完」这一档（0.7s 收敛窗）")
        self.assertEqual(with_ack.gate_routes(), without.gate_routes())

    # ---- ④ 同一版字「不要回答」→ 不应声；这一轮仍照旧只进上下文 ----
    def test_not_for_car_round_never_acks(self):
        bench = self.run_bench(self.SCRIPT_CHAT, self.EVENTS_CHAT, accept=False)
        self.assertEqual(bench.acks(), [])
        reasons = [event.get("reason") for event in bench.ack_gates()]
        self.assertTrue(any("不要回答" in (reason or "") for reason in reasons), reasons)
        self.assertEqual(bench.gate_routes(), ["context_only"], "不要回答 → 只进上下文，开闸线不被动")
        self.assertEqual(bench.session.context_consumed, 1)

    # ---- ⑤ 判断没回来 / 失败 → 不出声 ----
    def test_ack_pending_never_speaks(self):
        release = threading.Event()
        self.addCleanup(release.set)
        pending = self.run_bench(
            self.SCRIPT_WAIT, [(0, "帮我看看", False)],
            ack_fn=lambda text, scene, seat: (release.wait(1.0) or ack_outcome(True)))
        pending.pump(300)
        self.assertEqual(pending.acks(), [], "应声判断还没回来：这一版不出声")

    def test_ack_failure_never_speaks(self):
        def boom(text, scene, seat):
            raise RuntimeError("ack service down")

        failed = self.run_bench(self.SCRIPT_WAIT, [(0, "帮我看看", False)], ack_fn=boom)
        failed.pump(300)
        self.assertEqual(failed.acks(), [])
        reasons = [event.get("reason") for event in failed.ack_gates()]
        self.assertTrue(any("失败" in (reason or "") for reason in reasons), reasons)

    def test_ack_timeout_never_speaks(self):
        release = threading.Event()
        self.addCleanup(release.set)
        bench = self.run_bench(
            self.SCRIPT_WAIT, [(0, "帮我看看", False)],
            ack_fn=lambda text, scene, seat: (release.wait(1.0) or ack_outcome(True)),
            ack_timeout_ms=5)
        bench.pump(500)
        self.assertEqual(bench.acks(), [], "应声判断超时：这一版不出声")
        self.assertEqual(bench.gate.counters["ack_errors"], 1)

    def test_eot_unavailable_never_speaks(self):
        def eot_down(text):
            raise RuntimeError("eot service down")

        bench = Bench(self.SCRIPT_WAIT)
        bench.session.eot_fn = eot_down
        bench.drive([(0, "帮我看看", False)], tail_ms=300)
        self.assertEqual(bench.acks(), [], "判停没回来 / 失败：不出声（R3）")
        bench.close()

    def test_accept_unavailable_round_never_acks(self):
        bench = self.run_bench(self.SCRIPT_WAIT, [(0, "帮我看看", False)],
                               accept_fn=lambda text, scene, seat: {"state": "timeout", "accept": None})
        bench.pump(1200)
        self.assertEqual(bench.acks(), [], "要不要回答没回来：不出声")

    # ---- ⑥ 两席各算各的，不跨席压制 ----
    def test_two_seats_ack_independently(self):
        seat_d = self.run_bench(self.SCRIPT_WAIT, self.EVENTS_WAIT[:2], seat="D")
        seat_p = self.run_bench(self.SCRIPT_WAIT, self.EVENTS_WAIT[:2], seat="P")
        self.assertGreaterEqual(len(seat_d.acks()), 1)
        self.assertGreaterEqual(len(seat_p.acks()), 1)
        for event in seat_d.acks():
            self.assertEqual(event.get("seat_id"), "D")
        for event in seat_p.acks():
            self.assertEqual(event.get("seat_id"), "P")

    # ---- ⑦ 回声：与本机刚说过的话高度重合的字当回声丢掉，不停听、不改计时 ----
    def test_own_speech_echo_is_dropped_without_touching_the_timers(self):
        bench = self.run_bench(self.SCRIPT_WAIT, [(0, "帮我看看", False), (1200, "帮我看看前面", False)])
        self.assertGreaterEqual(len(bench.acks()), 1)
        spoken = bench.acks()[0]["text"]
        version_before = bench.session.version
        # 自己那句被麦收回来：这一版字就是那句应声
        bench.session.on_asr(bench.acks()[0]["t_ms"] + 100, spoken, False)
        self.assertEqual(bench.session.version, version_before, "回声不进这一版字")
        self.assertEqual(len(bench.events_of("ack_echo_dropped")), 1)
        self.assertEqual(bench.session.counters["ack_echo_drops"], 1)
        # 正常说话一个字都不动
        bench.session.on_asr(bench.acks()[0]["t_ms"] + 300, "帮我看看前面那台车", False)
        self.assertGreater(bench.session.version, version_before, "真说话照常进这一版字")
        self.assertEqual(len(bench.events_of("ack_echo_dropped")), 1)

    # ---- ⑧ 调用次数：同一版字上多一次调用（2 路 → 3 路），写在明面上 ----
    def test_one_extra_judge_call_per_version(self):
        bench = self.run_bench(self.SCRIPT_WAIT, self.EVENTS_WAIT)
        counters = bench.gate.counters
        versions = {event["text_version"] for event in bench.events_of("hub_text")}
        self.assertEqual(counters["texts"], len(versions), "每版字一条记录")
        self.assertEqual(counters["ack_calls"], counters["texts"],
                         "每一版字都应声判断一次（与「每版都判」同口径）")
        self.assertEqual(bench.session.counters["eot_calls"] + counters["accept_calls"]
                         + counters["ack_calls"], 3 * counters["texts"],
                         "同一版字上三路判断：判停 + 要不要回答 + 应不应一声")


class BenchContract(unittest.TestCase):
    """台面能看见应声次数与间隔（双席面板 + 事件行）。"""

    def test_index_html_shows_ack_rows(self):
        page = (ROOT / "ui/index.html").read_text(encoding="utf-8")
        self.assertIn("data-hub-ack", page)
        self.assertIn("data-hub-ack-judge", page)
        self.assertIn("ack_spoken", page)
        self.assertIn("ack_gate", page)
        self.assertIn("本席共", page, "台面能数出应声次数")
        self.assertIn("距上一声", page, "台面能看见应声间隔")

    def test_dual_stream_assembly_wires_the_ack_route(self):
        from app.assembly import hub_gates
        class Mind:
            def scene_snapshot(self):
                return None
        gates = hub_gates(Mind())
        self.assertIn("D", gates, gates)
        self.assertIsNotNone(gates["D"].ack_fn, "双席链路挂了应声判断（第三路）")
        self.assertIn("8009", gates["D"]._ack_source()["source"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
