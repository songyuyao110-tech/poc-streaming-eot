"""读心模块离线验收：ASR + 视频帧 → **只产当前场景**（不判停、不判接不接）。

口径：wiki `[[中枢并发闸门]]`（2026-11-10 拍板）。读心的产物只有场景：
- 出字或画面一变就更新（`revision` + `updated_at_ms`），不等待中枢锁字；
- 中枢取「此刻已有的那份」时用 `scene_snapshot()`：**不调模型、不重算**，并带 `age_ms`；
- 没有「要不要响应 / 进不进 Agent / 已消费 / 旁听」这些字段——那是中枢的结论。

不连任何外部服务（真模型通路用假 engine 钉调度契约）。

运行：python3 -B tests/test_mind_reading.py   （本仓库无 pytest，用标准库 unittest）
"""
from __future__ import annotations

import json
import base64
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mind.mind_reading import DEMOS, MindReading, MindRegistry
# noqa: E402

def replay(demo_id, seat=None):
    demo = next(item for item in DEMOS if item["id"] == demo_id)
    session = MindReading(seat=seat or demo["seat"], session_id="test-" + demo_id)
    snaps = []
    for step in demo["script"]:
        snaps.append(session.ingest(text=step["text"], final=step["final"]))
    return session, snaps, demo


def jpeg(tag: str) -> str:
    """一张「页面采好的彩色帧」：读心只认 data URI 这一段，本测试不关心真像素内容。"""
    return "data:image/jpeg;base64," + base64.b64encode(("jpeg-" + tag).encode()).decode()


def frame(tag="f", brightness=0.8, motion=0.0):
    return {"image": jpeg(tag), "w": 320, "h": 180,
            "brightness": brightness, "motion": motion}


def feed(session, f, **kwargs):
    """把一帧按新协议喂进读心（image + 尺寸 + 同一帧算出的统计）。"""
    return session.ingest(image=f["image"], width=f["w"], height=f["h"],
                          brightness=f["brightness"], motion=f["motion"], **kwargs)


NO_JUDGEMENT_FIELDS = ("decision", "overhear")


class SceneOnly(unittest.TestCase):
    def test_snapshot_has_no_gate_conclusion(self):
        """读心快照里不能再有「接不接 / 进不进 Agent」的结论（那是中枢的）。"""
        session, snaps, _ = replay("chat")
        last = snaps[-1]
        for key in NO_JUDGEMENT_FIELDS:
            self.assertNotIn(key, last, f"读心快照不该再有 {key}")
        self.assertNotIn("consumed", last["asr"], "读心不再记「已消费」，那是中枢开的闸")
        self.assertNotIn("need_response", last["scene"])
        self.assertNotIn("downstream", last["scene"])

    def test_chat_scene_points_at_passenger(self):
        session, snaps, _ = replay("chat")
        last = snaps[-1]
        self.assertEqual(last["scene"]["target"], "乘客")
        self.assertEqual(last["scene"]["intent"], "对人闲聊")
        # 场景在 partial 阶段就已在刷新，不是等 final 才动
        self.assertTrue(snaps[0]["scene"]["summary"].startswith("主驾说「"), snaps[0]["scene"]["summary"])

    def test_command_scene_points_at_car(self):
        session, snaps, _ = replay("command")
        last = snaps[-1]
        self.assertEqual(last["scene"]["intent"], "导航")
        self.assertEqual(last["scene"]["target"], "车机")
        nav_snap = next(s for s in snaps
                        if s["asr"]["finals"] and s["asr"]["finals"][-1]["text"] == "导航去望京SOHO")
        self.assertEqual(nav_snap["scene"]["topic"], "望京SOHO")
        self.assertGreaterEqual(last["scene"]["confidence"], 0.75)

    def test_partial_is_tentative_then_final_locks(self):
        session = MindReading(seat="D", session_id="test-partial")
        snap = session.ingest(text="导航去望京", final=False)
        self.assertFalse(snap["scene"]["finalized"])
        self.assertLessEqual(snap["scene"]["confidence"], 0.72, "出字中场景置信度封顶")
        self.assertEqual(snap["asr"]["partial"], "导航去望京")
        snap = session.ingest(text="导航去望京SOHO", final=True)
        self.assertTrue(snap["scene"]["finalized"])
        self.assertEqual(snap["asr"]["partial"], "")
        self.assertEqual(len(snap["asr"]["finals"]), 1)

    def test_scene_revision_follows_input_changes(self):
        session = MindReading(seat="D", session_id="test-revision")
        rev0 = session.snapshot()["scene"]["revision"]
        session.ingest(text="导航去", final=False)
        rev1 = session.snapshot()["scene"]["revision"]
        session.ingest(text="导航去望京", final=False)
        rev2 = session.snapshot()["scene"]["revision"]
        self.assertGreater(rev1, rev0, "出字一变场景版本就 +1")
        self.assertGreater(rev2, rev1)
        # 同一版字重复进来不再涨版本
        session.ingest(text="导航去望京", final=False)
        self.assertEqual(session.snapshot()["scene"]["revision"], rev2)

    def test_video_frames_reach_scene_understanding(self):
        session = MindReading(seat="D", session_id="test-frame")
        dark = frame("dark", brightness=0.03)
        bright = frame("bright", brightness=0.94, motion=0.6)
        snap = feed(session, dark)
        self.assertTrue(snap["scene"]["visual"]["has_frame"])
        self.assertLess(snap["scene"]["visual"]["brightness"], 0.1)
        snap = feed(session, bright)
        self.assertGreater(snap["scene"]["visual"]["brightness"], 0.9, "后一帧亮度应刷新")
        self.assertGreater(snap["scene"]["visual"]["motion"], 0.5, "换帧应体现运动量")
        self.assertIn("画面", snap["scene"]["summary"])

    def test_negation_and_environment_scene(self):
        session = MindReading(seat="D", session_id="test-negation")
        snap = session.ingest(text="我不是跟你说话，导航去公司", final=True)
        self.assertEqual(snap["scene"]["target"], "乘客", "明确否认 → 场景是「和同乘说话」")
        self.assertIn("否认", snap["scene"]["basis"])

        empty = feed(MindReading(seat="D", session_id="test-silence"), frame("silence", 0.0))
        self.assertEqual(empty["scene"]["intent"], "无语音")
        self.assertLess(empty["scene"]["confidence"], 0.4)

    def test_discussion_scene_keeps_both_cues(self):
        session = MindReading(seat="D", session_id="test-discuss")
        snap = session.ingest(text="你说导航去公司要不要走高速", final=True)
        self.assertIn("导航", snap["scene"]["cues"], "讨论里的车控词要作为场景线索留下")
        self.assertTrue(snap["scene"]["summary"].startswith("主驾刚说「"), "场景由读心给，不在这里判接不接")


