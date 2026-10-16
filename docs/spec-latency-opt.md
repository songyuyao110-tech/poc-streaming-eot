# PoC Spec：判停链路端到端延迟优化（1/2/4/5）

- 状态：设计待评审
- 范围：PoC，只探"体验边界"，不追求异常完备
- 分支：`poc/streaming-eot`（checkout `tmp/jev-reopt-checkouts/dev-streaming-eot`）+ 共享仓库 `Agents/navi-dual-system`
- 关联：`poc-streaming-final-report.md`、`poc-streaming-progress.md`

## 1. 目标与边界

**要回答的问题**：这条路能把"用户说完 → 动作/候选出现"压到多低？代价（LLM 浪费、KEV 调用量）随延迟怎么涨？

**基线（本次会话实测，108 个回合）**

| 指标 | 基线 |
|---|---|
| 最后一字 → LLM 发出（`t_llm_start_latency_ms`） | 平均 837ms / 中位 679ms |
| 　└ 最后一字→判停通过 | 434ms |
| 　└ 判停通过→短名单就绪 | 378ms |
| 　└ 都就绪→请求发出 | 14ms |
| LLM 往返（`llm_ms`，含每次新建连接的握手） | 1.4–2.9s |
| 新建 httpx client 握手成本（`GET /v1/models` 实测） | 186–240ms/次 |
| 复用连接池后 | 53–148ms/次（首连 377ms） |
| VAD 结束 → 首工具返回（感知） | 1142–1688ms |
| 音频 VAD 结束(740ms) → ASR 文本定稿 | +270～1330ms（ASR 侧，非本次范围） |

**目标**：`t_llm_start_latency_ms` p50 ≤ **250ms**（≈ 判停往返 + 调度），`llm_wasted` ≤ **15%**，决策正确率不降。
**非目标**：不改判停判定策略（`eot_margin`/margin 规则）、不做流式解析、不做 ASR 端点优化、不做多模型级联。

## 2. 四项改动

### 改动 1 · LLM 连接复用（warm client）

**问题**：`slow_turn` 每次调用 `with httpx2.Client(timeout=90.0) as client:` 新建客户端并关闭 → 每次重做 TCP+TLS，实测白扔 100–180ms（算在 `llm_ms` 里）。

**方案**（`Agents/navi-dual-system/model_clients.py`，`slow_turn`）
- 模块级 **懒加载** 单例：
  ```python
  _SLOW_CLIENT = None
  def _slow_client():
      global _SLOW_CLIENT
      if _SLOW_CLIENT is None:
          _SLOW_CLIENT = httpx2.Client(timeout=90.0,
              limits=httpx2.Limits(max_connections=8, max_keepalive_connections=8, keepalive_expiry=120.0))
      return _SLOW_CLIENT
  ```
- 调用处 `client = _slow_client()`，不再 `with` 关闭。
- **必须懒加载**：`trust_env` 默认 True 会在构造时读代理环境变量，而 `core_pipeline_run.existing_slow_configuration()` 会先修 `NO_PROXY` 里的 `[::1]`；构造早于那个修正会拿到坏代理配置。**不要**照抄 KEV 的 `trust_env=False`（LLM 走公司网关，可能需要代理）。
- `keepalive_expiry=120`：一轮之间常空闲数十秒，太短等于每轮重连。
- **PoC 唯一要处理的异常**：连接池里的旧连接被服务端掐断 → `httpx2.RemoteProtocolError` 时**重试一次**（换新连接）。其他异常照现流程抛。

**预计收益**：100–180ms/次（在 `llm_ms` 内，不在"启动"里）。

### 改动 2 · 预热 LLM（把冷启动移到用户说话期间）

**问题**：模型/网关侧可能被卸载或首次排队；即便连接复用，首个请求仍可能承担模型加载。

**方案**
- `model_clients.py` 加 `warm_slow_model()`：一次极小 completion（`max_tokens=1`，`stream=False`，不带 tools）。
- **触发点**：`TurnSession.on_asr` 里**本轮第一次文本变化**时触发一次（`self._warmed` 标记），异步提交到 `self._pool`，不阻塞主链路：
  ```python
  if not self._warmed:
      self._warmed = True
      self._warm_future = self._pool.submit(self.warm_fn)   # warm_fn 可注入，便于测试
  ```
  选这个点是因为：用户通常还要说 1–3s，预热完全藏在说话期间；脚本回放模式（A3/A4）也能覆盖到。
- 配置：`warm_llm: bool = True`（TurnConfig 新增字段）。
- 失败静默忽略（PoC 不做重试/探活），`warm_llm=False` 可一键回退。
- 不做空闲心跳（另立，本期不纳入）。

**预计收益**：把网关/模型冷启动从关键路径移走；效果取决于网关是否存在卸载/排队（改动 0b 的埋点就是用来证明这一点的）。

### 改动 4 · 选工具并发化 + 去 debounce（让短名单提前就绪）

