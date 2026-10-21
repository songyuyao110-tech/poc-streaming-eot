# 产品/流程设计：对话式二次确认 —— KEV 解读第二轮用户回复意图

- 状态：设计稿（只写产品与流程，不改代码）
- 仓库：`/Users/YUYSONG/Work/poc-streaming-eot/`
- 关联只读依据：
  - `src/turn_eot.py`：KEV 判停三问 `score_eot`（此版本作废 `docs/spec-kev-confirmation.md`，那是"执行前是否要确认"的错误建模，详见 §2.3）
  - `src/core_pipeline_run.py`：`kev_call` / `probability` / `select_tools`（两阶段工具打分）/ `decide`（一次决策，`kind ∈ {tool_call, ask_user, done}`）
  - `src/turn_session.py`：五状态回合状态机 `LISTENING / MAYBE_DONE / CUT_OFF_WAIT / THINK_WAIT / CONFIRMED`
  - `src/asr_stream.py`：流式 ASR 产出 `(t_ms, text, final)`，`final` 标注句子边界
  - `fixtures/tools-core-score.yaml`：242 工具 / 17 域，含 `side_effect`(可逆/不可逆) 与描述
  - `ui/server.py` + `ui/index.html`：`/api/run/stream`（mock 执行时间线）+ `/api/session/stream`（ASR 回放）

---

## 0. 一句话与核心差异（相对上一版作废方案）

**这一版做"对话式两轮确认"**：第一轮 agent 挑中一个高危工具动作后**不直接执行**，而是**开口（语音/文字）向用户要一句确认**；第二轮用户回复后，**用 KEV 解读用户这句话落在哪类意图**（同意 / 拒绝 / 接管），据此决定执行、放弃，还是 agent 接管转去处理用户的新诉求。

> 确认意图按**三类**建模：`agree`(同意→执行) / `reject`(拒绝→放弃) / `takeover`(接管→转新诉求)。其中"先别管 / 弹掉"这类显式撤回，语义上就是"不要做当前动作"，归入 `reject`（v2 早期草案的独立 `rescind` 类别已按产品意见移除）。

> 上一版方案把 KEV 用错了地方：它让 KEV 判断"**池内工具这一次到底要不要确认**"（执行前 gate）。用户要的不是这个——确认的**触发**是产品规则（哪些高危工具要问），KEV 真正该判的是**用户第二轮回复的意图**。这正是本设计重做的原因，也正好贴合本仓库 `turn_eot.py` 的既有哲学：**KEV 本来就是在解读用户意图**（判停判断用户是否"说完 / 没说完 / 要我等"），确认轮只是把这一范式复用到"这轮回复是同意、拒绝、取消、还是接管"。

始终区分两类决策：
- **产品决策**（PM 定，写死/配置）：哪些工具要确认（候选池）、确认文案、超时默认、只问一次还是可追问。
- **依赖 KEV 的意图判定**（模型输出）：用户第二轮回复落在三类里的哪一类。

---

## 1. 产品定义

### 1.1 一句话价值
**在高危动作动手前，用语音向用户要一句人类可自然应答的确认；用户的这句回应本身也是自然语音/文字，由 KEV 解读它确认还是反悔。** 让"关 ESP / 开引擎盖 / 开防滑链"这类高风险操作获得一道口语式的保险闸，同时其他绝大多数工具保持零打扰直接执行。

### 1.2 范围：候选确认池（要确认）vs 池外（永不确认）

**进池原则**：不是按 `side_effect=non_idempotent` 全进（那样有 191 个，吓跑用户）。真正的产品规则是——**只挑"影响行车安全 或 物理上不可逆 / 需人工动作才能恢复"的那几个**，且优先挑 YAML 描述里已自带"需二次确认 / 无法 UI 关闭"这类风险语义的。

**候选名单（建议，全部来自 vehicle_control / adas / energy，可从这 8 个里选）：**