class HumanSceneLine(unittest.TestCase):
    """读心产物是一句车舱旁白（v04 口吻）：≤30 字、不第一人称、不自称模块、逐版都刷新。"""

    def test_offline_scenes_are_human_one_liners(self):
        session = MindReading(seat="D", session_id="test-human")
        for text in ("导航去望京SOHO", "你说今天这天气可真舒服",
                     "帮我看看前面那个", "我不是跟你说话，导航去公司",
                     "把空调调到24度然后把座椅加热打开再放一首周杰伦的歌"):
            snap = session.ingest(text=text, final=True)
            summary = snap["scene"]["summary"]
            self.assertTrue(summary.startswith("主驾"), summary)
            self.assertLessEqual(len(summary), 30, summary)
            self.assertFalse(summary.startswith("我"), "不第一人称开场：" + summary)
            for banned in ("读心", "模块", "引擎", "感知", "我看到", "我听到", "我知道"):
                self.assertNotIn(banned, summary, "场景句不出现旧说法/模块自称：" + summary)


class SceneSnapshotForHub(unittest.TestCase):
    """中枢取「读心此刻已有的那份」：一次快照、不调模型、带新鲜度；读心自己跑自己的。"""

    class CountingEngine:
        model = "fake"

        def __init__(self):
            self.calls = 0

        def describe(self):
            return {"name": "fake", "mode": "litellm_vl", "source": "llm"}

        def judge(self, *, text, visual, seat="D", final=False, image=None, image_w=None,
                  image_h=None, prior_texts=None):
            self.calls += 1
            return {"result": {"scene": f"模型场景：{text}", "intent": "导航", "target": "车机",
                               "topic": "—", "confidence": 0.9, "reason": "fake", "cues": [],
                               "finalized": final},
                    "engine": {"name": "fake", "model": "fake", "mode": "litellm_vl",
                               "source": "llm", "fallback": False}}

    def test_scene_snapshot_does_not_call_model(self):
        engine = self.CountingEngine()
        session = MindReading(seat="D", session_id="hub-1", engine=engine)
        self.assertIsNone(session.scene_snapshot(), "真模型还没读过时，不假装有场景")
        session.ingest(text="导航去公司", final=True)
        session.on_utterance_close("D")          # 中枢把这句收住 → 读心读一次
        import time
        end = time.time() + 2.0
        while time.time() < end and session.snapshot()["scene"]["pending"]:
            time.sleep(0.02)
        calls = engine.calls
        scene = session.scene_snapshot()
        self.assertIsNotNone(scene)
        self.assertEqual(engine.calls, calls, "取已有场景不触发模型重算")
        self.assertEqual(scene["revision"], session.snapshot()["scene"]["revision"])
        self.assertGreaterEqual(scene["age_ms"], 0)
        self.assertEqual(scene["seat_label"], "主驾")

    def test_pending_placeholder_is_not_a_scene_for_hub(self):
        """真模型还没回话时的「读场景中…」占位句不能当中枢的场景输入。"""
        import threading
        hold = threading.Event()

        class SlowEngine(SceneSnapshotForHub.CountingEngine):
            def judge(self, **kwargs):
                hold.wait(1.0)
                return super().judge(**kwargs)

        engine = SlowEngine()
        session = MindReading(seat="D", session_id="hub-pending", engine=engine)
        session.ingest(text="导航去公司", final=True)
        session.on_utterance_close("D")          # 中枢把这句收住 → 才发请求
        self.assertIn("读场景中", session.snapshot()["scene"]["summary"], "页面上仍显示占位句")
        self.assertIsNone(session.scene_snapshot(),
                          "占位句不是场景：中枢按无场景照常判，不拿它当判据")
        hold.set()

    def test_scene_updates_do_not_wait_for_hub(self):
        session = MindReading(seat="D", session_id="hub-2")
        first = session.scene_snapshot()
        self.assertIsNotNone(first, "规则替身随时能给出「暂无语音」这种场景")
        self.assertEqual(first["intent"], "无语音")
        session.ingest(text="导航去公司", final=False)
        scene_a = session.scene_snapshot()
        self.assertIsNotNone(scene_a)
        session.ingest(text="导航去公司南门", final=False)
        scene_b = session.scene_snapshot()
        self.assertGreater(scene_b["revision"], scene_a["revision"], "出字一变，读心自己就更新场景")


