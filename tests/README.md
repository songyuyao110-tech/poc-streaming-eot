# tests · 验收与探针

沿用旧文件名（报告与进度日志里引用了这些名字），只做了目录归置。全部脚本可独立运行，
路径通过 `src/bootstrap.py` 解析（`POC_UPSTREAM` 覆盖上游仓库位置）。

## 目录（2026-11-11 归置）

脚本按「测哪块」分目录，文件名一律沿用旧名（报告与进度日志里引用了这些名字）：

| 目录 | 测哪块 | 典型命令 |
|---|---|---|
| `gate/` | 判停 / 开闸 / 每席回合状态机 / 旁听台账 / 确认闸门 | `python3 -B tests/gate/test_hub_gate.py` |
| `asr/` | 流式 ASR / 双席 / 双路文本用例 | `python3 -B tests/asr/run_dual_asr_text_cases.py` |
| `mind/` | 读心：场景、真模型引擎、真麦通道 | `python3 -B tests/mind/test_mind_reading.py` |
| `memory/` | 记忆接线：收住那一刻写、发起请求那一刻查（假记忆记账，不连 memos） | `python3 -B tests/memory/test_memory_wire.py` |
| `agent/` | 编排（`SessionBrain`）/ 任务台账 / 相关打分 | `python3 -B tests/agent/test_orchestra.py` |
| `app/` | 页面接线：双席 UI / 双席真麦前端契约 / SSE 回放 | `python3 -B tests/app/test_dual_mic_ui_contract.py` |
| `legacy/` | 历史基线 / 对比 / 诊断 / 断言面自检 | `python3 -B tests/legacy/test_assert_surface.py` |

留在 `tests/` 根：`_audit_smoke.py`（`--audit tests._audit_smoke:fake_audit` 用的假 audit）、`probes/`（调参 shell 驱动）、
本文件。数据集跟着用它的脚本走（`asr/dual_asr_text_cases.jsonl`、`gate/confirm_cases.json`）。

## 验收

