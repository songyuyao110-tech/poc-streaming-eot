"""Micro-benchmark: does giving EOT its own tunnel remove the queueing behind tool scoring?

Before: EOT and tool scoring both hit 127.0.0.1:8009 (one SSH tunnel).
After:  EOT hits 127.0.0.1:8010 (its own SSH tunnel), tool scoring stays on 8009.
"""
import statistics
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import bootstrap
bootstrap.install()          # 上游仓库根
sys.dont_write_bytecode = True

import core_pipeline_run as core
from turn_eot import score_eot
from core_pipeline_run import select_tools

CTX = {"home_address_configured": True, "company_address_configured": True, "synthetic": True}
N = 12


def stat(name, values):
    values = sorted(values)
    print(f"{name:<44} n={len(values)} p50={statistics.median(values):7.1f} mean={statistics.mean(values):7.1f} "
          f"max={max(values):7.1f} >400ms={sum(v > 400 for v in values)}")


def eot_via(port):
    saved = core.KEV_EOT_BASE
    core.KEV_EOT_BASE = f"http://127.0.0.1:{port}"
    import turn_eot
    turn_eot.KEV_EOT_BASE = core.KEV_EOT_BASE
    try:
        started = time.perf_counter()
        result = score_eot("导航回家")
        return (time.perf_counter() - started) * 1000, result
    finally:
        core.KEV_EOT_BASE = saved
        turn_eot.KEV_EOT_BASE = saved


if __name__ == "__main__":
    alone_eot, alone_tools = [], []
    for _ in range(N):
        alone_eot.append(eot_via(8010)[0])
    for _ in range(N):
        started = time.perf_counter()
        select_tools("导航回家", "core_score", CTX)
        alone_tools.append((time.perf_counter() - started) * 1000)
    shared_eot, separate_eot, tools_under_load = [], [], []
    for _ in range(N):
        with ThreadPoolExecutor(max_workers=2) as pool:
            eot = pool.submit(eot_via, 8009)          # old setup: both on the same tunnel
            tools = pool.submit(select_tools, "导航回家", "core_score", CTX)
            eot_ms, _ = eot.result()
            tools.result()
            shared_eot.append(eot_ms)
    for _ in range(N):
        with ThreadPoolExecutor(max_workers=2) as pool:
            eot = pool.submit(eot_via, 8010)          # new setup: EOT on its own tunnel
            tools = pool.submit(select_tools, "导航回家", "core_score", CTX)
            eot_ms, _ = eot.result()
            started = time.perf_counter()
            tools.result()
            separate_eot.append(eot_ms)
            tools_under_load.append((time.perf_counter() - started) * 1000)
    print(f"KEV_EOT_BASE in use: {core.KEV_EOT_BASE}")
    stat("EOT alone (own tunnel :8010)", alone_eot)
    stat("tools alone (:8009)", alone_tools)
    stat("EOT concurrent with tools, SAME tunnel :8009 (before)", shared_eot)
    stat("EOT concurrent with tools, OWN tunnel :8010 (after)", separate_eot)