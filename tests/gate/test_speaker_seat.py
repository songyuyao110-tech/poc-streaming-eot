"""「这句话是主驾还是副驾说的」必须进 Agent 的输入 —— 离线验收（不连 8009 / 不连 LLM / 不连 ASR）。

口径（2026-11-17 用户拍板）：交给 Agent 办理时，输入里必须明确标明这句话是主驾还是副驾说的；
需要座位位置（`area`）的工具**从这份输入里取位置**，编排层不在外面用规则硬填；也不允许回问位置。

本测试只钉住「输入里有没有这一栏」这一半（真模型是否据此取到位置，在真页面 / 模块探针里验）：

1. `speaker_context()`：D/P 各出主驾/副驾；单席（None）不出任何新栏（context 形状不变）；
2. 该栏确实跟着 `context` 送到 `decide_fn`（两席各一份，不串席）；
3. 「不要回问位置」这句说明也在输入里（缺位置不靠回问过关）；
4. `agent_request` 事件如实标明这一趟输入里带了说话席。

运行：python3 -B tests/gate/test_speaker_seat.py
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from gate.turn_session import TurnConfig, TurnSession, speaker_context  # noqa: E402


class SpeakerContext(unittest.TestCase):
    def test_labels_per_seat(self):
        d = speaker_context("D")
        p = speaker_context("P")
        self.assertEqual(d["speaker_position"], "主驾")
        self.assertEqual(p["speaker_position"], "副驾")
        self.assertEqual(d["speaker_seat"], "D")
        self.assertEqual(p["speaker_seat"], "P")

    def test_single_seat_shape_unchanged(self):
        self.assertEqual(speaker_context(None), {})
        self.assertEqual(speaker_context(""), {})

    def test_note_forbids_asking_back_for_position(self):
        for seat in ("D", "P"):
            note = speaker_context(seat)["speaker_note"]
            self.assertIn("不要回问位置", note)
            self.assertIn(speaker_context(seat)["speaker_position"], note)

    def test_turn_session_context_carries_speaker(self):
        self.assertEqual(TurnSession(TurnConfig(warm_llm=False), lambda event: None,
                                     seat_id="P", evidence_root=None).context["speaker_position"],
                         "副驾")
        self.assertNotIn("speaker_position",
                         TurnSession(TurnConfig(warm_llm=False), lambda event: None,
                                     evidence_root=None).context)


class SpeakerReachesDecide(unittest.TestCase):
    """这一栏真的送到 decide_fn（不是只放在实例属性里）。"""

    def make(self, seat):
        self.seen = []
        self.events = []
        session = TurnSession(
            TurnConfig(warm_llm=False), self.events.append, seat_id=seat, evidence_root=None,
            eot_fn=lambda text: {"label": "complete", "may_confirm": True, "complete": 0.99,
                                 "cut": 0.01, "wait": 0.0, "confidence": 0.99},
            select_tools_fn=lambda text, tool_set, context: {"selected": ["seat_massage_48u_control"],
                                                             "schemas": [], "source": "测试"},
            decide_fn=self.decide,
        )
        return session

    def decide(self, utterance, selected, schemas, tool_set, context, **kwargs):
        self.seen.append(dict(context or {}))
        return {"decision": {"kind": "tool_call", "tool": "seat_massage_48u_control",
                             "arguments": {"area": (context or {}).get("speaker_position")}},
                "llm_ms": 1.0}

    def drive(self, session, text, timeout_ms=3000):
        session.on_text_send(0, text)
        deadline = time.time() + timeout_ms / 1000.0
        now = 0
        while time.time() < deadline:
            now += 40
            session.tick(now)
            if self.seen or session.turn_done:
                return now
            time.sleep(0.01)
        return now

    def test_each_seat_gets_its_own_speaker_in_input(self):
        for seat, label in (("D", "主驾"), ("P", "副驾")):
            session = self.make(seat)
            self.drive(session, "打开座椅按摩")
            self.assertTrue(self.seen, seat + " 席的 decide 没被调到")
            self.assertEqual(self.seen[0].get("speaker_position"), label)
            self.assertEqual(self.seen[0].get("speaker_seat"), seat)
            requested = [event for event in self.events if event["event"] == "agent_request"]
            self.assertTrue(requested, "要有 agent_request 事件作证据")
            self.assertTrue(requested[0].get("speaker_in_input"))
            self.assertEqual(requested[0].get("speaker"), label)


if __name__ == "__main__":
    unittest.main(verbosity=2)
