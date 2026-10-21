# 测试设计与测试方法 · 对话式二次确认 + KEV 解读二轮回意图

对应功能：`src/confirm_gate.py`（裁决）+ `src/confirm_orchestrator.py`（编排）+ `src/turn_session.py` 的
`CONFIRM_WAIT` 状态机。本设计覆盖单元 / 交互 / 端到端 / 回归 / 真实 KEV / 执行方法六层。
配套可执行数据集：`tests/confirm_cases.json`（31 条交互层二轮回→意图用例）+ 运行器
`tests/run_confirm_cases.py`。

验收基线现状（本机实测）：
- L1 mock 启发式：26 判中 / 5 条 N/A（启发式无证据），0 fail。
- L2 judge 注入（纯逻辑主证据）：31/31 判中，0 fail —— 证明裁决逻辑把合理真实分映射到正确分支。
- L3 真实 KEV（隧道 :8010 可达）：单次 22/31，真实语义分在易错条存在分歧（见 §5 抖动与校准）。

---

## 1. 单元层（纯函数）测试

### 1.1 `judge_confirm_reply` 裁决逻辑（纯函数，喂构造分数即验）

已由 `tests/test_confirm_gate.py A 组` 覆盖 15 条，本设计建议扩充成一张**判定真值表**，逐分支+逐边界：

优先级（`docs/spec-kev-confirmation-v2.md` 注释 + 代码 `judge_confirm_reply`）：
1. `p_takeover >= 0.5`（常量 `TAKEOVER_THRESHOLD=0.5`）→ `takeover`
2. `p_reject - p_agree >= 0.15`（`CLEAR_MARGIN`）→ `reject`
3. `p_agree - p_reject >= 0.15` **且** `p_agree >= 0.75`（`AGREE_FLOOR`）→ `agree`
4. 否则 → `unknown`（调用方默认 reject，安全优先）

建议新增覆盖（不含在现有 15 条内或补强）：

| 用例 | 输入 (agree, reject, takeover) | 预期 | 说明 |
|---|---|---|---|
| T-1 takeover 恰好 =0.5 | (0.2,0.3,0.50) | takeover | 边界含等号，最高优先 |
| T-2 takeover 恰好 =0.4999 | (0.3,0.2001,0.4999) | reject | <0.5 失去接管优先，转 reject 差 |
| T-3 reject-agree 恰好 =0.15 | (0.30,0.45,0.25) | reject | 边界含等号 |
| T-4 reject-agree =0.149 | (0.31,0.459,0.231) | unknown | 差 0.149<0.15，落 unknown(safe)（agree 亦不达标） |
| T-5 agree-reject 恰好 =0.15 且 agree>=0.75 | (0.75,0.60,−?) | 见注 | 需要 agree+reject+takeover 和为 1，构造 (0.75,0.60) 不可能同时出现；真实约束下 agree 获胜差 ≥0.5。改用 (0.80,0.65) 同样+takeover=−0.45 非法 → 用合法归一化样本在 §1.1 注讲明。 |
| T-6 agree 恰好 =0.75（门槛含等号） | (0.75,0.20,0.05) | agree | 已覆盖(A13)，保留 |
| T-7 agree 恰好 <0.75(=0.749) | (0.749,0.20,0.051) | unknown | 护栏下拒（含糊即拒） |
| T-8 三问全 0.3 附近 | (0.33,0.33,0.34) | unknown | 平局→safe |
| T-9 takeover 关键但 agree 也高 | (0.85,0.05,0.10) | agree | 未达 0.5，不接管；由 agree 获胜 |
| T-10 takeover=0.55 且 agree=0.40 | (0.40,0.05,0.55) | takeover | 接管优先于 agree |
| T-11 takeover=0.3+reject 高 | (0.10,0.60,0.30) | reject | reject 差 0.5 |
| T-12 负数/超界输入 | (-0.1,0.5,…) | 应抛/防御 | 建议校验：judge 目前直接 float() 不校验，输入约定分数恒 [0,1]。测试断言:非法分数不静默判 agree。 |