class SeatIsolation(unittest.TestCase):
    def test_scenes_never_cross_seats(self):
        registry = MindRegistry()
        d = registry.get("s1", seat="D")
        p = registry.get("s2", seat="P")
        d.ingest(text="咱晚上去哪吃", final=True)
        p.ingest(text="导航去公司", final=True)
        self.assertIn("咱晚上去哪吃", d.snapshot()["scene"]["summary"])
        self.assertIn("导航去公司", p.snapshot()["scene"]["summary"])
        self.assertIn("主驾", d.snapshot()["scene"]["summary"])
        self.assertIn("副驾", p.snapshot()["scene"]["summary"])
        self.assertEqual(p.scene_snapshot()["seat"], "P", "只有副驾出字时场景就属于副驾")

    def test_registry_keeps_one_mind_per_session(self):
        """同一个 session_id 只有一份读心（两路共用）：换席不重建、也不丢另一路已经出到的字。"""
        registry = MindRegistry()
        mind = registry.get("same", seat="D")
        mind.ingest(text="导航去公司", final=True, seat="D")
        again = registry.get("same", seat="P")
        self.assertIs(mind, again, "同一会话换席拿到的是同一份读心")
        self.assertEqual(again.snapshot()["seat"], "P", "默认席跟着这次问的那一席（调试页一次推一路）")
        self.assertEqual(again.snapshot()["seats"]["D"]["finals"][0]["text"], "导航去公司",
                         "另一路已经出到的字不因为换席被清掉")
        again.ingest(text="我听说望京新开了一家火锅", final=True, seat="P")
        scene = again.scene_snapshot()
        self.assertEqual(len(scene["seats"]), 2, "这一份场景里两路都在（不是各席各一份）")
        self.assertIn("导航去公司", scene["reading"])
        self.assertIn("我听说望京新开了一家火锅", scene["reading"])


