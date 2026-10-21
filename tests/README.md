# tests · 验收与探针

沿用旧文件名（报告与进度日志里引用了这些名字），只做了目录归置。全部脚本可独立运行，
路径通过 `src/bootstrap.py` 解析（`POC_UPSTREAM` 覆盖上游仓库位置）。

## 验收

| 脚本 | 验证内容 | 期望 |
|---|---|---|
| `poc-streaming-c1c4.py` | 多轮消费核心 + F1/F2：单/双段消费、投机作废、整句重发去重（已消费全文前缀）、迟到 EOT 空后缀 | 49/49 |
| `test_confirm_gate.py` | 对话式二次确认闸门：`judge_confirm_reply` 纯裁决（6 验收场+边界）、`score_confirm_reply` mock 稳定、CONFIRM_POOL/文案（离线，不连 KEV/LLM） | 45/45 |
| `poc_confirm_flow.py` | 二次确认端到端（纯本地 mock）：一轮选中池内工具 → 二轮 intent → 执行/放弃/接管；池外直接执行 | PASS |
| `poc_confirm_e2e.py` | **确认闸门长进 TurnSession 主循环的全链路端到端**：第一轮 decide 命中池内工具 → CONFIRM_WAIT → 发确认请求 → 二轮回 EOT 判停收敛 → score_confirm_reply(mock)+judge → 四分支（a-e 五条路径） | PASS |
| `confirm_cases.json` + `run_confirm_cases.py` | **交互层用例集（31 条二轮回→意图）**：L1 mock 启发式 / L2 judge 注入 / L3 真实 KEV 三层验收。L2 纯逻辑主证据须 31/31，L3 需隧道按 §（docs/test-design-confirmation-v2.md）判中率门槛 | L2 31/31，L1 26+5N/A |
| `probe_confirm_kev_real.py` | 真实 KEV 三问打分探针：验收表 6 样例落在哪侧（需 EOT 隧道 :8010，用于校准阈值） | 命中数打表 |
| `poc-streaming-s1s3-core-score.py` | S1–S3：core_score（242/17）两阶段召回、短名单家族、一轮一次决策、执行保持 mock（需 KEV + LLM） | 21/21 |
| `poc-streaming-a3.py PORT` | 脚本化 ASR 回放 → SSE：判停/状态迁移/四类脚本用例（需 UI 在跑） | 10/10 |
| `poc-streaming-a4.py PORT [--with-reuse]` | 停顿分段、工具复用、barge-in | 8/8（+reuse 11/11） |
| `poc-streaming-a5.py PORT` | 投机 LLM 被用户继续说推翻 → 作废与浪费记账 | 4/4 |
| `poc-streaming-mic-ws.py` | 真 wav 按 20ms 推 WebSocket → ASR → 判停 → 决策 → 用例落盘 | 6/6 |
| `poc-streaming-m1b.py [wav]` | 文件流式 ASR 端到端（2 partial + 1 final + 1 决策） | PASS |

注：`a3.py` 的 A3-1「无浪费 LLM」期望已因工具集更换改写为「浪费只来自 M4 shortlist 重发（≤1）」，
原因写在脚本注释里（core_score 两阶段短名单比旧的 14 工具慢，`allow_stale_shortlist` 会先按旧短名单发起决策）。

## 探针 / 历史实验

| 脚本 | 用途 |
|---|---|
| `poc-streaming-a1-baseline.py` / `-compare.py` | 改动前后核心链路 diff（证明 KEV 阶段一致、LLM 非确定性） |
| `poc-streaming-kev-isolation-probe.py` | KEV 并发/隔离对延迟影响 |
| `poc-streaming-diag.py` | 单回合事件诊断 |
| `poc-streaming-session-smoke.py` | TurnSession 最小冒烟 |
| `probes/*.sh` | M2/M3 调参扫描驱动（历史上写入 tmp，现已改为写 `docs/`） |
