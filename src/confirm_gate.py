"""对话式二次确认闸门：high-risk tool -> 确认请求 -> KEV 解读第二轮用户回复意图。

流程叠加在"select_tools/decide 选出 tool_call 之后、执行之前"：
  1. 第一轮 decide 返回 tool_call 且工具 ∈ CONFIRM_POOL -> 进入 CONFIRM_WAIT，开口确认。
  2. 第二轮用户回复到达（先经 score_eot 判"说完没"）-> score_confirm_reply() 打三问分。
  3. judge_confirm_reply() 按优先级裁决 agree / reject / takeover / unknown（拿不准默认 reject）。

仿照 turn_eot.py 的判停三问范式（questions = {name: {"type":"noul","instructions":...}}、
state=json.dumps、kev_call(client, state, questions, base) + probability()），新增一套
"确认意图三问"（agree / reject / takeover）。唯一差异是问题集合与裁决逻辑（先判 takeover 优先级）。

mock 通道：本模块的 KEV 打分在 mock=True 时**完全不调用真实 KEV**，返回可配置/按关键词的
稳定分数，使判定逻辑可以在不连 LLM 隧道的情况下被验证。真实 KEV 打分需连 LLM 复验。
"""
from __future__ import annotations

import json
import math
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True

from core_pipeline_run import KEV_EOT_BASE, KEV_EOT_CLIENT, kev_call, probability
from turn_session import normalize  # 复用轻量文本规整（去全半角标点），仅用于 mock 关键词前筛

# 3 个"确认意图"问题标签，顺序即裁决优先级参考（takeover > agree/reject）。
CONFIRM_LABELS = ("agree", "reject", "takeover")
CONFIRM_ORDER = ("agree", "reject", "takeover")

# ------------------------------- 产品规则（非 KEV） -------------------------------

# 待确认工具池：只对这些工具触发二次确认。全部来自 vehicle_control / adas / energy。
# 进池原则见 docs/spec-kev-confirmation-v2.md §1.2（影响行车安全 或 物理不可逆/需人工恢复 的高危动作）。
CONFIRM_POOL = frozenset({
    "car_control_Vehicle_HoodOpen",
    "car_drive_ESPSwitch",
    "car_drive_SnowChainModeSet",
    "car_status_BrakeMaintenanceSet",
    "car_drive_DriveModeSwitch",
    "car_adas_control",
    "energy_unlock_charge_cable",
    "energy_stop_charging",
})

# 每个工具的确认文案模板（第一轮 agent 开口）。通用模板："我准备[操作]，这会[影响]，确认执行吗？"
CONFIRM_ASKS = {
    "car_control_Vehicle_HoodOpen": "我准备打开引擎盖，打开后需要手动才能关闭，确认执行吗？",
    "car_drive_ESPSwitch": "我准备关闭ESP，这会降低车身稳定控制，确认执行吗？",
    "car_drive_SnowChainModeSet": "我准备开启防滑链模式，这会限制车速，确认执行吗？",
    "car_status_BrakeMaintenanceSet": "我准备开启制动盘保养，这会直接作用于制动系统，确认执行吗？",
    "car_drive_DriveModeSwitch": "我准备切换到激进驾驶模式，会影响操纵特性，确认执行吗？",
    "car_adas_control": "我准备关闭驾驶辅助功能，这会降低辅助安全能力，确认执行吗？",
    "energy_unlock_charge_cable": "我准备解锁充电枪，这会断开充电物理连接，确认执行吗？",
    "energy_stop_charging": "我准备停止当前充电，确认执行吗？",
}
# 池外工具或未知工具的兜底确认文案。
_FALLBACK_ASK = "我准备执行该操作，这会带来一定影响，确认执行吗？"


def is_confirm_tool(tool_name) -> bool:
    """产品规则：该工具是否需要二次确认。池外永不确认。"""
    return tool_name in CONFIRM_POOL


def confirm_ask_for(tool_name) -> str:
    """取某待确认工具的确认文案；未知/池外工具退回通用文案。"""
    return CONFIRM_ASKS.get(tool_name, _FALLBACK_ASK)


# ------------------------------- KEV 确认意图三问 -------------------------------

