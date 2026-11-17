"""Turn-taking (EOT) judging through the StartLux decision service.

Call shape is the one documented in `eot-judge-api` (飞书, 2026-11-03): one `choice` question
named `eot` with the three criteria complete / cut / wait, `think_mode: "never"`, and
`state = {"utterance": ..., "last_agent_reply": ...}`. The judge does not generate text; it
returns `choice`, `confidence` and the per-label `probabilities`, and the turn policy
(`EOT_COMPLETE_MIN_SCORE=0.80`, `EOT_CONFIDENCE_MIN=0.70` in the doc, `TurnConfig` here) decides
from those numbers instead of trusting the argmax alone.

This replaces the earlier three-Noul-question request. `label` is still the argmax of
`probabilities` (what the doc calls \"模型选的\"), and `margin = complete - max(cut, wait)` is kept
so the margin / argmax policies can replay pre-change configurations against the same scores.

No retries: the turn session's `eot_timeout_ms`, its error counters and the turn budget are the
failure policy (spec-20261015 §0: 出错就报错，不加重试容灾).
"""
from __future__ import annotations

import atexit
import json
import math
import os
import sys
import time
from urllib.parse import urlsplit

import httpx2

sys.dont_write_bytecode = True

# 文档给的是 8009（SSH 隧道到远程 GPU）；`EOT_JUDGE_BASE` 可覆盖，探针用 `EOT_JUDGE_PORT`。
EOT_JUDGE_BASE = os.environ.get("EOT_JUDGE_BASE", "http://127.0.0.1:8009")
EOT_JUDGE_PORT = int(urlsplit(EOT_JUDGE_BASE).port or 80)
EOT_JUDGE_PATH = "/v1/systemone"
EOT_JUDGE_TIMEOUT_S = float(os.environ.get("EOT_JUDGE_TIMEOUT_S", "5.0"))
EOT_ORDER = ("complete", "cut", "wait")

# 逐字照抄 eot-judge-api.html 的「判断标准」：模型是按这些 criteria 调的，文档四个样例
# （导航去天安门 / 导航到 / 嗯 / 好的）就是验收样例，不要改写。
EOT_INSTRUCTIONS = (
    "车载语音助手场景。判断用户这句话的状态。\n\n"
    "complete — 能独立执行或回应，不需要更多信息：\n"
    "完整指令、完整问句、明确回应（好的/不要/对/行/算了/随便）、"
    "控制指令（停/暂停/继续/取消）、反馈（谢谢/再见/再说一遍/换一个）。\n"
    "判断方法：这句话能不能直接交给AI处理？能 = complete。\n\n"
    "cut — 语法不完整，缺少必要成分：\n"
    "缺宾语（导航到___）、缺谓语（空调___）、连接处断开（然后___）。\n"
    "判断方法：这句话语法上完整吗？单独一个名词或动词通常不完整。\n\n"
    "wait — 用户在犹豫或回忆，内容含糊：\n"
    "犹豫声（嗯、啊、哦、哎）、填充词（那个那个、就是）、回忆中（叫什么来着）。\n"
    "判断方法：用户在想而不是在说。"
)
EOT_CRITERIA = {
    "complete": "说完了，立刻处理",
    "cut": "话断了，后面马上来",
    "wait": "在犹豫/思考",
}

CLIENT = httpx2.Client(timeout=EOT_JUDGE_TIMEOUT_S, trust_env=False, follow_redirects=False,
                       limits=httpx2.Limits(max_connections=4, max_keepalive_connections=4,
                                           keepalive_expiry=60.0))
atexit.register(CLIENT.close)


def request_body(text: str, last_reply: str = "") -> dict:
    """文档的请求体：一个 choice 问题、`think_mode: never`、不带 `model`。"""
    state = {"utterance": text}
    if last_reply:
        # 可选；文档自己的例子（「要不要导航回家？」之后回「好的」）就是它存在的理由：
        # 短回复要有上一轮助手的话才能判成 complete。
        state["last_agent_reply"] = last_reply
    return {
        "state": state,
        "think_mode": "never",
        "questions": {
            "eot": {
                "type": "choice",
                "instructions": EOT_INSTRUCTIONS,
                "criteria": dict(EOT_CRITERIA),
            }
        },
    }


def _unique_object(pairs):
    result = {}
    for name, value in pairs:
        if name in result:
            raise ValueError("Duplicate EOT judge key")
        result[name] = value
    return result


def _probability(value, name: str) -> float:
    if type(value) not in (int, float) or not math.isfinite(value) or not 0.0 <= value <= 1.0:
        raise ValueError("Invalid EOT judge " + name)
    return float(value)


def parse_response(raw: dict) -> dict:
    """校验文档化响应，摊平成回合状态机用的 verdict 字典。

    判定要用的字段（answer id、choice、confidence、三个概率）一律严格；只容忍文档里有、
    活服务额外加的字段（`type` / `none_probability` / `model` / `usage` / `latency_ms`）。
    """
    if not isinstance(raw, dict):
        raise ValueError("Malformed EOT judge response")
    answers = raw.get("answers")
    if not isinstance(answers, dict) or set(answers) != {"eot"}:
        raise ValueError("Malformed EOT judge answers")
    answer = answers["eot"]
    if not isinstance(answer, dict):
        raise ValueError("Malformed EOT judge answer")
    if set(answer) - {"type", "choice", "confidence", "probabilities", "none_probability"}:
        raise ValueError("Malformed EOT judge answer fields")
    if "type" in answer and answer["type"] != "choice":
        raise ValueError("Malformed EOT judge answer type")
    probabilities = answer.get("probabilities")
    if not isinstance(probabilities, dict) or set(probabilities) != set(EOT_ORDER):
        raise ValueError("Malformed EOT judge probabilities")
    scores = {name: _probability(probabilities[name], name) for name in EOT_ORDER}
    confidence = _probability(answer.get("confidence"), "confidence")
    choice = answer.get("choice")
    if choice not in EOT_ORDER:
        raise ValueError("Malformed EOT judge choice")
    if "none_probability" in answer:
        _probability(answer["none_probability"], "none_probability")
    return {
        "complete": scores["complete"],
        "cut": scores["cut"],
        "wait": scores["wait"],
        "label": max(EOT_ORDER, key=lambda name: scores[name]),
        "choice": choice,
        "confidence": confidence,
        "margin": scores["complete"] - max(scores["cut"], scores["wait"]),
        "model": raw.get("model"),
    }


def score_eot(text: str, last_reply: str = "", *, client=None, base: str | None = None) -> dict:
    """对 `text` 发一次判停调用；`label` = argmax，`margin` = complete - max(cut, wait)。"""
    client = client or CLIENT
    base = (base or EOT_JUDGE_BASE).rstrip("/")
    began = time.monotonic()
    response = client.post(base + EOT_JUDGE_PATH, json=request_body(text, last_reply))
    wall_ms = (time.monotonic() - began) * 1000
    if response.status_code != 200:
        raise ValueError("EOT judge returned non-200")
    raw = json.loads(response.content, object_pairs_hook=_unique_object)
    result = parse_response(raw)
    latency = raw.get("latency_ms")
    if latency is not None and (type(latency) not in (int, float) or not math.isfinite(latency)
                                or latency < 0):
        raise ValueError("Invalid EOT judge latency_ms")
    result.update(wall_ms=wall_ms, kev_ms=latency)
    return result


if __name__ == "__main__":
    text = sys.argv[1] if len(sys.argv) > 1 else "导航回家"
    reply = sys.argv[2] if len(sys.argv) > 2 else ""
    print(json.dumps(score_eot(text, reply), ensure_ascii=False))
