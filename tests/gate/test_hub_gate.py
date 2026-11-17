"""对话中枢 · 并发闸门离线验收（不连 8009 / 不连 LLM）。

钉住 wiki `[[中枢并发闸门]]`（2026-11-10 拍板）的可验部分：

1. **同一版出字上两路并发**：判停（说没说完）与接不接同时在飞，不是先判完一个再判另一个；
   闸门用的场景是**发起那一拍取的一次快照**（`scene_fn` 每版只被调一次）。
2. **开闸只认「说完」那一刻那一份接不接**：
   - 说完 + 要接 → `agent`（主链才排工具披露 / 决策）
   - 说完 + 不接 → `context_only`（只进上下文，不进 Agent）
   - 没说完 → `wait`（先不开闸，中间的接不接不算数）
3. **失败 / 版本不齐 / 超时 → 不进 Agent**（界面可见，不静默放行）。
4. **无场景照常判**（缺场景 ≠ 缺判断），且中枢**绝不为等场景挂起**。
5. 主链（`TurnSession(gate=...)`）：开闸前不进 Agent（工具 / 决策一行都不发）；「只进上下文」
   的文本封口后可继续听，不复活成待执行请求。

运行：python3 -B tests/test_hub_gate.py
"""
from __future__ import annotations

import sys
import threading
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from gate.hub_accept import ACCEPT_MIN_SCORE, accept_meta, build_accept_fn, rule_accept
# noqa: E402
from gate.hub_gate import GateKeeper
# noqa: E402
from mind.mind_reading import MindReading
# noqa: E402
from gate.turn_session import TurnConfig, TurnSession
# noqa: E402

def eot_verdict(label="complete", may_confirm=True, latency=60.0):
    return {"label": label, "may_confirm": may_confirm, "may_start_early": may_confirm,
            "complete": 0.94 if label == "complete" else 0.10,
            "cut": 0.04, "wait": 0.02, "confidence": 0.91, "latency_ms": latency}


def wait_accept(gate, version=1, timeout=1.5):
    """等接不接这一路回来（它是并发 worker，不是同步调用）：返回最近一次 poll 的 now_ms。"""
    end = time.time() + timeout
    now = 0
    while time.time() < end:
        now += 40
        gate.poll(now)
        if gate.snapshot()["accept"]["state"] != "pending":
            return now
        time.sleep(0.01)
    return now


def accept_outcome(accept, latency=0.0, source="测试", model="fake/accept"):
    return {"accept": accept, "confidence": 0.9, "reason": "测试用接不接结论",
            "source": source, "model": model, "latency_ms": latency}


class SceneSource:
    """一块「读心此刻已有的场景」：中枢取一次就记一次调用。"""

    def __init__(self, summary="主驾在对车机说「导航去公司」"):
        self.calls = 0
        self.scene = {"summary": summary, "intent": "导航", "target": "车机",
                      "topic": "公司", "cues": ["导航"], "revision": 3,
                      "updated_at_ms": 0, "age_ms": 420, "pending": False}

    def __call__(self):
        self.calls += 1
        return dict(self.scene)


class ThreePaths(unittest.TestCase):
    def make(self, scene=None, accept=True, eot=None, **kwargs):
        self.events = []
        gate = GateKeeper(accept_fn=lambda text, sc, seat: accept_outcome(accept),
                          scene_fn=scene if scene is not None else SceneSource(),
                          seat="D", on_event=self.events.append, **kwargs)
        return gate

    def drive(self, gate, text="导航去公司", eot=None, timeout_ms=1500):
        gate.on_text(1, text, 0, final=True)
        gate.on_eot(1, 0, eot or eot_verdict())
        deadline = time.time() + timeout_ms / 1000.0
        while time.time() < deadline:
            gate.poll(400)
            verdict = gate.gate(1, 400)
            if verdict["route"] != "wait" or verdict["state"] == "还没说完":
                return verdict
            time.sleep(0.02)
        return gate.gate(1, 400)

    def test_complete_and_accept_opens_gate(self):
        gate = self.make(accept=True)
        verdict = self.drive(gate)
        self.assertEqual(verdict["route"], "agent")
        self.assertEqual(verdict["state"], "开闸")
        self.assertIn("gate_decision", [event["event"] for event in self.events])

    def test_complete_but_declined_only_enters_context(self):
        gate = self.make(accept=False)
        verdict = self.drive(gate)
        self.assertEqual(verdict["route"], "context_only")
        self.assertEqual(verdict["state"], "只进上下文")
        self.assertIn("gate_context_only", [event["event"] for event in self.events])

    def test_not_done_does_not_open(self):
        gate = self.make(accept=True)
        gate.on_text(1, "帮我看看前面那个", 0, final=True)
        gate.on_eot(1, 0, eot_verdict(label="cut", may_confirm=False))
        gate.poll(1000)
        verdict = gate.gate(1, 1000)
        self.assertEqual(verdict["route"], "wait")
        self.assertEqual(verdict["state"], "还没说完")
        self.assertIn("gate_wait", [event["event"] for event in self.events])
        self.assertNotIn("gate_decision", [event["event"] for event in self.events],
                         "没说完不开闸，也不产出一份要执行的闸门结论")

    def test_gate_can_open_later_on_same_version(self):
        """同一版字先 cut、静音窗到上限后当说完：仍用同一份（这一版取的）场景与接不接。"""
        gate = self.make(accept=True)
        gate.on_text(1, "导航去", 0, final=True)
        gate.on_eot(1, 0, eot_verdict(label="cut", may_confirm=False))
        self.assertEqual(gate.gate(1, 900)["route"], "wait")
        wait_accept(gate)
        verdict = gate.gate(1, 1600, assume_done=True)
        self.assertEqual(verdict["route"], "agent")
        self.assertIn("静音窗", verdict["reason"])


