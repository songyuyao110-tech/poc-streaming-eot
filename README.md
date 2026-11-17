# poc-streaming-eot

> Agent 协作约定：见 [`AGENTS.md`](AGENTS.md)（一功能一张 DSH 卡、多功能并行 worktree、不 push、不代拍）。

KEV 两阶段选工具 + 流式判停（EOT）的端到端 PoC：**ASR 实时更新 → KEV 判停三问 / 工具短名单 → 一次 DeepSeek 决策**，
唯一工具集 `core_score`（242 tools / 17 domains，两阶段 KEV：先域后工具），
含脚本回放、真实麦克风（火山豆包流式 ASR）、以及逐事件的延迟时间线 UI。

本仓库是这条 PoC 线的**唯一代码与文档归档**，与上游产品仓库、以及 `jev-research/poc-ui` 实验目录分离。

## 目录

```
docs/                全部文字资产（输入 spec、设计稿、进度、报告、调参记录、延迟优化 spec）
src/                 PoC 核心代码（按模块分目录）
  agent/             办理：编排、任务、对人说的回执
  app/               装配与启动
  asr/               流式识别与双席
  gate/              判停、中枢开闸、节奏与收口
  memory/            记忆
  mind/              读心
  shared/            共用约定
  core_pipeline_run.py  单句核心链路
  build_prefix_cases.py / streaming_prefix_eval.py   前缀回放评测
  fixtures/          PoC 自有 fixture（streaming-prefix-cases.jsonl）
fixtures/            core_score 工具集：tools-core-score.yaml（242 tools / 17 domains）+ 域描述 + 源 JSON
ui/                  本地回环 UI（Flask + 原生 WebSocket）
  neixinxi.html      现行面向用户的界面 /neixinxi（打开即接通正式答复、默认真判断）
  index.html         工程测试台 /（调试用，不是验收面）
  mind.html          读心调试页 /mind
  server.py          页面与会话接口
tests/               验收与探针（按模块分子目录，见 tests/README.md）
tools/               辅助脚本
cases/               麦克风整轮标注样本（jsonl；真实录音，已 gitignore，只在本机）
evidence/            运行时产物（gitignore，仅留 sample/）
assets/              关键截图
```

> 上树是现行代码布局。协作约定见 [`AGENTS.md`](AGENTS.md)。

## 运行前置（从零开始，照做即可）

上游代码（`model_clients.py` / `registry.py` / `decisions.py` 和
`eval/fixtures/core-pipeline-corpus.jsonl`）**不复制进本仓库、也不用 submodule**，
而是另clone 一份、用环境变量 `POC_UPSTREAM` 指过去。工具注册表用本仓的
`fixtures/tools-core-score.yaml`，**不读**上游 `tools.yaml` / `tools-multidomain.yaml`。

### 1. 克隆两个仓库

```sh
git clone https://github.com/songyuyao110-tech/poc-streaming-eot.git
git clone https://github.com/songyuyao110-tech/navi-dual-system.git
```

**上游必须切到 `poc-streaming-eot` 分支**——`main` 是早期基线快照，它的
`model_clients.py` 里没有 `MULTIDOMAIN_CONTROLS`，直接 import 会失败：

```sh
cd navi-dual-system && git checkout poc-streaming-eot && cd ..
```

### 2. Python 与依赖

需要 **Python 3.10+**（在 3.13 上验证；macOS 自带 3.9 不行）。

**一个 venv，装两个 `requirements.txt`**（PoC 会 import 上游模块，所以运行时同时需要
两边的依赖；只装本仓那份会在 import 上游 `model_clients` 时缺 `typesafe-sdk`）：

```sh
cd poc-streaming-eot
python3.13 -m venv .venv
.venv/bin/python -m pip install -r ../navi-dual-system/requirements.txt -r requirements.txt
```

（`../navi-dual-system` 是第 1 步克隆出来的目录，按你的实际路径改。venv 建在哪个仓都行，
下文命令假设它是 `poc-streaming-eot/.venv`。）

两份依赖的关系：上游那份（`typesafe-sdk` / `httpx2[socks]` / `flask` / `flask-socketio` /
`pyyaml` / `jsonschema`）是它自己的服务与 SDK 适配需要的；本仓那份只列 **PoC 代码直接
import** 的包，与上游重叠的版本约束兼容，额外只多一个 `simple-websocket`（麦克风 WS 模式）。

