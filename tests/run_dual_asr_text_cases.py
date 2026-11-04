"""双路 ASR 文本用例回放器（落仓版，wave-2 断言面）。

把 `tests/dual_asr_text_cases.jsonl` 的 37 条用例（wave-1 判停/隔离 + wave-2 语义=STAGED）
按 PM 同口径喂进 `DualSeatSession`，判停走真 8009 或假注入，选工具 / 决策用固定假实现。

    python3 -B tests/run_dual_asr_text_cases.py [--wave 1|2|all] [--judge real|fake]
          [--only TC-01,TC-07] [--audit MODULE:FUNC] [--out PATH] [--dump-surface]

行为要点（卡 4d3267ff §4.3）：
- 前置校验：用例可解析；`expect`/`expect_fake_judge` 每个 key ∈ REGISTRY ∪ meta；违规退出码 2。
- `expect_fake_judge`（可选）：同一用例在 `--judge fake` 下的附加期望（确认原因依赖判停 confidence）。
- wave-1：绝对毫秒 plan 喂双席；`eot_fn` 按 `case["judge"]` 选（real→8009，
  fake_weak/fake_timeout/fake_silent→假实现）；`--judge fake` 把 real 用例改用「立即 complete」假判停，
  并把静默确认 p50 ≤ AI×1.10 时延门槛降级为 OBSERVED。
- wave-2：事件锚 `D|P:confirm|decision` 按出现序号消费；无 `--audit` 且 `depends` 非空 → STAGED，
  orchestra 判据 BLOCKED、seat 判据 OBSERVED；有 `--audit MODULE:FUNC`（fn(case,duo)->dict）→
  全部门槛化 PASS|FAIL，缺字段按 BLOCKED。
- TC-34（reuse）：subprocess 跑 tests/test_confirm_gate.py，解「合计：N 项通过」==53。
- TC-36（无 plan、账目）：整轮结束后聚合 eq / iu_eq（iu_eq 需 audit）。
- 真判停不可达时：默认降级 `--judge fake`；显式 `--judge real` → SKIP 且退出码 0。

本文件只读 `src`（`python -B`），不写字节码、不改业务仓。
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import re
import statistics
import subprocess
import sys
import time
import traceback
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))

from assert_surface import (  # noqa: E402
    A1_CEILING_MS, META_FIELDS, REGISTRY, check_expect, dump_surface, seat_surface,
)
from dual_seat import DualSeatSession  # noqa: E402
from orchestra import SessionBrain  # noqa: E402
from turn_eot import EOT_JUDGE_BASE, score_eot  # noqa: E402
from turn_session import TurnConfig  # noqa: E402

CASES_FILE = Path(__file__).resolve().parent / "dual_asr_text_cases.jsonl"
DEFAULT_OUT = REPO / "evidence/dual-asr-text-cases/run-latest.json"
REUSE_SCRIPT = REPO / "tests/test_confirm_gate.py"

SCHEMA = {"name": "climate_set_power", "description": "开启或关闭座舱空调系统",
          "parameters": {"type": "object", "properties": {"enabled": {"type": "boolean"}},
                         "required": ["enabled"], "additionalProperties": False}}


# ---------------------------------------------------------------- 假实现

def select(text, tool_set, context):
    return {"scores": {"tool:climate_set_power": 0.95}, "selected": ["climate_set_power"],
            "schemas": [SCHEMA], "native_schemas": [SCHEMA], "domains": [],
            "timing": {"stage1_ms": 0.0, "stage2_ms": 0.0, "total_ms": 0.0}}


def decide(utterance, selected, schemas, tool_set="core_score", context=None, **kwargs):
    time.sleep(0.25)          # 制造「决策在途」窗口（TC-09 靠它）
    return {"decision": {"kind": "tool_call", "tool": "climate_set_power",
                         "arguments": {"enabled": True}},
            "llm_ms": 250.0, "log_fields": {"decision_head": "textcase_mock"}}


def eot_immediate(text):
    """--judge fake 给 real 用例的「立即 complete」假判停（不走真服务）。

    confidence 用弱（0.55，`may_start_early=False`）：与 PM 的 fake_weak 同口径，避免
    speculative LLM 在假判停下逐 partial 抢跑制造大量 self-cancel（真 8009 的判停时延
    本来就把 speculative 压得很稀，假判停必须反着压，否则 TC-09 的 own_cancel {D:0}
    这类隔离判据会被本席自己流式产生的取消污染）。
    """
    return {"complete": 0.90, "cut": 0.06, "wait": 0.04, "label": "complete", "choice": "complete",
            "confidence": 0.55, "margin": 0.84, "model": "fake-immediate", "wall_ms": 1.0}


def eot_weak_complete(text):
    """强 complete 概率、低 confidence：必须不抢跑。"""
    time.sleep(0.05)
    return {"complete": 0.90, "cut": 0.06, "wait": 0.04, "label": "complete", "choice": "complete",
            "confidence": 0.55, "margin": 0.84, "model": "fake-weak", "wall_ms": 50.0}


def eot_timeout(text):
    """比 eot_timeout_ms(900) 慢、但在 max_silence(2000) 前回来：迟到结果仍会生效。"""
    time.sleep(1.2)
    return {"complete": 0.93, "cut": 0.04, "wait": 0.03, "label": "complete", "choice": "complete",
            "confidence": 0.95, "margin": 0.89, "model": "fake-slow", "wall_ms": 1200.0}


def eot_silent(text):
    """判停 5s 才回（远超 max_silence 2000）：只能靠 max_silence 兜底（与 PM 同口径）。

    5s 才到的 verdict 落在已收口并发完的空后缀上，`_on_eot_result` 走 `empty_suffix` 分支
    直接 return，不会产生 state_change->MAYBE_DONE(eot_complete)。因此 TC-06A 的
    `late_verdict_drives` 恒为 False（用户 Q2 锁定口径：超时后不驱动），实测 3/3 稳定。
    """
    time.sleep(5.0)
    return {"complete": 0.93, "cut": 0.04, "wait": 0.03, "label": "complete", "choice": "complete",
            "confidence": 0.95, "margin": 0.89, "model": "fake-silent", "wall_ms": 5000.0}


FAKE_JUDGES = {"fake_weak": eot_weak_complete, "fake_timeout": eot_timeout,
               "fake_silent": eot_silent}


def _health_ok() -> bool:
    try:
        import httpx2
        resp = httpx2.get(EOT_JUDGE_BASE.rstrip("/") + "/health", timeout=3.0,
                          trust_env=False)
        return resp.status_code == 200 and json.loads(resp.content).get("status") == "ok"
    except Exception:
        return False


def pick_eot(case, mode):
    """按 case.judge 与 --judge 选判停实现。返回 (fn, judge_kind)。"""
    judge = case.get("judge")
    if judge in FAKE_JUDGES:
        return FAKE_JUDGES[judge], "fake"
    if judge == "real":
        if mode == "fake":
            return eot_immediate, "fake"
        return score_eot, "real"
    return None, judge  # existing（TC-34）等


# ---------------------------------------------------------------- 回放

def build_text_partials(text, mic, t0, prefix_interval=150, speech_end_delay=18):
    """逐字 partial：chars 按序累加，末片 final=True。"""
    text = text or ""
    entries = []
    if not text:
        return entries
    for k in range(1, len(text) + 1):
        prefix = text[:k]
        entries.append((t0 + (k - 1) * prefix_interval, "asr", mic, prefix,
                        k == len(text)))
    entries.append((t0 + len(text) * prefix_interval + speech_end_delay, "vad", mic, "speech_end"))
    return entries


class Replay:
    def __init__(self, case, mode):
        eot_fn, judge_kind = pick_eot(case, mode)
        # C2 起双席挂会话脑：决策走编排层一个入口，`--audit tests.dual_asr_audit:audit`
        # 从这里取真值（只有 brain 在场，编排层字段才会从 BLOCKED 变成 PASS/FAIL）。
        self.brain = SessionBrain(decide)
        self.duo = DualSeatSession(TurnConfig(), None, eot_fn=eot_fn or eot_immediate,
                                   select_tools_fn=select, decide_fn=decide,
                                   session_id="tc-" + case["case_id"], evidence_root=None,
                                   brain=self.brain)
        self.mode = mode
        self.judge_kind = judge_kind
        self.started = time.monotonic() * 1000
        self.case = case

    def now(self):
        return int(round(time.monotonic() * 1000 - self.started))

    def wait_until(self, at):
        while self.now() < at:
            self.duo.tick(self.now())
            time.sleep(0.003)
        self.duo.tick(self.now())

    def feed(self, entry):
        at, kind, mic, payload = entry[0], entry[1], entry[2], entry[3]
        self.wait_until(at)
        if kind == "asr":
            self.duo.on_asr(self.now(), mic, payload, bool(entry[4]))
        else:
            self.duo.on_vad(self.now(), mic, payload)


def _active_seats(case):
    seats = set()
    for entry in case.get("plan") or []:
        if entry[1] == "asr":
            seats.add("D" if entry[2] == "mic-D" else "P")
    for item in case.get("script") or []:
        mic = item.get("mic", "")
        if mic:
            seats.add("D" if mic == "mic-D" else "P")
    return seats


def replay_absolute(case, mode):
    """wave-1 / wave-2 绝对 plan 回放。"""
    replay = Replay(case, mode)
    active = _active_seats(case)
    plan = sorted(case.get("plan") or [], key=lambda e: e[0])
    plan_end = max((e[0] for e in plan), default=0)
    began = time.monotonic()
    for entry in plan:
        replay.feed(entry)

    def settled():
        if replay.now() < plan_end:
            return False
        return all(replay.duo.seats[seat].turn_done for seat in active)

    limit = plan_end + 9000
    while replay.now() < limit and not settled():
        replay.duo.tick(replay.now())
        time.sleep(0.003)
    wall = round(time.monotonic() - began, 2)
    return replay.duo, active, wall


def replay_anchored(case, mode):
    """wave-2 事件锚回放：脚本项按序推进，anchored 项在该锚第 N 次出现后 + offset 喂入。"""
    replay = Replay(case, mode)
    active = _active_seats(case)
    began = time.monotonic()

    # 处理 script 项：非锚的立即转绝对 partial；锚的登记等 (seat, kind, occurrence)
    absolute = []          # [(t, kind, mic, payload, final?)] 未排序
    waiting = []           # [(seat, kind, occurrence, offset, mic, text)]
    for item in case.get("script") or []:
        mic = item.get("mic", "")
        text = item.get("text", "")
        anchor = item.get("anchor")
        if anchor is None:
            t0 = item.get("start", 0)
            absolute.extend(build_text_partials(text, mic, t0))
        else:
            seat, kind = anchor.split(":", 1)
            occ = sum(1 for w in waiting if w[0] == seat and w[1] == kind) + 1
            waiting.append((seat, kind, occ, item.get("offset", 0), mic, text))

    absolute.sort(key=lambda e: e[0])
    wait_by_hit = {(w[0], w[1], w[2]): w for w in waiting}
    fired = set()
    anchor_fire_count = {}
    last_scanned = 0

    while replay.now() < 60_000:
        replay.duo.tick(replay.now())
        # 扫描新锚事件
        events = replay.duo.events
        i = last_scanned
        for ev in events[i:]:
            if ev.get("event") == "state_change" and ev.get("to") == "CONFIRMED":
                _on_anchor(replay, anchor_fire_count, absolute, wait_by_hit, fired,
                           ev.get("seat_id"), "confirm", ev.get("t_ms"))
            elif ev.get("event") == "llm_decision":
                _on_anchor(replay, anchor_fire_count, absolute, wait_by_hit, fired,
                           ev.get("seat_id"), "decision", ev.get("t_ms"))
        last_scanned = len(events)
        # 喂所有已到期的绝对条目
        while absolute and absolute[0][0] <= replay.now():
            replay.feed(absolute.pop(0))
        # 结束判定：对话已结束（所有 active 席 turn_done）即停。
        # 注：不强制要求「所有锚都触发」——anchored 文本若该席在收口时尚未产生决策，
        # 该 anchor 自然不触发（wave-2 语义 STAGED/audit 判定仍成立）；只要求两席回合收束，
        # 避免个别锚永不触发时空转满 `60s` 硬顶。
        if all(replay.duo.seats[seat].turn_done for seat in active):
            break
        time.sleep(0.003)
    wall = round(time.monotonic() - began, 2)
    return replay.duo, active, wall


def _on_anchor(replay, anchor_fire_count, absolute, wait_by_hit, fired, seat, kind, t_ms):
    """记录一次锚事件；命中等待项就把它的 partial 时间线安排到 t_ms + offset。"""
    if seat not in ("D", "P"):
        return
    if (seat, kind) not in ((w[0], w[1]) for w in wait_by_hit):
        return
    count = anchor_fire_count.get((seat, kind), 0) + 1
    anchor_fire_count[(seat, kind)] = count
    hit = (seat, kind, count)
    if hit in wait_by_hit and hit not in fired:
        fired.add(hit)
        _seat, _kind, _occ, offset, mic, text = wait_by_hit[hit]
        t0 = (t_ms or replay.now()) + int(offset)
        absolute.extend(build_text_partials(text, mic, t0))
        absolute.sort(key=lambda e: e[0])


def replay_case(case, mode):
    plan = case.get("plan")
    if case.get("expect", {}).get("reuse") is not None:
        return None, set(), 0.0, "reuse"
    if plan is None:
        duo, active, wall = replay_anchored(case, mode)
        return duo, active, wall, "anchored"
    if plan:  # 非空列表 -> 绝对
        duo, active, wall = replay_absolute(case, mode)
        return duo, active, wall, "absolute"
    # 空列表（TC-36）账目用例，不产生会话
    return None, set(), 0.0, "aggregate"


# ---------------------------------------------------------------- 判据

def latency_gate(case, duo, active, metrics, judge_kind, mode):
    """确认时延：只记，不再当门槛（去窗后旧判据的前提没了）。

    2026-11-04 去窗（[[无确认窗流程]] §1.1）之后，旧判据「只取 `why == confirm_silence` 的静默确认
    p50 ≤ A0×1.10（944.6ms）」失效——强 complete 不再走那条静默路径（`why == eot_complete`，判完
    当刻认）。这里把所有确认路径一起量并**降级为 OBSERVED**：去窗后的新基线要重跑 A0/A1 才有
    （wiki §未决 2，重锚时机待拍），不拿带窗地板去判新流程。
    """
    confirms = [item["latency_ms"] for seat in ("D", "P") if seat in active
                for item in duo.confirms.get(seat, [])]
    if not confirms:
        return []
    p50 = round(statistics.median(confirms), 1)
    whys = sorted({item["why"] for seat in ("D", "P") if seat in active
                   for item in duo.confirms.get(seat, [])})
    return [{"check": "确认 p50（%s；带窗地板 %sms 只作历史对照）" % ("/".join(whys), A1_CEILING_MS),
             "key": "__latency__", "layer": "seat", "status": "OBSERVED",
             "detail": "p50=%sms%s" % (p50, "" if (mode == "real" and judge_kind == "real")
                                        else "（假判停，数字不可比）"),
             "blocked_by": []}]


def eval_case(case, duo, active, mode, judge_kind, audit=None):
    """算一条用例的 checks + verdict。

    `expect_fake_judge`（可选）：同一用例在**假判停**下的附加/覆盖期望。去窗后「确认原因」这一项
    依赖判停给的 confidence —— 真判停强 complete 当刻认（`eot_complete`），而 `--judge fake` 给
    real 用例的假判停刻意用弱 confidence（见 `eot_immediate`，避免假判停逐 partial 落地），走的是
    「可能说完」那一档（`confirm_silence`）。不写这一块的用例两种判停共用 `expect`。
    """
    expect = dict(case.get("expect") or {})
    if judge_kind != "real" and case.get("expect_fake_judge"):
        expect.update(case["expect_fake_judge"])
    if expect.get("reuse") is not None:
        return _eval_reuse(case)

    checks = []
    seat = seat_surface(duo, case) if duo is not None else {}
    # seat 层 + orchestra 层（check_expect 负责 BLOCKED/OBSERVED）
    checks = check_expect(expect, seat, audit, case)
    # 真判停时延时延门槛（并入检查）
    if duo is not None and judge_kind == "real" and mode == "real":
        checks.extend(latency_gate(case, duo, active, seat, judge_kind, mode))

    verdict = _verdict(case, checks, audit)
    return checks, verdict


def _verdict(case, checks, audit):
    statuses = [c["status"] for c in checks]
    if any(s == "FAIL" for s in statuses):
        return "FAIL"
    # wave-2 语义、无 audit -> STAGED
    if (case.get("wave") == 2 or case.get("depends")) and audit is None:
        return "STAGED"
    if any(s == "BLOCKED" for s in statuses):
        return "FAIL"  # 有 audit 但缺字段：不能 PASS；check 明细里写 BLOCKED/blocked_by
    return "PASS"


def _eval_reuse(case):
    """TC-34：跑既有回归 tests/test_confirm_gate.py，解析『合计：N 项通过』==53。"""
    checks = []
    ok = False
    detail = ""
    try:
        res = subprocess.run([sys.executable, "-B", str(REUSE_SCRIPT)],
                             capture_output=True, text=True, timeout=120)
        m = re.search(r"合计：(\d+) 项通过", res.stdout)
        got = int(m.group(1)) if m else None
        ok = got == 53
        detail = "tests/test_confirm_gate.py -> 合计 %s 项通过" % (got if got is not None else "N/A")
        if res.returncode != 0:
            ok = False
            detail += "，退出码 %d（%s）" % (res.returncode, res.stderr.strip()[-200:] if res.stderr else "")
    except Exception as error:  # noqa: BLE001
        detail = "reuse 运行异常 %r" % (error,)
    checks.append({"check": "既有回归 53/53 不变", "key": "reuse", "layer": "seat",
                   "status": "PASS" if ok else "FAIL", "detail": detail, "blocked_by": []})
    return checks, ("PASS" if ok else "FAIL")


def aggregate_tc36(duos, cases, audit=None):
    """TC-36 账目：整轮结束后聚合 eq（每席账，用已跑双席的计数）与 iu_eq（需 audit）。"""
    checks = []
    if audit is not None:
        # eq 从本次轮次所有双席会话聚合（llm_calls==consumed+wasted，每席）
        eq_ok = True
        for duo in duos:
            for seat in ("D", "P"):
                c = duo.seats[seat].counters
                eq_ok = eq_ok and (c["llm_calls"] == c["consumed"] + c["llm_wasted"])
        checks.append({"check": "每席 llm_calls==consumed+wasted（聚合）", "key": "eq",
                       "layer": "seat", "status": "PASS" if eq_ok else "FAIL",
                       "detail": "" if eq_ok else "存在账目不平", "blocked_by": []})
        # iu_eq 需 audit
        iu = audit.get("iu_eq", audit.get("iu")) if isinstance(audit, dict) else None
        if iu is None:
            checks.append({"check": "iu_open==0 且 iu_committed==au_executed+au_abandoned",
                           "key": "iu_eq", "layer": "orchestra", "status": "BLOCKED",
                           "detail": "audit 缺 iu_eq", "blocked_by": ["missing audit field: iu_eq"]})
            return checks, "FAIL"
        checks.append({"check": "iu_open==0 且 iu_committed==au_executed+au_abandoned",
                       "key": "iu_eq", "layer": "orchestra",
                       "status": "PASS" if bool(iu) else "FAIL",
                       "detail": "audit.iu_eq=%r" % (iu,), "blocked_by": []})
        return checks, ("PASS" if eq_ok and bool(iu) else "FAIL")
    # 无 audit -> staged
    return checks, "STAGED"


# ---------------------------------------------------------------- 校验

_REUSE = re.compile(r"合计：(\d+) 项通过")


def _load_audit(module_name, func_name):
    """解析 `MODULE:FUNC`。先用包/模块 import，失败了按 `REPO/<module>.py` 文件加载。

    `--audit tests._audit_smoke:fake_audit` 这种 `tests` 非包路径也能吃。
    """
    import importlib.util
    try:
        mod = importlib.import_module(module_name)
    except ImportError:
        # 尝试按相对仓库的 .py 文件加载
        candidates = []
        if module_name.startswith("tests."):
            candidates.append(REPO / (module_name.replace("tests.", "tests/") + ".py"))
        else:
            candidates.append(REPO / (module_name.replace(".", "/") + ".py"))
        loaded = False
        for path in candidates:
            if path.exists():
                spec = importlib.util.spec_from_file_location("_audit_provider", path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                loaded = True
                break
        if not loaded:
            raise SystemExit("找不到 audit 模块：%s（试了 %s）" % (module_name, [str(p) for p in candidates]))
    if not hasattr(mod, func_name):
        raise SystemExit("audit 模块 %s 里没有函数 %s" % (module_name, func_name))
    return getattr(mod, func_name)


def validate_cases(rows):
    """行为约束：可解析 + expect 每个 key ∈ REGISTRY ∪ meta；违规退出码 2。"""
    bad = []
    for row in rows:
        cid = row.get("case_id")
        for block in ("expect", "expect_fake_judge"):
            for k in (row.get(block) or {}):
                if k not in REGISTRY and k not in META_FIELDS:
                    bad.append((cid, block + "." + k))
        # 顶层 meta 控键允许存在
    if bad:
        for cid, k in bad:
            print("未知断言字段：%s 的 expect 含 %r（不在断言面 REGISTRY/meta）" % (cid, k))
        print("退出码 2：用例含未知断言字段", file=sys.stderr)
        raise SystemExit(2)
    return rows


# ---------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--wave", default="all", choices=("1", "2", "all"))
    ap.add_argument("--judge", default="fake", choices=("real", "fake"))
    ap.add_argument("--only", default="")
    ap.add_argument("--audit", default="")
    ap.add_argument("--out", default=str(DEFAULT_OUT))
    ap.add_argument("--dump-surface", action="store_true")
    args = ap.parse_args()

    if args.dump_surface:
        print(json.dumps(dump_surface(), ensure_ascii=False, indent=1))
        return 0

    rows = [json.loads(line) for line in CASES_FILE.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    rows = validate_cases(rows)

    by_id = {r["case_id"]: r for r in rows}
    if args.wave == "all":
        picked = rows
    elif args.wave == "1":
        picked = [r for r in rows if r.get("wave") == 1]
    else:
        picked = [r for r in rows if r.get("wave") == 2]
    if args.only:
        sel = set(args.only.split(","))
        picked = [r for r in picked if r["case_id"] in sel]

    # audit provider
    audit_fn = None
    if args.audit:
        module_name, func_name = args.audit.split(":", 1)
        audit_fn = _load_audit(module_name, func_name)

    # 真判停不可达检测
    want_real = args.judge == "real"
    health = _health_ok()
    if want_real and not health:
        print("真判停服务不可达（%s/health 非 ok）：显式 --judge real → 这些用例 SKIP" % EOT_JUDGE_BASE)
        mode = "real"
    else:
        mode = "real" if args.judge == "real" and health else "fake"

    results = []
    duos_collected = []
    skipped_real = want_real and not health

    # 先排 TC-36 到末尾（它要聚合整轮）
    picked_ordered = [r for r in picked if r["case_id"] != "TC-36"]
    if any(r["case_id"] == "TC-36" for r in picked):
        picked_ordered.append(by_id["TC-36"])

    for case in picked_ordered:
        cid = case["case_id"]
        expect = case.get("expect") or {}
        judge = case.get("judge")
        # 实际用的判停（`--judge fake` 会把 real 用例降级成假判停）：`expect_fake_judge` 靠它生效。
        judge_kind = pick_eot(case, mode)[1]
        print("[%s] %s" % (cid, case["title"]), flush=True)
        try:
            if skipped_real:
                results.append({"case_id": cid, "verdict": "SKIP",
                                "detail": "真判停不可达（显式 real）"})
                print("   SKIP  真判停不可达")
                continue
            if expect.get("reuse") is not None:
                checks, verdict = eval_case(case, None, set(), mode, judge_kind, audit_fn)
                results.append({"case_id": cid, "waves": case.get("wave"),
                                "staged": case.get("staged"), "verdict": verdict,
                                "checks": checks})
                print("   %-7s %s" % (verdict, _line(case, checks)))
                continue
            duo, active, wall, kind = replay_case(case, mode) if not skipped_real else (None, set(), 0.0, "x")
            if kind == "aggregate":
                # TC-36 排到末尾统一聚合
                continue
            if duo is not None:
                duos_collected.append(duo)
            audit = audit_fn(case, duo) if audit_fn else None
            checks, verdict = eval_case(case, duo, active, mode, judge_kind, audit)
            results.append({"case_id": cid, "waves": case.get("wave"),
                            "staged": case.get("staged"), "judge": judge,
                            "judge_kind": judge_kind,
                            "wall_s": wall, "verdict": verdict,
                            "checks": checks, "audit": audit})
            print("   %-7s %s│ wall=%.2fs" % (verdict, _line(case, checks), wall))
            if duo is not None:
                duo.close()
        except Exception as error:  # noqa: BLE001 - 一条失败不中断整轮
            print("   FAIL  运行异常 %r" % (error,))
            traceback.print_exc()
            results.append({"case_id": cid, "verdict": "FAIL", "detail": repr(error)})

    # TC-36 聚合
    if any(r["case_id"] == "TC-36" for r in picked_ordered):
        tc36 = by_id["TC-36"]
        audit = audit_fn(tc36, None) if audit_fn else None
        checks, verdict = aggregate_tc36([d for d in duos_collected if d], picked, audit)
        results.append({"case_id": "TC-36", "waves": 2, "staged": True,
                        "verdict": verdict, "checks": checks, "audit": audit})
        print("[TC-36] %s" % tc36["title"])
        print("   %-7s %s" % (verdict, _line(tc36, checks)))

    summary = {v: sum(1 for r in results if r["verdict"] == v)
               for v in ("PASS", "FAIL", "STAGED", "SKIP")}
    payload = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
               "mode": mode, "a1_ceiling_ms": A1_CEILING_MS,
               "summary": summary, "results": results}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("\n== %s == %s" % (out.name, summary))
    return 0


def _line(case, checks):
    """一行关键数字。"""
    bits = []
    for c in (checks or []):
        if c["status"] == "OBSERVED":
            bits.append("%s→%s" % (c["key"], c["status"]))
        elif c["status"] == "BLOCKED":
            bits.append("%s→BLOCKED(%s)" % (c["key"], ",".join(c.get("blocked_by") or [])))
        elif c["status"] == "FAIL":
            bits.append("%s→FAIL" % c["key"])
        elif c["status"] == "PASS":
            bits.append("%s✓" % c["key"])
    return " | ".join(bits) if bits else ""


if __name__ == "__main__":
    sys.exit(main())