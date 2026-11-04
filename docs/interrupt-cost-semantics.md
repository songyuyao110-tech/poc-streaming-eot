# 生成中打断的成本口径（作废也计一次调用）

> 范围：wave-2 P0-3（文档口径）。只写清楚「作废不计调用」是错误的、实际口径是什么；
> 本期**不做**真 HTTP abort / 流式 cancel（未拍板），也**不**引入 `llm_max_attempts` /
> `turn_deadline_ms` 之类的预算/封顶动作（那是上游仓库的东西，本仓没有，且本轮不搬）。

## 1. 一句话口径

**结果作废 ≠ 调用作废。** 用户生成中打断，作废的是「本轮决策的结果」，不是「这次 LLM 调用」：
那次调用仍然已经发出（甚至已经跑完），成本照计。观测面上它记为 `llm_wasted`，不记入
`consumed`；`turn_void` 只是「本轮决策作废」的可观测事件，不动账目。

## 2. 为什么「作废」省不下钱

- `decide()` 是**一次同步、非流式**的调用（`src/core_pipeline_run.py:272-323`，
  `slow_turn(brief, ...)` / KEV fallback），发出后直到返回前没有任何取消钩子。
- 打断时 `_cancel_inflight_llm` 只做三件事（`src/turn_session.py:808-842`）：
  1. 把已备好的 speculative 结果从 `_llm_prepared` 里摘掉、弃置；
  2. 把在途的 llm job 打上 `canceled=True`（**不中断 worker、不撤销 HTTP 请求**）；
  3. 若是本席再开口触发，发一次 `turn_void`（纯观测）。
- worker 线程里那次 HTTP 会**继续跑到结束**：要么已返回、要么还在占用一个线程槽位。
  返回后 `_poll` → `_on_llm_result` 看到 `job["canceled"]` 直接丢弃，结果不进状态机。

结论：**作废一次在途决策 = 一次完整的调用成本 + 一个 worker 线程的占用**，只是结果被丢弃。

## 3. 账目口径

每席计数器（`summary()` 里 `**self.counters` 全量透出，turn_done 事件里也带全量）：

| 字段 | 含义 |
| --- | --- |
| `llm_calls` | 发出的决策调用总次数（含作废的），**不减** |
| `llm_wasted` | 结果被作废的次数（打断 / stale_text / confirm_wait 清场 / shortlist 变化重发） |
| `consumed` | 结果真正落地、被消费的次数 |
| `turn_voids` | 生成中打断（reason∈{speech_start,new_text}）作废本轮的次数 |

不变式（回放器 TC-36 也按它验账）：`llm_calls == consumed + llm_wasted`。
打断那条被作废的调用同时记进 `llm_wasted`，所以「作废也计一次调用」由这条不变式直接体现。

## 4. 线程池占用（为什么作废还占资源）

`TurnSession` 的 worker 池（`src/turn_session.py:130`）：

```
max_workers = 4 + max(0, max(eot_max_inflight, tools_max_inflight) - 1)
```

默认 `eot_max_inflight=3`、`tools_max_inflight=1` → 每席 **6 个** worker 线程；双席（D/P）各一份
`TurnSession`，各自独立池，共 **12** 个。被作废的决策仍在其中一条 worker 上跑到完，期间占用该槽位。

## 5. 本期明确不做

- 真 HTTP abort / SSE 流式 cancel（打断时立刻断开底层请求）——未拍板，本轮不实现。
- `llm_max_attempts` / `turn_deadline_ms` 这类「预算封顶 / 到期强杀」的动作（上游仓库有，本仓无），
  本轮不搬。它们与「作废」是两件事：一个是省钱，一个是省时间。

## 6. 落地位置备忘

- 实现：`src/turn_session.py`（`_cancel_inflight_llm` 的 `_VOID_REASONS` 白名单 + `turn_void` 事件
  + `turn_voids` 计数器）、`src/dual_seat.py`（`own_void`/`cross_void` 归因）、
  `src/assert_surface.py`（断言面 `own_void`）。
- 验证：`tests/test_turn_void.py`（生成中打断 / CONFIRM_WAIT 不 void / 三硬 / turn_reopen / 不变式 / 承接）。
