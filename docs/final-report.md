# 流式 KEV 判停 + 选工具 → DeepSeek 单次决策：PoC 最终报告（2026-10-15）

依据：`tmp/poc-streaming-kev-eot-spec-20261015.md`（spec）+ `tmp/poc-streaming-kev-eot-plan-20261015.md`（设计，数字与规则以它为准）。

## 0. 一行结论

**核心链路端到端跑通了**（脚本化 ASR 回放 + 真实流式 ASR 都能走完一轮、每轮恰好 1 次有效决策、投机结果被推翻时正确取消）。

**判停饥饿已修好**：给 EOT 独立 SSH 隧道（`:8010`）+ 去掉 single-flight 之后，**每个 ASR 更新都能拿到 EOT 打分**——150ms/前缀节奏下的判停覆盖率从 **3.3% / 2.5%（中/英）提到 95.0% / 89.2%**，总判停结果从 470 条增到 **3233 条**（28 倍），每句可用（非 stale）判停结果约 1.2 条不变。
代价：每句判停调用从 4.5/3.9 次增到 8.8/7.2 次；单次调用墙钟 p50 从（有幸存者偏差的）191ms 变成可信的 265ms。

**判停质量才是真瓶颈**（覆盖率上去后才第一次测准）：前缀误判说完率**中文 62.1%、英文 23.8%**（按句：97.4% / 44.6%）。
在 150ms 韵律下"提前触发 LLM 率"仍有 3.48% / 0%（因为真正可用的判停结果只有每句最后一个前缀）；
在 600ms（判停真正可用）韵律下，**投机策略按设计就是要提前启动 LLM**，所以提前触发率 100% / 70%、浪费率 75.9% / 47.4%——这是 (b) 明确保留的策略代价，不是 bug。

B3 五条标准：**4 PASS / 1 FAIL**，FAIL 项是英文"最后一字→LLM 启动中位 1501ms > 800ms"，按 (b) 接受、不再为此调优。
下一步按设计文档兜底：加**音频/韵律判停模型**（或中文专用判停 prompt/模型）。

工作分支：`/Users/YUYSONG/Work/tmp/jev-reopt-checkouts/dev-streaming-eot`，分支 `poc/streaming-eot`（基于 `dev-20261013` @ `fb5b259`）。
原 `dev-20261013` 未改动、未 push、未提交到无关分支。

## 1. 交付物

| 文件 | 说明 |
|---|---|
| `eval/core_pipeline_run.py`（改） | 拆出 `select_tools()` / `decide()`，`run()` 依次调用，`/api/run`、`/api/run/stream` 行为不变；新增 `KEV_EOT_CLIENT`（4 连接）与 `KEV_EOT_BASE`（默认 `http://127.0.0.1:8010`，EOT 独立隧道） |
| `eval/turn_eot.py`（新） | `EOT_LABELS`（原样复制 `pipecat-turn-eval-v2/kev_tune2.py` 的 `r2_en_utter_only`）、`score_eot()` |
| `eval/turn_session.py`（新） | `TurnConfig`、`TurnSession`、五态状态机、事件、单飞+pending、`text_version` 校验、timeline 落盘 |
| `eval/asr_stream.py`（新，M1b） | 火山引擎/豆包 v3 sauc 真实流式 ASR 适配器 `stream_asr(wav) -> (t_ms, text, final)` |
| `eval/build_prefix_cases.py`（新） | 生成 `eval/fixtures/streaming-prefix-cases.jsonl`（398 句 / 3307 前缀） |
| `eval/streaming_prefix_eval.py`（新） | 前缀回放评测 + 报告/明细生成 |
| `poc-ui/server.py`（改） | 新增 `POST /api/session/stream`（SSE 按真实时间间隔回放），旧接口保留；`POC_CHECKOUT`/`POC_PORT` |
| `poc-ui/index.html`（改） | 新增"流式模拟"模式：语速、`\|600\|` 停顿标记、TurnConfig 12 字段可改、8 列时间线表、LLM 事件标色 |

