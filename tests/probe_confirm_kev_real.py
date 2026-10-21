"""真实 KEV 打分探针：对验收表 6 样例跑 score_confirm_reply（mock=False，需 EOT 隧道 :8010）。

这不替代 tests/test_confirm_gate.py 的离线判定验证；它验证"真实 KEV 三问打分 -> 同一 judge"
在验收样例上落在哪一侧，用于校准 T_takeover / CLEAR_MARGIN。隧道不可达时给出清晰提示。

运行：python3 -B tests/probe_confirm_kev_real.py   （需 KEV EOT 隧道，见 README 前置）
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import bootstrap
bootstrap.install()
sys.dont_write_bytecode = True

from confirm_gate import confirm_ask_for, judge_confirm_reply, score_confirm_reply
from core_pipeline_run import KEV_EOT_BASE

# (第一轮工具, 第二轮回复, 产品预期 intent)
TABLE = [
    ("car_drive_ESPSwitch", "好的，关吧", "agree"),
    ("car_drive_ESPSwitch", "不要，别动", "reject"),
    ("car_drive_ESPSwitch", "你先别管这个", "reject"),
    ("car_control_Vehicle_HoodOpen", "先看看空调，调到24度", "takeover"),
    ("car_drive_ESPSwitch", "嗯……随便吧", "unknown"),
    ("car_drive_ESPSwitch", "你敢关试试", "reject"),
    # 补充"含糊即拒"校准样例：预期 unknown，安全优先、绝不误执行
    ("car_drive_ESPSwitch", "你看着办", "unknown"),
    ("car_drive_ESPSwitch", "随便你", "unknown"),
    # 正控：单字肯定词"嗯"（规范 §5.2 允许只说一个字）属明确同意，预期 agree（不能被误伤成 unknown）
    ("car_drive_ESPSwitch", "嗯", "agree"),
]


def main():
    print(f"真实 KEV 确认意图打分探针（KEV_EOT_BASE={KEV_EOT_BASE}）")
    print("按验收表：实判 vs 产品预期\n")
    rows = []
    for tool, reply, expected in TABLE:
        try:
            scores = score_confirm_reply(reply, confirm_ask_for(tool), mock=False)
            intent = judge_confirm_reply(reply, scores)
            hit = "Y" if intent == expected else "N"
            rows.append({"tool": tool, "reply": reply, "expected": expected,
                         "intent": intent, "scores": {k: round(v, 3) for k, v in scores.items()
                                                      if k in ("agree", "reject", "takeover")}, "hit": hit})
            print(f"  [{hit}] {reply:<16} expected={expected:<8} got={intent:<9} {scores['mode']}")
            for k in ("agree", "reject", "takeover"):
                print(f"         {k:<8}= {scores[k]:.3f}")
        except Exception as e:
            print(f"  [!!] {reply:<16} 打分失败: {type(e).__name__}: {e}")
            print("      （KEV 隧道未就绪？确认 src/core_pipeline_run.py 的 KEV_EOT_BASE 端口已开隧道）")
    hits = sum(1 for r in rows if r.get("hit") == "Y")
    print(f"\n命中 {hits}/{len(TABLE)}")
    print("注：表中 \"你先别管这个\" 期望显式为 reject，但真实 KEV 判 takeover——该 case 已获产品")
    print("    认可（用户\"先别管\"可理解为转去处理别的，判 takeover 没问题），不计为缺陷。")
    print("提示：此探针验证真实 KEV 打分端；若与预期不一致，说明 KEV 三问打分需要按样例校准阈值，")
    print("     判定逻辑本身由 tests/test_confirm_gate.py（纯函数+override）离线验证。")
    return 0


if __name__ == "__main__":
    sys.exit(main())