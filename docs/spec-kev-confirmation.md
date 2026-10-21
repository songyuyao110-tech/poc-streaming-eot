# 产品/流程设计：KEV 驱动的"执行前二次确认"（execute-gate confirmation）

- 状态：设计稿（只写文档与流程，不改代码）
- 仓库：`/Users/YUYSONG/Work/poc-streaming-eot/`
- 关联只读依据：`src/turn_eot.py`（判停三问）、`src/core_pipeline_run.py`（两阶段 KEV 打分）、`src/turn_session.py`（回合状态机）、`fixtures/tools-core-score.yaml`（242 工具 / 17 域）、`fixtures/kev-domain-descriptions-core-score.json`、`ui/server.py` + `ui/index.html`（mock 执行展示）。
- 一句话：**在不破坏"选工具/判停/决策"现有链路的前提下，只对极少数高危/不可逆工具，在执行前用 KEV 判定是否需要用户确认。**

---

## 0. 术语与范围澄清（先对齐你的三个真实想法）

| 你的想法 | 落成本设计 |
|---|---|
| ① 不是所有工具都要二轮确认，只挑几个关键的 | 引入"候选确认池"：产品侧一个**精选名单**（默认 vehicle_control/adas 里的极端动作若干），池外工具**永不确认**。 |
| ② 用 KEV 模型判断该不该触发确认，而不是写死 | KEV 只决定"**池内工具这一次要不要真的确认**"（基于动作的不可逆性 / 参数是否触碰安全阈值）。**进入池与不入池是产品规则，不是 KEV 判的**。 |
| ③ 核心是"哪一步用 KEV 判断 + 判断什么 + 结果怎么决定" | 见 §Δ 判定模型推荐、§3 流程位置、§4 完整流程图、§5 判定规则。 |

> 明确区分两类决策：
> - **产品决策**（PM 定，写死/配置）：哪些工具进候选池、确认卡文案、超时默认拒绝、只做确认/拒绝两种按钮。
> - **依赖 KEV 的判定设计**（模型输出）：池内"这一次"动作是否足够高风险，需要确认。

---

## 1. 产品定义

### 1.1 一句话价值
在**只对极少数真正不可逆 / 影响行车安全的关键操作**上，用一次轻量二次确认挡住"误选/听错/参数致命"带来的高风险误操作；其余绝大多数工具保持现在的全自动、零打扰。

核心权衡：**降低误操作风险 ↔ 保持流畅度**。方案整体倾向于"宁可少打扰、只保命门"——因为 PoC 里 242 个工具绝大多数是低风险舒适性/信息查询，为它们弹确认卡会毁掉判停+投机 LLM 换来的低延迟体验。

### 1.2 候选确认池（产品范围）
**进入候选池（默认只选这 6 个，全部来自 vehicle_control/adas，且 YAML 描述里已自带"需二次确认/不可恢复"这类高风险语义）：**

| 工具 | 域 | side_effect | 为什么进池（数据里已有的风险信号） |
|---|---|---|---|
| `car_drive_SnowChainModeSet` | vehicle_control | non_idempotent | 描述原文"**需二次确认**"；防滑链模式影响牵引/驾驶 |
| `car_status_BrakeMaintenanceSet` | vehicle_control | idempotent | 描述原文"（仅电车，**需二次确认**）"；制动盘保养，直接关驾驶安全 |
| `car_control_Vehicle_HoodOpen` | vehicle_control | non_idempotent | 描述原文"打开前舱盖（**无法 UI 关闭，只能手动关闭**）"，不可逆性最强 |
| `car_adas_control` $\to$ `action=disable` | adas | non_idempotent | 关闭驾驶辅助，参数动作不同风险天差地别（见 §Δ 精判例子） |
| `car_drive_ESPSwitch` | vehicle_control | non_idempotent | ESP 车身稳定，关掉影响主动安全 |
| `car_drive_DriveModeSwitch`（切到 sport / offroad） | vehicle_control | non_idempotent | 驾驶模式切换影响操控，非普通舒适性设置 |

> **进池原则不是"side_effect=non_idempotent"**（那有 192 个，会毁掉体验）。
> 真正的产品规则是：**只挑"影响行车安全 或 物理上不可逆/需人工动作才能恢复"的那几个**。
> `side_effect` 只用来做**加分信号**，不是池的筛选键。

**明确不确认（池外，永不弹卡）：**
- 导航类（navigation 22 个）、天气（weather 1）、媒体/音乐/视频（media_*）、空调舒适性（hvac）、座椅/香氛/氛围灯（seat / fragrance / ambience_*）、电量查询（energy 查询类）、车灯/车窗/后备箱等常规舒适控制、应用打开等——**这些信息查询与低风险设置直接执行。**

