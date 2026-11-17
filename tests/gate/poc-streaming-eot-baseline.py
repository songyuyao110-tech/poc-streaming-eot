"""单席 A0 基线：20 轮「最后一字 → CONFIRMED」实测（真判停服务，工具/决策为假实现）。

双席验收（A1）的分母必须是同一台机器、同一语料、同一 `score` 口径下量出来的单席基线；
旧的 703ms / 773ms 是 `margin` 策略 + 旧 judge 的数字，替换后不可比（见 wiki [[双路ASR]] §6）。

做法：从 `src/fixtures/streaming-prefix-cases.jsonl` 取 20 句中文，按语料自带的累进前缀
每 150ms 喂一次 ASR（最后一个前缀标 final），再喂一次 `speech_end`，跑真实 `TurnSession`：
判停走 `score_eot`（`EOT_JUDGE_BASE`，默认 127.0.0.1:8009），选工具/决策注入固定假实现。
每轮取 `summary.t_confirm_latency_ms`（t_confirmed − t_last_word），最后给 p50 / p90 / max。

⚠️ 口径变更（2026-11-04）：本脚本定义时的 A0 基线是**带窗**口径（t_confirmed 恒 ≥
`confirm_silence_ms=700`）。用户同日拍板撤掉「强 complete 也等 700ms」的整段确认窗
（[[无确认窗流程]] §1.1），强 complete 现在判完当刻认（`confirm_reasons` 里出现 `eot_complete`），
所以**今天跑出来的是去窗后的数，与历史 A0（858.7ms 等）不可比**；A0/A1 是否重锚、何时重锚待 P3-1，
本脚本不产出新基线，也不要拿它和历史带窗数混比。

运行（需要判停隧道：`curl 127.0.0.1:8009/health` 返回 ok）：

    python3 -B tests/poc-streaming-eot-baseline.py [--rounds 20] [--prefix-interval 150]
    python3 -B tests/poc-streaming-eot-baseline.py --smoke      # 2 轮冒烟，不写报告

产出：`/Users/YUYSONG/Work/tmp/poc-dual-asr-c0-baseline.json`（路径可用 `--out` 改）。
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from gate.turn_eot import EOT_JUDGE_BASE  # noqa: E402
from gate.turn_session import TurnConfig, TurnSession  # noqa: E402

CASES = ROOT / "src/fixtures/streaming-prefix-cases.jsonl"
DEFAULT_OUT = Path("/Users/YUYSONG/Work/tmp/poc-dual-asr-c0-baseline.json")
SCHEMA = {"name": "climate_set_power", "description": "开启或关闭座舱空调系统",
          "parameters": {"type": "object", "properties": {"enabled": {"type": "boolean"}},
                         "required": ["enabled"], "additionalProperties": False}}


def select(text, tool_set, context):
    """假选工具：一个固定短名单，立刻返回（基线只量判停，不量 KEV 选工具）。"""
    return {"scores": {"tool:climate_set_power": 0.95}, "selected": ["climate_set_power"],
            "schemas": [SCHEMA], "timing": {"stage1_ms": 0.0, "stage2_ms": 0.0, "total_ms": 0.0},
            "native_schemas": [SCHEMA], "domains": []}


def decide(utterance, selected, schemas, tool_set="core_score", context=None, **kwargs):
    return {"decision": {"kind": "tool_call", "tool": "climate_set_power",
                         "arguments": {"enabled": True}},
            "llm_ms": 5.0, "log_fields": {"decision_head": "baseline_mock"}}


def sentences(limit=None):
    """按 external_id 聚合中文前缀集，取前 `limit` 句（确定性顺序：语料出现顺序）。"""
    rows = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    grouped = {}
    order = []
    for row in rows:
        if row.get("lang") != "zh":
            continue
        key = row["external_id"]
        if key not in grouped:
            grouped[key] = []
            order.append(key)
        grouped[key].append(row)
    picked = []
    for key in order:
        items = sorted(grouped[key], key=lambda item: item["idx"])
        full = [item for item in items if item.get("is_full")]
        if len(items) < 4 or not full:
            continue
        picked.append({"external_id": key, "full_text": full[-1]["full_text"],
                       "prefixes": [item["prefix"] for item in items]})
        if limit and len(picked) >= limit:
            break
    return picked


def run_round(sentence, prefix_interval_ms, budget_ms=4000):
    """一句一轮：累进前缀 + speech_end，返回该轮 summary 与关键事件计数。"""
    events = []
    # 默认值：eot_policy=score、0.80/0.70。confirm_silence_ms=700 现在只挂「可能说完」那一档，
    # 强 complete 当刻认（2026-11-04 去窗），所以这一列不再是带窗 A0 的分母。
    cfg = TurnConfig()
    session = TurnSession(cfg, events.append, select_tools_fn=select, decide_fn=decide,
                          evidence_root=None, session_id="a0-baseline")
    prefixes = sentence["prefixes"]
    began = time.monotonic() * 1000
    now = lambda: time.monotonic() * 1000 - began

    def wait_until(target):
        while now() < target and not session.turn_done:
            session.tick(now())
            time.sleep(0.002)

    for index, prefix in enumerate(prefixes):
        at = index * prefix_interval_ms
        wait_until(at)
        if session.turn_done:            # 前缀阶段就被确认（罕见）：不再喂
            break
        session.on_asr(at, prefix, index == len(prefixes) - 1)
    last_at = (len(prefixes) - 1) * prefix_interval_ms
    wait_until(last_at + prefix_interval_ms)
    if not session.turn_done:
        session.on_vad(now(), "speech_end")
    limit = now() + budget_ms
    while not session.turn_done and now() < limit:
        session.tick(now())
        time.sleep(0.002)
    summary = session.summary()
    session.close()
    errors = [event for event in events if event["event"] == "error"]
    confirms = [event for event in events
                if event["event"] == "state_change" and event["to"] == "CONFIRMED"]
    starts = [event for event in events if event["event"] == "llm_start"]
    return {"external_id": sentence["external_id"], "full_text": sentence["full_text"],
            "prefixes": len(prefixes), "turn_done": session.turn_done,
            "t_confirm_latency_ms": summary.get("t_confirm_latency_ms"),
            "t_last_word": summary.get("t_last_word"), "t_confirmed": summary.get("t_confirmed"),
            "eot_calls": summary.get("eot_calls"), "eot_skipped": summary.get("eot_skipped"),
            "errors": len(errors), "confirm_reasons": [event.get("why") for event in confirms],
            "llm_start_speculative": [bool(event.get("speculative")) for event in starts]}


def percentile(values, p):
    ordered = sorted(values)
    if not ordered:
        return None
    index = min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))
    return round(ordered[index], 1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=20)
    parser.add_argument("--prefix-interval", type=int, default=150)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--smoke", action="store_true", help="只跑 2 轮、不写报告")
    args = parser.parse_args()

    rounds = 2 if args.smoke else args.rounds
    picked = sentences(limit=rounds)
    if len(picked) < rounds:
        raise SystemExit(f"语料里只凑到 {len(picked)} 句可用中文句，少于 {rounds}")

    print(f"判停端点：{EOT_JUDGE_BASE}/v1/systemone｜策略：score 0.80/0.70｜轮数：{len(picked)}")
    rows = []
    began = time.time()
    for index, sentence in enumerate(picked, 1):
        row = run_round(sentence, args.prefix_interval)
        rows.append(row)
        print(f"[{index}/{len(picked)}] {row['full_text']} → "
              f"confirm={row['t_confirm_latency_ms']}ms eot_calls={row['eot_calls']} "
              f"{row['confirm_reasons']}", flush=True)

    latencies = [row["t_confirm_latency_ms"] for row in rows
                 if isinstance(row["t_confirm_latency_ms"], (int, float))]
    seen_errors = sum(row["errors"] for row in rows)
    summary = {
        "rounds": len(rows),
        "confirmed": sum(bool(row["confirm_reasons"]) for row in rows),
        "eot_errors": seen_errors,
        "valid": seen_errors == 0 and all(row["confirm_reasons"] for row in rows),
        "policy": {"eot_policy": "score", "eot_complete_min": 0.80, "eot_confidence_min": 0.70},
        # 配置里的弱 complete 收敛窗；带窗 A0 的「整段确认窗」已按 2026-11-04 拍板撤掉，
        # 这里显式标出去窗口径，避免报告被读成历史 A0。
        "confirm_silence_ms": 700,
        "confirm_window": "strong_complete_confirms_immediately(2026-11-04); weak_complete_waits_confirm_silence_ms",
        "baseline_scope": "deconfirmed; not comparable with the windowed A0 (re-anchor pending P3-1)",
        "prefix_interval_ms": args.prefix_interval,
        "t_confirm_latency_ms": {
            "n": len(latencies),
            "p50": percentile(latencies, 50),
            "p90": percentile(latencies, 90),
            "max": round(max(latencies), 1) if latencies else None,
            "mean": round(statistics.mean(latencies), 1) if latencies else None,
        },
        "elapsed_s": round(time.time() - began, 1),
        "endpoint": EOT_JUDGE_BASE + "/v1/systemone",
    }
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    if not args.smoke:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({"summary": summary, "rows": rows}, ensure_ascii=False,
                                       indent=1) + "\n", encoding="utf-8")
        print("json:", args.out)
    if not summary["valid"]:
        raise SystemExit("基线无效：出现判停错误或某轮未确认（先查隧道与 /health）")


if __name__ == "__main__":
    main()
