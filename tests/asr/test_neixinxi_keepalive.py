"""内心戏页 · 同一轮收听「中途停了就不出字」的离线契约（脚本假识别，不连火山 / LLM）。

本卡主因（已核实）：没过线的那一路原来**一个包都不送**，识别服务按「还有没有音频」判会话生死，
停顿几秒就整段会话被收掉（45000081）；之后这台 worker 退出，再开口的音频被悄悄丢掉，
页面也没有中断提示 —— 表现就是「先说得出字，中间停了就再也不出字」。

本测用脚本假识别（注入 `server.NEIXINXI_ASR_FACTORY`）钉住三件事：

  [1] 长停顿：没过线的帧要送**静音保活**（识别会话不被收掉），停顿期间不出现「识别中断」；
      停顿后再说，照样出字。
  [2] 中途断：识别流被收掉 / 抛错时，服务端自动接回来（页面先收到 `asr_interrupted`，
      再接回后收到 `asr_ready{recovered:true}`），后面的音频仍出字（音频不丢）。
  [3] 失败要说出来：初始连识别引擎失败 → 页面收到 `kind=error stage=asr`；
      服务日志只记错误类型（不记原文、不出现任何凭据）。

用法：`/Users/YUYSONG/Agents/navi-dual-system/.venv/bin/python -B tests/asr/test_neixinxi_keepalive.py`
"""
from __future__ import annotations

import json
import logging
import os
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
FRAME = int(RATE * 2 * FRAME_MS / 1000)          # 640 bytes
# 一整段话按 0.4 秒一句往后长（火山在同一段语音里就是这么整段重发的）。
# 第二句（「便利店 顺便买点 水果」）是留给「停顿后再开口」那一步的新话：
# 收住之后另起一句，这一版应当只剩新字（不带前面「导航回家附近有什么」）。
PHRASES = {"D": ["导航", "回家", "附近有什么", "便利店", "顺便买点", "水果"],
           "P": ["把空调", "调到", "二十四度", "风再小一点"]}


def tone(ms, amplitude=4000):
    """有声帧：一个常数幅度的方波近似（假识别只看「有没有声音」）。"""
    return (int(amplitude).to_bytes(2, "little", signed=True)
            * int(RATE * ms / 1000))