运行入口：UI `http://127.0.0.1:8765/`（已重启指向新 checkout，PID 见 `tmp/poc-ui-8765.log`）。
证据：`eval/evidence/turn-session/<session_id>/timeline.json`、`eval/evidence/streaming-prefix/<run_id>/{detail.jsonl,detail.csv,summary.json}`。
报告：本文件、`tmp/poc-streaming-prefix-eval-report.md`（M2 脚本生成）、`tmp/poc-streaming-progress.md`（全程进度）。

## 2. ASR key 搜索结论（§1，只记位置，不含任何 key 值）

| provider | 位置 | 是否有效 |
|---|---|---|
| **火山引擎/豆包 流式 ASR**（`volc.bigasr.sauc.duration`） | `~/.zshrc:114-117` 的 `ASR_BACKEND` / `CLOUD_ASR_APP_ID` / `CLOUD_ASR_ACCESS_TOKEN` / `CLOUD_ASR_RESOURCE_ID`；备用 `Agents/AIOS Framework/.env` | **有效**（已实测握手 170ms 成功、出字正常） |
| 火山引擎 协议参考实现 | `Agents/AIOS Framework/agent/cascade/volcengine_asr.py`；端点 `wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async` | 复用其帧协议（本项目用 venv 已有 `simple-websocket`，未新增依赖） |
| 阿里云/百炼 paraformer、讯飞、腾讯云、Azure Speech、OpenAI whisper/realtime | 本机 env / shell rc / `~/.config` / `Work` 下 `.env` | **未找到** |
| whisper.cpp / 本地 ASR | 未找到 | 不可用 |

→ 因此 **M1b 已实装真实流式 ASR**（不是跳过）。key 只在运行时从环境/`~/.zshrc` 读入进程，未打印、未复制、未写入任何文件。

## 3. 验收结论

| 验收 | 结论 | 关键数字 / 说明 |
|---|---|---|
| **A1** `/api/run` + `/api/run/stream` 回归 | **PASS** | 4 个样例（3 导航 + 1 多域）**KEV 阶段（分数/短名单/schemas/事件序列与载荷，截至 `llm_start`）改动前后逐字节一致**；(a)(b) 改完后又复核一次仍一致。唯一差异是"取消当前导航"这句有歧义时 DeepSeek 选 `navi_get_status` 还是 `navi_cancel_route`——**未改动的基线代码连跑两次也会翻**，属模型随机性；判定脚本 `tmp/poc-streaming-a1-compare.py`，证据 `tmp/a1-*.json` |
| **A2** `score_eot` 单测 | **PASS** | `导航回家` → complete 0.7335 / cut 0.0896 / wait 0.1686，label=complete，margin=+0.5649，wall 134ms / kev 41ms。记录项：`我想去` label=complete 但 margin 仅 +0.0111（cut 0.4990 几乎持平）→ 半句判停置信度极低 |
| **A3** 四个 V2 脚本（curl 打 `/api/session/stream` + UI 人工核对） | **PASS 9/9** | ①1 次决策、`llm_wasted=0`；②停顿后 CONFIRMED 启动 LLM → 新文本 `llm_cancel` → 最终 1 次决策作用于合并文本；③CUT_OFF_WAIT，1500ms 停顿内无 LLM；④THINK_WAIT 内无 LLM。改默认配置后复跑仍 9/9（C2） |
| **A4** timeline.json / `turn_done` 字段 / 选工具复用 | **PASS 11/11** | 未变文本跳过 KEV（`eot_skipped: unchanged`）、已评分文本不再起第二轮选工具、timeline 与 SSE 事件数一致、`turn_done` 9 个汇总字段齐全 |
| **M1b** 真实流式 ASR | **PASS** | `say` 生成 16k wav → 2 条 partial + 1 条 final → 走完一轮、恰好 1 次有效决策（`navi_to_special_poi{poi_type:home}`） |
| **B1** V0 回归（英文 168 轮） | **PASS** | **留出集二分类 53/56 = 94.6%**（设计文档 [实测] 94.6%，差 0 条）；全集 157/168 = 93.5%（93 条未说完中 11 条误判说完，75 条说完 0 条漏判）。三分类 97.4% 需要另一套 cut/wait 金标，`cases.json` 只有二分类金标，不宣称复现 |
| **B2** 前缀回放报告 + 逐句明细 | **PASS** | 改前 `tmp/poc-streaming-prefix-eval-report.md`（470 条判停结果）/ 改后 `tmp/poc-streaming-prefix-eval-report-after.md`（3233 条），明细见 `px-{m2-primary,m2-after}/{detail.jsonl,detail.csv}` |
| **(a) 判停饥饿修复 + 覆盖/延迟对比** | **PASS** | 覆盖率 3.3%/2.5% → **95.0%/89.2%**；EOT/选工具分隧道；`eot_max_inflight` 1/2/3 = 42%/70%/94% 覆盖率、墙钟 p50 427/369/268ms（并发越高覆盖越高且越快） |
| **(b) 投机策略保留 + 反转正确取消** | **PASS** | 新增 A5 反向用例：投机在 1.03s 启动 → 用户继续说话 2.0s 时 `llm_cancel(reason=speech_start)` → 最终恰好 1 次有效决策、浪费 1 次已记账（4/4）；已完成的投机结果也会被丢弃并记账 |
| **B3** 通过标准 | **4 PASS / 1 FAIL** | 见下表 |
| **C1** 调优取满足标准的配置 | **部分达成** | 找到"提前触发 0% + 浪费 0%"的配置，但代价是中文延迟超标；**纯文本 KEV 无法同时满足**，已给出前后对比表与下一步 |
| **C2** 更新默认 `TurnConfig` 并复跑 A3 | **PASS** | 默认 = `eot_policy=margin, eot_margin=0.2, confirm_silence_ms=700, eot_timeout_ms=900, debounce_ms=80, eot_max_inflight=3, eot_input=last_segment, speculative_llm=true`；A3 9/9、A4 11/11、A5 4/4、M1b 1 次决策 |
| **C3** 最终报告 | **本文件** | — |

