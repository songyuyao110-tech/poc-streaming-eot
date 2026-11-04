# 双路 ASR 文本用例 · wave-2 断言面落地（设计 / 接续文档）

卡 `4d3267ff`（Dev）｜2026-11-03｜上游：PM `t_742d006e`（37 条用例 + 主文档 `poc-dual-asr-text-cases-spec.md`）
依赖：C1 已交付（`DualSeatSession` 双席、不串席）；C2/C3/C4（会话脑/判帧/游戏框架）**尚未落地**。

---

## 1. 一句话与波次

把 PM 交付的 37 条「双路 ASR 文本流式用例」从 `tmp/` 落进业务仓，并为 wave-2 的 20 条
（依赖 C2/C3/C4）定义并落地**可运行的断言面**：每条 expect 字段都有 canonical 名字、层级、
语义、取值来源；C2–C4 未落地时逐条报 `STAGED`（不记 FAIL），落地后同一命令直接转 PASS/FAIL，
**不需要改用例**。

| 波次 | 用例数 | 构成 | 断言层 | 当前状态 |
|---|---|---|---|---|
| wave-1 | 17 | TC-01..TC-15（16 条判停/隔离文本用例）+ TC-34（引用既有回归 53/53） | seat 层 | ✅ 可跑，真/假判停 |
| wave-2 | 20 | TC-16..TC-25（G3/G4 会话脑 C2）＋ TC-26..TC-29（游戏框架 C4）＋ TC-30..TC-33（共聊/分控 C3）＋ TC-35(C3)＋ TC-36(C2 账目) | seat 观测＋orchestra 断言 | ⏸ `STAGED`，等 C2–C4 |

**缺 C2/C3/C4 各几条（从用例集统计出来的真实数字）**：wave-2 里 `depends` 含 C2 的 **14 条**，
含 C3 的 **11 条**，含 C4 的 **4 条**；分布 `{C2×5, C2+C3×9, C4×4, C3×2}`。即：
- C4（游戏框架）缺席挡 **4 条**（TC-26/27/28/29）；
- C3（判帧/高危座检）缺席挡住 **11 条**（含 TC-20/30/31/32/33/35 等）；
- C2（会话脑/动作脑）缺席挡 **14 条**。

总集 37 条 = 16 判停/隔离 + 1 回归 + 20 语义。

---

## 2. 断言面字段字典

入口：`src/assert_surface.py` 的 `REGISTRY`（`--dump-surface` 可导出）。

### 2.1 seat 层（owner 全 `-`，只用 `DualSeatSession` 就能算，现在可取）

| 字段 | 类型 | 语义 | 当前可取 |
|---|---|---|---|
| `decisions` | `{seat:int\|[lo,hi]}` | 该席 `consumed_count` | ✅ |
| `segments` | `{seat:int\|[lo,hi]}` | 该席 `consumed_segments` | ✅ |
| `confirm_why` | `[str]` | 任一席确认路径覆盖列表每值（`confirm_silence`/`max_silence`…） | ✅ |
| `prefix_confirm_max` | `int` | 「最后一字前进 CONFIRMED」席级次数上限 | ✅ |
| `no_splice` | `bool` | 每席消费字符集 ⊆ 该席 script 字符集（跨段允许、跨人禁止） | ✅ |
| `llm_after_confirm` | `bool` | D 席 `t_llm_start >= t_confirmed`（弱 complete 不抢跑） | ✅ |
| `eot_skipped_reason` | `str` | 任一席存在该 reason 的 `eot_skipped` | ✅ |
| `late_verdict_drives` | `bool` | 存在 timeout 之后到 `MAYBE_DONE(eot_complete)` | ✅ |
| `no_cross` | `bool` | `cross_cancel==0 and cross_reset==0` | ✅ |
| `own_text_only` | `bool` | 两席消费文本 normalize 后互不为子串 | ✅ |
| `own_cancel` | `{seat:int}` | 该席 `llm_cancel` reason∈{speech_start,new_text} 次数 | ✅ |
| `ask_user` | `int` | 全局 `llm_decision` kind==ask_user 次数 | ✅ |
| `decide` | `int` | 全局 `llm_decision` 次数 | ✅ |
| `eq` | 不变式/席 | 每席 `llm_calls==consumed+llm_wasted` | ✅ |
| `reuse` | `str` 路径 | 跑 `tests/test_confirm_gate.py` 解「合计：N」==53 | ✅ |
| `record_only` | `bool` | deprecated（Q1 收口后无使用） | ✅（不判） |