### 1.3 用户最终效果（什么时候看到卡、什么时候看不到）

| 场景 | 是否出现确认卡 |
|---|---|
| "关闭 ESP"（池内 + KEV 判高风险） | ✅ 弹"执行前确认卡"，等确认/拒绝/超时 |
| "打开防滑链模式"（池内 + YAML 自带需二次确认，KEV 判 confirm） | ✅ 弹卡 |
| "查询胎压" / "播放周杰伦" / "导航回家" / "空调 24 度"（池外，或池内但 KEV 判低风险） | ❌ 永不弹卡，直接执行 |
| "打开前舱盖"（池内不可逆） | ✅ 弹卡（甚至可考虑产品层面强制，见 §5 备注） |

---

## Δ. KEV 在"二次确认"里的角色 —— 三种可选判定模型

新增一套**独立的 KEV 判定**（复用现有 `kev_call` + 每个问题一个 `Noul` 的机制，见 `turn_eot.py` 的 `score_eot` 与 `core_pipeline_run.py::score_tools` 目录），不篡改选工具/判停。

### 方案 A：KEV 作为"二次确认必要性打分"
新增三类 Noul 问题（仿判停三问），对**池内工具的具体动作和参数**打分：

```python
CONFIRM_QUESTIONS = {
  "irreversible":  "该动作是否不可逆（执行后无法通过 UI 撤销、需人工/机械动作才能恢复，或影响行车安全）？",
  "safety_critical":"该动作或传入参数是否触碰驾驶安全 / 车辆关键系统（制动、ESP、底盘、ADAS 关闭等）？",
  "costly":         "该动作是否有真实成本 / 后果（能耗、磨损、安全风险），误操作代价高？",
}
# 每项返回 Noul 概率；engine 分隔
confirm_score = w1*P(irreversible) + w2*P(safety_critical) + w3*P(costly)
# confirm_score >= CONFIRM_THRESHOLD(初值 0.6) → 触发确认
```
- **打分维度**：不可逆性 / 影响驾驶安全 / 用户可撤销性 / 成本。（"用户可撤销性"在 SaS：可撤销 → 反向抑制确认。）
- 优点：语义最贴合"该不该打扰用户"，维度可解释、可调权。缺点：又多一次 KEV 调用（延迟，仅对池内 6 个工具触发，可接受）。

### 方案 B：复用现有"工具评分"置信度
直接用两阶段打分里该工具的相关性分 `scores["tool:"+name]`：
- 分低（agent 拿不准，可能是误选）→ 触发确认；分高（意图明确）→ 放行。
- **但注意**：现有 Noul 问的是"工具是否相关"（见 upstream `b_questions`/`mixed_questions`：'判断完成整个原始任务是否可能需要下面的工具……只判断相关性，不填写参数，不执行'），**它衡量的不是风险**。一个"关闭 ESP"的相关性会很高，但方案 B 会据此放行——**恰好放走最该确认的**。方向反了，故不推荐作为主判据。

### 方案 C：规则打底 + KEV 精判（**推荐**）
- **规则层（产品定）**：候选池 = 上面 6 个精选工具。池外永不确认。
- **KEV 层**：只对池内这次调用判定"是否真的不可逆/参数是否触碰安全阈值"。典型精判例子：
  - `car_adas_control`：`action=disable` → 判 confirm；`action=enable` → 放行。
  - `car_drive_DriveModeSwitch`：切 `offroad/sport` → confirm；切 `comfort` → 放行。
  - 把动作/参数并入 KEV state，让模型"具体动作具体分析"，而不是工具一进池就永远确认。

### 三方案对比与推荐

| 维度 | A：必要性打分 | B：复用置信度 | C：规则+KEV 精判 |
|---|---|---|---|
| 判断对象 | 工具动作+参数 | 工具相关性分 | 规则圈池 + 动作/参数精判 |
| 占用 KEV 调用 | 每次池内调用 1 次 | 0（复用现有分） | 每次池内调用 1 次 |
| 可解释性 | 高（不可逆/安全/成本） | 中（且语义是"相关"非"风险"） | 高 |
| 是否会把"关闭 ESP"当成低风险放走 | 否 | **是（致命）** | 否 |
| 实现改动面 | 新增 CONFIRM_QUESTIONS | 最小 | 规则常量 + CONFIRM_QUESTIONS |
| 体验 | 池外零打扰，池内精确 | 池外也受波动影响 | 池外零打扰 + 池内精确 |

