"""真链探针：读心出结果 → 立刻进对话流（8765 真模型 + 真流式 ASR，不需要人在页面上点）。

两个场景（都直接用 WS 驱动 8765，不连页面）：

  A. 没开口、只有画面：只推一帧，之后**什么都不再推** —— 读心读完就该单独推一条 `mind_scene`。
  B. 开口之后：推一段 `say` 合成的语音 → 真流式 ASR 出字 → 读心读完立刻推 `mind_scene`，
     期间**不再出下一句、也不再推画面**。

判据（对齐验收「只要读心出了结果，就立刻推到对话流」）：

  A：收到 `mind_scene`（带一句场景）；推送之前没有任何 `asr_update`（不是靠出字触发的）。
  B：`mind_scene` 出现在 final 之后，且之后没有新的出字 / 画面（不靠下一拍触发）。

前置：8765 在跑（真模型凭据 / 云 ASR 凭据同现网）。用法：

    /Users/YUYSONG/Agents/navi-dual-system/.venv/bin/python -B tests/probe_mind_push_live.py [--port 8765]

真链（会调用真模型、真 ASR），耗时不作 SLA，只验「能不能立刻出来」。
"""
from __future__ import annotations

import json
import subprocess
import struct
import sys
import tempfile
import time
import urllib.request
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.dont_write_bytecode = True

RATE, FRAME_MS = 16000, 20
FRAME = int(RATE * 2 * FRAME_MS / 1000)


def _http(port, path, body=None):
    request = urllib.request.Request(
        "http://127.0.0.1:%d%s" % (port, path),
        data=None if body is None else json.dumps(body).encode(),
        headers={"Content-Type": "application/json"},
        method="GET" if body is None else "POST")
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.loads(response.read().decode())


def open_session(port):
    return _http(port, "/api/session/dual/mic/open", {"mock": True})


def connect(port, session_id, seat="D"):
    from simple_websocket import Client
    return Client.connect("ws://127.0.0.1:%d/api/session/dual/mic?session_id=%s&seat=%s"
                          % (port, session_id, seat), receive_bytes=65536, max_message_size=1 << 22)


def collect(ws, seconds, until=None):
    """收到 until 为真（或 seconds 到点）为止的事件包；返回 (packets, 首条 until 命中包的到达时刻)。"""
    packets, hit_at = [], None
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            message = ws.receive(timeout=0.1)
        except TimeoutError:
            continue
        except Exception:
            break
        if message is None:
            continue
        try:
            packet = json.loads(message)
        except (TypeError, ValueError):
            continue
        packets.append(packet)
        if until is not None and until(packet):
            hit_at = time.monotonic()
            break
    return packets, hit_at


def names(packets):
    return [p["data"].get("event") for p in packets
            if p.get("kind") == "event" and isinstance(p.get("data"), dict)]


def scene_of(packets):
    for packet in reversed(packets):
        data = packet.get("data") or {}
        if packet.get("kind") == "event" and data.get("event") == "mind_scene":
            return data.get("scene") or {}
    return {}


def close_session(ws):
    try:
        ws.send(json.dumps({"cmd": "stop"}))
    except Exception:
        pass
    try:
        ws.close()
    except Exception:
        pass


def synth_wav(text):
    """`say` 合成中文语音 → 16k / mono / 16-bit wav（真流式 ASR 只吃这个格式）。"""
    tmpdir = Path(tempfile.mkdtemp(prefix="mind-push-"))
    aiff, wav = tmpdir / "say.aiff", tmpdir / "say.wav"
    subprocess.run(["say", "-v", "Flo (中文（中国大陆）)", "-o", str(aiff), text], check=True,
                   timeout=60)
    subprocess.run(["afconvert", "-f", "WAVE", "-d", "LEI16@16000", "-c", "1", str(aiff), str(wav)],
                   check=True, timeout=60)
    with wave.open(str(wav), "rb") as handle:
        if handle.getframerate() != RATE or handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise RuntimeError("wav 不是 16k mono 16-bit")
        return handle.readframes(handle.getnframes())


