# PoC Spec：流式多轮消费 LLM（消费游标 + 段级多次决策）

- 状态：设计待评审（只写文档，不改代码）
- 仓库：`/Users/YUYSONG/Work/poc-streaming-eot/`（code @ `17d77d2`）
- 要改的核心文件：`src/turn_session.py`（+ 少量 `ui/server.py` 接线）
- 依据（只读）：`docs/spec-20261015.md`、`docs/plan-20261015.md`、`docs/spec-latency-opt.md`、`docs/progress.md`、`docs/final-report.md`
- 范围：PoC，只跑通"核心路径能连续消费多段"；不做生产级边界与容灾（见 §6）

---

## 1. 目标一句话

把"判停 → 一次 LLM 决策 → 本轮收尾"改成"判停 → 一次 LLM 决策 → **把该段标记为已消费** → ASR 继续说 → **只对未消费后缀**再判停、再调 LLM"，即一轮会话内按段连续消费多次 LLM 决策。

---

## 2. 现状 vs 目标行为

现状（`src/turn_session.py`，行号为当前 code）：

| 阶段 | 现状 |
|---|---|
| 决策落地 | `_apply_llm()`（L401-406）→ `_finish_turn()` → `turn_done=True`、`turns.append(summary)`、落 timeline（L472-478）；此后 `tick` 只在 `CONFIRMED` 调 `_maybe_start_llm`（L226-227），所以一轮只有 1 次决策 |
| 决策后的 ASR | 同归一化文本的 final → 忽略（L125-132）；**不同文本 → `barge_in` + `_reset_turn()`（L133-134）**，清空 `segments`/`segment`（L486-487）、`turn_index+1`，新决策只看到新文本（历史丢失） |
| LLM 输入 | `self.turn_text()` = 全部段拼接（L306） |
| 判停输入 | `eot_input()`：`last_segment`，当前段 <4 字时拼上一段（L257-262） |
| 选工具输入 | `turn_text()`（L166） |
| 每轮决策数 | 恰好 1（`docs/final-report.md` §3 的"每轮有效 LLM 决策恰好 1 次"） |

目标：

| 阶段 | 目标 |
|---|---|
| 决策落地 | 推进**消费游标**（`consumed_version` / `consumed_segments`），发 `segment_consumed`，状态回 `LISTENING`，**不收尾** |
| 决策后的 ASR | 并入**未消费后缀**，自动重新判停；**不触发 `barge_in`、不重置 turn、不清历史** |
| LLM 输入 | `suffix_text()` = 未消费后缀 |
| 判停 / 选工具输入 | 同样只看未消费后缀（后缀段视图） |
| 每轮决策数 | 等于已消费段数 `N`（N ≥ 0），每个已消费段恰好 1 次有效决策 |
| 收尾 | 后缀为空 + 静音 ≥ `max_silence_ms` + 无在途工作（或显式 `stop()`）→ 整轮 `turn_done` 一次 |
| `barge_in` | 只保留"`turn_done` 之后用户又开口"这一种（开新 turn，语义不变） |

---

## 3. 状态机 / 消费游标怎么改

### 3.1 consumed 边界定义（以哪次 ASR 文本 / 时间戳为界）

一句话：**边界 = 那次"落地了的决策"所使用的 ASR 文本版本（`text_version`）；落点 = 该版本覆盖到的段，封存为不可改写的已消费段。时间戳只用于证据，不参与边界判定。**

游标三元组（新增到 `TurnSession`）：

- `consumed_version: int` — 已消费决策发出时的 `job["version"]`，即该次 `decide()` 收到的文本版本；`0` 表示尚未消费任何段。
- `consumed_segments: int` — 已消费的**封段**数量；`self.segments[consumed_segments:]` 就是未消费的已封段。
- `consumed_tail: str | None` — 最近一次消费段的**原始文本**（去重用，见 §3.3）。

边界的合法性由现有版本机制保证：`_on_llm_result` 只接受 `job["version"] == self.version` 的结果（L391-394），所以决策落地那一刻，"发出去的那段文本"与"当前文本"逐字相同 → 落地时封段不会截断/错位。

