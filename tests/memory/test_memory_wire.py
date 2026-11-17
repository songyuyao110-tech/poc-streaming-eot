"""记忆接线离线验收（非麦路径）：写发生在「收住那一刻」、查发生在「Agent 发起请求那一刻」。

钉住 wiki `四模块职责与接口契约` §1 记忆行 / §2 接口 3 / §4 开闸分流 在本仓落地后的**时机**，
不连 memos、不连 KEV/LLM（记忆侧用假实现记账，判停/接不接/决策全用注入件）：

1. **收住才写**：判停说「还没说完」、静音窗没到时一刀都不写；收住那一刻写一刀。
2. **只进上下文的话也写**：说完但不接（不进 Agent）也写（与开闸那一路同一条写入口）。
3. **同一句只写一刀**：接不接没回来 / 闸门反复判定时，同一版字不重复写。
4. **场景配的是「这一刻」那份**：写入的 scene 是收住那一刻场景流里最新的一份，不是上一句那份。
5. **查在发起请求那一刻**：`decide` 拿到本次请求之前刚查到的记忆；事件顺序是「开闸 → 查 → 决策」，
   不是开闸那一刻查、也不是会话开始预取。
6. **查失败不拖住 Agent**：查不到 / 抛错时 context 里没有记忆栏，决策照常落地。

运行：`python3 -B tests/memory/test_memory_wire.py`（纯离线，不连外部服务）
"""
from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from gate.hub_gate import GateKeeper  # noqa: E402
from gate.turn_session import MEMORY_NOTE, TurnConfig, TurnSession  # noqa: E402


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
    return {"label": "complete", "complete": 0.95, "cut": 0.02, "wait": 0.03,
            "confidence": 0.9, "wall_ms": 1.0}


def eot_wait(text):
    return {"label": "wait", "complete": 0.10, "cut": 0.05, "wait": 0.85,
            "confidence": 0.4, "wall_ms": 1.0}


class FakeMemory:
    """假记忆：只记账（写了什么、什么时候查的、查了什么），不连任何外部服务。"""

    enabled = True

    def __init__(self, hits=None, fail_recall=False):
        self.hits = list(hits or [])
        self.fail_recall = fail_recall
        self.writes = []
        self.recalls = []
        self.requests = []
        self.last_recall = {}
        self.events = []

    def write(self, turn):
        receipt = {"status": "queued", "seat": (turn or {}).get("seat"),
                   "text": (turn or {}).get("text"), "scene": (turn or {}).get("scene"),
                   "t_ms": (turn or {}).get("t_ms"), "version": (turn or {}).get("version"),
                   "latency_ms": None, "error": None}
        self.writes.append(dict(turn or {}))
        self.events.append(("write", (turn or {}).get("t_ms")))
        return receipt

    def recall(self, t_ms=None, limit=5, query=""):
        self.recalls.append({"t_ms": t_ms, "limit": limit, "query": query})
        self.events.append(("recall", t_ms))
        if self.fail_recall:
            raise RuntimeError("memos 不可用（测试）")
        receipt = {"status": "ok", "count": len(self.hits), "latency_ms": 1.0, "reason": None,
                   "error": None, "t_ms": t_ms}
        self.last_recall = receipt
        return list(self.hits)

    def remember_request(self, request):
        self.requests.append(dict(request or {}))
        return {}

    def status(self):
        return {"enabled": True, "kind": "fake", "writes": len(self.writes),
                "recalls": len(self.recalls)}

    def close(self):
        return None


_NO_MEMORY = object()