### B3 逐条判定（改后 M2 主运行：150ms/前缀，398 句，最终默认配置）

| 标准 | 中文 | 英文 | 判定 |
|---|---|---|---|
| 整句二分类 ≥93% | — | 93.5%（留出 94.6%） | PASS |
| 提前触发 LLM 率 ≤5%（按句） | 3.48% | 0.00% | PASS |
| 最后一字→LLM 启动中位 ≤800ms（复用工具 ≤400ms） | 755ms | **1501ms** | **FAIL**（按 (b) 接受，不再为此调优） |
| 每轮有效 LLM 决策恰好 1 次 | 230/230 | 168/168 | PASS |
| 投机浪费率 ≤30% | 3.4% | 0.0% | PASS |

同一套配置在 **600ms/前缀**（判停真正可用）下：提前触发 LLM 率 100% / 70%、浪费率 75.9% / 47.4% —— 投机开着时"提前触发"就是设计行为，(b) 已明确保留，代价记在浪费率里。

## 4. 关键数字

### 4.1 延迟（本次实测）

| 环节 | 数值 | 备注 |
|---|---|---|
| KEV 判停单发（`score_eot`） | p50 **121ms** / mean 126 / max 181ms（n=12），无 >400ms | 与设计文档 [实测] 平均 160 / 中位 152 / p90 181ms 同量级 |
| KEV 判停与选工具并发（2 线程探针，导航 14 工具） | 判停 p50 **115ms**，选工具 p50 252 / max 585ms | 轻载下并发不慢 |
| KEV 判停与选工具并发（真实会话：ThreadPool + 持续 150ms 前缀节奏） | 判停自报墙钟 **340–556ms** | 命中设计文档风险 #3：两条 KEV 调用经同一 SSH 隧道互相排队 |
| KEV 选工具（导航，真实会话每轮） | 每句均值 3.55–4.09 次调用 | partial 上提前选工具的代价 |
| DeepSeek 决策（真实，PoC 非流式） | 1.4–2.2s（实测单次 1.6–2.9s） | 评测里用固定 1380ms（设计文档实测均值）替代，不花真钱 |
| 真实流式 ASR（火山引擎） | 握手 170ms；partial 间隔 ~500ms；final 在 last packet 后 ~110ms | 端到端出字 ~1.1s（1.06s 音频） |

### 4.2 M2 主运行：判停饥饿修复前后（150ms/前缀节奏，398 句，plan 的 100–200ms 假设）