**推荐：方案 C（规则打底 + KEV 精判）。** 理由：
1. **产品上只挑几个** = 规则圈池，天然实现你的想法①，且保证"池外永不打扰"。
2. **用 KEV 判断"这次要不要"** = 精判动作/参数，避免"进池就永远确认"的粗糙，实现你的想法②③。
3. 避开方案 B 的语义错位（现有分是"相关性"不是"风险"，会把关闭 ESP 放行）。
4. 相比方案 A 纯打分，C 用规则先砍掉 99% 的候选，KEV 只服务 6 个工具，延迟与误判面都小得多。

> 若想更接近"纯 KEV 驱动"，可在 C 里把规则层弱化为"KEV 必要性打分 > 阈值才进池"（即 C 的 KEV 层同时承担圈池），但第一次 MVP 建议固定池，避免模型抖动把新工具带进带出。

---

## 3. 判断在流程中的位置（在哪一步做 KEV 判断）

**结论：新增一轮独立的"确认必要性 KEV 判定"，且它必须发生在 DeepSeek 选出具体工具之后、mock 执行之前。**

- **不复用判停那轮 KEV。** 判停（`score_eot`）回答的是"这一轮该不该停下来等用户"（complete/cut/wait + margin），它的粒度是整段话/回合级，不知道具体要执行哪个工具、参数是什么。
- **不复用选工具那轮打分。** 两阶段打分产出的是"相关性短名单"，不是"风险判定"，且发生在 DeepSeek 决策之前、不携带最终参数。
- **新增一轮在执行前。** DeepSeek 决策给出确定的一个 `tool_call`（工具 + 已解析的具体参数）之后，若该工具命中候选池，再单独让 KEV 判"这个具体动作该不该确认"。这轮判定是**纯叠加**，完全不改选工具 / 判停 / 决策的输入输出。

```
（判停阶段，已有）
KEV 判停三问 ──► MAYBE_DONE/CUT_OFF_WAIT/THINK_WAIT ──► CONFIRMED
                                                          │
（选工具阶段，已有）                                        │
两阶段 KEV 打分 ──► shortlist ──► DeepSeek 一次决策         │
                                    │                      │
                                    ▼                      │
                               decision=tool_call ─────────┘ （现流程直接 mock 执行）
                                    │
        ┌───────────────────────────┘
        │  新增：执行前确认门（execute-gate，新增，不在此路径上改动现有步骤）
        ▼
  ↳ 工具在候选池? ──否──► 直接 mock 执行（exec_start/step/done）
        │是
        ▼
  KEV 判定必要性 (CONFIRM_QUESTIONS)
        │
        ├─ 判 confirm ──► 弹确认卡 → 用户「确认/拒绝/超时」
        │
        └─ 判放行   ──► 直接 mock 执行
```

**与判停(EOT)的关系（不要混淆）：**
- 判停决定"**什么时候**给模型发决策"（回合节奏）。
- 执行前确认门决定"**选出的那个高危工具，敢不敢直接动**"（安全闸门）。
- 二者正交：判停确认后照样出决策；但若是高危动作，执行前再多一道卡。

---

## 4. 完整的用户交互流程图

```
用户开口 ──► ASR 流式文本
   │
   ├─ [KEV] 判停三问 complete/cut/wait + margin ──► MAYBE_DONE/…/CONFIRMED
   │
   ├─ [KEV] 两阶段打分 ──► shortlist(top-k)
   │
   ├─ DeepSeek 一次决策
   │      ├─ ask_user ──► 追问（无工具执行，不进确认门）
   │      ├─ done     ──► 收尾（不进确认门）
   │      └─ tool_call ──► 进入执行前确认门
   │
   ▼
执行前确认门（execute-gate，新增）
   │
   ├─ tool ∈ 候选池?                       ──否──► 直接执行
   │    │是
   │    ▼
   │ [KEV] CONFIRM_QUESTIONS 必要性打分
   │    ├─ 判 放行 (score < 阈值)
   │    │      └─► 直接执行 → exec_step → exec_done → done
   │    └─ 判 确认 (score ≥ 阈值)
   │          ▼
   │    弹出【执行前确认卡】
   │          │
   │          ├─ 用户点「确认」───────────► 直接执行 → exec_done → done
   │          ├─ 用户点「拒绝」───────────► 不执行（tool_rejected）
   │          │      └─ 走向：回传给决策层，可选择：
   │          │             a) agent 换一个更安全的替代工具重试
   │          │             b) 追问用户真实意图（改 ask_user 口径）
   │          │             c) 放弃（done(reason=用户拒绝)）
   │          │        MVP 建议：默认 b) 追问 + 收尾，不自动重试换工具（见 §6）
   │          └─ 超时（未操作，默认 N 秒）──► 默认【拒绝】，安全优先
   │
   └────► 所有分支最终都回到回合状态机继续听 / 收尾 turn_done

Mock 演示路径 vs 真实路径：完全一致的口径。
- 现在 mock 执行就是在 /api/run/stream 里 emit exec_start/exec_step/exec_done。
- 二次确认门作为新增的"执行前闸"，mock 与真实共用同一段登录逻辑：
  候选池命中 + KEV 判 confirm → 先发 confirmation_request，收到确认/拒绝后再发 exec_*。
  唯一区别是真实路径在确认后真正调用业务接口，mock 路径确认后照旧只打"mock 成功"。
```

