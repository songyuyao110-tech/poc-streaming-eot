"""中枢「冷场三选」：已经较长时间没人说话了——要不要开口、开口做什么。

期四（2026-11-16）新增的一道判断。依据验收卡 `poc-hub-jev-absorb-p4-20261116-验收.md` 与
方案 `poc-hub-jev-absorb-plan-20261116-DETAIL.md` §3.4 / §4 第 7 条 / §4 第 9 条 / §5.2 期四。

它**不是每版出字都问**：只挂在「安静到刻度、且真的没人说话」这个时刻，每个刻度问一次
（首问约 4 秒；约 10 秒之后才允许「问一句」；之后每 8 秒一拍）。有人开口立刻清零；
判断在飞时人开口，这一回作废、不出声。

三条出口（穷举，全部是已拍板的读法，本模块不另出读法）：

| 出口 | 中枢做什么 | 记住吗 | 开闸吗 |
|---|---|---|---|
| **再等** `wait` | 不出声，等下一个刻度 | — | 否 |
| **主动接一句** `follow` | 接着刚才的话题说一句（推进 / 补充），只接上文，不发起新话题 / 新任务 | 进本席上下文台账 | 否 |
| **问一句** `check` | 问「还在吗 / 要不要继续说」这类一句；**只在约 10 秒之后才允许** | 进本席上下文台账 | 否 |
| 判没回来 / 失败 / 超时 | 不出声（按「再等」走） | — | 否 |

边界（卡面写死，见方案 §3.4 / §4 第 9 条）：冷场开口 **只接上文**，不得凭空开新话题、
不得发起新任务、不得进入「要不要执行」；它**不是**「主动发起对话 / 握手」（wiki `[[忌讳]]`
第 67 条）。冷场开口是中枢的「陪着说」：记住，但**不选工具、不执行、不开闸、不进 Agent**。

本模块只判「三条出口里的哪一条 / 兜底走哪条」，**不碰任何计时器、不碰闸门、不碰台账**：
什么时候问、问完出不出声、说哪句、记在哪里，全在 `TurnSession` 里（与期一应声、期二垫句、
期三四选同一分工）。

关于「起算点」（方案 §4 第 7 条、验收卡已拍板）：从**中枢最后一句说完**起算，不是从用户最后
一次出字起算。中枢自己因冷场开口的那一句**不重新起算**（否则同一段安静会被自己的接话重置，
「约 10 秒之后才允许问一句」永远到不了）；人开口立刻清零。本机没有真 TTS，台词一到台面
即视为「说完」（与期一 / 期二 / 期三「出声＝台面出字」同一口径）；接真 TTS 后应按真实播放
结束时刻起算。
"""
from __future__ import annotations

import atexit
import json
import math
import os
import sys
import time
from pathlib import Path

import httpx2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.dont_write_bytecode = True

from core_pipeline_run import KEV_EOT_BASE, KEV_MODEL, kev_call, probability  # noqa: E402
from shared.text import normalize  # noqa: E402

# ---- 三条出口 --------------------------------------------------------------------------------
QUIET_WAIT = "wait"        # 再等：不出声，等下一个刻度
QUIET_FOLLOW = "follow"    # 主动接一句：接着刚才的话题说一句（只接上文）
QUIET_CHECK = "check"      # 问一句：还在吗 / 要不要继续说（约 10 秒之后才允许）
QUIET_ORDER = (QUIET_WAIT, QUIET_FOLLOW, QUIET_CHECK)
QUIET_LABELS = {QUIET_WAIT: "再等",
                QUIET_FOLLOW: "主动接一句",
                QUIET_CHECK: "问一句"}
QUIET_ACTIONS = {
    QUIET_WAIT: "不出声，等下一个刻度（安静接着算，不重置）",
    QUIET_FOLLOW: "接着刚才的话题说一句（推进 / 补充）；只接上文，不发起新话题、不立新任务，不进 Agent",
    QUIET_CHECK: "问一句「还在吗 / 要不要继续说」；只接上文，不进 Agent（约 10 秒之后才允许）",
}

# 判断没回来 / 失败 / 超时一律走这一条（验收卡已拍板：不出声，按「再等」走）。
QUIET_SAFE = QUIET_WAIT