注：`judge_confirm_reply` 三问分数恒被 `_mock_scores`/KEV 归一化和为 1，故 decide 层只需测合法归一化输入；
手工注入非法值时行为由调用方契约保证（见 §1.3 约定）。

### 1.2 `is_confirm_tool`（池内 ≠ 池外）

- 池内 8 工具全部 True：`car_control_Vehicle_HoodOpen`、`car_drive_ESPSwitch`、
  `car_drive_SnowChainModeSet`、`car_status_BrakeMaintenanceSet`、`car_drive_DriveModeSwitch`、
  `car_adas_control`、`energy_unlock_charge_cable`、`energy_stop_charging`。
- 池外真实工具 False（从 `fixtures/tools-core-score.yaml` 取值，与真实注册表一致）：
  `car_navi_to_destination`、`car_hav_temperature`、`music_recommend`、`car_seat_temperature`、
  `energy_query_charging_info`、`car_window_*`、`car_door_open` 等。
- 未知/不存在工具 False：`"fake_tool"`、`"car_drive_ESP"`、`None`、`""`。
- 边界：大小写敏感性（`"CAR_drive_ESPSwitch"` 应 False）；`tool:` 前缀（若传入 `tool:car_drive_ESPSwitch`
  应 False，因为 Pool key 无前缀——调用方在 `ready()` 里传的是去掉前缀后的裸名）。

### 1.3 `score_confirm_reply` mock 通道

- 确定性：同输入两次结果逐位相等。
- 合法性：三问和为 1(容差 1e-9)，各 ∈[0,1]，`mode == "mock"`。
- override 注入：`mock={"agree":0.9,"reject":0.05,"takeover":0.05}` → judge=agree；能精确进任一分支。
- override 非法输入（全 0、负数、非有限）→ 抛 `ValueError`（代码已实现）。
- 关键词映射单测：`_scored_reply_keywords` 计数正确（agree/reject/takeover 三组词命中数）。

---

## 2. 交互层 · 二轮回用户回复 → 预期意图（**重点**，见 `tests/confirm_cases.json`）

31 条真实中文口语用例，每条约含字段：`id / category / first_tool / user_reply /
expected_kev_intent / expected_action / expected_mock_intent(null=启发式无据) / kev_needed / score_override / notes`。

用例分布：

| 类别 | 条数 | 预期动作 | 关键语句 | 验收层 |
|---|---|---|---|---|
| 明确同意 agree | 8 | execute | 好的，关吧；确认；行，执行；打开吧；可以；嗯(单字)；嗯，好；关掉吧 | mock+KEV |
| 明确拒绝 reject | 7 | abandon | 不要，别动；别动；取消；算了；你先别管这个；你今天先别管这个；你敢关试试 | mock+KEV |
| 含糊/推责 unknown | 5 | abandon(safe) | 嗯…随便吧；你看着办；随便你；行吧；都行 | mock+KEV |
| 接管 takeover | 4 | reroute | 先看看空调调24度；导航去公司；播放音乐；我们先回家再说 | mock+KEV |
| 边界/易错 edge | 7 | 混合 | 不是我说的是关掉它；不是别关是要关；就那个吧；嗯，好；别关了先看一下行程；今天先不弄了；嗯嗯好的谢谢你 | 多数需 KEV |

设计要点（“二轮回意图判定”的核心矛盾）：
1. **“含糊即拒”护栏** — 含糊/推责语句 agree 可能最高或偏高，但绝对分 < `AGREE_FLOOR(0.75)`，必须
   落 `unknown`→safe-abandon，**绝不**当同意执行。用例 unknown-01~05 专测此护栏；`行吧/都行`（unknown-04/05）
   是 mock 启发式会误判 agree 的代表，标记 `kev_needed=true`。
2. **否定重叠/澄清** — “不是，我说的是关掉它”（edge-negation-01）句首“不是”是否定助词，真正意图是重申
   同意。mock 启发式把“不是+关掉”当 reject（语义误判），真实 KEV 需借 `confirm_ask` 上下文区分
   “否定句首纠正”与“否定整个请求”。edge-negation-02“不是别关，是要关”同理。