---

## 5. 触发与不触发二轮确认的具体判断规则（伪代码/结构化）

```
INPUT: decision = {kind, tool, arguments}      # DeepSeek 已落地的单步决策
       CANDIDATE_POOL = {  # 产品规则，写死，来自精选名单
           "car_drive_SnowChainModeSet",
           "car_status_BrakeMaintenanceSet",
           "car_control_Vehicle_HoodOpen",
           "car_adas_control",
           "car_drive_ESPSwitch",
           "car_drive_DriveModeSwitch",
       }

if decision.kind != "tool_call":
    return DIRECT_EXECUTE          # ask_user / done 不经过确认门
if decision.tool not in CANDIDATE_POOL:
    return DIRECT_EXECUTE          # 池外工具：永不确认（导航/媒体/天气等）
# ↓ 下列分支只在"工具在候选池"时成立
key = CONFIRM_QUESTIONS(decision)  # 把 tool + arguments 并进 KEV state 做精判
score = w1*P(irreversible) + w2*P(safety_critical) + w3*P(costly)
if key < CONFIRM_THRESHOLD(初值 0.6):
    return DIRECT_EXECUTE          # 池内但这次动作/参数安全（如 enable ESP）
else:
    return REQUEST_CONFIRM         # 池内 + 高风险 → 弹确认卡
```

### 分支判定依据与阈值初值
- `CONFIRM_THRESHOLD`：**初值 0.6**（对合成的三问取加权和）。调参时看三条：误确认率（该放行的却弹卡）压到 ~0，漏确认率（该弹卡却没弹）压在可接受的极小值。凭现有工具数据，0.6 能让 `enable` / `comfort` 档明显低于，`disable` / `offroad` / 不可逆动作明显高于。
- 权重初值建议：`w_irreversible=0.4, w_safety_critical=0.4, w_costly=0.2`（安全与不可逆优先，成本其次）。
- 特殊备注：`car_control_Vehicle_HoodOpen`（"无法 UI 关闭"）可考虑产品层**强制确认**（KEV 分值外，单项不可逆=1 强制弹卡），不依赖阈值。

### 触发/不触发结果表（对齐 §1.3）

| 工具（池内） | 参数/动作 | KEV 判分 | 结果 |
|---|---|---|---|
| car_adas_control | action=disable | 高(safety) | ✅ 确认卡 |
| car_adas_control | action=enable | 低 | ❌ 直接执行 |
| car_drive_DriveModeSwitch | mode=sport | 高 | ✅ 确认卡 |
| car_drive_DriveModeSwitch | mode=comfort | 低 | ❌ 直接执行 |
| car_control_Vehicle_HoodOpen | 任意 | 高(不可逆) | ✅ 确认卡 |
| 池外任意（导航/媒体/查询） | — | —（不进池） | ❌ 永不确认 |

---

## 6. 产品上的确认卡片文案 & 交互

### 6.1 确认卡展示什么
- **工具名**（用户可懂的中文名，非 `car_adas_control` 这种内部名）。
- **将执行的动作 + 关键参数**（如"关闭车辆稳定系统 ESP"）。
- **影响提示**（安全/不可逆，来自 KEV 最高分项）。
- **风险等级**（高 / 中 / 低，来自三问合成）。

一句文案示例（中文，最终可以配风险图标）：
> 「将 **关闭 ESP 车身稳定系统**。这会影响急打方向时的车辆稳定与主动安全。确认执行吗？」

### 6.2 交互（MVP 只做两种）
- **确认** → 继续 mock/真实执行。
- **拒绝** → 不执行，事件 `tool_rejected`；随后（MVP 默认）追问用户真实意图 + 收尾，**不做自动换工具重试**（保 PoC 简单）。可留后门：agent 若判明"用户其实想要更安全的等价动作"，可改为再次决策换低风险工具（非 MVP）。
- ~~记住选择 / 此工具本次不再打扰~~：**MVP 不做**，进迭代方向（§7）。

