"""探针辅助：只为本机的「开口后读完立刻推」验证起一个 8766，ASR 用脚本假字，读心仍走真模型。

用途：本机的云 ASR 现在读不出字（`stream_asr` 对一个 2s 的合成语音只回 `audio_info.duration`
没有任何 text，与本卡改动无关），所以「真 ASR → 出字 → 读完 → 立刻推」这一段拆开验：

  - 出字那一段：脚本假 ASR（字固定、到点就出 final，可复现）；
  - 读心 + 推送：仍是 8765/8766 同一份真代码 + 真 `deepseek-v4.1-flash`。

用法（另开一个端口，不动 8765）：

    /Users/YUYSONG/Agents/navi-dual-system/.venv/bin/python -B tests/probe_mind_push_fake_asr_server.py 8766
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "ui"))
sys.path.insert(0, str(ROOT / "src"))
sys.dont_write_bytecode = True

import server
# noqa: E402

# 脚本假字：push 够 0.4s 音频就在 poll 里吐一句 final（与真 ASR 的“说完落段”同一形状）。
SEAT_TEXT = {"D": "导航去望京SOHO", "P": "把空调调到24度"}


class ScriptedASR:
    def __init__(self, rate, seat="D"):
        self.rate = int(rate)
        self.seat = "P" if str(seat).upper() == "P" else "D"
        self.pushed = 0
        self.final_sent = False

    def push(self, pcm, *, last=False):
        self.pushed += len(pcm or b"")

    def poll(self):
        if not self.final_sent and self.pushed >= self.rate * 2 * 0.4:
            self.final_sent = True
            return [(SEAT_TEXT[self.seat], True, 0)]
        return []

    def finish(self, timeout_s=5.0):
        return []

    def close(self):
        pass


if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8766
    server.DUAL_MIC_ASR_FACTORY = lambda rate, seat: ScriptedASR(rate, seat)
    print("probe server on %d（脚本假 ASR / 读心真模型）" % port)
    server.app.run(host="127.0.0.1", port=port, debug=False, use_reloader=False)