3. **单字肯定 vs 含糊** — “嗯”单字是明确同意(agree-06，正控防误伤)；而“嗯…随便吧”是含糊(unknown-01)。
   mock 对“嗯”判 unknown(无响亮关键词)而真实 KEV 判 agree(0.977)——必须真实隧道正控断言。
4. **多意图合读** — “别关了先看一下行程”(edge-multi-intent-01)：半放弃当前确认+半新诉求，裁决优先级应取
   更富信息的新诉求→takeover/reroute。
5. **叹词开头** — “嗯，好”(edge-interjection-01) 与 agree-06 区分：后接明确“好”消除歧义。

三层验收：
- L1 mock 启发式：`score_confirm_reply(mock=True)→judge` 对比 `expected_mock_intent`。可离线、确定。`null`
  表示启发式无证据(如跟单字否定重叠)，不参与硬通过。
- L2 judge 注入（**纯逻辑主证据**）：`score_confirm_reply(mock=score_override)→judge` 对比
  `expected_kev_intent`。用真实校准分样本验证裁决逻辑，不依赖打分通道，必须 100% 判中。
- L3 真实 KEV（需隧道）：`mock=False` 对比 `expected_kev_intent`，作为语义验收，按 §5 通过率门槛判定。

---

## 3. 端到端层（全链路 TurnSession）

以 `tests/poc_confirm_e2e.py` 的 5 条路径为基座，扩成完整用例矩阵。每条描述
`第一轮指令 → 二轮回(user_reply) → 目标工具 → 状态转移(CONFIRMED→CONFIRM_WAIT→分支) → 预期事件`。

| 用例 | 第一轮 | 二轮回 | 目标工具 | 状态转移 | 预期事件(key intent/action) |
|---|---|---|---|---|---|
| E2E-1 | 关闭ESP | 好的，关吧 | car_drive_ESPSwitch | CONFIRMED→CONFIRM_WAIT→LISTENING(agree) | `confirm_request`→`confirm_scores(agree)`→`confirm_resolve(intent=agree,action=execute)`→`confirm_exec` |
| E2E-2 | 关闭ESP | 不要，别动 | car_drive_ESPSwitch | …→LISTENING(reject) | `confirm_resolve(intent=reject,action=abandon)` |
| E2E-3 | 关闭ESP | 嗯…随便吧 | car_drive_ESPSwitch | …→LISTENING(unknown) | `confirm_resolve(intent=unknown,action=abandon)`(safe) |
| E2E-4 | 关闭ESP | 先看看空调，调24度 | →car_hav_temperature | …→takeover→reroute→LISTENING | `confirm_ruroute`→`reroute_tools`→`reroute_decision(tool=car_hav_temperature)`→`confirm_exec(池外直接)` |
| E2E-5 | 导航回家(池外) | (无二轮) | car_navi_to_destination | 不进 CONFIRM_WAIT | 无 `confirm_request`，直接 `llm_decision(tool_call)` |
| E2E-6 | 打开引擎盖 | 打开吧 | car_control_Vehicle_HoodOpen | …→LISTENING(agree) | `confirm_exec(tool=car_control_Vehicle_HoodOpen)` |
| E2E-7 | 停止充电 | 算了 | energy_stop_charging | …→LISTENING(reject) | `confirm_resolve(action=abandon)` |
| E2E-8 | 关闭辅助驾驶 | 你敢关试试 | car_adas_control | …→LISTENING(reject) | 负情绪不执行 |
| E2E-9 | 关闭ESP | (3s 无回应) | car_drive_ESPSwitch | CONFIRM_WAIT→超时→LISTENING | `confirm_resolve(intent=timeout,action=abandon)`(安全不执行) |
| E2E-10 | 关闭ESP | 播放音乐 | →music(music_recommend 类) | …→takeover→reroute | `reroute_decision` 不命中池内→直接执行(池外) |
| E2E-11 | 解锁充电枪 | 你看着办 | energy_unlock_charge_cable | …→LISTENING(unknown) | safe-abandon，不可逆动作绝不执行 |
| E2E-12 | 切换激进驾驶 | 嗯 | car_drive_DriveModeSwitch | …→LISTENING(agree) | 单字肯定正控，须真实 KEV |

