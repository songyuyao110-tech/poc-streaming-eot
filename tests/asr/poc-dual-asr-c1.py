"""A1 双席回放：两席并行判停、互不重置/取消、文本不串席（C1）。

范围（用户 2026-11-03 拍板收口）：**两路干净 ASR 由前端给出** —— 两路 mic 的人声分离/隔离
与同句去重都不在本卡。Agent 侧只证明「不串席」：

1) 隔离 20 组：D 半句 → P 插一句完整句 → D 续句。判据：两席各自 CONFIRMED；
   `cross_cancel=0`、`cross_reset=0`；D 的文本不出现在 P 的决策输入（反之亦然）；
   每席「最后一字→CONFIRMED」p50 只记录（去窗后与带窗 A0 基线不可比，重锚待 P3-1；
   2026-11-04 去窗：强 complete 判完当刻认，why=eot_complete，旧的 confirm_silence 判据失效）。
2) 不拼接 10 组：D 说前半、P 说后半。判据：任何一席的决策文本都只含自己那一路的内容
   （两席会话/状态不混 ⇒ 不会把两个人的半句拼成一条指令）。

真判停（`score` 0.80/0.70，8009 单服务），选工具/决策是固定假实现 —— 决策质量不在本卡。

运行（先确认隧道 `curl -s 127.0.0.1:8009/health`）：

    .../.venv/bin/python -B tests/poc-dual-asr-c1.py [--groups 20] [--splice 10] [--smoke]

产出：`/Users/YUYSONG/Work/tmp/poc-dual-asr-c1.json`（`--out` 可改）。
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from asr.dual_seat import DualSeatSession
# noqa: E402
from gate.turn_eot import EOT_JUDGE_BASE, score_eot
# noqa: E402
from gate.turn_session import TurnConfig
from shared.text import normalize
# noqa: E402

# noqa: E402
from shared.text import normalize
# noqa: E402

CASES = ROOT / "src/fixtures/streaming-prefix-cases.jsonl"
DEFAULT_BASELINE = Path("/Users/YUYSONG/Work/tmp/poc-dual-asr-c0-baseline.json")
DEFAULT_OUT = Path("/Users/YUYSONG/Work/tmp/poc-dual-asr-c1.json")
PREFIX_INTERVAL_MS = 150
SCHEMA = {"name": "climate_set_power", "description": "开启或关闭座舱空调系统",
          "parameters": {"type": "object", "properties": {"enabled": {"type": "boolean"}},
                         "required": ["enabled"], "additionalProperties": False}}


def select(text, tool_set, context):
    return {"scores": {"tool:climate_set_power": 0.95}, "selected": ["climate_set_power"],
            "schemas": [SCHEMA], "native_schemas": [SCHEMA], "domains": [],
            "timing": {"stage1_ms": 0.0, "stage2_ms": 0.0, "total_ms": 0.0}}


def decide(utterance, selected, schemas, tool_set="core_score", context=None, **kwargs):
    return {"decision": {"kind": "tool_call", "tool": "climate_set_power",
                         "arguments": {"enabled": True}},
            "llm_ms": 5.0, "log_fields": {"decision_head": "c1_mock"}}


def sentences(limit=None):
    rows = [json.loads(line) for line in CASES.read_text(encoding="utf-8").splitlines() if line.strip()]
    grouped, order = {}, []
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


class Replay:
    """按绝对毫秒时间线喂双席，只在一个线程里推进（单写者）。"""

    def __init__(self, session_id):
        self.started = time.monotonic() * 1000
        self.duo = DualSeatSession(TurnConfig(), None, select_tools_fn=select, decide_fn=decide,
                                   session_id=session_id, evidence_root=None)

    def now(self):
        return int(round(time.monotonic() * 1000 - self.started))

    def feed_asr(self, at, mic, text, final=False):
        self._wait_until(at)
        self.duo.on_asr(self.now(), mic, text, final)

    def speech_end(self, at, mic):
        self._wait_until(at)
        self.duo.on_vad(self.now(), mic, "speech_end")

    def _wait_until(self, at):
        while self.now() < at:
            self.duo.tick(self.now())
            time.sleep(0.003)

    def run_until(self, predicate, budget_ms):
        limit = self.now() + budget_ms
        while self.now() < limit and not predicate():
            self.duo.tick(self.now())
            time.sleep(0.003)


def feed_prefixes(replay, mic, prefixes, start_ms, final_last=True):
    for index, prefix in enumerate(prefixes):
        replay.feed_asr(start_ms + index * PREFIX_INTERVAL_MS, mic, prefix,
                        final=bool(final_last and index == len(prefixes) - 1))
    return start_ms + len(prefixes) * PREFIX_INTERVAL_MS


def isolate_group(index, seat_sentence, other_sentence):
    """D 半句 → P 插完整句 → D 续句。"""
    replay = Replay("c1-iso-%02d" % index)
    prefixes = seat_sentence["prefixes"]
    half = max(1, len(prefixes) // 2)
    end_first = feed_prefixes(replay, "mic-D", prefixes[:half], 0, final_last=False)
    replay.speech_end(end_first + 20, "mic-D")
    other_start = end_first + 200
    end_other = feed_prefixes(replay, "mic-P", other_sentence["prefixes"], other_start)
    replay.speech_end(end_other + 20, "mic-P")
    resume = end_other + 250
    end_second = feed_prefixes(replay, "mic-D", prefixes[half:], resume, final_last=True)
    replay.speech_end(end_second + 20, "mic-D")

    replay.run_until(lambda: replay.duo.turn_done, 6000)
    duo = replay.duo
    decisions = [{"seat": event["seat_id"], "t_ms": event["t_ms"], "utterance": event.get("utterance")}
                 for event in duo.events if event["event"] == "llm_decision"]
    own_text = {seat: normalize("".join(sentence["prefixes"]))
                for seat, sentence in (("D", seat_sentence), ("P", other_sentence))}
    leaks = [item for item in decisions
             if normalize(item["utterance"] or "") not in own_text[item["seat"]]]
    row = {
        "index": index,
        "seat_sentence": seat_sentence["full_text"],
        "other_sentence": other_sentence["full_text"],
        "seat_prefixes": len(prefixes), "split_at": half,
        "confirms": {seat: list(values) for seat, values in duo.confirms.items()},
        "decisions": decisions,
        "text_leaks": leaks,
        "cross_cancel": duo.counters["cross_cancel"], "cross_reset": duo.counters["cross_reset"],
        "own_reset": duo.counters["own_reset"], "own_cancel": duo.counters["own_cancel"],
        "errors": [event for event in duo.events if event["event"] == "error"],
    }
    duo.close()
    return row


def splice_group(index, sentence):
    """D 说前半、P 说后半：任何一席都不能把两路拼起来。"""
    replay = Replay("c1-splice-%02d" % index)
    prefixes = sentence["prefixes"]
    half = max(1, len(prefixes) // 2)
    first_text, second_text = prefixes[half - 1], prefixes[-1]
    tail = second_text[len(first_text):] if second_text.startswith(first_text) else second_text
    replay.feed_asr(0, "mic-D", first_text, final=True)
    replay.speech_end(150, "mic-D")
    replay.feed_asr(200, "mic-P", tail, final=True)
    replay.speech_end(350, "mic-P")
    replay.run_until(lambda: replay.duo.turn_done, 5000)
    duo = replay.duo
    own_text = {"D": normalize(first_text), "P": normalize(tail)}
    decisions = [{"seat": event["seat_id"], "t_ms": event["t_ms"], "utterance": event.get("utterance")}
                 for event in duo.events if event["event"] == "llm_decision"]
    violations = [item for item in decisions
                  if normalize(item["utterance"] or "") not in own_text[item["seat"]]]
    row = {"index": index, "first_half": first_text, "second_half": tail,
           "decisions": decisions, "violations": violations,
           "cross_cancel": duo.counters["cross_cancel"], "cross_reset": duo.counters["cross_reset"],
           "errors": [event for event in duo.events if event["event"] == "error"]}
    duo.close()
    return row


def percentile(values, p):
    ordered = sorted(values)
    if not ordered:
        return None
    index = min(len(ordered) - 1, int(round(p / 100 * (len(ordered) - 1))))
    return round(ordered[index], 1)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--groups", type=int, default=20)
    parser.add_argument("--splice", type=int, default=10)
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT)
    parser.add_argument("--baseline", type=Path, default=DEFAULT_BASELINE)
    parser.add_argument("--smoke", action="store_true", help="每组只跑 2 例、不写报告")
    args = parser.parse_args()

    groups = 2 if args.smoke else args.groups
    splice_n = 2 if args.smoke else args.splice
    picked = sentences(limit=groups * 2 + splice_n)
    if len(picked) < groups * 2 + 1:
        raise SystemExit("语料不够：需要 %d 句，只有 %d 句" % (groups * 2 + 1, len(picked)))
    try:
        score_eot("导航回家")   # 预检：隧道断了就别把一堆 eot error 当结论
    except Exception as error:
        raise SystemExit("判停服务不可用（%s: %s）：先 curl %s/health 或重开隧道"
                         % (type(error).__name__, error, EOT_JUDGE_BASE))
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    base_p50 = baseline["summary"]["t_confirm_latency_ms"]["p50"]
    limit_p50 = round(base_p50 * 1.10, 1)
    print("判停端点：%s/v1/systemone｜score 0.80/0.70｜A0 单席基线 p50=%sms｜A1 上限 %sms"
          % (EOT_JUDGE_BASE, base_p50, limit_p50), flush=True)

    pairs = [(picked[i], picked[groups + i]) for i in range(groups)]
    isolation = []
    print("\n[1/2] 隔离：D 半句 → P 插完整句 → D 续句", flush=True)
    for index, (seat_sentence, other) in enumerate(pairs, 1):
        row = isolate_group(index, seat_sentence, other)
        isolation.append(row)
        print("  [%d/%d] %s ｜ confirms %s ｜ decisions %d ｜ leak %d ｜ cross %d/%d"
              % (index, groups, seat_sentence["full_text"],
                 {seat: [item["why"] for item in values] for seat, values in row["confirms"].items()},
                 len(row["decisions"]), len(row["text_leaks"]),
                 row["cross_cancel"], row["cross_reset"]), flush=True)

    splice = []
    print("\n[2/2] 不拼接：D 前半 + P 后半", flush=True)
    for index, sentence in enumerate(picked[-splice_n:], 1):
        row = splice_group(index, sentence)
        splice.append(row)
        print("  [%d/%d] %s + %s ｜ decisions %d ｜ violations %d"
              % (index, splice_n, row["first_half"], row["second_half"],
                 len(row["decisions"]), len(row["violations"])), flush=True)

    # 时延口径（2026-11-04 去窗后）：强 complete 不再走 `confirm_silence` 那条静默路径（判完当刻认，
    # why=eot_complete），所以旧判据「只取 confirm_silence、与 A0 带窗基线 ×1.10 比」的前提没了。
    # 这里量**所有确认路径**并只记录、不当门槛：去窗后的 A0/A1 新基线要重跑才有
    # （wiki [[无确认窗流程]] §未决 2，重锚时机待拍 P3-1）。
    latencies = {seat: [item["latency_ms"] for row in isolation for item in row["confirms"][seat]]
                 for seat in ("D", "P")}
    confirms_by_why = {seat: {why: sum(1 for row in isolation for item in row["confirms"][seat]
                                       if item["why"] == why)
                              for why in sorted({item["why"] for row in isolation
                                                 for item in row["confirms"][seat]})}
                       for seat in ("D", "P")}
    summary = {
        "endpoint": EOT_JUDGE_BASE + "/v1/systemone",
        "policy": {"eot_policy": "score", "eot_complete_min": 0.80, "eot_confidence_min": 0.70},
        "scope": {"two_clean_asr_upstream": True, "same_sentence_dedup": "out_of_scope",
                  "voice_separation": "frontend"},
        "baseline": {"path": str(args.baseline), "p50": base_p50, "limit_p50": limit_p50},
        "isolation": {
            "groups": len(isolation),
            # 「两席各自 CONFIRMED」按任何确认路径算（话断了/犹豫后收口也是 CONFIRMED）；
            # 去窗后确认原因不止 confirm_silence，按 why 分列记录（见 confirms_by_why）。
            "both_seats_confirmed": sum(1 for row in isolation
                                        if row["confirms"]["D"] and row["confirms"]["P"]),
            "confirms_by_why": confirms_by_why,
            "cross_cancel": sum(row["cross_cancel"] for row in isolation),
            "cross_reset": sum(row["cross_reset"] for row in isolation),
            "text_leaks": sum(len(row["text_leaks"]) for row in isolation),
            "errors": sum(len(row["errors"]) for row in isolation),
            "own_reset": sum(row["own_reset"] for row in isolation),
            "own_cancel": sum(row["own_cancel"] for row in isolation),
            "p50": {seat: percentile(values, 50) for seat, values in latencies.items()},
            "p90": {seat: percentile(values, 90) for seat, values in latencies.items()},
            "n": {seat: len(values) for seat, values in latencies.items()},
            "latency_note": "去窗后与带窗 A0 基线（confirm_silence 路径）不可比；p50 只记录，重锚待 P3-1",
        },
        "splice": {
            "groups": len(splice),
            "cross_seat_texts": sum(len(row["violations"]) for row in splice),
            "errors": sum(len(row["errors"]) for row in splice),
            "cross_cancel": sum(row["cross_cancel"] for row in splice),
            "cross_reset": sum(row["cross_reset"] for row in splice),
        },
    }
    summary["verdict"] = {
        # 时延不再进 A1 判据：去窗后判据本身失效（见上面 latency_note），只记数、不当门槛。
        "A1_isolation": (summary["isolation"]["both_seats_confirmed"] == len(isolation)
                         and summary["isolation"]["cross_cancel"] == 0
                         and summary["isolation"]["cross_reset"] == 0
                         and summary["isolation"]["text_leaks"] == 0
                         and summary["isolation"]["errors"] == 0),
        "A1_no_concat": (summary["splice"]["cross_seat_texts"] == 0
                         and summary["splice"]["cross_cancel"] == 0
                         and summary["splice"]["cross_reset"] == 0),
    }
    summary["verdict"]["A1"] = all(summary["verdict"].values())
    print("\n" + json.dumps(summary, ensure_ascii=False, indent=1))
    if not args.smoke:
        args.out.parent.mkdir(parents=True, exist_ok=True)
        args.out.write_text(json.dumps({"summary": summary, "isolation": isolation,
                                        "splice": splice}, ensure_ascii=False, indent=1) + "\n",
                            encoding="utf-8")
        print("json:", args.out)
    if not summary["verdict"]["A1"]:
        raise SystemExit("A1 未全绿：见 summary.verdict 与各组明细")


if __name__ == "__main__":
    main()