# ---- 刻度与退避（卡面给了前两个数；第三个是「两次开口之间要退避」的落地值） -----------------------
# 首问约 4 秒：只判要不要开口，这时可以「再等」或「主动接一句」，还不能「问一句」。
QUIET_FIRST_MS = int(os.environ.get("HUB_QUIET_FIRST_MS", "4000"))
# 约 10 秒之后才允许「问一句」。
QUIET_CHECK_AFTER_MS = int(os.environ.get("HUB_QUIET_CHECK_AFTER_MS", "10000"))
# 之后每 8 秒一拍（刻度到点只问一次；不会连着问）。
QUIET_STEP_MS = int(os.environ.get("HUB_QUIET_STEP_MS", "8000"))
# 两次开口之间退避：同一段安静里，中枢两次真正出声至少隔这么久（卡面只写「要退避」，数值落在这里）。
# 取 5 秒：比刻度间隔（4s → 10s → 18s …）短，所以「4 秒接一句、10 秒再问一句」不会被退避挡掉。
QUIET_BACKOFF_MS = int(os.environ.get("HUB_QUIET_BACKOFF_MS", "5000"))
# 只接上文：引用的「刚才那句」最多带这么多个字（再长就截断），保证台词短、不跑题。
QUIET_TOPIC_CHARS = int(os.environ.get("HUB_QUIET_TOPIC_CHARS", "14"))

# 问法（底稿，逐字落地）：问的是「要不要开口 / 开口做什么」，不是「要不要回答」。
QUIET_INSTRUCTIONS = (
    "已经有一段时间没人说话了（从中枢上一句说完起算）。语音助手要不要开口？"
    "结合中枢上一句、刚才在聊的那件事，以及此刻已有的场景描述。"
    "再等：不出声，等下一个刻度。"
    "主动接一句：接着刚才的话题说一句（推进或补充）；只接上文，"
    "不凭空开新话题、不发起新任务、不进入「要不要执行」。"
    "问一句：问「还在吗 / 要不要继续说」这类一句（只有在安静更久之后才允许）。"
    "三种都不是回答请求：不选工具、不执行、不开闸。"
)
QUIET_CRITERIA = {
    QUIET_WAIT: "再等（不出声，等下一个刻度）",
    QUIET_FOLLOW: "主动接一句（接着刚才的话题说一句；只接上文，不发起新话题 / 新任务）",
    QUIET_CHECK: "问一句（还在吗 / 要不要继续说；约 10 秒之后才允许）",
}

# 判断模型给的是「三条出口里的一条」，三条都是有效动作，**不设起判线**（卡面没给，
# 也不该拿一条线把「有结论」当「没结论」）。只把判断失败 / 超时当没结论 → 再等。
SOURCE_MODEL = ("判断模型 8009（与「说没说完 / 要不要回答 / 应不应一声 / 节奏 / 垫一句 / "
                "两人同时说」同一套）")
MODE_MODEL = "kev_choice(quiet)"
TEMP_SOURCE = "离线替身（仅供离线演示）"
TEMP_MODEL = "rule/离线替身（未拍板）"
THRESHOLD_NOTE = ("冷场三选不设起判线（三条都是有效出口）；判断没回来 / 失败 / 超时一律按"
                  "「再等」走；「问一句」另有一道「约 10 秒之后」的刻度闸")

QUIET_TIMEOUT_S = float(os.environ.get("HUB_QUIET_TIMEOUT_S", "2.8"))
QUIET_CLIENT = httpx2.Client(timeout=QUIET_TIMEOUT_S, trust_env=False, follow_redirects=False,
                             limits=httpx2.Limits(max_connections=4, max_keepalive_connections=4,
                                                  keepalive_expiry=60.0))
atexit.register(QUIET_CLIENT.close)


def _seats(seat: str) -> str:
    return {"D": "主驾", "P": "副驾"}.get(str(seat).upper(), seat)


def quiet_label(choice) -> str:
    return QUIET_LABELS.get(choice, "未知出口")


def quiet_question() -> dict:
    """三选一（再等 / 主动接一句 / 问一句）。"""
    return {"type": "choice", "instructions": QUIET_INSTRUCTIONS,
            "criteria": dict(QUIET_CRITERIA)}


def quiet_state(*, quiet_ms, hub_last="", recent="", scene=None, check_allowed=None) -> str:
    """动态输入：安静了多久 + 中枢最后一句 + 刚才在聊的那件事 + 场景 + 这一拍允不允许「问一句」。"""
    scene_text = ""
    if isinstance(scene, dict):
        scene_text = str(scene.get("summary") or "").strip()
    body = {"quiet_ms": int(quiet_ms), "hub_last": hub_last or "", "recent": recent or "",
            "scene": scene_text}
    if check_allowed is not None:
        body["check_allowed"] = bool(check_allowed)
    return json.dumps(body, ensure_ascii=False, allow_nan=False)


