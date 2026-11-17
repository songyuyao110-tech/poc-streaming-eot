"""A0 质量回归：中文 230 句前缀集 + 英文 heldout 56 句，全部走真实判停服务（`score` 口径）。

口径（wiki [[双路ASR]] §6 A0，`-refine-spec.md` §2.3）：
- 中文 230 句 / 2068 前缀：整句「说完」召回（最后一个前缀判 complete）≥ 97.8%；
  有非末前缀被判 complete 的句子 ≤ 75.7%（±1pp 容差）。数字必须用同一批语料、同一服务重测，
  不得引用 `margin` 时代的 97.4% / 62.1%。
- 英文 heldout（pipecat cases.json，56 条）：按新口径登记 argmax 与 `complete ≥ 0.80` 两种口径，
  **不与旧 judge 的 94.6% 混比**（不同模型、不同 instructions、不同判定口径）。

本脚本只采集与统计，不改判定；`complete/confidence` 两阈值取 `TurnConfig` 默认（0.80 / 0.70），
可用 `EOT_COMPLETE_MIN_SCORE` / `EOT_CONFIDENCE_MIN` 覆盖。

运行（需要判停隧道 `curl 127.0.0.1:8009/health`）：

    python3 -B tests/poc-streaming-eot-quality.py             # 全量（约 4 分钟）
    python3 -B tests/poc-streaming-eot-quality.py --smoke     # 只跑 60 个前缀 + 12 句英文

产出：`/Users/YUYSONG/Work/tmp/poc-dual-asr-c0-quality.json`（`--out` 可改）。
"""
from __future__ import annotations

import argparse
import collections
import json
import statistics
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from gate.turn_eot import EOT_JUDGE_BASE, score_eot
# noqa: E402
from gate.turn_session import TurnConfig
# noqa: E402
ZH_CASES = ROOT / "src/fixtures/streaming-prefix-cases.jsonl"
PIPECAT_CASES = Path("/Users/YUYSONG/Work/tmp/pipecat-turn-eval-v2/cases.json")
DEFAULT_OUT = Path("/Users/YUYSONG/Work/tmp/poc-dual-asr-c0-quality.json")


def effective_label(scores, complete_min, confidence_min):
    """与 `TurnSession._eot_verdict` 的 `score` 分支同一规则（这里只关心标签）。"""
    if scores["complete"] >= complete_min:
        return "complete", scores["confidence"] >= confidence_min
    return ("cut" if scores["cut"] >= scores["wait"] else "wait"), False


def percentile(values, p):
    ordered = sorted(values)
    if not ordered:
        return None
    index = min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))
    return round(ordered[index], 1)


