"""交互层用例集 runner：加载 tests/confirm_cases.json，对每条跑 judge + mock/override/kev 打分。

三层校验（对应 docs/test-design-confirmation-v2.md §测试执行方法）：
  L1 mock 启发式   score_confirm_reply(mock=True) -> judge  对比 expected_mock_intent
                  （expected_mock_intent 为 null 时该条跳过比较，报 N/A；mock 启发式无足够证据）
  L2 judge 注入    score_confirm_reply(mock=<score_override>) -> judge  对比 expected_kev_intent
                  （用真实校准分样本验证裁决逻辑把合理分数映射到正确分支，不依赖打分通道）
  L3 真实 KEV     mock=False -> judge  对比 expected_kev_intent（需隧道 :8010，--mode kev 才跑）

本脚本可独立运行，不依赖 LLM/KEV 隧道（默认只跑 L1+L2，全离线、确定、可复现）。

运行：
  python3 -B tests/run_confirm_cases.py                 # L1+L2 离线
  python3 -B tests/run_confirm_cases.py --mode kev      # 加 L3 真实 KEV（需隧道）
  python3 -B tests/run_confirm_cases.py --only kev      # 仅 L3
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.dont_write_bytecode = True
import bootstrap  # noqa: E402
bootstrap.install()

from confirm_gate import judge_confirm_reply, score_confirm_reply  # noqa: E402

DATASET = Path(__file__).resolve().parent / "confirm_cases.json"


def load_cases():
    return json.loads(DATASET.read_text(encoding="utf-8"))["cases"]


def run_layer_mock(case):
    """L1：mock 启发式打分 -> judge。返回 None(跳过)或 (intent, matched)。"""
    scores = score_confirm_reply(case["user_reply"], mock=True)
    intent = judge_confirm_reply(case["user_reply"], scores)
    expected = case.get("expected_mock_intent")
    if expected is None:
        return intent, None  # mock 无足够证据，跳过比较
    return intent, intent == expected


def run_layer_override(case):
    """L2：注入真实校准分样本 -> judge。对比 expected_kev_intent。"""
    override = case.get("score_override")
    if not override:
        intent = None
        matched = None
    else:
        scores = score_confirm_reply(case["user_reply"], mock=dict(override))
        intent = judge_confirm_reply(case["user_reply"], scores)
        matched = intent == case["expected_kev_intent"]
    return intent, matched


def run_layer_kev(case):
    """L3：真实 KEV 打分 -> judge。对比 expected_kev_intent。隧道需 :8010。"""
    try:
        scores = score_confirm_reply(case["user_reply"], mock=False)
    except Exception as error:  # 隧道不可达等
        return None, None, f"{type(error).__name__}: {error}"
    intent = judge_confirm_reply(case["user_reply"], scores)
    return intent, intent == case["expected_kev_intent"], scores.get("mode")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--only", choices=("mock", "override", "kev"), default=None)
    ap.add_argument("--mode", choices=("offline", "kev", "all"), default="offline",
                    help="offline=L1+L2；kev=L1+L2+L3；all=同 kev")
    args = ap.parse_args()
    do_mock = args.only in (None, "mock")
    do_override = args.only in (None, "override")
    do_kev = args.only == "kev" or (args.only is None and args.mode in ("kev", "all"))

    cases = load_cases()
    print(f"交互层用例集 runner：{len(cases)} 条  (dataset=tests/confirm_cases.json)")
    print(f"  L1 mock启发式  : {'ON' if do_mock else 'OFF'}")
    print(f"  L2 judge注入   : {'ON' if do_override else 'OFF'}")
    print(f"  L3 真实KEV     : {'ON(需隧道 :8010)' if do_kev else 'OFF'}")
    print("=" * 100)

    stats = {"L1": {"pass": 0, "skip": 0, "fail": 0},
             "L2": {"pass": 0, "skip": 0, "fail": 0},
             "L3": {"pass": 0, "skip": 0, "fail": 0}}
    for case in cases:
        cid = case["id"]
        line = [f"{cid:<6} {case['user_reply']!r:<26} target[{case['expected_kev_intent']}]"]
        # L1
        if do_mock:
            intent, m = run_layer_mock(case)
            if m is None:
                s1 = f"L1 mock={intent}  N/A(启发式无据)"
                stats["L1"]["skip"] += 1
            elif m:
                s1 = f"L1 mock={intent}  ok"
                stats["L1"]["pass"] += 1
            else:
                s1 = f"L1 mock={intent}  **FAIL**(exp {case['expected_mock_intent']})"
                stats["L1"]["fail"] += 1
            line.append(s1)
        else:
            line.append("L1 off")
        # L2
        if do_override:
            intent, m = run_layer_override(case)
            if m is None:
                s2 = "L2 inject  N/A"
                stats["L2"]["skip"] += 1
            elif m:
                s2 = f"L2 inject={intent}  ok"
                stats["L2"]["pass"] += 1
            else:
                s2 = f"L2 inject={intent}  **FAIL**(exp {case['expected_kev_intent']})"
                stats["L2"]["fail"] += 1
            line.append(s2)
        else:
            line.append("L2 off")
        # L3
        if do_kev:
            intent, m, mode = run_layer_kev(case)
            if m is None:
                s3 = f"L3 kev  {mode}  N/A({intent})"
                stats["L3"]["skip"] += 1
            elif m:
                s3 = f"L3 kev={intent}  ok"
                stats["L3"]["pass"] += 1
            else:
                s3 = f"L3 kev={intent}  **FAIL**(exp {case['expected_kev_intent']})"
                stats["L3"]["fail"] += 1
            line.append(s3)
        else:
            line.append("L3 off")
        print("  " + "  |  ".join(line))

    print("=" * 100)
    print("汇总：")
    for layer in ("L1", "L2", "L3"):
        s = stats[layer]
        total = sum(s.values())
        print(f"  {layer}: pass={s['pass']}  skip/N/A={s['skip']}  fail={s['fail']}  (checked {s['pass']+s['fail']}/{total})")
    hard_fail = stats["L2"]["fail"]  # judge(override) 是纯逻辑验收，不能错
    print("\n结论：judge_inject(L2) 是纯逻辑验收主证据（不依赖打分通道），fail>0 即判定逻辑需修。")
    return 1 if hard_fail else 0


if __name__ == "__main__":
    sys.exit(main())