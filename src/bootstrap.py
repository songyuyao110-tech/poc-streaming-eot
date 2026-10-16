"""PoC 运行时路径装配。

PoC 代码依赖上游 navi-dual-system 仓库的 model_clients.py / gateway.py / registry.py / tools.yaml /
eval/fixtures/*（那些属于另一个项目，不复制进本仓库）。这里把「本仓库 src/」和「上游仓库根」
注入 sys.path，并对外暴露两个根：

    UPSTREAM_ROOT = install()      # 上游仓库根：只读地用它的代码与 fixtures
    POC_ROOT                       # 本仓库根：证据、产物、PoC 自有 fixtures 写这里

上游位置：环境变量 POC_UPSTREAM，否则按候选列表探测。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

POC_ROOT = Path(__file__).resolve().parents[1]
CANDIDATES = (
    # PoC 实际运行的上游 worktree（分支 poc/streaming-eot，含 model_clients.py / fixtures）
    "/Users/YUYSONG/Work/tmp/jev-reopt-checkouts/dev-streaming-eot",
    "/Users/YUYSONG/Agents/navi-dual-system",
)


def upstream_root() -> Path:
    candidates = [os.environ["POC_UPSTREAM"]] if os.environ.get("POC_UPSTREAM") else list(CANDIDATES)
    for candidate in candidates:
        root = Path(candidate).expanduser()
        if (root / "model_clients.py").is_file():
            return root
    raise RuntimeError("找不到上游仓库（需要 model_clients.py）；请设 POC_UPSTREAM=/path/to/navi-dual-system")


def install() -> Path:
    """src/ 置于 sys.path[0]，上游仓库根紧随其后；返回上游仓库根。"""
    root = upstream_root()
    for path in (str(root), str(POC_ROOT / "src")):
        if path in sys.path:
            sys.path.remove(path)
        sys.path.insert(0, path)
    return root


UPSTREAM_ROOT = install()