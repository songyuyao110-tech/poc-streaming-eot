"""期五（2026-11-16）：四处出声的同一套口径（优先级收口）。

依据验收卡 `poc-hub-jev-absorb-p5-20261116-验收.md` 与方案
`poc-hub-jev-absorb-plan-20261116-DETAIL.md` §4 第 5 条 / §5.1 期五 / §5.2 期五 / §7 第 6 条。

四处出声已经各在各自的模块里能跑（应一声 `hub_ack`、垫一句 `hub_pace`、两人同时说 `hub_floor`、
冷场 `hub_quiet`）。本模块**不新增判断、不改任何数字**，只把四处同时可能的先后收成一套：

1. **同一时刻最多一句**；出声后 **1 秒**内谁都不再出声（应声 / 垫句 / 垫句抢回 / 冷场开口共用
   同一个「上次出声」锚点与同一个静默闸）。
2. **垫句优先于应一声**：同一刻两者都该出时，只出垫句，不应声。
3. **有人在说 → 应声 / 垫句；没人说话 → 冷场**。两边触发**互斥**：不会在有人说话时出冷场那一句，
   也不会在安静时再应一声 / 垫一句。
4. **两人同时说**只在「这边正在说 / 正在准备说，人又开口」时问，两个入口按期三已收下的读法：
   ①**正在准备说 / 正在回答**（闸已开、这句还没落地）；②**正在说一句垫句**（1 秒窗口）。
   **应一声不算**（它不是一轮回答）；不要收窄成「只在已开闸之后」。

这里只做三件事：
- 一张能被代码、台面、验收逐条对照的口径表（`policy_table()`）；
- 两处共用的纯函数：静默闸（`speech_blocked`）与同刻让位（`same_tick_winner`）；
- 给台面 / 摘要一份 `policy_snapshot()`（静态口径 + 这一席已出声几次 + 上一声）。

不连 8009、不发请求、不碰计时器；四处的问法、出口与数字仍只在各期模块里。
"""
from __future__ import annotations

# ---- 四处的名字（台面 / 摘要 / 验收共用一套字面） -------------------------------------------
SLOT_ACK = "ack"              # 应一声
SLOT_CUSHION = "cushion"      # 垫一句（期二；期三的「垫句抢回」也是这一种出声）
SLOT_FLOOR = "floor"          # 两人同时说四选（它问「插话去哪」，本身不是一句出声）
SLOT_QUIET = "quiet"          # 冷场开口（主动接一句 / 问一句）
SLOT_LABELS = {SLOT_ACK: "应一声",
               SLOT_CUSHION: "垫一句",
               SLOT_FLOOR: "两人同时说",
               SLOT_QUIET: "冷场开口"}

# 垫句 > 应声：同刻两者都该出时只出垫句（验收卡第 2 条）。
PRIORITY = (SLOT_CUSHION, SLOT_ACK)
PRIORITY_NOTE = "同一刻垫句优先于应声：只出垫句，不应声"

# 有人说话 / 没人说话两边互斥（验收卡第 3 条）。
SPEAKING_SLOTS = (SLOT_ACK, SLOT_CUSHION)
SILENT_SLOTS = (SLOT_QUIET,)
EXCLUSIVE_NOTE = "有人在说 → 应声 / 垫句；没人说话 → 冷场。两边触发互斥，不在同一段安静里互相抢答"

# 两人同时说的两个入口（期三已收下的读法，期五沿用，不收窄）。应一声不算。
FLOOR_WINDOWS = ("preparing", "cushion_speaking")
FLOOR_WINDOW_LABELS = {"preparing": "正在准备说 / 正在回答（闸已开、这句还没落地）",
                       "cushion_speaking": "正在说一句垫句（1 秒窗口）"}
FLOOR_EXCLUDED = (SLOT_ACK,)
FLOOR_NOTE = ("只在「正在说 / 正在准备说」时问：①正在准备说 / 正在回答；②正在说一句垫句。"
              "应一声不算")

# 口径表：四处何时问 / 谁优先 / 出声之后去哪（一页能对照）。台词与数字全部引用各期已收下的值。
POLICY_TABLE = (
    {"slot": SLOT_CUSHION, "label": "垫一句（含垫句抢回）",
     "when": "人还在说、节奏=积极、这一句已连续说满 1.5 秒",
     "priority": "1（最高）：与应声同刻只出垫句",
     "after": "记住（进本席上下文）；不进 Agent、不选工具、不开闸",
     "remember": True, "agent": False},
    {"slot": SLOT_ACK, "label": "应一声",
     "when": "人还在说、这句还没说完（三个等待档里），同一版字上的判断说可以",
     "priority": "2：同刻让位给垫句",
     "after": "不记住（连上下文也不进）；不开闸、不进 Agent",
     "remember": False, "agent": False},
    {"slot": SLOT_FLOOR, "label": "两人同时说",
     "when": "只在那两个入口：①正在准备说 / 正在回答；②正在说一句垫句。应一声不算",
     "priority": "不跟另三句抢「谁出声」：它只定插话去哪；四条出口里的「垫句抢回」仍受同刻一句约束",
     "after": "让路=作废本轮（正在准备说）/ 停下这句垫句（正在说垫句），话都留下；说完再接 / 垫句抢回=插话排队随后回答；继续说=只进旁听；都不开闸",
     "remember": None, "agent": False},
    {"slot": SLOT_QUIET, "label": "冷场开口",
     "when": "只在中枢最后一句说完后安静到刻度（约 4 秒 / 10 秒 / 之后每 8 秒），且真的没人说话",
     "priority": "与应声 / 垫句互斥：有人在说就不会问；安静时也不会再应声 / 垫句",
     "after": "记住（进本席上下文），只接上文；不进 Agent、不选工具、不开闸",
     "remember": True, "agent": False},
)

