"""方案 B 离线验收：不接的原话按席留 3 句、标「不是对车说的」、跨席可见，只在开闸时两栏进 Agent。

钉住 wiki `[[中枢并发闸门]]` §13（2026-11-10 17:23 已拍）＋
`tmp/poc-overhear-context-b-原料.md` / `-验收.md` 的可验部分：

1. **留 3 句**：「说完但不接」的原话按席留，最近 3 句，第 4 句挤掉最早一句；不接 ≠ 丢弃。
2. **标签**：每条带「不是对车说的」；**跨席**可见（副驾留的，主驾开闸也看得到，反向同样）。
3. **一个口子 / 只在开闸那一轮**：进 Agent 的上下文只由中枢拼一次，两栏（此刻场景 / 刚才说过…），
   且只在闸门开的那一刻送；不接那一轮不拼、没说完不拼、超时/失败不拼。
4. **读心仍只出场景**：台账不吃场景，场景那一句仍来自读心快照（本测试用假 scene_fn 供给）。
5. **不复活成指令**：原话只进 close context 的第二栏，decide 的 utterance 仍是本句。

运行：python3 -B tests/test_overhear_context.py
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from gate.hub_gate import GateKeeper  # noqa: E402
from overhear_ledger import (KEEP_PER_SEAT, LINE_LABEL, OVERHEARD_COLUMN, SCENE_COLUMN,  # noqa: E402
                             OverhearLedger, render_columns)
from gate.turn_session import TurnConfig, TurnSession  # noqa: E402
SCENE = "我听到主驾在说昨天吃火锅。"


def accept(verdict):
    def _fn(text, scene=None, seat="D"):
        return {"accept": verdict, "score": 0.9 if verdict else 0.2,
                "confidence": 0.9 if verdict else 0.2, "threshold": 0.5,
                "threshold_note": "开闸线未拍，本次临时用 0.5",
                "reason": "测试用接不接结论", "source": "测试", "model": "fake/accept",
                "latency_ms": 0.1}
    return _fn


def accept_raises(text, scene=None, seat="D"):
    raise RuntimeError("8009 不可用（测试）")


def eot_complete(text):
    return {"label": "complete", "complete": 0.95, "cut": 0.02, "wait": 0.03, "confidence": 0.9,
            "wall_ms": 1.0}


def eot_wait(text):
    return {"label": "wait", "complete": 0.10, "cut": 0.05, "wait": 0.85, "confidence": 0.4,
            "wall_ms": 1.0}


class Chain:
    """一套主链最小装配：一本台账 + 两席闸门 + 两席 TurnSession（与 ui/server.py 同形）。"""

    def __init__(self, *, accept_fn=None, eot_fn=eot_complete, scene=SCENE):
        self.events = []
        self.decisions = []          # (seat, utterance, context)
        self.tools = []
        self.ledger = OverhearLedger()
        self.scene = scene
        self.gates = {}
        self.sessions = {}
        for seat in ("D", "P"):
            self.gates[seat] = GateKeeper(accept_fn=accept_fn or accept(True),
                                          scene_fn=lambda: self._scene(),
                                          seat=seat, on_event=self.events.append)
            self.sessions[seat] = TurnSession(
                TurnConfig(), lambda event: self.events.append(event),
                session_id="overhear-%s-%s" % (seat, id(self)), seat_id=seat,
                eot_fn=eot_fn,
                select_tools_fn=self._select, decide_fn=self._decide_factory(seat),
                evidence_root=None, gate=self.gates[seat], overhear=self.ledger)

    def _scene(self):
        if self.scene is None:
            return None
        return {"summary": self.scene, "revision": 3, "updated_at_ms": 0, "age_ms": 900}

    def _select(self, utterance, tool_set, context):
        self.tools.append((utterance, context))
        return {"scores": {"tool:car_navi_to_destination": 0.9}, "selected": ["car_navi_to_destination"],
                "schemas": [], "native_schemas": None, "domains": None, "timing": {}}

    def _decide_factory(self, seat):
        def _decide(utterance, selected, schemas, tool_set, context=None, **kwargs):
            self.decisions.append((seat, utterance, context))
            return {"decision": {"kind": "tool_call", "tool": "car_navi_to_destination",
                                 "arguments": {}}, "llm_ms": 1.0, "log_fields": {}}
        return _decide

    def say(self, seat, text, *, t0=1000, horizon_ms=4000):
        session = self.sessions[seat]
        session.on_asr(t0, text, False)
        session.on_asr(t0 + 300, text, True)
        end = time.time() + 1.2
        for t in range(t0 + 400, t0 + 400 + horizon_ms, 40):
            session.tick(t)
            time.sleep(0.004)
            if self.decisions and session._gate_open_version == session.version:
                time.sleep(0.06)          # 让 decide worker 回填
                session.tick(t)
                break
            if time.time() > end:
                break
        time.sleep(0.05)
        session.tick(t0 + 400 + horizon_ms)
        return session

    def events_of(self, name):
        return [event for event in self.events if event.get("event") == name]


class LedgerCase(unittest.TestCase):
    def test_keep_last_three_per_seat(self):
        ledger = OverhearLedger()
        for index in range(4):
            ledger.record("D", "闲聊第 %d 句" % index, 100 + index)
        lines = ledger.lines("D")
        self.assertEqual(len(lines), KEEP_PER_SEAT)
        self.assertEqual([line["text"] for line in lines],
                         ["闲聊第 1 句", "闲聊第 2 句", "闲聊第 3 句"], "第 4 句进来挤掉最早一句")
        self.assertEqual(ledger.counters["evicted"], 1)
        self.assertEqual(ledger.counters["recorded"], 4)
        self.assertTrue(all(line["label"] == LINE_LABEL for line in lines))

    def test_blank_text_is_not_recorded(self):
        ledger = OverhearLedger()
        self.assertIsNone(ledger.record("D", "   "))
        self.assertEqual(ledger.size(), 0)

    def test_columns_two_labels_and_cross_seat(self):
        ledger = OverhearLedger()
        ledger.record("P", "你帮我看看那个…", 100)
        columns = ledger.render_columns(SCENE)
        self.assertIn(SCENE_COLUMN + SCENE, columns)
        self.assertIn(OVERHEARD_COLUMN, columns)
        self.assertIn("[副驾] 你帮我看看那个…", columns, "跨席可见：每句带席名标签")
        self.assertIn(LINE_LABEL, ledger.snapshot()["label"])

    def test_empty_scene_keeps_only_overheard_column(self):
        ledger = OverhearLedger()
        ledger.record("D", "自言自语一句", 100)
        columns = ledger.render_columns(None)
        self.assertNotIn(SCENE_COLUMN, columns, "没有场景就不出场景栏，也不等场景")
        self.assertIn(OVERHEARD_COLUMN, columns)

    def test_nothing_to_send_returns_none(self):
        self.assertIsNone(render_columns(None, []))


class MainChainCase(unittest.TestCase):
    def test_declined_sentence_is_kept_with_label_and_not_entering_agent(self):
        chain = Chain(accept_fn=accept(False))
        session = chain.say("D", "你说今天这天气可真舒服，咱晚上去哪吃")
        self.assertEqual(session.context_consumed, 1, "说完但不接：只进上下文")
        self.assertEqual(chain.decisions, [], "不接那一轮不进 Agent（decide 一次都不发）")
        self.assertEqual(chain.tools, [], "不接那一轮也不披露工具")
        recorded = chain.events_of("overhear_recorded")
        self.assertEqual(len(recorded), 1)
        self.assertEqual(recorded[0]["text"], "你说今天这天气可真舒服，咱晚上去哪吃")
        self.assertEqual(recorded[0]["label"], LINE_LABEL)
        self.assertEqual(chain.ledger.lines("D")[0]["text"], "你说今天这天气可真舒服，咱晚上去哪吃")

    def test_gate_open_carries_scene_and_marked_lines_in_two_columns(self):
        chain = Chain(accept_fn=accept(False))
        chain.say("D", "你说今天这天气可真舒服，咱晚上去哪吃")
        chain.gates["D"].close()
        chain.gates["D"].accept_fn = accept(True)       # 下一轮改成要接
        chain.events.clear()
        chain.say("D", "导航去刚才说的那家")
        self.assertEqual(len(chain.decisions), 1, "开闸那一轮才进 Agent")
        seat, utterance, context = chain.decisions[0]
        self.assertEqual(utterance, "导航去刚才说的那家", "原话不复活成新指令：话语仍是本句")
        self.assertIn("overhear_context", context)
        columns = context["overhear_context"]
        self.assertIn(SCENE_COLUMN + SCENE, columns, "第一栏是读心此刻那句场景")
        self.assertIn(OVERHEARD_COLUMN, columns)
        self.assertIn("[主驾] 你说今天这天气可真舒服，咱晚上去哪吃", columns, "第二栏带席名与标签")
        self.assertNotEqual(columns.index(SCENE_COLUMN), columns.index(OVERHEARD_COLUMN), "两栏并列")
        self.assertIn("不是新指令", context["overhear_note"])
        # 只在开闸那一轮拼一次：agent_context 事件只有一条，且 columns 与 decide 拿到的一致
        agent_context = chain.events_of("agent_context")
        self.assertEqual(len(agent_context), 1)
        self.assertEqual(agent_context[0]["columns"], columns)
        self.assertEqual(agent_context[0]["scene"], SCENE)
        self.assertTrue(agent_context[0]["one_entry"])
        # 工具披露不走这份背景（一个口子只给 Agent 的决策段）
        self.assertEqual(len(chain.tools), 1)
        self.assertNotIn("overhear_context", chain.tools[0][1])

    def test_cross_seat_visible(self):
        chain = Chain(accept_fn=accept(False))
        chain.say("P", "你帮我看看那个…")
        self.assertEqual(len(chain.ledger.lines("P")), 1)
        chain.gates["D"].close()
        chain.gates["D"].accept_fn = accept(True)
        chain.say("D", "导航去望京SOHO")
        self.assertEqual(len(chain.decisions), 1)
        columns = chain.decisions[0][2]["overhear_context"]
        self.assertIn("[副驾] 你帮我看看那个…", columns, "副驾留的，主驾开闸也看得到")

    def test_four_declines_keep_only_last_three(self):
        chain = Chain(accept_fn=accept(False))
        for text in ("闲聊第一句", "闲聊第二句", "闲聊第三句", "闲聊第四句"):
            chain.gates["D"].close()
            chain.gates["D"].accept_fn = accept(False)
            chain.say("D", text)
        kept = [line["text"] for line in chain.ledger.lines("D")]
        self.assertEqual(kept, ["闲聊第二句", "闲聊第三句", "闲聊第四句"],
                         "第 4 句进来挤掉最早一句；最早一句不在")

    def test_timeout_is_not_recorded_as_declined(self):
        chain = Chain(accept_fn=accept_raises, eot_fn=eot_complete)
        # 闸门侧超时预算压到 150ms，等它收成「超时：先不开闸」，再看台账
        chain.gates["D"].accept_timeout_ms = 150
        session = chain.say("D", "你说今天这天气可真舒服")
        self.assertEqual(chain.ledger.size(), 0, "判断未回 / 超时 ≠ 不接：不写台账")
        self.assertEqual(session.context_consumed, 0, "超时也不把文本记进上下文")
        events = [event["event"] for event in chain.events]
        self.assertIn("gate_pending", events)
        self.assertNotIn("context_absorbed", events)
        self.assertNotIn("overhear_recorded", events)

    def test_not_done_is_not_recorded(self):
        chain = Chain(accept_fn=accept(False), eot_fn=eot_wait)
        chain.say("D", "帮我看看前面那个", horizon_ms=1200)
        self.assertEqual(chain.ledger.size(), 0, "还没说完：不按「不接」记账")
        self.assertEqual(chain.decisions, [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