class ConcurrencyAndScene(unittest.TestCase):
    def test_two_routes_run_concurrently_and_scene_read_once(self):
        """两路同时在飞 + 每版只取一次场景（不为了等场景反复要新快照）。"""
        scene = SceneSource()
        begun = threading.Event()
        order = []

        def accept_fn(text, sc, seat):
            order.append(("accept_start", time.time()))
            begun.set()
            time.sleep(0.25)
            order.append(("accept_end", time.time()))
            return accept_outcome(True, latency=250.0)

        def eot_fn(text):
            begun.wait(1.0)              # 接不接已经在飞了，判停才开始算
            order.append(("eot_start", time.time()))
            time.sleep(0.10)
            order.append(("eot_end", time.time()))
            return {"label": "complete", "may_confirm": True, "may_start_early": True,
                    "complete": 0.95, "cut": 0.02, "wait": 0.03, "confidence": 0.9}

        gate = GateKeeper(accept_fn=accept_fn, eot_fn=eot_fn, scene_fn=scene, seat="D")
        gate.on_text(1, "导航去公司", 0, final=True)
        deadline = time.time() + 2.0
        while time.time() < deadline and gate.snapshot()["accept"]["state"] == "pending":
            gate.poll(200)
            time.sleep(0.01)
        gate.poll(600)
        verdict = gate.gate(1, 600)
        self.assertEqual(verdict["route"], "agent")
        self.assertEqual(scene.calls, 1, "同一版字只取一次场景快照")
        # 两路有重叠：接不接开始早于判停结束（并飞而不是串行相加）
        starts = {name: at for name, at in order}
        ends = {name: at for name, at in order}
        self.assertIn("accept_start", starts)
        self.assertIn("eot_start", starts)
        self.assertLess(starts.get("accept_start", 99), ends.get("eot_end", 0),
                        "两路时间上重叠")
        self.assertLess(verdict.get("two_route_ms"), 400, "关键路径取 max 而不是 250+100 相加")

    def test_no_scene_still_judges_and_never_waits(self):
        calls = []

        def accept_fn(text, scene, seat):
            calls.append(scene)
            return accept_outcome(True)

        gate = GateKeeper(accept_fn=accept_fn, scene_fn=lambda: None, seat="D")
        gate.on_text(1, "导航去公司", 0, final=True)
        gate.on_eot(1, 0, eot_verdict())
        now = wait_accept(gate)
        verdict = gate.gate(1, now)
        self.assertEqual(verdict["route"], "agent", "无场景照常判（缺场景 ≠ 缺判断）")
        self.assertEqual(calls, [None])
        self.assertTrue(verdict["scene_missing"])
        self.assertEqual(gate.counters["scene_missing"], 1)

    def test_scene_age_is_reported_not_gated(self):
        scene = SceneSource()
        scene.scene["age_ms"] = 5000       # 一次很旧的快照
        gate = GateKeeper(accept_fn=lambda text, sc, seat: accept_outcome(True), scene_fn=scene)
        gate.on_text(1, "导航去公司", 0, final=True)
        gate.on_eot(1, 0, eot_verdict())
        now = wait_accept(gate)
        verdict = gate.gate(1, now)
        self.assertEqual(verdict["scene_age_ms"], 5000, "多旧只记出来给调试页看，不设硬阈值")
        self.assertEqual(verdict["route"], "agent")

    def test_scene_source_is_not_blocking_hub(self):
        """读心侧慢 / 抛错也不能把中枢挂住：拿不到就按无场景照常判。"""
        asserted = {}

        def slow_scene():
            raise RuntimeError("读心还没算完")

        gate = GateKeeper(accept_fn=lambda text, sc, seat: accept_outcome(True),
                          scene_fn=slow_scene)
        began = time.time()
        gate.on_text(1, "导航去公司", 0, final=True)
        gate.on_eot(1, 0, eot_verdict())
        now = wait_accept(gate)
        verdict = gate.gate(1, now)
        self.assertLess(time.time() - began, 0.5, "取场景失败不得阻塞中枢")
        self.assertEqual(verdict["route"], "agent")


