"""文字输入「发送即收住」离线验收（不连 8009 / 不连 LLM / 不连 ASR）。

钉住本卡的可验部分（其余真链行为在真 8765 上用页面验）：

1. **发送即收住**：整句原话按 final 出字；**判停模型一次都不该被调**（`eot_fn` 抛错即失败）；
2. **要不要回答照旧并发判**：要回答 → 开闸进 Agent；不回答 → 只进上下文；
3. **收住那一刻照旧**读一次心（`mind.on_utterance_close`）、写一刀记忆（两条路由都写）；
4. `hub_eot` 标 `settled_by=text_send`：界面据此写「文字发送即收住」，不冒充判停结论；
5. **两席分开**：一席打的字只进该席的 TurnSession（另一席版本号不动）。

运行：python3 -B tests/gate/test_text_send.py
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from gate.hub_gate import GateKeeper  # noqa: E402
from gate.turn_session import TurnConfig, TurnSession  # noqa: E402


class Counters:
    """读心 / 记忆的替身：只记「收住那一刻有没有被叫到」，不连外部服务。"""

    def __init__(self):
        self.closes = []
        self.writes = []
        self.eot_calls = 0

    def scene(self):
        return {"summary": "主驾在车里随口说了一句", "intent": "闲聊", "target": "无",
                "topic": "闲聊", "cues": [], "revision": 2, "updated_at_ms": 0,
                "age_ms": 300, "pending": False}

    def mind_close(self, seat=None, **kwargs):
        self.closes.append({"seat": seat, **kwargs})

    def memory_write(self, turn):
        self.writes.append(dict(turn))
        return {"status": "queued", "seat": turn.get("seat"), "text": turn.get("text")}


def accept_outcome(accept):
    return {"accept": accept, "confidence": 0.9, "reason": "测试用接不接结论",
            "source": "测试", "model": "fake/accept", "latency_ms": 1.0}


class TextSend(unittest.TestCase):
    def make(self, accept=True):
        self.counters = Counters()
        self.events = []
        gate = GateKeeper(accept_fn=lambda text, scene, seat: accept_outcome(accept),
                          scene_fn=self.counters.scene, seat="D", on_event=self.events.append)
        session = TurnSession(
            TurnConfig(warm_llm=False), self.events.append, gate=gate, seat_id="D",
            eot_fn=self._forbidden_eot,
            select_tools_fn=lambda text, tool_set, context: {"selected": ["navigation"],
                                                             "source": "测试", "latency_ms": 1.0},
            decide_fn=lambda utterance, selected, schemas, tool_set, context, **kwargs: {
                "decision": {"kind": "chat", "answer": "收到", "tool": None},
                "latency_ms": 1.0, "source": "测试"},
            mind=type("Mind", (), {"on_utterance_close": self.counters.mind_close})(),
            memory=type("Memory", (), {"write": self.counters.memory_write}),
            evidence_root=None,
        )
        return session

    def _forbidden_eot(self, text):
        self.counters.eot_calls += 1
        raise AssertionError("文字发送不该调判停模型：" + text)

    def drive(self, session, text, timeout_ms=1500):
        session.on_text_send(0, text)
        deadline = time.time() + timeout_ms / 1000.0
        now = 0
        while time.time() < deadline:
            now += 40
            session.tick(now)
            if session.state == "CONFIRMED" or session.context_consumed or session.turn_done:
                return now
            time.sleep(0.01)
        return now

    def test_send_is_settled_without_eot_model(self):
        session = self.make(accept=True)
        self.drive(session, "帮我把空调调到24度")
        names = [event["event"] for event in self.events]
        self.assertEqual(self.counters.eot_calls, 0, "发送即收住：不跑判停")
        self.assertNotIn("eot_scores", names)
        self.assertIn("text_send", names)
        settled = [event for event in self.events if event["event"] == "hub_eot"]
        self.assertTrue(settled, "闸门要拿到这一版字的「说完」")
        self.assertEqual(settled[0].get("settled_by"), "text_send")
        self.assertEqual(session.state, "CONFIRMED", "要回答 → 开闸")

    def test_settle_reads_mind_and_writes_memory(self):
        session = self.make(accept=True)
        self.drive(session, "主驾说今天天气不错")
        self.assertEqual(len(self.counters.closes), 1, "收住那一刻读一次心")
        self.assertEqual(self.counters.closes[0]["seat"], "D")
        self.assertEqual([write["text"] for write in self.counters.writes],
                         ["主驾说今天天气不错"], "收住那一刻写一刀记忆（含原话）")

    def test_declined_still_writes_memory_and_only_enters_context(self):
        session = self.make(accept=False)
        self.drive(session, "咱晚上去吃火锅吧")
        self.assertEqual(session.context_consumed, 1, "不回答 → 只进上下文")
        self.assertEqual(session.state, "LISTENING")
        self.assertEqual([write["text"] for write in self.counters.writes], ["咱晚上去吃火锅吧"],
                         "不回答的那句也要写进记忆")

    def test_seats_do_not_share_text(self):
        d = self.make(accept=True)
        p = self.make(accept=True)
        self.drive(d, "导航去公司")
        self.assertEqual(p.version, 0, "主驾打的字不该进副驾的会话")
        self.assertEqual(p.suffix_text(), "")

    def test_second_send_while_first_is_judging_waits_its_turn(self):
        """上一句还在判（接不接/工具/决策没落地）时又发一句：排队等上一句收口，不并进同一轮、不丢字。"""
        session = self.make(accept=True)
        self.drive(session, "导航去公司")           # 第一句已收住，决策还在飞（state=CONFIRMED）
        self.assertEqual(session.state, "CONFIRMED")
        session.on_text_send(0, "那改去机场")       # 第二句此时发来
        held = [event for event in self.events if event["event"] == "text_held"]
        self.assertTrue(held, "上一句没落地时，第二句应排队并如实报 text_held")
        self.assertEqual(session.version, 1, "排队期间不许把第二句并进第一句的版本")
        deadline = time.time() + 2.0
        now = 0
        while time.time() < deadline and not session.turn_done:
            now += 40
            session.tick(now)
            time.sleep(0.01)
        # 排队期间第一句按它自己的字落地（不能因为第二句发来就改判/丢判）。
        self.assertEqual([record["text"] for record in session.consumptions], ["导航去公司"])
        deadline = time.time() + 2.0
        while time.time() < deadline and not (
                session.turn_done and [record["text"] for record in session.consumptions] == ["那改去机场"]):
            now += 40
            session.tick(now)
            time.sleep(0.01)
        # 第二句在上一句收口后自己开一轮、自己落地（consumptions 每轮复位，所以这里只看这一轮）。
        self.assertEqual([record["text"] for record in session.consumptions], ["那改去机场"])
        sends = [event["text"] for event in self.events if event["event"] == "text_send"]
        self.assertEqual(sends, ["导航去公司", "那改去机场"])

    def test_next_sentence_does_not_stack(self):
        """下一句不叠上一句：上一句收口后再发一句，这一句就是它自己（不是两句拼起来）。"""
        session = self.make(accept=True)
        self.drive(session, "导航去公司")
        deadline = time.time() + 2.0
        now = 0
        while time.time() < deadline and not session.turn_done:
            now += 40
            session.tick(now)
            time.sleep(0.01)
        self.assertTrue(session.turn_done, "第一句要先收口")
        self.drive(session, "那改去机场")
        last_hub = [event for event in self.events if event["event"] == "hub_text"][-1]
        self.assertEqual(last_hub["text"], "那改去机场", "第二句不该带着上一句")
        self.assertEqual(session.suffix_text(), "那改去机场")
        self.assertEqual(session.version, 2, "每发一句都是一个新版本")


if __name__ == "__main__":
    unittest.main(verbosity=2)