class DualAsr(unittest.TestCase):
    """两路 ASR 输入：一份读心、一份场景，两路都在（2026-11-11 12:52 拍板）。"""

    def test_one_read_carries_both_lanes(self):
        """两路同时出字：同一份场景里主驾、副驾两路都在，且标了席别。"""
        session = MindReading(session_id="dual-both")
        session.ingest(seat="D", text="那就去那家", final=False)
        snap = session.ingest(seat="P", text="咱晚上去哪吃", final=False)
        scene = snap["scene"]
        self.assertEqual([lane["seat"] for lane in scene["seats"]], ["D", "P"])
        self.assertEqual(scene["seat"], "shared", "两路都在：这一份场景是两席的，不再各席一份")
        self.assertEqual(scene["seat_label"], "主驾和副驾")
        self.assertIn("主驾「那就去那家」", scene["reading"])
        self.assertIn("副驾「咱晚上去哪吃」", scene["reading"])
        summary = scene["summary"]
        self.assertNotIn("我听到", summary, "v04 口吻不写「我听到」")
        self.assertFalse(summary.startswith("我"), summary)
        self.assertIn("主驾", summary)
        self.assertIn("副驾", summary, "两路各自说的都要写进这一句场景")
        self.assertLessEqual(len(summary), 30)

    def test_single_lane_is_byte_identical_to_one_seat(self):
        """单席开口：另一路空着，行为与「只有一路在说」一致（场景只写那一路）。"""
        session = MindReading(session_id="dual-single")
        snap = session.ingest(seat="D", text="导航去望京SOHO", final=True)
        scene = snap["scene"]
        self.assertEqual(scene["seat"], "D")
        self.assertEqual(scene["seat_label"], "主驾")
        self.assertTrue(scene["summary"].startswith("主驾刚说「"), scene["summary"])
        self.assertNotIn("副驾", scene["summary"], "另一路没出字：不假装有字、也不写进场景")
        self.assertEqual(scene["seats"][1]["seat"], "P")
        self.assertFalse(scene["seats"][1]["speaking"], "副驾这一路是空的")
        self.assertEqual(scene["seats"][1]["text"], "")

    def test_no_lane_reads_the_picture_only(self):
        """两路都没出字：只凭画面读，不编一句人话进来。"""
        session = MindReading(session_id="dual-silent")
        snap = feed(session, frame("quiet", 0.05))
        self.assertEqual(snap["scene"]["intent"], "无语音")
        self.assertIn("画面", snap["scene"]["summary"])
        self.assertEqual([lane["text"] for lane in snap["scene"]["seats"]], ["", ""])
        self.assertEqual(snap["scene"]["speakers"], [])

    def test_settled_lane_rides_along_as_context_not_as_live_text(self):
        """一路刚说完、另一路还在说：同一次读心里刚落定的那句只作上下文，不冒充「这一刻出字」。"""
        session = MindReading(session_id="dual-settled")
        session.ingest(seat="P", text="咱晚上去哪吃", final=True)
        snap = session.ingest(seat="D", text="那就去那家", final=False)
        lanes = snap["scene"]["seats"]
        self.assertEqual(lanes[1]["text"], "", "副驾这一刻没出字：这一路空着")
        self.assertTrue(lanes[1]["settled"], "刚说完的那句留在上下文位")
        self.assertEqual(lanes[0]["text"], "那就去那家")
        self.assertIn("刚说完", snap["scene"]["reading"])

    def test_scene_revision_tracks_either_lane(self):
        session = MindReading(session_id="dual-rev")
        rev0 = session.snapshot()["scene"]["revision"]
        session.ingest(seat="D", text="导航去", final=False)
        rev1 = session.snapshot()["scene"]["revision"]
        session.ingest(seat="P", text="我也去", final=False)
        rev2 = session.snapshot()["scene"]["revision"]
        self.assertGreater(rev1, rev0)
        self.assertGreater(rev2, rev1, "另一路出字也要让这一份场景 +1 版")

    def test_one_model_call_carries_both_lanes(self):
        """真模型通路：一次读心只发一次请求，请求里两路出字都在（不是各席各发一次）。"""
        import time

        class DualFake:
            model = "cc/glm-5.2"

            def __init__(self):
                self.calls = []

            def describe(self):
                return {"name": self.model, "mode": "litellm_vl", "source": "llm"}

            def judge(self, *, text, visual, seat="D", final=False, image=None, image_w=None,
                      image_h=None, prior_texts=None, speech=None):
                self.calls.append({"speech": dict(speech or {}), "text": text,
                                   "prior": list(prior_texts or [])})
                return {"result": {"scene": "主驾和副驾都在说话，聊得挺热闹", "intent": "对人闲聊",
                                   "target": "乘客", "topic": "—", "confidence": 0.7,
                                   "reason": "fake", "cues": [], "finalized": final},
                        "engine": {"name": self.model, "model": self.model, "mode": "litellm_vl",
                                   "source": "llm", "fallback": False, "latency_ms": 10.0}}

        engine = DualFake()
        session = MindReading(session_id="dual-call", engine=engine)
        session.ingest(seat="P", text="咱晚上去哪吃", final=False)
        session.ingest(seat="D", text="那就去那家", final=True)
        session.on_utterance_close("D")          # 主驾这句被中枢收住 → 读一次（副驾还在说也一起带上）
        deadline = time.time() + 2.0
        while time.time() < deadline and not engine.calls:
            time.sleep(0.02)
        self.assertEqual(len(engine.calls), 1, "一次读心 = 一次请求（两路合并，不在两席各发一次）")
        speech = engine.calls[0]["speech"]
        self.assertEqual(set(speech), {"D", "P"})
        self.assertEqual(speech["D"]["settled"], "那就去那家", "刚收住的那一句也在同一次请求里")
        self.assertFalse(speech["D"]["speaking"], "这一路这一刻不再出字：只是刚落定")
        self.assertEqual(speech["P"]["text"], "咱晚上去哪吃", "另一路这一刻的字也要在同一次请求里")
        self.assertTrue(speech["P"]["speaking"])
        deadline = time.time() + 2.0
        while time.time() < deadline and session.snapshot()["scene"]["pending"]:
            time.sleep(0.02)
        snap = session.snapshot()
        self.assertFalse(snap["scene"]["pending"])
        self.assertEqual(snap["scene"]["summary"], "主驾和副驾都在说话，聊得挺热闹")

    def test_dual_hub_snapshot_is_one_shared_scene(self):
        """中枢两席取到的是同一份场景（不是各取各的、互相看不见对方的话）。"""
        session = MindReading(session_id="dual-hub")
        session.ingest(seat="D", text="那就去那家", final=False)
        session.ingest(seat="P", text="咱晚上去哪吃", final=True)
        scene_d = session.scene_snapshot()
        scene_p = session.scene_snapshot()
        self.assertEqual(scene_d["revision"], scene_p["revision"])
        self.assertEqual(scene_d["summary"], scene_p["summary"])
        self.assertEqual(scene_d["seat"], "shared")
        self.assertEqual(len(scene_d["seats"]), 2, "这一份场景里两路都在")


