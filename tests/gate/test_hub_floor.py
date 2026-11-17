"""期三 · 两人同时说四选离线验收（不连 8009 / 不连 LLM / 不连 TTS）。

钉住 2026-11-16 已拍板的口径（验收卡 `poc-hub-jev-absorb-p3-20261116-验收.md`）：

1. **四条出口各一条能复现**：同一段稿、同一句插话，只换四选结论 → 四条出口各自跑得出来。
2. **让路与改动前逐字段一致**（本期最重要的回归）：`floor_enabled=False`（改动前老路）与
   `floor_enabled=True + 判「让路」`两次回放的事件流**逐字段相同**（只多出四选自己那几条事件）；
   作废本轮 / 话留下 / 回到收听与 `turn_void` 的字段不变。
3. **把这句说完再接**：当前这句照旧说完（不作废），插话**不丢、顺序不乱**，这句说完之后
   插话作为下一轮**真的被回答**（有判停 / 接不接 / 开闸 / 决策）。
4. **继续说**：当前这句说完并继续；插话只进旁听上下文（「不是对车说」同一口径），
   **不进 Agent**（不选工具、不执行、不为插话开闸）。
5. **垫句抢回**：当前这句说完，垫句紧随其后（记住、不进 Agent、不开闸），插话收下、随后被回答。
6. **判断没回来 / 失败 / 超时** → 按「继续说」走；收口（0.7 / 1.5 / 3 秒）与开闸结论不变。
7. **调用次数**：每版字仍是 4 路（判停 + 要不要回答 + 应不应一声 + 节奏）；只在「这边正在说、
   人又开口」的那一回多 1 次四选（同一段插话的后续 partial 不再重复问）。

运行：python3 -B tests/gate/test_hub_floor.py
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from gate.hub_floor import (FLOOR_CONTINUE, FLOOR_CUSHION_BACK, FLOOR_FINISH_FIRST,  # noqa: E402
                            FLOOR_LABELS, FLOOR_ORDER, FLOOR_SAFE, FLOOR_YIELD, build_floor_fn,
                            decide_floor, floor_meta, floor_question, floor_state)
from gate.hub_gate import GateKeeper  # noqa: E402
from gate.turn_session import TurnConfig, TurnSession  # noqa: E402
from overhear_ledger import LINE_LABEL, OverhearLedger  # noqa: E402
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


def pace_outcome(tier="normal", *, source="测试节奏判断", model="fake/pace"):
    return {"pace": tier, "raw_pace": tier, "label": {"normal": "平常"}.get(tier, tier),
            "probabilities": {"normal": 0.82, "patient": 0.06, "narrative": 0.06, "eager": 0.06},
            "score": 0.82, "confidence": 0.9, "state": "ok", "source": source, "model": model,
            "reason": "测试用节奏结论"}


def floor_outcome(outlet, *, source="测试四选", model="fake/floor"):
    probabilities = {name: (0.82 if name == outlet else round(0.18 / 3, 3)) for name in FLOOR_ORDER}
    return {"choice": outlet, "outlet": outlet, "label": FLOOR_LABELS[outlet],
            "action": "测试用出口", "probabilities": probabilities,
            "score": probabilities[outlet], "confidence": 0.9, "state": "ok",
            "source": source, "model": model, "reason": "测试用四选结论"}


def tools_fn(text, tool_set=None, context=None):
    name = "car_navi_to_destination"
    return {"scores": {"tool:" + name: 0.9}, "selected": [name],
            "schemas": [{"name": name, "description": "d", "parameters": {"type": "object"}}],
            "native_schemas": None, "domains": None,
            "timing": {"stage1_ms": 1.0, "stage2_ms": 0.0, "total_ms": 1.0}}


class Bench:
    """一段固定稿 + 一个虚拟时钟 + 一道「这边正在说」的闸：同一段稿只换四选结论。

    `decide_fn` 卡在一道闸上（`release` / `hold`），这样「开闸之后正在准备说 / 正在回答」
    这一段可以停在原地，人又开口的插话才落得进这个窗口（真机里这一段就是决策还在生成）。
    """

    def __init__(self, script, *, floor=FLOOR_YIELD, floor_enabled=True, floor_fn=None,
                 floor_timeout_ms=None, floor_rule_absent=False, accept=True, seat="D",
                 ledger=True, cushion_gap_ms=1000):
        self.script = {normalize(key): value for key, value in script.items()}
        self.accept = accept
        self.decide_gate = threading.Event()
        self.floor_calls = 0
        self.floor_asked_n = 0           # 问第几回（`floor` 传一串时按回取：各回各判各的）
        self.floor_fn = floor_fn
        # `floor` 可以是单条，也可以是一串（按问的顺序取：第一回、第二回……各判各的）。
        self.floor_plan = list(floor) if isinstance(floor, (list, tuple)) else [floor]
        if floor_fn is None and not floor_rule_absent:
            def _floor_fn(text, scene=None, seat="D", hub_state=None, hub_sentence=None):
                index = min(len(self.floor_plan) - 1, self.floor_asked_n)
                self.floor_asked_n += 1
                return floor_outcome(self.floor_plan[index])
            self.floor_fn = _floor_fn
        self.ledger = OverhearLedger() if ledger else None
        self.gate = GateKeeper(
            accept_fn=lambda text, scene, seat: accept_outcome(self.accept),
            ack_fn=lambda text, scene, seat: ack_outcome(True),
            pace_fn=lambda text, scene, seat, speech_ms=None: pace_outcome("normal"),
            floor_fn=self.floor_fn,
            floor_timeout_ms=floor_timeout_ms,
            scene_fn=lambda: None, seat=seat, accept_timeout_ms=3000)
        cfg = TurnConfig(floor_enabled=floor_enabled, cushion_enabled=False,
                         cushion_gap_ms=cushion_gap_ms, confirm_silence_ms=700,
                         cut_wait_ms=1500, think_wait_ms=3000, warm_llm=False)
        self.session = TurnSession(cfg, None, gate=self.gate, session_id="floor-" + seat,
                                   evidence_root=None, eot_fn=self._eot,
                                   select_tools_fn=tools_fn, decide_fn=self._decide,
                                   seat_id=seat, overhear=self.ledger)
        self.now = 0

    def _eot(self, text):
        key = normalize(text)
        if key not in self.script:
            raise AssertionError("脚本里没有这一版字：" + text)
        return dict(self.script[key])

    def _decide(self, utterance, selected, schemas, tool_set, context, **kwargs):
        """决策卡在闸上：直到脚本放行，这一句都算「正在准备说 / 正在回答」。"""
        gate = self.decide_gate
        if gate.wait(timeout=5.0) is False:
            raise AssertionError("决策没人放行：" + utterance)
        return {"decision": {"kind": "tool_call",
                             "tool": (selected or ["car_navi_to_destination"])[0],
                             "arguments": {}},
                "llm_ms": 1.0, "log_fields": {"decision_head": "floor-test"}}

    def drive(self, commands, *, step_ms=10, tail_ms=1500):
        """按脚本推进：`("asr", t, text, final)` / `("vad", t, kind)` / `("release", t)` /
        `("hold", t)` / `("end", t)`。"""
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

    def confirm_times(self):
        return [(event["t_ms"], event["why"]) for event in self.events_of("state_change")
                if event.get("to") == "CONFIRMED"]

    def gate_routes(self):
        return [event.get("route") for event in self.events_of("gate_decision")]

    def turn_done_times(self):
        return [event["t_ms"] for event in self.events_of("turn_done")]

    def decisions(self):
        return [(event["t_ms"], event.get("utterance")) for event in self.events_of("llm_decision")]

    def index_of_text(self, text):
        """第一个「出字就是这句」的 hub_text 在事件流里的位置（'先排队、再出这一版'的顺序断言用）。"""
        for index, event in enumerate(self.session.events):
            if event.get("event") == "hub_text" and normalize(event.get("text")) == normalize(text):
                return index
        raise AssertionError("事件流里没有这一版出字：" + text)

    def close(self):
        self.session.close()
        self.gate.close()


# ---- 同一段稿：第一轮「导航回家」开闸之后正在准备说；t=300 人又开口插一句 ----
FIRST = "导航回家"
INSERTION = "等一下先别导航"
SCRIPT = {
    FIRST: STRONG_COMPLETE,
    INSERTION: STRONG_COMPLETE,
}
# 决策一直卡到 900ms：300ms 那一句插话稳稳落在「正在准备说 / 正在回答」的窗口里。
COMMANDS = [("asr", 0, FIRST, True), ("hold", 0), ("asr", 300, INSERTION, True),
            ("release", 900), ("hold", 1200), ("release", 1600), ("hold", 2000),
            ("release", 2400), ("end", 3600)]


def run(commands=None, script=None, **kwargs):
    bench = Bench(script or SCRIPT, **kwargs)
    try:
        bench.drive(commands or COMMANDS)
        return bench
    except Exception:
        bench.close()
        raise


def strip_floor(events):
    """去掉四选自己那几条事件与新增的 floor_* 字段：剩下的就是「改动前就存在」的那一份。

    新增字段（`floor_enabled` / `floor_asks_total` / `floor_queue` / `floor_log` …，含中枢快照里的
    `floor` 那一块）是本期**加**的观测字段，不是改动；让路 A/B 要比的是「改动前就有的那些字段
    逐字段一致」。
    """
    out = []
    for event in events:
        if event.get("event") in FLOOR_EVENTS:
            continue
        row = {key: value for key, value in event.items()
               if key != "event" and key != "hub_policy" and not str(key).startswith("floor")}
        cerebrum = row.get("cerebrum")
        if isinstance(cerebrum, dict):
            cerebrum = {key: value for key, value in cerebrum.items() if key != "floor"}
            if isinstance(cerebrum.get("counters"), dict):
                cerebrum["counters"] = {key: value for key, value in cerebrum["counters"].items()
                                        if not str(key).startswith("floor")}
            row["cerebrum"] = cerebrum
        out.append(row)
    return out


FLOOR_EVENTS = {"floor_asked", "floor_insertion", "floor_vad", "hub_floor", "floor_outcome",
                "floor_queued", "floor_released", "floor_context", "floor_gate", "floor_hold"}


# 三条并发结论（接不接 / 应一声 / 节奏）在同一个瞬间各自回来，先后本来就不定（改动前就是这样）。
# 让路 A/B 只按「同一瞬间的这三条」按名字排序，其余事件仍按原顺序逐条比。
GATE_NOISE = {"hub_accept", "hub_ack", "hub_pace"}


def canon_gate_noise(events):
    """同一个瞬间的三条并发结论按名字排好（其余事件一个位置都不动）。"""
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


class FloorContract(unittest.TestCase):
    """四选这一问的请求形状、出口表、兜底与来源标注（纯函数）。"""

    def test_question_is_a_four_way_choice_with_the_pinned_outlets(self):
        question = floor_question()
        self.assertEqual(question["type"], "choice")
        self.assertEqual(set(question["criteria"]), set(FLOOR_ORDER))
        for label in ("让路", "把这句说完再接", "继续说", "垫句抢回"):
            self.assertIn(label, question["instructions"], "四条出口都要写进问法：" + label)
        self.assertEqual([FLOOR_LABELS[name] for name in FLOOR_ORDER],
                         ["让路", "把这句说完再接", "继续说", "垫句抢回"])

    def test_state_carries_the_insertion_and_what_the_hub_is_saying(self):
        self.assertEqual(floor_state("等一下"), '{"insertion": "等一下", "scene": ""}')
        state = floor_state("等一下", {"summary": "主驾在看路口"}, hub_state="preparing",
                            hub_sentence="导航回家")
        self.assertIn('"hub_state": "preparing"', state)
        self.assertIn('"hub_sentence": "导航回家"', state)
        self.assertIn('"scene": "主驾在看路口"', state)

    def test_every_outlet_is_a_valid_answer_not_a_threshold_question(self):
        for outlet in FLOOR_ORDER:
            verdict = floor_outcome(outlet)
            choice, reason = decide_floor(floor=verdict)
            self.assertEqual(choice, outlet, reason)
            self.assertIn(FLOOR_LABELS[outlet], reason)

    def test_no_conclusion_falls_back_to_continue(self):
        self.assertEqual(FLOOR_SAFE, FLOOR_CONTINUE)
        for verdict in (None, {}, {"state": "pending"}, {"state": "failed"},
                        {"state": "timeout"}, {"state": "ok", "choice": "elsewhere"},
                        {"state": "ok", "outlet": None}):
            choice, reason = decide_floor(floor=verdict)
            self.assertEqual(choice, FLOOR_CONTINUE, str(verdict))
            self.assertIn("继续说", reason)

    def test_default_is_real_judge_and_the_rule_stub_is_labelled(self):
        meta = floor_meta()
        self.assertIn("8009", meta["source"])
        self.assertEqual(meta["mode"], "kev_choice(floor)")
        self.assertTrue(meta["decided"], "四条出口与失败兜底是已拍板的，不是临时值")
        self.assertIn("不设起判线", meta["threshold_note"])
        self.assertEqual(len(meta["outlets"]), 4)
        rule = build_floor_fn("rule")
        self.assertEqual(rule.meta["mode"], "rule")
        self.assertIn("离线", rule.meta["source"])
        verdict = rule("插话", None, "D")
        self.assertEqual(verdict["outlet"], FLOOR_YIELD, "离线替身默认让路＝改动前那条")
        self.assertEqual(verdict["source"], "离线替身（仅供离线演示）", "来源照实写，不冒充真判断")
        self.assertEqual(verdict["mode"], "rule")
        with self.assertRaises(ValueError):
            build_floor_fn("jev-omni")

    def test_assembly_wires_the_floor_route_on_both_seats(self):
        from app.assembly import hub_gates

        class Mind:
            def scene_snapshot(self):
                return None

        gates = hub_gates(Mind())
        self.assertEqual(set(gates), {"D", "P"})
        for seat in ("D", "P"):
            self.assertIsNotNone(gates[seat].floor_fn, "双席链路挂了四选这一路")
            source = gates[seat]._floor_source()
            # 默认是真判断（8009）；显式 HUB_FLOOR=rule 时是离线替身——两种都算挂上了，
            # 但来源必须自报家门（离线替身不许冒充真判断）。
            blob = source["source"] + " " + (source.get("model") or "")
            self.assertTrue("8009" in blob or "离线替身" in blob, blob)
            self.assertEqual({row["key"] for row in source["outlets"]},
                             set(FLOOR_ORDER), "四条出口在台面快照里都带着")


class YieldKeepsTheOldPath(unittest.TestCase):
    """让路＝改动前那条：同一段稿，改动前（floor 关）与判「让路」的事件流逐字段一致。"""

    def test_yield_events_are_identical_to_the_old_path(self):
        old = run(floor_enabled=False)
        new = run(floor=FLOOR_YIELD)
        try:
            self.assertEqual(strip_floor(canon_gate_noise(new.session.events)),
                             strip_floor(canon_gate_noise(old.session.events)),
                             "让路的事件流与改动前必须逐字段一致")
            self.assertEqual(new.confirm_times(), old.confirm_times(), "收住时刻一致")
            self.assertEqual(new.gate_routes(), old.gate_routes(), "开闸结论一致")
            self.assertEqual(new.turn_done_times(), old.turn_done_times(), "回合收口一致")
            self.assertEqual(new.decisions(), old.decisions(), "决策条文一致")
            self.assertEqual(new.session.counters["eot_calls"], old.session.counters["eot_calls"])
        finally:
            old.close()
            new.close()

    def test_yield_is_the_same_one_turn_void_with_the_same_fields(self):
        bench = run(floor=FLOOR_YIELD)
        try:
            voids = bench.events_of("turn_void")
            self.assertEqual(len(voids), 1, [event["event"] for event in bench.session.events])
            void = voids[0]
            self.assertEqual(void["reason"], "new_text")
            self.assertEqual(void["t_ms"], 300, "作废时刻就是人开口那一刻（不是判断回来那一刻）")
            self.assertTrue(void.get("voided_inflight") is True or void.get("voided_prepared") is True,
                            "真的作废了在飞 / 备好的那一句：" + str(void))
            outcome = bench.events_of("floor_outcome")[0]
            self.assertEqual(outcome["outlet"], FLOOR_YIELD)
            self.assertTrue(outcome["void"] and outcome["replay"])
            self.assertFalse(outcome["queued"] or outcome["context_only"])
            # 话留下：插话变成这一轮的新出字（新一版），并真的被回答——而且只有它被回答
            # （当前这一句的决策已经作废，所以不出现「导航回家」那条决策）。
            self.assertIn(INSERTION, bench.session.turn_text())
            self.assertEqual(len(bench.decisions()), 1, bench.decisions())
            self.assertIn(normalize(INSERTION), normalize(bench.decisions()[0][1]))

        finally:
            bench.close()


class FinishFirst(unittest.TestCase):
    """把这句说完再接：当前这句照旧说完（不作废），插话排队、随后作为下一轮真的被回答。"""

    def test_current_sentence_is_not_voided_and_the_insertion_is_answered_next(self):
        bench = run(floor=FLOOR_FINISH_FIRST)
        try:
            self.assertEqual(bench.events_of("turn_void"), [], "不作废本轮")
            self.assertEqual(bench.session.counters["turn_voids"], 0)
            outcome = bench.events_of("floor_outcome")[0]
            self.assertEqual(outcome["outlet"], FLOOR_FINISH_FIRST)
            self.assertTrue(outcome["queued"] and not outcome["void"])
            queued = bench.events_of("floor_queued")[0]
            self.assertEqual(queued["text"], INSERTION)
            self.assertEqual(queued["index"], 1, "顺序：第一段插话排在第 1 位")
            released = bench.events_of("floor_released")[0]
            self.assertEqual(released["text"], INSERTION)
            self.assertEqual(released["index"], 1)
            # 当前这句先落地（决策 1 = 导航回家），插话随后作为下一轮被真的回答（决策 2）
            decisions = bench.decisions()
            self.assertEqual(len(decisions), 2, decisions)
            self.assertIn(normalize(FIRST), normalize(decisions[0][1]))
            self.assertIn(normalize(INSERTION), normalize(decisions[1][1]))
            # 顺序不乱：先这句收口（turn_done），再开下一轮（turn_reopen），再出插话这一版
            order = [event["event"] for event in bench.session.events]
            self.assertLess(order.index("floor_queued"), order.index("turn_done"))
            self.assertLess(order.index("turn_done"), order.index("floor_released"))
            self.assertLess(order.index("floor_released"), bench.index_of_text(INSERTION))
            # 插话这一轮照常走判停 + 接不接 + 开闸（不是「人说了没人应」）
            self.assertEqual(bench.gate_routes(), ["agent", "agent"])
            self.assertEqual(len(bench.confirm_times()), 2)
            self.assertEqual(bench.session.summary()["floor_queue"], [], "放行后队列空")
            self.assertEqual(bench.session.floor_released_total, 1)
        finally:
            bench.close()

    def test_two_insertions_keep_their_order(self):
        script = dict(SCRIPT)
        script["再等一下"] = STRONG_COMPLETE
        commands = [("asr", 0, FIRST, True), ("hold", 0),
                    ("asr", 300, INSERTION, True), ("asr", 340, "再等一下", True),
                    ("release", 900), ("hold", 1200), ("release", 1600), ("hold", 2000),
                    ("release", 2400), ("hold", 2600), ("release", 3000), ("end", 4200)]
        bench = Bench(script, floor=FLOOR_FINISH_FIRST)
        try:
            bench.drive(commands)
            released = [event["text"] for event in bench.events_of("floor_released")]
            self.assertEqual(released, [INSERTION, "再等一下"], "先来后到，顺序不乱")
            decisions = [normalize(text) for _, text in bench.decisions()]
            self.assertEqual(decisions, [normalize(FIRST), normalize(INSERTION), "再等一下"])
        finally:
            bench.close()


class ContinueTalking(unittest.TestCase):
    """继续说：当前这句说完并继续；插话只进旁听上下文（不是对车说），不进 Agent。"""

    def test_insertion_goes_to_the_overhear_ledger_and_never_to_the_agent(self):
        bench = run(floor=FLOOR_CONTINUE)
        try:
            self.assertEqual(bench.events_of("turn_void"), [], "不打断自己")
            outcome = bench.events_of("floor_outcome")[0]
            self.assertEqual(outcome["outlet"], FLOOR_CONTINUE)
            self.assertTrue(outcome["context_only"])
            self.assertFalse(outcome["queued"] or outcome["void"])
            context = bench.events_of("floor_context")[0]
            self.assertEqual(context["text"], INSERTION)
            self.assertEqual(context["label"], LINE_LABEL)
            self.assertTrue(context["context"])
            self.assertFalse(context["agent"])
            recorded = bench.events_of("overhear_recorded")[0]
            self.assertEqual(recorded["text"], INSERTION)
            self.assertEqual(recorded["label"], LINE_LABEL)
            self.assertEqual(bench.ledger.lines("D")[-1]["text"], INSERTION)
            # 插话不进 Agent：没有为它开闸、没有为它选工具 / 决策 / 执行
            self.assertEqual(bench.gate_routes(), ["agent"], "只有当前这句开了闸")
            self.assertEqual(len(bench.decisions()), 1, "没有为插话起第二轮")
            scores = bench.events_of("tools_scores")
            self.assertEqual(len(scores), 1, "只给当前这句选过工具")
            self.assertEqual(normalize(scores[0]["text"]), normalize(FIRST))
            self.assertNotIn(normalize(INSERTION), [normalize(text) for _, text in bench.decisions()])
            self.assertIn(INSERTION, [row["text"] for row in bench.session.floor_context])
        finally:
            bench.close()


class CushionBack(unittest.TestCase):
    """垫句抢回：当前这句说完，垫句紧随其后；插话收下、随后被回答；垫句不进 Agent、不开闸。"""

    def test_cushion_follows_the_current_sentence_and_the_insertion_is_still_answered(self):
        bench = run(floor=FLOOR_CUSHION_BACK)
        try:
            self.assertEqual(bench.events_of("turn_void"), [], "当前这句不中止")
            outcome = bench.events_of("floor_outcome")[0]
            self.assertEqual(outcome["outlet"], FLOOR_CUSHION_BACK)
            self.assertTrue(outcome["cushion"] and outcome["queued"])
            cushions = bench.events_of("cushion_spoken")
            self.assertEqual(len(cushions), 1, "当前这句说完只垫一句")
            cushion = cushions[0]
            self.assertEqual(cushion["kind"], "cushion_back")
            self.assertTrue(cushion["context"])
            self.assertFalse(cushion["agent"], "垫句仍是陪着说：不进 Agent / 不开闸")
            self.assertIn("不接真 TTS", cushion["speech"])
            # 顺序：这句收口 → 垫句紧随其后 → 插话作为下一轮发出 → 被回答
            order = [event["event"] for event in bench.session.events]
            self.assertLess(order.index("turn_done"), order.index("cushion_spoken"))
            self.assertLess(order.index("cushion_spoken"), order.index("floor_released"))
            self.assertLess(order.index("floor_released"), bench.index_of_text(INSERTION))
            self.assertEqual(bench.session.floor_released_total, 1)
            decisions = bench.decisions()
            self.assertEqual(len(decisions), 2, decisions)
            self.assertIn(normalize(INSERTION), normalize(decisions[1][1]))
            # 垫句「记住」：本席上下文台账里有这一句，且决策 / 工具条数只跟两轮有关
            self.assertIn(cushion["text"], [row["text"] for row in bench.session.hub_said])
            self.assertEqual([row["kind"] for row in bench.session.hub_said], ["cushion_back"])
            self.assertEqual(bench.gate_routes(), ["agent", "agent"])
        finally:
            bench.close()

    def test_cushion_is_suppressed_within_one_second_but_the_insertion_is_never_lost(self):
        script = dict(SCRIPT)
        script[INSERTION] = STRONG_COMPLETE
        script["再问一句"] = STRONG_COMPLETE
        commands = [("asr", 0, FIRST, True), ("hold", 0),
                    ("asr", 300, INSERTION, True), ("release", 900), ("hold", 950),
                    ("asr", 1250, "再问一句", True), ("release", 1600), ("hold", 1700),
                    ("release", 2400), ("hold", 2600), ("release", 3000), ("end", 4200)]
        bench = Bench(script, floor=FLOOR_CUSHION_BACK)
        try:
            bench.drive(commands)
            cushions = bench.events_of("cushion_spoken")
            self.assertEqual([row["kind"] for row in cushions], ["cushion_back"],
                             "第一段垫了，第二段离上一声不到一秒：这次不垫")
            gates = bench.events_of("floor_gate")
            self.assertEqual(len(gates), 1, gates)
            self.assertIn("不到", gates[0]["reason"])
            self.assertFalse(gates[0]["spoken"])
            self.assertEqual(bench.session.floor_released_total, 2,
                             "没垫不等于丢话：两段插话都排上、都被回答")
            self.assertEqual(len(bench.decisions()), 3, "两轮当前句 + 一段插话都被回答")
        finally:
            bench.close()


class NoConclusionContinues(unittest.TestCase):
    """判断没回来 / 失败 / 超时 → 继续说；收口与开闸不变。"""

    def script_and_commands(self):
        script = dict(SCRIPT)
        script[INSERTION] = STRONG_COMPLETE
        commands = [("asr", 0, FIRST, True), ("hold", 0), ("asr", 300, INSERTION, True),
                    ("release", 900), ("hold", 1200), ("release", 1600), ("end", 3000)]
        return script, commands

    def assert_continue(self, bench, *, timeouts=0):
        outcome = bench.events_of("floor_outcome")[0]
        self.assertEqual(outcome["outlet"], FLOOR_CONTINUE, outcome)
        self.assertIn("继续说", outcome["reason"])
        self.assertEqual(bench.session.counters["floor_timeouts"], timeouts)
        self.assertEqual(bench.events_of("turn_void"), [])
        self.assertEqual(bench.ledger.lines("D")[-1]["text"], INSERTION, "插话照样记住（不丢）")
        self.assertEqual(len(bench.decisions()), 1, "不拿插话回答")
        self.assertEqual(bench.gate_routes(), ["agent"])

    def test_pending_judge_follows_continue(self):
        never = threading.Event()

        def floor_fn(text, scene=None, seat="D", hub_state=None, hub_sentence=None):
            never.wait(timeout=5.0)
            return floor_outcome(FLOOR_YIELD)

        script, commands = self.script_and_commands()
        bench = Bench(script, floor_fn=floor_fn, floor_timeout_ms=200)
        try:
            bench.drive(commands)
            self.assert_continue(bench, timeouts=1)
        finally:
            never.set()
            bench.close()

    def test_failed_judge_follows_continue(self):
        def floor_fn(text, scene=None, seat="D", hub_state=None, hub_sentence=None):
            raise ValueError("boom")

        script, commands = self.script_and_commands()
        bench = Bench(script, floor_fn=floor_fn)
        try:
            bench.drive(commands)
            self.assert_continue(bench)
            self.assertEqual(bench.gate.counters["floor_errors"], 1)
        finally:
            bench.close()


class HoldWhileTheJudgeFlies(unittest.TestCase):
    """四选还在飞时当前这句先不落地：判断回来之前一个字都不许动（不然「让路」就没有东西可作废）。"""

    def slow_floor(self, outlet, sleep_s=0.15):
        def floor_fn(text, scene=None, seat="D", hub_state=None, hub_sentence=None):
            time.sleep(sleep_s)          # 真实耗时：虚拟时钟这时已经往前走了一大段
            return floor_outcome(outlet)
        return floor_fn

    COMMANDS = [("asr", 0, FIRST, True), ("hold", 0), ("asr", 300, INSERTION, True),
                ("release", 350), ("end", 4000)]

    def test_the_current_sentence_waits_for_the_judge_then_continues_or_queues(self):
        for outlet in (FLOOR_CONTINUE, FLOOR_FINISH_FIRST):
            bench = Bench(SCRIPT, floor_fn=self.slow_floor(outlet))
            try:
                bench.drive(self.COMMANDS)
                holds = bench.events_of("floor_hold")
                self.assertEqual(len(holds), 1, "决策先到、四选还在飞：这一句被压住")
                self.assertEqual(holds[0]["version"], 1)
                decisions = bench.decisions()
                self.assertIn(normalize(FIRST), normalize(decisions[0][1]),
                              "被回答的是当前这句（插话没有被拿来顶替它）")
                outcome = bench.events_of("floor_outcome")[0]
                self.assertEqual(outcome["outlet"], outlet)
                self.assertGreaterEqual(decisions[0][0], outcome["t_ms"],
                                        "四选回来之后才落地：判断之前不动这一句")
                self.assertEqual(bench.events_of("turn_void"), [])
                if outlet == FLOOR_CONTINUE:
                    self.assertEqual(len(decisions), 1, "继续说：插话不进 Agent，不为它起一轮")
                    self.assertEqual(bench.ledger.lines("D")[-1]["text"], INSERTION)
                    self.assertEqual(bench.session.floor_released_total, 0)
                else:
                    self.assertEqual(bench.session.floor_released_total, 1)
                    self.assertIn(normalize(INSERTION), normalize(bench.decisions()[-1][1]),
                                  "此刻这句已经说完：插话直接作为下一轮发出（等这句话不丢字）")
            finally:
                bench.close()


class ClosingAndGateUnchanged(unittest.TestCase):
    """收住时刻与开闸结论不被带偏：0.7 / 1.5 / 3 秒一档都不动；没有插话时四选这一路一次不发。"""

    SCRIPT3 = {"打开空调": WEAK_COMPLETE,      # 弱 complete → 0.7 秒等待档
               "调低温度": CUT,               # 话断了 → 1.5 秒
               "导航回家": WAIT}              # 还在想 → 3 秒（不被 2 秒上限提前收）
    COMMANDS3 = [("asr", 0, "打开空调", True), ("release", 0),
                 ("asr", 3000, "调低温度", True), ("asr", 8000, "导航回家", True),
                 ("end", 14000)]

    def test_three_wait_tiers_are_not_skewed_by_the_floor_route(self):
        on = Bench(self.SCRIPT3, floor=FLOOR_YIELD)
        off = Bench(self.SCRIPT3, floor_enabled=False)
        try:
            on.drive(self.COMMANDS3)
            off.drive(self.COMMANDS3)
            self.assertEqual(on.session.counters["floor_asks"], 0, "没有插话就一次都不问")
            self.assertEqual(on.gate.counters["floor_calls"], 0)
            self.assertEqual(on.confirm_times(), off.confirm_times(), "收住时刻逐毫秒一致")
            self.assertEqual(on.gate_routes(), off.gate_routes(), "开闸结论一致")
            self.assertEqual(on.turn_done_times(), off.turn_done_times())
            self.assertEqual(on.session.counters["eot_calls"], off.session.counters["eot_calls"])
            waited = [t - asr for (t, _), asr in zip(on.confirm_times(), (0, 3000, 8000))]
            self.assertEqual(waited, [700, 1500, 3000],
                             "三档各走各的等待：0.7 / 1.5 / 3 秒，一格都没偏")
        finally:
            on.close()
            off.close()

    def test_the_insertion_version_keeps_its_own_confirm_and_route(self):
        """插话被回答的那一轮，也按它自己的判停 / 接不答走（不是借用当前这句的结论）。"""
        bench = run(floor=FLOOR_FINISH_FIRST)
        try:
            routes = bench.gate_routes()
            self.assertEqual(routes, ["agent", "agent"])
            second = bench.session.turns[-1]
            self.assertEqual(second["turns"], 2)
        finally:
            bench.close()


class CallCount(unittest.TestCase):
    """调用次数：每版 4 路不变；只在「人又开口」的那一回多 1 次四选（同一段只问一次）。"""

    def test_one_ask_per_insertion_episode_not_per_partial(self):
        script = dict(SCRIPT)
        for text in ("等一下先", "等一下先别", "等一下先别导航"):
            script[text] = STRONG_COMPLETE
        commands = [("asr", 0, FIRST, True), ("hold", 0),
                    ("asr", 300, "等一下先", False), ("asr", 340, "等一下先别", False),
                    ("asr", 380, "等一下先别导航", True), ("release", 900), ("hold", 1200),
                    ("release", 1600), ("end", 2600)]
        bench = Bench(script, floor=FLOOR_CONTINUE)
        try:
            bench.drive(commands)
            self.assertEqual(bench.gate.counters["floor_calls"], 1,
                             "同一段插话的三个 partial 只问一次")
            self.assertEqual(len(bench.events_of("floor_asked")), 1)
            # 判断用的是问出去那一刻的那一版（「等一下先」）；同一段插话后面冒出来的字
            # 只更新这一句本身——旁听台账里留下的是**整句**（不进 Agent）。
            self.assertEqual(bench.events_of("floor_outcome")[0]["text"], "等一下先")
            self.assertEqual(bench.session.floor_context[-1]["text"], "等一下先别导航",
                             "旁听台账里的这一句被补全成整句")
            self.assertEqual(bench.ledger.lines("D")[-1]["text"], "等一下先别导航")
            self.assertEqual(bench.ledger.counters["amended"], 2,
                             "三个 partial 里后面两版各补全一次，不追加第二行")
            self.assertEqual(len(bench.ledger.lines("D")), 1)
            # 每版 4 路的计数口径没变：判停 / 接不接 / 应声 / 节奏
            self.assertEqual(bench.gate.counters["accept_calls"], 1)
            self.assertEqual(bench.gate.counters["ack_calls"], 1)
            self.assertEqual(bench.gate.counters["pace_calls"], 1)
            self.assertEqual(bench.session.counters["eot_calls"], 1)
        finally:
            bench.close()

    def test_no_extra_calls_when_no_one_speaks_over_the_hub(self):
        bench = Bench({FIRST: STRONG_COMPLETE},
                      floor=FLOOR_YIELD)
        try:
            bench.drive([("asr", 0, FIRST, True), ("release", 200), ("end", 2000)])
            self.assertEqual(bench.gate.counters["floor_calls"], 0)
            self.assertEqual(bench.gate.counters["accept_calls"], 1)
            self.assertEqual(bench.gate.counters["ack_calls"], 1)
            self.assertEqual(bench.gate.counters["pace_calls"], 1)
            self.assertEqual(bench.session.counters["eot_calls"], 1)
        finally:
            bench.close()


class VadOpensTheSameDoor(unittest.TestCase):
    """VAD 先报「人开口」也走同一道四选：让路时仍按原路作废（事件字段不变）。"""

    def test_vad_first_then_the_text_still_gets_the_four_choice(self):
        commands = [("asr", 0, FIRST, True), ("hold", 0), ("vad", 290, "speech_start"),
                    ("asr", 300, INSERTION, True), ("release", 900), ("hold", 1200),
                    ("release", 1600), ("end", 2600)]
        bench = Bench(SCRIPT, floor=FLOOR_YIELD)
        try:
            bench.drive(commands)
            self.assertEqual(bench.gate.counters["floor_calls"], 1, "VAD 与出字是同一回，不重复问")
            self.assertEqual(bench.events_of("floor_asked")[0]["source"], "vad")
            self.assertEqual(len(bench.events_of("turn_void")), 1, "让路仍作废本轮")
            self.assertEqual(bench.events_of("turn_void")[0]["reason"], "new_text")
            self.assertEqual(len(bench.decisions()), 1)
        finally:
            bench.close()

    def test_vad_only_let_road_cancels_like_before(self):
        """只有 VAD、没有出字：让路 = 原来的 speech_start 作废（同一组字段）。"""
        commands = [("asr", 0, FIRST, True), ("hold", 0), ("vad", 300, "speech_start"),
                    ("release", 900), ("end", 1800)]
        bench = Bench({FIRST: STRONG_COMPLETE}, floor=FLOOR_YIELD)
        try:
            bench.drive(commands)
            voids = bench.events_of("turn_void")
            self.assertEqual(len(voids), 1)
            self.assertEqual(voids[0]["reason"], "speech_start")
            self.assertEqual(voids[0]["t_ms"], 300)
        finally:
            bench.close()


class VadBeforeTheText(unittest.TestCase):
    """VAD 先开口、四选回来时还没出字：按「继续说」兜底；字随后到了照样进旁听台账（不丢）。"""

    def test_no_text_yet_falls_back_to_continue_and_the_words_still_land_in_the_ledger(self):
        commands = [("asr", 0, FIRST, True), ("hold", 0), ("vad", 290, "speech_start"),
                    ("asr", 320, INSERTION, True), ("release", 900), ("hold", 1200),
                    ("release", 1600), ("end", 2600)]
        bench = Bench(SCRIPT, floor=FLOOR_FINISH_FIRST)   # 想「说完再接」，但那时还没有字
        try:
            bench.drive(commands)
            self.assertEqual(bench.gate.counters["floor_calls"], 1)
            outcome = bench.events_of("floor_outcome")[0]
            self.assertEqual(outcome["outlet"], FLOOR_CONTINUE, outcome)
            self.assertIn("还没出字", outcome["reason"])
            self.assertEqual(bench.events_of("floor_queued"), [], "没有拿空句排队")
            self.assertEqual(bench.ledger.lines("D")[-1]["text"], INSERTION,
                             "字到了之后照样记进旁听台账（不丢）")
            self.assertEqual(len(bench.decisions()), 1)
            self.assertEqual(bench.events_of("turn_void"), [])
        finally:
            bench.close()


class CushionSpeakingWindow(unittest.TestCase):
    """拍板 3 的另一半：**垫句也算「这边正在说」**（应声不算）。

    垫句那一小段（`SPEAK_HOLD_MS`）人又开口，四选同样问一次；这一段的落地：
    让路＝停下这句垫句（本机只记一笔，不接真 TTS）；说完再接＝排队，等垫句"说完"再放行；
    垫句抢回＝不再叠第二句（当前这句本身就是垫句），按说完再接落地。
    """

    SECOND = "等一下先别关窗"          # 与排队中的那句不是同一句（前缀也不同）

    def _script(self):
        script = dict(SCRIPT)
        script[self.SECOND] = CUT
        script[INSERTION] = CUT        # 放行后这一轮先停在话断档 1.5 秒：第二回插话才落在垫句那一小段
        return script

    def _commands(self):
        return [("asr", 0, FIRST, True), ("hold", 0),
                ("asr", 300, INSERTION, True), ("release", 900), ("hold", 950),
                ("asr", 1060, self.SECOND, True), ("release", 1600), ("hold", 1700),
                ("release", 2400), ("end", 4200)]

    def _drive(self, floor):
        bench = Bench(self._script(), floor=floor)
        self.addCleanup(bench.close)
        bench.drive(self._commands())
        return bench

    def test_the_cushion_itself_counts_as_being_said(self):
        bench = self._drive([FLOOR_CUSHION_BACK, FLOOR_FINISH_FIRST])
        cushions = bench.events_of("cushion_spoken")
        self.assertEqual([row["kind"] for row in cushions], ["cushion_back"])
        cushion_ms = cushions[0]["t_ms"]
        asked = bench.events_of("floor_asked")
        self.assertEqual(len(asked), 2, asked)
        self.assertEqual(asked[1]["window"], "speech", "垫句那一小段也算「正在说」（拍板 3）")
        self.assertEqual(asked[1]["hub_state"], "speaking")
        self.assertEqual(asked[1]["hub_sentence"], cushions[0]["text"], "判断知道这边正在说的是哪一句")
        self.assertLess(asked[1]["t_ms"] - cushion_ms, 1000, "落在垫句那一小段（1 秒）之内")
        outcome = bench.events_of("floor_outcome")[1]
        self.assertEqual(outcome["window"], "speech")
        self.assertFalse(outcome["void"], "垫句那一小段没有本轮可作废：让路在这里只是停下这句垫句")
        self.assertEqual(bench.events_of("turn_void"), [])
        # 插话照样被留下并被回答（不丢）
        self.assertEqual(bench.session.floor_released_total, 2)
        self.assertIn(normalize(self.SECOND), normalize(bench.decisions()[-1][1]))

    def test_yield_during_the_cushion_stops_it_and_keeps_the_words(self):
        bench = self._drive([FLOOR_CUSHION_BACK, FLOOR_YIELD])
        stopped = bench.events_of("cushion_stopped")
        self.assertEqual(len(stopped), 1, "让路落在垫句那一小段：把这句垫句停下")
        self.assertTrue(stopped[0]["context"] and not stopped[0]["agent"])
        self.assertIn("不接真 TTS", stopped[0]["note"])
        self.assertEqual(bench.events_of("turn_void"), [], "垫句那一小段没有本轮可作废，不该出现 turn_void")
        self.assertIn(self.SECOND, [row[1] for row in bench.decisions()],
                      "让路＝停下这句垫句、回到收听：插话留下并被回答（字不丢）")

    def test_cushion_back_during_a_cushion_does_not_stack_a_second_one(self):
        bench = self._drive([FLOOR_CUSHION_BACK, FLOOR_CUSHION_BACK])
        self.assertEqual(len(bench.events_of("cushion_spoken")) + len(bench.events_of("floor_gate")), 1,
                         "同一时刻最多一句：当前这句本身就是垫句，不再叠第二句")
        second = bench.events_of("floor_outcome")[1]
        self.assertEqual(second["outlet"], FLOOR_FINISH_FIRST, "按说完再接落地（插话不丢、随后回答）")
        self.assertIn("本身就是垫句", second["reason"])
        self.assertEqual(bench.session.floor_released_total, 2)

    def test_an_ack_does_not_open_the_window(self):
        bench = Bench({FIRST: CUT, "再说一句": STRONG_COMPLETE}, floor=FLOOR_YIELD)
        self.addCleanup(bench.close)
        bench.drive([("asr", 0, FIRST, True), ("asr", 600, "再说一句", True),
                     ("release", 900), ("end", 3400)])
        acks = bench.events_of("ack_spoken")
        self.assertTrue(acks, "这一拍应了一声（对照项必须真的出声）")
        self.assertLess(600 - acks[0]["t_ms"], 1000, "插话落在应声后 1 秒内")
        self.assertEqual(bench.events_of("floor_asked"), [], "应一声不算「这边正在说」（拍板 3）")
        self.assertEqual(bench.gate.counters["floor_calls"], 0)
        self.assertEqual(bench.session.hub_said, [], "应声不进「本机说过的话」表（它不是垫句）")


class Surface(unittest.TestCase):
    """台面：两席面板各有「两人同时说」一行，四条出口与失败兜底都能看见。"""

    def test_dual_seat_panel_shows_the_floor_row_and_events(self):
        page = (ROOT / "ui" / "index.html").read_text()
        self.assertEqual(page.count("data-hub-floor>"), 2, "主驾 / 副驾各一行")
        for key in ("floor_asked", "hub_floor", "floor_outcome", "floor_queued",
                    "floor_released", "floor_context", "floor_gate"):
            self.assertIn(key, page, "台面不认识这条事件：" + key)
        self.assertIn("让路", page)


if __name__ == "__main__":
    unittest.main(verbosity=2)
