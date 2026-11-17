"""双路相关性打分（P2-2 真调用）离线契约：noul 多问 / 命中 / 都不相关 / 有 running 才打。

对应 `src/relevance.py`（真调用）与 `src/task_entity.py`（`match()` 记账 + 事件）。本文件不连
8009、不起线程：真调用路径用假客户端（FakeClient）喂脚本化响应，离线 mock 通道（`mock=True`/
`dict`）一并钉住；再走一遍 `SessionBrain` 验证「有 running 才打、命中并进在跑任务、都不相关新开」。

运行：python3 -B tests/test_relevance.py   （本仓库无 pytest，用标准库 unittest）
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from core_pipeline_run import KEV_MODEL  # noqa: E402
from agent.orchestra import SessionBrain  # noqa: E402
from agent.relevance import RELEVANCE_MIN_SCORE, build_relevance_fn, relevance_questions, score_relevance  # noqa: E402
from agent.task_entity import TASK_RUNNING, Task  # noqa: E402

TOOL = "seat_heating"
SCHEMA = {"name": TOOL, "description": "座椅加热",
          "parameters": {"type": "object", "properties": {"enabled": {"type": "boolean"}},
                         "required": ["enabled"], "additionalProperties": False}}


def task(task_id, text):
    return Task(task_id=task_id, queue_no=int(task_id.split("-")[1]), seat="D", text=text, t_open=0)


class FakeResponse:
    def __init__(self, status_code=200, body=None, content=None):
        self.status_code = status_code
        self.content = content if content is not None else json.dumps(body).encode()


class FakeClient:
    def __init__(self, response=None, responses=None):
        self.response = response
        self.responses = list(responses or [])
        self.calls = []

    def post(self, url, json=None):
        self.calls.append({"url": url, "body": json})
        if self.responses:
            return self.responses.pop(0)
        return self.response


def noul_answer(*pairs):
    """构造一次相关性打分的活服务形状：answers 的键 = task_id，各一个 noul 概率。"""
    return {"model": KEV_MODEL,
            "answers": {name: {"type": "noul", "noul": value} for name, value in pairs},
            "usage": {"input_tokens": 1, "output_tokens": 0}}


def make_decide(tool=TOOL, arguments=None):
    def _decide(utterance, selected, schemas, tool_set="core_score", context=None, **kwargs):
        return {"decision": {"kind": "tool_call", "tool": tool,
                             "arguments": dict(arguments or {"enabled": True})},
                "llm_ms": 1.0, "log_fields": {"decision_head": "unit"}}
    return _decide


# ---------------------------------------------------------------- 请求形状

class RequestShapeTests(unittest.TestCase):
    def test_each_candidate_becomes_one_noul_question(self):
        candidates = [task("task-001", "温度调到22"), task("task-002", "开一下窗")]
        questions = relevance_questions(candidates)
        self.assertEqual(set(questions), {"task-001", "task-002"})
        for name, q in questions.items():
            self.assertEqual(q["type"], "noul")
            self.assertIn("延续或修改", q["instructions"])

    def test_call_goes_to_the_single_systemone_endpoint(self):
        client = FakeClient(FakeResponse(body=noul_answer(("task-001", 0.9))))
        result = score_relevance("还是调到24", [task("task-001", "温度调到22")],
                                 client=client, base="http://127.0.0.1:8123")
        self.assertEqual(client.calls[0]["url"], "http://127.0.0.1:8123/v1/systemone")
        self.assertEqual(result["task_id"], "task-001")

    def test_request_uses_noul_never_and_no_model(self):
        client = FakeClient(FakeResponse(body=noul_answer(("task-001", 0.9))))
        score_relevance("还是调到24", [task("task-001", "温度调到22")], client=client)
        body = client.calls[0]["body"]
        self.assertEqual(body["think_mode"], "never")
        self.assertNotIn("model", body)
        self.assertEqual(set(body["questions"]), {"task-001"})
        state = json.loads(body["state"])
        self.assertEqual(state["utterance"], "还是调到24")
        self.assertEqual([t["task_id"] for t in state["tasks"]], ["task-001"])


# ---------------------------------------------------------------- 命中 / 都不相关

class SelectionTests(unittest.TestCase):
    def test_argmax_above_threshold_hits(self):
        client = FakeClient(FakeResponse(body=noul_answer(("task-001", 0.1), ("task-002", 0.9))))
        result = score_relevance("还是调到24", [task("task-001", "温度调到22"),
                                                task("task-002", "开一下窗")], client=client)
        self.assertEqual(result["task_id"], "task-002")
        self.assertEqual(result["reason"], "hit")
        self.assertEqual(result["mode"], "kev")

    def test_all_below_threshold_is_none_relevant(self):
        client = FakeClient(FakeResponse(body=noul_answer(("task-001", 0.1), ("task-002", 0.4))))
        result = score_relevance("开一下窗", [task("task-001", "温度调到22"),
                                              task("task-002", "找附近吃饭")], client=client)
        self.assertIsNone(result["task_id"])
        self.assertEqual(result["reason"], "none_relevant")

    def test_exact_threshold_passes(self):
        client = FakeClient(FakeResponse(body=noul_answer(("task-001", RELEVANCE_MIN_SCORE))))
        result = score_relevance("还是调到24", [task("task-001", "温度调到22")], client=client)
        self.assertEqual(result["task_id"], "task-001")

    def test_no_candidates_no_call(self):
        client = FakeClient(FakeResponse(body=noul_answer()))
        result = score_relevance("还是调到24", [], client=client)
        self.assertIsNone(result["task_id"])
        self.assertEqual(result["reason"], "no_candidate")
        self.assertEqual(client.calls, [], "没有候选就不该发调用")


# ---------------------------------------------------------------- mock 通道

class MockTests(unittest.TestCase):
    def test_mock_true_hits_first_candidate(self):
        result = score_relevance("还是调到24", [task("task-001", "温度调到22"),
                                                task("task-002", "开一下窗")], mock=True)
        self.assertEqual(result["task_id"], "task-001")
        self.assertEqual(result["mode"], "mock")

    def test_mock_dict_overrides(self):
        result = score_relevance("还是调到24", [task("task-001", "温度调到22"),
                                                task("task-002", "开一下窗")],
                                 mock={"task-001": 0.1, "task-002": 0.9})
        self.assertEqual(result["task_id"], "task-002")

    def test_mock_dict_all_low_is_none(self):
        result = score_relevance("开一下窗", [task("task-001", "温度调到22")],
                                 mock={"task-001": 0.1})
        self.assertIsNone(result["task_id"])


# ---------------------------------------------------------------- 协议校验

class MalformedTests(unittest.TestCase):
    def test_wrong_answer_type_is_rejected(self):
        client = FakeClient(FakeResponse(body={
            "model": KEV_MODEL,
            "answers": {"task-001": {"type": "choice", "choice": "same"}},
        }))
        with self.assertRaises(ValueError):
            score_relevance("还是调到24", [task("task-001", "温度调到22")], client=client)

    def test_probability_out_of_range_is_rejected(self):
        client = FakeClient(FakeResponse(body=noul_answer(("task-001", 1.2))))
        with self.assertRaises(ValueError):
            score_relevance("还是调到24", [task("task-001", "温度调到22")], client=client)

    def test_non_200_is_rejected(self):
        client = FakeClient(FakeResponse(status_code=500, body=noul_answer(("task-001", 0.9))))
        with self.assertRaises(ValueError):
            score_relevance("还是调到24", [task("task-001", "温度调到22")], client=client)


# ---------------------------------------------------------------- 接线（SessionBrain / TaskBoard）

class WiringTests(unittest.TestCase):
    def say(self, brain, seat, text, t_ms=0):
        brain.mark_now(t_ms)
        return brain.decide(seat, text, [TOOL], [SCHEMA], "core_score", {"synthetic": True})

    def test_scores_only_when_a_task_is_running(self):
        """第一句来没有 running 任务 → 不打；第二句来时第一句还在跑 → 打一次相关分并命中 →
        第二句并进第一句那条任务（不开新任务）；两条 AU 照常执行、账平。"""
        calls = []

        def scorer(utterance, candidates):
            calls.append((utterance, [t.task_id for t in candidates]))
            return candidates[0].task_id

        brain = SessionBrain(make_decide(), relevance_fn=scorer)
        first = self.say(brain, "D", "温度调到22", 100)["decision"]
        self.assertEqual(calls, [], "没有 running 任务时一次调用都不发")
        second = self.say(brain, "D", "还是调到24", 200)["decision"]
        # 第一句的 AU 还没应用 → task-001 仍在跑 → 第二句对它打一次相关分并命中。
        self.assertEqual(calls, [("还是调到24", ["task-001"])], str(calls))
        snap = brain.snapshot()
        self.assertEqual(snap["relevance_hits"], 1, str(snap))
        self.assertEqual(snap["task_open"], 1, "命中 → 并进在跑任务，不开新任务")
        self.assertEqual(snap["tasks_merged"], 1, str(snap))
        self.assertEqual(snap["task_total"], 1, str(snap))
        # 两条 AU 都照常执行（合并只改归属、不取消执行），账照旧平（打分不破 A6）。
        brain.mark_now(300)
        brain.observe({"event": "llm_decision", "t_ms": 300, "seat_id": "D", "decision": first})
        brain.mark_now(400)
        brain.observe({"event": "llm_decision", "t_ms": 400, "seat_id": "D", "decision": second})
        audit = brain.audit()
        self.assertTrue(audit["task_eq"] and audit["iu_eq"], str(audit))
        self.assertEqual(audit["au_executed"], 2)
        self.assertEqual(audit["task_done"], 1, str(audit))

    def test_none_relevant_still_opens_a_new_task(self):
        brain = SessionBrain(make_decide(), relevance_fn=lambda text, candidates: None)
        self.say(brain, "D", "温度调到22", 100)
        self.say(brain, "D", "开一下窗", 200)
        snap = brain.snapshot()
        self.assertEqual(snap["relevance_misses"], 1, str(snap))
        self.assertEqual(snap["task_open"], 2, "都不相关 → 新开任务（FIFO），不合并")

    def test_build_relevance_fn_returns_task_id_or_none(self):
        fn = build_relevance_fn(mock={"task-001": 0.9})
        self.assertEqual(fn("还是调到24", [task("task-001", "温度调到22")]), "task-001")
        fn_none = build_relevance_fn(mock={"task-001": 0.1})
        self.assertIsNone(fn_none("开一下窗", [task("task-001", "温度调到22")]))

    def test_first_task_is_running_until_applied(self):
        """候选资格＝「还在运行中」：AU 应用前在跑、应用后收口出候选（拍板① 做完不进候选）。"""
        brain = SessionBrain(make_decide())
        decision = self.say(brain, "D", "温度调到22", 100)["decision"]
        self.assertEqual(brain.tasks.get("task-001").state, TASK_RUNNING)
        brain.mark_now(200)
        brain.observe({"event": "llm_decision", "t_ms": 200, "seat_id": "D", "decision": decision})
        self.assertNotEqual(brain.tasks.get("task-001").state, TASK_RUNNING)
        self.assertEqual(brain.tasks.candidates(), [], "已做完的任务不进候选")


if __name__ == "__main__":
    unittest.main(verbosity=2)
