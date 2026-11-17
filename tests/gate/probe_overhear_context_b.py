"""真链探针：方案 B —— 说完但不接的原话按席留 3 句、跨席、在开闸那一轮两栏带进 Agent。

与 `ui/server.py` 的 `dual_stream` / `DualMicZoom` 装配同形（同一批类，不是另写一条链）：

    MindReading（读心，规则档）→ GateKeeper(accept_fn=8009 真判断) → DualSeatSession → 两席 TurnSession
    一本会话级 OverhearLedger（跨席）

只把 **decide 段换成记录用的假件**（真 decide 要连 LLM 隧道，与本卡验收点无关）：
它把收到的 `context` 原样存下来，用来证明「开闸那一刻 Agent 拿到的是两栏」。
判停（8009）与「接不接」（8009）都是真调用。

跑法：
    python3 -B tests/probe_overhear_context_b.py            # 打印证据 JSON
    python3 -B tests/probe_overhear_context_b.py --json     # 只打 JSON（给证据文件用）

判据（`tmp/poc-overhear-context-b-验收.md`）：
1. 闲聊判定「说完但不接」→ 该席留存里出现这句，带「不是对车说的」；
2. 随后对车指令开闸 → decide 的 context 里两栏（此刻场景 + 刚才说过（不是对我说的））；
3. 副驾留一句 → 主驾开闸时也看得到（反之不验，同一本台账同一套拼法）；
4. 没说完 / 超时 → 不写台账（另一条 `tests/test_overhear_context.py` 用注入件钉死）。
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from asr.dual_seat import DualSeatSession
# noqa: E402
from gate.hub_accept import accept_meta, build_accept_fn
# noqa: E402
from gate.hub_gate import GateKeeper
# noqa: E402
from mind.mind_reading import MindReading
# noqa: E402
from overhear_ledger import OverhearLedger
# noqa: E402
from gate.turn_session import TurnConfig
# noqa: E402
CHAT = "你说今天这天气可真舒服"
COMMAND = "导航去望京SOHO"
CHAT_P = "你帮我看看那个…"


class Probe:
    def __init__(self):
        self.events = []
        self.decisions = []          # 每次 decide 收到的 (seat, utterance, context)
        self.ledger = OverhearLedger()
        self.session_id = "probe-overhear-b-%d" % int(time.time() * 1000)
        self.minds = {seat: MindReading(seat=seat, session_id=self.session_id + "-mind-" + seat)
                      for seat in ("D", "P")}
        self.gates = {seat: GateKeeper(accept_fn=build_accept_fn(), scene_fn=self.minds[seat].scene_snapshot,
                                       seat=seat, on_event=self.events.append)
                      for seat in ("D", "P")}

        def decide(utterance, selected, schemas, tool_set, context=None, **kwargs):
            self.decisions.append({"seat": self._deciding_seat, "utterance": utterance,
                                   "context": dict(context or {})})
            return {"decision": {"kind": "tool_call", "tool": "car_navi_to_destination",
                                 "arguments": {}}, "llm_ms": 1.0, "log_fields": {"head": "probe"}}
        self._deciding_seat = None
        self.session = DualSeatSession(
            TurnConfig(), self.events.append, session_id=self.session_id, evidence_root=None,
            decide_fn=self._decide_factory(decide),
            seat_extra=lambda seat: {"gate": self.gates[seat], "overhear": self.ledger})

    def _decide_factory(self, decide):
        def _fn(utterance, selected, schemas, tool_set, context=None, **kwargs):
            for seat in ("D", "P"):
                session = self.session.seats[seat]
                if getattr(session, "_agent_context", None) is context:
                    self._deciding_seat = seat
                    break
            return decide(utterance, selected, schemas, tool_set, context, **kwargs)
        return _fn

    def say(self, seat, text, t0, *, hold_ms=3000):
        """一席说一句：partial → final，然后按主链节奏 tick 到该席收口或超时。"""
        began = time.monotonic() * 1000 - t0
        mic = {"D": "mic-D", "P": "mic-P"}[seat]
        self.minds[seat].ingest(text=text[:max(1, len(text) // 2)], final=False)
        self.session.on_asr(t0, mic, text[:max(1, len(text) // 2)], False)
        self.minds[seat].ingest(text=text, final=True)
        self.session.on_asr(t0 + 400, mic, text, True)
        end = time.monotonic() * 1000 - began + hold_ms
        while time.monotonic() * 1000 - began < end:
            self.session.tick(int(time.monotonic() * 1000 - began))
            if self.session.seats[seat].turn_done:
                break
            time.sleep(0.01)
        # 收口后再多走几拍，让 decide worker / 事件落完
        for _ in range(8):
            self.session.tick(int(time.monotonic() * 1000 - began))
            time.sleep(0.01)
        return self.session.seats[seat]

    def close(self):
        for gate in self.gates.values():
            gate.close()
        self.session.close()
        for mind in self.minds.values():
            mind.close()


def main():
    probe = Probe()
    try:
        t0 = 1000
        # 1) 主驾闲聊 → 说完但不接：按席留存
        probe.say("D", CHAT, t0)
        chat_session = probe.session.seats["D"]
        chat_absorbed = int(chat_session.context_consumed)
        chat_turn_done = bool(chat_session.turn_done)
        # 2) 主驾对车指令 → 开闸 → Agent 拿到两栏（含刚才那句闲聊 + 当时的读心场景）
        probe.say("D", COMMAND, t0 + 3500)
        # 3) 副驾留一句闲聊；主驾再开闸 → 跨席能看到副驾那句
        probe.say("P", CHAT_P, t0 + 7000)
        probe.say("D", COMMAND, t0 + 10500)
        # 4) 副驾连着 4 句都判「说完但不接」→ 只留最近 3 句，最早那句被挤掉
        for index in range(4):
            probe.say("P", "闲聊第 %d 句" % index, t0 + 14000 + index * 3000)
        p_lines = [line["text"] for line in probe.ledger.lines("P")]
        summary = probe.session.summary()
        chat_decisions = [item for item in probe.decisions if item["seat"] == "D"]
        evidence = {
            "session_id": probe.session_id,
            "accept_source": {key: accept_meta()[key] for key in ("source", "threshold", "threshold_note")},
            "chat_absorbed": chat_absorbed,
            "chat_turn_done": chat_turn_done,
            "overhear_ledger": probe.ledger.snapshot(),
            "overhear_recorded_events": [
                {key: event.get(key) for key in ("seat", "seat_label", "text", "label", "kept")}
                for event in probe.events if event.get("event") == "overhear_recorded"],
            "agent_context_events": [
                {key: event.get(key) for key in ("seat_id", "scene", "columns", "one_entry")}
                for event in probe.events if event.get("event") == "agent_context"],
            "decisions": [
                {"seat": item["seat"], "utterance": item["utterance"],
                 "overhear_context": item["context"].get("overhear_context"),
                 "overhear_scene": item["context"].get("overhear_scene"),
                 "overhear_note": item["context"].get("overhear_note")}
                for item in chat_decisions],
            "gate_routes": [
                {"seat": event.get("seat_id"), "route": event.get("route"), "state": event.get("state"),
                 "reason": event.get("reason")}
                for event in probe.events
                if event.get("event") in ("gate_decision", "gate_open", "gate_context_only")],
            "counters": {"context_consumed": summary["seats"]["D"]["context_consumed"],
                         "consumed_D": summary["seats"]["D"]["consumed_count"]},
            "overhear_in_summary": summary["overhear"],
            "p_kept_after_four_more": p_lines,
        }
        print(json.dumps(evidence, ensure_ascii=False, indent=2))
    finally:
        probe.close()


if __name__ == "__main__":
    main()
