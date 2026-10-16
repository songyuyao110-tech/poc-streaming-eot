#!/bin/bash
# 历史实验驱动脚本（M2/M3 调参扫描）：结果已归档在 docs/m3-*.md
set -x
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PY=${POC_PY:-/Users/YUYSONG/Agents/navi-dual-system/.venv/bin/python}
cd "$ROOT" || exit 1

# 1) full M2 replay at the plan's cadence, final defaults (separate EOT tunnel + concurrent EOT)
$PY -u -B src/streaming_prefix_eval.py --langs zh,en --prefix-interval-ms 150 --pause-ratio 0.2 --decide-ms 1380 \
    --run-id px-m2-after --report docs/prefix-eval-after.md

# 2) M3 cadence (600ms) with the final defaults, to compare against px-m3-longconfirm
$PY -u -B src/streaming_prefix_eval.py --langs zh,en --per-lang-limit 20 --prefix-interval-ms 600 --pause-ratio 0.2 \
    --decide-ms 1380 --skip-v0 --run-id px-m3-after-default \
    --report docs/m3-after-default.md

# 3) same cadence + the ORIGINAL M3 baseline timers, to isolate the infrastructure change vs px-m3-baseline
$PY -u -B src/streaming_prefix_eval.py --langs zh,en --per-lang-limit 20 --prefix-interval-ms 600 --pause-ratio 0.2 \
    --decide-ms 1380 --skip-v0 \
    --config '{"eot_policy":"argmax","eot_margin":0.0,"confirm_silence_ms":300,"eot_timeout_ms":400,"debounce_ms":80,"speculative_llm":false}' \
    --run-id px-m3-after-oldtimers --report docs/m3-after-oldtimers.md

echo "AFTER SWEEP DONE"
