# 验收报告 · 对话式二次确认 + KEV 解读第二轮回复意图（POC 正式验收）

- 项目：`poc-streaming-eot`（Agent POC）
- 被测功能：对话式二次确认闸门 + CONFIRM_WAIT 状态机 + 编排层 + 确认轮 UI
- 被测文件：`src/confirm_gate.py`、`src/turn_session.py`、`src/confirm_orchestrator.py`
- 测试基线：`docs/test-design-confirmation-v2.md`（六层设计）+ `tests/confirm_cases.json`（31 条）+ `tests/run_confirm_cases.py`
- 验收人/时间：POC 验收代理 · 本报告基于本机实测
- Python 环境：`/Users/YUYSONG/Work/tmp/jev-reopt-checkouts/dev-streaming-eot/.venv/bin/python`（3.12.13）

---

## 验收结论

**有条件 PASS（CONDITIONAL PASS）**

功能主体（确认闸门纯逻辑、CONFIRM_WAIT 状态机、编排、端到端流程、回归）全部达标；但**真实 KEV 语义判中率未达到设计稿 ≥85% 的发布门槛，且存在 2 处“含糊被误判为同意而会执行”的护栏泄漏风险**，若涉及高危/不可逆工具（如关闭 ADAS、解锁充电枪）存在误执行隐患。建议修复后或经产品明确接受风险后 Publish。

---

## 一、各层用例跑的结果

| 层 / 脚本 | 结果 | 状态 |
|---|---|---|
| **单元层** `tests/test_confirm_gate.py` | 53 项全过（A 组裁决 16 + B 组 mock 13 + C 组池/文案 24） | ✅ 通过（内容） |
| **交互层 L1** `run_confirm_cases.py` | pass=26 / skip-N/A=5（启发式无据）/ fail=**0** | ✅ 通过 |
| **交互层 L2（纯逻辑主证据）** `run_confirm_cases.py` | pass=**31/31** / fail=0 | ✅ 通过（100%，主门槛） |
| **端到端** `poc_confirm_e2e.py` | 5 路径全部 PASS（agree/direct/reject/takeover/unknown） | ✅ 通过 |
| **全流程** `poc_confirm_flow.py` | PASS（6 分支 + 池外直行 + 安全默认） | ✅ 通过 |
| **回归** `poc-streaming-c1c4.py` | C1–C5/F2 共 **49/49** | ✅ 通过 |
| **回归** `poc-streaming-session-smoke.py` | smoke 全流程跑通（含 confirm_silence 状态迁移） | ✅ 通过 |
| **真实 KEV L3**（隧道 :8010 可达）`--mode kev` | 无上下文：5 次连跑均 **pass=22/31（71%）** | ⚠️ 低于发布门槛 |
| **真实 KEV L3 + 生产 confirm_ask 上下文**（本验收补充） | pass=**25/31（81%）**，多次中位数判定，**无抖动** | ⚠️ 仍低于 85% |

> 说明：设计稿 §5.2 设定 L3 合格线 **≥85%（31 条 ≥26 判中）**。实测两类口径均未达标。

---

## 二、真实 KEV 判中率与“上下文依赖”关键发现

本验收发现 runner 的 L3 与真实生产路径存在一处**上下文偏差**，直接影响判中率口径：

- **runner 的 L3**（`run_confirm_cases.run_layer_kev`）调用 `score_confirm_reply(case["user_reply"], mock=False)`，**未传 `confirm_ask`**。
- **生产路径**（`confirm_orchestrator` / `turn_session`）始终通过 `confirm_ask_for(tool)` 传入第一轮确认文案。

将 confirm_ask 上下文补上后复测（等价的真实打分），判中率由 71%→**81%**，且个别“易错条”（`agree-08 '关掉吧'`、`edge-negation-02`、`takeover-04`）由 FAIL 转 ok。说明：
1. **非确定性 / agree 抖动**：本次隧道实测 3–5 连跑三问分数逐位一致，**未观察到设计稿所述的跨调用 agree 抖动**（本机隧道这部分是确定的），故无需多次取中位数；判中差异全部来自语义本身而非采样。
2. **runner 的无上下文 L3 低估了生产表现**，但仍未达 85%。runner 本身建议补充 `confirm_ask` 以对齐生产口径（属测试工具缺陷，非 src 缺陷）。

---

## 三、关键用例明细（代表性输出）

真实 KEV 三问分数（`{agree,reject,takeover}`，production-context，多次一致）：

