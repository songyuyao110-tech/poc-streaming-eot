"""双路相关性打分（P2-2 真调用）：新话 vs 正在跑的任务，用判停那个模型（8009）打相关分。

口径来源：wiki `[[无确认窗流程]]` §4、`[[双路ASR]]` §4 Q4 注，拍板原文
`raw/2026-11-03-dual-related-model-decision.md`（总原则 / 用哪个模型 / 已拍示例），偏差盘点
`poc-no-confirm-gap-DETAIL.md` P2-2。

一件事（本模块只做这一件，**不碰判定与调度**）：
- 给 `TaskBoard(relevance_fn=...)` 提供一份**真调用**的打分器：新话进来时对「正在运行的任务」
  逐个问判停那个模型「是不是在延续 / 修改这条任务」，取最高分、过阈值才算命中，否则「都不相关」。
  签名对齐 `task_entity.TaskBoard.match` 的约定：`fn(utterance, candidates) -> task_id | None`。

已拍口径（都来自 wiki，不新增服务、不新增判定）：
- 用**判停同一个模型、同一个 8009 服务**（`KEV_EOT_BASE` / `/v1/systemone`），复用
  `core_pipeline_run.kev_call` 的请求形状（`think_mode: "never"`、不带 `model`、noul 打分）。
- 只跟**还在运行**的任务比；已做完的不进候选、不参与打分——这条由 `TaskBoard.match()` 在
  候选集边界上强制（本模块拿到的 `candidates` 就是 `candidates()`＝运行中集合）。
- 「命中哪个在跑的任务 / 都不相关」：每个候选一个 noul 问题（沿用 `confirm_gate` / 工具打分的
  noul 范式，不拿 choice 枚举 criteria），argmax 过阈值才算命中；全低于阈值 → `None`（都不相关）。

未定 / 本单不选（待拍项，别在这里代拍）：
- **命中阈值**：noul 分数判「相关」的线没有拍板。本单默认 `RELEVANCE_MIN_SCORE=0.5`
  （「更可能相关才认」，对齐工具线 0.5；`RELEVANCE_MIN_SCORE` 可 env 覆盖）。真服务校准后要重标。
- **noul 多问（N 选 1）是否被判停服务支持**：P2-2 风险④ 标【待核实】，需一次真服务验（本单不跑）。
- 合并 / 覆盖 / 新开的**行为**不在本模块（打分器只输出「命中哪个 / 都不相关」）：落点已接到
  `SessionBrain.decide` → `TaskBoard.merge_into`（命中并进那条在跑任务）与 `open_task`（都不相关
  新开 FIFO），见 `orchestra.py` / `task_entity.py`；覆盖式冲突（后值覆盖先值、只执行一次）仍归
  P2-3 / C3。

并发：与 `SessionBrain` 的 IU/AU 同一条约束——由 `SessionBrain._lock` 串起来（`decide()` 在席
worker 线程、`match()` 在 `decide()` 内持锁调用，打分失败被 `match()` 兜住记 `relevance_errors`）。
"""
from __future__ import annotations

import json
import math
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.dont_write_bytecode = True

from core_pipeline_run import KEV_EOT_BASE, KEV_EOT_CLIENT, kev_call, probability
# noqa: E402

# 判「相关」的 noul 分数线。拍板没给相关打分阈值，本单默认对齐工具线 0.5（「更可能相关才认」）。
RELEVANCE_MIN_SCORE = float(os.environ.get("RELEVANCE_MIN_SCORE", "0.5"))

# 每个候选一题的 noul 模板。问题名 = task_id（响应 `answers` 的键也按 task_id 对回来，别改名）。
_RELEVANCE_QUESTION_TMPL = "用户这句话是否是在延续或修改下面这条正在执行的任务：「%s」？"


def relevance_questions(candidates) -> dict:
    """每候选一个 noul 问题；问题名 = task_id。与 `confirm_gate._CONFIRM_QUESTIONS` 同一范式。"""
    return {task.task_id: {"type": "noul",
                           "instructions": _RELEVANCE_QUESTION_TMPL % (task.text or "")}
            for task in candidates}


