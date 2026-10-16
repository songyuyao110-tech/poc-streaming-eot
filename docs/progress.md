# 流式 KEV 判停 PoC — 进度

依据：`tmp/poc-streaming-kev-eot-spec-20261015.md`（spec，以它为准）+ `tmp/poc-streaming-kev-eot-plan-20261015.md`（设计）。
规则：PoC 核心链路优先；单个 DEV；子步骤合并成一次交付/测试；不打印/复制任何 key 值。

---

## 2026-10-15 · 启动 + §1 ASR key 本机搜索（限时只读）

**做了什么**
- 读完 spec + 设计文档；定位实际路径（spec 里写的 `jev-reopt-checkouts/` 真实位置是 `tmp/jev-reopt-checkouts/`；`poc-ui/` 真实位置是 `tmp/jev-research/poc-ui/`，不在 git 里）。
- 确认 `dev-20261013` = worktree of `Agents/navi-dual-system`，分支 `poc-reopt-20261013` @ `fb5b259`。
- §1 限时只读搜索（env 变量名 / `~/.zshrc` / `~/.zprofile` / `~/.bash_profile` / `~/.config` / Work 下 `.env` / jev-voice / 本地 whisper）。

**搜索结果（只记位置与是否看起来有效，不含值）**

| provider | 位置（变量名 / 文件） | 是否看起来有效 |
|---|---|---|
| 火山引擎 / 豆包 流式 ASR（Volcengine bigasr sauc） | `~/.zshrc:114-117` → `ASR_BACKEND`、`CLOUD_ASR_APP_ID`、`CLOUD_ASR_ACCESS_TOKEN`、`CLOUD_ASR_RESOURCE_ID` | **是**：`ASR_BACKEND=volcengine`，`CLOUD_ASR_RESOURCE_ID=volc.bigasr.sauc.duration`，APP_ID/ACCESS_TOKEN 非空（只校验长度，未读取值）。当前 DSH 进程 env 里只有 APP_ID/RESOURCE_ID，ACCESS_TOKEN 需从 `~/.zshrc` 取。 |
| 火山引擎 端点/协议 | `Agents/AIOS Framework/config.py:82-96` → 默认 `wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async` | 有效依据：已有可复用客户端 `Agents/AIOS Framework/agent/cascade/volcengine_asr.py`（301 行，自带 v3 二进制帧协议 + gzip） |
| 火山引擎 key 备用位置 | `Agents/AIOS Framework/.env`（存在，权限 600） | 可能有效（未读值） |
| 阿里云/百炼 paraformer/qwen-asr | `Agents/AIOS Framework/config.py` 里的 `CASCADE_QWEN_ASR_*` / `DASHSCOPE_*` 默认值 | 未在本机 env/.env 找到对应 key（`CASCADE_ASR_PROVIDER` 默认 qwen，但无 key） |
| 讯飞 / 腾讯云 / Azure Speech / OpenAI whisper-realtime | 本机 env、shell rc、`.env`、`~/.config` | 未找到 |
| whisper.cpp / 本地 ASR | 未找到（`Agents` 下无 whisper.cpp）；`aios-wt-voice/voice/` 只有 `audio_io.py`/`vad_handler.py`/`session.py`，无本地 ASR | 不可用 |

**结论**：找到真实流式 ASR key（火山引擎，`~/.zshrc`）→ **M1 跑通后加 M1b 真实流式 ASR 适配器**（复用 AIOS 的 v3 帧协议实现）。M1–M3 主体仍用脚本化 ASR 回放。

**问题/风险**
- KEV 走 `127.0.0.1:8009` 隧道，需先确认连通。
- ASR 供应商端点在外网（`openspeech.bytedance.com`），本机代理环境（`HTTP_PROXY`/`ALL_PROXY` 已设）可能影响 WebSocket 握手；M1b 时验。
---

## 2026-10-15 · M1 核心链路（含 M1b 真实流式 ASR）— PASS

**做了什么**
- 新建 checkout `tmp/jev-reopt-checkouts/dev-streaming-eot`，分支 `poc/streaming-eot`（基于 `dev-20261013` @ `fb5b259`），单个 DEV 一次交付。原 `dev-20261013` 未改动、未 push。
- 修复环境：启动时 KEV 隧道（ssh PID 58402）已死 → 用 `~/.ssh/kev/id_ed25519` 重建 `127.0.0.1:8009` 隧道（远端 KEV 服务本身正常，POST noul 31ms 返回）。
- `eval/core_pipeline_run.py`：拆出 `select_tools()` / `decide()`，`run()` 依次调用二者；新增 `KEV_EOT_CLIENT`（max_connections=1）；`select_tools` 增加 `on_start` 回调保证 `kev_start` 事件载荷与改动前逐字段一致。
- 新增 `eval/turn_eot.py`（R2 三问 noul 原样复制 + `score_eot`）、`eval/turn_session.py`（TurnConfig/TurnSession/五态状态机/事件/单飞+pending/版本校验）
- `poc-ui/server.py`：新增 `POST /api/session/stream`（SSE 按真实时间回放），`POC_CHECKOUT`/`POC_PORT` 环境变量（便于前后对比）；旧接口保留。
- `poc-ui/index.html`：新增"流式模拟"模式（语速、`|600|` 停顿标记、TurnConfig 12 个字段可改、时间线表 8 列、LLM 事件单独标色）。
- 8765 已重启指向新 checkout（旧 PID 60277 → 新 PID 40763）；索引页刷新即可看到新模式。