class EngineNeutrality(unittest.TestCase):
    def test_snapshot_is_json_and_engine_labelled(self):
        snap = MindReading(seat="D", session_id="test-track").ingest(text="导航去公司", final=True)
        self.assertEqual(snap["engine"]["mode"], "offline")
        self.assertEqual(json.loads(json.dumps(snap))["scene"]["intent"], "导航", "快照可 JSON 序列化")
        self.assertIn("revision", snap["scene"])
        self.assertIn("updated_at_ms", snap["scene"])


class RealModelPath(unittest.TestCase):
    """真模型主路径：用假 engine 钉住调度 / 主路径 / 失败回落三条契约（不连外部服务）。"""

    class FakeLLM:
        model = "cc/glm-5.2"

        def __init__(self, error=None, block=None):
            self.calls = []
            self.error = error
            self.block = block

        def describe(self):
            return {"name": self.model, "mode": "litellm_vl", "source": "llm",
                    "vision": {"status": "unknown"}}

        def judge(self, *, text, visual, seat="D", final=False, image=None, image_w=None,
                  image_h=None, prior_texts=None):
            self.calls.append({"text": text, "final": final, "image": image, "visual": dict(visual),
                               "prior_texts": prior_texts})
            if self.block is not None:
                self.assert_event_wait()
            if self.error is not None:
                raise self.error
            nav = "导航" in text
            return {"result": {
                "scene": f"模型看到{'导航指令' if nav else '有人在闲聊'}",
                "intent": "导航" if nav else "对人闲聊",
                "target": "车机" if nav else "乘客",
                "topic": "望京SOHO" if nav else "—",
                "confidence": 0.88 if nav else 0.71,
                "reason": "模型场景依据",
                "cues": ["导航"] if nav else [], "finalized": final,
            }, "engine": {"name": self.model, "model": self.model, "mode": "litellm_vl",
                          "source": "llm", "fallback": False, "latency_ms": 1234.5,
                          "vision": {"attempted": bool(image), "used": bool(image),
                                     "status": "ok" if image else "skipped", "error": None},
                          "frame": {"data_uri": image, "w": image_w, "h": image_h,
                                    "format": "jpeg", "bytes": len(image or "")}}}

        def assert_event_wait(self):
            import threading
            if not isinstance(self.block, threading.Event):
                raise RuntimeError("block must be threading.Event")
            self.block.wait(2.0)

    def wait(self, session, timeout=2.0):
        import time
        end = time.time() + timeout
        while time.time() < end:
            snap = session.snapshot()
            if not snap["scene"]["pending"]:
                return snap
            time.sleep(0.02)
        return session.snapshot()

    def test_model_is_main_path_and_scene_is_llm_sourced(self):
        fake = self.FakeLLM()
        session = MindReading(seat="D", session_id="test-llm", engine=fake)
        session.ingest(text="导航去望京SOHO", final=True, image=jpeg("120"), width=320, height=180, brightness=0.5, motion=0.0)
        session.on_utterance_close("D")
        snap = self.wait(session)
        self.assertEqual(snap["scene"]["intent"], "导航")
        self.assertEqual(snap["scene"]["source"], "llm")
        self.assertFalse(snap["scene"]["pending"])
        self.assertEqual(snap["engine"]["name"], "cc/glm-5.2")
        self.assertEqual(snap["engine"]["mode"], "litellm_vl")
        self.assertFalse(snap["engine"]["fallback"])
        self.assertEqual(fake.calls[-1]["image"], jpeg("120"), "帧（同一段 data URI）应随文本一起进真模型")
        self.assertTrue(snap["engine"]["vision"]["used"])

    def test_continuation_final_carries_seat_prior_context(self):
        fake = self.FakeLLM()
        session = MindReading(seat="D", session_id="test-context", engine=fake)
        session.ingest(text="导航去望京SOHO", final=True, image=jpeg("100"), width=320, height=180, brightness=0.5, motion=0.0)
        session.on_utterance_close("D")
        self.wait(session)
        session.ingest(text="尽量走高速，别堵在路上", final=True, image=jpeg("110"), width=320, height=180, brightness=0.5, motion=0.0)
        session.on_utterance_close("D")
        self.wait(session)
        self.assertEqual(fake.calls[-1]["prior_texts"], ["导航去望京SOHO"],
                         "同一轮的后继分句要把本席上一句当上下文送模型")

    def test_pending_does_not_claim_a_rules_scene(self):
        import threading
        block = threading.Event()
        fake = self.FakeLLM(block=block)
        session = MindReading(seat="D", session_id="test-pending", engine=fake)
        pending = session.ingest(text="导航去公司", final=True, image=jpeg("90"), width=320, height=180, brightness=0.5, motion=0.0)
        self.assertTrue(pending["scene"]["pending"], "还没被中枢收住：没有请求、也没有场景（占位句不算）")
        session.on_utterance_close("D")          # 中枢收住 → 这一拍才发请求
        self.assertTrue(session.snapshot()["scene"]["pending"], "请求发出去了，结果还在读")
        self.assertEqual(pending["scene"]["source"], "llm_pending")
        self.assertEqual(pending["scene"]["intent"], "读取中")
        block.set()
        final = self.wait(session)
        self.assertFalse(final["scene"]["pending"])
        self.assertEqual(final["scene"]["source"], "llm")

    def test_engine_failure_falls_back_with_receipt(self):
        from mind.mind_llm import MindEngineError
        fake = self.FakeLLM(error=MindEngineError("timeout", "read timeout after 60s"))
        session = MindReading(seat="D", session_id="test-fallback", engine=fake)
        session.ingest(text="咱晚上去哪吃", final=True, image=jpeg("30"), width=320, height=180, brightness=0.5, motion=0.0)
        session.on_utterance_close("D")
        snap = self.wait(session)
        self.assertEqual(snap["engine"]["mode"], "rule_fallback")
        self.assertTrue(snap["engine"]["fallback"])
        self.assertEqual(snap["engine"]["error"]["kind"], "timeout")
        self.assertEqual(snap["scene"]["source"], "rules_fallback")
        self.assertEqual(snap["scene"]["intent"], "对人闲聊")

    def test_latest_frame_is_reused_for_asr_only_tick(self):
        fake = self.FakeLLM()
        session = MindReading(seat="D", session_id="test-frame-llm", engine=fake)
        feed(session, frame("reuse", 0.2))
        session.ingest(text="导航去公司", final=True)
        session.on_utterance_close("D")
        self.wait(session)
        self.assertEqual(fake.calls[-1]["image"], jpeg("reuse"), "本拍只来 ASR 时用最近一帧")