**问题（根因）**：`turn_session.py` 里
- `on_asr` 给 `_tools_pending` 设 `due_ms = t_ms + debounce_ms`（80ms）；
- `tick()` 里 `if not self._jobs["tools"] and self._tools_pending and ...` → **工具调用是单飞**。

在 150ms partial 韵律 + 250–480ms KEV stage1 下，单飞意味着"在途的永远是为旧文本发的"，最新文本的短名单必须等上一轮返回才开始 → **短名单恒定落后约一个往返**。这与 EOT 侧早先测到的一致（`eot_max_inflight` 1/2/3 → 覆盖率 42%/70%/94%）。

**方案**
- **并发化**：`_tools_jobs` 改为列表 + `tools_max_inflight`（默认 3），与 `_eot_jobs` 同构；`_fire_tools` 生成 job 入列，`_poll` 收结果。
- **去 debounce**：工具不再用 `due_ms`，文本一变立即发（判停仍保留 `debounce_ms`）。
- **使用侧仍然 latest-wins**：`_tools_cache` 只被"最新一轮"覆盖；`_start_llm` 用 `_tools_result`（最新完成的结果），并记录它对应哪个版本。
- 配置：`tools_max_inflight: int = 3`（新增），`debounce_ms` 继续只管判停。
- 边界：只保留**最新文本**的结果进入 `_tools_cache`；旧版本结果到达时记 `tools_scores(version=..)` 供展示，不覆盖更新的缓存。

**预计收益**：把"等短名单 378ms"压到接近 0（短名单在判停通过前就绪的概率大幅上升），**且不引入 LLM 浪费**——这是四项里最划算的一刀。

**风险**：KEV 调用量上升（每轮可能 3–8 次 stage1）。需在边界矩阵里记录 `tool_calls/turn` 与 KEV 侧是否限流。

### 改动 5 · 不等短名单就发 LLM（shortlist-optional）

**问题**：`_maybe_start_llm` 有硬门 `if not self._tools_ready(): return`，短名单没就绪就干等。

**方案**（`turn_session.py`）
- 放开该门，改为"有可用短名单就发，没有就等"：
  - `_tools_cache` 已就绪且文本匹配 → `shortlist_source=fresh`（最优，现状）；
  - `_tools_cache` 是本轮的但文本不同 → `shortlist_source=stale`（`shortlist_age_ms` = 当前版本与短名单版本的时间差），**立即发 LLM**；
  - 本轮完全没有短名单结果 → 保持等待（保底，无短名单无法构造 `decide` 入参）。
- 短名单迟到后的处置由开关控制：
  - `shortlist_reissue=True`（默认，语义干净）：`normalize(text)` 与 LLM 用的短名单来源不一致 → 复用现有 `_cancel_inflight_llm` 作废并重发，计入 `llm_wasted`；
  - `shortlist_reissue=False`（乐观）：接受该决策，但在 `turn_done` 里标 `shortlist_policy=accept`，供正确率对比。
- 配置：`allow_stale_shortlist: bool = True`、`shortlist_reissue: bool = True`。
- `_start_llm` 的 `llm_start` 事件增加 `shortlist_source` / `shortlist_age_ms` 字段。

**预计收益**：吃掉 378ms 里"短名单晚于判停"的部分；与改动 4 叠加后，`t_llm_start_latency_ms` 期望落到"判停往返 + 调度"量级（~150–250ms）。
**代价**：`llm_wasted` 上升——**这正是要探的边界曲线**。

## 3. 埋点（先做，否则无法验收）

timeline 事件新增字段：

| 锚点 | 位置 | 用途 |
|---|---|---|
| `t_first_partial` | 本轮第一个 `asr_update(changed=True)` | 预热触发点、改动 4 的提前量基准 |
| `tools_start` / `tools_scores` 带 `version` | 已有事件补字段 | 计算"短名单就绪提前量" |
| `t_tools_ready_for_version` | 新增 | 改动 4 的核心指标 |
| `llm_start.shortlist_source` ∈ `fresh/stale/none` + `shortlist_age_ms` | 新增 | 改动 5 的核心指标 |
| `llm_ms` 拆 `connect_ms` / `ttft_ms` / `total_ms` | `slow_turn` 返回 | 改动 1/2 的唯一证据 |
| `turn_done.<span>n_reissue</span>` / `warm_ms` | 新增 | 浪费与预热效果 |

指标条新增芯片：`编排: 判停 Xms · 等短名单 Yms · 调度 Zms`、`短名单: fresh/stale(age)`。

## 4. 验证

**分层**
1. 单元：`GET /v1/models` 冷/热对比脚本（已有）；`slow_turn` 改前/改后 `llm_ms` × 10；预热后首请求 `ttft_ms` 对比。
2. 集成回归（必须全绿）：A3 9/9、A4 11/11、A5 4/4、M4 6/6、M1b PASS。
3. 边界矩阵（本期核心产出）：脚本回放模式跑固定 case 集（每格 20 回合）