class EveryVersionAndScene(unittest.TestCase):
    """16:37 拍板的三处基建：每版都判不丢、场景真进判据、超时/失败不当成不接。"""

    def test_every_version_is_judged_not_dropped(self):
        seen = []
        release = threading.Event()

        def accept_fn(text, scene, seat):
            seen.append(text)
            release.wait(2.0)
            return accept_outcome(True, latency=10.0)

        gate = GateKeeper(accept_fn=accept_fn, scene_fn=SceneSource(), seat="D")
        gate.on_text(1, "导航去", 0)
        gate.on_text(2, "导航去公司", 100)
        gate.on_text(3, "导航去公司南门", 200)
        release.set()
        deadline = time.time() + 2.0
        while time.time() < deadline:
            gate.poll(400)
            if len(seen) >= 3 and all(gate.records[v]["accept"].get("state") == "ok"
                                      for v in (1, 2, 3)):
                break
            time.sleep(0.02)
        self.assertEqual(sorted(seen), ["导航去", "导航去公司", "导航去公司南门"],
                         "三版都要真判一次：不因上一版在飞就丢掉/合并排队中的旧一版")
        for version in (1, 2, 3):
            record = gate.records[version]
            self.assertEqual(record["accept"]["state"], "ok", "每一版都有自己的接不接结论")
            self.assertEqual(record["accept"]["accept"], True)

    def test_scene_really_enters_the_judgement(self):
        """读心那句场景必须真进判据（不是只拿来补一句展示理由）。"""
        scene = SceneSource(summary="我听到主驾说「导航去公司」，像在对车说。")
        captured = {}

        def accept_fn(text, sc, seat):
            captured["text"] = text
            captured["scene"] = sc
            return accept_outcome(True, source="判断模型 8009")

        gate = GateKeeper(accept_fn=accept_fn, scene_fn=scene, seat="D")
        gate.on_text(1, "导航去公司", 0, final=True)
        now = wait_accept(gate)
        self.assertEqual(captured["text"], "导航去公司")
        self.assertIsNotNone(captured["scene"])
        self.assertEqual(captured["scene"]["summary"],
                         "我听到主驾说「导航去公司」，像在对车说。",
                         "接不接这一路拿到的就是那句场景")
        self.assertEqual(captured["scene"]["revision"], 3)

    def test_timeout_then_late_result_can_still_open(self):
        """超时先不开闸；判断晚回来仍按同一版字改判（不丢结论、不当成不接）。"""
        gate = GateKeeper(
            accept_fn=lambda text, scene, seat: (time.sleep(0.25) or accept_outcome(True)),
            scene_fn=lambda: None, accept_timeout_ms=50)
        gate.on_text(1, "导航去公司", 0, final=True)
        gate.on_eot(1, 0, eot_verdict())
        gate.poll(100)
        first = gate.gate(1, 100)
        self.assertEqual(first["route"], "accept_unavailable")
        deadline = time.time() + 1.0
        while time.time() < deadline:
            gate.poll(300)
            verdict = gate.gate(1, 300)
            if verdict["route"] == "agent":
                break
            time.sleep(0.02)
        self.assertEqual(verdict["route"], "agent", "晚回来的判断仍可开闸")
        self.assertEqual(verdict["state"], "开闸")