说明：
- E2E-9 超时不执行已由 `turn_session.tick()` 的 `CONFIRM_WAIT` 超时分支实现（`think_wait_ms` 作为确认等待窗）。
- E2E-5 池外直接执行验证“确认不影响既有直接执行路径”。
- 断言建议：`assert` 关键事件 `intent/action`，参照 `poc_confirm_e2e.py` 的 `run_case` 驱动风格。

---

## 4. 回归层（不破坏既有功能）

确认改造叠加在 `TurnSession` 主循环上，不改变分段/消费/判停/工具打分。回归必须全绿的分组：

| 脚本 | 验证内容 | 通过标准 |
|---|---|---|
| `tests/poc-streaming-c1c4.py` | 多轮消费核心 + F1/F2：单/双段消费、投机作废、整句重发去重、迟到 EOT 空后缀 | 49/49 |
| `tests/test_confirm_gate.py` | 确认闸门纯函数（本功能自身） | 53/53 |
| `tests/poc_confirm_flow.py` | 确认全流程（mock） | PASS |
| `tests/poc_confirm_e2e.py` | 确认端到端 5 路径（mock） | PASS |
| `tests/poc-streaming-s1s3-core-score.py` | core_score 242/17 两阶段召回、短名单、一轮一决策 | 21/21（需 KEV+LLM） |
| `tests/run_confirm_cases.py`（见 §2） | 交互层用例集 | L2 31/31 |
| `tests/poc-streaming-a3.py`/`a4`/`a5`/mic-ws | 判停/状态迁移/投机/barge-in（需 UI/ASR 环境） | 按 README 各自标准 |

回归关注点：确认轮引入后，`llm_calls`/`llm_wasted`/`consumed` 计数在非确认路径应不变；判停时序不被确认挂起破坏；
`_consume_suffix` 正确封口二轮回（避免被当新指令再决策）。

---

## 5. 真实 KEV 验证方法与阈值校准（需隧道 :8010）

### 5.1 怎么跑探针
```bash
PY=/Users/YUYSONG/Work/tmp/jev-reopt-checkouts/dev-streaming-eot/.venv/bin/python
$PY -B tests/probe_confirm_kev_real.py          # 单次打表 9 样例
$PY -B tests/run_confirm_cases.py --mode kev    # 31 条三层，含 L3 真实分
```
前提：KEV EOT 隧道在 `:8010` 可达（`KEV_EOT_BASE` 可被 `env KEV_EOT_BASE=` 覆盖）；本机已实测可达。

### 5.2 判断 agree 抖动的合格线（非确定性的处理）
真实 KEV 对同一句多次打分有波动（尤其 agree 分）。方法：
- **多次运行采样**：对每个 `kev_needed=true` 或全部 agree 类样例连跑 N（建议 N≥5，隔 ~100ms 去相关）次，
  记录每次 `p_agree`。
- **中位数 vs 阈值**：用 `p_agree` 的**中位数**而非单值比对 `AGREE_FLOOR(0.75)`。单次振幅在内
  （如 |max−min| ≤ 0.10~0.15）视为可接受抖动；若跨阈值边界（0.75 上下乱跳），判定不可靠，需提高阈值或加“连续两次达标”再执行护栏。
- **判中率门槛**：L3 不合格线建议 **≥85% 命中**（31 条中 ≥26 判中）为“可发布”；<85% 先校准再复用。
  本机单次 22/31=71%（受非确定性与易错条共同影响）——不满足发布门槛，须校准（见下）。
- **分簇看**：不只看总数——agree/reject/takeover/unknown 各簇判中率分开看（例如 unknown 簇易被误判、edge 簇最难）。