class Chain:
    """最小主链：一席闸门 + 一席 TurnSession（与 `ui/server.py` 同形），记忆用假实现。"""

    def __init__(self, *, accept_fn=None, eot_fn=eot_complete, scene=None, memory=_NO_MEMORY):
        self.events = []
        self.decisions = []
        self.tools = []
        self.scene = scene if scene is not None else {"summary": "我听到主驾在说导航。",
                                                     "revision": 3, "updated_at_ms": 0, "age_ms": 900}
        self.memory = FakeMemory() if memory is _NO_MEMORY else memory
        self.gate = GateKeeper(accept_fn=accept_fn or accept(True), scene_fn=self._scene,
                               seat="D", on_event=self.events.append)
        self.session = TurnSession(
            TurnConfig(), self.events.append, session_id="memory-wire-" + str(id(self)),
            seat_id="D", eot_fn=eot_fn, select_tools_fn=self._select,
            decide_fn=self._decide, evidence_root=None, gate=self.gate, memory=self.memory)

    def _scene(self):
        scene = self.scene() if callable(self.scene) else self.scene
        return None if scene is None else dict(scene)

    def _select(self, utterance, tool_set, context):
        self.tools.append((utterance, context))
        return {"scores": {"tool:car_navi_to_destination": 0.9},
                "selected": ["car_navi_to_destination"], "schemas": [], "native_schemas": None,
                "domains": None, "timing": {}}

    def _decide(self, utterance, selected, schemas, tool_set, context=None, **kwargs):
        self.decisions.append((utterance, context))
        return {"decision": {"kind": "tool_call", "tool": "car_navi_to_destination",
                             "arguments": {}}, "llm_ms": 1.0, "log_fields": {}}

    def partial(self, text, *, t0=1000, horizon_ms=600):
        """只说一半、没到静音窗：这一刀不能写记忆。"""
        self.session.on_asr(t0, text, False)
        for t in range(t0 + 40, t0 + horizon_ms, 40):
            self.session.tick(t)
        return self.session

    def say(self, text, *, t0=1000, horizon_ms=4000):
        self.session.on_asr(t0, text, False)
        self.session.on_asr(t0 + 300, text, True)
        deadline = time.time() + 1.5
        for t in range(t0 + 400, t0 + 400 + horizon_ms, 40):
            self.session.tick(t)
            time.sleep(0.004)
            if self.decisions and self.session._gate_open_version == self.session.version:
                time.sleep(0.06)
                self.session.tick(t)
                break
            if time.time() > deadline:
                break
        time.sleep(0.05)
        self.session.tick(t0 + 400 + horizon_ms)
        return self.session

    def events_of(self, name):
        return [event for event in self.events if event.get("event") == name]


class WriteTiming(unittest.TestCase):
    def test_not_written_before_the_sentence_is_held(self):
        """还没收住（判停说 wait、静音窗没到）：一刀都不写。"""
        chain = Chain(eot_fn=eot_wait)
        chain.partial("帮我看看前面那个")
        self.assertEqual(chain.memory.writes, [], "话还没收住，不写记忆")
        self.assertEqual(chain.events_of("memory_write"), [])

    def test_written_once_at_the_close_moment(self):
        """判停说完 / 静音窗收口那一刻写一刀：这句原话 + 这一刻的场景。"""
        chain = Chain()
        session = chain.say("导航去望京SOHO")
        self.assertEqual(len(chain.memory.writes), 1, "收住那一刻写一刀")
        turn = chain.memory.writes[0]
        self.assertEqual(turn["text"], "导航去望京SOHO", "写的是这句完整原话")
        self.assertEqual(turn["seat"], "D")
        self.assertEqual(turn["scene"]["summary"], chain._scene()["summary"], "配这一刻的场景")
        self.assertEqual(len(chain.events_of("memory_write")), 1)
        self.assertEqual(chain.events_of("memory_write")[0]["why"], "eot_complete")
        self.assertGreater(session.consumed_count, 0, "开闸那一轮照旧进 Agent")

    def test_context_only_sentence_is_also_written(self):
        """说完但不回答（只进上下文、不进 Agent）：照样写记忆。"""
        chain = Chain(accept_fn=accept(False))
        session = chain.say("你说今天这天气可真舒服，咱晚上去哪吃")
        self.assertEqual(session.context_consumed, 1, "这一轮只进上下文")
        self.assertEqual(chain.decisions, [], "不进 Agent")
        self.assertEqual(len(chain.memory.writes), 1, "只进上下文的话也写")
        self.assertEqual(chain.memory.writes[0]["text"], "你说今天这天气可真舒服，咱晚上去哪吃")

    def test_same_sentence_is_not_written_twice(self):
        """接不接没回来（闸门反复判定）时，同一版字只写一刀。"""
        chain = Chain(accept_fn=accept_raises)
        session = chain.say("导航去望京SOHO", horizon_ms=6000)
        self.assertEqual(len(chain.memory.writes), 1, "同一句只写一刀，不随闸门反复写")
        self.assertGreaterEqual(len(chain.events_of("gate_pending")), 1)
        # 闸门后来仍能按同一版字开闸（记忆写法不影响闸门判据）
        chain.gate.accept_fn = accept(True)
        chain.gate.poll(int(session.version_t_ms or 0) + 99999)
        self.assertEqual(len(chain.memory.writes), 1)

    def test_scene_paired_is_the_one_at_the_close_moment_not_the_previous(self):
        """配的是这句收住那一刻的场景，不是上一句那份。"""
        scenes = [{"summary": "我听到主驾在说上一句闲聊。", "revision": 1},
                  {"summary": "我听到主驾在说这一句导航。", "revision": 7}]
        state = {"index": 0}

        def scene():
            return scenes[min(state["index"], len(scenes) - 1)]

        chain = Chain(scene=scene)
        # 上一句（不接，只进上下文）先进台账，并让场景停在「上一句那份」
        chain.session.gate.accept_fn = accept(False)
        chain.say("闲聊一句，先说个别的")
        state["index"] = 1                     # 这句收住前，场景流已更新到「这一句」
        chain.session.gate.accept_fn = accept(True)
        chain.events.clear()
        chain.memory.writes.clear()
        chain.say("导航去望京SOHO")
        self.assertEqual(len(chain.memory.writes), 1)
        self.assertEqual(chain.memory.writes[0]["scene"]["summary"],
                         "我听到主驾在说这一句导航。", "写的是这一刻那份场景，不是上一句那份")


