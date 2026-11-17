"""开麦路由 · 前端接线契约 + 共用纯函数（静态断言 + node 行为测，不需要浏览器）。

钉的是「选了哪支就必须开哪支」这条链在两页都接上了（真机/真麦留给用户自测）：

    [1] 两页共用 `mic_route.js`（服务端 `/mic_route.js` 也在）；不新增第三方依赖。
    [2] 内心戏页：按所选设备 exact 打开；设备号为空/没授权 → 不按默认开、当场说明；
        浏览器实际给的不是所选那支 → 当场失败；页面写「真在用哪支」；
        报给服务端的 claim + 处理服务端 `mic_route`（同一份声音 / 给错设备 → 当场停下）。
    [3] 对话流页 dualmic：同一套写法一并改掉（exact / 空设备号 / 实际设备 / claim / 现场判定）。
    [4] 服务端：两页各自的会话都挂同一份 `MicRouteGuard`；判据是**每席自己收到的 PCM 电平走势**；
        `mic_route` 事件在两处 driver 里发出、两处 WS 都收 `cmd=route`。
    [5] `mic_route.js` 的纯函数在 node 里真跑一遍（plan / grant 的每条分支）。

    python3 -B tests/app/test_mic_route_ui_contract.py
"""
from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
NEIXINXI = (ROOT / "ui" / "neixinxi.html").read_text(encoding="utf-8")
INDEX = (ROOT / "ui" / "index.html").read_text(encoding="utf-8")
SERVER = (ROOT / "ui" / "server.py").read_text(encoding="utf-8")
MIC_ROUTE_JS = (ROOT / "ui" / "mic_route.js").read_text(encoding="utf-8")


def check(label, condition, detail=""):
    print(("  ok   " if condition else "  FAIL ") + label
          + (("  | " + str(detail)) if detail and not condition else ""))
    if not condition:
        raise AssertionError(label + ("  | " + str(detail) if detail else ""))


def inline_scripts(html):
    return re.findall(r"<script>(.*?)</script>", html, re.S)