# ---- 台词（只接上文） -------------------------------------------------------------------------
# 本机没有「按内容生成一句」的能力（真 TTS / 真生成都不是本期范围）：台词从固定的一小撮里取，
# 每句都把「刚才那句」带进来——这就是「只接上文」的可验落点（不凭空开新话题、不立新任务）。
QUIET_FOLLOW_WORDS = (
    "刚才「%s」，我接着记着呢。",
    "还是「%s」这件事，我接着听。",
    "「%s」这句，我记下了，接着这个就行。",
    "接着刚才「%s」：您说，我跟着听。",
)
QUIET_CHECK_WORDS = (
    "还在吗？刚才「%s」要不要接着说？",
    "还在听吗？「%s」这句，您还接着说吗？",
    "刚才「%s」还没说完，您还接着说吗？",
)
QUIET_FOLLOW_BLANK = "刚才那几句，我接着记着呢。"
QUIET_CHECK_BLANK = "还在吗？要不要接着说刚才的？"


def quiet_fragment(text, limit=None) -> str:
    """「刚才那句」的引用片段：去空白、太长截断（不是把整段念一遍）。"""
    body = (text or "").strip()
    if not normalize(body):
        return ""
    limit = QUIET_TOPIC_CHARS if limit is None else max(1, int(limit))
    return body if len(body) <= limit else body[:limit] + "…"


def quiet_text(outlet, topic="", *, index=0, avoid=None) -> str:
    """这一句说什么：按下标从固定小句里轮着取（不随机、可回放）；`avoid` 保证同一句不重复说。"""
    fragment = quiet_fragment(topic)
    if outlet == QUIET_CHECK:
        bank = tuple(word % fragment for word in QUIET_CHECK_WORDS) if fragment else \
            (QUIET_CHECK_BLANK,)
    else:
        bank = tuple(word % fragment for word in QUIET_FOLLOW_WORDS) if fragment else \
            (QUIET_FOLLOW_BLANK,)
    candidates = [text for text in bank if not avoid or text != avoid] or list(bank)
    return candidates[int(index) % len(candidates)]


def quiet_chars(text: str) -> int:
    return len(normalize(text))


# ---- 结论 / 响应 ----------------------------------------------------------------------------
def _scene_block(scene) -> dict:
    if isinstance(scene, dict):
        return {"summary": scene.get("summary"), "revision": scene.get("revision"),
                "age_ms": scene.get("age_ms"), "missing": not bool(scene.get("summary"))}
    return {"summary": "", "revision": None, "age_ms": None, "missing": True}


def _quiet_verdict(choice, probabilities, confidence, *, quiet_ms, hub_last, recent, scene, seat,
                   wall_ms, kev_ms=None, source=SOURCE_MODEL, model=KEV_MODEL, mode=MODE_MODEL) -> dict:
    probs = {name: round(float(probabilities.get(name) or 0.0), 3) for name in QUIET_ORDER}
    top = float(probs.get(choice) or 0.0)
    return {
        "choice": choice,
        "outlet": choice,
        "label": quiet_label(choice),
        "action": QUIET_ACTIONS.get(choice),
        "probabilities": probs,
        "score": round(top, 3),
        "confidence": round(float(confidence), 3),
        "threshold_note": THRESHOLD_NOTE,
        "reason": "P(%s) %.2f → %s。" % (quiet_label(choice), top, quiet_label(choice)),
        "source": source,
        "model": model,
        "mode": mode,
        "seat": _seats(seat),
        "quiet_ms": int(quiet_ms),
        "hub_last": hub_last or "",
        "recent": recent or "",
        "scene_used": _scene_block(scene),
        "wall_ms": round(wall_ms, 1),
        "kev_ms": kev_ms,
        "latency_ms": round(wall_ms, 1),
        "state": "ok",
    }