**验收结果（全部 PASS）**
| 验收 | 结果 | 证据 |
|---|---|---|
| A1 回归 `/api/run` + `/api/run/stream` | **PASS**：4 个样例（3 导航 + 1 多域）改动前后归一化输出完全一致 | `tmp/poc-streaming-a1-before.json` / `-after.json`（对比脚本 `tmp/poc-streaming-a1-baseline.py`） |
| A2 `score_eot("导航回家")` | **PASS**：返回三概率 + label=complete + margin=0.5649 + wall_ms=134 / kev_ms=41。记录："我想去" label=complete 但 margin 仅 +0.0111（cut=0.4990 几乎持平），spec 允许记录不阻塞 | 见下 M2 B1 |
| A3 四个 V2 脚本（curl 打 `/api/session/stream` + UI 人工看） | **PASS 9/9** | `tmp/poc-streaming-a3-output.txt` |
| A4 timeline.json / turn_done 字段 / 选工具复用 | **PASS 11/11**（含注入桩函数的确定性复用检查：同一归一化文本只跑一轮选工具） | `tmp/poc-streaming-a4.py` |
| M1b 真实流式 ASR 适配器 | **PASS**：2 条 partial + 1 条 final，走完一轮且恰好 1 次有效决策 | `tmp/poc-streaming-m1b.py` |

A3 细节：① `导航回家` 1 次决策 / `llm_wasted=0`；② `我去买菜|600|对了还要买酱油` 进入 MAYBE_DONE→CONFIRMED 并启动 LLM，随后 `speech_start` 触发 `llm_cancel`，最终 1 次决策作用于合并文本；③ `帮我导航到|1500|人民广场` 进 CUT_OFF_WAIT，1500ms 停顿内无 LLM 启动，后缀并入后 1 次决策；④ `等一下我想想|2500|回家` 进 THINK_WAIT，期间无 LLM 启动，1 次决策。

**M1b（真实流式 ASR）**
- 供应商：火山引擎/豆包 v3 sauc 流式 ASR（`wss://openspeech.bytedance.com/api/v3/sauc/bigmodel_async`，`volc.bigasr.sauc.duration`）；凭证取自 `~/.zshrc` 的 `CLOUD_ASR_*`（**未打印、未复制任何 key 值**）。
- 客户端：复用 venv 里已有的 `simple-websocket`（Flask-SocketIO 依赖），**未新增依赖**；握手 170ms 连通。自定义 v3 二进制帧（gzip + 序号，注意握手占用序号 1，音频从 2 开始；`definite` 在 `result.utterances[0]` 里而不是 `result.is_final`）。
- 音频：`say -v Tingting` → `afconvert` 16k 单声道 16bit wav。
- 发现并记录：真实 ASR partial 间隔约 500ms > `confirm_silence_ms=300`，因此前半句会被 CONFIRMED 并真启动一次 LLM，随后新文本到来即 `llm_cancel`（浪费 1 次）——正是 plan 风险 V1 描述的现象，M3 调。

**过程中发现并修掉的实现问题（都是真实 bug）**
1. `poc-ui/server.py` 把 VAD 的 `kind` 字段覆盖成派发标签，导致 `speech_end/speech_start` 从未生效（A3-3 检查被跳过；已修并复跑）。
2. `_on_tools_result` 无条件按投机启动 LLM，忽略 `speculative_llm=false`（已修）。
3. EOT 超时被当作立即 CONFIRMED，会在用户仍在说话时提前调 LLM → 改为按设计文档走 `max_silence` 兜底（晚到的结果若版本仍最新仍会采用）。
4. `speech_start` 未重置静音锚点，用户重新开口时被 `max_silence` 立刻 CONFIRMED 并浪费一次 LLM（已修）。
5. CONFIRMED 后在途 LLM 未被登记作废就直接被新调用覆盖（`llm_wasted` 丢失、无 `llm_cancel` 事件）→ 改为任何在途 LLM 在文本变化/重新开口时统一作废。
6. 归一化后未变的 ASR 更新会把 CONFIRMED 打回 LISTENING，导致要用 `max_silence=2000ms` 兜底恢复（真实 ASR 路径实测多花约 1.4s）→ 未变更新不再重置状态。
7. turn 结束后重复到达的同文本 final 被当作 barge_in 开了新一轮（多一次 LLM）→ 同文本视为重复更新忽略。

