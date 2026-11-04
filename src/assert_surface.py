"""断言面契约（测试支撑代码，非业务运行时）。

本模块只被 `tests/` 消费（`run_dual_asr_text_cases.py` 回放器、`test_assert_surface.py`
离线自检）。**不得被 `turn_session` / `dual_seat` / `confirm_orchestrator` 在运行时 import**；
若有人试图在业务仓 src 启动链里 import，会违背「断言面 = 测试层」的分层约定。

职责（对应卡 4d3267ff，wave-2 断言面落地）：
- `REGISTRY`：全量断言词汇表。每条 expect 里的 key 都属于这里（seat / orchestra 两层）
  或 meta 集合；`check_expect` 据此决定怎么取数、怎么判。
- `seat_surface(duo, case)`：只用 `DualSeatSession` 的 summary / events / confirms 算出
  seat 层观测值（判停/隔离层，现在就能跑，不需要 C2–C4）。
- `check_expect(expect, seat, audit, case)`：把一条用例的 `expect` 逐 key 判 PASS/FAIL/
  BLOCKED/OBSERVED。orchestra 字段（C2–C4 的 audit）缺席时记 BLOCKED，绝不记 FAIL。
- `dump_surface()`：导出 REGISTRY + ORCHESTRA_FIELDS，供 `--dump-surface` 与自检用。

分层口径：
- **seat 层**：只用双席会话就能算的观测，owner 全是 "-"。
- **orchestra 层**：C2/C3/C4 必须发布的 audit 字段，当前缺席；缺席 → BLOCKED。
  若 audit provider 返回了同名的 seat 层键（例如 C2 落盘后的 `decisions` 是合并后的真值），
  以 audit 为准（C2 语义覆盖裸状态机，见 test-design 文档）。
- **meta 层**：`staged` / `depends` / `repeat_action_allowed` / `backend_dedupe`，
  不是断言，判定时跳过（只在 REPLAYER 里当控制信息读）。

禁止：本模块写 C2/C3/C4 的业务逻辑（会话脑 / 判帧 / 游戏框架都不在这里实现）。
"""
from __future__ import annotations

import statistics

# 用户 2026-11-03 拍板在用例里的常量（写进 docstring，别散落）。
A1_CEILING_MS = round(858.7 * 1.10, 1)  # 静默确认 p50 ≤ A0×1.10；假判停数字不可比，不可当门槛