| 工具 | 域 | side_effect | 为什么进池（数据里的风险信号） |
|---|---|---|---|
| `car_control_Vehicle_HoodOpen` | vehicle_control | non_idempotent | 描述"打开前舱盖（**无法 UI 关闭，只能手动关闭**）"，不可逆性最强 |
| `car_drive_ESPSwitch` | vehicle_control | non_idempotent | ESP 车身稳定，关掉影响主动安全 |
| `car_drive_SnowChainModeSet` | vehicle_control | non_idempotent | 描述"防滑链模式（**需二次确认**）” |
| `car_status_BrakeMaintenanceSet` | vehicle_control | idempotent | 描述"制动盘保养（仅电车，**需二次确认**）”，直关制动安全 |
| `car_adas_control (action=disable)` | adas | non_idempotent | 关闭驾驶辅助 —— 注意只有关辅助 / 关巡航这类"降级"参数动作才需确认，开启类不必然触发 |
| `car_drive_DriveModeSwitch → sport/offroad` | vehicle_control | non_idempotent | 切到激进/越野操纵相关模式 |
| `energy_unlock_charge_cable` | energy | non_idempotent | 解锁充电枪，断开充电物理连接 |
| `energy_stop_charging` | energy | non_idempotent | 停止正在进行的充电 |

> `side_effect=non_idempotent` 只作**加分信号**，不是池的筛选键。`idempotent` 但描述自带"需二次确认"（如 `BrakeMaintenanceSet`）也应进池——证据在 YAML 描述里，不在 side_effect 字段里。

**明确不确认（池外，永不弹确认、直接执行）：**
- 导航 / 路线（navigation 22 个）、天气、媒体/音乐/视频（media_*）；
- 舒适性 / 常规设置：空调 hvac、座椅 `seat_*`/`car_seat_*`、香氛、氛围灯、普通车灯、车窗、后备箱 `TrunkControl`、车门解锁/上锁等；
- 信息查询：`energy_*query*`、`car_status_*Query*`、`car_window_state`、`car_door_state`、`car_trunk_status` 等只读查询；
- **复位类虽不可逆但属"设置回水"的低危动作**（`RangeReset` / `TripReset`）——产品建议放进观察名单，默认不确认（用户尺度）但可作为池的候选扩展。

### 1.3 用户能感受到的效果（什么时候会听到/看到确认、什么时候不会）

| 场景 | 表现 |
|---|---|
| "关闭 ESP" / "打开引擎盖" / "开启防滑链模式" | ✅ agent **开口**确认（第一轮），等待一句口头的"好 / 确认 / 不要" |
| 用户第二轮回一句"好的 / 别动 / 先管空调" | ✅ KEV 解读 → 执行 / 放弃 / 放弃并接管新诉求 |
| "导航回家" / "空调 24 度" / "播放周杰伦" / "查胎压"（池外） | ❌ 永不确认，直接执行 |
| 用户第一句只说一半（"打开那个"流式未收敛） | ❌ 先用 EOT 判停判断"说完没"，没说完就继续听，不谈确认 |

---

## 2. KEV 在"确认轮"里的角色（本方案最核心）

### 2.1 KEV 判什么：确认回复意图"多问"打分

仿照 `turn_eot.py` 的判停三问——对用户的一句话，用 KEV 独立性 noul 提问逐一打分，`probability(noul)` 归一为 0~1，然后按规则归类。确认轮复用同一套 `kev_call(state, questions, base)` + `probability()` 的接线（见 `core_pipeline_run.py:76-92`）。

**问题模板示例（`state` 同时带第一轮确认文案与用户第二轮回复，让 KEV 知道"在回应什么"）：**

```json
{
  "state": {
    "confirm_ask": "我准备关闭ESP，这会降低车身稳定控制，确认执行吗？",
    "user_reply": "好的，关吧",
    "confirm_tool": "car_drive_ESPSwitch"
  },
  "questions": {
    "agree":   {"type": "noul", "instructions": "用户明确同意/确认执行该待确认的操作？"},
    "reject":  {"type": "noul", "instructions": "用户明确拒绝/不同意执行该待确认的操作（含“先别管/弹掉”这类显式撤回）？"},
    "takeover":{"type": "noul", "instructions": "用户转移话题或提出新的独立请求（如“先看看空调”“导航去公司”）而不是在回应本次确认？"}
  }
}
```

