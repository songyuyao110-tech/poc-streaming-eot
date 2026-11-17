"""写记忆前的快判断：这段内容有没有必要写进记忆（是非题，另准备的一份短稿）。

口径来源：`tmp/poc-neixinxi-memory-write-验收.md`（用户 2026-11-12 10:29–10:31 拍板）：

- 用**现网判停同一套模型 / 同一个 8009 服务**（`gate.turn_eot` 的 `EOT_JUDGE_BASE` /
  `/v1/systemone`，`think_mode: "never"`、请求不带 `model`），**不沿用读心那个较慢的模型**；
- 只做是非（写 / 不写），关思考、短输出；判断失败 / 超时 / 分不清 → **宁可不写**（抛错让调用方跳过）；
- 稿里必须约束「应该写 / 不应该写」两类（逐字照验收第 6 条，不自行改写）；
- 离线替身（页面 `?eot=` 不是 `real` 时）：本地规则可测，覆盖「太短或纯应声不写」「能并成完整且有
  有效信息的才写」，不连 8009（`rule_memory_write`，明确标 `source=rules_offline`，不冒充真模型）。

请求形状与判停同一套 choice 问题（只是问题名与 criteria 换成「写 / 不写」）：

    {"state": {"content": ..., "seat": ..., "scene": ..., "last_memory": ...},
     "think_mode": "never",
     "questions": {"memory_write": {"type": "choice", "instructions": ..., "criteria": {...}}}}

本模块只产一个判断结论，不写记忆、不碰读心 / 判停收口。凭据与端点沿用既有配置（不落盘、不打印）。
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

from gate.turn_eot import EOT_JUDGE_BASE  # noqa: E402

# 写前快判断的问题名（响应 `answers` 的键；判停是 `eot`，这里单独一个，别混）。
QUESTION = "memory_write"
# 与判停同一套口径：关思考、短输出、不带 `model`（服务端只有一个模型）。
MEMORY_WRITE_TIMEOUT_S = float(os.environ.get("MEMORY_WRITE_TIMEOUT_S", "2.0"))
# 「更可能有必要才写」的临时线（不是产品已拍的正式线；真服务校准前先按它，宁可不写也不写错）。
MEMORY_WRITE_MIN_SCORE = float(os.environ.get("MEMORY_WRITE_MIN_SCORE", "0.65"))
THRESHOLD_NOTE = "写前快判断线未拍，本次临时用 %g（宁可不写，不写错）" % MEMORY_WRITE_MIN_SCORE

SOURCE_MODEL = "判停同一套模型 8009（写前快判断，另准备的一份短稿）"
MODE_MODEL = "kev_choice"
TEMP_SOURCE = "离线替身（本地规则，仅供非麦验收）"
TEMP_MODEL = "rule/离线替身"

# 逐字照验收第 6 条的「应该写 / 不应该写」约束；不要自行加戏。
MEMORY_WRITE_INSTRUCTIONS = (
    "车载对话记忆的写前判断：这段内容有没有必要写进长期记忆？只回答写还是不写，不要补充内容。\n\n"
    "应该写：人名对上了哪一席；明确的事实或约定；当场纠正了谁是谁；有新的有效信息。\n"
    "不应该写：单字或半句不成意思；和上一刀重复；没有新信息；分不清是谁说的；"
    "只有「哦」「嗯」这类应声。"
)
MEMORY_WRITE_CRITERIA = {"write": "有必要写进记忆", "skip": "没有必要写"}

CLIENT = httpx2.Client(timeout=MEMORY_WRITE_TIMEOUT_S, trust_env=False, follow_redirects=False,
                       limits=httpx2.Limits(max_connections=4, max_keepalive_connections=4,
                                             keepalive_expiry=60.0))
atexit.register(CLIENT.close)


def memory_write_question() -> dict:
    """这一次的快判断问题（choice：写 / 不写）。"""
    return {"type": "choice", "instructions": MEMORY_WRITE_INSTRUCTIONS,
            "criteria": dict(MEMORY_WRITE_CRITERIA)}


def memory_write_state(content: str, *, seat: str = "D", scene=None, prev: str = "") -> dict:
    """动态输入：攒好并完的这一段 + 这一刻读心 + 上一刀写过的内容（判「和上一刀重复」用）。"""
    scene_text = ""
    if isinstance(scene, dict):
        scene_text = str(scene.get("summary") or "").strip()
    seat_label = {"D": "主驾", "P": "副驾"}.get(str(seat).upper(), str(seat))
    return {"content": (content or "").strip(), "seat": seat_label,
            "scene": scene_text, "last_memory": (prev or "").strip()}


def request_body(content: str, *, seat: str = "D", scene=None, prev: str = "") -> dict:
    """判停同形状的请求体（`think_mode: never`、不带 `model`、一个 choice 问题）。"""
    return {"state": memory_write_state(content, seat=seat, scene=scene, prev=prev),
            "think_mode": "never",
            "questions": {QUESTION: memory_write_question()}}


def _probability(value, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError("Invalid memory write judge " + name)
    return float(value)


def parse_response(raw: dict) -> dict:
    """严格校验「写 / 不写」的 choice 响应（多余字段只容忍文档里那几个）。"""
    if not isinstance(raw, dict):
        raise ValueError("Malformed memory write judge response")
    answers = raw.get("answers")
    if not isinstance(answers, dict) or set(answers) != {QUESTION}:
        raise ValueError("Malformed memory write judge answers")
    answer = answers[QUESTION]
    if not isinstance(answer, dict):
        raise ValueError("Malformed memory write judge answer")
    if set(answer) - {"type", "choice", "confidence", "probabilities", "none_probability"}:
        raise ValueError("Malformed memory write judge answer fields")
    if "type" in answer and answer["type"] != "choice":
        raise ValueError("Malformed memory write judge answer type")
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict) or set(probabilities) != set(MEMORY_WRITE_CRITERIA):
        raise ValueError("Malformed memory write judge probabilities")
    scores = {name: _probability(probabilities[name], name) for name in MEMORY_WRITE_CRITERIA}
    confidence = _probability(answer.get("confidence"), "confidence")
    choice = answer.get("choice")
    if choice not in MEMORY_WRITE_CRITERIA:
        raise ValueError("Malformed memory write judge choice")
    if "none_probability" in answer:
        _probability(answer["none_probability"], "none_probability")
    return {"write_score": scores["write"], "skip_score": scores["skip"],
            "choice": choice, "confidence": confidence, "model": raw.get("model")}


def judge_memory_write(content: str, *, seat: str = "D", scene=None, prev: str = "",
                       client=None, base: str | None = None,
                       threshold: float | None = None) -> dict:
    """真模型快判断：同一台 8009、同一个模型，只问「这段内容有没有必要写进记忆」。

    失败（HTTP / 超时 / 解析）直接抛出——调用方按「宁可不写」处理，**不回落成本地规则冒充判断**。
    """
    threshold = MEMORY_WRITE_MIN_SCORE if threshold is None else float(threshold)
    body = (content or "").strip()
    if not body:
        return _verdict(False, 0.0, "empty_content", seat, 0.0, threshold,
                        source=SOURCE_MODEL)
    client = client or CLIENT
    base = (base or EOT_JUDGE_BASE).rstrip("/")
    began = time.monotonic()
    response = client.post(base + "/v1/systemone",
                           json=request_body(body, seat=seat, scene=scene, prev=prev))
    wall_ms = (time.monotonic() - began) * 1000
    if response.status_code != 200:
        raise ValueError("Memory write judge returned non-200")
    raw = json.loads(response.content)
    parsed = parse_response(raw)
    score = parsed["write_score"]
    # 只认「模型选的也是写」且分数过线：两个条件都满足才写（宁可不写，不写错）。
    write = parsed["choice"] == "write" and score >= threshold
    reason = ("模型选「%s」，写分 %.2f %s 临时线 %g → %s。"
              % (MEMORY_WRITE_CRITERIA[parsed["choice"]], score,
                 "≥" if score >= threshold else "<", threshold,
                 "写一刀" if write else "不写"))
    result = _verdict(write, score, reason, seat, wall_ms, threshold, source=SOURCE_MODEL)
    result.update(confidence=parsed["confidence"], choice=parsed["choice"],
                  skip_score=parsed["skip_score"], model=parsed["model"],
                  latency_ms=raw.get("latency_ms"))
    return result


def _verdict(write: bool, score: float, reason: str, seat: str, wall_ms: float,
             threshold: float, *, source: str) -> dict:
    return {"write": bool(write), "score": round(float(score), 3), "threshold": threshold,
            "threshold_note": THRESHOLD_NOTE, "reason": reason, "source": source,
            "mode": MODE_MODEL, "seat": {"D": "主驾", "P": "副驾"}.get(str(seat).upper(), seat),
            "wall_ms": round(float(wall_ms), 1), "latency_ms": round(float(wall_ms), 1),
            "error": None}


# ---- 离线替身（页面 `?eot=` 不是 real 时用；不连 8009，明确标「本地规则」） ----------------
# 各条都是**临时本地规则**，只保证非麦验收可复现，不冒充真模型质量。
OFFLINE_MIN_CHARS = 6      # 短于这个只当碎片（单字 / 半句）
_ACK_RE = None


def _ack_regex():
    global _ACK_RE
    if _ACK_RE is None:
        import re
        _ACK_RE = re.compile(
            r"^(?:好的|好|行|对|是|嗯|哦|啊|哎|呀|哈|知道了|明白了|懂了|收到|谢谢|不客气|"
            r"没事|没关系|ok|OK|行吧|算了|随便|再见|回头见)[，,。.!！?？、… ]*$")
    return _ACK_RE


def _normalized(text: str) -> str:
    from shared.text import normalize
    return normalize(text)


def rule_memory_write(content: str, *, seat: str = "D", scene=None, prev: str = "",
                      threshold: float | None = None) -> dict:
    """离线本地规则（非麦验收用）：太短 / 纯应声 / 和上一刀重复 → 不写；并成完整且有信息 → 写。

    判断只看「并完的这一段」本身 + 上一刀写过什么；`scene` 只回执里带上，不参与规则。
    """
    began = time.monotonic()
    norm = _normalized(content)
    prev_norm = _normalized(prev)
    threshold = MEMORY_WRITE_MIN_SCORE if threshold is None else float(threshold)

    def skip(reason: str, score: float = 0.0) -> dict:
        return _verdict(False, score, "离线本地规则：%s → 不写。" % reason, seat,
                        (time.monotonic() - began) * 1000, threshold, source=TEMP_SOURCE)

    if not norm:
        return skip("空内容")
    if prev_norm and (norm == prev_norm or norm.startswith(prev_norm)
                      or prev_norm.startswith(norm) or norm in prev_norm or prev_norm in norm):
        return skip("和上一刀重复 / 没有新信息")
    if _ack_regex().match(norm):
        return skip("只有「哦」「嗯」这类应声")
    if len(norm) < OFFLINE_MIN_CHARS:
        return skip("单字或半句不成意思（%d 字 < %d）" % (len(norm), OFFLINE_MIN_CHARS))
    result = _verdict(True, 0.9, "离线本地规则：并成了完整的一段、有新的有效信息 → 写一刀。",
                      seat, (time.monotonic() - began) * 1000, threshold, source=TEMP_SOURCE)
    result.update(mode="rules_offline", threshold_note=THRESHOLD_NOTE)
    return result


if __name__ == "__main__":
    text = sys.argv[1] if len(sys.argv) > 1 else "张先生，我给您介绍一下。"
    print(json.dumps(judge_memory_write(text), ensure_ascii=False))