### 判中正确（代表）
| 用例 | 回复 | 预期→实判 | 真实分数 | 说明 |
|---|---|---|---|---|
| agree-01 | 好的，关吧 | agree→**agree** | (0.975,0.234,0.051) | 明确同意，判中 |
| agree-06 | 嗯（单字） | agree→**agree** | (0.983,0.070,0.032) | 单字正控成功，未误伤为 unknown/agree 之外 |
| reject-01 | 不要，别动 | reject→**reject** | (0.018,0.915,0.182) | 明确拒绝，判中 |
| reject-07 | 你敢关试试 | reject→**reject** | (0.028,0.779,0.319) | 负情绪不执行 |
| unknown-01 | 嗯…随便吧 | unknown→**unknown** | (0.581,0.156,0.247) | 含糊即拒护栏生效（<0.75） |
| unknown-05 | 都行 | unknown→**unknown** | (0.488,0.136,0.240) | 两可，未误执 |
| takeover-01 | 先看看空调调24度 | takeover→**takeover** | (0.048,0.449,0.936) | 接管优先 |
| takeover-03 | 播放音乐 | takeover→**takeover** | (0.128,0.173,0.957) | 池外接管→直接执行 |
| edge-politeness-01 | 嗯嗯好的谢谢你 | agree→**agree** | (0.949,0.290,0.111) | 礼貌性确认判中 |
| edge-followup-01 | 今天先不弄了 | reject→**reject** | (0.012,0.918,0.421) | 撤回复核判中 |

### 判错 / 护栏风险（详见缺陷章节）
`行吧`→agree(0.946)、`就那个吧`→agree(0.867)、`不是我说的是关掉它`→unknown、`别关了先看一下行程`→reject、`你先别管这个`/`你今天先别管这个`→takeover（产品认可）。

---

## 四、8 条 `kev_needed=true` 用例重点核对

| 用例 | 回复 | 预期 | 真实 KEV（有上下文） | 护栏判定 |
|---|---|---|---|---|
| agree-06 | 嗯 | agree | **agree**（0.983） | ✅ 单字肯定未被误伤 |
| **agree-08** | 关掉吧 | agree | **agree**（0.967，无上下文时 unknown） | ⚠️ 上下文敏感；生产 OK |
| **unknown-04** | 行吧 | unknown | **agree**（0.946）→ **会执行** | ❌ **护栏泄漏**（见 BUG-1） |
| unknown-05 | 都行 | unknown | **unknown**（0.488） | ✅ 正确处理 |
| **edge-negation-01** | 不是我说的是关掉它 | agree | **unknown** → 放弃（真同意被弃） | ⚠️ 可用性损失，安全方向（见 BUG-3） |
| edge-negation-02 | 不是别关是要关 | agree | **agree**（0.865） | ✅ 双重否定判中 |
| **edge-multi-intent-01** | 别关了先看一下行程 | takeover | **reject** → 未接管新诉求 | ⚠️ 行程诉求被丢弃（见 R-2） |
| edge-followup-01 | 今天先不弄了 | reject | **reject**（0.918） | ✅ 判中 |

**护栏健康度小结**：含糊/否定**未被误执行**的机制整体正确（`都行/随便吧/嗯…` 都正确落 unknown）；但 `行吧` 与 `就那个吧` 两处真实 KEV 高置信判 agree 穿透了 `AGREE_FLOOR=0.75` 护栏，构成真实误执行入口。

---

## 五、发现的缺陷 / Bug / 风险点

### BUG-1（护栏泄漏 · 优先级最高）：`行吧` 被真实 KEV 判为 agree 会执行
- **现象**：`unknown-04 '行吧'`（first_tool=car_adas_control，即关闭 ADAS 等高危动作）真实 KEV（有上下文）三问=(0.946,0.134,0.059)，`agree-reject=0.812 ≥ 0.15` 且 `agree=0.946 ≥ AGREE_FLOOR=0.75` → 判 **agree** → 调用方执行。
- **复现**：`score_confirm_reply('行吧', confirm_ask_for('car_adas_control'), mock=None)` → judge=agree。
- **影响**：`带'吧'的敷衍让步句'行吧'`被当成明确同意执行，**对关闭 ADAS / 解锁充电枪 / 停止充电等不可逆/高危动作是误执行入口**，违背“含糊即拒”设计意图。设计稿 notes 亦注明“若 KEV 也判 agree 需产品确认是否接受该风险”。
- **方向**：抬高 `AGREE_FLOOR`（如 0.90）会误伤单字 `嗯`(0.98) 与 `好的关吧`(0.975)，需在可区分句型上校准，或对“吧”字收尾的肯定句增加二次置信门槛 / “连续两次达标”护栏。

### BUG-2（护栏泄漏）：`就那个吧`（指代省略）被真实 KEV 判 agree 会执行
- **现象**：`edge-pronoun-01 '就那个吧'`（first_tool=car_control_Vehicle_HoodOpen，打开引擎盖）真实得分=(0.867,0.195,0.175)→ judge=agree → 执行“打开引擎盖”。预期为 unknown/safe-abandon（指代对象不明，语义含糊）。
- **复现**：对 `'就那个吧'` 用 `confirm_ask_for('car_control_Vehicle_HoodOpen')` 打真实分即得。
- **影响**：无明确指代的含糊答复被当真同意执行，危险方向同 BUG-1。