| 指标 | 中文 改前 | 中文 改后 | 英文 改前 | 英文 改后 |
|---|---|---|---|---|
| 判停覆盖率（有判停结果的非整句前缀／非整句前缀总数） | 60/1838 = **3.3%** | 1746/1838 = **95.0%** | 27/1071 = **2.5%** | 955/1071 = **89.2%** |
| 判停结果总条数（含整句/停顿增量） | 275 | 2030 | 195 | 1203 |
| 每句可用（非 stale）判停结果数（含整句） | 1.20 | 1.21 | 1.16 | 1.20 |
| 每句判停调用数（均值） | 4.46 | 8.83 | 3.89 | 7.17 |
| 单次判停墙钟 p50 / p90 | 180 / 481 ms（**有幸存者偏差**，只统计能落地的） | 263 / 444 ms（全量） | 348 / 491 ms（同上） | 267 / 463 ms（全量） |
| 前缀误判说完率（按前缀，排除 natural） | 30/42 = 71.4% | 898/1445 = **62.1%** | 2/21 = 9.5% | 188/791 = **23.8%** |
| 前缀误判说完率（按句，任一前缀误判） | 13.0% | 97.4% | 1.2% | 44.6% |
| 提前触发 LLM 率（按句） | 4.35% | 3.48% | 0.60% | 0.00% |
| 最后一字→CONFIRMED 中位 / p90 | 440 / 789 ms | 703 / 706 ms | 871 / 3002 ms | 1501 / 3003 ms |
| 最后一字→LLM 启动 中位 / p90 | 708 / 910 ms | 755 / 973 ms | 960 / 3002 ms | 1501 / 3003 ms |
| 最后一字→决策 中位 / p90 | 2098 / 2301 ms | 2142 / 2360 ms | 2352 / 4392 ms | 2889 / 4394 ms |
| LLM 调用 / 浪费 / 浪费率 | 240/10/4.2% | 238/8/3.4% | 169/1/0.6% | 168/0/0.0% |
| 恰 1 次有效决策 | 230/230 | 230/230 | 168/168 | 168/168 |

改前/改后差异 = ① EOT 走独立隧道 `:8010`；② EOT 去掉 single-flight（`eot_max_inflight=3`，每个前缀都发一次）；③ 判停结果无论是否过期都上报（这样"前缀误判率"才第一次测准）；④ 默认计时器改为 `eot_margin=0.2 / confirm_silence_ms=700 / eot_timeout_ms=900 / debounce_ms=80 / 投机开`。

读法：
- **覆盖率 28 倍**，误判率随之"变大"——不是模型变差，而是改前只统计了能落地的少量（偏向句尾/停顿前）前缀，样本只有 42/21 个；改后是全量前缀。
- **每句可用判停结果仍约 1.2 条**：150ms 韵律下，调用往返（~265ms）必然长于前缀间隔，所以只有"文本停下来"时落地的那条才可用。**"每个前缀都有分"≠"每个前缀的分都能用"**，这是纯文本判停在该韵律下的物理上限。
- 英文"最后一字→LLM 启动"从 960ms 变 1501ms，是确认窗从 300ms 提到 700ms 的直接结果（用延迟换"提前触发"）；按 (b) 该延迟不再作为调优目标。

### 4.3 M3 配置对比（600ms/前缀：判停真正可用的韵律，20 句中文 + 20 句英文）

| 配置 | 判停覆盖 / 可用 zh·en | 前缀误判说完 zh/en | 提前触发 LLM zh/en | 最后一字→LLM 启动 zh/en | LLM 浪费率 zh/en |
|---|---|---|---|---|---|
| 改前基础设施 + 默认旧计时器（argmax/confirm300/timeout400/debounce80/无投机） | 87%·81% / 87%·81% | 48.3% / 12.5% | 100% / 85% | 573 / 569 ms | 82.9% / 62.3% |
| **改后基础设施 + 同一旧计时器**（隔离基础设施影响） | 87%·81% / 87%·80% | 48.3% / 12.5% | 100% / 85% | 585 / 572 ms | 82.5% / 63.6% |
| **改后基础设施 + 最终默认**（margin0.2/confirm700/timeout900/debounce80/投机开） | 87%·81% / 87%·80% | 48.3% / 12.5% | 100% / 70% | 605 / 605 ms | 75.9% / 47.4% |
| （参考）margin0.2 + confirm700 + **关投机** | 70%·80% / — | 46.5% / 12.6% | 0% / 0% | 1159 / 704 ms | 0% / 0% |