class FailuresGoToContext(unittest.TestCase):
    def test_accept_failure_never_enters_agent(self):
        def boom(text, scene, seat):
            raise RuntimeError("accept service down")

        gate = GateKeeper(accept_fn=boom, scene_fn=lambda: None)
        gate.on_text(1, "导航去公司", 0, final=True)
        gate.on_eot(1, 0, eot_verdict())
        now = wait_accept(gate)
        verdict = gate.gate(1, now)
        self.assertEqual(verdict["route"], "accept_unavailable",
                         "判断失败也不是「不接」：先不开闸、界面可见")
        self.assertEqual(verdict["state"], "接不接失败：先不开闸")
        self.assertIn("失败", verdict["reason"])

    def test_accept_timeout_never_enters_agent(self):
        gate = GateKeeper(accept_fn=lambda text, scene, seat: (time.sleep(1.0) or accept_outcome(True)),
                          scene_fn=lambda: None, accept_timeout_ms=50)
        gate.on_text(1, "导航去公司", 0, final=True)
        gate.on_eot(1, 0, eot_verdict())
        gate.poll(200)              # 超过 50ms 超时线：先不开闸，但不是「不接」
        verdict = gate.gate(1, 200)
        self.assertEqual(verdict["route"], "accept_unavailable")
        self.assertEqual(verdict["state"], "接不接超时：先不开闸")
        self.assertIn("不当成", verdict["reason"])
        self.assertIn("超时", verdict["reason"])

    def test_version_mismatch_never_enters_agent(self):
        gate = GateKeeper(accept_fn=lambda text, scene, seat: accept_outcome(True),
                          scene_fn=lambda: None)
        gate.on_text(1, "导航去公司", 0, final=True)
        now = wait_accept(gate)
        verdict = gate.gate(7, now)             # 这份闸门结论对不上任何一版字
        self.assertEqual(verdict["route"], "context_only")
        self.assertIn("版本对不齐", verdict["reason"])
        self.assertEqual(gate.counters["version_mismatch"], 1)

    def test_superseded_version_keeps_its_own_record(self):
        """老一版字的记录还在：它的闸门结论只对它自己有效，不会漂到最新一版上。"""
        gate = GateKeeper(accept_fn=lambda text, scene, seat: accept_outcome(True),
                          scene_fn=lambda: None)
        gate.on_text(1, "导航去公司", 0, final=True)
        gate.on_text(2, "导航去公司南门", 100, final=True)
        now = wait_accept(gate)
        gate.on_eot(1, 100, eot_verdict())
        self.assertEqual(gate.gate(1, now)["route"], "agent")
        self.assertEqual(gate.gate(2, now)["route"], "wait", "新一版字还没判停，不能借用老的结论")


class AcceptInterface(unittest.TestCase):
    """「接不接」用哪个模型/服务未拍板：接口可替换，界面显示实际用了什么。"""

    def test_default_is_real_judge_not_rule(self):
        """默认必须是 8009 真判断（与判停同一套模型），不再把关键词规则当主路径。"""
        meta = accept_meta()
        self.assertIn("8009", meta["source"])
        self.assertEqual(meta["mode"], "kev_noul")
        self.assertFalse(meta["decided"], "未拍板不得写成已拍板")
        self.assertEqual(meta["threshold"], ACCEPT_MIN_SCORE)
        self.assertIn("开闸线未拍", meta["threshold_note"])
        self.assertEqual(meta["threshold_note"], "开闸线未拍，本次临时用 0.5")
        fn = build_accept_fn()
        self.assertEqual(fn.meta["mode"], "kev_noul", "默认走真判断，不是 rule")
        self.assertIn("8009", fn.meta["source"])

    def test_rule_is_only_explicit_offline_stand_in(self):
        fn = build_accept_fn(mode="rule")
        self.assertEqual(fn.meta["mode"], "rule")
        self.assertIn("离线", fn.meta["source"])
        out = fn("导航去公司", None, "D")
        self.assertTrue(out["accept"])
        self.assertIn("离线", out["model"], "离线替身不得冒充真判断")

    def test_unknown_mode_is_rejected_not_silently_switched(self):
        with self.assertRaises(ValueError):
            build_accept_fn(mode="jev-omni")

    def test_rule_splits_car_commands_from_chat(self):
        cases = {
            "导航去望京SOHO": True,
            "把空调调到24度": True,
            "我有点冷，开个座椅加热": True,
            "你说今天这天气真好": False,
            "咱晚上去哪吃": False,
            "我不是跟你说话，导航去公司": False,
        }
        for text, expected in cases.items():
            self.assertEqual(rule_accept(text)["accept"], expected, text)


