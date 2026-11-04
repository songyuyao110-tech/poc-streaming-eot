"""系统逻辑图 · 三态挂窗版（2026-11-04 拍板口径）出图脚本。

相对旧脚本 `tmp/poc-sys-flow-map-dual-diagram.py` 的改动只有三处，画的仍是同一套链路：
  1) 状态机里的 MAYBE_DONE 从「等 700ms 确认窗」改成**三态枢纽**：
     说完 → 立刻往后（0 等）/ 可能说完 → 约 0.7s 收敛 / 没说完 → 继续听 / 任何一态上限 2s；
  2) 底栏阈值行与口径行按新口径重写，并明确标出「本图是口径、实现仍是旧语义」的差异；
  3) 打断与 `turn_reopen`（原 `barge_in`）命名已收口：2026-11-04 改名，避免与 TTS 播报 barge-in 撞名。

口径基准：`docs/no-confirm-window.md`（本仓）。
事实源（未改行为的部分照旧）：
  src/turn_session.py（每席状态机、判停三态、静默窗；confirm 在 tick 路径里同步跑）
  src/dual_seat.py（DualSeatSession：通道先验、单写者 FIFO、两席独立 TurnSession）
  tmp/poc-dual-asr-c1.json（A1 回放实测，**旧口径**）  tmp/poc-dual-asr-c0-baseline.json（单席 score 基线）

自检：框重叠 / 文字溢出框 / 箭头穿框 / 箭头端点不在框边，任何一项不过就不落盘。
跑法：python3 assets/make-sys-flow-map-3state.py
"""
from __future__ import annotations

import math

from PIL import Image, ImageDraw, ImageFont

W, H = 2400, 1800
FONT = "/System/Library/Fonts/Hiragino Sans GB.ttc"
OUT = "/Users/YUYSONG/Work/poc-streaming-eot/assets/poc-sys-flow-map-3state.png"

INK = (32, 36, 44)
MUTED = (110, 118, 130)
LINE = (60, 70, 84)
ACCENT = (24, 90, 160)
WARN = (170, 70, 40)
OK = (30, 120, 70)
BG = (252, 252, 250)
GHOST = (150, 150, 150)

title = ImageFont.truetype(FONT, 46)
h2 = ImageFont.truetype(FONT, 34)
h3 = ImageFont.truetype(FONT, 24)
body = ImageFont.truetype(FONT, 20)
small = ImageFont.truetype(FONT, 18)

img = Image.new("RGB", (W, H), BG)
d = ImageDraw.Draw(img)
boxes = []
problems = []


def box(name, x, y, w, h, lines, *, fill=(255, 255, 255), border=LINE, title_color=INK, width=2):
    d.rounded_rectangle([x, y, x + w, y + h], radius=12, fill=fill, outline=border, width=width)
    boxes.append((name, x, y, x + w, y + h))
    ty = y + 12
    for index, line in enumerate(lines):
        font = h3 if index == 0 else body
        color = title_color if index == 0 else MUTED
        tw = d.textlength(line, font=font)
        if tw > w - 24:
            problems.append("text overflow in %s: %r (%d > %d)" % (name, line, tw, w - 24))
        d.text((x + 12, ty), line, font=font, fill=color)
        ty += (h3.size + 8) if index == 0 else (body.size + 6)
        if ty > y + h - 4:
            problems.append("text overflow vertically in %s (last ty=%d, bottom=%d)" % (name, ty, y + h))


segments = []


