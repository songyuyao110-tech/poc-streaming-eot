"""期二 · 节奏 + 垫句离线验收（不连 8009 / 不连 LLM / 不连 TTS）。

钉住 2026-11-16 已拍板的口径（验收卡 `poc-hub-jev-absorb-p2-20261116-验收.md`）：

1. **四档节奏各跑一段**：只有「积极」会出现垫句（询问或出声）；平常 / 耐心 / 讲述**根本不问**垫句
   （不是问了不用：`cushion_calls == 0`）。
2. **积极但没满 1.5 秒**：不问、不垫。
3. **垫句之后这句在上下文（`hub_said`）但没进 Agent**：不选工具、不执行、不开闸；
   同一段脚本「垫句开 vs 关」的工具 / 决策条数一样。
4. **收住时刻与开闸结论不被带偏**：same script on/off → CONFIRMED 时刻逐毫秒一致、
   路由一致（0.7 / 1.5 / 3 秒都不被垫句拉长或缩短）。
5. **垫句优先于应声、同一时刻最多一句**：同一拍只垫不应；出声后 1 秒内不再出声。
6. **判断没回来 / 失败 / 超时**：不垫、节奏保持上一拍、收口与开闸不变。
7. **调用次数**：同一版字 4 路（判停 + 要不要回答 + 应不应一声 + 节奏）；
   「积极 + 已满 1.5 秒」的那一版再多 1 次（垫句这一问），明确写出来。

运行：python3 -B tests/gate/test_hub_pace.py
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from gate.hub_pace import (CUSHION_MAX_CHARS, CUSHION_MIN_CHARS, CUSHION_MIN_SPEECH_MS,  # noqa: E402
                          CUSHION_WORDS, PACE_EAGER, PACE_LABELS, PACE_NARRATIVE, PACE_NORMAL,
                          PACE_ORDER, PACE_PATIENT, build_cushion_fn, build_pace_fn, cushion_chars,
                          cushion_question, cushion_text, decide_cushion, hold_pace, pace_question,
                          pace_state)
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


def pace_outcome(tier, *, probs=None, source="测试节奏判断", model="fake/pace"):
    probabilities = dict(probs) if probs else {
        name: (0.82 if name == tier else round(0.18 / 3, 3)) for name in PACE_ORDER}
    return {"pace": tier, "raw_pace": tier, "label": PACE_LABELS.get(tier, tier),
            "probabilities": probabilities, "score": probabilities.get(tier, 0.82),
            "confidence": 0.9, "state": "ok", "source": source, "model": model,
            "reason": "测试用节奏结论"}


def cushion_outcome(say, *, source="测试垫句判断", model="fake/cushion"):
    return {"say": say, "state": "ok", "score": 0.8 if say else 0.2, "confidence": 0.9,
            "source": source, "model": model, "reason": "测试用垫句判断"}


def tools_fn(text, tool_set=None, context=None):
    name = "car_navi_to_destination"
    return {"scores": {"tool:" + name: 0.9}, "selected": [name],
            "schemas": [{"name": name, "description": "d", "parameters": {"type": "object"}}],
            "native_schemas": None, "domains": None,
            "timing": {"stage1_ms": 1.0, "stage2_ms": 0.0, "total_ms": 1.0}}


def decide_fn(utterance, selected, schemas, tool_set, context, **kwargs):
    return {"decision": {"kind": "tool_call", "tool": (selected or ["car_navi_to_destination"])[0],
                         "arguments": {}}, "llm_ms": 1.0, "log_fields": {"decision_head": "cushion-test"}}


class Bench:
    """一段固定稿 + 一个虚拟时钟：同一脚本可以「垫句开 / 关」、换四档节奏各跑一遍。"""

    def __init__(self, script, *, accept=True, ack=True, pace=PACE_NORMAL, cushion=True,
                 accept_fn=None, ack_fn=None, pace_fn=None, cushion_fn=None,
                 ack_enabled=True, cushion_enabled=True, cushion_gap_ms=1000,
                 ack_timeout_ms=None, pace_timeout_ms=None, cushion_timeout_ms=None,
                 seat="D", confirm_silence_ms=700, cut_wait_ms=1500, think_wait_ms=3000):
        self.script = {normalize(key): value for key, value in script.items()}
        self.accept = accept
        self.ack = ack
        self.pace = pace
        self.cushion = cushion
        self.seat = seat
        self.gate = GateKeeper(
            accept_fn=accept_fn or (lambda text, scene, seat: accept_outcome(self.accept)),
            ack_fn=None if ack_fn is False else
            (ack_fn or (lambda text, scene, seat: ack_outcome(self.ack))),
            pace_fn=None if pace_fn is False else
            (pace_fn or (lambda text, scene, seat, speech_ms=None: pace_outcome(self.pace))),
            cushion_fn=None if cushion_fn is False else
            (cushion_fn or (lambda text, scene, seat, speech_ms=None:
                            cushion_outcome(self.cushion))),
            scene_fn=lambda: None, seat=seat, ack_timeout_ms=ack_timeout_ms,
            pace_timeout_ms=pace_timeout_ms, cushion_timeout_ms=cushion_timeout_ms,
            accept_timeout_ms=3000)
        cfg = TurnConfig(ack_enabled=ack_enabled, cushion_enabled=cushion_enabled,
                         cushion_gap_ms=cushion_gap_ms, confirm_silence_ms=confirm_silence_ms,
                         cut_wait_ms=cut_wait_ms, think_wait_ms=think_wait_ms, warm_llm=False)
        self.session = TurnSession(cfg, None, gate=self.gate, session_id="cushion-" + seat,
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

    def drive(self, events, *, step_ms=10, tail_ms=1200):
        """按脚本喂 ASR，再按虚拟时钟一路推进（可跨轮：收口后有新字就自动重开一轮）。"""
        index = 0
        end = events[-1][0] + 6000
        while self.now <= end:
            while index < len(events) and events[index][0] <= self.now:
                t_ms, text, final = events[index]
                self.session.on_asr(t_ms, text, final)
                index += 1
            self.session.tick(self.now)
            time.sleep(0.001)
            if self.session.turn_done and index >= len(events) \
                    and self.now >= events[-1][0] + tail_ms:
                break
            self.now += step_ms
        return self.session

    def events_of(self, name):
        return [event for event in self.session.events if event.get("event") == name]

    def cushions(self):
        return self.events_of("cushion_spoken")

    def cushion_gates(self):
        return self.events_of("cushion_gate")

    def paces(self):
        return self.events_of("hub_pace")

    def confirm_times(self):
        return [(event["t_ms"], event["why"]) for event in self.events_of("state_change")
                if event.get("to") == "CONFIRMED"]

    def gate_routes(self):
        return [event.get("route") for event in self.events_of("gate_decision")]

    def turn_done_times(self):
        return [event["t_ms"] for event in self.events_of("turn_done")]

    def close(self):
        self.session.close()
        self.gate.close()


class PaceJudgementContract(unittest.TestCase):
    """节奏这一问 / 垫句这一问的请求形状、固定稿与来源标注。"""

    def test_pace_question_is_a_four_tier_choice(self):
        question = pace_question()
        self.assertEqual(question["type"], "choice")
        self.assertEqual(set(question["criteria"]), set(PACE_ORDER))
        for label in ("平常", "耐心", "讲述", "积极"):
            self.assertIn(label, question["instructions"])
        self.assertEqual([PACE_LABELS[name] for name in PACE_ORDER], ["平常", "耐心", "讲述", "积极"])

    def test_pace_state_carries_speech_ms_and_the_same_shape_as_accept(self):
        self.assertEqual(pace_state("导航"), '{"utterance": "导航", "scene": ""}')
        state = pace_state("导航", {"summary": "主驾在看前面"}, speech_ms=1800)
        self.assertIn('"speech_ms": 1800', state)
        self.assertIn('"scene": "主驾在看前面"', state)

    def test_cushion_question_is_a_two_choice_ask_not_an_answer(self):
        question = cushion_question()
        self.assertEqual(question["type"], "choice")
        self.assertEqual(set(question["criteria"]), {"cushion", "hold"})
        self.assertIn("垫一句", question["instructions"])
        self.assertIn("不回答", question["instructions"], "问的是陪着说，不是回答")

    def test_cushion_words_are_one_short_sentence(self):
        self.assertEqual(len(CUSHION_WORDS), len(set(CUSHION_WORDS)), "固定稿不重复")
        for word in CUSHION_WORDS:
            chars = cushion_chars(word)
            self.assertGreaterEqual(chars, CUSHION_MIN_CHARS, word)
            self.assertLessEqual(chars, CUSHION_MAX_CHARS, word)
        self.assertEqual([cushion_text(i) for i in range(len(CUSHION_WORDS))],
                         list(CUSHION_WORDS))
        self.assertEqual(cushion_text(len(CUSHION_WORDS)), CUSHION_WORDS[0], "按下标轮着说，不随机")

    def test_default_is_real_judge_and_rule_is_labelled_offline(self):
        pace = build_pace_fn().meta
        self.assertIn("8009", pace["source"])
        self.assertEqual(pace["mode"], "kev_choice(pace)")
        self.assertFalse(pace["decided"], "节奏的开场保持 / 平滑换档未拍板，不得写成已拍板")
        self.assertIn("只用于", pace["scope"])
        cushion = build_cushion_fn().meta
        self.assertIn("8009", cushion["source"])
        self.assertFalse(cushion["decided"], "垫句起判线未拍，不得写成已拍板")
        self.assertIn("未拍", cushion["threshold_note"])
        self.assertEqual(cushion["min_speech_ms"], CUSHION_MIN_SPEECH_MS)
        rule = build_cushion_fn(mode="rule")
        self.assertEqual(rule.meta["mode"], "rule")
        self.assertIn("离线", rule.meta["source"])
        self.assertTrue(rule("嗯，那个", None, "D", speech_ms=2000)["say"],
                        "离线替身一律判「垫一句」，出不出由中枢两道闸决定")
        with self.assertRaises(ValueError):
            build_pace_fn(mode="jev-omni")
        with self.assertRaises(ValueError):
            build_cushion_fn(mode="jev-omni")


class PaceHoldPolicy(unittest.TestCase):
    """拍板 2：「节奏开头 1–2 轮先稳住，之后平滑换档，不因为一版字硬切」。"""

    def test_first_beat_starts_from_normal(self):
        pace, held, why = hold_pace(PACE_EAGER, {PACE_EAGER: 0.9}, None, 3)
        self.assertEqual(pace, PACE_NORMAL)
        self.assertTrue(held)
        self.assertIn("开场", why)

    def test_opening_two_turns_are_held(self):
        for turn in (1, 2):
            pace, held, why = hold_pace(PACE_EAGER, {PACE_EAGER: 0.95, PACE_NORMAL: 0.02},
                                        PACE_NORMAL, turn)
            self.assertEqual(pace, PACE_NORMAL, "第 %d 轮先稳住" % turn)
            self.assertTrue(held)

    def test_switch_only_when_the_new_tier_is_clearly_ahead(self):
        pace, held, why = hold_pace(PACE_EAGER, {PACE_EAGER: 0.82, PACE_NORMAL: 0.06},
                                    PACE_NORMAL, 3)
        self.assertEqual(pace, PACE_EAGER, why)
        self.assertFalse(held)
        pace, held, why = hold_pace(PACE_EAGER, {PACE_EAGER: 0.40, PACE_NORMAL: 0.34},
                                    PACE_NORMAL, 3)
        self.assertEqual(pace, PACE_NORMAL, "只领先 0.06：不硬切")
        self.assertTrue(held)
        self.assertIn("平滑换档", why)

    def test_same_tier_never_switches(self):
        pace, held, why = hold_pace(PACE_EAGER, {PACE_EAGER: 0.9}, PACE_EAGER, 5)
        self.assertEqual(pace, PACE_EAGER)
        self.assertFalse(held)
        self.assertIn("没变", why)


class CushionPolicy(unittest.TestCase):
    """垫句的唯一判据（纯函数，逐条对照拍板）。"""

    def ok_pace(self, tier=PACE_EAGER):
        return pace_outcome(tier)

    def ok_cushion(self, **kwargs):
        base = cushion_outcome(True)
        base.update(kwargs)
        return base

    def decide(self, **kwargs):
        base = {"pace": self.ok_pace(), "cushion": self.ok_cushion(),
                "eot": {"label": "cut", "may_confirm": False, "may_start_early": False},
                "accept": accept_outcome(True), "speech_ms": 2000,
                "last_spoken_ms": None, "now_ms": 1000, "text": cushion_text(0)}
        base.update(kwargs)
        return decide_cushion(**base)

    def test_eager_and_past_one_and_a_half_seconds_speaks(self):
        say, reason = self.decide()
        self.assertTrue(say, reason)

    def test_other_three_tiers_never_even_ask(self):
        for tier in (PACE_NORMAL, PACE_PATIENT, PACE_NARRATIVE):
            say, reason = self.decide(pace=self.ok_pace(tier))
            self.assertFalse(say, reason)
            self.assertIn("不是积极", reason)
            self.assertIn(PACE_LABELS[tier], reason)

    def test_eager_before_one_and_a_half_seconds_does_not_speak(self):
        say, reason = self.decide(speech_ms=CUSHION_MIN_SPEECH_MS - 1)
        self.assertFalse(say, reason)
        self.assertIn("说满", reason)
        say, reason = self.decide(speech_ms=CUSHION_MIN_SPEECH_MS)
        self.assertTrue(say, "刚好说满 1.5 秒就算满：" + reason)

    def test_pace_unavailable_never_speaks(self):
        for pace in (None, {}, {"state": "pending"}, {"state": "timeout", "pace": None},
                     {"state": "failed", "pace": None}):
            say, reason = self.decide(pace=pace)
            self.assertFalse(say, reason)
            self.assertIn("节奏", reason)

    def test_cushion_unavailable_never_speaks(self):
        for cushion in (None, {}, {"state": "pending"}, {"state": "timeout"},
                        {"state": "failed", "say": False}, self.ok_cushion(say=False)):
            say, reason = self.decide(cushion=cushion)
            self.assertFalse(say, reason)
            self.assertIn("垫句", reason)

    def test_not_asked_never_speaks(self):
        say, reason = self.decide(cushion={"state": "not_asked", "say": False,
                                           "reason": "节奏不是积极（平常）：不问垫句"})
        self.assertFalse(say)
        self.assertIn("不是积极", reason)

    def test_done_beat_never_speaks(self):
        for eot in (None, {}, {"label": "error", "may_confirm": False, "error": {"kind": "X"}},
                    {"label": "complete", "may_confirm": True, "may_start_early": True}):
            say, reason = self.decide(eot=eot)
            self.assertFalse(say, reason)

    def test_not_for_car_never_cushions(self):
        say, reason = self.decide(accept=accept_outcome(False))
        self.assertFalse(say)
        self.assertIn("不要回答", reason)
        for accept in (None, {}, {"state": "pending"}, {"state": "timeout", "accept": None},
                       {"state": "failed", "accept": None}):
            say, reason = self.decide(accept=accept)
            self.assertFalse(say, reason)
            self.assertIn("要不要回答", reason)

    def test_one_second_hold_after_speaking(self):
        say, reason = self.decide(last_spoken_ms=0, now_ms=900, gap_ms=300)
        self.assertFalse(say, "出声后 1 秒内不再出声（哪怕把间隔闸调小）")
        self.assertIn("不到 1.0 秒", reason)
        say, _ = self.decide(last_spoken_ms=0, now_ms=1200, gap_ms=300)
        self.assertTrue(say, "过了 1 秒就能再垫一句")

    def test_wording_must_be_one_short_sentence(self):
        say, reason = self.decide(text="嗯")
        self.assertFalse(say)
        self.assertIn("不在", reason)
        say, _ = self.decide(text="我跟你说啊这条路最近在修所以得绕一下再走别的路线")
        self.assertFalse(say, "长到像回答就不行")


class CushionInTurnSession(unittest.TestCase):
    """主链：四档各跑一段、1.5 秒那条闸、垫句优先于应声、收口与开闸有没有被带偏。"""

    # 三段稿：前两轮是「开场稳住」的轮次（节奏不换档），第三轮才看这一段的节奏。
    SCRIPT = {
        "导航回家": STRONG_COMPLETE,
        "把空调调到24度": STRONG_COMPLETE,
        "我跟你说前面那个路口": CUT,
        "我跟你说前面那个路口最近": CUT,
        "我跟你说前面那个路口最近在": CUT,
        "我跟你说前面那个路口最近在修": CUT,
        "我跟你说前面那个路口最近在修路": CUT,
    }
    # 第三轮：每版隔 700ms（小于 1.5 秒的静音窗，这一轮不会被提前收口），
    # 连续说满的时长分别是 0 / 700 / 1400 / 2100 / 2800ms ——从第 4 版（2100ms）起够 1.5 秒。
    EVENTS = [(0, "导航回家", True), (5000, "把空调调到24度", True),
              (10000, "我跟你说前面那个路口", False),
              (10700, "我跟你说前面那个路口最近", False),
              (11400, "我跟你说前面那个路口最近在", False),
              (12100, "我跟你说前面那个路口最近在修", False),
              (12800, "我跟你说前面那个路口最近在修路", False)]

    # 积极但没满 1.5 秒：第三轮三版分别是 0 / 600 / 1200ms
    SCRIPT_SHORT = {
        "导航回家": STRONG_COMPLETE,
        "把空调调到24度": STRONG_COMPLETE,
        "前面有点堵": CUT,
        "前面有点堵啊": CUT,
        "前面有点堵啊那": CUT,
    }
    EVENTS_SHORT = [(0, "导航回家", True), (5000, "把空调调到24度", True),
                    (10000, "前面有点堵", False), (10600, "前面有点堵啊", False),
                    (11200, "前面有点堵啊那", False)]

    def run_bench(self, script, events, **kwargs):
        bench = Bench(script, **kwargs)
        bench.drive(events)
        self.addCleanup(bench.close)
        return bench

    # ---- ① 四档各跑一段：只有积极会出现垫句（其余三档根本不问） ----
    def test_only_eager_asks_for_a_cushion(self):
        observed = {}
        for tier in (PACE_NORMAL, PACE_PATIENT, PACE_NARRATIVE, PACE_EAGER):
            bench = self.run_bench(self.SCRIPT, self.EVENTS, pace=tier)
            observed[tier] = {"cushion_calls": bench.gate.counters["cushion_calls"],
                              "spoken": len(bench.cushions()),
                              "pace": (bench.session.pace_last or {}).get("pace")}
        self.assertEqual(observed[PACE_NORMAL]["cushion_calls"], 0)
        self.assertEqual(observed[PACE_PATIENT]["cushion_calls"], 0)
        self.assertEqual(observed[PACE_NARRATIVE]["cushion_calls"], 0)
        self.assertEqual(observed[PACE_NORMAL]["spoken"], 0)
        self.assertEqual(observed[PACE_PATIENT]["spoken"], 0)
        self.assertEqual(observed[PACE_NARRATIVE]["spoken"], 0)
        self.assertGreaterEqual(observed[PACE_EAGER]["cushion_calls"], 1,
                                "积极 + 已说满 1.5 秒：会问垫句")
        self.assertGreaterEqual(observed[PACE_EAGER]["spoken"], 1, "问了并且垫了一句")
        self.assertEqual(observed[PACE_EAGER]["pace"], PACE_EAGER)
        self.assertEqual(observed[PACE_PATIENT]["pace"], PACE_PATIENT)
        self.assertEqual(observed[PACE_NARRATIVE]["pace"], PACE_NARRATIVE)

    def test_opening_turns_are_held_even_when_the_judge_says_eager(self):
        bench = self.run_bench(self.SCRIPT, self.EVENTS[:2], pace=PACE_EAGER)
        held = [event for event in bench.paces() if event.get("held")]
        self.assertTrue(held, "开场一两轮先稳住：即使判「积极」也不换档")
        self.assertIn("开场", held[0].get("hold_reason") or "")
        self.assertEqual(bench.cushions(), [], "开场稳住的轮次里不垫句")

    # ---- ② 积极但没满 1.5 秒：不问、不垫 ----
    def test_eager_but_under_one_and_a_half_seconds_never_asks(self):
        bench = self.run_bench(self.SCRIPT_SHORT, self.EVENTS_SHORT, pace=PACE_EAGER)
        self.assertEqual((bench.session.pace_last or {}).get("pace"), PACE_EAGER,
                         "第三轮确实换到积极")
        self.assertEqual(bench.gate.counters["cushion_calls"], 0, "没满 1.5 秒：垫句这一问根本不发")
        self.assertEqual(bench.cushions(), [])
        reasons = [event.get("reason") for event in bench.cushion_gates()]
        self.assertTrue(any("1.5" in (reason or "") or "说满" in (reason or "")
                            for reason in reasons), reasons)

    # ---- ③ 垫句记住（进上下文）但没进 Agent（不选工具、不执行、不开闸） ----
    def test_cushion_is_context_only_never_agent(self):
        with_cushion = self.run_bench(self.SCRIPT, self.EVENTS, pace=PACE_EAGER)
        without = self.run_bench(self.SCRIPT, self.EVENTS, pace=PACE_EAGER,
                                 cushion_enabled=False)
        spoken = with_cushion.cushions()
        self.assertGreaterEqual(len(spoken), 1)
        event = spoken[0]
        self.assertTrue(event.get("context"), "垫句记住：进上下文")
        self.assertFalse(event.get("agent"), "垫句不进 Agent")
        self.assertIn("不进 Agent", event.get("note") or "")
        self.assertEqual([entry["text"] for entry in with_cushion.session.hub_said],
                         [item["text"] for item in spoken], "「记住」落在本席上下文台账（跨轮留存）")
        self.assertEqual(without.session.hub_said, [], "关掉垫句时上下文里没有这一句")
        # 不选工具、不执行：同一段脚本「垫句开 vs 关」的决策 / 工具条数完全一样
        self.assertEqual(len(with_cushion.events_of("llm_decision")),
                         len(without.events_of("llm_decision")))
        self.assertEqual(len(with_cushion.events_of("tools_scores")),
                         len(without.events_of("tools_scores")))
        self.assertEqual(with_cushion.session.decision, without.session.decision)
        # 垫句那一拍没有开闸（同一时刻闸门没有因为垫句而开）
        same_tick_opens = [item for item in with_cushion.events_of("gate_open")
                           if item.get("t_ms") == event.get("t_ms")]
        self.assertEqual(same_tick_opens, [], "垫句那一拍不开闸")

    # ---- ④ 收住时刻与开闸结论不被带偏（A/B 逐毫秒） ----
    def test_end_of_turn_and_gate_are_bit_identical_with_or_without_cushion(self):
        with_cushion = self.run_bench(self.SCRIPT, self.EVENTS, pace=PACE_EAGER)
        without = self.run_bench(self.SCRIPT, self.EVENTS, pace=PACE_EAGER,
                                 cushion_enabled=False)
        self.assertEqual(with_cushion.confirm_times(), without.confirm_times(),
                         "垫句不延长也不缩短收住时刻：CONFIRMED 时刻逐毫秒一致")
        self.assertEqual(with_cushion.turn_done_times(), without.turn_done_times())
        self.assertEqual(with_cushion.gate_routes(), without.gate_routes(),
                         "开闸结论与关掉垫句时一致")
        self.assertEqual(with_cushion.session.counters["eot_calls"],
                         without.session.counters["eot_calls"], "判停调用数一致")
        self.assertGreaterEqual(len(with_cushion.cushions()), 1, "这一跑确实垫了，才谈得上对照")

    def test_all_three_waiting_tiers_still_close_on_the_same_millisecond(self):
        # 0.7s（弱 complete）/ 1.5s（话断了）/ 3s（还在想）三档，各自带垫句跑一遍
        cases = {
            "weak": ({'把空调调到': WEAK_COMPLETE, '把空调调到24度': WEAK_COMPLETE},
                     [(0, '把空调调到', False), (300, '把空调调到24度', True)], 1000),
            "cut": ({'我跟你说前面那个路口': CUT},
                    [(0, '我跟你说前面那个路口', False)], 1500),
            "think": ({'嗯那个': WAIT}, [(0, '嗯那个', False)], 3000),
        }
        for name, (script, events, expect_ms) in cases.items():
            on = self.run_bench(script, events, pace=PACE_EAGER, cushion_enabled=True)
            off = self.run_bench(script, events, pace=PACE_EAGER, cushion_enabled=False)
            self.assertEqual(on.confirm_times(), off.confirm_times(), name)
            self.assertEqual(on.confirm_times()[0][0], expect_ms, name)
            self.assertEqual(on.gate_routes(), off.gate_routes(), name)

    # ---- ⑤ 垫句优先于应声：同一时刻最多一句，出声后 1 秒内不再出声 ----
    def test_cushion_wins_over_ack_and_nothing_speaks_twice(self):
        bench = self.run_bench(self.SCRIPT, self.EVENTS, pace=PACE_EAGER)
        spoken = bench.cushions()
        self.assertGreaterEqual(len(spoken), 1)
        for event in spoken:
            same_tick_acks = [item for item in bench.events_of("ack_spoken")
                              if item.get("t_ms") == event.get("t_ms")]
            self.assertEqual(same_tick_acks, [], "同一时刻最多一句：垫了就不再应声")
        blocked = [item for item in bench.cushion_gates()
                   if "垫句优先" in (item.get("reason") or "")]
        ack_blocked = [item for item in bench.events_of("ack_gate")
                       if "垫句优先" in (item.get("reason") or "")]
        self.assertTrue(blocked or ack_blocked, "台面能看见「垫句优先」这条")
        # 出声后 1 秒内不再出声：两次出声（应声 + 垫句）之间至少 1 秒
        all_voices = sorted([(item["t_ms"], "cushion") for item in bench.cushions()]
                            + [(item["t_ms"], "ack") for item in bench.events_of("ack_spoken")])
        for (earlier, _), (later, _) in zip(all_voices, all_voices[1:]):
            self.assertGreaterEqual(later - earlier, 1000,
                                    "出声后 1 秒内不再出声：%s" % all_voices)

    # ---- ⑥ 判断没回来 / 失败 / 超时：不垫、节奏保持上一拍、收口与开闸不变 ----
    def test_pending_cushion_never_speaks(self):
        release = threading.Event()
        self.addCleanup(release.set)
        bench = self.run_bench(
            self.SCRIPT, self.EVENTS[:3], pace=PACE_EAGER,
            cushion_fn=lambda text, scene, seat, speech_ms=None: (
                release.wait(1.0) or cushion_outcome(True)))
        bench.pump(500)
        self.assertEqual(bench.cushions(), [], "垫句判断还没回来：这一版不垫")

    def test_failed_cushion_never_speaks(self):
        def boom(text, scene, seat, speech_ms=None):
            raise RuntimeError("cushion service down")

        bench = self.run_bench(self.SCRIPT, self.EVENTS, pace=PACE_EAGER, cushion_fn=boom)
        self.assertEqual(bench.cushions(), [])
        reasons = [event.get("reason") for event in bench.cushion_gates()]
        self.assertTrue(any("失败" in (reason or "") for reason in reasons), reasons)
        self.assertEqual(bench.gate.counters["cushion_errors"],
                         bench.gate.counters["cushion_calls"])

    def test_timeout_cushion_never_speaks(self):
        release = threading.Event()
        self.addCleanup(release.set)
        bench = self.run_bench(
            self.SCRIPT, self.EVENTS, pace=PACE_EAGER, cushion_timeout_ms=5,
            cushion_fn=lambda text, scene, seat, speech_ms=None: (
                release.wait(1.0) or cushion_outcome(True)))
        bench.pump(3000)
        self.assertEqual(bench.cushions(), [], "垫句判断超时：这一版不垫")
        self.assertEqual(bench.gate.counters["cushion_errors"],
                         bench.gate.counters["cushion_calls"])

    def test_failed_pace_keeps_the_previous_beat_and_still_closes_identically(self):
        def boom(text, scene, seat, speech_ms=None):
            raise RuntimeError("pace service down")

        failed = self.run_bench(self.SCRIPT, self.EVENTS, pace_fn=boom)
        control = self.run_bench(self.SCRIPT, self.EVENTS, cushion_enabled=False)
        self.assertIsNone(failed.session.pace_last, "节奏判断失败：保持上一拍（这里还没有上一拍）")
        self.assertEqual(failed.cushions(), [])
        self.assertEqual(failed.gate.counters["cushion_calls"], 0)
        self.assertEqual(failed.confirm_times(), control.confirm_times(), "收口不被带偏")
        self.assertEqual(failed.gate_routes(), control.gate_routes(), "开闸不被带偏")
        self.assertEqual(failed.gate.counters["pace_errors"], failed.gate.counters["texts"])

    def test_pending_pace_never_asks_for_a_cushion(self):
        release = threading.Event()
        self.addCleanup(release.set)
        bench = self.run_bench(
            self.SCRIPT, self.EVENTS[:3], pace_fn=lambda text, scene, seat, speech_ms=None: (
                release.wait(1.0) or pace_outcome(PACE_EAGER)))
        bench.pump(400)
        self.assertEqual(bench.cushions(), [], "节奏还没回来：不垫（也不换档）")
        self.assertEqual(bench.gate.counters["cushion_calls"], 0)

    def test_timeout_pace_never_asks_for_a_cushion(self):
        release = threading.Event()
        self.addCleanup(release.set)
        bench = self.run_bench(
            self.SCRIPT, self.EVENTS, pace_timeout_ms=5,
            pace_fn=lambda text, scene, seat, speech_ms=None: (
                release.wait(1.0) or pace_outcome(PACE_EAGER)))
        bench.pump(3000)
        self.assertEqual(bench.cushions(), [], "节奏判断超时：不垫、不换档")
        self.assertEqual(bench.gate.counters["pace_errors"], bench.gate.counters["texts"])
        self.assertIsNone(bench.session.pace_last)

    # ---- ⑦ 调用次数：每版 4 路；积极 + 满 1.5 秒的那一版再多 1 次 ----
    def test_call_count_per_version(self):
        bench = self.run_bench(self.SCRIPT, self.EVENTS, pace=PACE_EAGER)
        counters = bench.gate.counters
        versions = {event["text_version"] for event in bench.events_of("hub_text")}
        self.assertEqual(counters["texts"], len(versions), "每版字一条记录")
        self.assertEqual(counters["accept_calls"], counters["texts"])
        self.assertEqual(counters["ack_calls"], counters["texts"])
        self.assertEqual(counters["pace_calls"], counters["texts"], "节奏每版都判")
        # 第 4 版（已说 2100ms）问了并当场垫了一句；第 5 版（2800ms）也够 1.5 秒、又问了一次，
        # 但离上一声不到 1 秒（拍板 5）→ 没出声。所以「问了几次」≥「垫了几句」，且都少于总版数。
        self.assertEqual(counters["cushion_calls"], 2,
                         "只有积极 + 已说满 1.5 秒的那两版才问垫句")
        self.assertLessEqual(len(bench.cushions()), counters["cushion_calls"])
        self.assertGreaterEqual(len(bench.cushions()), 1, "够条件就真的垫了一句")
        self.assertLess(counters["cushion_calls"], counters["texts"],
                        "不是每版都问垫句：只有积极 + 满 1.5 秒那几版")
        # 期二在「每版都判」的那几路上各加一条：节奏每版 +1；垫句只在合格的那几版 +1。
        self.assertEqual(counters["pace_calls"], counters["texts"], "节奏每版都判")
        self.assertEqual(counters["cushion_calls"], 2,
                         "垫句是节奏的下一条：只在「积极 + 已说满 1.5 秒」那几版才发")
        # 判停那条自己的 debounce / 在途规则期二没碰：它的调用数开 / 关垫句时必须一样。
        off = self.run_bench(self.SCRIPT, self.EVENTS, pace=PACE_EAGER, cushion_enabled=False)
        self.assertEqual(bench.session.counters["eot_calls"],
                         off.session.counters["eot_calls"], "判停调用数没被期二改动")


class BenchContract(unittest.TestCase):
    """台面能看见节奏与垫句（双席面板 + 事件行）。"""

    def test_index_html_shows_pace_and_cushion_rows(self):
        page = (ROOT / "ui/index.html").read_text(encoding="utf-8")
        self.assertIn("data-hub-pace", page)
        self.assertIn("data-hub-cushion", page)
        self.assertIn("cushion_spoken", page)
        self.assertIn("cushion_gate", page)
        self.assertIn("hub_pace", page)
        self.assertIn("hub_cushion", page)
        self.assertIn("节奏", page, "台面能看见这一拍是什么节奏")
        self.assertIn("垫一句", page, "台面能看见垫没垫、垫了几句")

    def test_dual_stream_assembly_wires_the_pace_and_cushion_routes(self):
        from app.assembly import hub_gates

        class Mind:
            def scene_snapshot(self):
                return None

        gates = hub_gates(Mind())
        self.assertIn("D", gates, gates)
        self.assertIsNotNone(gates["D"].pace_fn, "双席链路挂了节奏这一路")
        self.assertIsNotNone(gates["D"].cushion_fn, "双席链路挂了垫句这一路")
        self.assertIn("8009", gates["D"]._pace_source()["source"])
        self.assertEqual(gates["D"]._cushion_source()["min_speech_ms"], CUSHION_MIN_SPEECH_MS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