### 3. 指向上游

```sh
export POC_UPSTREAM=/absolute/path/to/navi-dual-system
```

`POC_UPSTREAM` 不设时，`src/app/bootstrap.py` 会去探测两个开发机上的固定路径
（`Work/tmp/jev-reopt-checkouts/dev-streaming-eot`、`Agents/navi-dual-system`），
在别人机器上一定探测不到，会抛
`RuntimeError: 找不到上游仓库（需要 model_clients.py）`。**必须显式 export。**

### 4. 需要自备的外部服务 / 凭据（仓库里没有任何密钥）

| 依赖 | 是什么 | 默认地址 / 变量名 | 没有时的后果 |
|---|---|---|---|
| KEV 服务（选工具 / 判停 / 二次确认） | 远程 `StartLux-Decision-v1.1-9B-AutoThink` 决策模型；两阶段工具打分、判停、确认意图三处都走它（2026-11-03 起单服务，旧 `kev-latest` 的 `:8010` 判停隧道已退役） | `http://127.0.0.1:8009`，变量 `KEV_BASE`；判停另有 `EOT_JUDGE_BASE`，确认轮 `KEV_EOT_BASE`（默认同 8009） | 选工具/判停/确认轮的真实打分失败（mock 路径不受影响） |
| LLM 凭据 | 公司 LiteLLM（走**上游** `model_clients.existing_slow_configuration()` 读配置） | 变量 `LITELLM_BASE_URL` / `LITELLM_API_KEY` / `SLOW_MODEL`（三个都要，缺一个显式失败） | 真实决策失败；mock 路径不受影响 |
| ASR 凭据 | 火山豆包流式 ASR（麦克风模式 / wav 文件回放） | 变量 `CLOUD_ASR_APPID` / `CLOUD_ASR_ACCESS_TOKEN` 等 `CLOUD_ASR_*` | 麦克风与 m1b/mic-ws 脚本跑不了 |

配在哪：这些都只从**进程环境变量**读，不读仓库里的文件。LiteLLM 三个变量由
上游 `model_clients.py` 消费，本仓不落盘、不打印；ASR 变量由 `src/asr/asr_stream.py` 消费。
两条隧道怎么建取决于你的 KEV 服务位置（SSH 端口转发即可），本仓不含隧道配置。现在只需要一条
`:8009`（选工具、判停、确认轮同用）；旧的第二条 `:8010` 判停专用隧道随 2026-11-03 判停换模型退役。

### 5. 没有外部服务时能跑什么

不连 KEV / LLM / ASR 也能跑（纯 mock，**本次已在干净环境验证**）。
下面假设你已经按第 2 步 `cd` 进了 `poc-streaming-eot`：

```sh
export POC_UPSTREAM=/absolute/path/to/navi-dual-system

.venv/bin/python -B tests/gate/test_confirm_gate.py    # 确认判据，53 项全过
.venv/bin/python -B tests/gate/poc_confirm_flow.py     # 确认编排，PASS
.venv/bin/python -B tests/gate/poc_confirm_e2e.py      # 主循环全链路 5 条路径，PASS
.venv/bin/python -c "import sys; sys.path.insert(0,'src'); from app import bootstrap; print(bootstrap.UPSTREAM_ROOT)"
```

需要 KEV（或 LLM / ASR）才能跑的，本次**未验证**：`poc-streaming-s1s3-core-score.py`、
`poc-streaming-a3/a4/a5.py`、`poc-streaming-mic-ws.py`、`poc-streaming-m1b.py`、
`src/streaming_prefix_eval.py`、`tests/gate/probe_confirm_kev_real.py`、
`tests/gate/run_confirm_cases.py` 的 L3 层，以及 `ui/server.py` 的"单句/流式模拟/麦克风"三种真实模式
（UI 的"确认轮"模式是离线 mock，可以空跑）。

### 6. `POC_CASES`：依赖 pipecat 语料的脚本

`src/build_prefix_cases.py` 和 `src/streaming_prefix_eval.py` 里
`POC_CASES` 默认值是**开发机路径** `/Users/YUYSONG/Work/tmp/pipecat-turn-eval-v2/cases.json`，
别人机器上不存在。要做前缀回放评测必须自己准备这个语料并 `export POC_CASES=/your/cases.json`；
没有就跳过这两个脚本——它们只影响"前缀回放评测"，不影响其他任何路径。
（`src/fixtures/streaming-prefix-cases.jsonl` 是生成好的结果，已入库，评测可以直接复用。）