**关键测量：判停与选工具互相拖慢（plan 风险 #3 命中）**
- 单发 EOT（`score_eot`）：p50 121ms / max 181ms，无 >400ms。
- 与选工具并发（2 线程探针）：EOT p50 115ms。
- 但在真实会话（ThreadPool + 150ms 前缀节奏 + 持续并发）中，`score_eot` 自报墙钟经常 340–556ms，超过 `eot_timeout_ms=400`，且多数结果因文本版本已前进而被判 `stale`。
- 结论：两条 KEV 调用经同一个 SSH 隧道（同一台 KEV）会互相排队；plan 假设"选工具在 partial 上提前跑是零成本"不成立，M2/M3 需据此评估与调优。

**其余记录**
- `/api/session/stream` 事件与 timeline.json 同一格式；每条含 `t_ms` 与 `text_version`；`turn_done` 汇总字段齐全并附 `timeline_path`。
- 证据目录：`dev-streaming-eot/eval/evidence/turn-session/<session_id>/timeline.json`。

---

## 2026-10-15 · M2 前缀回放评测 — 完成（1 项通过标准未达标）

**做了什么**
- `eval/build_prefix_cases.py`：中文 200 句（`tools-multidomain.yaml` 注释里的 sample_utterances）+ `core-pipeline-corpus.jsonl` 30 句按 1–2 字切前缀；英文 `pipecat-turn-eval-v2/cases.json` 168 轮按词切前缀。产出 `eval/fixtures/streaming-prefix-cases.jsonl`：**398 句 / 3307 前缀**（中文 230 句 2068 前缀，英文 168 句 1239 前缀），字段 `{id,lang,source,full_text,prefix,idx,is_full,gold,natural_complete,heldout,external_id}`。
- 真前缀默认 `gold=incomplete`；用 DeepSeek 逐句预标"可自然结束"的前缀 493 个（占 15%），单列不计误判（抽查发现偏宽松：如"雨刷""锁上"被判可独立成句，报告里同时给出"不排除"口径）。
- `eval/streaming_prefix_eval.py`：把前缀序列喂给 `TurnSession`（真 KEV 判停 + 真 KEV 选工具，决策 LLM 用固定 1380ms 的 mock = plan §4 实测均值，避免成本），约 20% 句中插入 400–800ms 停顿（停顿同时切分 ASR 段，后半句只送增量文本，这是 `last_segment` 与 `turn_text` 真正出现差异的地方）。
- 运行：`--prefix-interval-ms 150`（plan 的 100–200ms 假设）、398 句、470 条判停结果；明细 `eval/evidence/streaming-prefix/px-m2-primary/detail.jsonl` + `detail.csv` + `summary.json`。
- 报告：`Work/tmp/poc-streaming-prefix-eval-report.md`（脚本生成，数字同源）。

**B1（V0 回归）PASS**：`score_eot` 重跑英文 168 轮，**留出集二分类 53/56 = 94.6%**（与设计文档 [实测] 94.6% 完全一致，差 0 条）；全集二分类 157/168 = 93.5%（93 条未说完中 11 条误判说完——与设计文档描述性统计一致；75 条说完 0 条漏判）。设计文档的"三分类 97.4%"需要另一套 cut/wait 金标，`cases.json` 只有二分类金标，不宣称复现。

**B2 关键数字（150ms/前缀节奏）**

| 指标 | 中文(230句) | 英文(168句) |
|---|---|---|
| 判停覆盖率（被判到的非整句前缀 / 全部前缀） | 2.9% (60/2068) | 2.2% (27/1239) |
| 前缀误判说完率（按前缀，排除 natural） | 71.4% (30/42) | 9.5% (2/21) |
| 前缀误判说完率（按句） | 13.0% | 1.2% |
| 提前触发 LLM 率（按句） | 4.35% | 0.60% |
| 最后一字→CONFIRMED 中位 / p90 | 440 / 789 ms | 871 / 3002 ms |
| 最后一字→LLM 启动 中位 / p90 | 708 / 910 ms | 960 / 3002 ms |
| 最后一字→决策 中位 / p90 | 2098 / 2301 ms | 2352 / 4392 ms |
| 每句 KEV 判停调用 / 跳过（均值） | 4.46 / 5.10 | 3.89 / 3.60 |
| 每句选工具调用（均值） | 4.09 | 3.55 |
| LLM 调用 / 浪费 / 浪费率 | 240/10/4.2% | 169/1/0.6% |
| 恰 1 次有效决策的句子 | 230/230 | 168/168 |