# 静默闸：出声后 1 秒内谁都不再出声。数字直接引用期二「出声后 1 秒内不再出声」的常量。
from gate.hub_pace import SPEAK_HOLD_MS  # noqa: E402  (放在表后，避免表被 import 顺序影响)

ONE_SENTENCE_HOLD_MS = int(SPEAK_HOLD_MS)


def speech_hold_ms(gap_ms=None) -> int:
    """这一处「出声后多久内不再出声」：至少 1 秒；应声 / 垫句各自还有自己的频次闸（3 秒 / 1 秒）。"""
    return max(ONE_SENTENCE_HOLD_MS, int(gap_ms or 0))


def speech_blocked(*, now_ms, last_spoken_ms, gap_ms=None) -> tuple[bool, str]:
    """四处共用的静默闸：返回 (这一拍能不能出声, 不能出的理由)。

    与应声 / 垫句共用一个「上次出声」锚点（期一 / 期二已收下）：只要离上一声不到 1 秒，
    应声 / 垫句 / 垫句抢回 / 冷场开口谁都不再出声（「同一时刻最多一句」）。
    """
    if last_spoken_ms is None:
        return False, ""
    hold = speech_hold_ms(gap_ms)
    elapsed = int(now_ms) - int(last_spoken_ms)
    if elapsed < hold:
        return True, ("离上一声不到 %.1f 秒（同一时刻最多一句，出声后 1 秒内不再出声）"
                      % (hold / 1000.0))
    return False, ""


def same_tick_winner(*, cushion: bool, ack: bool) -> str:
    """同一刻应声与垫句都该出时：返回该出声的那一处（`cushion` / `ack` / 空串=都不出）。

    只判「谁优先」，不判「该不该」：该不该仍由 `hub_ack.decide_ack` / `hub_pace.decide_cushion`
    各自的闸决定（本模块不重写任何一道闸）。
    """
    if cushion:
        return SLOT_CUSHION
    if ack:
        return SLOT_ACK
    return ""


def speaking_mode(user_speaking: bool) -> str:
    """有人说话 → 应声 / 垫句；没人说话 → 冷场。台面上写这一行，把两边互斥看死。"""
    return "应声 / 垫句（有人在说）" if user_speaking else "冷场（没人说话）"


def policy_meta() -> dict:
    """静态口径（台面 / 摘要 / 验收共用）：表 + 优先级 + 互斥 + 两个入口 + 静默闸。"""
    return {
        "decided": True,
        "new_decisions": 0,          # 期五只收口，不新增判断（验收卡「不含新增判断」）
        "one_sentence_hold_ms": int(ONE_SENTENCE_HOLD_MS),
        "priority": list(PRIORITY),
        "priority_note": PRIORITY_NOTE,
        "speaking_slots": list(SPEAKING_SLOTS),
        "silent_slots": list(SILENT_SLOTS),
        "exclusive_note": EXCLUSIVE_NOTE,
        "floor_windows": list(FLOOR_WINDOWS),
        "floor_window_labels": dict(FLOOR_WINDOW_LABELS),
        "floor_excluded": list(FLOOR_EXCLUDED),
        "floor_note": FLOOR_NOTE,
        "table": [dict(row) for row in POLICY_TABLE],
        "same_moment_rule": "同一时刻最多一句；出声后 1 秒内不再出声",
        "note": ("期五只把四处已收下的出声收成一套先后：同一时刻最多一句、垫句优先于应声、"
                 "有人说话走应声/垫句、没人说话走冷场、两人同时说只在正在说/正在准备说（垫句那一小段也算，"
                 "应一声不算）。不加新判断、不改任何数字。"),
    }


def policy_snapshot(*, ack_spoken: int = 0, cushion_spoken: int = 0, quiet_spoken: int = 0,
                    floor_asks: int = 0, last_spoken_ms=None, last_text: str = "",
                    now_ms=None) -> dict:
    """这一席的实时口径快照（台面 / 摘要）：静态表 + 已出声次数 + 上一声 + 这一拍能不能再出声。"""
    blocked, reason = speech_blocked(now_ms=now_ms if now_ms is not None else last_spoken_ms or 0,
                                     last_spoken_ms=last_spoken_ms)
    if now_ms is None:
        blocked, reason = False, ""
        if last_spoken_ms is not None:
            reason = "上一声在 %dms；出声后 1 秒内谁都不再出声" % int(last_spoken_ms)
    return {
        "policy": policy_meta(),
        "counts": {SLOT_ACK: int(ack_spoken), SLOT_CUSHION: int(cushion_spoken),
                   SLOT_FLOOR: int(floor_asks), SLOT_QUIET: int(quiet_spoken)},
        "last_spoken": {"t_ms": None if last_spoken_ms is None else int(last_spoken_ms),
                        "text": last_text or "",
                        "hold_ms": int(ONE_SENTENCE_HOLD_MS)},
        "blocked_this_tick": bool(blocked),
        "blocked_reason": reason,
    }


__all__ = ["SLOT_ACK", "SLOT_CUSHION", "SLOT_FLOOR", "SLOT_QUIET", "SLOT_LABELS", "PRIORITY",
           "PRIORITY_NOTE", "SPEAKING_SLOTS", "SILENT_SLOTS", "EXCLUSIVE_NOTE", "FLOOR_WINDOWS",
           "FLOOR_WINDOW_LABELS", "FLOOR_EXCLUDED", "FLOOR_NOTE", "POLICY_TABLE",
           "ONE_SENTENCE_HOLD_MS", "speech_hold_ms", "speech_blocked", "same_tick_winner",
           "speaking_mode", "policy_meta", "policy_snapshot"]
