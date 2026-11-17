"""中枢「要不要应一声」：陪着说的一小句，不是回答。

期一（2026-11-16）只做两件事：

1. **最小出声出口**：中枢能自己出一句很短的话（几个字），不选工具、不执行、不开闸；
   本期先出字到测试台（`ack_spoken` 事件）＋ 播报桩（同一份文本，前端可见），**不接真 TTS**。
2. **同一版出字上多问一道「要不要应一声」**：与「说没说完 / 要不要回答」同一套判断模型、
   同一个服务（8009 `/v1/systemone`，`think_mode: never`、请求不带 `model`）。

应一声的三道闸（用户 2026-11-16 已拍板，落在这里，不另出读法）：

- **同一轮两次至少隔 3 秒**（`ACK_GAP_MS`），出声后 1 秒内不再出声（被 3 秒覆盖）；
- **一句 3–6 个字**（`ACK_MIN_CHARS` / `ACK_MAX_CHARS`，按 `shared.text.normalize` 数字）；
- **判断「说完了」把握高时不出**；同一版字上「不要回答」（不是对车说）一律不应声；
  判断没回来 / 失败也一律不出声（R3）。

应一声**只作用于本席**、**不跨席**；**不记住**（不进上下文、不进 Agent）。
应一声**不延长也不缩短**任何一档等待（0.7 / 1.5 / 3 秒由 `TurnSession` 自己的计时器管，
本模块只判断「要不要出声」，不碰 `_silence_ms` 的任何锚点）。
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

# 判断问题（底稿，逐字落地）：问的是「人还在说、中枢要不要陪一声」，不是「要不要回答」。
ACK_INSTRUCTIONS = (
    "用户还在说话、这句话还没说完，语音助手要不要应一声（就像「嗯」「是吗」那样）"
    "让对方知道你还在听着？结合这句话，以及此刻已有的场景描述。"
    "应一声：只是陪着听，不回答、不执行、不打断；"
    "不出声：判断不了、对方已经说完了、或者在和旁边人说话。"
)
ACK_CRITERIA = {"ack": "可以应一声（陪着说）", "quiet": "不出声（继续听）"}
ACK_ORDER = ("ack", "quiet")

# 本期出声的「这句话」只能从这一小撮里取：每句 3–6 个字，短到不会被当成抢话。
ACK_WORDS = (
    "嗯，您说",
    "是吗，然后呢",
    "我在听着",
    "嗯，接着说",
    "哦，是这样",
    "对，然后呢",
    "嗯嗯，明白了",
    "您继续说",
    "好的，您说",
    "听着呢",
)
ACK_MIN_CHARS = 3
ACK_MAX_CHARS = 6

# 应一声的频次：同一轮两次至少隔 3 秒；出声后 1 秒内不再出声（后者被前者覆盖，写出来是为了别被改小）。
ACK_GAP_MS = int(os.environ.get("HUB_ACK_GAP_MS", "3000"))
ACK_AFTER_SPEAK_MS = int(os.environ.get("HUB_ACK_HOLD_MS", "1000"))
# 应声只认「还在说」这一侧：判断回来时这一版字已经过去这么久，就不再补一声（不追着旧话出声）。
ACK_STALE_MS = int(os.environ.get("HUB_ACK_STALE_MS", "1500"))
# 自己刚说的那句被本机麦收回来：这段时间内、高度重合的字当回声丢掉（极短应声不必停听）。
ACK_ECHO_MS = int(os.environ.get("HUB_ACK_ECHO_MS", "1200"))

# 应声判断的起判线：**本期临时起始值，未拍板**（与开闸线不是一回事，开闸线仍 0.5 未拍）。
ACK_MIN_PROB = float(os.environ.get("HUB_ACK_MIN_PROB", "0.55"))
ACK_MIN_CONF = float(os.environ.get("HUB_ACK_MIN_CONF", "0.50"))
THRESHOLD_NOTE = "应声起判线未拍，本期临时用 P(应一声)≥%g 且 confidence≥%g" % (ACK_MIN_PROB, ACK_MIN_CONF)

SOURCE_MODEL = "判断模型 8009（与「说没说完 / 要不要回答」同一套）"
MODE_MODEL = "kev_choice"
TEMP_SOURCE = "离线替身（仅供离线演示）"
TEMP_MODEL = "rule/离线替身（未拍板）"

ACK_TIMEOUT_S = float(os.environ.get("HUB_ACK_TIMEOUT_S", "2.8"))
ACK_CLIENT = httpx2.Client(timeout=ACK_TIMEOUT_S, trust_env=False, follow_redirects=False,
                           limits=httpx2.Limits(max_connections=6, max_keepalive_connections=6,
                                                keepalive_expiry=60.0))
atexit.register(ACK_CLIENT.close)


def _seats(seat: str) -> str:
    return {"D": "主驾", "P": "副驾"}.get(str(seat).upper(), seat)


def ack_question() -> dict:
    """本版字上的「要不要应一声」：二选一（应一声 / 不出声）。"""
    return {"type": "choice", "instructions": ACK_INSTRUCTIONS,
            "criteria": dict(ACK_CRITERIA)}


def ack_state(text: str, scene=None) -> str:
    """动态输入与「要不要回答」同一形状：当前这一版出字 + 读心此刻已有的那句场景。"""
    scene_text = ""
    if isinstance(scene, dict):
        scene_text = str(scene.get("summary") or "").strip()
    return json.dumps({"utterance": text or "", "scene": scene_text},
                      ensure_ascii=False, allow_nan=False)


def ack_chars(text: str) -> int:
    """按四模块共用的归一（去标点/空白）数字数：3–6 字是应声的硬闸。"""
    return len(normalize(text))


def ack_text(index: int) -> str:
    """本期不了「说什么」：从固定的一小撮里按下标取一句，保证 3–6 字、可回放、不随机。"""
    return ACK_WORDS[int(index) % len(ACK_WORDS)]


def _verdict(score: float, confidence: float, choice, text: str, scene, seat: str, *,
             wall_ms: float, kev_ms=None) -> dict:
    say = score >= ACK_MIN_PROB and confidence >= ACK_MIN_CONF
    scene_block = {"summary": "", "revision": None, "age_ms": None, "missing": True}
    if isinstance(scene, dict):
        scene_block = {"summary": scene.get("summary"), "revision": scene.get("revision"),
                       "age_ms": scene.get("age_ms"), "missing": not bool(scene.get("summary"))}
    reason = ("P(应一声) %.2f %s 临时线 %g 且 confidence %.2f %s %g → %s。" %
              (score, "≥" if score >= ACK_MIN_PROB else "<", ACK_MIN_PROB,
               confidence, "≥" if confidence >= ACK_MIN_CONF else "<", ACK_MIN_CONF,
               "可以应一声" if say else "不出声"))
    return {
        "say": say,
        "state": "ok",
        "choice": choice,
        "score": round(score, 3),
        "confidence": round(confidence, 3),
        "threshold": ACK_MIN_PROB,
        "threshold_conf": ACK_MIN_CONF,
        "threshold_note": THRESHOLD_NOTE,
        "reason": reason,
        "source": SOURCE_MODEL,
        "model": KEV_MODEL,
        "mode": MODE_MODEL,
        "seat": _seats(seat),
        "scene_used": scene_block,
        "wall_ms": round(wall_ms, 1),
        "kev_ms": kev_ms,
        "latency_ms": round(wall_ms, 1),
        "spoken": False,
        "spoken_text": None,
    }


def _parse_payload(raw: dict) -> dict:
    """严格校验二选一响应（只有文档里的字段；多出来的活服务字段也容忍）。"""
    if not isinstance(raw, dict):
        raise ValueError("Malformed ack judge response")
    answers = raw.get("answers")
    if not isinstance(answers, dict) or set(answers) != {"ack"}:
        raise ValueError("Malformed ack judge answers")
    answer = answers["ack"]
    if not isinstance(answer, dict):
        raise ValueError("Malformed ack judge answer")
    if set(answer) - {"type", "choice", "confidence", "probabilities", "none_probability"}:
        raise ValueError("Malformed ack judge answer fields")
    if "type" in answer and answer["type"] != "choice":
        raise ValueError("Malformed ack judge answer type")
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict) or set(probabilities) != set(ACK_ORDER):
        raise ValueError("Malformed ack judge probabilities")
    scores = {name: probability(probabilities[name]) for name in ACK_ORDER}
    confidence = probability(answer.get("confidence"))
    choice = answer.get("choice")
    if choice not in ACK_ORDER:
        raise ValueError("Malformed ack judge choice")
    if "none_probability" in answer:
        probability(answer["none_probability"])
    return {"ack": scores["ack"], "quiet": scores["quiet"], "choice": choice,
            "confidence": confidence}


def score_ack(text: str, scene=None, seat: str = "D", *, client=None, base=None) -> dict:
    """真判断：同一台 8009 问「要不要应一声」，失败直接抛出（调用方按「不出声」处置）。"""
    client = client or ACK_CLIENT
    base = (base or KEV_EOT_BASE).rstrip("/")
    questions = {"ack": ack_question()}
    began = time.monotonic()
    raw = kev_call(client, ack_state(text, scene), questions, base=base)
    wall_ms = (time.monotonic() - began) * 1000
    parsed = _parse_payload(raw)
    if parsed["ack"] < 0 or parsed["ack"] > 1 or not math.isfinite(parsed["confidence"]):
        raise ValueError("Invalid ack score")
    return _verdict(parsed["ack"], parsed["confidence"], parsed["choice"], text, scene, seat,
                    wall_ms=wall_ms, kev_ms=raw.get("latency_ms"))


# ---- 离线替身（只在显式 HUB_ACK=rule 时用；不冒充真判断） ------------------------------------
def rule_ack(text: str, scene=None, seat: str = "D") -> dict:
    """离线替身：**一律判「可以应一声」**，出不出声交给中枢三道闸（频次 / 字数 / 不要回答）。

    它不是产品口径的判法，只在 `HUB_ACK=rule`（离线测试 / 无服务演示）时使用；来源照实写。
    这样离线回放能验到「应声只出现在等待档、两次隔 3 秒、说完了不出、不要回答不出」。
    """
    began = time.monotonic()
    scene_block = {"summary": "", "revision": None, "age_ms": None, "missing": True}
    if isinstance(scene, dict):
        scene_block = {"summary": scene.get("summary"), "revision": scene.get("revision"),
                       "age_ms": scene.get("age_ms"), "missing": not bool(scene.get("summary"))}
    return {"say": True, "state": "ok", "choice": "ack", "score": 0.8, "confidence": 0.9,
            "threshold": ACK_MIN_PROB, "threshold_conf": ACK_MIN_CONF,
            "threshold_note": THRESHOLD_NOTE,
            "reason": "离线替身：一律判「可以应一声」（出不出声由三道闸决定）。",
            "source": TEMP_SOURCE, "model": TEMP_MODEL, "mode": "rule",
            "seat": _seats(seat), "scene_used": scene_block,
            "wall_ms": round((time.monotonic() - began) * 1000, 1),
            "latency_ms": round((time.monotonic() - began) * 1000, 1),
            "spoken": False, "spoken_text": None}


def ack_meta() -> dict:
    return {"source": SOURCE_MODEL, "model": KEV_MODEL, "mode": MODE_MODEL,
            "base": KEV_EOT_BASE, "question": ACK_INSTRUCTIONS,
            "threshold": ACK_MIN_PROB, "threshold_conf": ACK_MIN_CONF,
            "threshold_note": THRESHOLD_NOTE, "decided": False,
            "gap_ms": ACK_GAP_MS, "min_chars": ACK_MIN_CHARS, "max_chars": ACK_MAX_CHARS,
            "words": list(ACK_WORDS),
            "speech": "台面出字（ack_spoken）＋ 播报桩；本期不接真 TTS",
            "note": "应一声＝中枢的陪着说：不出声就不开闸、不进 Agent、不选工具、不执行；"
                    "应声起判线未拍，本期临时用 P≥%g 且 confidence≥%g" % (ACK_MIN_PROB, ACK_MIN_CONF)}


def build_ack_fn(mode: str | None = None):
    """构造 `GateKeeper(ack_fn=...)` 用的判断函数（与 `build_accept_fn` 同一风格）。"""
    choice = (mode or os.environ.get("HUB_ACK", "model")).strip().lower() or "model"
    if choice in ("model", "kev", "real", "judge"):
        def _fn(text, scene=None, seat="D"):
            return score_ack(text, scene=scene, seat=seat)
        _fn.meta = ack_meta()
        return _fn
    if choice in ("rule", "temp", "offline"):
        def _fn(text, scene=None, seat="D"):
            return rule_ack(text, scene=scene, seat=seat)
        _fn.meta = {"source": TEMP_SOURCE, "model": TEMP_MODEL, "mode": "rule",
                    "threshold": ACK_MIN_PROB, "threshold_conf": ACK_MIN_CONF,
                    "threshold_note": THRESHOLD_NOTE, "decided": False,
                    "gap_ms": ACK_GAP_MS, "min_chars": ACK_MIN_CHARS, "max_chars": ACK_MAX_CHARS,
                    "words": list(ACK_WORDS),
                    "speech": "台面出字（ack_spoken）＋ 播报桩；本期不接真 TTS",
                    "note": "离线替身：一律判「可以应一声」，出不出声由中枢三道闸决定"}
        return _fn
    raise ValueError("HUB_ACK=" + choice + " 未实现：可选 model（真判断，默认）/ rule（离线替身）")


def decide_ack(*, eot: dict | None, accept: dict | None, ack: dict | None,
               last_spoken_ms: int | None, now_ms: int, gap_ms: int = ACK_GAP_MS,
               text: str = "", min_chars: int = ACK_MIN_CHARS,
               max_chars: int = ACK_MAX_CHARS) -> tuple[bool, str]:
    """三道闸的**唯一**判据：返回 (要不要出声, 理由)。调用方只管出声、不管规则。

    - 应声判断没回来 / 超时 / 失败 → 不出声（R3）；
    - 判停这一路没回来 / 失败 → 不出声；
    - 判停说「说完了」且把握高（`may_confirm and may_start_early`）→ 不出声（当刻要收住）；
    - 「要不要回答」没回来 / 超时 / 失败 → 不出声；判的是「不要回答」→ 不出声（同一版字）；
    - 同一轮两次应声不到 3 秒（含出声后 1 秒内）→ 不出声；
    - 这一句不在 3–6 字 → 不出声。
    """
    ack = ack or {}
    if ack.get("state") in (None, "pending"):
        return False, "应声判断还没回来"
    if ack.get("state") != "ok":
        return False, "应声判断失败 / 超时：不出声"
    if not ack.get("say"):
        return False, "应声判断说「不出声」"
    if not eot:
        return False, "判停还没回来：先不出声"
    if eot.get("label") in ("error", None) or eot.get("error"):
        return False, "判停失败：不出声"
    if eot.get("may_confirm") and eot.get("may_start_early"):
        return False, "判停说「说完了」（当刻要收住）：不出声"
    if accept is None or accept.get("state") in (None, "pending"):
        return False, "要不要回答还没回来：不出声"
    if accept.get("state") != "ok" or accept.get("accept") is None:
        return False, "要不要回答失败 / 超时：不出声"
    if accept.get("accept") is False:
        return False, "同一版字「不要回答」（不是对车说）：不出声"
    hold = max(int(gap_ms), ACK_AFTER_SPEAK_MS)
    if last_spoken_ms is not None and now_ms - int(last_spoken_ms) < hold:
        return False, "离上一声不到 %.1f 秒" % (hold / 1000.0)
    chars = ack_chars(text)
    if chars < min_chars or chars > max_chars:
        return False, "这一句 %d 个字，不在 %d–%d 字" % (chars, min_chars, max_chars)
    return True, "可以应一声"


__all__ = ["ACK_INSTRUCTIONS", "ACK_CRITERIA", "ACK_ORDER", "ACK_WORDS", "ACK_MIN_CHARS",
           "ACK_MAX_CHARS", "ACK_GAP_MS", "ACK_AFTER_SPEAK_MS", "ACK_STALE_MS", "ACK_ECHO_MS",
           "ACK_MIN_PROB", "ACK_MIN_CONF",
           "THRESHOLD_NOTE", "ack_question", "ack_state", "ack_chars", "ack_text", "score_ack",
           "rule_ack", "ack_meta", "build_ack_fn", "decide_ack", "SOURCE_MODEL", "TEMP_SOURCE"]
