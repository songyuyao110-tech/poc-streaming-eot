# poc-streaming-eot · 项目全景记录

| 项 | 内容 |
|---|---|
| 项目 | `poc-streaming-eot`（Agent POC：流式 ASR → KEV 判停/选工具 → 一次 DeepSeek 决策 → mock 执行） |
| 文档日期 | 2026-10-20 |
| 代码基线 | 提交 `35202f2`；其上另有**未提交改动**（`src/confirm_gate.py`/`confirm_orchestrator.py` 新增、`turn_session.py`/`ui/*` 接入二次确认） |
| 状态 | 核心链路端到端跑通；判停覆盖率已修复；判停**质量**是首要瓶颈；二次确认为有条件 PASS 的分支功能 |
| 一句话 | **把"用户说完 → 机器才开始想"的半双工回合制，改成"用户还在说 → 判停、选工具、甚至决策都已经跑过一遍"的准全双工体验**，并把这个提前量付出的代价全部量化。 |
| 读者 | 产品/PM 读第一部分；开发/测试读第二部分；附录用于速查 |

> **本文是整个项目的完整记录**：主线是"三段式流水线 + 投机/并行/提前预选 → 接近全双工的体验"。
> 对话式二次确认只是主线上的**一个分支功能**（§1.8 / §2.9.5），专题细节见 [`PROJECT-CONFIRMATION-POC.md`](./PROJECT-CONFIRMATION-POC.md)。
>
> 事实来源：`src/*.py`（当前代码）+ `docs/`（输入 spec、设计稿、进度、最终报告、调参记录）。数字都标注了 **[实测]** / **[假设]**，与 `docs/plan-20261015.md` 的约定一致。

---

# 第一部分 · 产品逻辑

## 1.1 一句话与核心命题

**一句话**：用户还没说完，机器就已经判完"你说完没"、选好了"大概要调哪个工具"，甚至把决策跑完了一半；用户最后一个字落地时，只剩最后一段路。

**核心命题**（这条链就是项目的全部）：

```
流式 ASR 实时更新  →  KEV 判停（EOT：说完 / 没说完 / 要我等）
                   →  KEV 两阶段选工具（domain → tool，242 工具 / 17 域）
                   →  一次 DeepSeek 决策（tool_call / ask_user / finish）
                   →  mock 执行
```

**为什么这件事值得做**：语音助手现在的体感是"半双工"——用户必须把话说完、还必须停顿够久（机器才敢确认你说完了），然后才开始理解、选工具、调模型、再执行。三段串起来就是 2–4 秒。用户感知到的不是"某个模块慢"，而是**"它要愣一下才理我"**。

**项目做的所有工程努力，都在做同一件事**：把这三段从"用户说完之后串行执行"，改成"用户还在说的时候并行/提前执行"。做到之后，交互就从"我说完—它想一下—它回应"变成"我说着—它已经在准备—我话音刚落它就接上"，也就是**接近全双工**。

---

## 1.2 半双工的三个卡点

| # | 卡点 | 现状做法 | 用户体感 |
|---|---|---|---|
| 1 | **判"说完"只能靠固定静音阈值** | 上游 `VADConfig.end_silence_ms=550`；PoC 兜底 `max_silence_ms=2000` | 阈值短 → 抢话（用户只是换气）；阈值长 → 慢半拍、甚至要用户"喂"一声 |
| 2 | **判停之后才开始选工具** | 两阶段 KEV：先域、再域内工具。旧 200 工具/20 域时 [实测] p50 684ms（阶段1 430 + 阶段2 256）；现 core_score 242/17 **未单独测两阶段总时延** | 又是一段"愣住"，且这段用户完全无感、纯等待 |
| 3 | **决策是一次同步大模型调用** | `deepseek-v4.1-flash`（公司 LiteLLM），非流式 [实测] 1.4–2.2s（单次最高 2.9s） | 最长的一段，且只能等 |

关键认识：**总延迟不是一个大数字，而是三段串起来的结果**。所以优化不能只压某一个模块，而要把后两段整体"往前挪"。这就是三段式流水线 + 提前量的由来。

---

## 1.3 三段式流水线（项目骨架）

```
                     ┌────────────────── 用户还在说话的时间窗口 ──────────────────┐
 音频 ──► ① 流式 ASR ──► ② KEV 判停（每前缀并行）  ──┐
          （增量文本）     ② KEV 两阶段选工具（partial 上就选） ──┤
                                                          ▼
                                              ③ 一次 DeepSeek 决策 ──► mock 执行
```

| 段 | 输入 → 输出 | 关键实现 | 提前量手段 | 这一段的耗时（实测/估算） |
|---|---|---|---|---|
| **① 流式 ASR** | 16k 单声道 PCM → `(t_ms, text, final)` 增量文本 | `src/asr_stream.py`，火山豆包 v3 sauc 帧协议 | 边推边出字（麦克风 20ms/帧 640B；文件回放 100ms/块） | 握手 170ms；partial 间隔 ~500ms；final 在 last packet 后 ~110ms |
| **② KEV 判停 + 选工具** | 当前段/后缀文本 → 三问概率；本轮文本 → 短名单 top-3 | `src/turn_eot.py`、`src/core_pipeline_run.py` | 判停每前缀都发（并发 3）；选工具在 partial 上就发；文本不变直接复用 | 判停墙钟 p50 121ms（单发）/263ms（全量含并发）；两阶段选工具 p50 数百 ms |
| **③ 一次决策** | 短名单 + 原文 → `tool_call`/`ask_user`/`finish` | `decide()`，`slow_turn(single_next_step=True)` | 投机（确认窗之前就发）；预热（首个 partial 就热身）；不等短名单 | 1.4–2.2s（PoC 非流式） |

**术语澄清**："三段式"指上表三段流水线；UI 里的**瀑布条也是三段**（判停 → LLM 启动 → 决策），那是同一套流水线在"单轮延迟"上的投影，不是另一套架构。

**这条流水线的最关键性质**：状态机（是否算说完）**只依赖②的判停支路**，②的选工具支路和③的决策都只是"备料"。所以判停一旦说"说完了"，料往往已经备好——这是能把延迟压下去的**结构前提**。

---

## 1.4 判停：把"说完没"做成三态，而不是一个阈值

### 1.4.1 三问（KEV `noul` 独立概率打分）

`src/turn_eot.py` 的 `EOT_LABELS` 是**原样复制** `pipecat-turn-eval-v2/kev_tune2.py` 的 `r2_en_utter_only` 配置：

| 标签 | KEV 问题（原文） | 产品语义 | 用户说了什么会这样 |
|---|---|---|---|
| `complete` | "The user's last utterance is a complete turn that can be answered now." | 说完了，可以回应 | "导航回家" |
| `cut` | "The user's last utterance stopped mid-sentence; they are still going to continue (e.g. ends on a conjunction, preposition, or unfinished list)." | 没说完，还要接着说 | "帮我导航到…"（停在这） |
| `wait` | "The user needs time to think or asks the assistant to wait a moment." | 让我想想 / 等我一下 | "等一下我想想" |

`score_eot(text)` 一次 KEV 调用返回三个 0–1 概率 + `label=argmax` + `margin = complete − max(cut, wait)` + 墙钟耗时。

### 1.4.2 为什么必须是三态（而不是"说完/没说完"二分类）

因为 **`cut` 和 `wait` 要求的动作完全相反**：

- `cut`（说一半）→ **再等一会儿**，但不要调 LLM（用户马上会接着说）；
- `wait`（要我想想）→ **别催、也别调 LLM**，而且这次"静音"是合法的长静音，不能被兜底逻辑抢跑。

如果只有二分类，这两种情况会被合并成"没说完"，于是"等一下我想想"要么被当噪音催问，要么被静音兜底强行确认并浪费一次决策。

### 1.4.3 margin：置信度不够就不许判"说完"

`label=argmax` 在真实数据上会在半句上给出 `complete`（例如 "我想去" 的 `margin` 只有 **+0.011**，`cut` 0.499 几乎持平）。所以默认策略是：

```
complete 且 (complete − max(cut,wait)) ≥ eot_margin(0.2)  →  真的算说完
否则                                                      →  降级成 cut/wait（继续听）
```

