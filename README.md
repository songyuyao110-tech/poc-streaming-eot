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
cases/               麦克风整轮标注样本（jsonl；真实录音，已 gitignore，只在本机）
evidence/            运行时产物（gitignore，仅留 sample/）
assets/              关键截图
```

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

```sh
# 上游依赖（typesafe-sdk / httpx2 / flask / flask-socketio / PyYAML / jsonschema）
cd navi-dual-system
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cd ..

# 本 PoC 的依赖（httpx2 / Flask / PyYAML / jsonschema / simple-websocket）
cd poc-streaming-eot
python3.13 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cd ..
```

两边版本约束兼容，也可以只建一个 venv 把两个 `requirements.txt` 一起装，
再让两个仓库都用同一个解释器。本仓 `requirements.txt` 只列 **PoC 代码直接 import**
的包，不列传递依赖；上游 `requirements.txt` 覆盖它自己那套（含 `typesafe-sdk`）。

### 3. 指向上游

```sh
export POC_UPSTREAM=/absolute/path/to/navi-dual-system
```

`POC_UPSTREAM` 不设时，`src/bootstrap.py` 会去探测两个开发机上的固定路径
（`Work/tmp/jev-reopt-checkouts/dev-streaming-eot`、`Agents/navi-dual-system`），
在别人机器上一定探测不到，会抛
`RuntimeError: 找不到上游仓库（需要 model_clients.py）`。**必须显式 export。**

### 4. 需要自备的外部服务 / 凭据（仓库里没有任何密钥）

| 依赖 | 是什么 | 默认地址 / 变量名 | 没有时的后果 |
|---|---|---|---|
| KEV 选工具隧道 | 指向远程 KEV 服务（管"这句话该用哪些工具"的两阶段打分） | `http://127.0.0.1:8009`，变量 `KEV_BASE` | 选工具/判停类脚本失败 |
| KEV 判停隧道 | 同一条远程 KEV 服务、独立一条隧道（避免和选工具排队） | `http://127.0.0.1:8010`，变量 `KEV_EOT_BASE` | 判停打分、确认轮真实打分失败（mock 路径不受影响） |
| LLM 凭据 | 公司 LiteLLM（走**上游** `model_clients.existing_slow_configuration()` 读配置） | 变量 `LITELLM_BASE_URL` / `LITELLM_API_KEY` / `SLOW_MODEL`（三个都要，缺一个显式失败） | 真实决策失败；mock 路径不受影响 |
| ASR 凭据 | 火山豆包流式 ASR（麦克风模式 / wav 文件回放） | 变量 `CLOUD_ASR_APPID` / `CLOUD_ASR_ACCESS_TOKEN` 等 `CLOUD_ASR_*` | 麦克风与 m1b/mic-ws 脚本跑不了 |

配在哪：这些都只从**进程环境变量**读，不读仓库里的文件。LiteLLM 三个变量由
上游 `model_clients.py` 消费，本仓不落盘、不打印；ASR 变量由 `src/asr_stream.py` 消费。
两条隧道怎么建取决于你的 KEV 服务位置（SSH 端口转发即可），本仓不含隧道配置。

### 5. 没有外部服务时能跑什么

不连 KEV / LLM / ASR 也能跑（纯 mock，**本次已在干净环境验证**）：

```sh
export POC_UPSTREAM=/absolute/path/to/navi-dual-system
cd poc-streaming-eot

.venv/bin/python -B tests/test_confirm_gate.py    # 确认判据，53 项全过
.venv/bin/python -B tests/poc_confirm_flow.py     # 确认编排，PASS
.venv/bin/python -B tests/poc_confirm_e2e.py      # 主循环全链路 5 条路径，PASS
.venv/bin/python -c "import sys; sys.path.insert(0,'src'); import bootstrap; print(bootstrap.UPSTREAM_ROOT)"
```

需要 KEV（或 LLM / ASR）才能跑的，本次**未验证**：`poc-streaming-s1s3-core-score.py`、
`poc-streaming-a3/a4/a5.py`、`poc-streaming-mic-ws.py`、`poc-streaming-m1b.py`、
`src/streaming_prefix_eval.py`、`tests/probe_confirm_kev_real.py`、
`tests/run_confirm_cases.py` 的 L3 层，以及 `ui/server.py` 的"单句/流式模拟/麦克风"三种真实模式
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
- 公开的 `navi-dual-system` 仓库有两条分支：`main`（早期 21 文件基线）和
  `poc-streaming-eot`（本 PoC 依赖的代码，对应本地分支 `poc/streaming-eot` 的 `9375581`）。
  公开分支去掉了 `eval/evidence/`（本机评测产物，含真实麦克风会话的逐字时间线），
  其余代码与 fixtures 与原分支一致。

## 已知边界（PoC 取舍）

- 工具集只有 `core_score`（242/17）；旧的 `navigation`(14) 与 `multidomain`(200) 工具集与 UI 选项已移除，上游 `tools.yaml` / `tools-multidomain.yaml` 物理保留但 PoC 不再加载（见 `docs/spec-core-score-tools.md`）。
- 判停策略固定为 `eot_policy=margin`，`eot_margin=0.2`；`spec_margin=0.3` 控制投机启动。
- 麦克风模式的"VAD 结束"用 ASR 断句 `end_time` 代理，仅用于展示，不参与状态机。
- 语法上不追求异常完备：KEV 失效/超时回退 `max_silence` 兜底；预热失败静默。
- 音频静音 → 文本定稿仍有 0.3–1.3s（ASR 侧），是感知链路最大的一块，见 `docs/spec-latency-opt.md`。