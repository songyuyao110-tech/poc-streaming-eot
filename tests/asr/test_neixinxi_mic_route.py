"""开麦路由离线验收（内心戏页 / 对话流页同一套）：选了哪支就必须开哪支，两路不能是同一份声音。

2026-11-12 用户实测的 bug：主驾选小米耳夹蓝牙、副驾选 Poly 有线，人离有线很远，两路音量却一直
完全一样、识别也是同一段话 —— 浏览器把两路都开成了同一支麦。设备名 / deviceId 都是浏览器自己
报的，不能当证据；本测就按**实际推到服务端的 PCM**验：

  [0] 纯函数：同一份声音（含错位）→ same_source；两段不同的声音 → separate；静音/不够 → unknown。
  [1] 同一份声音（两路收到完全一样的 PCM，回报的设备名不同、都报「一致」）：
      必须判出来（same_source），不能当两路已分开继续听；两路电平确实一模一样（这就是 bug 的样子）。
  [2] 真两支（两路电平走势明显不同、一近一远）：判成 ok；两路电平各走各的（不被画成一样）；
      两席的字只进自己那席（不串席）。
  [3] 浏览器给的不是所选那支（回报 match=false）：判成 device_mismatch（哪怕声音确实不同），
      文案写清两席「选的是哪支、实际给的是哪支」。
  [4] 未回报设备号（match=null）+ 两路仍是同一份声音：照样判 same_source（不能因为「浏览器没报」
      就放过）。
  [5] 两路都只是静音：判成 pending（不冒充「已确认分开」）。

用法（venv 走上游那份，本机 system python 缺 flask）：
    /Users/YUYSONG/Agents/navi-dual-system/.venv/bin/python -B tests/asr/test_neixinxi_mic_route.py
"""
from __future__ import annotations

import json
import os
import random
import socket
import sys
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ui"))
sys.path.insert(0, str(ROOT / "src"))
sys.dont_write_bytecode = True

for candidate in ("/Users/YUYSONG/Work/tmp/jev-reopt-checkouts/dev-streaming-eot",
                  "/Users/YUYSONG/Agents/navi-dual-system"):
    if not os.environ.get("POC_UPSTREAM") and (Path(candidate) / "model_clients.py").is_file():
        os.environ["POC_UPSTREAM"] = candidate

os.environ.setdefault("POC_MEMORY_ENABLED", "0")   # 离线用例不连真 memos
import server  # noqa: E402

RATE, FRAME_MS = 16000, 20
FRAME_SAMPLES = int(RATE * FRAME_MS / 1000)        # 320


def frame_at(amplitude):
    """一帧 16-bit 单声道 PCM：amplitude 决定这一帧的电平。"""
    value = int(max(-32768, min(32767, amplitude)))
    return (value.to_bytes(2, "little", signed=True)) * FRAME_SAMPLES


def speech_frames(seed, base, count=120):
    """一段「像说话」的电平走势：每 10 帧换一句、句内慢慢起伏（不是周期波形，不会被错位对齐）。"""
    rnd = random.Random(seed)
    frames, level = [], 1.0
    for index in range(count):
        if index % 10 == 0:
            level = rnd.uniform(0.15, 1.0)
        frames.append(frame_at(max(90, int(base * level * rnd.uniform(0.85, 1.15)))))
    return frames


SILENT_FRAMES = [frame_at(0) for _ in range(120)]


class ScriptASR:
    """脚本假 ASR：收到（过门槛的）音频才吐字；两席各一份、各说各的话。"""

    SCRIPT = {"D": "导航回家顺便买点水果", "P": "把空调调到二十四度风再小一点"}
    STEP = RATE * 2 * 0.3

    def __init__(self, sample_rate, seat):
        self.seat = seat
        self.pushed = 0
        self.next_at = self.STEP
        self.version = 0
        self.final_sent = False
        self.closed = False

    def push(self, pcm, *, last=False):
        if pcm and any(pcm):
            self.pushed += len(pcm)

    def poll(self):
        if self.closed or self.pushed < self.next_at:
            return []
        self.next_at += self.STEP
        self.version += 1
        take = min(len(self.SCRIPT[self.seat]), max(3, self.version * 2))
        return [(self.SCRIPT[self.seat][:take], False, 0)]

    def finish(self, timeout_s=5.0):
        if self.final_sent:
            return []
        self.final_sent = True
        return [(self.SCRIPT[self.seat], True, 0)]

    def close(self):
        self.closed = True


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server():
    port = free_port()
    threading.Thread(target=lambda: server.app.run(host="127.0.0.1", port=port,
                                                  debug=False, use_reloader=False), daemon=True).start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return port
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("server did not start")


def check(label, condition, detail=""):
    print(("  ok   " if condition else "  FAIL ") + label
          + (("  | " + str(detail)) if detail and not condition else ""))
    if not condition:
        raise AssertionError(label + ("  | " + str(detail) if detail else ""))


def post(port, path, body):
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read().decode())