### BUG-3（可用性/安全方向取舍）：`不是我说的是关掉它` 被判 unknown 放弃
- **现象**：`edge-negation-01` 真实分=(0.715,0.437,0.262)，agree 未达 0.75 → unknown → safe-abandon。这是“否定句首纠正、真正重申同意”的真实场景，用户确已同意 `关闭ESP`，但被放弃执行。
- **影响**：方向安全（**不会误执行**），但**真实同意的可用性受损**（用户要执行却未执行）。属“宁可错过不可误伤”的接受度权衡；EGR 门槛与靠 confirm_ask 区分“句首否定纠正”仍未解决此条（0.715 距 0.75 很近，微调阈值可含入）。

### R-1（测试工具缺陷，非 src）：`test_confirm_gate.py` 退出码反了
- **现象**：53 项全过，但脚本 `sys.exit(0 if ok else 1)` 中 `ok=main()` 返回 `0 if PASS>=3 else 1`，即 PASS 足够时 `ok=0`，`0 if 0 else 1=1` → **总是以退出码 1 结束**（即便全过）。
- **影响**：CI/脚本按退出码判断会误判失败。仅供参考，不影响判定内容（53 项明确全过）。

### R-2（runner 口径偏差，非功能缺陷）：L3 未传 `confirm_ask` 低估判中率
- `run_confirm_cases.run_layer_kev` 未传 `confirm_ask`，与生产路径（始终传）不一致，导致 `agree-08/'关掉吧'`、`edge-negation-02`、`takeover-04` 无上下文跑到与生产不同的结果（71% vs 81%）。建议 runner 补充上下文以对齐生产口径。

### R-3（已获产品认可，记录不判缺陷）：`你先别管这个` / `你今天先别管这个` → takeover
- 真实 KEV 判 takeover（takeover 分 0.906/0.862 ≥ 0.5）。设计稿与 notes 注明“先别管”可理解为转去处理别的事，产品已认可 takeover，不计为缺陷。

### R-4：真实 KEV 无 agree 抖动（本次隧道）
- 设计稿假设“agree 偶发抖动”，本次 3–5 连跑分数逐位一致，未观察到抖动。对本次验收是好消息（判中差异可完全归因于语义），但说明**该隧道为确定性打分**，与设计稿假设不同，多次取中位数的抗抖策略本机未得到需要发挥的场景。

---

## 六、未验证项（明确列出）

- **真实 ASR（mic / WebSocket ASR）人工验收**：未跑 `poc-streaming-mic-ws.py`、`poc-streaming-m3*`（真实语音流判停/投机/barge-in），需真实麦克风与 UI 环境。
- **浏览器 UI 人工验收**：确认轮卡片渲染（`ui/server.py /api/session/stream` + `confirm_rounds`）未人工点验；仅验证了后端状态机事件链（`confirm_request → confirm_scores → confirm_resolve → confirm_exec`）。
- **超额/长超时确认**：E2E-9（3s 无回应超时->abandon）依赖 `turn_session.tick()` 超时窗，本次未人工驱动真实超时倒计时验证（c1c4/flow 覆盖了状态转移逻辑，但超时秒级计时为人工/集成项）。
- **更大的真实语义样本**：31 条为设计集，未扩展随机中文口语样本做泛化验证。

---

## 七、建议下一步

1. **优先处理护栏泄漏（BUG-1/BUG-2）**：对 `行吧/就那个吧` 类“口头让步/指代省略”在真实 KEV 高置信 agree 时加二次门槛（提升 `AGREE_FLOOR` 或“连续两次 agree 达标再执行”），并在 `src/confirm_gate.py` 确定性回归（L2 31/31 保持）+ L3 重测；**高危/不可逆工具（ADAS关闭、解锁充电枪、停止充电）建议强制从严**（如按工具类别提高门槛）。
2. **校准阈值并回放**：按设计稿 §5.3 用本次真实样本（尤其 0.7–0.75 区间的 `不是我说的是关掉它(0.715)`）做混淆矩阵，微调 `AGREE_FLOOR/CLEAR_MARGIN`，在 L2 回放确认不破坏纯逻辑基线，再跑 L3 看能否到 85%。
3. **修正测试工具**：补 runner 的 `confirm_ask` 上下文（R-2）；修正 `test_confirm_gate.py` 退出码（R-1）。
4. **重跑发布门槛**：修复后用 `--mode kev`（补上下文后）目标命中 ≥26/31，并分簇看（agreed/reject/unknown/takeover 各自判中率），尤其 unknown 簇与 edge 簇。
5. **补人工项**：真实 ASR 语音、浏览器确认轮 UI、超时倒计时人工验证后再做最终发布结论。

---

## 附：临时产物
- `tmp/probe_fails_scores.py`：崩溃用例真实分数 dump。
- `tmp/probe_l3_with_context.py`：生产上下文 L3 复测。
- `tmp/kev/run3.txt`：runner L3（无上下文）3 连跑原始输出。
- `tmp/kev/` 下保留各次运行明细。

> 说明：本次为验收，未改动任何 `src/` 被测逻辑；除 `tests/README.md`、`src/turn_session.py`、`ui/*` 为功能改造自身的既有 git 变更外，被我写出的文件仅位于 `tmp/` 下。