**B3 通过标准判定**：4/5 PASS，1 项 FAIL
- PASS 整句二分类 ≥93%（93.5% / 留出 94.6%）
- PASS 提前触发 LLM 率 ≤5%（4.35% / 0.60%）
- **FAIL 最后一字→LLM 启动中位 ≤800ms**：中文 708ms 达标，**英文 960ms 超标**（确认窗 300ms + 选工具还在途）
- PASS 每轮有效 LLM 决策恰好 1 次（398/398）
- PASS 投机浪费率 ≤30%（未开投机，浪费率 4.2% / 0.6%）

**关键发现（比数字更重要）**
1. **判停被"合并"饿死**：150ms 前缀节奏下，单飞+pending 合并导致绝大多数中间前缀拿不到判停结果（覆盖率仅 2–3%），只有"停顿前/最后一段"的前缀能落地。所以"提前触发 LLM 率≈0"不等于模型判得准，而是判停结果来晚了、没赶上任何停顿。
2. **误判率只能按"被判到的前缀"算，且有采样偏差**：能被判到的前缀多是停顿前/句尾（本身就更像完整话语），所以中文 71%、英文 9.5% 这个条件概率不能外推到全部前缀。英文比中文好很多，中文车载口语判停确实更容易误判说完（plan 风险 #2 命中）。
3. **EOT 与选工具互相排队（plan 风险 #3 命中）**：判停调用 4.46 次/句里有一半以上以 `timeout`（>400ms）或 `stale`（版本已前进）结束；单发 EOT 只要 121ms，并发+真实会话下涨到 340–556ms。这既是 `eot_timeout_ms=400` 频繁触发的原因，也是英文"最后一字→LLM 启动 960ms"的主因。
4. `natural_complete` 预标偏差已单列；报告中同时给出排除/不排除两种口径。

---

## 2026-10-15 · M3 调优 + 最终报告 — 完成（B3 有一项不可同时满足，已写明）

**做了什么**
- 调优对比（统一在 **600ms/前缀** 节奏上跑，20 句中文 + 20 句英文，让判停真正判得到前缀；150ms 节奏下覆盖率只有 2–3%，数字没有区分度）：

| 配置 | 判停覆盖 zh/en | 前缀误判说完 zh/en | 提前触发 LLM zh/en | 最后一字→LLM 启动 zh/en | LLM 浪费率 zh/en |
|---|---|---|---|---|---|
| 默认 | 87%/81% | 48.3%/12.5% | 100%/85% | 573/569 ms | 82.9%/62.3% |
| confirm250+timeout900+debounce150+投机 on | 78%/79% | 50.2%/12.6% | 100%/65% | 612/633 ms | 74.0%/44.4% |
| ＋margin 0.2 | 74%/81% | 46.9%/12.5% | 95%/50% | 675/667 ms | 62.3%/37.5% |
| ＋eot_input=turn_text | 85%/80% | 50.9%/12.6% | 100%/75% | 664/608 ms | 74.0%/50.0% |
| **＋margin0.2+confirm700+关投机（采用）** | 70%/80% | 46.5%/12.6% | **0%/0%** | **1159/704 ms** | **0%/0%** |

- `eot_input`：`last_segment` 略优于 `turn_text`（英文提前触发 65% vs 75%，误判率持平）→ 保留 `last_segment`。
- **C1 结论（部分达成）**：找到把"提前触发 LLM 率"和"投机浪费率"压到 0% 的配置（`eot_margin=0.2` + `confirm_silence_ms=700`），但代价是中文"最后一字→LLM 启动"中位 1159ms（标准 ≤800ms）。**"提前触发 ≤5%" 与 "启动延迟 ≤800ms" 在纯文本 KEV 下不可同时满足**；中文判停是瓶颈（各配置误判说完率 46.5–50.9%，提前触发 95–100%），证实 plan 风险 #2。下一步按设计文档兜底：加音频/韵律判停模型（Smart Turn 类）或中文专用判停 prompt/模型。
- **C2 PASS**：默认 `TurnConfig` 更新为推荐值（`eot_policy=margin, eot_margin=0.2, confirm_silence_ms=700, eot_timeout_ms=900, debounce_ms=150, eot_input=last_segment, speculative_llm=false`），`poc-ui/index.html` 的 TurnConfig 默认值同步；重启 8765/8767 后复跑 A3 **9/9 PASS**、A4 **11/11 PASS**、M1b **PASS**（且真实 ASR 路径在确认窗 700ms > partial 间隔 ~500ms 后，从"提前调一次 LLM 再作废"变成 `llm_calls=1 / llm_wasted=0`）。
- 另修：`debounce_ms` 现在同时作用于选工具（原来选工具一有文本变化就发，与判停抢同一条隧道）。

**代码提交**：本 checkout 内已提交到 `poc/streaming-eot`（commit `07520cc`），未 push、未动 `dev-20261013`。

**C3 最终报告**：`/Users/YUYSONG/Work/tmp/poc-streaming-final-report.md`
（M2 明细报告：`/Users/YUYSONG/Work/tmp/poc-streaming-prefix-eval-report.md`；M3 各配置报告：`/Users/YUYSONG/Work/tmp/poc-streaming-m3-{baseline,tuned,margin,turntext,longconfirm}.md`）

