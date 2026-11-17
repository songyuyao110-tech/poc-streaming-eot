"""任务实体（P2-1/P2-2）离线单测：运行中集合 / 已完成不进候选 / 先来后到 / 打分接到行为 / 不破 A6 账。

不连 8009、不起线程——直接喂 `TaskBoard` 与 `SessionBrain`：

    python3 -B tests/test_task_entity.py      → 期望「N/N 例通过」

口径来源：wiki `[[双路ASR]]` §4 Q4 注、`[[无确认窗流程]]` §4，拍板原文
`raw/2026-11-03-dual-related-model-decision.md`（总原则 / 已拍示例）、
`raw/2026-11-04-no-confirm-window-flow-decision.md`（双路调度）、偏差盘点
`poc-no-confirm-gap-DETAIL.md` P2-1 / P2-2（验收句：「这算新任务还是同一件」能给出理由）。

本单**验**：打分结果接到任务行为——命中 → 并进在跑任务（`merge_into`）、都不相关 → 新开 FIFO、
已做完不进候选、merge 兜底与决策失败不误结原任务。本单**不验**：覆盖式冲突（后值覆盖先值、
P2-3 / C3）、双席顺序与回执全文（P2-4）、任何 UI、打分本身判得对不对（那是 P2-2 真服务卡）。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.dont_write_bytecode = True
from agent.orchestra import SessionBrain  # noqa: E402
from agent.task_entity import TASK_ABANDONED, TASK_DONE, TASK_RUNNING, TaskBoard  # noqa: E402

TOOL = "seat_heating"
SCHEMA = {"name": TOOL, "description": "座椅加热",
          "parameters": {"type": "object", "properties": {"enabled": {"type": "boolean"}},
                         "required": ["enabled"], "additionalProperties": False}}
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("  %s  %s%s" % ("ok  " if ok else "FAIL", name, ("｜" + detail) if detail and not ok else ""))


# ---------------------------------------------------------------- board 级辅助

def open_task(board, text, t_ms=0, seat="D"):
    return board.open_task(seat, text, t_ms)


def settle(board, task, t_ms=0, outcome="executed", au_id=None):
    au_id = au_id or ("au-" + task.task_id)
    board.attach_au(task.task_id, au_id, t_ms)
    board.settle_au(au_id, t_ms, outcome)


# ---------------------------------------------------------------- brain 级辅助

def make_decide(tool=TOOL, arguments=None, calls=None, raises=None):
    def _decide(utterance, selected, schemas, tool_set="core_score", context=None, **kwargs):
        if raises is not None:
            raise raises
        if calls is not None:
            calls.append(utterance)
        return {"decision": {"kind": "tool_call", "tool": tool,
                             "arguments": dict(arguments or {"enabled": True})},
                "llm_ms": 1.0, "log_fields": {"decision_head": "unit"}}
    return _decide


def say(brain, seat, text, t_ms=0):
    brain.mark_now(t_ms)
    return brain.decide(seat, text, [TOOL], [SCHEMA], "core_score", {"synthetic": True})


def applied(brain, seat, decision, t_ms):
    brain.mark_now(t_ms)
    brain.observe({"event": "llm_decision", "t_ms": t_ms, "seat_id": seat, "decision": decision})


def new_brain(board=None, decide=None, **kwargs):
    return SessionBrain(decide or make_decide(), task_board=board, **kwargs)


# ---------------------------------------------------------------- 用例

def case_fifo_queue():
    """先来后到：队列位按开任务次序给，前面收口后后面的位次往前挪。"""
    board = TaskBoard()
    a = open_task(board, "温度调到22", 100)
    b = open_task(board, "开一下窗", 200)
    c = open_task(board, "找附近吃饭", 300)
    check("queue_no 单调（先开先小）", [a.queue_no, b.queue_no, c.queue_no] == [1, 2, 3],
          str([a.queue_no, b.queue_no, c.queue_no]))
    check("队列位 1/2/3", [board.queue_position(t.task_id) for t in (a, b, c)] == [1, 2, 3])
    check("运行中集合按开任务次序", [t.task_id for t in board.running()] ==
          [a.task_id, b.task_id, c.task_id])
    settle(board, a, 400)
    check("先来的收口后不再在运行集合", a.task_id not in [t.task_id for t in board.running()])
    check("后来者位次往前挪（B→1、C→2）",
          board.queue_position(b.task_id) == 1 and board.queue_position(c.task_id) == 2)
    check("已收口任务的队列位是 None", board.queue_position(a.task_id) is None)
    check("收口任务状态 done", a.state == TASK_DONE, a.state)


def case_completed_not_candidate():
    """已完成不进相关候选：候选集只含在跑任务；打分器指向已完结任务 → 不认。"""
    board = TaskBoard(relevance_fn=lambda text, candidates: None)
    a = open_task(board, "温度调到22", 100)
    b = open_task(board, "开一下窗", 200)
    settle(board, a, 300)
    check("候选集只有在跑的 B", [t.task_id for t in board.candidates()] == [b.task_id],
          str([t.task_id for t in board.candidates()]))
    board.relevance_fn = lambda text, candidates: a.task_id     # 模型给了一条已做完的任务
    result = board.match("还是调到24", 400)
    check("已完结任务不算命中", result["hit_task_id"] is None, str(result))
    check("reason=bad_hit", result["reason"] == "bad_hit", str(result))
    check("记一次 relevance_bad_hit", board.counters["relevance_bad_hit"] == 1)
    check("不记成命中", board.counters["relevance_hits"] == 0)


def case_no_scorer_no_call():
    """接口位默认不接真调用：没有 scorer 就一次调用都不发，也不发事件。"""
    board = TaskBoard()
    open_task(board, "温度调到22", 100)
    events = []
    board.on_event = events.append
    result = board.match("还是调到24", 200)
    check("reason=no_scorer", result["reason"] == "no_scorer", str(result))
    check("零次打分调用", board.counters["relevance_calls"] == 0)
    check("不发 task_relevance 事件（没打分就没有记账）",
          not [e for e in events if e["event"] == "task_relevance"], str(events))


def case_scorer_hit_and_miss():
    """接了 scorer：命中在跑任务 / 都不相关 / 没有候选时不发调用。"""
    calls = []

    def scorer(text, candidates):
        calls.append((text, [t.task_id for t in candidates]))
        return candidates[0].task_id if "同一件" in text else None

    board = TaskBoard(relevance_fn=scorer)
    a = open_task(board, "温度调到22", 100)
    hit = board.match("还是调到24（同一件事）", 200)
    check("命中：hit_task_id 给在跑任务", hit["hit_task_id"] == a.task_id, str(hit))
    check("reason=hit", hit["reason"] == "hit")
    miss = board.match("开一下窗", 300)
    check("都不相关：hit_task_id=None", miss["hit_task_id"] is None, str(miss))
    check("reason=none_relevant", miss["reason"] == "none_relevant")
    check("打分器看到的是候选集快照", calls[0][1] == [a.task_id], str(calls))
    check("记 2 次调用 / 1 命中 / 1 不相关",
          (board.counters["relevance_calls"], board.counters["relevance_hits"],
           board.counters["relevance_misses"]) == (2, 1, 1),
          str(board.counters))
    settle(board, a, 400)
    board.match("再说一句", 500)
    check("没有在跑任务 → 不发打分调用", board.counters["relevance_calls"] == 2,
          str(board.counters))
    check("reason=no_candidate", board.last_match["reason"] == "no_candidate",
          str(board.last_match))


def case_scorer_error_is_not_fatal():
    """打分器炸了也不许打断决策：记错误、当没命中。"""
    def boom(text, candidates):
        raise RuntimeError("scorer down")

    board = TaskBoard(relevance_fn=boom)
    open_task(board, "温度调到22", 100)
    result = board.match("还是调到24", 200)
    check("reason=scorer_error", result["reason"] == "scorer_error", str(result))
    check("hit_task_id=None（不命中不抛）", result["hit_task_id"] is None)
    check("记一次 relevance_errors", board.counters["relevance_errors"] == 1)


def case_settle_policy():
    """收口判据：有执行过 → done；全被作废 → abandoned；会话收口兜底不留 open。"""
    board = TaskBoard()
    done_task = open_task(board, "打开座椅加热", 100)
    settle(board, done_task, 200, "executed")
    check("执行过 → done", done_task.state == TASK_DONE, done_task.state)
    gone_task = open_task(board, "开一下窗", 300)
    settle(board, gone_task, 400, "abandoned")
    check("全作废 → abandoned", gone_task.state == TASK_ABANDONED, gone_task.state)
    silent = open_task(board, "找附近吃饭", 500)             # 没有 AU 的空任务
    board.close_running(600, "session_end")
    check("会话收口把没 AU 的任务也结掉", silent.state in (TASK_DONE, TASK_ABANDONED))
    snap = board.snapshot()
    check("task_open=0", snap["task_open"] == 0, str(snap["task_open"]))
    check("task_eq 成立", snap["task_eq"] is True, str(snap))
    check("任务总数＝done+abandoned",
          snap["task_total"] == snap["task_done"] + snap["task_abandoned"], str(snap))


def case_brain_task_lifecycle():
    """brain 级：一条请求＝一条任务，IU/AU 挂上去，落地后任务收口，且 A6 账不破。"""
    brain = new_brain()
    outcome = say(brain, "D", "打开座椅加热", 100)
    decision = outcome["decision"]
    check("开出一条任务", brain.tasks.snapshot()["task_total"] == 1)
    task = brain.tasks.get("task-001")
    check("任务 id 挂到 IU 上", brain.ius[0].task_id == "task-001", str(brain.ius[0].task_id))
    check("IU 挂在任务下", task.iu_ids == [brain.ius[0].iu_id], str(task.iu_ids))
    check("AU 挂在任务下", task.au_ids == [decision["au_id"]], str(task.au_ids))
    check("未应用前任务仍在跑", task.state == TASK_RUNNING)
    check("队列位 1", brain.tasks.queue_position(task.task_id) == 1)
    applied(brain, "D", decision, 350)
    snap = brain.snapshot()
    check("落地后任务收口", task.state == TASK_DONE, task.state)
    check("task_open=0 / task_eq", snap["task_open"] == 0 and snap["task_eq"] is True, str(snap))
    check("AU 都被挂到任务下（au_unassigned 0）", snap["au_unassigned"] == 0)
    check("A6 账没被动：iu_eq 仍成立", snap["iu_eq"] is True, str(snap))
    audit = brain.audit()
    check("结算后仍平", audit["task_eq"] is True and audit["iu_eq"] is True, str(audit))
    names = [event["event"] for event in brain.events]
    check("任务事件与 IU/AU 同流", {"task_open", "task_au_attached", "task_close"} <= set(names),
          str(sorted(set(names))))


def case_two_requests_two_tasks():
    """两条请求＝两条任务，先后可见；收口后两条都结算、队列清空。"""
    brain = new_brain()
    first = say(brain, "D", "温度调到22", 100)["decision"]
    second = say(brain, "P", "开一下窗", 200)["decision"]
    snap = brain.snapshot()
    check("两条在跑任务", snap["task_open"] == 2, str(snap["task_open"]))
    check("队列位按先来后到",
          [item["queue_pos"] for item in snap["task_queue"]] == [1, 2], str(snap["task_queue"]))
    check("候选集＝两条在跑任务", snap["task_candidates"] == ["task-001", "task-002"],
          str(snap["task_candidates"]))
    applied(brain, "D", first, 300)
    applied(brain, "P", second, 400)
    audit = brain.audit()
    check("两条都 done", audit["task_done"] == 2 and audit["task_abandoned"] == 0, str(audit))
    check("task_eq / iu_eq 双平", audit["task_eq"] is True and audit["iu_eq"] is True)
    check("队列清空", audit["task_queue"] == [])


def case_scoring_hit_merges_into_running_task():
    """接了 scorer 且命中：新话并进那条在跑任务，不开新任务；两条 AU 都照常执行、账平。"""
    board = TaskBoard(relevance_fn=lambda text, candidates: candidates[0].task_id)
    brain = new_brain(board=board)
    first = say(brain, "D", "温度调到22", 100)["decision"]
    second = say(brain, "P", "开一下窗", 200)["decision"]     # scorer 命中首候选 task-001
    snap = brain.snapshot()
    check("命中 → 不开新任务（task_total 仍 1）", snap["task_total"] == 1, str(snap))
    check("记 1 次合并", snap["tasks_merged"] == 1, str(snap))
    check("两条 IU 挂在同一条任务下",
          board.get("task-001").iu_ids == [brain.ius[0].iu_id, brain.ius[1].iu_id],
          str(board.get("task-001").iu_ids))
    check("两条 AU 挂在同一条任务下",
          board.get("task-001").au_ids == [first["au_id"], second["au_id"]],
          str(board.get("task-001").au_ids))
    check("在跑任务仍 1 条", snap["task_open"] == 1, str(snap))
    applied(brain, "D", first, 300)
    applied(brain, "P", second, 400)
    audit = brain.audit()
    check("两条 AU 都执行（合并不取消执行）", audit["au_executed"] == 2, str(audit))
    check("一条任务收口 done", audit["task_done"] == 1 and audit["task_total"] == 1, str(audit))
    check("账平", audit["task_eq"] is True and audit["iu_eq"] is True, str(audit))
    check("task_merged 事件进流水", any(e["event"] == "task_merged" for e in brain.events))
    check("命中只可能是当时在跑的任务",
          board.counters["relevance_bad_hit"] == 0, str(board.counters))


def case_scoring_none_relevant_opens_new_task():
    """接了 scorer 但都不相关：新开一条任务（FIFO），不合并、不占别人的队列位。"""
    board = TaskBoard(relevance_fn=lambda text, candidates: None)
    brain = new_brain(board=board)
    first = say(brain, "D", "温度调到22", 100)["decision"]
    second = say(brain, "P", "开一下窗", 200)["decision"]
    snap = brain.snapshot()
    check("都不相关 → 两条任务", snap["task_total"] == 2 and snap["task_open"] == 2, str(snap))
    check("没有合并", snap["tasks_merged"] == 0, str(snap))
    check("队列位 1/2（FIFO）", [item["queue_pos"] for item in snap["task_queue"]] == [1, 2],
          str(snap["task_queue"]))
    check("各自挂自己的 AU",
          board.get("task-001").au_ids == [first["au_id"]]
          and board.get("task-002").au_ids == [second["au_id"]])
    applied(brain, "D", first, 300)
    applied(brain, "P", second, 400)
    audit = brain.audit()
    check("两条各自收口、账平", audit["task_eq"] is True and audit["iu_eq"] is True, str(audit))


def case_merge_into_rejects_closed_task():
    """merge_into 只对在跑任务生效：已收口 → 拒绝（None）；在跑 → 接合并、不占新队列位。"""
    board = TaskBoard()
    a = open_task(board, "温度调到22", 100)
    settle(board, a, 200)                       # a 收口 done
    result = board.merge_into(a.task_id, "iu-x", 300)
    check("已收口任务不接合并", result is None, str(result))
    check("不记合并", board.counters["tasks_merged"] == 0, str(board.counters))
    b = open_task(board, "开一下窗", 400)
    result = board.merge_into(b.task_id, "iu-y", 500)
    check("在跑任务可接合并", result is b and b.iu_ids == ["iu-y"], str(b.iu_ids))
    check("记 1 次合并", board.counters["tasks_merged"] == 1, str(board.counters))
    check("合并不占新队列位", board.queue_position(b.task_id) == 1, str(board.queue_position(b.task_id)))


def case_decide_error_on_merged_task_keeps_original():
    """并进的任务决策失败：只结掉新 IU，不把原任务（还有自己的 AU）一起放弃。"""
    def flaky(utterance, *a, **k):
        raise RuntimeError("model down")

    board = TaskBoard(relevance_fn=lambda text, candidates: candidates[0].task_id)
    brain = new_brain(board=board)
    first = say(brain, "D", "温度调到22", 100)["decision"]     # task-001 正常开出、挂 AU
    brain.decide_fn = flaky
    raised = False
    try:
        say(brain, "P", "开一下窗", 200)                        # 命中 task-001 → 合并后决策失败
    except RuntimeError:
        raised = True
    check("异常照旧抛出", raised)
    task = board.get("task-001")
    check("原任务仍在跑（有自己的 AU，不被误结）", task is not None and task.running,
          task.state if task else "missing")
    check("原 AU 还挂着", task.au_ids == [first["au_id"]], str(task.au_ids))
    applied(brain, "D", first, 300)
    check("原任务正常收口 done", task.state == TASK_DONE, task.state)


def case_cancel_settles_task():
    """决策被作废（投机 stale / 打断）→ 任务记 abandoned，账照样平。"""
    brain = new_brain()
    say(brain, "D", "打开座椅加热", 100)
    brain.mark_now(200)
    brain.observe({"event": "llm_cancel", "t_ms": 200, "seat_id": "D", "reason": "stale_text"})
    snap = brain.snapshot()
    check("任务收口为 abandoned", brain.tasks.get("task-001").state == TASK_ABANDONED,
          brain.tasks.get("task-001").state)
    check("task_open=0", snap["task_open"] == 0, str(snap["task_open"]))
    check("au_abandoned 记 1 且账平", snap["au_abandoned"] == 1 and snap["iu_eq"] is True, str(snap))


def case_decide_error_closes_task():
    """决策抛异常：任务当场结掉（不留空跑任务占候选位），异常照旧抛给调用方。"""
    brain = new_brain(decide=make_decide(raises=RuntimeError("model down")))
    raised = False
    try:
        say(brain, "D", "打开座椅加热", 100)
    except RuntimeError:
        raised = True
    task = brain.tasks.get("task-001")
    check("异常照旧抛出", raised)
    check("任务记 abandoned", task is not None and task.state == TASK_ABANDONED,
          task.state if task else "missing")
    check("收口原因=decide_error", task.close_why == "decide_error", str(task.close_why))
    check("没有 AU 挂着", task.au_ids == [], str(task.au_ids))
    check("task_open=0", brain.snapshot()["task_open"] == 0)


def case_no_target_reference_has_no_task():
    """会话刚开始的「我也要」（无指代对象）＝一次回问，不是一条任务——别拿它占候选位。"""
    brain = new_brain()
    outcome = say(brain, "P", "我也要", 100)
    check("回问（ask_user）", outcome["decision"].get("kind") == "ask_user",
          str(outcome["decision"]))
    check("不产生任务", brain.tasks.snapshot()["task_total"] == 0,
          str(brain.tasks.snapshot()["task_total"]))
    check("也不产生 IU", brain.ius == [])


def case_golden_examples_task_layer():
    """已拍示例 3「各做各的」在任务层的现状：A 还在跑 → B「开一下窗」＝两条任务（明显不同任务不合并）。

    这里用无打分器的 brain（`relevance_fn=None` → 不打分 → 都不合并），钉住「明显不同 → 两条任务」。
    示例 1「还是调到24 → 覆盖 A」/ 示例 2「别吃辣的 → 合进 A」的「并进同一条任务」由
    `case_scoring_hit_merges_into_running_task` 钉；覆盖式冲突（后值覆盖先值）仍归 P2-3 / C3。
    """
    board = TaskBoard()
    brain = new_brain(board=board)
    first = say(brain, "D", "温度调到22", 100)["decision"]
    brain.mark_now(150)
    second = say(brain, "P", "开一下窗", 200)["decision"]     # A 还在执行中 → 但 B 是新任务
    snap = brain.snapshot()
    check("A 仍在跑（未应用）", board.get("task-001").state == TASK_RUNNING)
    check("B 已另开一条任务", board.get("task-002") is not None)
    check("两条任务同时在跑，队列位 1/2",
          [item["queue_pos"] for item in snap["task_queue"]] == [1, 2], str(snap["task_queue"]))
    check("两条任务各自挂自己的 AU",
          board.get("task-001").au_ids == [first["au_id"]]
          and board.get("task-002").au_ids == [second["au_id"]])
    applied(brain, "P", second, 300)
    check("B 先落地只结 B", board.get("task-002").state == TASK_DONE
          and board.get("task-001").state == TASK_RUNNING)
    applied(brain, "D", first, 400)
    audit = brain.audit()
    check("两条都结、账平", audit["task_eq"] is True and audit["iu_eq"] is True, str(audit))


# ---------------------------------------------------------------- 主入口

CASES = (case_fifo_queue, case_completed_not_candidate, case_no_scorer_no_call,
         case_scorer_hit_and_miss, case_scorer_error_is_not_fatal, case_settle_policy,
         case_brain_task_lifecycle, case_two_requests_two_tasks,
         case_scoring_hit_merges_into_running_task, case_scoring_none_relevant_opens_new_task,
         case_merge_into_rejects_closed_task, case_decide_error_on_merged_task_keeps_original,
         case_cancel_settles_task, case_decide_error_closes_task,
         case_no_target_reference_has_no_task, case_golden_examples_task_layer)


def main():
    for case in CASES:
        print("[%s]" % case.__name__)
        case()
    passed = sum(1 for _, ok, _ in RESULTS if ok)
    total = len(RESULTS)
    print("\n%d/%d 例通过" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
