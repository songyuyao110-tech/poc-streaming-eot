"""双席单元测试：两席会话/状态不混（C1 的离线部分）。

范围（用户 2026-11-03 收口）：两路干净 ASR 由前端给出 —— 同句去重与两路 ASR 隔离**不在本卡**，
所以这里没有 `dup_of`，只钉「一路 ASR 进一席、两席互不重置/取消/串文本」。
不连判停服务：`eot_fn` 是假实现，时间线由调用方显式喂入，任何机器都能复现
（真服务回放见 tests/poc-dual-asr-c1.py）。

    python3 -B tests/test_dual_seat.py

为什么压短 `confirm_silence_ms` 等窗口：假时钟按 10ms 步进，但判停/决策仍在线程池里真跑，
实时间只能用 sleep 换调度机会；窗口压到 200/400ms 才不会把 900ms 的判停超时当成「没判完」。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from asr.dual_seat import DualSeatSession, MIC_OF_SEAT, SEAT_OF_MIC
# noqa: E402
from gate.turn_session import TurnConfig, TurnSession
from shared.text import normalize
# noqa: E402

# noqa: E402
from shared.text import normalize
# noqa: E402

def fast_config():
    """压短的窗口：假时钟走 10ms 步、判停/决策仍在线程池里真跑。"""
    return TurnConfig(confirm_silence_ms=200, cut_wait_ms=300, think_wait_ms=600,
                      max_silence_ms=400, debounce_ms=80, eot_timeout_ms=900)
SCHEMA = {"name": "seat_heating", "description": "座椅加热",
          "parameters": {"type": "object", "properties": {"enabled": {"type": "boolean"}},
                         "required": ["enabled"], "additionalProperties": False}}


def eot_complete(text):
    return {"complete": 0.93, "cut": 0.03, "wait": 0.04, "label": "complete", "choice": "complete",
            "confidence": 0.95, "margin": 0.89, "model": "fake", "wall_ms": 1.0}


def eot_waiting(text):
    """一直「在犹豫」：让半句话停在 THINK_WAIT，便于观察另一席说话时它的锚点有没有被改。"""
    return {"complete": 0.10, "cut": 0.10, "wait": 0.80, "label": "wait", "choice": "wait",
            "confidence": 0.80, "margin": -0.70, "model": "fake", "wall_ms": 1.0}


def select(text, tool_set, context):
    return {"scores": {"tool:seat_heating": 0.9}, "selected": ["seat_heating"],
            "schemas": [SCHEMA], "native_schemas": [SCHEMA], "domains": [],
            "timing": {"stage1_ms": 0.0, "stage2_ms": 0.0, "total_ms": 0.0}}


def decide(utterance, selected, schemas, tool_set="core_score", context=None, **kwargs):
    return {"decision": {"kind": "tool_call", "tool": "seat_heating", "arguments": {"enabled": True}},
            "llm_ms": 1.0, "log_fields": {"decision_head": "fake"}}


def run_plan(plan, *, until, eot=eot_complete, step=10, sleep=0.004):
    """把 `[(t_ms, "asr", mic, text, final) | (t_ms, "vad", mic, kind)]` 按时间喂进双席。"""
    duo = DualSeatSession(fast_config(), None, eot_fn=eot, select_tools_fn=select,
                          decide_fn=decide, session_id="unit", evidence_root=None)
    plan = sorted(plan, key=lambda item: item[0])
    for now in range(0, until, step):
        while plan and plan[0][0] <= now:
            item = plan.pop(0)
            if item[1] == "asr":
                duo.on_asr(item[0], item[2], item[3], item[4])
            else:
                duo.on_vad(item[0], item[2], item[3])
        duo.tick(now)
        if sleep:
            time.sleep(sleep)
    return duo


def decisions(duo, seat=None):
    return [event for event in duo.events
            if event.get("event") == "llm_decision" and (seat is None or event["seat_id"] == seat)]


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(name + (" | " + detail if detail else ""))
    print("  ok  " + name + (" | " + detail if detail else ""))


# ---------- 1. 通道先验 / 两席 ----------

def test_channel_prior():
    print("[1] 一路 ASR 进一席（通道先验）")
    check("mic-D → 席 D", SEAT_OF_MIC["mic-D"] == "D" and MIC_OF_SEAT["D"] == "mic-D")
    check("mic-P → 席 P", SEAT_OF_MIC["mic-P"] == "P" and MIC_OF_SEAT["P"] == "mic-P")
    duo = DualSeatSession(fast_config(), None, eot_fn=eot_complete, select_tools_fn=select,
                          decide_fn=decide, session_id="unit-prior", evidence_root=None)
    check("只开两席", tuple(duo.seats) == ("D", "P"), str(tuple(duo.seats)))
    try:
        duo.on_asr(0, "mic-X", "你好", False)
    except ValueError as error:
        check("第 3 路直接拒收", "unknown mic_id" in str(error))
    else:
        raise AssertionError("mic-X 应该被拒收")
    duo.close()


def test_queue_single_writer():
    print("[2] 单写者：未 tick 前不派发")
    duo = DualSeatSession(fast_config(), None, eot_fn=eot_complete, select_tools_fn=select,
                          decide_fn=decide, session_id="unit-queue", evidence_root=None)
    duo.on_asr(0, "mic-D", "打开座椅加热", False)
    check("入队后本席还没有事件", not duo.seats["D"].events and not duo.events)
    duo.tick(0)
    check("tick 后才有 asr_update", any(event["event"] == "asr_update" for event in duo.events))
    duo.close()


# ---------- 2. 跨席隔离 ----------

def test_cross_seat_isolation():
    print("[3] D 半句 → P 插完整句 → D 续句：两席各自 CONFIRMED、互不重置/取消")
    duo = run_plan([
        (0, "asr", "mic-D", "把空调", False),
        (200, "vad", "mic-D", "speech_end"),
        (260, "asr", "mic-P", "导航去天安门", False),
        (560, "vad", "mic-P", "speech_end"),
        (700, "asr", "mic-D", "把空调调到二十四度", False),
        (900, "asr", "mic-D", "把空调调到二十四度", True),
        (920, "vad", "mic-D", "speech_end"),
    ], until=3000, eot=eot_waiting)
    check("两席都有 CONFIRMED", duo.confirm_latencies["D"] and duo.confirm_latencies["P"],
          str(duo.confirm_latencies))
    check("cross_cancel = 0", duo.counters["cross_cancel"] == 0, str(duo.counters))
    check("cross_reset = 0", duo.counters["cross_reset"] == 0, str(duo.counters))
    check("D 的文本不进 P 的决策输入",
          all("空调" not in text for text in duo.seat_texts()["P"]), str(duo.seat_texts()))
    check("P 的文本不进 D 的决策输入",
          all("天安门" not in text for text in duo.seat_texts()["D"]), str(duo.seat_texts()))
    duo.close()


def test_other_seat_does_not_touch_anchors():
    print("[4] P 说话不改 D 的锚点/版本/段（直接比对内部状态）")
    duo = DualSeatSession(fast_config(), None, eot_fn=eot_waiting, select_tools_fn=select,
                          decide_fn=decide, session_id="unit-anchor", evidence_root=None)
    duo.on_asr(0, "mic-D", "把空调", False)
    duo.on_vad(200, "mic-D", "speech_end")
    for now in range(0, 260, 10):
        duo.tick(now)
        time.sleep(0.004)
    before = {key: getattr(duo.seats["D"], key)
              for key in ("state", "version", "speech_start_ms", "last_change_ms", "segment")}
    duo.on_asr(260, "mic-P", "导航去天安门", False)
    duo.on_vad(560, "mic-P", "speech_end")
    for now in range(260, 620, 10):
        duo.tick(now)
        time.sleep(0.004)
    after = {key: getattr(duo.seats["D"], key)
             for key in ("state", "version", "speech_start_ms", "last_change_ms", "segment")}
    check("D 的状态/锚点/版本一字未动", before == after, "before=%s after=%s" % (before, after))
    check("D 没收到 P 的 vad", not any(event.get("seat_id") == "D" and event["event"] == "vad"
                                       and event["t_ms"] >= 260 for event in duo.events))
    duo.close()


def test_cross_counters_are_live():
    print("[5] 计数链路不是摆设：本席续句会触发 own 计数")
    duo = run_plan([(0, "asr", "mic-D", "把空调", False),
                    (300, "vad", "mic-D", "speech_end"),
                    (400, "asr", "mic-D", "把空调调到二十四度", False),
                    (600, "vad", "mic-D", "speech_end")], until=2000, eot=eot_waiting)
    check("own_reset > 0（本席续句重置了本席）", duo.counters["own_reset"] > 0, str(duo.counters))
    check("cross 全 0", duo.counters["cross_cancel"] == 0 and duo.counters["cross_reset"] == 0,
          str(duo.counters))
    duo.close()


# ---------- 3. 同时说话 ----------

def test_both_seats_speak_simultaneously():
    print("[6] 同刻两路不同指令 → 各一段一动作、不合并")
    duo = run_plan([(0, "asr", "mic-D", "打开座椅加热", False), (200, "vad", "mic-D", "speech_end"),
                    (0, "asr", "mic-P", "导航去天安门", False), (200, "vad", "mic-P", "speech_end")],
                   until=2000)
    check("动作 2 次", len(decisions(duo)) == 2, str([(e["seat_id"], e.get("utterance")) for e in decisions(duo)]))
    check("各落各席", {event["seat_id"] for event in decisions(duo)} == {"D", "P"})
    check("各席只拿到自己的文本", duo.seat_texts() == {"D": ["打开座椅加热"], "P": ["导航去天安门"]},
          str(duo.seat_texts()))
    check("cross 全 0", duo.counters["cross_cancel"] == 0 and duo.counters["cross_reset"] == 0)
    duo.close()


def test_halves_never_concatenate():
    print("[7] 两人各说半句 → 任何一席的文本都不含另一席的内容")
    duo = run_plan([(0, "asr", "mic-D", "把空调调到", True), (150, "vad", "mic-D", "speech_end"),
                    (200, "asr", "mic-P", "二十四度", True), (350, "vad", "mic-P", "speech_end")],
                   until=3000, eot=eot_waiting)
    texts = duo.seat_texts()
    check("D 的文本不含 P 的内容", all("二十四度" not in text for text in texts["D"]), str(texts))
    check("P 的文本不含 D 的内容", all("把空调" not in text for text in texts["P"]), str(texts))
    joined = normalize("".join(texts["D"]))
    check("没有拼成整句", normalize("把空调调到二十四度") != joined, joined)
    duo.close()


# ---------- 4. 单席回归 ----------

def test_single_seat_unchanged():
    print("[8] 单席（不经双席编排）行为不变、事件不带 seat_id")
    events = []
    session = TurnSession(fast_config(), events.append, eot_fn=eot_complete,
                          select_tools_fn=select, decide_fn=decide, evidence_root=None,
                          session_id="unit-single")
    for now in range(0, 1200, 10):
        if now == 0:
            session.on_asr(0, "打开座椅加热", False)
        if now == 200:
            session.on_vad(200, "speech_end")
        session.tick(now)
        time.sleep(0.004)
    confirmed = [event for event in events if event["event"] == "state_change" and event["to"] == "CONFIRMED"]
    decided = [event for event in events if event["event"] == "llm_decision"]
    check("单席 CONFIRMED", bool(confirmed))
    check("单席决策 1 次", len(decided) == 1)
    check("单席事件不带 seat_id", "seat_id" not in events[0])
    session.close()


CASES = [test_channel_prior, test_queue_single_writer, test_cross_seat_isolation,
         test_other_seat_does_not_touch_anchors, test_cross_counters_are_live,
         test_both_seats_speak_simultaneously, test_halves_never_concatenate,
         test_single_seat_unchanged]


def main():
    failures = []
    for case in CASES:
        try:
            case()
        except AssertionError as error:
            print("  FAIL " + str(error))
            failures.append((case.__name__, str(error)))
    print("\n%d/%d 例通过" % (len(CASES) - len(failures), len(CASES)))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
