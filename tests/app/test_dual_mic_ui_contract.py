"""双席真麦 · 前端接线契约（静态断言，不需要浏览器）。

只钉「网页能让 D/P 各选一个输入设备、并把 PCM 推到两路独立 ASR」这条链的接线，
不测浏览器行为（真机/真麦在真链冒烟里验）：

    [1] 运行模式里有 `dualmic` 选项，且旧的 `dual`（文本回放）入口没被拆；
    [2] 有 per-seat 的设备下拉（dualmic-device-D / -P）、开关（-d-on / -p-on）、开始/停止/刷新按钮；
    [3] JS 用 enumerateDevices 列 audioinput、用 getUserMedia(deviceId) 取每席设备、
        推 16kHz/20ms 帧、连 WS /api/session/dual/mic?session_id=…&seat=…；
    [4] 先 POST /api/session/dual/mic/open 拿 session_id（两路共用）；
    [5] 事件复用文本回放同一套 dualApply / dualFillVerdict（不另起一套显示）；
    [6] applyMode 会显示/隐藏 dualmic 面板；提交时 dualmic 模式走 dualmicStart。

    python3 -B tests/test_dual_mic_ui_contract.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
HTML = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")


def check(label, condition, detail=""):
    print(("  ok   " if condition else "  FAIL ") + label + (("  | " + str(detail)) if detail and not condition else ""))
    if not condition:
        raise AssertionError(label + ("  | " + str(detail) if detail else ""))


def main():
    print("[1] 模式入口：新增 dualmic，保留旧 dual 文本回放")
    check("mode 有 dualmic 选项", 'value="dualmic"' in HTML)
    check("mode 仍有旧 dual 选项（文本回放不拆）", 'value="dual"' in HTML)
    check("旧文本回放入口 dual-go 还在", 'id="dual-go"' in HTML)
    check("旧文本回放端点 /api/session/dual/stream 还在", "/api/session/dual/stream" in HTML)

    print("[2] per-seat 设备选择与开关")
    for element in ("dualmic-controls", "dualmic-device-D", "dualmic-device-P",
                    "dualmic-d-on", "dualmic-p-on", "dualmic-start", "dualmic-stop",
                    "dualmic-devices", "dualmic-status", "turn-config-dualmic"):
        check('有 id="%s"' % element, ('id="%s"' % element) in HTML)

    print("[3] 采集与推流接线")
    check("用 enumerateDevices 枚举设备", "navigator.mediaDevices.enumerateDevices" in HTML)
    check("只挑 audioinput", "kind === 'audioinput'" in HTML)
    check("getUserMedia 按所选设备精确打开（exact，不回落默认）",
          "deviceId: {exact: deviceId}" in HTML and "deviceId: deviceId ? {exact: deviceId}" not in HTML)
    check("开麦前两席都要真设备号、且不能是同一支（MicRoute.plan）", "MicRoute.plan(selected)" in HTML)
    check("浏览器实际给的不是所选那支要当场失败（MicRoute.grant）", "MicRoute.grant(selected, settings)" in HTML)
    check("每席一个 AudioContext/采集节点（dualmicSeatStart）", "async function dualmicSeatStart(seat" in HTML)
    check("16kHz/20ms 帧长常量", "DUALMIC_FRAME_SAMPLES = 320" in HTML)
    check("连 WS /api/session/dual/mic", "/api/session/dual/mic?session_id=" in HTML)
    check("WS 带 seat 参数", "&seat=${seat}" in HTML)
    check("两席共用同一 session_id", "dualmicSeatStart(seat, sessionId, turn, selected[seat])" in HTML)

    print("[4] 会话创建走 open 端点")
    check("POST /api/session/dual/mic/open", "/api/session/dual/mic/open" in HTML)
    check("open 带 mock 开关", "mock: byId('dualmic-mock').checked" in HTML)

    print("[5] 复用文本回放同一套双席显示")
    check("事件走 dualApply", "dualApply(data.event, data)" in HTML)
    check("收口走 dualFillVerdict", "dualFillVerdict(data)" in HTML)

    print("[6] 模式切换与提交路由")
    check("applyMode 显隐 dualmic-controls", "byId('dualmic-controls').hidden = mode !== 'dualmic'" in HTML)
    check("提交路由到 dualmicStart", "value === 'dualmic') return dualmicStart()" in HTML)
    check("关掉席＝那路不参与（按开关过滤）", "byId('dualmic-' + seat.toLowerCase() + '-on').checked" in HTML)

    print("[7] JS 语法可解析（node --check）")

    import subprocess
    import shutil
    import tempfile
    node = shutil.which("node")
    if node:
        script = re.findall(r"<script>(.*?)</script>", HTML, re.S)
        check("只有一个内联 script 块", len(script) == 1, len(script))
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(script[0])
            path = handle.name
        result = subprocess.run([node, "--check", path], capture_output=True, text=True)
        check("node --check 通过", result.returncode == 0, result.stderr[-400:])
    else:
        print("  skip node --check（本机没有 node）")

    print("\n7 组前端契约通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