```python
def suffix_segments(self):                 # 未消费的已封段
    return self.segments[self.consumed_segments:]

def suffix_text(self):                     # 未消费后缀
    return self._join(self.suffix_segments() + ([self.segment] if self.segment else []))

def _seal_consumed(self):                  # 只在决策落地时调用一次
    if self.segment:                       # 把在用段封进 segments，之后不可被后续 partial 改写
        self.segments.append(self.segment)
        self.segment = ""
    self.segment_closed = False
    self.consumed_tail = self.segments[-1] if self.segments else None
    self.consumed_segments = len(self.segments)
```

- **不要**在 `_start_llm()` 时就封段：段在发出后仍可能被作废（用户继续说），而 ASR 的 partial 是"整句重写"而不是增量，提前封段会把 `导航回` + `回家` 拼成 `导航回回家`。
- `consumed_t_ms`（证据用）= 该版本对应的 ASR 更新 `t_ms`，在 `on_asr` 里随 `changed=True` 记录，消费时抄进记录；PoC 承认这是近似值（版本可能在同一次更新里推进多次）。

### 3.2 判停 → 调 LLM 且未被打断 → 标记已消费

- 触发链不变：`eot complete` 且 margin 满足 → `MAYBE_DONE`（可选投机）→ 静音 ≥ `confirm_silence_ms` → `CONFIRMED` → `_maybe_start_llm()` → `decide()`。
- **唯一改动**在 `_apply_llm()`：不再 `_finish_turn()`，改为 `_consume()`：

```python
def _apply_llm(self, now_ms, result, job):
    # 版本已在 _on_llm_result 校验过（job["version"] == self.version）
    self.decision = result["decision"]
    self.t_decision = now_ms
    consumed_text = self.suffix_text()      # == 该次 decide 的输入
    self._seal_consumed()
    self._consume_record = {"index": len(self.consumptions) + 1, "version": job["version"],
                            "segments": self.consumed_segments, "text": consumed_text,
                            "t_ms": now_ms, "asr_t_ms": self.version_t_ms,
                            "speculative": bool(job.get("speculative")), "llm_ms": result.get("llm_ms"),
                            "decision_kind": result["decision"].get("kind"),
                            "shortlist_source": self._shortlist_source}
    self.consumptions.append(self._consume_record)
    self._emit("segment_consumed", now_ms, **self._consume_record)
    self._emit("llm_decision", now_ms, decision=result["decision"], llm_ms=result.get("llm_ms"),
               utterance=consumed_text, consumed_index=self._consume_record["index"],
               consumed_version=job["version"], consumed_segments=self.consumed_segments,
               decision_head=(result.get("log_fields") or {}).get("decision_head"))
    self._to_state("LISTENING", now_ms, "consumed")   # 游标已推进，继续听后缀
```

- "未被打断"的判定 = 决策**落地**（`job["canceled"] == False` 且版本仍最新）。发出后被打断的调用不消费、计入 `llm_wasted`（§4）。

### 3.3 ASR 继续：只对未消费后缀判停与调 LLM

- `on_asr` 里 `_eot_pending` / `_tools_pending` 的文本、判停入口 `eot_input()`、选工具输入、`_tools_ready()` 比较基准，全部从 `turn_text()` 换成**后缀视图**：

```python
def eot_input(self):                        # 后缀内的"最后一段"，<4 字时拼后缀的上一段
    if self.cfg.eot_input == "turn_text":
        return self.suffix_text()
    if len(normalize(self.segment)) >= 4 or not self.suffix_segments():
        return self.segment
    return self._join(self.suffix_segments()[-1:] + [self.segment])

def _tools_ready(self):
    return self._tools_cache is not None and self._tools_cache["norm"] == normalize(self.suffix_text())
```

- `_start_llm()` 的 `decide()` 入参从 `self.turn_text()` 改成 `self.suffix_text()`，并给 `llm_start` 增 `utterance` / `consumed_segments` 字段（验收要能核对"每次输入文本"）。
- 决策 #2 看不到已消费的 #1（不做跨段上下文，见 §6）。
- 消费后到达的 ASR 更新按下面规则进入后缀（**mic 核心路径会遇到**：火山 ASR 对同一 utterance 会重发整句文本）：

