"""中枢「现在是什么节奏」+「要不要垫一句」（期二，2026-11-16）。

两件事，一问一答（验收卡 `poc-hub-jev-absorb-p2-20261116-验收.md`）：

1. **节奏四档**（平常 / 耐心 / 讲述 / 积极）：与「说没说完 / 要不要回答 / 应不应一声」**同一拍**、
   同一版出字上判（每版都判）。节奏**只用来决定要不要问垫句**——不改任何一档等待时长
   （0.7 / 1.5 / 3 秒），不改开闸线，不改让路，也不给「讲述」开专属选项（那属可选期 B，本卡不做）。
2. **垫一句**：**只有**「节奏＝积极」且「对方这一轮已经连续说满 1.5 秒」才问。
   垫一句是中枢的「陪着说」：出一句有内容的小话把话接住，**记住（进上下文）**，
   但**不进 Agent、不选工具、不执行、不开闸**；一句话、十几个字以内，不能写成回答。

已拍板（用户 2026-11-16，逐条落地，不另出读法）：

- 垫句**记住**，但不开闸（不进 Agent / 不选工具 / 不执行）；
- 节奏**开头 1–2 轮先稳住**，之后平滑换档，不因为一版字硬切（`hold_pace()`）；
- **同一时刻最多一句**：垫句优先于应一声，出声后 1 秒内不再出声（`SPEAK_HOLD_MS`）；
- 判断没回来 / 失败 / 超时 → **不垫、不改节奏（保持上一拍）**、不改收口、不改开闸；
- 同一版字若是「不要回答」（不是对车说）→ **不垫句**（应声那条只作用于本席、不跨席）；
- 「说完了」按期一读法：判停说「说完了」的那一版 / 那一当口**不再出声**（应声与垫句都是）；
- 节奏**不外溢**：不改等待时长、不改让路把握、不加讲述专属选项。

两条出口共用同一套「出声」能力与同一个「上次出声」锚点（`TurnSession._ack_last_ms`），
所以垫句与应声不会同时出声；本模块只判断「要不要出声 / 说什么」，不碰任何计时器
（`TurnSession._silence_ms` 的锚点一行不动）。
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

# ---- 节奏：四档 ------------------------------------------------------------------------------
PACE_NORMAL = "normal"
PACE_PATIENT = "patient"
PACE_NARRATIVE = "narrative"
PACE_EAGER = "eager"
PACE_ORDER = (PACE_NORMAL, PACE_PATIENT, PACE_NARRATIVE, PACE_EAGER)
PACE_LABELS = {PACE_NORMAL: "平常", PACE_PATIENT: "耐心",
               PACE_NARRATIVE: "讲述", PACE_EAGER: "积极"}
PACE_CRITERIA = {
    PACE_NORMAL: "平常：一来一回，对方说完就等一个回应",
    PACE_PATIENT: "耐心：对方说得慢、在斟酌，需要给他空间",
    PACE_NARRATIVE: "讲述：对方在讲一件事（叙述 / 解释），先别打断",
    PACE_EAGER: "积极：对方说得很急、话头在对方手上，可以主动接一句",
}
PACE_INSTRUCTIONS = (
    "现在这通对话是什么节奏？结合这句话，以及此刻已有的场景描述。"
    "平常：一来一回，对方说完就等一个回应。"
    "耐心：对方说得慢、在斟酌，需要给他空间。"
    "讲述：对方在讲一件事（叙述 / 解释），先别打断。"
    "积极：对方说得很急、话头在对方手上，可以主动接一句。"
)
# 节奏只用于「要不要问垫句」：这四档**不改**等待时长、不改开闸线、不加讲述专属选项。
PACE_SCOPE = "只用于决定要不要问垫句；不改 0.7 / 1.5 / 3 秒，不改开闸线，不改让路"

# 开场保持 + 平滑换档（拍板 2：「开头 1–2 轮先稳住，之后平滑换档，不要因为一版字就硬切」）。
PACE_HOLD_TURNS = int(os.environ.get("HUB_PACE_HOLD_TURNS", "2"))
PACE_SWITCH_MARGIN = float(os.environ.get("HUB_PACE_SWITCH_MARGIN", "0.15"))

# ---- 垫一句 ----------------------------------------------------------------------------------
CUSHION_INSTRUCTIONS = (
    "对话节奏是「积极」，对方已经连续说满 1.5 秒还没停。语音助手要不要垫一句"
    "（插一句有内容的小话，把话头接住）？结合这句话，以及此刻已有的场景描述。"
    "垫一句：接着上文说一句小话、把话接住，不回答、不选工具、不执行、不抢话头；"
    "不垫：让对方把话说完。"
)
CUSHION_CRITERIA = {"cushion": "垫一句（把话接住）", "hold": "不垫（让对方说完）"}
CUSHION_ORDER = ("cushion", "hold")

# 垫句的话只能从这一小撮里取：一句话、十几个字以内（不是回答，也不能长到像回答）。
# 垫句固定稿：不绑具体场景的 10 句短话（用户 2026-11-17 10:28 批准的字库，顺序即下表）。
# 第 7 句「嗯，您先说」、第 10 句「我在听着呢」是按现行 4–14 字闸对原拟更短说法的**最小加长**，
# 不加长会被字数闸挡住、永远说不出去；落库原文就是句子本身，不带任何括号说明。
CUSHION_WORDS = (
    "您接着说，我听着",
    "这个我记一下",
    "我明白您的意思",
    "嗯，我听着呢",
    "您继续说",
    "我跟着听",
    "嗯，您先说",
    "好，我记着",
    "您先说完",
    "我在听着呢",
)
CUSHION_MIN_CHARS = 4
CUSHION_MAX_CHARS = 14

# 只在这两道闸都成立时才问：节奏＝积极 + 这一轮已经连续说满 1.5 秒。
CUSHION_MIN_SPEECH_MS = int(os.environ.get("HUB_CUSHION_MIN_SPEECH_MS", "1500"))
# 垫句这一问回来时，这一版字已经过去这么久就不再补（不追着旧话出声）。
CUSHION_STALE_MS = int(os.environ.get("HUB_CUSHION_STALE_MS", "1500"))
# 垫句与应声共用的「出声后不再出声」窗口（拍板 5：出声后 1 秒内不再出声）。
SPEAK_HOLD_MS = int(os.environ.get("HUB_SPEAK_HOLD_MS", "1000"))
# 垫句之间的间隔：**卡面只给了「出声后 1 秒内不再出声」，没给垫句专属间隔**——本期就按这一条落，
# 不另外发明一个更大的数（对方一直说、节奏仍是积极时，隔一版可以再垫一句，台面能数出次数）。
CUSHION_GAP_MS = int(os.environ.get("HUB_CUSHION_GAP_MS", "1000"))

# 垫句判断的起判线：**本期临时起始值，未拍板**（与开闸线不是一回事，开闸线仍 0.5 未拍）。
CUSHION_MIN_PROB = float(os.environ.get("HUB_CUSHION_MIN_PROB", "0.55"))
CUSHION_MIN_CONF = float(os.environ.get("HUB_CUSHION_MIN_CONF", "0.50"))
THRESHOLD_NOTE = ("垫句起判线未拍，本期临时用 P(垫一句)≥%g 且 confidence≥%g"
                  % (CUSHION_MIN_PROB, CUSHION_MIN_CONF))

SOURCE_MODEL = "判断模型 8009（与「说没说完 / 要不要回答 / 应不应一声」同一套）"
MODE_PACE = "kev_choice(pace)"
MODE_CUSHION = "kev_choice(cushion)"
TEMP_SOURCE = "离线替身（仅供离线演示）"
TEMP_MODEL = "rule/离线替身（未拍板）"

PACE_TIMEOUT_S = float(os.environ.get("HUB_PACE_TIMEOUT_S", "2.8"))
CUSHION_TIMEOUT_S = float(os.environ.get("HUB_CUSHION_TIMEOUT_S", "2.8"))
PACE_CLIENT = httpx2.Client(timeout=PACE_TIMEOUT_S, trust_env=False, follow_redirects=False,
                            limits=httpx2.Limits(max_connections=6, max_keepalive_connections=6,
                                                 keepalive_expiry=60.0))
CUSHION_CLIENT = httpx2.Client(timeout=CUSHION_TIMEOUT_S, trust_env=False, follow_redirects=False,
                               limits=httpx2.Limits(max_connections=6, max_keepalive_connections=6,
                                                    keepalive_expiry=60.0))
atexit.register(PACE_CLIENT.close)
atexit.register(CUSHION_CLIENT.close)


def _seats(seat: str) -> str:
    return {"D": "主驾", "P": "副驾"}.get(str(seat).upper(), seat)


def _scene_block(scene) -> dict:
    if isinstance(scene, dict):
        return {"summary": scene.get("summary"), "revision": scene.get("revision"),
                "age_ms": scene.get("age_ms"), "missing": not bool(scene.get("summary"))}
    return {"summary": "", "revision": None, "age_ms": None, "missing": True}


def pace_label(pace) -> str:
    return PACE_LABELS.get(pace, "未知")


def pace_question() -> dict:
    """节奏这一问：四选一（平常 / 耐心 / 讲述 / 积极）。"""
    return {"type": "choice", "instructions": PACE_INSTRUCTIONS,
            "criteria": dict(PACE_CRITERIA)}


def cushion_question() -> dict:
    """垫句这一问：二选一（垫一句 / 不垫）。只有在「积极 + 已满 1.5 秒」时才发。"""
    return {"type": "choice", "instructions": CUSHION_INSTRUCTIONS,
            "criteria": dict(CUSHION_CRITERIA)}


def pace_state(text: str, scene=None, *, speech_ms=None) -> str:
    """动态输入：当前这一版出字 + 场景；节奏这一问额外带上「这一轮已经连续说了多久」。"""
    scene_text = ""
    if isinstance(scene, dict):
        scene_text = str(scene.get("summary") or "").strip()
    body = {"utterance": text or "", "scene": scene_text}
    if speech_ms is not None:
        body["speech_ms"] = int(speech_ms)
    return json.dumps(body, ensure_ascii=False, allow_nan=False)


def cushion_chars(text: str) -> int:
    """按四模块共用的归一（去标点/空白）数字数：一句话、十几个字以内是硬闸。"""
    return len(normalize(text))


def cushion_text(index: int) -> str:
    """本期不做「说什么」的模型：从固定的一小撮里按下标取一句，保证可回放、不随机。"""
    return CUSHION_WORDS[int(index) % len(CUSHION_WORDS)]


# ---- 节奏：开场保持 + 平滑换档 ----------------------------------------------------------------
def hold_pace(raw_pace, probabilities, current=None, turn=None, *,
              hold_turns: int = PACE_HOLD_TURNS, margin: float = PACE_SWITCH_MARGIN):
    """把「这一版的节奏判断」压成「这一拍实际的节奏」（拍板 2：不因为一版字硬切）。

    返回 `(pace, held, why)`：`held=True` 表示这一次没有换档（还在开场保持 / 领先不够）。

    - 还没定过节奏（`current is None`）→ 先按「平常」稳住；
    - 第 1–`hold_turns` 轮 → 一律保持，不换档（开场一两轮先稳住）；
    - 判的还是现在这一档 → 不变；
    - 判的是别的档 → 只有新档的概率**领先现档 ≥ `margin`** 才换，否则保持（平滑换档）。
    """
    current = current if current in PACE_ORDER else None
    if current is None:
        return PACE_NORMAL, True, "开场先稳住：先按「平常」记这一拍"
    try:
        turn_no = int(turn) if turn is not None else None
    except (TypeError, ValueError):
        turn_no = None
    if turn_no is not None and turn_no <= int(hold_turns):
        return current, True, "开场 %d 轮先稳住：第 %d 轮不换档" % (int(hold_turns), turn_no)
    if not raw_pace or raw_pace not in PACE_ORDER or raw_pace == current:
        return current, False, "节奏没变：还是「%s」" % pace_label(current)
    probs = probabilities or {}
    try:
        new_p = float(probs.get(raw_pace) or 0.0)
        cur_p = float(probs.get(current) or 0.0)
    except (TypeError, ValueError):
        return current, True, "节奏分数不可用：保持上一拍「%s」" % pace_label(current)
    if new_p - cur_p >= float(margin):
        return raw_pace, False, ("换档：%s %.2f 领先 %s %.2f ≥ %.2f"
                                 % (pace_label(raw_pace), new_p, pace_label(current), cur_p,
                                    float(margin)))
    return current, True, ("平滑换档：新判 %s 只领先 %.2f（< %.2f），先不硬切"
                           % (pace_label(raw_pace), new_p - cur_p, float(margin)))


def _pace_verdict(raw_pace, probabilities, confidence, text, scene, seat, *, wall_ms,
                  kev_ms=None, source=SOURCE_MODEL, model=KEV_MODEL) -> dict:
    probs = {name: round(float(probabilities.get(name) or 0.0), 3) for name in PACE_ORDER}
    top = float(probs.get(raw_pace) or 0.0)
    return {
        "pace": raw_pace,
        "raw_pace": raw_pace,
        "label": pace_label(raw_pace),
        "probabilities": probs,
        "score": round(top, 3),
        "confidence": round(float(confidence), 3),
        "reason": "P(%s) %.2f → 节奏「%s」。" % (pace_label(raw_pace), top, pace_label(raw_pace)),
        "source": source,
        "model": model,
        "mode": MODE_PACE,
        "scope": PACE_SCOPE,
        "seat": _seats(seat),
        "scene_used": _scene_block(scene),
        "wall_ms": round(wall_ms, 1),
        "kev_ms": kev_ms,
        "latency_ms": round(wall_ms, 1),
        "state": "ok",
    }


def _cushion_verdict(score, confidence, choice, text, scene, seat, *, wall_ms, kev_ms=None,
                     source=SOURCE_MODEL, model=KEV_MODEL) -> dict:
    """垫句结论**按模型给的选档**（用户 2026-11-17 09:32 批准）：choice=cushion 垫、hold 不垫。

    P 与 confidence 只留在详情里做诊断，**不再拿双门槛否决模型已经给出的 choice**；
    选档缺失 / 非法一律按失败处理（不垫），不自行从 P 猜。
    """
    picked = str(choice or "").strip().lower()
    if picked not in CUSHION_ORDER:
        return {
            "say": False, "state": "failed", "choice": choice,
            "score": round(score, 3) if isinstance(score, (int, float)) else None,
            "confidence": round(confidence, 3) if isinstance(confidence, (int, float)) else None,
            "threshold": CUSHION_MIN_PROB, "threshold_conf": CUSHION_MIN_CONF,
            "threshold_note": THRESHOLD_NOTE,
            "reason": ("模型没给合法选档（choice=%r）：按失败处理，这一版不垫" % (choice,)),
            "source": source, "model": model, "mode": MODE_CUSHION, "seat": _seats(seat),
            "scene_used": _scene_block(scene), "wall_ms": round(wall_ms, 1), "kev_ms": kev_ms,
            "latency_ms": round(wall_ms, 1),
        }
    say = picked == "cushion"
    reason = ("模型选档 cushion → 垫一句（前置条件另由中枢把关）；诊断 P %.2f / confidence %.2f"
              % (score, confidence)) if say else \
             ("模型选档 hold → 不垫（诊断 P %.2f / confidence %.2f）" % (score, confidence))
    return {
        "say": say,
        "state": "ok",
        "choice": choice,
        "score": round(score, 3),
        "confidence": round(confidence, 3),
        "threshold": CUSHION_MIN_PROB,
        "threshold_conf": CUSHION_MIN_CONF,
        "threshold_note": THRESHOLD_NOTE,
        "reason": reason,
        "source": source,
        "model": model,
        "mode": MODE_CUSHION,
        "seat": _seats(seat),
        "scene_used": _scene_block(scene),
        "wall_ms": round(wall_ms, 1),
        "kev_ms": kev_ms,
        "latency_ms": round(wall_ms, 1),
    }


def _parse_choice_payload(raw: dict, key: str, order) -> dict:
    """严格校验 one-of-N 响应（只有文档里的字段；多出来的活服务字段也容忍）。"""
    if not isinstance(raw, dict):
        raise ValueError("Malformed %s judge response" % key)
    answers = raw.get("answers")
    if not isinstance(answers, dict) or set(answers) != {key}:
        raise ValueError("Malformed %s judge answers" % key)
    answer = answers[key]
    if not isinstance(answer, dict):
        raise ValueError("Malformed %s judge answer" % key)
    if set(answer) - {"type", "choice", "confidence", "probabilities", "none_probability"}:
        raise ValueError("Malformed %s judge answer fields" % key)
    if "type" in answer and answer["type"] != "choice":
        raise ValueError("Malformed %s judge answer type" % key)
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict) or set(probabilities) != set(order):
        raise ValueError("Malformed %s judge probabilities" % key)
    scores = {name: probability(probabilities[name]) for name in order}
    confidence = probability(answer.get("confidence"))
    choice = answer.get("choice")
    if choice not in order:
        raise ValueError("Malformed %s judge choice" % key)
    if "none_probability" in answer:
        probability(answer["none_probability"])
    return {"probabilities": scores, "choice": choice, "confidence": confidence}


# ---- 真判断（同一台 8009） --------------------------------------------------------------------
def score_pace(text: str, scene=None, seat: str = "D", *, client=None, base=None,
               speech_ms=None) -> dict:
    """真判断：问「现在是什么节奏」。失败直接抛出（调用方按「保持上一拍」处置）。"""
    client = client or PACE_CLIENT
    base = (base or KEV_EOT_BASE).rstrip("/")
    questions = {"pace": pace_question()}
    began = time.monotonic()
    raw = kev_call(client, pace_state(text, scene, speech_ms=speech_ms), questions, base=base)
    wall_ms = (time.monotonic() - began) * 1000
    parsed = _parse_choice_payload(raw, "pace", PACE_ORDER)
    confidence = parsed["confidence"]
    if not math.isfinite(confidence):
        raise ValueError("Invalid pace confidence")
    return _pace_verdict(parsed["choice"], parsed["probabilities"], confidence, text, scene, seat,
                         wall_ms=wall_ms, kev_ms=raw.get("latency_ms"))


def score_cushion(text: str, scene=None, seat: str = "D", *, client=None, base=None,
                  speech_ms=None) -> dict:
    """真判断：问「要不要垫一句」。失败直接抛出（调用方按「不垫」处置）。"""
    client = client or CUSHION_CLIENT
    base = (base or KEV_EOT_BASE).rstrip("/")
    questions = {"cushion": cushion_question()}
    began = time.monotonic()
    raw = kev_call(client, pace_state(text, scene, speech_ms=speech_ms), questions, base=base)
    wall_ms = (time.monotonic() - began) * 1000
    parsed = _parse_choice_payload(raw, "cushion", CUSHION_ORDER)
    score = parsed["probabilities"]["cushion"]
    if score < 0 or score > 1 or not math.isfinite(parsed["confidence"]):
        raise ValueError("Invalid cushion score")
    return _cushion_verdict(score, parsed["confidence"], parsed["choice"], text, scene, seat,
                            wall_ms=wall_ms, kev_ms=raw.get("latency_ms"))


# ---- 离线替身（只在显式 HUB_PACE=rule / HUB_CUSHION=rule 时用；不冒充真判断） -------------------
def _rule_pace_value() -> str:
    value = (os.environ.get("HUB_PACE_RULE", PACE_NORMAL) or PACE_NORMAL).strip().lower()
    return value if value in PACE_ORDER else PACE_NORMAL


def rule_pace(text: str, scene=None, seat: str = "D", speech_ms=None) -> dict:
    """离线替身：**一律判 `HUB_PACE_RULE` 那一档**（默认平常）。

    它只是让离线回放能验「四档各跑一段」：档用环境变量点，出不出垫句仍由中枢的
    「积极 + 已满 1.5 秒」两道闸与收口 / 不要回答那几条决定。不是产品判法。
    """
    began = time.monotonic()
    tier = _rule_pace_value()
    probabilities = {name: (0.82 if name == tier else round(0.18 / 3, 3)) for name in PACE_ORDER}
    return _pace_verdict(tier, probabilities, 0.9, text, scene, seat,
                         wall_ms=(time.monotonic() - began) * 1000,
                         source=TEMP_SOURCE, model=TEMP_MODEL)


def rule_cushion(text: str, scene=None, seat: str = "D", speech_ms=None) -> dict:
    """离线替身：**一律判「垫一句」**，出不出由中枢的两道闸（积极 + 1.5 秒）再拦。"""
    began = time.monotonic()
    return _cushion_verdict(0.8, 0.9, "cushion", text, scene, seat,
                            wall_ms=(time.monotonic() - began) * 1000,
                            source=TEMP_SOURCE, model=TEMP_MODEL)


def pace_meta() -> dict:
    return {"source": SOURCE_MODEL, "model": KEV_MODEL, "mode": MODE_PACE,
            "base": KEV_EOT_BASE, "question": PACE_INSTRUCTIONS,
            "tiers": [{"key": name, "label": PACE_LABELS[name]} for name in PACE_ORDER],
            "scope": PACE_SCOPE, "decided": False,
            "hold_turns": PACE_HOLD_TURNS, "switch_margin": PACE_SWITCH_MARGIN,
            "note": "节奏只用于「要不要问垫句」；开场 1–%d 轮先稳住，之后按概率平滑换档（未拍板）"
                    % PACE_HOLD_TURNS}


def cushion_meta() -> dict:
    return {"source": SOURCE_MODEL, "model": KEV_MODEL, "mode": MODE_CUSHION,
            "base": KEV_EOT_BASE, "question": CUSHION_INSTRUCTIONS,
            "threshold": CUSHION_MIN_PROB, "threshold_conf": CUSHION_MIN_CONF,
            "threshold_note": ("垫句按模型选档执行（cushion 垫 / hold 不垫，2026-11-17 用户批准）；"
                               "P 与 confidence 只作诊断，不再用双门槛否决选档"
                               "（这两条临时线未拍，留作诊断口径）"),
            "decided": False, "judged_by": "model_choice",
            "min_speech_ms": CUSHION_MIN_SPEECH_MS, "gap_ms": CUSHION_GAP_MS,
            "after_speak_ms": SPEAK_HOLD_MS,
            "min_chars": CUSHION_MIN_CHARS, "max_chars": CUSHION_MAX_CHARS,
            "words": list(CUSHION_WORDS),
            "speech": "台面出字（cushion_spoken）＋ 播报桩；本期不接真 TTS",
            "note": "垫一句＝中枢的陪着说：记住（进上下文），但不开闸、不进 Agent、不选工具、不执行"}


def build_pace_fn(mode: str | None = None):
    """构造 `GateKeeper(pace_fn=...)` 用的判断函数（与 `build_ack_fn` 同一风格）。"""
    choice = (mode or os.environ.get("HUB_PACE", "model")).strip().lower() or "model"
    if choice in ("model", "kev", "real", "judge"):
        def _fn(text, scene=None, seat="D", speech_ms=None):
            return score_pace(text, scene=scene, seat=seat, speech_ms=speech_ms)
        _fn.meta = pace_meta()
        return _fn
    if choice in ("rule", "temp", "offline"):
        def _fn(text, scene=None, seat="D", speech_ms=None):
            return rule_pace(text, scene=scene, seat=seat, speech_ms=speech_ms)
        _fn.meta = {"source": TEMP_SOURCE, "model": TEMP_MODEL, "mode": "rule",
                    "tiers": [{"key": name, "label": PACE_LABELS[name]} for name in PACE_ORDER],
                    "scope": PACE_SCOPE, "decided": False,
                    "hold_turns": PACE_HOLD_TURNS, "switch_margin": PACE_SWITCH_MARGIN,
                    "note": "离线替身：一律判 HUB_PACE_RULE 那一档（默认平常）"}
        return _fn
    raise ValueError("HUB_PACE=" + choice + " 未实现：可选 model（真判断，默认）/ rule（离线替身）")


def build_cushion_fn(mode: str | None = None):
    """构造 `GateKeeper(cushion_fn=...)` 用的判断函数。"""
    choice = (mode or os.environ.get("HUB_CUSHION", "model")).strip().lower() or "model"
    if choice in ("model", "kev", "real", "judge"):
        def _fn(text, scene=None, seat="D", speech_ms=None):
            return score_cushion(text, scene=scene, seat=seat, speech_ms=speech_ms)
        _fn.meta = cushion_meta()
        return _fn
    if choice in ("rule", "temp", "offline"):
        def _fn(text, scene=None, seat="D", speech_ms=None):
            return rule_cushion(text, scene=scene, seat=seat, speech_ms=speech_ms)
        _fn.meta = {"source": TEMP_SOURCE, "model": TEMP_MODEL, "mode": "rule",
                    "threshold": CUSHION_MIN_PROB, "threshold_conf": CUSHION_MIN_CONF,
                    "threshold_note": THRESHOLD_NOTE, "decided": False,
                    "min_speech_ms": CUSHION_MIN_SPEECH_MS, "gap_ms": CUSHION_GAP_MS,
                    "after_speak_ms": SPEAK_HOLD_MS,
                    "min_chars": CUSHION_MIN_CHARS, "max_chars": CUSHION_MAX_CHARS,
                    "words": list(CUSHION_WORDS),
                    "speech": "台面出字（cushion_spoken）＋ 播报桩；本期不接真 TTS",
                    "note": "离线替身：一律判「垫一句」，出不出由中枢两道闸决定"}
        return _fn
    raise ValueError("HUB_CUSHION=" + choice + " 未实现：可选 model（真判断，默认）/ rule（离线替身）")


# ---- 唯一判据 --------------------------------------------------------------------------------
def decide_cushion(*, pace: dict | None, cushion: dict | None, eot: dict | None,
                   accept: dict | None, speech_ms, last_spoken_ms: int | None, now_ms: int,
                   gap_ms: int = CUSHION_GAP_MS, text: str = "",
                   min_chars: int = CUSHION_MIN_CHARS,
                   max_chars: int = CUSHION_MAX_CHARS) -> tuple[bool, str]:
    """垫句的**唯一**判据：返回 (要不要出声, 理由)。调用方只管出声、不管规则。

    顺序：先兜底（判断没回来一律不出声），再拍板闸（积极 + 已满 1.5 秒 + 不要回答 + 频次 + 字数）。
    """
    pace = pace or {}
    cushion = cushion or {}
    if pace.get("state") in (None, "pending"):
        return False, "节奏还没回来：不问垫句"
    if pace.get("state") != "ok":
        return False, "节奏判断失败 / 超时：不问垫句（节奏保持上一拍）"
    tier = pace.get("pace")
    if tier != PACE_EAGER:
        return False, "节奏不是积极（%s）：不问垫句" % pace_label(tier)
    if speech_ms is None:
        return False, "这一版没有带「已连续说满多久」：不问垫句"
    if int(speech_ms) < CUSHION_MIN_SPEECH_MS:
        return False, "对方还没说满 %.1f 秒：不问垫句" % (CUSHION_MIN_SPEECH_MS / 1000.0)
    state = cushion.get("state")
    if state in (None, "pending"):
        return False, "垫句判断还没回来：不垫"
    if state != "ok":
        return False, cushion.get("reason") or "垫句判断失败 / 超时：不垫"
    if cushion.get("say") is not True:
        return False, "垫句判断说「不垫」"
    if not eot:
        return False, "判停还没回来：先不垫"
    if eot.get("label") in ("error", None) or eot.get("error"):
        return False, "判停失败：不垫"
    if eot.get("may_confirm") and eot.get("may_start_early"):
        return False, "判停说「说完了」（当刻要收住）：不出声"
    if accept is None or accept.get("state") in (None, "pending"):
        return False, "要不要回答还没回来：不垫"
    if accept.get("state") != "ok" or accept.get("accept") is None:
        return False, "要不要回答失败 / 超时：不垫"
    if accept.get("accept") is False:
        return False, "同一版字「不要回答」（不是对车说）：不垫句"
    hold = max(int(gap_ms), SPEAK_HOLD_MS)
    if last_spoken_ms is not None and now_ms - int(last_spoken_ms) < hold:
        return False, "离上一声不到 %.1f 秒" % (hold / 1000.0)
    chars = cushion_chars(text)
    if chars < min_chars or chars > max_chars:
        return False, "这一句 %d 个字，不在 %d–%d 字" % (chars, min_chars, max_chars)
    return True, "可以垫一句"


__all__ = ["PACE_NORMAL", "PACE_PATIENT", "PACE_NARRATIVE", "PACE_EAGER", "PACE_ORDER",
           "PACE_LABELS", "PACE_CRITERIA", "PACE_INSTRUCTIONS", "PACE_SCOPE",
           "PACE_HOLD_TURNS", "PACE_SWITCH_MARGIN", "CUSHION_INSTRUCTIONS", "CUSHION_CRITERIA",
           "CUSHION_ORDER", "CUSHION_WORDS", "CUSHION_MIN_CHARS", "CUSHION_MAX_CHARS",
           "CUSHION_MIN_SPEECH_MS", "CUSHION_STALE_MS", "SPEAK_HOLD_MS", "CUSHION_GAP_MS",
           "CUSHION_MIN_PROB", "CUSHION_MIN_CONF", "THRESHOLD_NOTE",
           "pace_label", "pace_question", "cushion_question", "pace_state", "cushion_chars",
           "cushion_text", "hold_pace", "score_pace", "score_cushion", "rule_pace", "rule_cushion",
           "pace_meta", "cushion_meta", "build_pace_fn", "build_cushion_fn", "decide_cushion",
           "SOURCE_MODEL", "TEMP_SOURCE"]