- 600ms 韵律下"改前/改后基础设施"几乎无差别（p50 140.6 → 145.9ms，提前触发与浪费率不变）：**该韵律本来就没有饥饿/争用可修**——瓶颈是判停模型质量（中文 48%，英文 12.5%）。
- 投机打开后，600ms 韵律的"提前触发 LLM 率"按设计变成 100% / 70%，代价以浪费率（75.9% / 47.4%）体现；(b) 已确认保留该策略。
- 关掉投机 + 700ms 确认窗能把提前触发压到 0%，但中文启动延迟 1159ms。

### 4.4 基础设施（a）的直接测量

| 测量 | 结果 |
|---|---|
| 微基准（2 线程、单轮）：EOT 与选工具同一隧道 `:8009` | EOT p50 115.2ms（n=12，max 254） |
| 微基准：EOT 独立隧道 `:8010` + 选工具 `:8009` | EOT p50 120.7ms（n=12，max 275） |
| → 单轮微基准**测不出**隧道争用：远程 KEV 服务本身才是持续负载下的共享瓶颈 | 会话内实测（改前）EOT 经常 340–556ms，是因为一轮要等一次选工具往返 + GIL 争用 |
| `eot_max_inflight` 权衡（150ms、20 句中文、全量统计） | 1 → 覆盖 42.1%、p50 427ms；2 → 70.0%、369ms；**3 → 93.9%、268ms** |
| → 结论：并发越高，覆盖越高且单次更快（远程 batched 服务在有并发时吞吐更好），故默认取 3 | |
| 600ms 韵律全量判停墙钟 p50 | 改前 140.6ms → 改后（同旧计时器）145.9ms → 改后（最终默认）141.9ms（无回退） |

## 5. 与设计文档假设的修正（[假设] → [实测]）

1. **`eot_timeout_ms=400` 太紧**：改前同一隧道下判停经常 340–556ms，`timeout` + `stale` 占判停结果 70% 以上；默认改为 900ms。
2. **`debounce_ms` 必须小于 ASR partial 间隔**：150ms 韵律下若 `debounce_ms=150`，每个 pending 都会在下一次更新到来时被替换、**一次判停都不会发**（覆盖率反而归零）；默认回到设计文档的 80ms。
3. **single-flight 是饥饿的根因**：改前 150ms 韵律下只有 2–3% 的前缀能拿到判停结果；改成"每个前缀都发、最多 3 个在途"后覆盖率 89–95%。
4. **但"每个前缀都有分"≠"每分都能用"**：150ms 韵律下可用判停结果仍是每句约 1.2 条。判停应当只用于**停顿/段尾**，而不是每一次文本变化——这是下一步的结构改动（见 §6.2）。
5. **"选工具在 partial 上提前跑零成本"不成立**（改前同一隧道下 EOT 会被选工具拖到 340–556ms）；已用独立隧道隔离，并把 `debounce_ms` 同时用于选工具。
6. **`confirm_silence_ms=300` 与"100–200ms partial"自相矛盾**：300ms 确认窗 < 停顿（400–800ms），任何一次误判 complete 都会真的启动一次 LLM；默认改为 700ms（配合投机）以把误触发压到 ≤5%（150ms 韵律实测 3.48% / 0%）。
7. **`max_silence_ms=2000` 与 `think_wait_ms=3000` 冲突**：实现按设计文档 §3.3 意图处理——THINK_WAIT 只用 `think_wait_ms`，其余状态才用 max_silence 兜底（否则 A3-4 会失败）。
8. **判停超时语义**：spec §4 的"KEV 超时 → CONFIRMED"若按字面实现，会在用户说话中途提前确认并浪费一次 LLM；实现按设计文档：超时只是放弃该次判定、退回 `max_silence` 兜底，晚到结果仍会上报（标记 stale）。
9. **"虚拟时钟"评测**：回放用真实时钟做时基（KEV 延迟本身是要测的量），仅决策 LLM 用固定 1380ms 的 mock（spec 允许）。

