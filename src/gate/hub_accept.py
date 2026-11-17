"""中枢「接不接」：与「说没说完」同一套判断模型、同一个服务（8009 `/v1/systemone`）。

口径来源：`tmp/poc-mind-accept-judge-原料.md` / `-验收.md`（2026-11-10 16:54 用户拍板）。
一句话：

- 「接不接」不再靠关键词猜，它是一道**是非判断**：问模型「用户这句话是不是在对车说」，
  输出 **0 到 1 的一个分数**（`noul` 题型，与二次确认 / 任务相关性同一类）。
- 和「说没说完」（`turn_eot.score_eot`，同一台 8009、`think_mode: never`、请求不带 `model`）
  **用同一套判断模型、同一个服务**；两路在同一版出字上并发发起（并发/开闸在 `hub_gate.py`）。
- 判的时候动态带上：**当前这一版出字** + **读心此刻已有的那句场景**（取一次，不等待）。
  没有场景也照常判（`scene=""`），不得因此卡住。
- **开闸线用户还没拍**：本卡不代拍，只用一个显式临时起始值 `HUB_ACCEPT_MIN_SCORE=0.5`
  （分数 ≥ 0.5 才算要接，与任务相关性那条「更可能才认」的临时线对齐），**可环境变量覆盖**。
  界面必须写「开闸线未拍，本次临时用 0.5」，不得写成已拍板。

判断问题的底稿（本卡按原文落地，不改写）：

    「用户这句话是不是在对车说？结合这句话，以及此刻已有的场景描述。
     对车说：叫车机、下指令、问车、接上轮车的话。
     不对车说：和旁边人聊天、自言自语、明显不是对车说。」

失败口径：HTTP / 超时 / 解析失败一律**抛错**，由 `hub_gate.GateKeeper` 记成
`state=failed|timeout`，闸门结论是**先不开闸（不当成「不接」）**、界面能看见是超时/失败。
绝不把远程失败悄悄降级成本机关键词规则。

离线替身：`rule_accept()` 只保留给**离线测试 / 无服务演示**（显式 `HUB_ACCEPT=rule`），
来源明确写「离线替身（仅供离线演示）」，不冒充真判断，也不作为 8765 主链的默认路径。

配置（凭据不落盘、不打印）：
- `HUB_ACCEPT_MIN_SCORE` 默认 `0.5`（临时起始线；未拍板）
- `HUB_ACCEPT_TIMEOUT_S` 默认 `2.8`（单次调用的 HTTP 读超时；闸门侧超时预算见
  `HUB_ACCEPT_TIMEOUT_MS`，默认 3000，按真模型实测留余量）
- `KEV_EOT_BASE` / `KEV_MODEL` 与判停、二次确认、相关性共用同一既有配置
  （`core_pipeline_run.KEV_EOT_BASE` / `KEV_MODEL`）
"""
from __future__ import annotations

import atexit
import json
import os
import re
import sys
import time
from pathlib import Path

import httpx2

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.dont_write_bytecode = True

from core_pipeline_run import KEV_EOT_BASE, KEV_MODEL, kev_call, probability  # noqa: E402
# 线索词表只给离线替身用（真判断不看关键词；读心也不再需要它判接不接）。
from shared.lexicon import CAR_CUES, CHAT_CUES, NEGATION_CUES  # noqa: E402
# 判断问题（底稿，逐字落地；不要自行改写）。
ACCEPT_INSTRUCTIONS = (
    "用户这句话是不是在对车说？结合这句话，以及此刻已有的场景描述。"
    "对车说：叫车机、下指令、问车、接上轮车的话。"
    "不对车说：和旁边人聊天、自言自语、明显不是对车说。"
)

# 开闸线未拍：本卡只落显式临时起始值，可环境变量覆盖；界面照实标「未拍板」。
ACCEPT_MIN_SCORE = float(os.environ.get("HUB_ACCEPT_MIN_SCORE", "0.5"))
THRESHOLD_NOTE = "开闸线未拍，本次临时用 %s" % ("%g" % ACCEPT_MIN_SCORE)

SOURCE_MODEL = "判断模型 8009（与「说没说完」同一套）"
MODE_MODEL = "kev_noul"
TEMP_SOURCE = "离线替身（仅供离线演示）"
TEMP_MODEL = "rule/离线替身（未拍板）"
TEMP_NOTE = "离线替身只在 HUB_ACCEPT=rule 时使用；8765 主链走 8009 真判断"

ACCEPT_TIMEOUT_S = float(os.environ.get("HUB_ACCEPT_TIMEOUT_S", "2.8"))
ACCEPT_CLIENT = httpx2.Client(timeout=ACCEPT_TIMEOUT_S, trust_env=False, follow_redirects=False,
                              limits=httpx2.Limits(max_connections=6, max_keepalive_connections=6,
                                                   keepalive_expiry=60.0))
