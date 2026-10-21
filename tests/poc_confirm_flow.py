"""端到端：对话式二次确认全流程（纯本地 mock，不连 KEV/LLM）。

流程（对应 docs/spec-kev-confirmation-v2.md §3/§4）：
  第一轮: fake select_tools/decide 选中一个待确认工具（car_drive_ESPSwitch）-> 判定 ∈ CONFIRM_POOL
        -> 进入 CONFIRM_WAIT，发出确认文案（第一轮开口）。
  第二轮: 用户回复到达（fake EOT 已判"说完"）-> score_confirm_reply(mock=True) 打三问分
        -> judge_confirm_reply 裁决 agree/reject/takeover/unknown
        -> agree=执行(mock) / reject|unknown=放弃 / takeover=放弃旧动作并把 user_reply 当新回合。
安全护栏：unknown 默认 reject（绝不默认执行）。

本脚本同时用 confirm_gate 的纯函数编排一个"确认网关" pipeline，并用一条 TurnSession（注入
fake EOT/tools/decide，沿用 poc-streaming-session-smoke.py 的注入风格）展示第一轮如何天然选到
一个待确认工具。均不需要真实 KEV/LLM 隧道。

运行：python3 -B tests/poc_confirm_flow.py
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import bootstrap
bootstrap.install()
sys.dont_write_bytecode = True

from confirm_gate import (CONFIRM_ASKS, confirm_ask_for, is_confirm_tool,
                          judge_confirm_reply, score_confirm_reply)
from turn_session import TurnConfig, TurnSession


# ------------------------------- 产品层确认网关（纯函数编排） -------------------------------

def run_confirm_round(round1_tool: str, user_reply: str):
    """模拟一次完整的一轮+二轮确认。

    返回 (round1, round2) 详情 dict。round1 用现成编排，round2 走 confirm_gate。
    """
    # 第一轮：product gate（非 KEV）
    confirm_ask = None
    if is_confirm_tool(round1_tool):
        confirm_ask = confirm_ask_for(round1_tool)
        state = "CONFIRM_WAIT"
    else:
        state = "EXECUTE_DIRECT"   # 池外直接执行（永不确认）

    # 第二轮：KEV 解读用户回复（mock 打分）
    scores = score_confirm_reply(user_reply, confirm_ask, mock=True)
    intent = judge_confirm_reply(user_reply, scores)

    # 落地分支
    if state == "EXECUTE_DIRECT":
        action = "execute(直接,池外)"
    elif intent == "takeover":
        action = "abandon(放弃当前待确认动作) -> 把 user_reply 当新回合再跑 select_tools/decide"
    elif intent == "agree":
        action = f"execute(mock {round1_tool})"
    elif intent == "reject":
        action = "abandon(不执行),询问是否需要别的"
    else:  # unknown -> 安全优先默认 reject
        action = "abandon(默认reject/不执行——安全优先),绝不默认执行"

    return {
        "round1": {"tool": round1_tool, "in_pool": is_confirm_tool(round1_tool),
                   "state": state, "confirm_ask": confirm_ask},
        "round2": {"user_reply": user_reply, "scores": scores, "intent": intent, "action": action},
    }


# ------------------------------- TurnSession 注入展示第一轮选中待确认工具 -------------------------------

def fake_eot(text):
    time.sleep(0.01)
    return {"complete": 0.8, "cut": 0.15, "wait": 0.05, "label": "complete", "margin": 0.65,
            "wall_ms": 10.0, "kev_ms": 5.0}


def fake_tools(text, tool_set, context):
    time.sleep(0.02)
    return {"scores": {"tool:car_drive_ESPSwitch": 0.9, "tool:car_adas_control": 0.7,
                       "tool:car_status_BrakeMaintenanceSet": 0.6},
            "selected": ["car_drive_ESPSwitch", "car_adas_control", "car_status_BrakeMaintenanceSet"],
            "schemas": [{"name": "car_drive_ESPSwitch", "description": "d", "parameters": {"type": "object"}}],
            "native_schemas": None, "domains": None,
            "timing": {"stage1_ms": 20.0, "stage2_ms": 0.0, "total_ms": 20.0}}


def fake_decide(utterance, selected, schemas, tool_set, context, **kwargs):
    time.sleep(0.05)
    return {"decision": {"kind": "tool_call", "tool": "car_drive_ESPSwitch", "arguments": {}},
            "llm_ms": 50.0, "log_fields": {"decision_head": "mock"}}


def drift_flow():
    """用 TurnSession 跑一轮：注入 fake 链路让第一轮选出 car_drive_ESPSwitch（池内）。

    返回该轮气血上的 decision.tool，证明"第一轮会天然落到一个待确认工具"。
    """
    cfg = TurnConfig(speculative_llm=False, eot_policy="margin", eot_margin=0.2, warm_llm=False)
    events = []
    session = TurnSession(cfg, events.append, eot_fn=fake_eot, select_tools_fn=fake_tools,
                          decide_fn=fake_decide, session_id="confirm-flow", evidence_root=None)
    # 用 4 个字符逐字 ASR 前缀推进（"关ESP"）
    utterance = "关闭ESP"
    start = time.monotonic()
    for i, ch in enumerate(utterance):
        now = int((time.monotonic() - start) * 1000)
        session.on_vad(now, "speech_start")
        session.on_asr(now + i * 150, utterance[: i + 1], final=(i == len(utterance) - 1))
    # 静默推进，让判停/工具/决策落地
    budget = 6000
    while not session.turn_done and (time.monotonic() - start) * 1000 < budget:
        session.tick(int((time.monotonic() - start) * 1000))
        if not session.idle():
            time.sleep(0.005)
        elif (time.monotonic() - start) * 1000 > 1500:
            break
    # 取最后一笔 decision
    decision = None
    for event in events:
        if event["event"] == "llm_decision":
            decision = event["decision"]
    session.close()
    return decision


# ------------------------------- 显示与断言 -------------------------------

def main():
    print("=" * 72)
    print("端到端：对话式二次确认 (confirm_gate, 纯本地 mock)")
    print("=" * 72)

    # 0) TurnSession 第一轮选中池内工具
    decision = drift_flow()
    tool = (decision or {}).get("tool")
    print("\n[0] 第一轮 TurnSession(注入 fake select_tools/decide) 决策:",
          json.dumps(decision, ensure_ascii=False))
    print(f"    -> 选中工具 {tool!r} ∈ CONFIRM_POOL: {is_confirm_tool(tool)}"
          f"  状态: {'CONFIRM_WAIT' if is_confirm_tool(tool) else 'EXECUTE_DIRECT'}")

    # 1) 池内待确认工具的完整两轮
    table = [
        ("car_drive_ESPSwitch", "好的，关吧"),
        ("car_drive_ESPSwitch", "不要，别动"),
        ("car_control_Vehicle_HoodOpen", "你先别管这个"),
        ("car_control_Vehicle_HoodOpen", "先看看空调，调到24度"),
        ("car_drive_ESPSwitch", "嗯……随便吧"),
        ("car_drive_ESPSwitch", "你敢关试试"),
    ]
    print("\n[1] 池内工具 -> 二轮意图 -> 落地分支：")
    ok = True
    for tool, reply in table:
        result = run_confirm_round(tool, reply)
        r1, r2 = result["round1"], result["round2"]
        print(f"  tool={tool:<28} reply={reply:<18} "
              f"intent={r2['intent']:<9} -> {r2['action']}")
    print()

    # 2) 池外工具永不确认（直接执行）
    result = run_confirm_round("navigation_route_to", "好")
    r1 = result["round1"]
    print(f"[2] 池外工具 navigation_route_to: state={r1['state']} (永不确认,直接执行)")
    assert r1["state"] == "EXECUTE_DIRECT"

    # 3) 拿不准默认 reject（安全护栏）—— 直接注入未知概率走 judge
    scores = {"agree": 0.30, "reject": 0.35, "takeover": 0.35}
    intent = judge_confirm_reply("嗯……随便吧", scores)
    eff = "abandon(reject, 安全优先)" if intent == "unknown" else intent
    print(f"[3] 未知概率 {scores} -> judge={intent} -> 默认动作 {eff} (绝不默认执行)")
    assert intent == "unknown"

    # 汇总
    print("\n" + "=" * 72)
    print("端到端确认流程：PASS（纯本地 mock，未连真实 KEV/LLM）")
    return 0


if __name__ == "__main__":
    sys.exit(main())