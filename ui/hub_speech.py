"""内心戏用户页的「车说话」接线：把双席主链已经在用的中枢出声接到用户页上。

依据验收卡 `poc-neixinxi-user-ui-20261116-验收.md`：主画面中间大字是**车说的话**
（应声 / 垫句 / 冷场接话），判断全部收进「详情」；用户页不另写一套判断，也不做手写示例。

这里**不新写任何判断**：

- 「接不接 / 应不应一声 / 这一拍什么节奏 / 要不要垫一句 / 冷场开口做什么」五路判断仍由
  `gate/hub_*.py` 里现成的 `build_*_fn()` 提供，`GateKeeper` 仍是同一份编排（同一版字上并发问，
  结论按版本回填）；场景取的是本页同一份读心；
- 出不出声仍由 `gate/hub_ack.decide_ack` / `gate/hub_pace.decide_cushion` /
  `gate/hub_quiet.decide_quiet` 三道**唯一判据**决定（频率、字数、不要回答、积极 + 满 1.5 秒、
  静默闸、冷场刻度与退避，全部沿用它们的默认数字）；
- 说哪一句仍是 `ack_text` / `cushion_text` / `quiet_text` 那三份台词模板；
- 「同一时刻最多一句、出声后 1 秒内谁都不再出声」仍走 `gate/hub_arbiter.speech_blocked`。

本模块只做一件事：把主链 `TurnSession.tick()` 里的三段薄接线（垫句 → 应声 → 冷场）按同样的
顺序、同样的锚点接成一个小循环，并把这拍出的那一句交给页面。差别的来源只有一条：本页不建
`TurnSession`（本页只判停、不开闸、不进 Agent），所以判停结论由页面那条链读完回填进来
（`on_eot`），其余逐条对齐主链。

`mode`：`rule` = 离线替身（`HUB_*` 的 rule 分支，详情里照实标「离线替身」）；
`model` = 真判断（8009），页面选 `?eot=real` 时用。
"""
from __future__ import annotations

import logging
from threading import RLock

from gate import hub_arbiter
from gate.hub_accept import build_accept_fn
from gate.hub_ack import ACK_STALE_MS, ack_text, build_ack_fn, decide_ack
from gate.hub_floor import FLOOR_YIELD, build_floor_fn, floor_label
from gate.hub_gate import GateKeeper
from gate.hub_pace import (CUSHION_STALE_MS, build_cushion_fn, build_pace_fn,
                           cushion_text, decide_cushion)
from gate.hub_quiet import (QUIET_BACKOFF_MS, QUIET_CHECK_AFTER_MS, QUIET_FIRST_MS, QUIET_LABELS,
                            QUIET_STEP_MS, QUIET_WAIT, build_quiet_fn, decide_quiet, quiet_text)
from gate.turn_session import TurnConfig
from shared.text import normalize

log = logging.getLogger(__name__)

JUDGE_KEYS = ("accept", "ack", "pace", "cushion", "floor", "quiet")
# 车这一句「正在说」的窗口：页面中间大字显示约 4.2 秒，这段时间里人又开口就是期三的插话。
# 应一声不占话（期三只管「车正在说 / 正准备说」的那句内容话），所以窗口只为出声的整句开。
SPEAK_WINDOW_MS = 4200


def build_judges(mode="rule"):
    """五路判断各一份（与 `app/assembly.hub_gates` 同一批 `build_*_fn()`、同一个 mode 口径）。"""
    choice = "model" if mode == "model" else "rule"
    return {"accept": build_accept_fn(choice), "ack": build_ack_fn(choice),
            "pace": build_pace_fn(choice), "cushion": build_cushion_fn(choice),
            "floor": build_floor_fn(choice), "quiet": build_quiet_fn(choice)}


