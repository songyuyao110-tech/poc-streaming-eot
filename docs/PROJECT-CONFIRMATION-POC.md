# 对话式二次确认 + KEV 解读第二轮用户回复意图

> **本文件仅为二次确认专题**（项目主线上的一个分支功能）；项目全景（流式 ASR → KEV 判停 → KEV 两阶段选工具 → 一次 DeepSeek 决策，三段式流水线 + 投机/并行/提前预选做到接近全双工）见 [`PROJECT-POC.md`](./PROJECT-POC.md)。

| 项 | 内容 |
|---|---|
| 项目 | `poc-streaming-eot`（Agent POC：ASR 流式输入 → 两阶段 KEV 打分 → 一次 DeepSeek 决策 → 工具 mock 执行） |
| 功能 | 高危工具执行前的对话式两轮确认；第二轮用户回复的意图由 KEV 三问解读 |
| 文档日期 | 2026-10-20 |
| 代码基线 | 工作区基于提交 `35202f2`；本功能相关文件（`src/confirm_gate.py`、`confirm_orchestrator.py`、`turn_session.py` 改动、`ui/*`、确认测试与文档）目前为**未提交改动** |
| 状态 | 功能已实现；离线（mock）链路全绿；真实 KEV 验收为**有条件 PASS**（判中率 81% < 85% 门槛）；若干产品决策已确认，池/文案/超时精确值待定 |
| 一句话 | 高危动作动手前先开口问一句；用户第二轮回的自然回复由 KEV 判成“同意 / 拒绝 / 接管 / 含糊”，只有明确同意才执行，含糊一律放弃。 |
| 读者 | 产品/PM 读第一部分；开发/测试读第二部分；附录用于速查 |

> 本文档是**项目现状的权威记录**，不是新设计稿。设计过程见 `docs/spec-kev-confirmation-v2.md`（有效版），验收细节见 `docs/acceptance-confirmation-v2.md`，测试设计见 `docs/test-design-confirmation-v2.md`。`docs/spec-kev-confirmation.md` 是“执行前由 KEV 判断要不要确认”的**作废版**，不要据此实现。

---

# 第一部分 · 产品逻辑

## 1.1 一句话与价值

**在高危动作动手前，用一句人能自然回答的话向用户要确认；用户第二轮怎么回，由 KEV 读懂，决定做、不做，还是转去做用户新提的事。**

价值有两面：

- **安全**：关 ESP、开引擎盖、解锁充电枪这类“影响行车安全或物理不可逆”的动作，多一道口语确认闸门；误执行的代价远高于多问一句。
- **不打扰**：只对 8 个高危工具确认；导航、空调、媒体、查询等绝大多数操作保持零确认、直接执行。用户不会因为“什么都要点一下”而烦。

## 1.2 为什么是“对话式 + KEV 解读”

这个方案是三次取舍后的结果：

**① 为什么不是按钮确认？**
座舱的主交互是语音，驾驶中用户的手和眼睛都不可靠。按钮把“确认”变成必须看屏幕、必须点准的二次操作，和语音优先的交互相悖。当前实现里 UI 有一个确认轮的展示卡（确认文案、三问分、意图、动作），但它的定位是**语音的可视化对齐**，不是主通道；卡上也没有确认/取消按钮。主通道始终是“agent 开口问、用户开口答”。

**② 为什么不是所有工具都确认？**
工具集共 242 个，其中 `side_effect=non_idempotent` 有 192 个（当前 fixture 统计）。如果按“不可逆就确认”全量拦，79% 的操作都要多一轮，产品上不可用。真正的筛选规则是：**只挑影响行车安全、或物理上不可逆/需人工恢复的极少数动作**，并且优先挑 YAML 描述里已经写明“需二次确认 / 无法 UI 关闭”的工具。

**③ 为什么不是让 KEV 判断“这次要不要确认”？**
这是作废版方案犯的错。**确认的触发必须是确定性的产品规则**（哪些工具入池），不能交给模型每次即兴决定——否则同一个“关 ESP”，今天问、明天不问，产品行为不可解释。KEV 应该用在它擅长且项目已经在用的地方：**解读用户这句话是什么意思**。

项目原本就有这个哲学：判停（EOT）用 KEV 三问判断用户“说完了没 / 没说完 / 要我等”。确认轮只是把同一范式复用到第二轮回：“你是在同意、拒绝，还是在说别的事？”——**产品规则负责“问不问”，KEV 负责“答的是什么”**，两者职责彻底分开。

## 1.3 哪些工具要确认，哪些永不确认

### 确认池（8 个）

| 工具 | 域 | 产品语义 / 为什么进池 | YAML 信号 |
|---|---|---|---|
| `car_control_Vehicle_HoodOpen` | vehicle_control | 打开前舱盖；**打开后无法用 UI 关闭，只能手动关**，不可逆性最强 | 描述明写“无法UI关闭”；non_idempotent |
| `car_drive_ESPSwitch` | vehicle_control | 关闭 ESP 会降低车身稳定控制，直接影响主动安全 | non_idempotent |
| `car_drive_SnowChainModeSet` | vehicle_control | 防滑链模式会限制车速，改变车辆行驶特性 | 描述明写“需二次确认”；non_idempotent |
| `car_status_BrakeMaintenanceSet` | vehicle_control | 制动盘保养模式直接作用于制动系统 | 描述明写“需二次确认”（虽为 idempotent，仍入池） |
| `car_drive_DriveModeSwitch` | vehicle_control | 切换驾驶模式会改变操纵特性 | non_idempotent |
| `car_adas_control` | adas | 关闭驾驶辅助会降低辅助安全能力 | non_idempotent |
| `energy_unlock_charge_cable` | energy | 解锁充电枪，断开充电的物理连接 | non_idempotent |
| `energy_stop_charging` | energy | 停止正在进行的充电，物理过程不可逆 | non_idempotent |

> 当前实现是**按工具名整工具入池**，没有按参数细分：`car_adas_control` 的“开启辅助”也会确认，`car_drive_DriveModeSwitch` 切到任何模式都会确认。设计稿原本建议只拦“关闭辅助 / 激进或越野模式”等降级参数。参数级收窄属于**待定项**，现状以整工具为准。

### 永不确认（池外，直接执行）

- **导航 / 路线**：目的地设置、路线规划等（navigation 域）。
- **媒体 / 娱乐**：音乐、视频、播放控制。
- **天气 / 信息查询**：天气、胎压、充电状态、车窗/车门/后备箱状态等只读查询。
- **舒适与常规设置**：空调/温度、座椅、香氛、氛围灯、普通车灯、车窗、后备箱、车门解锁/上锁。
- **复位类**（`RangeReset` / `TripReset`）：虽不可逆，但属于“设置回水”的低危动作；设计稿建议放观察名单，默认不确认。

池外工具**永不弹确认**，这是产品规则，不经过 KEV。