`eot_margin` 的产品含义：**越大越不容易误判说完（少打扰、少浪费决策），但会漏判、启动更晚**。这就是 §1.6 里"提前触发 vs 启动延迟"那根绳的一头。

### 1.4.4 判完之后还要等多久：四个计时器

| 计时器 | 默认 | 作用 | 产品语义 |
|---|---|---|---|
| `confirm_silence_ms` | **700** | 判 `complete` 后再确认 0.7s 文本没变才算数 | "给你一个改口的窗口" |
| `cut_wait_ms` | 1500 | 判 `cut` 后最多再等 1.5s，到点强制确认 | 不让会话卡死；必要时由决策去 `ask_user` |
| `think_wait_ms` | 3000 | 判 `wait` 后最多等 3s，**期间绝不调 LLM** | "你想，我不催" |
| `max_silence_ms` | 2000 | 任何状态静音到 2s 直接确认（KEV 挂了/超时也走这条） | 最后一道兜底 |

**静音从哪算起**：取「最后一次文本变化 / VAD `speech_end` / `speech_start` / 本轮开始 / **最近一次已消费决策**」中**最晚**的那个。最后一项是多段消费引入的关键修正（§1.7）。

**两条容易踩的坑（已写进代码注释）**：
- `confirm_silence_ms` 必须 < `max_silence_ms`，否则确认窗永远被兜底抢先；
- `THINK_WAIT` **只**用 `think_wait_ms`，不能被 `max_silence_ms` 抢（否则"等一下我想想"3s 的话会失败）。

---

## 1.5 工具选择：242 个工具怎么在几百毫秒内收敛到 3 个

### 1.5.1 两阶段：先域后工具

| 阶段 | 做什么 | 阈值 | 规模 |
|---|---|---|---|
| stage 1 | 对 **17 个域**各打一个分，取 ≥0.4 的域；若全不过则取 top-1（`domain_min1_fallback`） | 0.4 | 1 次 KEV 调用 |
| stage 2 | 对召回域内的**工具**逐个打分（每批 40，最多 2 批并发） | 0.5 | 1–N 次 KEV 调用 |
| 汇总 | 按分数取 **top-3**，装配 schema 短名单 | `top_k=3` | — |

- 工具集**只有一套**：`core_score`，**242 工具 / 17 域**（`fixtures/tools-core-score.yaml` + `fixtures/kev-domain-descriptions-core-score.json`）。域分布：vehicle_control 94、navigation 22、sound 21、seat 18、energy 14、media_video 13、media_audio 11、hvac 10、system 7、fragrance 6、media_short_video 6、alipay 5、camera 5、adas 3、media_radio 3、phone 3、weather 1。
- 工具/域数量**一律从 registry 快照读**，不硬编码（旧的 200/20 常量已解耦）。
- 为什么两阶段：242 个工具塞进一次打分，请求体积和耗时都不可接受；先域后工具把单次请求压到 ≤40 个工具，总延迟从"一次巨慢"变成"一次快 + 一次中"。

### 1.5.2 一次决策，只给 3 个工具

`decide()` 调一次 `slow_turn(..., single_next_step=True)`，模型看到的是**严格的选择型 prompt + 只披露 top-3 业务工具 + `ask_user`/`finish`**，必须返回**恰好一个**可解析的下一步：

| `kind` | 含义 | 产品表现 |
|---|---|---|
| `tool_call` | 调某个业务工具 | mock 执行（不真调车） |
| `ask_user` | 信息不足，追问 | 问一句 |
| `done` | 不支持/越权，明确拒绝 | 礼貌拒绝 |

返回后走 JSON Schema 校验（`Draft202012Validator`）；更严的实车参数引用校验对这套细粒度工具集**降级为 warn**（`argument_validation_warning`），因为那套规则是为旧工具集写的。KEV/LiteLLM 传输不可用时，会退化成"用 KEV 的 choice 问题选一个已披露工具"，但**只追问不执行**（`parameters_generated: false`）。

**为什么只给 top-3**：决策质量与选项数强负相关；同时 prompt 越短，网关侧越稳（设计稿把"缩短 prompt"列为低优先优化项——收益没测过）。

---

## 1.6 提前量：并行判停、提前预选、投机决策（+预热）

这是全项目最核心的产品设计。四个提前量 + 一个预热，各自的收益与代价都被实测过：

| 机制 | 提前到什么时候 | 收益 | 代价 | 开关 |
|---|---|---|---|---|
| **判停并行化** | 每个 ASR 前缀都发一次判停，最多 3 个在途 | 判停覆盖率 **3.3% → 95.0%**（中）、2.5% → 89.2%（英） | 判停调用 4.46 → 8.83 次/句；**97% 的结果落地即过期** | `eot_max_inflight=3` |
| **选工具预选** | partial 上就选；去掉 debounce；最终文本归一化后不变则**直接复用** | 把"等短名单 378ms"从关键路径上拿掉；零额外 LLM 成本 | 选工具调用 3.97–14.35 次/句（随韵律） | `reuse_tools_if_text_unchanged` |
| **不等短名单** | 短名单没就绪也先发 LLM（用本轮稍旧的短名单，`shortlist_source=stale`）；短名单变了再重发 | LLM 启动 p50 **849ms → 501ms（−41%）** | `llm_wasted` 15 → 5 次（重发条件收敛到"top-k 真变了"后）；`n_reissue` 上升 | `allow_stale_shortlist` / `shortlist_reissue` |
| **投机 LLM** | 进入 MAYBE_DONE 且 `margin ≥ spec_margin(0.3)` 就发，不等 700ms 确认窗 | 确认窗被隐藏；LLM 与选工具的尾巴重叠 | 用户继续说 → 作废并记 `llm_wasted`；浪费率随韵律升高（600ms 韵律中文 75.9%） | `speculative_llm=true` |
| **预热**（延迟优化） | 本轮第一次文本变化就发一个 1-token 请求 | 把 0.8–1.3s 的冷启动移出关键路径 | 一次极小调用（配额/计费未知，按可忽略处理） | `warm_llm=true` |

### 1.6.1 必须讲清的产品判断

**① "提前触发 LLM 率"和"启动延迟"是一根绳的两头。**

- 150ms 韵律、投机**开**：中文最后一字→LLM 启动 **755ms**，提前触发率 3.48%，浪费率 3.4%；
- 同样配置、投机**关** + 700ms 确认窗：提前触发率 **0%**，但中文启动 **1159ms**；
- 600ms 韵律（判停真正可用）、投机开：提前触发率 **100% / 70%**，浪费率 **75.9% / 47.4%**。

**在 600ms 韵律下"提前触发"是投机策略的正常工作方式，不是 bug。** 代价被如实记在浪费率里，产品需要接受的是"延迟换消耗"这条曲线，而不是要求两个指标同时最优。

**② 覆盖率 ≠ 可用率。** 150ms 韵律下，判停往返（~265ms）必然长于前缀间隔（150ms），所以"每个前缀都有分"但"每个前缀的分都过期"。**每句真正可用的判停结果仍约 1.2 条**——这是纯文本判停在流式韵律下的物理上限，也是 §1.9 里"判停应只用在停顿上"这条下一步的直接依据。

**③ 并发不是万能的。** 判停并发有效（去饥饿、提覆盖），但**选工具并发有害**——实测把工具调用从 5.55/句推到 10.75/句，p50 反而从 849ms 恶化到 1503ms，还挤占了同一台 KEV 的判停调用（13.35 → 7.45/句）。所以 `tools_max_inflight` 默认回到 **1**，而 `eot_max_inflight=3` 保留。

---

## 1.7 多段消费：从"一轮一次决策"到"一段一次决策"

### 1.7.1 场景

用户说：**"帮我导航回家…（停顿 3s）…顺便看看路况"**。

旧行为：第一段说完 → 决策 → **本轮收尾**；用户接着说 → 触发 `barge_in` + 清空历史，第二段变成全新一轮（第一段的上下文丢了）。

目标行为：第一段说完 → 决策 → **只把这段封存为"已消费"**，继续听；第二段只对**未消费后缀**判停、选工具、决策。

### 1.7.2 消费游标的语义