## 常用命令

下面假设你在本仓根目录、且已 `export POC_UPSTREAM=...`：

```bash
cd poc-streaming-eot
PY=.venv/bin/python

# 起 UI（默认 8765，麦克风模式需要 localhost）
$PY -u -B ui/server.py
# 两个页面：http://127.0.0.1:8765/neixinxi 是**现行面向用户的界面**（打开即接通正式答复、默认真判断；
# ?eot=offline 才是离线替身，离线通过只算预检），用户与最终验收都看这一页；
# http://127.0.0.1:8765/ 的工程测试台只作调试，不是验收面。页面口径见 ui/README.md。

# 验收（需 UI 在跑）
$PY -u -B tests/legacy/poc-streaming-s1s3-core-score.py   # S1-S3：core_score 两阶段召回/短名单/一次决策（需 KEV + LLM）
$PY -u -B tests/app/poc-streaming-a3.py 8765     # 脚本回放：判停/状态机/取消
$PY -u -B tests/app/poc-streaming-a4.py 8765 --with-reuse
$PY -u -B tests/app/poc-streaming-a5.py 8765     # 投机 LLM 作废
$PY -u -B tests/asr/poc-streaming-mic-ws.py      # 麦克风 WS 链路（真 wav 按 20ms 推）
$PY -u -B tests/asr/test_dual_mic_ws.py          # 双席真麦：两路 wav PCM → 两席独立 ASR（离线契约，假 ASR）
$PY -u -B tests/asr/poc_dual_mic_wav_replay.py   # 双席真麦：真火山豆包流式 ASR ×2 路 wav 回放（需 CLOUD_ASR_*）
$PY -u -B tests/mind/test_mind_asr_ws.py          # 读心真麦通道：/api/mind/asr 契约（离线，假 ASR）
$PY -u -B tests/mind/poc_mind_asr_live.py         # 读心真麦真链：say 合成语音 → 真流式 ASR → 字进读心（需 CLOUD_ASR_*）
$PY -u -B tests/asr/poc-streaming-m1b.py /tmp/asr-zh.wav   # 文件流式 ASR 端到端

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
- 公开的 `navi-dual-system` 仓库有两条分支：`main`（早期 21 文件基线）和
  `poc-streaming-eot`（本 PoC 依赖的代码，对应本地分支 `poc/streaming-eot` 的 `9375581`）。
  公开分支去掉了 `eval/evidence/`（本机评测产物，含真实麦克风会话的逐字时间线），
  其余代码与 fixtures 与原分支一致。

## 已知边界（PoC 取舍）

- 工具集只有 `core_score`（242/17）；旧的 `navigation`(14) 与 `multidomain`(200) 工具集与 UI 选项已移除，上游 `tools.yaml` / `tools-multidomain.yaml` 物理保留但 PoC 不再加载（见 `docs/spec-core-score-tools.md`）。
- 判停策略默认 `eot_policy=score`：`probabilities.complete ≥ eot_complete_min(0.80)` 才算「说完」，`confidence ≥ eot_confidence_min(0.70)` 才抢跑；两个阈值可用 `EOT_COMPLETE_MIN_SCORE` / `EOT_CONFIDENCE_MIN` 覆盖。`margin`(`eot_margin=0.2`) / `argmax` 只作 A/B，默认关。判停端点 `EOT_JUDGE_BASE`（默认 `http://127.0.0.1:8009`，SSH 隧道到远程 GPU）。单席 20 轮「最后一字→CONFIRMED」基线 p50 858.7ms / p90 861.5ms（2026-11-03，`tests/gate/poc-streaming-eot-baseline.py`）。
- 麦克风模式的"VAD 结束"用 ASR 断句 `end_time` 代理，仅用于展示，不参与状态机。
- 语法上不追求异常完备：KEV 失效/超时回退 `max_silence` 兜底；预热失败静默。
- 音频静音 → 文本定稿仍有 0.3–1.3s（ASR 侧），是感知链路最大的一块，见 `docs/spec-latency-opt.md`。