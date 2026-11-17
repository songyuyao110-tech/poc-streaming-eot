"""会话脑（C2）离线单测：指代 / 在途 IU / 单一决策入口 / 归并窗 / 账目。

不连 8009、不起线程、不建双席会话——直接喂 `SessionBrain` 事件，钉住语义：

    python3 -B tests/test_orchestra.py      → 期望「N/N 例通过」

口径来源：定稿 `raw/2026-11-03-dual-asr-v2-refine-spec.md` §4.1 §4.3（A2 指代/作用域、A6 对账）、
`tests/asr/dual_asr_text_cases.jsonl` TC-16～TC-19 / TC-36。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.dont_write_bytecode = True

from agent.orchestra import SessionBrain, is_anaphora_prefix, is_reference  # noqa: E402

TOOL = "seat_heating"
SCHEMA = {"name": TOOL, "description": "座椅加热",
          "parameters": {"type": "object", "properties": {"enabled": {"type": "boolean"}},
                         "required": ["enabled"], "additionalProperties": False}}
RESULTS = []


def check(name, ok, detail=""):
    RESULTS.append((name, bool(ok), detail))
    print("  %s  %s%s" % ("ok  " if ok else "FAIL", name, ("｜" + detail) if detail and not ok else ""))


def make_decide(tool=TOOL, arguments=None, calls=None):
    def _decide(utterance, selected, schemas, tool_set="core_score", context=None, **kwargs):
        if calls is not None:
            calls.append(utterance)
        return {"decision": {"kind": "tool_call", "tool": tool,
                             "arguments": dict(arguments or {"enabled": True})},
                "llm_ms": 1.0, "log_fields": {"decision_head": "unit"}}
    return _decide


def new_brain(decide=None, calls=None, **kwargs):
    return SessionBrain(decide or make_decide(calls=calls), **kwargs)


def say(brain, seat, text, selected=None, t_ms=0):
    """让某席走一次决策入口（等价于该席的 decide_fn 被调用）。"""
    brain.mark_now(t_ms)
    return brain.decide(seat, text, [TOOL], [SCHEMA], "core_score", {"synthetic": True})


def applied(brain, seat, decision, t_ms):
    """回填一次「该席真的用上了这个决策」（TurnSession 的 llm_decision 事件）。"""
    brain.mark_now(t_ms)
    brain.observe({"event": "llm_decision", "t_ms": t_ms, "seat_id": seat, "decision": decision})


def speech(brain, seat, text, t_ms):
    brain.mark_now(t_ms)
    brain.observe({"event": "asr_update", "t_ms": t_ms, "seat_id": seat, "segment": text,
                   "changed": True})


def shortened(brain):
    """中途观察：只取账，不结算（结算会把在途 AU 记成放弃）。"""
    audit = brain.snapshot()
    return {key: audit[key] for key in ("actions_effective", "repeat_action", "receipt_has_both",
                                        "au_executed", "au_abandoned", "wrong_seat",
                                        "iu_open", "iu_committed", "iu_eq")}


# ---------------------------------------------------------------- 用例

def case_reference_lexicon():
    check("「我也要」＝指代", is_reference("我也要"))
    check("「我也」＝指代", is_reference("我也"))
    check("「我也要通风」不当短附和（留给 C3 判帧）", not is_reference("我也要通风"))
    check("「不要了」不是指代", not is_reference("不要了"))
    check("「打开座椅加热」不是指代", not is_reference("打开座椅加热"))
    check("开口前缀「我」算附和候选", is_anaphora_prefix("我"))
    check("开口前缀「打」不算附和候选", not is_anaphora_prefix("打"))


def case_direct_decision():
    calls = []
    brain = new_brain(calls=calls)
    outcome = say(brain, "D", "打开座椅加热", t_ms=100)
    decision = outcome["decision"]
    check("直连决策走一次模型", calls == ["打开座椅加热"], str(calls))
    check("决策带 iu_id / au_id", bool(decision.get("iu_id")) and bool(decision.get("au_id")))
    check("决策未应用前不执行", shortened(brain)["au_executed"] == 0)
    check("在途 IU 是 open", any(iu["status"] == "open" for iu in brain.snapshot()["ius"]))
    applied(brain, "D", decision, 350)
    audit = shortened(brain)
    check("应用后 1 个 AU 执行", audit["au_executed"] == 1, str(audit))
    check("回执写到屏上（含发起席）", "发起 D" in brain.aus[0].receipt, brain.aus[0].receipt)
    check("账：iu_open 0 且 committed==executed+abandoned", audit["iu_eq"])
    check("无指代时不发布 decisions 覆盖（席级口径为准）", "decisions" not in brain.snapshot())


def case_merge_before_landing():
    """TC-16：D 打开座椅加热，D 确认后 150ms P「我也要」→ 并入同一 AU、一次执行到两座。"""
    calls = []
    brain = new_brain(calls=calls)
    d = say(brain, "D", "打开座椅加热", t_ms=1000)["decision"]
    speech(brain, "P", "我", 1150)
    applied(brain, "D", d, 1250)                      # 已应用，但另一席刚开口 → 卡归并窗
    check("另一席同时开口 → AU 卡进归并窗", brain.aus[0].state == "held", brain.aus[0].state)
    p = say(brain, "P", "我也要", t_ms=1900)["decision"]
    check("附和并入同一 AU（absorbed）", p.get("absorbed") is True)
    check("参与者变成两席", p.get("participants") == ["D", "P"], str(p.get("participants")))
    applied(brain, "P", p, 1950)
    audit = shortened(brain)
    check("只执行一次", audit["au_executed"] == 1, str(audit))
    check("生效动作 1 个", audit["actions_effective"] == 1, str(audit))
    check("重复动作 0", audit["repeat_action"] == 0)
    check("合并回执含两席", audit["receipt_has_both"] is True)
    check("作用域＝两座", brain.aus[0].scope == ["D", "P"], str(brain.aus[0].scope))
    check("编排层真值：D 1 / P 0", brain.audit()["decisions"] == {"D": 1, "P": 0},
          str(brain.snapshot().get("decisions")))
    check("只调了一次模型（附和不再决策）", calls == ["打开座椅加热"], str(calls))
    check("账平", audit["iu_eq"])


def case_after_landing():
    """TC-17：决策落地后才说「我也要」→ 新 AU，只作用 P 座，不重发 D 座。"""
    brain = new_brain()
    d = say(brain, "D", "打开座椅加热", t_ms=1000)["decision"]
    applied(brain, "D", d, 1250)
    check("无人附和时立即落地", brain.aus[0].state == "executed")
    p = say(brain, "P", "我也要", t_ms=1750)["decision"]
    check("新 AU 指代上一个动作", p.get("inherited_from") == d.get("au_id"), str(p))
    check("新 AU 作用域只有 P 座", p.get("scope") == ["P"], str(p.get("scope")))
    applied(brain, "P", p, 2100)
    audit = shortened(brain)
    check("2 个 AU 各自执行 1 次", audit["au_executed"] == 2, str(audit))
    check("重复动作 0（D 座没有重发）", audit["repeat_action"] == 0)
    check("错座 0", audit["wrong_seat"] == 0)
    check("编排层真值：D 1 / P 1", brain.audit()["decisions"] == {"D": 1, "P": 1})
    check("账平", audit["iu_eq"])


def case_no_recent_action():
    """TC-19：会话刚开始说「我也要」→ 恰 1 次回问，不瞎猜、不产生动作。"""
    brain = new_brain()
    p = say(brain, "P", "我也要", t_ms=100)["decision"]
    check("回问（ask_user）", p.get("kind") == "ask_user", str(p))
    check("回问不带动作", not p.get("tool") or p.get("tool") == "ask_user")
    again = say(brain, "P", "我也要", t_ms=3000)["decision"]
    check("不循环追问（第二次不再问）", again.get("kind") != "ask_user", str(again))
    applied(brain, "P", p, 900)
    audit = shortened(brain)
    check("执行动作 0", audit["actions_effective"] == 0 and audit["au_executed"] == 0, str(audit))
    check("编排层真值：P 0", brain.audit()["decisions"] == {"D": 0, "P": 0})
    check("账平", audit["iu_eq"])


def case_merge_window_timeout():
    """另一席开口后说成了别的指令：不再占着窗口，立刻落地（不漏执行）。"""
    brain = new_brain(merge_window_ms=2500)
    d = say(brain, "D", "打开座椅加热", t_ms=1000)["decision"]
    speech(brain, "P", "我", 1150)
    applied(brain, "D", d, 1250)
    check("先卡窗", brain.aus[0].state == "held")
    speech(brain, "P", "我这边", 1400)          # 不是附和的走向
    brain.tick(1500)
    check("另一席不像附和 → 立刻落地", brain.aus[0].state == "executed", brain.aus[0].state)
    check("只作用发起席", brain.aus[0].scope == ["D"])


def case_merge_window_deadline():
    brain = new_brain(merge_window_ms=500)
    d = say(brain, "D", "打开座椅加热", t_ms=1000)["decision"]
    speech(brain, "P", "我", 1150)
    applied(brain, "D", d, 1250)
    check("卡窗", brain.aus[0].state == "held")
    brain.tick(1250 + 600)
    check("窗口走完照常落地", brain.aus[0].state == "executed")
    check("账平", shortened(brain)["iu_eq"])


def case_cancelled_decision_accounts():
    """投机决策被判 stale 作废：AU 记放弃，IU 照样结账（不漏执行、不虚记）。"""
    brain = new_brain()
    d = say(brain, "D", "打开座椅加热", t_ms=1000)["decision"]
    brain.mark_now(1100)
    brain.observe({"event": "llm_cancel", "t_ms": 1100, "seat_id": "D", "reason": "stale_text"})
    audit = shortened(brain)
    check("作废的决策不进执行", audit["au_executed"] == 0, str(audit))
    check("记一次放弃", audit["au_abandoned"] == 1, str(audit))
    check("iu_committed == executed + abandoned", audit["iu_committed"] == 1)
    check("账平", audit["iu_eq"])


def case_repeat_action():
    """同一动作（tool+args+scope）下发两次 → 重复动作计数会响（硬门槛 0 的探针）。"""
    brain = new_brain()
    for t in (1000, 3000):
        d = say(brain, "D", "打开座椅加热", t_ms=t)["decision"]
        applied(brain, "D", d, t + 200)
    audit = shortened(brain)
    check("两次落地都执行", audit["au_executed"] == 2, str(audit))
    check("重复动作被抓到", audit["repeat_action"] == 1, str(audit))


def case_confirm_pool_not_executed():
    """8 池高危工具不由 C2 执行（跨席确认路径 C3 才做）：记待确认、结算时记放弃。"""
    brain = new_brain(decide=make_decide(tool="car_drive_DriveModeSwitch", arguments={}))
    d = say(brain, "D", "切换到运动驾驶模式", t_ms=1000)["decision"]
    applied(brain, "D", d, 1200)
    check("池内工具不直接执行", brain.aus[0].state == "awaiting_confirm", brain.aus[0].state)
    audit = brain.audit()
    check("直执 0 / 执行 0", audit["au_executed"] == 0)
    check("结算后账平", audit["iu_eq"] and audit["iu_open"] == 0)


def main():
    cases = [case_reference_lexicon, case_direct_decision, case_merge_before_landing,
             case_after_landing, case_no_recent_action, case_merge_window_timeout,
             case_merge_window_deadline, case_cancelled_decision_accounts, case_repeat_action,
             case_confirm_pool_not_executed]
    for case in cases:
        print("[%s]" % case.__name__)
        case()
    passed = sum(1 for _name, ok, _detail in RESULTS if ok)
    print("\n%d/%d 例通过" % (passed, len(RESULTS)))
    return 0 if passed == len(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
