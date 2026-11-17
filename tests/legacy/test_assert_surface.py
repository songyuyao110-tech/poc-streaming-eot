"""离线自检：断言面本身是活的，不是恒真/恒假的空壳（卡 4d3267ff §4.4）。

不连判停服务、不跑真回放，只用假 session / 构造的 seat/audit dict 去敲 `check_expect` 与
`seat_surface`，证明六件事：

1. 37 条用例的 expect key 全部能在 REGISTRY 找到（未知 key 必须报错）。
2. 标量 / 区间两套匹配：`decisions {"D":1}` 与 `{"D":[1,2]}`。
3. orchestra 缺席 → BLOCKED（不是 FAIL）；给 FakeAudit 正确值 → PASS；给错值（au_executed=3
   对 TC-17 期望 2）→ FAIL。
4. `late_verdict_drives` 两个方向：timeout 后到 MAYBE_DONE 或 CONFIRMED（都是 why=eot_complete）
   → True；只有 timeout 无后续 → False。
5. `ask_user` / `decide` / `no_splice` / `own_text_only` 各一条正反例。
6. `dump_surface()` 里 20 个 orchestra 字段齐全、每个 REGISTRY 条目有 layer/owner/semantics。

全程离线、<10s。输出 `N/N 例通过`，失败 `SystemExit(1)`。
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from assert_surface import (
# noqa: E402
    REGISTRY, ORCHESTRA_FIELDS, META_FIELDS, check_expect, dump_surface, seat_surface)

CASES_FILE = Path(__file__).resolve().parents[1] / "asr" / "dual_asr_text_cases.jsonl"

_CASES = [json.loads(l) for l in CASES_FILE.read_text(encoding="utf-8").splitlines() if l.strip()]


def check(name, cond, detail=""):
    if not cond:
        raise AssertionError(name + (" | " + detail if detail else ""))
    print("  ok  " + name + (" | " + detail if detail else ""))


# ---------------------------------------------------------------- 1. 词汇表覆盖

def test_registry_covers_all_case_keys():
    print("[1] 37 条用例 expect 的所有 key 都在 REGISTRY")
    for case in _CASES:
        for key in (case.get("expect") or {}):
            check("%s 的 %s 在 REGISTRY" % (case["case_id"], key), key in REGISTRY,
                  "layer=%s" % REGISTRY.get(key, {}).get("layer", "?"))


# ---------------------------------------------------------------- 2. 标量/区间匹配

def test_scalar_interval():
    print("[2] decisions 标量 / 区间匹配")
    # 标量 {D:1}
    checks = check_expect({"decisions": {"D": 1}},
                          {"decisions": {"D": 1, "P": 1}}, None, {"wave": 1})
    d = [c for c in checks if c["key"] == "decisions"][0]
    check("标量 1==1 → PASS", d["status"] == "PASS", str(d))
    # 标量错 {D:2}
    checks = check_expect({"decisions": {"D": 2}},
                          {"decisions": {"D": 1, "P": 1}}, None, {"wave": 1})
    check("标量 2!=1 → FAIL", [c for c in checks if c["key"] == "decisions"][0]["status"] == "FAIL")
    # 区间 {D:[1,2]}
    checks = check_expect({"decisions": {"D": [1, 2]}},
                          {"decisions": {"D": 1, "P": 0}}, None, {"wave": 1})
    check("区间 [1,2] ⊇ 1 → PASS",
          [c for c in checks if c["key"] == "decisions"][0]["status"] == "PASS")
    checks = check_expect({"decisions": {"D": [1, 2]}},
                          {"decisions": {"D": 3, "P": 0}}, None, {"wave": 1})
    check("区间 [1,2] ∌ 3 → FAIL",
          [c for c in checks if c["key"] == "decisions"][0]["status"] == "FAIL")


# ---------------------------------------------------------------- 3. orchestra BLOCKED / PASS / FAIL

def test_orchestra_absent_blocked():
    print("[3] orchestra 缺席 → BLOCKED；audit 正确 → PASS；audit 错 → FAIL")
    # TC-17 期望 au_executed=2，无 audit → BLOCKED（不是 FAIL）
    checks = check_expect({"au_executed": 2}, {}, None, {"case_id": "TC-17", "wave": 2,
                                                         "depends": ["C2"]})
    au = [c for c in checks if c["key"] == "au_executed"][0]
    check("无 audit → BLOCKED", au["status"] == "BLOCKED", str(au))
    check("blocked_by=case.depends", au["blocked_by"] == ["C2"], str(au))
    # 给对 audit → PASS（TC-17 au_executed=2）
    checks = check_expect({"au_executed": 2}, {}, {"au_executed": 2}, {"case_id": "TC-17"})
    check("audit 正确 au_executed=2 → PASS",
          [c for c in checks if c["key"] == "au_executed"][0]["status"] == "PASS")
    # 给错 audit → FAIL（au_executed=3 对期望 2）
    checks = check_expect({"au_executed": 2}, {}, {"au_executed": 3}, {"case_id": "TC-17"})
    check("audit 错 au_executed=3 → FAIL",
          [c for c in checks if c["key"] == "au_executed"][0]["status"] == "FAIL")
    # audit 缺字段 → BLOCKED（哪怕给了别的字段）
    checks = check_expect({"au_executed": 2, "repeat_action": 0}, {},
                          {"repeat_action": 0}, {"case_id": "TC-17", "wave": 2,
                                                 "depends": ["C2"]})
    au = [c for c in checks if c["key"] == "au_executed"][0]
    check("audit 缺 au_executed → BLOCKED", au["status"] == "BLOCKED", str(au))
    check("blocked_by=缺失字段名", au["blocked_by"] == ["missing audit field: au_executed"],
          str(au))


# ---------------------------------------------------------------- 4. late_verdict_drives 两方向

def test_late_verdict_drives():
    print("[4] late_verdict_drives 两方向")
    on = _surface_with_events(
        [("eot_skipped", 100, {"reason": "timeout"}),
         ("state_change", 130, {"to": "MAYBE_DONE", "why": "eot_complete"})])
    check("timeout 后到 MAYBE_DONE/complete → True", on["late_verdict_drives"] is True)
    # 去窗后强 complete 不再过 MAYBE_DONE，直接进 CONFIRMED(why=eot_complete)，同样是「驱动了状态机」。
    on_strong = _surface_with_events(
        [("eot_skipped", 100, {"reason": "timeout"}),
         ("state_change", 130, {"to": "CONFIRMED", "why": "eot_complete"})])
    check("timeout 后到 CONFIRMED/complete → True（强 complete 直接认）",
          on_strong["late_verdict_drives"] is True)
    off = _surface_with_events([("eot_skipped", 100, {"reason": "timeout"})])
    check("只有 timeout、无后续 → False", off["late_verdict_drives"] is False)
    # 埋进 check_expect
    checks = check_expect({"late_verdict_drives": True},
                          {"late_verdict_drives": True}, None, {"wave": 1})
    check("late_verdict_drives True→PASS",
          [c for c in checks if c["key"] == "late_verdict_drives"][0]["status"] == "PASS")
    checks = check_expect({"late_verdict_drives": True},
                          {"late_verdict_drives": False}, None, {"wave": 1})
    check("late_verdict_drives False→FAIL",
          [c for c in checks if c["key"] == "late_verdict_drives"][0]["status"] == "FAIL")


def _surface_with_events(raw_events):
    """造一个含指定事件的假 duo，喂给 seat_surface。"""
    class _FakeSeatSession:
        consumed_count = 0
        consumed_segments = 0
        consumptions = []
        t_last_word = 120
        t_confirmed = 120
        t_llm_start = 140
        events = []
        counters = {"llm_calls": 1, "consumed": 1, "llm_wasted": 0, "eot_skipped": 0,
                    "eot_calls": 1, "eot_stale": 0, "n_reissue": 0}

    class _FakeDuo:
        events = []
        counters = {"cross_cancel": 0, "cross_reset": 0, "own_cancel": 0, "own_reset": 0,
                    "errors": 0, "inputs": 0}
        confirms = {"D": [], "P": []}

        def __init__(self):
            self.seats = {}
            for seat in ("D", "P"):
                s = _FakeSeatSession()
                s.events = []
                self.seats[seat] = s

    duo = _FakeDuo()
    for name, t, payload in raw_events:
        ev = dict(payload)
        ev["event"] = name
        ev["t_ms"] = t
        ev["seat_id"] = "D"
        duo.events.append(ev)
        duo.seats["D"].events.append(ev)
    # seat_surface 会读 D 座
    return seat_surface(duo, {})


# ---------------------------------------------------------------- 5. ask_user/decide/no_splice/own_text_only 正反例

def test_basic_checks_posneg():
    print("[5] ask_user / decide / no_splice / own_text_only 各一条正反例")
    # ask_user: 全局 ask_user 计数
    checks = check_expect({"ask_user": 1}, {"ask_user": 1, "decide": 1}, None, {"wave": 2})
    s = [c for c in checks if c["key"] == "ask_user"][0]
    check("ask_user 正确 → PASS", s["status"] == "PASS")
    # 注意 wave=2 无 depends → 仍是门槛（OBSERVED 只发生在 depends 非空且无 audit）
    checks = check_expect({"ask_user": 0}, {"ask_user": 1, "decide": 1}, None,
                          {"case_id": "TC-19", "wave": 2, "depends": ["C2"]})
    s = [c for c in checks if c["key"] == "ask_user"][0]
    check("ask_user 错、depends+无audit → OBSERVED（不记 FAIL）", s["status"] == "OBSERVED",
          str(s))
    # decide
    checks = check_expect({"decide": 0}, {"ask_user": 0, "decide": 2}, None, {"wave": 2})
    check("decide 错 2 vs 0 → FAIL",
          [c for c in checks if c["key"] == "decide"][0]["status"] == "FAIL")
    # no_splice
    checks = check_expect({"no_splice": True}, {"no_splice": True}, None, {"wave": 1})
    check("no_splice True → PASS",
          [c for c in checks if c["key"] == "no_splice"][0]["status"] == "PASS")
    checks = check_expect({"no_splice": True}, {"no_splice": False}, None, {"wave": 1})
    check("no_splice False → FAIL",
          [c for c in checks if c["key"] == "no_splice"][0]["status"] == "FAIL")
    # own_text_only
    checks = check_expect({"own_text_only": True}, {"own_text_only": True}, None, {"wave": 1})
    check("own_text_only True → PASS",
          [c for c in checks if c["key"] == "own_text_only"][0]["status"] == "PASS")
    checks = check_expect({"own_text_only": True}, {"own_text_only": False}, None, {"wave": 1})
    check("own_text_only False → FAIL",
          [c for c in checks if c["key"] == "own_text_only"][0]["status"] == "FAIL")


# ---------------------------------------------------------------- 6. dump_surface 完整性

def test_dump_surface_complete():
    print("[6] dump_surface: 20 orchestra 字段齐全、每条目有 layer/owner/semantics")
    dumped = dump_surface()
    check("orchestra_fields 恰好 20 个", len(dumped["orchestra_fields"]) == 20,
          str(len(dumped["orchestra_fields"])))
    check("ORCHESTRA_FIELDS 与 dump 一致", dumped["orchestra_fields"] == list(ORCHESTRA_FIELDS))
    missing = [k for k, m in REGISTRY.items()
               if not all(k in m for k in ("layer", "type", "owner", "semantics", "source"))]
    check("无缺失元数据条目", not missing, str(missing))
    # 每个 orchestra 条目 owner 在 {C2,C3,C4}
    bad_owner = [k for k in ORCHESTRA_FIELDS if REGISTRY[k]["owner"] not in ("C2", "C3", "C4")]
    check("orchestra owner 都是 C2/C3/C4", not bad_owner, str(bad_owner))
    # meta 键不在断言层
    for mk in META_FIELDS:
        check("meta 键 %s 不在断言 REGISTRY" % mk, mk not in REGISTRY)


# ---------------------------------------------------------------- main

CASES = [test_registry_covers_all_case_keys, test_scalar_interval,
         test_orchestra_absent_blocked, test_late_verdict_drives,
         test_basic_checks_posneg, test_dump_surface_complete]


def main():
    failures = []
    for case in CASES:
        try:
            case()
        except AssertionError as error:
            print("  FAIL " + str(error))
            failures.append((case.__name__, str(error)))
    print("\n%d/%d 例通过" % (len(CASES) - len(failures), len(CASES)))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()