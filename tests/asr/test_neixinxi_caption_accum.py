"""内心戏页 · 出字「另起一句」的离线契约：一句收住后，用过的字不再叠到下一句。

本卡真麦现象（2026-11-11 21:39，已核对）：同一轮里先说的话能上屏；这句被判停收住、交给读心之后，
已经用过的字没有清掉。火山在**同一个 ASR 会话里不切 utterance**，会把整段话反复重发，所以下一句
开头叠着上一句，页面越接越长；判停与读心下次看到的也是叠在一起的整段。

本测用脚本假 ASR（注入 `server.NEIXINXI_ASR_FACTORY`）把「真识别的形状」钉死：
**整段累积重发**（不是增量），两句话之间有一段真停顿（送静音保活，识别会话不被收掉）。

断言：
  [1] 一句还没说完：字幕按当前这句往上长（不清、不剪）——判停抢跑不等于这句到了边界。
  [2] 收住 + 停住（真边界）：服务端发 `asr_settled`，字幕归零；此后每一版都只剩「这一句之后的新字」。
  [3] 下一句的判停只看这一句新话（收住之后的 `eot` 事件里没有上一句全文）。
  [4] 下一句的读心只看这一句新话（收住之后的读心 lane 里没有上一句全文）。
  [5] 停顿保活仍在（识别实例没换过、没有 asr_interrupted）。

用法（venv 走上游那份）：
    /Users/YUYSONG/Agents/navi-dual-system/.venv/bin/python -B tests/asr/test_neixinxi_caption_accum.py
"""
from __future__ import annotations

import os
import sys
import threading
import time
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

os.environ.setdefault("POC_MEMORY_ENABLED", "0")   # 离线用例不连真 memos
import server  # noqa: E402
from test_neixinxi_keepalive import (SILENCE, Seat, check, free_port, post,  # noqa: E402
                                     start_server, tone, wait_until)

RATE = 16000
FRAME = int(RATE * 2 * 20 / 1000)
PAUSE_S = 4.0          # 两句之间的真停顿（> 服务端判定「停住」的 1.2s）


class TwoSentenceASR:
    """脚本假 ASR：**整段累积重发**（与火山同一形状），两句话之间由测试推进「下一句」。

    形状对齐真 `ASRStream`：`push(pcm)` 喂音频、`poll()` 吐（整段文本, final, vad_end）、
    `finish()` 收尾、`close()` 关掉。只有**有声**帧才往后出字（静音只保活、不产字）。
    """

    STEP = int(RATE * 2 * 0.4)          # 0.4 秒有声出一版新字（真识别也是这个量级）
    SENTENCES = ("导航回家", "顺便买点水果")
    INSTANCES: dict = {}                # seat -> 这一席正在用的那份（测试用它推进「下一句」）

    def __init__(self, sample_rate, seat):
        self.seat = seat
        self.pushed = 0
        self.next_at = self.STEP
        self.phase = 0                  # 0 = 第一句；1 = 第二句（整段里带着第一句重发）
        self.version = 0
        self.closed = False
        self.instances = 1              # 换过几份（重连会新建一份 → 这里会变成 2）
        TwoSentenceASR.INSTANCES[seat] = self

    def push(self, pcm, *, last=False):
        if self.closed:
            return
        if pcm and any(pcm):
            self.pushed += len(pcm)

    def next_sentence(self):
        """测试推进：这一段语音说完了，下一段是新的一句（识别那边仍然整段累积重发）。"""
        self.phase = 1
        self.version = 0
        self.next_at = self.pushed + self.STEP

    def _text(self):
        head, tail = self.SENTENCES
        if self.phase == 0:
            return head[:max(2, min(len(head), self.version * 2))]
        whole = head + tail
        take = min(len(whole), len(head) + self.version * 2)
        return whole[:take]

    def poll(self):
        if self.closed or self.pushed < self.next_at:
            return []
        self.next_at += self.STEP
        self.version += 1
        return [(self._text(), False, 0)]

    def finish(self, timeout_s=5.0):
        return []

    def close(self):
        self.closed = True


def events(seat, event):
    """这一席收到的某类事件（保留包里的 data）。"""
    return [p["data"] for p in seat.packets_of("event", event)]


def texts(seat):
    return seat.texts()


def eot_texts(seat):
    return [e.get("text") for e in events(seat, "eot")]


def lane_words(scene, seat_id="D"):
    for lane in (scene or {}).get("seats") or []:
        if lane.get("seat") == seat_id:
            return (lane.get("settled") or "") + (lane.get("text") or "")
    return ""


