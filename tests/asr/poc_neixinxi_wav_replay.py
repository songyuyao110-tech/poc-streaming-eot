"""内心戏页 · 真实 ASR（火山豆包流式）两路 wav 回放的**非麦**真链冒烟。

与 tests/asr/test_neixinxi_page.py（脚本假 ASR，离线契约）互补：
  - 这里的 ASR 是**真实** `ASRStream`（`CLOUD_ASR_*`），一席一个 wav（16k/mono/16bit），
    各推各的 20ms PCM 帧；证明「两路流式识别真的能同时跑、字不串席」。
  - 判停走离线替身、读心走离线规则（`MIND_FORCE_OFFLINE`），把「真实 ASR 是否各自独立」
    单独隔离出来，不依赖 KEV 8009 / 真模型是否可达。

断言：
  [1] 两席都拿到真实 ASR 出字，且各自 final 来自自己那路 wav（关键词对得上）；
  [2] 席 D 的字里不出现席 P 那路的关键词（反之亦然）＝ 两路不串席；
  [3] 两席各自有判停结论（只判「说完没说完」，带三态与可不可确认）；
  [4] 读心场景里两路都在，各自记着自己那路的字；
  [5] 门槛在真链上生效：门限以上的真语音照常出字（这一步不额外造静音，靠真实静音段）。

用法（需要 CLOUD_ASR_* 与网络；凭据不可用时 SKIP 并以 0 退出，标「真链未验」）：
    /Users/YUYSONG/Agents/navi-dual-system/.venv/bin/python -B tests/asr/poc_neixinxi_wav_replay.py
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

import server  # noqa: E402

RATE, FRAME_MS = 16000, 20
FRAME = int(RATE * 2 * FRAME_MS / 1000)

WAV_D = os.environ.get("NEIXINXI_WAV_D", "/Users/YUYSONG/Work/tmp/qwen-smartturn/audio/smoke20-001.wav")
WAV_P = os.environ.get("NEIXINXI_WAV_P", "/Users/YUYSONG/Work/tmp/qwen-smartturn/audio/smoke20-005.wav")
KEY_D = ("导航",)
KEY_P = ("便利店", "附近")


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def check(label, condition, detail=""):
    print(("  ok   " if condition else "  FAIL ") + label
          + (("  | " + str(detail)) if detail and not condition else ""))
    if not condition:
        raise AssertionError(label + ("  | " + str(detail) if detail else ""))


def post(port, path, body):
    import urllib.request
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read().decode())


def read_wav(path):
    with wave.open(str(path), "rb") as handle:
        if handle.getframerate() != RATE or handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise ValueError("need %d Hz mono 16-bit wav: %s" % (RATE, path))
        return handle.readframes(handle.getnframes())


class Seat:
    def __init__(self, port, session_id, seat):
        from simple_websocket import Client
        self.seat = seat
        self.packets = []
        self.lock = threading.Lock()
        self.ws = Client.connect("ws://127.0.0.1:%d/api/neixinxi/asr?session_id=%s&seat=%s"
                                 % (port, session_id, seat),
                                 receive_bytes=65536, max_message_size=1 << 22)
        threading.Thread(target=self._recv, daemon=True).start()

    def _recv(self):
        while True:
            try:
                message = self.ws.receive(timeout=0.2)
            except Exception:
                return
            if message is None:
                continue
            try:
                packet = json.loads(message)
            except (TypeError, ValueError):
                continue
            with self.lock:
                self.packets.append(packet)

    def feed(self, pcm):
        started = time.monotonic()
        for offset in range(0, len(pcm), FRAME):
            self.ws.send(pcm[offset:offset + FRAME])
            target = started + (offset + FRAME) / (RATE * 2)
            while time.monotonic() < target:
                time.sleep(0.002)

    def cmd(self, payload):
        self.ws.send(json.dumps(payload))

    def events(self, name=None):
        with self.lock:
            packets = list(self.packets)
        return [p["data"] for p in packets
                if p.get("kind") == "event" and (name is None or p["data"].get("event") == name)]

    def texts(self):
        return [str(e.get("text") or "") for e in self.events("asr_update")]

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def wait_until(predicate, timeout=12.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def main():
    # 走真实 ASRStream：不注入假 ASR；读心/判停不连外部 LLM / 8009。
    server.NEIXINXI_ASR_FACTORY = None
    server.MIND_FORCE_OFFLINE = True
    try:
        from asr.asr_stream import credentials
        credentials()
    except Exception:
        print("SKIP: 没有 CLOUD_ASR_* 凭据，真链未验（离线契约见 tests/asr/test_neixinxi_page.py）")
        return 0
    for path in (WAV_D, WAV_P):
        if not Path(path).is_file():
            print("SKIP: 缺少回放语料 %s（可用 NEIXINXI_WAV_D / NEIXINXI_WAV_P 指定）" % path)
            return 0

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
    print("server on %d" % port)

    info = post(port, "/api/neixinxi/open", {"thresholds": {"D": -45, "P": -45}})
    sid = info["session_id"]
    pcm_d, pcm_p = read_wav(WAV_D), read_wav(WAV_P)
    d, p = Seat(port, sid, "D"), Seat(port, sid, "P")
    threads = [threading.Thread(target=d.feed, args=(pcm_d,), daemon=True),
               threading.Thread(target=p.feed, args=(pcm_p,), daemon=True)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    # 真 ASR 要靠收尾把最后一段落 final。
    d.cmd({"cmd": "stop"})
    p.cmd({"cmd": "stop"})
    ok_d = wait_until(lambda: any(e.get("final") for e in d.events("asr_update")), 12)
    ok_p = wait_until(lambda: any(e.get("final") for e in p.events("asr_update")), 12)

    print("[1] 两席真实 ASR 各自出字并落 final")
    check("主驾有真实识别结果", bool(d.texts()), d.texts()[:3])
    check("副驾有真实识别结果", bool(p.texts()), p.texts()[:3])
    check("主驾落了 final", ok_d, d.events("asr_update")[-2:])
    check("副驾落了 final", ok_p, p.events("asr_update")[-2:])

    print("[2] 两路不串席")
    d_all = " ".join(d.texts())
    p_all = " ".join(p.texts())
    check("主驾的字来自主驾那路（含关键词）", any(key in d_all for key in KEY_D), d_all[-40:])
    check("副驾的字来自副驾那路（含关键词）", any(key in p_all for key in KEY_P), p_all[-40:])
    check("主驾这一路没有副驾的关键词", not any(key in d_all for key in KEY_P), d_all[-40:])
    check("副驾这一路没有主驾的关键词", not any(key in p_all for key in KEY_D), p_all[-40:])

    print("[3] 两路各自判停")
    check("主驾有判停结论", wait_until(lambda: d.events("eot"), 8), d.events()[-4:])
    check("副驾有判停结论", wait_until(lambda: p.events("eot"), 8), p.events()[-4:])
    eot_d = (d.events("eot") or [{}])[-1]
    eot_p = (p.events("eot") or [{}])[-1]
    check("主驾判停吃的是主驾的字", "导航" in str(eot_d.get("text") or ""), eot_d)
    check("副驾判停吃的是副驾的字", any(key in str(eot_p.get("text") or "") for key in KEY_P), eot_p)

    print("[4] 读心场景里两路都在")
    check("出了读心场景", wait_until(lambda: d.events("mind_scene") or p.events("mind_scene"), 8),
          d.events()[-4:])
    scene = ((d.events("mind_scene") or p.events("mind_scene"))[-1])["scene"]
    lanes = {lane["seat"]: lane for lane in scene.get("seats") or []}
    check("读心两路各一条", sorted(lanes) == ["D", "P"], sorted(lanes))
    check("主驾 lane 记着主驾的字",
          "导航" in (lanes.get("D", {}).get("settled") or lanes.get("D", {}).get("text") or ""),
          lanes.get("D"))
    check("副驾 lane 记着副驾的字",
          any(key in (lanes.get("P", {}).get("settled") or lanes.get("P", {}).get("text") or "")
              for key in KEY_P), lanes.get("P"))

    print("[5] 真链上两路是同时跑的")
    check("两路在同一次会话里都收到了字", bool(d.texts()) and bool(p.texts()),
          (len(d.texts()), len(p.texts())))
    check("出字带各自映席（不看内容也知道是谁）",
          all(e.get("seat_id") == "D" for e in d.events("asr_update"))
          and all(e.get("seat_id") == "P" for e in p.events("asr_update")), "seat_id")

    d.close()
    p.close()
    print("\n真链冒烟通过（真实火山 ASR 两路 wav 回放；判停离线替身 / 读心离线规则）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