| 维度 | 取值 |
|---|---|
| `debounce_ms` | 0 / 30 / 80 |
| `tools_max_inflight` | 1 / 3 |
| `allow_stale_shortlist` | false / true |

每格记录：`t_llm_start_latency_ms`(p50/p90)、`llm_wasted` 率、`tool_calls/turn`、`n_reissue`、决策是否与金标一致。产出"延迟 ↓ vs 浪费 ↑ / KEV 调用 ↑"的曲线，即体验边界。
4. 真机：麦克风 2 轮，看 `VAD结束→首工具`、`决策`、按钮/保存无回归。

**通过标准**：`t_llm_start_latency_ms` p50 ≤ 250ms；`llm_wasted` ≤ 15%；决策正确率不低于基线；回归全绿。

## 5. 不做（明确排除）

- 判停判定策略（`eot_policy`/`eot_margin`/`spec_margin` 语义不动）
- LLM 流式解析、多模型级联、短名单本地缓存
- ASR 端点/断句优化（**注**：这是感知链路最大的一块 0.3–1.3s，但属于另一条线）
- 异常完备性：预热失败静默、连接失效重试一次、短名单缺失回退等待、KEV 失败沿用现有 `max_silence` 兜底
- 参数持久化/UI 配置面板（除必要的三个新开关）

## 6. 里程碑（每个可独立回滚）

| M | 内容 | 产出 |
|---|---|---|
| M0 | 埋点（§3）+ 基线复测 | 基线数字，可对比 |
| M1 | 改动 1 连接复用 | `llm_ms` 前后对比 |
| M2 | 改动 2 预热 | 首请求 `ttft`/`llm_ms` 对比 |
| M3 | 改动 4 工具并发 + 去 debounce | `等短名单` 归零，`tool_calls` 上升幅度 |
| M4 | 改动 5 不等短名单 + 边界矩阵 | 延迟/浪费曲线 + 结论 |

## 7. 实测结果与设计修订（M0–M4 已完成）

zh，20 句，150ms 前缀韵律，决策用 1380ms 模拟（隔离编排）；`LLM 启动` = 最后一字 → 第一次发出请求。

| 配置 | LLM 启动 p50 | p90 | 工具调用/句 | 判停调用/句 | LLM 调用 | 浪费 |
|---|---|---|---|---|---|---|
| 改动 4/5 全关（基线） | 849ms | 1504ms | 5.55 | 13.35 | 20 | 0 |
| **只开改动 4（工具并发=3）** | **1503ms** | 1793ms | **10.75** | **7.45** | 20 | 0 |
| 只开改动 5（重发看文本变化） | 531ms | 1504ms | 5.60 | 12.95 | 35 | 15 |
| **改动 5 + 重发收敛到 top-k 变化（最终默认）** | **501ms** | 1504ms | 5.90 | — | 25 | **5** |

**结论（推翻了两条原设计）**
1. **改动 4 的"并发化"有害，已撤销**（`tools_max_inflight` 默认回到 1）：并发把工具调用从 5.55/句推到 10.75/句，反而让 p50 从 849→1503ms，并把**判停调用从 13.35/句挤到 7.45/句**——同一 KEV 服务被工具调用占满。EOT 并发之所以有效是因为它是"去饥饿、提覆盖率"；工具短名单只需要最新一份，并发只会排队。**保留"工具去 debounce"（它是免费的提前量）。**
2. **改动 5 有效**：p50 849→**501ms（-41%）**。重发条件必须收敛到"**入选 top-k 变了**"而不是"文本变了"：看文本变化时 20 句里有 15 句重发（浪费 15 次 LLM），收敛后只有 5 句（**-67% 浪费**），p50 还略好。
3. **p90 三格都是 1504ms 不变** → 长尾不由编排决定，而是判停结果到达晚 / 确认窗，属于改动 5 能力之外。
4. **改动 1（连接复用）**：轻量端点实测每次省 **68ms**（另一轮探针 100–180ms）；在完整 completion 上被网关方差（1-token 请求都能跑 1.0–2.9s）掩盖，端到端测不出。
5. **改动 2（预热）**：冷启动惩罚实测 **~0.8–1.3s**（首个 1-token 请求 vs 后续），预热把它移到用户说话期间；网关本身方差极大，这是"体验边界"的真正约束。

**回归**：A3 9/9、A4 11/11、A5 4/4、M4 6/6、M1b PASS（改动后全绿）。

## 8. 开放问题

1. 共享仓库 `navi-dual-system/model_clients.py` 是否可直接改（改动 1/2 落在那里）。
2. 预热的 1-token 请求是否消耗网关配额/计费（未知，按可忽略处理）。
3. `shortlist_reissue=False`（乐观接受旧短名单决策）要不要纳入矩阵——它可能提升延迟表现但污染正确率，需你确认是否值得测。