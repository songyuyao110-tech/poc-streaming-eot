"""中枢「两人同时说」四选：这边正在说 / 正在准备说，人又开口了，这一句插话怎么办。

期三（2026-11-16）新增的一道判断。依据验收卡 `poc-hub-jev-absorb-p3-20261116-验收.md` 与
方案 `poc-hub-jev-absorb-plan-20261116-DETAIL.md` §3.3 / §4 第 6 条 / §5.2 期三。

这道判断**不是每版出字都问**：只挂在「这边正在说 / 正在准备说，人又开口」的那一版上问一次
（`TurnSession` 拦到这一版才发；同一段插话的后续几个 partial 不再重复问）。

四条出口（穷举，全部是已拍板的读法，本模块不另出读法）：

| 出口 | 中枢做什么 | 本轮决策 | 插话那句 | 记住吗 | 开闸吗 |
|---|---|---|---|---|---|
| **让路** `yield` | 停下当前这句、回到收听 | **作废本轮**（现网那条，逐字段不变） | 留下（半截 + 新话承接，继续听这一席） | 本轮作废、话不丢 | 否 |
| **把这句说完再接** `finish_first` | 当前这句照旧说完（不作废） | 不作废 | **排队**：等这句说完，作为**下一轮**再回答（顺序不变、字不丢） | 是（作为下一轮的用户话） | 按下一轮的判据（不提前、不改变） |
| **继续说** `continue` | 不拿插话来回答，当前这句说完并继续往下说 | 不作废 | 只进**旁听上下文**（与「不是对车说」同一口径） | 进上下文，不写任务 | 否（不为插话开闸） |
| **垫句抢回** `cushion_back` | 当前这句说完，紧随一句垫句把话头抢回来 | 不作废 | 收下（排队，随后回答） | 是（垫句记住；插话作下一轮） | 否（垫句不开闸） |

失败与兜底（拍板 5）：**判断没回来 / 失败 / 超时 → 按「继续说」走**（不打断自己、不丢人的话），
不改收口（0.7 / 1.5 / 3 秒）、不改开闸线、不改开闸时机。

本模块只判「四条出口里的哪一条 / 兜底走哪条」，**不碰任何计时器、不碰闸门、不碰旁听台账**：
出不出声、把插话放哪里、什么时候放，全在 `TurnSession` 里（与期一应声、期二垫句同一分工）。

「这边正在说 / 正在准备说」这个状态本身：期三最小版落在 `TurnSession`（开闸之后正在准备说 /
正在回答，即现网「生成中人又开口 → 让路」的那个状态）。本模块只吃它传进来的 `hub_state`，
不自己定义它（见 DETAIL 里「关于垫句窗口的一处读法」）。
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

# ---- 四条出口 --------------------------------------------------------------------------------
FLOOR_YIELD = "yield"                      # 让路：停下当前这句、回到收听（现网那条）
FLOOR_FINISH_FIRST = "finish_first"        # 把这句说完再接：插话排队，等这句说完再作为下一轮回答
FLOOR_CONTINUE = "continue"                # 继续说：不拿插话回答；插话只进旁听上下文
FLOOR_CUSHION_BACK = "cushion_back"        # 垫句抢回：这句说完紧接一句垫句，插话收下随后回答
FLOOR_ORDER = (FLOOR_YIELD, FLOOR_FINISH_FIRST, FLOOR_CONTINUE, FLOOR_CUSHION_BACK)
FLOOR_LABELS = {FLOOR_YIELD: "让路",
                FLOOR_FINISH_FIRST: "把这句说完再接",
                FLOOR_CONTINUE: "继续说",
                FLOOR_CUSHION_BACK: "垫句抢回"}
FLOOR_ACTIONS = {
    FLOOR_YIELD: "停下当前这句、回到收听；本轮决策作废，插话留下、继续听这一席（与改动前同一条）",
    FLOOR_FINISH_FIRST: "当前这句照旧说完（不作废）；插话排队，等这句说完后作为下一轮回答",
    FLOOR_CONTINUE: "当前这句说完并继续往下说；插话只进旁听上下文（不是对车说），不进 Agent",
    FLOOR_CUSHION_BACK: "当前这句说完，紧接一句垫句抢回话头；插话收下，随后作为下一轮回答",
}

# 判断没回来 / 失败 / 超时一律走这一条（拍板 5：不打断自己、不丢人的话）。
FLOOR_SAFE = FLOOR_CONTINUE

FLOOR_INSTRUCTIONS = (
    "这边（语音助手）正在说、或正在准备说一句有内容的话，人又开口了——这一句插话怎么处理？"
    "结合人刚说的这句插话、这边正在说的那句，以及此刻已有的场景描述。"
    "让路：停下这边这一句、回到收听，这一轮的回答作废；人的话留下，继续听这一席。"
    "把这句说完再接：这边这一句照旧说完（不作废），人的话排队，等这句说完再作为下一轮回答。"
    "继续说：不拿人的话来回答，这边这一句说完并继续往下说；人的话只当旁听记下来，不回答、不执行。"
    "垫句抢回：这边这一句说完，紧接着垫一句小话把话头抢回来；人的话收下，随后回答。"
    "四种都不是「现在停下改回答人这句」；回答都要等到该回答的时机。"
)
FLOOR_CRITERIA = {
    FLOOR_YIELD: "让路（停下这边这句，回到收听；本轮回答作废，人的话留下）",
    FLOOR_FINISH_FIRST: "把这句说完再接（这边这句照旧说完，人的话排队，随后作为下一轮回答）",
    FLOOR_CONTINUE: "继续说（不拿人的话回答；这边这句说完并继续，人的话只进旁听）",
    FLOOR_CUSHION_BACK: "垫句抢回（这边这句说完，紧接一句小话抢回话头；人的话收下，随后回答）",
}

# 判断模型给的是「四条出口里的一条」，四条都是有效动作，**不设起判线**（卡面没给，
# 也不该像应声 / 垫句那样拿一条线把「有结论」当「没结论」）。只把判断失败/超时当没结论 → 继续说。
SOURCE_MODEL = "判断模型 8009（与「说没说完 / 要不要回答 / 应不应一声 / 节奏 / 垫一句」同一套）"
MODE_MODEL = "kev_choice(floor)"
TEMP_SOURCE = "离线替身（仅供离线演示）"
TEMP_MODEL = "rule/离线替身（未拍板）"
THRESHOLD_NOTE = "四选不设起判线（四条都是有效出口）；判断没回来 / 失败 / 超时一律按「继续说」走"

FLOOR_TIMEOUT_S = float(os.environ.get("HUB_FLOOR_TIMEOUT_S", "2.8"))
FLOOR_CLIENT = httpx2.Client(timeout=FLOOR_TIMEOUT_S, trust_env=False, follow_redirects=False,
                             limits=httpx2.Limits(max_connections=4, max_keepalive_connections=4,
                                                  keepalive_expiry=60.0))
atexit.register(FLOOR_CLIENT.close)


def _seats(seat: str) -> str:
    return {"D": "主驾", "P": "副驾"}.get(str(seat).upper(), seat)


def floor_label(choice) -> str:
    return FLOOR_LABELS.get(choice, "未知出口")


def floor_question() -> dict:
    """四选一（让路 / 把这句说完再接 / 继续说 / 垫句抢回）。"""
    return {"type": "choice", "instructions": FLOOR_INSTRUCTIONS,
            "criteria": dict(FLOOR_CRITERIA)}


def floor_state(text: str, scene=None, *, hub_state=None, hub_sentence=None) -> str:
    """动态输入：人的插话 + 这边正在说 / 正在准备说的那句 + 场景。

    与「要不要回答」同一套 state 形状（`utterance` / `scene`），多带两栏：
    `hub_state`（`preparing` / `saying`）与 `hub_sentence`（这边正在说的那句，可为空）。
    """
    scene_text = ""
    if isinstance(scene, dict):
        scene_text = str(scene.get("summary") or "").strip()
    body = {"insertion": text or "", "scene": scene_text}
    if hub_state:
        body["hub_state"] = str(hub_state)
    if hub_sentence:
        body["hub_sentence"] = str(hub_sentence)
    return json.dumps(body, ensure_ascii=False, allow_nan=False)


def _scene_block(scene) -> dict:
    if isinstance(scene, dict):
        return {"summary": scene.get("summary"), "revision": scene.get("revision"),
                "age_ms": scene.get("age_ms"), "missing": not bool(scene.get("summary"))}
    return {"summary": "", "revision": None, "age_ms": None, "missing": True}


def _floor_verdict(choice, probabilities, confidence, text, scene, seat, *, wall_ms,
                   kev_ms=None, hub_state=None, hub_sentence=None,
                   source=SOURCE_MODEL, model=KEV_MODEL, mode=MODE_MODEL) -> dict:
    probs = {name: round(float(probabilities.get(name) or 0.0), 3) for name in FLOOR_ORDER}
    top = float(probs.get(choice) or 0.0)
    return {
        "choice": choice,
        "outlet": choice,
        "label": floor_label(choice),
        "action": FLOOR_ACTIONS.get(choice),
        "probabilities": probs,
        "score": round(top, 3),
        "confidence": round(float(confidence), 3),
        "threshold_note": THRESHOLD_NOTE,
        "reason": "P(%s) %.2f → %s。" % (floor_label(choice), top, floor_label(choice)),
        "source": source,
        "model": model,
        "mode": mode,
        "seat": _seats(seat),
        "scene_used": _scene_block(scene),
        "hub_state": hub_state,
        "hub_sentence": hub_sentence,
        "wall_ms": round(wall_ms, 1),
        "kev_ms": kev_ms,
        "latency_ms": round(wall_ms, 1),
        "state": "ok",
    }


def _parse_payload(raw: dict) -> dict:
    """严格校验四选一响应（只有文档里的字段；多出来的活服务字段也容忍）。"""
    if not isinstance(raw, dict):
        raise ValueError("Malformed floor judge response")
    answers = raw.get("answers")
    if not isinstance(answers, dict) or set(answers) != {"floor"}:
        raise ValueError("Malformed floor judge answers")
    answer = answers["floor"]
    if not isinstance(answer, dict):
        raise ValueError("Malformed floor judge answer")
    if set(answer) - {"type", "choice", "confidence", "probabilities", "none_probability"}:
        raise ValueError("Malformed floor judge answer fields")
    if "type" in answer and answer["type"] != "choice":
        raise ValueError("Malformed floor judge answer type")
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict) or set(probabilities) != set(FLOOR_ORDER):
        raise ValueError("Malformed floor judge probabilities")
    scores = {name: probability(probabilities[name]) for name in FLOOR_ORDER}
    confidence = probability(answer.get("confidence"))
    choice = answer.get("choice")
    if choice not in FLOOR_ORDER:
        raise ValueError("Malformed floor judge choice")
    if "none_probability" in answer:
        probability(answer["none_probability"])
    return {"probabilities": scores, "choice": choice, "confidence": confidence}


# ---- 真判断（同一台 8009） --------------------------------------------------------------------
def score_floor(text: str, scene=None, seat: str = "D", *, client=None, base=None,
                hub_state=None, hub_sentence=None) -> dict:
    """真判断：问「这一句插话怎么处理」。失败直接抛出（调用方按「继续说」兜底）。"""
    client = client or FLOOR_CLIENT
    base = (base or KEV_EOT_BASE).rstrip("/")
    questions = {"floor": floor_question()}
    began = time.monotonic()
    raw = kev_call(client, floor_state(text, scene, hub_state=hub_state,
                                      hub_sentence=hub_sentence), questions, base=base)
    wall_ms = (time.monotonic() - began) * 1000
    parsed = _parse_payload(raw)
    if not math.isfinite(parsed["confidence"]):
        raise ValueError("Invalid floor confidence")
    return _floor_verdict(parsed["choice"], parsed["probabilities"], parsed["confidence"],
                          text, scene, seat, wall_ms=wall_ms, kev_ms=raw.get("latency_ms"),
                          hub_state=hub_state, hub_sentence=hub_sentence)


# ---- 离线替身（只在显式 HUB_FLOOR=rule 时用；不冒充真判断） ------------------------------------
def _rule_floor_value() -> str:
    value = (os.environ.get("HUB_FLOOR_RULE", FLOOR_YIELD) or FLOOR_YIELD).strip().lower()
    return value if value in FLOOR_ORDER else FLOOR_YIELD


def rule_floor(text: str, scene=None, seat: str = "D", *, hub_state=None,
               hub_sentence=None) -> dict:
    """离线替身：**一律判 `HUB_FLOOR_RULE` 那一条**（默认让路＝改动前那条）。

    它只是让离线回放能验「四条出口各跑一条、同一段稿只换选择」：出口用环境变量点，
    真实行为（作废 / 排队 / 只进旁听 / 垫句）仍由 `TurnSession` 按四条出口的字面落实。
    """
    began = time.monotonic()
    choice = _rule_floor_value()
    probabilities = {name: (0.82 if name == choice else round(0.18 / 3, 3)) for name in FLOOR_ORDER}
    return _floor_verdict(choice, probabilities, 0.9, text, scene, seat,
                          wall_ms=(time.monotonic() - began) * 1000,
                          hub_state=hub_state, hub_sentence=hub_sentence,
                          source=TEMP_SOURCE, model=TEMP_MODEL, mode="rule")


def floor_meta() -> dict:
    return {"source": SOURCE_MODEL, "model": KEV_MODEL, "mode": MODE_MODEL,
            "base": KEV_EOT_BASE, "question": FLOOR_INSTRUCTIONS,
            "outlets": [{"key": name, "label": FLOOR_LABELS[name], "action": FLOOR_ACTIONS[name]}
                        for name in FLOOR_ORDER],
            "safe_outlet": FLOOR_SAFE, "threshold_note": THRESHOLD_NOTE, "decided": True,
            "note": "四条出口与失败兜底已拍板（2026-11-16）；只判«哪一条»，不碰计时 / 开闸 / 台账"}


def build_floor_fn(mode: str | None = None):
    """构造 `GateKeeper(floor_fn=...)` 用的判断函数（与 `build_ack_fn` 同一风格）。"""
    choice = (mode or os.environ.get("HUB_FLOOR", "model")).strip().lower() or "model"
    if choice in ("model", "kev", "real", "judge"):
        def _fn(text, scene=None, seat="D", hub_state=None, hub_sentence=None):
            return score_floor(text, scene=scene, seat=seat, hub_state=hub_state,
                               hub_sentence=hub_sentence)
        _fn.meta = floor_meta()
        return _fn
    if choice in ("rule", "temp", "offline"):
        def _fn(text, scene=None, seat="D", hub_state=None, hub_sentence=None):
            return rule_floor(text, scene=scene, seat=seat, hub_state=hub_state,
                              hub_sentence=hub_sentence)
        _fn.meta = {"source": TEMP_SOURCE, "model": TEMP_MODEL, "mode": "rule",
                    "outlets": [{"key": name, "label": FLOOR_LABELS[name],
                                 "action": FLOOR_ACTIONS[name]} for name in FLOOR_ORDER],
                    "safe_outlet": FLOOR_SAFE, "threshold_note": THRESHOLD_NOTE, "decided": True,
                    "note": "离线替身：一律判 HUB_FLOOR_RULE 那一条（默认让路＝改动前那条）"}
        return _fn
    raise ValueError("HUB_FLOOR=" + choice + " 未实现：可选 model（真判断，默认）/ rule（离线替身）")


# ---- 唯一判据 --------------------------------------------------------------------------------
def decide_floor(*, floor: dict | None) -> tuple[str, str]:
    """四选的**唯一**判据：返回 (走哪条出口, 理由)。调用方只管按出口做事、不管规则。

    顺序：先兜底（没结论一律「继续说」——拍板 5：不打断自己、不丢人的话），再看判断给的是哪条。
    四条都是有效出口，所以这里不做「概率够不够」的二次判断（卡面没给这条线，也不该发明）。
    """
    floor = floor or {}
    if floor.get("state") in (None, "pending"):
        return FLOOR_SAFE, "四选判断还没回来 → 按「继续说」走（不打断自己、不丢人的话）"
    if floor.get("state") != "ok":
        return FLOOR_SAFE, "四选判断失败 / 超时 → 按「继续说」走（不打断自己、不丢人的话）"
    choice = floor.get("outlet") or floor.get("choice")
    if choice not in FLOOR_ORDER:
        return FLOOR_SAFE, "四选判断没给出合法出口 → 按「继续说」走（不打断自己、不丢人的话）"
    return choice, "判断说「%s」" % floor_label(choice)


__all__ = ["FLOOR_YIELD", "FLOOR_FINISH_FIRST", "FLOOR_CONTINUE", "FLOOR_CUSHION_BACK",
           "FLOOR_ORDER", "FLOOR_LABELS", "FLOOR_ACTIONS", "FLOOR_SAFE", "FLOOR_INSTRUCTIONS",
           "FLOOR_CRITERIA", "FLOOR_TIMEOUT_S", "THRESHOLD_NOTE", "floor_question", "floor_state",
           "floor_label", "score_floor", "rule_floor", "floor_meta", "build_floor_fn",
           "decide_floor", "SOURCE_MODEL", "TEMP_SOURCE", "MODE_MODEL"]