# 断言词汇表：key -> dict。layer: seat（判停/隔离）| orchestra（C2-C4 audit）| meta（非断言）。
# type / owner / semantics / source 是给 dump_surface() 与文档用的元数据。
REGISTRY: dict[str, dict] = {
    # ---------------- seat 层（owner 全是 "-"，只用 DualSeatSession 就能算） ----------------
    "decisions": {"layer": "seat", "type": "int|interval/seat",
                  "owner": "-",
                  "semantics": "该席落地决策次数（= 该席 consumed_count）",
                  "source": "DualSeatSession.seats[seat].consumed_count"},
    "segments": {"layer": "seat", "type": "int|interval/seat",
                 "owner": "-",
                 "semantics": "该席被消费的切段数（= 该席 consumed_segments）",
                 "source": "DualSeatSession.seats[seat].consumed_segments"},
    "confirm_why": {"layer": "seat", "type": "list[str]",
                    "owner": "-",
                    "semantics": "任一席确认事件的 why（如 confirm_silence / max_silence）覆盖列表里每个值",
                    "source": "DualSeatSession.confirms[*][].why"},
    "prefix_confirm_max": {"layer": "seat", "type": "int",
                           "owner": "-",
                           "semantics": "「最后一字之前就进入 CONFIRMED」的席级次数上限（正常句应为 0）",
                           "source": "each seat: state_change->CONFIRMED with t_ms < t_last_word"},
    "no_splice": {"layer": "seat", "type": "bool",
                  "owner": "-",
                  "semantics": "每席消费文本的字符集 ⊆ 该席 script 文本字符集（跨段合并允许、跨人拼接不允许）",
                  "source": "consumption texts vs case.script texts（normalize 后比字符集）"},
    "llm_after_confirm": {"layer": "seat", "type": "bool",
                          "owner": "-",
                          "semantics": "D 席第一次 LLM 启动不早于确认（弱 complete 不抢跑）",
                          "source": "DualSeatSession.seats.D.t_llm_start >= t_confirmed"},
    "eot_skipped_reason": {"layer": "seat", "type": "str",
                           "owner": "-",
                           "semantics": "任一席存在该 reason（如 timeout）的 eot_skipped 事件",
                           "source": "duo.events: event==eot_skipped and reason==X"},
    "late_verdict_drives": {"layer": "seat", "type": "bool",
                            "owner": "-",
                            "semantics": "存在 eot_skipped(timeout)，且其后有 state_change->MAYBE_DONE(why=eot_complete)",
                            "source": "duo.events 存在顺序：timeout 之后到 MAYBE_DONE/eot_complete"},
    "no_cross": {"layer": "seat", "type": "bool",
                 "owner": "-",
                 "semantics": "duo.counters.cross_cancel==0 且 cross_reset==0（跨席不重置/不取消）",
                 "source": "DualSeatSession.counters"},
    "own_text_only": {"layer": "seat", "type": "bool",
                      "owner": "-",
                      "semantics": "两席消费文本 normalize 后互不为对方的子串（相等不算串）",
                      "source": "consumption texts（D vs P 双向）"},
    "own_cancel": {"layer": "seat", "type": "int/seat",
                   "owner": "-",
                   "semantics": "该席 llm_cancel 中 reason∈{speech_start,new_text} 的次数",
                   "source": "duo.seats[seat].events: llm_cancel reason in {speech_start,new_text}"},
    "ask_user": {"layer": "seat", "type": "int",
                 "owner": "-",
                 "semantics": "全局 llm_decision 中 decision.kind==ask_user 的次数（回问）",
                 "source": "duo.events: llm_decision with decision.kind==ask_user"},
    "decide": {"layer": "seat", "type": "int",
               "owner": "-",
               "semantics": "全局 llm_decision 次数",
               "source": "duo.events: llm_decision 计数"},
    "eq": {"layer": "seat", "type": "invariant/seat",
           "owner": "-",
           "semantics": "每席 llm_calls == consumed + llm_wasted（账目不变量；TC-36 席级半条）",
           "source": "DualSeatSession.seats[seat].counters"},
    "reuse": {"layer": "seat", "type": "path",
              "owner": "-",
              "semantics": "跑既有回归脚本 tests/test_confirm_gate.py，期望『合计：53 项通过』",
              "source": "subprocess 跑 tests/test_confirm_gate.py 解析"},
    "record_only": {"layer": "seat", "type": "bool",
                    "owner": "-",
                    "semantics": "兼容保留：37 条里 Q1 收口后已无使用（deprecated）",
                    "source": "历史字段，370 用例不再使用"},
    # ---------------- orchestra 层（C2-C4 的 audit 字段，当前缺席 -> BLOCKED） ----------------
    "actions_effective": {"layer": "orchestra", "type": "int", "owner": "C2",
                          "semantics": "真正生效的动作数（按 tool+args+scope 归并）",
                          "source": "C2 audit.actions_effective"},
    "repeat_action": {"layer": "orchestra", "type": "int", "owner": "C2",
                      "semantics": "同一动作（tool+args+scope）重复下发的次数",
                      "source": "C2 audit.repeat_action"},
    "receipt_has_both": {"layer": "orchestra", "type": "bool", "owner": "C2",
                         "semantics": "合并回执文本是否同时含两席的标识/内容",
                         "source": "C2 audit.receipt_has_both"},
    "au_executed": {"layer": "orchestra", "type": "int", "owner": "C2",
                    "semantics": "已下发（执行）的动作单元 AU 数",
                    "source": "C2 audit.au_executed"},
    "au_abandoned": {"layer": "orchestra", "type": "int", "owner": "C2",
                     "semantics": "已放弃的 AU 数",
                     "source": "C2 audit.au_abandoned"},
    "wrong_seat": {"layer": "orchestra", "type": "int", "owner": "C2",
                   "semantics": "错座执行次数",
                   "source": "C2 audit.wrong_seat"},
    "blocked_by_seat": {"layer": "orchestra", "type": "int", "owner": "C3",
                        "semantics": "因发起席无权/需主驾而拦下的次数",
                        "source": "C3 audit.blocked_by_seat"},
    "direct_exec": {"layer": "orchestra", "type": "int", "owner": "C3",
                    "semantics": "高危池工具未经确认直接执行的次数",
                    "source": "C3 audit.direct_exec"},
    "params_include": {"layer": "orchestra", "type": "list[str]", "owner": "C2",
                       "semantics": "最终动作的参数值需包含全部给定子串",
                       "source": "C2 audit.params_include（判定用）"},
    "final_param": {"layer": "orchestra", "type": "str", "owner": "C2",
                    "semantics": "最终动作某参数值（字符串）等于期望",
                    "source": "C2 audit.final_param"},
    "coverage_receipt": {"layer": "orchestra", "type": "int", "owner": "C2",
                         "semantics": "覆盖回执次数（后发者写明覆盖先发者）",
                         "source": "C2 audit.coverage_receipt"},
    "game_session_open": {"layer": "orchestra", "type": "int", "owner": "C4",
                          "semantics": "游戏会话新开次数",
                          "source": "C4 audit.game_session_open"},
    "game_join": {"layer": "orchestra", "type": "int", "owner": "C4",
                  "semantics": "第二席加入同一游戏会话的次数",
                  "source": "C4 audit.game_join"},
    "turn_owner_errors": {"layer": "orchestra", "type": "int", "owner": "C4",
                          "semantics": "作答归属错误次数",
                          "source": "C4 audit.turn_owner_errors"},
    "game_turns": {"layer": "orchestra", "type": "int", "owner": "C4",
                   "semantics": "游戏内作答轮数",
                   "source": "C4 audit.game_turns"},
    "frame_switch": {"layer": "orchestra", "type": "int", "owner": "C3",
                     "semantics": "共聊/分控切换生效次数",
                     "source": "C3 audit.frame_switch"},
    "merge_errors": {"layer": "orchestra", "type": "int", "owner": "C3",
                     "semantics": "误合并次数",
                     "source": "C3 audit.merge_errors"},
    "correction_effective": {"layer": "orchestra", "type": "int", "owner": "C3",
                             "semantics": "一句纠正生效的次数",
                             "source": "C3 audit.correction_effective"},
    "mode_declared_required": {"layer": "orchestra", "type": "int", "owner": "C3",
                               "semantics": "需要用户先声明模式的次数",
                               "source": "C3 audit.mode_declared_required"},
    "iu_eq": {"layer": "orchestra", "type": "invariant", "owner": "C2",
              "semantics": "iu_open==0 且 iu_committed==au_executed+au_abandoned（编排层账目）",
              "source": "C2 audit.iu_open / iu_committed / au_executed / au_abandoned"},
}