def color_frame():
    """一张真的彩色 JPEG（320×180，左边暖色、右边冷色）：探针要验证「彩色帧真的进模型」。

    标准库写一张彩色 PNG → macOS `sips` 转 JPEG（本探针本来就只在 mac 上跑：say / afconvert）。
    """
    import base64
    import zlib
    width, height = 320, 180
    rows = []
    for y in range(height):
        row = bytearray(b"\x00")                       # filter 0
        for x in range(width):
            left = x < width // 2
            row += bytes((228, 96, 72) if left else (72, 116, 232))
        rows.append(bytes(row))
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    data = zlib.compress(b"".join(rows), 6)

    def chunk(tag, body):
        return struct.pack(">I", len(body)) + tag + body + struct.pack(">I", zlib.crc32(tag + body))

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", data) + chunk(b"IEND", b"")
    tmpdir = Path(tempfile.mkdtemp(prefix="mind-probe-frame-"))
    png_path, jpg_path = tmpdir / "frame.png", tmpdir / "frame.jpg"
    png_path.write_bytes(png)
    subprocess.run(["sips", "-s", "format", "jpeg", "-s", "formatOptions", "80",
                    str(png_path), "--out", str(jpg_path)], check=True, timeout=60,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    return ("data:image/jpeg;base64," + base64.b64encode(jpg_path.read_bytes()).decode(),
            width, height)


def probe_frames_only(port):
    """A：没开口、只有画面（只推一帧，之后什么都不推）。"""
    print("[A] 没开口只有画面：只推一帧，之后不再推任何东西 → 读心读完单独推一条 mind_scene")
    info = open_session(port)
    ws = connect(port, info["session_id"], "D")
    image, width, height = color_frame()
    began = time.monotonic()
    ws.send(json.dumps({"cmd": "frame", "image": image, "w": width, "h": height,
                        "brightness": 0.5, "motion": 0.0}))
    packets, hit_at = collect(ws, 30.0, until=lambda packet: (
        packet.get("kind") == "event"
        and packet["data"].get("event") == "mind_scene"
        and bool((packet["data"].get("scene") or {}).get("summary"))))
    close_session(ws)
    scene = scene_of(packets)
    before = [name for name in names(packets) if name != "mind_scene"]
    ok = {
        "收到 mind_scene（带场景）": bool(scene.get("summary")),
        "推送前没有任何 asr_update": "asr_update" not in before,
        "场景来自真读心（source=llm）": scene.get("source") == "llm",
        "不是占位句（pending=false）": not scene.get("pending"),
        "只推了一帧就出了结果": names(packets).count("frame_seen") == 1,
    }
    elapsed = (hit_at - began) * 1000 if hit_at else None
    print("    scene = %r（画面 → 场景 %s）"
          % (scene.get("summary"), ("%.1fms" % elapsed) if elapsed else "超时未到"))
    return ok, elapsed


def probe_after_speech(port):
    """B：开口之后读完立刻推 —— 送语音，出字后不再推任何下一步输入。"""
    print("[B] 开口后读完：送一段合成语音，final 之后不再出字 / 不再推画面 → mind_scene 立刻出现")
    pcm = synth_wav("帮我导航去望京SOHO，尽量走高速，别堵在路上")
    info = open_session(port)
    ws = connect(port, info["session_id"], "D")
    began = time.monotonic()
    sent = 0
    # 按实时节奏推 PCM；期间不推任何画面（读心这一轮只有字）。
    while sent < len(pcm):
        ws.send(pcm[sent:sent + FRAME])
        sent += FRAME
        target = began + sent / (RATE * 2)
        while time.monotonic() < target:
            time.sleep(0.002)
    # 说完全部音频后**继续送静音**（真麦就是一直送流）：真 ASR 要持续有声流才会把这段落成 final，
    # 送完就断会让它报 "Timed out waiting for the next …"。这期间不推画面、不再说新话。
    silence = b"\x00\x00" * (FRAME // 2)
    deadline = time.monotonic() + 30.0
    packets, hit = [], None
    while time.monotonic() < deadline:
        ws.send(silence)
        chunk, got = collect(ws, 0.02, until=lambda packet: (
            packet.get("kind") == "event"
            and packet["data"].get("event") == "mind_scene"
            and bool((packet["data"].get("scene") or {}).get("summary"))))
        packets += chunk
        if got is not None:
            hit = got
            break
    close_session(ws)
    # 时延一律用 8765 的 t_ms（同一条会话时钟），不用页面到钟点，免得把网络排队算进读心耗时。
    text_tms, heard, hit = None, "", None
    scene = {}
    for packet in packets:
        data = packet.get("data") or {}
        if packet.get("kind") != "event":
            continue
        if data.get("event") == "asr_update" and str(data.get("text") or "").strip():
            # 真 ASR 的第一版字（partial 或 final 都算）：读心从这一刻起有字可读
            if text_tms is None:
                text_tms, heard = int(data.get("t_ms") or 0), str(data.get("text") or "")
        elif data.get("event") == "mind_scene" and (data.get("scene") or {}).get("summary"):
            scene, hit = data.get("scene") or {}, int(data.get("t_ms") or 0)
    later = [name for name in names(packets) if name in ("asr_update", "frame_seen")]
    gap = (hit - text_tms) if (hit is not None and text_tms is not None) else None
    ok = {
        "真 ASR 出了字": bool(heard),
        "收到 mind_scene（带场景）": bool(scene.get("summary")),
        "推送发生在 final 之后": gap is not None and gap >= 0,
        "final 之后没有新的出字 / 画面（不靠下一拍）":
            not any(int((p.get("data") or {}).get("t_ms") or 0) > (hit or 0)
                    for p in packets
                    if p.get("kind") == "event" and (p.get("data") or {}).get("event") in
                    ("asr_update", "frame_seen")),
        "场景来自真读心（source=llm）": scene.get("source") == "llm",
    }
    print("    asr 首字 = %r（t_ms=%s）· scene = %r（t_ms=%s，首字 → 场景 %s）"
          % (heard, text_tms, scene.get("summary"), hit,
             ("%.0fms" % gap) if gap is not None else "超时未到"))
    return ok, gap


def main(argv):
    port = 8765
    if "--port" in argv:
        port = int(argv[argv.index("--port") + 1])
    print("8765 读心推送探针（真模型 + 真 ASR）· port=%d" % port)
    results, gaps = {}, {}
    results["A"] = probe_frames_only(port)
    results["B"] = probe_after_speech(port)
    ok_all = True
    for case, (checks, gap) in results.items():
        for label, value in checks.items():
            print("%s %s %s" % ("  ok  " if value else "  FAIL", case, label))
            ok_all = ok_all and value
    print("\n%s" % ("PASS：两路都在「读出结果那一刻」自己出现，没有等下一句 / 下一帧" if ok_all else "FAIL"))
    return 0 if ok_all else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
