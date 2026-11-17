"""真网络失败/超时核对：接不接判断失败或超时时，闸门是「先不开闸」，不是「不接」。

本地起一个小 HTTP 服务模拟 8009：一条路径直接断连（判断失败），一条路径延迟返回（判断超时，
随后晚回来）。跑法：

    /Users/YUYSONG/Agents/navi-dual-system/.venv/bin/python -B tests/probe_accept_unavailable.py
"""
from __future__ import annotations

import json
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import httpx2
# noqa: E402

from gate.hub_accept import score_accept
# noqa: E402
from gate.hub_gate import GateKeeper
# noqa: E402
FAIL_PORT = 18011
SLOW_PORT = 18012


class FailHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        self.send_response(503)
        self.end_headers()
        self.wfile.write(b"down")

    def log_message(self, *args):
        pass


class SlowHandler(BaseHTTPRequestHandler):
    def do_POST(self):  # noqa: N802
        time.sleep(1.0)
        body = json.dumps({"model": "StartLux-Decision-v1.1-9B-AutoThink",
                           "answers": {"accept": {"type": "noul", "noul": 0.93}}}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def eot_complete(text):
    return {"label": "complete", "may_confirm": True, "may_start_early": True,
            "complete": 0.94, "cut": 0.03, "wait": 0.03, "confidence": 0.9, "latency_ms": 80.0}


def drive(gate, timeout_s=4.0):
    gate.on_text(1, "导航去望京SOHO", 0, final=True)
    gate.on_eot(1, 0, eot_complete("导航去望京SOHO"))
    deadline = time.time() + timeout_s
    verdict = {}
    now = 100
    while time.time() < deadline:
        gate.poll(now)
        verdict = gate.gate(1, now, assume_done=True)
        if verdict["route"] == "accept_unavailable":
            return verdict
        now += 100
        time.sleep(0.03)
    return verdict


def main() -> int:
    fail_server = ThreadingHTTPServer(("127.0.0.1", FAIL_PORT), FailHandler)
    slow_server = ThreadingHTTPServer(("127.0.0.1", SLOW_PORT), SlowHandler)
    for server in (fail_server, slow_server):
        threading.Thread(target=server.serve_forever, daemon=True).start()

    fail_client = httpx2.Client(timeout=0.4, trust_env=False, follow_redirects=False)
    slow_client = httpx2.Client(timeout=3.0, trust_env=False, follow_redirects=False)
    out = {}

    failed_gate = GateKeeper(
        accept_fn=lambda text, scene, seat: score_accept(
            text, scene=scene, client=fail_client, base="http://127.0.0.1:%d" % FAIL_PORT),
        scene_fn=lambda: None, accept_timeout_ms=2000)
    failed = drive(failed_gate)
    out["failure_503"] = {"route": failed["route"], "state": failed["state"],
                          "reason": failed["reason"],
                          "accept_state": (failed.get("accept") or {}).get("state"),
                          "accept_error": (failed.get("accept") or {}).get("error")}

    slow_gate = GateKeeper(
        accept_fn=lambda text, scene, seat: score_accept(
            text, scene=scene, client=slow_client, base="http://127.0.0.1:%d" % SLOW_PORT),
        scene_fn=lambda: None, accept_timeout_ms=300)
    first = drive(slow_gate)
    out["timeout_first"] = {"route": first["route"], "state": first["state"],
                            "reason": first["reason"]}
    deadline = time.time() + 3.0
    late = {}
    now = 500
    while time.time() < deadline:
        now += 200
        slow_gate.poll(now)
        late = slow_gate.gate(1, now, assume_done=True)
        if late["route"] == "agent":
            break
        time.sleep(0.05)
    out["timeout_late_result"] = {"route": late.get("route"), "state": late.get("state"),
                                  "reason": late.get("reason")}

    fail_server.shutdown()
    slow_server.shutdown()
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