| 概念 | 定义 |
|---|---|
| 边界 | **那次"落地了的决策"所依据的 ASR 文本版本**（`job["version"] == self.version`，代码保证它和当前文本逐字一致） |
| 落点 | 该版本覆盖到的段被**封段**，此后任何 partial 都改写不到它 |
| 未消费后缀 | `segments[consumed_segments:] + 当前在用段`，判停入口、选工具、`decide()` 输入**全部只吃后缀** |
| 收尾 | 后缀为空 + 静音 ≥ `max_silence_ms`（或显式 `stop()`）→ `turn_done` 一次 |
| 不变式 | **`llm_calls == consumed_count + llm_wasted`**：每一次 LLM 调用要么落地成一个已消费段，要么被记成浪费 |

### 1.7.3 为什么这是"接近全双工"的关键演进

因为它拆掉的不是延迟，而是**"用户必须等机器做完才能说下一句"这条硬约束**。用户说完一个意图，机器就开始做，用户接着说下一个意图——两个意图在同一轮里被连续消费，中间不打断、不清历史、不重新确认。

### 1.7.4 产品代价（必须承认）

| 限制 | 现状 |
|---|---|
| 段间无上下文 | 决策 #2 只拿到未消费后缀，拿不到决策 #1 的文本和结果（PoC 明确不做） |
| 动作不可回滚 | 已消费段的动作已 mock 执行，后续"纠正"不撤销 |
| 停顿窗口窄 | 消费 #1 落地前不能来第二段文本，且第二段必须在「消费 #1 + `max_silence_ms`」内到达。实测 **3.0s 停顿通过、5.0s 会收尾** |
| ASR 重发 | 火山对同一 utterance 会重发整句：完全重发 → `consumed_duplicate` 忽略；前缀重叠 → 只取增量（按归一字数对齐，不要求逐字节相同） |

---

## 1.8 分支功能：高危工具的对话式二次确认（局部）

> 这一节只讲它在整个项目里的位置。完整产品逻辑、确认池依据、四分支流程、超时策略见 [`PROJECT-CONFIRMATION-POC.md`](./PROJECT-CONFIRMATION-POC.md)。

**叠加位置**：挂在"第一轮决策落地之后、mock 执行之前"，不改主循环的分段/消费机制。

**产品规则 vs 模型分工**（这是它最重要的设计判断）：

- **问不问** = 确定性产品规则：只有 **8 个池内工具**（关 ESP、开引擎盖、开防滑链、制动盘保养、切驾驶模式、关 ADAS、解锁充电枪、停止充电）才确认；池外 234 个工具**永不确认**。
- **答的是什么** = KEV 三问：`agree` / `reject` / `takeover`，按优先级裁决（takeover ≥ 0.5 直接接管；正负差 ≥ 0.15 才判同意/拒绝；同意还要求 `p_agree ≥ 0.75` 的绝对分护栏）。
- **含糊即拒**：`unknown` 一律按放弃处理，绝不默认执行。

**四分支落地**：同意 → mock 执行；拒绝 → 放弃；接管（takeover）→ 放弃当前动作，把第二轮回当新诉求重跑选工具+决策；含糊/超时 → 安全放弃。

**当前状态（有条件 PASS）**：mock 链路全绿（L2 31/31、端到端 5 路径）；**真实 KEV 判中率 25/31 = 81%，低于设计门槛 85%**；两处护栏泄漏已记录（`行吧` 0.946、`就那个吧` 0.867 被判同意 → 会执行高危动作），属发布前必修。

---

## 1.9 结论：验证了什么，没验证什么

| 能力 | 结论 | 关键数字 |
|---|---|---|
| 核心链路端到端 | ✅ 跑通（脚本回放 + 真实麦克风） | 每段恰好 1 次有效决策；投机被推翻正确取消 |
| 判停覆盖率（是否"判得到"） | ✅ 已修复 | 3.3% / 2.5% → **95.0% / 89.2%**（150ms 韵律，398 句） |
| 判停**质量**（是否"判得准"） | ❌ 首要瓶颈 | 前缀误判说完率 中文 **62.1%** / 英文 **23.8%**（按句 97.4% / 44.6%） |
| 整句判停（非流式回归） | ✅ 保持 | 留出集二分类 **94.6%**（53/56），全集 93.5% |
| 延迟：最后一字 → LLM 启动 | ⚠️ 一半达标 | 中文 **755ms**（达标）/ 英文 **1501ms**（未达 800ms，按产品决定接受） |
| 延迟：最后一字 → 决策 | 记录 | 中文 2142ms / 英文 2889ms（中位；**评测里决策用 1380ms mock**，所以这行 ≈ 编排时延 + 1.38s，不是真实网关时延） |
| 真实决策时延 | 记录 | 1.4–2.2s（PoC 非流式；实测单次 1.6–2.9s） |
| 单轮决策"选得对不对" | ⚠️ 未做大样本 | B1 的 94.6% 是**判停二分类**准确率，不是决策正确率；A3/A4/A5 验的是"每段恰好 1 次、投机正确作废"这类功能正确性 |
| 多段连续消费 | ✅ | C1–C4 36/36；不变式 `llm_calls == consumed + wasted` 全成立 |
| 高危工具二次确认 | ⚠️ 有条件 | mock 全绿；真实 KEV 81% < 85%，2 处护栏泄漏 |
| **明确没做** | — | TTS、真实工具执行、多会话并发/重连/鉴权、真车部署、音频/韵律判停模型、真人麦克风多段验收 |

**一句话结论**：**延迟这条路已经探到纯文本方案的边界**——不是编排不动了，而是判停模型质量不够。下一步该上音频/韵律判停（Smart Turn 类）或中文专用判停 prompt/模型，而不是继续在编排层抠毫秒。

---

# 第二部分 · 开发 spec

## 2.1 代码地图

| 文件 | 行数 | 职责 |
|---|---|---|
| `src/bootstrap.py` | 45 | 把上游仓库根注入 `sys.path`（`model_clients`/`registry`/`decisions`/语料 fixture 都不复制） |
| `src/asr_stream.py` | 250 | 火山豆包 v3 sauc 帧协议；`ASRStream`（推流：`push/poll/finish/close`）+ `stream_asr()`（wav 回放） |
| `src/turn_eot.py` | 45 | `EOT_LABELS`、`score_eot(text) -> {complete,cut,wait,label,margin,wall_ms,kev_ms}` |
| `src/core_pipeline_run.py` | 449 | `kev_call`/`probability`、`score_tools`（两阶段）、`select_tools`、`decide`、`run`（单句）、`run_corpus` |
| `src/turn_session.py` | 854 | `TurnConfig`、`TurnSession`：五态状态机 + 消费游标 + 并发/版本/去抖/投机/预热 + 事件与 timeline |
| `src/confirm_gate.py` | 218 | 确认池、确认文案、确认三问打分（`score_confirm_reply`）、裁决（`judge_confirm_reply`） |
| `src/confirm_orchestrator.py` | 120 | `TurnConfirmGate`：把上述纯函数接成 TurnSession 可调用的 `ready/ask_for/resolve` |
| `src/build_prefix_cases.py` | 170 | 生成前缀用例（398 句 / 3307 前缀；`natural_complete` 用 DeepSeek 预标） |
| `src/streaming_prefix_eval.py` | 404 | 前缀回放评测（真 KEV + mock 决策 1380ms），产出 report/detail.jsonl/detail.csv/summary.json |
| `ui/server.py` | 423 | 5 个路由（`/api/run`、`/api/run/stream` SSE、`/api/session/stream` SSE、`/api/session/mic` WS、`/api/session/mic/save`） |
| `ui/index.html` | — | 4 种模式 + 判停卡片流时间线 + 指标条/瀑布条 + 用例保存 |
| `fixtures/tools-core-score.yaml` | — | 唯一工具集：242 tools / 17 domains，`binding: mock` |
| `fixtures/kev-domain-descriptions-core-score.json` | — | 17 个域的描述（一阶段 state 用） |

## 2.2 运行形态与依赖