atexit.register(ACCEPT_CLIENT.close)


def _seats(seat: str) -> str:
    return {"D": "主驾", "P": "副驾"}.get(str(seat).upper(), seat)


def accept_question() -> dict:
    """本次「接不接」这一问（noul：0..1 的是非分数，不是三选一 / 二选一）。"""
    return {"type": "noul", "instructions": ACCEPT_INSTRUCTIONS}


def accept_state(text: str, scene=None) -> str:
    """动态输入：当前这一版出字 + 读心此刻已有的**场景描述**（没有场景就留空，照常判）。

    只送「谁在说什么」那半句（`scene["desc"]`），**不送**读心对「更像朝谁说」的推断
    （`summary` 里那半句「像在跟旁边人聊天 / 像在对车说」）：那是同一个问题的另一个答案，
    送进来会让判断变成复述它自己。2026-11-17 实测「今天的天气怎么样」：带推断 0.26（不开闸），
    只带描述 0.86（开闸）——这是输入带了自己答案的错，不是开闸线低。
    """
    scene_text = ""
    if isinstance(scene, dict):
        scene_text = str(scene.get("desc") or "").strip()
        if not scene_text:
            # 没有描述（真模型读心的旁白本来就是描述、不含推断）才退回 summary。
            scene_text = str(scene.get("summary") or "").strip()
    return json.dumps({"utterance": text or "", "scene": scene_text},
                      ensure_ascii=False, allow_nan=False)


def _verdict(score: float, text: str, scene, seat: str, *, wall_ms: float,
             kev_ms=None, threshold: float | None = None) -> dict:
    threshold = ACCEPT_MIN_SCORE if threshold is None else float(threshold)
    accept = score >= threshold
    scene_block = {"summary": "", "revision": None, "age_ms": None, "missing": True}
    if isinstance(scene, dict):
        scene_block = {"summary": scene.get("summary"), "revision": scene.get("revision"),
                       "age_ms": scene.get("age_ms"), "missing": not bool(scene.get("summary"))}
    reason = ("分数 %.2f %s 临时线 %g → %s。" %
              (score, "≥" if accept else "<", threshold, "要回答" if accept else "不回答（只进上下文）"))
    return {
        "accept": accept,
        "score": round(score, 3),
        # confidence 沿用既有字段（页面读它显示百分比）；它就是那一个 0..1 的分数。
        "confidence": round(score, 3),
        "threshold": threshold,
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
    }


def score_accept(text: str, scene=None, seat: str = "D", *, client=None, base=None,
                 threshold=None, timeout_s=None) -> dict:
    """真判断：同一台 8009、同一个模型，问「这句话是不是在对车说」，返回 0..1 分。

    失败（HTTP / 超时 / 解析）直接抛出，不回落关键词规则；调用方（闸门）记成
    「先不开闸 · 超时/失败」，不当成「不接」。
    """
    threshold = ACCEPT_MIN_SCORE if threshold is None else float(threshold)
    client = client or ACCEPT_CLIENT
    base = (base or KEV_EOT_BASE).rstrip("/")
    questions = {"accept": accept_question()}
    began = time.monotonic()
    raw = kev_call(client, accept_state(text, scene), questions, base=base)
    wall_ms = (time.monotonic() - began) * 1000
    answer = raw["answers"]["accept"]
    if not isinstance(answer, dict) or answer.get("type", "noul") != "noul":
        raise ValueError("Malformed accept Noul answer")
    score = probability(answer.get("noul"))
    return _verdict(score, text, scene, seat, wall_ms=wall_ms, kev_ms=raw.get("latency_ms"),
                    threshold=threshold)


# ---- 离线替身（只在显式 HUB_ACCEPT=rule 时用；不冒充真判断） -------------------------------
NEED_FLOOR = 0.8          # 离线替身的内部规则线（不是产品开闸线）
NEED_MARGIN = 0.2


