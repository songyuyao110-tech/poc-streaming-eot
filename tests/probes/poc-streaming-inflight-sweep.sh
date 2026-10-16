#!/bin/bash
# 历史实验驱动脚本（M2/M3 调参扫描）：结果已归档在 docs/m3-*.md
set -x
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PY=${POC_PY:-/Users/YUYSONG/Agents/navi-dual-system/.venv/bin/python}
cd "$ROOT" || exit 1
for n in 1 2 3; do
  $PY -u -B src/streaming_prefix_eval.py --langs zh --per-lang-limit 20 --prefix-interval-ms 150 --pause-ratio 0.2 \
      --decide-ms 0 --skip-v0 --config "{\"eot_max_inflight\":$n}" \
      --run-id px-inflight-$n --report /tmp/px-inflight-$n.md
done
echo "INFLIGHT SWEEP DONE"
