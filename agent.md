# agent.md · 进仓先读（人 / Agent 通用）

> 一句话：本仓是「流式 ASR × KEV 判停」端到端 PoC 的**唯一代码与文档归档**；整理后的形态是
> **四模块分目录（`mind/ gate/ memory/ agent/`）+ 共享层 `shared/` + 装配层 `app/` + 页面 `ui/`**。
> **产品口径、拍板、验收判据、数字只在 wiki**；这里只说「代码在哪、规则在哪、别踩什么」。

- 项目知识库：`/Users/YUYSONG/Work/knowledge/agent-poc-llm-wiki/`（进仓先读 `[[边界]]` `[[忌讳]]` `[[当前主线]]`；
  四模块交接契约读 `wiki/四模块职责与接口契约.md`）
- 本仓交接契约（落点版）：`docs/interface-contract.md`
- 协作约定（派活/并行/铁律）：`AGENTS.md`（本仓根；`CLAUDE.md` 指回它）

---

## 1. 目录（5 分钟看懂）

| 目录 | 职责 | 关键文件 |
|---|---|---|
| `src/shared/` | 四模块共用的纯数据 / 纯函数 | `text.py`（文本归一）、`lexicon.py`（车控/闲聊/否定线索词表）、`contracts.py`（`SceneSnapshot` / `GateDecision` / `MindNotifier` 等形状与协议） |
| `src/mind/` | **读心**：只看画面 + 出字，产一句场景；不判停、不判要不要回答 | `mind_reading.py`（`MindReading` / `MindRegistry`）、`mind_llm.py`（真模型引擎与三选一档位） |
| `src/gate/` | **对话中枢**：判停 / 要不要回答 / 开闸 / 每席回合状态机 / 旁听台账 | `turn_eot.py`（判停三态）、`hub_accept.py`（要不要回答，临时线 0.5）、`hub_gate.py`（并发开闸）、`turn_session.py`（`TurnSession`）、旁听台账在 `src/overhear_ledger.py` |
| `src/memory/` | **记忆**：收住那一刻写、Agent 发起请求那一刻查；真写真查走 memos（没 key 自动退化成空实现） | `memos.py`（`MemosMemoryStore` / `NullMemoryStore` / `build_store`）、`interface.py`（协议） |
| `src/agent/` | **后台任务**：只在闸开后接手 | `orchestra.py`（`SessionBrain` 单入口决策）、`task_entity.py`（`TaskBoard`）、`relevance.py`（相关打分） |
| `src/asr/` | 语音前端：流式 ASR 与双席 | `asr_stream.py`（`ASRStream`）、`dual_seat.py`（`DualSeatSession`，每席一份 `TurnSession`） |
| `src/app/` | 装配：运行路径 / 上游注入 / 页面接线 | `bootstrap.py`（`install()` 注入上游根 + 本仓 src）、`assembly.py`（闸门/台账/读心接进会话） |
| `src/` 根 | **未归类的遗留**（不迁移、不删） | `core_pipeline_run.py`（选工具+决策，判停/中枢共用）、`confirm_gate.py` / `confirm_orchestrator.py`（高危二次确认）、`overhear_ledger.py`（旁听台账）、`assert_surface.py` / `streaming_prefix_eval.py` / `build_prefix_cases.py`（评测/断言）、`fixtures/`（前缀用例） |
| `ui/` | 调试页 + 测试台（**只调不改**） | `server.py`（8765 入口，`dual_stream()` / `/api/hub/*` / `/api/mind/*`）、`index.html`、`mind.html` |
| `tests/` | 离线回归，按模块分目录 | `gate/` `asr/` `mind/` `agent/` `app/` `legacy/`（一眼看出测哪块）；`tests/README.md` 有总表 |
| `cases/ assets/ evidence/ fixtures/ tools/ tmp/` | 语料 / 产物 / 证据 / 工具集 / 临时 | 不改、不搬（`evidence/` 由 `gate/turn_session.py` 写） |