def main():
    print("[1] 两页共用同一份开麦路由脚本")
    check("内心戏页引入 /mic_route.js", '<script src="/mic_route.js"></script>' in NEIXINXI)
    check("对话流页引入 /mic_route.js", '<script src="/mic_route.js"></script>' in INDEX)
    check("服务端提供 /mic_route.js", '@app.get("/mic_route.js")' in SERVER)
    check("共用脚本无第三方依赖（纯函数 / UMD）",
          "require(" not in MIC_ROUTE_JS and "import " not in MIC_ROUTE_JS)

    print("[2] 内心戏页：选了哪支就开哪支，开错 / 同一份声音当场说清")
    check("按所选设备精确打开（exact，绝不回落默认）",
          "deviceId: {exact: deviceId}" in NEIXINXI
          and "deviceId: deviceId ? {exact: deviceId}" not in NEIXINXI)
    check("设备号为空直接失败，不按默认开",
          "没有真实设备号" in NEIXINXI and "不按默认开麦" in NEIXINXI)
    check("点开始前先拿真设备列表（未授权先授权再枚举）",
          "async function listInputs(unlock)" in NEIXINXI
          and "await listInputs(true)" in NEIXINXI)
    check("授权后列表刷新、选择对不上时明确让用户再选（不猜）",
          "请按真名重新选一次" in NEIXINXI)
    check("浏览器实际给的不是所选那支 → 当场失败", "MicRoute.grant(selected, settings)" in NEIXINXI
          and "granted.known && !granted.ok" in NEIXINXI)
    check("开麦前校验两席不是同一支 / 都有真设备号", "MicRoute.plan(selected)" in NEIXINXI)
    check("页面写「真在用哪支」（不是只改下拉）", "真在用：" in NEIXINXI
          and "actualLabel" in NEIXINXI)
    check("把「要哪支 / 实际给哪支」报给服务端", "cmd: 'route'" in NEIXINXI)
    check("处理服务端 mic_route 判定", "handleMicRoute" in NEIXINXI
          and "data.event === 'mic_route'" in NEIXINXI)
    check("同一份声音 / 给错设备 → 当场说清并停下（abort）",
          "data.status === 'same_source' || data.status === 'device_mismatch'" in NEIXINXI
          and "abort(" in NEIXINXI)
    check("没开成不静默继续听：abort 会关掉当场建的空通路", "function closePrimedAudio" in NEIXINXI)

    print("[3] 对话流页 dualmic：同一套开麦写法一并改")
    check("按所选设备精确打开（exact，绝不回落默认）",
          "deviceId: {exact: deviceId}" in INDEX
          and "deviceId: deviceId ? {exact: deviceId}" not in INDEX)
    check("设备号为空直接失败，不按默认开", "没有真实设备号" in INDEX and "不按默认开麦" in INDEX)
    check("点开始前先拿真设备列表（未授权先授权再枚举）", "async function dualmicInputs(unlock)" in INDEX
          and "await dualmicEnsureRoute(enabled)" in INDEX)
    check("授权后列表刷新、选择对不上时明确让用户再选", "请按真名重新选一次" in INDEX)
    check("浏览器实际给的不是所选那支 → 当场失败", "MicRoute.grant(selected, settings)" in INDEX
          and "granted.known && !granted.ok" in INDEX)
    check("开麦前校验两席不是同一支 / 都有真设备号", "MicRoute.plan(selected)" in INDEX)
    check("页面写「真在用哪支」", "真在用：" in INDEX and "actualLabel" in INDEX)
    check("把「要哪支 / 实际给哪支」报给服务端", "cmd: 'route'" in INDEX)
    check("处理服务端 mic_route 判定并停下", "function dualmicRoute" in INDEX
          and "data.event === 'mic_route'" in INDEX
          and "dualmicStop()" in INDEX)
    check("一席没开成也不假装两路在听（停下这一轮）", "dualmicStop();\n        return;" in INDEX)

    print("[4] 服务端：判据是每席自己收到的声音")
    check("判定函数按两路电平走势比（同一份 / 分开 / 判不了）", "def mic_route_verdict(" in SERVER)
    check("判「同一份」看对齐后走势 + 逐帧电平差（不只看设备名）",
          "MIC_ROUTE_CORR_SAME" in SERVER and "MIC_ROUTE_GAP_SAME" in SERVER)
    check("电平差 ≥ 4 dB 直接算两条不同采集链（同一支麦差不出这个数）",
          "MIC_ROUTE_GAP_SPLIT" in SERVER)
    check("安静帧不参与「像不像」的比对（静音对静音不算证据）",
          "MIC_ROUTE_FLOOR" in SERVER and "x >= MIC_ROUTE_FLOOR and y >= MIC_ROUTE_FLOOR" in SERVER)
    check("内心戏会话挂路由账", "MicRouteGuard()" in SERVER)
    check("两页两个会话类都挂（DualMicZoom / NeixinxiZoom）",
          SERVER.count("self.mic_route = MicRouteGuard()") == 2,
          SERVER.count("self.mic_route = MicRouteGuard()"))
    check("内心戏每席电平由自己那一路的 PCM 算，并记进路由账",
          "self.mic_route.note_level(seat, dbfs)" in SERVER)
    check("对话流双席麦也记每路电平", "self.mic_route.note_level(seat, _mic_route_frame_level(frame))" in SERVER)
    check("两处 driver 都发 mic_route", SERVER.count("self.mic_route.poll(") == 2,
          SERVER.count("self.mic_route.poll("))
    check("两处 WS 都收 cmd=route", SERVER.count('command.get("cmd") == "route"') == 2,
          SERVER.count('command.get("cmd") == "route"'))
    check("判出「同一份声音」时文案写清「没开成两支」", "这一轮没开成两支" in SERVER)
    check("电平与路由共用同一份 dBFS 实现", "return _mic_route_frame_level(pcm)" in SERVER)

    print("[5] mic_route.js 纯函数在 node 里真跑")
    node = shutil.which("node")
    if node:
        script = r"""
// 这份脚本本身跑在 TMPDIR（仓库外）；mic_route.js 在仓库里，而仓库根 package.json 是 type=module，
// 直接 require 会按 ESM 解析（拿不到 module.exports）。这里按浏览器/CJS 两种用法各喂一个 module 壳。
const fs = require('fs');
const path = require('path');
const file = path.join(%r, 'mic_route.js');
const sandbox = {module: {exports: {}}, exports: {}};
new Function('module', 'exports', 'window', fs.readFileSync(file, 'utf8'))(
  sandbox.module, sandbox.exports, undefined);
const MicRoute = sandbox.module.exports;
const assert = require('assert');

// 两席都有真设备号且不同 → 可以开
const ok = MicRoute.plan({D: {deviceId: 'a', label: '小米耳夹'}, P: {deviceId: 'b', label: 'Poly'}});
assert.strictEqual(ok.ok, true);
// 设备号为空 → 不按默认开
const missing = MicRoute.plan({D: {deviceId: '', label: '输入设备 1'}, P: {deviceId: 'b', label: 'Poly'}});
assert.strictEqual(missing.ok, false);
assert.strictEqual(missing.status, 'missing_device_id');
assert.ok(missing.message.indexOf('默认') >= 0);
// 两席填了同一支 → 开不出两路
const same = MicRoute.plan({D: {deviceId: 'a', label: '小米耳夹'}, P: {deviceId: 'a', label: '小米耳夹'}});
assert.strictEqual(same.ok, false);
assert.strictEqual(same.status, 'same_device');
// 浏览器给的就是所选那支
const good = MicRoute.grant({deviceId: 'a', label: '小米耳夹'}, {deviceId: 'a', label: '小米耳夹'});
assert.strictEqual(good.ok, true);
assert.strictEqual(good.known, true);
// 浏览器给了另一支 → 当场失败
const wrong = MicRoute.grant({deviceId: 'a', label: '小米耳夹'}, {deviceId: 'b', label: 'Poly'});
assert.strictEqual(wrong.ok, false);
assert.strictEqual(wrong.known, true);
assert.strictEqual(wrong.status, 'device_mismatch');
assert.ok(wrong.message.indexOf('不是所选') >= 0);
// 设备号回声对不上但设备名一样（有的浏览器/合成音轨会回报 default 或另一套号）→ 不可判，
// 不拿「号不一样」或「名字一样」冒充结论，交给服务端按实际声音判。
const echoOnly = MicRoute.grant({deviceId: 'a', label: '小米耳夹'}, {deviceId: 'default'});
assert.strictEqual(echoOnly.known, false);
assert.strictEqual(echoOnly.ok, false);
// 浏览器不回报设备号 → 不可判（不冒充一致，也不冒充不一致）
const unknown = MicRoute.grant({deviceId: 'a', label: '小米耳夹'}, {});
assert.strictEqual(unknown.known, false);
assert.strictEqual(unknown.ok, false);
// 空设备号 → 不按默认
const blank = MicRoute.grant({deviceId: '', label: ''}, {deviceId: 'b'});
assert.strictEqual(blank.status, 'missing_device_id');
// 页面那一行写「真在用」并与所选对齐
const line = MicRoute.seatText('D', {selectedLabel: '小米耳夹', actualLabel: '小米耳夹', status: 'ok'});
assert.ok(line.indexOf('主驾 真在用：小米耳夹') === 0);
assert.ok(line.indexOf('已确认分开') >= 0);
const mismatched = MicRoute.seatText('P', {selectedLabel: 'Poly', actualLabel: '小米耳夹', status: 'same_source'});
assert.ok(mismatched.indexOf('选的是「Poly」') >= 0);
assert.ok(mismatched.indexOf('同一份声音') >= 0);
assert.deepStrictEqual(Object.keys(MicRoute).sort(),
  ['grant', 'plan', 'sameLabel', 'seatName', 'seatText', 'statusText']);
console.log('mic_route.js behaviour ok');
""" % str(ROOT / "ui")
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(script)
            node_test = handle.name
        result = subprocess.run([node, node_test], capture_output=True, text=True)
        check("mic_route.js 纯函数行为（plan/grant/seatText 全分支）", result.returncode == 0,
              (result.stdout + result.stderr)[-600:])
    else:
        print("  skip mic_route.js 行为测（本机没有 node）")

    for name, html in (("内心戏页", NEIXINXI), ("对话流页", INDEX)):
        if not node:
            break
        blocks = inline_scripts(html)
        check("%s只有一个内联 script 块" % name, len(blocks) == 1, len(blocks))
        with tempfile.NamedTemporaryFile("w", suffix=".js", delete=False) as handle:
            handle.write(blocks[0])
            path = handle.name
        result = subprocess.run([node, "--check", path], capture_output=True, text=True)
        check("%s JS 语法可解析（node --check）" % name, result.returncode == 0, result.stderr[-400:])

    print("\n开麦路由前端契约通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
