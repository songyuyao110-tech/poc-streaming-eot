"""真链验收：8765 上「读心一句场景 + 中枢真判断两路 + 开闸」端到端（真 8009 + 真 litellm）。

跑法（8765 已在跑）：
    /Users/YUYSONG/Agents/navi-dual-system/.venv/bin/python -B tests/probe_mind_accept_e2e.py

只读验收用的现场记录：每个用例记下出字、场景句（字数）、接不接分数、开闸线、判停结论、
闸门结论；另外跑一次「连续多版出字」看每一版是否都有自己的接不接结论。
"""
from __future__ import annotations

import json
import time
import urllib.request

BASE = "http://127.0.0.1:8765"
MODEL = "deepseek-v4.1-flash"   # 2026-11-11 起线上默认档；本探针验默认档这条链


def post(path: str, body: dict, timeout: float = 30.0) -> dict:
    request = urllib.request.Request(BASE + path, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.loads(response.read())


def mind_tick(session: str, text: str, final: bool) -> dict:
    return post("/api/mind/tick", {"session_id": session, "seat": "D", "model": MODEL,
                                   "asr": {"text": text, "final": final}})


def hub_tick(session: str, text: str, final: bool = True, eot: str = "real") -> dict:
    return post("/api/hub/tick", {"session_id": session, "seat": "D", "model": MODEL,
                                  "asr": {"text": text, "final": final}, "eot": eot})


def wait_scene(session: str, text: str, timeout: float = 25.0) -> dict:
    """等读心那句场景真出来（真模型一次约 1.3～1.5s；partial 要稳定 2s 才发）。"""
    end = time.time() + timeout
    last = {}
    while time.time() < end:
        last = mind_tick(session, text, True)
        scene = last.get("scene") or {}
        if scene.get("summary") and not scene.get("pending") and scene.get("source") != "llm_pending":
            return last
        time.sleep(0.3)
    return last


def wait_gate(session: str, text: str, timeout: float = 15.0) -> dict:
    end = time.time() + timeout
    last = {}
    while time.time() < end:
        last = hub_tick(session, text, True)
        gate = last.get("gate") or {}
        if gate.get("route") in ("agent", "context_only"):
            return last
        if gate.get("route") == "accept_unavailable":
            return last
        time.sleep(0.2)
    return last


def record(name: str, session: str, text: str, scene_snap: dict, hub_snap: dict) -> dict:
    scene = (scene_snap.get("scene") or {})
    hub = hub_snap.get("hub") or {}
    gate = hub_snap.get("gate") or {}
    accept = gate.get("accept") or hub.get("accept") or {}
    eot = gate.get("eot") or hub.get("eot") or {}
    source = hub_snap.get("accept_source") or {}
    used = (accept.get("scene_used") or {}).get("summary") or scene.get("summary")
    return {
        "case": name,
        "text": text,
        # 判接不接那一刻真正送进判据的那句场景（不是页面后来刷新的那一份）
        "scene": used,
        "scene_chars": len(used or ""),
        "scene_source": scene.get("source"),
        "scene_engine": (scene_snap.get("engine") or {}).get("mode"),
        "accept_score": accept.get("score", accept.get("confidence")),
        "accept_accept": accept.get("accept"),
        "accept_state": accept.get("state"),
        "accept_threshold": accept.get("threshold", source.get("threshold")),
        "threshold_note": accept.get("threshold_note", source.get("threshold_note")),
        "accept_source": source.get("source"),
        "accept_model": source.get("model"),
        "accept_scene_used": accept.get("scene_used"),
        "accept_latency_ms": accept.get("latency_ms"),
        "eot_label": eot.get("label"),
        "eot_latency_ms": eot.get("latency_ms"),
        "gate_route": gate.get("route"),
        "gate_state": gate.get("state"),
        "gate_reason": gate.get("reason"),
        "two_route_ms": gate.get("two_route_ms"),
        "version": hub.get("version"),
        "versions": hub.get("versions"),
    }


def main() -> int:
    out = {"model": MODEL, "cases": []}

    # 1) 对车说：要接 + 开闸进 Agent
    command = "导航去望京SOHO"
    s1 = "e2e-command-%d" % int(time.time())
    scene1 = wait_scene(s1, command)
    out["cases"].append(record("对车说", s1, command, scene1, wait_gate(s1, command)))

    # 2) 和旁边人聊天：不接 + 只进上下文
    chat = "我跟你说，昨天那个电影可好看了"
    s2 = "e2e-chat-%d" % int(time.time())
    scene2 = wait_scene(s2, chat)
    out["cases"].append(record("对人闲聊", s2, chat, scene2, wait_gate(s2, chat)))

    # 3) 没说完：先不开闸
    cut = "帮我看看前面那个"
    s3 = "e2e-cut-%d" % int(time.time())
    scene3 = wait_scene(s3, cut)
    out["cases"].append(record("没说完", s3, cut, scene3, wait_gate(s3, cut)))

    # 4) 连续多版出字：每一版都有接不接结果（不丢 / 不合并）
    s4 = "e2e-versions-%d" % int(time.time())
    texts = ["导航", "导航去", "导航去望京", "导航去望京SOHO"]
    snap = {}
    for index, text in enumerate(texts):
        snap = hub_tick(s4, text, final=(index == len(texts) - 1))
        time.sleep(0.35)
    deadline = time.time() + 12
    while time.time() < deadline:
        snap = hub_tick(s4, texts[-1], final=True)
        versions = (snap.get("hub") or {}).get("versions") or []
        if len(versions) >= len(texts) and all(v["accept"]["state"] in ("ok", "failed", "timeout")
                                               for v in versions):
            break
        time.sleep(0.2)
    out["versions_case"] = {
        "session": s4,
        "sent": texts,
        "versions": (snap.get("hub") or {}).get("versions"),
        "counters": (snap.get("hub") or {}).get("counters"),
        "every_version_has_result": len((snap.get("hub") or {}).get("versions") or []) >= len(texts)
        and all(v["accept"]["state"] != "pending"
                for v in ((snap.get("hub") or {}).get("versions") or [])),
    }

    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
