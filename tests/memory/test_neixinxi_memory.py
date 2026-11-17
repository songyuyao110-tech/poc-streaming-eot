"""内心戏页「收住 → 读心落地 → 写前快判断 → 通过才写一刀」的离线契约（不连 memos / 不连模型）。

本卡（2026-11-12 10:29–10:31 拍板）改的是内心戏页记忆落账的**时机与攒句**：
**读心出了结果才有资格写；写之前用判停同一套模型做一次很快的「有没有必要写」；通过了才落一刀；
碎片先攒再并**。页面显示的仍然必须是记忆里**查回来**的内容，不是把读心旁白再贴一次。

断言（对照 `tmp/poc-neixinxi-memory-write-验收.md` 的「离线 / 非麦」）：
  [1] 送法不变：`主驾说：“…”` / `副驾说：“…”` + `当时场景：<读心>`。
  [2] 同一席先收住「搞」、再收住「搞好了还蛮有意思」：记忆只写一刀，内容是并好的完整句。
  [3] 单字 / 半句 / 纯应声（「哦」「嗯」）：判断为不写，记忆不新增。
  [4] 完整且有明确事实 / 人名的一句：收住并等这次读心落地后，判断为要写；页面胶囊显示查回原文。
  [5] 说话还在进行中的读心：不写记忆（只更新旁白）。
  [6] 只有画面变化、没有新的收住：不写记忆。
  [7] 同一份读心结果 / 同一句不会写两次；判断失败 / 超时：宁可不写。
  [8] 记忆没接（空实现）时页面照常跑，不抛、不假装记下。

运行：`python3 -B tests/memory/test_neixinxi_memory.py`（纯离线）
"""
from __future__ import annotations

import os
import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ui"))
sys.path.insert(0, str(ROOT / "src"))
sys.dont_write_bytecode = True

for candidate in ("/Users/YUYSONG/Work/tmp/jev-reopt-checkouts/dev-streaming-eot",
                  "/Users/YUYSONG/Agents/navi-dual-system"):
    if not os.environ.get("POC_UPSTREAM") and (Path(candidate) / "model_clients.py").is_file():
        os.environ["POC_UPSTREAM"] = candidate

import server  # noqa: E402
from memory.memos import MemosMemoryStore  # noqa: E402


class FakeTransport:
    """只记账的 memos 传输（不连外网）：`posts` 里留每次请求体，测试直接看送了什么。"""

    def __init__(self):
        self.posts = []

    def post(self, path, payload, timeout_s=None):
        self.posts.append({"path": path, "payload": payload})
        return {"code": 0, "data": {"task_id": "fake-task"}}


class FakeMemory:
    """假记忆：写只记账，查回固定几条（模拟「记忆里查回来的内容」）。"""

    enabled = True

    def __init__(self, hits=("副驾是张先生",), enabled=True):
        self.hits = list(hits) if enabled else []
        self.enabled = enabled
        self.writes = []
        self.recalls = []
        self.last_recall = {}

    def write(self, turn):
        self.writes.append(dict(turn or {}))
        return {"status": "queued", "seat": (turn or {}).get("seat"),
                "text": (turn or {}).get("text"), "scene": (turn or {}).get("scene"),
                "latency_ms": 1.0, "error": None}

    def recall(self, t_ms=None, limit=5, query=""):
        self.recalls.append({"query": query, "t_ms": t_ms})
        self.last_recall = {"status": "ok", "query": query, "count": len(self.hits),
                            "latency_ms": 2.0, "error": None}
        return list(self.hits)[:limit]

    def remember_request(self, request):
        return {}

    def status(self):
        return {"enabled": True, "kind": "fake", "user_id": "test", "key_source": "test"}


