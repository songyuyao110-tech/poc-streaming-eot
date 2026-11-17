# AGENTS.md · poc-streaming-eot 仓库协作约定

> 本仓是「流式 ASR × KEV 判停」端到端 PoC 的唯一代码与文档归档。Agent 在本仓按「一功能一张卡、多功能并行 worktree、不 push、不代拍」干活，每张卡端到端打穿到可验完成态。

## 先读（项目上下文）

- 项目知识库：`/Users/YUYSONG/Work/knowledge/agent-poc-llm-wiki/`。派活卡只带 wiki 路径指针，约束以 wiki 页为准；进仓先读 `[[边界]]` `[[忌讳]]` `[[当前主线]]`。
- 上游依赖 `navi-dual-system`（`POC_UPSTREAM` 环境变量指过去）；上游代码不复制进本仓，也不读上游 `tools.yaml`。

## 代码地图（5 分钟，新卡冷启动动手用）

> 只答两件事：**入口在哪、摸哪个文件**。产品口径 / 拍板 / 验收判据 / 数字一律去 wiki（`wiki/index.md` → `[[边界]]` `[[忌讳]]` `[[当前主线]]`），不在这里复述方案细则、不当验收清单。
> 维护口径：**改入口 / 分层的功能卡同卡维护本节**；只有**结构变**（加/删/挪模块、换入口、换分层）才更新，单个文件内部改动不更地图。

产品逻辑一条链：**前台两席听 + 判停 → 后台任务排队**。

| 产品层 | 落点文件 | 一句话 |
|---|---|---|
| 双席入口 | `ui/server.py`（`POST /api/session/dual/stream` → `dual_stream()`） | 两路 ASR 事件喂 `DualSeatSession`；`mock` 决定离线/真链；挂 `brain` 才走 C2 编排 |
| 双席编排（不串席） | `src/dual_seat.py`（`DualSeatSession`） | 单写者队列 `on_asr/on_vad → tick()` FIFO 派两席；每席一份 `TurnSession`；`cross_cancel/cross_reset` 归因计数 |
| 会话脑（单一决策入口） | `src/orchestra.py`（`SessionBrain`） | C2 起「做决定」收进一个入口；IU/AU 台账 + 指代（「我也要」）+ 归并窗 + 对账 |
| 任务台账（后台排队） | `src/task_entity.py`（`TaskBoard`） | 一条用户请求 = 一条任务；候选只含运行中；先来后到 FIFO；相关打分接口位 |
| 相关打分（真调用） | `src/relevance.py`（`build_relevance_fn`） | 新话 vs 在跑任务，用判停那个模型（8009）打相关分 → 命中并进 / 都不相关新开 |
| 旁听台账（不接的原话） | `src/overhear_ledger.py`（`OverhearLedger`） | 「说完但不接」的原话按席留最近 3 句、贴「不是对车说的」、跨席可见；只在开闸那轮由中枢拼两栏带进 Agent |
| 每席回合状态机 | `src/turn_session.py`（`TurnSession`） | 判停 → 选工具 → 投机 LLM → 高危二次确认 → 取消/落地；锚点/版本/消费游标本席私有 |
| 判停调用 | `src/turn_eot.py`（`score_eot`） | 三态 complete/cut/wait + 双阈值（0.80/0.70），`POST 8009 /v1/systemone` |
| 选工具 + 一次决策 | `src/core_pipeline_run.py`（`select_tools` / `decide`） | 两阶段 KEV 短名单 → 一次决策；唯一工具集 core_score（242/17） |
| 高危二次确认 | `src/confirm_gate.py` + `src/confirm_orchestrator.py` | 8 池高危工具先确认再执行，叠加在 TurnSession 之上 |
| 路径装配 | `src/bootstrap.py` | `POC_UPSTREAM` 指上游；上游代码不复制、不读上游 `tools.yaml` |

**先读顺序（约 5 分钟，读完即可动手）**：

1. `src/dual_seat.py` 头部 docstring —— 双席怎么隔离、`brain` 可选开关（C1/C2 切换点）。
2. `src/orchestra.py` 头部 docstring —— 三层：席 D/P → `SessionBrain.decide` → `TaskBoard`。
3. `src/task_entity.py` 头部 docstring —— 任务 = IU/AU 上一层容器，本模块只做三件事。
4. `src/turn_session.py` 头部 docstring + `TurnConfig` —— 状态机 + 判停策略/阈值。
5. 一个驱动样例 `tests/poc-dual-asr-c2.py` —— 怎么挂 `brain` + 假判停/选工具/决策跑通一条回放。

再往下：改判停/选工具/决策 → `turn_eot.py` / `core_pipeline_run.py`；改确认 → `confirm_gate.py`；改入口/UI → `ui/server.py` 的 `dual_stream()`。

**与 wiki 分工（别串）**：

- **产品拍板 / 口径 / 判据 / 数字** → 只读 wiki。派活卡只带 wiki 路径指针；别把 wiki 方案细则抄进本仓当验收清单。
- **怎么并行 / 摸哪个文件 / 入口在哪** → 本仓 `AGENTS.md`（本节 + 下面并行规则）。

## 并行派活规则（已拍板，产品口径）

1. **一功能一张 DSH 卡**，端到端打穿到可验完成态（有可复现的验收判据；离线回归全绿，真实服务路径未验的要写明）。
2. **多功能并行**：git worktree（或多隔离工作区）各开一棵树，互不抢同一工作树；每张卡只在自己那棵树上干活。
3. **单树强串行不做默认**；前后端分卡仅契约钉死且文件不重叠时例外，否则回到一张卡 / 一棵树。
4. **分流 / 执行 / 验证三角**，以后叠在上述默认之上。
5. **不 push（除非用户另说）；不代拍产品未决**——缺拍板就写「待拍」，不自己补。

## 本仓铁律

- 仓库里没有任何密钥；凭据只从进程环境变量读（`KEV_BASE` / `LITELLM_*` / `CLOUD_ASR_*`），不落盘、不打印。
- 不连外部服务时纯 mock 可跑离线回归；真实服务（KEV / LLM / ASR）路径要单独标「未验」。
- 项目知识库不在本仓 git 内，别把 `knowledge/` 塞进来。