## 1.4 二轮确认的完整用户流程

```
用户第一轮：“关闭ESP”
      │  流式 ASR
      ▼
[EOT 判停] ── 没说完 ──▶ 继续听，不进入确认
      │ 说完
      ▼
[KEV 两阶段选工具] ──▶ [DeepSeek 一次决策]
      │
      ├─ done（结束）/ ask_user（追问）──────────▶ 回合结束，不涉及确认
      │
      └─ tool_call
            │
            ▼
   ┌──────────────────────────────────────┐
   │ 产品规则：该工具 ∈ 确认池？            │
   └──────────────────────────────────────┘
            │
            ├─ 否（导航/空调/媒体/查询/舒适…）──▶ 直接执行（mock）
            │
            └─ 是
                  ▼
   ┌────────────────────────────────────────────┐
   │ 等待确认（CONFIRM_WAIT）                     │
   │ 第一轮：agent 开口                          │
   │ “我准备关闭ESP，这会降低车身稳定控制，        │
   │   确认执行吗？”（同时显示确认卡）             │
   └────────────────────────────────────────────┘
                  │
                  │ 用户第二轮语音/文本到达（流式 ASR）
                  ▼
   [EOT 判停] ── 没说完 ──▶ 继续听（用户在慢慢说“好的…没问题”）
                  │ 说完
                  ▼
   ┌────────────────────────────────────────────┐
   │ KEV 确认三问：agree / reject / takeover      │
   │ 输入：第一轮确认文案 + 用户第二轮回           │
   └────────────────────────────────────────────┘
                  │
                  ▼
   [裁决：takeover 优先 > reject > agree(需≥0.75) > unknown]
                  │
        ┌─────────┼──────────┬──────────────┐
        ▼         ▼          ▼              ▼
      agree     reject    takeover       unknown
        │         │          │              │
     执行(mock)  放弃    放弃旧动作，     安全放弃
        │         │      把二轮回当        （等同拒绝）
        │         │      新诉求重跑
        │         │      选工具/决策
        └─────────┴──────────┴──────────────┘
                  │
                  ▼
        第二轮回封口 → 回到监听（等下一句或收尾）
```

关键点：

- **确认只在第一轮决策真的选中池内工具时发生**；池外工具没有任何额外延迟。
- **第二轮回先判停、再判意图**。用户说“先别动，我看下空调”时，不能在他刚说“先别动”时就下结论；必须等这句说完，才能正确判成“接管”。
- 第二轮回被判定后即被“消费”，不会被系统误当成一条新指令再决策一次；只有 takeover 分支会显式把这句话当新诉求复用。

## 1.5 用户第二轮意图：三类 + 失败兜底

| 意图 | 含义 | 典型表达 | 系统动作 |
|---|---|---|---|
| **agree** | 明确同意执行 | 好的，关吧 / 确认 / 行，执行 / 打开吧 / 可以 / 嗯 / 嗯嗯好的谢谢你 | 执行（mock） |
| **reject** | 明确拒绝、撤回或负情绪 | 不要，别动 / 取消 / 算了 / 别动 / 你敢关试试 / 今天先不弄了 | 放弃，不执行 |
| **takeover** | 没在回应确认，转而提出新诉求 | 先看看空调，调到24度 / 导航去公司 / 播放音乐 / 我们先回家再说 | 放弃旧动作，把这句话当新诉求重新选工具/决策 |
| **unknown** | 含糊、推责、指代不明、无法判明 | 嗯……随便吧 / 你看着办 / 随便你 / 都行 | **安全放弃**，绝不默认执行 |

真实 KEV 实测样例（三问分数，越高越像）：

| 第二轮回 | agree | reject | takeover | 判成 | 说明 |
|---|---:|---:|---:|---|---|
| 好的，关吧 | 0.975 | 0.234 | 0.051 | agree | 明确肯定 |
| 嗯 | 0.977 | 0.146 | 0.034 | agree | 单字也可执行（产品已确认） |
| 不要，别动 | 0.018 | 0.915 | 0.182 | reject | 明确拒绝 |
| 你敢关试试 | 0.033 | 0.818 | 0.292 | reject | 负情绪不执行 |
| 先看看空调，调到24度 | 0.029 | 0.788 | 0.937 | takeover | 接管优先，即使 reject 也高 |
| 你先别管这个 | 0.080 | 0.880 | 0.906 | takeover | 产品认可为“转去处理别的事” |
| 嗯……随便吧 | 0.539 | 0.152 | 0.264 | unknown | 含糊即拒护栏生效 |
| 你看着办 | 0.512 | 0.158 | 0.442 | unknown | 推责不执行 |
| 随便你 | 0.711 | 0.174 | 0.334 | unknown | 推责不执行（方向安全） |
| 行吧 | 0.946 | 0.134 | 0.059 | agree | 产品判定“半推半就=默许”，接受执行 |
| 就那个吧 | 0.867 | 0.195 | 0.175 | agree | 产品同上接受执行 |

> 三问是**独立打分**，三个分数不要求和为 1；裁决只看相对差与 agree 的绝对下限。

## 1.6 “含糊即拒”产品规则

判 agree 有两个条件同时满足：

1. agree 比 reject 高出至少 **0.15**（明显正向）；
2. agree 的绝对分至少 **0.75**（足够明确）。

只要 agree 不到 0.75，无论它是不是三问里最高，都落 unknown → **安全放弃**。

为什么必须有绝对分下限：真实 KEV 对含糊/推责的回复常给出“agree 最高但不高”的分布——“随便你”agree 0.711、“你看着办”0.512、“嗯……随便吧”0.539。如果只看谁最高，这些都会被当成同意，去执行关 ESP、开引擎盖这类不可逆动作。**高危动作宁可少做，不可误做。**

这条规则的产品含义是：**含糊不是同意**。用户没明确点头，就当作没同意；可以之后再问，但不能先执行。

## 1.7 已确认的产品决策（重要）

以下决策已由产品确认，作为当前实现的产品口径记录在案：

| # | 决策 | 产品结论 | 代码/验收影响 |
|---|---|---|---|
| D1 | 单字“嗯”是否可以执行？ | **可以，不改**。单字肯定就是同意 | 真实 KEV 判 agree（0.977/0.983）；保留为正控用例，不得为了防含糊把它误伤 |
| D2 | “你先别管这个”判成 takeover 是否可接受？ | **认可，不改**。“先别管”可以理解为转去处理别的事 | 验收报告 R-3 记录为“产品认可、不计缺陷”；原测试期望 reject，属期望口径问题 |
| D3 | “随便你”/推责类怎么处理？ | 按**接管或放弃**处理，方向安全 | 实测落 unknown→放弃；即使某次判 takeover 也可接受，**绝不能执行** |
| D4 | “行吧 / 就那个吧”判 agree 并执行，是否是问题？ | **不是问题**。半推半就的默许 = 同意 | 验收报告曾列为 BUG-1/BUG-2（护栏泄漏）；按本决策转为**已接受风险**，不再作为待修缺陷。若未来要从严，可提高 AGREE_FLOOR 或对“吧”字收尾句加二次门槛 |
| D5 | 确认池扩缩、确认文案口径、超时精确值 | **暂不定，留待后续** | 当前池固定 8 个；文案用 `CONFIRM_ASKS`；超时沿用 `think_wait_ms`（默认 3s），未单独定值 |

