"""端到端：对话式二次确认真正长在 TurnSession 主循环里的全链路驱动（纯本地 mock）。

与只是"用纯函数编排一遍"的 poc_confirm_flow.py 不同，本脚本把确认闸门接到真实
TurnSession 状态机：第一轮 decide 命中池内工具 -> CONFIRM_WAIT -> 发确认请求 ->
第二轮 ASR 到达并经 EOT 判停收敛 -> score_confirm_reply(mock) -> judge -> 落四分支。
通过 `CONFIRM_WAIT` 状态、`_resolve_confirm`/`_run_takeover` 在真实主循环里跑通。

5 条验收路径（docs/spec-kev-confirmation-v2.md §6.1 / 本任务目标 a-e）：
  a) 关闭ESP -> "好的，关吧" -> agree -> 执行(mock ESC)
  b) 导航回家(池外) -> 不确认 -> 直接执行
  c) 关闭ESP -> "你不要关，取消" -> reject -> 放弃
  d) 关闭ESP -> "先看看空调，调到24度" -> takeover -> 放弃ESP，转去处理空调
  e) 关闭ESP -> "嗯…随便吧"(含糊) -> unknown -> 默认放弃不执行

运行：python3 -B tests/poc_confirm_e2e.py
"""
from __future__ import annotations

import json
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import bootstrap
bootstrap.install()
sys.dont_write_bytecode = True

from turn_session import TurnConfig, TurnSession

SPEED = 6.0
PAUSE = re.compile(r"\|(\d+)\|")


def build_script(text):
    """把一句话展开成逐字 ASR partial（前缀累积）+ speech_start/speech_end VAD。"""
    updates, vad = [], []
    t = 0.0
    vad.append({"t_ms": int(t), "kind": "speech_start"})
    for i, _char in enumerate(text):
        t += 1000.0 / SPEED
        updates.append({"t_ms": int(t), "text": text[:i + 1], "final": False})
    updates[-1]["final"] = True
    vad.append({"t_ms": int(updates[-1]["t_ms"]) + 20, "kind": "speech_end"})
    return updates, vad


def fake_eot(text):
    time.sleep(0.01)
    return {"complete": 0.8, "cut": 0.15, "wait": 0.05, "label": "complete", "margin": 0.65,
            "wall_ms": 10.0, "kev_ms": 5.0}


class FakeChain:
    """可配置的注入链：round1_tool 决定第一轮决策；takeover 后 tools() 返回空调短名单。"""

    def __init__(self, round1_tool="car_drive_ESPSwitch", take_selected=("car_hvac_set_temp",),
                 take_decision="car_hvac_set_temp"):
        self.round1_tool = round1_tool
        self.round1_text = round1_tool if round1_tool else ""
        self.take_selected = list(take_selected)
        self.take_decision = take_decision
        self.takeover = False        # 第一轮确认落地后置 True，接管时就该选空调

    def tools(self, text, tool_set, context):
        time.sleep(0.02)
        # 接管识别：文本是第二轮转折（含空调等新诉求）且不是第一轮本身 -> 切到空调候选
        if not self.takeover and "空调" in text and text != self.round1_text:
            self.takeover = True
        if not self.takeover:
            return {"scores": {"tool:" + self.round1_tool: 0.9, "tool:car_hvac_set_temp": 0.2,
                               "tool:car_navi_to_destination": 0.7},
                    "selected": [self.round1_tool, "car_hvac_set_temp"],
                    "schemas": [{"name": self.round1_tool, "description": "d", "parameters": {"type": "object"}}],
                    "native_schemas": None, "domains": None,
                    "timing": {"stage1_ms": 20.0, "stage2_ms": 0.0, "total_ms": 20.0}}
        # 接管：返回空调候选
        return {"scores": {"tool:" + self.take_decision: 0.9},
                "selected": self.take_selected,
                "schemas": [{"name": self.take_decision, "description": "d", "parameters": {"type": "object"}}],
                "native_schemas": None, "domains": None,
                "timing": {"stage1_ms": 20.0, "stage2_ms": 0.0, "total_ms": 20.0}}

    def decide(self, utterance, selected, schemas, tool_set, context, **kwargs):
        time.sleep(0.03)
        if self.takeover and self.take_selected:
            tool = self.take_selected[0]
        elif selected:
            tool = selected[0]
        else:
            tool = self.round1_tool
        if self.takeover:
            self.takeover = False   # 接管只跑一次
        return {"decision": {"kind": "tool_call", "tool": tool, "arguments": {}},
                "llm_ms": 30.0, "log_fields": {"decision_head": "mock"}}