class FakeMindEngine:
    """假读心引擎：结果回来得可控（`release` 没放开就一直挂在途）。

    用来验真模型路径的两条时机：**收住触发的读心落地前不写**；落地时这席还在说话也不写。
    """

    model = "fake-mind"

    def __init__(self, blocked=False):
        self.release = threading.Event()
        self.calls = []
        if not blocked:
            self.release.set()

    def block(self):
        self.release.clear()

    def unblock(self):
        self.release.set()

    def judge(self, *, text, visual, seat="D", final=False, image=None, image_w=None,
              image_h=None, prior_texts=None, speech=None, prior_results=None):
        closed = ""
        closed_seat = seat
        for lane_seat, lane in (speech or {}).items():
            if lane.get("settled"):
                closed, closed_seat = lane["settled"], lane_seat
        self.calls.append({"text": text, "seat": seat, "closed": closed})
        self.release.wait(timeout=10)
        label = {"D": "主驾", "P": "副驾"}.get(closed_seat, closed_seat)
        scene = "%s刚说「%s」" % (label, closed) if closed else "这一刻没人说话"
        return {"result": {"scene": scene, "intent": "闲聊", "target": "无", "topic": "闲聊",
                           "cues": [], "confidence": 0.8, "reason": "fake"},
                "engine": {"name": self.model, "model": self.model, "mode": "litellm_vl",
                           "source": "fake", "fallback": False},
                "frame": None}


class MemoryPayloadWording(unittest.TestCase):
    """[1] 送法：`主驾说：“…”` / `副驾说：“…”` + `当时场景：…`（与对话流页共用同一处）。"""

    def test_payload_is_say_quote_plus_scene(self):
        store = MemosMemoryStore(session_id="t", api_key="fake-key", key_source="test",
                                 transport=FakeTransport())
        payload = store._write_payload("张先生，我给您介绍一下。", "D",
                                       {"summary": "副驾是张先生", "revision": 3})
        self.assertEqual(payload["messages"][0],
                         {"role": "user", "content": "主驾说：“张先生，我给您介绍一下。”"})
        self.assertEqual(payload["messages"][1],
                         {"role": "assistant", "content": "当时场景：副驾是张先生"})

    def test_seat_prefix_is_副驾_for_p(self):
        store = MemosMemoryStore(session_id="t", api_key="fake-key", key_source="test",
                                 transport=FakeTransport())
        payload = store._write_payload("好的。", "P", {})
        self.assertEqual(payload["messages"], [{"role": "user", "content": "副驾说：“好的。”"}],
                         "副驾这一席也写成「副驾说：“…”」，不带旧前缀")
        self.assertEqual(payload["tags"], ["seat:P", "poc-streaming-eot"])