| 依赖 | 说明 |
|---|---|
| **上游仓库** | `model_clients.py` / `registry.py` / `decisions.py` / 语料 fixture 来自 `navi-dual-system`，**不复制**。`POC_UPSTREAM` 指定，默认探测 `/Users/YUYSONG/Agents/navi-dual-system` |
| **Python** | `/Users/YUYSONG/Agents/navi-dual-system/.venv/bin/python`（含 `httpx2`、`simple-websocket`、`jsonschema`、`flask`） |
| **KEV 双隧道** | `:8009` 选工具、`:8010` 判停（**两条独立 SSH 隧道指向同一远程 KEV** `kev-latest @ 106.53.0.235:8009`），`KEV_EOT_BASE` 可覆盖 |
| **ASR 凭证** | 火山 `CLOUD_ASR_APP_ID / ACCESS_TOKEN / RESOURCE_ID / URL`，运行时从环境或 `~/.zshrc` 读，**不打印、不入库** |
| **LLM 凭证** | 公司 LiteLLM，走上游 `model_clients`，经 `~/.dsh/.credentials.yaml` 取，**不打印** |

**为什么判停要独立隧道**：实测两条 KEV 调用走同一隧道时会互相排队（真实会话里判停自报墙钟 340–556ms，超 `eot_timeout_ms`，大量结果变 stale）。但**单轮微基准测不出**（同隧道 p50 115ms vs 分隧道 121ms）——因为持续负载下的争用来自"一轮要等一次选工具往返 + GIL"，远程 KEV 服务自身才是共享瓶颈。所以隧道隔离是缓解措施，不是根治。

## 2.3 数据契约

### 2.3.1 `TurnSession` 公开 API

```python
TurnSession(cfg: TurnConfig, on_event, *, clock=None,
            eot_fn=score_eot, select_tools_fn=select_tools, decide_fn=decide,
            session_id=None, evidence_root=EVIDENCE_ROOT, warm_fn=None, confirm_gate=None)

on_asr(t_ms: int, text: str, final: bool) -> None    # 每次 ASR 更新
on_vad(t_ms: int, kind: str) -> None                 # kind: speech_start | speech_end
tick(now_ms: int) -> None                            # 驱动计时器与收结果（所有状态变更只在这里）
stop(now_ms: int) -> None                            # 显式收尾：先决策剩余后缀，再结束本轮
idle() -> bool                                       # 无在途/无 pending
summary() -> dict                                    # 汇总（见 2.7.3）
close() -> None                                      # 关线程池
```

所有状态迁移都发生在**调用 `tick()` 的那条线程**上；注入的 KEV/LLM 可调用函数跑在线程池里，结果只在 `text_version` 仍是最新时生效，否则按 stale 丢弃（这是整个并发设计的**唯一同步约束**）。

### 2.3.2 `TurnConfig`（18 字段，默认值 = 实测推荐值）

| 字段 | 默认 | 语义 / 约束 |
|---|---|---|
| `eot_policy` | `"margin"` | `argmax` 取最大 / `margin` 要求 complete 领先 max(cut,wait) 超过 `eot_margin` |
| `eot_margin` | `0.2` | margin 门槛；越大越不容易误判说完，但会漏判、启动更晚 |
| `spec_margin` | `0.3` | 进入 MAYBE_DONE 且 margin ≥ 此值才投机调 LLM（0 = 只要可能说完就投机） |
| `speculative_llm` | `True` | 确认前就投机调决策，被推翻则作废并记 `llm_wasted` |
| `eot_input` | `"last_segment"` | 判停送什么文本：`last_segment`（<4 字拼上一段）/ `turn_text`（整轮） |
| `confirm_silence_ms` | `700` | 判 complete 后连续静音多久才确认；**必须 < `max_silence_ms`** |
| `cut_wait_ms` | `1500` | 判 cut 后再等多久强制确认 |
| `think_wait_ms` | `3000` | 判 wait 时最多等多久；**`THINK_WAIT` 不用 `max_silence` 兜底** |
| `max_silence_ms` | `2000` | 任何状态静音到此即确认（KEV 失败/超时也走这条） |
| `eot_timeout_ms` | `900` | 单次判停超过此值算超时（只放弃该次判定，晚到结果仍上报并标 stale） |
| `eot_max_inflight` | `3` | 同时最多几个判停在途；`>1` 才不会被 single-flight 饿死 |
| `debounce_ms` | `80` | 文本变化后等多久才发判停；**必须 < ASR partial 间隔**，否则一次判停都发不出去 |
| `reuse_tools_if_text_unchanged` | `True` | 最终文本归一化后与已评分相同则复用短名单 |
| `tools_max_inflight` | `1` | 选工具在途数；**实测并发有害，保持 1**（工具去 debounce 保留） |
| `allow_stale_shortlist` | `True` | 允许用本轮稍旧的短名单先发决策 |
| `shortlist_reissue` | `True` | 短名单**入选 top-k 变了**就作废重发（看文本变化会浪费 3 倍） |
| `warm_llm` | `True` | 本轮第一次文本变化发一次 1-token 预热 |
| `tool_set` | `"core_score"` | **唯一合法值**，`__post_init__` 强校验，其它值直接抛 |

### 2.3.3 `select_tools()` 返回

```python
{"scores": {"tool:car_navi_to_destination": 0.87, ...},
 "selected": ["car_navi_to_destination", ...],        # top_k=3，阈值 0.5
 "schemas": [...],                                    # 披露给决策的 schema
 "native_schemas": [...],                             # 原始 schema（参数校验用）
 "domains": [...],
 "timing": {"stage1_ms":..., "stage2_ms":..., "total_ms":...},
 "log_fields": {...}}                                 # 阶段耗时、域分数、召回域、batch 明细
```

`decide()` 返回 `{"decision": {"kind": "tool_call|ask_user|done", "tool":..., "arguments":...}, "llm_ms":..., "log_fields":...}`。

### 2.3.4 ASR 更新

`ASRStream` 产出 `(text, final, end_ms)`；`ui/server.py` 补 `t_ms` 后成为 `{t_ms, text, final, vad_end_ms}`。

`end_ms` 来自 `result.utterances[].end_time`（或 `result.end_time`，取最大）——这是火山给出的"这一句语音在哪结束"，**只用于展示**（标出音频 VAD 结束点、算 `VAD 结束 → 首工具` 感知时延），**不进状态机**。

协议要点（踩过的坑都在这）：握手占序号 1、音频从 2 开始；`definite` 在 `result.utterances[0]` 里而不是 `result.is_final`；last packet 只能发一次（`finish()` 独占，否则偶发 `45000000 last packet has been received already`）。

## 2.4 状态机

### 2.4.1 五个状态（+1 个确认状态）

| 状态 | 含义 | 出口 |
|---|---|---|
| `LISTENING` | 在听；后缀可能为空 | 判停结果进入 → MAYBE_DONE / CUT_OFF_WAIT / THINK_WAIT |
| `MAYBE_DONE` | 判"说完了"，等确认窗 | 静音 ≥ `confirm_silence_ms` → CONFIRMED；新文本/speech_start → LISTENING |
| `CUT_OFF_WAIT` | 判"说一半"，再等一会儿 | 静音 ≥ `cut_wait_ms`（或 `max_silence`）→ CONFIRMED |
| `THINK_WAIT` | 判"让我想想" | 静音 ≥ `think_wait_ms` → CONFIRMED（期间不调 LLM） |
| `CONFIRMED` | 已确认；这是**中转态**不是终态 | 决策落地 → 消费一段 → 回 LISTENING |
| `CONFIRM_WAIT` | 二次确认专用：第一轮决策命中池内工具，已挂起等二轮回 | 二轮回判停收敛 → 意图判定 → 四分支 |

### 2.4.2 迁移与守卫（实现口径）

```text
# tick() 的分支优先级
THINK_WAIT      : silence >= think_wait_ms  -> _confirm("think_wait")      # 不用 max_silence
MAYBE_DONE      : 后缀空 且 silence >= confirm_silence -> _maybe_finish_session()
                  后缀非空 且 silence >= confirm_silence -> _confirm("confirm_silence")
                  后缀非空 且 silence >= max_silence     -> _confirm("max_silence")
CUT_OFF_WAIT    : silence >= cut_wait_ms -> _confirm("cut_wait") / >= max_silence -> _confirm
CONFIRM_WAIT    : silence >= think_wait_ms -> abandon（安全优先，绝不静默执行）
LISTENING       : silence >= max_silence -> 后缀非空? _confirm("max_silence") : _maybe_finish_session()
```

