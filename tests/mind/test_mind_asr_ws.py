"""读心页真麦通道（/api/mind/asr）的契约与冒烟测。

用**脚本假 ASR**（注入 `server.MIND_ASR_FACTORY`）跑离线链路：起一个 Flask 开发服务器，
用真 `simple_websocket.Client` 连 WS，把静音 PCM 帧当 16k/20ms 音频推上去，断言：

  1. 只回 `ready` / `asr`（partial→final）/ `stopped`，不建 TurnSession（不碰判停主链）；
  2. 字进得来：把收到的 final 走 `/api/mind/tick`（离线规则引擎）→ 读心快照里出现该句。

真链（真实火山豆包流式 ASR）见 tests/poc_mind_asr_live.py，需要 CLOUD_ASR_*，单独标「真链」。

用法：
    /Users/YUYSONG/Agents/navi-dual-system/.venv/bin/python -B tests/test_mind_asr_ws.py
"""
from __future__ import annotations

import json
import os
import socket
import sys
import threading
import time
import urllib.error
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


def free_port():
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class FakeASR:
    """脚本假 ASR：推够 1.2s 音频先吐 partial，finish 时吐 final。形状同 asr_stream.ASRStream。"""

    TEXT = "导航回家"

    def __init__(self, sample_rate):
        self.pushed = 0
        self.partials = 0
        self.final_sent = False

    def push(self, pcm, *, last=False):
        self.pushed += len(pcm)

    def poll(self):
        if self.pushed >= int(RATE * 2 * 0.6) and not self.partials:   # 0.6s 后吐一次 partial
            self.partials += 1
            return [("导航", False, None)]
        return []

    def finish(self, timeout_s=5.0):
        if self.final_sent:
            return []
        self.final_sent = True
        return [(self.TEXT, True, 1200)]

    def close(self):
        pass


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


def check(label, condition, detail=""):
    print(("  ok   " if condition else "  FAIL ") + label
          + (("  | " + str(detail)) if detail and not condition else ""))
    if not condition:
        raise AssertionError(label + (("  | " + str(detail)) if detail else ""))


def silence_pcm(seconds=1.5):
    path = ROOT / "tmp" / "mind_asr_silence.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1); handle.setsampwidth(2); handle.setframerate(RATE)
        handle.writeframes(b"\x00\x00" * int(RATE * seconds))
    with wave.open(str(path), "rb") as handle:
        return handle.readframes(handle.getnframes())


def mic_round(port, pcm, timeout=15):
    """连 /api/mind/asr 推 PCM → 收 ready/asr → 送 stop → 收 stopped（真链 3 类消息）。"""
    from simple_websocket import Client
    url = "ws://127.0.0.1:%d/api/mind/asr" % port
    ws = Client.connect(url, receive_bytes=65536, max_message_size=1 << 22)
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
            messages.append(json.loads(message))
            if messages[-1].get("kind") == "stopped":
                done.set()

    reader = threading.Thread(target=recv, daemon=True)
    reader.start()
    for offset in range(0, len(pcm), FRAME):        # 真时间轴推流（20ms 一帧）
        ws.send(pcm[offset:offset + FRAME])
        time.sleep(FRAME_MS / 1000.0)
    ws.send(json.dumps({"cmd": "stop"}))
    done.wait(timeout)
    try:
        ws.close()
    except Exception:
        pass
    return messages


def mind_tick(port, text, final, model=None):
    body = {"session_id": "mind-test-1", "seat": "D", "asr": {"text": text, "final": final}}
    if model:
        body["model"] = model
    req = urllib.request.Request("http://127.0.0.1:%d/api/mind/tick" % port,
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode())


def mind_tick_error(port, model):
    body = {"session_id": "mind-test-bad", "seat": "D", "model": model,
            "asr": {"text": "导航回家", "final": True}}
    req = urllib.request.Request("http://127.0.0.1:%d/api/mind/tick" % port,
                                 data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read().decode())


def mind_get(port, path):
    with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path)) as resp:
        return json.loads(resp.read().decode())