```python
# on_asr 开头，消费重发/同句延续的去重（只与最近一次消费段比较）
if self.consumed_tail and normalize(text).startswith(normalize(self.consumed_tail)):
    if normalize(text) == normalize(self.consumed_tail):
        self._emit("eot_skipped", t_ms, reason="consumed_duplicate", text=text)   # 已消费段的重发：忽略
        return
    if text.startswith(self.consumed_tail):
        text = text[len(self.consumed_tail):]        # 同句延续：只取增量作为新段
```

- 消费后 `self.segment` 为空，`on_asr` 自然开新段（L137-140 的 `segment_closed` 机制），因此 `suffix_text()` 只含增量。
- 计时器语义不变：静音仍从"最后一次文本变化 / VAD `speech_end`"中较晚者起算（`_silence_ms`，L410-413）。

### 3.4 打断如何作废未消费段 / 进行中的投机 LLM

两类打断要分清，这是本改动的行为分水岭：

| # | 场景 | 目标行为 |
|---|---|---|
| A | **消费前**打断（决策还没落地）：新文本、`speech_start`、版本前进 | 复用现有 `_cancel_inflight_llm()`（L442-454）：在途 job 置 `canceled`、`_llm_prepared` 丢弃，各记 1 次 `llm_wasted` + `llm_cancel(reason=new_text/speech_start)`；**游标不动**，该段文本留在未消费后缀，和新增文本一起重判 |
| B | **消费后**继续说话 | 不撤销已消费决策（动作已 mock 执行，PoC 不做回滚）；不 `barge_in`、不 `_reset_turn`；新文本成为后缀，重新走判停→确认→决策 |
| C | 消费后的**同句重发 / 整句重发** | §3.3 的去重：完全重发忽略（`consumed_duplicate`），前缀重叠只取增量 |
| D | 本轮已 `turn_done`，用户又开口 | 保持现状：`barge_in` + `_reset_turn()` 开新 turn（L133-134 不变） |

- 未消费后缀**永不因打断被丢弃**：它只被"重新判停"，不重置文本。唯一会丢文本的情形是 `_reset_turn`（D，整轮已结束）。
- 已消费段的文本是封段（进 `segments`），后续任何 partial 都改写不到它。

### 3.5 状态机与计时器改动清单

状态集**不变**（`LISTENING / MAYBE_DONE / CUT_OFF_WAIT / THINK_WAIT / CONFIRMED`）；变化是 `CONFIRMED` 从"终态"变成"中转态"：

| 触发 | 现状 | 目标 |
|---|---|---|
| 静音 ≥ `confirm_silence_ms` / `cut_wait_ms` / `think_wait_ms` / `max_silence_ms` | → `CONFIRMED` → LLM → 收尾 | → `CONFIRMED` → LLM →（落地）**回 `LISTENING`** |
| 决策落地 | `turn_done` | `_consume()` + `LISTENING`，继续听 |
| `CONFIRMED` 期间来新文本 | 取消在途 LLM | 取消在途 LLM（不消费），回 `LISTENING` 重新判停 |
| 消费后来新文本 | 新 turn（清历史） | 后缀增加，同一 turn 内重新判停 |
| `LISTENING` 且**后缀为空**且静音 ≥ `max_silence_ms` | `_confirm()` → 对空文本调 LLM | **收尾**：`consumed_count ≥ 1` 且无在途 LLM → `_finish_turn()`（`turn_done`） |
| `LISTENING` 且后缀为空且 `consumed_count == 0` | 同上（空文本 LLM） | 不确认、不收尾（等用户说话） |
| 显式停止 | 无此入口 | 新增 `stop(now_ms)`：后缀非空 → 立即 `_confirm()` 走最后一次决策，落地后收尾；后缀为空 → 直接 `_finish_turn()` |

要点：

- `_confirm()` 增加守卫：`if not normalize(self.suffix_text()): return`（没有未消费文本就不调 LLM）。
- `_maybe_start_llm()` 增加守卫：空后缀直接 `return`；
- `tick()` 的 `CONFIRMED` 分支（`_maybe_start_llm`）与各计时器优先级不变；只在 `LISTENING` 分支把"空后缀 + 已消费"导向收尾：