# C2-C4 必须发布的 audit 字段清单（它们也就是 REGISTRY 里 orchestra 层的键）。
ORCHESTRA_FIELDS: tuple[str, ...] = tuple(
    key for key, meta in REGISTRY.items() if meta["layer"] == "orchestra")

# meta 层键（非断言，判定时跳过）。
META_FIELDS: frozenset[str] = frozenset(
    {"staged", "depends", "repeat_action_allowed", "backend_dedupe"})

# 断言里 scalar 与区间两套匹配都要支持。
def _match(value, spec) -> bool:
    if isinstance(spec, list) and len(spec) == 2:
        return spec[0] <= value <= spec[1]
    return value == spec


def _normalize(text: str) -> str:
    from turn_session import normalize  # 惰性：避免 import 时耦合（虽然本模块已是测试层）
    return normalize(text)


def _prefix_confirms(session) -> int:
    last_word = session.t_last_word
    if last_word is None:
        return 0
    return sum(1 for event in session.events
               if event.get("event") == "state_change" and event.get("to") == "CONFIRMED"
               and (event.get("t_ms") or 0) < last_word)


def seat_surface(duo, case=None) -> dict:
    """从双席会话取 seat 层观测值（判停/隔离层，现在就能算）。

    `case` 可选：仅供 no_splice 取该席 script 文本字符集。
    """
    case = case or {}
    script_texts = {}
    for item in (case.get("script") or []):
        mic = item.get("mic", "")
        seat = mic.replace("mic-", "") if mic.startswith("mic-") else mic
        script_texts.setdefault(seat, set()).update(_normalize(item.get("text", "")))

    decisions = {seat: duo.seats[seat].consumed_count for seat in ("D", "P")}
    segments = {seat: duo.seats[seat].consumed_segments for seat in ("D", "P")}
    confirm_whys = [item["why"] for seat in ("D", "P") for item in duo.confirms.get(seat, [])]

    # no_splice：每席消费文本字符集 ⊆ 该席 script 文本字符集
    no_splice = True
    for seat in ("D", "P"):
        consumed_chars = set().union(*[
            set(_normalize(rec["text"])) for rec in duo.seats[seat].consumptions
        ]) if duo.seats[seat].consumptions else set()
        base = script_texts.get(seat, set())
        if base and not consumed_chars <= base:
            no_splice = False

    # own_text_only：两席消费文本 normalize 后互不为子串（相等不算串）
    texts = {seat: [_normalize(rec["text"]) for rec in duo.seats[seat].consumptions]
             for seat in ("D", "P")}
    own_text_only = True
    for seat, other in (("D", "P"), ("P", "D")):
        for own in texts[seat]:
            for foreign in texts[other]:
                if foreign and foreign in own and foreign != own:
                    own_text_only = False

    d = duo.seats["D"]
    llm_after_confirm = (d.t_llm_start is not None and d.t_confirmed is not None
                         and d.t_llm_start >= d.t_confirmed)

    events = duo.events
    eot_reasons = sorted({e.get("reason") for e in events
                          if e.get("event") == "eot_skipped" and e.get("reason")})

    # late_verdict_drives：存在 eot_skipped(timeout)，且其后有 state_change->MAYBE_DONE(why=eot_complete)
    late_verdict_drives = False
    timeout_seen = False
    for e in events:
        if e.get("event") == "eot_skipped" and e.get("reason") == "timeout":
            timeout_seen = True
        elif timeout_seen and e.get("event") == "state_change" \
                and e.get("to") == "MAYBE_DONE" and e.get("why") == "eot_complete":
            late_verdict_drives = True

    own_cancel = {}
    for seat in ("D", "P"):
        own_cancel[seat] = sum(1 for e in duo.seats[seat].events
                               if e.get("event") == "llm_cancel"
                               and e.get("reason") in ("speech_start", "new_text"))

    ask_user = sum(1 for e in events
                   if e.get("event") == "llm_decision"
                   and (e.get("decision") or {}).get("kind") == "ask_user")
    decide = sum(1 for e in events if e.get("event") == "llm_decision")

    eq = {}
    for seat in ("D", "P"):
        c = duo.seats[seat].counters
        eq[seat] = (c["llm_calls"] == c["consumed"] + c["llm_wasted"])

    return {
        "decisions": decisions,
        "segments": segments,
        "confirm_why": confirm_whys,
        "prefix_confirm_max": max(_prefix_confirms(duo.seats[s]) for s in ("D", "P")),
        "no_splice": no_splice,
        "llm_after_confirm": llm_after_confirm,
        "eot_skipped_reason": eot_reasons,
        "late_verdict_drives": late_verdict_drives,
        "no_cross": duo.counters["cross_cancel"] == 0 and duo.counters["cross_reset"] == 0,
        "own_text_only": own_text_only,
        "own_cancel": own_cancel,
        "ask_user": ask_user,
        "decide": decide,
        "eq": eq,
        "counters": {k: v for k, v in duo.counters.items()},
        "errors": duo.counters["errors"],
    }


