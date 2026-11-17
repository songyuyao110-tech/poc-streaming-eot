"""真调用核对：同一版出字上「说没说完」与「接不接」能不能同时发、两边都回来。

口径：`tmp/poc-mind-accept-judge-原料.md` §两路能不能同时发 / `-验收.md` 第 6 条。
不写结论、不按代码猜测：**同一刻发两个真请求**，记录客户端起止、两边耗时、服务端自报时延。

跑法（需 8009 隧道；不连 LLM）：
    /Users/YUYSONG/Agents/navi-dual-system/.venv/bin/python -B tests/probe_hub_two_route.py
"""
from __future__ import annotations

import json
import sys
import threading
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
from gate.hub_accept import score_accept  # noqa: E402
from gate.turn_eot import score_eot  # noqa: E402

TEXTS = [
    "导航去望京SOHO",      # 对车说
    "咱晚上去哪吃",        # 和旁边人聊天
    "帮我看看前面那个",    # 没说完 / 截断
]


def one_trial(text: str, scene: str) -> dict:
    barrier = threading.Barrier(3)          # 两个 worker + 主线程同时放行
    out = {}

    def worker(name, fn):
        barrier.wait()
        began = time.monotonic()
        try:
            result = fn()
            out[name] = {"ok": True, "t_req": began, "t_end": time.monotonic(), "result": result}
        except Exception as error:  # noqa: BLE001 - 探针要把失败也记下来
            out[name] = {"ok": False, "t_req": began, "t_end": time.monotonic(),
                         "error": "%s: %s" % (type(error).__name__, str(error)[:160])}

    eot_thread = threading.Thread(target=worker, args=(
        "eot", lambda: score_eot(text),))
    accept_thread = threading.Thread(target=worker, args=(
        "accept", lambda: score_accept(text, scene={"summary": scene, "revision": 1, "age_ms": 0})))
    eot_thread.start()
    accept_thread.start()
    barrier.wait()
    eot_thread.join(10)
    accept_thread.join(10)

    eot, accept = out["eot"], out["accept"]
    pair = {
        "text": text,
        "scene": scene,
        "eot_ok": eot["ok"],
        "accept_ok": accept["ok"],
        "eot_wall_ms": round((eot["t_end"] - eot["t_req"]) * 1000, 1),
        "accept_wall_ms": round((accept["t_end"] - accept["t_req"]) * 1000, 1),
        "start_gap_ms": round(abs(accept["t_req"] - eot["t_req"]) * 1000, 1),
        "wall_ms": round((max(eot["t_end"], accept["t_end"])
                          - min(eot["t_req"], accept["t_req"])) * 1000, 1),
        "overlap": accept["t_req"] < eot["t_end"] and eot["t_req"] < accept["t_end"],
        "eot_label": (eot.get("result") or {}).get("label"),
        "accept_accept": (accept.get("result") or {}).get("accept"),
        "accept_score": (accept.get("result") or {}).get("score"),
    }
    pair["serial_ratio"] = round(pair["wall_ms"] / max(1.0, pair["eot_wall_ms"] + pair["accept_wall_ms"]), 2)
    pair["parallel_if_wall_lt_sum"] = pair["wall_ms"] < (pair["eot_wall_ms"] + pair["accept_wall_ms"])
    return pair


def main() -> int:
    scene = "我听到主驾说「导航去望京SOHO」，像在对车说。"
    trials = []
    for text in TEXTS:
        trials.append(one_trial(text, scene if text.startswith("导航") else "我听到主驾在和旁边人聊天。"))
    summary = {
        "service": "http://127.0.0.1:8009/v1/systemone",
        "trials": trials,
        "all_started_together": all(t["start_gap_ms"] <= 50 for t in trials),
        "all_both_returned": all(t["eot_ok"] and t["accept_ok"] for t in trials),
        "all_overlap": all(t["overlap"] for t in trials),
        "concurrent": all(t["eot_ok"] and t["accept_ok"] and t["overlap"]
                          and t["parallel_if_wall_lt_sum"] for t in trials),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["concurrent"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