# KEV noul 打分当前配置：复用 EOT 的钱与隧道（意图判定类，与 242 工具打分分离）。真实模式用。
_CONFIRM_QUESTIONS = {
    "agree": {
        "type": "noul",
        "instructions": "用户明确同意/确认执行该待确认的操作？",
    },
    "reject": {
        "type": "noul",
        "instructions": "用户明确拒绝/不同意执行该待确认的操作（含“先别管/弹掉”这类显式撤回）？",
    },
    "takeover": {
        "type": "noul",
        "instructions": "用户转移话题或提出新的独立请求（如“先看看空调”“导航去公司”）而不是在回应本次确认？",
    },
}

# 裁决阈值（可在 mock 前用样例集校准）。
TAKEOVER_THRESHOLD = 0.5      # p_takeover 达到此值 -> 直接接管
CLEAR_MARGIN = 0.15           # 正负(p_reject-p_agree 或 p_agree-p_reject)差距达到此值才判 agree/reject
# "含糊即拒"护栏：判 agree（唯一会触发执行的支路）除需正负差达到 CLEAR_MARGIN 外，还必须
# p_agree 绝对分达到 AGREE_FLOOR。原因：真实 KEV 对明确肯定给 ~0.97+，而对含糊/把决定推回来
# 的回复（"嗯…随便吧"=0.54、"你看着办"=0.51、"随便你"=0.71）agree 或最高或偏高、正负差也常>0.15，
# 却绝对分不达标、置信不足，绝不能当 agree 执行。低于下限即落 unknown（安全优先默认 reject）。
# 真实样例校准（KEV @ 本机隧道）：
#    明确同意 cluster:  好的，关吧 0.975 / "嗯" 0.977  >= 0.75 -> agree
#    含糊/推责 cluster: 嗯…随便吧 0.539 / 你看着办 0.512 / 随便你 0.711  < 0.75 -> unknown
# 对不可逆/影响行车安全的高危动作宁低勿高（宁可少做、不可误做）。
AGREE_FLOOR = 0.75           # 判 agree 的 p_agree 绝对下限（>0.75 才算“明确肯定”）


def _scored_reply_keywords(user_reply: str):
    """mock 打分用的低成本关键词前筛（仅供 mock 通道，不是最终裁决依据）。

    返回 (agree 信号数, reject 信号数, takeover 信号数)。只用于让 mock 分数对验收样例
    稳定、可复现，绝不作为真实模式的裁决。
    """
    text = normalize(user_reply)
    agree = ("好", "确认", "执行", "可以", "行", "弄", "这样", "同意", "开吧")
    reject = ("不", "别", "取消", "不要", "关掉", "算了", "先别管", "弹掉", "等等", "敢")
    takeover = ("空调", "导航", "音乐", "播放", "看看", "查", "去哪", "公司", "回家", "新")
    return (sum(1 for k in agree if k in text),
            sum(1 for k in reject if k in text),
            sum(1 for k in takeover if k in text))


def _mock_scores(user_reply: str, confirm_ask, override):
    """构造一个稳定、可复现的三问分数（0~1，和为 1），映射已知意图短语到对应分支。

    - 若调用方传了 override（dict with keys agree/reject/takeover），直接用它（测试注入分支）。
    - 否则按关键词给默认分支一个明显优势分，使 judge 走对分支。
    """
    if override is not None:
        values = {name: float(override.get(name, 0.0)) for name in CONFIRM_ORDER}
        total = sum(values.values())
        if not (total > 0) or any(not math.isfinite(v) or v < 0 for v in values.values()):
            raise ValueError("confirm mock 概率必须为非负有限数且至少一个为正")
        return {name: v / total for name, v in values.items()}
    a, r, t = _scored_reply_keywords(user_reply)
    if t > 0 and t >= a and t >= r:
        return {"agree": 0.05, "reject": 0.10, "takeover": 0.85}      # 接管：转移话题
    if r > a:
        return {"agree": 0.05, "reject": 0.88, "takeover": 0.07}      # 拒绝（含负情绪）
    if a > r:
        return {"agree": 0.88, "reject": 0.05, "takeover": 0.07}      # 同意
    # 含糊（如"嗯……随便吧"）：三问都不突出 -> judge 落 unknown -> 默认 reject
    return {"agree": 0.30, "reject": 0.35, "takeover": 0.35}