三条必须记住的守卫：

1. **`_confirm()` 空后缀直接 return** —— 永远不对空文本调 LLM；
2. **`_maybe_start_llm()` 空后缀直接 return**，并且 `MAYBE_DONE` 且非投机时不发；
3. **迟到的判停如果后缀已被消费完，只上报不驱动状态机**（`eot_skipped(reason="empty_suffix")`）——否则会把本轮停在"空后缀的 MAYBE_DONE"里永远出不来。

### 2.4.3 反转与取消

| 触发 | 行为 |
|---|---|
| 新文本（版本前进） | 在途/已完成的决策统一作废：`llm_cancel(reason=new_text)` + `llm_wasted++`；回 LISTENING 重判 |
| `speech_start` | 同上，`reason=speech_start`；并重置静音锚点（否则会被 `max_silence` 立刻确认） |
| 短名单入选 top-k 变化 | `llm_cancel(reason=shortlist_changed)` + `n_reissue++`，用新短名单重发 |
| 本轮已 `turn_done` 后又说 | `barge_in` + `_reset_turn()` 开新轮（唯一的"清历史"入口） |
| 显式 `stop()` | 先决策剩余后缀，落地后 `turn_done`；后缀为空则直接收尾 |

"取消"的实现口径：**放弃该线程结果，token 照常消耗**，不改模型客户端（PoC 明确不做真正的请求中断）。

## 2.5 并发、版本与去抖

| 机制 | 实现 | 为什么 |
|---|---|---|
| `text_version` | 每次"归一化后缀真的变了"才 +1；所有异步结果按版本校验 | 避免旧判定/旧决策污染新文本 |
| stale 上报 | 判停结果**无论是否过期都发 `eot_scores`**（带 `stale` 标记） | 覆盖率与误判率指标必须基于全量，否则只有"能落地的"样本 → 幸存者偏差（改前判停覆盖率 3.3% 就是这么被掩盖的） |
| `debounce_ms` | 只作用于判停（`due_ms = t_ms + debounce_ms`）；选工具**立即发** | 判停不能按每个字符发；短名单要抢提前量 |
| inflight 模型 | 判停/选工具各是一个列表 + 上限；pending 只保留最新一条 | 单飞会导致"在途的永远是为旧文本发的" → 饿死 |
| 连接池 | `KEV_TOOLS_CLIENT` 2 连接；`KEV_EOT_CLIENT` 4 连接；都 `trust_env=False` | 判停不能被选工具排队 |
| 预热 | 本轮第一次 `changed=True` 时异步提交一次 `warm_slow_model()`，`warm_ms` 记入 summary | 网关冷启动 0.8–1.3s，必须移到说话期间 |
| 线程模型（mic） | 收帧线程 → ASR 线程 → **驱动线程（唯一改状态）** → 发送线程（唯一写 socket） | 状态机的所有竞态都被收敛成"单写者 + 版本校验" |

## 2.6 消费游标（多段连续决策的实现）

### 2.6.1 游标三元组 + 派生量

```python
consumed_version        # 落地决策发出时的 job["version"]
consumed_segments       # 已封段数；segments[consumed_segments:] 是未消费的已封段
consumed_text           # 归一化后的"整段已消费文本"（去重基准，不是最后一个段！）
consumed_tail           # 最近一个已消费段的原始文本（兼容/指标用）
consumptions[]          # 每条：index/version/segments/text/t_ms/asr_t_ms/speculative/llm_ms/decision_kind/shortlist_source
consumed_count          # == len(consumptions)
version_t_ms            # 当前版本对应的 ASR 更新时间（证据用）
t_last_consumed         # 静音锚点的第五项（关键修正）
```

### 2.6.2 封段时机（唯一正确时机 = 决策落地）

```python
def _apply_llm(self, now_ms, result, job):
    consumed_text = self.suffix_text()   # == 这次 decide 的输入（版本已校验，逐字一致）
    self._seal_consumed()                # 把在用段封进 segments，之后 partial 改不到它
    self.consumed_version = job["version"]; self.t_last_consumed = now_ms
    self._emit("llm_decision", ...); self._emit("segment_consumed", ...)
    self._to_state("LISTENING", now_ms, "consumed")   # 不收尾，继续听后缀
```

**绝对不要在 `_start_llm()` 时封段**：决策发出后仍可能被作废，而 ASR 的 partial 是"整句重写"而非增量，提前封段会把 `导航回` + `回家` 拼成 `导航回回家`。

### 2.6.3 已消费段的去重（mic 核心路径）

火山对同一 utterance 会重发整句，所以：

```
normalize(text) == consumed_text                        → eot_skipped(consumed_duplicate)，忽略
normalize(text).startswith(consumed_text)               → 只取增量作为新段（按归一字数对齐）
否则                                                     → 正常新文本
```

`_tail_after_consumed()` 逐字符走原文、只数"归一动过"的字符，数够 `len(consumed_text)` 就切——因为重发可能**重新加标点**，不能逐字节比较。

### 2.6.4 收尾

| 入口 | 条件 |
|---|---|
| 自动 | 后缀为空 + 无在途决策 + 静音（锚点含 `t_last_consumed`）≥ `max_silence_ms` + `consumed_count ≥ 1` |
| 显式 | `stop(now_ms)`：后缀非空先 `_confirm("stop")`，决策落地后强制收尾；后缀为空直接收尾 |
| 不收尾 | `consumed_count == 0` 且后缀为空 → 保持 `LISTENING` 等用户开口（不对空文本决策） |

## 2.7 事件与证据

### 2.7.1 事件表（SSE 与 `timeline.json` 同一格式，每条带 `t_ms` + `text_version`）

| 事件 | 关键字段 | 说明 |
|---|---|---|
| `asr_update` | `text, final, segment, turn_text, changed` | 每次 ASR 更新；`changed=False` 表示归一化后没变 |
| `vad` | `kind` | `speech_start` / `speech_end` |
| `eot_start` | `text, version` | 判停发出 |
| `eot_scores` | `stale, text, complete, cut, wait, label, margin, wall_ms, kev_ms` | 判停结果；**stale 也上报** |
| `eot_skipped` | `reason` ∈ `unchanged` / `timeout` / `stale` / `empty_suffix` / `consumed_duplicate` / `unchanged_after_turn_done` | 跳过原因 |
| `tools_start` | `text, version` | 选工具发出 |
| `tools_scores` | `reused, text, version, newest, selected, timing, top` | 短名单结果 + top-3 |
| `state_change` | `from, to, why` | 每次状态迁移 |
| `llm_start` | `speculative, selected, shortlist_source, shortlist_age_ms, utterance, consumed_segments` | 决策发出（`t_llm_start` 只记**第一次**） |
| `llm_cancel` | `reason` ∈ `new_text` / `speech_start` / `shortlist_changed` / `stale_text` / `confirm_wait` / `confirm_timeout`, `speculative, prepared` | 作废 |
| `llm_decision` | `decision, llm_ms, utterance, consumed_index, consumed_version, consumed_segments, decision_head` | 决策落地 |
| `segment_consumed` | `index, version, segments, text, asr_t_ms, speculative, llm_ms, decision_kind, shortlist_source` | 游标推进（多段消费的核心埋点） |
| `barge_in` | `why, text` | 动作后又说 |
| `stop_requested` | `text, consumed_segments` | 显式停止 |
| `turn_done` | 全量 `summary` + `timeline_path` | 本轮结束 |
| `error` | `stage` ∈ `eot`/`tools`/`llm`/`takeover`, `error_type, error` | PoC 口径：报出来，不重试（UI 侧另有 `asr`/`socket`/`config`/`save` 阶段） |
| `confirm_request` / `confirm_scores` / `confirm_resolve` / `confirm_exec` / `confirm_reroute` / `confirm_consumed` / `reroute_tools` / `reroute_decision` | — | 二次确认分支专用 |

### 2.7.2 证据落盘

`evidence/turn-session/<session_id>/timeline.json`：