class FrameDrivenRead(unittest.TestCase):
    """没开口也有画面：画面一变就读一次当前场景（读心不是「只有出字才工作」）。

    2026-11-11 拍板口径：读心是持续在走的通路，开口 / 不开口只要有画面都要产出；出了结果由上层
    （`ui/server.py` 的 driver）立刻推进对话流。本类只钉读心这边的调度契约（不连外部服务）。
    """

    FakeLLM = RealModelPath.FakeLLM

    def wait(self, session, timeout=2.0):
        return RealModelPath.wait(self, session, timeout)

    def test_frame_without_speech_reads_a_scene(self):
        """没出字、只来一帧：也要读一次真模型，且不编造一句人话当输入。"""
        import time
        fake = self.FakeLLM()
        session = MindReading(seat="D", session_id="frame-only", engine=fake)
        feed(session, frame("only", 0.6))
        deadline = time.time() + 2.0
        while time.time() < deadline and not fake.calls:
            time.sleep(0.02)
        self.assertTrue(fake.calls, "没开口只有画面也要调度真模型（此前这条通路根本不走）")
        self.assertEqual(fake.calls[0]["text"], "", "没开口时没有字：画面读不编一句人话进模型")
        self.assertEqual(fake.calls[0]["image"], jpeg("only"), "读的就是这一帧（同一段 data URI）")
        snap = self.wait(session)
        self.assertEqual(snap["scene"]["source"], "llm")
        self.assertFalse(snap["scene"]["pending"])
        self.assertIsNotNone(session.scene_snapshot(), "读到结果就有一份场景（占位句不算）")

    def test_frames_do_not_pile_up_requests(self):
        """连续几帧不每帧都发请求：两次画面读之间至少隔 LLM_FRAME_MIN_INTERVAL。"""
        import time
        fake = self.FakeLLM()
        session = MindReading(seat="D", session_id="frame-interval", engine=fake)
        for _ in range(6):
            feed(session, frame("pile", 0.1))
        deadline = time.time() + 2.0
        while time.time() < deadline and not fake.calls:
            time.sleep(0.02)
        time.sleep(0.2)
        self.assertEqual(len(fake.calls), 1, "同一时刻只该有一次画面读（其余帧在排队的被覆盖）")

    def test_no_frame_read_while_speaking(self):
        """还在出字时画面不单独再读一遍：那一版字自己会带着最近一帧走。"""
        import time
        fake = self.FakeLLM()
        session = MindReading(seat="D", session_id="frame-speaking", engine=fake)
        session.ingest(text="导航", final=False)
        feed(session, frame("speaking", 0.11))
        time.sleep(0.2)
        self.assertEqual(fake.calls, [], "出字期间画面读不抢跑（等这一版字自己发）")

    def test_frame_read_failure_falls_back_with_receipt(self):
        """画面读失败也照旧记账回落，不假装读出了一句真场景。"""
        from mind.mind_llm import MindEngineError
        fake = self.FakeLLM(error=MindEngineError("timeout", "read timeout"))
        session = MindReading(seat="D", session_id="frame-fallback", engine=fake)
        feed(session, frame("fallback", 0.3))
        snap = self.wait(session)
        self.assertEqual(snap["scene"]["source"], "rules_fallback")
        self.assertTrue(snap["engine"]["fallback"])
        self.assertEqual(snap["scene"]["intent"], "无语音", "画面读失败回落的是规则读出的「没听到人说话」")