_SEAT_KEYS = frozenset(key for key, meta in REGISTRY.items() if meta["layer"] == "seat")
_ORCH_KEYS = frozenset(key for key, meta in REGISTRY.items() if meta["layer"] == "orchestra")


def _detail_for(key, observed, spec) -> str:
    return "期望 %s，实测 %s" % (spec, observed)


def check_expect(expect: dict, seat: dict, audit: dict | None, case: dict) -> list[dict]:
    """逐条判定一条用例的 expect。

    返回 `[{"check", "key", "layer", "status", "detail", "blocked_by"}]`。
    status ∈ {PASS, FAIL, BLOCKED, OBSERVED}。

    规则：
    - seat 层 key：有 audit（C2 语义可覆盖同名键）或用例不依赖编排层时门槛化 PASS/FAIL；
      wave-2 且无 audit 时记 OBSERVED（语义要靠 C2-C4，观测值如实给出）。
    - orchestra 层 key：无 audit -> BLOCKED（blocked_by=case.depends 或缺失字段）；
      有 audit 但缺该字段 -> BLOCKED（blocked_by=缺失字段名）；有值 -> PASS/FAIL。
    - meta 层 key：跳过。
    """
    depend = case.get("depends") or []
    wave2_noaudit = audit is None and case.get("wave") == 2 and bool(depend)
    results = []

    for key, spec in (expect or {}).items():
        if key not in REGISTRY:                      # 未知字段在回放器前置校验拦截，这里兜底
            results.append({"check": key, "key": key, "layer": "unknown",
                            "status": "FAIL", "detail": "未知断言字段", "blocked_by": []})
            continue
        meta = REGISTRY[key]
        if meta["layer"] == "meta":
            continue  # meta 不是断言，跳过

        if meta["layer"] == "seat":
            # C2-C4 语义可覆盖 seat 层观测（如合并后 decisions 真值），audit 同名键优先
            observed = (audit or {}).get(key, seat.get(key))
            if wave2_noaudit:
                results.append({"check": meta["semantics"], "key": key, "layer": "seat",
                                "status": "OBSERVED",
                                "detail": "语义依赖 C2-C4，观测值=%s（期望 %s）" % (observed, spec),
                                "blocked_by": []})
                continue
            ok = _verdict_seat(key, observed, spec)
            results.append({"check": meta["semantics"], "key": key, "layer": "seat",
                            "status": "PASS" if ok else "FAIL",
                            "detail": _detail_for(key, observed, spec), "blocked_by": []})
            continue

        # orchestra 层
        if audit is None:
            results.append({"check": meta["semantics"], "key": key, "layer": "orchestra",
                            "status": "BLOCKED",
                            "detail": "缺少 %s audit（该层未落地）" % meta["owner"],
                            "blocked_by": list(depend)})
            continue
        if key not in audit:
            results.append({"check": meta["semantics"], "key": key, "layer": "orchestra",
                            "status": "BLOCKED",
                            "detail": "audit 缺少字段 %s" % key,
                            "blocked_by": ["missing audit field: " + key]})
            continue
        ok = _verdict_orch(key, audit[key], spec)
        results.append({"check": meta["semantics"], "key": key, "layer": "orchestra",
                        "status": "PASS" if ok else "FAIL",
                        "detail": _detail_for(key, audit[key], spec), "blocked_by": []})
    return results