## 6. 已知问题 / 剩余风险 / 下一步

1. **纯文本判停质量是首要瓶颈**（覆盖率修好后第一次测准）：前缀误判说完率中文 **62.1%**、英文 **23.8%**；按句中文 **97.4%**、英文 **44.6%**。下一步按设计文档兜底：接**音频/韵律判停模型**（Smart Turn 类），或先用中文专用判停 prompt/模型（设计文档提到的 `r6_zh_rich_utter_only`）对比。
2. **判停应只用在"停顿"上**：现在每个前缀都发一次判停（覆盖率高但 97% 的结果落地即过期）。建议改成"文本稳定 ≥X ms 或 VAD speech_end 后才发判停"，把调用量降回每句 1–2 次、可用率提到接近 100%。这是本 PoC 之后最值得做的一步。
3. **投机开/关是两个产品策略**：开着 = 换延迟（150ms 韵律下中文 755ms 启动）但按设计会提前触发并按需作废（浪费率随韵律上升，600ms 时中文 75.9%）；关掉 = 提前触发 0% 但中文启动 1159ms。标准里的"提前触发 ≤5%"只在"关投机 + 长确认窗"下有意义。
4. **KEV 隧道/服务是单点**：启动时 `:8009` 隧道进程已死（本次重建），现 EOT 另走 `:8010`（同一个远程 KEV）。两条隧道是缓解措施，真正要验证的是**远程 KEV 服务自身的并发能力**（微基准测不出争用，会话内才会暴露）。
5. **`natural_complete` 预标偏宽松**（LLM 自标，例："雨刷""锁上"被判可独立成句）；报告同时给出排除/不排除两种口径，人工只做了抽样。
6. **未做**（非目标）：TTS、真实工具执行（保持 mock）、多会话并发、重连、鉴权、真车部署、Smart Turn；真实流式 ASR 只在 `eval/asr_stream.py` 验证，未接到 UI 麦克风。
## 6b. M4 · 麦克风模式（真实流式 ASR 进 UI）

**做了什么**（PoC 范围，只求核心链路通）
- `eval/asr_stream.py` 新增 `ASRStream`：把原来只能读 wav 的生成器抽成**推流式会话**（`push(pcm)` / `poll()` / `finish()` / `close()`），复用同一套 v3 帧协议与 `~/.zshrc` 的 `CLOUD_ASR_*`（运行时读入，不打印）；原 `stream_asr()` 改为基于它，M1b 复跑仍 PASS。
- `poc-ui/server.py` 新增 `@app.route("/api/session/mic", websocket=True)`：浏览器推 **16kHz/单声道/16bit、20ms 一帧（640B）** 的 PCM，server 直接喂火山豆包流式 ASR；ASR 的 partial/final 实时送 `TurnSession`（判停 + 选工具 + 投机 LLM），事件按同一份词汇表推进 timeline。线程模型：收帧线程 → ASR 线程 → 驱动线程（所有状态变更只在驱动线程）→ 发送线程（只它写 socket）。
- WS 协议：浏览器→server 二进制=PCM，文本=`{"cmd":"config"|"stop"|"save"}`；server→浏览器 `{"kind":"ready"|"asr"|"event"|"saved"|"stopping"|"config"|"error","data":…}`。
- `poc-ui/index.html` 新增第三种模式「麦克风（真实流式 ASR）」：`getUserMedia` → `AudioContext` → `ScriptProcessorNode` → 重采样到 16k → Int16 → 20ms 分帧 → WS；时间线复用同一张 8 列表（文本 / 三项分数 / label·margin / 状态 / KEV 墙钟·自报 / 工具 top-3 / LLM 启动·作废·决策 / 各环节耗时），LLM 事件同样标色；顶部按钮：开始说话 / 停止 / 是否说完 + **保存为用例**。
- 「保存为用例」写 `Work/tmp/poc-streaming-mic-cases/<session_id>.jsonl`：一行一个整轮 = `{id, created_at, sample_rate:16000, frame_ms:20, label_complete（我标注的是否说完）, tool_set, config, asr_updates[{t_ms,text,final}], events[…], summary{t_last_word,t_confirmed,t_llm_start,t_decision,eot_calls,eot_skipped,tool_calls,llm_calls,llm_wasted}, decision, timeline_path}`。