class RequestTiming(unittest.TestCase):
    """请求时机（2026-11-11 13:13 拍板，wiki `[[读心请求时机]]`）：只在两处请求。

    ① **这句说完了**：中枢把这句收住（判停给说完 / 静音窗收口）→ `on_utterance_close()` 读一次，
       带最完整的字 + 当时画面；② **没在说话、画面变了**：直接读（见 `FrameDrivenRead`）。
    **出字还在变 / 字稳住一小段 / ASR 自己报了一段结束都不请求**；说话过程中不出「暂定场景」。
    """

    FakeLLM = RealModelPath.FakeLLM

    def wait(self, session, timeout=2.0):
        return RealModelPath.wait(self, session, timeout)

    def test_partial_text_alone_never_requests(self):
        """出字还在变 / 字稳住一小段：不请求（本卡撤掉「稳住一小段就读」与「按拍 tick」两条旧触发）。"""
        import time
        fake = self.FakeLLM()
        session = MindReading(seat="D", session_id="timing-partial", engine=fake)
        session.ingest(text="帮我导航去望京SOHO", final=False)
        time.sleep(0.4)                                  # 字稳住了一小段：仍然不是说完
        self.assertEqual(fake.calls, [], "没被中枢收住就不请求")
        session.ingest(text="帮我导航去望京SOHO南门", final=False)
        time.sleep(0.4)
        self.assertEqual(fake.calls, [], "出字还在变：不是请求理由")

    def test_asr_final_alone_never_requests(self):
        """ASR 自己报的一段结束不是「说完了」：不请求，等中枢收住。"""
        fake = self.FakeLLM()
        session = MindReading(seat="D", session_id="timing-final", engine=fake)
        session.ingest(text="导航去公司", final=True, image=jpeg("fin"), width=320, height=180,
                       brightness=0.5, motion=0.0)
        self.assertEqual(fake.calls, [], "ASR final 不触发读心请求")
        self.assertIsNone(session.scene_snapshot(), "没请求就没有场景")

    def test_close_reads_once_with_full_text_and_frame(self):
        """中枢收住 → 读一次：带这句最完整的字（有 final 用末稿）＋ 收住这一刻最近的那张画面。"""
        fake = self.FakeLLM()
        session = MindReading(seat="D", session_id="timing-close", engine=fake)
        session.ingest(text="导航去望京", final=False, image=jpeg("during"), width=320,
                       height=180, brightness=0.4, motion=0.0)
        session.ingest(text="导航去望京SOHO", final=True, image=jpeg("after"), width=320,
                       height=180, brightness=0.6, motion=0.0)
        session.on_utterance_close("D", why="eot_complete")
        snap = self.wait(session)
        self.assertEqual(snap["scene"]["source"], "llm")
        self.assertEqual(fake.calls[-1]["text"], "导航去望京SOHO", "用收住那一刻最完整的字")
        self.assertEqual(fake.calls[-1]["image"], jpeg("after"), "用收住那一刻的画面")
        self.assertTrue(fake.calls[-1]["final"], "这一刻不是「还在出字」")

    def test_close_reads_partial_only_utterance(self):
        """真 ASR 只吐 partial、说完就静音：中枢收住时用最后一版 partial 读，不要求 final。"""
        import time
        fake = self.FakeLLM()
        session = MindReading(seat="D", session_id="timing-partial-only", engine=fake)
        session.ingest(text="导航去公司南门", final=False, image=jpeg("partial-only"), width=320,
                       height=180, brightness=0.5, motion=0.0)
        session.on_utterance_close("D", why="max_silence")
        snap = self.wait(session)
        self.assertEqual(fake.calls[-1]["text"], "导航去公司南门")
        self.assertEqual(snap["scene"]["intent"], "导航", "用 partial 也读出了场景")

    def test_same_utterance_closed_twice_reads_once(self):
        """同一句重复收住（判停说完后静音窗再收一次 / 闸门反复取结论）：只读一次。"""
        fake = self.FakeLLM()
        session = MindReading(seat="D", session_id="timing-close-dedupe", engine=fake)
        session.ingest(text="导航去公司", final=True)
        session.on_utterance_close("D", why="eot_complete")
        self.wait(session)
        session.on_utterance_close("D", why="max_silence")
        session.on_utterance_close("D")
        self.assertEqual(len(fake.calls), 1, "同一句只读一次")

    def test_close_carries_other_lane_while_it_is_still_speaking(self):
        """另一席还在说：收住这一席时不丢另一席这一刻的字（一份场景两路都在）。"""
        import time

        class DualFake:
            model = "cc/glm-5.2"

            def __init__(self):
                self.calls = []

            def describe(self):
                return {"name": self.model, "mode": "litellm_vl", "source": "llm"}

            def judge(self, *, text, visual, seat="D", final=False, image=None, image_w=None,
                      image_h=None, prior_texts=None, speech=None):
                self.calls.append({"speech": dict(speech or {}), "final": final})
                return {"result": {"scene": "主驾和副驾都在说话，聊得挺热闹", "intent": "对人闲聊",
                                   "target": "乘客", "topic": "—", "confidence": 0.7,
                                   "reason": "fake", "cues": [], "finalized": final},
                        "engine": {"name": self.model, "model": self.model, "mode": "litellm_vl",
                                   "source": "llm", "fallback": False}}

        engine = DualFake()
        session = MindReading(session_id="timing-dual", engine=engine)
        session.ingest(seat="P", text="咱晚上去哪吃", final=False)
        session.ingest(seat="D", text="那就去那家", final=True)
        session.on_utterance_close("D")
        deadline = time.time() + 2.0
        while time.time() < deadline and not engine.calls:
            time.sleep(0.02)
        self.assertEqual(len(engine.calls), 1, "一次读心 = 一次请求")
        speech = engine.calls[0]["speech"]
        self.assertEqual(speech["D"]["settled"], "那就去那家")
        self.assertFalse(speech["D"]["speaking"], "收住的那一路标「刚说完」")
        self.assertEqual(speech["P"]["text"], "咱晚上去哪吃", "另一路还在说的字也在同一次请求里")