> 这是产品层可理解的问题模板，落到代码即 `ConfirmedQuestions` 常量；与判停三问只差一个问题集合，KEV 接线完全一致。确认意图按**三问**（agree/reject/takeover），不做独立的 rescind 类别——"先别管/弹掉"由 `reject` 覆盖。

### 2.2 三类意图不互斥时如何定优先（重点）

KEV 对三题各自给一个概率，三题**逻辑上不完全互斥**（一条"好的，但先看看空调"既有 agree 信号又有 takeover 信号）。需要一条**固定的裁决次序**，而不是直接 argmax：

1. **`takeover`（接管）优先级最高** —— 只要这条明显，说明用户根本没在回应本次确认，是在说别的事。正确动作是放弃当前待确认动作，转去处理新诉求，而不是去执行或拒绝旧动作。
2. **`agree` vs `reject` 看正负情绪/肯定词** —— 若没走到 takeover，则在"同意"和"拒绝"之间二选一。依据：明确肯定词（好的/确认/执行吧/打开吧/可以）→ agree；明确否定词（不要/取消/算了/别动/先别管/弹掉/关掉吧）→ reject。若两者都模棱两可且差距小，进入"拿不准"兜底。
3. **兜底默认** —— 见 §4，安全优先默认"拒绝/放弃 + 可追问"，绝不默认执行。

### 2.3 建模比较与推荐：(a) 独立一套"确认意图 KEV" —— 推荐

比较三种建模：

**(a) 独立新增一套"确认意图 KEV 多问"（推荐）**
- 独立的 `score_confirm_reply(user_reply, confirm_ask)`，仿 `turn_eot.score_eot`，独立的问题模板、独立的阈值。
- 理由：
  1. **时机与输入不同，不该混**：判停三问的输入是一段"流式未收敛的说话前缀"，目标是判断"你说完没"；确认轮的输入是"用户对既定请求的回应"，目标是判断"你同不同意"。共享一套打分会让两套语义互相污染。
  2. **判停三问忙着判"说完没"，无暇判"同不同意"**。若把确认并入判停，一场对话里 KEV 一次性要既判断流式边界又判断确认意图，问题集合会爆炸、且单个打分不稳定。
  3. **独立性/可测性**：确认轮是一个清晰、有限的场景，独立打分可以被单独评测（§6 的场景表），阈值也能独立调参，不影响既有判停/打分的 M3 调参成果。
  4. **判停的输出仍要保留**：用户第二轮语音到达时，我们**先要用判停判断"说完了没"**（§3.2），再说"然后才进确认意图判定"。两者是顺序两步，天然不同函数。

**(b) 复用/扩展判停三问机制** —— 判停是"三问→argmax→margin"的流式范式，跟"三问→优先级裁决"的确认范式不同（确认多一个"优先级"维度，且输入到的是最终回复而非流式前缀）。硬塞进判停会破坏判停的单一职责。**不推荐。**

**(c) 把待确认动作直接并进现有两阶段工具打分** —— 工具打分是"这句话要不要这个工具"，确认轮关心的是"这句用户回应是同意还是拒绝"。两者对象不同（一个是业务意图，一个是对话状态）。且两阶段打分 242 工具很重，为一个二值确认问题拉 17 域不划算。**不推荐。**

> **明确时机**：确认 KEV 只在"用户第二轮回复到达并判停收敛后"运行，输入是 `user_reply`（+ 第一轮确认文案作上下文 reference）。它与第一轮选工具的 `select_tools`、EOT 判停的 `score_eot` 是三个不同时机、不同输入、不同问题集，互不复用、互不串扰。

### 2.4 与现有代码的真实贴合

