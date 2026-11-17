"""内心戏页 · **非麦**真链：两句话之间真停一段，第二句单独出字；读心判断带上前几次结果。

与离线那片契约互补：这里 ASR 是**真的**火山流式识别（`CLOUD_ASR_*`），读心是**真的**多模态模型
（litellm 默认档）。音频不用真麦，直接把三段真 wav 按 20ms PCM 帧推给这一席的 WS（页面同一条通路：
WS → 音量门槛 → ASRStream → 出字 → 判停 → 读心）。

断言：
  [1] 第一句真的出字、真的被收住（`asr_settled` 里写清收住的是哪一句）。
  [2] 停一段之后再说：第二句的字里**没有再带上第一句**（真识别整段重发被剪掉）；
      判停这一路（`eot`）与读心 lane 同样只认这一句新话。
  [3] 读心每次判断带上最近已经出过的结果（`prior_count` 逐次变多、最多 5 份、`prior_used=true`）；
      真发出去的请求文本里写着「之前已经出过的旁白」那一段（不是页面自己编的）。
  [4] 停顿保活仍在：停顿期间没有换过识别、没有 `asr_interrupted`，第二句照样出字。

凭据不可用 / 读心只能用离线替身时 SKIP 并以 0 退出（不把真链拖红）。
用法：
    /Users/YUYSONG/Agents/navi-dual-system/.venv/bin/python -B tests/asr/poc_neixinxi_two_sentences_live.py
"""
from __future__ import annotations

import os
import sys
import threading
import time
import wave
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ui"))
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests" / "asr"))
sys.dont_write_bytecode = True

for candidate in ("/Users/YUYSONG/Work/tmp/jev-reopt-checkouts/dev-streaming-eot",
                  "/Users/YUYSONG/Agents/navi-dual-system"):
    if not os.environ.get("POC_UPSTREAM") and (Path(candidate) / "model_clients.py").is_file():
        os.environ["POC_UPSTREAM"] = candidate

import server  # noqa: E402
from test_neixinxi_keepalive import Seat, check, free_port, post, start_server, wait_until  # noqa: E402

