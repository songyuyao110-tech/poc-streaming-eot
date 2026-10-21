"""对话式二次确认的"编排/落地层"（叠加在 TurnSession 主循环之上）。

职责：把 confirm_gate 的纯函数（is_confirm_tool / confirm_ask_for / score_confirm_reply /
judge_confirm_reply）接成 TurnSession 可以直接调用的整体，并产出 4 个分支的可视化 payload。

TurnSession 在 `_apply_llm` 里拦截到一个池内高危工具的 tool_call 决策后，进入 CONFIRM_WAIT，
把第一轮决策挂起；收到第二轮用户回复（已由 EOT 判停判为"说完"）后，调用
`ConfirmGate.run(user_reply, confirm_ask, mock)` 得到 `score`/`intent`，再按四分支落地：

  - intent == "agree"   -> 执行待确认动作（mock），报 execute(action, tool)
  - intent == "reject"  -> 放弃当前动作，报 abandon
  - intent == "unknown" -> 安全优先，当作 reject 放弃，报 abandon(unknown)
  - intent == "takeover"-> 放弃当前动作，并把 user_reply 当新诉求，再走 select_tools/decide 一轮，
                           报 reroute(next_request, next_decision)

本模块是**纯编排**：不碰真实业务工具、不连 KEV/LLM（打分由注入的 mock/真实通道决定），
也不改变 TurnSession 的 segment/消费机制——真正封口/写回由 TurnSession 负责，
本模块只对"已经收敛的第二轮回文本"做意图判定并给出落地动作。

mock 通道说明：TurnConfirmGate 用 `mock=True`（默认）时回到 confirm_gate 的稳定 mock 打分，
不依赖 KEV 隧道；真实通道用 `mock=None`（需 EOT 隧道 :8010）。见 docs/spec-kev-confirmation-v2.md。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.dont_write_bytecode = True

from confirm_gate import confirm_ask_for, is_confirm_tool, judge_confirm_reply, score_confirm_reply


class TurnConfirmGate:
    """一次确认回合的编排句柄，把 confirm_gate 的纯函数封装成 TurnSession 可调用的一组回调。

    注入方式与 eot_fn/select_tools_fn/decide_fn 一致：TurnSession 负责在正确时机调用，
    本类只做意图判定与四分支落地，不改动分段/消费逻辑。
    """

    def __init__(self, *, mock=True, score_fn=score_confirm_reply,
                 judge_fn=judge_confirm_reply, is_pool_fn=is_confirm_tool,
                 ask_fn=confirm_ask_for):
        self.mock = mock                      # True -> mock 打分（离线）；None/False -> 真实 KEV
        self.score_fn = score_fn
        self.judge_fn = judge_fn
        self.is_pool_fn = is_pool_fn
        self.ask_fn = ask_fn

    # -- 第一轮：产品 gate -------------------------------------------------
    def ready(self, decision) -> bool:
        """第一轮决策是否需要二次确认：tool_call 且工具 ∈ 待确认池（产品规则，非 KEV）。"""
        decision = decision or {}
        return decision.get("kind") == "tool_call" and self.is_pool_fn(decision.get("tool"))

    def ask_for(self, decision) -> str:
        """第一轮确认文案（agent 开口）。"""
        return self.ask_fn((decision or {}).get("tool"))

    # -- 第二轮：KEV(或 mock) 意图判定 + 落地 -----------------------------
    def resolve(self, user_reply, decision, *, mock=None):
        """对已经判停收敛的第二轮回做意图判定并落地。

        返回 dict：
          {
            "user_reply": 第二轮回文本,
            "confirm_ask": 第一轮确认文案,
            "pending_tool": 待确认工具,
            "scores":  三问得分 + mode/wall_ms...
            "intent":  agree/reject/takeover/unknown,
            "action":  "execute" | "abandon" | "reroute",
            "message": 对 PM/UI 的人类可读说明,
          }
        """
        tool = (decision or {}).get("tool")
        confirm_ask = self.ask_for(decision)
        scores = self.score_fn(user_reply, confirm_ask, mock=(self.mock if mock is None else mock))
        intent = self.judge_fn(user_reply, scores)
        if intent == "agree":
            action, message = "execute", f"用户确认，执行(mock) {tool}"
        elif intent == "takeover":
            action, message = "reroute", f"用户转移话题，放弃 {tool}，把回复当新诉求处理"
        elif intent == "unknown":
            action, message = "abandon", f"意图不明(intent=unknown)，安全优先放弃，不执行 {tool}"
        else:  # reject
            action, message = "abandon", f"用户拒绝，放弃执行 {tool}"
        # 简化 payload：分数里去掉不需要的上下文键，保留判定用信息
        clean_scores = {
            k: scores.get(k) for k in ("agree", "reject", "takeover", "mode", "wall_ms", "kev_ms")
            if k in scores
        }
        return {
            "user_reply": user_reply,
            "confirm_ask": confirm_ask,
            "pending_tool": tool,
            "scores": clean_scores,
            "intent": intent,
            "action": action,
            "message": message,
        }


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--reply", default="好的，关吧")
    parser.add_argument("--tool", default="car_drive_ESPSwitch")
    parser.add_argument("--mock", action="store_true")
    args = parser.parse_args()

    gate = TurnConfirmGate(mock=True if args.mock else False)
    decision = {"kind": "tool_call", "tool": args.tool, "arguments": {}}
    print("需要确认:", gate.ready(decision))
    if gate.ready(decision):
        print("确认文案:", gate.ask_for(decision))
        result = gate.resolve(args.reply, decision)
        print("判定结果:", __import__("json").dumps(result, ensure_ascii=False))
    else:
        print("池外工具，无需确认，直接执行。")