- 判停三问范式（`turn_eot.py:25-42`）：本确认 KEV 完全照抄其结构——`questions = {name: {"type":"noul","instructions":...}}`、`state = json.dumps({...})`、`kev_call(client, state, questions, base)`、`probability(answer["noul"])`。唯一差异是问题集合与裁决逻辑（先判 takeover 优先级）。
- 可复用独立 `KEV_EOT_CLIENT` 连接池与 `KEV_EOT_BASE`（`core_pipeline_run.py:40,50`），因为确认轮同样不希望排在 242 工具打分之后；建议新增 `KEV_CONFIRM_CLIENT`（或复用 EOT 的 tunnel/池），语义上归"意图判定"这一类，与工具打分分离。

---

## 3. 完整的两轮交互流程图（ASCII）

```
┌──────────────────────────────  回合开始  ─────────────────────────────┐
│                                                                        │
│  用户第一句（流式 ASR: t_ms,text,final）                                  │
│      │                                                                 │
│      ▼                                                                 │
│  [EOT 判停:score_eot]   ←── 每段前缀} 判 complete/cut/wait + margin       │
│      │ 未说完(cut/wait) → 继续听，回到 LISTENING                         │
│      ▼ 说完(complete, margin≥0.2)                                       │
│  [两阶段工具打分:select_tools] → 短名单 top-k                              │
│      │                                                                 │
│      ▼                                                                 │
│  [一次决策:decide] → kind ∈ {tool_call, ask_user, done}                  │
│      │                                                                 │
│      ├─ done(结束)/ask_user(追问) → 进 ask_user 卡片, 回合结束             │
│      │                                                                 │
│      ▼ tool_call                                                       │
│  [产品判定: 该工具 ∈ 待确认池 ?]   ←── 产品规则, 非 KEV                     │
│      │                                                                │
│      ├─ 否(池外) ──────► [直接执行(mock)] ──► segment_consumed ──► LISTENING
│      ▼ 是(池内, 高危)                                                  │
│  ┌─────────────────────────────────────────────────────────────┐      │
│  │  状态: CONFIRM_WAIT（新增, 等用户第二轮回复）                     │      │
│  │  第一轮: agent 开口/显示确认文案                                 │      │
│  │    ╔═══ [第一轮输出] ═══╗  "我准备关闭ESP，这会降低车身稳定        │      │
│  │    ║  确认请求 (语音+卡) ║   控制，确认执行吗？"                  │      │
│  │    ╚═══════════════════╝   用户随时可说"好/不要/先看空调"        │      │
│  └─────────────────────────────────────────────────────────────┘      │
│      │                                                                 │
│      ▼  用户第二轮语音到达（流式 ASR）                                   │
│  [EOT 判停:score_eot(第二轮前缀)]                                       │
│      │ 未说完 → 继续听（用户可能在慢慢说"好的…没问题"）                  │
│      ▼ 说完(收敛) → suffix_text = 第二轮回复 user_reply                 │
│  ╔════════════════════════════════════════════════════════╗             │
│  ║  [KEV 确认意图:score_confirm_reply(user_reply, confirm_ask)]      ║  ← 核心
│  ║   三问 → agree/reject/takeover → 按优先级裁决                          ║
│  ╚════════════════════════════════════════════════════════╝            │
│      │                                                                │
│      ├─ takeover(接管) 优先> 放弃当前待确认动作, agent 转向 user_reply 新诉求
│      │                      → 新诉求重新走 select_tools/decide（新回合）
│      ├─ agree      > 重复校验工具仍在待确认池 → [执行该动作(mock)]
│      └─ reject     > 放弃/不执行（含"先别管/弹掉"）, 询问是否要别的
│      │                                                                │
│      └──(任一分支落地) → segment_consumed → LISTENING(等下一句)          │
│                                                                        │
└────────────────────────────────────────────────────────────────────┘
```

### 3.1 状态机：如何与现有五状态共存

