"""量「决策落地 → 回合收口」的那段尾巴（turn tail）。

一次真实链路回放：真判停（8009）+ 真两阶段选工具 + 真 LiteLLM 决策，脚本化一条 ASR，
记录 `llm_decision`（决策已落地）到 `turn_done`（回合关闭）之间的毫秒数。

    python -B tests/measure_turn_tail.py                 # 现行默认
    python -B tests/measure_turn_tail.py --legacy        # 复现改动前的 2s 尾巴
    python -B tests/measure_turn_tail.py --text 导航回家 --out evidence/turn-tail/default.json

`--legacy` 只对旧版（无 `close_turn_on_decision` 字段）与新版都成立：旧版本来就是这个行为，
新版显式把它设成 False。判停服务不可达时直接退出码 3，不伪造数字。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from app import bootstrap

bootstrap.install()          # 上游仓库根
sys.dont_write_bytecode = True

from core_pipeline_run import TOOL_SET, decide, select_tools
# noqa: E402
from gate.turn_eot import EOT_JUDGE_BASE, score_eot
# noqa: E402
from gate.turn_session import TurnConfig, TurnSession
# noqa: E402
PREFIX_MS = 150              # 逐字 partial 间隔（与双席用例回放器同口径）
BUDGET_MS = 30000


def judge_ready() -> bool:
    try:
        import httpx2
        resp = httpx2.get(EOT_JUDGE_BASE.rstrip("/") + "/health", timeout=3.0, trust_env=False)
        return resp.status_code == 200 and json.loads(resp.content).get("status") == "ok"
    except Exception:  # noqa: BLE001 - 探活失败就按不可达处理
        return False


def timeline(text):
    """逐字 partial + 末片 final + speech_end，绝对毫秒。"""
    items = [(0, "vad", "speech_start")]
    for index in range(1, len(text) + 1):
        items.append((index * PREFIX_MS, "asr", text[:index], index == len(text)))
    items.append((len(text) * PREFIX_MS + 20, "vad", "speech_end"))
    return items


def run(text, legacy):
    cfg = TurnConfig()
    if hasattr(cfg, "close_turn_on_decision"):
        # 旧版没有这个字段：legacy 与否都是「等 max_silence」的老行为。
        cfg.close_turn_on_decision = not legacy
    events = []
    session = TurnSession(cfg, events.append, eot_fn=score_eot,
                          select_tools_fn=select_tools, decide_fn=decide,
                          session_id="tail-" + ("legacy" if legacy else "now"),
                          evidence_root=None)
    plan = timeline(text)
    began = time.monotonic() * 1000
    while time.monotonic() * 1000 - began < BUDGET_MS and not session.turn_done:
        now = time.monotonic() * 1000 - began
        while plan and plan[0][0] <= now:
            at, kind, payload, *rest = plan.pop(0)
            if kind == "asr":
                session.on_asr(at, payload, bool(rest[0]))
            else:
                session.on_vad(at, payload)
        session.tick(now)
        time.sleep(0.005)
    # 判停/决策都在线程池里：turn_done 之后再 tick 一次收尾（同一线程写状态，符合约束）
    session.tick(time.monotonic() * 1000 - began)
    wall = round(time.monotonic() * 1000 - began, 1)
    done = next((event for event in events if event["event"] == "turn_done"), None)
    decisions = [event for event in events if event["event"] == "llm_decision"]
    result = {
        "text": text,
        "legacy_tail": bool(legacy),
        "close_turn_on_decision": getattr(cfg, "close_turn_on_decision", None),
        "turn_done": bool(done),
        "wall_ms": wall,
        "t_last_word": done.get("t_last_word") if done else None,
        "t_confirmed": done.get("t_confirmed") if done else None,
        "t_llm_start": done.get("t_llm_start") if done else None,
        "t_last_decision": done.get("t_last_decision") if done else None,
        "turn_done_t_ms": done.get("t_ms") if done else None,
        "tail_ms": (done["t_ms"] - done["t_last_decision"]
                    if done and done.get("t_last_decision") is not None else None),
        "word_to_decision_ms": (done["t_last_decision"] - done["t_last_word"]
                                if done and done.get("t_last_decision") is not None
                                and done.get("t_last_word") is not None else None),
        "llm_decisions": len(decisions),
        "consumed_count": done.get("consumed_count") if done else None,
        "llm_calls": done.get("llm_calls") if done else None,
        "llm_wasted": done.get("llm_wasted") if done else None,
        "decision": (decisions[-1].get("decision") if decisions else None),
    }
    session.close()
    return result


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--text", default="导航回家")
    ap.add_argument("--legacy", action="store_true", help="复现改前的 2s 尾巴（新版本里显式关掉 close_turn_on_decision）")
    ap.add_argument("--repeat", type=int, default=1)
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    if not judge_ready():
        print("判停服务不可达（%s/health 非 ok）：不伪造数字，退出码 3" % EOT_JUDGE_BASE)
        return 3
    results = [run(args.text, args.legacy) for _ in range(max(1, args.repeat))]
    payload = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
               "eot_judge_base": EOT_JUDGE_BASE, "tool_set": TOOL_SET, "prefix_ms": PREFIX_MS,
               "results": results}
    for item in results:
        print(json.dumps({key: item[key] for key in
                          ("text", "legacy_tail", "turn_done", "t_last_word", "t_confirmed",
                           "t_last_decision", "turn_done_t_ms", "tail_ms", "word_to_decision_ms",
                           "llm_decisions", "llm_wasted")}, ensure_ascii=False))
        print("   decision:", json.dumps(item["decision"], ensure_ascii=False))
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print("evidence:", out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