```python
elif self.state == "LISTENING" and silence >= self.cfg.max_silence_ms:
    if self.suffix_text():
        self._confirm(now_ms, "max_silence")
    elif self.consumed_count and (self._jobs["llm"] is None or self._jobs["llm"]["canceled"]):
        self._maybe_finish_session(now_ms)     # -> _finish_turn()
```

- `_reset_turn()` 增加重置：`consumed_version/consumed_segments/consumed_tail/consumptions/consumed_count/version_t_ms`。
- `_finish_turn()` 语义不变（写 `turns` + timeline），但此时**可能已经消费了 N 段**。

### 3.6 计数与埋点（验收要能核对）

| 项 | 改动 |
|---|---|
| 新事件 | `segment_consumed(index, version, segments, text, t_ms, asr_t_ms, speculative, llm_ms, decision_kind, shortlist_source)` |
| 事件补字段 | `llm_start.utterance`（发给 `decide` 的原文）、`llm_start.consumed_segments`；`llm_decision.utterance / consumed_index / consumed_version / consumed_segments` |
| counters | 增 `consumed`（= 已消费段数） |
| `summary()` | 增 `consumed_count`、`consumptions[]`、`t_last_llm_start`、`t_last_decision`；保留现有 `t_last_word / t_confirmed / t_llm_start / t_decision`（维持"第一次"语义，见 §附录 A） |
| 不变式（验收断言） | `turn_done` 时 **`llm_calls == consumed_count + llm_wasted`**：每次 LLM 调用要么落地成一个已消费段，要么被记账浪费 |

---

## 4. 与投机 LLM 的关系

投机触发与取消机制**不变**（`MAYBE_DONE` 且 `margin ≥ spec_margin` → 立即带当前短名单调 LLM；`speculative_llm=true` 为当前默认）。变化只在"命中后如何推进游标"和"浪费怎么算"：

| 投机结局 | 游标 | 计数 |
|---|---|---|
| **命中**：`CONFIRMED` 时 `_llm_prepared["version"] == self.version` → `_apply_llm()` 使用它 | **推进**（`consumed_version = prepared["version"]`，落地时封段） | `llm_calls` 记 1（不重复计数）；不计浪费 |
| **被反转**：用户继续说 / `speech_start` / 短名单变化重发（`n_reissue`） | 不动 | `llm_wasted++`，`llm_cancel(reason=new_text/speech_start/shortlist_changed)`；文本留在后缀重判 |
| **版本过期**（`job["version"] != self.version`） | 不动 | `llm_wasted++`（`reason=stale_text`） |
| **prepared 已丢弃** | 不动 | 现有实现已记账（L444-448），保持 |

- `_llm_prepared` 需补记 `speculative` 与 `version`（现在只有 `version/result`），供 `_consume` 记录埋点；文本从落地时的后缀取回（版本校验保证一致）。
- 指标口径：`有效决策率 = consumed_count / llm_calls`；`浪费率 = llm_wasted / llm_calls`。多段消费下每条决策都对应一个已消费段，"提前触发"不再等于"浪费"——只有被反转的那次才算。
- 短名单（选工具）与投机的关系不变：`allow_stale_shortlist=true` / `shortlist_reissue=true` 当前默认保留；重发时旧调用记浪费、新调用落地，**不变式 §3.6 仍成立**。
- PoC 明确不做：**投机结果跨段复用**（一个已消费段的投机结果不能顶替后缀的新决策）。

---

## 5. 验收用例（4 条，文本注入；mic 只做端到端冒烟）

**统一口径**：每条用例都用现有离线 harness 形态（`TurnSession` + 注入的 `eot_fn / select_tools_fn / decide_fn`，见 `tests/poc-streaming-a4.py` 的 `deterministic_reuse()`），形状与 `tests/poc-streaming-a3.py` 一致 —— 这样"期望调用次数 / 每次输入文本"是确定性的，不受 KEV 判停模型波动影响。判停桩固定返回 `complete` + `margin 0.9`；`decide` 桩立即返回固定决策。

