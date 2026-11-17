"""内心戏页（/neixinxi）的离线契约测：两路识别 / 音量门槛 / 两路判停 / 读心旁白。

用**脚本假 ASR**（注入 `server.NEIXINXI_ASR_FACTORY`）不起真实麦克风、不连火山/LLM：
起一个 Flask 开发服务器在后台线程，用两个真实 `simple_websocket.Client` 当两席（D / P），
把 16k/20ms PCM 推进去，断言：

  [1] 开页契约：一页一 session，两席各一路；响应里写清「只接判停」的边界。
  [2] 两路并行且不串席：两席同时说，各自只收自己的字；读心里两路各一条 lane。
  [3] 音量门槛：没过线的一路**不送识别**（没有字）；调高门槛立刻让这一路安静；调回来立刻恢复。
  [4] 判停接到两路识别：每路出字各自判停，只有「说完」才可确认。
  [5] 判停来源可切真 8009：`eot=real` 走 `gate.turn_eot.score_eot`（本测用假函数，不连网）。
  [6] 画面同一路：送一帧彩色 data URI → 读心收到同一张（不转灰阶），页上可显示。
  [7] 本页不建 TurnSession：没有闸门 / Agent 对象（不开闸、不进 Agent）；记忆只在**收住那一刻**写
      （本离线用例把真 memos 关掉：只断言「有一条写入口、没有查进 Agent」）。

用法（venv 走上游那份，本机 system python 缺 flask）：
    /Users/YUYSONG/Agents/navi-dual-system/.venv/bin/python -B tests/asr/test_neixinxi_page.py
"""
from __future__ import annotations

import json
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
FRAME = int(RATE * 2 * FRAME_MS / 1000)          # 320 samples = 640 bytes
COLOR_FRAME = ("data:image/jpeg;base64," + "A" * 40)


def tone(ms, amplitude):
    """一段 16-bit 单声道 PCM：amplitude 决定电平（过不过门槛由服务端按 dBFS 判）。"""
    value = int(max(-32768, min(32767, amplitude)))
    return (value.to_bytes(2, "little", signed=True)) * int(RATE * ms / 1000)