**只答两件事的读法**：入口在哪 → `ui/server.py` 的 `dual_stream()` / `/api/hub/tick`；改规则 → 判停 `gate/turn_eot.py`、
要不要回答 `gate/hub_accept.py`、开闸 `gate/hub_gate.py`、状态机 `gate/turn_session.py`、读心 `mind/`、任务编排 `agent/`。

---

## 2. 边界

- `mind/ gate/ memory/ agent/` **只依赖 `shared/`**，互不 import；跨模块要用什么，先放进 `shared/`。
  （`asr/`、`app/`、`ui/` 不在该约束内；`asr/dual_seat.py` 每席建 `TurnSession` 是既有分工。）
- **规则不许顺手改**：判停线、要不要回答的规则、开闸线（现网临时 0.5）、确认窗口径都按 wiki；
  本仓整理不改行为。
- **未拍不落地**：缺拍板就写「待拍」，不自己补。当前未落地项见 §4。

---

## 3. 跑起来

```bash
# 离线回归（任选其一；全部纯 mock，不连外部服务）
PYTHONPATH=/Users/YUYSONG/Agents/navi-dual-system/.venv/lib/python3.13/site-packages \
  /opt/homebrew/bin/python3.13 -B tests/gate/test_hub_gate.py
PYTHONPATH=... /opt/homebrew/bin/python3.13 -B tests/gate/test_hub_accept.py
# 页面/测试台：8765（127.0.0.1）
PYTHONPATH=... CLOUD_ASR_*=... /opt/homebrew/bin/python3.13 -u -B ui/server.py
```

- 解释器是 `/opt/homebrew/bin/python3.13`（系统 `python3` 是 3.9，不可用）。
- 依赖只在上游 venv 里（`httpx2` / `flask` / `simple_websocket`），靠 `PYTHONPATH` 指过去。
- 上游依赖 `navi-dual-system` 用 `POC_UPSTREAM` 指过去（`app/bootstrap.py` 注入），**不复制进本仓**。
- 凭据只从进程环境读（`KEV_*` / `LITELLM_*` / `CLOUD_ASR_*` / `MEMOS_API_KEY`），**不落盘、不打印**；
  `MEMOS_API_KEY` 没设时按 `MEMOS_ENV_FILE`（默认指本机已有那份 `.env`）只读就地取，不复制。
- 8765 是现网演示进程：**只重启这一份**，不动测试台（`ui/` 只读）。

---

## 4. 未落地 / 待补（别当成已实现）

| 项 | 状态 | 去哪看 |
|---|---|---|
| 开闸线数值 | **未拍**，现网临时线 0.5（`HUB_ACCEPT_MIN_SCORE`，页面标「未拍板」） | wiki `四模块职责与接口契约` §6 |
| 读心 10 秒兜底 | wiki 已拍，**本仓未实现** | 同上 §4 |
| 读心带「最近几份场景」 | wiki 已拍，**数量待补**，本仓未带 | 同上 §6 第 2 条 |
| 记忆「记什么」 | 已接上 memos 真写真查；**抽成什么结构由 memos 决定**，收窄（只留人名/席位）待拍板 | 同上 §6 第 3 条 |
| 记忆抽取延迟 | 实测结构化记忆比写入慢十几到二十几秒（原始记录立刻可查） | 同上 §4 第 0 条 |
| Agent 按请求时刻取最新场景 | 已落（发起请求那一刻重取场景栏；闸门判据仍用开闸时刻那一帧） | 同上 §4 第 4 条 |

---

## 5. 维护口径

- 改**入口 / 分层 / 目录**的功能卡：同卡更新本文件与 `docs/interface-contract.md`；单个文件内部改动不更本文。
- 本文件与 wiki 冲突：**wiki 为准**，回来改准本文件（不两套口径并存）。
- 目录整理卡（2026-11-11）只搬文件与拆共享层，**不改判断规则**；搬动后离线回归与现网 8765 必须照旧。