现有 `TurnSession` 状态：`LISTENING / MAYBE_DONE / CUT_OFF_WAIT / THINK_WAIT / CONFIRMED`，语义是**一个回合内对"用户当前这句"的收敛**（MAYBE_DONE=说完待确认，CONFIRMED=静默阈值到、已递交 LLM 决策，CUT_OFF_WAIT/THINK_WAIT=用户没说完或要等）。

确认轮新增一个状态 **`CONFIRM_WAIT`**（等待用户第二轮回复）：

- 进入条件：第一轮 `decide` 返回 `tool_call` 且该工具 ∈ 待确认池 → 从 `CONFIRMED` 转入 `CONFIRM_WAIT`（或作为新回合的初始态）。
- 在 `CONFIRM_WAIT` 下，**暂停再跑 `decide`**（`_maybe_start_llm` 在确认等待期间被 gating，避免 agent 在等确认时又自作主张决策），但 **EOT 判停照常跑**（用于判断第二轮语音是否说完）。
- 收到第二轮回复并判停收敛 → 跑 `score_confirm_reply` → 三分支落地后回到 `LISTENING`。
- 现有 `LISTENING/MAYBE_DONE/CUT_OFF_WAIT/THINK_WAIT` 都是"第一轮（或之后普通指令）收敛"用的，`CONFIRM_WAIT` 只叠加在其后，不破坏它们。

> 说明：本仓库 `TurnSession` 的"回合(turn)"是"一次已消耗的决策 → 回到 LISTENING"。确认轮可建模为：第一轮落地一个 `CONFIRM_WAIT`（不消耗 segment / 或消耗但保留 pending action），待第二轮确认意判决之后再真正更新状态。具体立项时落在 `_to_state("CONFIRM_WAIT", ...)`。

### 3.2 用户说话中途：先用 EOT 判停判断"是否说完"，再进确认意图判定

这是关键的正确顺序（复用本仓库真实机制）：

1. 第二轮用户开始说话，ASR 流式吐出 `(t_ms, text, final)` 前缀；
2. `TurnSession.on_asr` 对每个前缀跑 `score_eot`，用 `complete/cut/wait + margin` 判断"这句话说完没";
3. **只有判停给出 `complete`（margin≥0.2 默认阈值）才把当前 `suffix_text()` 当作"完整第二轮回复 user_reply"**；
4. 拿这个 `user_reply` 进 `score_confirm_reply`。

不做这步就会出错：用户在确认轮里说"好的，然后……"或"先别动，我看下空调"，若在"好的"半句就按下确认，会误判为同意并提前执行；先判停就能等到"我看下空调"说完，从而正确走到 takeover 分支。**EOT 判停负责"说完没"，确认意图 KEV 负责"说的是啥"，顺序、职责彻底分开。** ASR 的 `final` 字段（`asr_stream.py:120-121` 的句子边界）可作为提前收敛提示，但真正的门控仍是 EOT 判停（与现有回合一致）。

---

## 4. 意图判定规则（伪代码 / 结构化）

```
# 第一轮：产品 gate
decision = decide(...)                 # kind ∈ {tool_call, ask_user, done}
if decision.kind == "tool_call" and decision.tool in CONFIRM_POOL:
    state → CONFIRM_WAIT
    speak/emit(CONFIRM_ASKS[decision.tool])     # 第一轮口语确认，如"我准备关闭ESP…确认执行吗？"
    emitted_msg = decision.tool
    wait_for_user_second_reply()       # EOT 判停在此照常跑，但不 advance decide()
else:
    execute_or_ask(decision)           # 池外直接执行 / done / ask_user

# 第二轮：KEV 解读意图
state = CONFIRM_WAIT
收到 user_reply（已通过 EOT 判停判定"说完"）:
    intent = KEV_judge_confirm_reply(user_reply, confirm_ask=emitted_msg)

    if intent == "takeover":           # 优先级最高：没在回应确认
        放弃当前待确认动作(decision.tool)
        agent 接管, 把 user_reply 作新诉求 → select_tools/decide 新回合
    elif intent == "agree":
        if decision.tool still in CONFIRM_POOL:   # 无状态失效才执行
            执行(decision.tool, mock)
        else:
            放弃（池被改/工具失效, 不执行）
    elif intent == "reject":           # 含"先别管/弹掉"这类显式撤回
        放弃该操作, 询问"是否需要别的"
    else:  # intent == "unknown"/ 拿不准
        默认按 reject 处理（不执行——安全优先）
        可选: 追问一次"没听清，确认要执行吗？"或直接放弃

    → segment_consumed → LISTENING
```

