# Spec：用 `core_tool_schemas_score.json` **替换** PoC 全部工具定义

状态：用户确认「把现在的工具删掉，就用这个」（2026-10-16）
来源：`~/Downloads/core_tool_schemas_score.json`（242 tools / 17 domains）
目标仓：`Work/poc-streaming-eot`

## 1. 目标

PoC 只保留这一套细粒度车载工具。删除 / 停用现有 `navigation`（14）与 `multidomain`（200）工具集与 UI 选项。流式链路：ASR → KEV 选工具（两阶段）→ DeepSeek 决策 → 多轮消费，全部基于这 242 条。

不做：真车执行、abilities 合并版、改 KEV 服务、push（除非另说）。

## 2. 替换范围

| 项 | 动作 |
|---|---|
| `tool_set` | **只保留** `core_score`（或直接去掉 tool_set 参数，默认即此套）。UI 下拉删除 navigation/multidomain。 |
| 注册表 | 新增 PoC 自有 `fixtures/tools-core-score.yaml`（由 JSON 转换）；`select_tools` 只读它。 |
| 上游 `tools.yaml` / `eval/fixtures/tools-multidomain.yaml` | **不删上游文件**（只读依赖），但 PoC **不再 load / 不再引用**。 |
| `score_multidomain` 硬编码 `200/20` | 改为读 snapshot 实际 `tool_count` / `domain_count`（242/17）。 |
| 域描述 | 新文件 `fixtures/kev-domain-descriptions-core-score.json`（17 域）。 |
| 决策 | 仍 shortlist → DeepSeek；`top_k` 默认 3；阈值工具 0.5、域 0.4。 |
| 执行 | 仍 mock。 |
| 多轮消费 | 不改游标逻辑。 |

## 3. 转换规则

源：`core_tool_schemas_score.json`（OpenAI function 数组）。

每条 → registry 条目：

- `name` / `description` / `domain` / `parameters` 原样
- `binding: mock`
- `side_effect`：名称或描述匹配查询类（查|询|get|query|status|info|历史）→ `idempotent`，否则 `non_idempotent`
- `parameters.additionalProperties: false`（缺则补）

原件也拷入 `Work/poc-streaming-eot/fixtures/core_tool_schemas_score.json`。

## 4. 代码改动面

| 文件 | 改什么 |
|---|---|
| `src/core_pipeline_run.py` | 去掉 navigation/multidomain 分支；只走两阶段 KEV + core_score registry；解耦 200/20；`decide`/`run`/`CLI` 同步 |
| `src/turn_session.py` | `tool_set` 默认/唯一 `core_score`；校验收紧 |
| `ui/server.py` | 只接受 `core_score`（或忽略传入、强制） |
| `ui/index.html` | 删除双选项；展示「core_score · 242」即可 |
| 测试 | 更新依赖旧工具名的用例：C1–C4 / a3 等若写死 `navi_*` 或 tool_set=navigation，改为本套工具名或 mock select；新增 S1–S3 冒烟 |
| docs | `docs/spec-core-score-tools.md` 从本文件拷入 |

**兼容策略（PoC）**：旧用例里 `tool_set=navigation|multidomain` 入参 → 映射到 `core_score` 并打 log，避免整片测试立刻炸；但 UI/文档不再提供旧选项。更干净的做法是测例直接改名——优先改测例。

若 `validate_multidomain_arguments` 对实车参数过严：保留 JSON Schema 校验，引用校验降级 warn，记 progress。

## 5. 验收

配置：`tool_set=core_score`（唯一），`top_k=3`，投机可关。

| ID | 输入 | 期望 |
|---|---|---|
| S1 | `导航回家` | stage1 召回含 `navigation`；shortlist 为 `car_navi_*` 等；1 次决策 |
| S2 | `把空调开到 24 度` | 召回 `hvac`；shortlist 含 `car_hav_*` |
| S3 | `打开座椅加热` | 召回 `seat`；shortlist 含 seat/heating |
| S4 | 旧 C1–C4 / a3/a4/a5/M4/M1b | 全部改到新工具集后仍 PASS，或明确标「因工具名更换已改写期望」；不许静默挂 |
| S5 | UI | 无 navigation/multidomain 选项；文本/麦克风一轮可见 selected 为新工具名 |
| S6 | 负向 | 代码路径不再 `load_registry(.../tools.yaml)` 或 `tools-multidomain.yaml`（rg 验收） |

交付：

- `Work/tmp/poc-core-score-impl-progress.md`（末行 DONE）
- commit 本地即可，**不 push**
- 进度写清：改了哪些文件、S1–S6 数字、相对本 spec 的偏差

## 6. 非目标

- 不接真车 / 不 push / 不动 `dev-20261013` / 不改 git 历史
- 不打印密钥
- 不做 abilities 合并对照评测

## 7. 开放点（暂定，已按用户意图收口）

| # | 暂定 |
|---|---|
| 1 | 唯一 tool_set 名：`core_score` |
| 2 | 旧 14/200 **逻辑删除**（PoC 不再引用），上游文件物理保留 |
| 3 | 域描述：脚本自动生成 |
| 4 | 参数引用校验：过严则 warn |
| 5 | top_k=3，阈值 0.5 / 域 0.4 |

确认后开干：按本 spec 实现。