**收尾状态（2026-10-15）**
- 最终代码提交：`dev-streaming-eot` @ `07520cc`（分支 `poc/streaming-eot`，未 push；`dev-20261013` 未改动）。
- 交付 UI：`http://127.0.0.1:8765/`（指向 `dev-streaming-eot`），按最终默认配置再跑一次 A3 = **9/9 PASS**（`tmp/poc-streaming-a3-8765-final.txt`）；A1 最终复核 before==after **完全一致**（`/tmp/a1-{before,after}-final.json`）。
- 进程：只留 `127.0.0.1:8009` KEV 隧道（ssh PID 26970，本次重建）与 8765 UI；临时对比端口 8767/8776 已关闭。
- **最终报告路径：`/Users/YUYSONG/Work/tmp/poc-streaming-final-report.md`**

---

## 2026-10-15 · M3 续：(a) 判停饥饿修复 + (b) 投机保留 — 完成

**(a) EOT 独立隧道 + 去掉 single-flight**
- 第二条 SSH 隧道：`127.0.0.1:8010 -> 106.53.0.235:8009`（EOT 专用，与选工具 `:8009` 分离）；`core_pipeline_run.KEV_EOT_BASE`（可用环境变量覆盖）、`KEV_EOT_CLIENT` 连接数 1 → 4。
- `TurnSession`：`_jobs["eot"]` 单槽 → `_eot_jobs` 列表 + `TurnConfig.eot_max_inflight`（默认 3）；每个变化的前缀都发一次判停，不再等前一次回来；**判停结果无论是否过期都上报**（`eot_scores.stale`），状态机只在版本仍最新时才采用。
- 关键副作用（必须记）：`debounce_ms` 必须 < ASR partial 间隔。第一版把默认 debounce 提到 150ms 时，150ms 韵律下每个 pending 都被下一次更新替换 → 一次判停都不发、覆盖率反而归零；默认回到 80ms。

**改前 / 改后（150ms/前缀，398 句，plan 的韵律假设）**

| 指标 | 中文改前 | 中文改后 | 英文改前 | 英文改后 |
|---|---|---|---|---|
| 判停覆盖率（非整句前缀） | 3.3% (60/1838) | **95.0% (1746/1838)** | 2.5% (27/1071) | **89.2% (955/1071)** |
| 判停结果总条数 | 275 | 2030 | 195 | 1203 |
| 每句可用（非 stale）判停结果（含整句） | 1.20 | 1.21 | 1.16 | 1.20 |
| 每句判停调用数 | 4.46 | 8.83 | 3.89 | 7.17 |
| 单次墙钟 p50 / p90 | 180 / 481 ms（幸存者偏差） | 263 / 444 ms（全量） | 348 / 491 ms | 267 / 463 ms |
| 前缀误判说完率（按前缀） | 71.4% (30/42) | **62.1% (898/1445)** | 9.5% (2/21) | **23.8% (188/791)** |
| 前缀误判说完率（按句） | 13.0% | 97.4% | 1.2% | 44.6% |
| 提前触发 LLM 率 | 4.35% | 3.48% | 0.60% | 0.00% |
| 最后一字→LLM 启动 中位 | 708 ms | 755 ms | 960 ms | 1501 ms |
| 恰 1 次有效决策 | 230/230 | 230/230 | 168/168 | 168/168 |
| LLM 浪费率 | 4.2% | 3.4% | 0.6% | 0.0% |

覆盖率的"变大"让误判率第一次测准：**改前只统计到能落地的少量（偏向句尾/停顿前）前缀（42/21 个），改后是全量前缀**。改后数字才是可信的判停质量：中文 62.1% / 英文 23.8%（按句 97.4% / 44.6%）→ 纯文本判停是首要瓶颈。

**基础设施（a）的直接测量**
- 微基准（2 线程单轮）：EOT 与选工具同隧道 p50 115ms / 分隧道 p50 121ms → **单轮微基准测不出隧道争用**；会话内改前的 340–556ms 来自"一轮要等一次选工具往返 + GIL 争用"，不是隧道本身。
- `eot_max_inflight` 权衡（150ms、20 句中文、全量统计）：**1 → 覆盖 42.1%、p50 427ms；2 → 70.0%、369ms；3 → 93.9%、268ms**。并发越高覆盖越高且单次更快（远程 batched 服务）→ 默认取 3。
- 600ms 韵律下（本来就没有饥饿）：同旧计时器 改前 140.6ms → 改后 145.9ms，提前触发/浪费率不变（100%/85%、82.9%/62.3% → 100%/85%、82.5%/63.6%）。

