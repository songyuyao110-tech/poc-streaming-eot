#!/bin/bash
# 历史实验驱动脚本（M2/M3 调参扫描）：结果已归档在 docs/m3-*.md
set -x
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PY=${POC_PY:-/Users/YUYSONG/Agents/navi-dual-system/.venv/bin/python}
cd "$ROOT" || exit 1
# M3 tuning sweep: one cadence (600ms/prefix) so that most prefixes actually get a judgement.
COMMON="--langs zh,en --per-lang-limit 20 --prefix-interval-ms 600 --pause-ratio 0.2 --decide-ms 1380 --skip-v0"

$PY -u -B src/streaming_prefix_eval.py $COMMON --run-id px-m3-baseline \
    --report docs/m3-baseline.md

$PY -u -B src/streaming_prefix_eval.py $COMMON \
    --config '{"confirm_silence_ms":250,"eot_timeout_ms":900,"debounce_ms":150,"speculative_llm":true,"spec_margin":0.3,"eot_input":"last_segment"}' \
    --run-id px-m3-tuned --report docs/m3-tuned.md

$PY -u -B src/streaming_prefix_eval.py $COMMON \
    --config '{"eot_policy":"margin","eot_margin":0.2,"confirm_silence_ms":250,"eot_timeout_ms":900,"debounce_ms":150,"speculative_llm":true,"spec_margin":0.3,"eot_input":"last_segment"}' \
    --run-id px-m3-margin --report docs/m3-margin.md

$PY -u -B src/streaming_prefix_eval.py $COMMON \
    --config '{"confirm_silence_ms":250,"eot_timeout_ms":900,"debounce_ms":150,"speculative_llm":true,"spec_margin":0.3,"eot_input":"turn_text"}' \
    --run-id px-m3-turntext --report docs/m3-turntext.md

echo "M3 SWEEP DONE"