### 2.2 orchestra 层（C2/C3/C4 的 audit 字段，当前缺席 → `BLOCKED`，不可取）

| 字段 | 类型 | 语义 | owner |
|---|---|---|---|
| `actions_effective` | int | 真正生效动作数（tool+args+scope 归并） | C2 |
| `repeat_action` | int | 同一动作重复下发次数 | C2 |
| `receipt_has_both` | bool | 合并回执含两席标识 | C2 |
| `au_executed` | int | 已下发 AU 数 | C2 |
| `au_abandoned` | int | 已放弃 AU 数 | C2 |
| `wrong_seat` | int | 错座执行次数 | C2 |
| `blocked_by_seat` | int | 发起席无权/需主驾拦下次数 | C3 |
| `direct_exec` | int | 高危池未经确认直接执行次数 | C3 |
| `params_include` | [str] | 最终动作参数值包含全部给定子串 | C2 |
| `final_param` | str | 最终动作某参数值（字符串）== 期望 | C2 |
| `coverage_receipt` | int | 覆盖回执次数 | C2 |
| `game_session_open` | int | 游戏会话新开次数 | C4 |
| `game_join` | int | 第二席加入同一会话次数 | C4 |
| `turn_owner_errors` | int | 作答归属错误次数 | C4 |
| `game_turns` | int | 游戏内作答轮数 | C4 |
| `frame_switch` | int | 共聊/分控切换生效次数 | C3 |
| `merge_errors` | int | 误合并次数 | C3 |
| `correction_effective` | int | 一句纠正生效次数 | C3 |
| `mode_declared_required` | int | 需用户先声明模式的次数 | C3 |
| `iu_eq` | 不变式 | `iu_open==0 and iu_committed==au_executed+au_abandoned` | C2 |

### 2.3 meta 层（非断言，回放器把控制信息读走，判定跳过）

`staged` / `depends` / `repeat_action_allowed` / `backend_dedupe`。

---

## 3. 判定状态机

| 状态 | 含义 | 触发条件 |
|---|---|---|
| `PASS` | 全部硬判据通过 | 任一 realm 门槛 check 全 PASS（wave-1；wave-2 且给了完整 audit） |
| `FAIL` | 至少一条硬判据不通过 | 任一 check status==FAIL |
| `STAGED` | 语义依赖 C2–C4，暂缓判定 | wave-2 且 `depends` 非空且无 `--audit` |
| `BLOCKED` | orchestra 字段拿不到 | 无 audit 或 audit 缺字段（check 级；blocked_by 写明 depends 或缺失字段名） |
| `OBSERVED` | seat 观测如实给出但不作门槛 | wave-2 无 audit 时 seat 层检查（语义要靠 C2–C4） |
| `SKIP` | 真判停不可达且显式 `--judge real` | 8009 `/health` 非 ok 时显式 real → SKIP，退出码 0 |

`--judge fake` 时：real 用例改用「立即 complete」假判停，静默确认 p50 ≤ 944.6ms 等任何
时延门槛降级 `OBSERVED`（假判停数字不可比）。

---

## 4. 用户 Q1/Q2/Q3 拍板落地位置

| 拍板 | 落地点 | 字段 |
|---|---|---|
| **Q1 同句双拾**：允许执行两次、Agent 不去重、前端保证两路干净 | TC-13/14/15：删 `record_only`，`expect.decisions={"D":1,"P":1}`，`checks` 改写为「两席各 1 次决策＝两次执行」 | `repeat_action_allowed:2`、`backend_dedupe:false`、前端契约入 `checks` |
| **Q2 判停超时后迟到 verdict**：维持现状，迟到仍驱动 | TC-06 `expect.late_verdict_drives:true`；TC-06A `expect.late_verdict_drives:false`（5s 不回 → 不驱动） | 只在 `expect` 里写（不重复到顶层）；`checks` 追加「迟到 verdict 仍驱动状态机 = 用户 Q2 锁定口径」 |
| **Q3 语义类 18 条**：先留脚本等 C2–C4 | wave-2 全部保留，`staged:true`，`depends` 字段 | 回放器依赖缺席时报 `STAGED` 并列出缺哪些字段 |

