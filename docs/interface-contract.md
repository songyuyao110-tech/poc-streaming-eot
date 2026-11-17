# 接口契约（仓库落点版）

本文件把 wiki 现行契约 `四模块职责与接口契约`（2026-11-11 14:16 拍板）落到本仓的**目录与文件**上。
**规则口径只认 wiki**；这里只说明「每条交接在本仓由谁进出、落在哪个文件」，不新增任何判断规则。

- 权威口径：`/Users/YUYSONG/Work/knowledge/agent-poc-llm-wiki/wiki/四模块职责与接口契约.md`
- 结构总览：仓库根 `agent.md`
- 本文件与 wiki 冲突时：**wiki 为准**，本仓改准（不两套口径并存）。

---

## 1. 四模块 ↔ 目录

| wiki 模块 | 本仓目录 | 谁在进出 |
|---|---|---|
| 读心 | `src/mind/`（`mind_reading.py` / `mind_llm.py`） | 进：画面帧、两路出字、中枢的「收住」通知；出：按时间追加的场景（`SceneSnapshot`） |
| 对话中枢 | `src/gate/`（`turn_eot.py` 判停、`hub_accept.py` 要不要回答、`hub_gate.py` 并发开闸、`turn_session.py` 每席回合状态机） | 进：每一版出字、读心最新场景；出：`GateDecision`（agent / context_only / wait） |
| 记忆 | `src/memory/`（`memos.py` 真实现 / `interface.py` 协议） | 进：收住那一刻的对话 + 场景；出：memos 查回来的可查询记忆 |
| Agent | `src/agent/`（`orchestra.py` 单入口决策、`task_entity.py` 任务台账、`relevance.py` 相关打分） | 进：闸开后的完整原话 + 当时记忆 + 发起请求那一刻最新场景 |

共享层 `src/shared/`：`text.py`（文本归一）、`lexicon.py`（线索词表）、`contracts.py`（模块间数据形状与协议）。
装配层 `src/app/`：`bootstrap.py`（运行路径/上游注入）、`assembly.py`（把闸门/台账/读心接进会话）。
页面 `ui/`（调试台）与 `src/asr/`（语音前端）不在四模块的「只依赖 shared」约束内。

---

## 2. 交接契约（逐条对应 wiki §1–§4）

1. **出字同时给读心和中枢**：同一份出字两路都拿，谁都不等谁（读心不判停，中枢不等读心）。
2. **读心按时间往场景流追加场景**：不判停、不判要不要回答；每次出场景带当前画面、两路已出到的字
   （「最近几份场景」是 wiki 已拍、本仓**未落地**的一项，见 §4）。
3. **中枢每一版出字并行做两件事**：「说完没说完」（判停三态）与「要不要回答」（`hub_accept`）；
   取景用**此刻场景流里已有的最新一份**，不等待本轮读心，**用到上一拍允许**。
4. **开闸只认收住那一刻同一版出字上的要不要回答**：要回答 → 进 Agent；不回答 → 只进上下文
   （仍进对话流和记忆）；还没说完 → 不开闸，等（判停三态挂窗不变）。
5. **记忆以收住时刻对齐场景**：中枢把这句收住那一刻（判停说完 / 静音窗收口）写一刀，`turn` 带这句完整
   原话 + **这一刻**的场景；话没收住不写；只进上下文的话也写（已落地：`gate/turn_session._memory_write_close`）。
6. **Agent 只在闸开才接手**：发起请求那一刻取当时最新场景（不冻结在开闸时刻），并带当时记忆
   （`AgentRequest`；「发起请求那一刻」的工程形态待补）。

**闸门侧与 Agent 侧取景不冲突**：闸门判据用锁字那一刻的场景；Agent 用发起请求那一刻的最新场景。

---

## 3. 本仓实现对照（整理后落点）

| 交接 | 落点（代码） | 现状 |
|---|---|---|
| 出字 → 读心 / 中枢 | `ui/server.py` 的 driver / `asr/dual_seat.py` → `TurnSession.on_asr`；读心侧 `MindReading.ingest` | 已落 |
| 收住 → 通知读心 | `TurnSession` 调 `self.mind.on_utterance_close(seat, why)`（`shared/contracts.MindNotifier`） | 已落 |
| 中枢取场景 | `TurnSession` 通过 `scene_fn`（装配层注入 `MindReading.scene_snapshot`）取最新一份 | 已落（读到的是缓存，不等待） |
| 开闸判定 | `gate/hub_gate.GateKeeper.gate()` → `GateDecision` | 已落（开闸线为现网临时线 0.5，未拍板） |
| 不回答只进上下文 | `TurnSession` 的 `route=context_only` 分支：进对话流/旁听台账，不进 Agent | 已落 |
| Agent 接手 | `agent/orchestra.SessionBrain` + `TaskBoard`，只在闸开后调用 | 已落 |
| 记忆写（收住那一刻） | `gate/turn_session._memory_write_close` → `memory/memos.py` 的 `/add/message` | 已落（两条路由都写；同一版字只写一刀） |
| 记忆查（发起请求那一刻） | `gate/turn_session._request_context` → `memory/memos.py` 的 `/search/memory` | 已落（同步、有上限，查不到就不带记忆栏） |

---

## 4. 未落地 / 待补（不代填，与 wiki §6 对齐）

0. **记忆「记什么」不由本仓控制**：送进去的是「原话 + 场景 + 席位标签」，抽成什么结构（人名 / 家属关系 /
   偏好 / 计划 / 原样记录）由 memos 自己决定；本仓不做「只留人名和席位」这类本地过滤。抽成结构化记忆比
   写入慢十几到二十几秒（实测），所以「刚写完立刻就能查回抽好的那条」不成立。
1. 读心 **10 秒兜底**（没变化也满 10 秒出一份）——wiki 已拍，本仓**未实现**（计时起点等细节待补）。
2. 读心出场景**带最近几份场景**——wiki 已拍「最近几份」，**具体几份待补**，本仓未带。
3. **记忆记什么**（收窄到人名/席位还是把原料交给它自己抽）——本仓按「交原料」落地，收窄待拍板。
4. **Agent 取景按请求时刻选最新**——已落（`_request_context` 在发起请求那一刻重取场景栏；闸门判据那一帧仍锁在开闸时刻，两处不串）。
5. **开闸线数值**——未拍，**维持现网临时线**（`HUB_ACCEPT_MIN_SCORE` 默认 0.5，页面照实标「未拍板」）。
   本仓**不改这条线**、也不据此改判停/要不要回答的规则。

---

## 5. 边界与禁止

- `mind/ gate/ memory/ agent/` **只依赖 `shared/`**，互不 import（`asr/`、`app/`、`ui/` 不在此约束）。
- 不改判断规则、不改开闸线、不落地未拍功能；结构性改动同卡更新根 `agent.md` 与本文件。
- 凭据只从环境变量读（`KEV_*` / `LITELLM_*` / `CLOUD_ASR_*`），不落盘、不打印。
