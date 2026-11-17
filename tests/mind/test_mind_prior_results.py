"""读心判断带「最近 5 次已经出过的结果」的离线验收（假引擎，不连 LLM）。

2026-11-11 21:50 用户拍板：读心每次判断除了当前画面和两路已经出到的字，还要带上最近 5 次
**已经出过**的读心结果（不够 5 份就有几份带几份，不要编造更早的），让前后旁白能连上。

本测钉住：
  [1] 第 1 次判断没有前序结果（不编造）；
  [2] 第 2 次判断带第 1 份；之后按「更早 → 更近」往后带；
  [3] 最多 5 份：第 7 次判断带的是第 2～6 份（不是 6 份、也不是第 1 份）；
  [4] 带上的就是真的出过的那几句（不重不漏、没有占位句）；
  [5] 提示词里真的写了这几份（`build_messages(prior_results=…)`）；
  [6] 老引擎（judge 不收 prior_results）照旧能跑，不被新参数打挂；
  [7] 场景快照把「这次判断带上了哪几份」交给页面（`prior_count / prior_used / prior_limit`）；
  [8] 离线替身也把出过的结果记给页面看，但不冒充「已经用过」（`prior_used=False`）。

用法：`python3 -B tests/mind/test_mind_prior_results.py`
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.dont_write_bytecode = True

from mind.mind_llm import MAX_PRIOR_RESULTS, build_messages  # noqa: E402
from mind.mind_reading import MindReading  # noqa: E402


class RecordingEngine:
    """假引擎：把这次判断收到的上下文记下来，并按这一版字产一句不同的旁白。"""

    model = "fake/prior"

    def __init__(self):
        self.calls = []

    def describe(self):
        return {"name": self.model, "mode": "litellm_vl", "source": "llm", "fallback": False}

    def judge(self, *, text, visual, seat="D", final=False, image=None, image_w=None,
              image_h=None, prior_texts=None, speech=None, prior_results=None):
        self.calls.append({"text": text, "prior_results": list(prior_results or [])})
        return {"result": {"scene": "旁白：" + (text or "画面"), "intent": "—", "target": "—",
                           "topic": "—", "confidence": 0.6, "reason": "fake", "cues": [],
                           "finalized": final},
                "engine": {"name": self.model, "model": self.model, "mode": "litellm_vl",
                           "source": "llm", "fallback": False, "latency_ms": 5.0}}


class LegacyEngine(RecordingEngine):
    """老引擎：judge 不收 `prior_results`（也没收 speech）——新参数不能把它打挂。"""

    def judge(self, *, text, visual, seat="D", final=False, image=None, image_w=None,
              image_h=None, prior_texts=None):
        self.calls.append({"text": text})
        return {"result": {"scene": "老引擎旁白：" + (text or "画面"), "intent": "—", "target": "—",
                           "topic": "—", "confidence": 0.5, "reason": "fake", "cues": [],
                           "finalized": final},
                "engine": {"name": self.model, "model": self.model, "mode": "litellm_vl",
                           "source": "llm", "fallback": False, "latency_ms": 5.0}}


def read_once(session, engine, text, timeout=3.0):
    """喂一句并让中枢收住 → 等这一次判断落地（假引擎是秒回，这里只等一等同步）。"""
    before = len(engine.calls)
    session.ingest(seat="D", text=text, final=True)
    session.on_utterance_close("D")
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(engine.calls) > before and not session.snapshot()["scene"]["pending"]:
            return
        time.sleep(0.01)
    raise AssertionError("这次判断没有落地：%r" % text)


class PriorResultsContext(unittest.TestCase):
    def test_first_judgement_carries_no_prior_results(self):
        engine = RecordingEngine()
        session = MindReading(session_id="prior-1", engine=engine)
        read_once(session, engine, "导航去望京")
        self.assertEqual(engine.calls[0]["prior_results"], [], "第 1 次判断还没有前序结果")

    def test_second_judgement_carries_the_first_result(self):
        engine = RecordingEngine()
        session = MindReading(session_id="prior-2", engine=engine)
        read_once(session, engine, "导航去望京")
        read_once(session, engine, "尽量走高速")
        carried = engine.calls[1]["prior_results"]
        self.assertEqual([item["scene"] for item in carried], ["旁白：导航去望京"])

    def test_carries_only_the_last_five(self):
        engine = RecordingEngine()
        session = MindReading(session_id="prior-5", engine=engine)
        texts = ["第%d句" % index for index in range(1, 8)]
        for text in texts:
            read_once(session, engine, text)
        carried = [item["scene"] for item in engine.calls[-1]["prior_results"]]
        self.assertEqual(len(carried), MAX_PRIOR_RESULTS, "最多 5 份（不是 6 份）")
        self.assertEqual(MAX_PRIOR_RESULTS, 5, "本卡口径就是 5 份")
        self.assertEqual(carried, ["旁白：第%d句" % index for index in range(2, 7)],
                         "带的是最近 5 次（第 2～6 份），不漏不重、更早的（第 1 份）不再带")

    def test_order_is_oldest_to_newest_with_relative_time(self):
        engine = RecordingEngine()
        session = MindReading(session_id="prior-order", engine=engine)
        for text in ("先说一句", "再说一句", "最后一句"):
            read_once(session, engine, text)
        carried = engine.calls[-1]["prior_results"]
        self.assertEqual([item["scene"] for item in carried],
                         ["旁白：先说一句", "旁白：再说一句"], "更早 → 更近")
        ages = [item["ago_ms"] for item in carried]
        self.assertTrue(all(age >= 0 for age in ages), ages)
        self.assertLessEqual(ages[0], ages[1] + 50, "先出的那份更早（相对时间不该倒挂）")

    def test_carried_items_are_only_really_produced_results(self):
        engine = RecordingEngine()
        session = MindReading(session_id="prior-real", engine=engine)
        produced = []
        for text in ("一", "二", "三", "四"):
            read_once(session, engine, text)
            produced.append("旁白：" + text)
        for call in engine.calls[1:]:
            for item in call["prior_results"]:
                self.assertIn(item["scene"], produced, "带上的必须是真出过的那几句")
                self.assertNotIn("读场景中", item["scene"], "占位句不能当成已经出过的结果")

    def test_placeholder_is_not_recorded(self):
        """真模型还没回话时的「读场景中…」占位不算出过；判断落地后才记。"""
        engine = RecordingEngine()
        session = MindReading(session_id="prior-pending", engine=engine)
        session.ingest(seat="D", text="导航去公司", final=False)
        snap = session.snapshot()
        self.assertEqual(snap["scene"]["prior_count"], 0, "还没出过结果：一份都不带")
        self.assertEqual(snap["scene"]["prior_results"], [])

    def test_legacy_engine_is_not_passed_the_new_argument(self):
        engine = LegacyEngine()
        session = MindReading(session_id="prior-legacy", engine=engine)
        read_once(session, engine, "老引擎也要能跑")
        read_once(session, engine, "第二句")
        self.assertEqual(len(engine.calls), 2, "老引擎照旧一次判断一次调用（没被新参数打挂）")

    def test_scene_exposes_what_this_judgement_carried(self):
        engine = RecordingEngine()
        session = MindReading(session_id="prior-scene", engine=engine)
        read_once(session, engine, "第一句")
        read_once(session, engine, "第二句")
        scene = session.snapshot()["scene"]
        self.assertEqual(scene["prior_limit"], 5)
        self.assertEqual(scene["prior_count"], 1, "这一次判断带了 1 份前序结果")
        self.assertTrue(scene["prior_used"], "真模型这次判断确实读到了这份上下文")
        self.assertEqual([item["scene"] for item in scene["prior_results"]], ["旁白：第一句"])


class OfflineHistory(unittest.TestCase):
    def test_offline_readings_are_recorded_but_not_claimed_as_used(self):
        """离线替身也把「已经出过的结果」记给页面看，但明写没有用它判断（不冒充）。"""
        session = MindReading(session_id="prior-offline")
        first = session.ingest(seat="D", text="导航去望京", final=False)["scene"]
        self.assertEqual(first["prior_count"], 0, "第 1 次还是第 1 次")
        second = session.ingest(seat="P", text="咱晚上去哪吃", final=False)["scene"]
        self.assertGreaterEqual(second["prior_count"], 1, "出过的那一份要记下来")
        self.assertFalse(second["prior_used"], "离线规则替身不读这份上下文")
        self.assertLessEqual(second["prior_count"], 5)


class PromptCarriesPriorResults(unittest.TestCase):
    def test_prompt_block_is_written_from_the_carried_items(self):
        part = build_messages("导航去公司", {"has_frame": True}, "D", True,
                              prior_results=[{"scene": "车里两个人在闲聊", "ago_ms": 6400},
                                             {"scene": "主驾说要去望京", "ago_ms": 2200}])
        text = part[-1]["content"][0]["text"]
        self.assertIn("之前已经出过的旁白", text)
        self.assertIn("约 6 秒前", text)
        self.assertIn("约 2 秒前", text)
        self.assertIn("「车里两个人在闲聊」", text)
        self.assertIn("「主驾说要去望京」", text)
        self.assertIn("最多 5 份", text, "口径写在提示词里：最多 5 份，不够就有几份带几份")

    def test_no_prior_results_no_block(self):
        text = build_messages("导航去公司", {"has_frame": True}, "D", True)[-1]["content"][0]["text"]
        self.assertNotIn("之前已经出过的旁白", text, "一份都没有时整段不出现（不编造）")

    def test_prompt_keeps_system_prompt_untouched(self):
        from mind.mind_llm import PROMPT_VERSION, SYSTEM_PROMPT
        messages = build_messages("导航去公司", {"has_frame": True}, "D", True,
                                 prior_results=[{"scene": "车里很安静", "ago_ms": 3000}])
        self.assertEqual(messages[0], {"role": "system", "content": SYSTEM_PROMPT},
                         "稿 E 底稿逐字不动：前几次结果只写进这一次请求的上下文")
        self.assertEqual(PROMPT_VERSION, "v05")


if __name__ == "__main__":
    unittest.main(verbosity=2)
