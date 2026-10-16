"""Streaming turn session: ASR updates + VAD → KEV EOT judging + tool shortlist → one decision.

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

from core_pipeline_run import POC_ROOT, decide, select_tools
from turn_eot import score_eot

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
    tool_set: str = "navigation"        # navigation|multidomain

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
                 evidence_root=EVIDENCE_ROOT, warm_fn=None):
        self.cfg = cfg
        self.on_event = on_event
        self.clock = clock or (lambda: time.monotonic() * 1000.0)
        self.eot_fn = eot_fn
        self.select_tools_fn = select_tools_fn
        self.decide_fn = decide_fn
        self.warm_fn = warm_fn
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
                         "llm_calls": 0, "llm_wasted": 0, "n_reissue": 0}
        self.eot_scores = None
        self.decision = None

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
        if self.turn_start_ms is None:
            self.turn_start_ms = t_ms
        if self.segment_closed:
            self.segments.append(self.segment)
            self.segment = ""
            self.segment_closed = False
        before = normalize(self.turn_text())
        self.segment = text
        changed = normalize(self.turn_text()) != before
        if changed:
            self.version += 1
        self._emit("asr_update", t_ms, text=text, final=bool(final), segment=self.segment,
                   turn_text=self.turn_text(), changed=changed)
        if not changed:
            # No new text: keep the current state and any in-flight judgement for the same version.
            self.counters["eot_skipped"] += 1
            self._emit("eot_skipped", t_ms, reason="unchanged", text=text)
            return
        if self.state != "LISTENING":
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
        self._eot_pending = {"version": self.version, "text": self.eot_input(), "due_ms": t_ms + self.cfg.debounce_ms}
        self._tools_pending = {"version": self.version, "text": self.turn_text()}
        self.eot_scores = None
        self.tick(t_ms)

    def on_vad(self, t_ms: int, kind: str) -> None:
        self._emit("vad", t_ms, kind=kind)
        if kind == "speech_start":
            if self.turn_done:
                self._reset_turn(t_ms)
            elif self.state != "LISTENING":
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
            if silence >= self.cfg.confirm_silence_ms:
                self._confirm(now_ms, "confirm_silence")
            elif silence >= self.cfg.max_silence_ms:
                self._confirm(now_ms, "max_silence")
        elif self.state == "CUT_OFF_WAIT":
            if silence >= self.cfg.cut_wait_ms:
                self._confirm(now_ms, "cut_wait")
            elif silence >= self.cfg.max_silence_ms:
                self._confirm(now_ms, "max_silence")
        elif self.state == "LISTENING" and silence >= self.cfg.max_silence_ms:
            self._confirm(now_ms, "max_silence")
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
                **self.counters}

    # ---------- text ----------

    def turn_text(self) -> str:
        return self._join(self.segments + ([self.segment] if self.segment else []))

    def eot_input(self) -> str:
        if self.cfg.eot_input == "turn_text":
            return self.turn_text()
        if len(normalize(self.segment)) >= 4 or not self.segments:
            return self.segment
        return self._join(self.segments[-1:] + [self.segment])

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
        self._emit("llm_start", now_ms, speculative=bool(speculative), selected=result.get("selected", []),
                   shortlist_source=self._shortlist_source, shortlist_age_ms=self._shortlist_age_ms)
        self._jobs["llm"] = {"shortlist_norm": cache.get("norm"),
                             "shortlist_selected": tuple(result.get("selected") or []),
                             "future": self._pool.submit(
            self.decide_fn, self.turn_text(), result.get("selected", []), result.get("schemas", []),
            self.cfg.tool_set, self.context, native_schemas=result.get("native_schemas"),
            available_domains=result.get("domains")),
            "version": self.version, "started_ms": now_ms, "speculative": bool(speculative), "canceled": False}

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
        if self.state == "CONFIRMED":
            return
        label = result["label"]
        margin = result["margin"]
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
            self._tools_pending = {"version": self.version, "text": self.turn_text()}
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
            self._llm_prepared = {"version": job["version"], "result": result}
            return
        self._apply_llm(now_ms, result)

    def _apply_llm(self, now_ms, result):
        self.decision = result["decision"]
        self.t_decision = now_ms
        self._emit("llm_decision", now_ms, decision=result["decision"], llm_ms=result.get("llm_ms"),
                   decision_head=(result.get("log_fields") or {}).get("decision_head"))
        self._finish_turn(now_ms)

    # ---------- state ----------

    def _silence_ms(self, now_ms):
        anchors = [value for value in (self.last_change_ms, self.last_speech_end_ms,
                                       self.speech_start_ms, self.turn_start_ms) if value is not None]
        return now_ms - max(anchors) if anchors else 0

    def _tools_ready(self):
        return self._tools_cache is not None and self._tools_cache["norm"] == normalize(self.turn_text())

    def _maybe_start_llm(self, now_ms, speculative=False):
        if self.turn_done or self.state not in ("CONFIRMED", "MAYBE_DONE"):
            return
        if self.state == "CONFIRMED" and self._llm_prepared and self._llm_prepared["version"] == self.version:
            prepared, self._llm_prepared = self._llm_prepared, None
            self._apply_llm(now_ms, prepared["result"])
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
                    self._tools_pending = {"version": self.version, "text": self.turn_text()}
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
        self.t_confirmed = now_ms
        self._to_state("CONFIRMED", now_ms, why)
        self._maybe_start_llm(now_ms)

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
        self.counters = {"eot_calls": 0, "eot_skipped": 0, "eot_stale": 0, "tool_calls": 0,
                         "llm_calls": 0, "llm_wasted": 0, "n_reissue": 0}

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