> D4 与“含糊即拒”并不矛盾：规则仍然拦“低置信的含糊”；D4 说的是这两句被真实 KEV 高置信判成同意时，产品接受把它当默许。两者边界由 AGREE_FLOOR 决定，后续若调整阈值需重新跑 L2/L3 并回报产品。

## 1.8 超时策略

- 进入等待确认后开始计时；超时**默认放弃，不执行**，并发出明确的“确认超时，安全优先放弃”事件。
- 实现上直接复用 `think_wait_ms` 作为确认等待窗（默认 **3000ms**），**单次超时即放弃**。
- 没有实现设计稿建议的“先追问/重播一次，再超时放弃”，也没有独立的 `confirm_timeout_ms` 配置；**超时精确值与是否追问，产品尚未定**（D5）。
- UI 确认轮演示提交的是完整 TurnConfig（`think_wait_ms` 默认 3000ms）；服务端只在调用方未给 `config` 时补 20000ms 作防抢跑兜底。二轮回在 1.02s 到达，3000ms 窗口足够；20s 不代表产品默认值。

## 1.9 边界与风险（产品视角）

| 项 | 现状 | 影响 |
|---|---|---|
| 真实 KEV 判中率 | 31 条用例中 **25 条判中（81%）**，低于设计稿 ≥85%（≥26/31）的发布门槛；无上下文口径仅 22/31（71%） | 语义层面仍有偏差；发布结论为“有条件 PASS” |
| 已接受的风险 | “行吧 / 就那个吧”被判 agree 会执行；“你先别管”判 takeover | 已由产品决策 D2/D4 接受，不作为缺陷；但属于“护栏在高置信下会被穿透”的既知边界 |
| 真实剩余偏差 | “不是，我说的是关掉它”被判 unknown（真同意被放弃，方向安全）；“别关了先看一下行程”被判 reject（新诉求被丢掉，未接管） | 前者是可用性损失，后者是接管漏判；均不影响“不误执行”的安全底线 |
| 测试工具缺陷 | 单元脚本退出码恒为 1；L3 运行器未传 confirm_ask 上下文 | 会影响 CI/判中率口径，不是产品功能缺陷（详见第二部分） |
| 未人工验收 | 真实麦克风 ASR、浏览器确认卡、超时倒计时均未人工点验 | 目前只验证了后端状态机事件链与离线/mock UI 链路 |
| 真实链路默认未启用 | 主循环默认走 mock 打分；真实 KEV 通道需显式注入 | 81% 是“直接调用真实打分”的口径，不是“真实 TurnSession 端到端”的口径 |
| 只做 mock 执行 | 同意后不调用真实车控 | PoC 定位；不代表真实任务完成 |

---

# 第二部分 · 开发 spec

## 2.1 架构总览

确认闸门是**叠加层**：不改变既有“判停 → 选工具 → 决策”链路，只在“决策已产出 tool_call、尚未执行”这个缝里插入一次挂起与二次判定。

```
                    ┌──────────────── TurnSession（单线程状态机，所有状态变更在 tick 调用线程）────────────────┐
 ASR 流式 ─on_asr─▶ │  LISTENING ──EOT complete──▶ MAYBE_DONE ──静音──▶ CONFIRMED ──decide──▶ _apply_llm   │
 VAD      ─on_vad─▶ │       ▲                          │                    │                            │
 tick()   ────────▶ │       │                          │                    ├─ 池外 / done / ask_user ──▶ │
                    │       │                          │                    │   consume → LISTENING       │
                    │       │                          │                    └─ tool_call 且 ∈ CONFIRM_POOL │
                    │       │                          │                              ▼                  │
                    │       │                          │                    ┌──────────────────┐          │
                    │       │                          │                    │  CONFIRM_WAIT     │          │
                    │       │                          │                    │  挂起决策 + 开口   │          │
                    │       │                          │                    └──────────────────┘          │
                    │       │                          │                              │                  │
                    │       │                          │        第二轮 ASR → EOT 判停（照常跑）           │
                    │       │                          │                              ▼                  │
                    │       │                          │              confirm_gate.score_confirm_reply()  │
                    │       │                          │              （mock 或真实 KEV :8010）            │
                    │       │                          │                              ▼                  │
                    │       │                          │              confirm_gate.judge_confirm_reply()  │
                    │       │                          │                              │                  │
                    │       └──── agree/reject/unknown ─┴── _resolve_confirm 封口 ────┘                  │
                    │                              takeover → _run_takeover → select_tools/decide        │
                    │                                          （新决策若又命中池内 → 再进 CONFIRM_WAIT）    │
                    └─────────────────────────────────────────────────────────────────────────────────────┘
                                                     │ events: confirm_request / confirm_scores /
                                                     │         confirm_resolve / confirm_exec / ...
                                                     ▼
                                        ui/server.py  /api/session/stream（SSE）
                                                     ▼
                                        ui/index.html 确认轮卡片 + 时间线
```

模块分工：

| 模块 | 职责 | 不做什么 |
|---|---|---|
| `src/confirm_gate.py` | 产品数据（池、文案、阈值）+ 纯函数：入池判断、取文案、三问打分（mock/真实）、裁决；CLI | 不碰 TurnSession、不执行工具 |
| `src/confirm_orchestrator.py` | `TurnConfirmGate`：把纯函数编排成 `ready / ask_for / resolve`，产出四分支 payload | 不连 KEV/LLM、不改分段与消费 |
| `src/turn_session.py` | 新增 `CONFIRM_WAIT`、挂起/解析、超时、接管、二轮回封口、事件 | 不实现打分与裁决细节 |
| `src/core_pipeline_run.py` | 既有 `kev_call` / `probability` / `select_tools` / `decide`、`KEV_EOT_BASE=:8010`、`KEV_EOT_CLIENT` | 本功能未改动其语义 |
| `src/turn_eot.py` | 既有判停三问 `score_eot`；确认三问完全仿照其接线 | — |
| `ui/server.py` | `/api/session/stream` 支持 `confirm_rounds`；展开两段 ASR 时间线；注入离线 mock 链 | 演示链不连真实 KEV/LLM |
| `ui/index.html` | “确认轮”模式、输入/预设、确认卡渲染、事件 JSON | 只消费事件，不参与判定 |