### 4.1 KEV_judge_confirm_reply 判定要点

- **问题项**：`agree / reject / takeover` 三题（noul，见 §2.1）。不设独立的 `rescind`，显式撤回由 `reject` 覆盖。
- **裁决次序**（`judge_confirm_reply` 内部逻辑，非裸 argmax）：
  1. `if p_takeover >= T_takeover(建议0.5): return takeover`
  2. `if p_reject  - p_agree >= T_clear(建议0.15): return reject`
  3. `if p_agree   - p_reject >= T_clear: return agree`
  4. `else: return unknown`
- **置信度/阈值**：三题概率必须都经 `probability()` 校验（0~1 有限）。`T_takeover / T_clear` 为可调常量，可在 mock 前用样例集校准。
- **拿不准默认哪类**：**安全优先默认 `reject`（不执行）** 或退一步 `takeover`（把用户这句当新诉求、不执行旧动作）。**绝不默认 `agree` 执行**。理由：高危动作宁可"少做"，误执行不可逆动作的代价远高于多问一句。

### 4.2 判定依据速查（三类各自的信号）

| 意图 | 语言信号（举例如下但不仅限） | 判定权重 |
|---|---|---|
| **agree 同意** | 好的 / 确认 / 执行吧 / 打开 / 可以 / 行 / 弄吧 / 就这样 | 明确肯定词，正情绪 |
| **reject 拒绝** | 不要 / 取消 / 算了 / 别动 / 不 / 别 / 关掉吧 / 不需要 / 先别管 / 弹掉 / 等等再说 | 明确否定词，负情绪；"先别管/弹掉"类显式撤回归入此类 |
| **takeover 接管** | 先看看空调 / 导航去公司 / 播放音乐 / 问个别的 | 无任何回应确认的词素，直接提出新诉求 → 优先级最高 |

> 用 KEV 而非正则的原因：口语有省略、指代、反讽（"你敢关试试"）、否定重叠（"不是，我说的是另一辆"），正则/关键词易误判；KEV noul 打分能结合上下文 `confirm_ask` 判断"这句到底在回应确认还是在转移话题"。正则只可作为低成本的快速前筛或置信度参考，不作为最终裁决。

---

## 5. 确认请求的文案 & 交互（语音为主）

### 5.1 第一轮 agent 怎么开口（语音为主）+ 是否同时给卡片

- **语音一句安全的确认文案（示例模板）**：
  - "我准备关闭ESP，这会降低车身稳定控制，确认执行吗？"
  - "我准备打开引擎盖，打开后需要手动才能关闭，确认执行吗？"
  - "我准备开启防滑链模式，确认执行吗？"
  - 通用模板：**"我准备[操作]，这会[影响]，确认执行吗？"**
- **同时展示一张确认卡**（作为语音的对齐视觉参考，沿用 `ui/` 交互卡/时间线风格）：卡片上显示操作名、影响、两个主按钮（确认/取消）+ 提示"也可以直接说出来"。卡片不是唯一入口，语音才是主通道。

### 5.2 用户在第二轮的回复自由度

- **可以只说一个字**："好""嗯""行""不""别"；
- **可以说完整确认/拒绝**："好的，执行吧""别动那个""算了取消"；
- **可以转移话题/提新诉求**（触发 takeover）："先看看空调吧""导航去公司"——agent 应放弃当前待确认动作并转去处理新内容，不打断地去理解新诉求；
- 不需要用户理解"三类意图"，这正是 KEV 替 agent 读懂的地方。