def _parse_payload(raw: dict) -> dict:
    """严格校验三选一响应（只有文档里的字段；多出来的活服务字段也容忍）。"""
    if not isinstance(raw, dict):
        raise ValueError("Malformed quiet judge response")
    answers = raw.get("answers")
    if not isinstance(answers, dict) or set(answers) != {"quiet"}:
        raise ValueError("Malformed quiet judge answers")
    answer = answers["quiet"]
    if not isinstance(answer, dict):
        raise ValueError("Malformed quiet judge answer")
    if set(answer) - {"type", "choice", "confidence", "probabilities", "none_probability"}:
        raise ValueError("Malformed quiet judge answer fields")
    if "type" in answer and answer["type"] != "choice":
        raise ValueError("Malformed quiet judge answer type")
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict) or set(probabilities) != set(QUIET_ORDER):
        raise ValueError("Malformed quiet judge probabilities")
    scores = {name: probability(probabilities[name]) for name in QUIET_ORDER}
    confidence = probability(answer.get("confidence"))
    choice = answer.get("choice")
    if choice not in QUIET_ORDER:
        raise ValueError("Malformed quiet judge choice")
    if "none_probability" in answer:
        probability(answer["none_probability"])
    return {"probabilities": scores, "choice": choice, "confidence": confidence}


# ---- 真判断（同一台 8009） --------------------------------------------------------------------
def score_quiet(*, quiet_ms, hub_last="", recent="", scene=None, seat: str = "D", client=None,
                base=None, check_allowed=None) -> dict:
    """真判断：问「要不要开口、开口做什么」。失败直接抛出（调用方按「再等」兜底）。"""
    client = client or QUIET_CLIENT
    base = (base or KEV_EOT_BASE).rstrip("/")
    questions = {"quiet": quiet_question()}
    began = time.monotonic()
    raw = kev_call(client, quiet_state(quiet_ms=quiet_ms, hub_last=hub_last, recent=recent,
                                       scene=scene, check_allowed=check_allowed),
                   questions, base=base)
    wall_ms = (time.monotonic() - began) * 1000
    parsed = _parse_payload(raw)
    if not math.isfinite(parsed["confidence"]):
        raise ValueError("Invalid quiet confidence")
    return _quiet_verdict(parsed["choice"], parsed["probabilities"], parsed["confidence"],
                          quiet_ms=quiet_ms, hub_last=hub_last, recent=recent, scene=scene,
                          seat=seat, wall_ms=wall_ms, kev_ms=raw.get("latency_ms"))


# ---- 离线替身（只在显式 HUB_QUIET=rule 时用；不冒充真判断） ------------------------------------
def _rule_quiet_value() -> str:
    value = (os.environ.get("HUB_QUIET_RULE", QUIET_WAIT) or QUIET_WAIT).strip().lower()
    return value if value in QUIET_ORDER else QUIET_WAIT


def rule_quiet(*, quiet_ms, hub_last="", recent="", scene=None, seat: str = "D",
               check_allowed=None) -> dict:
    """离线替身：**一律判 `HUB_QUIET_RULE` 那一条**（默认再等＝不出声）。

    它只是让离线回放能验「三条出口各跑一条、同一段安静只换选择」：出口用环境变量点，
    真实行为（不出声 / 接一句 / 问一句、记住、不进 Agent）仍由 `TurnSession` 按字面落实。
    """
    began = time.monotonic()
    choice = _rule_quiet_value()
    probabilities = {name: (0.82 if name == choice else round(0.18 / 2, 3)) for name in QUIET_ORDER}
    return _quiet_verdict(choice, probabilities, 0.9, quiet_ms=quiet_ms, hub_last=hub_last,
                          recent=recent, scene=scene, seat=seat,
                          wall_ms=(time.monotonic() - began) * 1000,
                          source=TEMP_SOURCE, model=TEMP_MODEL, mode="rule")


def quiet_meta() -> dict:
    return {"source": SOURCE_MODEL, "model": KEV_MODEL, "mode": MODE_MODEL,
            "base": KEV_EOT_BASE, "question": QUIET_INSTRUCTIONS,
            "outlets": [{"key": name, "label": QUIET_LABELS[name], "action": QUIET_ACTIONS[name]}
                        for name in QUIET_ORDER],
            "safe_outlet": QUIET_SAFE, "threshold_note": THRESHOLD_NOTE, "decided": True,
            "first_ms": QUIET_FIRST_MS, "check_after_ms": QUIET_CHECK_AFTER_MS,
            "step_ms": QUIET_STEP_MS, "backoff_ms": QUIET_BACKOFF_MS,
            "speech": "台面出字（quiet_spoken）＋ 播报桩；本期不接真 TTS",
            "note": "三条出口与失败兜底已拍板（2026-11-16）；只判«哪一条»，不碰计时 / 开闸 / 台账；"
                    "开口只接上文，不是「主动发起」"}