def rule_accept(text: str, scene: dict | None = None, seat: str = "D") -> dict:
    """离线替身：车控线索命中且明显领先对人线索 → 要接；否则不接（只进上下文）。

    `scene` 只作展示/理由补强，**不改变规则判据**。它**不是**产品口径的最终判法，
    只在 `HUB_ACCEPT=rule`（离线测试 / 无服务演示）时使用。
    """
    began = time.monotonic()
    original = text or ""
    body = original.strip()
    if not body:
        return {"accept": False, "score": 0.2, "confidence": 0.2,
                "threshold": ACCEPT_MIN_SCORE, "threshold_note": THRESHOLD_NOTE,
                "reason": "没有出字，不构成一轮请求。",
                "source": TEMP_SOURCE, "model": TEMP_MODEL, "mode": "rule", "cues": [],
                "scene_used": {}, "latency_ms": round((time.monotonic() - began) * 1000, 1)}

    car_hits: list[tuple[float, str, str, str]] = []
    for pattern, weight, intent, obj in CAR_CUES:
        hit = re.search(pattern, body)
        if hit:
            car_hits.append((weight, intent, obj, hit.group(0)))
    chat_hits: list[tuple[float, str]] = []
    for pattern, weight in CHAT_CUES:
        hit = re.search(pattern, body)
        if hit:
            chat_hits.append((weight, hit.group(0)))

    car_score = sum(item[0] for item in car_hits)
    chat_score = sum(item[0] for item in chat_hits)
    negated = re.search(NEGATION_CUES, body) is not None
    if negated:
        car_score = 0.0

    accept = bool(car_score >= NEED_FLOOR and car_score >= chat_score + NEED_MARGIN)
    margin = car_score - chat_score
    score = max(0.30, min(0.97, 0.55 + abs(margin) * 0.18 + (0.10 if accept else 0.0)))

    if negated:
        reason = "句子里明确说不是对车说 → 不回答。"
    elif accept:
        cue = "、".join(sorted({item[3] for item in car_hits}))[:24]
        reason = f"出现车控线索「{cue}」，像是在对车机下单 → 要回答（离线替身）。"
    elif car_score > 0 and chat_score > 0:
        reason = "同时有车控词和聊天线索，更像在和人讨论 → 不回答。"
    elif chat_score > 0:
        reason = "像是在和同车乘客聊天 → 不回答，文本只进上下文。"
    elif car_score > 0:
        reason = "有车控词但不成完整指令 → 不回答。"
    else:
        reason = "没听出车控诉求（自语 / 环境话）→ 不回答，文本只进上下文。"

    cues = sorted({item[3] for item in car_hits} | {item[1] for item in chat_hits})
    scene_text = ""
    if isinstance(scene, dict) and scene.get("summary"):
        scene_text = str(scene["summary"])
        reason = reason + "（场景：" + scene_text + "）"
    return {
        "accept": accept,
        "score": round(score, 3),
        "confidence": round(score, 3),
        "threshold": ACCEPT_MIN_SCORE,
        "threshold_note": THRESHOLD_NOTE,
        "reason": reason,
        "source": TEMP_SOURCE,
        "model": TEMP_MODEL,
        "mode": "rule",
        "cues": cues,
        "seat": _seats(seat),
        "scene_used": {"summary": scene_text, "missing": not bool(scene_text)},
        "latency_ms": round((time.monotonic() - began) * 1000, 1),
    }


def accept_meta() -> dict:
    """界面上要写明的「本次实际用了什么」：真判断模型 + 临时开闸线（未拍板）。"""
    return {"source": SOURCE_MODEL,
            "model": KEV_MODEL,
            "mode": MODE_MODEL,
            "base": KEV_EOT_BASE,
            "question": ACCEPT_INSTRUCTIONS,
            "threshold": ACCEPT_MIN_SCORE,
            "threshold_note": THRESHOLD_NOTE,
            "decided": False,
            "note": "接不接与「说没说完」同一套判断模型、同一个 8009 服务；开闸线未拍，本次临时用 %g"
                    % ACCEPT_MIN_SCORE}


def build_accept_fn(mode: str | None = None):
    """构造 `GateKeeper(accept_fn=...)` 用的判断函数。

    `model`（默认，8765 主链）：8009 真判断（与判停同一套模型/服务）。
    `rule`：离线替身，只给离线测试 / 无服务演示（来源照实写，不冒充真判断）。
    """
    choice = (mode or os.environ.get("HUB_ACCEPT", "model")).strip().lower() or "model"
    if choice in ("model", "kev", "real", "judge"):
        def _fn(text, scene=None, seat="D"):
            return score_accept(text, scene=scene, seat=seat)
        _fn.meta = accept_meta()
        return _fn
    if choice in ("rule", "temp", "offline"):
        def _fn(text, scene=None, seat="D"):
            return rule_accept(text, scene=scene, seat=seat)
        _fn.meta = {"source": TEMP_SOURCE, "model": TEMP_MODEL, "mode": "rule",
                    "threshold": ACCEPT_MIN_SCORE, "threshold_note": THRESHOLD_NOTE,
                    "decided": False, "note": TEMP_NOTE}
        return _fn
    raise ValueError("HUB_ACCEPT=" + choice + " 未实现：可选 model（真判断，默认）/ rule（离线替身）")


__all__ = ["score_accept", "rule_accept", "build_accept_fn", "accept_meta", "accept_question",
           "accept_state", "ACCEPT_INSTRUCTIONS", "ACCEPT_MIN_SCORE", "THRESHOLD_NOTE",
           "TEMP_SOURCE", "TEMP_MODEL", "TEMP_NOTE", "NEED_FLOOR", "NEED_MARGIN"]
