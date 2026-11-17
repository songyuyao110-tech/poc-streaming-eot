"""双席 · 真实麦克风（/api/session/dual/mic）的契约与冒烟测。

用**脚本假 ASR**（注入 `server.DUAL_MIC_ASR_FACTORY`）跑离线链路：不起真实 WS？不起 —
这里起一个 Flask 开发服务器在后台线程，用两个真实 `simple_websocket.Client` 汇合成一对
（D / P），把 wav 当 16k/20ms PCM 帧推进去，走离线判停/选工具/决策（mock=true），
断言两路各自收口、不串席、duo_done。这是**可复现契约测**（不连火山/KEV/LLM）。

真链（真实 ASR 回放进两路 / 两路真实麦）见 tests/poc_dual_mic_wav_replay.py，
需要 CLOUD_ASR_*，标「真链未验」。

用法：
    /Users/YUYSONG/Work/tmp/jev-reopt-checkouts/dev-streaming-eot/.venv/bin/python -B tests/test_dual_mic_ws.py

（venv 走上游那份：本机 py3.9 缺 httpx2，server.py 会 import 上游 model_clients。）
"""
from __future__ import annotations

import base64
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

FRAME_MS, RATE = 20, 16000
FRAME = int(RATE * 2 * FRAME_MS / 1000)


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeASR:
    """脚本假 ASR：按累积字节数吐 partial，finish 时吐 final。

    形状对齐真 `ASRStream` 的 push/poll/finish/close 三件套。
    """

    SCRIPT = {"D": ("导航回家", 0.8), "P": ("把空调调到24度", 1.6)}
    BYTES_PER_SEC = RATE * 2

    def __init__(self, sample_rate, seat="D"):
        self.seat = seat
        self.pushed = 0
        self.partials_sent = 0
        self.final_sent = False

    def push(self, pcm, *, last=False):
        self.pushed += len(pcm)

    def poll(self):
        # 契约测试不关心流式部分；只需一次确定性的 final，让离线 mock 恰好消费整句。
        return []

    def finish(self, timeout_s=5.0):
        if self.final_sent:
            return []
        self.final_sent = True
        text, first_ms = self.SCRIPT[self.seat]
        return [(text, True, int(first_ms * 1000))]

    def close(self):
        pass


def start_server():
    port = free_port()
    th = threading.Thread(target=lambda: server.app.run(host="127.0.0.1", port=port,
                                                        debug=False, use_reloader=False),
                          daemon=True)
    th.start()
    # 等到端口可连（Flask 起线程后极快；最多等 15s）。
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.2):
                return port
        except OSError:
            time.sleep(0.05)
    raise RuntimeError("server did not start")


def check(label, condition, detail=""):
    print(("  ok   " if condition else "  FAIL ") + label + (("  | " + str(detail)) if detail and not condition else ""))
    if not condition:
        raise AssertionError(label + ("  | " + str(detail) if detail else ""))


def read_wav(path):
    with wave.open(str(path), "rb") as handle:
        if handle.getframerate() != RATE or handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise ValueError("need %d Hz mono 16-bit wav" % RATE)
        return handle.readframes(handle.getnframes())