```json
{"session_id": "...", "config": {...18 字段...}, "events": [...], "turns": [...], "summary": {...}}
```

评测证据：`evidence/streaming-prefix/<run_id>/{detail.jsonl, detail.csv, summary.json}`。

### 2.7.3 `summary()` 字段

`t_last_word / t_confirmed / t_llm_start / t_decision`（**首次**语义）+ `t_last_llm_start / t_last_decision`（末次）+ 派生 `t_confirm_latency_ms / t_llm_start_latency_ms / t_decision_latency_ms` + `t_first_partial / t_tools_ready / t_first_partial_to_last_word_ms` + `shortlist_source / shortlist_age_ms / warm_ms` + `consumed_count / consumptions / t_last_consumed` + counters `{eot_calls, eot_skipped, eot_stale, tool_calls, llm_calls, llm_wasted, n_reissue, consumed}`。

## 2.8 API 与 UI

### 2.8.1 五个路由（4 个入口 + 1 个用例保存）

| 接口 | 形态 | 请求 | 响应 |
|---|---|---|---|
| `POST /api/run` | JSON | `{utterance}` | `run()` 结果（单句链路） |
| `POST /api/run/stream` | SSE | `{utterance}` | `accepted → kev_start → kev_scores → llm_start → llm_decision → exec_start/exec_step/exec_done → done` |
| `POST /api/session/stream` | SSE | `{updates:[{t_ms,text,final}], vad:[{t_ms,kind}], config:{...}, tool_set}` 或 `{confirm_rounds:{round1,round2}}` | 逐条 `TurnSession` 事件，最后一条 `turn_done` |
| `WS /api/session/mic` | WS | 二进制=16k/单声道/16bit PCM（20ms/640B 一帧）；文本=`{"cmd":"config"\|"stop"\|"save"}` | `{"kind":"ready"\|"asr"\|"event"\|"saved"\|"stopping"\|"config"\|"error","data":…}` |
| `POST /api/session/mic/save` | JSON | `{session_id, label}` | 用例落盘路径（服务端登记最近 8 轮，socket 关了也能存） |

`/api/session/stream` 的 `confirm_rounds` 走**离线 mock 注入链**（不连真 KEV/LLM），会把两轮展开成一条 ASR 时间线并放宽等待窗（`think_wait_ms=20000`）。

### 2.8.2 UI 四种模式

| 模式 | 干什么 | 默认 |
|---|---|---|
| 单句（一次决策） | 一句话跑完整链路，看 14 个 KEV 分数 + 阶段时间线 | — |
| 流式模拟（KEV 判停） | 输入文本 + 语速 + `\|600\|` 停顿标记，生成 partial 序列走 `/api/session/stream` | — |
| 确认轮（对话式二次确认） | 填第一轮指令 + 二轮回（4 个预置分支：同意/拒绝/接管/含糊） | ✅ 当前默认 |
| 麦克风（真实流式 ASR） | `getUserMedia → AudioContext → ScriptProcessorNode → 重采样 16k → Int16 → 20ms 分帧 → WS` | — |

**时间线是"判停卡片流"**：**一张卡 = 一次 ASR 更新**。

- 卡头：`时间 / 文本 / vN / partial|final / 状态 / KEV 墙钟·自报`；
- 卡身左侧：`complete / cut / wait` 三条概率条 + 数值 + `label·margin` + 过期标记 + **margin 通过（绿）/未通过（无颜色）徽标**（阈值取当前 `eot_margin`，只展示不改判定）；
- 卡身右侧：工具 top-3 芯片 + **该次更新下发生的事件流**（各带时间戳）；
- 卡片左边框按判停结论着色（绿=complete / 橙=cut / 蓝=wait）；紫色标 `VAD 结束（VAD speech_end / ASR 断句 end_time）`；
- 顶部**指标条**：`最后一字 → 判停确认 +Xms → LLM 启动 +Yms → 决策 +Zms`、`VAD 结束 Xms → 首工具 +Yms`、判停/选工具/LLM 次数与浪费、证据路径，外加**三段配色的瀑布条**；
- 原始事件 JSON 收进 `<details>` 抽屉。

两个渲染口径上的坑（已修）：`vad` 不该开卡；事件要按"它评的那段文本"归卡（服务端事件的 `text_version` 是**发出时**的最新版本，不能用来归组）。

### 2.8.3 用例格式（`cases/*.jsonl`）

```json
{"id","created_at","sample_rate":16000,"frame_ms":20,"label_complete":true,"tool_set","config":{...},
 "asr_updates":[{"t_ms","text","final","vad_end_ms"}],"events":[...],"summary":{...},"decision":{...},
 "timeline_path":"..."}
```

## 2.9 评测与验收

### 2.9.1 脚本清单

| 脚本 | 验证 | 期望 |
|---|---|---|
| `tests/poc-streaming-c1c4.py` | 多段消费 C1–C4 + F1/F2（迟到 EOT 空后缀等） | 49/49 |
| `tests/poc-streaming-a3.py PORT` | 脚本回放 → 判停/状态迁移/四类用例 | 全 PASS（历史报告记 9/9；`tests/README.md` 记 10/10，断言随功能增补，以脚本输出为准） |
| `tests/poc-streaming-a4.py PORT [--with-reuse]` | 停顿分段、工具复用、barge-in | 8/8（+reuse 11/11） |
| `tests/poc-streaming-a5.py PORT` | 投机被推翻 → 作废与浪费记账 | 4/4 |
| `tests/poc-streaming-mic-ws.py` | 真 wav 按 20ms 推 WS → ASR → 判停 → 决策 → 落盘 | 6/6 |
| `tests/poc-streaming-m1b.py [wav]` | 文件流式 ASR 端到端 | PASS（2 partial + 1 final + 1 决策） |
| `tests/poc-streaming-s1s3-core-score.py` | S1–S3：core_score 两阶段召回/短名单/一次决策 | 21/21 |
| `src/streaming_prefix_eval.py` | 前缀回放（M2/M3 主评测） | 产出报告 + 明细 |

验收脚本连真 KEV + 真 LLM 的（a3/a4/a5/s1s3/mic-ws）需要先起隧道与 UI；离线 harness（c1c4）用桩函数 + 虚拟时钟，所以"调用次数/输入文本"是**确定性**的。

### 2.9.2 验收矩阵（最终口径）

| 验收 | 结果 | 关键数字 |
|---|---|---|
| A1 `/api/run` + `/api/run/stream` 回归 | PASS | 4 样例 KEV 阶段**逐字节一致**（截至 `llm_start`）；唯一差异是 DeepSeek 在歧义句上的随机性（基线连跑两次也会翻） |
| A2 `score_eot` 单测 | PASS | `导航回家` → 0.7335/0.0896/0.1686，margin +0.5649，wall 134ms/kev 41ms；记录项：`我想去` margin 仅 +0.0111 |
| A3 四个端到端脚本 | PASS（历史 9/9） | ① 1 次决策、`wasted ≤1`（来自短名单重发）；② 停顿后启动 → 新文本 `llm_cancel` → 最终 1 次（合并文本）；③ CUT_OFF_WAIT 内无 LLM；④ THINK_WAIT 内无 LLM |
| A4 timeline/汇总/复用 | PASS 11/11 | `eot_skipped: unchanged`、已评分文本不再起第二轮选工具、事件数与 SSE 一致、9 个汇总字段齐全 |
| M1b 真实流式 ASR | PASS | `say` 16k wav → 2 partial + 1 final → 1 次有效决策 |
| M4 麦克风（WS） | PASS 6/6 | 752 帧/约 15s；`turn_done`：最后一字 1399 → 确认 2099 → LLM +324 → 决策 +1847（eot 2 / 工具 2 / LLM 1 / 浪费 0） |
| B1 V0 回归（英文 168 轮） | PASS | 留出集 **53/56 = 94.6%**；全集 157/168 = 93.5% |
| B2 前缀回放报告 + 明细 | PASS | 改前 470 条判停结果 / 改后 3233 条 |
| B3 五条通过标准 | **4 PASS / 1 FAIL** | 见下表 |
| C2 默认配置复跑 | PASS | A3 9/9、A4 11/11、A5 4/4、M1b 通过 |
| S1–S3 core_score | PASS 21/21 | 两阶段召回/短名单家族/一次决策/mock 执行 |
| 多段消费 C1–C4 | PASS 36/36 | 不变式 `llm_calls == consumed + wasted` 全成立 |
| 二次确认（mock） | PASS | L2 31/31；端到端 5 路径 |
| 二次确认（真实 KEV） | **有条件 PASS** | 25/31 = 81%（< 85% 门槛）；2 处护栏泄漏 |