**验收用配置覆盖（不改默认值）**：

```json
{"speculative_llm": false, "confirm_silence_ms": 300, "max_silence_ms": 1500,
 "debounce_ms": 0, "shortlist_reissue": false, "warm_llm": false, "eot_max_inflight": 1,
 "tools_max_inflight": 1, "eot_policy": "argmax", "tool_set": "navigation"}
```
（`C3` 额外把 `speculative_llm` 打开、`spec_margin=0.0`。这些覆盖只用于把验收变确定，不进入 `TurnConfig` 默认。）

### C1 · 单次消费基线（回归）

- 输入：`speech_start@0`；`asr "导航回家"@100 (final)`；`speech_end@120`
- 期望 LLM 调用：**1** 次，输入文本 `"导航回家"`
- 期望事件：`llm_start(utterance="导航回家")` → `llm_decision(utterance="导航回家", consumed_index=1)` → `segment_consumed(1, text="导航回家")` → 静音 ≥ `max_silence_ms` 后 `turn_done`
- 期望最终状态：`turn_done` 已发，`state == "LISTENING"`；`consumed_count == 1`、`llm_calls == 1`、`llm_wasted == 0`（`1 == 1 + 0`）

### C2 · 两段连续消费（本改动核心）

- 输入：`speech_start@0`；`asr "导航回家"@100 (final)`；`speech_end@120`；`speech_start@800`；`asr "顺便看看路况"@900 (final)`；`speech_end@920`
- 期望 LLM 调用：**2** 次；输入文本依次为 `"导航回家"`、`"顺便看看路况"`（第 2 次**不含**已消费前缀）
- 期望事件：`segment_consumed(1)` 出现在 `speech_start@800` **之前**；`segment_consumed(2, text="顺便看看路况")`；全程**无** `barge_in`、**无** `turn_done` 在两段之间
- 期望最终状态：`turn_done` 已发，`state == "LISTENING"`；`consumed_count == 2`、`llm_calls == 2`、`llm_wasted == 0`；`consumptions[*].segments` 依次为 `1, 2`（说明后缀视图逐段推进）

### C3 · 消费后继续说话，且未消费后缀的投机被作废

- 配置：`speculative_llm=true, spec_margin=0.0`，其余同 C2
- 输入（分步驱动，harness 控制 tick）：
  1. `speech_start@0`；`asr "导航回家"@100 (final)`；`speech_end@120` → tick 到决策落地：`segment_consumed(1)`
  2. `asr "顺便看看"@400 (partial)` → tick 到出现 `llm_start(speculative=true, utterance="顺便看看")`（**先不 tick 到落地**）
  3. `asr "顺便看看路况"@600 (final)` → 同一驱动线程内文本版本前进，在途/已备好的投机结果作废
  4. tick 到决策落地 + 收尾
- 期望 LLM 调用：**3** 次（#1 段1、#2 被作废的投机、#3 段2）；输入文本依次 `"导航回家"`、`"顺便看看"`、`"顺便看看路况"`
- 期望事件：至少 1 条 `llm_cancel(reason ∈ {new_text, speech_start}, speculative=true)`；`segment_consumed` 恰好 2 条，文本 `"导航回家"` / `"顺便看看路况"`
- 期望最终状态：`consumed_count == 2`、`llm_wasted == 1`、`llm_calls == 3`（`3 == 2 + 1`）

### C4 · 消费后 ASR 同句重发 / 整句重发去重（mic 核心路径）

- 输入（模拟火山 ASR：同一 utterance 会重发整句文本）：
  1. `speech_start@0`；`asr "导航回家"@100 (final)` → `segment_consumed(1, text="导航回家")`
  2. `asr "导航回家。"@600 (final)`（同句重发，归一化后相同）
  3. `speech_start@900`；`asr "导航回家附近有加油站吗"@1000 (final)`（整句重发并延续）
- 期望 LLM 调用：**2** 次，输入文本依次 `"导航回家"`、`"附近有加油站吗"`（第 3 步去掉已消费前缀）
- 期望事件：第 2 步产生 `eot_skipped(reason="consumed_duplicate")`，**不**产生新的 `eot_start` / `tools_start` / `llm_start`；第 3 步 `segment_consumed(2, text="附近有加油站吗")`
- 期望最终状态：`consumed_count == 2`、`llm_calls == 2`、`llm_wasted == 0`；`turn_text()` 内无重复的 `"导航回家"` 两次