### 6.3 超时默认策略
- **默认拒绝**（安全优先）：确认卡出现后 N 秒（建议初值 `confirm_timeout_ms=15000`）内无操作，按拒绝处理并 `done(reason=超时未确认)`，绝不默认执行一个高危动作。

---

## 7. 验证 / 验收(product) 与迭代方向

### 7.1 验收样例（三组场景）
| # | 场景 | 期望 |
|---|---|---|
| 1 | 该确认的安全操作："**关闭 ESP**" / "**打开防滑链模式**" / "**打开前舱盖**" | 弹出确认卡；点确认→照常 mock 执行；点拒绝→不执行 + 收尾 |
| 2 | 不该确认的查询/低风险："**有多少油**" / "**播放周杰伦的歌**" | 永不弹卡，直接执行（对 existing 短名单回归：select+decision 完全不变） |
| 3 | 候选池外的高频操作："**空调 24 度**" / "**座椅加热**" | 永不确认（即使这些是 non_idempotent，只因不进池）|

**回归红线**：对现有 `tests/poc-streaming-*.py` 与 `/api/run/stream` mock 执行输出（exec_start/step/done）做回归——确认门是**纯叠加**，非高危路径的事件流应与现状逐字节一致。

### 7.2 用什么样的插件/度量验证
- 用 `/api/run/stream` 跑一组用例，核对 `confirmation_request` / `tool_rejected` / `confirm_success` 事件是否按 §5 表出现。
- 度量：**误确认率（该放行却弹卡）、漏确认率（该弹卡却没弹）、确认门延迟（KEV 精判耗时）、拒绝后用户是否满意走向**。

### 7.3 后续迭代方向
1. **池扩展**：确认流程成熟后，从 6 个精选工具扩展到更多车辆高危工具（先加 `car_drive_RaiseSuspensionSwitch`、`car_status_TireTypeSet` 等），每次只加已验证过的。
2. **添加"记住选择"**：用户可对某个工具"记住我的选择"，一次确认后该动作 N 天内不再打扰（需产品权衡——高危安全项通常不建议永久记住）。
3. **规则层 KEV 化**：用方案 A 提到了——让 KEV 必要性分代替硬编码池，动态决定"进池"，减少维护名单成本。
4. **确认卡风险分级配色**：按 KEV 三问合成的高/中/低，展示不同强度 UI 与文案。

---

## 附录 · 本设计引用的真实机制（读码确认）
- 判停三问：`src/turn_eot.py::score_eot`，`KEV_EOT_BASE`（:8010），输出 `complete/cut/wait + margin`。→ 只决定回合节奏，**不用于确认门**。
- 两阶段打分：`src/core_pipeline_run.py::score_tools`，stage1 域（threshold 0.4）→ stage2 recalled 域内工具，`TOOL_THRESHOLD=0.5`，选 top-k=3；`select_tools` 产出 `scores/schemas/selected`。Noul 问的是"工具相关性"（upstream `b_questions`/`mixed_questions`），**衡量相关不是风险** —— 这是方案 B 被否的关键事实。
- 回合状态机：`src/turn_session.py` 五态 `LISTENING / MAYBE_DONE / CUT_OFF_WAIT / THINK_WAIT / CONFIRMED`；`CONFIRMED → _start_llm → _apply_llm`。确认门插在 `_apply_llm` 拿到 `decision` 之后、进 mock 执行之前。
- 前端/服务 mock 执行：`ui/server.py /api/run/stream` 现有 `emit(exec_start/exec_step/exec_done)`；确认门作为新增的 `confirmation_request`/`tool_rejected` 事件叠加在 exec_* 之前。
- 候选池：来自 `fixtures/tools-core-score.yaml`，真实工具名与 side_effect 已核对；`side_effect=non_idempotent` 有 192 个 → 不能直接当池的筛选键。

### 需要再确认的设计点
1. `mixed_questions` 的确切 Noul 措辞是否与上游 `b_questions` 完全一致（"只判断相关性"）——本设计据此支撑方案 B 的否定论断，建议实现前在运行时打一条日志确认。
2. `car_status_BrakeMaintenanceSet`/`car_drive_SnowChainModeSet` 描述里"需二次确认"是否已有产品语义背书，还是仅凭描述字符串（PoC 阶段两者皆可当信号）。
3. 确认超时时长与"是否允许记住选择"需 PM 拍板（本稿给了初值 15s、默认拒绝、MVP 不做记住）。
4. 拒绝后的走向 MVP 定为"追问 + 收尾"，是否要预留"agent 自动换低风险等价工具"开关。

DONE