SILENCE = b"\x00\x00" * (RATE // 100)            # 10 ms 静音，-90 dBFS 起底


class ScriptASR:
    """脚本假 ASR：**只在收到音频时**每 0.3 秒吐一版新字，finish 时吐 final。

    形状对齐真 `ASRStream` 的 push/poll/finish/close；两席各一份、各说各的话：
    门槛没过线的这一路不会被推音频 → 一版字都吐不出来（本测靠这个证明门槛真的挡住了识别）。
    每版往后**多出两个字**（真字，不是标点）：一句收住另起之后仍会有「新字」可出（与真识别同形）。
    """

    SCRIPT = {"D": "导航回家顺便买点水果", "P": "把空调调到二十四度风再小一点"}
    STEP = RATE * 2 * 0.3                        # 0.3 秒音频出一版字
    FIRST = 4                                    # 第一版先给 4 个字（够判「这是哪一路的话」）

    def __init__(self, sample_rate, seat):
        self.seat = seat
        self.pushed = 0
        self.next_at = self.STEP
        self.version = 0
        self.final_sent = False
        self.closed = False

    def push(self, pcm, *, last=False):
        # 服务端对「没过线」的帧送**静音保活**（识别会话不因停顿被收掉）；假识别只把有声音的帧算数，
        # 所以门槛没过的那一路仍然一版字都吐不出来（和真识别一致：静音不产字）。
        if pcm and any(pcm):
            self.pushed += len(pcm)

    def _text(self):
        script = self.SCRIPT[self.seat]
        take = min(len(script), max(self.FIRST, self.version * 2))
        return script[:take]

    def poll(self):
        if self.closed or self.pushed < self.next_at:
            return []
        self.next_at += self.STEP
        self.version += 1
        return [(self._text(), False, 0)]

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
    """一席连接：开 WS、后台收包，喂音频/命令。"""

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

    def cmd(self, payload):
        self.ws.send(json.dumps(payload))

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


def wait_until(predicate, timeout=6.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def main():
    # 本测一律离线：假 ASR + 规则读心，不连火山 / KEO / LLM。
    server.NEIXINXI_ASR_FACTORY = lambda rate, seat: ScriptASR(rate, seat)
    server.MIND_FORCE_OFFLINE = True

    port = start_server()
    print("server on %d" % port)

    # --- [1] 开页契约 ---
    print("[1] 开页契约：一页一 session、两席各一路、边界写清")
    info = post(port, "/api/neixinxi/open", {})
    check("返回 session_id", bool(info.get("session_id")), info)
    check("两席各一路", [s["seat"] for s in info["seats"]] == ["D", "P"], info["seats"])
    check("默认门槛 -45dBFS 两路", info["thresholds"] == {"D": -45.0, "P": -45.0}, info["thresholds"])
    check("判停默认离线替身（与调试页同一口径）", info["eot"]["mode"] == "offline", info["eot"])
    check("边界：只接判停、不接要不要回答/不开闸/不进 Agent；记忆只在收住时写",
          all(word in info["limits"] for word in ("判停", "要不要回答", "开闸", "Agent", "记忆")), info["limits"])
    check("开页回执写明记忆状态（只报开没开 / 挂在谁 / 凭据来源，不含密钥）",
          isinstance(info.get("memory"), dict)
          and all(word in info["memory"] for word in ("enabled", "kind", "user_id")),
          info.get("memory"))

    # --- [2] 两路并行且不串席 ---
    print("[2] 两路同时说：各自只收自己的字，读心里两路各一条")
    sid = info["session_id"]
    d, p = Seat(port, sid, "D"), Seat(port, sid, "P")
    threads = [threading.Thread(target=d.feed, args=(tone(1600, 6000),), daemon=True),
               threading.Thread(target=p.feed, args=(tone(1600, 5000),), daemon=True)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    check("两席都收到了字", wait_until(lambda: d.events("asr_update") and p.events("asr_update"), 5),
          (len(d.events("asr_update")), len(p.events("asr_update"))))
    # 两席各自收尾：这一席最后一句落 final（判停/读心都以这句末稿为准）
    d.cmd({"cmd": "stop"})
    p.cmd({"cmd": "stop"})
    check("两席各自落 final", wait_until(
        lambda: any(e.get("final") for e in d.events("asr_update"))
        and any(e.get("final") for e in p.events("asr_update")), 6),
        (d.events("asr_update")[-1:], p.events("asr_update")[-1:]))
    d_texts = [e["text"] for e in d.events("asr_update")]
    p_texts = [e["text"] for e in p.events("asr_update")]
    # 一句收住另起之后，这一席后续的每一版只剩「这一句之后的新字」（是这话的后半截，空的表示这一版
    # 全是已经用掉的字）。所以按「每一版都出自这一席自己那一段话」判串席，而不是要求每版都含整句。
    check("主驾的字只进主驾（每一版都出自主驾自己那段话）",
          any(t for t in d_texts) and all(t == "" or t in ScriptASR.SCRIPT["D"] for t in d_texts),
          d_texts[:3])
    check("副驾的字只进副驾（每一版都出自副驾自己那段话）",
          any(t for t in p_texts) and all(t == "" or t in ScriptASR.SCRIPT["P"] for t in p_texts),
          p_texts[:3])
    check("主驾这一路没有副驾的话", all("二十四度" not in t for t in d_texts), d_texts[:2])
    check("副驾这一路没有主驾的话", all("导航回家" not in t for t in p_texts), p_texts[:2])

    # 收到读心场景：两路都在（谁说的归谁，不串成一条）
    check("读心出了场景（规则离线档也能出）", wait_until(lambda: d.events("mind_scene"), 6),
          [e.get("event") for e in d.events()][:8])
    scenes = [e["scene"] for e in d.events("mind_scene")]
    check("读心场景里两路都在", any(len(s.get("seats") or []) == 2 for s in scenes),
          [(s.get("seats") or [])[:1] for s in scenes][:2])
    check("主驾 lane 记的是主驾的字",
          any("导航回家" in ((lane.get("settled") or "") + (lane.get("text") or ""))
              for s in scenes for lane in (s.get("seats") or []) if lane.get("seat") == "D"), "D lane")
    check("副驾 lane 记的是副驾的字",
          any("二十四度" in ((lane.get("settled") or "") + (lane.get("text") or ""))
              for s in scenes for lane in (s.get("seats") or []) if lane.get("seat") == "P"), "P lane")

    # --- [4] 判停接到两路识别 ---
    print("[4] 判停：每路出字各自判停，只有「说完」可确认")
    check("主驾有判停结论", wait_until(lambda: d.events("eot"), 6), d.events())
    eot_d = d.events("eot")[-1]
    check("主驾判停吃的是主驾这一版的字", "导航回家" in eot_d["text"], eot_d)
    check("判停只给三态 + 可不可确认（没有接不接/开闸）",
          set(eot_d.keys()) >= {"label", "may_confirm", "scores"}
          and not ({"accept", "route", "open"} & set(eot_d.keys())), sorted(eot_d.keys()))
    if eot_d["label"] == "complete":
        check("「说完」才可以确认", eot_d["may_confirm"] is True, eot_d)
    else:
        check("没说完全就不确认", eot_d["may_confirm"] is False, eot_d["label"])
    p_eot = p.events("eot")
    check("副驾也有自己的判停结论", bool(p_eot) and "二十四度" in p_eot[-1]["text"], p_eot[-1:] or None)
    check("两路判停互不串（主驾结论里没有副驾的字）",
          all("二十四度" not in e["text"] for e in d.events("eot")), [e["text"] for e in d.events("eot")])

    # --- [6] 画面：送一帧彩色 data URI，读心收到同一张 ---
    print("[6] 画面：彩色 data URI 原样进读心（不转灰阶），页上可见")
    d.cmd({"cmd": "frame", "image": COLOR_FRAME, "w": 320, "h": 180, "brightness": 0.62, "motion": 0.0})
    check("收到画面回执", wait_until(lambda: d.events("frame_seen"), 5), d.events("frame_seen")[-1:])
    frame_seen = d.events("frame_seen")[-1]
    check("回执写着这一帧的尺寸（320×180）", frame_seen.get("w") == 320 and frame_seen.get("h") == 180,
          frame_seen)
    check("画面回执里带着当前场景", bool(frame_seen.get("scene")), frame_seen.get("scene"))

    d.close()
    p.close()
    time.sleep(0.4)

    # --- [3] 音量门槛：没过线就不识别；调高门槛立刻安静；调回来立刻恢复 ---
    print("[3] 门槛：安静的一路不出字；调高门槛立刻安静；调回来立刻恢复")
    info3 = post(port, "/api/neixinxi/open", {"thresholds": {"D": -45, "P": -45}})
    sid3 = info3["session_id"]
    d3, p3 = Seat(port, sid3, "D"), Seat(port, sid3, "P")
    quiet = threading.Thread(target=p3.feed, args=(SILENCE * 12,), daemon=True)   # 约 1.2 秒纯静音
    quiet.start()
    d3.feed(tone(900, 5000))                                                     # 约 -16 dBFS，过 -45 线
    quiet.join()
    check("过线的一路正常出字", wait_until(lambda: d3.events("asr_update"), 4), d3.events()[:4])
    before = len(p3.events("asr_update"))
    check("没过线的一路一版字都没有", before == 0, p3.events("asr_update")[:2])
    check("没过线的一路电平回执是「安静」",
          wait_until(lambda: any(e.get("quiet") for e in p3.events("level")), 3),
          p3.events("level")[-1:] or None)
    check("过线的一路电平回执是「过线」",
          any(not e.get("quiet") for e in d3.events("level")), d3.events("level")[-2:])

    # 调高门槛到 -12（高于这一路约 -16 的电平）→ 立刻安静
    d3.cmd({"cmd": "threshold", "value": -12})
    check("门槛改动被服务端确认", wait_until(
        lambda: any(p.get("kind") == "threshold" and p["data"].get("value") == -12.0 for p in d3.packets), 3),
        [p for p in d3.packets if p.get("kind") != "event"][-3:])
    time.sleep(0.6)                      # 等门槛余留窗（400ms）过去，再取冻结点
    frozen = len(d3.events("asr_update"))
    d3.feed(tone(900, 5000))
    time.sleep(0.6)
    check("门槛调高后这一路不再出字", len(d3.events("asr_update")) == frozen,
          d3.events("asr_update")[frozen:][:3])
    check("门槛调高后电平回执变「安静」",
          wait_until(lambda: d3.events("level") and d3.events("level")[-1].get("quiet"), 3),
          d3.events("level")[-1:])
    # 调回 -45 → 立刻恢复
    d3.cmd({"cmd": "threshold", "value": -45})
    resumed = len(d3.events("asr_update"))
    d3.feed(tone(900, 5000))
    check("门槛调回后这一路恢复出字", wait_until(lambda: len(d3.events("asr_update")) > resumed, 4),
          len(d3.events("asr_update")))
    d3.close()
    p3.close()
    time.sleep(0.3)

    # --- [5] 真 8009 分支：来源可切，且真的调 score_eot（不连网）---
    print("[5] 判停来源可切真 8009：eot=real 走 gate.turn_eot.score_eot")
    import gate.turn_eot as turn_eot
    real_calls = []
    original = turn_eot.score_eot

    def fake_score_eot(text, last_reply="", **kwargs):
        real_calls.append(text)
        return {"complete": 0.92, "cut": 0.04, "wait": 0.04, "label": "complete", "choice": 1,
                "confidence": 0.92, "margin": 0.88, "model": "fake-8009", "wall_ms": 12.0}

    turn_eot.score_eot = fake_score_eot
    try:
        info5 = post(port, "/api/neixinxi/open", {"eot": "real"})
        check("eot=real 被接受", info5["eot"]["mode"] == "real" and "8009" in info5["eot"]["label"], info5["eot"])
        d5 = Seat(port, info5["session_id"], "D")
        d5.feed(tone(1200, 6000))
        check("真判停分支真的调了 score_eot", wait_until(lambda: real_calls, 5), real_calls[:2])
        check("喂进去的是这一路的字", any("导航回家" in text for text in real_calls), real_calls[:2])
        check("判停结论按真 8009 来源标注",
              wait_until(lambda: any(e.get("source_mode") == "real" for e in d5.events("eot")), 4),
              [e.get("source_mode") for e in d5.events("eot")])
        d5.close()
        time.sleep(0.3)
    finally:
        turn_eot.score_eot = original

    # --- [7] 本页不建 TurnSession：没有闸门 / 记忆 / Agent ---
    print("[7] 本页不建 TurnSession：不开闸、不进 Agent；记忆只有「收住写入」这一条通路")
    zoom = server.NEIXINXI_SESSIONS.get(sid3) or server.NEIXINXI_SESSIONS.get(sid)
    check("内心戏会话不是 DualSeatSession / TurnSession",
          zoom is not None and not hasattr(zoom, "session") and not hasattr(zoom, "gates"), 
          [name for name in dir(zoom) if name in ("session", "gates", "memory", "ledger")])
    # 记忆在这张卡里接上了（收住那一刻写一刀 + 查回来给页面看），但**不查进 Agent**、不开闸：
    # 本离线用例把真 memos 关掉（POC_MEMORY_ENABLED=0），断言「只有写入口、没有成功写」，不连外网。
    store = getattr(zoom, "memory", None) if zoom is not None else None
    check("记忆只有「收住写入」这一条通路（本页不查进 Agent）",
          store is not None and not hasattr(zoom, "session"), type(store).__name__)
    check("离线用例里记忆是空实现（没有成功写 / 没连真 memos）",
          store is not None and not getattr(store, "enabled", False)
          and all(item.get("status") != "ok" for item in getattr(store, "writes", [])),
          getattr(store, "status", lambda: "absent")())

    print("\n7 组契约通过（假 ASR + 规则读心，未连火山 / KEO / 真模型）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