def arrow(points, *, color=LINE, width=3, dash=False, label=None, label_at=None, label_color=None):
    pts = list(points)
    segments.append(pts)
    for start, end in zip(pts, pts[1:]):
        if dash:
            x0, y0 = start
            x1, y1 = end
            length = max(abs(x1 - x0), abs(y1 - y0))
            steps = max(1, int(length // 14))
            for i in range(steps):
                if i % 2:
                    continue
                d.line([x0 + (x1 - x0) * i / steps, y0 + (y1 - y0) * i / steps,
                        x0 + (x1 - x0) * (i + 1) / steps, y0 + (y1 - y0) * (i + 1) / steps],
                       fill=color, width=width)
        else:
            d.line([start, end], fill=color, width=width)
    (ax, ay), (bx, by) = pts[-2], pts[-1]
    ang = math.atan2(by - ay, bx - ax)
    size = 14
    p1 = (bx - size * math.cos(ang - 0.45), by - size * math.sin(ang - 0.45))
    p2 = (bx - size * math.cos(ang + 0.45), by - size * math.sin(ang + 0.45))
    d.polygon([(bx, by), p1, p2], fill=color)
    if label:
        lx, ly = label_at if label_at else ((ax + bx) / 2, (ay + by) / 2 - 22)
        d.text((lx, ly), label, font=small, fill=label_color or MUTED)


# ---------------- 标题 ----------------
d.text((60, 34), "Agent POC · 两路 ASR 双席：系统链路与判停三态（2026-11-04 口径版）", font=title, fill=INK)
d.text((62, 96), "2026-11-04 CST｜落地仓 poc-streaming-eot（未推送；出图时 HEAD 718f3ce）｜单位 ms｜"
                 "本图按 2026-11-04 拍板口径重出：说完立刻 / 可能说完≈0.7s / 没说完继续听 / 上限 2s｜"
                 "※ 标「旧口径」处实现尚未改（归去窗单），灰框 = 本系统边界外或本期未落地",
       font=small, fill=MUTED)

# ---------------- ① 拓扑 ----------------
d.text((60, 142), "① 两路 ASR 双席：谁听、谁判、谁决策（C1 已落地；本图不改这条链）", font=h2, fill=ACCENT)

box("fe", 60, 200, 440, 180, [
    "前端（本系统之外）",
    "两路 mic：mic-D 主驾 / mic-P 副驾",
    "人声分离、隔离成两路干净 ASR",
    "同句去重：不做（2026-11-03 拍板）",
    "→ 两路同拾一句 = 两次动作（已知代价）",
], fill=(246, 246, 244), border=GHOST, title_color=MUTED)

box("asrD", 560, 200, 380, 85, [
    "ASR-D（外部，火山 v3 sauc）",
    "出 (t, text, final)，~500ms partial",
], fill=(240, 246, 252), border=ACCENT)

box("asrP", 560, 295, 380, 85, [
    "ASR-P（第二路，同适配层）",
    "出 (t, text, final)，两路各自独立",
], fill=(240, 246, 252), border=ACCENT)

box("queue", 1000, 200, 440, 180, [
    "DualSeatSession（单写者）",
    "路由＝通道先验，不用声纹",
    "mic-D → 席 D ｜ mic-P → 席 P",
    "on_asr / on_vad → FIFO 队列",
    "只在 tick() 派发；第 3 路 mic 直接抛错",
])

box("seatD", 1500, 200, 380, 85, [
    "席 D 的 TurnSession",
    "主驾席｜判停 + 6 态机 + 静默窗私有",
])

box("seatP", 1500, 295, 380, 85, [
    "席 P 的 TurnSession",
    "副驾席｜同上，与 D 并行、互不重置",
])

box("decide", 1940, 200, 400, 180, [
    "决策与执行（按席各一次）",
    "各自判完 → 各自一次决策",
    "同一台 8009 / LiteLLM，按动作计费",
    "执行面：一次一个动作（物理事实）",
    "高危 8 池：二轮回三问确认（未分席）",
])

arrow([(500, 242), (560, 242)], label="mic-D", label_at=(504, 212))
arrow([(500, 337), (560, 337)], label="mic-P", label_at=(504, 307))
arrow([(940, 242), (1000, 242)])
arrow([(940, 337), (1000, 337)])
arrow([(1440, 242), (1500, 242)])
arrow([(1440, 337), (1500, 337)])
arrow([(1880, 242), (1940, 242)])
arrow([(1880, 337), (1940, 337)])

# ---------------- ①b 三种不对称 ----------------
box("s1", 60, 410, 740, 110, [
    "并行在编排之前：听与判停（已落地）",
    "两席各一份判停实例，同时跑；一席说话不重置、不取消另一席",
], fill=(240, 250, 244), border=OK)
box("s2", 840, 410, 740, 110, [
    "归并在编排之内：段 → 意图单元（未落地）",
    "同一人两段要合成一个意图；C2 会话脑（最近动作台账 / 在途 IU）",
], fill=(246, 246, 244), border=GHOST, title_color=MUTED)
box("s3", 1620, 410, 720, 110, [
    "唯一性在编排之后：决策与执行（已落地）",
    "一次只落地一个动作；同目标冲突＝后到覆盖（覆盖是 C3，未落地）",
])

d.text((62, 530), "现状：ui/server.py 仍是单席入口（一 session 一 TurnSession）；DualSeatSession.on_asr / tick 已可直接接线，尚未接 UI。"
                  "※ 判停后的等待口径按 2026-11-04 拍板（②），实现仍是旧语义，代码归去窗单。",
       font=small, fill=MUTED)

# ---------------- ② 每席一份的状态机 ----------------
d.text((60, 570), "② 每席一份的状态机（两席各一套实例；6 个 state + 终态标记 turn_done，转移只在单写者 tick 线程里）", font=h2, fill=ACCENT)

box("maybe", 700, 650, 400, 180, [
    "MAYBE_DONE　判停三态",
    "说完：立刻往后，0 等",
    "可能说完：约 0.7s 收敛",
    "（任何一态上限 2s）",
    "没说完：继续听，不选工具",
], fill=(240, 250, 244), border=OK)
box("listening", 100, 810, 460, 200, [
    "LISTENING",
    "入：会话开始 / 新文本 / 续句",
    "出：判停三态分流；或空后缀 +",
    "静默 2000 → turn_done",
], fill=(240, 246, 252), border=ACCENT)
box("cut", 700, 930, 460, 170, [
    "CUT_OFF_WAIT　「话断了」",
    "出：静默 1500 → CONFIRMED",
    "（停顿 >1.5s 的一截自成一段）",
])
box("think", 700, 1190, 460, 160, [
    "THINK_WAIT　判停说「在犹豫」",
    "出：静默 3000 → CONFIRMED",
    "（唯一允许超过 2000 兜底的档）",
])
box("confirmed", 1240, 810, 460, 200, [
    "CONFIRMED",
    "入：说完立刻 / 可能说完收敛 / 静默窗到点",
    "出：决策落地 → 消费该段 → LISTENING",
    "（文本又变也会被拉回 LISTENING）",
], fill=(240, 250, 244), border=OK)
box("confirm_wait", 1240, 1120, 460, 230, [
    "CONFIRM_WAIT　等第二轮回",
    "入：决策命中 8 个池内高危工具",
    "出：二轮回判停 complete → 三问",
    "→ agree / reject / takeover / unknown",
    "超时 3000 → 安全放弃（不执行）",
], fill=(255, 250, 244), border=WARN)
box("turn_done", 1800, 810, 460, 160, [
    "turn_done（每席各自的终态标记）",
    "后缀空 + 静默 2000",
    "+ 本席本轮至少消费过 1 段",
])

arrow([(560, 850), (700, 730)], label="complete", label_at=(566, 718), label_color=OK)
arrow([(560, 930), (700, 1000)], label="cut", label_at=(600, 922))
arrow([(560, 990), (700, 1250)], label="wait", label_at=(590, 1060))
arrow([(1100, 800), (1240, 900)], color=OK)
arrow([(1160, 1010), (1240, 950)])
arrow([(1160, 1270), (1240, 990)])
arrow([(1700, 910), (1800, 910)])
arrow([(1440, 1010), (1440, 1120)], label="命中池内 → 挂起第一轮决策",
      label_at=(1450, 1032), label_color=WARN)

# 三态标签：贴在 maybe → confirmed 的箭头上（不画在框里，避免压字；窗的细节写在框内文字里）
d.text((1104, 770), "说完不等", font=small, fill=OK)

# ---------------- ③ 阈值 / 实测 / 风险 ----------------
d.text((60, 1382), "③ 判停三态口径、阈值、旧口径实测与已知风险", font=h2, fill=ACCENT)
free_lines = [
    (1428, "判停（8009 /v1/systemone，score 双阈值）：complete ≥0.80 且 confidence ≥0.70 → 说完（强）→ 立刻往后；"
           "complete ≥0.80 但 confidence <0.70 → 可能说完 → 约 0.7s 收敛；否则看 cut / wait。margin 仅 A/B，默认关。",
     body, INK),
    (1462, "等待（2026-11-04 新口径）：说完 0 等｜可能说完 ≈0.7s（confirm_silence 700）｜cut_wait 1500｜think_wait 3000｜"
           "max_silence 2000＝任何一态的上限 2s 封顶", body, INK),
    (1496, "其余阈值不动：eot_timeout 900｜debounce 80｜eot_max_inflight 3｜tools_max_inflight 1｜工具 0.5 / 域 0.4｜确认池 8/242"
           "（撤的只有「判完一律整段等」那一个窗，cut/think/max_silence 三个别顺手删）", body, MUTED),
    (1530, "※ 差异（别混说）：本图是拍板口径；截至 2026-11-04 实现仍把 confirm_silence 700 当「判完一律等」的整段窗（代码归去窗单）。"
           "带窗基线 858.7（单席）/ 877 / 876（双席）/ M4 3243.7ms 都不得当新流程实测，重锚归测评单。", body, WARN),
    (1564, "生成中打断：本席再开口 → 作废本轮决策 + 继续听；话不丢（半截 + 新话一起再送＝承接）；"
           "落地即不可撤（再说话＝下一轮，不用「执行前加等待」去换可回撤）。", body, INK),
    (1598, "命名（已收口 2026-11-04）：事件名 turn_reopen（原 barge_in）的语义是「动作已落地后再开口 → 开新一轮」，"
           "与打断 TTS 播报（barge-in）的撞名已随改名消解；本项目不做播报，图上与面板不再加「非 TTS 播报打断」注。", body, INK),
    (1632, "双席实测（A1 回放，真 8009，旧口径）：两席各自 CONFIRMED 20/20；cross_cancel 0 / cross_reset 0 / 串席文本 0；"
           "每席 p50 D 877 / P 876（≤944.6＝单席 858.7×1.10）；cut_wait 路径 D 侧 1671 单独记，不与基线混比。", body, INK),
    (1666, "风险【判断】：二次确认的三问打分同步跑在 tick 路径（turn_session.py，实测 130–195ms）；两席共用一个 tick，"
           "一席确认会顺带拖住另一席。", small, WARN),
    (1694, "出图：assets/make-sys-flow-map-3state.py（自检：框重叠 / 文字溢出 / 箭头穿框 / 端点不在框边，任一不过不落盘）｜"
           "口径基准 docs/no-confirm-window.md｜旧图 tmp/poc-sys-flow-map.png 只作沿革", small, MUTED),
]
for ly, text, font, color in free_lines:
    tw = d.textlength(text, font=font)
    if tw > W - 120:
        problems.append("free line overflow at y=%d: %d > %d" % (ly, tw, W - 120))
    d.text((62, ly), text, font=font, fill=color)

# ---------------- 自检 ----------------
for i in range(len(boxes)):
    for j in range(i + 1, len(boxes)):
        _, ax0, ay0, ax1, ay1 = boxes[i]
        _, bx0, by0, bx1, by1 = boxes[j]
        if ax0 < bx1 and bx0 < ax1 and ay0 < by1 and by0 < ay1:
            problems.append("box overlap: %s / %s" % (boxes[i][0], boxes[j][0]))
for name, x0, y0, x1, y1 in boxes:
    if x0 < 0 or y0 < 0 or x1 > W or y1 > H:
        problems.append("box out of canvas: %s" % name)

# 箭头：终点必须落在某个框的边上（±14px），且线段不得穿过别的框内部
for pts in segments:
    ex, ey = pts[-1]
    hit = [name for name, x0, y0, x1, y1 in boxes
           if min(abs(ex - x0), abs(ex - x1), abs(ey - y0), abs(ey - y1)) <= 14
           and x0 - 14 <= ex <= x1 + 14 and y0 - 14 <= ey <= y1 + 14]
    if not hit:
        problems.append("arrow end (%s,%s) not on any box edge" % (ex, ey))
    for start, end in zip(pts, pts[1:]):
        (ax, ay), (bx, by) = start, end
        for name, x0, y0, x1, y1 in boxes:
            lo_x, hi_x = min(ax, bx), max(ax, bx)
            lo_y, hi_y = min(ay, by), max(ay, by)
            if lo_x < x1 - 2 and hi_x > x0 + 2 and lo_y < y1 - 2 and hi_y > y0 + 2:
                if hit and name == hit[0] and end == pts[-1]:
                    continue
                problems.append("arrow segment (%s,%s)-(%s,%s) crosses box %s"
                                % (round(ax), round(ay), round(bx), round(by), name))

print("boxes:", len(boxes), "arrows:", len(segments))
print("problems:", problems or "none")
if not problems:
    img.save(OUT)
    print("saved:", OUT)
else:
    raise SystemExit("自检不过，不落盘")