**(b) 投机策略保留 + 反转正确取消**
- 默认改为 `speculative_llm=true, spec_margin=0.3`（配合 `eot_margin=0.2, confirm_silence_ms=700, eot_timeout_ms=900, debounce_ms=80, eot_max_inflight=3`）。
- 新增 `tmp/poc-streaming-a5.py` 反向用例（刻意构造"判完整→用户继续说话"）：**投机在 1.03s 启动 → 2.0s `llm_cancel(reason=speech_start)` → 最终恰好 1 次有效决策、浪费 1 次已记账 → A5 4/4 PASS**。
- 另修：已完成的投机结果（`_llm_prepared`）被推翻时原先静默丢弃、不记账 → 现在同样 `llm_cancel` + `llm_wasted++`；并加了"已有可用投机结果就不再重复发"的守卫。
- 不再为英文"最后一字→LLM 启动 1501ms"调优（用户确认可接受）。

**复跑（最终默认配置，8765 交付实例）**：A1 KEV 阶段逐字节一致（`tmp/poc-streaming-a1-compare.py`；基线代码连跑两次也会因 DeepSeek 歧义选择不同）、A3 **9/9**、A4 **11/11**、A5 **4/4**、M1b **PASS**。

**B3 复判（150ms，最终配置）**：4 PASS / 1 FAIL —— FAIL 项为英文"最后一字→LLM 启动中位 1501ms > 800ms"（按 (b) 接受）。
同一配置在 600ms（判停真正可用）下：提前触发 LLM 100%/70%、浪费 75.9%/47.4% —— 投机开着时这是设计行为，代价记在浪费率。

**提交**：`caa73d8`（第二笔，M3 续）。报告已更新：`/Users/YUYSONG/Work/tmp/poc-streaming-final-report.md`。

**收尾状态（M3 续，最终）**
- 进程：KEV 隧道两条 `:8009`（选工具）+ `:8010`（判停）→ 同一远程 `106.53.0.235:8009`；交付 UI `http://127.0.0.1:8765/`（指向 `dev-streaming-eot`）；临时对比端口 8766/8767/8776 已关闭。
- 最终报告路径：`/Users/YUYSONG/Work/tmp/poc-streaming-final-report.md`
- 改后 M2 报告：`/Users/YUYSONG/Work/tmp/poc-streaming-prefix-eval-report-after.md`；M3 改后：`/Users/YUYSONG/Work/tmp/poc-streaming-m3-after-{default,oldtimers}.md`；inflight 权衡：`/tmp/px-inflight-{1,2,3}.md`。

---

## 2026-10-16 · M4 麦克风模式（真实流式 ASR 进 UI）— 完成

**做了什么**
- `eval/asr_stream.py`：新增推流式 `ASRStream`（`push/poll/finish/close`），原 wav 版 `stream_asr()` 改为基于它；key 仍只在运行时从 `~/.zshrc` 的 `CLOUD_ASR_*` 读入（**未打印/未复制**）。
- `poc-ui/server.py`：新增 **WebSocket** `@app.route("/api/session/mic", websocket=True)`（Flask 开发服务器 + 已在 venv 里的 `simple-websocket`，**未新增依赖**）。浏览器推 16kHz/单声道/16bit、20ms 一帧（640B）PCM → 火山豆包流式 ASR → partial/final 实时进 `TurnSession`（判停 + 选工具 + 投机 LLM）。四个线程：收帧 / ASR / 驱动（所有状态变更只在这条线程）/ 发送（只它写 socket）。
- WS 协议：浏览器→server：二进制帧 = PCM，文本 = `{"cmd":"config"|"stop"|"save"}`；server→浏览器：`{"kind":"ready"|"asr"|"event"|"saved"|"stopping"|"config"|"error","data":…}`。
- `poc-ui/index.html`：新增「麦克风（真实流式 ASR）」模式 —— `getUserMedia` → AudioContext → ScriptProcessorNode → 重采样 16k → Int16 → 20ms 分帧 → WS；时间线复用同一张 8 列表（文本 / 三项分数 / label·margin / 状态 / KEV 墙钟·自报 / 工具 top-3 / LLM 启动·作废·决策 / 各环节耗时），LLM 事件标色；按钮：🎤开始说话 / 停止 / 「是否说完」下拉 / **保存为用例**；TurnConfig 面板（13 字段）在该模式下同样可改。
- 「保存为用例」→ `Work/tmp/poc-streaming-mic-cases/<session_id>.jsonl`，一行一个整轮：`asr_updates[{t_ms,text,final}] + events[] + summary + decision + label_complete（我标注的是否说完）`。

