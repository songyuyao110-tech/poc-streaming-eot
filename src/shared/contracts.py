"""模块间契约：只声明「谁给谁什么形状的东西」，不实现任何判断规则。

四个边界的交接（与 wiki `四模块职责与接口契约` §1–§6 一致，改契约先改 wiki）：

1. 出字同时给读心和中枢（`ASRText`）。
2. 读心按时间往场景流追加场景（`SceneSnapshot`），**不判停、不判要不要回答**。
3. 中枢每一版出字并行做「说完没说完」（`EotVerdict`）与「要不要回答」（`AcceptVerdict`）；
   取景用此刻已有最新一份、不等待本轮读心，用上一拍允许。
4. 开闸只认收住那一刻同一版出字上的要不要回答（`GateDecision`）：要回答进 Agent，
   不回答只进上下文（仍进对话流和记忆）。
5. 记忆以收住时刻对齐场景；无新收住第一刀不写人名类记忆（`MemoryStore`，接口位，未落地）。
6. Agent 只在闸开才接手，发起请求那一刻取当时最新场景并带当时记忆（`AgentRequest`）。

本文件只有类型与协议（TypedDict / Protocol），没有任何运行时逻辑；落地实现仍在各模块。
"""
from __future__ import annotations

from typing import Any, Literal, Protocol, TypedDict


SeatId = Literal["D", "P"]


class ASRText(TypedDict, total=False):
    """一路出字：同一份字同时给读心与中枢（谁都不等谁）。"""

    seat: SeatId
    text: str             # 这一版已经出到的字（partial 或刚落定的整句）
    final: bool           # ASR 自己报的段落结束；**不是**判停结论，没人拿它当「说完」
    t_ms: int


class SceneSnapshot(TypedDict, total=False):
    """读心的产物（一句车舱旁白，v04 轻松口吻、不第一人称），按时间往场景流追加。

    读心只描述「谁在说什么、更像朝谁说、画面里在做什么」；
    没有「接不接 / 进不进 Agent / 已消费」字段——那些是中枢的结论。
    """

    summary: str
    intent: str
    target: str
    topic: str
    cues: list[str]
    confidence: float
    basis: str
    visual: dict[str, Any]
    seat: str
    seat_label: str
    speakers: list[str]
    seats: list[dict[str, Any]]
    reading: str
    revision: int
    updated_at_ms: int
    finalized: bool
    pending: bool


class EotVerdict(TypedDict, total=False):
    """判停这一路：三态 + 双阈值（complete / cut / wait）。"""

    label: str
    may_confirm: bool
    confidence: float
    complete: float
    cut: float
    wait: float
    latency_ms: float
    error: str


class AcceptVerdict(TypedDict, total=False):
    """开闸判断这一路：这句话要不要回答（不是三选一，是 0..1 的是非 + 临时线）。"""

    state: str
    accept: bool
    confidence: float
    score: float
    threshold: float
    threshold_note: str
    reason: str
    source: str
    model: str
    latency_ms: float
    scene_used: dict[str, Any]
    error: str


class GateDecision(TypedDict, total=False):
    """开闸结论：只认收住那一刻同一版出字上的「要不要回答」。

    route = agent / context_only / wait（当前实现的三档；wiki 有对应口径）。
    """

    route: str
    state: str
    reason: str
    version: int
    text: str
    scene: dict[str, Any]
    eot: EotVerdict
    accept: AcceptVerdict
    decided_at_ms: int
    gate_ms: float | None
    accept_source: str
    scene_age_ms: int | None
    scene_missing: bool | None
    two_route_ms: float


class DialogueTurn(TypedDict, total=False):
    """对话流的一条（收口那一刻对齐场景；记忆按同一时刻切）。"""

    t_ms: int
    seat: SeatId
    text: str
    version: int
    route: str
    scene: SceneSnapshot | None
    gate: GateDecision | None
    memory_ids: list[str]


class AgentRequest(TypedDict, total=False):
    """Agent 只在闸开才接手：发起请求那一刻取当时最新场景并带当时记忆。"""

    t_ms: int
    seat: SeatId
    text: str
    scene: SceneSnapshot | None
    memory: list[str]


class MindNotifier(Protocol):
    """读心对外的唯一入口：中枢收住那一刻通知它读一次（读心不判停）。"""

    def on_utterance_close(self, seat: str, why: str = "") -> None: ...
