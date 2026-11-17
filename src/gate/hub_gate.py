"""对话中枢 · 并发闸门：同一版出字上并发「说没说完」与「接不接」，只在「说完」那一刻开闸。

口径来源：wiki `[[中枢并发闸门]]`（2026-11-10 15:12 拍板，现行口径页）。一句话：

    一次 ASR 出字 → 中枢取读心**此刻已有的**场景（一次，不等待）
                 ├─ 判停一路（说没说完）    ┐ 同一版字 + 同一份场景，并发、不串行
                 └─ 接不接一路（hub_accept）┘
    开闸只认「判定说完」那一刻、同一版出字上的「接不接」：
      说完 且 要接   → route=agent        （闸门开了，才进工具披露 / 决策 / Agent）
      说完 但 不接   → route=context_only （不开闸；文本只进上下文，不进 Agent）
      没说完         → route=wait         （先不开闸；中间那次「接不接」不算数）

本模块只做「并发 + 开闸」这一件事，不产场景（读心给）、不回答不执行（Realtime / Background 做）。
两条硬约束（wiki §4）：

- **中枢 → 读心**：`scene_fn()` 只取一次读心**此刻已有的**快照；拿不到就按「无场景」照常判，
  绝不为了等场景把闸门挂起（`scene_missing` 会写在结果里，界面可见）。
- **读心 → 中枢**：读心自己按「出字或画面变化」更新场景；本模块不锁字、不驱动读心的节奏。

两种用法（同一套并发/开闸规则，不重复实现）：

1. **主链**（`TurnSession(gate=...)`）：判停仍由 TurnSession 自己的 worker 跑（它管 debounce / 在途数 /
   状态机），本类只并发发起「接不接」，判停结果由 `on_eot(...)` 回填。
2. **调试页**（`GateKeeper(eot_fn=score_eot, ...)`）：两路都由本类并发发起，供 `/api/hub/tick`
   在同一拍里展示「判停 / 接不接 / 闸门」三样。真实链路里两路同样是**同时在飞**，不是先判完再判另一个。

未拍板（wiki §8，本卡不代拍）：接不接用哪个模型/服务 → 走 `hub_accept.build_accept_fn()` 的可替换接口，
界面显示实际用的「临时规则」；「一次快照能多旧」不设硬阈值，只把 `scene_age_ms` 记出来给调试页看。
"""
from __future__ import annotations

import copy
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.dont_write_bytecode = True

# 期二（2026-11-16）节奏 + 垫句：节奏每版都判，垫句只在「积极 + 已连续说满 1.5 秒」才问。
from gate.hub_pace import CUSHION_MIN_SPEECH_MS, PACE_EAGER, PACE_LABELS, hold_pace  # noqa: E402

ROUTE_AGENT = "agent"
ROUTE_CONTEXT_ONLY = "context_only"
ROUTE_WAIT = "wait"
# 判断超时 / 失败：**先不开闸，但不当成「不接」**（2026-11-10 16:54 拍板）。第四条去处只在
# 「接不接没判成」时出现，页面必须写清是超时/失败，不能显示成「不接」。
ROUTE_ACCEPT_UNAVAILABLE = "accept_unavailable"

GATE_OPEN = "开闸"
GATE_CONTEXT = "只进上下文"
GATE_WAIT = "还没说完"
GATE_PENDING = "等接不接"
GATE_ACCEPT_TIMEOUT = "接不接超时：先不开闸"
GATE_ACCEPT_FAILED = "接不接失败：先不开闸"
GATE_CLOSED = "不进 Agent"

# 每来一版出字都要判接不接（16:37 拍板）：**不丢、不合并排队中的旧一版**。
# 真模型一次约 0.5s，出字最快约每 200ms 一版 → 6 个 worker 兜得住短时积压；
# 排队期间版本不丢，各版本各自有一份结论（记录保留最近 _KEEP_VERSIONS 版）。
_POOL = ThreadPoolExecutor(max_workers=6, thread_name_prefix="hub-gate")

# 接不接判断的时间预算随真模型定（HTTP 读超时见 hub_accept.ACCEPT_TIMEOUT_S，默认 2.8s；
# 闸门侧 3000ms 略宽，保证「读超时」先以 failed 落地，而不是被闸门提前判成 timeout）。
ACCEPT_TIMEOUT_MS = int(os.environ.get("HUB_ACCEPT_TIMEOUT_MS", "3000"))

# 每版记录保留数（界面只读最近几版；不留无限长账）
_KEEP_VERSIONS = 12

# 应一声（期一）：它是第三条判断，但**不在关键路径上**——用自己的一小撮 worker，绝不跟判停 /
# 要不要回答抢线程（那两路才是收住与开闸的关键路径）。回来晚了就只是这一版不出声。
_ACK_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hub-ack")
ACK_TIMEOUT_MS = int(os.environ.get("HUB_ACK_TIMEOUT_MS", "3000"))

# 期二（2026-11-16）节奏 + 垫句：同样**不在关键路径上**——各走自己的小线程池，绝不跟判停 /
# 要不要回答抢线程。节奏每版都问；垫句只在「节奏＝积极 + 已说满 1.5 秒」这一版再问一次，
# 所以其它三档**根本不问垫句**（不是问了不用）。
_PACE_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hub-pace")
_CUSHION_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hub-cushion")
PACE_TIMEOUT_MS = int(os.environ.get("HUB_PACE_TIMEOUT_MS", "3000"))
CUSHION_TIMEOUT_MS = int(os.environ.get("HUB_CUSHION_TIMEOUT_MS", "3000"))

# 期三（2026-11-16）两人同时说四选：**不是每版都问**——只挂在「这边正在说 / 正在准备说，人又开口」
# 的那一版上问一次（同一段插话的后续 partial 不再重复问），所以它不是「每版固定多一路」，
# 而是「每次插话多一次」。同样走自己的小线程池，绝不跟判停 / 要不要回答抢线程。
# 这一问没回来 / 失败 / 超时 → 中枢按「继续说」兜底（拍板 5），**不先作废、不先开闸**。
_FLOOR_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hub-floor")
FLOOR_TIMEOUT_MS = int(os.environ.get("HUB_FLOOR_TIMEOUT_MS", "3000"))

# 期四（2026-11-16）冷场三选：**不是每版都问**——只在「安静到刻度、且真的没人说话」这个时刻
# 按刻度问一次（首问约 4 秒；约 10 秒之后才允许「问一句」）。同样走自己的小线程池，
# 绝不跟判停 / 要不要回答抢线程。这一问没回来 / 失败 / 超时 → 不出声（按「再等」走）。
_QUIET_POOL = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hub-quiet")
QUIET_TIMEOUT_MS = int(os.environ.get("HUB_QUIET_TIMEOUT_MS", "3000"))


def _dumps(value):
    """事件里只放可 JSON 化的浅结构（快照 / 计时），避免把对象引用带到 SSE。"""
    if isinstance(value, dict):
        return {key: _dumps(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_dumps(item) for item in value]
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)