**验收**
- **脚本喂 websocket（`tmp/poc-streaming-mic-ws.py`，真 wav 按 20ms 实时推）**：**PASS 6/6** —— 2 partial + 1 final（`导航`→`导航回家`）、`turn_done`、恰好 1 次有效决策、用例文件落盘且 `label_complete=true`、无协议错误。
- **浏览器端到端（真语音）**：把 `say` 生成的 wav 解码成 MediaStream 注入页面（走页面自己的采集 + WS 代码）：752 帧、2 partial + 1 final、20 行时间线、`turn_done`（最后一字 1399ms → 确认 2099ms → LLM 启动 +324ms → 决策 +1847ms；eot 2 / 工具 2 / LLM 1 / 浪费 0）、保存出 `mic-…jsonl`（标注"说完了"）。截图 `tmp/poc-streaming-mic-ui.png`。
- **回归**：M1b PASS（`ASRStream` 重构后仍 2 partial + 1 final + 1 决策）、A3 9/9、A4 11/11、A5 4/4（连跑 4 次稳定）。
- 修 A5 测试自身的两个不稳定点（不是产品问题）：`max_silence_ms` 必须大于 `confirm_silence_ms`，否则长确认窗会被 max_silence 提前确认；投机是否启动受 KEV margin 波动影响，验收用例把 `spec_margin` 设为 0 只测"取消"。

**提交**：`e9e0001`（M4，ASRStream）。`poc-ui/server.py`、`poc-ui/index.html` 在 git 外（`tmp/jev-research/poc-ui/`），未提交。

### 怎么用
1. 起两条 KEV 隧道 + UI（若已在跑可跳过）：
   ```
   K=~/.ssh/kev/id_ed25519; H=ubuntu@106.53.0.235
   ssh -i $K -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -N -L 8009:127.0.0.1:8009 $H &
   ssh -i $K -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -N -L 8010:127.0.0.1:8009 $H &
   /Users/YUYSONG/Agents/navi-dual-system/.venv/bin/python -u -B /Users/YUYSONG/Work/tmp/jev-research/poc-ui/server.py
   ```
2. 打开 `http://127.0.0.1:8765/` → 运行模式选 **麦克风（真实流式 ASR）** → 点 **🎤 开始说话** → 首次会弹麦克风授权 → 说一句（如"导航回家"）→ 点 **停止**。
3. 页面会实时出现：每次 ASR 更新的文本、判停三项分数 + label/margin、状态变化（LISTENING/MAYBE_DONE/CUT_OFF_WAIT/THINK_WAIT/CONFIRMED）、选工具 top-3、LLM 启动（投机）/作废/决策，底部一行是各环节耗时汇总；结束后选「是否说完」→ 点 **保存为用例**，文件进 `Work/tmp/poc-streaming-mic-cases/`。
4. 不开麦克风也能验链路：`/Users/YUYSONG/Agents/navi-dual-system/.venv/bin/python /Users/YUYSONG/Work/tmp/poc-streaming-mic-ws.py`（默认喂 `/tmp/asr-zh.wav`，可传自己的 wav）。
5. 注意：麦克风需要 localhost/https（`127.0.0.1` 满足）；开始说话前可在面板里改 TurnConfig；ASR key 来自 `~/.zshrc`，运行时不打印。

**M4 收尾状态**
- 交付 UI 仍在 `http://127.0.0.1:8765/`（指向 `dev-streaming-eot`），已包含「麦克风（真实流式 ASR）」模式；两条 KEV 隧道 `:8009`（选工具）/`:8010`（判停）到同一远程 `106.53.0.235:8009`。
- 用例目录：`/Users/YUYSONG/Work/tmp/poc-streaming-mic-cases/`（现有 2 条：脚本喂 wav 一条、浏览器注入真语音一条，均 `label_complete=true`）。
- 最终报告（含 §6b M4）：`/Users/YUYSONG/Work/tmp/poc-streaming-final-report.md`

---

## 2026-10-16 · M4b 页面可读性重做（用户反馈：参数含义要上页面 + 日志可视化太差）

**参数说明上页面**：TurnConfig 13 个字段每个带一行中文说明（hover 也有 title）：
- `eot_policy` 判"说完了"的策略（argmax=取最大 / margin=complete 必须领先 max(cut,wait) 超过 eot_margin）
- `eot_margin` margin 策略要求的领先幅度（推荐 0.2，越大越不容易误判说完但会漏判）
- `spec_margin` 进入"可能说完"且 margin ≥ 此值才投机调 LLM（0=只要可能说完就投机）
- `speculative_llm` 确认前就投机调 DeepSeek，被推翻时作废并计入 llm_wasted
- `eot_input` 判停送什么文本（last_segment=当前段，<4 字拼上一段 / turn_text=本轮累计全文）
- `confirm_silence_ms` 判 complete 后要连续静音多久才确认（必须 < max_silence_ms）
- `cut_wait_ms` 判 cut 后再等多久强制确认，避免卡住
- `think_wait_ms` 判 wait 时最多等多久确认，期间不调 LLM
- `max_silence_ms` 兜底：任何状态静音到这么久就确认（KEV 失败/超时也走这条）
- `eot_timeout_ms` 单次判停超过多久算超时（放弃该次判定、退回静音兜底；晚到结果仍展示）
- `debounce_ms` 文本变化后等多久才发判停/选工具（必须 < ASR partial 间隔，否则每次判停都被顶掉）
- `eot_max_inflight` 同时最多几个判停在途（>1 才不会被 single-flight 饿死）
- `reuse_tools_if_text_unchanged` 最终文本归一化后与已评分相同则复用短名单，不再调 KEV