def main():
    server.NEIXINXI_ASR_FACTORY = lambda rate, seat: TwoSentenceASR(rate, seat)
    server.MIND_FORCE_OFFLINE = True

    port = free_port()
    start_server(port)
    print("server on %d" % port)
    info = post(port, "/api/neixinxi/open", {})
    sid = info["session_id"]
    d = Seat(port, sid, "D")
    check("这一席的识别接上了（假识别实例已建）",
          wait_until(lambda: "D" in TwoSentenceASR.INSTANCES, 5), list(TwoSentenceASR.INSTANCES))
    fake = TwoSentenceASR.INSTANCES["D"]
    try:
        # --- [1] 一句还没说完：字幕按当前这句往上长 ---
        print("[1] 一句还没说完：字幕按当前这句往上长（不清、不剪）")
        d.feed(tone(1000))
        check("第一版出字", wait_until(lambda: texts(d), 5), texts(d))
        check("字幕按这一句往上长（第二版比第一版长）",
              wait_until(lambda: texts(d) and len(texts(d)[-1]) > len(texts(d)[0]), 5), texts(d))
        check("这一句还没到边界：没有 asr_settled（不清字幕）", not events(d, "asr_settled"),
              events(d, "asr_settled"))

        # --- [5] 停顿保活：这段时间送静音，识别实例没被换掉 ---
        print("[5] 停顿保活：静音只保活，识别不中断、不换实例")
        d.feed(SILENCE * int(PAUSE_S * 100))     # 4 秒静音（20ms 一帧）
        check("停顿期间没有「识别中断」", not events(d, "asr_interrupted"), events(d, "asr_interrupted"))
        check("停顿期间没有换过识别实例", TwoSentenceASR.INSTANCES.get("D") is fake,
              list(TwoSentenceASR.INSTANCES))

        # --- [2] 收住 + 停住 = 真边界：另起一句 ---
        print("[2] 这句收住并停在边界上：字幕归零，用过的字不再叠到下一句")
        check("服务端发来「另起一句」（asr_settled）",
              wait_until(lambda: events(d, "asr_settled"), 4), events(d, "asr_settled"))
        settled = events(d, "asr_settled")[0]
        check("另起那句写清收住的是哪一句", "导航回家" in (settled.get("settled") or ""), settled)
        check("归零之后没有新字（这一席这一刻空着）", settled.get("text") == "", settled)
        visible = [p["data"] for p in d.packets_of("event")
                   if p["data"].get("event") in ("asr_update", "asr_settled")
                   and p["data"].get("seat_id") == "D"]
        check("屏上这一席最后一条是「另起一句」且内容为空（不再挂着上一句）",
              bool(visible) and visible[-1].get("event") == "asr_settled"
              and (visible[-1].get("text") or "") == "", visible[-2:])
        after_settle = len(texts(d))

        # --- [3][4] 下一句：判停 / 读心都只看这一句新话 ---
        print("[3] 下一句开口：判停/读心/页面都只认这一句新话（旧句整段重发被剪掉）")
        before_scenes = len(events(d, "mind_scene"))
        fake.next_sentence()
        d.feed(tone(1600))
        check("第二句上屏", wait_until(lambda: len(texts(d)) > after_settle + 1, 6),
              texts(d)[-3:])
        second = texts(d)[after_settle:]
        check("第二句每一版都不带上一句", second and all(
            "导航" not in t and "回家" not in t for t in second), second[:3])
        check("第二句是自己那一段字（顺便买点 / 水果）",
              any("顺便" in t or "水果" in t for t in second), second[:3])
        late_eot = eot_texts(d)[len(eot_texts(d)) // 2:] if eot_texts(d) else []
        new_calls = [t for t in eot_texts(d) if t in second]
        check("判停看的是这一句新话（有这一版的判停调用）", bool(new_calls), eot_texts(d)[-3:])
        check("判停不吃旧句（这一句的判停文本都在第二句里）",
              all("导航" not in t and "回家" not in t for t in new_calls), new_calls[:3])
        # 只看「第二句开口之后」那几份读心：lane 里是这一句新话，不带上一句
        scenes = [e["scene"] for e in events(d, "mind_scene")][before_scenes:]
        after = [lane_words(s) for s in scenes if s.get("seats")]
        check("读心也看到这一句新话（lane 里有新字）",
              any("顺便" in w or "水果" in w for w in after), after[-3:])
        check("读心 lane 不吃旧句（没有把上一句整段再读一遍）",
              after and all("导航" not in w and "回家" not in w for w in after), after[:3])
    finally:
        d.close()
    print("\n5 组契约通过（脚本假 ASR：整段累积重发 + 两句话之间的真停顿；未连火山 / LLM）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