class EngineSwitch(unittest.TestCase):
    """页面切引擎：同一会话换 engine 时，旧模型的场景/在途不能算到新模型头上。"""

    FakeLLM = RealModelPath.FakeLLM

    def wait(self, session, timeout=2.0):
        return RealModelPath.wait(self, session, timeout)

    def make(self, model):
        fake = self.FakeLLM()
        fake.model = model
        return fake

    def test_registry_switches_engine_without_resetting_seat(self):
        a, b = self.make("cc/glm-5.2"), self.make("deepseek-v4.1-flash")
        reg = MindRegistry(engine=None)
        session = reg.get("switch-1", "D", engine=a)
        self.assertIs(session.engine, a, "每拍带进来的引擎要真正挂上")
        session.ingest(text="导航去公司", final=True, image=jpeg("80"), width=320, height=180, brightness=0.5, motion=0.0)
        session.on_utterance_close("D")
        self.wait(session)
        session = reg.get("switch-1", "D", engine=b)
        self.assertIs(session.engine, b, "同一 session 换引擎生效，不新建会话")
        self.assertEqual(len(session.finals), 1, "换模型不重置本席 ASR")
        session.ingest(text="导航去望京SOHO", final=True, image=jpeg("90"), width=320, height=180, brightness=0.5, motion=0.0)
        session.on_utterance_close("D")
        snap = self.wait(session)
        self.assertEqual(snap["engine"]["name"], "deepseek-v4.1-flash", "新场景打到新引擎")
        self.assertEqual(len(b.calls), 1)

    def test_switch_drops_old_inflight_result(self):
        import threading
        import time
        block = threading.Event()
        a = self.FakeLLM(block=block)
        a.model = "cc/glm-5.2"
        b = self.make("deepseek-v4.1-flash")
        session = MindReading(seat="D", session_id="switch-inflight", engine=a)
        session.ingest(text="导航去公司", final=True, image=jpeg("70"), width=320, height=180, brightness=0.5, motion=0.0)
        session.on_utterance_close("D")
        session.set_engine(b)            # A 还在途就换档
        block.set()
        time.sleep(0.25)                 # 给旧在途线程跑完的时间
        snap = session.snapshot()
        self.assertEqual(snap["engine"].get("model") or snap["engine"].get("name"),
                         "deepseek-v4.1-flash")
        self.assertTrue(snap["scene"]["pending"], "旧在途结果作废后，页面等新引擎的场景")
        self.assertEqual(snap["scene"]["source"], "llm_pending", "旧引擎结果作废，页面显示新引擎的待读占位")
        self.assertIsNone(session.scene_snapshot(),
                          "旧引擎结果不作数、新引擎还没读：中枢按无场景照常判，不拿占位句")
        self.assertIn("deepseek-v4.1-flash", snap["scene"]["summary"])
        session.ingest(text="导航去公司", final=True, image=jpeg("70"), width=320, height=180, brightness=0.5, motion=0.0)
        snap2 = self.wait(session)
        self.assertEqual(snap2["engine"]["name"], "deepseek-v4.1-flash")


if __name__ == "__main__":
    unittest.main(verbosity=2)
