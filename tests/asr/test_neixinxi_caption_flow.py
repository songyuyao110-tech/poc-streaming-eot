"""内心戏页 · 页面级回归：同一轮收听中「停顿后再开口」「识别中途断了再接回来」「失败要说出来」。

跑法：真浏览器（Chrome for Testing headless-shell，本机 puppeteer 缓存里那份）+ 真页面 +
真 WS + 脚本假识别（`server.NEIXINXI_ASR_FACTORY`）。麦用页面侧 `getUserMedia` 替身发声
（一个 440 Hz 音源 + 可调增益），所以能精确造「有声 / 停住 / 再出声」，不依赖真麦硬件。

断言（对应本卡验收 1/2/4 与既有停顿保活/中途重接）：
  [1] 一句还没说完时字幕按当前这句往上长（不在句中清掉）；这句收住并停在边界上之后，字幕另起一句
      （用过的字清掉）；停几秒再出声 → 第二句**单独**上屏，不是「上一句 + 这一句」。
  [2] 识别流中途被收掉 → 页面错误区写明「识别中断 … 已重新接上」（主画面也有）→ 后面的话继续上屏。
  [3] 识别引擎接不上 → 页面主画面写明哪一席、哪一步；按钮回到可重试，不假装在听。

找不到浏览器时 SKIP 并以 0 退出（不把离线回归拖红）。
用法：`/Users/YUYSONG/Agents/navi-dual-system/.venv/bin/python -B tests/asr/test_neixinxi_caption_flow.py`
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
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
from test_neixinxi_keepalive import PausableASR  # noqa: E402

def find_shell():
    base = Path.home() / ".cache/puppeteer/chrome-headless-shell"
    if base.is_dir():
        for version in sorted(base.iterdir(), reverse=True):
            binary = version / "chrome-headless-shell-mac-arm64" / "chrome-headless-shell"
            if binary.is_file():
                return binary
    chrome = Path("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
    return chrome if chrome.is_file() else None


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


class Browser:
    """最小 CDP 客户端：headless-shell + 一个页面，真鼠标点击 + 读 DOM。"""

    def __init__(self, url):
        self.binary = find_shell()
        if self.binary is None:
            raise RuntimeError("no chrome-headless-shell / Chrome found")
        self.port = free_port()
        self.profile = tempfile.mkdtemp(prefix="neixinxi-cdp-")
        flags = [] if "headless-shell" in str(self.binary) else ["--headless=new"]
        self.proc = subprocess.Popen(
            [str(self.binary), *flags, "--remote-debugging-port=%d" % self.port,
             "--user-data-dir=" + self.profile, "--no-first-run", "--disable-gpu",
             "--use-fake-ui-for-media-stream", url],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            try:
                urllib.request.urlopen("http://127.0.0.1:%d/json/version" % self.port, timeout=0.5)
                break
            except OSError:
                time.sleep(0.1)
        else:
            raise RuntimeError("chrome devtools did not come up")
        time.sleep(1.8)
        targets = json.loads(urllib.request.urlopen(
            "http://127.0.0.1:%d/json/list" % self.port).read().decode())
        page = [t for t in targets if t.get("type") == "page"][0]
        from simple_websocket import Client
        self.cdp = Client.connect(page["webSocketDebuggerUrl"], receive_bytes=1 << 24)
        self.next_id = 0

    def call(self, method, **params):
        self.next_id += 1
        self.cdp.send(json.dumps({"id": self.next_id, "method": method, "params": params}))
        deadline = time.monotonic() + 12
        while time.monotonic() < deadline:
            message = self.cdp.receive(timeout=1)
            if message is None:
                continue
            data = json.loads(message)
            if data.get("id") == self.next_id:
                if "error" in data:
                    raise RuntimeError(data["error"])
                return data.get("result")
        raise TimeoutError(method)

    def js(self, expression):
        result = self.call("Runtime.evaluate", expression=expression, returnByValue=True)
        detail = result.get("result") or {}
        if result.get("exceptionDetails"):
            raise RuntimeError("page eval failed: " + json.dumps(result["exceptionDetails"], ensure_ascii=False)[:300])
        return detail.get("value")

    def click(self, element_id):
        rect = self.js("(() => { const b = document.getElementById('%s'); const r = b.getBoundingClientRect();"
                       " return {x: r.x + r.width / 2, y: r.y + r.height / 2}; })()" % element_id)
        self.call("Input.dispatchMouseEvent", type="mousePressed", x=rect["x"], y=rect["y"],
                  button="left", clickCount=1)
        self.call("Input.dispatchMouseEvent", type="mouseReleased", x=rect["x"], y=rect["y"],
                  button="left", clickCount=1)

    def close(self):
        try:
            self.cdp.close()
        except Exception:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.proc.kill()


# 页面侧假麦：一个音源 + 每次开麦各自一档增益；页面点开始后从它拿 mediaStream（其余代码全走真路径）。
# 两席必须是「两份不同的采集」（这里用逐帧电平差模拟近讲/远讲），否则开麦路由会（正确地）判出
# 「两路是同一份声音」并停下——那正是 2026-11-12 用户报的 bug 的样子。
FAKE_MIC = r"""
(() => {
  if (window.__fakeMic) return 'already';
  window.__fakeMic = { ctx: null, osc: null, gains: [], dests: [], state: 'off', count: 0 };
  const ensure = () => {
    if (window.__fakeMic.ctx) return window.__fakeMic.ctx;
    const ctx = new AudioContext();
    window.__fakeMic.ctx = ctx;
    ctx.resume();
    return ctx;
  };
  // 两席各一支「麦」：两个独立音源（440Hz / 660Hz），基础增益不同（近讲大声 / 另一支小一点）。
  // 开麦路由是按两路实际收到的声音判「是不是同一份」：替身必须真的是两份不同的声音，
  // 否则页面会（正确地）判出「两路是同一份声音」并停下 —— 那正是 2026-11-12 用户报的 bug 的样子。
  navigator.mediaDevices.getUserMedia = async (constraints) => {
    const ctx = ensure();
    if (!window.__fakeMic.osc) {
      const osc = ctx.createOscillator();
      osc.type = 'sine'; osc.frequency.value = 440;
      osc.start();
      window.__fakeMic.osc = osc;
    }
    // 每次开麦各自一档增益（近讲大声 0.28 / 另一支小一点 0.12）：两路逐帧电平差 ~7 dB，
    // 开麦路由据此判「两条不同的采集链」；若两席拿到同一档（=同一份数字信号）就会被判成同一支麦。
    const base = (window.__fakeMic.count % 2 === 0) ? 0.28 : 0.12;
    window.__fakeMic.count += 1;
    const gain = ctx.createGain();
    gain.gain.value = base;
    gain.__base = base;
    window.__fakeMic.osc.connect(gain);
    const dest = ctx.createMediaStreamDestination();
    gain.connect(dest);
    window.__fakeMic.gains.push(gain);
    window.__fakeMic.dests.push(dest);
    window.__fakeMic.state = 'on';
    return dest.stream;
  };
  window.__fakeMic.setLevel = (value) => {
    ensure();
    for (const gain of window.__fakeMic.gains) {
      gain.gain.value = gain.__base * value;
    }
    window.__fakeMic.state = value > 0 ? 'on' : 'muted';
  };
  return 'ok';
})()
"""


def snapshot(browser):
    return browser.js(
        "({btn: document.getElementById('startBtn').textContent,"
        " dis: document.getElementById('startBtn').disabled,"
        " D: document.getElementById('text-D').textContent,"
        " P: document.getElementById('text-P').textContent,"
        " err: document.getElementById('d-errors').textContent,"
        " notice: document.getElementById('notice').className,"
        " noticeTitle: document.getElementById('notice-title').textContent,"
        " noticeBody: document.getElementById('notice-body').textContent,"
        " prior: document.getElementById('d-prior').textContent,"
        " micD: document.getElementById('mic-name-D').textContent})")


def check(label, condition, detail=""):
    print(("  ok   " if condition else "  FAIL ") + label
          + (("  | " + str(detail)) if detail and not condition else ""))
    if not condition:
        raise AssertionError(label + ("  | " + str(detail) if detail else ""))


def wait_until(predicate, timeout=8.0, interval=0.15):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def main():
    if find_shell() is None:
        print("SKIP: 本机没有 chrome-headless-shell / Chrome，页面级用例跳过（真识别链另见 wav 回放）")
        return 0

    server.NEIXINXI_ASR_FACTORY = lambda rate, seat: PausableASR(rate, seat)
    server.MIND_FORCE_OFFLINE = True
    port = free_port()
    start_server(port)
    url = "http://127.0.0.1:%d/neixinxi" % port
    print("server on %d" % port)

    browser = Browser(url)
    try:
        check("假麦替身装上", browser.js(FAKE_MIC) == "ok", browser.js(FAKE_MIC))
        print("[1] 同一轮收听：出字 → 停住几秒 → 再开口，新的话照样上屏")
        browser.click("startBtn")
        check("两条识别通道都接上后才变成「停止」",
              wait_until(lambda: snapshot(browser)["btn"].strip() == "■ 停止", 10), snapshot(browser))
        check("主驾显示「已接入」",
              wait_until(lambda: "已接入" in snapshot(browser)["micD"], 4), snapshot(browser))

        check("第一段出字", wait_until(lambda: "导航" in snapshot(browser)["D"], 6), snapshot(browser))
        # 验收 1：一句还没说完时，字幕仍按当前这句往上长（判停抢跑也不清、不剪）
        check("一句还没说完时字幕往上长（不在这句中间清掉）",
              wait_until(lambda: "导航回家附近有什么便利店" in snapshot(browser)["D"], 10),
              snapshot(browser))
        check("这一整句在屏上没有出现「旧字清空」（还在说就不另起）",
              "导航" in snapshot(browser)["D"], snapshot(browser))
        browser.js("window.__fakeMic.setLevel(0)")
        check("停住后主画面写明「声音没过线，不会上屏」",
              wait_until(lambda: snapshot(browser)["noticeTitle"] == "声音没过线，不会上屏"
                         and "show" in snapshot(browser)["notice"]
                         and "gate" in snapshot(browser)["notice"], 8), snapshot(browser))
        # 验收 2：这句被判停收住、交给读心并停在边界上之后，字幕另起一句（用过的字清掉）
        check("收住后字幕清掉（不再把上一句挂在下一句前面）",
              wait_until(lambda: "导航" not in snapshot(browser)["D"], 8), snapshot(browser))
        browser.js("window.__fakeMic.setLevel(0.25)")
        check("再出声后主画面提示收起",
              wait_until(lambda: "gate" not in snapshot(browser)["notice"], 5), snapshot(browser))
        # 验收 4：停顿十几秒再说 → 第二句单独上屏，不是「上一句 + 这一句」
        check("停顿后再说，第二句单独上屏",
              wait_until(lambda: "顺便买点" in snapshot(browser)["D"]
                         or "水果" in snapshot(browser)["D"], 12), snapshot(browser))
        second = snapshot(browser)["D"]
        check("第二句的字里没有上一句全文",
              "导航" not in second and "回家" not in second and "便利店" not in second, second)
        # 验收 5：读心每次判断带上前几次结果，页面上看得见（离线替身段：只记账、不冒充已用）
        check("读心抽屉写明这次判断带上的前几次结果",
              wait_until(lambda: "份" in snapshot(browser)["prior"]
                         or "第 1 次" in snapshot(browser)["prior"], 6), snapshot(browser)["prior"])
        prior_text = snapshot(browser)["prior"]
        count = prior_text.split("/")[0].strip()
        check("带上的份数不超过 5 份（口径就是最近 5 次）",
              (not count.isdigit()) or int(count) <= 5, prior_text)

        print("[2] 识别流中途被收掉：自动接回，页面写明「识别中断，已重新接上」，后面的话继续上屏")
        stream = PausableASR.INSTANCES.get("D")
        stream.drop_after_voiced = stream.voiced + int(16000 * 2 * 0.6)
        check("中途断开后页面先提示「识别中断」",
              wait_until(lambda: ("识别中断" in snapshot(browser)["err"]
                                  or "识别中断" in snapshot(browser)["noticeTitle"]), 6),
              snapshot(browser))
        check("页面写明已重新接上（抽屉错误区）",
              wait_until(lambda: "已重新接上" in snapshot(browser)["err"], 8), snapshot(browser))
        text_before = snapshot(browser)["D"]
        check("接回之后继续说，仍能上屏",
              wait_until(lambda: (snapshot(browser)["D"]) != text_before, 8),
              snapshot(browser))

        print("[3] 识别引擎接不上：页面写明哪一席哪一步；按钮回到可重试")
        browser.click("startBtn")         # 先停
        check("停止后按钮回到「开始双路」",
              wait_until(lambda: "开始双路" in snapshot(browser)["btn"], 5), snapshot(browser))
        PausableASR.FAIL_SEATS["P"] = RuntimeError("boom: token=never-log")
        browser.click("startBtn")
        check("失败后按钮不是「停止」、而是可重试",
              wait_until(lambda: ("重试接入" in snapshot(browser)["btn"]
                                  or "开始双路" in snapshot(browser)["btn"]), 10), snapshot(browser))
        failed = snapshot(browser)
        check("主画面写明哪一席哪一步（识别引擎）",
              "没接上" in failed["noticeTitle"] or "识别出错" in failed["noticeTitle"],
              failed)
        check("错误区写明席与阶段",
              ("席 P" in failed["noticeBody"]) or ("席 P" in failed["err"]),
              failed)
        check("失败后按钮不会变成「停止」", "停止" not in failed["btn"], failed)
    finally:
        browser.close()

    print("\n3 组页面级用例通过（真页面 + 真 WS + 脚本假识别 + 页面侧假麦）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