**循环 import 处理**：`confirm_gate` 为了复用轻量文本规整 `normalize`，会 import `turn_session`；因此 `turn_session` 在顶层**不能** import `confirm_orchestrator`，而是在 `_default_confirm_gate()` 里惰性导入。改动这两个模块时要保持这个方向。

## 2.2 数据与配置

| 名称 | 值 / 位置 | 说明 |
|---|---|---|
| `CONFIRM_POOL` | `frozenset`，8 个工具（见 1.3） | 只对这些工具触发确认；池外恒 False |
| `CONFIRM_ASKS` | dict，8 条文案 | 第一轮开口文案；缺失时用通用兜底 `_FALLBACK_ASK` |
| `_FALLBACK_ASK` | “我准备执行该操作，这会带来一定影响，确认执行吗？” | 未知/池外工具取文案时兜底（正常不会用到） |
| `CONFIRM_LABELS` / `CONFIRM_ORDER` | `("agree","reject","takeover")` | 三问标签与遍历顺序 |
| `TAKEOVER_THRESHOLD` | `0.5` | `p_takeover >= 0.5` 直接接管；实测接管句 0.9+，推责句 0.33–0.44，区分度足够 |
| `CLEAR_MARGIN` | `0.15` | 正/负差达到 0.15 才判 agree/reject；设计稿建议值，沿用 |
| `AGREE_FLOOR` | `0.75` | 判 agree 的绝对下限（“含糊即拒”护栏）；明确肯定簇 0.865–0.983，含糊簇 0.488–0.711 |
| `_CONFIRM_QUESTIONS` | 3 条 noul instructions | 见 2.3 |
| `KEV_EOT_BASE` | `http://127.0.0.1:8010`，可用环境变量覆盖 | 与判停共用“意图判定”隧道，不排在 242 工具打分之后 |
| `KEV_EOT_CLIENT` | `httpx2.Client(timeout=30s, 连接池 4)` | 与判停共用；确认三问是一次请求内完成 |
| mock 关键词 | agree / reject / takeover 三组 | 仅 `mock=True` 时用于稳定复现，**不是**真实裁决依据 |

阈值取值依据（真实样例）：

- `TAKEOVER_THRESHOLD=0.5`：接管句 “先看空调” 0.937、“播放音乐” 0.957、“你先别管” 0.906；非接管句 “随便你” 0.334、“嗯…随便吧” 0.264。
- `AGREE_FLOOR=0.75`：明确肯定 “嗯” 0.977、“好的关吧” 0.975、“嗯嗯好的” 0.949；含糊 “随便你” 0.711、“随便吧” 0.539、“你看着办” 0.512、“都行” 0.488。
- `CLEAR_MARGIN=0.15`：拒绝句的 reject-agree 差通常 >0.5（“不要别动” 0.897），远高于门槛；门槛只用于挡“两可”分布。

## 2.3 关键接口（签名级）

### 产品 gate

```python
def is_confirm_tool(tool_name) -> bool
# 产品规则：tool_name in CONFIRM_POOL。池外/未知/None 一律 False（大小写敏感，无 "tool:" 前缀）。

def confirm_ask_for(tool_name) -> str
# CONFIRM_ASKS.get(tool_name, _FALLBACK_ASK)
```

### 确认三问打分

```python
def score_confirm_reply(user_reply: str,
                        confirm_ask: str | None = None,
                        *, mock=None) -> dict
```

`mock` 三种取值：

| 取值 | 通道 | 行为 | 返回 |
|---|---|---|---|
| `None`（默认） | 真实 KEV | 一次 `kev_call(KEV_EOT_CLIENT, state, _CONFIRM_QUESTIONS, base=KEV_EOT_BASE)` | `{agree, reject, takeover, mode:"kev", wall_ms, kev_ms, confirm_ask}` |
| `True` | mock | 关键词前筛 → 固定分布，不连 LLM | `{agree, reject, takeover, mode:"mock"}` |
| `dict` | mock + 注入 | 视为 `mock=True` 且用 dict 覆盖三问概率（归一化后） | 同上；用于测试精确进分支 |

真实通道的 `state`：

```json
{
  "confirm_ask": "我准备关闭ESP，这会降低车身稳定控制，确认执行吗？",
  "user_reply": "好的，关吧",
  "questions": ["agree", "reject", "takeover"]
}
```

> 注意：设计稿曾建议在 state 里带 `confirm_tool`，**当前实现没有带**，只带第一轮确认文案。文案本身含操作语义，所以能工作；补 `confirm_tool` 是否提升判中率属待定优化（见 2.8）。

三问定义：

```python
_CONFIRM_QUESTIONS = {
  "agree":    {"type": "noul", "instructions": "用户明确同意/确认执行该待确认的操作？"},
  "reject":   {"type": "noul", "instructions": "用户明确拒绝/不同意执行该待确认的操作（含“先别管/弹掉”这类显式撤回）？"},
  "takeover": {"type": "noul", "instructions": "用户转移话题或提出新的独立请求（如“先看看空调”“导航去公司”）而不是在回应本次确认？"},
}
```

返回校验：每个 answer 必须为 dict、键集合只含 `{"type","noul"}`、`type` 为 `noul`，并经 `probability()` 校验为 `[0,1]` 有限数；否则抛 `ValueError`。真实三问**不归一化**，mock 三问归一化到和为 1。

mock 注入的非法输入（全 0、负数、非有限数）会抛 `ValueError("confirm mock 概率必须为非负有限数且至少一个为正")`。

### 裁决

```python
def judge_confirm_reply(user_reply, scores) -> str
```

```text
1. p_takeover >= 0.5                                      -> "takeover"
2. p_reject - p_agree >= 0.15                             -> "reject"
3. p_agree - p_reject >= 0.15 且 p_agree >= 0.75          -> "agree"
4. 其他                                                    -> "unknown"（调用方按 reject 落地）
```

`user_reply` 只用于信息/日志，不参与裁决——概率已由 KEV/mock 给出。

### 编排层

```python
class TurnConfirmGate:
    def __init__(self, *, mock=True, score_fn=score_confirm_reply,
                 judge_fn=judge_confirm_reply, is_pool_fn=is_confirm_tool,
                 ask_fn=confirm_ask_for): ...

    def ready(self, decision) -> bool
    # decision.kind == "tool_call" 且 decision.tool ∈ 池

    def ask_for(self, decision) -> str

    def resolve(self, user_reply, decision, *, mock=None) -> dict
    # 返回 {user_reply, confirm_ask, pending_tool,
    #       scores: {agree,reject,takeover,mode[,wall_ms,kev_ms]},
    #       intent, action, message}
```

`resolve` 的动作映射：