def build_quiet_fn(mode: str | None = None):
    """构造 `GateKeeper(quiet_fn=...)` 用的判断函数（与 `build_floor_fn` 同一风格）。"""
    choice = (mode or os.environ.get("HUB_QUIET", "model")).strip().lower() or "model"
    if choice in ("model", "kev", "real", "judge"):
        def _fn(*, quiet_ms, hub_last="", recent="", scene=None, seat="D", check_allowed=None):
            return score_quiet(quiet_ms=quiet_ms, hub_last=hub_last, recent=recent, scene=scene,
                               seat=seat, check_allowed=check_allowed)
        _fn.meta = quiet_meta()
        return _fn
    if choice in ("rule", "temp", "offline"):
        def _fn(*, quiet_ms, hub_last="", recent="", scene=None, seat="D", check_allowed=None):
            return rule_quiet(quiet_ms=quiet_ms, hub_last=hub_last, recent=recent, scene=scene,
                              seat=seat, check_allowed=check_allowed)
        _fn.meta = {"source": TEMP_SOURCE, "model": TEMP_MODEL, "mode": "rule",
                    "outlets": [{"key": name, "label": QUIET_LABELS[name],
                                 "action": QUIET_ACTIONS[name]} for name in QUIET_ORDER],
                    "safe_outlet": QUIET_SAFE, "threshold_note": THRESHOLD_NOTE, "decided": True,
                    "first_ms": QUIET_FIRST_MS, "check_after_ms": QUIET_CHECK_AFTER_MS,
                    "step_ms": QUIET_STEP_MS, "backoff_ms": QUIET_BACKOFF_MS,
                    "note": "离线替身：一律判 HUB_QUIET_RULE 那一条（默认再等＝不出声）"}
        return _fn
    raise ValueError("HUB_QUIET=" + choice + " 未实现：可选 model（真判断，默认）/ rule（离线替身）")


# ---- 唯一判据 --------------------------------------------------------------------------------
def decide_quiet(*, quiet: dict | None, quiet_ms: int = 0, first_ms: int = QUIET_FIRST_MS,
                 check_after_ms: int = QUIET_CHECK_AFTER_MS) -> tuple[str, str]:
    """冷场三选的**唯一**判据：返回 (走哪条出口, 理由)。调用方只管按出口做事、不管规则。

    顺序：先兜底（没结论一律「再等」——验收卡已拍板：不出声），再看判断给的是哪条；
    最后过两道刻度闸：还没到首问刻度 → 再等；还没到「问一句」的刻度却判了 check → 再等
    （免得 4 秒那一拍就问「还在吗」）。
    """
    quiet = quiet or {}
    if quiet.get("state") in (None, "pending"):
        return QUIET_SAFE, "冷场判断还没回来 → 再等（不出声）"
    if quiet.get("state") != "ok":
        return QUIET_SAFE, "冷场判断失败 / 超时 → 再等（不出声）"
    choice = quiet.get("outlet") or quiet.get("choice")
    if choice not in QUIET_ORDER:
        return QUIET_SAFE, "冷场判断没给出合法出口 → 再等（不出声）"
    if int(quiet_ms) < int(first_ms):
        return QUIET_SAFE, "还没到首问刻度（约 %.1f 秒）→ 再等（不出声）" % (first_ms / 1000.0)
    if choice == QUIET_CHECK and int(quiet_ms) < int(check_after_ms):
        return QUIET_SAFE, ("这一拍（约 %.1f 秒）还不能问「问一句」（要安静到约 %.1f 秒）→ 再等"
                            % (int(quiet_ms) / 1000.0, check_after_ms / 1000.0))
    return choice, "判断说「%s」" % quiet_label(choice)


__all__ = ["QUIET_WAIT", "QUIET_FOLLOW", "QUIET_CHECK", "QUIET_ORDER", "QUIET_LABELS",
           "QUIET_ACTIONS", "QUIET_SAFE", "QUIET_INSTRUCTIONS", "QUIET_CRITERIA",
           "QUIET_FIRST_MS", "QUIET_CHECK_AFTER_MS", "QUIET_STEP_MS", "QUIET_BACKOFF_MS",
           "QUIET_TOPIC_CHARS", "QUIET_TIMEOUT_S", "THRESHOLD_NOTE", "quiet_question",
           "quiet_state", "quiet_label", "quiet_fragment", "quiet_text", "quiet_chars",
           "score_quiet", "rule_quiet", "quiet_meta", "build_quiet_fn", "decide_quiet",
           "SOURCE_MODEL", "TEMP_SOURCE", "MODE_MODEL"]