def _verdict_seat(key, observed, spec) -> bool:
    """seat 层逐 key 的匹配口径。"""
    if key in ("decisions", "segments", "own_cancel"):
        # 形如 {"D": n|[lo,hi]} 的按席匹配
        if not isinstance(spec, dict):
            return False
        for seat, want in spec.items():
            val = (observed or {}).get(seat)
            if val is None or not _match(val, want):
                return False
        return True
    if key == "confirm_why":
        got = set(observed or [])
        return all(w in got for w in spec)
    if key == "eot_skipped_reason":
        return spec in set(observed or [])
    if key == "own_text_only":
        return bool(observed)
    if key in ("no_cross", "no_splice", "llm_after_confirm", "late_verdict_drives"):
        return bool(observed) == bool(spec)
    if key == "eq":
        # 席级半条：expect['eq'] 是字符串不变式，这里按「每席都成立」判定
        if isinstance(observed, dict):
            return all(observed.get(s) for s in ("D", "P"))
        return bool(observed)
    if key == "reuse":   # 由回放器另行处理（subprocess），这里不判
        return bool(observed)
    if key == "record_only":  # deprecated，命中即废弃
        return False
    # int 字段：ask_user / decide / prefix_confirm_max
    return _match(observed, spec)


def _verdict_orch(key, value, spec) -> bool:
    if key == "receipt_has_both":
        return bool(value)
    if key == "iu_eq":
        # "iu_open==0 且 iu_committed==au_executed+au_abandoned"
        return bool(value)
    if key == "params_include":
        # 判断另算：这里假定 audit['params_include'] 是已经把「包含判定」算成的 bool；
        # 若要支持 list 语义，回放器/audit 提供方应预先给出 list_or_bool。
        if isinstance(value, list):
            # 允许 audit 直接给 bool；给 list 时按 audit 自己声明是否全部包含
            return _match_bool_or_list(value, spec)
        return bool(value)
    if key == "repeat_action":
        # 硬门槛「重复动作 0」用等于
        return _match(value, spec)
    if isinstance(spec, (int, float)):
        return value == spec
    if isinstance(spec, bool):
        return bool(value) == spec
    return value == spec


def _match_bool_or_list(value, spec) -> bool:
    # spec 是 [str], value 可能是 bool（audit 已判定）或一串值
    if isinstance(value, (bool, int)):
        return bool(value)
    if isinstance(value, list):
        vals = [str(v) for v in value]
        return all(any(w in v for v in vals) for w in spec)
    return False


def dump_surface() -> dict:
    """导出断言面契约（registry + orchestra_fields + meta）。"""
    return {
        "registry": {key: dict(meta) for key, meta in REGISTRY.items()},
        "orchestra_fields": list(ORCHESTRA_FIELDS),
        "meta_fields": sorted(META_FIELDS),
        "seat_keys": sorted(_SEAT_KEYS),
        "a1_ceiling_ms": A1_CEILING_MS,
    }


# 档案里各字段的默认「当前是否可取」由 layer 决定（seat 可取 / orchestra 待 C2-C4）。
def layer_of(key: str) -> str:
    return REGISTRY[key]["layer"]