def main():
    server.MIND_ASR_FACTORY = lambda rate: FakeASR(RATE)
    server.MIND_FORCE_OFFLINE = True     # 强制离线规则引擎：契约测不连真模型
    pcm = silence_pcm()
    port = start_server()
    print("server on %d" % port)

    # --- [1] WS 契约 ---
    print("[1] /api/mind/asr：ready → asr(partial) → asr(final) → stopped")
    before_mic_sessions = dict(server.MIC_SESSIONS)
    messages = mic_round(port, pcm)
    kinds = [m.get("kind") for m in messages]
    ready = next((m["data"] for m in messages if m.get("kind") == "ready"), None)
    check("收到 ready", ready is not None, kinds)
    check("ready 带 session_id / 采样率", bool(ready and ready.get("session_id")
          and ready.get("sample_rate") == RATE and ready.get("frame_ms") == FRAME_MS), ready)
    asr = [m["data"] for m in messages if m.get("kind") == "asr"]
    check("先有 partial", any(not item["final"] and item["text"] == "导航" for item in asr), asr)
    check("stop 后有 final", any(item["final"] and item["text"] == FakeASR.TEXT for item in asr), asr)
    check("收尾有 stopped", "stopped" in kinds, kinds)
    check("没有 error", not any(m.get("kind") == "error" for m in messages), messages)
    check("不建 TurnSession（不碰判停主链）", server.MIC_SESSIONS == before_mic_sessions,
          set(server.MIC_SESSIONS) ^ set(before_mic_sessions))

    # --- [2] 字进读心：partial/final 走 tick ---
    print("[2] 收到的字按 tick 进读心：partial → 场景刷新，final → 场景落定")
    snap_partial = mind_tick(port, asr[0]["text"], False)
    check("partial 出现在快照", snap_partial["asr"]["partial"] == "导航", snap_partial["asr"])
    snap_final = mind_tick(port, FakeASR.TEXT, True)
    check("final 落段", any(item["text"] == FakeASR.TEXT for item in snap_final["asr"]["finals"]),
          snap_final["asr"]["finals"])
    check("final 已给场景（不判接不接）", bool(snap_final.get("scene", {}).get("summary"))
          and "decision" not in snap_final, snap_final.get("scene"))

    # --- [3] UI 契约：主路径绑定真麦 ---
    print("[3] mind.html：真麦是主路径，演示/手打降为兜底")
    html = (ROOT / "ui" / "mind.html").read_text()
    for needle in ("/api/mind/asr", "id=\"micStart\"", "id=\"micPill\"", "id=\"micLevel\"",
                   "createScriptProcessor", "startMic()"):
        check("mind.html 含 " + needle, needle in html)
    for needle in ("id=\"enginePick\"", "switchModel", "mind.engine", "body.model = state.model"):
        check("mind.html 含引擎选择器 " + needle, needle in html)

    # --- [4] 页面引擎三选一：目录 / 按 model 路由 / 未知 id 400 ---
    print("[4] 页面三选一：目录与按 model 路由（本测强制离线，只验契约、不连真模型）")
    demos = mind_get(port, "/api/mind/demos")
    mids = [item["id"] for item in demos.get("models") or []]
    check("三档目录", mids == ["cc/glm-5.2", "cc/glm-5.3-flash", "deepseek-v4.1-flash"], mids)
    check("默认档 deepseek-v4.1-flash", demos.get("default_model") == "deepseek-v4.1-flash",
          demos.get("default_model"))
    check("默认标记只有一档", [item["id"] for item in demos.get("models") or [] if item.get("default")]
          == ["deepseek-v4.1-flash"], [item.get("id") for item in demos.get("models") or []])
    for item in demos.get("models") or []:
        check("选项带视觉/快慢标签 " + item["id"],
              bool(item.get("vision_label") and item.get("speed_label")), item)
    for mid in mids:
        snap = mind_tick(port, "导航回家", True, model=mid)
        check("model=" + mid + " 可路由", snap["engine"].get("model") == mid
              or snap["engine"].get("name") == "fast_multimodal_stub", snap["engine"])
    code, payload = mind_tick_error(port, "cc/glm-9.9-flash")
    check("未知模型被拒（400，不偷偷换档）", code == 400 and "三选一" in payload.get("error", ""),
          (code, payload))

    # --- [5] 冷启动回归：先被选中的是「非默认档」时，其它档不能被一起拖成离线（本单踩过的坑）---
    print("[5] 引擎缓存按档独立：冷启动先选非默认档，默认/其它档仍建得出来")
    from mind import mind_llm

    class StubEngine:
        def __init__(self, model_id):
            self.model = model_id

    real_factory = mind_llm.engine_for_model
    try:
        mind_llm.engine_for_model = lambda model_id: StubEngine(model_id)
        server.MIND_FORCE_OFFLINE = False
        server.MIND_ENGINES.clear()
        first = server._mind_engine("cc/glm-5.3-flash")
        other = server._mind_engine("deepseek-v4.1-flash")
        default = server._mind_engine("cc/glm-5.2")
        check("冷启动先选非默认档后，其它档仍建成真引擎",
              all(item is not None for item in (first, other, default)))
        check("每档缓存各自模型",
              [item.model for item in (first, other, default)]
              == ["cc/glm-5.3-flash", "deepseek-v4.1-flash", "cc/glm-5.2"])
    finally:
        mind_llm.engine_for_model = real_factory
        server.MIND_ENGINES.clear()
        server.MIND_FORCE_OFFLINE = True

    print("\n全部契约通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