def _state(utterance: str, candidates) -> str:
    """把新话与候选任务一起送进 `state`（模型拿得到每条任务的文本，noul 只判每个候选是否相关）。"""
    return json.dumps({
        "utterance": utterance,
        "tasks": [{"task_id": task.task_id, "text": task.text} for task in candidates],
    }, ensure_ascii=False, allow_nan=False)


def _pick(scores: dict, threshold: float) -> dict:
    """argmax 过阈值 → 命中；否则（或空分数）→ 都不相关。"""
    best = max(scores, key=scores.get) if scores else None
    if best is None or scores[best] < threshold:
        return {"task_id": None, "reason": "none_relevant", "scores": scores}
    return {"task_id": best, "reason": "hit", "scores": scores}


def _mock_scores(utterance: str, candidates) -> dict:
    """离线 mock（不连 8009）：首候选给 0.9、其余 0.1——镜像 `score_tools` 的 mock 口径，
    只保证判定逻辑可复现，不构成真实质量证据。"""
    names = [task.task_id for task in candidates]
    scores = {name: 0.1 for name in names}
    if names:
        scores[names[0]] = 0.9
    return scores


def score_relevance(utterance: str, candidates, *, mock=None, threshold=None,
                    client=None, base=None) -> dict:
    """新话 vs 在跑任务打一次相关分，返回 `{task_id, reason, scores, mode, ...}`。

    `task_id` = 选中任务 id（过阈值）或 `None`（都不相关 / 无候选）。
    `mock`：`None` 真调用（判停那个模型，需 8009 隧道）；`True` 离线首候选命中；`dict` 覆盖各候选分。
    """
    threshold = RELEVANCE_MIN_SCORE if threshold is None else float(threshold)
    if not candidates:
        return {"task_id": None, "reason": "no_candidate", "scores": {}, "mode": "none"}
    if mock is not None and isinstance(mock, dict):
        scores = {task.task_id: float(mock.get(task.task_id, 0.0)) for task in candidates}
        return {**_pick(scores, threshold), "mode": "mock"}
    if mock:
        return {**_pick(_mock_scores(utterance, candidates), threshold), "mode": "mock"}

    client = client or KEV_EOT_CLIENT
    base = (base or KEV_EOT_BASE).rstrip("/")
    questions = relevance_questions(candidates)
    began = time.monotonic()
    raw = kev_call(client, _state(utterance, candidates), questions, base=base)
    wall_ms = (time.monotonic() - began) * 1000
    scores = {}
    for task in candidates:
        answer = raw["answers"][task.task_id]
        if (not isinstance(answer, dict) or set(answer) - {"type", "noul"}
                or answer.get("type", "noul") != "noul"):
            raise ValueError("Malformed relevance Noul")
        scores[task.task_id] = probability(answer.get("noul"))
    return {**_pick(scores, threshold), "mode": "kev", "wall_ms": wall_ms,
            "kev_ms": raw.get("latency_ms")}


def build_relevance_fn(*, mock=None, threshold=None, client=None, base=None):
    """构造 `TaskBoard` 用的 relevance_fn：`fn(utterance, candidates) -> task_id | None`。

    `mock=None` 走真调用（判停那个模型）；`mock=True` / `dict` 走离线通道（测试 / 回放用）。
    返回的就是 `score_relevance(...)["task_id"]`——命中给 id，都不相关 / 无候选给 `None`。
    """
    def _fn(utterance, candidates):
        return score_relevance(utterance, candidates, mock=mock, threshold=threshold,
                               client=client, base=base)["task_id"]
    return _fn


if __name__ == "__main__":
    # 手动探针：真调用一次（需 8009 隧道），参数为「新话」+「候选任务文本（逗号分隔）」。
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("utterance")
    parser.add_argument("candidates", help="逗号分隔的候选任务文本，按序 task-001 / task-002 / …")
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()

    from agent.task_entity import Task
    tasks = [Task(task_id="task-%03d" % (i + 1), queue_no=i + 1, seat="D", text=text.strip(),
                  t_open=0) for i, text in enumerate(args.candidates.split(",")) if text.strip()]
    result = score_relevance(args.utterance, tasks, mock=args.mock)
    print(json.dumps(result, ensure_ascii=False))
