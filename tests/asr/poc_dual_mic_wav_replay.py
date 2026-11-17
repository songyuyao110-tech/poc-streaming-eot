"""双席 · 真实 ASR（火山豆包流式）wav 回放进两路的可复现真链冒烟。

与 tests/test_dual_mic_ws.py（脚本假 ASR，离线契约）互补：
  - 这里的 ASR 是**真实** `ASRStream`（`CLOUD_ASR_*`），一路一个 wav，分别演进席 D / 席 P；
  - 判停/选工具/决策走离线 mock（`mock=true`），把「真实 ASR 是否各自独立、不串席」单独隔离出来，
    不依赖 KEV 8009 / LLM 是否在跑。

断言：
  [1] 两席都拿到真实 ASR 更新（不只 1 条），且各自 final 文本来自自己那路 wav；
  [2] 席 D 的 ASR 文本里不出现席 P 那路的关键词（反之亦然）＝ ASR 路由不串席；
  [3] duo_done 收口，cross_cancel / cross_reset = 0；
  [4] 单独一路（只开 D）也能收口、另一席无事件（=那路没说话）。

用法（需要 CLOUD_ASR_* 与网络）：
    POC_UPSTREAM=<上游根> <venv>/python -B tests/poc_dual_mic_wav_replay.py
凭据不可用时打印 SKIP 并以 0 退出（标「真链未验」）。
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ui"))
sys.path.insert(0, str(ROOT / "src"))
sys.dont_write_bytecode = True

for candidate in ("/Users/YUYSONG/Work/tmp/jev-reopt-checkouts/dev-streaming-eot",
                  "/Users/YUYSONG/Agents/navi-dual-system"):
    if not os.environ.get("POC_UPSTREAM") and (Path(candidate) / "model_clients.py").is_file():
        os.environ["POC_UPSTREAM"] = candidate

RATE, FRAME_MS = 16000, 20
FRAME = int(RATE * 2 * FRAME_MS / 1000)

# 两路 wav：D = 导航回家（关键词「导航」），P = 附近有什么便利店（关键词「便利店/附近」）。
# 可用环境变量覆盖；默认取本机已有的 16k/mono/16bit 语料。
WAV_D = os.environ.get("DUAL_WAV_D", "/Users/YUYSONG/Work/tmp/qwen-smartturn/audio/smoke20-001.wav")
WAV_P = os.environ.get("DUAL_WAV_P", "/Users/YUYSONG/Work/tmp/qwen-smartturn/audio/smoke20-005.wav")
KEY_D, KEY_P = "导航", "便利店"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def check(label, condition, detail=""):
    print(("  ok   " if condition else "  FAIL ") + label + (("  | " + str(detail)) if detail and not condition else ""))
    if not condition:
        raise AssertionError(label + ("  | " + str(detail) if detail else ""))


def read_wav(path):
    with wave.open(str(path), "rb") as handle:
        if handle.getframerate() != RATE or handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise ValueError("need 16000 Hz mono 16-bit wav: " + str(path))
        return handle.readframes(handle.getnframes())


def creds_available():
    try:
        from asr.asr_stream import credentials
        credentials()
        return True
    except Exception as error:
        print("SKIP: 真实 ASR 凭据不可用（CLOUD_ASR_*）：%s" % type(error).__name__)
        return False


def main():
    if not creds_available():
        print("真链未验：缺少 CLOUD_ASR_APP_ID / CLOUD_ASR_ACCESS_TOKEN。")
        return 0
    if not (Path(WAV_D).is_file() and Path(WAV_P).is_file()):
        print("SKIP: 缺 wav 语料 %s / %s（用 DUAL_WAV_D / DUAL_WAV_P 指定）" % (WAV_D, WAV_P))
        return 0

    import server
    # 不覆盖 DUAL_MIC_ASR_FACTORY → 用真实 ASRStream。
    from simple_websocket import Client

    port = free_port()
    threading.Thread(target=lambda: server.app.run(host="127.0.0.1", port=port,
                                                   debug=False, use_reloader=False), daemon=True).start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                break
        except OSError:
            time.sleep(0.05)
    else:
        raise RuntimeError("server did not start")
    print("server on %d · 真 ASR 两路 wav 回放" % port)

    def open_zoom():
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:%d/api/session/dual/mic/open" % port,
                                     data=json.dumps({"mock": True}).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode())

    def run(seats_pcm, timeout_s=90):
        """seats_pcm = {"D": pcm, "P": pcm}；返回 (collected, duo_done)。"""
        session_id = open_zoom()["session_id"]
        collected = {}
        socks = {}
        lock = threading.Lock()

        def recv(seat, ws):
            msgs = []
            while True:
                try:
                    message = ws.receive(timeout=0.2)
                except TimeoutError:
                    continue                 # 静默继续等（不要一次超时就退出）
                except Exception:
                    break                    # 连接关闭/异常
                if message is None:
                    continue             # simple_websocket: 超时返回 None（不是关闭）；关闭会抛 ConnectionClosed
                msgs.append(json.loads(message))
                with lock:
                    collected[seat] = list(msgs)

        def feed(seat, pcm):
            ws = Client.connect("ws://127.0.0.1:%d/api/session/dual/mic?session_id=%s&seat=%s"
                                % (port, session_id, seat), receive_bytes=65536, max_message_size=1 << 22)
            socks[seat] = ws
            threading.Thread(target=recv, args=(seat, ws), daemon=True).start()
            started = time.monotonic()
            for offset in range(0, len(pcm), FRAME):
                ws.send(pcm[offset:offset + FRAME])
                target = started + (offset + FRAME) / (RATE * 2)
                while time.monotonic() < target:
                    time.sleep(0.002)
            ws.send(json.dumps({"cmd": "stop"}))

        threads = [threading.Thread(target=feed, args=(seat, pcm), daemon=True)
                   for seat, pcm in seats_pcm.items()]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        duo_done = None
        end = time.monotonic() + timeout_s
        while time.monotonic() < end:
            with lock:
                for msgs in collected.values():
                    for m in msgs:
                        if m.get("kind") == "event" and m["data"].get("event") == "duo_done":
                            duo_done = m["data"]
            if duo_done:
                break
            time.sleep(0.1)
        # 收到 duo_done（或超时）后再关连接，让 recv 线程退出。
        time.sleep(0.3)
        for ws in socks.values():
            try:
                ws.close()
            except Exception:
                pass
        return collected, duo_done

    # --- [1][2][3] 两路真实 ASR 同时回放 ---
    print("[1] 两路真实 ASR 同时回放（D=%s / P=%s）" % (Path(WAV_D).name, Path(WAV_P).name))
    collected, duo_done = run({"D": read_wav(WAV_D), "P": read_wav(WAV_P)})
    check("收到 duo_done", duo_done is not None)
    plan = (duo_done or {}).get("plan") or {}
    summary = (duo_done or {}).get("summary") or {}
    for seat in ("D", "P"):
        entry = plan.get(seat) or {}
        print("   席 %s: asr_updates=%s final=%r" % (seat, entry.get("asr_updates"), entry.get("final_text")))
    check("席 D 拿到真实 ASR 且 final 含「%s」" % KEY_D,
          (plan.get("D") or {}).get("asr_updates", 0) >= 2 and KEY_D in ((plan.get("D") or {}).get("final_text") or ""),
          plan.get("D"))
    check("席 P 拿到真实 ASR 且 final 含「%s」" % KEY_P,
          (plan.get("P") or {}).get("asr_updates", 0) >= 2 and KEY_P in ((plan.get("P") or {}).get("final_text") or ""),
          plan.get("P"))
    check("席 D 的 ASR 文本不含席 P 关键词（ASR 不串席）",
          KEY_P not in ((plan.get("D") or {}).get("final_text") or ""), plan.get("D"))
    check("席 P 的 ASR 文本不含席 D 关键词（ASR 不串席）",
          KEY_D not in ((plan.get("P") or {}).get("final_text") or ""), plan.get("P"))
    check("cross_cancel = 0", summary.get("cross_cancel") == 0, summary.get("cross_cancel"))
    check("cross_reset = 0", summary.get("cross_reset") == 0, summary.get("cross_reset"))

    # --- [4] 只开一路 ---
    print("[2] 只开席 D 一路 → 只 D 收口，P 无事件")
    collected2, done2 = run({"D": read_wav(WAV_D)})
    check("单路也收到 duo_done", done2 is not None)
    plan2 = (done2 or {}).get("plan") or {}
    check("单路 plan 只有 D", list(plan2) == ["D"], list(plan2))
    p_events = [1 for msgs in collected2.values() for m in msgs
                if m.get("kind") == "event" and m["data"].get("seat_id") == "P"]
    check("单路 P 无事件", not p_events)

    print("\n真链冒烟通过（真实豆包流式 ASR ×2 路，双席各进各席）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
