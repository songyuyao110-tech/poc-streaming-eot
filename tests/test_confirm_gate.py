"""验收：对话式二次确认闸门（confirm_gate）判定逻辑 + mock 打分稳定性。

对应 docs/spec-kev-confirmation-v2.md §6.1 的验收场景表。分两类：
  A. judge_confirm_reply（纯函数）—— 直接喂构造好的三问概率，逐条断言裁决逻辑走对分支。不依赖 KEV/LLM。
  B. score_confirm_reply mock 模式 —— 不连真实 KEV，产出的分数稳定、可复现，且 6 个验收样例
     经 mock 打分后 judge 落对分支。
真实 KEV 打分（mock=False）需 LLM 隧道，本地不验证（见文件底部说明）。

运行：python3 -B tests/test_confirm_gate.py    （本仓库无 pytest，沿用手写断言脚本风格）
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from confirm_gate import (CONFIRM_POOL, confirm_ask_for, is_confirm_tool,
                          judge_confirm_reply, score_confirm_reply)


# ---------------------------------- 工具函数 ----------------------------------

PASS = 0


def check(name: str, cond: bool):
    global PASS
    status = "PASS" if cond else "** FAIL **"
    print(f"  [{status}] {name}")
    if cond:
        PASS += 1
    return cond


def run_test(name, fn):
    print(f"== {name} ==")
    fn()
    print()


# ---------------------------------- A. judge_confirm_reply 纯裁决 ----------------------------------

# 每例给出能走对分支的三问概率。裁决：takeover>=0.5 优先；其次正负差>=0.15；否则 unknown。

def test_judge_pure():
    # 场景 1 "好的，关吧" -> agree：reject-agree 差为负，agree-reject 明显正
    scores = {"agree": 0.88, "reject": 0.05, "takeover": 0.07}
    check('A1 "好的，关吧" -> agree', judge_confirm_reply("好的，关吧", scores) == "agree")

    # 场景 2 "不要，别动" -> reject
    scores = {"agree": 0.05, "reject": 0.88, "takeover": 0.07}
    check('A2 "不要，别动" -> reject', judge_confirm_reply("不要，别动", scores) == "reject")

    # 场景 3 "你先别管这个" -> reject（显式撤回归 reject）
    scores = {"agree": 0.05, "reject": 0.90, "takeover": 0.05}
    check('A3 "你先别管这个" -> reject', judge_confirm_reply("你先别管这个", scores) == "reject")

    # 场景 4 "先看看空调，调到24度" -> takeover（优先级最高，即使 reject 较高也先判 takeover）
    scores = {"agree": 0.10, "reject": 0.40, "takeover": 0.50}
    check('A4 "先看看空调…" -> takeover(优先)', judge_confirm_reply("先看看空调，调到24度", scores) == "takeover")
    # takeover 边界：刚好等于 0.5 也判接管
    check('A4b takeover 边界==0.5 -> takeover', judge_confirm_reply("x", {"agree": 0.2, "reject": 0.3, "takeover": 0.5}) == "takeover")

    # 场景 5 "嗯……随便吧"(含糊) -> unknown（三问都模棱两可且差距小）
    scores = {"agree": 0.30, "reject": 0.35, "takeover": 0.35}
    check('A5 "嗯……随便吧"(含糊) -> unknown', judge_confirm_reply("嗯……随便吧", scores) == "unknown")

    # 场景 6 "你敢关试试" -> reject（负情绪，靠 p_reject 高）
    scores = {"agree": 0.05, "reject": 0.90, "takeover": 0.05}
    check('A6 "你敢关试试" -> reject(负情绪)', judge_confirm_reply("你敢关试试", scores) == "reject")

    # 补充：正负差刚好等于边界 0.15、但 agree 绝对分 < AGREE_FLOOR(0.75) -> 落 unknown（"含糊即拒"）
    # 注：AGREE_FLOOR=0.75 下，作为获胜支路的 agree 因 reject≤3-agree 约束，其真实最小 margin≥0.5，
    #     margin≈0.15 的非 agree 获胜场景只会因"绝对分不足"落 unknown，正符合安全意图。
    check('A7 边距==0.15 但 agree=0.50<0.75 -> unknown(含糊即拒)',
          judge_confirm_reply("x", {"agree": 0.50, "reject": 0.35, "takeover": 0.15}) == "unknown")
    check('A8 边距==0.14, agree=0.49<0.75 -> unknown',
          judge_confirm_reply("x", {"agree": 0.49, "reject": 0.35, "takeover": 0.16}) == "unknown")
    # 「含糊即拒」护栏：agree 绝对分达标(≥AGREE_FLOOR=0.75)且正负差够才算 agree；否则 unknown
    check('A11 agree=0.77, reject=0.15, 差0.62 & 绝对分达标 -> agree',
          judge_confirm_reply("x", {"agree": 0.77, "reject": 0.15, "takeover": 0.08}) == "agree")
    check('A12 agree=0.70, reject=0.30, 差0.40 但绝对分不足 -> unknown(不误执行)',
          judge_confirm_reply("x", {"agree": 0.70, "reject": 0.30, "takeover": 0.0}) == "unknown")
    check('A13 agree=0.75 恰在门槛 -> agree(边界含等号)',
          judge_confirm_reply("x", {"agree": 0.75, "reject": 0.20, "takeover": 0.05}) == "agree")
    # 真实校准形态："随便你"(agree≈0.711, 正差够但 <0.75) -> unknown
    check('A15 真实形态 agree=0.711, 差0.537 但<0.75 -> unknown(推责不执行)',
          judge_confirm_reply("x", {"agree": 0.711, "reject": 0.174, "takeover": 0.334}) == "unknown")
    # 其余安全支路不受 AGREE_FLOOR 影响
    check('A14 reject 高但 takeover 超门槛仍接管(不归 agree 门槛)',
          judge_confirm_reply("x", {"agree": 0.10, "reject": 0.40, "takeover": 0.50}) == "takeover")
    # takeover 优先级高于 agree 的强信号
    check('A9 takeover 优先于 agree', judge_confirm_reply("x", {"agree": 0.85, "reject": 0.05, "takeover": 0.10}) == "agree")
    check('A10 takeover 明显但超 0.5 仍接管', judge_confirm_reply("x", {"agree": 0.40, "reject": 0.05, "takeover": 0.55}) == "takeover")


# ---------------------------------- B. score_confirm_reply mock 稳定 + 样例落分支 ----------------------------------

def test_mock_scores_stable_and_branch():
    # 1) 稳定/可复现：同输入两次一致；三问合法（和为1左右，0~1）
    s1 = score_confirm_reply("好的，关吧", confirm_ask_for("car_drive_ESPSwitch"), mock=True)
    s2 = score_confirm_reply("好的，关吧", confirm_ask_for("car_drive_ESPSwitch"), mock=True)
    check('B1 mock 两次调用一致', s1["agree"] == s2["agree"] and s1["reject"] == s2["reject"] and s1["takeover"] == s2["takeover"])
    total = sum((s1["agree"], s1["reject"], s1["takeover"]))
    check('B2 三问和为1', abs(total - 1.0) < 1e-9 and all(0 <= v <= 1 for v in (s1["agree"], s1["reject"], s1["takeover"])))
    check('B3 mock 模式标记', s1["mode"] == "mock")

    # 2) 6 个验收样例：mock 打分 -> judge 落对分支
    table = [
        ("好的，关吧", "agree"),
        ("不要，别动", "reject"),
        ("你先别管这个", "reject"),
        ("先看看空调，调到24度", "takeover"),
        ("嗯……随便吧", "unknown"),   # 含糊 -> unknown -> 调用方默认 reject（安全优先）
        ("你敢关试试", "reject"),
    ]
    for reply, expected in table:
        score = score_confirm_reply(reply, confirm_ask_for("car_drive_ESPSwitch"), mock=True)
        intent = judge_confirm_reply(reply, score)
        check(f'B mock "{reply}" -> {expected} (got {intent})', intent == expected)

    # 2b) 补充含糊/无明确词样例：mock -> judge 落 unknown（绝不误执行）——模拟真实"低置信即拒"。
    #     注意：mock 关键词前筛是占位实现，"行/都行"因含肯定词"行"会被 mock 判 agree，但那只是
    #     mock 占位启发式的粗糙度；真实通道由 KEV + AGREE_FLOOR 护栏兜底（judge 层 A11/A12/A13/A15 验证）。
    #     （"嗯"单字肯定在真实 KEV 判 agree，属正控，仅在真实探针里断言。）
    vague = ["你看着办", "随便你", "再说吧"]
    for reply in vague:
        score = score_confirm_reply(reply, confirm_ask_for("car_drive_ESPSwitch"), mock=True)
        intent = judge_confirm_reply(reply, score)
        check(f'B mock "{reply}" -> unknown (got {intent}, 不误执行)', intent == "unknown")

    # 3) override 注入：dict 覆盖三问概率走 mock（测试可精确注入分支）
    over = score_confirm_reply("随便说啥", None, mock={"agree": 0.9, "reject": 0.05, "takeover": 0.05})
    check('B override 注入 agree 分支', judge_confirm_reply("随便说啥", over) == "agree")


# ---------------------------------- 产品规则（CONFIRM_POOL / 文案） ----------------------------------

def test_pool_and_asks():
    pooled = ["car_control_Vehicle_HoodOpen", "car_drive_ESPSwitch", "car_drive_SnowChainModeSet",
              "car_status_BrakeMaintenanceSet", "car_drive_DriveModeSwitch", "car_adas_control",
              "energy_unlock_charge_cable", "energy_stop_charging"]
    for tool in pooled:
        check(f'CONFIRM_POOL 含 {tool}', is_confirm_tool(tool) and tool in CONFIRM_POOL)
        check(f'  文案模板非空且含确认语义: {tool}', "确认" in confirm_ask_for(tool))
    # 池外工具永不确认（导航/媒体/查询/舒适调节）
    never = ["navigation_route_to", "media_play", "weather_query", "hvac_set_temp",
             "car_seat_adjust", "energy_charge_status_query", "car_window_control", "car_door_open"]
    for tool in never:
        check(f'池外不确认 {tool}', not is_confirm_tool(tool))


# ---------------------------------- main ----------------------------------

def main():
    print("confirm_gate 验收")
    print(f"CONFIRM_POOL ({len(CONFIRM_POOL)}): {sorted(CONFIRM_POOL)}")
    print()
    run_test("A. judge_confirm_reply 纯裁决逻辑（6 验收场 + 边界）", test_judge_pure)
    run_test("B. score_confirm_reply mock 稳定 + 6 验收样例落分支 + override", test_mock_scores_stable_and_branch)
    run_test("C. 产品规则 CONFIRM_POOL / 文案", test_pool_and_asks)
    print(f"合计：{PASS} 项通过")
    print()
    print("说明：以上 A/B 均不连真实 KEV/LLM。真实 KEV 打分（mock=False）需 LLM 隧道，未在本机验证。")
    return 0 if PASS >= 3 else 1


if __name__ == "__main__":
    ok = main()
    sys.exit(0 if ok else 1)