| intent | action | message 口径 |
|---|---|---|
| agree | `execute` | 用户确认，执行(mock) `{tool}` |
| reject | `abandon` | 用户拒绝，放弃执行 `{tool}` |
| takeover | `reroute` | 用户转移话题，放弃 `{tool}`，把回复当新诉求处理 |
| unknown | `abandon` | 意图不明(intent=unknown)，安全优先放弃，不执行 `{tool}` |

**默认 mock**：`TurnConfirmGate()` 的 `mock=True`。`TurnSession` 的默认 `confirm_gate` 就是 `TurnConfirmGate()`，所以**主循环当前默认走 mock**；真实 KEV 需显式注入 `TurnConfirmGate(mock=False)`（或 `mock=None` 的等价路径）。这一点是当前项目状态，不是“真实链路已经默认启用”。

### CLI

```bash
python -B src/confirm_gate.py --reply '好的，关吧' --tool car_drive_ESPSwitch --mock
python -B src/confirm_gate.py --reply '行吧' --scores '{"agree":0.946,"reject":0.134,"takeover":0.059}'
python -B src/confirm_orchestrator.py --reply '好的，关吧' --tool car_drive_ESPSwitch --mock
```

## 2.4 状态机

既有五状态：`LISTENING / MAYBE_DONE / CUT_OFF_WAIT / THINK_WAIT / CONFIRMED`；新增第六态 **`CONFIRM_WAIT`**。

| 状态 | 含义 | 本功能相关变化 |
|---|---|---|
| `LISTENING` | 听用户说话，无可消费后缀 | 确认分支落完后回到这里 |
| `MAYBE_DONE` | EOT 判“说完”，等静音确认 | 不变 |
| `CUT_OFF_WAIT` | EOT 判“没说完”，等一个短窗 | 二轮回被判 cut 时会进入（见已知缺口） |
| `THINK_WAIT` | EOT 判“要我等”，等更长的思考窗 | 二轮回被判 wait 时会进入 |
| `CONFIRMED` | 静音窗到，可递交决策 | 第一轮决策从这里产出；确认轮挂起前也在该态 |
| `CONFIRM_WAIT` | **已挂起第一轮决策，等用户第二轮回** | 新增 |

### 进入与退出

**进入**：`_apply_llm()` 在决策落地、封口第一轮文本之后检查 `confirm_gate.ready(decision)`；命中则 `_begin_confirm()`：

1. `confirm_pending = dict(decision)`（挂起副本）；
2. `confirm_resolved = None`；
3. 取 `confirm_ask_for(tool)`，发 `confirm_request` 事件（含 tool、arguments、ask 和 summary 快照）；
4. `_to_state("CONFIRM_WAIT", "pooled_tool")`；
5. 取消在途/已完成的投机决策（`_cancel_inflight_llm`），避免挂起后误用。

**在 `CONFIRM_WAIT` 期间**：

- `on_asr` 对新文本**保留**该状态（`state not in ("LISTENING","CONFIRM_WAIT")` 才回 LISTENING）；
- EOT 判停照常跑（用第二轮回前缀），工具短名单也照常跑，但 `_maybe_start_llm` 被状态门控，**不会重复决策**；
- `tick()` 负责超时。

**退出**：

| 触发 | 路径 | 结果 |
|---|---|---|
| 二轮回 EOT 判 complete 且 margin 达标 | `_on_eot_result` → `_resolve_confirm(now, suffix_text)` | 判意图，落分支 |
| agree | `_resolve_confirm` | 封口二轮回 → 发 `confirm_scores`/`confirm_resolve`/`confirm_exec` → LISTENING |
| reject / unknown | `_resolve_confirm` | 封口 → `confirm_resolve`（action=abandon）→ LISTENING |
| takeover | `_resolve_confirm` → `_run_takeover` | 封口 → `confirm_scores`/`confirm_resolve` → `confirm_reroute` → 同步跑 `select_tools`+`decide` → `reroute_tools`/`reroute_decision`；新决策命中池内则**再进 CONFIRM_WAIT**，否则 `confirm_exec`(mock) → LISTENING |
| 超时 | `tick`（`confirm_pending` 非空且静音 ≥ `think_wait_ms`） | 取消在途决策 → 发 `confirm_resolve`（intent=`timeout`, action=abandon）→ LISTENING |
| 二轮回 EOT 判 cut/wait | `_to_state(CUT_OFF_WAIT/THINK_WAIT, "confirm_wait_eot_*")` | 走既有等待窗（见 2.8 已知缺口） |

### 核心方法职责

- **`_begin_confirm(now, decision)`**：挂起 + 发确认请求 + 进状态 + 清在途 LLM。不消费文本（第一轮已在 `_apply_llm` 中消费）。
- **`_resolve_confirm(now, user_reply)`**：取 pending，调 `confirm_gate.resolve()`，把二轮回用 `_consume_suffix()` 封口（避免被当新指令），发 `confirm_scores`/`confirm_resolve`，清 pending，按 intent 落地。agree 发 `confirm_exec`；takeover 调 `_run_takeover`；其余回 LISTENING。
- **`_run_takeover(now, request)`**：把二轮回当新诉求，同步调用注入的 `select_tools_fn` + `decide_fn`，发 `reroute_*`；异常只报 `error(stage="takeover")` 并保持安全（不执行旧动作）。若新决策也命中池内，复用 `_begin_confirm` 再确认一次。
- **`_consume_suffix(now, why)`**：复用 `_seal_consumed()` 把当前未消费后缀封口，记账 `consumed_count`，发 `confirm_consumed` 事件（`why="confirm_reply"`）。这是“二轮回不会被 Listener 再决策”的关键。
- **`_maybe_start_llm` 的 gating**：函数开头 `if self.turn_done or self.state not in ("CONFIRMED","MAYBE_DONE"): return`；`tick()` 也只在 `CONFIRMED` 调它。因此 `CONFIRM_WAIT` 下不会启动新决策，但 EOT/工具短名单仍跑。

### 事件