### 2.9.3 B3 逐条判定（150ms/前缀，398 句，最终默认配置）

| 标准 | 中文 | 英文 | 判定 |
|---|---|---|---|
| 整句二分类 ≥93% | — | 93.5%（留出 94.6%） | PASS |
| 提前触发 LLM 率 ≤5%（按句） | 3.48% | 0.00% | PASS |
| 最后一字→LLM 启动中位 ≤800ms（复用工具 ≤400ms） | 755ms | **1501ms** | **FAIL**（按产品决定接受，不再为此调优） |
| 每轮有效 LLM 决策恰好 1 次（多段消费后 = 每段 1 次） | 230/230 | 168/168 | PASS |
| 投机浪费率 ≤30% | 3.4% | 0.0% | PASS |

同一配置在 600ms/前缀（判停真正可用）下：提前触发 100% / 70%、浪费 75.9% / 47.4% —— 投机开着时这是**设计行为**。

### 2.9.4 关键指标口径（避免误读）

| 指标 | 定义 | 陷阱 |
|---|---|---|
| 判停覆盖率 | 有判停结果的**非整句前缀** / 全部非整句前缀 | 改前只统计"能落地的"，有幸存者偏差（仅 42/21 个样本） |
| 判停可用覆盖率 | 结果落地时**仍是最新文本**的比例 | 150ms 韵律下只有 3.1% ——"有分"≠"能用" |
| 前缀误判说完率 | 被误判为 complete 的前缀 / 被判定过的前缀（排除 `natural_complete`） | `natural_complete` 是 LLM 自标，偏宽松（"雨刷""锁上"被判可独立成句），报告同时给排除/不排除两口径 |
| 提前触发 LLM 率 | 任一前缀误判 + 停顿 ≥ 确认窗 → 真的启动 LLM 的句子占比（按句） | 关投机 + 长确认窗时才有意义 |
| 浪费率 | `llm_wasted / llm_calls` | 多段消费下"提前触发"不再等于浪费，只有**被反转**的才算 |

### 2.9.5 二次确认的验收口径

| 层 | 内容 | 结果 |
|---|---|---|
| 单元 | `judge_confirm_reply` 纯裁决 + mock 打分 + 池/文案 | 53 项全过（注意：该脚本退出码恒非 0，判定看输出，不看 exit code） |
| L1 | mock 启发式 | 26 pass / 5 N-A / 0 fail |
| L2 | judge 注入（纯逻辑主证据） | **31/31** |
| 端到端 | 5 路径（agree/direct/reject/takeover/unknown） | PASS |
| L3 | 真实 KEV（隧道 :8010） | 22/31（无 `confirm_ask` 上下文）/ **25/31（带上下文 = 生产口径）** |

**关键发现**：L3 runner 不传 `confirm_ask`，与生产路径不一致，会**低估**判中率（71% vs 81%）——属测试工具口径问题，不是 src 缺陷。

## 2.10 参数史与踩坑清单（[假设] → [实测]）

### 2.10.1 九个被实测推翻的初始假设

| # | 原假设 | 实测结论 | 对应默认值 |
|---|---|---|---|
| 1 | `eot_timeout_ms=400` 够 | 真实会话判停常 340–556ms，`timeout` + `stale` 占 70%+ | → **900** |
| 2 | `debounce_ms=150` 更稳 | 必须 **< ASR partial 间隔**，否则每个 pending 都被下一次更新替换，**一次判停都发不出去**（覆盖率归零） | → **80** |
| 3 | 单飞 + pending 合并够用 | **single-flight 是饥饿根因**：150ms 韵律下只有 2–3% 前缀能拿到结果 | → `eot_max_inflight=3`（覆盖 42%→70%→94%） |
| 4 | "每个前缀都有分"就等于判停可用 | 150ms 韵律下可用判停仍约 **1.2 条/句**；判停应只用于**停顿/段尾** | 下一步结构改动 |
| 5 | "选工具在 partial 上提前跑零成本" | 同一隧道下会被选工具拖到 340–556ms | → EOT 独立隧道 `:8010` |
| 6 | `confirm_silence_ms=300` 可以 | 300ms < 停顿（400–800ms），任何一次误判都会**真的**启动 LLM | → **700**（配合投机压到 3.48% / 0%） |
| 7 | `max_silence_ms=2000` 与 `think_wait_ms=3000` 可共存 | 按字面实现会让 A3-4 失败 | THINK_WAIT **只**用 `think_wait_ms` |
| 8 | "KEV 超时 → 立即 CONFIRMED" | 会在用户说话中途提前确认并浪费一次 LLM | 超时只放弃该次判定，退回 `max_silence` 兜底 |
| 9 | 评测要用虚拟时钟 | KEV 延迟本身是被测量，必须用**真实时钟**做时基；只有决策 LLM 用固定 1380ms mock | — |

### 2.10.2 实现期修掉的真实 bug（M1）

1. `ui/server.py` 把 VAD 的 `kind` 覆盖成派发标签 → `speech_start/speech_end` 从未生效；
2. `_on_tools_result` 无条件按投机启动 LLM，忽略 `speculative_llm=false`；
3. EOT 超时被当作立即 CONFIRMED；
4. `speech_start` 未重置静音锚点 → 重新开口被 `max_silence` 立刻确认；
5. CONFIRMED 后在途 LLM 未被登记作废就被覆盖（`llm_wasted` 丢失、无事件）；
6. 归一化未变的更新把 CONFIRMED 打回 LISTENING（真实 ASR 路径多花约 1.4s）；
7. turn 结束后重复到达的同文本 final 被当成 `barge_in` 开新轮。

### 2.10.3 多段消费期修掉的 spec 内部矛盾

1. spec §3.2 的 `_emit(..., **record)` 会重复传 `t_ms`（TypeError）→ 实现跳过该键；
2. §3.2 与 §5 C1 的事件顺序冲突 → 按验收章节：`llm_decision` 先于 `segment_consumed`；
3. **静音锚点缺"最近一次消费"**：决策落地耗时 > `max_silence_ms` 时会在消费同一拍立刻 `turn_done`，第二段永远进不来（实测两段冒烟只出 1 条决策）→ 新增 `t_last_consumed` 锚点，`turn_done = 末次消费 + max_silence_ms`。

### 2.10.4 延迟优化的结论（含一次自我否决）

| 配置 | LLM 启动 p50 | 工具/句 | 判停/句 | 浪费 |
|---|---|---|---|---|
| 基线 | 849ms | 5.55 | 13.35 | 0 |
| 只开"选工具并发 = 3" | **1503ms（更差）** | 10.75 | **7.45** | 0 |
| 只开"不等短名单（看文本变化重发）" | 531ms | 5.60 | 12.95 | 15 |
| **不等短名单 + 重发收敛到 top-k 变化（最终默认）** | **501ms（−41%）** | 5.90 | — | **5** |

- **选工具并发化有害，已撤销**（`tools_max_inflight=1`）：把工具调用推到 10.75/句，挤占同一台 KEV 的判停（13.35 → 7.45/句），p50 反而 +654ms。判停并发之所以有效是"去饥饿、提覆盖"，而短名单只需要**最新一份**，并发只会排队。
- **p90 三格恒为 1504ms** → 长尾不在编排侧，由"判停结果到达晚 / 确认窗"决定。

## 2.11 复现

