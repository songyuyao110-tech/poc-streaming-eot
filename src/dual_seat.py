"""双席编排：一路干净 ASR 进一席，一席一份判停实例，Agent 侧只保证「不串席」（C1）。

前提（用户 2026-11-03 拍板，范围收口）：**两路干净 ASR 由前端给出**（两路 mic 的分离/隔离与
同句去重都不在本卡、不在本模块）。本模块只解决 Agent 侧的隔离问题：

    ASR-D ──队列──▶ 席 D 的 TurnSession（状态机 / 锚点 / 版本 / 消费游标，本席私有）
    ASR-P ──队列──▶ 席 P 的 TurnSession（同上，与 D 并行、互不重置/取消）

要点：
- **席不是人，也不是通道**。`mic-D → 席 D`（主驾）、`mic-P → 席 P`（副驾优先）是通道先验
  （R12a 的默认归属）；上层编排脑之后可以按上下文改归属（R12c，C3 的活），本模块只提供
  `seat_id`，不猜、不合并。
- **单写者**：所有 ASR/VAD 先入 `_queue`，只由调用 `tick()` 的那个线程按 FIFO 派给各席；
  判停/选工具/决策的 worker 线程只回填结果，不改状态（TurnSession 原有约束不变）。
- **跨席隔离**：路由只把某一路 ASR 派给该通道先验席，因此一席的 `speech_start` 不可能重置
  另一席的锚点、也不可能取消它在途的决策。`cross_cancel` / `cross_reset` 按「事件发生前
  该席最后一次输入的来源」归因，回放里必须为 0；`own_cancel` / `own_reset` 是同一套计数器
  的对照（本席自己的续句会正常触发），用来证明计数链路是活的而不是恒 0 的摆设。

不做：同句去重（用户已拍「不做」，前端保证两路干净）、两套决策、第 3 席、判停阈值改动。

C2 起多挂一个可选的**会话脑**（`brain=SessionBrain(...)`，见 `orchestra.py`）：两席的
`decide_fn` 槽改成「先经编排层一个入口」，席的事件回填脑的账（IU/AU），`tick()` 顺带推进
脑的归并窗。不传 `brain` 时行为与 C1 完全一致（默认关，单席/旧双席回放不受影响）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True

from turn_session import TurnConfig, TurnSession, normalize

SEAT_ORDER = ("D", "P")
MIC_OF_SEAT = {"D": "mic-D", "P": "mic-P"}
SEAT_OF_MIC = {mic: seat for seat, mic in MIC_OF_SEAT.items()}


class DualSeatSession:
    """两路干净 ASR、两席并行判停、单写者派发。"""

    def __init__(self, cfg: "TurnConfig | None" = None, on_event=None, *, seat_cfg: "TurnConfig | None" = None,
                 eot_fn=None, select_tools_fn=None, decide_fn=None, session_id=None,
                 evidence_root=None, clock=None, confirm_gate=None, warm_fn=None,
                 brain=None):
        self.cfg = cfg or seat_cfg or TurnConfig()
        self.on_event = on_event
        self.session_id = session_id or ("duo-" + str(int(time.time() * 1000)))
        self.evidence_root = evidence_root
        self.events = []
        self._queue = []
        self._last_input_mic = {seat: None for seat in SEAT_ORDER}
        self._last_change_ms = {seat: None for seat in SEAT_ORDER}
        self.confirm_latencies = {seat: [] for seat in SEAT_ORDER}
        # 逐次确认台账：{t_ms, latency_ms, why}。时延判据只取 `confirm_silence` 这一条路径
        # （与 A0 基线同定义：A0 的 20/20 都由静默确认收口）；`cut_wait` 等另记，不混比。
        self.confirms = {seat: [] for seat in SEAT_ORDER}
        self.counters = {"inputs": 0, "cross_cancel": 0, "cross_reset": 0, "own_cancel": 0,
                         "own_reset": 0, "unattributed_events": 0, "actions": 0, "deposits": 0,
                         "errors": 0, "own_void": 0, "cross_void": 0}
        overrides = {}
        if eot_fn is not None:
            overrides["eot_fn"] = eot_fn
        if select_tools_fn is not None:
            overrides["select_tools_fn"] = select_tools_fn
        if decide_fn is not None:
            overrides["decide_fn"] = decide_fn
        # UI 双席演示需要与单席一致的注入面：离线 mock 时给每席一份 mock 确认闸门、
        # 不预热（不连真实 KEV/LLM）；真实模式每席各自预热各自的模型连接。
        if confirm_gate is not None:
            overrides["confirm_gate"] = confirm_gate
        if warm_fn is not None:
            overrides["warm_fn"] = warm_fn
        # C2 会话脑（可选）：决策单一入口 + 会话级台账/IU。不传就还是 C1 的两席各自决策。
        self.brain = brain
        if brain is not None:
            brain.on_event = self._on_brain_event

        def seat_overrides(seat):
            slots = dict(overrides)
            if brain is not None:
                slots["decide_fn"] = brain.decide_fn_for(seat)
            return slots

        self.seats = {}
        for seat in SEAT_ORDER:
            self.seats[seat] = TurnSession(
                self.cfg, lambda event, s=seat: self._on_seat_event(s, event), clock=clock,
                session_id=self.session_id + "-" + seat, evidence_root=evidence_root,
                seat_id=seat, mic_id=MIC_OF_SEAT[seat], **seat_overrides(seat))

    # ---------- 入口（单写者队列） ----------

    def on_asr(self, t_ms: int, mic_id: str, text: str, final: bool) -> None:
        self._queue.append(("asr", int(t_ms), self._seat_for(mic_id), mic_id, text, bool(final)))

    def on_vad(self, t_ms: int, mic_id: str, kind: str) -> None:
        self._queue.append(("vad", int(t_ms), self._seat_for(mic_id), mic_id, kind))

    def tick(self, now_ms: int) -> None:
        self._drain()
        if self.brain is not None:
            self.brain.mark_now(now_ms)
        for seat in SEAT_ORDER:
            self.seats[seat].tick(now_ms)
        if self.brain is not None:
            self.brain.tick(now_ms)      # 归并窗到期 → 该落地的落地，不漏执行

    def stop(self, now_ms: int) -> None:
        self._drain()
        for seat in SEAT_ORDER:
            self.seats[seat].stop(now_ms)
        if self.brain is not None:
            self.brain.settle(now_ms)

    def close(self) -> None:
        if self.brain is not None:
            self.brain.settle()
        for session in self.seats.values():
            session.close()

    @staticmethod
    def _seat_for(mic_id: str) -> str:
        try:
            return SEAT_OF_MIC[mic_id]
        except KeyError:
            raise ValueError("unknown mic_id (two seats only): " + str(mic_id)) from None

    def _drain(self) -> None:
        while self._queue:
            kind, t_ms, seat, mic_id, *rest = self._queue.pop(0)
            self.counters["inputs"] += 1
            self._last_input_mic[seat] = mic_id
            session = self.seats[seat]
            if kind == "asr":
                session.on_asr(t_ms, rest[0], rest[1])
            else:
                session.on_vad(t_ms, rest[0])

    # ---------- 事件 / 计数 ----------

    def _on_seat_event(self, seat: str, event: dict) -> None:
        name = event.get("event")
        owner_mic = MIC_OF_SEAT[seat]
        if name == "asr_update" and event.get("changed"):
            self._last_change_ms[seat] = event["t_ms"]
        elif name == "state_change" and event.get("to") == "CONFIRMED":
            last_change = self._last_change_ms[seat]
            if last_change is not None:
                event["confirm_latency_ms"] = round(event["t_ms"] - last_change, 1)
                self.confirm_latencies[seat].append(event["confirm_latency_ms"])
                self.confirms[seat].append({"t_ms": event["t_ms"],
                                            "latency_ms": event["confirm_latency_ms"],
                                            "why": event.get("why")})
        elif name == "state_change" and event.get("to") == "LISTENING" \
                and event.get("why") in ("speech_start", "new_text"):
            self._attribute(seat, owner_mic, "own_reset", "cross_reset")
        elif name == "llm_cancel":
            self._attribute(seat, owner_mic, "own_cancel", "cross_cancel")
        elif name == "turn_void":
            # 生成中打断=作废本轮（P0-2）：按最后一输入 mic 归因，own/cross 与 llm_cancel 同口径。
            self._attribute(seat, owner_mic, "own_void", "cross_void")
        elif name == "llm_decision":
            self.counters["actions"] += 1
        elif name == "segment_consumed":
            self.counters["deposits"] += 1
        elif name == "error":
            self.counters["errors"] += 1
        self.events.append(event)
        if self.on_event:
            self.on_event(event)
        if self.brain is not None:
            # 会话脑按席事件回填账（asr → 指代候选；llm_decision → AU 应用；llm_cancel → 作废）
            self.brain.observe(event)

    def _on_brain_event(self, event: dict) -> None:
        """编排层事件（iu_* / au_* / receipt）与席事件同流，回放和断言面都读得到。"""
        self.events.append(event)
        if self.on_event:
            self.on_event(event)

    def _attribute(self, seat: str, owner_mic: str, same_key: str, cross_key: str) -> None:
        """按「该席最后一次输入的 mic」归因：本席的算 own，另一路的算 cross，无输入算 unattributed。"""
        mic = self._last_input_mic[seat]
        if mic == owner_mic:
            self.counters[same_key] += 1
        elif mic is None:
            self.counters["unattributed_events"] += 1
        else:
            self.counters[cross_key] += 1

    # ---------- 汇总 ----------

    @property
    def turn_done(self) -> bool:
        return all(session.turn_done for session in self.seats.values())

    def seat_texts(self):
        """每席已落地的决策文本（`segment_consumed`），按席分开 —— 串席检查的原料。"""
        return {seat: [record["text"] for record in self.seats[seat].consumptions]
                for seat in SEAT_ORDER}

    def summary(self) -> dict:
        return {
            "session_id": self.session_id,
            "seats": {seat: self.seats[seat].summary() for seat in SEAT_ORDER},
            "confirm_latency_ms": {seat: list(values) for seat, values in self.confirm_latencies.items()},
            "confirms": {seat: list(values) for seat, values in self.confirms.items()},
            "seat_texts": self.seat_texts(),
            **self.counters,
        }