### 回归 + 端到端冒烟（不算新验收条数，但必须跑）

- 既有回归全绿：`tests/poc-streaming-a3.py`（9/9，其中"每轮恰好 1 次决策"重述为"每个已消费段恰好 1 次"）、`a4.py`（11/11）、`a5.py`（4/4）、M4 mic（6/6）、M1b（PASS）。
- `POST /api/session/stream`（真 KEV）两段脚本：`帮我导航回家|3200|顺便看看路况`（停顿 ≥ `think_wait_ms=3000`，任何 label 分支都会确认）→ 期望 2 条 `llm_decision`、2 条 `segment_consumed`、不变式成立。
- 麦克风一轮说两段：期望 UI 时间线上出现两个"已消费"段、`turn_done` 正常出来、`llm_wasted == 0`。

---

## 6. 非目标（PoC 不做，按现有 PoC 排除项对齐）

- **LLM 流式解析**：`decide()` 仍是同步、非流式的单次调用（沿用 `docs/spec-latency-opt.md` §5 的排除项）。
- **多工具并发 / 真实执行**：动作仍是 mock，不做并发工具、不做真实业务调用。
- **跨决策上下文**：决策 #2 只拿到未消费后缀，拿不到决策 #1 的文本与结果（不做 context 传递、不做对话历史）。
- **动作回滚**：消费后的 `barge_in` 不撤销已执行动作，不做"纠正上一轮"。
- **ASR 对已消费段的纠正**：同句重发但文本不同（如 `"导航回家"` → `"导航回老家"`）按 §3.3 的前缀规则处理，不回溯改写已消费文本。
- **跨 session / 跨 turn 恢复**：游标不持久化、不做断线重连、不做多会话并发、不做鉴权。
- **判停策略与模型**：`eot_policy / eot_margin / spec_margin` 语义不动；不接音频/韵律判停模型（Smart Turn）；不换中文专用 prompt。
- **默认参数调整**：`TurnConfig` 默认值全部保持（见附录 B），改动只在 `turn_session.py` 的消费逻辑。
- **UI 重构**：不重做页面；只在时间线上加"已消费段"的展示（可选）与必要的字段透传。
- **异常完备性**：不加重试/容灾；沿用"KEV 超时 → `max_silence` 兜底"、预热失败静默等现有约定。

---

## 7. 完成定义（怎样算验收通过）

1. **C1–C4 全过（4/4）**，判定脚本按 §5 的期望逐条断言：LLM 调用次数、每次输入文本、`segment_consumed` 序列、`consumed_count`、最终 `state`。
2. **不变式成立**：每个用例 `turn_done` 时 `llm_calls == consumed_count + llm_wasted`，且 `consumptions` 条数 == `consumed_count`。
3. **既有回归全绿**：A3 9/9、A4 11/11、A5 4/4、M4 6/6、M1b PASS（A3 的断言重述为"每个已消费段恰好 1 次有效决策"）。
4. **端到端冒烟通过**：`/api/session/stream` 两段脚本 2 条决策；麦克风一轮两段出两个已消费段。
5. **证据齐全**：`timeline.json` 含 `consumptions[]` 与 `segment_consumed` 事件，事件数与 SSE 一致；`summary` 汇总字段齐全。
6. **记录**：本轮结论追加到 `docs/progress.md`（做了什么、各用例通过情况、关键数字、已知问题）。
7. 若某项未过：C1–C4 **不允许**豁免；回归/冒烟项可记录为已知限制，但必须在 `docs/progress.md` 写明具体现象与原因。

---

## 附录 A · 预计改动面（函数级）