class RecallTiming(unittest.TestCase):
    def test_recall_happens_when_the_request_is_fired_not_at_gate_open(self):
        memory = FakeMemory(hits=["主驾叫张伟。", "副驾是他老婆李娜。"])
        chain = Chain(memory=memory)
        chain.say("导航去望京SOHO")
        self.assertEqual(len(chain.decisions), 1, "开闸那一轮进 Agent")
        utterance, context = chain.decisions[0]
        self.assertEqual(utterance, "导航去望京SOHO")
        self.assertEqual(context.get("memory"), ["主驾叫张伟。", "副驾是他老婆李娜。"],
                         "Agent 拿到发起请求那一刻查到的记忆")
        self.assertIn("不是本次新指令", context.get("memory_note", ""))
        self.assertEqual(memory.recalls[0]["query"], "导航去望京SOHO", "按这句原话查")
        self.assertEqual(memory.requests[0]["memory"], ["主驾叫张伟。", "副驾是他老婆李娜。"],
                         "这一次请求带了什么，记成一次对账记录")
        # 事件顺序：开闸 → 查 → 决策（不是开闸那一刻查、也不是会话开始预取）
        names = [event.get("event") for event in chain.events]
        self.assertIn("gate_open", names)
        self.assertIn("memory_recall", names)
        self.assertEqual(len(chain.events_of("memory_recall")), 1)
        self.assertIn("llm_start", names)
        self.assertLess(names.index("gate_open"), names.index("memory_recall"))
        self.assertLess(names.index("memory_write"), names.index("memory_recall"))
        self.assertEqual(len(chain.events_of("agent_request")), 1)
        self.assertEqual(chain.events_of("agent_request")[0]["memory_count"], 2)

    def test_recall_failure_does_not_block_the_agent(self):
        memory = FakeMemory(hits=["随便一条"], fail_recall=True)
        chain = Chain(memory=memory)
        chain.say("导航去望京SOHO")
        self.assertEqual(len(chain.decisions), 1, "查记忆失败不拖住这次请求")
        utterance, context = chain.decisions[0]
        self.assertNotIn("memory", context, "查不到就不带记忆栏")
        self.assertEqual(utterance, "导航去望京SOHO")

    def test_no_memory_wired_keeps_the_judgement_chain_intact(self):
        """不挂记忆：写/查都不发生（离线回归的默认形态），判停/开闸/决策照旧。"""
        chain = Chain(memory=None)
        chain.say("导航去望京SOHO")
        self.assertEqual(len(chain.decisions), 1, "不挂记忆不影响这一轮进 Agent")
        self.assertEqual(chain.events_of("memory_write"), [], "不挂记忆就不写")
        self.assertEqual(chain.events_of("memory_recall"), [], "不挂记忆就不查")
        self.assertNotIn("memory", chain.decisions[0][1])


if __name__ == "__main__":
    unittest.main(verbosity=2)
