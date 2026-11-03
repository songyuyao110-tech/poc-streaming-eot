"""C2 编排层 audit provider：把会话脑（`src/orchestra.py`）的账喂给断言面。

    python3 -B tests/run_dual_asr_text_cases.py --wave 2 --audit tests.dual_asr_audit:audit

接口按断言面约定 `fn(case, duo) -> dict`：
- 有 duo → 取该会话脑的真值（C2 字段：actions_effective / repeat_action / receipt_has_both /
  au_executed / au_abandoned / wrong_seat / iu_* / decisions 覆盖）。
- duo 为 None（TC-36 聚合）→ 汇总本轮跑过的所有会话脑：`iu_open == 0` 且
  `iu_committed == au_executed + au_abandoned` 必须全成立。

只给 C2 已落地的字段；C3/C4 的字段（blocked_by_seat / direct_exec / coverage_receipt /
params_include / final_param / frame_switch / game_* …）**不伪造**，断言面会把它们记
BLOCKED——缺哪层就 BLOCKED，这是断言面的既定口径。
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.dont_write_bytecode = True

C2_KEYS = ("actions_effective", "repeat_action", "receipt_has_both", "au_executed",
           "au_abandoned", "wrong_seat", "iu_open", "iu_committed", "iu_eq", "decisions")

_BRAINS = []


def _snapshot_keys(brain, case=None):
    audit = brain.audit(case)
    return {key: audit[key] for key in C2_KEYS if key in audit}


def audit(case, duo):
    """断言面 audit 入口。"""
    if duo is not None and getattr(duo, "brain", None) is not None:
        brain = duo.brain
        if brain not in _BRAINS:
            _BRAINS.append(brain)
        return _snapshot_keys(brain, case)
    if not _BRAINS:
        return {}
    # TC-36：整轮聚合（每个会话收口后都要账平）
    ledgers = [_snapshot_keys(brain, case) for brain in _BRAINS]
    return {
        "iu_open": sum(item["iu_open"] for item in ledgers),
        "iu_committed": sum(item["iu_committed"] for item in ledgers),
        "au_executed": sum(item["au_executed"] for item in ledgers),
        "au_abandoned": sum(item["au_abandoned"] for item in ledgers),
        "iu_eq": all(item["iu_eq"] for item in ledgers),
        "sessions": len(ledgers),
    }
