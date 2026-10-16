# tests · 验收与探针

沿用旧文件名（报告与进度日志里引用了这些名字），只做了目录归置。全部脚本可独立运行，
路径通过 `src/bootstrap.py` 解析（`POC_UPSTREAM` 覆盖上游仓库位置）。

## 验收（需 UI 在 `PORT` 上运行）

| 脚本 | 验证内容 | 期望 |
|---|---|---|
| `poc-streaming-a3.py PORT` | 脚本化 ASR 回放 → SSE：判停/状态迁移/四类脚本用例 | 9/9 |
| `poc-streaming-a4.py PORT [--with-reuse]` | 停顿分段、工具复用、barge-in | 8/8（+reuse 11/11） |
| `poc-streaming-a5.py PORT` | 投机 LLM 被用户继续说推翻 → 作废与浪费记账 | 4/4 |
| `poc-streaming-mic-ws.py` | 真 wav 按 20ms 推 WebSocket → ASR → 判停 → 决策 → 用例落盘 | 6/6 |
| `poc-streaming-m1b.py [wav]` | 文件流式 ASR 端到端（2 partial + 1 final + 1 决策） | PASS |

## 探针 / 历史实验

| 脚本 | 用途 |
|---|---|
| `poc-streaming-a1-baseline.py` / `-compare.py` | 改动前后核心链路 diff（证明 KEV 阶段一致、LLM 非确定性） |
| `poc-streaming-kev-isolation-probe.py` | KEV 并发/隔离对延迟影响 |
| `poc-streaming-diag.py` | 单回合事件诊断 |
| `poc-streaming-session-smoke.py` | TurnSession 最小冒烟 |
| `probes/*.sh` | M2/M3 调参扫描驱动（历史上写入 tmp，现已改为写 `docs/`） |