class Seat:
    """一席连接：开 WS、后台收包，喂音频 / 命令。"""

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

    def send(self, pcm):
        self.ws.send(pcm)

    def cmd(self, payload):
        self.ws.send(json.dumps(payload))

    def claim(self, selected_label, actual_label, match):
        self.cmd({"cmd": "route", "seat": self.seat, "selected_label": selected_label,
                  "actual_label": actual_label, "match": match, "requested_exact": True})

    def events(self, name=None, seat=None):
        with self.lock:
            packets = list(self.packets)
        return [p["data"] for p in packets
                if p.get("kind") == "event" and (name is None or p["data"].get("event") == name)
                and (seat is None or p["data"].get("seat_id") == seat)]

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def feed_pair(left, right, left_frames, right_frames, speed=2.0):
    """两席同时推各自的音频（每 20ms 一帧；speed 只影响墙钟，不影响波形）。"""
    for index in range(max(len(left_frames), len(right_frames))):
        if index < len(left_frames):
            left.send(left_frames[index])
        if index < len(right_frames):
            right.send(right_frames[index])
        if speed > 0:
            time.sleep((FRAME_MS / 1000.0) / speed)


def wait_until(predicate, timeout=8.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def route_events(seat):
    return seat.events("mic_route")


def wait_route(seat, status, timeout=8.0):
    return wait_until(lambda: any(event.get("status") == status for event in route_events(seat)),
                      timeout=timeout)


def open_session(port, thresholds=None):
    body = {"thresholds": thresholds} if thresholds else {}
    return post(port, "/api/neixinxi/open", body)["session_id"]


def main():
    server.NEIXINXI_ASR_FACTORY = lambda rate, seat: ScriptASR(rate, seat)
    server.MIND_FORCE_OFFLINE = True

    # --- [0] 纯函数：判定只认实际收到的电平走势 ---
    print("[0] 纯函数：同一份声音 / 真好分开 / 静音判不了")
    same_left = speech_frames(1, 6000)
    same_levels = [server._neixinxi_dbfs(frame) for frame in same_left]
    shifted = same_levels[2:] + same_levels[:2]      # 同一份声音，两路 buffering 错开两帧
    check("同一份声音（错位 2 帧）判成 same_source",
          server.mic_route_verdict(same_levels, shifted)["status"] == "same_source",
          server.mic_route_verdict(same_levels, shifted))
    other_levels = [server._neixinxi_dbfs(frame) for frame in speech_frames(99, 3000)]
    check("两段走势不同的声音判成 separate",
          server.mic_route_verdict(same_levels, other_levels)["status"] == "separate",
          server.mic_route_verdict(same_levels, other_levels))
    silent = [server._neixinxi_dbfs(frame) for frame in SILENT_FRAMES]
    check("两路都只是静音：判不了（不冒充已分开）",
          server.mic_route_verdict(silent, silent)["status"] == "unknown",
          server.mic_route_verdict(silent, silent))
    check("音频不够（少于 0.6s）：判不了",
          server.mic_route_verdict(same_levels[:10], other_levels[:10])["status"] == "unknown",
          server.mic_route_verdict(same_levels[:10], other_levels[:10]))

    port = start_server()
    print("server on %d" % port)

    # --- [1] 两路同一份声音（回报的设备名可以不同）：必须识别出来 ---
    print("[1] 两路是同一份声音（浏览器表面上开了两支）：判 same_source，不当作两路分开")
    session = open_session(port)
    d, p = Seat(port, session, "D"), Seat(port, session, "P")
    d.claim("小米耳夹式蓝牙耳机", "小米耳夹式蓝牙耳机", True)
    p.claim("Poly Blackwire 320", "Poly Blackwire 320", True)
    shared_audio = speech_frames(5, 6000)
    feed_pair(d, p, shared_audio, list(shared_audio))
    check("判出「两路是同一份声音」", wait_route(d, "same_source"), route_events(d)[-1:] or None)
    check("另一席也收到同一条判定（广播）", any(e.get("status") == "same_source" for e in route_events(p)),
          route_events(p)[-1:] or None)
    event = [e for e in route_events(d) if e.get("status") == "same_source"][-1]
    check("文案写清「这一轮没开成两支」",
          "同一份声音" in event.get("message", "") and "没开成" in event.get("message", ""),
          event.get("message"))
    print("[6] 两路音量跟各路自己收到的声音走：同一份声音时确实一模一样（这就是 bug 的样子）")
    d_levels = [e["dbfs"] for e in d.events("level")]
    p_levels = [e["dbfs"] for e in p.events("level")]
    check("两路都有电平回执", bool(d_levels) and bool(p_levels), (d_levels[-2:], p_levels[-2:]))
    check("同一份声音时两路电平一致（跟 bug 观察一致）", d_levels[-1] == p_levels[-1],
          (d_levels[-1:], p_levels[-1:]))
    d.close()
    p.close()
    time.sleep(0.3)

    # --- [2] 真两支：电平走势明显不同、一近一远 ---
    print("[2] 真两支（一近一远）：判 ok；两路电平各走各的；两席的字不互串")
    session = open_session(port)
    d, p = Seat(port, session, "D"), Seat(port, session, "P")
    d.claim("小米耳夹式蓝牙耳机", "小米耳夹式蓝牙耳机", True)
    p.claim("Poly Blackwire 320", "Poly Blackwire 320", True)
    near_audio = speech_frames(11, 6000)          # 近讲：大声
    far_audio = speech_frames(23, 900)            # 远处那支：小声，而且走势不一样
    feed_pair(d, p, near_audio, far_audio)
    check("判成两路分开（ok）", wait_route(d, "ok"), route_events(d)[-1:] or None)
    event = [e for e in route_events(d) if e.get("status") == "ok"][-1]
    check("文案写清「两路已确认分开」", "确认分开" in event.get("message", ""), event.get("message"))
    levels_d = [e["dbfs"] for e in d.events("level")]
    levels_p = [e["dbfs"] for e in p.events("level")]
    check("两路都收到电平回执", bool(levels_d) and bool(levels_p), (levels_d[-2:], levels_p[-2:]))
    # 同一时刻两路电平不能一样：主驾那支近讲大声、副驾那支远离小声
    check("两路电平分开走（差值 > 8dB，不被画成一样）",
          abs(levels_d[-1] - levels_p[-1]) > 8, (levels_d[-1], levels_p[-1]))
    time.sleep(0.5)
    d_texts = [e["text"] for e in d.events("asr_update")]
    p_texts = [e["text"] for e in p.events("asr_update")]
    check("主驾的字只进主驾", any(t for t in d_texts)
          and all(t == "" or t in ScriptASR.SCRIPT["D"] for t in d_texts), d_texts[:3])
    check("副驾的字只进副驾", any(t for t in p_texts)
          and all(t == "" or t in ScriptASR.SCRIPT["P"] for t in p_texts), p_texts[:3])
    check("主驾这一路没有副驾的话", all("二十四度" not in t for t in d_texts), d_texts[:2])
    check("副驾这一路没有主驾的话", all("导航回家" not in t for t in p_texts), p_texts[:2])
    d.close()
    p.close()
    time.sleep(0.3)

    # --- [3] 浏览器给的不是所选那支（回报 match=false）---
    print("[3] 浏览器给的不是所选那支：判 device_mismatch（哪怕声音真的不同）")
    session = open_session(port)
    d, p = Seat(port, session, "D"), Seat(port, session, "P")
    d.claim("小米耳夹式蓝牙耳机", "Poly Blackwire 320", False)
    p.claim("Poly Blackwire 320", "Poly Blackwire 320", True)
    feed_pair(d, p, speech_frames(31, 6000), speech_frames(47, 900))
    check("判成「浏览器给的不是所选那支」", wait_route(d, "device_mismatch"), route_events(d)[-1:] or None)
    event = [e for e in route_events(d) if e.get("status") == "device_mismatch"][-1]
    check("文案写清两席「选的是哪支、实际给的是哪支」",
          "小米耳夹" in event.get("message", "") and "Poly" in event.get("message", "")
          and "没开成" in event.get("message", ""), event.get("message"))
    check("这一轮不算两路分开（没有 ok 结论）",
          not any(e.get("status") == "ok" for e in route_events(d)), route_events(d))
    d.close()
    p.close()
    time.sleep(0.3)

    # --- [4] 浏览器没回报设备号（match=null）+ 同一份声音：照样要判出来 ---
    print("[4] 未回报设备号 + 两路同一份声音：照样判 same_source（不因「没报」就放过）")
    session = open_session(port)
    d, p = Seat(port, session, "D"), Seat(port, session, "P")
    d.claim("小米耳夹式蓝牙耳机", "", None)
    p.claim("Poly Blackwire 320", "", None)
    shared_audio = speech_frames(58, 5000)
    feed_pair(d, p, shared_audio, list(shared_audio))
    check("仍判出「两路是同一份声音」", wait_route(d, "same_source"), route_events(d)[-1:] or None)
    d.close()
    p.close()
    time.sleep(0.3)

    # --- [5] 两路都只是静音：判 pending，不冒充已分开 ---
    print("[5] 两路都只是静音：判 pending（不冒充「已确认分开」）")
    session = open_session(port)
    d, p = Seat(port, session, "D"), Seat(port, session, "P")
    d.claim("小米耳夹式蓝牙耳机", "小米耳夹式蓝牙耳机", True)
    p.claim("Poly Blackwire 320", "Poly Blackwire 320", True)
    feed_pair(d, p, SILENT_FRAMES, SILENT_FRAMES)
    check("判成「还在确认」（pending）",
          wait_route(d, "pending"), route_events(d)[-1:] or None)
    event = [e for e in route_events(d) if e.get("status") == "pending"][-1]
    check("文案写明「还没听到能分辨的声音」",
          "还没听到" in event.get("message", "") or "确认" in event.get("message", ""),
          event.get("message"))
    check("静音时没有冒充两路分开",
          not any(e.get("status") == "ok" for e in route_events(d)), route_events(d))
    d.close()
    p.close()

    print("\n全部通过")


if __name__ == "__main__":
    main()