> 注：Q3 说的是「语义类 18 条」的保留姿势，落地后 wave-2 语义集即 `STAGED`。

---

## 5. C2–C4 接续方式（audit provider）

接口：`fn(case, duo) -> dict`，返回 `ORCHESTRA_FIELDS` 里的字段（audit 对象）。
只要接上 `--audit MODULE:FUNC`，wave-2 全部门槛化 `PASS|FAIL`；audit 缺字段按 `BLOCKED`
并在 summary 列缺失字段名。

- **谁负责**：C2 → 前 11 个动作脑字段 + `iu_eq`；C3 → `blocked_by_seat/direct_exec/frame_switch/merge_errors/correction_effective/mode_declared_required`；C4 → 游戏框架 4 字段。
- **同一命令直接转判**：落地的 C2/C3/C4 会把自己面向该用例的 audit 数写进 `fn(case, duo)`，
  无需改写 `tests/dual_asr_text_cases.jsonl`（expect 已含全部期望值）。
- **C2 语义覆盖 seat 观测**：个别用例（如 TC-16「我也要」合并）在 C2 落盘后 `decisions`
  是合并后真值，audit 可携带同名 seat 键覆盖裸状态机观测（断言面支持 audit 同名键优先）。
- 假 provider 示证：`tests/_audit_smoke.py::fake_audit`（只证明通路，不进入 `src/`）。

---

## 6. 复现命令

```bash
cd /Users/YUYSONG/Work/poc-streaming-eot
PY=/Users/YUYSONG/Work/tmp/jev-reopt-checkouts/dev-streaming-eot/.venv/bin/python

# 离线自检（不连 8009，<10s）
$PY -B tests/test_assert_surface.py                       # 期望 6/6 例通过，exit 0

# 全量假判停（无网络也能全绿）：wave-1 17 PASS（16 文本 + TC-34）、wave-2 20 STAGED、exit 0
$PY -B tests/run_dual_asr_text_cases.py --wave all --judge fake --out /tmp/run-fake.json

# 导出断言面契约
$PY -B tests/run_dual_asr_text_cases.py --dump-surface | head -40

# 真 8009 冒烟（需隧道）：3 例真判停
$PY -B tests/run_dual_asr_text_cases.py --wave 1 --only TC-01,TC-07,TC-13 --judge real

# --audit 门槛化通路示证（TC-16 应 PASS）
$PY -B tests/run_dual_asr_text_cases.py --wave 2 --only TC-16 \
      --audit tests._audit_smoke:fake_audit
```

真判停不可达时：默认 `--judge fake`；显式 `--judge real` → 报 `SKIP`、退出码 0。

---

## 7. 已知边界

- **不改业务仓行为**：`src/turn_session.py`、`src/dual_seat.py`、`src/confirm_orchestrator.py`、
  `ui/`、`tests/test_confirm_gate.py` 一行不动；本卡只新增 `assert_surface.py` + tests 三件套 + docs + tests/README 2 行。
- **不验决策质量**：选工具/决策在本套是固定假实现（`select`/`decide`），只验「是否出决策/出几次」。
- **不验音频 / 串音**：两路干净 ASR 由前端保证（用户 Q1 拍板），Agent 侧只证不串席。
- **假判停数字不进时延门槛**：`--judge fake` 下 p50 ≤ 944.6ms 降级 OBSERVED。
- **假判停与 speculative LLM**：`eot_immediate`（fake 给 real 用例）用弱 confidence，避免在
  假判停下逐 partial 抢跑制造 self-cancel、污染 TC-09 隔离判据（详见该函数 docstring）。
- **TC-06A 的 late_verdict_drives**：假判停 `eot_silent` 5s 后才返回 complete（与 PM 同口径），
  此时 max_silence 早已收口、suffix 为空，迟到 verdict 走 `empty_suffix` 不驱动 MAYBE_DONE；
  Q2 锁定口径 False，实测 3/3 稳定（不是靠改假实现凑出来的）。