| 位置 | 改动 |
|---|---|
| `TurnSession.on_asr` | 消费后重复/同句重发去重；`_eot_pending`/`_tools_pending` 文本改 `suffix_text()`；记录 `version_t_ms`；`turn_done` 分支保持 |
| `TurnSession.eot_input` | 改为基于后缀视图（后缀最后一段，<4 字拼后缀上一段） |
| `TurnSession.tick` | `LISTENING` + 空后缀 + 已消费 + 静音 ≥ `max_silence_ms` + 无在途 → 收尾；其余计时器不变 |
| `TurnSession._confirm` | 空后缀直接 `return` |
| `TurnSession._maybe_start_llm` | 空后缀 `return`；`_tools_ready()` 用后缀文本 |
| `TurnSession._start_llm` | `decide` 入参 `suffix_text()`；`llm_start` 增 `utterance`/`consumed_segments` |
| `TurnSession._on_llm_result` | 版本校验/投机分支不变；`_llm_prepared` 补记 `speculative` |
| `TurnSession._apply_llm` | 不再 `_finish_turn`；改 `_consume()`（推进游标 + `segment_consumed` + 回 `LISTENING`） |
| `TurnSession._tools_ready` | 比较 `normalize(suffix_text())` |
| `TurnSession._reset_turn` | 重置游标与 `consumptions` |
| `TurnSession.summary` | 增 `consumed_count`/`consumptions`/`t_last_llm_start`/`t_last_decision`（现有 `t_*` 保留"第一次"语义） |
| 新增 | `suffix_segments` / `suffix_text` / `_seal_consumed` / `_consume` / `_maybe_finish_session` / `stop(now_ms)` |
| `ui/server.py` | `cmd: stop` → 调 `session.stop(now)`；回放结束后继续 tick 到 `turn_done`（现有 20s deadline 足够） |
| `ui/index.html` | 可选：时间线上标"已消费段" |
| **不动** | `turn_eot.py`、`core_pipeline_run.py`（判停/选工具/决策的签名与语义不变） |

## 附录 B · 当前 `TurnConfig` 默认值（引用 `src/turn_session.py` L31-57，勿臆造）

| 字段 | 默认 | 与本改动的关系 |
|---|---|---|
| `eot_policy` | `"margin"` | 不变 |
| `eot_margin` | `0.2` | 不变（判停策略不动） |
| `spec_margin` | `0.3` | 不变（投机触发阈值） |
| `speculative_llm` | `True` | 不变；投机命中即推进游标（§4） |
| `eot_input` | `"last_segment"` | 语义不变，作用对象改为后缀 |
| `confirm_silence_ms` | `700` | 不变（每段确认窗） |
| `cut_wait_ms` | `1500` | 不变 |
| `think_wait_ms` | `3000` | 不变（`THINK_WAIT` 不用 `max_silence` 兜底） |
| `max_silence_ms` | `2000` | 不变；新增"空后缀 + 已消费 → 收尾"用法 |
| `eot_timeout_ms` | `900` | 不变 |
| `eot_max_inflight` | `3` | 不变 |
| `debounce_ms` | `80` | 不变 |
| `reuse_tools_if_text_unchanged` | `True` | 不变，作用对象改为后缀 |
| `tools_max_inflight` | `1` | 不变（并发经实测撤销） |
| `allow_stale_shortlist` | `True` | 不变 |
| `shortlist_reissue` | `True` | 不变（重发计入浪费，不变式仍成立） |
| `warm_llm` | `True` | 不变（每轮第一次文本变化触发一次） |
| `tool_set` | `"navigation"` | 不变 |

## 附录 C · 未决问题（实现前需确认）

1. 段间是否需要上下文：决策 #2 是否应带上已消费文本/上一决策结果（PoC 暂定不带）。
2. 消费后 `barge_in` 是否需要"撤销/纠正"能力（PoC 暂定不做，动作不可回滚）。
3. 现有 `t_last_word / t_confirmed / t_llm_start / t_decision` 在多段下取"首次"还是"最后一次"（本 spec 暂定：保留首次语义 + 新增 `t_last_*`）。
4. 收尾入口：只靠静音 `max_silence_ms` 自动收尾是否够，还是必须把 `stop(now_ms)` 接进 `ui/server.py` 的「停止」按钮（本 spec 暂定两者都要）。
5. ASR 对已消费段的"纠正"（同句重发但文本不同）按前缀去重是否可接受为 PoC 行为。

DONE