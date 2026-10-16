# poc-streaming-eot

KEV 两阶段选工具 + 流式判停（EOT）的端到端 PoC：**ASR 实时更新 → KEV 判停三问 / 工具短名单 → 一次 DeepSeek 决策**，
唯一工具集 `core_score`（242 tools / 17 domains，两阶段 KEV：先域后工具），
含脚本回放、真实麦克风（火山豆包流式 ASR）、以及逐事件的延迟时间线 UI。

本仓库是这条 PoC 线的**唯一代码与文档归档**，与上游产品仓库、以及 `jev-research/poc-ui` 实验目录分离。

## 目录

```
docs/                全部文字资产（输入 spec、设计稿、进度、报告、调参记录、延迟优化 spec）
src/                 PoC 核心代码（上游 eval/ 的副本 + 本 PoC 新增模块）
  bootstrap.py       运行时路径装配（上游依赖注入）
  turn_session.py    五状态回合状态机（判停/选工具/投机 LLM/取消）
  turn_eot.py        KEV 判停三问打分（complete / cut / wait + margin）
  asr_stream.py      火山豆包流式 ASR 适配（帧协议 + 推流式 ASRStream）
  core_pipeline_run.py  单句核心链路（含 KEV_BASE / KEV_EOT_BASE / on_start 改动）
  build_prefix_cases.py / streaming_prefix_eval.py   前缀回放评测（M2 覆盖率/误判率）
  fixtures/          PoC 自有 fixture（streaming-prefix-cases.jsonl）
fixtures/            core_score 工具集：tools-core-score.yaml（242 tools / 17 domains）+ 域描述 + 源 JSON
ui/                  本地回环 UI（Flask + 原生 WebSocket）
  server.py          /api/run、/api/run/stream、/api/session/stream(SSE)、/api/session/mic(WS)
  index.html         单句 / 流式模拟 / 麦克风三模式 + 判停卡片流时间线
tests/               验收与探针脚本（见 tests/README.md）
cases/               麦克风整轮标注样本（jsonl）
evidence/            运行时产物（gitignore，仅留 sample/）
assets/              关键截图
```

## 运行前置

| 依赖 | 说明 |
|---|---|
| 上游仓库 | `model_clients.py`/`registry.py`/`decisions.py`/`eval/fixtures/core-pipeline-corpus.jsonl` 来自 navi-dual-system，**不复制**。工具注册表是本仓 `fixtures/tools-core-score.yaml`，**不再**加载上游 `tools.yaml` / `tools-multidomain.yaml`。用 `POC_UPSTREAM` 指定（默认探测 `/Users/YUYSONG/Agents/navi-dual-system`） |
| Python | `/Users/YUYSONG/Agents/navi-dual-system/.venv/bin/python`（含 `httpx2` 等依赖） |
| KEV 隧道 | 两条独立隧道：`:8009` 选工具、`:8010` 判停（指向同一远程 KEV 服务） |
| ASR 凭据 | 火山豆包 `CLOUD_ASR_*`，运行时从环境或 `~/.zshrc` 读入，**不入库、不打印** |
| LLM 凭据 | 公司 LiteLLM，走上游 `model_clients.existing_slow_configuration()`（`~/.dsh/.credentials.yaml`），**不打印** |

## 常用命令

```bash
PY=/Users/YUYSONG/Agents/navi-dual-system/.venv/bin/python
cd /Users/YUYSONG/Work/poc-streaming-eot

# 起 UI（默认 8765，麦克风模式需要 localhost）
$PY -u -B ui/server.py

# 验收（需 UI 在跑）
$PY -u -B tests/poc-streaming-s1s3-core-score.py   # S1-S3：core_score 两阶段召回/短名单/一次决策（需 KEV + LLM）
$PY -u -B tests/poc-streaming-a3.py 8765     # 脚本回放：判停/状态机/取消
$PY -u -B tests/poc-streaming-a4.py 8765 --with-reuse
$PY -u -B tests/poc-streaming-a5.py 8765     # 投机 LLM 作废
$PY -u -B tests/poc-streaming-mic-ws.py      # 麦克风 WS 链路（真 wav 按 20ms 推）
$PY -u -B tests/poc-streaming-m1b.py /tmp/asr-zh.wav   # 文件流式 ASR 端到端

# 前缀回放评测（离线，需 KEV 隧道）
$PY -u -B src/streaming_prefix_eval.py --langs zh,en --report docs/prefix-eval-after.md
```

`POC_PORT` / `POC_UPSTREAM` / `POC_CASES`（pipecat 语料路径）/ `POC_MIC_CASES` 可覆盖默认值。

## 已验证结论（摘要）

| 项 | 结果 |
|---|---|
| 判停覆盖率（150ms 前缀韵律，398 句） | zh 3.3% → **95.0%**，en 2.5% → **89.2%** |
| 每句可用判停数 | zh 1.20 → 1.21，en 1.16 → 1.20 |
| 句中前缀误判"说完"率 | zh 71.4% → **62.1%**，en 9.5% → 23.8% |
| 最后一字 → LLM 启动 | zh 755ms / en 1501ms（en 未达 800ms 目标，已按约定接受） |
| 决策正确率（held-out） | 94.6%（与设计稿一致） |
| 未决前提 | `eot_max_inflight` 1/2/3 → 覆盖率 42.1%/70.0%/93.9%（并发同时提升覆盖与延迟） |

细节见 `docs/final-report.md`；逐段调参记录见 `docs/m3-*.md`。

## 与上游的关系（provenance）

- 上游 base：`fb5b259`（分支 `dev-20261013`，仓库 navi-dual-system）
- 本 PoC 在上游 worktree 的提交：`07520cc`（M1–M3）、`caa73d8`（独立 EOT 隧道 + 并发判停）、`e9e0001`（推流式 ASRStream）、`32560cc`（ASR 断句 end_time + last packet 修正）
- 这些提交已导出为 `docs/patches/*.patch`，可在上游仓库按序重放；本仓库的 `src/` 是等价快照，并额外做了路径仓库相对化。
- 上游 worktree 与 `jev-research/poc-ui` 保持原样（未改动），仅作为历史参考。

## 已知边界（PoC 取舍）

- 工具集只有 `core_score`（242/17）；旧的 `navigation`(14) 与 `multidomain`(200) 工具集与 UI 选项已移除，上游 `tools.yaml` / `tools-multidomain.yaml` 物理保留但 PoC 不再加载（见 `docs/spec-core-score-tools.md`）。
- 判停策略固定为 `eot_policy=margin`，`eot_margin=0.2`；`spec_margin=0.3` 控制投机启动。
- 麦克风模式的"VAD 结束"用 ASR 断句 `end_time` 代理，仅用于展示，不参与状态机。
- 语法上不追求异常完备：KEV 失效/超时回退 `max_silence` 兜底；预热失败静默。
- 音频静音 → 文本定稿仍有 0.3–1.3s（ASR 侧），是感知链路最大的一块，见 `docs/spec-latency-opt.md`。