### 5.3 超时没回复怎么办（建议）

- **默认不执行**（安全优先）。
- 产品建议：`CONFIRM_WAIT` 设一个超时（如 **3~5 秒无确认**），先**语音追问/重播一次**确认；若再超时（总计 ~8s），**按 reject 语义放弃本次确认并回到 LISTENING**，**绝不静默执行**。
- 确认可"轻声重复 + 给卡片倒计时"，让用户感知到在等自己。

---

## 6. 产品验证 / 验收

### 6.1 关键样例场景（语音，第二轮）→ 预期分支

| # | 第一轮动作 | 用户第二轮语音 | 预期 intent | 预期动作 |
|---|---|---|---|---|
| 1 | 关闭ESP | "好的，关吧" | agree | 执行 mock `car_drive_ESPSwitch` |
| 2 | 打开引擎盖 | "不要，别动" | reject | 放弃，问是否需要别的 |
| 3 | 关闭ESP | "你先别管这个" | reject | 放弃（显式撤回归 reject），不执行 |
| 4 | 打开引擎盖 | "先看看空调，调到24度" | takeover | 放弃开引擎盖，agent 转去执行空调设置 |
| 5 | 关闭ESP | "嗯……随便吧"（含糊） | unknown | 按 reject 不执行（安全优先），可追问一次 |
| 6 | 关闭ESP | （3s 无回应） | timeout | 重播一次；再超时按 reject 放弃，不执行 |
| 7 | 关闭ESP | "你敢关试试"（反讽/负情绪） | reject（负情绪词+KEV 上下文） | 放弃，不执行 |

### 6.2 怎么在这个 mock PoC 里验证

1. **接入现成 ASR**：用 `stream_asr`（`asr_stream.py`）做语音输入，产 `(t_ms,text,final)`；或复用 `ui/server.py /api/session/stream` 的时间线回放通道喂 wav/脚本 ASR。
2. **复用判停**：第二轮语音到达时用现有 `score_eot` 判定"说完没"，收敛后才进确认意图判定。
3. **mock 执行**：沿用 `ui/server.py /api/run/stream` 的 `mock_only` 执行时间线来展示第 N 轮"我确认执行 / 放弃了 / 转去处理新诉求"。
4. **渲染确认轮卡**：在 `ui/index.html` 时间线里加一个"确认轮"阶段卡，展示 `confirm_ask` 文案、`score_confirm_reply` 三问得分、裁决出的 `intent`、最终落地分支——让 PM 一眼看到 KEV 判对了没。
5. **判定闭环验证脚本**：跑 6.1 的用例集，统计三类判中率 + "拿不准默认 reject"的安全率；校准 `T_takeover / T_clear` 两个阈值。

---

## 附：本项目真实机制的落点（供立项参考，不改代码）

| 现有机制 | 本设计怎么接 |
|---|---|
| `score_eot`（`turn_eot.py:25`）判停三问 | 第二轮语音到达先判"说完没"，收敛后再进确认意图 |
| `kev_call` + `probability`（`core_pipeline_run.py:76-92`） | `score_confirm_reply` 用它发三问并校验概率 |
| `KEV_EOT_CLIENT` / `KEV_EOT_BASE`（`core_pipeline_run.py:40,50`） | 确认意图判定作为"意图判定类"，建议新 `KEV_CONFIRM_*` 独立连接池，不排在 242 工具打分后 |
| `CONFIRMED` / `LISTENING`（`turn_session.py`） | 新增 `CONFIRM_WAIT`，暂停 `_maybe_start_llm`，EOT 判停照常 |
| `select_tools` / `decide`（`core_pipeline_run.py`） | `decide` 返回 `tool_call` 且工具∈池 → 进确认；takeover 分支用它处理新诉求 |
| `ui/server.py /api/run/stream` | mock 执行时间线 + 确认轮卡渲染 |