class GateKeeper:
    """一个席（一个回合流）一份：并发两路 + 开闸结论 + 调试快照。"""

    def __init__(self, *, accept_fn, eot_fn=None, scene_fn=None, seat="D", on_event=None,
                 accept_timeout_ms=None, clock=None, pool=None, ack_fn=None,
                 ack_timeout_ms=None, ack_pool=None, pace_fn=None, cushion_fn=None,
                 pace_timeout_ms=None, cushion_timeout_ms=None, pace_pool=None,
                 cushion_pool=None, floor_fn=None, floor_timeout_ms=None, floor_pool=None,
                 quiet_fn=None, quiet_timeout_ms=None, quiet_pool=None):
        self.accept_fn = accept_fn
        self.eot_fn = eot_fn                  # None = 判停由调用方（TurnSession）跑并回填
        self.ack_fn = ack_fn                  # None = 本期不出声（期一以外 / 离线不需要时可不挂）
        self.pace_fn = pace_fn                # None = 不判节奏（更早的链路 / 离线不需要时可不挂）
        self.cushion_fn = cushion_fn          # None = 不问垫句（A/B 对照 / 期二以前）
        self.floor_fn = floor_fn              # None = 不判四选（A/B 对照 / 期三以前）
        self.quiet_fn = quiet_fn              # None = 不判冷场（A/B 对照 / 期四以前）
        self.scene_fn = scene_fn              # () -> 读心此刻已有的场景快照（同步、只取一次）
        self.seat = "D" if str(seat).upper() != "P" else "P"
        self.on_event = on_event
        self.accept_timeout_ms = int(ACCEPT_TIMEOUT_MS if accept_timeout_ms is None
                                     else accept_timeout_ms)
        self.ack_timeout_ms = int(ACK_TIMEOUT_MS if ack_timeout_ms is None else ack_timeout_ms)
        self.pace_timeout_ms = int(PACE_TIMEOUT_MS if pace_timeout_ms is None else pace_timeout_ms)
        self.cushion_timeout_ms = int(CUSHION_TIMEOUT_MS if cushion_timeout_ms is None
                                      else cushion_timeout_ms)
        self.floor_timeout_ms = int(FLOOR_TIMEOUT_MS if floor_timeout_ms is None
                                    else floor_timeout_ms)
        self.quiet_timeout_ms = int(QUIET_TIMEOUT_MS if quiet_timeout_ms is None
                                    else quiet_timeout_ms)
        self.clock = clock or (lambda: int(time.monotonic() * 1000))
        self._pool = pool or _POOL
        self._ack_pool = ack_pool or _ACK_POOL
        self._pace_pool = pace_pool or _PACE_POOL
        self._cushion_pool = cushion_pool or _CUSHION_POOL
        self._floor_pool = floor_pool or _FLOOR_POOL
        self._quiet_pool = quiet_pool or _QUIET_POOL
        self._lock = threading.RLock()
        self.records = {}                     # version -> record（最近 _KEEP_VERSIONS 版）
        # 期三：四选按「插话这一回」（episode）记账，不按版：同一段插话只有一个 episode。
        self.floor_records = {}               # episode -> {"request", "outcome", "asked_ms"}
        # 期四：冷场按「刻度这一回」（episode）记账——一个刻度只问一次，不会连着问。
        self.quiet_records = {}               # episode -> {"state", "quiet_ms", "asked_ms", ...}
        self.version = 0
        self.text = ""
        self.pace = None                      # 这一拍实际的节奏（跨轮保留：换档要平滑，不能一版一翻）
        self._inflight = {}                   # version -> future（每版一份，不覆盖、不丢）
        self._last_gate = None
        self.counters = {"texts": 0, "accept_calls": 0, "accept_errors": 0, "eot_calls": 0,
                         "gates": 0, "gate_open": 0, "gate_context_only": 0, "gate_wait": 0,
                         "scene_missing": 0, "version_mismatch": 0,
                         "ack_calls": 0, "ack_errors": 0, "ack_say": 0,
                         "pace_calls": 0, "pace_errors": 0, "pace_late": 0,
                         "cushion_calls": 0, "cushion_errors": 0, "cushion_late": 0,
                         "cushion_say": 0,
                         "floor_calls": 0, "floor_errors": 0, "floor_late": 0,
                         "floor_yield": 0, "floor_finish_first": 0, "floor_continue": 0,
                         "floor_cushion_back": 0,
                         "quiet_calls": 0, "quiet_errors": 0, "quiet_late": 0,
                         "quiet_wait": 0, "quiet_follow": 0, "quiet_check": 0}

    # ---------- 时钟 ----------
    def _now(self, t_ms=None) -> int:
        if t_ms is not None:
            try:
                return int(t_ms)
            except (TypeError, ValueError):
                return self.clock()
        return self.clock()

    # ---------- 事件 ----------
    def _emit(self, name, t_ms, **payload):
        event = {"event": name, "t_ms": t_ms, "layer": "hub", "seat_id": self.seat}
        event.update(_dumps(payload))
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:  # noqa: BLE001 - 事件出口失败不该打断判断
                pass
        return event

    # ---------- 读心：只取一次「此刻已有的」场景 ----------
    def _take_scene(self, t_ms: int) -> dict | None:
        if self.scene_fn is None:
            self.counters["scene_missing"] += 1
            return None
        try:
            scene = self.scene_fn()
        except Exception:  # noqa: BLE001 - 拿不到场景就按「无场景」照常判，绝不等待
            scene = None
        if not scene:
            self.counters["scene_missing"] += 1
            return None
        scene = copy.deepcopy(scene)
        updated = scene.get("updated_at_ms")
        scene["taken_at_ms"] = t_ms
        if not isinstance(scene.get("age_ms"), (int, float)):
            # 读心自己给了 age_ms（用读心的时钟算）；只有没给时才按中枢的会话时钟算。
            scene["age_ms"] = max(0, t_ms - int(updated)) if isinstance(updated, (int, float)) else None
        return scene

    # ---------- 一次出字：并发两路 ----------
    def on_text(self, version: int, text: str, t_ms: int, final: bool = False,
                speech_ms=None, turn=None) -> dict:
        """这一版字到了：取一次场景 + 立刻发起「接不接」（判停若本类负责则同时发起）。

        `speech_ms`：这一轮对方已经**连续说满多久**（期二垫句用；`None` = 本期不问垫句 / A/B 关闭）。
        `turn`：本席第几轮（节奏「开场 1–2 轮先稳住」要它）。返回这一版记录（不含结论时 `gate` 仍为空）。
        **不阻塞**：判断跑在 worker 线程里。
        """
        t_ms = self._now(t_ms)
        with self._lock:
            self.version = int(version)
            self.text = text or ""
            scene = self._take_scene(t_ms)
            record = {
                "version": int(version),
                "text": text or "",
                "final": bool(final),
                "t_start_ms": t_ms,
                "turn": turn,
                "speech_ms": None if speech_ms is None else int(speech_ms),
                "scene": scene,
                "eot": None,
                "accept": {"state": "pending", "source": self._accept_source()},
                "ack": None,
                "pace": None,
                "cushion": None,
                "gate": None,
            }
            self.records[int(version)] = record
            while len(self.records) > _KEEP_VERSIONS:
                self.records.pop(next(iter(self.records)))
            self.counters["texts"] += 1
            self._emit("hub_text", t_ms, version=int(version), text=text or "", final=bool(final),
                       scene=self._scene_brief(scene), text_version=int(version),
                       speech_ms=record["speech_ms"], turn=turn)
            # 同一版字上四路并发：接不接、应不应一声、这一拍什么节奏在 on_text 当刻就发出去；
            # 判停一路（主链）由 TurnSession 在自己的 debounce 后发，四者同时在飞，不是串行。
            # 各走各自的 worker，互不抢线程。垫句**不在这里发**：只有节奏回来是「积极」且这一版
            # 已经连续说满 1.5 秒，才在 `_run_pace` 里再发一次（其它三档根本不问）。
            self._start_accept(int(version), text or "", scene, t_ms)
            if self.ack_fn is not None:
                self._start_ack(int(version), text or "", scene, t_ms)
            if self.pace_fn is not None:
                self._start_pace(int(version), text or "", scene, t_ms, record["speech_ms"], turn)
            if self.eot_fn is not None:
                self._start_eot(int(version), text or "", t_ms)
            return record

    def _accept_source(self) -> dict:
        meta = getattr(self.accept_fn, "meta", None)
        if isinstance(meta, dict):
            return dict(meta)
        return {"source": "可替换接口", "model": "未标注", "note": "accept_fn 未标注来源"}

    def _start_accept(self, version, text, scene, t_ms):
        """这一版字的接不接：**立刻独立发出去**，不等别的版本、不丢排队中的旧一版。

        每版一个 future（`_inflight[version]`），结果按版本回填到自己的记录；
        真模型慢 / 短时积压只影响这一版何时回来，不会让某一版悄悄没有结论。
        """
        with self._lock:
            request = {"version": version, "text": text, "scene": scene, "t_ms": t_ms}
        self._submit_accept(request)

    def _submit_accept(self, request):
        with self._lock:
            self.counters["accept_calls"] += 1
            future = self._pool.submit(self._run_accept, request)
            self._inflight[request["version"]] = future

    def _run_accept(self, request):
        began = time.monotonic()
        try:
            outcome = self.accept_fn(request["text"], request["scene"], self.seat) or {}
        except Exception as error:  # noqa: BLE001 - 判断失败必须显式记账（失败 → 先不开闸）
            outcome = {"accept": None, "state": "failed",
                       "error": {"kind": type(error).__name__, "detail": str(error)[:200]},
                       "source": self._accept_source().get("source", "可替换接口"),
                       "model": self._accept_source().get("model", "未标注"),
                       "threshold": self._accept_source().get("threshold"),
                       "threshold_note": self._accept_source().get("threshold_note"),
                       "reason": "接不接判断失败：先不开闸（不当成「不回答」；界面可见）"}
        latency = (time.monotonic() - began) * 1000
        with self._lock:
            outcome.setdefault("latency_ms", round(latency, 1))
            outcome.setdefault("state", "failed" if outcome.get("accept") is None else "ok")
            outcome["state"] = "failed" if outcome.get("accept") is None else "ok"
            record = self.records.get(request["version"])
            if record is not None:
                record["accept"] = outcome
                record["accept_t_ms"] = request["t_ms"]
                record["accept_done_ms"] = round(latency, 1)
            # 记账：失败 / 成功都进事件，界面能看到「先不开闸」与超时/失败原因
            if outcome["state"] == "failed":
                self.counters["accept_errors"] += 1
            self._emit("hub_accept", request["t_ms"],
                       accept=outcome.get("accept"), state=outcome["state"],
                       confidence=outcome.get("confidence"), score=outcome.get("score"),
                       threshold=outcome.get("threshold"),
                       reason=outcome.get("reason"),
                       source=outcome.get("source"), model=outcome.get("model"),
                       latency_ms=outcome.get("latency_ms"), error=outcome.get("error"),
                       scene_used=outcome.get("scene_used"),
                       text_version=request["version"])
            self._inflight.pop(request["version"], None)

    # ---------- 应一声（期一：中枢的「陪着说」，不在关键路径上） ----------
    def _ack_source(self) -> dict:
        meta = getattr(self.ack_fn, "meta", None)
        if isinstance(meta, dict):
            return dict(meta)
        return {"source": "可替换接口", "model": "未标注", "note": "ack_fn 未标注来源"}

    def _start_ack(self, version, text, scene, t_ms):
        """这一版字的「要不要应一声」：立刻独立发出去（同一版字上并发，不等判停 / 接不接）。"""
        with self._lock:
            self.counters["ack_calls"] += 1
            self._ack_pool.submit(self._run_ack,
                                  {"version": version, "text": text, "scene": scene, "t_ms": t_ms})

    def _run_ack(self, request):
        began = time.monotonic()
        try:
            outcome = self.ack_fn(request["text"], request["scene"], self.seat) or {}
        except Exception as error:  # noqa: BLE001 - 判断失败：这一版不出声（R3），界面可见
            outcome = {"say": False, "state": "failed",
                       "error": {"kind": type(error).__name__, "detail": str(error)[:200]},
                       "source": self._ack_source().get("source", "可替换接口"),
                       "model": self._ack_source().get("model", "未标注"),
                       "reason": "应声判断失败：这一版不出声（不当成「不出声」的结论；界面可见）"}
        latency = (time.monotonic() - began) * 1000
        with self._lock:
            outcome.setdefault("latency_ms", round(latency, 1))
            outcome["state"] = "failed" if outcome.get("say") is None or outcome.get("state") == "failed" else "ok"
            record = self.records.get(request["version"])
            if record is not None:
                record["ack"] = outcome
                record["ack_t_ms"] = request["t_ms"]
                record["ack_done_ms"] = round(latency, 1)
            if outcome["state"] != "ok":
                self.counters["ack_errors"] += 1
            elif outcome.get("say"):
                self.counters["ack_say"] += 1
            self._emit("hub_ack", request["t_ms"],
                       say=outcome.get("say"), state=outcome["state"],
                       score=outcome.get("score"), confidence=outcome.get("confidence"),
                       threshold=outcome.get("threshold"), reason=outcome.get("reason"),
                       source=outcome.get("source"), model=outcome.get("model"),
                       latency_ms=outcome.get("latency_ms"), error=outcome.get("error"),
                       text_version=request["version"])

    def ack(self, version: int) -> dict | None:
        """这一版字的应声判断（没回来时 state=pending）；调用方（TurnSession）据此决定出不出声。"""
        with self._lock:
            record = self.records.get(int(version))
            if record is None:
                return None
            outcome = record.get("ack")
            if outcome is None:
                return {"state": "pending"}
            return dict(outcome)

    def judgements(self, version: int) -> dict:
        """同一版字上的判断（判停 / 要不要回答 / 应不应一声 / 节奏 / 垫句），只读给决策用。"""
        with self._lock:
            record = self.records.get(int(version)) or {}
            return {"eot": dict(record.get("eot") or {}) or None,
                    "accept": dict(record.get("accept") or {}) or None,
                    "ack": dict(record.get("ack") or {}) or None,
                    "pace": dict(record.get("pace") or {}) or None,
                    "cushion": dict(record.get("cushion") or {}) or None}

    # ---------- 节奏 + 垫句（期二：中枢的「陪着说」，都不在关键路径上） ----------
    def _pace_source(self) -> dict:
        meta = getattr(self.pace_fn, "meta", None)
        if isinstance(meta, dict):
            return dict(meta)
        return {"source": "可替换接口", "model": "未标注", "note": "pace_fn 未标注来源"}

    def _cushion_source(self) -> dict:
        meta = getattr(self.cushion_fn, "meta", None)
        if isinstance(meta, dict):
            return dict(meta)
        return {"source": "可替换接口", "model": "未标注", "note": "cushion_fn 未标注来源"}

    def _start_pace(self, version, text, scene, t_ms, speech_ms, turn):
        """这一版的节奏：立刻独立发出去（与接不接 / 应不应一声同一拍，不等判停）。"""
        with self._lock:
            self.counters["pace_calls"] += 1
            self._pace_pool.submit(self._run_pace,
                                   {"version": version, "text": text, "scene": scene,
                                    "t_ms": t_ms, "speech_ms": speech_ms, "turn": turn})

    def _run_pace(self, request):
        began = time.monotonic()
        try:
            outcome = self.pace_fn(request["text"], request["scene"], self.seat,
                                   speech_ms=request["speech_ms"]) or {}
        except Exception as error:  # noqa: BLE001 - 节奏判断失败：不换档、不垫（R3），界面可见
            outcome = {"pace": None, "state": "failed",
                       "error": {"kind": type(error).__name__, "detail": str(error)[:200]},
                       "source": self._pace_source().get("source", "可替换接口"),
                       "model": self._pace_source().get("model", "未标注"),
                       "reason": "节奏判断失败：这一拍不换档、不问垫句（界面可见）"}
        latency = (time.monotonic() - began) * 1000
        with self._lock:
            settled = (self.records.get(request["version"]) or {}).get("pace") or {}
            if settled.get("state") == "timeout":
                # 已经记成超时：晚回来的结论不再翻案（不换档、不补问垫句）——超时就是超时。
                self.counters["pace_late"] += 1
                return
            outcome.setdefault("latency_ms", round(latency, 1))
            outcome["state"] = "failed" if outcome.get("pace") is None else "ok"
            record = self.records.get(request["version"])
            if outcome["state"] == "ok":
                # 开场保持 + 平滑换档（拍板 2）：节奏不因为一版字硬切，也不能一版一翻。
                probabilities = outcome.get("probabilities") or {}
                raw = outcome.get("pace")
                if not probabilities and raw:
                    # 判断方没给分数：按它判的那一档当满分（只影响换档那条闸，不影响别的）。
                    probabilities = {raw: 1.0}
                smoothed, held, why = hold_pace(raw, probabilities, self.pace, request.get("turn"))
                outcome["raw_pace"] = raw
                outcome["pace"] = smoothed
                outcome["label"] = PACE_LABELS.get(smoothed, smoothed)
                outcome["held"] = bool(held)
                outcome["hold_reason"] = why
                self.pace = smoothed
            if record is not None:
                record["pace"] = outcome
                record["pace_t_ms"] = request["t_ms"]
                record["pace_done_ms"] = round(latency, 1)
            if outcome["state"] != "ok":
                self.counters["pace_errors"] += 1
            self._emit("hub_pace", request["t_ms"],
                       state=outcome["state"],
                       pace=outcome.get("pace"), label=outcome.get("label"),
                       raw_pace=outcome.get("raw_pace"), held=outcome.get("held"),
                       hold_reason=outcome.get("hold_reason"),
                       probabilities=outcome.get("probabilities"),
                       score=outcome.get("score"), confidence=outcome.get("confidence"),
                       reason=outcome.get("reason"), scope=outcome.get("scope"),
                       source=outcome.get("source"), model=outcome.get("model"),
                       latency_ms=outcome.get("latency_ms"), error=outcome.get("error"),
                       speech_ms=request.get("speech_ms"), turn=request.get("turn"),
                       text_version=request["version"])
            # 垫句：**只有**节奏是「积极」且这一版已经连续说满 1.5 秒才问（其它三档根本不问）。
            self._plan_cushion(request, outcome)
            self._inflight.pop(request["version"], None)

    def _plan_cushion(self, request, pace_outcome):
        """决定这一版要不要发「垫句」这一问；不发就把原因记在记录里（界面/验收能看见「根本没问」）。"""
        with self._lock:
            record = self.records.get(request["version"])
            if record is None:
                return
            speech_ms = request.get("speech_ms")
            tier = pace_outcome.get("pace")
            unasked = {"say": False, "state": "not_asked", "speech_ms": speech_ms,
                       "pace": tier, "asked": False, **self._cushion_source()}
            if pace_outcome.get("state") != "ok":
                unasked["reason"] = "节奏没判成：不问垫句（不换档、不垫）"
                record["cushion"] = unasked
                return
            if tier != PACE_EAGER:
                unasked["reason"] = ("节奏不是积极（%s）：不问垫句"
                                     % PACE_LABELS.get(tier, tier))
                record["cushion"] = unasked
                return
            if speech_ms is None:
                unasked["reason"] = "这一版没有带「已连续说满多久」：不问垫句（A/B 或本期关掉）"
                record["cushion"] = unasked
                return
            if int(speech_ms) < CUSHION_MIN_SPEECH_MS:
                unasked["reason"] = ("对方还没说满 %.1f 秒：不问垫句（已 %.0fms）"
                                     % (CUSHION_MIN_SPEECH_MS / 1000.0, float(speech_ms)))
                record["cushion"] = unasked
                return
            if self.cushion_fn is None:
                unasked["reason"] = "垫句这一路没挂：不问垫句"
                record["cushion"] = unasked
                return
            record["cushion"] = {"state": "pending", "asked": True, "speech_ms": speech_ms,
                                 "pace": tier, **self._cushion_source()}
            self.counters["cushion_calls"] += 1
            self._cushion_pool.submit(self._run_cushion,
                                      {"version": request["version"], "text": request["text"],
                                       "scene": request["scene"], "t_ms": request["t_ms"],
                                       "speech_ms": speech_ms, "turn": request.get("turn"),
                                       "pace": tier})

    def _run_cushion(self, request):
        began = time.monotonic()
        try:
            outcome = self.cushion_fn(request["text"], request["scene"], self.seat,
                                      speech_ms=request["speech_ms"]) or {}
        except Exception as error:  # noqa: BLE001 - 判断失败：这一版不垫（R3），界面可见
            outcome = {"say": False, "state": "failed", "asked": True,
                       "error": {"kind": type(error).__name__, "detail": str(error)[:200]},
                       "source": self._cushion_source().get("source", "可替换接口"),
                       "model": self._cushion_source().get("model", "未标注"),
                       "reason": "垫句判断失败：这一版不垫（界面可见）"}
        latency = (time.monotonic() - began) * 1000
        with self._lock:
            settled = (self.records.get(request["version"]) or {}).get("cushion") or {}
            if settled.get("state") == "timeout":
                # 已经记成超时：晚回来的结论不再翻案（这一版就是不垫）。
                self.counters["cushion_late"] += 1
                return
            outcome.setdefault("latency_ms", round(latency, 1))
            outcome["state"] = "failed" if outcome.get("say") is None or \
                outcome.get("state") == "failed" else "ok"
            outcome["asked"] = True
            outcome["speech_ms"] = request["speech_ms"]
            outcome["pace"] = request.get("pace")
            record = self.records.get(request["version"])
            if record is not None:
                record["cushion"] = outcome
                record["cushion_t_ms"] = request["t_ms"]
                record["cushion_done_ms"] = round(latency, 1)
            if outcome["state"] != "ok":
                self.counters["cushion_errors"] += 1
            elif outcome.get("say"):
                self.counters["cushion_say"] += 1
            self._emit("hub_cushion", request["t_ms"],
                       say=outcome.get("say"), state=outcome["state"], asked=True,
                       score=outcome.get("score"), confidence=outcome.get("confidence"),
                       threshold=outcome.get("threshold"), reason=outcome.get("reason"),
                       source=outcome.get("source"), model=outcome.get("model"),
                       latency_ms=outcome.get("latency_ms"), error=outcome.get("error"),
                       speech_ms=request["speech_ms"], pace=request.get("pace"),
                       text_version=request["version"])

    # ---------- 两人同时说四选（期三：只在这一回插话上问一次，不在关键路径上） ----------
    def _floor_source(self) -> dict:
        meta = getattr(self.floor_fn, "meta", None)
        if isinstance(meta, dict):
            return dict(meta)
        return {"source": "可替换接口", "model": "未标注", "note": "floor_fn 未标注来源"}

    def start_floor(self, episode: int, insertion_text: str, hub_sentence=None,
                    scene=None, t_ms: int = None, *, hub_state: str = "preparing",
                    version=None) -> None:
        """这一回插话的四选：立刻独立发出去（与这一版的判停 / 接不接并行，不串行等待）。

        **按 episode 记账、不按版**：同一段插话的后续 partial 不再重复问（中枢自己只发一次），
        所以它不会变成「每版固定多一路」；只在「这边正在说 / 正在准备说，人又开口」的那一次多一次。
        """
        if self.floor_fn is None:
            return
        t_ms = self._now(t_ms)
        with self._lock:
            self.counters["floor_calls"] += 1
            self.floor_records[int(episode)] = {
                "episode": int(episode), "state": "pending", "text": insertion_text or "",
                "hub_state": hub_state, "hub_sentence": hub_sentence, "t_ms": t_ms,
                "asked_ms": t_ms, "version": version, **self._floor_source()}
            while len(self.floor_records) > _KEEP_VERSIONS:
                self.floor_records.pop(next(iter(self.floor_records)))
        self._floor_pool.submit(self._run_floor,
                                {"episode": int(episode), "text": insertion_text or "",
                                 "scene": scene, "t_ms": t_ms, "hub_state": hub_state,
                                 "hub_sentence": hub_sentence})

    def _run_floor(self, request):
        began = time.monotonic()
        try:
            outcome = self.floor_fn(request["text"], request["scene"], self.seat,
                                    hub_state=request.get("hub_state"),
                                    hub_sentence=request.get("hub_sentence")) or {}
        except Exception as error:  # noqa: BLE001 - 判断失败必须显式记账（失败 → 按「继续说」兜底）
            outcome = {"choice": None, "outlet": None, "state": "failed",
                       "error": {"kind": type(error).__name__, "detail": str(error)[:200]},
                       "reason": "四选判断失败 → 按「继续说」兜底（不打断自己、不丢人的话）"}
        latency = (time.monotonic() - began) * 1000
        with self._lock:
            record = self.floor_records.get(request["episode"])
            if (record or {}).get("state") == "timeout":
                # 已经记成超时：晚回来的结论不再翻案（这一回就是「继续说」）。
                self.counters["floor_late"] += 1
                return
            outcome.setdefault("latency_ms", round(latency, 1))
            outcome.setdefault("state", "failed" if outcome.get("choice") is None else "ok")
            outcome["state"] = "failed" if outcome.get("choice") is None else "ok"
            if record is not None:
                record.update(outcome)
                record["done_ms"] = round(latency, 1)
            if outcome["state"] != "ok":
                self.counters["floor_errors"] += 1
            else:
                key = "floor_" + str(outcome.get("outlet") or outcome.get("choice"))
                if key in self.counters:
                    self.counters[key] += 1
            self._emit("hub_floor", request["t_ms"], episode=request["episode"],
                       choice=outcome.get("choice"), outlet=outcome.get("outlet"),
                       label=outcome.get("label"), action=outcome.get("action"),
                       state=outcome["state"], score=outcome.get("score"),
                       confidence=outcome.get("confidence"),
                       probabilities=outcome.get("probabilities"),
                       reason=outcome.get("reason"), threshold_note=outcome.get("threshold_note"),
                       source=outcome.get("source"), model=outcome.get("model"),
                       latency_ms=outcome.get("latency_ms"), error=outcome.get("error"),
                       hub_state=request.get("hub_state"),
                       text_version=request.get("version"))

    def floor_verdict(self, episode: int, now_ms: int = None) -> dict | None:
        """这一回插话的四选结论（没回来时 state=pending；到点记超时 → 中枢按「继续说」兜底）。"""
        with self._lock:
            record = self.floor_records.get(int(episode))
            if record is None:
                return None
            if record.get("state") == "pending" and now_ms is not None:
                if int(now_ms) - int(record["asked_ms"]) >= self.floor_timeout_ms:
                    record.update({"state": "timeout", "choice": None, "outlet": None,
                                   "reason": "四选判断超时（%.1fs）→ 按「继续说」兜底（不打断自己、"
                                             "不丢人的话）" % (self.floor_timeout_ms / 1000.0),
                                   **self._floor_source()})
                    self.counters["floor_errors"] += 1
                    self._emit("hub_floor", int(now_ms), episode=int(episode), state="timeout",
                               choice=None, timed_out=True, reason=record["reason"],
                               source=record.get("source"),
                               text_version=record.get("version"))
            return dict(record)

    # ---------- 冷场三选（期四：只在「安静到刻度、真的没人说话」时问一次） ----------
    def _quiet_source(self) -> dict:
        meta = getattr(self.quiet_fn, "meta", None)
        if isinstance(meta, dict):
            return dict(meta)
        return {"source": "可替换接口", "model": "未标注", "note": "quiet_fn 未标注来源"}

    def start_quiet(self, episode: int, *, quiet_ms: int, text: str = "", hub_last: str = "",
                    check_allowed=None, scene=None, t_ms: int = None) -> None:
        """这一拍的冷场三选：立刻独立发出去（不在判停 / 接不接 / 应声的关键路径上）。

        **按刻度这一回记账、不按版**：一个刻度只问一次（中枢自己只发一次），所以它不是
        「每版固定多一路」，而是「每到一个安静刻度多一次」；同一段安静里连问两次也不会发生。
        """
        if self.quiet_fn is None:
            return
        t_ms = self._now(t_ms)
        with self._lock:
            self.counters["quiet_calls"] += 1
            self.quiet_records[int(episode)] = {
                "episode": int(episode), "state": "pending", "quiet_ms": int(quiet_ms),
                "text": text or "", "hub_last": hub_last or "",
                "check_allowed": bool(check_allowed), "asked_ms": t_ms,
                "source": self._quiet_source().get("source"),
                "model": self._quiet_source().get("model")}
            while len(self.quiet_records) > _KEEP_VERSIONS:
                self.quiet_records.pop(next(iter(self.quiet_records)))
        self._quiet_pool.submit(self._run_quiet,
                                {"episode": int(episode), "quiet_ms": int(quiet_ms),
                                 "text": text or "", "hub_last": hub_last or "",
                                 "check_allowed": check_allowed, "scene": scene, "t_ms": t_ms})

    def _run_quiet(self, request):
        began = time.monotonic()
        try:
            outcome = self.quiet_fn(quiet_ms=request["quiet_ms"], hub_last=request["hub_last"],
                                    recent=request["text"], scene=request["scene"],
                                    seat=self.seat,
                                    check_allowed=request.get("check_allowed")) or {}
        except Exception as error:  # noqa: BLE001 - 判断失败必须显式记账（失败 → 不出声，按「再等」走）
            outcome = {"choice": None, "outlet": None, "state": "failed",
                       "error": {"kind": type(error).__name__, "detail": str(error)[:200]},
                       "reason": "冷场判断失败 → 不出声（按「再等」走）"}
        latency = (time.monotonic() - began) * 1000
        with self._lock:
            record = self.quiet_records.get(request["episode"])
            if (record or {}).get("state") == "timeout":
                # 已经记成超时：晚回来的结论不再翻案（这一拍就是「再等」）。
                self.counters["quiet_late"] += 1
                return
            outcome.setdefault("latency_ms", round(latency, 1))
            outcome.setdefault("state", "failed" if outcome.get("choice") is None else "ok")
            outcome["state"] = "failed" if outcome.get("choice") is None else "ok"
            if record is not None:
                record.update(outcome)
                record["done_ms"] = round(latency, 1)
            if outcome["state"] != "ok":
                self.counters["quiet_errors"] += 1
            else:
                key = "quiet_" + str(outcome.get("outlet") or outcome.get("choice"))
                if key in self.counters:
                    self.counters[key] += 1
            self._emit("hub_quiet", request["t_ms"], episode=request["episode"],
                       choice=outcome.get("choice"), outlet=outcome.get("outlet"),
                       label=outcome.get("label"), action=outcome.get("action"),
                       state=outcome["state"], score=outcome.get("score"),
                       confidence=outcome.get("confidence"),
                       probabilities=outcome.get("probabilities"),
                       reason=outcome.get("reason"), threshold_note=outcome.get("threshold_note"),
                       source=outcome.get("source"), model=outcome.get("model"),
                       latency_ms=outcome.get("latency_ms"), error=outcome.get("error"),
                       quiet_ms=request["quiet_ms"], check_allowed=request.get("check_allowed"))

    def quiet_verdict(self, episode: int, now_ms: int = None) -> dict | None:
        """这一拍冷场三选的结论（没回来时 state=pending；到点记超时 → 不出声，按「再等」走）。"""
        with self._lock:
            record = self.quiet_records.get(int(episode))
            if record is None:
                return None
            if record.get("state") == "pending" and now_ms is not None:
                if int(now_ms) - int(record["asked_ms"]) >= self.quiet_timeout_ms:
                    record.update({"state": "timeout", "choice": None, "outlet": None,
                                   "reason": "冷场判断超时（%.1fs）→ 不出声（按「再等」走）"
                                             % (self.quiet_timeout_ms / 1000.0),
                                   **self._quiet_source()})
                    self.counters["quiet_errors"] += 1
                    self._emit("hub_quiet", int(now_ms), episode=int(episode), state="timeout",
                               choice=None, timed_out=True, reason=record["reason"],
                               quiet_ms=record.get("quiet_ms"), source=record.get("source"))
            return dict(record)

    def pace_judgement(self, version: int) -> dict | None:
        """这一版字的节奏结论（含「这一拍实际的节奏」与平滑换档说明）。"""
        with self._lock:
            record = self.records.get(int(version))
            if record is None:
                return None
            outcome = record.get("pace")
            if outcome is None:
                return {"state": "pending"}
            return dict(outcome)

    def cushion_inputs(self, newest: int = 4) -> list:
        """最近几版里「判停 / 要不要回答 / 节奏 / 垫句（问过或没问都算）」都齐的那几版。

        垫句**只在积极 + 满 1.5 秒**那一版才真问；其它版本 `cushion.state=not_asked`（带原因），
        也照样给出来——这样中枢能对每一版都说明「这一版根本没问垫句，为什么」。
        """
        with self._lock:
            out = []
            for version in sorted(self.records, reverse=True)[:max(1, int(newest))]:
                record = self.records[version]
                accept = record.get("accept") or {}
                pace = record.get("pace") or {}
                cushion = record.get("cushion") or {}
                if record.get("eot") is None:
                    continue
                if accept.get("state") in (None, "pending"):
                    continue
                if pace.get("state") in (None, "pending"):
                    continue
                if cushion.get("state") in (None, "pending"):
                    continue
                out.append({"version": int(version), "t_start_ms": record.get("t_start_ms"),
                            "turn": record.get("turn"), "speech_ms": record.get("speech_ms"),
                            "eot": dict(record.get("eot") or {}), "accept": dict(accept),
                            "ack": dict(record.get("ack") or {}), "pace": dict(pace),
                            "cushion": dict(cushion)})
            return out

    def ack_inputs(self, newest: int = 4) -> list:
        """最近几版里「三份判断都已经回来」的那几版，按版本从新到旧给出来（应声决策用）。

        只要有一份还在飞（accept / ack pending）就不算「回来」：这一版先不出声，下一拍再看。
        """
        with self._lock:
            out = []
            for version in sorted(self.records, reverse=True)[:max(1, int(newest))]:
                record = self.records[version]
                accept = record.get("accept") or {}
                ack = record.get("ack") or {}
                if record.get("eot") is None:
                    continue
                if accept.get("state") in (None, "pending"):
                    continue
                if ack.get("state") in (None, "pending"):
                    continue
                out.append({"version": int(version), "t_start_ms": record.get("t_start_ms"),
                            "eot": dict(record.get("eot") or {}), "accept": dict(accept),
                            "ack": dict(ack)})
            return out

    # ---------- 判停：主链回填 / 调试页自跑 ----------
    def _start_eot(self, version, text, t_ms):
        self.counters["eot_calls"] += 1
        self._pool.submit(self._run_eot, {"version": version, "text": text, "t_ms": t_ms})

    def _run_eot(self, request):
        began = time.monotonic()
        try:
            scores = self.eot_fn(request["text"]) or {}
        except Exception as error:  # noqa: BLE001 - 判停失败：这一版不能算「说完」，闸门不开
            self.on_eot(request["version"], request["t_ms"],
                        {"label": "error", "may_confirm": False, "may_start_early": False,
                         "error": {"kind": type(error).__name__, "detail": str(error)[:200]}})
            return
        self.on_eot(request["version"], request["t_ms"], {
            "label": scores.get("label"),
            "may_confirm": bool(scores.get("may_confirm")),
            "may_start_early": bool(scores.get("may_start_early")),
            "complete": scores.get("complete"), "cut": scores.get("cut"),
            "wait": scores.get("wait"), "confidence": scores.get("confidence"),
            "latency_ms": round((time.monotonic() - began) * 1000, 1),
        })

    def on_eot(self, version: int, t_ms: int, verdict: dict) -> None:
        """判停一路的结果回填（主链由 TurnSession 调；调试页由本类 worker 调）。"""
        t_ms = self._now(t_ms)
        with self._lock:
            record = self.records.get(int(version))
            if record is None:
                self.counters["version_mismatch"] += 1
                self._emit("hub_eot", t_ms, version=int(version), stale=True,
                           reason="这一版字已被新一版取代：判停结果不作数", text_version=int(version))
                return
            record["eot"] = dict(verdict)
            self._emit("hub_eot", t_ms, version=int(version), label=verdict.get("label"),
                       may_confirm=verdict.get("may_confirm"),
                       confidence=verdict.get("confidence"),
                       complete=verdict.get("complete"), cut=verdict.get("cut"),
                       wait=verdict.get("wait"), latency_ms=verdict.get("latency_ms"),
                       # `settled_by=text_send`：这一版不是判停模型判的，是文字输入「发送即收住」，
                       # 界面据此如实写出来源，不冒充判停结论。
                       settled_by=verdict.get("settled_by"),
                       error=verdict.get("error"), text_version=int(version))

    # ---------- 时间推进 ----------
    def poll(self, now_ms: int) -> None:
        """收结果 / 超时收口（每次 tick 调一次即可，不阻塞）。"""
        with self._lock:
            pending = [(version, rec) for version, rec in self.records.items()
                       if rec["accept"].get("state") == "pending" and rec["gate"] is None]
            for version, rec in pending:
                if now_ms - rec["t_start_ms"] >= self.accept_timeout_ms:
                    # 超时不是「不接」：这一版先不开闸；若判断后来回来，`_run_accept` 会覆盖成 ok，
                    # 闸门仍可按同一版字的这份结论开闸（不重取场景、不重判）。
                    rec["accept"] = {"state": "timeout", "accept": None,
                                     "reason": "接不接判断超时（%.1fs）→ 先不开闸，不当成「不回答」"
                                     % (self.accept_timeout_ms / 1000.0),
                                     **self._accept_source()}
                    self.counters["accept_errors"] += 1
                    self._emit("hub_accept", now_ms, version=version, state="timeout", accept=None,
                               timed_out=True, reason=rec["accept"]["reason"],
                               source=rec["accept"].get("source"), model=rec["accept"].get("model"),
                               threshold=rec["accept"].get("threshold"),
                               text_version=version)
            if self.ack_fn is not None:
                for version, rec in list(self.records.items()):
                    ack = rec.get("ack")
                    if ack is not None and ack.get("state") != "pending":
                        continue                       # 已经有结论 / 已经记过超时
                    if now_ms - rec["t_start_ms"] >= self.ack_timeout_ms:
                        # 应声判断超时：这一版不出声（R3），但**不当成「不出声」的结论**硬记；
                        # 晚回来仍可出声（只要这一版还是当前这一版）。
                        rec["ack"] = {"state": "timeout", "say": False, "spoken": False,
                                      "reason": "应声判断超时（%.1fs）→ 这一版不出声"
                                                % (self.ack_timeout_ms / 1000.0),
                                      **self._ack_source()}
                        self.counters["ack_errors"] += 1
                        self._emit("hub_ack", now_ms, version=version, state="timeout", say=False,
                                   timed_out=True, reason=rec["ack"]["reason"],
                                   source=rec["ack"].get("source"),
                                   text_version=version)
            if self.pace_fn is not None:
                for version, rec in list(self.records.items()):
                    pace = rec.get("pace")
                    if pace is not None and pace.get("state") != "pending":
                        continue                       # 已经有结论 / 已经记过超时
                    if now_ms - rec["t_start_ms"] >= self.pace_timeout_ms:
                        # 节奏判断超时：**不换档、不垫**（R3），保持上一拍；不当成「平常」硬记。
                        rec["pace"] = {"state": "timeout", "pace": None,
                                       "reason": "节奏判断超时（%.1fs）→ 这一拍不换档、不垫"
                                                 % (self.pace_timeout_ms / 1000.0),
                                       **self._pace_source()}
                        unasked = {"say": False, "state": "not_asked", "asked": False,
                                   "reason": "节奏判断超时：不问垫句",
                                   **self._cushion_source()}
                        if rec.get("cushion") is None:
                            rec["cushion"] = unasked
                        self.counters["pace_errors"] += 1
                        self._emit("hub_pace", now_ms, version=version, state="timeout",
                                   pace=None, timed_out=True, reason=rec["pace"]["reason"],
                                   source=rec["pace"].get("source"), text_version=version)
            if self.cushion_fn is not None:
                for version, rec in list(self.records.items()):
                    cushion = rec.get("cushion")
                    if cushion is None or cushion.get("state") != "pending":
                        continue
                    if now_ms - rec["t_start_ms"] >= self.cushion_timeout_ms:
                        # 垫句判断超时：这一版不垫（R3）；晚回来仍可在这一版还是当前版时出声。
                        rec["cushion"] = {"state": "timeout", "say": False, "asked": True,
                                          "reason": "垫句判断超时（%.1fs）→ 这一版不垫"
                                                    % (self.cushion_timeout_ms / 1000.0),
                                          **self._cushion_source()}
                        self.counters["cushion_errors"] += 1
                        self._emit("hub_cushion", now_ms, version=version, state="timeout",
                                   say=False, asked=True, timed_out=True,
                                   reason=rec["cushion"]["reason"],
                                   source=rec["cushion"].get("source"),
                                   text_version=version)

    # ---------- 开闸 ----------
    def gate(self, version: int, now_ms: int, assume_done: bool = False) -> dict:
        """这一版字上的闸门结论（只认「说完」那一刻的那一份）。

        `assume_done=True`：判停这一路还没判「说完」、但本席静音窗到上限（`cut_wait` / `max_silence` /
        `stop`）已经把这一版字收口——按「说完」处理，仍只用同一版字上的那份接不接；
        `assume_done=False`（默认，`eot_complete` 路径）只认判停这一路真的判了「说完」。
        """
        now_ms = self._now(now_ms)
        with self._lock:
            record = self.records.get(int(version))
            if record is None:
                self.counters["version_mismatch"] += 1
                return self._verdict(record=None, route=ROUTE_CONTEXT_ONLY, state=GATE_CLOSED,
                                     reason="版本对不齐（没有这一版的两路判断）→ 不进 Agent",
                                     version=int(version), now_ms=now_ms)
            if record.get("gate") is not None:
                return record["gate"]
            eot = record.get("eot") or {}
            accept = record.get("accept") or {}
            scene = record.get("scene") or {}
            scene_block = self._scene_brief(scene)
            common = {"version": int(version), "text": record.get("text", ""),
                      "scene": scene_block,
                      "eot": {key: eot.get(key) for key in ("label", "may_confirm", "confidence",
                                                            "complete", "cut", "wait", "latency_ms",
                                                            "error")},
                      "accept": {key: accept.get(key) for key in ("state", "accept", "confidence",
                                                                  "score", "threshold", "threshold_note",
                                                                  "reason", "source", "model", "latency_ms",
                                                                  "scene_used", "error")}}
            if not eot:
                # 判停这一路还没回来：不算「说完」，先不开闸（也不消耗接不接的中间结论）。
                return self._verdict(record=record, route=ROUTE_WAIT, state=GATE_WAIT,
                                     reason="判停还没回来：先不开闸", version=int(version),
                                     now_ms=now_ms, extra=common, emit=False)
            if not eot.get("may_confirm") and not assume_done:
                self.counters["gate_wait"] += 1
                if eot.get("label") == "error":
                    why = "判停失败：这一版不能算说完"
                elif eot.get("label") == "complete":
                    why = "判停说「可能说完」但没过能确认的线：先不开闸"
                else:
                    why = "判停这一路说「还没说完」"
                verdict = self._verdict(record=record, route=ROUTE_WAIT, state=GATE_WAIT, reason=why,
                                        version=int(version), now_ms=now_ms, extra=common,
                                        extra_reason={"accept_note": "中间的「接不接」不算数"},
                                        emit=False)
                # 「还没说完」不作废、不缓存成结论：同一版字后面判成「说完」时仍要按那份接不接开闸。
                if not record.get("wait_noted"):
                    record["wait_noted"] = True
                    self._emit("gate_wait", now_ms, state=GATE_WAIT, reason=why,
                               version=int(version), text=verdict.get("text"),
                               eot=verdict.get("eot"), accept=verdict.get("accept"),
                               scene_age_ms=verdict.get("scene_age_ms"),
                               two_route_ms=verdict.get("two_route_ms"), text_version=int(version))
                return verdict
            state = accept.get("state")
            if state == "pending":
                if now_ms - record["t_start_ms"] < self.accept_timeout_ms:
                    # 判停已说完，接不接还在飞：闸门等这一份（不重新取场景、不重判）。
                    return self._verdict(record=None, route=ROUTE_WAIT, state=GATE_PENDING,
                                         reason="判停已说完，同一版字的「接不接」还在飞：等这一份",
                                         version=int(version), now_ms=now_ms, extra=common, emit=False)
                # 超时：先不开闸，但不缓存成「不接」；判断晚回来仍可改判。
                return self._verdict(record=None, route=ROUTE_ACCEPT_UNAVAILABLE,
                                     state=GATE_ACCEPT_TIMEOUT,
                                     reason="接不接超时 → 先不开闸（不当成「不回答」；晚回来仍可改判）",
                                     version=int(version), now_ms=now_ms, extra=common, emit=False)
            if state == "timeout":
                # 已被 poll 记为超时，但 worker 可能还在跑：先不开闸，等它回来（或下一版字）。
                return self._verdict(record=None, route=ROUTE_ACCEPT_UNAVAILABLE,
                                     state=GATE_ACCEPT_TIMEOUT,
                                     reason="接不接超时 → 先不开闸（不当成「不回答」；晚回来仍可改判）",
                                     version=int(version), now_ms=now_ms, extra=common, emit=False)
            if state != "ok" or accept.get("accept") is None:
                error = accept.get("error") or {}
                why = ("（%s）" % error.get("kind")) if error.get("kind") else ""
                return self._verdict(record=record, route=ROUTE_ACCEPT_UNAVAILABLE,
                                     state=GATE_ACCEPT_FAILED,
                                     reason="接不接判断失败%s → 先不开闸（不当成「不回答」）" % why,
                                     version=int(version), now_ms=now_ms, extra=common)
            note = "" if eot.get("may_confirm") else "（判停没说完，静音窗到上限当说完）"
            if accept.get("accept"):
                self.counters["gate_open"] += 1
                return self._verdict(record=record, route=ROUTE_AGENT, state=GATE_OPEN,
                                     reason="说完且要回答 → 开闸，进 Agent" + note, version=int(version),
                                     now_ms=now_ms, extra=common)
            self.counters["gate_context_only"] += 1
            return self._verdict(record=record, route=ROUTE_CONTEXT_ONLY, state=GATE_CONTEXT,
                                 reason="说完但不回答 → 不开闸，只进上下文" + note, version=int(version),
                                 now_ms=now_ms, extra=common)

    # ---------- 结论 / 快照 ----------
    def _verdict(self, *, record, route, state, reason, version, now_ms, extra=None,
                 emit=True, extra_reason=None) -> dict:
        verdict = {"route": route, "state": state, "reason": reason,
                   "version": int(version), "decided_at_ms": now_ms,
                   "gate_ms": None, "accept_source": (self._accept_source()),
                   "scene_age_ms": None, "scene_missing": None}
        if extra:
            verdict.update(extra)
        if verdict.get("scene"):
            verdict["scene_age_ms"] = verdict["scene"].get("age_ms")
            verdict["scene_missing"] = verdict["scene"].get("summary") is None
        eot = (extra or {}).get("eot") or {}
        accept = (extra or {}).get("accept") or {}
        if eot.get("latency_ms") is not None or accept.get("latency_ms") is not None:
            # 两路是并发的：关键路径 = max(判停, 接不接)，不是相加。
            verdict["two_route_ms"] = max(float(eot.get("latency_ms") or 0.0),
                                          float(accept.get("latency_ms") or 0.0))
        if extra_reason:
            verdict.update(extra_reason)
        if emit and record is not None:
            record["gate"] = {**verdict, "scene": _dumps(verdict.get("scene"))}
            self.counters["gates"] += 1
            self._last_gate = record["gate"]
            self._emit("gate_decision", now_ms, route=route, state=state, reason=reason,
                       version=int(version), text=verdict.get("text"),
                       scene=verdict.get("scene"), eot=verdict.get("eot"),
                       accept=verdict.get("accept"), scene_age_ms=verdict.get("scene_age_ms"),
                       two_route_ms=verdict.get("two_route_ms"),
                       accept_source=verdict.get("accept_source"),
                       text_version=int(version))
            if route == ROUTE_CONTEXT_ONLY:
                self._emit("gate_context_only", now_ms, version=int(version),
                           text=verdict.get("text"), reason=reason, text_version=int(version))
        return verdict

    def _scene_brief(self, scene: dict | None) -> dict:
        if not scene:
            return {"summary": None, "revision": None, "age_ms": None, "missing": True,
                    "note": "无场景：按 wiki 默认照常判（缺场景 ≠ 缺判断）"}
        return {"summary": scene.get("summary"), "intent": scene.get("intent"),
                "target": scene.get("target"), "topic": scene.get("topic"),
                "cues": scene.get("cues"), "visual": scene.get("visual"),
                "revision": scene.get("revision"), "updated_at_ms": scene.get("updated_at_ms"),
                "age_ms": scene.get("age_ms"), "taken_at_ms": scene.get("taken_at_ms"),
                "pending": scene.get("pending"), "missing": False}

    def snapshot(self) -> dict:
        """调试页读这一份：当前场景 / 这一版字 / 两路判断 / 闸门结果。"""
        with self._lock:
            record = self.records.get(self.version)
            if record is None and self.records:
                record = list(self.records.values())[-1]
            if record is None:
                return {"version": self.version, "text": "", "scene": None, "eot": None,
                        "accept": None, "ack": None, "gate": None,
                        "accept_source": self._accept_source(),
                        "ack_source": self._ack_source(),
                        "counters": dict(self.counters)}
            eot = record.get("eot") or {}
            accept = record.get("accept") or {}
            ack = record.get("ack") or {"state": "pending" if self.ack_fn is not None else "off"}
            pace = record.get("pace") or {"state": "pending" if self.pace_fn is not None else "off"}
            cushion = record.get("cushion") or {
                "state": "pending" if self.cushion_fn is not None else "off"}
            floor = (list(self.floor_records.values())[-1] if self.floor_records else
                     {"state": "pending" if self.floor_fn is not None else "off"})
            quiet = (list(self.quiet_records.values())[-1] if self.quiet_records else
                     {"state": "pending" if self.quiet_fn is not None else "off"})
            return {
                "version": record["version"],
                "text": record.get("text"),
                "final": record.get("final"),
                "scene": self._scene_brief(record.get("scene")),
                "eot": {"label": eot.get("label"), "may_confirm": eot.get("may_confirm"),
                        "confidence": eot.get("confidence"), "complete": eot.get("complete"),
                        "cut": eot.get("cut"), "wait": eot.get("wait"),
                        "latency_ms": eot.get("latency_ms"), "error": eot.get("error")}
                if eot else None,
                "accept": {"state": accept.get("state", "pending"), "accept": accept.get("accept"),
                           "confidence": accept.get("confidence"), "score": accept.get("score"),
                           "threshold": accept.get("threshold"),
                           "threshold_note": accept.get("threshold_note"),
                           "reason": accept.get("reason"), "source": accept.get("source"),
                           "model": accept.get("model"), "error": accept.get("error"),
                           "scene_used": accept.get("scene_used"),
                           "latency_ms": accept.get("latency_ms")},
                "ack": {"state": ack.get("state"), "say": ack.get("say"),
                        "score": ack.get("score"), "confidence": ack.get("confidence"),
                        "threshold": ack.get("threshold"), "reason": ack.get("reason"),
                        "source": ack.get("source"), "model": ack.get("model"),
                        "speech": (self._ack_source() or {}).get("speech"),
                        "error": ack.get("error"), "latency_ms": ack.get("latency_ms")},
                "pace": {"state": pace.get("state"), "pace": pace.get("pace"),
                         "label": pace.get("label"), "raw_pace": pace.get("raw_pace"),
                         "held": pace.get("held"), "hold_reason": pace.get("hold_reason"),
                         "probabilities": pace.get("probabilities"),
                         "score": pace.get("score"), "confidence": pace.get("confidence"),
                         "reason": pace.get("reason"), "scope": pace.get("scope"),
                         "source": pace.get("source"), "model": pace.get("model"),
                         "tiers": (self._pace_source() or {}).get("tiers"),
                         "error": pace.get("error"), "latency_ms": pace.get("latency_ms")},
                "cushion": {"state": cushion.get("state"), "asked": cushion.get("asked"),
                            "say": cushion.get("say"), "speech_ms": cushion.get("speech_ms"),
                            "score": cushion.get("score"),
                            "confidence": cushion.get("confidence"),
                            "threshold": cushion.get("threshold"), "reason": cushion.get("reason"),
                            "source": cushion.get("source"), "model": cushion.get("model"),
                            "words": (self._cushion_source() or {}).get("words"),
                            "speech": (self._cushion_source() or {}).get("speech"),
                            "error": cushion.get("error"), "latency_ms": cushion.get("latency_ms")},
                "gate": record.get("gate"),
                "floor": {"episode": floor.get("episode"), "state": floor.get("state"),
                          "choice": floor.get("choice"), "label": floor.get("label"),
                          "action": floor.get("action"), "text": floor.get("text"),
                          "hub_state": floor.get("hub_state"), "reason": floor.get("reason"),
                          "source": floor.get("source"), "model": floor.get("model"),
                          "outlets": (self._floor_source() or {}).get("outlets"),
                          "error": floor.get("error"), "latency_ms": floor.get("latency_ms")},
                "quiet": {"episode": quiet.get("episode"), "state": quiet.get("state"),
                          "choice": quiet.get("choice"), "label": quiet.get("label"),
                          "action": quiet.get("action"), "quiet_ms": quiet.get("quiet_ms"),
                          "check_allowed": quiet.get("check_allowed"),
                          "reason": quiet.get("reason"), "source": quiet.get("source"),
                          "model": quiet.get("model"),
                          "outlets": (self._quiet_source() or {}).get("outlets"),
                          "first_ms": (self._quiet_source() or {}).get("first_ms"),
                          "check_after_ms": (self._quiet_source() or {}).get("check_after_ms"),
                          "error": quiet.get("error"), "latency_ms": quiet.get("latency_ms")},
                "accept_source": self._accept_source(),
                "ack_source": self._ack_source(),
                "pace_source": self._pace_source(),
                "cushion_source": self._cushion_source(),
                "quiet_source": self._quiet_source(),
                "counters": dict(self.counters),
                "records": len(self.records),
                # 每一版各自的接不接结论（调试页 / 验收看「没有被后一版顶掉」）
                "versions": [
                    {"version": rec["version"], "text": rec.get("text", "")[:40],
                     "accept": {"state": (rec.get("accept") or {}).get("state", "pending"),
                                "accept": (rec.get("accept") or {}).get("accept"),
                                "score": (rec.get("accept") or {}).get("score"),
                                "confidence": (rec.get("accept") or {}).get("confidence")},
                     "eot": {"label": (rec.get("eot") or {}).get("label")},
                     "ack": {"state": (rec.get("ack") or {}).get("state"),
                             "say": (rec.get("ack") or {}).get("say")},
                     "pace": {"state": (rec.get("pace") or {}).get("state"),
                              "pace": (rec.get("pace") or {}).get("pace"),
                              "held": (rec.get("pace") or {}).get("held")},
                     "cushion": {"state": (rec.get("cushion") or {}).get("state"),
                                 "asked": (rec.get("cushion") or {}).get("asked"),
                                 "say": (rec.get("cushion") or {}).get("say")},
                     "speech_ms": rec.get("speech_ms"), "turn": rec.get("turn"),
                     "route": (rec.get("gate") or {}).get("route")}
                    for _, rec in sorted(self.records.items())[-6:]
                ],
            }

    def close(self) -> None:
        with self._lock:
            self.records.clear()
            self._inflight.clear()


