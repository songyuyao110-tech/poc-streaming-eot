"""期五 · 四处并存的口径收口：离线验收（不连 8009 / 不连 LLM / 不连 TTS）。

钉住 2026-11-16 验收卡 `poc-hub-jev-absorb-p5-20261116-验收.md` 的四条口径：

1. **同一时刻最多一句**；出声后 1 秒内不再出声（应声 / 垫句 / 垫句抢回 / 冷场开口共用）。
2. **垫句优先于应一声**：同一刻两者都该出时，只出垫句、不应声。
3. **有人在说 → 应声 / 垫句；没人说话 → 冷场**：两边触发互斥。
4. **两人同时说**只在「正在说 / 正在准备说」时问，两个入口（正在准备说 / 正在说一句垫句），
   **应一声不算**；不收窄成「只在已开闸之后」。

另外把「期一~期四已经做到」的部分用交叉用例钉住（不重写行为），并验：
- 口径表 / 台面契约（四处何时问、谁优先、出声之后去哪）与代码常量一致；
- 期五不新增判断（gate 计数键与期四一致）、不改收口与开闸（同一段稿 A/B 逐字段一致）。

运行：python3 -B tests/gate/test_hub_arbiter.py
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from gate import hub_arbiter as arb  # noqa: E402
from gate.hub_ack import ACK_GAP_MS  # noqa: E402
from gate.hub_floor import (FLOOR_CUSHION_BACK, FLOOR_FINISH_FIRST, FLOOR_LABELS,  # noqa: E402
                            FLOOR_YIELD)
from gate.hub_gate import GateKeeper  # noqa: E402
from gate.hub_pace import PACE_EAGER, PACE_NORMAL, SPEAK_HOLD_MS  # noqa: E402
from gate.hub_quiet import QUIET_FIRST_MS, QUIET_FOLLOW, QUIET_WAIT  # noqa: E402
from gate.turn_session import TurnConfig, TurnSession  # noqa: E402
from shared.text import normalize  # noqa: E402


# ---- 固定结论（与各期离线用例同一形状） ------------------------------------------------------
def eot_scores(label, *, complete=None, cut=None, wait=None, confidence=0.9, latency=10.0):
    if label == "complete":
        return {"label": "complete", "complete": complete if complete is not None else 0.95,
                "cut": cut if cut is not None else 0.02, "wait": wait if wait is not None else 0.03,
                "confidence": confidence, "wall_ms": latency}
    if label == "cut":
        return {"label": "cut", "complete": 0.08, "cut": cut if cut is not None else 0.86,
                "wait": 0.06, "confidence": confidence, "wall_ms": latency}
    return {"label": "wait", "complete": 0.06, "cut": 0.04,
            "wait": wait if wait is not None else 0.90, "confidence": confidence,
            "wall_ms": latency}


STRONG_COMPLETE = eot_scores("complete", complete=0.95, confidence=0.95)
# 「可能说完」：complete 过 0.80、但 confidence 没过 0.70 → 停在 0.7 秒等待档（应声 / 垫句能出）。
WEAK_COMPLETE = eot_scores("complete", complete=0.86, confidence=0.40)
CUT = eot_scores("cut")


def accept_outcome(accept, *, source="测试接不接", model="fake/accept"):
    return {"accept": accept, "state": "ok", "confidence": 0.9 if accept else 0.2,
            "score": 0.9 if accept else 0.2, "source": source, "model": model,
            "reason": "测试用接不接结论"}


def ack_outcome(say, *, source="离线替身（仅供离线演示）", model="rule/离线替身（未拍板）"):
    return {"say": bool(say), "state": "ok", "score": 0.8 if say else 0.2, "confidence": 0.9,
            "source": source, "model": model, "reason": "测试用应声结论"}


def pace_outcome(tier, *, source="离线替身（仅供离线演示）", model="rule/离线替身（未拍板）"):
    probabilities = {name: (0.82 if name == tier else 0.06) for name in
                     ("normal", "patient", "narrative", "eager")}
    return {"pace": tier, "raw_pace": tier, "label": tier, "probabilities": probabilities,
            "score": probabilities[tier], "confidence": 0.9, "state": "ok",
            "source": source, "model": model, "reason": "测试用节奏结论"}


def cushion_outcome(say, *, source="离线替身（仅供离线演示）", model="rule/离线替身（未拍板）"):
    return {"say": bool(say), "state": "ok", "score": 0.8 if say else 0.2, "confidence": 0.9,
            "source": source, "model": model, "reason": "测试用垫句结论"}


def floor_outcome_for(outlet, *, source="离线替身（仅供离线演示）", model="rule/离线替身（未拍板）"):
    return {"choice": outlet, "outlet": outlet, "label": FLOOR_LABELS[outlet],
            "action": "测试用出口", "score": 0.82, "confidence": 0.9, "state": "ok",
            "source": source, "model": model, "reason": "测试用四选结论"}


def quiet_outcome(choice, *, source="离线替身（仅供离线演示）", model="rule/离线替身（未拍板）"):
    return {"choice": choice, "outlet": choice, "label": choice, "score": 0.82,
            "confidence": 0.9, "state": "ok", "source": source, "model": model,
            "reason": "测试用冷场结论"}


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
            "log_fields": {"decision_head": "arbiter-test"}}


class Bench:
    """一段固定稿 + 一个虚拟时钟：四处判断都能挂上，并按需换结论。

    `decide_hold=True` 时决策卡在闸上（`("release", t)` 放行）——用来把「正在准备说」那一刻停住，
    人又开口才落得进期三的窗口一。
    """

    def __init__(self, script, *, accept=True, ack=True, pace=PACE_NORMAL, cushion=True,
                 floor=FLOOR_YIELD, quiet=QUIET_WAIT, ack_enabled=True, cushion_enabled=True,
                 floor_enabled=True, quiet_enabled=True, decide_hold=False,
                 cushion_gap_ms=1000, ack_gap_ms=ACK_GAP_MS, seat="D", tail_ms=2000):
        self.script = {normalize(key): value for key, value in script.items()}
        self.accept = accept
        self.ack_plan = list(ack) if isinstance(ack, (list, tuple)) else [ack]
        self.pace_plan = list(pace) if isinstance(pace, (list, tuple)) else [pace]
        self.cushion_plan = list(cushion) if isinstance(cushion, (list, tuple)) else [cushion]
        self.floor_plan = list(floor) if isinstance(floor, (list, tuple)) else [floor]
        self.quiet_plan = list(quiet) if isinstance(quiet, (list, tuple)) else [quiet]
        self.ack_n = self.pace_n = self.cushion_n = self.floor_n = self.quiet_n = 0
        self.tail_ms = tail_ms
        self.decide_gate = threading.Event()
        if not decide_hold:
            self.decide_gate.set()
        self.gate = GateKeeper(
            accept_fn=lambda text, scene, seat: accept_outcome(self.accept),
            ack_fn=self._ack if ack_enabled else None,
            pace_fn=self._pace,
            cushion_fn=self._cushion if cushion_enabled else None,
            floor_fn=self._floor if floor_enabled else None,
            quiet_fn=self._quiet if quiet_enabled else None,
            scene_fn=lambda: None, seat=seat, accept_timeout_ms=3000)
        # 省掉「开场一两轮先稳住」：直接把这一拍的实际节奏预置成测试要的档（只影响垫句这一问的闸，
        # 不改 hold_pace 的任何规则；期二用例另有一段专门验开场保持）。
        if str(pace).lower() == PACE_EAGER:
            self.gate.pace = PACE_EAGER
        cfg = TurnConfig(ack_enabled=ack_enabled, ack_gap_ms=ack_gap_ms,
                         cushion_enabled=cushion_enabled, cushion_gap_ms=cushion_gap_ms,
                         floor_enabled=floor_enabled, quiet_enabled=quiet_enabled,
                         confirm_silence_ms=700, cut_wait_ms=1500, think_wait_ms=3000,
                         warm_llm=False)
        self.session = TurnSession(cfg, None, gate=self.gate, session_id="arb-" + seat,
                                   evidence_root=None, eot_fn=self._eot,
                                   select_tools_fn=tools_fn, decide_fn=self._decide,
                                   seat_id=seat)
        self.now = 0

    # ---- 四处判断（按第几次问取脚本里的那一条） ------------------------------------------
    @staticmethod
    def _take(plan, index):
        return plan[min(index, len(plan) - 1)]

    def _ack(self, text, scene, seat):
        value = self._take(self.ack_plan, self.ack_n)
        self.ack_n += 1
        return ack_outcome(value)

    def _pace(self, text, scene, seat, speech_ms=None):
        value = self._take(self.pace_plan, self.pace_n)
        self.pace_n += 1
        return pace_outcome(value)

    def _cushion(self, text, scene, seat, speech_ms=None):
        value = self._take(self.cushion_plan, self.cushion_n)
        self.cushion_n += 1
        return cushion_outcome(value)

    def _floor(self, text, scene=None, seat="D", hub_state=None, hub_sentence=None):
        value = self._take(self.floor_plan, self.floor_n)
        self.floor_n += 1
        return floor_outcome_for(value)

    def _quiet(self, *, quiet_ms, hub_last="", recent="", scene=None, seat="D",
               check_allowed=None):
        value = self._take(self.quiet_plan, self.quiet_n)
        self.quiet_n += 1
        return quiet_outcome(value)

    def _eot(self, text):
        key = normalize(text)
        if key not in self.script:
            raise AssertionError("脚本里没有这一版字：" + text)
        return dict(self.script[key])

    def _decide(self, utterance, selected, schemas, tool_set, context, **kwargs):
        if not self.decide_gate.wait(timeout=5.0):
            raise AssertionError("决策没人放行：" + utterance)
        return {"decision": {"kind": "tool_call",
                             "tool": (selected or ["car_navi_to_destination"])[0],
                             "arguments": {}}, "llm_ms": 1.0,
                "log_fields": {"decision_head": "arbiter-test"}}

    # ---- 驱动 ---------------------------------------------------------------------------
    def drive(self, commands, *, step_ms=10):
        """`("asr", t, text, final)` / `("vad", t, kind)` / `("release", t)` / `("hold", t)` /
        `("end", t)`。"""
        index = 0
        end = max(item[1] for item in commands) + self.tail_ms
        while self.now <= end:
            while index < len(commands) and commands[index][1] <= self.now:
                item = commands[index]
                kind = item[0]
                index += 1
                if kind == "asr":
                    self.session.on_asr(item[1], item[2], item[3])
                elif kind == "vad":
                    self.session.on_vad(item[1], item[2])
                elif kind == "release":
                    self.decide_gate.set()
                elif kind == "hold":
                    self.decide_gate.clear()
                elif kind == "end":
                    return self
            self.session.tick(self.now)
            time.sleep(0.001)
            self.now += step_ms
        return self

    def events_of(self, name):
        return [event for event in self.session.events if event.get("event") == name]

    def speech_events(self):
        """四处真正出声的事件：应声 / 垫句（含垫句抢回）/ 冷场开口。"""
        out = []
        for name, kind in (("ack_spoken", "ack"), ("cushion_spoken", "cushion"),
                           ("quiet_spoken", "quiet")):
            for event in self.events_of(name):
                out.append({"t_ms": int(event["t_ms"]), "event": name,
                            "kind": event.get("kind") or kind, "text": event.get("text") or ""})
        return sorted(out, key=lambda item: item["t_ms"])

    def assert_one_sentence(self, testcase):
        """同一时刻最多一句；出声后 1 秒内没有第二句。"""
        speech = self.speech_events()
        for index in range(1, len(speech)):
            gap = speech[index]["t_ms"] - speech[index - 1]["t_ms"]
            testcase.assertGreaterEqual(
                gap, int(SPEAK_HOLD_MS),
                "出声后 %dms 内又出了第二句：%r" % (int(SPEAK_HOLD_MS), speech))
        return speech

    def close(self):
        self.session.close()
        self.gate.close()


def run(script, commands, **kwargs):
    bench = Bench(script, **kwargs)
    try:
        bench.drive(commands)
        return bench
    except Exception:
        bench.close()
        raise


# ---- 交叉情形用的固定稿 ---------------------------------------------------------------------
# ①「应声与垫句同一刻都该出」：连续的弱 complete（停在 0.7 秒等待档），最后一版已说满 1.5 秒。
RUN_TEXT = "导航回家啊呀嗯"
RUN_SCRIPT = {
    "导航回家": WEAK_COMPLETE,
    "导航回家啊": WEAK_COMPLETE,
    "导航回家啊呀": WEAK_COMPLETE,
    RUN_TEXT: WEAK_COMPLETE,
    "等一下先别导航": STRONG_COMPLETE,
}
RUN_COMMANDS = [("asr", 0, "导航回家", True), ("asr", 600, "导航回家啊", False),
                ("asr", 1200, "导航回家啊呀", False), ("asr", 1500, RUN_TEXT, False),
                ("end", 2600)]

SIMPLE_SCRIPT = {"导航回家": STRONG_COMPLETE, "等一下先别导航": STRONG_COMPLETE,
                 "还有多远": WEAK_COMPLETE}


# =============================================================================================
class PolicyTableContract(unittest.TestCase):
    """一页口径表（代码 / 台面 / SUMMARY 同一份字面）与共用静默闸。"""

    def test_table_covers_the_four_speeches_with_when_priority_and_after(self):
        table = {row["slot"]: row for row in arb.policy_meta()["table"]}
        self.assertEqual(set(table), {arb.SLOT_CUSHION, arb.SLOT_ACK,
                                      arb.SLOT_FLOOR, arb.SLOT_QUIET})
        for row in table.values():
            for key in ("label", "when", "priority", "after"):
                self.assertTrue(row.get(key), "口径表缺 %s：%r" % (key, row))
            self.assertFalse(row["agent"], "四处都不进 Agent")
        self.assertTrue(table[arb.SLOT_CUSHION]["remember"], "垫句记住")
        self.assertFalse(table[arb.SLOT_ACK]["remember"], "应声不记住")
        self.assertTrue(table[arb.SLOT_QUIET]["remember"], "冷场记住")

    def test_priority_is_cushion_over_ack_and_the_hold_is_one_second(self):
        meta = arb.policy_meta()
        self.assertEqual(meta["one_sentence_hold_ms"], int(SPEAK_HOLD_MS))
        self.assertEqual(meta["one_sentence_hold_ms"], 1000, "出声后 1 秒内不再出声（不改数字）")
        self.assertEqual(meta["priority"], [arb.SLOT_CUSHION, arb.SLOT_ACK])
        self.assertIn("垫句优先于应声", meta["priority_note"])
        self.assertEqual(arb.same_tick_winner(cushion=True, ack=True), arb.SLOT_CUSHION)
        self.assertEqual(arb.same_tick_winner(cushion=False, ack=True), arb.SLOT_ACK)
        self.assertEqual(arb.same_tick_winner(cushion=False, ack=False), "")

    def test_speech_gate_is_shared_and_starts_from_top_speech_now(self):
        blocked, why = arb.speech_blocked(now_ms=500, last_spoken_ms=0)
        self.assertTrue(blocked)
        self.assertIn("1 秒", why)
        self.assertFalse(arb.speech_blocked(now_ms=1000, last_spoken_ms=0)[0])
        self.assertFalse(arb.speech_blocked(now_ms=10, last_spoken_ms=None)[0])
        # 应声自己的频次闸比 1 秒更宽（3 秒）；静默闸取更宽的那一个。
        self.assertEqual(arb.speech_hold_ms(ACK_GAP_MS), int(ACK_GAP_MS))

    def test_someone_speaking_goes_ack_cushion_nobody_speaking_goes_quiet(self):
        meta = arb.policy_meta()
        self.assertEqual(set(meta["speaking_slots"]), {arb.SLOT_ACK, arb.SLOT_CUSHION})
        self.assertEqual(meta["silent_slots"], [arb.SLOT_QUIET])
        self.assertIn("互斥", meta["exclusive_note"])
        self.assertEqual(arb.speaking_mode(True), "应声 / 垫句（有人在说）")
        self.assertEqual(arb.speaking_mode(False), "冷场（没人说话）")

    def test_floor_has_exactly_the_two_pinned_windows_and_ack_is_not_one(self):
        meta = arb.policy_meta()
        self.assertEqual(meta["floor_windows"], ["preparing", "cushion_speaking"])
        self.assertEqual(meta["floor_excluded"], [arb.SLOT_ACK])
        self.assertIn("应一声不算", meta["floor_note"])
        self.assertIn("正在说一句垫句", meta["floor_note"])
        self.assertIn("正在准备说", meta["floor_note"])

    def test_offline_substitutes_never_claim_the_real_judge(self):
        # 台面 / SSE 里「离线替身」不能写成「判断模型 8009」：期二/期三的节奏与垫句替身也要照实标。
        from gate.hub_ack import rule_ack
        from gate.hub_floor import rule_floor
        from gate.hub_pace import PACE_NORMAL, rule_cushion, rule_pace
        from gate.hub_quiet import rule_quiet
        verdicts = [
            rule_pace("导航回家", None, "D", speech_ms=2000),
            rule_cushion("导航回家", None, "D", speech_ms=2000),
            rule_ack("导航回家", None, "D"),
            rule_floor("导航回家", None, "D"),
            rule_quiet(quiet_ms=4000, hub_last="导航回家", recent=[], scene=None, seat="D"),
        ]
        for verdict in verdicts:
            self.assertIn("离线", verdict["source"], verdict)
            self.assertNotIn("8009", verdict["source"], verdict)

    def test_phase_five_adds_no_judgement_and_no_new_gate_counters(self):
        meta = arb.policy_meta()
        self.assertEqual(meta["new_decisions"], 0)
        self.assertNotIn("instructions", meta, "期五不新增问法")
        bench = run(SIMPLE_SCRIPT, [("asr", 0, "导航回家", True), ("end", 1200)])
        try:
            counters = bench.gate.counters
            expected = {
                "texts", "accept_calls", "accept_errors", "eot_calls", "gates", "gate_open",
                "gate_context_only", "gate_wait", "scene_missing", "version_mismatch",
                "ack_calls", "ack_errors", "ack_say", "pace_calls", "pace_errors", "pace_late",
                "cushion_calls", "cushion_errors", "cushion_late", "cushion_say",
                "floor_calls", "floor_errors", "floor_late", "floor_yield", "floor_finish_first",
                "floor_continue", "floor_cushion_back", "quiet_calls", "quiet_errors",
                "quiet_late", "quiet_wait", "quiet_follow", "quiet_check",
            }
            self.assertEqual(set(counters), expected, "期五不新增判断：计数键与期四一致")
            summary = bench.session.summary()
            self.assertIn("hub_policy", summary)
            self.assertEqual(summary["hub_policy"]["policy"]["new_decisions"], 0)
            self.assertEqual(set(summary["hub_policy"]["counts"]),
                             {arb.SLOT_ACK, arb.SLOT_CUSHION, arb.SLOT_FLOOR, arb.SLOT_QUIET})
        finally:
            bench.close()


# =============================================================================================
class SameTickPriority(unittest.TestCase):
    """① 应声与垫句同一刻都该出：只出垫句；出声后 1 秒内没有第二句。"""

    def test_cushion_beats_ack_when_both_are_due_in_the_same_tick(self):
        bench = run(RUN_SCRIPT, RUN_COMMANDS, ack=True, pace=PACE_EAGER, cushion=True)
        try:
            speech = bench.assert_one_sentence(self)
            cushions = [item for item in speech if item["event"] == "cushion_spoken"]
            self.assertEqual(len(cushions), 1, "最后一版「积极 + 说满 1.5 秒」：垫一句")
            cushion_t = cushions[0]["t_ms"]
            # 同一刻（垫句真的出声的这一拍）不许再有应声：垫句优先。
            same_tick_acks = [item for item in speech
                              if item["event"] == "ack_spoken" and item["t_ms"] == cushion_t]
            self.assertEqual(same_tick_acks, [], "同一时刻最多一句：垫了就不再应声")
            later_acks = [item for item in speech
                          if item["event"] == "ack_spoken" and item["t_ms"] >= cushion_t]
            self.assertEqual(later_acks, [], "垫句之后这一拍也不应声（垫句优先）")
            gates = [event for event in bench.events_of("ack_gate")
                     if int(event.get("t_ms") or 0) == cushion_t
                     and "垫句优先" in (event.get("reason") or "")]
            self.assertTrue(gates, "台面要能看见「垫句优先」："
                            + str([event.get("reason") for event in bench.events_of("ack_gate")]))
            self.assertEqual(bench.session.cushion_spoken_total, 1)
        finally:
            bench.close()

    def test_second_ack_within_one_second_is_blocked_after_a_cushion(self):
        commands = [("asr", 0, "导航回家", True), ("asr", 600, "导航回家啊", False),
                    ("asr", 1200, "导航回家啊呀", False), ("asr", 1500, RUN_TEXT, False),
                    ("asr", 1700, RUN_TEXT + "呢", False), ("end", 3200)]
        script = dict(RUN_SCRIPT)
        script[RUN_TEXT + "呢"] = WEAK_COMPLETE
        bench = run(script, commands, ack=True, pace=PACE_EAGER, cushion=True)
        try:
            speech = bench.speech_events()
            self.assertGreaterEqual(len(speech), 1)
            first = speech[0]["t_ms"]
            early = [item for item in speech if 0 < item["t_ms"] - first < int(SPEAK_HOLD_MS)]
            self.assertEqual(early, [], "垫句之后 1 秒内不能有第二句：%r" % speech)
            gates = bench.events_of("ack_gate") + bench.events_of("cushion_gate")
            self.assertTrue(any("离上一声" in (event.get("reason") or "") for event in gates),
                            "台面要能看见「离上一声不到 1 秒」：" + str(gates))
        finally:
            bench.close()


# =============================================================================================
class CushionWindow(unittest.TestCase):
    """② 垫句那一小段也算「正在说」：人又开口 → 四选照问（window=speech），且不叠第二句。"""

    def test_insertion_during_cushion_asks_floor_with_the_speech_window(self):
        commands = list(RUN_COMMANDS[:-1]) + [("asr", 1650, "等一下先别导航", True), ("end", 2600)]
        bench = run(RUN_SCRIPT, commands, ack=True, pace=PACE_EAGER, cushion=True,
                    floor=FLOOR_YIELD)
        try:
            asked = bench.events_of("floor_asked")
            self.assertTrue(asked, "垫句那一小段里人又开口：要问一次四选")
            self.assertEqual(asked[0]["window"], "speech")
            self.assertEqual(asked[0]["hub_state"], "speaking")
            stops = bench.events_of("cushion_stopped")
            self.assertTrue(stops, "让路落在垫句窗口：这句垫句停下、回到收听")
            self.assertEqual([event["outlet"] for event in bench.events_of("floor_outcome")],
                             [FLOOR_YIELD])
            bench.assert_one_sentence(self)
        finally:
            bench.close()

    def test_cushion_back_does_not_stack_a_second_cushion_and_keeps_the_insertion(self):
        commands = list(RUN_COMMANDS[:-1]) + [("asr", 1650, "等一下先别导航", True),
                                              ("release", 1650), ("end", 3600)]
        bench = run(RUN_SCRIPT, commands, ack=True, pace=PACE_EAGER, cushion=True,
                    floor=FLOOR_CUSHION_BACK, decide_hold=True)
        try:
            outcomes = bench.events_of("floor_outcome")
            self.assertEqual([event["outlet"] for event in outcomes], [FLOOR_FINISH_FIRST],
                             "当前这句本身就是垫句：不再叠第二句，按「说完再接」落地")
            cushions = bench.events_of("cushion_spoken")
            self.assertEqual(len(cushions), 1, "这一小段只允许那一句垫句：%r" % cushions)
            self.assertEqual(bench.events_of("turn_void"), [])
            self.assertTrue(bench.events_of("floor_released"), "插话排队后放行、随后被回答")
            bench.assert_one_sentence(self)
        finally:
            bench.close()

    def test_cushion_back_just_spoke_blocks_the_next_ack_within_one_second(self):
        script = {"导航回家": STRONG_COMPLETE, "等一下先别导航": WEAK_COMPLETE}
        commands = [("asr", 0, "导航回家", True), ("hold", 0),
                    ("asr", 300, "等一下先别导航", True), ("release", 700), ("end", 3000)]
        bench = run(script, commands, ack=True, cushion=False, pace=PACE_NORMAL,
                    floor=FLOOR_CUSHION_BACK, decide_hold=True)
        try:
            speech = bench.speech_events()
            back = [item for item in speech if item["event"] == "cushion_spoken"
                    and item["kind"] == "cushion_back"]
            self.assertEqual(len(back), 1, "这一回应当垫句抢回一句：%r" % speech)
            t = back[0]["t_ms"]
            early = [item for item in speech
                     if item["event"] == "ack_spoken" and 0 <= item["t_ms"] - t < 1000]
            self.assertEqual(early, [], "垫句抢回刚出声：1 秒内不许再应声：%r" % speech)
            gates = [event for event in bench.events_of("ack_gate")
                     if "离上一声" in (event.get("reason") or "")]
            self.assertTrue(gates, "台面要能看见「离上一声不到 1 秒」")
            bench.assert_one_sentence(self)
        finally:
            bench.close()


# =============================================================================================
class QuietVersusSpeaking(unittest.TestCase):
    """③ 有人说话 → 应声 / 垫句；没人说话 → 冷场。两边触发互斥。"""

    def test_human_speech_just_before_the_tick_clears_quiet_and_never_speaks(self):
        commands = [("asr", 0, "导航回家", True), ("asr", 4000, "等一下先别导航", True),
                    ("end", 5200)]
        bench = run(SIMPLE_SCRIPT, commands, ack=True, pace=PACE_EAGER, cushion=True,
                    quiet=QUIET_FOLLOW, decide_hold=False)
        try:
            self.assertEqual(bench.events_of("quiet_asked"), [], "有人开口后这一拍不再问冷场")
            self.assertEqual(bench.events_of("quiet_spoken"), [], "有人说话时不出冷场那一句")
            cleared = bench.events_of("quiet_cleared")
            self.assertTrue(any(event.get("why") == "human_speech" for event in cleared),
                            "人一开口，冷场计时立刻清零")
        finally:
            bench.close()

    def test_nobody_speaking_goes_quiet_and_then_no_ack_or_cushion(self):
        commands = [("asr", 0, "导航回家", True), ("end", 5600)]
        bench = run(SIMPLE_SCRIPT, commands, ack=True, pace=PACE_EAGER, cushion=True,
                    quiet=QUIET_FOLLOW)
        try:
            spoken = bench.events_of("quiet_spoken")
            self.assertTrue(spoken, "没人说话、安静到刻度：走冷场")
            quiet_t = int(spoken[0]["t_ms"])
            # 安静时不会再应声 / 垫句：冷场开口之后这一小段里没有第二句。
            later = [item for item in bench.speech_events()
                     if item["kind"] in ("ack", "cushion") and item["t_ms"] >= quiet_t]
            self.assertEqual(later, [], "没人说话时不应声、不垫句：%r" % later)
            self.assertGreaterEqual(quiet_t, QUIET_FIRST_MS)
            self.assertEqual(bench.events_of("ack_spoken"), [])
            self.assertEqual(bench.events_of("cushion_spoken"), [])
            bench.assert_one_sentence(self)
        finally:
            bench.close()

    def test_quiet_due_but_human_speaks_exactly_at_the_tick_does_not_steal_the_turn(self):
        commands = [("asr", 0, "导航回家", True), ("asr", 4100, "还有多远", False),
                    ("end", 5400)]
        bench = run(SIMPLE_SCRIPT, commands, ack=True, pace=PACE_EAGER, cushion=True,
                    quiet=QUIET_FOLLOW)
        try:
            self.assertEqual(bench.events_of("quiet_spoken"), [])
            # 人已经在说：这一侧最多是应声 / 垫句（这里第一条是应声，不许是冷场）。
            for event in bench.speech_events():
                self.assertNotEqual(event["kind"], "quiet")
        finally:
            bench.close()


# =============================================================================================
class FloorEntry(unittest.TestCase):
    """④ 两人同时说只在「正在说 / 正在准备说」：两个入口 + 应声不算。"""

    def test_ack_alone_is_not_a_floor_window(self):
        commands = [("asr", 0, "导航回家", True), ("vad", 200, "speech_start"),
                    ("end", 2000)]
        bench = run({"导航回家": WEAK_COMPLETE}, commands, ack=True, pace=PACE_NORMAL,
                    cushion=False, quiet=QUIET_WAIT)
        try:
            self.assertTrue(bench.events_of("ack_spoken"), "这一版应当先应一声")
            self.assertEqual(bench.events_of("floor_asked"), [],
                             "应一声不算「正在说」：不在这里问四选")
        finally:
            bench.close()

    def test_preparing_to_answer_is_the_first_window(self):
        commands = [("asr", 0, "导航回家", True), ("hold", 0),
                    ("asr", 300, "等一下先别导航", True), ("release", 700), ("end", 2600)]
        bench = run(SIMPLE_SCRIPT, commands, ack=True, pace=PACE_NORMAL, cushion=False,
                    floor=FLOOR_YIELD, decide_hold=True)
        try:
            asked = bench.events_of("floor_asked")
            self.assertTrue(asked, "正在准备说 / 正在回答，人又开口：要问四选")
            self.assertEqual(asked[0]["window"], "turn")
            self.assertEqual(asked[0]["hub_state"], "preparing")
            self.assertEqual([event["outlet"] for event in bench.events_of("floor_outcome")],
                             [FLOOR_YIELD])
            self.assertEqual(len(bench.events_of("turn_void")), 1, "让路仍作废本轮（现网那条）")
        finally:
            bench.close()


# =============================================================================================
class FullRegressionFlag(unittest.TestCase):
    """期五不许带偏收口与开闸：同一段稿「四处全开 vs 四下全关」逐字段一致。"""

    @staticmethod
    def _strip(events):
        drop = {"ack_spoken", "ack_gate", "hub_ack", "hub_pace", "hub_cushion",
                "cushion_spoken", "cushion_gate", "hub_floor", "floor_asked", "floor_insertion",
                "floor_vad", "floor_outcome", "floor_queued", "floor_released", "floor_context",
                "floor_gate", "floor_hold", "cushion_stopped", "hub_quiet", "quiet_asked",
                "quiet_cleared", "quiet_gate", "quiet_spoken"}
        out = []
        for event in events:
            if event.get("event") in drop:
                continue
            row = {key: value for key, value in event.items()
                   if key != "event" and key != "hub_policy"
                   and not str(key).startswith(("ack", "cushion", "pace", "floor", "quiet",
                                                "hub_last"))}
            cerebrum = row.get("cerebrum")
            if isinstance(cerebrum, dict):
                cerebrum = {key: value for key, value in cerebrum.items()
                            if key not in ("ack", "ack_source", "pace", "pace_source", "cushion",
                                           "cushion_source", "floor", "floor_source", "quiet",
                                           "quiet_source")}
                if isinstance(cerebrum.get("counters"), dict):
                    cerebrum["counters"] = {key: value for key, value in
                                            cerebrum["counters"].items()
                                            if not str(key).startswith(("ack", "cushion", "pace",
                                                                        "floor", "quiet"))}
                if isinstance(cerebrum.get("versions"), list):
                    cerebrum["versions"] = [
                        {key: value for key, value in version.items()
                         if key not in ("ack", "pace", "cushion")}
                        for version in cerebrum["versions"]]
                row["cerebrum"] = cerebrum
            out.append(row)
        return out

    def test_turn_times_gate_routes_and_decisions_are_untouched(self):
        commands = [("asr", 0, "导航回家", True), ("end", 1200)]
        on = run(SIMPLE_SCRIPT, commands, ack=True, pace=PACE_EAGER, cushion=True,
                 floor=FLOOR_YIELD, quiet=QUIET_FOLLOW)
        # 「四处都不出声」但保留同一版字上的 speech_ms 输入：只用来对照收口 / 开闸 / 决策。
        off = run(SIMPLE_SCRIPT, commands, ack_enabled=False, cushion_enabled=True,
                  cushion=False, floor_enabled=False, quiet_enabled=False, pace=PACE_NORMAL)
        try:
            self.assertEqual([event["t_ms"] for event in on.events_of("turn_done")],
                             [event["t_ms"] for event in off.events_of("turn_done")])
            self.assertEqual([event.get("route") for event in on.events_of("gate_decision")],
                             [event.get("route") for event in off.events_of("gate_decision")])
            self.assertEqual([(event["t_ms"], event.get("utterance"))
                              for event in on.events_of("llm_decision")],
                             [(event["t_ms"], event.get("utterance"))
                              for event in off.events_of("llm_decision")])
            self.assertEqual(self._strip(on.session.events), self._strip(off.session.events),
                             "去掉四处自己的事件与字段后，收口 / 开闸 / 决策逐字段一致")
        finally:
            on.close()
            off.close()


if __name__ == "__main__":
    unittest.main(verbosity=2)