**时间线重做（判停卡片流）**：一张卡 = 一次 ASR 更新；卡头 `时间/文本/vN/partial|final/状态/KEV 耗时`；卡身左侧三条概率条 + label·margin + 过期标记，右侧工具 top-3 芯片 + 该次更新下的事件流（带各自时间戳）；卡片按判停结论着色，LLM 卡蓝框、决策绿、作废橙；顶部指标条 = 各阶段延迟芯片 + 瀑布条 + 计数 + 证据路径 + "最新识别"实时芯片；原始 JSON 移入折叠抽屉。修了三个渲染问题：`vad` 不该开卡、事件要按"它评的那段文本"归卡（服务端 `text_version` 是发出时的最新版本）、`asr_update` 必须自己开卡（原守卫把它吞了）。

**回归**：A3 9/9、A4 11/11、A5 4/4、M4 6/6、M1b PASS，单句模式不变。截图 `tmp/poc-timeline-v2.png`、`tmp/poc-timeline-mic-v2.png`。

---

## 2026-10-16 · M5 交互与展示修正（用户反馈）

**1. 暂停后无法再次开始（真 bug，两处）**
- 旧连接的 `onclose` 会把**新一轮**的 `mic.socket` 置空（旧连接关闭是异步的），导致第二轮帧根本没发出去；现在每一轮带编号 `mic.turn`，所有 socket 回调先校验 `mic.turn === turn` 才生效。
- `applyMode()` 在"还有旧连接"时禁用「开始说话」，切模式后按钮点不动；改为开始按钮永不被旧连接阻塞（新一轮自己收掉旧连接，旧轮用例已在服务端登记、仍可保存）。
- 「停止」后立即恢复「开始说话」+「保存为用例」；`micStart` 会先关掉上一轮连接；`mic.sessionId` 每轮清空，避免存错轮。
- 用例保存改为 **HTTP `POST /api/session/mic/save`**（服务端 `MIC_SESSIONS` 登记最近 8 轮），socket 关了也能存；WS 的 `save` 命令保留给脚本用。

**2. 默认值**：进页面默认 `multidomain（200工具/两阶段）` + `麦克风（真实流式 ASR）`，并立即执行 `applyMode()` 把面板摆对（之前默认选中了但面板没显示）。

**3. margin 两个状态上日志**（不改判定策略）：每个判停结果后面跟一枚徽标——**绿色**`margin 通过（+0.565 ≥ 0.200）→ 判停`；**无颜色**`margin 未通过（… < 0.200）→ 继续听` 或 `非 complete（cut/wait）→ 继续听`；已过期（stale）的判定统一无颜色并注明"（已过期，未用于判停）"。阈值取自当前 TurnConfig，所以改 `eot_margin` 徽标跟着变。

**4. 标出音频 VAD 在哪里结束 + VAD结束→首工具返回**
- 麦克风模式没有独立 VAD，用**流式 ASR 断句的 `utterances[].end_time`** 作为"音频 VAD 结束"（`asr_stream.parse_message` 现在返回 `(text, final, end_time)` 三元组，server 透传 `vad_end_ms`，仅用于展示、不进状态机）；脚本回放模式直接用脚本给的 `vad speech_end`。
- 卡片事件流里出现紫色 `VAD 结束（VAD speech_end / ASR 断句 end_time）· t=740ms`，指标条常驻一枚紫色芯片 `VAD 结束 740ms → 首工具 +1688ms`（实时更新，不必等 turn_done）。
- 基准取每轮**最后一次** VAD 结束；若基准后移而配对的首个工具返回在基准之前，会重配（修掉过一次 `+-1701ms` 负值）。
- 顺带修掉一个 M4 重构遗留 bug：`stream_asr` 推完最后一个包又让 `finish()` 再发一次 last packet，火山返回 `45000000 last packet has been received already`（偶发），现在 finish() 独占 last packet。

**验收**：A3 9/9、A4 11/11、A5 4/4、M4 6/6、M1b PASS；浏览器同一页面**连开两轮**真语音各 283/290 帧、2 partial+1 final、2 张卡、`VAD 结束 740ms → 首工具 +1233ms / +1142ms`、绿灰徽标同时可见、停止后按钮可用、HTTP 保存成功（`mic-20261016-111252-5ec8a9.jsonl`，tool_set=multidomain，asr_updates 带 vad_end_ms）。截图 `tmp/poc-mic-v4.png`、`tmp/poc-timeline-v3.png`。