def build_eot_verdict(scores: dict, *, may_confirm: bool, may_start_early: bool) -> dict:
    """把 `turn_eot.score_eot` 的分数压成闸门要的判停一路结论（形状与 TurnSession 的 `_eot_verdict` 同口径）。"""
    return {"label": scores.get("label"),
            "may_confirm": bool(may_confirm), "may_start_early": bool(may_start_early),
            "complete": scores.get("complete"), "cut": scores.get("cut"),
            "wait": scores.get("wait"), "confidence": scores.get("confidence"),
            "latency_ms": scores.get("wall_ms") if scores.get("wall_ms") is not None else scores.get("latency_ms")}


__all__ = ["GateKeeper", "build_eot_verdict", "ROUTE_AGENT", "ROUTE_CONTEXT_ONLY", "ROUTE_WAIT",
           "ROUTE_ACCEPT_UNAVAILABLE", "GATE_OPEN", "GATE_CONTEXT", "GATE_WAIT", "GATE_PENDING",
           "GATE_ACCEPT_TIMEOUT", "GATE_ACCEPT_FAILED", "ACCEPT_TIMEOUT_MS", "ACK_TIMEOUT_MS",
           "PACE_TIMEOUT_MS", "CUSHION_TIMEOUT_MS", "FLOOR_TIMEOUT_MS", "QUIET_TIMEOUT_MS"]