class TurnSessionGate(unittest.TestCase):
    """主链：开闸前不进 Agent；三条去向按闸门结论分。"""

    def make(self, text, accept=True, eot_label="complete"):
        self.events = []
        mind = MindReading(seat="D", session_id="gate-chain")
        mind.ingest(text=text, final=True)
        self.mind = mind
        self.tools = 0
        self.llm = 0

        def select_tools(text, tool_set=None, context=None):
            self.tools += 1
            return {"selected": ["climate_set_power"], "scores": {}, "stage1_ms": 1, "stage2_ms": 1}

        def decide(*args, **kwargs):
            self.llm += 1
            return {"kind": "tool_call", "tool": "climate_set_power", "arguments": {}}

        gate = GateKeeper(accept_fn=lambda t, sc, seat: accept_outcome(accept),
                          scene_fn=mind.scene_snapshot, seat="D", on_event=self.events.append)
        session = TurnSession(TurnConfig(), lambda event: self.events.append(event),
                              session_id="gate-chain", seat_id="D",
                              eot_fn=lambda text: {"label": eot_label,
                                                   "complete": 0.95 if eot_label == "complete" else 0.1,
                                                   "cut": 0.02 if eot_label == "complete" else 0.8,
                                                   "wait": 0.03,
                                                   "confidence": 0.9},
                              select_tools_fn=select_tools, decide_fn=decide,
                              evidence_root=None, gate=gate)
        return session

    def drive(self, session, t0=1000, horizon_ms=2000, stop_on_tools=True):
        session.on_asr(t0, self.text, False)
        session.on_asr(t0 + 400, self.text, True)
        for t in range(t0 + 500, t0 + 400 + horizon_ms, 40):
            session.tick(t)
            if stop_on_tools and any(event["event"] == "tools_start" for event in self.events):
                break
            if session.turn_done:
                break
            time.sleep(0.005)
        return session

    def test_agent_path_opens_gate_then_tools(self):
        self.text = "导航去公司"
        session = self.make(self.text, accept=True)
        session = self.drive(session)
        self.assertEqual(session.state, "CONFIRMED")
        self.assertEqual(session._gate_open_version, session.version)
        time.sleep(0.15)                        # 工具披露在 worker 里跑，给它落地的时间
        self.assertEqual(self.tools, 1, "闸门开了才排工具披露")
        events = [event["event"] for event in self.events]
        self.assertIn("gate_open", events)
        self.assertIn("tools_start", events)
        self.assertLess(events.index("gate_open"), events.index("tools_start"))

    def test_declined_path_never_enters_agent(self):
        self.text = "你说今天这天气真好"
        session = self.make(self.text, accept=False)
        session = self.drive(session)
        self.assertEqual(self.tools, 0, "说完但不接：不披露工具")
        self.assertEqual(self.llm, 0, "说完但不接：不决策")
        self.assertEqual(session.context_consumed, 1)
        self.assertTrue(session.turn_done, "只进上下文的文本封口后回合收口")
        events = [event["event"] for event in self.events]
        self.assertIn("context_absorbed", events)
        self.assertNotIn("tools_start", events)
        absorbed = next(event for event in self.events if event["event"] == "context_absorbed")
        self.assertEqual(absorbed["text"], "你说今天这天气真好")
        self.assertIn("不接", absorbed["reason"])
        self.assertEqual(session.consumed_count, 0, "只进上下文不算 Agent 消费")

    def test_not_done_path_waits_and_does_not_enter_agent(self):
        """判停说「还没说完」：静音窗到上限之前不开闸、不排工具；到上限才按收口当说完。"""
        self.text = "帮我看看前面那个"
        session = self.make(self.text, accept=True, eot_label="cut")
        session.on_asr(1000, self.text, False)
        session.on_asr(1400, self.text, True)
        for t in range(1500, 2400, 40):          # cut_wait 1.5s 从本席最后一次出字算：2500 之前都算「还没说完」
            session.tick(t)
            time.sleep(0.003)
        self.assertEqual(self.tools, 0, "没说完：工具披露不发起")
        self.assertEqual(self.llm, 0)
        self.assertEqual(session._gate_open_version, 0)
        events = [event["event"] for event in self.events]
        self.assertIn("gate_wait", events)
        self.assertNotIn("gate_open", events)
        # 静音窗到上限：这一版字收口当说完，才按同一份接不接开闸
        for t in range(2400, 3400, 40):
            session.tick(t)
            if session._gate_open_version:
                break
            time.sleep(0.003)
        self.assertEqual(session._gate_open_version, session.version)
        opened = next(event for event in self.events if event["event"] == "gate_open")
        self.assertIn("静音窗", opened["reason"])

    def test_gate_version_mismatch_blocks_agent(self):
        """同一版字被新一版替换：老版本上的闸门不能放行。"""
        self.text = "导航去公司"
        session = self.make(self.text, accept=True)
        session.on_asr(1000, "导航去公司", False)
        session.on_asr(1200, "导航去公司", True)
        # 新一版字进来（用户继续说），老版本判停的结论对不上新版本 → 不开闸
        session.on_asr(1500, "导航去公司南门", False)
        session.tick(1600)
        self.assertEqual(session._gate_open_version, 0)
        self.assertEqual(self.tools, 0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
