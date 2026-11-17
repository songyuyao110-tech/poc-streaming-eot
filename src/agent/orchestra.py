"""会话脑（C2）：最近动作台账 + 在途 IU + 单一决策层 + IU/AU 对账。

C1 之后两席各自判停、各自把段交给 `decide`；C2 把「做决定」这件事挪到编排层的**一个入口**，
并给会话一份只活一次会话的记忆。三层结构（定稿 `refine-spec` §4.1）：

    席 D 段 ─┐
             ├─▶ SessionBrain.decide(seat, …)          ← 唯一决策入口（一个 AU 只决策一次）
    席 P 段 ─┘        │
                      ├─ 任务：新话 vs 在跑任务打相关分——命中并进那条、都不相关新开（FIFO，`task_entity.py`）
                      ├─ 指代解析：短附和（「我也要」）→ 最近动作台账
                      ├─ 在途 IU：目标 + 参与者 + 段；同目标未落地 → 并入同一 AU
                      ├─ AU：一次决策 + 一次执行 + 一条回执（执行面一次一个动作）
                      └─ 台账：落地后的动作可被下一段指代

    ASR 段 ─▶ 席自己的 TurnSession（判停 / 消费游标不变）─▶ 决策入口由本模块提供
    `llm_decision` / `llm_cancel` 事件 ─▶ 回填 AU 状态（应用 / 作废 / 结算）

口径要点（都可追到定稿 §4.1 §4.3 A2 / A6）：

- **动作未落地前到达的「我也要」并入同一 AU**：一次执行、作用到发起席与附和席两座；
  **已落地后**到达的「我也要」＝一次新 AU，只作用说话人自己的座位，不重发别的座。
- **无最近动作时说「我也要」**：恰 1 次回问，不瞎猜（不产生动作）；再问就不追问第二次。
- **账**：`iu_open == 0` 且 `iu_committed == au_executed + au_abandoned`（会话收口时结算）。
- **执行面一次一个动作**：mock 执行器按 AU 的 `scope` 落地，回执写清发起席 / 作用席 / 是否合并。
- 8 池高危工具**不由本卡执行**（跨席确认路径 C3 才做）：决策标记 `awaiting_confirm`，
  结算时记为放弃，`direct_exec` 保持 0。

不做（定稿 §8 C2 的「不做什么」）：跨会话记忆、多任务并发、按席决策器、判帧与覆盖式冲突（C3）、
游戏框架（C4）、TTS。

指代解析的边界（诚实标注，别当已达标）：**本轮只认短附和词表**（`_ANAPHORA`，全部 ≤8 字、
不含否定词）。带明确对象的附和（如「我也要通风」）留给 C3 的 agent 判帧 / 归并，本模块不猜。
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import sys
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.dont_write_bytecode = True

from agent.spoken import spoken_line
from agent.task_entity import TaskBoard
from shared.text import normalize

SEAT_ORDER = ("D", "P")
DEFAULT_MERGE_WINDOW_MS = 2500      # 定稿 §5：第二席同时开口最多等一个归并窗
DEFAULT_RECENT_LIMIT = 8            # 台账长度：够指代、不无限长

# 短附和词表（normalize 后精确匹配）。只收「自己不带动作对象」的指代句。
_ANAPHORA = frozenset({
    "我也要", "我也要一个", "我也要那个", "我也要一样", "我也要一样的",
    "我也", "我也来", "我也来一个", "我也一样", "我也需要",
    "也一样", "一样", "同款", "加我一个", "加上我", "给我也来一个",
})
_NEGATIVE = ("不", "别", "取消", "关", "停")

# 工具 → 屏上回执里的中文对象（只覆盖本卡会碰到的；未知工具直接用工具名，不编造语义）
_TOOL_LABELS = {
    "climate_set_power": "空调",
    "seat_heating": "座椅加热",
}


def is_reference(text: str) -> bool:
    """这一段是不是「不带对象的短附和」＝一次指代（不是一条完整指令）。"""
    norm = normalize(text)
    if not norm or len(norm) > 8:
        return False
    if any(word in norm for word in _NEGATIVE):
        return False
    return norm in _ANAPHORA


def is_anaphora_prefix(text: str) -> bool:
    """开口的头几个字是不是在往「附和」上说（「我」→「我也」→「我也要」）。

    归并窗判据用它：第二席**同时开口**、且开口字像是在附和，就把另一席的 AU 卡进窗里等一下；
    开口字不像附和（「打…」「调…」）就不挡，分控场景不被拖。
    """
    norm = normalize(text)
    if not norm or len(norm) > 8:
        return False
    if any(word in norm for word in _NEGATIVE):
        return False
    return any(phrase.startswith(norm) for phrase in _ANAPHORA)


@dataclass
class IntentUnit:
    """在途意图单元：一个目标 + 一组参与者 + 已进这个 IU 的段。"""
    iu_id: str
    seat: str
    text: str
    t_open: int
    goal: str = None
    participants: list = field(default_factory=list)
    segments: list = field(default_factory=list)
    status: str = "open"            # open | committed
    au_id: str = None
    t_close: int = None
    task_id: str = None             # 所属任务（`task_entity.TaskBoard`，P2-1）

    def as_dict(self):
        return {"iu_id": self.iu_id, "seat": self.seat, "text": self.text, "goal": self.goal,
                "participants": list(self.participants), "segments": list(self.segments),
                "status": self.status, "au_id": self.au_id, "t_open": self.t_open,
                "t_close": self.t_close, "task_id": self.task_id}


@dataclass
class ActionUnit:
    """动作单元：一次决策 + 一次执行 + 一条回执。"""
    au_id: str
    iu_id: str
    initiator: str
    participants: list
    scope: list
    kind: str
    tool: str
    arguments: dict
    decision: dict
    t_decided: int
    inherited_from: str = None
    merged: bool = False
    state: str = "decided"          # decided | held | executed | awaiting_confirm | abandoned
    receipt: str = ""               # 办理记录（只进台账，不上屏）
    spoken: str = ""                # 对人说的那一句（办完上屏的就是它；`agent/spoken.py`）
    t_executed: int = None
    applied_ms: int = None
    merge_deadline_ms: int = None
    abandon_why: str = None

    def as_dict(self):
        return {"au_id": self.au_id, "iu_id": self.iu_id, "initiator": self.initiator,
                "participants": list(self.participants), "scope": list(self.scope),
                "kind": self.kind, "tool": self.tool, "arguments": dict(self.arguments or {}),
                "state": self.state, "merged": self.merged,
                "inherited_from": self.inherited_from, "receipt": self.receipt,
                "spoken": self.spoken,
                "t_decided": self.t_decided, "t_executed": self.t_executed,
                "merge_deadline_ms": self.merge_deadline_ms, "abandon_why": self.abandon_why}


class SessionBrain:
    """一个会话一份：最近动作台账 + 在途 IU + 单一决策入口 + 对账。

    线程约束与 `TurnSession` 一致：`decide()` 在席的 worker 线程里跑（只动本模块状态，
    用锁串起来）；`observe()` / `tick()` / `settle()` 在调用 `tick()` 的主线程里跑。
    """

    def __init__(self, decide_fn, *, merge_window_ms: int = DEFAULT_MERGE_WINDOW_MS,
                 recent_limit: int = DEFAULT_RECENT_LIMIT, on_event=None, clock=None,
                 task_board=None, relevance_fn=None):
        self.decide_fn = decide_fn
        self.merge_window_ms = int(merge_window_ms)
        self.recent_limit = int(recent_limit)
        self.on_event = on_event
        self.clock = clock or (lambda: 0)
        self.events = []
        self.ius = []
        self.aus = []
        self.recent = []                      # 最近动作台账（已落地 AU，最新在末尾）
        # 任务实体（P2-1，`task_entity.py`）：运行中集合 / 已完成不进候选 / 先来后到。
        # 默认建一份空台账（`relevance_fn=None` → 一次调用都不发）；要挂「判停模型打相关分」
        # 就传 `relevance_fn`（真调用见 `relevance.build_relevance_fn`，或直接传 `task_board`）。
        self.tasks = task_board or TaskBoard(relevance_fn=relevance_fn)
        if self.tasks.on_event is None:
            self.tasks.on_event = self._on_task_event
        self._lock = threading.RLock()
        self._now_ms = 0
        self._seq = 0
        # seat -> {"text", "t_ms"}：本席已开口、且开口字还像「附和」（归并窗的候选）。
        # 注意 t_ms 记的是**第一次开口**的时刻：判「同时开口」用开口时刻，不用收敛时刻。
        self._pending_ref = {}
        self._unapplied = {seat: [] for seat in SEAT_ORDER}   # 已决策未应用（会被判 stale 作废）
        self._dispatched = {}                 # 动作键 (tool, args, scope) -> au_id
        self._asked_no_target = 0
        self._receipt_both = False
        self.counters = {"decide_calls": 0, "references": 0, "merges": 0, "absorbed": 0,
                         "ask_no_target": 0, "executed": 0, "abandoned": 0, "held": 0,
                         "repeat_action": 0, "wrong_seat": 0, "awaiting_confirm": 0}

    # ---------- 时钟 ----------

    def mark_now(self, now_ms: int) -> None:
        try:
            self._now_ms = max(self._now_ms, int(now_ms))
        except (TypeError, ValueError):
            pass

    def _now(self) -> int:
        if self._now_ms:
            return self._now_ms
        try:
            return int(self.clock() or 0)
        except Exception:  # noqa: BLE001 - 时钟出问题不该打断决策
            return 0

    # ---------- 决策入口（每席一个闭包，注入给该席的 decide_fn 槽） ----------

    def decide_fn_for(self, seat: str):
        def _decide(utterance, selected, schemas, tool_set, context=None, **kwargs):
            return self.decide(seat, utterance, selected, schemas, tool_set, context, **kwargs)
        _decide.__name__ = "brain_decide_" + seat
        return _decide

    def decide(self, seat, utterance, selected, schemas, tool_set, context=None, **kwargs):
        # 模型调用**不持锁**：decide 在席的 worker 线程里跑，持锁会把主线程的 tick/事件流拖住
        # （实测能拖住一个决策时延的量级）。锁只护状态变更。
        with self._lock:
            now = self._now()
            reference = is_reference(utterance)
            # 任务相关性打分：有 running 才打（`task_entity.match` 内守卫）；命中 → 并进那条任务，
            # 都不相关 / 无候选 / 无打分器 / 打分出错 → 新开。结果在直连路径消费，见 `_decide_direct`。
            match = self.tasks.match(utterance, now)
        if reference:
            with self._lock:
                return self._decide_reference(seat, utterance, now)
        hit_task_id = match.get("hit_task_id") if isinstance(match, dict) else None
        return self._decide_direct(seat, utterance, selected, schemas, tool_set, context, now,
                                   kwargs, merge_task_id=hit_task_id)

    # ---------- 指代 ----------

    def _decide_reference(self, seat, utterance, now):
        self.counters["references"] += 1
        self._pending_ref.setdefault(seat, {"text": normalize(utterance), "t_ms": now})
        self._pending_ref[seat]["text"] = normalize(utterance)
        target = self.recent_action()
        self._emit("orchestra_reference", now, seat=seat, text=utterance,
                   target=target.au_id if target else None,
                   target_state=target.state if target else None)
        if target is None:
            return self._ask_without_target(seat, utterance, now)
        if target.state in ("decided", "held"):
            return self._absorb(seat, target, utterance, now)
        return self._inherit(seat, target, utterance, now)

    def _ask_without_target(self, seat, utterance, now):
        """会话刚开始（或上一个动作已不可指代）：「恰 1 次回问，不瞎猜」。"""
        self.counters["ask_no_target"] += 1
        self._asked_no_target += 1
        self._emit("orchestra_no_target", now, seat=seat, text=utterance,
                   asked=self._asked_no_target)
        if self._asked_no_target > 1:
            # 已经问过一次了：不再循环追问，明确收住（不是动作，也不假装会做）。
            decision = {"kind": "done", "why": "repeat_reference_without_target"}
        else:
            decision = {"kind": "ask_user", "tool": "ask_user",
                        "arguments": {"question": "你想跟刚才一样做什么？先说一句要做的动作，我记下来。"},
                        "why": "no_recent_action"}
        return {"decision": decision, "llm_ms": 0.0,
                "log_fields": {"decision_head": "orchestra_no_target"},
                "iu_id": None, "au_id": None, "participants": [], "scope": []}

    def _absorb(self, seat, target, utterance, now):
        """未落地（decided / held）时到达的附和：并入同一 AU，一次执行到两座。"""
        self.counters["merges"] += 1
        self.counters["absorbed"] += 1
        if seat not in target.participants:
            target.participants.append(seat)
        if seat not in target.scope:
            target.scope.append(seat)
        target.merged = True
        iu = self._iu(target.iu_id)
        if iu is not None:
            iu.participants = list(target.participants)
            iu.segments.append(utterance)
            iu.text = (iu.text + "｜" + utterance) if iu.text else utterance
        decision = dict(target.decision or {})
        decision.update({"absorbed": True, "iu_id": target.iu_id, "au_id": target.au_id,
                         "seat": seat, "participants": list(target.participants),
                         "scope": list(target.scope), "merged": True})
        self._emit("au_merged", now, au_id=target.au_id, iu_id=target.iu_id, seat=seat,
                   participants=list(target.participants), scope=list(target.scope),
                   text=utterance)
        if target.state == "held":
            # 目标已经应用、正卡在归并窗：现在人齐了，立刻落地（一次动作、两座）。
            target.state = "decided"
            self._execute(target, now)
        return {"decision": decision, "llm_ms": 0.0,
                "log_fields": {"decision_head": "orchestra_absorb"},
                "iu_id": target.iu_id, "au_id": target.au_id,
                "participants": list(target.participants), "scope": list(target.scope)}

    def _inherit(self, seat, target, utterance, now):
        """已落地后到达的附和：凭台账开一次新 AU，只作用说话人自己的座位。"""
        self.counters["merges"] += 0
        iu = self._open_iu(seat, utterance, now, goal=target.tool or target.kind)
        decision = dict(target.decision or {})
        decision.update({"inherited_from": target.au_id, "iu_id": iu.iu_id})
        au = self._new_au(iu, seat, decision, now, tool=target.tool,
                          arguments=dict(target.arguments or {}), inherited_from=target.au_id)
        decision.update({"iu_id": iu.iu_id, "au_id": au.au_id, "seat": seat,
                         "participants": [seat], "scope": [seat]})
        self._emit("au_inherited", now, au_id=au.au_id, iu_id=iu.iu_id, seat=seat,
                   inherited_from=target.au_id, tool=au.tool, text=utterance)
        return {"decision": decision, "llm_ms": 0.0,
                "log_fields": {"decision_head": "orchestra_inherit"},
                "iu_id": iu.iu_id, "au_id": au.au_id,
                "participants": [seat], "scope": [seat]}

    # ---------- 直连决策（唯一模型入口） ----------

    def _decide_direct(self, seat, utterance, selected, schemas, tool_set, context, now, kwargs,
                       merge_task_id=None):
        with self._lock:
            iu = self._open_iu(seat, utterance, now, task_id=merge_task_id)
            self.counters["decide_calls"] += 1
        try:
            outcome = self.decide_fn(utterance, selected, schemas, tool_set, context, **kwargs)
        except Exception:
            with self._lock:
                self._close_iu(iu, now, "decide_error")
                task = self.tasks.get(iu.task_id)
                # 只有「刚开、还没挂任何 AU」的新任务才当场结掉（别留空跑任务占候选位）；
                # 并进的在跑任务还有自己的 AU，决策失败不该把它一起放弃。
                if task is not None and task.running and not task.au_ids:
                    self.tasks.close_task(iu.task_id, now, "abandoned", "decide_error")
            raise
        with self._lock:
            outcome = dict(outcome or {})
            decision = dict(outcome.get("decision") or {})
            au = self._new_au(iu, seat, decision, now,
                              tool=decision.get("tool"), arguments=decision.get("arguments") or {})
            decision.update({"iu_id": iu.iu_id, "au_id": au.au_id, "seat": seat,
                             "participants": [seat], "scope": [seat]})
            outcome["decision"] = decision
            outcome.setdefault("iu_id", iu.iu_id)
            outcome.setdefault("au_id", au.au_id)
            return outcome

    # ---------- IU / AU ----------

    def _next_id(self, prefix):
        self._seq += 1
        return "%s-%03d" % (prefix, self._seq)

    def _open_iu(self, seat, text, now, goal=None, task_id=None):
        iu = IntentUnit(iu_id=self._next_id("iu"), seat=seat, text=text or "", t_open=now,
                        goal=goal, participants=[seat], segments=[text] if text else [])
        self.ius.append(iu)
        # 命中相关 → 并进那条还在跑的任务（不开新任务）；否则＝一条新请求＝一条新任务（FIFO）。
        if task_id is not None:
            task = self.tasks.merge_into(task_id, iu.iu_id, now)
            if task is not None:
                iu.task_id = task_id
                self._emit("iu_open", now, iu_id=iu.iu_id, seat=seat, goal=goal, text=text,
                           task_id=iu.task_id, queue_pos=self.tasks.queue_position(iu.task_id),
                           merged=True)
                return iu
            # 目标已不在跑（竞态兜底）：退回新开一条任务。
        task = self.tasks.open_task(seat, text, now)
        iu.task_id = task.task_id
        self.tasks.attach_iu(task.task_id, iu.iu_id)
        self._emit("iu_open", now, iu_id=iu.iu_id, seat=seat, goal=goal, text=text,
                   task_id=iu.task_id, queue_pos=self.tasks.queue_position(iu.task_id))
        return iu

    def _close_iu(self, iu, now, why):
        if iu is None or iu.status != "open":
            return
        iu.status = "committed"
        iu.t_close = now
        self._emit("iu_committed", now, iu_id=iu.iu_id, au_id=iu.au_id, seat=iu.seat,
                   participants=list(iu.participants), why=why)

    def _iu(self, iu_id):
        for iu in self.ius:
            if iu.iu_id == iu_id:
                return iu
        return None

    def _au(self, au_id):
        for au in self.aus:
            if au.au_id == au_id:
                return au
        return None

    def _new_au(self, iu, seat, decision, now, *, tool=None, arguments=None, inherited_from=None):
        au = ActionUnit(au_id=self._next_id("au"), iu_id=iu.iu_id, initiator=seat,
                        participants=[seat], scope=[seat], kind=decision.get("kind") or "tool_call",
                        tool=tool, arguments=dict(arguments or {}), decision=dict(decision),
                        t_decided=now, inherited_from=inherited_from)
        self.aus.append(au)
        iu.au_id = au.au_id
        self.tasks.attach_au(iu.task_id, au.au_id, now)
        self._unapplied[seat].append(au)
        self._emit("au_decided", now, au_id=au.au_id, iu_id=iu.iu_id, seat=seat,
                   tool=au.tool, kind=au.kind, inherited_from=inherited_from)
        return au

    def recent_action(self):
        """最近一个可被指代的动作单元（已落地优先，其次在途）。"""
        for au in reversed(self.aus):
            if au.state in ("executed", "decided", "held"):
                return au
        return None

    # ---------- 应用 / 执行 / 作废 ----------

    def _clip_applied(self, au):
        for au_seat in self._unapplied.get(au.initiator, []):
            if au_seat is au:
                self._unapplied[au.initiator].remove(au)
                break

    def _on_applied(self, seat, decision, now):
        au = self._au(decision.get("au_id"))
        if au is None:
            return
        self._clip_applied(au)
        if au.state in ("executed", "abandoned", "held"):
            return
        au.applied_ms = now
        if self._is_pool_tool(au.tool):
            # 8 池高危：执行前要确认；跨席确认路径本期下架（C3），C2 只记账不执行。
            au.state = "awaiting_confirm"
            self.counters["awaiting_confirm"] += 1
            self._emit("au_awaiting_confirm", now, au_id=au.au_id, iu_id=au.iu_id,
                       seat=seat, tool=au.tool, why="confirm_pool_c3")
            return
        if au.kind in ("ask_user", "done"):
            # 不是动作：不执行、不落地，IU 直接结掉（台账不记问句）。
            self._close_iu(self._iu(au.iu_id), now, au.kind)
            au.state = "executed"
            au.receipt = "已回问：" + str((au.arguments or {}).get("question", ""))
            self.counters["executed"] += 1
            self.tasks.settle_au(au.au_id, now, "executed")     # 任务层同样结算（不留空跑任务）
            self._emit("au_executed", now, au_id=au.au_id, iu_id=au.iu_id, seat=seat,
                       kind=au.kind, tool=au.tool, arguments=dict(au.arguments or {}),
                       participants=list(au.participants),
                       scope=list(au.scope), receipt=au.receipt, merged=False,
                       note="非动作（回问/结束），不产生执行")
            return
        if self._should_hold(au, now):
            au.state = "held"
            au.merge_deadline_ms = now + self.merge_window_ms
            self.counters["held"] += 1
            self._emit("au_held", now, au_id=au.au_id, iu_id=au.iu_id,
                       deadline_ms=au.merge_deadline_ms, reason="merge_window")
            return
        self._execute(au, now)

    def _should_hold(self, au, now):
        """已应用但还可能有第二席附和：卡进归并窗，等一次执行到两座。

        判据（A2 的三档时序就靠这一条区分）：另一席**在本 AU 应用之前**已经开口，而且开口字
        还像附和（`is_anaphora_prefix`）→ 卡窗；另一席是在本 AU 落地之后才开口 → 不卡，
        那一次「我也要」按「已落地」处理成新 AU（只作用他自己的座位）。
        """
        for seat, pending in list(self._pending_ref.items()):
            if seat == au.initiator:
                continue
            if pending["t_ms"] <= now and now - pending["t_ms"] <= self.merge_window_ms:
                return True
        return False

    def _action_key(self, au, applied):
        return (au.tool or au.kind, json.dumps(au.arguments or {}, sort_keys=True, ensure_ascii=False),
                tuple(sorted(applied)))

    def _receipt(self, au, applied):
        label = _TOOL_LABELS.get(au.tool or "", au.tool or au.kind)
        who = "+".join(applied)
        bits = ["已执行：" + label, "发起 " + au.initiator, "作用 " + who]
        bits.append("合并：是" if au.merged else "合并：否")
        if au.inherited_from:
            bits.append("指代 " + str(au.inherited_from))
        return "（" + "，".join(bits) + "）"

    def _spoken(self, au, applied):
        """办完之后**对人说的那一句**（上屏的就是它；办理记录不上屏）。

        只用「这句原话的动词 + 工具说明里的对象 + 执行参数的位置/值」拼，不编造没给的数据；
        查询类工具在 PoC 里没有真实数据源，用 `agent/spoken.py` 里的假数据（DETAIL 标注）。
        """
        iu = self._iu(au.iu_id)
        return spoken_line(getattr(iu, "text", "") or "", au.tool, au.arguments)

    def _execute(self, au, now):
        if au.state == "executed":
            return
        applied = list(au.scope)
        au.state = "executed"
        au.t_executed = now
        au.receipt = self._receipt(au, applied)
        au.spoken = self._spoken(au, applied)
        key = self._action_key(au, applied)
        if key in self._dispatched:
            self.counters["repeat_action"] += 1
        else:
            self._dispatched[key] = au.au_id
        if set(applied) - set(au.participants):
            # 作用到了没人声明的座位 = 错座
            self.counters["wrong_seat"] += 1
        if (set(au.participants) == set(SEAT_ORDER) and "D" in au.receipt and "P" in au.receipt):
            self._receipt_both = True
        self.counters["executed"] += 1
        self._close_iu(self._iu(au.iu_id), now, "au_executed")
        self.recent.append(au)
        if len(self.recent) > self.recent_limit:
            self.recent = self.recent[-self.recent_limit:]
        self._emit("au_executed", now, au_id=au.au_id, iu_id=au.iu_id, seat=au.initiator,
                   kind=au.kind, tool=au.tool, arguments=dict(au.arguments or {}),
                   participants=list(au.participants), scope=applied, receipt=au.receipt,
                   spoken=au.spoken,
                   merged=au.merged, inherited_from=au.inherited_from, action_key=list(key))
        self._emit("receipt", now, au_id=au.au_id, seat=au.initiator, text=au.receipt,
                   spoken=au.spoken,
                   participants=list(au.participants), scope=applied)
        self.tasks.settle_au(au.au_id, now, "executed")

    def _abandon(self, au, now, why):
        if au.state in ("executed", "abandoned"):
            return
        self._clip_applied(au)
        au.state = "abandoned"
        au.abandon_why = why
        self.counters["abandoned"] += 1
        self._close_iu(self._iu(au.iu_id), now, "au_abandoned")
        self._emit("au_abandoned", now, au_id=au.au_id, iu_id=au.iu_id, seat=au.initiator,
                   why=why)
        self.tasks.settle_au(au.au_id, now, "abandoned")

    @staticmethod
    def _is_pool_tool(tool):
        if not tool:
            return False
        try:
            from confirm_gate import CONFIRM_POOL     # 惰性：避免 import 期耦合
        except Exception:  # noqa: BLE001 - 拿不到池就当池外，不阻塞决策
            return False
        return tool in CONFIRM_POOL

    # ---------- 事件回流（席的事件 → 账） ----------

    def observe(self, event):
        with self._lock:
            name = event.get("event")
            seat = event.get("seat_id")
            t_ms = event.get("t_ms")
            if t_ms is not None:
                self.mark_now(t_ms)
            now = self._now()
            if name == "asr_update":
                if not event.get("changed"):
                    return
                text = event.get("segment") or event.get("text") or ""
                if is_anaphora_prefix(text):
                    # 保留第一次开口的时刻（判定「同时开口」要靠它），只刷新看到的最新文本
                    entry = self._pending_ref.setdefault(seat, {"text": "", "t_ms": now})
                    entry["text"] = normalize(text)
                else:
                    self._pending_ref.pop(seat, None)
            elif name == "llm_decision":
                decision = event.get("decision") or {}
                self._pending_ref.pop(seat, None)
                if decision.get("absorbed"):
                    return                      # 附和已并入别人的 AU，不再算一次应用
                self._on_applied(seat, decision, now)
            elif name == "llm_cancel":
                for au in list(self._unapplied.get(seat) or []):
                    self._abandon(au, now, "cancel:" + str(event.get("reason")))
            elif name == "error" and event.get("stage") == "llm":
                for au in list(self._unapplied.get(seat) or []):
                    self._abandon(au, now, "llm_error")

    # ---------- 时间推进 / 收口 / 对账 ----------

    def tick(self, now_ms: int) -> None:
        with self._lock:
            self.mark_now(now_ms)
            now = self._now()
            for au in list(self.aus):
                if au.state != "held":
                    continue
                if au.merge_deadline_ms is not None and now >= au.merge_deadline_ms:
                    # 归并窗走完没人来：按现有参与者落地，不再等（漏执行是硬门槛）。
                    self._emit("au_merge_timeout", now, au_id=au.au_id, iu_id=au.iu_id)
                    self._execute(au, now)
                    continue
                if not self._hold_has_signal(au, now):
                    # 另一席开口后说成了别的指令（不像附和了）：不再占着窗口，立刻落地。
                    self._emit("au_merge_released", now, au_id=au.au_id, iu_id=au.iu_id,
                               reason="other_seat_not_anaphora")
                    self._execute(au, now)

    def _hold_has_signal(self, au, now):
        for seat, pending in list(self._pending_ref.items()):
            if seat == au.initiator:
                continue
            if now - pending["t_ms"] <= self.merge_window_ms:
                return True
        return False

    def settle(self, now_ms=None) -> None:
        """会话收口结算：在途/未应用/待确认一律结掉，保证账能对上（iu_open == 0）。"""
        with self._lock:
            if now_ms is not None:
                self.mark_now(now_ms)
            now = self._now()
            for au in list(self.aus):
                if au.state == "held":
                    self._execute(au, now)        # 卡在归并窗的决策照常落地，不漏执行
                elif au.state == "decided":
                    self._abandon(au, now, "session_end")
                elif au.state == "awaiting_confirm":
                    self._abandon(au, now, "confirm_path_c3_not_landed")
            for iu in list(self.ius):
                if iu.status == "open":
                    self._close_iu(iu, now, "session_end_no_au")
            # 任务层兜底：会话结束还在跑的（含没有 AU 的空任务）一律结掉，`task_open` 才收得拢。
            self.tasks.close_running(now, "session_end")

    def audit(self, case=None) -> dict:
        """A6 对账 / wave-2 orchestra audit：先按会话收口结算，再出编排层真值（不编）。"""
        self.settle()
        return self.snapshot(case)

    def snapshot(self, case=None) -> dict:
        """只看当前账，不结算（回放中途观察用）。"""
        with self._lock:
            executed = [au for au in self.aus if au.state == "executed"
                        and au.kind not in ("ask_user", "done")]
            abandoned = [au for au in self.aus if au.state == "abandoned"]
            open_ius = [iu for iu in self.ius if iu.status == "open"]
            committed = [iu for iu in self.ius if iu.status != "open"]
            out = {
                "actions_effective": len(self._dispatched),
                "repeat_action": int(self.counters["repeat_action"]),
                "receipt_has_both": bool(self._receipt_both),
                "au_executed": len(executed),
                "au_abandoned": len(abandoned),
                "wrong_seat": int(self.counters["wrong_seat"]),
                "iu_open": len(open_ius),
                "iu_committed": len(committed),
                "iu_eq": (not open_ius) and len(committed) == len(executed) + len(abandoned),
                "recent_actions": [au.as_dict() for au in self.recent],
                "ius": [iu.as_dict() for iu in self.ius],
                "aus": [au.as_dict() for au in self.aus],
                **{key: value for key, value in self.counters.items()},
            }
            # 任务层真值（P2-1）：`task_*` 前缀，与 iu_/au_ 的账并列，不覆盖 A6 判据。
            out.update(self.tasks.snapshot())
            if self.counters["references"]:
                # 编排层真值覆盖席级观测：指代/合并之后「谁真的落了决策」按 AU 的发起席算，
                # 被并进别人 AU 的附和席不再单独计一次（TC-16 期望 P:0 就是这条）。
                counts = {seat: 0 for seat in SEAT_ORDER}
                for au in executed:
                    counts[au.initiator] = counts.get(au.initiator, 0) + 1
                out["decisions"] = counts
            return out

    # ---------- 事件 ----------

    def _on_task_event(self, event: dict) -> None:
        """任务台账事件与 IU/AU 事件同流（layer 一致，回放 / 诊断面都读得到）。"""
        event.setdefault("layer", "orchestra")
        self.events.append(event)
        if self.on_event:
            self.on_event(event)

    def _emit(self, name, t_ms, **payload):
        event = {"event": name, "t_ms": t_ms, "layer": "orchestra"}
        event.update(payload)
        self.events.append(event)
        if self.on_event:
            self.on_event(event)
