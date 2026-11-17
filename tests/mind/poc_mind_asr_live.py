"""读心真麦通道的**真链**回放：本机合成语音 → /api/mind/asr（真火山豆包流式 ASR）→ 字进读心。

与浏览器里「真麦」走的是同一条服务端路径（ASRStream + CLOUD_ASR_*），区别只是音频来自
`say` 合成的 wav 而不是麦克风，用来在没有嘴的情况下可复现地证明「字是真 ASR 出的」。

需要 CLOUD_ASR_*（环境变量或 ~/.zshrc）；凭据只读不打印。

用法：
    /Users/YUYSONG/Agents/navi-dual-system/.venv/bin/python -B tests/poc_mind_asr_live.py
    /Users/YUYSONG/Agents/navi-dual-system/.venv/bin/python -B tests/poc_mind_asr_live.py "导航回家"
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
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

import server  # noqa: E402

RATE, FRAME_MS = 16000, 20
FRAME = int(RATE * 2 * FRAME_MS / 1000)
TEXT = sys.argv[1] if len(sys.argv) > 1 else "导航回家，尽量走高速"


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def speak_wav(text):
    """macOS `say` → 16 kHz mono 16-bit wav（本机合成语音，内容确定、可复现）。"""
    raw = Path(tempfile.mkdtemp()) / "speak.aiff"
    out = raw.with_suffix(".wav")
    subprocess.run(["say", "-v", "Tingting", "-o", str(raw), text], check=True)
    subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", str(raw), "-ac", "1", "-ar", str(RATE),
                    "-acodec", "pcm_s16le", str(out)], check=True)
    with wave.open(str(out), "rb") as handle:
        assert handle.getframerate() == RATE and handle.getnchannels() == 1
        return handle.readframes(handle.getnframes())


def start_server():
    port = free_port()
    threading.Thread(target=lambda: server.app.run(host="127.0.0.1", port=port, debug=False,
                                                   use_reloader=False), daemon=True).start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return port
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("server did not start")


def run(port, pcm):
    from simple_websocket import Client
    ws = Client.connect("ws://127.0.0.1:%d/api/mind/asr" % port,
                        receive_bytes=65536, max_message_size=1 << 22)
    messages, done = [], threading.Event()

    def recv():
        while not done.is_set():
            try:
                message = ws.receive(timeout=0.2)
            except TimeoutError:
                continue
            except Exception:
                break
            if message is None:
                continue
            packet = json.loads(message)
            messages.append(packet)
            print("  <-", packet.get("kind"), json.dumps(packet.get("data"), ensure_ascii=False)[:160])
            if packet.get("kind") == "stopped":
                done.set()

    threading.Thread(target=recv, daemon=True).start()
    began = time.monotonic()
    for offset in range(0, len(pcm), FRAME):
        ws.send(pcm[offset:offset + FRAME])
        target = began + (offset + FRAME) / (RATE * 2)
        while time.monotonic() < target:
            time.sleep(0.002)
    ws.send(json.dumps({"cmd": "stop"}))
    done.wait(20)
    try:
        ws.close()
    except Exception:
        pass
    return messages


def main():
    server.MIND_FORCE_OFFLINE = True     # 读心判断走离线规则：本脚本只验 ASR 那段真链
    pcm = speak_wav(TEXT)
    port = start_server()
    print("server on %d；合成语音 = %r；音频 %.2fs" % (port, TEXT, len(pcm) / (RATE * 2)))
    messages = run(port, pcm)
    asr = [m["data"] for m in messages if m.get("kind") == "asr"]
    partials = [item["text"] for item in asr if not item["final"]]
    finals = [item["text"] for item in asr if item["final"]]
    print("\npartial 条数 = %d，final = %s" % (len(partials), finals))
    if not finals:
        print("FAIL：真 ASR 没出 final（检查 CLOUD_ASR_* 与网络）")
        return 1
    # 字进读心：拿真 ASR 的 final 走一次 tick。
    body = {"session_id": "mind-live-1", "seat": "D", "asr": {"text": finals[0], "final": True}}
    req = urllib.request.Request("http://127.0.0.1:%d/api/mind/tick" % port, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as resp:
        snap = json.loads(resp.read().decode())
    print("读心快照：partial=%r finals=%s 需要响应=%s"
          % (snap["asr"]["partial"], [f["text"] for f in snap["asr"]["finals"]],
             snap["decision"]["need_response"]))
    common = set("".join(finals).replace("，", "").replace(",", "")) & set(TEXT)
    ok = bool(finals[0]) and len(common) >= max(2, len(TEXT) // 4)
    print("\n%s：真 ASR final=%r（合成语音=%r，重合字 %d 个）"
          % ("PASS" if ok else "FAIL", finals[0], TEXT, len(common)))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
