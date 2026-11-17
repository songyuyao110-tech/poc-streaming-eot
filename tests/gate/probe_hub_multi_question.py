"""期一技术前置：「两问」探针 —— 同一版出字上，「说没说完 / 要不要回答」与「要不要应一声」
能不能**一次请求问完**（调用次数不增），还是必须退化成分开调用（调用次数 +1）。

口径：`tmp/poc-hub-jev-absorb-p1-20261116-验收.md` §技术前置 与
`poc-hub-jev-absorb-plan-20261116-SUMMARY.md` 两个坑之一（现网请求只带一个问题，「多问」能不能
被同一个服务稳定支持还没验过）。

探什么（四组，同一份 state）：

    A. 两问（都是 noul）：{"accept": noul, "ack": noul}
       —— 已知形状（选工具一路一次带 17 个 noul），作对照组。
    B. 两问（混类型）：{"accept": noul, "ack": choice}
    C. 三问（混类型）：{"eot": choice, "accept": noul, "ack": choice}
       —— 能过 = 一次请求把这三道判断问完（调用次数可从 2 路降到 1 路/版）。
    D. 五问（期二横断面）：{"eot": choice, "accept": noul, "ack": choice,
       "pace": choice, "cushion": choice}
       —— 能过 = 期二那 4 路（积极 + 已说满 1.5 秒那一版是 5 路）也能并成 1 路/版。

判读：

- C 通过 → 「一次请求带多问」可用；本期实现仍走 3 路（判停 / 要不要回答 / 应不应一声各自发），
  合并留给后续期（合并会改关键路径）。
- C 不过、A 过 → 混类型（choice + noul）是这个服务的边界；期一按退化路径落：
  同一版出字上**多发一次调用**（2 路 → 3 路/版），已在 SUMMARY 写明「调用次数变了」。
- 都不通 → 多问本身不支持；结论同上（退化路径），并把服务侧响应原文记下来。

跑法（需 8009 隧道 / 已有判断服务；不连 LLM、不打印任何凭据）：

    /Users/YUYSONG/Agents/navi-dual-system/.venv/bin/python -B tests/gate/probe_hub_multi_question.py
    # 端口不在默认位置：EOT_JUDGE_BASE=http://127.0.0.1:8009 ...（与判停同一套环境变量）

退出码：0 = 至少一组通过；2 = 服务不可达（探针没跑，结论写「未验」）；3 = 服务可达但没有一组通过。
"""
from __future__ import annotations

import json
import os
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
sys.dont_write_bytecode = True

import httpx2  # noqa: E402

from gate.hub_accept import ACCEPT_INSTRUCTIONS  # noqa: E402
from gate.hub_ack import ACK_CRITERIA, ACK_INSTRUCTIONS  # noqa: E402
from gate.hub_pace import (CUSHION_CRITERIA, CUSHION_INSTRUCTIONS, PACE_CRITERIA,  # noqa: E402
                           PACE_INSTRUCTIONS)
from gate.turn_eot import EOT_CRITERIA, EOT_INSTRUCTIONS  # noqa: E402

BASE = os.environ.get("EOT_JUDGE_BASE", "http://127.0.0.1:8009").rstrip("/")
PORT = urlsplit(BASE).port or 80
PATH = "/v1/systemone"
TIMEOUT_S = float(os.environ.get("EOT_JUDGE_TIMEOUT_S", "5.0"))
TEXT = "帮我看看前面那个"          # 没说完：既可能「该应一声」，也可能「不用回答」
SCENE = "主驾正看着前方，像在跟车机说话。"


def questions() -> dict:
    return {
        "accept": {"type": "noul", "instructions": ACCEPT_INSTRUCTIONS},
        "ack_choice": {"type": "choice", "instructions": ACK_INSTRUCTIONS,
                       "criteria": dict(ACK_CRITERIA)},
        "ack_noul": {"type": "noul", "instructions": ACK_INSTRUCTIONS},
        "eot": {"type": "choice", "instructions": EOT_INSTRUCTIONS,
                "criteria": dict(EOT_CRITERIA)},
        # 期二（2026-11-16）：节奏 + 垫句——能不能跟判停 / 要不要回答 / 应一声挤进同一次请求。
        "pace": {"type": "choice", "instructions": PACE_INSTRUCTIONS,
                 "criteria": dict(PACE_CRITERIA)},
        "cushion": {"type": "choice", "instructions": CUSHION_INSTRUCTIONS,
                    "criteria": dict(CUSHION_CRITERIA)},
    }