class NeixinxiMemoryWrite(unittest.TestCase):
    """[2]~[8] 内心戏页这一侧：收住只是有资格；读心落地 + 快判断通过才写一刀。"""

    def setUp(self):
        server.MIND_FORCE_OFFLINE = True             # 默认读心走离线规则替身：不连模型
        server.NEIXINXI_MEMORY_RECALL_AT_S = (0.2, 0.5)   # 召回轮询在测试里加速
        server.NEIXINXI_MEMORY_CLOSE_WAIT_S = 3.0         # 等读心的上限在测试里缩短
        self.memory = FakeMemory()
        server._hub_memory = lambda session_id, **kwargs: self.memory

    def tearDown(self):
        server.MIND_FORCE_OFFLINE = False
        server.NEIXINXI_MEMORY_RECALL_AT_S = (4, 8, 15, 25, 40, 60, 90, 120)
        server.NEIXINXI_MEMORY_CLOSE_WAIT_S = 8.0

    def zoom(self, model="deepseek-v4.1-flash", eot_mode="offline"):
        room = server.NeixinxiZoom("test-neixinxi-memory", model=model, eot_mode=eot_mode)
        room.events = []
        room._on_event = room.events.append     # 事件改记在测试里（不推 WS）
        return room

    @staticmethod
    def wait_for(predicate, timeout=3.0):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return False

    def events(self, room, kind):
        return [event for event in room.events if event.get("event") == kind]

    # ---- [2] 碎片先攒再并：两刀碎片只写一刀、内容是完整那版 ----

    def test_fragments_are_merged_and_only_one_cut_is_written(self):
        room = self.zoom()
        room.say_text("D", "搞")
        room.say_text("D", "搞好了还蛮有意思")
        self.assertTrue(self.wait_for(lambda: len(self.memory.writes) == 1),
                        "并好之后写一刀（%s）" % self.memory.writes)
        self.assertEqual([write["text"] for write in self.memory.writes],
                         ["搞好了还蛮有意思"],
                         "只写并好的完整句，不写「搞」那一刀碎片")
        self.assertEqual(self.memory.writes[0].get("seat"), "D")
        write_event = self.events(room, "memory_write")[-1]
        self.assertEqual(write_event.get("parts"), ["搞", "搞好了还蛮有意思"],
                         "回执里写清这一刀由哪几段并成")
        self.assertEqual(write_event.get("merged_text"), "搞好了还蛮有意思")
        self.assertIsNotNone(self.memory.writes[0].get("scene"), "记忆要带上这一刻的场景")

    # ---- [3] 单字 / 半句 / 纯应声：判断为不写，记忆不新增 ----

    def test_short_fragments_and_backchannels_are_not_written(self):
        for text in ("哦", "嗯", "搞", "我给您介"):
            with self.subTest(text=text):
                room = self.zoom()
                room.say_text("D", text)
                self.assertTrue(self.wait_for(lambda: self.events(room, "memory_skip")),
                                "要看得见「判断为不写」（%s）" % text)
                self.assertEqual(self.memory.writes, [], "这一刀不该写（%s）" % text)
                skip = self.events(room, "memory_skip")[-1]
                self.assertFalse(skip.get("write_score"),
                                 "离线快判断：不写（%s）" % text)

    # ---- [4] 完整一句：收住 → 读心落地 → 判断要写；页面显示查回原文 ----

    def test_full_sentence_writes_one_cut_and_page_shows_recall(self):
        room = self.zoom()
        room.say_text("D", "张先生，我给您介绍一下。")
        self.assertTrue(self.wait_for(lambda: len(self.memory.writes) == 1), "完整一句要写一刀")
        written = self.memory.writes[0]
        self.assertEqual(written.get("text"), "张先生，我给您介绍一下。")
        self.assertIn("张先生", (written.get("scene") or {}).get("summary") or "",
                      "场景是这一句的读心（人名以这一刻读心为准）")
        self.assertTrue(self.events(room, "memory_write"), "页面要收到「写入」回执")
        self.assertTrue(self.wait_for(lambda: self.events(room, "memory_recall")),
                        "页面要收到「记忆里查回」")
        recall = self.events(room, "memory_recall")[-1]
        self.assertEqual(recall.get("items"), ["副驾是张先生"],
                         "页面显示的是记忆查回来的内容，不是把旁白再贴一次")
        self.assertEqual(recall.get("count"), 1)
        self.assertTrue(self.memory.recalls, "查回是真查（带并好的问法）")

    # ---- [5] 说话还在进行中的读心：只更新旁白，不送判断、不写 ----

    def test_mind_result_while_still_speaking_does_not_write(self):
        room = self.zoom(eot_mode="real")
        engine = FakeMindEngine(blocked=True)
        room.mind.set_engine(engine)
        room.memory_judge = lambda text, seat, scene, prev: {
            "write": True, "score": 0.95, "threshold": 0.65,
            "source": "测试", "reason": "测试用判断：写", "wall_ms": 1.0}
        room.say_text("D", "张先生，我给您介绍一下。")
        self.assertTrue(self.wait_for(lambda: engine.calls), "这句收住要触发一次读心")
        room.feed_mind("D", "他姓张", final=False)      # 还在说：这句收住的读心结果落地时这席仍在说话
        engine.unblock()
        self.assertTrue(self.wait_for(lambda: self.events(room, "memory_skip")), "要记「为什么没写」")
        skip = self.events(room, "memory_skip")[-1]
        self.assertEqual(skip.get("reason"), "still_speaking",
                         "说话还在进行中的读心：不送判断、不写记忆")
        self.assertEqual(self.memory.writes, [])

    # ---- [6] 只有画面变化、没有新的收住：不写 ----

    def test_frame_change_alone_never_writes(self):
        room = self.zoom(eot_mode="real")
        engine = FakeMindEngine()
        room.mind.set_engine(engine)
        room.put_frame({"image": "data:image/jpeg;base64,AAAA", "w": 320, "h": 180,
                        "brightness": 0.5, "motion": 0.3})
        server._mind_ingest(room.mind, room.frame_latest, None, False)
        self.assertTrue(self.wait_for(lambda: engine.calls), "画面变化照样出旁白（读心读一次）")
        time.sleep(0.3)
        self.assertEqual(self.memory.writes, [],
                         "只有画面变化、没有新的收住：这一刀默认不写")

    # ---- [7] 同一份读心结果 / 同一句不写两次；判断失败不写 ----

    def test_same_sentence_is_written_once(self):
        room = self.zoom()
        room.say_text("D", "张先生，我给您介绍一下。")
        self.assertTrue(self.wait_for(lambda: len(self.memory.writes) == 1), "第一刀")
        room.say_text("D", "张先生，我给您介绍一下。")
        self.assertTrue(self.wait_for(lambda: len(self.events(room, "memory_skip")) == 1),
                        "同一句再收一次：判断为「和上一刀重复」")
        time.sleep(0.2)
        self.assertEqual(len(self.memory.writes), 1, "同一句不重写")

    def test_two_rapid_closes_with_real_mind_write_only_one_cut(self):
        room = self.zoom(eot_mode="real")
        engine = FakeMindEngine(blocked=True)
        room.mind.set_engine(engine)
        room.memory_judge = lambda text, seat, scene, prev: {
            "write": True, "score": 0.95, "threshold": 0.65,
            "source": "测试", "reason": "测试用判断：写", "wall_ms": 1.0}
        room.say_text("D", "张先生，我给您介绍一下。")
        room.say_text("D", "张先生，我给您介绍一下。")
        engine.unblock()
        self.assertTrue(self.wait_for(lambda: len(self.memory.writes) == 1), "只写一刀")
        time.sleep(0.4)                              # 第二份读心结果也可能落地：不再写第二刀
        self.assertEqual(len(self.memory.writes), 1, "同一份读心结果 / 同一句只写一刀")

    def test_judge_failure_does_not_write(self):
        room = self.zoom()

        def boom(text, seat, scene, prev):
            raise RuntimeError("8009 timeout")

        room.memory_judge = boom
        room.say_text("D", "张先生，我给您介绍一下。")
        self.assertTrue(self.wait_for(lambda: self.events(room, "memory_skip")))
        skip = self.events(room, "memory_skip")[-1]
        self.assertEqual(skip.get("reason"), "judge_error",
                         "判断失败 / 超时：宁可不写，不拿回落编一刀记忆")
        self.assertEqual(self.memory.writes, [])

    def test_two_seats_nearly_same_utterance_is_indistinguishable(self):
        room = self.zoom()
        with room.memory_lock:              # 另一席刚收住的是同一句：分不清是谁说的
            room.memory_close["P"] = {"text": "张先生，我给您介绍一下。",
                                      "norm": "张先生我给您介绍一下",
                                      "at_ms": room.now_ms(), "gen": 99}
        room.memory_judge = lambda text, seat, scene, prev: {
            "write": True, "score": 0.95, "threshold": 0.65,
            "source": "测试", "reason": "测试用判断：写", "wall_ms": 1.0}
        room.say_text("D", "张先生，我给您介绍一下。")
        self.assertTrue(self.wait_for(lambda: self.events(room, "memory_skip")))
        skip = self.events(room, "memory_skip")[-1]
        self.assertEqual(skip.get("reason"), "leak_indistinguishable",
                         "两席几乎是同一句、分不清是谁说的：不写（也不送判断）")
        self.assertEqual(self.memory.writes, [])
        self.assertEqual(self.memory.recalls, [], "没写就不查")

    def test_other_seat_shorter_fragment_does_not_block_the_complete_one(self):
        room = self.zoom()
        with room.memory_lock:              # 另一席只收住了一截：本席这版更完整，像本席
            room.memory_close["P"] = {"text": "搞好了还蛮", "norm": "搞好了还蛮",
                                      "at_ms": room.now_ms(), "gen": 99}
        room.say_text("D", "搞好了还蛮有意思")
        self.assertTrue(self.wait_for(lambda: len(self.memory.writes) == 1), "完整的这一路要写")
        self.assertEqual(self.memory.writes[0].get("text"), "搞好了还蛮有意思")

    # ---- [8] 记忆没接（空实现）时页面照常跑 ----

    def test_no_memory_wired_still_runs(self):
        self.memory = FakeMemory(enabled=False)
        server._hub_memory = lambda session_id, **kwargs: self.memory
        room = self.zoom()
        room.say_text("P", "好的。")
        self.assertTrue(any(e.get("event") == "asr_update" for e in room.events),
                        "记忆没开也照常出字 / 判停 / 读心")
        self.assertEqual(self.memory.writes, [], "记忆没开：不假装记下")


if __name__ == "__main__":
    unittest.main(verbosity=2)
