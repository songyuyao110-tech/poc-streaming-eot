"""Streaming turn session: ASR updates + VAD → KEV EOT judging + tool shortlist → per-segment decisions.

One turn can consume several ASR segments: a landed decision seals the text it was made on (the
"consumption cursor"), emits `segment_consumed` and returns to LISTENING; whatever the user says next
is judged as the unconsumed suffix only. The turn is over when the suffix is empty and the silence
timer (or an explicit stop()) says so.

All state transitions happen on the caller's thread (the driver that calls tick()); the injected
KEV/LLM callables run in worker threads and their results are applied only if text_version is
still the newest one, otherwise they are dropped as stale.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, fields
import json
from pathlib import Path
import re
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True

from core_pipeline_run import POC_ROOT, TOOL_SET, decide, select_tools
from turn_eot import score_eot
# 注意：TurnConfirmGate 在 turn_session 顶层就 import 会与 confirm_gate(→turn_session) 循环；
# 因此在 __init__ 里惰性加载（见 _confirm_gate()）。

EVIDENCE_ROOT = POC_ROOT / "evidence/turn-session"
_PUNCT = re.compile(r"[\s，。！？、；：,.!?;:\"'“”‘’()（）\[\]【】\-—…~]+")


def normalize(text: str) -> str:
    return _PUNCT.sub("", text or "")


@dataclass
class TurnConfig:
    # M3 recommended defaults: margin 0.2 + a 700 ms confirm window is the only measured setting that
    # drives premature LLM starts to 0%; see tmp/poc-streaming-final-report.md for the tradeoff table.
    eot_policy: str = "margin"          # argmax|margin
    eot_margin: float = 0.2
    spec_margin: float = 0.3
    speculative_llm: bool = True     # intentional: start the decision early, cancel it on reversal
    eot_input: str = "last_segment"     # last_segment|turn_text
    confirm_silence_ms: int = 700
    cut_wait_ms: int = 1500
    think_wait_ms: int = 3000
    max_silence_ms: int = 2000
    eot_timeout_ms: int = 900
    eot_max_inflight: int = 3        # >1 removes single-flight starvation: every prefix gets scored
    # Must stay below the ASR partial interval, otherwise each pending call is replaced before it fires
    # and prefixes stop being judged at all (measured at 150 ms/prefix).
    debounce_ms: int = 80
    reuse_tools_if_text_unchanged: bool = True
    # M3/M4 latency work: tools run concurrently (single-flight starves the newest text) and fire
    # without debounce, so the shortlist is usually ready before the EOT verdict lands; if it is not,
    # the decision may start on a slightly older shortlist and is reissued once it changes.
    tools_max_inflight: int = 1        # measured: concurrency does NOT help (see spec 的矩阵)
    allow_stale_shortlist: bool = True
    shortlist_reissue: bool = True
    warm_llm: bool = True               # one tiny completion on the first partial (hides cold start)
    tool_set: str = TOOL_SET              # the PoC's only tool set: core_score (242 tools / 17 domains)

    def __post_init__(self):
        if self.tool_set != TOOL_SET:
            raise ValueError("Unknown tool_set: only " + TOOL_SET + " is available")

    @classmethod
    def from_dict(cls, data):
        known = {item.name for item in fields(cls)}
        unknown = set(data or {}) - known
        if unknown:
            raise ValueError("Unknown TurnConfig fields: " + ", ".join(sorted(unknown)))
        return cls(**data)


class TurnSession:
    def __init__(self, cfg: TurnConfig, on_event, *, clock=None, eot_fn=score_eot,
                 select_tools_fn=select_tools, decide_fn=decide, session_id=None,
                 evidence_root=EVIDENCE_ROOT, warm_fn=None, confirm_gate=None):
        self.cfg = cfg
        self.on_event = on_event
        self.clock = clock or (lambda: time.monotonic() * 1000.0)
        self.eot_fn = eot_fn
        self.select_tools_fn = select_tools_fn
        self.decide_fn = decide_fn
        self.warm_fn = warm_fn
        # 对话式二次确认编排器（默认 mock：不连真实 KEV）。TurnSession 只在第一轮决策
        # 命中池内高危工具时启用它，叠加在主循环上，不改变既有分段/消费机制。
        self.confirm_gate = confirm_gate if confirm_gate is not None else self._default_confirm_gate()
        self.session_id = session_id or ("s-" + str(int(time.time() * 1000)))
        self.evidence_root = Path(evidence_root) if evidence_root else None
        self._pool = ThreadPoolExecutor(max_workers=4 + max(0, max(cfg.eot_max_inflight, cfg.tools_max_inflight) - 1))
        self.context = {"home_address_configured": True, "company_address_configured": True, "synthetic": True}
        self.events = []
        self.turns = []
        self.state = "LISTENING"
        self.turn_start_ms = None
        self.segment = ""
        self.segments = []
        self.version = 0
        self.last_change_ms = None
        self.last_speech_end_ms = None
        self.speech_start_ms = None
        self.t_last_word = None
        self.t_confirmed = None
        self.t_llm_start = None
        self.t_decision = None
        self.t_decision_last = None
        # Consumption cursor: segments[:consumed_segments] were sealed by a landed decision, so the
        # unconsumed suffix is segments[consumed_segments:] + the in-use segment (see suffix_text()).
        self.consumed_version = 0
        self.consumed_segments = 0
        # Dedup basis: the *whole* consumed text, not just the last sealed segment. Volcengine
        # re-sends a whole utterance, so once several segments were consumed the re-send prefixes
        # the full consumed text while `consumed_tail` (kept for compatibility/metrics) does not.
        self.consumed_text = ""
        self.consumed_tail = None
        self.consumptions = []
        self.consumed_count = 0
        self.version_t_ms = None
        # A landed decision is "work just done", not silence: the end-of-turn silence window restarts
        # when a segment is consumed (otherwise a decision slower than max_silence_ms would close the
        # turn the moment it lands and the user's next segment could never arrive).
        self.t_last_consumed = None
        self._stop_requested = False
        self.timeline_path = None
        self.turn_done = False
        self.turn_index = 1
        self.segment_closed = False
        self._jobs = {"llm": None}
        self._eot_jobs = []
        self._tools_jobs = []           # M3: concurrent shortlist calls (was a single-flight slot)
        self._eot_pending = None
        self._tools_pending = None
        self._tools_cache = None
        self._tools_result = None
        self._llm_prepared = None
        self.t_tools_ready = None
        self.t_first_partial = None
        self.t_llm_start_last = None
        self.warm_ms = None
        self._warmed = False
        self._warm_future = None
        self._shortlist_source = None
        self._shortlist_age_ms = None
        self.counters = {"eot_calls": 0, "eot_skipped": 0, "eot_stale": 0, "tool_calls": 0,
                         "llm_calls": 0, "llm_wasted": 0, "n_reissue": 0, "consumed": 0}
        self.eot_scores = None
        self.decision = None
        # 对话式二次确认：挂起的第一轮决策 + 解析结果（CONFIRM_WAIT 期间保存）
        self.confirm_pending = None
        self.confirm_resolved = None

    @staticmethod
    def _default_confirm_gate():
        """惰性加载 TurnConfirmGate，绕开 turn_session <-> confirm_gate 的循环 import。"""
        from confirm_orchestrator import TurnConfirmGate
        return TurnConfirmGate()

    # ---------- public API ----------

    def on_asr(self, t_ms: int, text: str, final: bool) -> None:
        if self.turn_done:
            if normalize(text) == normalize(self.turn_text()):
                # A repeated/duplicate final update after the decision is not a barge-in.
                self.counters["eot_skipped"] += 1
                self._emit("asr_update", t_ms, text=text, final=bool(final), segment=self.segment,
                           turn_text=self.turn_text(), changed=False)
                self._emit("eot_skipped", t_ms, reason="unchanged_after_turn_done", text=text)
                return
            self._emit("barge_in", t_ms, why="asr_after_action", text=text)
            self._reset_turn(t_ms)
        # Volcengine re-sends the whole sentence of an utterance: after one or more segments were
        # consumed, an exact re-send is a duplicate and a re-send that continues the consumed text
        # only contributes the tail. The judgement uses the *full* consumed text — with several
        # consumptions the last sealed segment alone no longer prefixes the re-send (spec 问题 1).
        if self.consumed_text:
            norm_text = normalize(text)
            if norm_text and norm_text.startswith(self.consumed_text):
                if norm_text == self.consumed_text:
                    self.counters["eot_skipped"] += 1
                    self._emit("asr_update", t_ms, text=text, final=bool(final), segment=self.segment,
                               turn_text=self.turn_text(), changed=False)
                    self._emit("eot_skipped", t_ms, reason="consumed_duplicate", text=text)
                    return
                text = self._tail_after_consumed(text)
        if self.turn_start_ms is None:
            self.turn_start_ms = t_ms
        if self.segment_closed:
            self.segments.append(self.segment)
            self.segment = ""
            self.segment_closed = False
        before = normalize(self.suffix_text())
        self.segment = text
        changed = normalize(self.suffix_text()) != before
        if changed:
            self.version += 1
            self.version_t_ms = t_ms
        self._emit("asr_update", t_ms, text=text, final=bool(final), segment=self.segment,
                   turn_text=self.turn_text(), changed=changed)
        if not changed:
            # No new text: keep the current state and any in-flight judgement for the same version.
            self.counters["eot_skipped"] += 1
            self._emit("eot_skipped", t_ms, reason="unchanged", text=text)
            return
        if self.state not in ("LISTENING", "CONFIRM_WAIT"):
            self._to_listening(t_ms, "new_text")
        self.last_change_ms = t_ms
        self.t_last_word = t_ms
        if self.t_first_partial is None:
            self.t_first_partial = t_ms
            if self.cfg.warm_llm and self.warm_fn is not None and not self._warmed:
                self._warmed = True
                self._warm_future = self._pool.submit(self.warm_fn)
        self._cancel_inflight_llm(t_ms, "new_text")
        # EOT keeps the debounce (it must not fire per keystroke); tools fire immediately and the newest
        # text wins the single pending slot, so the shortlist starts one round earlier than before.
        # Both now look at the unconsumed suffix only.
        self._eot_pending = {"version": self.version, "text": self.eot_input(), "due_ms": t_ms + self.cfg.debounce_ms}
        self._tools_pending = {"version": self.version, "text": self.suffix_text()}
        self.eot_scores = None
        self.tick(t_ms)

    def _tail_after_consumed(self, text: str) -> str:
        """Raw tail left after the consumed prefix, aligned on normalized characters.

        The re-send need not match byte-for-byte (Volcengine re-punctuates), so walk `text` counting
        only characters that survive `normalize()` until the consumed text's length is reached.
        """
        seen = 0
        for index, char in enumerate(text):
            if _PUNCT.fullmatch(char):
                continue
            seen += 1
            if seen == len(self.consumed_text):
                return text[index + 1:]
        return text

    def on_vad(self, t_ms: int, kind: str) -> None:
        self._emit("vad", t_ms, kind=kind)
        if kind == "speech_start":
            if self.turn_done:
                self._reset_turn(t_ms)
            elif self.state not in ("LISTENING", "CONFIRM_WAIT"):
                self._to_listening(t_ms, "speech_start")
            if self.segment:
                self.segment_closed = True
            # The user is speaking again: the previous silence interval is over.
            self.speech_start_ms = t_ms
            self._cancel_inflight_llm(t_ms, "speech_start")
        elif kind == "speech_end":
            self.last_speech_end_ms = t_ms
        self.tick(t_ms)

    def tick(self, now_ms: int) -> None:
        if self.turn_start_ms is None:
            return
        self._poll(now_ms)
        silence = self._silence_ms(now_ms)
        if self.state == "THINK_WAIT":
            # A thinking pause is allowed to outlast max_silence_ms; only think_wait_ms confirms it.
            if silence >= self.cfg.think_wait_ms:
                self._confirm(now_ms, "think_wait")
        elif self.state == "MAYBE_DONE":
            if not normalize(self.suffix_text()):
                # Nothing left to confirm: close the turn like the LISTENING empty-suffix branch
                # instead of waiting for a _confirm() that can never act (spec 问题 2.2). This is the
                # safety net for paths this fix does not change (e.g. a timed-out EOT still reporting).
                if silence >= self.cfg.confirm_silence_ms:
                    self._maybe_finish_session(now_ms)
            elif silence >= self.cfg.confirm_silence_ms:
                self._confirm(now_ms, "confirm_silence")
            elif silence >= self.cfg.max_silence_ms:
                self._confirm(now_ms, "max_silence")
        elif self.state == "CUT_OFF_WAIT":
            if silence >= self.cfg.cut_wait_ms:
                self._confirm(now_ms, "cut_wait")
            elif silence >= self.cfg.max_silence_ms:
                self._confirm(now_ms, "max_silence")
        elif self.state == "CONFIRM_WAIT":
            # 确认等待：只会在第二轮回复判停收敛后被 _resolve_confirm 移出。
            # 若超时（默认沿用 think_wait_ms 作为确认等待窗）都没有二轮回，安全优先放弃，
            # 绝不静默执行（spec §5.3 超时默认不执行）。
            if self.confirm_pending is not None and silence >= self.cfg.think_wait_ms:
                self._cancel_inflight_llm(now_ms, "confirm_timeout")
                self._emit("confirm_resolve", now_ms, user_reply="", intent="timeout",
                           action="abandon", message="确认超时，安全优先放弃，不执行",
                           pending_tool=self.confirm_pending.get("tool"))
                self.confirm_pending = None
                self._to_state("LISTENING", now_ms, "confirm_timeout")
        elif self.state == "LISTENING" and silence >= self.cfg.max_silence_ms:
            if normalize(self.suffix_text()):
                self._confirm(now_ms, "max_silence")
            else:
                # Nothing left to consume and the user has been quiet: close the whole turn (once).
                self._maybe_finish_session(now_ms)
        for job in self._eot_jobs:
            if not job.get("timed_out") and now_ms - job["started_ms"] > self.cfg.eot_timeout_ms:
                # Design doc: a KEV failure/timeout falls back to the max_silence rule below; the late
                # result is still scored and reported, it just no longer gates the state machine.
                job["timed_out"] = True
                self.counters["eot_skipped"] += 1
                self._emit("eot_skipped", now_ms, reason="timeout", text=job["text"])
        while len(self._eot_jobs) < max(1, self.cfg.eot_max_inflight) and self._eot_pending \
                and now_ms >= self._eot_pending["due_ms"]:
            pending, self._eot_pending = self._eot_pending, None
            self._fire_eot(now_ms, pending)
        while len(self._tools_jobs) < max(1, self.cfg.tools_max_inflight) and self._tools_pending:
            self._fire_tools(now_ms)
        if self._warm_future is not None and self._warm_future.done() and self.warm_ms is None:
            try:
                self.warm_ms = (self._warm_future.result() or {}).get("warm_ms")
            except Exception:
                self.warm_ms = None
            self._warm_future = None
        if self.state == "CONFIRMED":
            self._maybe_start_llm(now_ms)

    def wait_until(self, now_ms: int) -> None:
        self.tick(now_ms)

    def idle(self) -> bool:
        return (not any(self._jobs.values()) and not self._eot_jobs and not self._tools_jobs
                and not self._eot_pending and not self._tools_pending)

    def summary(self) -> dict:
        return {"session_id": self.session_id, "turns": self.turn_index,
                "t_last_word": self.t_last_word, "t_confirmed": self.t_confirmed,
                "t_llm_start": self.t_llm_start, "t_decision": self.t_decision,
                # t_* keep their "first time in this turn" meaning; t_last_* are the latest occurrence
                # (one turn can now hold several decisions).
                "t_last_llm_start": self.t_llm_start_last, "t_last_decision": self.t_decision_last,
                "t_confirm_latency_ms": self._delta(self.t_confirmed, self.t_last_word),
                "t_llm_start_latency_ms": self._delta(self.t_llm_start, self.t_last_word),
                "t_decision_latency_ms": self._delta(self.t_decision, self.t_last_word),
                # M0 instrumentation: where the "last word → LLM request" time actually went.
                "t_first_partial": self.t_first_partial,
                "t_tools_ready": self.t_tools_ready,
                "t_first_partial_to_last_word_ms": self._delta(self.t_last_word, self.t_first_partial),
                "shortlist_source": self._shortlist_source,
                "shortlist_age_ms": self._shortlist_age_ms,
                "warm_ms": self.warm_ms,
                "consumed_count": self.consumed_count, "consumptions": self.consumptions,
                "t_last_consumed": self.t_last_consumed,
                **self.counters}

    # ---------- text ----------

    def turn_text(self) -> str:
        return self._join(self.segments + ([self.segment] if self.segment else []))

    def suffix_segments(self):
        """Sealed-but-not-yet-consumed segments (the consumed ones can no longer change)."""
        return self.segments[self.consumed_segments:]

    def suffix_text(self) -> str:
        """The unconsumed suffix: everything the next decision is allowed to see."""
        return self._join(self.suffix_segments() + ([self.segment] if self.segment else []))

    def eot_input(self) -> str:
        if self.cfg.eot_input == "turn_text":
            return self.suffix_text()
        if len(normalize(self.segment)) >= 4 or not self.suffix_segments():
            return self.segment
        return self._join(self.suffix_segments()[-1:] + [self.segment])

    @staticmethod
    def _join(parts):
        text = ""
        for part in parts:
            if not part:
                continue
            if text and text[-1].isascii() and part[0].isascii():
                text += " "
            text += part
        return text

    # ---------- workers ----------

    def _fire_eot(self, now_ms, pending):
        self.counters["eot_calls"] += 1
        self._emit("eot_start", now_ms, text=pending["text"], version=pending["version"])
        self._eot_jobs.append({"future": self._pool.submit(self.eot_fn, pending["text"]),
                               "version": pending["version"], "started_ms": now_ms, "text": pending["text"]})

    def _fire_tools(self, now_ms):
        pending, self._tools_pending = self._tools_pending, None
        self.counters["tool_calls"] += 1
        self._emit("tools_start", now_ms, text=pending["text"], version=pending["version"])
        self._tools_jobs.append(
            {"future": self._pool.submit(self.select_tools_fn, pending["text"], self.cfg.tool_set, self.context),
             "version": pending["version"], "started_ms": now_ms, "text": pending["text"]})

    def _start_llm(self, now_ms, speculative):
        self.counters["llm_calls"] += 1
        # 第一次发出才是"启动"时刻；短名单变化后的重发不覆盖它（否则指标被推后，看起来更慢）
        if self.t_llm_start is None:
            self.t_llm_start = now_ms
        self.t_llm_start_last = now_ms
        result = self._tools_result or {}
        cache = self._tools_cache or {}
        self._shortlist_source = "fresh" if self._tools_ready() else "stale"
        self._shortlist_age_ms = 0 if self._tools_ready() else now_ms - cache.get("ready_ms", now_ms)
        # The decision only ever sees the unconsumed suffix (no cross-decision context in this PoC).
        utterance = self.suffix_text()
        self._emit("llm_start", now_ms, speculative=bool(speculative), selected=result.get("selected", []),
                   shortlist_source=self._shortlist_source, shortlist_age_ms=self._shortlist_age_ms,
                   utterance=utterance, consumed_segments=self.consumed_segments)
        self._jobs["llm"] = {"shortlist_norm": cache.get("norm"),
                             "shortlist_selected": tuple(result.get("selected") or []),
                             "future": self._pool.submit(
            self.decide_fn, utterance, result.get("selected", []), result.get("schemas", []),
            self.cfg.tool_set, self.context, native_schemas=result.get("native_schemas"),
            available_domains=result.get("domains")),
            "version": self.version, "utterance": utterance,
            "started_ms": now_ms, "speculative": bool(speculative), "canceled": False}

    def _poll(self, now_ms):
        for job in [job for job in self._eot_jobs if job["future"].done()]:
            self._eot_jobs.remove(job)
            try:
                result = job["future"].result()
            except Exception as error:  # PoC: surface, do not retry
                self._emit("error", now_ms, stage="eot", error_type=type(error).__name__, error=str(error))
                continue
            self._on_eot_result(now_ms, job, result)
        for job in [job for job in self._tools_jobs if job["future"].done()]:
            self._tools_jobs.remove(job)
            try:
                result = job["future"].result()
            except Exception as error:  # PoC: surface, do not retry
                self._emit("error", now_ms, stage="tools", error_type=type(error).__name__, error=str(error))
                continue
            self._on_tools_result(now_ms, job, result)
        job = self._jobs["llm"]
        if job is not None and job["future"].done():
            self._jobs["llm"] = None
            try:
                result = job["future"].result()
            except Exception as error:  # PoC: surface, do not retry
                self._emit("error", now_ms, stage="llm", error_type=type(error).__name__, error=str(error))
            else:
                self._on_llm_result(now_ms, job, result)

    def _on_eot_result(self, now_ms, job, result):
        stale = job["version"] != self.version
        if stale:
            # The judgement was paid for but no longer describes the newest text: report it (metrics
            # need every prefix score) and drop it from the state machine.
            self.counters["eot_skipped"] += 1
            self.counters["eot_stale"] += 1
        self.eot_scores = result if not stale else self.eot_scores
        self._emit("eot_scores", now_ms, stale=stale, text=job["text"], **result)
        if stale:
            self._emit("eot_skipped", now_ms, reason="stale", text=job["text"])
            return
        if not normalize(self.suffix_text()):
            # Everything this judgement could speak about is already consumed: keep the score and
            # emit, but never drive the state machine. A late `complete` used to park the turn in
            # MAYBE_DONE with an empty suffix, where _confirm() can never act (spec 问题 2.1).
            self.counters["eot_skipped"] += 1
            self._emit("eot_skipped", now_ms, reason="empty_suffix", text=job["text"],
                       label=result["label"])
            return
        if self.state == "CONFIRMED":
            return
        label = result["label"]
        margin = result["margin"]
        # ---- 对话式二次确认：第二轮回（CONFIRM_WAIT 内）判停收敛后进意图判定 ----
        if self.state == "CONFIRM_WAIT":
            # 只在判"说完"(complete 且 margin 达标)时把当前未消费后缀当完整第二轮回；
            # 没说完(cut/wait)就继续听（spec §3.2：先判说完，再说确认意图）。
            if label == "complete" and not (self.cfg.eot_policy == "margin"
                                            and margin < self.cfg.eot_margin):
                self._resolve_confirm(now_ms, self.suffix_text())
            else:
                self._to_state("CUT_OFF_WAIT" if label == "cut" else "THINK_WAIT",
                               now_ms, "confirm_wait_eot_" + label)
            return
        if label == "complete":
            if self.cfg.eot_policy == "margin" and margin < self.cfg.eot_margin:
                label = "cut" if result["cut"] >= result["wait"] else "wait"
            else:
                self._to_state("MAYBE_DONE", now_ms, "eot_complete")
                if self.cfg.speculative_llm and margin >= self.cfg.spec_margin:
                    self._maybe_start_llm(now_ms, speculative=True)
                return
        self._to_state("CUT_OFF_WAIT" if label == "cut" else "THINK_WAIT", now_ms, "eot_" + label)

    def _on_tools_result(self, now_ms, job, result):
        newest = self._tools_cache is None or job["version"] >= self._tools_cache["version"]
        if newest:
            self._tools_cache = {"norm": normalize(job["text"]), "text": job["text"],
                                 "version": job["version"], "ready_ms": now_ms}
            self._tools_result = result
            self.t_tools_ready = now_ms
        scores = result.get("scores") or {}
        top = [{"name": name.replace("tool:", ""), "score": value} for name, value in
               sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:3]]
        self._emit("tools_scores", now_ms, reused=False, text=job["text"], version=job["version"],
                   newest=newest, selected=result.get("selected", []), timing=result.get("timing"), top=top)
        if self.cfg.reuse_tools_if_text_unchanged and not self._tools_ready():
            self._tools_pending = {"version": self.version, "text": self.suffix_text()}
        # M4: an in-flight decision that used an older shortlist is reissued once the new one lands.
        llm_job = self._jobs["llm"]
        # 只有"入选的 top-k 真的变了"才重发：文本 lengthen 了但短名单没变时重发纯属浪费。
        changed = set(result.get("selected") or []) != set(llm_job.get("shortlist_selected") or []) if llm_job else False
        if (newest and self.cfg.shortlist_reissue and changed and llm_job is not None and not llm_job["canceled"]
                and llm_job["version"] == self.version and self.state in ("MAYBE_DONE", "CONFIRMED")):
            self._cancel_inflight_llm(now_ms, "shortlist_changed")
            self.counters["n_reissue"] += 1
        self._maybe_start_llm(now_ms, speculative=self.cfg.speculative_llm and self.state == "MAYBE_DONE")

    def _on_llm_result(self, now_ms, job, result):
        if job["canceled"]:
            return
        if job["version"] != self.version:
            self.counters["llm_wasted"] += 1
            self._emit("llm_cancel", now_ms, reason="stale_text")
            return
        if self.state != "CONFIRMED":
            # Speculative result arrived early; keep it until the turn is confirmed.
            self._llm_prepared = {"version": job["version"], "result": result,
                                  "speculative": bool(job.get("speculative")), "utterance": job.get("utterance")}
            return
        self._apply_llm(now_ms, result, job)

    def _apply_llm(self, now_ms, result, job):
        """A decision landed: consume the suffix it was made on and keep listening for more."""
        self.decision = result["decision"]
        self.t_decision = now_ms if self.t_decision is None else self.t_decision
        self.t_decision_last = now_ms
        # job["version"] == self.version was checked before this call, so the text below is byte-for-byte
        # what decide() received: sealing it cannot truncate or shift the segment.
        consumed_text = self.suffix_text()
        self._seal_consumed()
        self.consumed_version = job["version"]
        record = {"index": len(self.consumptions) + 1, "version": job["version"],
                  "segments": self.consumed_segments, "text": consumed_text,
                  "t_ms": now_ms, "asr_t_ms": self.version_t_ms,
                  "speculative": bool(job.get("speculative")), "llm_ms": result.get("llm_ms"),
                  "decision_kind": result["decision"].get("kind"),
                  "shortlist_source": self._shortlist_source}
        self.consumptions.append(record)
        self.consumed_count = len(self.consumptions)
        self.counters["consumed"] = self.consumed_count
        self.t_last_consumed = now_ms
        self._emit("llm_decision", now_ms, decision=result["decision"], llm_ms=result.get("llm_ms"),
                   utterance=consumed_text, consumed_index=record["index"],
                   consumed_version=job["version"], consumed_segments=self.consumed_segments,
                   decision_head=(result.get("log_fields") or {}).get("decision_head"))
        # `record["t_ms"]` is the same instant _emit stamps, so it is not passed twice.
        self._emit("segment_consumed", now_ms, **{key: value for key, value in record.items() if key != "t_ms"})
        # 对话式二次确认：第一轮决策命中池内高危工具 -> 挂起该决策进 CONFIRM_WAIT，等用户二轮回。
        # 不立即消费为普通执行；EOT 判停照常跑（用于判断二轮回"说完没"），但不再 advance decide()。
        if self.confirm_gate.ready(result["decision"]):
            self._begin_confirm(now_ms, result["decision"])
            if self._stop_requested:
                self._maybe_finish_session(now_ms, force=True)
            return
        self._to_state("LISTENING", now_ms, "consumed")
        if self._stop_requested:
            # Explicit stop(): consume what was left, then close the turn.
            self._maybe_finish_session(now_ms, force=True)

    def _begin_confirm(self, now_ms, decision):
        """第一轮决策命中待确认池：挂起决策、发出确认请求、进入 CONFIRM_WAIT。

        EOT 判停/选工具照常跑（为二轮回做准备、判断是否说完），但 decide() 被 gating：
        因为 `_maybe_start_llm` 只在 CONFIRMED/MAYBE_DONE 下 advance，CONFIRM_WAIT 不在其中，
        所以二轮回期间 agent 不会自作主张重复决策。
        """
        self.confirm_pending = dict(decision or {})
        self.confirm_resolved = None
        ask = self.confirm_gate.ask_for(decision)
        self._emit("confirm_request", now_ms, tool=decision.get("tool"),
                   arguments=decision.get("arguments"), ask=ask, **self.summary())
        self._to_state("CONFIRM_WAIT", now_ms, "pooled_tool")
        # 清掉上一轮的 LLM/工具中间态，避免挂起后误用
        self._cancel_inflight_llm(now_ms, "confirm_wait")

    def _resolve_confirm(self, now_ms, user_reply):
        """第二轮用户回复（已由 EOT 判停判为"说完"）收敛 -> 判意图 -> 落四分支。

        结束后 segment 已在 _seal_consumed 消费，回 LISTENING；takeover 分支把 user_reply
        当新诉求再走 select_tools/decide 一轮（若再命中池内工具会再进 CONFIRM_WAIT）。
        """
        if self.confirm_pending is None:
            self._to_state("LISTENING", now_ms, "no_pending")
            return
        pending = self.confirm_pending
        resolved = self.confirm_gate.resolve(user_reply, pending)
        self.confirm_resolved = resolved
        # 第二轮回已被意图判定"消费"：封口该 segment，避免随后被 Listener 当新指令再决策。
        self._consume_suffix(now_ms, "confirm_reply")
        self._emit("confirm_scores", now_ms, **resolved.get("scores", {}), user_reply=user_reply,
                   confirm_ask=resolved.get("confirm_ask"), pending_tool=resolved.get("pending_tool"))
        intent = resolved["intent"]
        self._emit("confirm_resolve", now_ms, user_reply=user_reply, intent=intent,
                   action=resolved["action"], message=resolved["message"],
                   pending_tool=resolved.get("pending_tool"), scores=resolved.get("scores"),
                   confirm_ask=resolved.get("confirm_ask"))
        # 先落地"放弃当前待确认动作"的底（except agree 会执行它）
        self.confirm_pending = None
        if intent == "takeover":
            # 转新诉求：用用户回复重跑选工具+决策（注入链），不耗新 segment
            self._emit("confirm_reroute", now_ms, request=user_reply,
                       message=f"把第 {self.turn_index} 轮的新诉求交给 decide 处理")
            self._run_takeover(now_ms, user_reply)
            return
        if intent == "agree":
            # 已确认：记录 `executed`(mock)。不真实触发业务工具；可在 UI 端展示 mock 执行。
            self._emit("confirm_exec", now_ms, tool=resolved.get("pending_tool"),
                       mock_only=True, message="用户已确认，mock 执行 " + str(resolved.get("pending_tool")))
        self._to_state("LISTENING", now_ms, "confirm_" + intent)

    def _run_takeover(self, now_ms, request):
        """takeover：把第二轮回文本当新诉求，走注入的 select_tools + decide。

        供 mock/真实注入链复用的同一段异步落链：捞出短名单后交给 decide_fn，再把结果
        以 `reroute_decision` 事件发出；若新决策也命中池内工具会再次进入 CONFIRM_WAIT。
        """
        context = self.context
        try:
            selection = self.select_tools_fn(request, self.cfg.tool_set, context)
            self._emit("reroute_tools", now_ms, request=request,
                       selected=selection.get("selected", []))
            outcome = self.decide_fn(request, selection.get("selected", []),
                                     selection.get("schemas", []), self.cfg.tool_set, context,
                                     native_schemas=selection.get("native_schemas"),
                                     available_domains=selection.get("domains"))
            decision = outcome["decision"]
        except Exception as error:  # PoC: 接管失败不执行旧动作即可, 报出但保持安全
            self._emit("error", now_ms, stage="takeover", error_type=type(error).__name__,
                       error=str(error))
            self._to_state("LISTENING", now_ms, "takeover_error")
            return
        self._emit("reroute_decision", now_ms, request=request, decision=decision,
                   message="接管：转去处理新诉求 " + str(request))
        if self.confirm_gate.ready(decision):
            # 新诉求又命中池内高危工具 -> 再进 CONFIRM_WAIT（可再确认一次）
            self._begin_confirm(now_ms, decision)
        else:
            self._emit("confirm_exec", now_ms, tool=decision.get("tool"), request=request,
                       mock_only=True, message="接管后直接执行(池外) " + str(decision.get("tool")))
            self._to_state("LISTENING", now_ms, "reroute_done")

    def _seal_consumed(self):
        """Seal the in-use segment into `segments` so later partials can no longer rewrite it."""
        if self.segment:
            self.segments.append(self.segment)
            self.segment = ""
        self.segment_closed = False
        self.consumed_segments = len(self.segments)
        self.consumed_text = normalize(self._join(self.segments[:self.consumed_segments]))
        # Kept for compatibility (metrics, older tests); dedup is judged on `consumed_text`.
        self.consumed_tail = self.segments[-1] if self.segments else None

    def _consume_suffix(self, now_ms, why):
        """把当前未消费后缀当作"已处理完的消费"（确认轮第二轮回用）：封口 + 记账 + 发事件。

        复用 _seal_consumed 的封口逻辑，避免该 segment 被当作新一轮待决策文本。
        """
        consumed_text = self.suffix_text()
        self._seal_consumed()
        self.consumed_version = self.version
        self.t_last_consumed = now_ms
        self.consumptable_count = self.consumed_count = self.consumed_count + 1
        self.counters["consumed"] = self.consumed_count
        self._emit("confirm_consumed", now_ms, index=self.consumed_count, text=consumed_text,
                   why=why, consumed_segments=self.consumed_segments)

    # ---------- state ----------

    def _silence_ms(self, now_ms):
        anchors = [value for value in (self.last_change_ms, self.last_speech_end_ms,
                                       self.speech_start_ms, self.turn_start_ms,
                                       self.t_last_consumed) if value is not None]
        return now_ms - max(anchors) if anchors else 0

    def _tools_ready(self):
        return self._tools_cache is not None and self._tools_cache["norm"] == normalize(self.suffix_text())

    def _maybe_start_llm(self, now_ms, speculative=False):
        if self.turn_done or self.state not in ("CONFIRMED", "MAYBE_DONE"):
            return
        if not normalize(self.suffix_text()):
            return
        if self.state == "CONFIRMED" and self._llm_prepared and self._llm_prepared["version"] == self.version:
            prepared, self._llm_prepared = self._llm_prepared, None
            job = {"version": prepared["version"], "speculative": prepared.get("speculative", False),
                   "utterance": prepared.get("utterance"), "canceled": False}
            self._apply_llm(now_ms, prepared["result"], job)
            return
        job = self._jobs["llm"]
        if job is not None and not job["canceled"] and job["version"] == self.version:
            return
        if not self._tools_ready():
            # M4: a shortlist from this turn is good enough to start with (it is reissued if it changes);
            # only a turn with no shortlist at all has to wait.
            usable = self.cfg.allow_stale_shortlist and self._tools_result is not None and self._tools_cache is not None
            if not usable:
                if self.state == "CONFIRMED" and not self._tools_jobs and self._tools_pending is None:
                    self._tools_pending = {"version": self.version, "text": self.suffix_text()}
                return
        if self.state == "MAYBE_DONE" and self._llm_prepared and self._llm_prepared["version"] == self.version:
            return
        if self.state == "MAYBE_DONE" and not speculative:
            return
        self._start_llm(now_ms, speculative)

    def _cancel_inflight_llm(self, now_ms, reason):
        """Drop a decision that the user's continuation invalidated (in flight or already finished)."""
        prepared, self._llm_prepared = self._llm_prepared, None
        if prepared is not None:
            # A speculative result that already finished is discarded too, and counted as waste.
            self.counters["llm_wasted"] += 1
            self._emit("llm_cancel", now_ms, reason=reason, speculative=True, prepared=True)
        job = self._jobs["llm"]
        if job is None or job["canceled"]:
            return
        job["canceled"] = True
        self.counters["llm_wasted"] += 1
        self._emit("llm_cancel", now_ms, reason=reason, speculative=job["speculative"], prepared=False)

    def _to_state(self, state, now_ms, why):
        if state == self.state:
            return
        previous, self.state = self.state, state
        self._emit("state_change", now_ms, **{"from": previous, "to": state, "why": why})

    def _to_listening(self, now_ms, why):
        self._to_state("LISTENING", now_ms, why)

    def _confirm(self, now_ms, why):
        if self.state == "CONFIRMED":
            return
        if not normalize(self.suffix_text()):
            # Nothing unconsumed to decide on: never ask the LLM about an empty utterance.
            return
        self.t_confirmed = now_ms if self.t_confirmed is None else self.t_confirmed
        self._to_state("CONFIRMED", now_ms, why)
        self._maybe_start_llm(now_ms)

    def stop(self, now_ms):
        """Explicit stop: decide on whatever suffix is left, then close the turn."""
        if self.turn_done:
            return
        self._stop_requested = True
        self._emit("stop_requested", now_ms, text=self.suffix_text(), consumed_segments=self.consumed_segments)
        if normalize(self.suffix_text()):
            self._confirm(now_ms, "stop")
        self._maybe_finish_session(now_ms, force=True)

    def _maybe_finish_session(self, now_ms, force=False):
        """Close the whole turn once there is nothing left to consume and no decision is in flight."""
        if self.turn_done:
            return False
        if normalize(self.suffix_text()):
            return False
        if not force and not self.consumed_count:
            # The user has not consumed anything yet: keep the turn open and wait for speech.
            return False
        job = self._jobs["llm"]
        if job is not None and not job["canceled"]:
            return False
        if self._llm_prepared is not None and self._llm_prepared["version"] == self.version:
            return False
        self._finish_turn(now_ms)
        return True

    def _finish_turn(self, now_ms):
        self.turn_done = True
        summary = self.summary()
        self.turns.append(summary)
        path = self._timeline_path()
        self._emit("turn_done", now_ms, **summary, timeline_path=str(path) if path else None)
        self._dump_timeline()

    def _timeline_path(self):
        return Path(self.evidence_root) / self.session_id / "timeline.json" if self.evidence_root else None

    def _reset_turn(self, now_ms):
        self.turn_index += 1
        self.turn_done = False
        self.segment = ""
        self.segments = []
        self.segment_closed = False
        self.last_change_ms = None
        self.last_speech_end_ms = None
        self.speech_start_ms = now_ms
        self.t_last_word = None
        self.t_confirmed = None
        self.t_llm_start = None
        self.t_decision = None
        self.t_decision_last = None
        self.consumed_version = 0
        self.consumed_segments = 0
        self.consumed_text = ""
        self.consumed_tail = None
        self.consumptions = []
        self.consumed_count = 0
        self.version_t_ms = None
        self.t_last_consumed = None
        self._stop_requested = False
        self.turn_start_ms = now_ms
        self.state = "LISTENING"
        self._jobs["llm"] = None
        self._tools_jobs = []
        self._eot_jobs = []
        self._eot_pending = None
        self._tools_pending = None
        self._tools_cache = None
        self._tools_result = None
        self._llm_prepared = None
        self.t_tools_ready = None
        self.t_llm_start_last = None
        self._shortlist_source = None
        self._shortlist_age_ms = None
        self.eot_scores = None
        self.decision = None
        self.confirm_pending = None
        self.confirm_resolved = None
        self.counters = {"eot_calls": 0, "eot_skipped": 0, "eot_stale": 0, "tool_calls": 0,
                         "llm_calls": 0, "llm_wasted": 0, "n_reissue": 0, "consumed": 0}

    # ---------- events / evidence ----------

    def _emit(self, name, t_ms, **payload):
        event = {"event": name, "t_ms": t_ms, "text_version": self.version, **payload}
        self.events.append(event)
        if self.on_event:
            self.on_event(event)

    @staticmethod
    def _delta(later, earlier):
        return None if later is None or earlier is None else later - earlier

    def _dump_timeline(self):
        if self.evidence_root is None:
            return
        folder = Path(self.evidence_root) / self.session_id
        folder.mkdir(parents=True, exist_ok=True)
        payload = {"session_id": self.session_id, "config": asdict(self.cfg),
                   "events": self.events, "turns": self.turns, "summary": self.summary()}
        (folder / "timeline.json").write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
        self.timeline_path = folder / "timeline.json"
        return self.timeline_path

    def close(self):
        self._pool.shutdown(wait=False)