def score_confirm_reply(user_reply: str, confirm_ask: str | None = None, *, mock=None):
    """确认意图三问打分，结构 {"agree": p, "reject": p, "takeover": p}（模仿 score_eot）。

    参数：
      user_reply  第二轮用户回复（已通过 EOT 判停判为"说完"的完整句）。
      confirm_ask 第一轮确认文案（作为上下文 reference，让 KEV 知道用户在回应什么）。
      mock        True  -> 走 mock 通道（不连 LLM，返回稳定可复现分数）。
                  dict  -> 等价于 mock=True 且覆盖三问概率（键 agree/reject/takeover，测试注入用）。
                  None  -> 真实 KEV 打分，复用 EOT 的 client/base（需 LLM 隧道）。
    返回 dict：{"agree": ..., "reject": ..., "takeover": ..., "mode": ...}
    """
    if mock is not None and isinstance(mock, dict):
        override, use_mock = mock, True
    else:
        override, use_mock = None, bool(mock)

    if use_mock:
        scores = _mock_scores(user_reply, confirm_ask, override)
        return {**scores, "mode": "mock"}

    state = json.dumps({
        "confirm_ask": confirm_ask or "",
        "user_reply": user_reply,
        "questions": CONFIRM_ORDER,
    }, ensure_ascii=False, allow_nan=False)
    began = time.monotonic()
    raw = kev_call(KEV_EOT_CLIENT, state, _CONFIRM_QUESTIONS, base=KEV_EOT_BASE)
    wall_ms = (time.monotonic() - began) * 1000
    answers = raw["answers"]
    scores = {}
    for name in CONFIRM_ORDER:
        answer = answers[name]
        if (not isinstance(answer, dict) or set(answer) - {"type", "noul"}
                or answer.get("type", "noul") != "noul"):
            raise ValueError("Malformed KEV confirm Noul")
        scores[name] = probability(answer.get("noul"))
    return {**scores, "mode": "kev", "wall_ms": wall_ms,
            "kev_ms": raw.get("latency_ms"), "confirm_ask": confirm_ask}


def judge_confirm_reply(user_reply, scores) -> str:
    """裁决逻辑（非裸 argmax）。返回 agree / reject / takeover / unknown。

    优先级：
      1. p_takeover >= 0.5                    -> takeover（兼顾：没在回应确认）
      2. p_reject - p_agree >= 0.15           -> reject（显式拒绝/撤回/负情绪）
      3. p_agree  - p_reject >= 0.15 且 p_agree >= AGREE_FLOOR  -> agree（明确肯定，
           还需绝对分达标——"含糊即拒"护栏，防止含糊回复靠微弱正差被当成同意执行）
      4. else                                 -> unknown（拿不准，调用方默认 reject）
    user_reply 用于信息/日志，不参与裁决（三问概率已由 KEV/mock 给出）。
    """
    take = {name: float(scores[name]) for name in ("agree", "reject", "takeover")}
    if take["takeover"] >= TAKEOVER_THRESHOLD:
        return "takeover"
    if take["reject"] - take["agree"] >= CLEAR_MARGIN:
        return "reject"
    if take["agree"] - take["reject"] >= CLEAR_MARGIN and take["agree"] >= AGREE_FLOOR:
        return "agree"
    return "unknown"


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reply", default="好的，关吧")
    parser.add_argument("--tool", default="car_drive_ESPSwitch")
    parser.add_argument("--mock", action="store_true")
    parser.add_argument("--scores", default=None, help='JSON dict {"agree":..,"reject":..,"takeover":..} 注入三问概率')
    args = parser.parse_args()
    override = json.loads(args.scores) if args.scores else None
    scores = score_confirm_reply(args.reply, confirm_ask_for(args.tool),
                                 mock=override if args.scores else args.mock)
    intent = judge_confirm_reply(args.reply, scores)
    print(json.dumps({"user_reply": args.reply, "confirm_tool": args.tool,
                      "confirm_ask": confirm_ask_for(args.tool), "scores": scores,
                      "intent": intent}, ensure_ascii=False))