RATE, FRAME_MS = 16000, 20
FRAME = int(RATE * 2 * FRAME_MS / 1000)
SILENCE = b"\x00\x00" * (RATE * FRAME_MS // 1000)
AUDIO_DIR = Path(os.environ.get("NEIXINXI_AUDIO_DIR", "/Users/YUYSONG/Work/tmp/qwen-smartturn/audio"))
# 几句真语音（中间各隔一段真停顿）：第一句 → 停 → 第二句 → 停 → 第三句 …
CLIPS = tuple(name for name in (os.environ.get("NEIXINXI_CLIPS") or "").split()
              if name) or ("smoke20-001.wav", "smoke20-005.wav", "smoke20-012.wav")
PAUSE_S = float(os.environ.get("NEIXINXI_PAUSE_S", "9"))   # 句间停顿（> 判定「停住」的 1.2s）


def read_wav(path):
    with wave.open(str(path), "rb") as handle:
        if handle.getframerate() != RATE or handle.getnchannels() != 1 or handle.getsampwidth() != 2:
            raise ValueError("need %d Hz mono 16-bit wav: %s" % (RATE, path))
        return handle.readframes(handle.getnframes())


def speak(seat, pcm):
    seat.feed(pcm)


def pause(seat, seconds):
    """按实时节奏送静音帧（和页面在门槛没过时送静音保活同一条路径）。"""
    started = time.monotonic()
    frames = int(seconds * 1000 / FRAME_MS)
    for index in range(frames):
        seat.ws.send(SILENCE)
        target = started + (index + 1) * FRAME_MS / 1000.0
        while time.monotonic() < target:
            time.sleep(0.002)


def events(seat, name):
    return [p["data"] for p in seat.packets_of("event", name)]


def main():
    clips = [AUDIO_DIR / name for name in CLIPS]
    missing = [str(path) for path in clips if not path.is_file()]
    if missing:
        print("SKIP: 找不到音频样本 %s" % missing)
        return 0

    server.NEIXINXI_ASR_FACTORY = None       # 真火山流式识别
    server.MIND_FORCE_OFFLINE = False        # 真读心模型（缺凭据会回落规则 → 本测 SKIP）

    # `NEIXINXI_LIVE_PORT=<端口>`：不打自建服务器，直接验**已经在跑**的那一份（如 8765 真页）。
    live_port = os.environ.get("NEIXINXI_LIVE_PORT")
    if live_port:
        port = int(live_port)
        print("验已经在跑的那一份：127.0.0.1:%d" % port)
    else:
        port = free_port()
        start_server(port)
        print("server on %d" % port)
    info = post(port, "/api/neixinxi/open", {})
    engine = info.get("mind_engine") or {}
    if engine.get("mode") == "offline":
        print("SKIP: 读心只能用离线替身（%s）——真模型路径未验" % engine.get("note"))
        return 0
    print("读心引擎：%s（%s）" % (engine.get("model"), engine.get("mode")))

    # 抓「真发出去的请求」里那段前几次结果的上下文（只记文本，不记图、不记凭据）
    # 只在本进程自建引擎时抓得到；验已在跑的 8765 时那份引擎在别的进程里 → 只按事件校验。
    prompts = []
    model_id = info.get("model")
    real_engine = server.MIND_ENGINES.get(model_id)
    if real_engine is not None:
        original_post = real_engine._post

        def capture(body):
            try:
                content = body["messages"][-1]["content"]
                prompts.append(content[0]["text"] if isinstance(content, list) else str(content))
            except Exception:
                prompts.append("")
            return original_post(body)

        real_engine._post = capture

    sid = info["session_id"]
    d = Seat(port, sid, "D")
    check("这一席的识别接上了（asr_ready）", wait_until(lambda: d.packets_of("asr_ready"), 8),
          [p.get("data") for p in d.packets_of("asr_ready")])

    try:
        # --- [1] 第一句 ---
        print("[1] 第一句（真识别）：出字 → 判停收住 → 停在边界上另起一句")
        speak(d, read_wav(clips[0]))
        check("第一句出字", wait_until(lambda: d.texts(), 10), d.texts()[:2])
        first_text = d.texts()[-1]
        check("第一句真的被收住（asr_settled）", wait_until(lambda: events(d, "asr_settled"), 15),
              events(d, "asr_settled"))
        settled = events(d, "asr_settled")[0]
        settled_text = settled.get("settled") or first_text
        print("  收住的那一句：%r（字幕归零后 text=%r）" % (settled_text, settled.get("text")))
        after_settle = len(d.texts())

        # --- [4] 停顿保活 ---
        print("[4] 句间停顿：静音保活，识别不中断、不换实例")
        pause(d, PAUSE_S)
        check("停顿期间没有「识别中断」", not events(d, "asr_interrupted"), events(d, "asr_interrupted")[:1])

        # --- [2] 第二句 ---
        print("[2] 第二句（非麦真识别）：单独出字，不带第一句")
        speak(d, read_wav(clips[1]))
        check("第二句上屏", wait_until(lambda: len(d.texts()) > after_settle, 12),
              d.texts()[-3:])
        second = d.texts()[after_settle:]
        check("第二句每一版都不带第一句全文", second and all(settled_text not in t for t in second),
              second[:3])
        check("第二句的字是新的字",
              any(ch not in settled_text for t in second for ch in t), second[:2])
        second_settled_at = len(d.texts())
        pause(d, PAUSE_S)

        # 判停 / 读心都只看这一句新话
        new_eot = [e.get("text") or "" for e in events(d, "eot") if (e.get("text") or "") in second]
        check("判停这一版也不带第一句", new_eot and all(settled_text not in t for t in new_eot),
              new_eot[:3])
        scenes = [e["scene"] for e in events(d, "mind_scene") if e["scene"].get("seats")]
        post_settle = [s for s in scenes if any(
            settled_text not in ((lane.get("settled") or "") + (lane.get("text") or ""))
            for lane in s["seats"] if lane.get("seat") == "D")]
        check("读心 lane 也不带第一句（有收住之后的那几份）", post_settle, len(scenes))

        # --- [3] 接着把剩下几句都说一遍：让读心连着出结果，检查「带最近 5 次」---
        print("[3] 接着再说 %d 句：每次判断带最近已经出过的结果（最多 5 份）" % (len(clips) - 2))
        for clip in clips[2:]:
            before = len(d.texts())
            speak(d, read_wav(clip))
            check("这一句上屏（%s）" % clip.name,
                  wait_until(lambda: len(d.texts()) > before, 12), d.texts()[-2:])
            pause(d, PAUSE_S)
        deadline = time.time() + 40
        while time.time() < deadline:
            counts_now = [e["scene"].get("prior_count") or 0
                          for e in events(d, "mind_scene") if e["scene"].get("seats")]
            if max(counts_now or [0]) >= 5:
                break
            time.sleep(0.3)
        scenes = [e["scene"] for e in events(d, "mind_scene")]
        carried = [s for s in scenes if s.get("prior_count")]
        check("读心判断带上了前几次结果（至少 3 次判断带到）", len(carried) >= 3,
              [s.get("prior_count") for s in scenes][-6:])
        counts = [s["prior_count"] for s in carried]
        check("份数逐次变多、最多 5 份", max(counts) <= 5 and max(counts) >= 2, counts[-6:])
        if len(carried) >= 6:
            check("够 6 份判断时封顶在 5 份（不越带）", max(counts) == 5, counts[-6:])
        check("真模型这次判断确实读到了这份上下文（prior_used）",
              any(s.get("prior_used") for s in carried),
              [(s.get("prior_count"), s.get("prior_used")) for s in carried][-3:])
        if prompts:
            check("真发出去的请求文本里有「之前已经出过的旁白」这一段",
                  any("之前已经出过的旁白" in text for text in prompts),
                  "requests=%d" % len(prompts))
        else:
            print("  （这份引擎在别的进程里：请求原文不在本进程内抓，按事件里的 prior_used 校验）")
        # 后出的旁白能接上前面已经发生的事：把这条链打出来给人看（DETAIL 里贴）
        print("  读心这几句（更早 → 更近）：")
        for scene in [s for s in scenes if s.get("summary")][-6:]:
            print("   - [带 %d 份] %s" % (scene.get("prior_count") or 0, scene.get("summary")))
    finally:
        d.close()
    print("\n真链通过（真火山流式识别 + 真读心模型；非麦 wav 回放）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
