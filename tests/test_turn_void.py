"""turn_void 单元测试：生成中打断 = 作废本轮（P0-2）+ 承接 + P0-5 三硬复核。

范围（wave-2 P0-2/P0-5，2026-11-04 收口）：
- 生成期本席再开口 → 打断在途决策 → 发一次 `turn_void`（本轮决策作废、继续听本席），
  与「落地后再开口 → barge_in 开新一轮」「判停前改口/未说完」都分开计数。
- 承接：作废的是本轮决策，不是用户的话；半截 + 新话合起来再送，不丢弃重来。
- CONFIRM_WAIT 二轮回不是「作废本轮」，不得发 turn_void（reason 白名单 + 状态门双重保护）。
- 三硬复核（P0-5）：互不取消 / 不清对方上下文 / 不串席，对本条「生成中打断」源复核。

    python3 -B tests/test_turn_void.py

为什么压短窗口：假时钟按 10ms 步进，但判停/决策仍在线程池里真跑，实时间只能用 sleep 换
调度机会；窗口压到 200/400ms 才不会把 900ms 判停超时当成「没判完」。「决策在途」窗口由
decide 里 sleep(0.25) 制造（与 run_dual_asr_text_cases.py 同口径）。
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from dual_seat import DualSeatSession, MIC_OF_SEAT, SEAT_OF_MIC  # noqa: E402
from turn_session import TurnConfig, TurnSession, normalize  # noqa: E402


def fast_config():
    """压短的窗口：假时钟走 10ms 步、判停/决策仍在线程池里真跑。"""
    return TurnConfig(confirm_silence_ms=200, cut_wait_ms=300, think_wait_ms=600,
                      max_silence_ms=400, debounce_ms=80, eot_timeout_ms=900)


SCHEMA = {"name": "seat_heating", "description": "座椅加热",
          "parameters": {"type": "object", "properties": {"enabled": {"type": "boolean"}},
                         "required": ["enabled"], "additionalProperties": False}}
CONFIRM_SCHEMA = {"name": "car_drive_ESPSwitch", "description": "关闭 ESP 车身稳定控制",
                  "parameters": {"type": "object", "properties": {"enabled": {"type": "boolean"}},
                                 "required": ["enabled"], "additionalProperties": False}}


# ---------------------------------------------------------------- 假实现

def eot_complete(text):
    """强 complete + 高 confidence：当刻认（eot_complete），不走「可能说完」收敛档。"""
    return {"complete": 0.93, "cut": 0.03, "wait": 0.04, "label": "complete", "choice": "complete",
            "confidence": 0.95, "margin": 0.89, "model": "fake", "wall_ms": 1.0}


def eot_weak(text):
    """强 complete 概率、低 confidence：不抢跑，走「可能说完」→ confirm_silence 收口。

    confidence 用弱（0.55，`may_start_early=False`）：避免 speculative LLM 逐 partial 抢跑
    制造 self-cancel，污染 own_cancel/own_void 的隔离判据（与 run_dual_asr_text_cases.py
    的 fake_weak 同口径）。
    """
    return {"complete": 0.93, "cut": 0.03, "wait": 0.04, "label": "complete", "choice": "complete",
            "confidence": 0.55, "margin": 0.89, "model": "fake-weak", "wall_ms": 1.0}


def select(text, tool_set, context):
    return {"scores": {"tool:seat_heating": 0.9}, "selected": ["seat_heating"],
            "schemas": [SCHEMA], "native_schemas": [SCHEMA], "domains": [],
            "timing": {"stage1_ms": 0.0, "stage2_ms": 0.0, "total_ms": 0.0}}


def select_confirm(text, tool_set, context):
    return {"scores": {"tool:car_drive_ESPSwitch": 0.9}, "selected": ["car_drive_ESPSwitch"],
            "schemas": [CONFIRM_SCHEMA], "native_schemas": [CONFIRM_SCHEMA], "domains": [],
            "timing": {"stage1_ms": 0.0, "stage2_ms": 0.0, "total_ms": 0.0}}


def decide_slow(utterance, selected, schemas, tool_set="core_score", context=None, **kwargs):
    time.sleep(0.25)          # 制造「决策在途」窗口
    return {"decision": {"kind": "tool_call", "tool": "seat_heating", "arguments": {"enabled": True}},
            "llm_ms": 250.0, "log_fields": {"decision_head": "slow_mock"}}


def decide_fast(utterance, selected, schemas, tool_set="core_score", context=None, **kwargs):
    return {"decision": {"kind": "tool_call", "tool": "seat_heating", "arguments": {"enabled": True}},
            "llm_ms": 1.0, "log_fields": {"decision_head": "fast_mock"}}


def decide_confirm(utterance, selected, schemas, tool_set="core_score", context=None, **kwargs):
    """命中 CONFIRM_POOL 的高危工具，进入二次确认路径。"""
    return {"decision": {"kind": "tool_call", "tool": "car_drive_ESPSwitch",
                         "arguments": {"enabled": True}},
            "llm_ms": 1.0, "log_fields": {"decision_head": "confirm_mock"}}


# ---------------------------------------------------------------- 驱动

def make_duo(*, eot=eot_weak, decide=decide_slow, select_fn=select, session_id="unit"):
    return DualSeatSession(fast_config(), None, eot_fn=eot, select_tools_fn=select_fn,
                           decide_fn=decide, session_id=session_id, evidence_root=None)


def run_plan(plan, *, until, eot=eot_weak, decide=decide_slow, select_fn=select,
             step=10, sleep=0.004):
    """把 `[(t_ms, "asr", mic, text, final) | (t_ms, "vad", mic, kind)]` 按时间喂进双席。"""
    duo = make_duo(eot=eot, decide=decide, select_fn=select_fn)
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


def voids(duo, seat=None):
    return [event for event in duo.events
            if event.get("event") == "turn_void" and (seat is None or event["seat_id"] == seat)]


def check(name, condition, detail=""):
    if not condition:
        raise AssertionError(name + (" | " + detail if detail else ""))
    print("  ok  " + name + (" | " + detail if detail else ""))


# ---------------------------------------------------------------- 用例

def test_mid_generation_interrupt_voids_turn():
    print("[1] 生成中本席再开口 → 打断在途决策、作废本轮（turn_void reason=new_text）")
    duo = run_plan([(0, "asr", "mic-D", "打开座椅加热", False),
                    (200, "vad", "mic-D", "speech_end"),
                    (500, "asr", "mic-D", "打开座椅加热和通风", False)],
                   until=3000, eot=eot_weak, decide=decide_slow)
    seat_voids = voids(duo, "D")
    check("D 恰好 1 次 turn_void", len(seat_voids) == 1, str(seat_voids))
    check("turn_void reason == new_text", seat_voids and seat_voids[0]["reason"] == "new_text",
          str(seat_voids[0] if seat_voids else None))
    check("D 席计数器 turn_voids == 1", duo.seats["D"].counters["turn_voids"] == 1,
          str(duo.seats["D"].counters))
    check("own_void == 1 且 cross_void == 0",
          duo.counters["own_void"] == 1 and duo.counters["cross_void"] == 0, str(duo.counters))
    check("合句仍落地 1 次决策",
          len(decisions(duo, "D")) == 1
          and normalize(decisions(duo, "D")[0]["utterance"]) == normalize("打开座椅加热和通风"),
          str([(e["seat_id"], e.get("utterance")) for e in decisions(duo)]))
    duo.close()


def test_confirm_wait_no_void():
    print("[2] 二次确认二轮回不回 LISTENING → 不得发 turn_void（reason 白名单 + 状态门）")
    duo = run_plan([(0, "asr", "mic-D", "关闭ESP", False),
                    (200, "vad", "mic-D", "speech_end"),
                    (300, "asr", "mic-D", "好的", True),
                    (400, "vad", "mic-D", "speech_end")],
                   until=2000, eot=eot_complete, decide=decide_confirm, select_fn=select_confirm)
    confirm_requests = [e for e in duo.events if e.get("event") == "confirm_request"]
    confirm_execs = [e for e in duo.events if e.get("event") == "confirm_exec"]
    check("触发二次确认（confirm_request）", bool(confirm_requests), str(confirm_requests))
    check("二轮回「好的」→ 同意执行（confirm_exec）", bool(confirm_execs), str(confirm_execs))
    check("全程 0 次 turn_void", not voids(duo), str(voids(duo)))
    check("D 席计数器 turn_voids == 0", duo.seats["D"].counters["turn_voids"] == 0,
          str(duo.seats["D"].counters))
    duo.close()


def test_both_seats_interrupt_no_cross():
    print("[3] P0-5：两席同刻被生成中打断 → 各自作废、互不取消/串席")
    duo = run_plan([(0, "asr", "mic-D", "打开座椅加热", False),
                    (0, "asr", "mic-P", "导航去天安门", False),
                    (200, "vad", "mic-D", "speech_end"),
                    (200, "vad", "mic-P", "speech_end"),
                    (500, "asr", "mic-D", "打开座椅加热和通风", False),
                    (500, "asr", "mic-P", "导航去西单", False)],
                   until=3000, eot=eot_weak, decide=decide_slow)
    check("每席各 1 次 turn_void",
          duo.seats["D"].counters["turn_voids"] == 1 and duo.seats["P"].counters["turn_voids"] == 1,
          "D=%s P=%s" % (duo.seats["D"].counters["turn_voids"], duo.seats["P"].counters["turn_voids"]))
    check("own_void == 2（两席各自归本席）且 cross_void == 0",
          duo.counters["own_void"] == 2 and duo.counters["cross_void"] == 0, str(duo.counters))
    check("cross_cancel == 0 且 cross_reset == 0",
          duo.counters["cross_cancel"] == 0 and duo.counters["cross_reset"] == 0, str(duo.counters))
    texts = duo.seat_texts()
    check("不串席：D 不含 P 的续句、P 不含 D 的续句",
          all("导航" not in text for text in texts["D"]) and all("座椅" not in text for text in texts["P"]),
          str(texts))
    check("每席续句各自落地决策",
          len(decisions(duo, "D")) == 1 and len(decisions(duo, "P")) == 1,
          str([(e["seat_id"], e.get("utterance")) for e in decisions(duo)]))
    duo.close()


def test_post_landing_barge_in_no_void():
    print("[4] 落地后再开口 → barge_in 开新一轮，不是 turn_void")
    duo = run_plan([(0, "asr", "mic-D", "打开座椅加热", False),
                    (200, "vad", "mic-D", "speech_end"),
                    (500, "asr", "mic-D", "打开座椅通风", False),
                    (700, "vad", "mic-D", "speech_end")],
                   until=2000, eot=eot_complete, decide=decide_fast)
    barge_ins = [e for e in duo.events if e.get("event") == "barge_in"]
    check("触发 barge_in（why=asr_after_action）",
          bool(barge_ins) and all(e["why"] == "asr_after_action" for e in barge_ins), str(barge_ins))
    check("全程 0 次 turn_void", not voids(duo), str(voids(duo)))
    check("开新一轮（turn_index == 2）", duo.seats["D"].turn_index == 2,
          str(duo.seats["D"].turn_index))
    check("两轮各落 1 次决策", len(decisions(duo, "D")) == 2,
          str([e.get("utterance") for e in decisions(duo, "D")]))
    duo.close()


def test_invariant_llm_calls_eq():
    print("[5] 打断后账目不变式：每席 llm_calls == consumed + llm_wasted")
    duo = run_plan([(0, "asr", "mic-D", "打开座椅加热", False),
                    (200, "vad", "mic-D", "speech_end"),
                    (500, "asr", "mic-D", "打开座椅加热和通风", False)],
                   until=3000, eot=eot_weak, decide=decide_slow)
    for seat in ("D", "P"):
        c = duo.seats[seat].counters
        check("%s 席 llm_calls == consumed + llm_wasted" % seat,
              c["llm_calls"] == c["consumed"] + c["llm_wasted"],
              str(c))
    check("D 席确有一个被作废的在途决策（llm_wasted >= 1）", duo.seats["D"].counters["llm_wasted"] >= 1,
          str(duo.seats["D"].counters))
    duo.close()


def test_interrupt_preserves_text():
    print("[6] 承接：作废的是决策不是话；半截 + 新话合起来落地，不丢弃重来")
    duo = run_plan([(0, "asr", "mic-D", "打开座椅加热", False),
                    (200, "vad", "mic-D", "speech_end"),
                    (500, "vad", "mic-D", "speech_start"),
                    (520, "asr", "mic-D", "和通风", False),
                    (700, "vad", "mic-D", "speech_end")],
                   until=3000, eot=eot_weak, decide=decide_slow)
    seat_voids = voids(duo, "D")
    check("恰好 1 次 turn_void（reason=speech_start）",
          len(seat_voids) == 1 and seat_voids[0]["reason"] == "speech_start",
          str(seat_voids))
    check("半截决策未落地（被取消的在途 decide 不产生 llm_decision）",
          len(decisions(duo, "D")) == 1,
          str([e.get("utterance") for e in decisions(duo, "D")]))
    check("半截 + 新话合句落地（段拼接不丢字）",
          normalize(decisions(duo, "D")[0]["utterance"]) == normalize("打开座椅加热和通风"),
          str(decisions(duo, "D")[0]["utterance"]))
    check("turn_void 不重置版本（version 单调递增）", duo.seats["D"].version >= 2,
          str(duo.seats["D"].version))
    check("承接口径账目平", duo.seats["D"].counters["llm_calls"]
          == duo.seats["D"].counters["consumed"] + duo.seats["D"].counters["llm_wasted"],
          str(duo.seats["D"].counters))
    duo.close()


CASES = [test_mid_generation_interrupt_voids_turn, test_confirm_wait_no_void,
         test_both_seats_interrupt_no_cross, test_post_landing_barge_in_no_void,
         test_invariant_llm_calls_eq, test_interrupt_preserves_text]


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