**验收**
| 检查 | 结果 |
|---|---|
| 脚本模拟音频喂 websocket（`tmp/poc-streaming-mic-ws.py`，真 wav 按 20ms 实时推送） | **PASS 6/6**：2 partial + 1 final、`turn_done`、恰好 1 次有效决策、用例文件落盘且带标注、无协议错误 |
| 浏览器端到端（Chrome，把 `say` 生成的 16k wav 解码成 MediaStream 注入页面，走页面自己的采集+WS 代码） | **PASS**：752 帧送出（约 15s 音频）、ASR 2 partial + 1 final（`导航`→`导航回家`）、20 行时间线、`turn_done`（最后一字 1399ms → 确认 2099ms → LLM 启动 +324ms → 决策 +1847ms，eot 2 / 工具 2 / LLM 1 / 浪费 0）、「保存为用例」写出 `mic-…jsonl`（label=说完了） |
| 回归 | M1b PASS、A3 9/9、A4 11/11、A5 4/4（连跑 4 次稳定） |
| 截图 | `tmp/poc-streaming-mic-ui.png` |

**M4b · 页面可读性重做（用户反馈后）**
- **参数含义上页面**：TurnConfig 13 个字段每个都带一行说明（hover 字段名也有 title），例如 `eot_policy`="argmax 取最大 / margin 要求 complete 领先 max(cut,wait)"、`debounce_ms`="必须小于 ASR partial 间隔，否则每次判停都会被下一次更新顶掉"、`confirm_silence_ms`="必须小于 max_silence_ms，否则被兜底抢先"、`eot_max_inflight`=">1 才不会被 single-flight 饿死"。
- **时间线改为"判停卡片流"**（弃用原来 8 列大表）：**一张卡 = 一次 ASR 更新**，卡头是 `时间 / 文本 / vN / partial|final / 状态 / KEV 墙钟·自报`；卡身左侧是 complete/cut/wait **三条概率条 + 数值 + label·margin + 是否已过期**，右侧是**工具 top-3 芯片**和**该次更新下发生的事件流**（判停开始/结果/跳过、选工具、状态迁移、LLM 启动(投机)/作废/决策），全部带各自时间戳；卡片左边框按判停结论着色（绿=complete / 橙=cut / 蓝=wait），LLM 卡有蓝框、确认后整卡浅蓝底。
- **事件按"它评的那段文本"归卡**（服务端事件的 `text_version` 是发出时的最新版本，不能用来归组）；`asr_update` 建卡、`vad` 只记一行、空卡不再产生。
- **指标条**：顶部一行芯片 `最后一字 → 判停确认 +Xms → LLM 启动 +Yms → 决策 +Zms`、判停/选工具/LLM 次数与浪费、证据路径，外加一条按阶段配色的**瀑布条**（判停/启动/决策三段宽度按占比）；跑之前显示"等待判停…"和"最新识别"实时芯片。
- **原始 JSON 挪进抽屉**：时间线里再也不用看 `{"event":"asr_update",...}`，调试时展开 `<details>原始事件 JSON` 即可。
- 三个模式共用同一套渲染；重做后复跑 A3 9/9、A4 11/11、A5 4/4、M4 6/6、M1b PASS，单句模式功能不变（14 个 KEV 分数 + 10 行阶段时间线 + 决策）。
- 截图：`tmp/poc-timeline-v2.png`（脚本回放）、`tmp/poc-timeline-mic-v2.png`（麦克风）。

**M5 · 交互与展示修正（用户反馈后）**
- 修「暂停后不能再开始」：根因是旧连接的 `onclose` 异步清空了新一轮的 socket，加上切模式时把「开始说话」置灰；现在每轮带 `mic.turn` 编号、回调先校验，开始按钮永不被旧连接阻塞，停止后即可再开一轮，`mic.sessionId` 每轮重置。
- 用例保存改为 `POST /api/session/mic/save`（服务端登记最近 8 轮），WebSocket 关闭后仍可保存；WS `save` 命令保留。
- 默认进入即 `multidomain（200工具）` + `麦克风（真实流式 ASR）`，并立即应用模式（面板/按钮状态正确）。
- 时间线新增：每个判停结果后的 **margin 通过（绿）/未通过（无颜色）** 徽标（阈值取当前 `eot_margin`，只展示不改判定）；**VAD 结束标记**（麦克风用 ASR 断句 `end_time`，脚本回放用 `vad speech_end`）；指标条常驻 **`VAD 结束 Xms → 首工具 +Yms`** 实时芯片，即用户感知时延。
- 修 `stream_asr` 重复发送 last packet 导致的偶发 `45000000` ASR 错误。