def open_zoom(port, mock=True, config=None):
    import urllib.request
    body = {"mock": mock}
    if config:
        body["config"] = config
    req = urllib.request.Request(
        "http://127.0.0.1:%d/api/session/dual/mic/open" % port,
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def ws_connect(port, session_id, seat):
    from simple_websocket import Client
    url = "ws://127.0.0.1:%d/api/session/dual/mic?session_id=%s&seat=%s" % (port, session_id, seat)
    return Client.connect(url, receive_bytes=65536, max_message_size=1 << 22)


def feed_and_wait(port, session_id, wavs, stops_after=None, timeout=40):
    """对每个坐席：起一个线程喂 wav PCM，送回 stop，汇总收到的消息。返回 {seat: messages}。"""
    from simple_websocket import Client
    clients = {}
    lock = threading.Lock()
    collected = {}

    def recv_loop(seat, ws):
        msgs = []
        while True:
            try:
                message = ws.receive(timeout=0.2)
            except TimeoutError:
                continue                 # 没有新消息：继续等（不要因为一次静默就退出）
            except Exception:
                break                    # 连接已关闭/异常
            if message is None:
                continue             # simple_websocket: 超时返回 None（不是关闭）；关闭会抛 ConnectionClosed
            msgs.append(json.loads(message))
            with lock:
                collected[seat] = list(msgs)

    def feed(seat, pcm):
        ws = Client.connect("ws://127.0.0.1:%d/api/session/dual/mic?session_id=%s&seat=%s"
                            % (port, session_id, seat), receive_bytes=65536, max_message_size=1 << 22)
        clients[seat] = ws
        threading.Thread(target=recv_loop, args=(seat, ws), daemon=True).start()
        started = time.monotonic()
        for offset in range(0, len(pcm), FRAME):
            ws.send(pcm[offset:offset + FRAME])
            target = started + (offset + FRAME) / (RATE * 2)
            while time.monotonic() < target:
                time.sleep(0.002)
        ws.send(json.dumps({"cmd": "stop"}))

    threads = []
    for seat, pcm in wavs.items():
        t = threading.Thread(target=feed, args=(seat, pcm), daemon=True)
        t.start()
        threads.append(t)
    for t in threads:
        t.join()

    # 等到 duo_done 或超时。
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with lock:
            d = {k: list(v) for k, v in collected.items()}
        if any(any(m.get("kind") == "event" and m.get("data", {}).get("event") == "duo_done"
                   for m in msgs) for msgs in d.values()):
            break
        time.sleep(0.05)
    for ws in clients.values():
        try:
            ws.close()
        except Exception:
            pass
    return collected


def collect_events(ws, seconds):
    """收 `seconds` 秒内的事件包（按到达先后），返回原始 packet 列表。"""
    packets = []
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            message = ws.receive(timeout=0.2)
        except TimeoutError:
            continue
        except Exception:
            break
        if message is None:
            continue
        try:
            packets.append(json.loads(message))
        except (TypeError, ValueError):
            continue
    return packets


def _event_pairs(packets):
    """只留 `kind == event` 的 (事件名, data)，保持到达顺序。"""
    return [(m["data"].get("event"), m["data"]) for m in packets
            if m.get("kind") == "event" and isinstance(m.get("data"), dict)]


class SlowSceneEngine:
    """测试用假读心引擎：0.3s 后给一句场景，模拟「真模型读完那一刻」。不连外部服务。"""

    model = "fake-slow"

    def __init__(self, scene="我听到主驾说要导航回家"):
        self.scene = scene
        self.calls = []

    def describe(self):
        return {"name": "fake-slow", "model": self.model, "mode": "litellm_vl", "source": "llm",
                "vision": {"capability": "ok", "note": "测试假引擎（不连外部服务）"}}

    def judge(self, *, text, visual, seat="D", final=False, image=None, image_w=None,
              image_h=None, prior_texts=None):
        self.calls.append({"text": text, "final": final, "image": image})
        time.sleep(0.3)
        return {"result": {"scene": self.scene, "intent": "—", "target": "—", "topic": "—",
                           "confidence": 0.0, "reason": "fake", "cues": [], "finalized": bool(final)},
                "engine": {"name": self.model, "model": self.model, "mode": "litellm_vl",
                           "source": "llm", "fallback": False, "latency_ms": 300.0}}


class PollFinalASR(FakeASR):
    """靠 poll 吐 final 的假 ASR：不收尾也能出一句（验证「读完就推」不依赖下一句 / 下一帧）。"""

    def poll(self):
        if not self.final_sent and self.pushed >= RATE * 2 * 0.4:
            self.final_sent = True
            return [("导航回家", True, 0)]
        return []


COLOR_FRAME = "data:image/jpeg;base64," + base64.b64encode(b"color-frame" * 300).decode()


def frame_only_session(port, engine=None):
    """只开 D 席、只推一帧（不发音频、期间不再来第二帧）：读心出结果就该进对话流。"""
    old = server._mind_engine
    if engine is not None:
        # 只本用例注假引擎：读心「读完那一刻」由 0.3s 的假时延表示，不连真 LLM。
        server._mind_engine = lambda model_id=None: engine
    packets = []
    try:
        info = open_zoom(port, mock=True)
        ws = ws_connect(port, info["session_id"], "D")
        try:
            ws.send(json.dumps({"cmd": "frame", "w": 320, "h": 180, "brightness": 0.7, "motion": 0.0,
                                "image": COLOR_FRAME}))
            packets += collect_events(ws, 2.5)          # 只推了这一帧，后面没有画面再来
        finally:
            try:
                ws.send(json.dumps({"cmd": "stop"}))
                collect_events(ws, 0.5)                 # 收尾用，不计入断言
            except Exception:
                pass
            try:
                ws.close()
            except Exception:
                pass
    finally:
        server._mind_engine = old
    pairs = _event_pairs(packets)
    return {"order": [name for name, _ in pairs], "pairs": pairs,
            "mind_scenes": [data for name, data in pairs if name == "mind_scene"],
            "frame_seen": [data for name, data in pairs if name == "frame_seen"],
            "asr_updates": [data for name, data in pairs if name == "asr_update"]}


def mind_scene_after_final(port):
    """[4] 开口后读完：没有任何下一句字、也没有下一帧，读心结果也要立刻出现在对话流。"""
    engine = SlowSceneEngine()
    # 只本用例注假引擎：读心读完那句的「那一刻」由 0.3s 的假时延表示，不连真 LLM。
    server._mind_engine = lambda model_id=None: engine
    server.DUAL_MIC_ASR_FACTORY = lambda rate, seat: PollFinalASR(RATE, seat)
    info = open_zoom(port, mock=True)
    ws = ws_connect(port, info["session_id"], "D")
    packets = []
    try:
        # 只推 0.5s 静音音频（假 ASR 在 0.4s 后从 poll 吐 final）：不发 stop、不再发字、不发画面。
        for _ in range(int(0.5 * 1000 / FRAME_MS)):
            ws.send(b"\x00\x00" * (FRAME // 2))
            time.sleep(FRAME_MS / 1000.0)
        packets += collect_events(ws, 3.0)
    finally:
        try:
            ws.send(json.dumps({"cmd": "stop"}))
            collect_events(ws, 0.5)
        except Exception:
            pass
        try:
            ws.close()
        except Exception:
            pass
    pairs = _event_pairs(packets)
    names = [name for name, _ in pairs]
    final_at = next((index for index, (name, data) in enumerate(pairs)
                     if name == "asr_update" and data.get("final")), None)
    push_at = next((index for index, (name, data) in enumerate(pairs) if name == "mind_scene"), None)
    pushed_scene = next((data.get("scene") for name, data in pairs if name == "mind_scene"), None) or {}
    after = [name for name, _ in pairs[push_at + 1:]] if push_at is not None else []
    return {
        "events": names,
        "pushed": push_at is not None,
        "expect": engine.scene,
        "scene": pushed_scene.get("summary"),
        "after_final": final_at is not None and push_at is not None and push_at > final_at,
        "later_input": any(name in ("asr_update", "frame_seen") for name in after),
    }


class PartialOnlyASR(FakeASR):
    """只吐一版 partial、**永远不给 final** 的假 ASR：真 ASR 实测就有这种（说完静音、不再落 final）。"""

    def poll(self):
        if not self.final_sent and self.pushed >= RATE * 2 * 0.4:
            self.final_sent = True
            return [("导航回家", False, 0)]
        return []


def mind_scene_after_stable_partial(port):
    """[5] 只出 partial、没有 final：中枢静音窗把这句收住 → 读心读一次并推（不等下一句 / 下一个 final）。

    读心不拿「字稳住一小段」或「ASR 自己报结束」当请求理由：这句是**中枢**收住的
    （`TurnSession._confirm` 的 `max_silence` 静音窗），读心只听中枢的收住（wiki `[[读心请求时机]]`）。
    """
    engine = SlowSceneEngine("我听到主驾说要导航回家")
    old = server._mind_engine
    server._mind_engine = lambda model_id=None: engine
    server.DUAL_MIC_ASR_FACTORY = lambda rate, seat: PartialOnlyASR(RATE, seat)
    packets = []
    try:
        info = open_zoom(port, mock=True)
        ws = ws_connect(port, info["session_id"], "D")
        try:
            for _ in range(int(0.5 * 1000 / FRAME_MS)):
                ws.send(b"\x00\x00" * (FRAME // 2))
                time.sleep(FRAME_MS / 1000.0)
            packets += collect_events(ws, 3.0)
        finally:
            try:
                ws.send(json.dumps({"cmd": "stop"}))
                collect_events(ws, 0.5)
            except Exception:
                pass
            try:
                ws.close()
            except Exception:
                pass
    finally:
        server._mind_engine = old
    pairs = _event_pairs(packets)
    names = [name for name, _ in pairs]
    push_at = next((index for index, (name, _) in enumerate(pairs) if name == "mind_scene"), None)
    pushed_scene = next((data.get("scene") for name, data in pairs if name == "mind_scene"), None) or {}
    finals = [data for name, data in pairs if name == "asr_update" and data.get("final")]
    after = [name for name, _ in pairs[push_at + 1:]] if push_at is not None else []
    return {"events": names, "pushed": push_at is not None, "expect": engine.scene,
            "scene": pushed_scene.get("summary"), "finals": finals,
            "later_input": any(name in ("asr_update", "frame_seen") for name in after)}


def main():
    server.DUAL_MIC_ASR_FACTORY = lambda rate, seat: FakeASR(RATE, seat)
    # 本测一律离线：读心也走规则替身（不连真 LLM；[4] 才临时注入假引擎，仍不连外部服务）。
    # 与文件头「不连火山 / KEV / LLM」的契约一致，也不让每次跑测都打真模型。
    server.MIND_FORCE_OFFLINE = True

    # 需要一个 16k mono 16bit 的静音/短 wav 作「假音频流」——脚本假 ASR 不看内容，只数字节。
    # 生成一个 1.5s 的静音 wav 即可。
    tmp_wav = Path(ROOT) / "tmp" / "dual_mic_silence.wav"
    tmp_wav.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(tmp_wav), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(b"\x00\x00" * (RATE * 3 // 2))
    pcm_d = read_wav(tmp_wav)
    pcm_p = read_wav(tmp_wav)

    port = start_server()
    print("server on %d" % port)

    # --- [1] 双路同时喂 wav PCM → 各自收口、不串席、duo_done ---
    print("[1] 双路同时喂 wav → 各自 CONFIRMED/决策/收口 + duo_done")
    info = open_zoom(port, mock=True)
    check("open 返回 session_id + mic_of_seat", info.get("session_id") and info.get("mic_of_seat") == {"D": "mic-D", "P": "mic-P"}, info)
    sid = info["session_id"]
    collected = feed_and_wait(port, sid, {"D": pcm_d, "P": pcm_p})
    seat_events = {}
    for seat, msgs in collected.items():
        seat_events[seat] = [(m["data"].get("event"), m["data"])
                             for m in msgs if m.get("kind") == "event"]
    # 广播语义：两路都应收到 duo_done。断言合并去重后两席各 1 次 CONFIRMED/决策/收口。
    all_events = []
    for seat in ("D", "P"):
        all_events += [(seat, ev, data) for ev, data in seat_events.get(seat, []) if data.get("seat_id") == seat]
    duo_done = next((m["data"] for msgs in collected.values()
                     for m in msgs if m.get("kind") == "event" and m["data"].get("event") == "duo_done"),
                    None)
    check("收到 duo_done", duo_done is not None)
    if duo_done:
        summary = duo_done.get("summary") or {}
        plan = duo_done.get("plan") or {}
        check("plan 含两席", sorted((plan or {}).keys()) == ["D", "P"], plan)
        check("两席各 1 次决策", summary.get("actions") == 2, summary.get("actions"))
        check("两席各消费自己的文本", summary.get("seat_texts", {}).get("D") == ["导航回家"]
              and summary.get("seat_texts", {}).get("P") == ["把空调调到24度"], summary.get("seat_texts"))
        check("cross_cancel = 0（三硬之一）", summary.get("cross_cancel") == 0, summary.get("cross_cancel"))
        check("cross_reset = 0（三硬之一）", summary.get("cross_reset") == 0, summary.get("cross_reset"))
        # 串席文本 0：席 D 事件里不该带「空调」，席 P 不该带「导航」。
        for seat, foreign in (("D", "空调"), ("P", "导航")):
            leaked = [text for s, ev, data in all_events if s == seat and ev in ("asr_update", "segment_consumed")
                      for text in [str(data.get("text", ""))] if foreign in text]
            check("席 %s 事件里没有另一席内容" % seat, not leaked, leaked[:2])
    # 读心推给对话流的是**一份**场景（两路共用），不是每席各一条：
    mind_pushes = [m["data"] for msgs in collected.values() for m in msgs
                   if m.get("kind") == "event" and m["data"].get("event") == "mind_scene"]
    check("两路真麦：推的是同一份场景（shared，不带 seat_id）",
          bool(mind_pushes) and all(p.get("shared") is True and "seat_id" not in p for p in mind_pushes),
          [p.get("seat_id") for p in mind_pushes])
    shared = next((p["scene"] for p in reversed(mind_pushes)
                   if any(lane.get("text") or lane.get("settled")
                          for lane in p["scene"].get("seats") or [])), {})
    lanes = {lane["seat"]: lane for lane in shared.get("seats") or []}
    check("两席各自说的话都在这一份场景里（标了席别，不串成一条）",
          sorted(lanes) == ["D", "P"] and "导航回家" in (lanes["D"].get("settled") or "")
          and "空调" in (lanes["P"].get("settled") or "")
          and "主驾" in shared.get("summary", "") and "副驾" in shared.get("summary", ""),
          shared.get("summary"))

    # --- [2] 单路（只开 D）→ 只 D 收口，P 无事件（=那路没说话） ---
    print("[2] 只开一路 → 只该席收口，另一席无事件")
    info2 = open_zoom(port, mock=True)
    sid2 = info2["session_id"]
    collected2 = feed_and_wait(port, sid2, {"D": pcm_d})
    d_events = [m["data"]["event"] for msgs in collected2.values() for m in msgs
                if m.get("kind") == "event" and m["data"].get("seat_id") == "D"]
    p_present = any(m.get("kind") == "event" and m["data"].get("seat_id") == "P"
                    for msgs in collected2.values() for m in msgs)
    check("只开 D → D 有收口事件", any(e == "turn_done" for e in d_events), set(d_events))
    check("只开 D → P 一条事件都没有", not p_present)

    # --- [3] 没开口、只有画面 → 读心读完就把场景单推一条（不等出字、不等下一帧） ---
    print("[3] 没开口只有画面 → 读心读完立刻推进对话流（没有下一帧、没有出字）")
    engine3 = SlowSceneEngine("我看到了车里两个人，没人说话")
    cases3 = frame_only_session(port, engine3)
    scene3 = next((item.get("scene") or {} for item in cases3["mind_scenes"]), {})
    check("没开口也有读心结果推进对话流", bool((scene3.get("summary") or "").strip()), cases3["order"][:6])
    check("推的是模型读到的那一刻（只有那一帧，没被下一帧带出来）",
          scene3.get("summary") == engine3.scene and len(cases3["frame_seen"]) == 1,
          (scene3.get("summary"), len(cases3["frame_seen"])))
    check("整个过程没有 asr_update（不是靠出字触发的）", not cases3["asr_updates"], cases3["asr_updates"][:2])

    # --- [3b] 离线规则替身同样「没开口也有场景」：走画面回执那一条，页面照样看得见 ---
    print("[3b] 离线规则：没开口时画面回执里就带着当前场景")
    cases3b = frame_only_session(port)
    receipt_scene = (cases3b["frame_seen"] or [{}])[0].get("scene") or {}
    check("规则替身也能给「没开口」的场景", bool((receipt_scene.get("summary") or "").strip()), receipt_scene)
    check("两路都在这一份场景里（谁没说话那一路标空着）",
          [lane.get("seat") for lane in receipt_scene.get("seats") or []] == ["D", "P"],
          receipt_scene.get("seats"))

    # --- [4] 开口后读完 → 立刻推（不等下一句、不等再来一帧） ---
    print("[4] 开口后读完 → mind_scene 立刻出现（没有下一句也没有下一帧）")
    cases4 = mind_scene_after_final(port)
    check("读完那一刻就推上来了", cases4["pushed"], cases4["events"][-4:])
    check("推的是模型读出的那句（不是占位句/规则句）", cases4["scene"] == cases4["expect"], cases4["scene"])
    check("推送在 final 之后、且期间没有新的出字/画面", cases4["after_final"] and not cases4["later_input"], cases4)

    # --- [5] 只出 partial、没有 final（真 ASR 实测会出现）→ 中枢静音窗收住后读一次、照样推 ---
    print("[5] 只出 partial 没有 final → 中枢静音窗收住 → 读一次，出结果就推")
    cases5 = mind_scene_after_stable_partial(port)
    check("没有 final 也推上了（收住由中枢给，不是字稳住）",
          cases5["pushed"] and not cases5["finals"], cases5["events"][-5:])
    check("推的是模型读出的那句", cases5["scene"] == cases5["expect"], cases5["scene"])
    check("推完没有新的出字 / 画面（不靠下一拍）", not cases5["later_input"], cases5["events"][-5:])

    print("\n9 例契约通过（残留 server 线程无需清理，测试退出即随进程回收）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())