def run_zh(limit, thresholds):
    rows = [json.loads(line) for line in ZH_CASES.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    zh = [row for row in rows if row.get("lang") == "zh"]
    if limit:
        zh = zh[:limit]
    out = []
    began = time.time()
    for index, row in enumerate(zh, 1):
        scores = score_eot(row["prefix"])
        label, may_start = effective_label(scores, *thresholds)
        out.append({"id": row["id"], "external_id": row["external_id"], "full_text": row["full_text"],
                    "prefix": row["prefix"], "idx": row["idx"], "is_full": row["is_full"],
                    "argmax": scores["label"], "effective": label, "may_start_early": may_start,
                    "complete": scores["complete"], "cut": scores["cut"], "wait": scores["wait"],
                    "confidence": scores["confidence"], "wall_ms": scores["wall_ms"]})
        if index % 250 == 0:
            print(f"  zh {index}/{len(zh)}", flush=True)

    by_sentence = collections.defaultdict(list)
    for row in out:
        by_sentence[row["external_id"]].append(row)

    def sentence_stats(field):
        sentences = any_nonfinal = 0
        full_total = full_complete = 0
        for items in by_sentence.values():
            items = sorted(items, key=lambda item: item["idx"])
            sentences += 1
            if any(item[field] == "complete" for item in items if not item["is_full"]):
                any_nonfinal += 1
            for item in items:
                if item["is_full"]:
                    full_total += 1
                    full_complete += item[field] == "complete"
        return {"sentences": sentences, "sentences_with_any_nonfinal_complete": any_nonfinal,
                "any_nonfinal_rate": round(any_nonfinal / sentences, 4) if sentences else None,
                "full_utterances": full_total, "full_judged_complete": full_complete,
                "recall_full_rate": round(full_complete / full_total, 4) if full_total else None}

    walls = [row["wall_ms"] for row in out]
    return {"n_prefixes": len(out), "sentences": len(by_sentence),
            "argmax": sentence_stats("argmax"), "score_gate": sentence_stats("effective"),
            "latency_ms": {"n": len(walls), "p50": percentile(walls, 50),
                           "p90": percentile(walls, 90), "max": round(max(walls), 1)},
            "elapsed_s": round(time.time() - began, 1), "rows": out}


def last_agent_reply(case):
    for turn in reversed(case.get("context") or []):
        if turn.get("role") == "assistant":
            return turn.get("content") or ""
    return ""


def run_en(limit, thresholds):
    cases = json.loads(PIPECAT_CASES.read_text(encoding="utf-8"))
    if limit:
        cases = cases[:limit]
    rows = []
    began = time.time()
    for index, case in enumerate(cases, 1):
        scores = score_eot(case["user"], last_agent_reply(case))
        label, may_start = effective_label(scores, *thresholds)
        rows.append({"id": case["id"], "heldout": bool(case.get("heldout", False)),
                     "gold": case["expected"], "argmax": scores["label"], "effective": label,
                     "may_start_early": may_start, "complete": scores["complete"],
                     "confidence": scores["confidence"], "wall_ms": scores["wall_ms"]})
        if index % 40 == 0:
            print(f"  en {index}/{len(cases)}", flush=True)

    def binary(subset):
        n = len(subset)
        gold_complete = [row for row in subset if row["gold"] == "complete"]
        gold_incomplete = [row for row in subset if row["gold"] != "complete"]
        argmax_ok = sum((row["argmax"] == "complete") == (row["gold"] == "complete") for row in subset)
        gate_ok = sum((row["effective"] == "complete") == (row["gold"] == "complete") for row in subset)
        return {"n": n, "argmax_correct": argmax_ok,
                "argmax_rate": round(argmax_ok / n, 4) if n else None,
                "score_gate_correct": gate_ok,
                "score_gate_rate": round(gate_ok / n, 4) if n else None,
                "gold_complete": {"n": len(gold_complete),
                                  "argmax_complete": sum(r["argmax"] == "complete"
                                                         for r in gold_complete),
                                  "gate_complete": sum(r["effective"] == "complete"
                                                       for r in gold_complete)},
                "gold_incomplete": {"n": len(gold_incomplete),
                                    "argmax_false_complete": sum(r["argmax"] == "complete"
                                                                 for r in gold_incomplete),
                                    "gate_false_complete": sum(r["effective"] == "complete"
                                                               for r in gold_incomplete)}}

    walls = [row["wall_ms"] for row in rows]
    return {"n": len(rows), "all": binary(rows),
            "heldout": binary([row for row in rows if row["heldout"]]),
            "latency_ms": {"n": len(walls), "p50": percentile(walls, 50),
                           "p90": percentile(walls, 90), "max": round(max(walls), 1)},
            "elapsed_s": round(time.time() - began, 1), "rows": rows}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--smoke", action="store_true")
    parser.add_argument("--skip-en", action="store_true")
    args = parser.parse_args()

    cfg = TurnConfig()
    thresholds = (cfg.eot_complete_min, cfg.eot_confidence_min)
    print(f"判停端点：{EOT_JUDGE_BASE}/v1/systemone｜阈值：complete>={thresholds[0]} "
          f"confidence>={thresholds[1]}")
    summary = {"endpoint": EOT_JUDGE_BASE + "/v1/systemone",
               "policy": {"eot_policy": cfg.eot_policy, "eot_complete_min": thresholds[0],
                          "eot_confidence_min": thresholds[1]},
               "thresholds": {"complete": thresholds[0], "confidence": thresholds[1]}}
    summary["zh"] = run_zh(60 if args.smoke else None, thresholds)
    zh_rows = summary["zh"].pop("rows")
    if not args.skip_en:
        if not PIPECAT_CASES.is_file():
            raise SystemExit(f"缺英文语料：{PIPECAT_CASES}（可用 --skip-en 只跑中文）")
        summary["en"] = run_en(12 if args.smoke else None, thresholds) if args.smoke else run_en(None, thresholds)
        en_rows = summary["en"].pop("rows")
    else:
        en_rows = []

    print(json.dumps({key: value for key, value in summary.items()}, ensure_ascii=False, indent=1))
    if not args.smoke:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({"summary": summary, "zh_rows": zh_rows,
                                        "en_rows": en_rows}, ensure_ascii=False, indent=1) + "\n",
                            encoding="utf-8")
        print("json:", args.out)


if __name__ == "__main__":
    main()