```bash
PY=/Users/YUYSONG/Agents/navi-dual-system/.venv/bin/python
cd /Users/YUYSONG/Work/poc-streaming-eot

# 1) 两条 KEV 隧道（选工具 :8009 / 判停 :8010 → 同一远程）
K=~/.ssh/kev/id_ed25519; H=ubuntu@106.53.0.235
ssh -i $K -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -N -L 8009:127.0.0.1:8009 $H &
ssh -i $K -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -N -L 8010:127.0.0.1:8009 $H &

# 2) UI（默认 8765；麦克风需要 localhost）
$PY -u -B ui/server.py

# 3) 回归（需 UI 在跑）
$PY -u -B tests/poc-streaming-c1c4.py                       # 离线，多段消费 49/49
$PY -u -B tests/poc-streaming-a3.py 8765                    # 9/9
$PY -u -B tests/poc-streaming-a4.py 8765 --with-reuse        # 11/11
$PY -u -B tests/poc-streaming-a5.py 8765                    # 4/4
$PY -u -B tests/poc-streaming-mic-ws.py                     # 6/6
$PY -u -B tests/poc-streaming-m1b.py /tmp/asr-zh.wav        # PASS
$PY -u -B tests/poc-streaming-s1s3-core-score.py            # 21/21（需 KEV + LLM）

# 4) 前缀回放评测（真 KEV，决策 mock 1380ms）
$PY -u -B src/build_prefix_cases.py --workers 4
$PY -u -B src/streaming_prefix_eval.py --langs zh,en --prefix-interval-ms 150 \
    --pause-ratio 0.2 --decide-ms 1380 --run-id px-m2-primary --report docs/prefix-eval-after.md

# 5) 生成音频（真实 ASR 用）
say -v Tingting -o /tmp/asr-zh.aiff "导航回家" && \
  afconvert -f WAVE -d LEI16@16000 -c 1 /tmp/asr-zh.aiff /tmp/asr-zh.wav
```

环境变量：`POC_PORT`、`POC_UPSTREAM`、`POC_SRC`、`POC_CASES`（pipecat 语料）、`POC_MIC_CASES`、`KEV_EOT_BASE`。

## 2.12 已知问题、剩余风险与下一步

按"该做什么"排序：

1. **判停质量是首要瓶颈**（覆盖率修好后才第一次测准）：前缀误判说完率中文 62.1% / 英文 23.8%，按句 97.4% / 44.6%。→ 接**音频/韵律判停模型**（Smart Turn 类），或先用中文专用判停 prompt/模型（`r6_zh_rich_utter_only`）对比。这是本 PoC 之后**最该做的一件事**。
2. **判停应只用在"停顿"上**：现在每个前缀都发一次（覆盖率高但 97% 落地即过期）。改成"文本稳定 ≥X ms 或 VAD `speech_end` 后才发判停"，可把调用量降回 1–2 次/句、可用率提到接近 100%。
3. **二次确认的两处护栏泄漏必修**（`行吧` 0.946、`就那个吧` 0.867 → 会执行高危动作）：抬高 `AGREE_FLOOR` 会误伤单字"嗯"（0.98），需按句型校准或"连续两次达标"护栏；高危工具建议强制从严。
4. **投机开/关是两个产品策略，不是调参**：开 = 换延迟但会提前触发（浪费率随韵律升到 75.9%）；关 = 提前触发 0% 但中文启动 1159ms。B3 里"提前触发 ≤5%"只在"关投机 + 长确认窗"下有意义——**先定策略，再谈达标**。
5. **KEV 是单点**：两条隧道只是缓解，真正要验的是远程 KEV 服务自身的并发能力（单轮微基准测不出，会话内才暴露）。
6. **`natural_complete` 预标偏宽松**（LLM 自标），统计需保留双口径。
7. **多段消费的窗口很窄**：真人麦克风未验（合成语音下火山把整轮当一个 utterance），一轮说两段的停顿窗口是「消费 #1 落地 → +max_silence」，实测 3.0s 通过、5.0s 收尾。
8. **非目标（PoC 明确不做）**：TTS、真实工具执行（全程 mock）、多会话并发/重连/鉴权、真车部署、Smart Turn、跨决策上下文、动作回滚、异常完备性（KEV 失败只走 `max_silence` 兜底，预热失败静默）。

---

# 附录

## A. 术语表

| 术语 | 含义 |
|---|---|
| **EOT** | End-of-Turn，判停：判断用户"说完了没" |
| **KEV** | 项目使用的打分服务（`kev-latest`），支持 `noul`（独立概率）和 `choice`（选项概率）两类问题 |
| **noul** | 一种提问类型：对每个问题独立给 0–1 概率，不要求互斥归一 |
| **判停三问** | `complete` / `cut` / `wait` 三个 noul 问题 |
| **margin** | `complete − max(cut, wait)`，判停置信度 |
| **两阶段选工具** | 先 17 域打分，再域内工具打分，取 top-3 |
| **投机（speculative）** | 在正式确认之前就调 LLM，被推翻则作废 |
| **消费游标** | 已落地决策封存的文本边界；之后只对未消费后缀判停/决策 |
| **伪/准全双工** | 用户不必等机器做完就能继续说；机器在用户说话期间已完成大部分准备 |
| **stale** | 异步结果落地时文本版本已前进，结果不再驱动状态机 |
| **p50 / p90** | 中位数 / 90 分位延迟 |

## B. 文档地图

| 文档 | 角色 |
|---|---|
| **`docs/PROJECT-POC.md`**（本文） | 项目全景记录：产品逻辑 + 开发 spec |
| `docs/spec-20261015.md` | **权威输入 spec**：总规则、M1–M3、签名、`TurnConfig`、事件表、指标与通过标准 |
| `docs/plan-20261015.md` | **设计稿**：为什么这么设计（三段/投机/复用动机），[实测]/[假设] 标注 |
| `docs/final-report.md` | 最终报告：一行结论、关键指标、B3 判定、已知问题 |
| `docs/spec-streaming-multi-llm.md` | 消费游标 + 段级多次决策（"接近全双工"的关键演进） |
| `docs/spec-latency-opt.md` | 延迟优化四项改动 + 实测曲线（含对"工具并发"的自我否决） |
| `docs/spec-core-score-tools.md` | 工具集替换为 `core_score`（242/17）的 spec |
| `docs/progress.md` | 逐里程碑进度（M1–M5、归档、延迟优化、多段消费） |
| `docs/m3-*.md`（7 份） | M2/M3 调参记录（baseline / tuned / margin / turntext / longconfirm / after-default / after-oldtimers） |
| `docs/prefix-eval-before.md` / `-after.md` | 判停饥饿修复前后的 M2 评测报告 |
| `docs/PROJECT-CONFIRMATION-POC.md` | **二次确认专题**（局部功能，非项目主线） |
| `docs/spec-kev-confirmation-v2.md` / `acceptance-confirmation-v2.md` / `test-design-confirmation-v2.md` | 二次确认的设计 / 验收 / 测试设计（`spec-kev-confirmation.md` 是**作废版**，勿据此实现） |
| `README.md` / `docs/README.md` / `tests/README.md` | 总览、文档对照、测试清单 |

## C. 提交与 provenance

| 提交 | 内容 |
|---|---|
| `fb5b259` | 上游 base（分支 `dev-20261013`，仓库 `navi-dual-system`） |
| `07520cc` | M1–M3（拆 `select_tools`/`decide`、判停、状态机、评测、UI） |
| `caa73d8` | 独立 EOT 隧道 + 并发判停 + 投机保留 |
| `e9e0001` | 推流式 `ASRStream`（麦克风模式） |
| `32560cc` | ASR 断句 `end_time` + last packet 修正 |
| `c9888ef` | PoC 归档独立成库（路径仓库相对化） |
| `17d77d2` | 延迟优化 M0–M4（连接复用/预热/不等短名单/工具并发送撤销） |
| `b3d254f` | 流式多轮消费（消费游标 + 段级多次决策） |
| `83e627e` | 工具集替换为 `core_score`（242/17） |
| `35202f2` | 整句重发按已消费全文判重 + 迟到 EOT 空后缀不再驱动状态机 |

- 上游 4 个提交已导出为 `docs/patches/*.patch`，可在上游仓库按序重放；本仓 `src/` 是等价快照。
- **未提交**：`src/confirm_gate.py`、`src/confirm_orchestrator.py`（新增）、`turn_session.py`/`ui/*` 的二次确认接入、`tests/README.md`。
- 未 push、未改 `dev-20261013`、未动上游 worktree；密钥只在运行时读入，从未打印或写入任何文件。