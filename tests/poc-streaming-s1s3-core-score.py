"""S1-S3 acceptance for the core_score tool set (242 tools / 17 domains, two-stage KEV).

Each case runs the real pipeline end to end (`run()`: two-stage KEV scoring -> shortlist ->
one DeepSeek decision, execution stays mock) and checks:
  * stage 1 recalled the expected domain,
  * the threshold-selected shortlist holds the expected tool family,
  * exactly one decision call was made and it produced a well-formed decision.

    python tests/poc-streaming-s1s3-core-score.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import bootstrap
bootstrap.install()          # 上游仓库根
sys.dont_write_bytecode = True

import core_pipeline_run as core

CASES = [
    {"id": "S1", "utterance": "导航回家", "domain": "navigation",
     "shortlist": lambda name: name.startswith("car_navi_")},
    {"id": "S2", "utterance": "把空调开到 24 度", "domain": "hvac",
     "shortlist": lambda name: name.startswith("car_hav_")},
    {"id": "S3", "utterance": "打开座椅加热", "domain": "seat",
     "shortlist": lambda name: "seat" in name or "heating" in name},
]
DECISION_CALLS = []
REAL_DECIDE = core.decide


def counted_decide(*args, **kwargs):
    """run() must decide exactly once per turn; the wrapper makes that observable."""
    DECISION_CALLS.append(args[0])
    return REAL_DECIDE(*args, **kwargs)


def check(name, ok, note=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {note}")
    return bool(ok)


def run_case(case):
    print("=" * 100)
    DECISION_CALLS.clear()
    log = core.run(case["utterance"])
    recalled = [item["name"] for item in log.get("recalled_domains") or []]
    selected = log.get("selected_tools") or []
    matched = [name for name in selected if case["shortlist"](name)]
    decision = log.get("decision") or {}
    print(f"  input={case['utterance']!r} outcome={log['outcome']} "
          f"tool_count={log.get('tool_count')} domain_count={log.get('domain_count')} "
          f"registry={log.get('registry_version')}")
    print(f"  recalled={recalled} selected={selected} decision={json.dumps(decision, ensure_ascii=False)}")
    print(f"  kev stage1={log.get('kev_stage1_ms')} stage2={log.get('kev_stage2_ms')} "
          f"scored_tools={log.get('scored_tool_count')} decision_head={log.get('decision_head')}")
    return [
        check(f"{case['id']} run PASS", log["outcome"] == "PASS", log.get("error", "")),
        check(f"{case['id']} registry is 242 tools / 17 domains",
              log.get("tool_count") == 242 and log.get("domain_count") == 17,
              f"{log.get('tool_count')}/{log.get('domain_count')}"),
        check(f"{case['id']} stage-1 recall contains {case['domain']}", case["domain"] in recalled, f"{recalled}"),
        check(f"{case['id']} shortlist has the expected family", bool(matched), f"{matched} in {selected}"),
        check(f"{case['id']} exactly 1 decision call", len(DECISION_CALLS) == 1, f"{DECISION_CALLS}"),
        check(f"{case['id']} decision is a next step", decision.get("kind") in {"tool_call", "ask_user", "done"},
              f"{decision.get('kind')} / {decision.get('tool')}"),
        check(f"{case['id']} execution stayed mock", log.get("executed_tools") == 0, f"{log.get('executed_tools')}"),
    ]


if __name__ == "__main__":
    core.decide = counted_decide
    results = {case["id"]: run_case(case) for case in CASES}
    print("=" * 100)
    for case_id, items in results.items():
        print(f"{case_id}: {'PASS' if all(items) else 'FAIL'} ({sum(items)}/{len(items)})")
    passed = all(all(items) for items in results.values())
    total = sum(len(items) for items in results.values())
    print(f"S1-S3 RESULT: {'PASS' if passed else 'FAIL'} ({sum(sum(items) for items in results.values())}/{total})")
    raise SystemExit(0 if passed else 1)