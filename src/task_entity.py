"""任务实体（P2-1 最小骨架）：运行中任务集合 / 已完成不进候选 / 先来后到队列位 / 相关性打分接口位。

口径来源：wiki `[[双路ASR]]` §4 Q4 注、`[[无确认窗流程]]` §4，拍板原文
`raw/2026-11-03-dual-related-model-decision.md`（总原则），偏差盘点 `poc-no-confirm-gap-DETAIL.md` P2-1。

三件事（本模块只做这三件，**不碰判定与调度**）：

1. **任何一条用户请求都当一条任务**。任务是 IU / AU 的上一层容器：
   `任务 → 若干 IU/AU → 结算`（现在是 `段 → IU → AU`，缺这一层）。
2. **候选集只含还在运行的任务**：`candidates()` 就是在跑任务集合，已结算（`done` / `abandoned`）
   的任务**不进候选、不参与打分**——这条在 `match()` 的边界上强制（打分器给一条已完结任务
   不算命中，记 `relevance_bad_hit`），不靠调用方自觉。
3. **任务之间先来后到**：`queue_no` 单调（先开先小），队列位 `queue_pos` = 在运行任务里的 1 起序号；
   任务之间不做任何额外逻辑。

**相关性打分只留接口位（本单不接真调用）**：`TaskBoard(relevance_fn=...)`，
签名 `relevance_fn(utterance, candidates) -> task_id | None`——就是「判停那个模型对新话 vs
在跑任务打相关分，输出选中哪一个任务 / 都不相关」的落点。默认 `None` → **一次调用都不发**
（`match()` 返回 `reason="no_scorer"`）。打分结果**只记账、不改行为**：合并 / 覆盖 / 新开
（gap P2-2 / P2-3）本单不实现，`SessionBrain` 只把结果发布成 `task_relevance` 事件。

未定 / 本单不选（待拍项，别在这里代拍）：
- **打分频次**（gap P2-2 ④）：每句都打 / 只在有任务在跑时打。现状＝有在跑任务才可能发调用
  （没有候选就没有可命中的对象，不是择一），两个分支的差别只是调用方在哪一行调 `match()`。
- **`_ANAPHORA` 词表去留**：本模块不动它；职责在 `orchestra.py`，归 P2-2。
- 覆盖式冲突（P2-3）、双席顺序与回执全文（P2-4）、任何 UI。

并发：与 `SessionBrain` 的 IU/AU 同一条约束——**本模块不加锁**，由 `SessionBrain` 的 `_lock`
串起来（`decide()` 在席 worker 线程、`observe()`/`tick()`/`settle()` 在主线程）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

TASK_RUNNING = "running"
TASK_DONE = "done"
TASK_ABANDONED = "abandoned"
SETTLED = (TASK_DONE, TASK_ABANDONED)


@dataclass
class Task:
    """一条任务：一条用户请求 + 它下面的 IU / AU + 结算状态。"""
    task_id: str
    queue_no: int                  # 先来后到序号（单调、不复用）
    seat: str
    text: str
    t_open: int
    iu_ids: list = field(default_factory=list)
    au_ids: list = field(default_factory=list)
    state: str = TASK_RUNNING      # running | done | abandoned
    executed: int = 0              # 挂在本任务下、已执行的 AU 数
    abandoned: int = 0             # 挂在本任务下、已作废的 AU 数
    t_close: int = None
    close_why: str = None

    @property
    def running(self) -> bool:
        return self.state == TASK_RUNNING

    @property
    def pending_au(self) -> int:
        """还没结算的 AU 数（>0 表示这条任务仍在跑）。"""
        return len(self.au_ids) - self.executed - self.abandoned

    def as_dict(self) -> dict:
        return {"task_id": self.task_id, "queue_no": self.queue_no, "seat": self.seat,
                "text": self.text, "state": self.state, "iu_ids": list(self.iu_ids),
                "au_ids": list(self.au_ids), "pending_au": self.pending_au,
                "executed": self.executed, "abandoned": self.abandoned,
                "t_open": self.t_open, "t_close": self.t_close, "close_why": self.close_why}


class TaskBoard:
    """一次会话一份的任务台账（运行中集合 / 候选集 / FIFO / 打分接口位）。"""

    def __init__(self, *, relevance_fn=None, on_event=None):
        #: 相关性打分的接口位：`fn(utterance, candidates) -> task_id | None`。默认不接真调用。
        self.relevance_fn = relevance_fn
        #: 事件出口（`SessionBrain._on_task_event` 注入，与 IU/AU 事件同流）。
        self.on_event = on_event
        self.tasks: list = []              # 全量台账（含已结算；候选集不用它）
        self.last_match: dict = None       # 最近一次打分结果（观测用）
        self._seq = 0
        self._queue = 0
        self.counters = {"tasks_opened": 0, "tasks_done": 0, "tasks_abandoned": 0,
                         "au_attached": 0, "au_settled": 0, "au_unassigned": 0,
                         "relevance_calls": 0, "relevance_hits": 0, "relevance_misses": 0,
                         "relevance_bad_hit": 0, "relevance_errors": 0}

    # ---------- 生命周期 ----------

    def open_task(self, seat: str, text: str, now: int) -> Task:
        """开一条任务：任何一条进入编排层的用户请求都走这里。"""
        self._seq += 1
        self._queue += 1
        task = Task(task_id="task-%03d" % self._seq, queue_no=self._queue, seat=seat,
                    text=text or "", t_open=int(now or 0))
        self.tasks.append(task)
        self.counters["tasks_opened"] += 1
        self._emit("task_open", now, task_id=task.task_id, queue_no=task.queue_no, seat=seat,
                   text=task.text, queue_pos=self.queue_position(task.task_id))
        return task

    def attach_au(self, task_id: str, au_id: str, now: int = None) -> Task:
        """把一次决策产生的 AU 挂到任务下面（一条任务可以挂多个 IU/AU）。"""
        task = self._task(task_id)
        if task is None:
            # 没有任务可挂 = 账目裂缝（AU 会漂在任务之外），计数而不是静默吞掉。
            self.counters["au_unassigned"] += 1
            return None
        task.au_ids.append(au_id)
        self.counters["au_attached"] += 1
        self._emit("task_au_attached", now, task_id=task.task_id, au_id=au_id,
                   queue_pos=self.queue_position(task.task_id))
        return task

    def attach_iu(self, task_id: str, iu_id: str) -> Task:
        task = self._task(task_id)
        if task is None:
            return None
        if iu_id not in task.iu_ids:
            task.iu_ids.append(iu_id)
        return task

    def settle_au(self, au_id: str, now: int, outcome: str) -> Task:
        """某个 AU 结算（`executed` / `abandoned`）：从在途里划掉；任务空了就收口。"""
        task = None
        for candidate in self.tasks:
            if au_id in candidate.au_ids:
                task = candidate
                break
        if task is None:
            self.counters["au_unassigned"] += 1
            return None
        if outcome == "executed":
            task.executed += 1
        elif outcome == "abandoned":
            task.abandoned += 1
        else:
            return task
        self.counters["au_settled"] += 1
        if task.running and task.au_ids and task.pending_au == 0:
            # 全部 AU 结算完就是做完：有过执行记 done，全被作废记 abandoned。
            self.close_task(task.task_id, now, None, "au_settled")
        return task

    def close_task(self, task_id: str, now: int, state: str = None, why: str = None) -> Task:
        """收口一条任务。`state` 不给就按「有没有真的执行过」推（done / abandoned）。"""
        task = self._task(task_id)
        if task is None or not task.running:
            return None
        if state not in SETTLED:
            state = TASK_DONE if task.executed else TASK_ABANDONED
        task.state = state
        task.t_close = int(now or 0)
        task.close_why = why
        self.counters["tasks_done" if state == TASK_DONE else "tasks_abandoned"] += 1
        self._emit("task_close", now, task_id=task.task_id, state=state, why=why,
                   executed=task.executed, abandoned=task.abandoned)
        return task

    def close_running(self, now: int, why: str = "session_end") -> list:
        """会话收口兜底：会话结束时还在跑的任务一律结掉（漏结会让 `task_open` 收不拢）。"""
        closed = []
        for task in self.running():
            closed.append(self.close_task(task.task_id, now, None, why))
        return [task for task in closed if task is not None]

    # ---------- 视图：运行中集合 / 候选集 / 队列位 ----------

    def running(self) -> list:
        """运行中任务集合，按 `queue_no` 升序（append 顺序天然是先来后到）。"""
        return [task for task in self.tasks if task.running]

    def candidates(self) -> list:
        """相关性打分的候选集＝运行中任务。**已做完的不进候选、不参与打分。**"""
        return self.running()

    def queue_position(self, task_id: str):
        """队列位（1 起，在运行任务里的次序）；已结算 / 未知 → None。"""
        for index, task in enumerate(self.running()):
            if task.task_id == task_id:
                return index + 1
        return None

    def get(self, task_id: str):
        return self._task(task_id)

    # ---------- 相关性打分（接口位，本单不接真调用） ----------

    def match(self, utterance: str, now: int) -> dict:
        """新话 vs 正在跑的任务：**唯一**的相关性打分入口。

        `relevance_fn(utterance, candidates) -> task_id | None`：
        - 返回某个在跑任务的 id → 命中（"并进这一件"）；
        - 返回 `None` → 都不相关（＝新开一条任务，先来后到）；
        - 返回不在候选集里的 id（例如一条已做完的任务）→ **不算命中**，记 `relevance_bad_hit`。

        只记账、不改行为：合并 / 覆盖 / 新开归 P2-2 / P2-3；调用方拿结果也只该记账。
        没接打分器（`relevance_fn is None`）或没有在跑任务时**不发起任何调用**。
        """
        candidates = self.candidates()
        result = {"hit_task_id": None, "reason": "no_scorer", "candidates": [],
                  "text": utterance or ""}
        if self.relevance_fn is None:
            self.last_match = result
            return result
        result["candidates"] = [task.task_id for task in candidates]
        if not candidates:
            result["reason"] = "no_candidate"
            self.last_match = result
            return result
        self.counters["relevance_calls"] += 1
        try:
            answer = self.relevance_fn(utterance, candidates)
        except Exception:  # noqa: BLE001 - 打分失败不许打断决策（与时钟同一条约束）
            self.counters["relevance_errors"] += 1
            result["reason"] = "scorer_error"
            self.last_match = result
            self._emit("task_relevance", now, **result)
            return result
        hit = answer.get("task_id") if isinstance(answer, dict) else answer
        if hit is not None and hit not in result["candidates"]:
            # 已做完的任务不进候选：模型给了也不认（这条是「不进候选」的硬边界）。
            self.counters["relevance_bad_hit"] += 1
            result["reason"] = "bad_hit"
            result["rejected_task_id"] = hit
        elif hit is None:
            self.counters["relevance_misses"] += 1
            result["reason"] = "none_relevant"
        else:
            self.counters["relevance_hits"] += 1
            result["reason"] = "hit"
            result["hit_task_id"] = hit
        self.last_match = result
        self._emit("task_relevance", now, **result)
        return result

    # ---------- 账 / 观测 ----------

    def snapshot(self) -> dict:
        """任务层真值。`task_eq` 与 A6 的 `iu_eq` 并列读：**任务层不破 IU/AU 层的账**。"""
        running = self.running()
        settled = [task for task in self.tasks if not task.running]
        done = [task for task in settled if task.state == TASK_DONE]
        abandoned = [task for task in settled if task.state == TASK_ABANDONED]
        unassigned = int(self.counters["au_unassigned"])
        return {
            "task_total": len(self.tasks),
            "task_open": len(running),                # 只在全部收口后为 0（与 iu_open 同读法）
            "task_done": len(done),
            "task_abandoned": len(abandoned),
            "task_eq": (not running) and len(settled) == len(done) + len(abandoned)
                       and unassigned == 0,
            "task_queue": [{"task_id": task.task_id, "queue_no": task.queue_no,
                            "queue_pos": index + 1, "seat": task.seat, "text": task.text,
                            "pending_au": task.pending_au}
                           for index, task in enumerate(running)],
            "task_candidates": [task.task_id for task in running],
            "task_last_match": dict(self.last_match) if self.last_match else None,
            "tasks": [task.as_dict() for task in self.tasks],
            **self.counters,
        }

    # ---------- 内部 ----------

    def _task(self, task_id):
        if not task_id:
            return None
        for task in self.tasks:
            if task.task_id == task_id:
                return task
        return None

    def _emit(self, name, t_ms, **payload):
        event = {"event": name, "t_ms": t_ms}
        event.update(payload)
        if self.on_event:
            self.on_event(event)