def run_case(label, round1_text, round2_text=None, *, round1_pool=True, chain=None):
    """用 TurnSession 跑一遍：round1 说话 -> (若池内) 确认请求 -> round2 说话 -> 判意图。

    每个回合作为一条完整 final ASR 输入 + speech_end（模拟"一句话说完了"）。确认轮的二轮回
    在 CONFIRM_WAIT 内到达并经 EOT 判停（fake_eot 恒判 complete）收敛，随即判意图落四分支。
    """
    if chain is None:
        chain = FakeChain()
    chain.round1_text = round1_text
    if not round1_pool:
        chain.round1_tool = "car_navi_to_destination"   # 池外，直接执行

    cfg = TurnConfig(speculative_llm=False, eot_policy="margin", eot_margin=0.2,
                     warm_llm=False, confirm_silence_ms=250, max_silence_ms=900,
                     think_wait_ms=15000, cut_wait_ms=1200)
    events = []
    session = TurnSession(cfg, events.append, eot_fn=fake_eot,
                          select_tools_fn=chain.tools, decide_fn=chain.decide,
                          session_id="e2e-" + label.split()[0],
                          evidence_root=None)
    start = time.monotonic()
    # 回合 1
    session.on_vad(10, "speech_start")
    session.on_asr(200, round1_text, True)
    gap = 600        # 说完后静默等待判停/确认 + LLM 决策
    order = []
    # round2
    if round2_text:
        base = 200 + gap + 500   # 确认请求已发出、进入 CONFIRM_WAIT 后再说二轮回
        order.append(("vad", base, "speech_start"))
        order.append(("asr", base + 150, round2_text, True))

    # 统一推进：先喂事件，再持续 tick 到收尾
    deadline = time.monotonic() + 20
    fed = [False] * len(order)

    def feed(now):
        for index, item in enumerate(order):
            if fed[index] or now < item[1]:
                continue
            if item[0] == "vad":
                session.on_vad(item[1], item[2])
            else:
                session.on_asr(item[1], item[2], item[3])
            fed[index] = True

    while True:
        now = int((time.monotonic() - start) * 1000)
        feed(now)
        session.tick(now)
        # 已收到确认判定（或接管）且回到听完态，给足时间落分支
        if (session.confirm_resolved is not None
                and session.state == "LISTENING"):
            break
        if session.state == "LISTENING" and any(e["event"] in {"confirm_resolve", "confirm_request"}
                                                for e in events):
            # b: 池外直接执行 -> 有 llm_decision 即收
            if not round2_text and any(e["event"] == "llm_decision" for e in events):
                break
            # a/c/d/e: 确认轮已落分支
            if round2_text and any(e["event"] == "confirm_resolve" for e in events):
                break
        if session.turn_done:
            break
        if time.monotonic() > deadline:
            print("    [driver] 超时未收尾，state=", session.state)
            break
        time.sleep(0.002)

    print("=" * 78)
    print(f"路径 {label}: 第一轮={round1_text!r}  第二轮={round2_text!r}")
    print("  -- 本轮事件（只列关键）--")
    for e in events:
        name = e["event"]
        if name in {"asr_update", "vad", "eot_start", "tools_start", "tools_scores", "eot_scores"}:
            continue
        if name == "llm_decision":
            print(f"    llm_decision      tool={e['decision'].get('tool')} kind={e['decision'].get('kind')}")
        elif name == "confirm_request":
            print(f"    confirm_request   tool={e.get('tool')} ask={e.get('ask')!r}")
        elif name == "confirm_scores":
            print(f"    confirm_scores    " + " ".join(f"{k}={round(v,3) if isinstance(v,float) else v}" for k,v in e.items() if k in ("agree","reject","takeover","mode")))
        elif name == "confirm_resolve":
            print(f"    confirm_resolve   intent={e.get('intent')} action={e.get('action')} user_reply={e.get('user_reply')!r}\n                     {e.get('message')}")
        elif name == "confirm_exec":
            print(f"    confirm_exec      tool={e.get('tool')} mock_only={e.get('mock_only')} {e.get('message')}")
        elif name == "reroute_decision":
            print(f"    reroute_decision  -> {e.get('request')!r} decision_tool={e.get('decision',{}).get('tool')}")
        elif name == "state_change":
            print(f"    state_change      {e.get('from')} -> {e.get('to')} ({e.get('why')})")
        elif name == "error":
            print(f"    error             {e.get('error_type')} {e.get('error')}")
    session.close()
    return events


def print_header():
    print("=" * 78)
    print("端到端：对话式二次确认长在 TurnSession 主循环（纯本地 mock，不连 KEV/LLM）")
    print("=" * 78)


def main():
    print_header()
    # a) 关闭ESP -> 好的，关吧 -> agree
    events = run_case("a agree", "关闭ESP", "好的，关吧")
    assert any(e["event"] == "confirm_resolve" and e.get("intent") == "agree" for e in events), "a 未走到 agree"
    assert any(e["event"] == "confirm_exec" for e in events), "a 未执行"
    # b) 导航回家(池外) -> 直接执行
    events = run_case("b direct", "导航回家", round1_pool=False)
    assert not any(e["event"] == "confirm_request" for e in events), "b 不应进入确认"
    assert any(e["event"] == "llm_decision" and e["decision"].get("kind") == "tool_call" for e in events), "b 未决策"
    # c) 关闭ESP -> 你不要关，取消 -> reject
    events = run_case("c reject", "关闭ESP", "你不要关，取消")
    assert any(e["event"] == "confirm_resolve" and e.get("intent") == "reject" and e.get("action") == "abandon" for e in events), "c 未 reject"
    # d) 关闭ESP -> 先看看空调 -> takeover
    events = run_case("d takeover", "关闭ESP", "先看看空调，调到24度")
    assert any(e["event"] == "confirm_resolve" and e.get("intent") == "takeover" for e in events), "d 未 takeover"
    # e) 关闭ESP -> 嗯……随便吧 -> unknown -> 默认放弃
    events = run_case("e unknown", "关闭ESP", "嗯……随便吧")
    assert any(e["event"] == "confirm_resolve" and e.get("intent") == "unknown" and e.get("action") == "abandon" for e in events), "e 未 unknown"
    print("=" * 78)
    print("端到端全链路：PASS（5 条路径，本地 mock）")
    return 0


if __name__ == "__main__":
    sys.exit(main())