| 脚本 | 验证内容 | 期望 |
|---|---|---|
| `poc-streaming-c1c4.py` | 多轮消费核心 + F1/F2：单/双段消费、投机作废、整句重发去重（已消费全文前缀）、迟到 EOT 空后缀、**C6 决策落地即关回合（默认口径）** | 54/54 |
| `measure_turn_tail.py [--legacy]` | 真链路（8009 + 真两阶段选工具 + 真 LLM）量「决策落地 → `turn_done`」的尾巴：默认应为 0ms，`--legacy` 复现旧口径 ~2002ms | 默认 0ms / legacy ~2s |
| `test_confirm_gate.py` | 对话式二次确认闸门：`judge_confirm_reply` 纯裁决（6 验收场+边界）、`score_confirm_reply` mock 稳定、CONFIRM_POOL/文案（离线，不连 KEV/LLM） | 53/53 |
| `test_eot_judge.py` | 判停调用契约（eot-judge-api 单 choice 问题 / 响应强校验 / 重复键）+ `score` 四分支：强 complete 抢跑、弱 complete 等静默串行、cut、wait；`argmax`/`margin` A/B 保留；env 覆盖（离线，假客户端） | 24/24 |
| `poc-streaming-eot-baseline.py [--smoke]` | **A0 基线**：单席 20 轮「最后一字→CONFIRMED」，真判停 + 假工具/决策（需判停隧道） | 20/20 确认、0 判停错误，登记 p50/p90 |
| `poc-streaming-eot-quality.py [--smoke] [--skip-en]` | **A0 质量回归**：中文 230 句 / 2068 前缀（整句召回、非末前缀误判）+ 英文 heldout 56 句（argmax / 0.80 阈值），真判停 | 中召回 ≥97.8%、非末 ≤75.7%；英 87.5% / 80.4%（不与旧 94.6% 混比） |
| `poc_confirm_flow.py` | 二次确认端到端（纯本地 mock）：一轮选中池内工具 → 二轮 intent → 执行/放弃/接管；池外直接执行 | PASS |
| `poc_confirm_e2e.py` | **确认闸门长进 TurnSession 主循环的全链路端到端**：第一轮 decide 命中池内工具 → CONFIRM_WAIT → 发确认请求 → 二轮回 EOT 判停收敛 → score_confirm_reply(mock)+judge → 四分支（a-e 五条路径） | PASS |
| `confirm_cases.json` + `run_confirm_cases.py` | **交互层用例集（31 条二轮回→意图）**：L1 mock 启发式 / L2 judge 注入 / L3 真实 KEV 三层验收。L2 纯逻辑主证据须 31/31，L3 需隧道按 §（docs/test-design-confirmation-v2.md）判中率门槛 | L2 31/31，L1 26+5N/A |
| `probe_confirm_kev_real.py` | 真实 KEV 三问打分探针：验收表 6 样例落在哪侧（需 KEV 服务 :8009 / `KEV_EOT_BASE`，用于校准阈值） | 命中数打表 |
| `poc-streaming-s1s3-core-score.py` | S1–S3：core_score（242/17）两阶段召回、短名单家族、一轮一次决策、执行保持 mock（需 KEV + LLM） | 21/21 |
| `poc-streaming-a3.py PORT` | 脚本化 ASR 回放 → SSE：判停/状态迁移/四类脚本用例（需 UI 在跑） | 10/10 |
| `poc-streaming-a4.py PORT [--with-reuse]` | 停顿分段、工具复用、barge-in | 8/8（+reuse 11/11） |
| `poc-streaming-a5.py PORT` | 投机 LLM 被用户继续说推翻 → 作废与浪费记账 | 4/4 |
| `poc-streaming-mic-ws.py` | 真 wav 按 20ms 推 WebSocket → ASR → 判停 → 决策 → 用例落盘 | 6/6 |
| `poc-streaming-m1b.py [wav]` | 文件流式 ASR 端到端（2 partial + 1 final + 1 决策） | PASS |
| `dual_asr_text_cases.jsonl` + `run_dual_asr_text_cases.py` | **37 条双路 ASR 文本用例**：wave-1 16 条判停/隔离可跑（TC-34 复用 53/53）+ wave-2 20 条语义断言面（`STAGED`，等 C2/C3/C4；`--audit` 一接即门槛化） | wave-1 PASS / wave-2 STAGED |
| `test_assert_surface.py` | **离线断言面自检**：REGISTRY 覆盖 37 条 expect、标量/区间、orchestra BLOCKED→PASS/FAIL、`late_verdict_drives` 两方向、`ask_user/decide/no_splice/own_text_only` 正反例、`dump_surface` 20 字段（不连 8009） | N/N |
| `test_mind_reading.py` | **读心主通路离线验收（两路 ASR 输入）**：两条演示（闲聊→不响应 / 对车→需要响应）、partial 暂定、帧真进模块、本席旁听不跨席、**一个会话一份场景（两路都在 / 单路 / 只有画面；一次请求带齐两路，两席闸门取同一份）**，以及真模型调度契约（fake engine：主路径 / pending 不冒充规则结论 / 失败回落带 `rule_fallback` 收据 / 最近帧随 ASR 一起进模型 / 本席上句作为上下文 / **切引擎：同一会话换 model 生效、旧模型在途结果作废且不算已消费**）、**请求时机契约（`RequestTiming`，wiki `[[读心请求时机]]`）：只有「中枢把句收住」与「没在说话且画面变了」两处发请求；出字还在变 / 字稳住一小段 / ASR 自己报 final 都不请求；同一句收住只读一次；收住时带最完整的字 + 当时画面；另一路还在说也一起带上** | 39/39 |
| `test_mind_llm.py` | **读心真多模态引擎离线验收**：请求形状（默认档模型 id + `image_url` data URI PNG，含 chunk 顺序/CRC 回归）、**页面三选一目录（只有 `cc/glm-5.2` / `cc/glm-5.3-flash` / `deepseek-v4.1-flash`，默认 deepseek-v4.1-flash 且默认标记只有这一档，选项带能不能读图+快慢标签）**、**thinking 只对 deepseek 档加（另两档请求体不带）**、`vision.capability` 标 glm-5.2 视觉不可用且不冒充真多模态、未知模型 id 报 config、视觉被后端拒绝→同模型 text-only 降级收据、HTTP/超时/坏 JSON/空内容分类、凭据缺失与 `MIND_ENGINE` 开关、默认超时 30s / `max_tokens` 768、本席上下文（不连外部服务） | 26/26 |
| `test_dual_mic_ws.py` | **双席真麦离线契约**（脚本假 ASR）：open 拿 session_id → 两路 WS 汇合 → 两席各消费自己的文本 / 不串席 / cross_cancel=cross_reset=0 / duo_done；单路只收口该席；**读心只推一份两路场景（shared、不带 seat_id，标了席别）**。开闸那一侧要用离线接不接替身：`HUB_ACCEPT=rule`（默认会打 8009，隧道不在时 `accept_unavailable` 会一直不开闸） | 9 例 |
| `poc_dual_mic_wav_replay.py` | **双席真麦真链**：两路真实火山豆包流式 ASR（D/P 各一路 wav）同时回放 → 各进各席、不串席、duo_done（需 `CLOUD_ASR_*`） | PASS |
| `asr/test_neixinxi_page.py` | **内心戏页（/neixinxi）离线契约**（脚本假 ASR）：一页一 session 边界写清 / 两席并行各自只收自己的字（本席事件只发本席连接）/ 音量门槛没过线这一路不识别·调高立刻安静·调回立刻恢复 / 每路各自判停只给三态+可不可确认 / 画面彩色 data URI 原样进读心 / `eot=real` 真调 `gate.turn_eot.score_eot`（假函数，不连网）/ 不建 `TurnSession`（无闸门·无记忆·不进 Agent） | 7 组 |
| `asr/poc_neixinxi_wav_replay.py` | **内心戏页真链**：两路真实火山豆包流式 ASR（D/P 各一路 wav）同时回放 → 各自出字、不串席、各自判停、读心两路 lane（需 `CLOUD_ASR_*`；判停离线替身 / 读心规则档） | PASS |
| `test_dual_mic_ui_contract.py` | **双席真麦前端接线契约**：模式选项 `dualmic`、per-seat 设备下拉/开关、`enumerateDevices`+`getUserMedia(deviceId)`、WS `/api/session/dual/mic`、复用 `dualApply` | 7 组 |
| `test_mind_asr_ws.py` | **读心真麦通道离线契约**：`/api/mind/asr` ready→asr(partial)→asr(final)→stopped、不建 TurnSession（不碰判停主链）、字走 `/api/mind/tick` 进读心、`mind.html` 真麦接线、**页面引擎三选一目录与按 `model` 路由（未知 id 400、不偷偷换档）** | N/N |
| `poc_mind_asr_live.py` | **读心真麦真链**：`say` 合成语音 → 真火山豆包流式 ASR（partial 逐字 + final）→ 字进读心（需 `CLOUD_ASR_*`） | PASS |
| `test_task_entity.py` | **任务实体（P2-1/P2-2）**：运行中任务集合 / 已完成不进相关候选 / 先来后到队列位、相关性打分接口位与**行为接线**（不接真调用时零调用、命中并进在跑任务、都不相关新开 FIFO、已完结任务给的命中不认、merge 兜底与决策失败不误结原任务）、任务层 `task_eq` 与 A6 `iu_eq` 共存（不连 8009） | N/N |
| `test_hub_ack.py` | **期一·应一声离线验收**（不连 8009 / LLM / TTS）：应声这一问的请求形状与固定稿、`decide_ack` 每道闸逐条、**同一稿带/不带应声的 A/B**（说完当刻 / 0.7s / 1.5s / 3s 四档收住时刻与开闸结论同毫秒、台面无应声）、「不要回答」版不出声、判断缺失 / 失败 / 超时不出声、回声丢弃不改计时、两席各算各的、每版多一次判断调用 | 30/30 |
| `test_hub_pace.py` | **期二·节奏 + 垫句离线验收**（不连 8009 / LLM / TTS）：节奏这一问的请求形状（四档）与「开场 1–2 轮先稳住、之后按概率平滑换档」、垫句 `decide_cushion` 每道闸逐条（只有积极 + 已连续说满 1.5 秒才问）、**四档各跑一段**（其余三档根本不问垫句：`cushion_calls=0`）、**同一稿「垫句开 vs 关」A/B**（四档各自的收住时刻逐毫秒一致、开闸结论一致、判停调用数一致）、垫句优先于应声与「出声后 1 秒内不再出声」、垫句进上下文（`hub_said`）不进 Agent（不选工具不决策不开闸）、节奏/垫句判断缺失 · 失败 · 超时一律不垫且保持上一拍、调用次数（每版 4 路，积极 + 满 1.5 秒那版 +1） | 35/35 |
| `test_hub_floor.py` | **期三·两人同时说四选离线验收**（不连 8009 / LLM / TTS）：四选这一问的请求形状（四条出口 · 不设起判线）、`decide_floor` 每条出口与「没结论 → 继续说」兜底、**同一段稿只换结论四条出口各跑一段**（让路 / 把这句说完再接 / 继续说 / 垫句抢回）、**让路与改动前 A/B 逐字段一致**（`floor_enabled=False` vs 判「让路」，去掉本期新增事件与 `floor_*` 字段后事件流完全相同，`turn_void` 时刻与字段不变）、说完再接**不丢字 · 顺序不乱 · 随后真的被回答**（两段插话先来后到）、继续说只进旁听台账（「不是对车说」同一口径）不进 Agent、垫句抢回紧随当前这句且不进 Agent、四选还在飞时当前这句先不落地、超时 / 失败 → 继续说、**收住三档（0.7 / 1.5 / 3 秒）与开闸结论不被带偏**、调用次数（每版 4 路，只在人又开口的那一回 +1；同一段插话多个 partial 只问一次）、VAD 先开口也走同一道、**「正在说」的两个入口**（开闸后正在准备说 / 正在说一句垫句——垫句那一小段让路＝停下垫句、垫句抢回不叠第二句、**应声不算**）、台面两席各一行 | 28/28 |
| `test_hub_quiet.py` | **期四·冷场三选离线验收**（不连 8009 / LLM / TTS）：三选这一问的请求形状（三条出口 · 不设起判线）；`decide_quiet` 每条出口与「没结论 → 再等」兜底、**首问约 4s / 问一句约 10s / 之后每 8s** 的刻度闸；**同一段安静三条出口各跑一段**（再等不出声 / 主动接一句 / 问一句只能在 10s 后）；**一个刻度只问一次 · 不连着问**（首问只一拍的 `quiet_asked`）、两次开口退避（≥5s）且同一句不重复；**有人开口立刻清零**（出字 / VAD / 判断还在飞三种时点都不出声）；**只接上文**（台词带「刚才那句」的短片段，不出现新话题 / 新任务，不选工具不决策不开闸）；冷场开口记住（`hub_said`）但不进 Agent（同一段稿「开 vs 关」工具 / 决策条数一致）；判断缺失 / 失败 / 超时一律不出声；**收住三档与开闸结论不被带偏**（`quiet_enabled=False` vs 判「再等」，去掉冷场事件与 `quiet_*` 字段后事件流逐字段相同）；调用次数（不是每版多一路，只在安静刻度那一拍 +1） | 26/26 |
| `test_hub_arbiter.py` | **期五·四处并存的口径收口离线验收**（不连 8009 / LLM / TTS）：口径表四处（垫句 / 应声 / 两人同时说 / 冷场）何时问 · 谁优先 · 出声之后去哪；**同一时刻最多一句 + 出声后 1 秒内不再出声**（四处共用一个静默闸）；**垫句优先于应声**（同一刻两者都该出 → 只垫句，台面给出「垫句优先」）；**有人在说走应声 / 垫句、没人说话走冷场**（两边触发互斥；冷场将到点人又开口 → 清零、不出声）；**两人同时说只在两个入口**（正在准备说 / 正在说一句垫句；应一声不算）；**垫句抢回刚出声 1 秒内不许再应声**；**离线替身照实标注**（不冒充 8009）；**收口 / 开闸 / 决策 A/B 逐字段一致**；期五不新增判断（gate 计数键与期四一致） | 18/18 |
| `probe_hub_multi_question.py` | **多问探针**（需 8009）：同一版字上一次请求带两问 / 三问 / **五问（期二：判停 + 要不要回答 + 应一声 + 节奏 + 垫句）**（`choice` + `noul` 混类型）能否把每题都答回来，决定要不要退化成分开调用；8009 不可达时退出码 2 并说明「未验」（期一 / 期二隧道都不在 → 未跑） | 打表 / 未跑 |

注：`a3.py` 的 A3-1「无浪费 LLM」期望已因工具集更换改写为「浪费只来自 M4 shortlist 重发（≤1）」，
原因写在脚本注释里（core_score 两阶段短名单比旧的 14 工具慢，`allow_stale_shortlist` 会先按旧短名单发起决策）。

## 探针 / 历史实验

| 脚本 | 用途 |
|---|---|
| `poc-streaming-a1-baseline.py` / `-compare.py` | 改动前后核心链路 diff（证明 KEV 阶段一致、LLM 非确定性） |
| `poc-streaming-kev-isolation-probe.py` | KEV 并发/隔离对延迟影响 |
| `poc-streaming-diag.py` | 单回合事件诊断 |
| `poc-streaming-session-smoke.py` | TurnSession 最小冒烟 |
| `probes/*.sh` | M2/M3 调参扫描驱动（历史上写入 tmp，现已改为写 `docs/`） |