class HubSpeech:
    """一个会话一份：两席各自一套出声记账（与主链每席一份 TurnSession 的出声记账同构）。"""

    def __init__(self, mind, seats=("D", "P"), mode="rule"):
        self.seats = tuple(seats)
        self.mind = mind
        self.mode = "model" if mode == "model" else "rule"
        self.cfg = TurnConfig()
        self.judges = build_judges(self.mode)
        self.gates = {
            seat: GateKeeper(accept_fn=self.judges["accept"], scene_fn=mind.scene_snapshot,
                             seat=seat, ack_fn=self.judges["ack"],
                             pace_fn=self.judges["pace"], cushion_fn=self.judges["cushion"],
                             floor_fn=self.judges["floor"], quiet_fn=self.judges["quiet"])
            for seat in self.seats}
        self.lock = RLock()
        self.version = {seat: 0 for seat in self.seats}
        self.text_norm = {seat: "" for seat in self.seats}
        self.turns = {seat: 0 for seat in self.seats}
        self.speech_start_ms = {seat: None for seat in self.seats}
        self.last_text_ms = {seat: -10 ** 9 for seat in self.seats}
        # 「上次出声」锚点与说过的话（与 `TurnSession._ack_last_ms` / `hub_said` 同一套）
        self.last_spoken_ms = {seat: None for seat in self.seats}
        self.last_spoken_text = {seat: "" for seat in self.seats}
        self.ack_tried = {seat: set() for seat in self.seats}
        self.cushion_tried = {seat: set() for seat in self.seats}
        self.ack_total = {seat: 0 for seat in self.seats}
        self.cushion_total = {seat: 0 for seat in self.seats}
        self.quiet_total = {seat: 0 for seat in self.seats}
        self.hub_said = {seat: [] for seat in self.seats}
        self.last_gate = {seat: {"slot": None, "spoken": False, "reason": "", "t_ms": 0}
                          for seat in self.seats}
        # 冷场记账（与 `TurnSession` 的 `_quiet_*` 同一套）
        self.human_ms = {seat: None for seat in self.seats}
        self.hub_last_said_ms = None
        self.hub_last_text = ""
        self.quiet_anchor_ms = None
        self.quiet_tick_next = 0
        self.quiet_seq = 0
        self.quiet_episode = None
        self.quiet_last_open_ms = None
        self.quiet_last_text = None
        self.quiet_state = {"state": "idle", "outlet": None, "label": None, "reason": "",
                            "quiet_ms": 0, "asked": False}
        # 期三插话让路：车这句「正在说」的窗口 + 这一回四选（同一段插话只问一次）
        self.speak_until = {seat: -10 ** 9 for seat in self.seats}
        self.floor_seq = 0
        self.floor_episode = None
        self.floor_state = {"state": "idle", "choice": None, "outlet": None, "label": None,
                            "action": None, "reason": "", "sentence": "", "text": "",
                            "asked_ms": None, "latency_ms": None, "probabilities": None,
                            "confidence": None, "source": None, "model": None}

    # ---------- 来源（页面详情照实写：真判断 / 离线替身） ----------
    def sources(self):
        out = {}
        for key in JUDGE_KEYS:
            meta = getattr(self.judges.get(key), "meta", None)
            if isinstance(meta, dict):
                out[key] = {"mode": meta.get("mode"), "source": meta.get("source"),
                            "model": meta.get("model")}
        return out

    # ---------- 人这一侧：出字 → 同一版字上并发问五路 ----------
    def on_text(self, seat, text, now_ms, final=False):
        """这一席出了新字：版本号 +1 → `GateKeeper.on_text`（接不接 / 应声 / 节奏 / 垫句一起发）。

        与主链 `TurnSession.on_asr` 同一件事：只有**字真的变了**才算新的一版；`speech_ms` 按
        「这一段连续出声多久」算（垫句那把 1.5 秒的尺）。返回这一版的版本号（判停读完回填用）。
        """
        gate = self.gates.get(seat)
        body = (text or "").strip()
        norm = normalize(body)
        if gate is None or not norm:
            return None
        with self.lock:
            now_ms = int(now_ms)
            changed = norm != self.text_norm[seat]
            if changed:
                self.version[seat] += 1
                self.text_norm[seat] = norm
            if now_ms - self.last_text_ms[seat] > 900 or self.speech_start_ms[seat] is None:
                self.speech_start_ms[seat] = now_ms
            self.last_text_ms[seat] = now_ms
            self.human_ms[seat] = now_ms
            # 有人开口：冷场计时清零（与主链 `_quiet_clear` 同一条）
            self.quiet_anchor_ms = None
            self.quiet_episode = None
            self.quiet_tick_next = 0
            self.quiet_state.update({"state": "cleared", "outlet": None, "label": None,
                                     "quiet_ms": 0, "reason": "有人开口：冷场计时清零"})
            if not changed:
                return None
            speech_ms = (now_ms - int(self.speech_start_ms[seat])
                         if self.speech_start_ms[seat] else None)
            version = self.version[seat]
        gate.on_text(version, body, now_ms, final=bool(final),
                     speech_ms=(speech_ms if self.cfg.cushion_enabled else None),
                     turn=self.turns[seat])
        self._floor_maybe_ask(seat, body, version, int(now_ms))
        return version

    # ---------- 期三：车正在说，人又开口 → 问既有四选（不另造规则） ----------
    def _floor_maybe_ask(self, seat, text, version, now_ms):
        """车这句的「正在说」窗口里人又开口：按现成 `GateKeeper.start_floor` 问一次四选。

        同一段插话只问一次（有在飞的那回就不再问）；应声不开窗口，所以「应一声」不会进这里。
        """
        gate = self.gates.get(seat)
        if gate is None or getattr(gate, "floor_fn", None) is None:
            return
        with self.lock:
            if self.floor_episode is not None:
                log.warning("floor: 上一回四选还没落地，这一版不再问")
                return
            if now_ms >= self.speak_until[seat]:
                log.warning("floor: 车这句已过窗口（now=%s until=%s），不问",
                                   now_ms, self.speak_until[seat])
                return
            sentence = self.last_spoken_text[seat]
            self.floor_seq += 1
            episode = {"id": self.floor_seq, "seat": seat, "text": text,
                       "sentence": sentence, "asked_ms": now_ms}
            self.floor_episode = episode
            self.floor_state.update({"state": "asked", "choice": None, "outlet": None,
                                     "label": None, "action": None, "sentence": sentence,
                                     "text": text, "asked_ms": now_ms, "latency_ms": None,
                                     "probabilities": None, "confidence": None,
                                     "reason": "车这句还在说，人又开口：问四选（插话去哪）"})
        log.warning("floor: 车在说「%s」，人插话「%s」→ 问四选 #%s",
                    sentence[:20], text[:20], episode["id"])
        gate.start_floor(episode["id"], text, hub_sentence=sentence,
                         scene=self.mind.scene_snapshot(), t_ms=now_ms,
                         hub_state="speaking", version=version)

    def _floor_step(self, seat, now_ms):
        """四选回来没有：回来了就落账（让路 = 停下这句），并给页面一条 `car_floor`。"""
        with self.lock:
            episode = self.floor_episode
        if episode is None:
            return None
        verdict = self.gates[seat].floor_verdict(episode["id"], now_ms)
        if verdict is None or verdict.get("state") == "pending":
            return None
        outlet = verdict.get("outlet") or verdict.get("choice")
        action = verdict.get("action")
        with self.lock:
            self.floor_episode = None
            stop = outlet == FLOOR_YIELD
            if stop:
                # 让路：停下这句、回到收听（话都留下；本轮决策作废）
                self.speak_until[seat] = int(now_ms)
            self.floor_state.update({
                "state": verdict.get("state") or "ok", "choice": verdict.get("choice"),
                "outlet": outlet, "label": verdict.get("label") or floor_label(verdict.get("choice")),
                "action": action, "reason": verdict.get("reason") or "",
                "latency_ms": verdict.get("latency_ms"),
                "probabilities": verdict.get("probabilities"),
                "confidence": verdict.get("confidence"),
                "source": verdict.get("source"), "model": verdict.get("model"),
                "sentence": episode.get("sentence") or "", "text": episode.get("text") or ""})
            event = {"event": "car_floor", "t_ms": int(now_ms), "seat_id": seat, "shared": True,
                     "state": self.floor_state["state"], "choice": verdict.get("choice"),
                     "outlet": outlet, "label": self.floor_state["label"], "action": action,
                     "reason": self.floor_state["reason"], "sentence": episode.get("sentence"),
                     "text": episode.get("text"), "stopped": stop,
                     "latency_ms": verdict.get("latency_ms"),
                     "probabilities": verdict.get("probabilities"),
                     "confidence": verdict.get("confidence"),
                     "source": verdict.get("source"), "model": verdict.get("model")}
        return event

    def on_eot(self, seat, version, now_ms, verdict):
        """判停读完回填给闸门（主链在 `TurnSession._on_eot_result` 里做的同一件事）。"""
        gate = self.gates.get(seat)
        if gate is None or version is None:
            return
        verdict = verdict or {}
        gate.on_eot(int(version), int(now_ms), {
            "label": verdict.get("label"),
            "may_confirm": bool(verdict.get("may_confirm")),
            "may_start_early": bool(verdict.get("may_start_early")),
            "complete": verdict.get("complete"), "cut": verdict.get("cut"),
            "wait": verdict.get("wait"), "confidence": verdict.get("confidence"),
            "settled_by": verdict.get("settled_by"), "latency_ms": verdict.get("latency_ms"),
            "source": verdict.get("source"),
        })

    def note_turn_closed(self, seat):
        """这一席这一句收住了（判停说完 / 静音窗收口 / 文字发送）：轮次 +1，连续出声重新计时。

        冷场从这一刻起算（优先「中枢最后一句说完」的时刻，与冷场模块的起算口径一致）。
        """
        with self.lock:
            self.turns[seat] = self.turns[seat] + 1
            self.speech_start_ms[seat] = None
            self.quiet_anchor_ms = self.hub_last_said_ms or int(self.human_ms.get(seat) or 0) or None
            self.quiet_tick_next = 0

    # ---------- 车这一侧：垫句 → 应声 → 冷场（顺序与主链 tick 一致） ----------
    def poll(self, seat, now_ms, idle=False, topic=""):
        """主链 tick 里的三段出声：返回这一拍新出的话（`[]` = 这一拍没出声）。"""
        if seat not in self.gates:
            return []
        out = []
        spoke = self._maybe_cushion(seat, int(now_ms))
        if spoke:
            out.append(spoke)
        spoke = self._maybe_ack(seat, int(now_ms), cushion_spoke=bool(out))
        if spoke:
            out.append(spoke)
        spoke = self._maybe_quiet(seat, int(now_ms), idle=idle, topic=topic)
        if spoke:
            out.append(spoke)
        floor = self._floor_step(seat, int(now_ms))
        if floor:
            out.append(floor)
        return out

    def _speech_blocked(self, seat, now_ms):
        return hub_arbiter.speech_blocked(now_ms=now_ms,
                                          last_spoken_ms=self.last_spoken_ms[seat])

    def _note_spoken(self, seat, now_ms, text, slot):
        """出声之后：锚点、冷场起算、本席台账（与主链 `_quiet_note_hub_said` 同一条）。"""
        with self.lock:
            self.last_spoken_ms[seat] = int(now_ms)
            # 这句「正在说」的窗口：这之后人再开口，就是期三的插话（应声不开窗，它不算占话）
            if slot in ("quiet", "cushion"):
                self.speak_until[seat] = int(now_ms) + SPEAK_WINDOW_MS
            self.last_spoken_text[seat] = text
            self.hub_last_said_ms = int(now_ms)
            self.hub_last_text = text
            self.hub_said[seat].append({"t_ms": int(now_ms), "text": text, "slot": slot})
            if len(self.hub_said[seat]) > 8:
                self.hub_said[seat] = self.hub_said[seat][-8:]

    def _skip(self, seat, now_ms, slot, reason):
        with self.lock:
            self.last_gate[seat] = {"slot": slot, "spoken": False, "reason": reason,
                                    "t_ms": int(now_ms)}

    def _speech(self, seat, now_ms, slot, text, reason, source=None, model=None):
        self._note_spoken(seat, now_ms, text, slot)
        with self.lock:
            self.last_gate[seat] = {"slot": slot, "spoken": True, "reason": reason,
                                    "t_ms": int(now_ms)}
        return {"event": "car_speech", "slot": slot, "text": text, "t_ms": int(now_ms),
                "seat_id": seat, "reason": reason, "source": source, "model": model,
                "mode": self.mode, "shared": True}

    def _maybe_cushion(self, seat, now_ms):
        """垫一句（主链 `TurnSession._maybe_cushion` 同一条）：只认最近一版，积极 + 满 1.5 秒才问。"""
        if not self.cfg.cushion_enabled:
            return None
        gate = self.gates[seat]
        ready = gate.cushion_inputs()
        with self.lock:
            fresh = [item for item in ready
                     if int(item["version"]) not in self.cushion_tried[seat]]
            if not fresh:
                return None
            marks = fresh[0]
            for item in fresh:
                self.cushion_tried[seat].add(int(item["version"]))
            if len(self.cushion_tried[seat]) > 32:
                self.cushion_tried[seat] = set(sorted(self.cushion_tried[seat])[-16:])
            age_ms = now_ms - int(marks.get("t_start_ms") or now_ms)
            speech_ms = marks.get("speech_ms")
            text = cushion_text(self.cushion_total[seat])
            cushion = dict(marks.get("cushion") or {})
        if age_ms > CUSHION_STALE_MS:
            self._skip(seat, now_ms, "cushion",
                       "这一版已经过去 %.0fms（> %dms）：不追着旧话垫句"
                       % (age_ms, CUSHION_STALE_MS))
            return None
        say, reason = decide_cushion(pace=marks.get("pace"), cushion=cushion,
                                     eot=marks.get("eot"), accept=marks.get("accept"),
                                     speech_ms=speech_ms, last_spoken_ms=self.last_spoken_ms[seat],
                                     now_ms=now_ms, gap_ms=self.cfg.cushion_gap_ms, text=text)
        if not say:
            self._skip(seat, now_ms, "cushion", reason)
            return None
        with self.lock:
            self.cushion_total[seat] += 1
        return self._speech(seat, now_ms, "cushion", text, reason,
                            cushion.get("source"), cushion.get("model"))

    def _maybe_ack(self, seat, now_ms, cushion_spoke=False):
        """应一声（主链 `TurnSession._maybe_ack` 同一条）：这一拍刚垫过就不应声（垫句优先）。"""
        if not self.cfg.ack_enabled:
            return None
        gate = self.gates[seat]
        ready = gate.ack_inputs()
        with self.lock:
            fresh = [item for item in ready if int(item["version"]) not in self.ack_tried[seat]]
            if not fresh:
                return None
            if cushion_spoke:
                for item in fresh:
                    self.ack_tried[seat].add(int(item["version"]))
                self._skip(seat, now_ms, "ack", "同一时刻最多一句：垫句优先（这一拍刚垫了一句）")
                return None
            marks = fresh[0]
            for item in fresh:
                self.ack_tried[seat].add(int(item["version"]))
            if len(self.ack_tried[seat]) > 32:
                self.ack_tried[seat] = set(sorted(self.ack_tried[seat])[-16:])
            age_ms = now_ms - int(marks.get("t_start_ms") or now_ms)
            text = ack_text(self.ack_total[seat])
            ack = dict(marks.get("ack") or {})
        if age_ms > ACK_STALE_MS:
            self._skip(seat, now_ms, "ack",
                       "这一版已经过去 %.0fms（> %dms）：不追着旧话出声" % (age_ms, ACK_STALE_MS))
            return None
        say, reason = decide_ack(eot=marks.get("eot"), accept=marks.get("accept"),
                                 ack=ack, last_spoken_ms=self.last_spoken_ms[seat],
                                 now_ms=now_ms, gap_ms=self.cfg.ack_gap_ms, text=text)
        if not say:
            self._skip(seat, now_ms, "ack", reason)
            return None
        with self.lock:
            self.ack_total[seat] += 1
        return self._speech(seat, now_ms, "ack", text, reason, ack.get("source"), ack.get("model"))

    # ---------- 冷场（主链 `TurnSession._quiet_step` 同一条） ----------
    @staticmethod
    def _quiet_tick_at(index):
        """第几个刻度的安静时长（与主链 `TurnSession._quiet_tick_at` 同一套数字）：4s / 10s / 18s…"""
        index = int(index)
        if index <= 0:
            return int(QUIET_FIRST_MS)
        if index == 1:
            return int(QUIET_CHECK_AFTER_MS)
        return int(QUIET_CHECK_AFTER_MS) + int(QUIET_STEP_MS) * (index - 1)

    def _quiet_context(self, seat, topic):
        """「刚才在聊的那件事」：人这一侧最近一版字 → 中枢刚说过的 → 空。

        与主链 `TurnSession._quiet_context` 同一条（`_quiet_topic` 就是人那句原话）；
        别拿车自己上一句当上文，否则台词会套娃成「接着刚才「…」」。
        """
        if topic:
            return topic
        with self.lock:
            said = self.text_norm.get(seat) or ""
        return said or self.hub_last_text or ""

    def _scene_now(self):
        """这一刻场景流里已有的最新一份（与 `TurnSession._scene_now` 同一条：不等待、不重算）。"""
        source = getattr(self.mind, "scene_snapshot", None)
        if not callable(source):
            return None
        try:
            return source()
        except Exception:  # noqa: BLE001 - 取不到场景不该影响出声
            return None

    def _quiet_skip(self, seat, now_ms, reason):
        with self.lock:
            self.quiet_tick_next += 1
            self.quiet_state.update({"state": "skipped", "outlet": None, "label": None,
                                     "asked": True, "reason": reason})
        self._skip(seat, now_ms, "quiet", reason)

    def _maybe_quiet(self, seat, now_ms, idle, topic):
        """冷场三选：安静到刻度、真的没人说话才问一次（主链 `_quiet_step` 同一条）。"""
        if not self.cfg.quiet_enabled or not idle:
            return None
        gate = self.gates[seat]
        if getattr(gate, "quiet_fn", None) is None:
            return None
        with self.lock:
            anchor = self.quiet_anchor_ms
            episode = self.quiet_episode
            quiet_ms = int(now_ms) - int(anchor) if anchor is not None else 0
        if episode is not None:
            verdict = gate.quiet_verdict(episode["id"], now_ms)
            if verdict is None or verdict.get("state") == "pending":
                with self.lock:
                    self.quiet_state.update({"state": "asked", "asked": True,
                                             "quiet_ms": episode.get("quiet_ms") or 0,
                                             "reason": "冷场判断还没回来"})
                return None
            return self._apply_quiet(seat, now_ms, episode, verdict, topic)
        if anchor is None:
            with self.lock:
                self.quiet_state.update({"state": "no_anchor", "outlet": None, "label": None,
                                         "quiet_ms": 0,
                                         "reason": "还没起算：中枢没说过、人也还没说完一句"})
            return None
        due = self._quiet_tick_at(self.quiet_tick_next)
        if quiet_ms < due:
            with self.lock:
                self.quiet_state.update({"state": "counting", "outlet": None, "label": None,
                                         "quiet_ms": quiet_ms,
                                         "reason": "安静 %dms，下一拍刻度 %dms" % (quiet_ms, due)})
            return None
        blocked, why = self._speech_blocked(seat, now_ms)
        if blocked:
            self._quiet_skip(seat, now_ms, why)
            return None
        if self.quiet_last_open_ms is not None \
                and now_ms - int(self.quiet_last_open_ms) < int(QUIET_BACKOFF_MS):
            self._quiet_skip(seat, now_ms,
                             "离上一次冷场开口不到 %.1f 秒（两次开口之间要退避）"
                             % (QUIET_BACKOFF_MS / 1000.0))
            return None
        with self.lock:
            self.quiet_tick_next += 1
            self.quiet_seq += 1
            episode = {"id": self.quiet_seq, "anchor_ms": int(anchor), "quiet_ms": quiet_ms,
                       "tick_ms": int(due), "asked_ms": int(now_ms), "done": False,
                       "check_allowed": quiet_ms >= int(QUIET_CHECK_AFTER_MS)}
            self.quiet_episode = episode
            self.quiet_state.update({"state": "asked", "asked": True, "outlet": None,
                                     "label": None, "quiet_ms": quiet_ms,
                                     "reason": "安静到刻度、且真的没人说话：问一次「要不要开口」"})
        gate.start_quiet(episode["id"], quiet_ms=quiet_ms,
                         text=self._quiet_context(seat, topic), hub_last=self.hub_last_text,
                         check_allowed=episode["check_allowed"], scene=self._scene_now(),
                         t_ms=now_ms)
        return None

    def _apply_quiet(self, seat, now_ms, episode, verdict, topic):
        outlet, reason = decide_quiet(quiet=verdict, quiet_ms=episode.get("quiet_ms") or 0)
        with self.lock:
            episode["done"] = True
            episode["outlet"] = outlet
            episode["reason"] = reason
            self.quiet_episode = None
            self.quiet_state.update({"state": "done", "asked": True, "outlet": outlet,
                                     "label": QUIET_LABELS.get(outlet),
                                     "quiet_ms": episode.get("quiet_ms"), "reason": reason})
        if outlet == QUIET_WAIT:
            self._skip(seat, now_ms, "quiet", reason)
            return None
        text = quiet_text(outlet, self._quiet_context(seat, topic),
                          index=int(self.quiet_total[seat]), avoid=self.quiet_last_text)
        blocked, why = self._speech_blocked(seat, now_ms)
        if blocked:
            self._skip(seat, now_ms, "quiet", why)
            return None
        with self.lock:
            self.quiet_total[seat] += 1
            self.quiet_last_open_ms = int(now_ms)
            self.quiet_last_text = text
            self.quiet_state.update({"state": "spoken", "outlet": outlet,
                                     "label": QUIET_LABELS.get(outlet),
                                     "quiet_ms": episode.get("quiet_ms"), "reason": reason})
        event = self._speech(seat, now_ms, "quiet", text, reason,
                             verdict.get("source"), verdict.get("model"))
        event["outlet"] = outlet
        event["label"] = QUIET_LABELS.get(outlet)
        return event

    # ---------- 详情：这一拍的判断（页面只从这儿读；主画面不出现） ----------
    def snapshot(self, seat):
        gate = self.gates.get(seat)
        with self.lock:
            version = self.version[seat]
            turns = self.turns[seat]
            ack_total = self.ack_total[seat]
            cushion_total = self.cushion_total[seat]
            quiet_total = self.quiet_total[seat]
            last = dict(self.last_gate[seat])
            spoken_text = self.last_spoken_text[seat]
            spoken_ms = self.last_spoken_ms[seat]
            quiet_state = dict(self.quiet_state)
        judgements = gate.judgements(version) if gate is not None and version else {}
        return {"seat": seat, "version": version, "turns": turns,
                "eot": judgements.get("eot"), "accept": judgements.get("accept"),
                "ack": judgements.get("ack"), "pace": judgements.get("pace"),
                "cushion": judgements.get("cushion"), "floor": dict(self.floor_state),
                "quiet": dict(quiet_state, spoken=quiet_total),
                "spoken": {"ack": ack_total, "cushion": cushion_total, "quiet": quiet_total,
                           "last_text": spoken_text, "last_ms": spoken_ms},
                "last_gate": last, "mode": self.mode, "sources": self.sources()}

    def close(self):
        for gate in self.gates.values():
            try:
                gate.close()
            except Exception:  # noqa: BLE001 - 收会话时关闸门失败不影响退出
                pass