### 5.3 阈值校准方法（当真实分与预期不相符）
1. 跑 31 条收集真实 `p_agree/p_reject/p_takeover` 样本（多次取中位数）。
2. 对每条人工标注“应然 intent”，与当前 judge 输出做混淆矩阵。
3. 若某簇系统性偏（如含糊条 P_agree 普遍 0.7~0.74 恰在门槛下，或拒绝条 P_agree 虚高）：
   - 调整 `AGREE_FLOOR`（方向：含糊误执行就调高，单字肯定被误伤就调低）。
   - 调整 `CLEAR_MARGIN`（负差判别过紧/过松）。
   - `TAKEOVER_THRESHOLD`（接管误伤拒绝条时调高；接管漏判时调低）。
4. 用 `score_override` 注入候选阈值，在 L2 层回放验证阈值改动不破坏纯逻辑基线（31/31）。
5. 校准基准放在 `docs/spec-kev-confirmation-v2.md`（真实样例 cluster 表），改动阈值需回归 §4 全绿。

---

## 6. 测试执行方法

### 6.1 环境与命令
Python 环境：系统 `python3` 缺 `httpx2`；**必须用** venv：
`/Users/YUYSONG/Work/tmp/jev-reopt-checkouts/dev-streaming-eot/.venv/bin/python`。上游仓库由
`src/bootstrap.py` 探测，`POC_UPSTREAM` 可覆盖。

```bash
PY=/Users/YUYSONG/Work/tmp/jev-reopt-checkouts/dev-streaming-eot/.venv/bin/python
cd /Users/YUYSONG/Work/poc-streaming-eot

# 交互层用例集（默认 L1+L2 离线）
$PY -B tests/run_confirm_cases.py
# 交互层含真实 KEV L3（需隧道）
$PY -B tests/run_confirm_cases.py --mode kev
# 只跑真实 KEV
$PY -B tests/run_confirm_cases.py --only kev

# 单元层（纯函数 + mock 稳定）
$PY -B tests/test_confirm_gate.py
# 全流程（mock）
$PY -B tests/poc_confirm_flow.py
# 端到端 5 路径（mock）
$PY -B tests/poc_confirm_e2e.py
# 回归多轮消费核心
$PY -B tests/poc-streaming-c1c4.py
# 真实 KEV 探针（需隧道）
$PY -B tests/probe_confirm_kev_real.py
```

mock vs 真实切换：`score_confirm_reply(mock=True)` / `mock=None`；`TurnConfirmGate(mock=True)` 默认 mock，
`mock=None` 走真实。CLI：`confirm_gate.py --reply '...' --tool car_drive_ESPSwitch --mock` 或 `--scores '{...}'`。

### 6.2 通过标准（每条 pass 判定）
| 层 | 判定 |
|---|---|
| L1 mock 启发式 | `expected_mock_intent` 非 null 且 judge==expected → pass；null → N/A（不算失败）；其余 fail |
| L2 judge 注入 | judge(score_override)==expected_kev_intent → pass；否则 fail（**主门槛，须 100%**） |
| L3 真实 KEV | judge(真实分)==expected_kev_intent → pass；按 §5.2 用多次中位数+判中率≥85% 定合格 |
| 单元纯函数 / 回归 | 各自断言全过即 pass（见脚本 return code） |

### 6.3 验收层归属总表
- 现在 mock 就能跑通（无需隧道）：L1 + L2 全部 31 条、单元层、poc_confirm_flow / e2e、元回归 c1c4。
- 需真实 KEV 隧道：L3（尤其 `kev_needed=true` 的 8 条 + agree-06 单字正控等）；`probe_confirm_kev_real.py`；
  `poc-streaming-s1s3-core-score.py`。
- 需真实 ASR(mic) 人工验收：`poc-streaming-mic-ws.py`、`poc-streaming-m3*`（真实语音流判定）；UI 确认轮卡
  渲染（`ui/server.py` `/api/session/stream` + `confirm_rounds`）。

---

## 附：本设计未改动 src/ 核心逻辑
仅为测试新增：`tests/confirm_cases.json`、`tests/run_confirm_cases.py`、本文档。未修改
`confirm_gate.py` / `confirm_orchestrator.py` / `turn_session.py` 任何实现。见 `tests/README.md` 更新。