**M4 已知限制**：① `ScriptProcessorNode` 已废弃、重采样是直接抽取（无抗混叠），PoC 够用；② 无头浏览器无法给真实麦克风权限，所以浏览器验收用"注入 MediaStream"代替真人说话，真机点一次即可；③ 麦克风模式是"按一次说一句"，没有做说话中打断捕获；④ 一个 server 进程同时只跑一个 mic 会话（判停/选工具仍共享远程 KEV）。

## 7. 复现命令

```bash
PY=/Users/YUYSONG/Agents/navi-dual-system/.venv/bin/python
CD=/Users/YUYSONG/Work/tmp/jev-reopt-checkouts/dev-streaming-eot

# 回归（A1）：两个 checkout 各起一个服务，再对拉
POC_CHECKOUT=$CD POC_PORT=8767 $PY -u -B /Users/YUYSONG/Work/tmp/jev-research/poc-ui/server.py &
$PY /Users/YUYSONG/Work/tmp/poc-streaming-a1-baseline.py 8776 /tmp/before.json   # 基线 checkout
$PY /Users/YUYSONG/Work/tmp/poc-streaming-a1-baseline.py 8767 /tmp/after.json

# A3 / A4（打 /api/session/stream）
$PY /Users/YUYSONG/Work/tmp/poc-streaming-a3.py 8767
$PY /Users/YUYSONG/Work/tmp/poc-streaming-a4.py 8767 --with-reuse

# M1b 真实流式 ASR
say -v Tingting -o /tmp/asr-zh.aiff "导航回家" && afconvert -f WAVE -d LEI16@16000 -c 1 /tmp/asr-zh.aiff /tmp/asr-zh.wav
$PY /Users/YUYSONG/Work/tmp/poc-streaming-m1b.py /tmp/asr-zh.wav

# M2 前缀用例 + 评测（真 KEV，决策 mock 1380ms）
cd $CD
$PY -u -B eval/build_prefix_cases.py --workers 4
$PY -u -B eval/streaming_prefix_eval.py --langs zh,en --prefix-interval-ms 150 --pause-ratio 0.2 --decide-ms 1380 \
    --run-id px-m2-primary --report /Users/YUYSONG/Work/tmp/poc-streaming-prefix-eval-report.md

# M3 配置对比（600ms 节奏，20 句/语言）
$PY -u -B eval/streaming_prefix_eval.py --langs zh,en --per-lang-limit 20 --prefix-interval-ms 600 --pause-ratio 0.2 \
    --decide-ms 1380 --skip-v0 --config '{"eot_policy":"margin","eot_margin":0.2,"confirm_silence_ms":700,
    "eot_timeout_ms":900,"debounce_ms":150,"speculative_llm":false,"eot_input":"last_segment"}' \
    --run-id px-m3-longconfirm --report /tmp/px-m3-longconfirm.md
```

# A5 投机反转取消
$PY /Users/YUYSONG/Work/tmp/poc-streaming-a5.py 8765

# (a) 改后完整重测（150ms 全量 + 600ms 两种配置）
bash /Users/YUYSONG/Work/tmp/poc-streaming-after-sweep.sh
bash /Users/YUYSONG/Work/tmp/poc-streaming-inflight-sweep.sh
$PY /Users/YUYSONG/Work/tmp/poc-streaming-kev-isolation-probe.py
```

两条 KEV 隧道（选工具走 `:8009`，判停走 `:8010`，同一个远程 `106.53.0.235:8009`）：
```bash
K=~/.ssh/kev/id_ed25519; H=ubuntu@106.53.0.235
ssh -i $K -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -N -L 8009:127.0.0.1:8009 $H &
ssh -i $K -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -N -L 8010:127.0.0.1:8009 $H &
```