| 事件 | 关键字段 | 时机 |
|---|---|---|
| `confirm_request` | `tool`, `arguments`, `ask`, `summary` 快照 | 第一轮确认开口 |
| `confirm_scores` | `agree`, `reject`, `takeover`, `mode`, `wall_ms`/`kev_ms`（真实）, `user_reply`, `confirm_ask`, `pending_tool` | 三问打分完成 |
| `confirm_resolve` | `user_reply`, `intent`（agree/reject/takeover/unknown/**timeout**）, `action`（execute/abandon/reroute）, `message`, `pending_tool`, `scores`、`confirm_ask`（timeout 事件无 `scores`） | 裁决落地 |
| `confirm_exec` | `tool`, `mock_only=True`, `message`（takeover 池外时多 `request`） | 执行(mock) |
| `confirm_consumed` | `index`, `text`, `why`, `consumed_segments` | 二轮回封口 |
| `confirm_reroute` | `request`, `message` | 决定接管 |
| `reroute_tools` | `request`, `selected` | 接管后短名单 |
| `reroute_decision` | `request`, `decision`, `message` | 接管后新决策 |

## 2.5 二轮判定时序：先判停，再判意图

```
第二轮回 ASR 前缀
   │
   ├─▶ score_eot(prefix)        判断“说完没”
   │       ├─ complete 且 margin >= eot_margin(0.2) ──▶ 收敛为 user_reply
   │       └─ cut / wait / margin 不足 ──────────────▶ 继续听
   │
   └─▶ 收敛后：score_confirm_reply(user_reply, confirm_ask)
           └─▶ judge_confirm_reply(...)  判断“说的是啥”
```

**顺序不能反**，原因：

1. 用户在确认轮里可能说“好的，然后……”“先别动，我看下空调”。在“好的”半句就判意图，会把还没说完的话误判成同意并**提前执行**——这正是最危险的错误。
2. 先判停可以把“先别动…我看下空调”整句收齐，从而正确走到 takeover，而不是在“先别动”处判成 reject、把后面的新诉求丢掉。
3. 两个 KEV 的职责天然不同：判停判断**流式边界**，确认判断**对话意图**；混在一起会让问题集合爆炸、也破坏既有判停调参成果。

实现上，`TurnSession._on_eot_result` 在 `CONFIRM_WAIT` 分支里只对 `label == "complete"` 且（`eot_policy != "margin"` 或 `margin >= eot_margin`）调用 `_resolve_confirm`；否则只做状态迁移，不判意图。

## 2.6 前端

### API

`POST /api/session/stream`，两种输入：

```text
// A. 直接回放 ASR/VAD 时间线
{"updates": [{"t_ms": 0, "text": "关闭ESP", "final": true}],
 "vad": [{"t_ms": 0, "kind": "speech_start"}],
 "config": {...}, "tool_set": "core_score"}

// B. 确认轮：只给两轮文本，服务端展开时间线
{"confirm_rounds": {"round1": "关闭ESP", "round2": "好的，关吧"},
 "config": {...}, "tool_set": "core_score"}
```

响应是 `text/event-stream`，每条：

```
event: confirm_request
data: {"event":"confirm_request","t_ms":...,"tool":...,"ask":...,...}
```

`confirm_rounds` 的服务端行为（`ui/server.py`）：

1. `_build_confirm_timeline()` 展开两段：第一轮 VAD 0ms、ASR 120ms、speech_end 140ms；第二轮 VAD speech_start 900ms、ASR 1020ms、speech_end 1040ms，确保二轮回落在确认等待窗内。
2. 调用方未给 `config` 时，用 `setdefault` 补 `think_wait_ms=20000`、`max_silence_ms=800`、`confirm_silence_ms=200` 作防抢跑兜底；UI 会提交完整 TurnConfig，实际以 UI 值（`think_wait_ms` 默认 3000ms）为准，二轮回 1.02s 到达仍在窗内。
3. 注入离线 mock 链（`_confirm_mock_chain`）：
   - `eot_fn` 恒判 complete；
   - `select_tools_fn` 用关键词：第一轮含 ESP/关/引擎盖/防滑/充电/刹车 → 池内 ESP；二轮回含空调/导航/音乐/媒体 → 池外空调；否则池外导航；
   - `decide_fn` 直接取 shortlist 第一个；
   - `TurnConfirmGate(mock=True)`。
4. **该演示链路不连真实 KEV/LLM**，只证明流程与 UI，不证明语义判中率。

### 确认卡渲染

DOM/数据结构：

- 控件：`#confirm-r1`（第一轮指令）、`#confirm-r2`（模拟二轮回）、`#confirm-preset`（a/c/d/e 预设）、`#confirm-go`。
- 卡片：`#confirm-card` → `#confirm-summary`（一句话结果）、`#confirm-detail`（逐事件行）、`#confirm-raw`（完整事件 JSON）。
- 前端累积的 `result` 对象：

```js
{ round1, round2,
  decision,     // 第一轮决策工具
  confirmed,    // 需确认的工具（confirm_request.tool）
  ask,          // 确认文案
  scores: [agree, reject, takeover],
  intent, action,
  exec,         // confirm_exec.tool
  reroute }     // reroute_decision.decision.tool
```

渲染规则：

- `llm_decision` → “第一轮决策：{kind}/{tool}”
- `confirm_request` → “⚠ 确认请求：{ask}”（warn）
- `confirm_scores` → “KEV 确认三问：agree x.xxx · reject x.xxx · takeover x.xxx（mode）”
- `confirm_resolve` → “✔ 意图 {intent} · 动作 {action} · 二轮回「…」”
- `confirm_exec` → “→ 执行(mock) {tool}”
- `reroute_decision` → “↪ 接管 → 「request」 新决策 {tool}”
- `turn_done` → “本轮完成”
- 同时把 `asr_update` / 其它事件喂给既有时间线组件。

最终 summary 会拼成：第一轮 → 是否需确认 → 二轮回 → 三问分 → 意图/动作的人类可读结论。

### 预设

| 预设 | 二轮回 | 预期 |
|---|---|---|
| a | 好的，关吧 | agree → 执行(mock) |
| c | 你不要关，取消 | reject → 放弃 |
| d | 先看看空调，调到24度 | takeover → 转空调 |
| e | 嗯…随便吧 | unknown → 默认放弃 |
| （b） | 第一轮填“导航回家”等池外句 | 不弹确认，直接执行 |

## 2.7 测试与验收

### 文件清单

| 文件 | 层 | 作用 | 当前结果 |
|---|---|---|---|
| `tests/test_confirm_gate.py` | 单元 | 裁决纯函数（16）、mock 稳定性与样例落分支（13）、池/文案（24），共 **53 项** | 53/53 通过；**退出码恒为 1**（缺陷） |
| `tests/run_confirm_cases.py` + `tests/confirm_cases.json` | 交互 L1/L2/L3 | 31 条二轮回→意图用例：L1 mock 启发式、L2 注入真实校准分验 judge、L3 真实 KEV | L1 26 pass / 5 N/A / 0 fail；L2 **31/31**；L3 见下 |
| `tests/poc_confirm_flow.py` | 全流程（纯函数） | 用 `confirm_gate` 纯函数编排两轮；并用 TurnSession 证明第一轮会天然选到池内工具 | PASS（验收报告口径） |
| `tests/poc_confirm_e2e.py` | 端到端（真状态机） | 5 条路径：agree / 池外直接 / reject / takeover / unknown，跑真实 `CONFIRM_WAIT` | PASS（验收报告口径） |
| `tests/probe_confirm_kev_real.py` | 真实 KEV 探针 | 9 个样例打真实三问分并判分支 | 8/9（“你先别管”判 takeover，产品认可） |
| `tests/poc-streaming-c1c4.py` | 回归 | 多轮消费、投机作废、整句重发去重、迟到 EOT 空后缀 | 49/49（验收报告口径） |
| `tests/poc-streaming-session-smoke.py` | 回归 | TurnSession 最小冒烟 | 通过（验收报告口径） |

用例分布（31 条）：agree 8 / reject 7 / unknown 5 / takeover 4 / edge 7。

### 三层验收口径

| 层 | 打分来源 | 对比 | 通过标准 | 实测 |
|---|---|---|---|---|
| **L1** | mock 关键词启发式 | `expected_mock_intent`（null 则 N/A） | 非 null 全对 | 26 pass，5 N/A，0 fail |
| **L2（主证据）** | 注入 `score_override`（真实校准分） | `expected_kev_intent` | **必须 100%** | **31/31** |
| **L3** | 真实 KEV（隧道 `:8010`） | `expected_kev_intent` | ≥85%（≥26/31） | **25/31 = 81%**（带生产 confirm_ask 上下文）；无上下文 22/31 = 71% |

L2 的意义：它绕开打分通道，只验证“给定一组合理分数，judge 是否映射到正确分支”。L3 才验证真实语义。

真实 KEV 结论（验收报告）：

- 带 `confirm_ask` 上下文时 25/31；不带上下文 22/31。补上下文显著提升（“关掉吧”等由 FAIL 转 ok），说明**上下文对齐**很重要。
- 3–5 次连跑三问分数逐位一致，本次隧道**未观察到抖动**；判中差异来自语义，不是采样。
- 发布门槛按原始标注算仍是 81% < 85%，因此验收结论为**有条件 PASS**。

### 环境与门槛

- Python：`/Users/YUYSONG/Work/tmp/jev-reopt-checkouts/dev-streaming-eot/.venv/bin/python`（3.12.13，含 `httpx2` 等）；验收报告使用此环境。
  - README 里另写 `/Users/YUYSONG/Agents/navi-dual-system/.venv/bin/python`，本机也存在；两者都能解析上游（`bootstrap` 优先探测 dev-streaming-eot）。以实际能 import `httpx2` 的为准。
- 真实 KEV：`KEV_EOT_BASE` 默认 `http://127.0.0.1:8010`（独立 EOT 隧道）；不可达时 L3/探针会报错，不影响 L1/L2。
- 门槛：**离线（单元 + L1 + L2 + e2e + 回归）全过**，且**真实 KEV L3 ≥85%**，才可发布；当前卡在第二项。

## 2.8 已知问题与后端 TODO

### A. 真实语义（发布门槛）

1. **L3 81% < 85%**：25/31，未达 ≥26/31。
   - 6 条未判中：`行吧`、`就那个吧`（判 agree，产品已接受）、`你先别管这个`、`你今天先别管这个`（判 takeover，产品已接受）、`不是，我说的是关掉它`（判 unknown，真同意被放弃，方向安全）、`别关了先看一下行程`（判 reject，本应 takeover，新诉求被丢）。
   - 按产品口径把前 4 条重标为可接受后，重标口径为 29/31，剩余 2 条仍属语义偏差；但发布门槛按原始标注计算，故结论仍是有条件 PASS。
2. **阈值/措辞校准**：可尝试提高 `AGREE_FLOOR`（会误伤“嗯”“好的关吧”）、对“吧”字收尾句加二次门槛（连续两次 agree / 更高绝对分）、或改问题措辞。任何改动都要 L2 保持 31/31、L3 重测。
3. **`confirm_tool` 未进 state**：真实打分只看到 `confirm_ask` 文案；补工具名/参数是否提升判中率待验证。

### B. 测试工具 bug（不是 src 缺陷）

1. **`tests/test_confirm_gate.py` 退出码反了**：`main()` 返回 `0 if PASS>=3 else 1`，`sys.exit(0 if ok else 1)` 导致全过也退出 1。当前 53 项全过但 `EXIT=1`。
2. **`run_confirm_cases.run_layer_kev` 未传 `confirm_ask`**：`score_confirm_reply(case["user_reply"], mock=False)` 与生产路径（始终传 `confirm_ask_for(tool)`）不一致，L3 被低估（71% vs 81%）。应在 runner 中补上下文。
3. **`tests/README.md` 数字过期**：写 `test_confirm_gate.py` 45/45，实际 53 项。

### C. 未人工验收

- 真实麦克风 ASR / WebSocket 链路（`poc-streaming-mic-ws.py`、`poc-streaming-m3*`）。
- 浏览器确认卡渲染（`/api/session/stream` + `confirm_rounds`）只验证了后端事件链，未人工点验。
- 超时倒计时（3s 无回应 → abandon）未人工驱动秒级验证；c1c4/flow 只覆盖状态转移逻辑。
- 真实 KEV 未接入 `TurnSession` 默认路径做端到端跑：主循环默认 `TurnConfirmGate(mock=True)`；81% 是“直接调用打分”的口径。

### D. 代码走查发现（未纳入验收，标“待确认”）

1. **二轮回被判 cut/wait 时会离开 `CONFIRM_WAIT`**：`_on_eot_result` 对非 complete 只做 `_to_state(THINK_WAIT/CUT_OFF_WAIT)`；随后既有 `_confirm()` 会在等待窗到后进入 `CONFIRMED`，把二轮回当**普通指令重新决策**（走查探针里 fake 工具恒返回 ESP，因此再次命中池内工具并重新发确认请求），而不是继续等同一确认回合或直接判意图。
   - 复现：`tmp/probe_confirm_wait_path.py`（fake EOT 对“嗯……”返回 wait），可见 `CONFIRM_WAIT → THINK_WAIT → CONFIRMED → llm_decision → CONFIRM_WAIT`。
   - 影响：不会误执行**原待确认动作**（新决策若仍命中池内会再确认）；但会把二轮回当新指令，重复提问、丢掉“这一句是对上一问的回答”的语义；若新决策恰好落到池外工具，则会直接 mock 执行该新动作——这是该缺口更值得注意的一面。是否接受**待产品/开发确认**；现有 5 条 e2e 路径未覆盖该分支。
2. **超时只放弃、不追问**：实现用 `think_wait_ms` 单次超时即 abandon；设计稿建议的“先重播一次、总计 ~8s 再放弃”未实现，也没有独立 `confirm_timeout_ms`。
3. **池是工具级，不是参数级**：`car_adas_control` 开启也会确认；`DriveModeSwitch` 所有模式都确认。设计稿建议只拦降级/激进参数。
4. **agree 分支未二次校验池成员**：设计稿 §4 写了“工具仍在池内才执行”；代码直接用 pending_tool。池是常量，当前无失效路径，但若未来池可配置需补。
5. **reject 分支未追问“要不要别的”**：只放弃，不主动引导；设计稿标为“可选”。
6. **`_run_takeover` 同步执行**：在 tick 线程里跑 `select_tools + decide`（真实模式下是 KEV + LLM，数百 ms 到秒级），会阻塞状态机/UI 推进；PoC 可接受，产品化需改异步。
7. **`_consume_suffix` 有一处拼写残余**：`self.consumptable_count = self.consumed_count = ...`，多创建一个无用属性，不影响行为。
8. **同回合内多次确认时 `t_confirm_latency_ms` 可能为负**：`t_confirmed` 语义是“本回合第一次确认”，第二次确认不更新它；属指标口径问题。
9. **确认卡是只读展示**：显示确认文案/三问分/意图/动作，没有确认、取消按钮；按钮入口未实现（与“语音为主”一致，但设计稿曾建议同时给按钮）。

### E. TODO（建议顺序）

1. 修测试工具：runner 补 `confirm_ask`；`test_confirm_gate.py` 退出码。
2. 用生产上下文重跑 L3，分簇（agree/reject/unknown/takeover/edge）看漏判，再做阈值/措辞校准。
3. 明确 cut/wait 分支的产品行为（继续等 or 直接判意图），补 e2e。
4. 决定是否参数级收窄池、是否加 `confirm_tool` 上下文、是否实现超时追问。
5. 真实 KEV 接入 `TurnSession` 跑一次端到端；再做真实麦克风 + 浏览器确认卡 + 超时倒计时人工验收。

## 2.9 运行方式

```bash
PY=/Users/YUYSONG/Work/tmp/jev-reopt-checkouts/dev-streaming-eot/.venv/bin/python
cd /Users/YUYSONG/Work/poc-streaming-eot

# 起 UI（默认 127.0.0.1:8765；确认轮是默认模式）
$PY -u -B ui/server.py

# 离线：单元（注意当前退出码恒 1，看输出“53 项通过”而不是退出码）
$PY -B tests/test_confirm_gate.py

# 离线：L1 + L2（31 条，L2 必须 31/31）
$PY -B tests/run_confirm_cases.py

# 加真实 KEV L3（需隧道 :8010）
$PY -B tests/run_confirm_cases.py --mode kev
$PY -B tests/run_confirm_cases.py --only kev

# 端到端（mock）
$PY -B tests/poc_confirm_flow.py
$PY -B tests/poc_confirm_e2e.py

# 真实 KEV 探针（9 样例，需隧道）
$PY -B tests/probe_confirm_kev_real.py

# 回归
$PY -B tests/poc-streaming-c1c4.py

# CLI 单点验证
$PY -B src/confirm_gate.py --reply '好的，关吧' --tool car_drive_ESPSwitch --mock
$PY -B src/confirm_gate.py --reply '嗯…随便吧' --tool car_drive_ESPSwitch --mock
```

浏览器操作：`http://127.0.0.1:8765` → 模式选“确认轮” → 填第一轮指令与二轮回（或选预设）→ “跑确认轮”。池外第一轮（如“导航回家”）用于验证 b 路径（不弹确认）。

---

# 附录

## A. 文件清单

| 路径 | 说明 |
|---|---|
| `src/confirm_gate.py` | 确认池/文案/三问/裁决/CLI（218 行） |
| `src/confirm_orchestrator.py` | `TurnConfirmGate` 编排层（119 行） |
| `src/turn_session.py` | 六状态状态机、`CONFIRM_WAIT`、挂起/解析/接管/封口/超时（854 行，本功能有改动） |
| `src/turn_eot.py` | 判停三问 `score_eot`（确认三问的范式来源） |
| `src/core_pipeline_run.py` | `KEV_EOT_BASE`、`kev_call`、`probability`、`select_tools`、`decide` |
| `ui/server.py` | `/api/session/stream`、`confirm_rounds`、`_confirm_mock_chain`、mic WS |
| `ui/index.html` | 确认轮模式、确认卡、事件渲染 |
| `fixtures/tools-core-score.yaml` | 242 工具 / 17 域；池内工具的 `side_effect` 与描述依据 |
| `docs/spec-kev-confirmation-v2.md` | **有效**设计与产品口径 |
| `docs/spec-kev-confirmation.md` | **作废**（执行前由 KEV 判断要不要确认的错误建模） |
| `docs/acceptance-confirmation-v2.md` | 验收报告（有条件 PASS、81%、BUG/R 编号） |
| `docs/test-design-confirmation-v2.md` | 六层测试设计、L1/L2/L3、阈值校准方法 |
| `tests/confirm_cases.json` | 31 条语义用例（含 `score_override`、`kev_needed`） |
| `tests/run_confirm_cases.py` | 用例 runner（L1/L2/L3） |
| `tests/test_confirm_gate.py` | 单元 53 项（退出码 bug） |
| `tests/poc_confirm_flow.py` | 纯函数全流程 + 第一轮命中演示 |
| `tests/poc_confirm_e2e.py` | 真状态机 5 路径 |
| `tests/probe_confirm_kev_real.py` | 真实 KEV 9 样例探针 |
| `tmp/probe_confirm_wait_path.py` | 本文档新增走查探针：二轮回 cut/wait 的路径（临时） |
| `tmp/probe_fails_scores.py`、`tmp/probe_l3_with_context.py` | 验收期临时探针（fail 分数 dump、上下文 L3 复测） |

## B. 已确认决策速查

| # | 决策 | 结论 |
|---|---|---|
| D1 | 单字“嗯” | 可执行，不改 |
| D2 | “你先别管这个”判 takeover | 产品认可，不改 |
| D3 | “随便你”/推责类 | 接管或放弃均可；**绝不执行** |
| D4 | “行吧 / 就那个吧”判 agree 并执行 | 产品接受（半推半就=默许）；验收 BUG-1/BUG-2 转为已接受风险 |
| D5 | 池扩缩、文案口径、超时精确值 | 暂不定，留待后续 |
| D6 | 含糊默认 | 绝不默认执行；unknown→放弃 |
| D7 | 确认触发 | 产品规则（池）决定，KEV 只判第二轮回意图 |

## C. 已知问题 / TODO 速查

| 优先级 | 项 | 状态 |
|---|---|---|
| P0 | L3 真实 KEV 81% < 85% | 待校准阈值/措辞，重跑门槛 |
| P1 | 测试 runner 未传 `confirm_ask` | 待修（测试工具） |
| P1 | `test_confirm_gate.py` 退出码恒 1 | 待修（测试工具） |
| P1 | 二轮回 cut/wait 离开 CONFIRM_WAIT、重复决策 | 待产品/开发确认；补 e2e |
| P2 | 超时无追问/无独立配置 | 待产品定值 |
| P2 | 池为工具级、未参数级收窄 | 待产品定池 |
| P2 | 真实 KEV 未接入主循环默认路径 | 待端到端跑 |
| P3 | 真实 ASR / 浏览器 UI / 超时倒计时人工验收 | 未做 |
| P3 | agree 未二次校验池、reject 未追问、takeover 同步阻塞 | 产品化前处理 |