SILENCE = b"\x00\x00" * (RATE * FRAME_MS // 1000)


class PausableASR:
    """脚本假识别：全零帧 = 静音（含服务端保活），不计入有声；有声累计到 0.4s 出一版新字。

    `drop_after_voiced` 设为「再收多少有声字节就抛一次错」，用来模拟识别流中途被收掉；
    重连后会新建一份实例（同一个 seat 的短语继续往后长）。
    """
    INSTANCES: dict = {}
    VERSION: dict = {"D": 0, "P": 0}
    FAIL_SEATS: dict = {}          # seat -> Exception（构造时直接失败，模拟连不上识别引擎）

    def __init__(self, sample_rate, seat):
        failure = self.FAIL_SEATS.get(seat)
        if failure is not None:
            raise failure
        self.seat = seat
        self.pushed = 0
        self.voiced = 0
        self.silence = 0
        self.text = ""
        self.version = 0
        self.drop_after_voiced = None
        self.closed = False
        self.close_calls = 0
        PausableASR.INSTANCES[seat] = self

    @staticmethod
    def _voiced(pcm):
        return any(pcm)

    def push(self, pcm, *, last=False):
        if self.closed:
            return
        size = len(pcm or b"")
        self.pushed += size
        if self._voiced(pcm):
            self.voiced += size
        else:
            self.silence += size

    def poll(self):
        if self.closed:
            return []
        if self.drop_after_voiced is not None and self.voiced >= self.drop_after_voiced:
            self.drop_after_voiced = None
            raise RuntimeError("scripted ASR drop")
        if self.voiced - self.version * int(RATE * 2 * 0.4) >= int(RATE * 2 * 0.4):
            self.version += 1
            phrases = PHRASES[self.seat]
            self.text = "".join(phrases[:min(self.version, len(phrases))])
            PausableASR.VERSION[self.seat] = self.version
            return [(self.text, False, None)]
        return []

    def finish(self, timeout_s=5.0):
        return [(self.text, True, None)] if self.text else []

    def close(self):
        self.closed = True
        self.close_calls += 1


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def start_server(port):
    threading.Thread(target=lambda: server.app.run(host="127.0.0.1", port=port,
                                                   debug=False, use_reloader=False),
                     daemon=True).start()
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("server did not start")


def post(port, path, body):
    request = urllib.request.Request("http://127.0.0.1:%d%s" % (port, path),
                                     data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request) as response:
        return json.loads(response.read().decode())


def check(label, condition, detail=""):
    print(("  ok   " if condition else "  FAIL ") + label
          + (("  | " + str(detail)) if detail and not condition else ""))
    if not condition:
        raise AssertionError(label + ("  | " + str(detail) if detail else ""))


def wait_until(predicate, timeout=6.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


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

    def feed(self, pcm, speed=1.0):
        started = time.monotonic()
        for offset in range(0, len(pcm), FRAME):
            self.ws.send(pcm[offset:offset + FRAME])
            target = started + ((offset + FRAME) / (RATE * 2)) / speed
            while time.monotonic() < target:
                time.sleep(0.002)

    def packets_of(self, kind=None, event=None):
        with self.lock:
            packets = list(self.packets)
        result = []
        for packet in packets:
            data = packet.get("data") or {}
            if kind is not None and packet.get("kind") != kind:
                continue
            if event is not None and data.get("event") != event:
                continue
            result.append(packet)
        return result

    def texts(self):
        return [p["data"].get("text") for p in self.packets_of("event", "asr_update")]

    def close(self):
        try:
            self.ws.close()
        except Exception:
            pass


def main():
    server.NEIXINXI_ASR_FACTORY = lambda rate, seat: PausableASR(rate, seat)
    server.MIND_FORCE_OFFLINE = True
    logs = []
    handler = logging.Handler()
    handler.emit = lambda record: logs.append(record.getMessage())
    server.app.logger.addHandler(handler)
    server.app.logger.setLevel(logging.INFO)

    port = free_port()
    start_server(port)
    print("server on %d" % port)

    # --- [1] 长停顿：静音保活，会话不被收掉，停顿后照样出字 ---
    print("[1] 长停顿：没过线的帧送静音保活；停顿后再说仍出字")
    info = post(port, "/api/neixinxi/open", {})
    sid = info["session_id"]
    d = Seat(port, sid, "D")
    check("识别引擎接上（asr_ready）", wait_until(lambda: d.packets_of("asr_ready"), 5),
          [p.get("kind") for p in d.packets])
    stream = PausableASR.INSTANCES["D"]

    d.feed(tone(1200))
    check("第一段出字", wait_until(lambda: any("导航" in t for t in d.texts()), 4), d.texts())

    silence_ms = 14000
    d.feed(b"\x00" * (RATE * 2 * 14))    # 14 秒纯静音（门槛不过线）
    keepalive = stream.silence
    check("停顿期间送的是静音保活（不是把这一席的话送进识别）",
          keepalive >= int(RATE * 2 * 13 * 0.9), keepalive)
    check("停顿期间识别流没被收掉（没有 asr_interrupted）",
          not d.packets_of("event", "asr_interrupted"),
          d.packets_of("event", "asr_interrupted"))
    check("停顿期间没有换过识别实例（会话还在）",
          PausableASR.INSTANCES.get("D") is stream, id(PausableASR.INSTANCES.get("D")))

    d.feed(tone(2400))
    after_pause = len(d.texts())
    check("停顿后再开口，新的话照样上屏",
          wait_until(lambda: any("便利店" in t for t in d.texts()), 6), d.texts()[-3:])
    # 本卡验收 4：第二句是**单独一句**，不是「上一句 + 这一句」
    d.feed(tone(1600))
    check("停顿后这一版另起一句（不带已经用掉的上一句）",
          wait_until(lambda: len(d.texts()) > after_pause, 5), d.texts()[-3:])
    new_texts = d.texts()[after_pause:]
    check("第二句的字里没有上一句全文", new_texts and all("导航" not in t and "回家" not in t
                                                     for t in new_texts), new_texts[:3])
    check("判停这一版也不带上一句（只判这一句新话）",
          (d.packets_of("event", "eot")[-1]["data"].get("text") in new_texts),
          [p["data"].get("text") for p in d.packets_of("event", "eot")][-2:])

    # --- [2] 中途断：自动接回来，后面的音频继续出字 ---
    print("[2] 识别流中途被收掉：自动接回（页面收到中断与已接回），后续仍出字")
    stream = PausableASR.INSTANCES["D"]
    stream.drop_after_voiced = stream.voiced + int(RATE * 2 * 0.6)
    d.feed(tone(1600))
    check("页面收到「识别中断」（asr_interrupted）",
          wait_until(lambda: d.packets_of("event", "asr_interrupted"), 5),
          [p.get("data") for p in d.packets_of("event", "asr_interrupted")])
    check("服务端自动接回（asr_ready recovered=true）",
          wait_until(lambda: [p for p in d.packets_of("asr_ready")
                              if (p.get("data") or {}).get("recovered")], 6),
          [p.get("data") for p in d.packets_of("asr_ready")])
    new_stream = PausableASR.INSTANCES.get("D")
    check("重连后是新的识别实例（旧的那份已关）", new_stream is not stream and stream.close_calls >= 1,
          {"same": new_stream is stream, "close_calls": stream.close_calls})

    before = len(d.texts())
    d.feed(tone(2400))
    check("断掉之后再说，仍然上屏", wait_until(lambda: len(d.texts()) > before, 6), d.texts()[-3:])

    # --- [3] 初始识别失败：错误包直达页面；日志只记类型 ---
    print("[3] 初始连不上识别引擎：页面能收到错误；日志只记类型、不记原文")
    PausableASR.FAIL_SEATS["P"] = RuntimeError("boom: token=should-never-be-logged")
    info2 = post(port, "/api/neixinxi/open", {})
    sid2 = info2["session_id"]
    p = Seat(port, sid2, "P")
    check("页面收到 stage=asr 的错误包",
          wait_until(lambda: [x for x in p.packets_of("error")
                              if (x.get("data") or {}).get("stage") == "asr"], 6),
          [x.get("data") for x in p.packets_of("error")])
    error_data = p.packets_of("error")[0]["data"]
    check("错误包写明阶段与错误类型", error_data.get("stage") == "asr"
          and error_data.get("error_type") == "RuntimeError", error_data)
    check("错误原文里不带凭据原文（长串已被抹掉）",
          "should-never-be-logged" not in json.dumps(error_data, ensure_ascii=False),
          error_data.get("error"))
    joined = "\n".join(logs)
    check("服务日志记了错误类型", "neixinxi seat P asr error: RuntimeError" in joined,
          [line for line in logs if "neixinxi" in line][-3:])
    check("服务日志没有任何凭据原文", "should-never-be-logged" not in joined
          and "CLOUD_ASR" not in joined, [line for line in logs if "neixinxi" in line][-3:])

    p.close()
    d.close()
    time.sleep(0.2)
    print("\n3 组契约通过（脚本假识别；真识别链见 tests/asr/poc_neixinxi_wav_replay.py + 本卡非麦验收）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