SHAPES = {
    "A 两问（都 noul）": ("accept", "ack_noul"),
    "B 两问（混类型 noul + choice）": ("accept", "ack_choice"),
    "C 三问（混类型 choice + noul）": ("eot", "accept", "ack_choice"),
    # 期二横断面：五问一次带（判停 + 要不要回答 + 应一声 + 节奏 + 垫句）能全答回来，
    # 才谈得上把每版 4 路（积极 + 满 1.5 秒那版 5 路）合并回 1 路。本期没通隧道 → 未验。
    "D 五问（期二：eot + accept + ack + pace + cushion）":
        ("eot", "accept", "ack_choice", "pace", "cushion"),
}


def one_request(names) -> dict:
    allq = questions()
    payload = {"state": json.dumps({"utterance": TEXT, "scene": SCENE}, ensure_ascii=False),
               "think_mode": "never",
               "questions": {name: allq[name] for name in names}}
    began = time.monotonic()
    try:
        response = httpx2.post(BASE + PATH, json=payload, timeout=TIMEOUT_S, trust_env=False)
    except Exception as error:  # noqa: BLE001 - 探针要把失败也记下来，不猜结论
        return {"shape": " / ".join(names), "ok": False, "state": "unreachable",
                "error": "%s: %s" % (type(error).__name__, str(error)[:140])}
    wall_ms = (time.monotonic() - began) * 1000
    try:
        raw = json.loads(response.content)
    except Exception:  # noqa: BLE001
        raw = None
    answers = sorted((raw or {}).get("answers", {}) or {})
    complete = raw is not None and set(answers) == set(names)
    return {"shape": " / ".join(names), "ok": bool(complete), "state": "ok" if complete else "partial",
            "http": response.status_code, "wall_ms": round(wall_ms, 1),
            "answers": answers, "missing": sorted(set(names) - set(answers)),
            "extra": sorted(set(answers) - set(names)),
            "model": (raw or {}).get("model") if isinstance(raw, dict) else None,
            "latency_ms": (raw or {}).get("latency_ms") if isinstance(raw, dict) else None,
            "body_head": (response.text or "")[:200]}


def main() -> int:
    print("服务：%s  （%s%s，端口 %s）" % (BASE, BASE, PATH, PORT))
    print("文本：%r  场景：%r" % (TEXT, SCENE))
    print("探针：同一版出字上一次请求带 %s 个问题，看服务是否把每题都答回来（%s）\n"
          % ("多", uuid.uuid4().hex[:6]))
    rows = []
    for tag, names in SHAPES.items():
        row = one_request(names)
        rows.append(row)
        print("[%s] %s" % (tag, "通过" if row["ok"] else "不通"))
        print("      answers=%s  missing=%s  extra=%s  http=%s  wall=%sms  model=%s  latency=%s"
              % (row.get("answers"), row.get("missing"), row.get("extra"), row.get("http"),
                 row.get("wall_ms"), row.get("model"), row.get("latency_ms")))
        if row.get("error"):
            print("      error=%s" % row["error"])
        if not row["ok"] and row.get("body_head"):
            print("      服务原文（截断）：%s" % row["body_head"])
    reachable = [row for row in rows if row["state"] != "unreachable"]
    if not reachable:
        print("\n结论：8009 不可达 → 探针未跑，多问能否一次问完「未验」。")
        print("      期一 / 期二都按退化路径落地：同一版出字上分别发（期一 2 路 → 3 路/版；")
        print("      期二 3 路 → 4 路/版，「积极且已说满 1.5 秒」那一版再 +1 问垫句），已写在 SUMMARY。")
        return 2
    passed = [row for row in rows if row["ok"]]
    if not passed:
        print("\n结论：服务可达，但三组多问都没有把每题答回来 → 多问不支持。")
        print("      期一按退化路径落地：同一版出字上多发一次调用（2 路 → 3 路/版），已写在 SUMMARY。")
        return 3
    print("\n结论：至少一组通过。能一次问完的是 %s。" % "、".join(row["shape"] for row in passed))
    if any(row["shape"] == "eot / accept / ack_choice" for row in passed):
        print("      三问一次带（判停 + 要不要回答 + 应不应一声）可用 → 调用次数可从 2 路/版降到 1 路/版；")
        print("      期一未改关键路径，这是后续期可选的合并（合并要单独拍，不改收口与开闸）。")
    else:
        print("      三问混类型不通（choice + noul 是服务边界）→ 期一按退化路径：3 路/版，已写明。")
    out = Path("/tmp") / "poc-hub-multi-question-%s.json" % time.strftime("%Y%m%d-%H%M%S")
    out.write_text(json.dumps({"base": BASE, "text": TEXT, "scene": SCENE, "rows": rows},
                              ensure_ascii=False, indent=2) + "\n")
    print("      明细：%s" % out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
