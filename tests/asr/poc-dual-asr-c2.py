"""C2 会话脑回放：A2「我也要」三档时序 + 无最近动作 + A6 对账 + 单人回归不变。

    python3 -B tests/poc-dual-asr-c2.py                 # 20 组 × 三档 + 无最近动作 + 回归
    python3 -B tests/poc-dual-asr-c2.py --only 3        # 只跑前 3 组（调试）
    python3 -B tests/poc-dual-asr-c2.py --no-regression # 跳过既有回归
    python3 -B tests/poc-dual-asr-c2.py --out /tmp/c2.json

判据（定稿 `refine-spec` §4.3 A2 / A6，用例 TC-16～TC-19 / TC-36）：

| 档 | 剧本 | 判据 |
|---|---|---|
| **未落地** | D：「打开座椅加热」→ D 确认后 150ms，P：「我也要」 | 1 个 AU 作用两座、D 座下发 1 次、合并回执含两席、重复动作 0、回问 0 |
| **已落地 0.5s** | D 决策落地 500ms 后，P：「我也要」 | 2 个 AU（D 一次、P 一次）、P 只作用自己座、重复动作 0、错座 0 |
| **已落地 5s** | 同上，间隔 5s | 同上（台账里最近动作仍可指代） |
| **无最近动作** | 会话刚开始，P：「我也要」 | 恰 1 次回问、执行动作 0、不循环追问 |

每档都核账：每席 `llm_calls == consumed + llm_wasted`；编排层 `iu_open == 0` 且
`iu_committed == au_executed + au_abandoned`（A6）。

判停用假实现（立即 complete、confidence 0.55 → 不抢跑），选工具/决策用固定假实现：
本脚本验的是**编排层语义与账**，不验判停质量；数字不与真 8009 口径混比。
"""
from __future__ import annotations

import argparse
import json
import os
import re
import statistics
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.dont_write_bytecode = True

from asr.dual_seat import DualSeatSession
# noqa: E402
from agent.orchestra import SessionBrain
# noqa: E402
from gate.turn_session import TurnConfig
# noqa: E402
DEFAULT_OUT = ROOT / "evidence/dual-asr-c2/run-latest.json"

# 20 组剧本：判停/选工具/决策都是假的，只有（工具, 参数）承载「同一个动作」的语义。
SCENES = [
    ("打开座椅加热", "seat_heating", {"enabled": True}),
    ("打开座椅通风", "seat_vent", {"enabled": True}),
    ("打开座椅按摩", "seat_massage", {"enabled": True}),
    ("关闭座椅加热", "seat_heating", {"enabled": False}),
    ("座椅靠背放倒一点", "seat_recline", {"angle": 120}),
    ("方向盘加热打开", "steering_heating", {"enabled": True}),
    ("空调调到24度", "climate_set_temperature", {"temperature": 24}),
    ("空调风量调到3档", "climate_set_fan", {"level": 3}),
    ("打开内循环", "climate_recirculation", {"enabled": True}),
    ("后排空调开一下", "climate_rear", {"enabled": True}),
    ("打开车窗", "window", {"position": "all", "percent": 30}),
    ("天窗打开", "sunroof", {"percent": 50}),
    ("氛围灯换成蓝色", "ambient_light", {"color": "blue"}),
    ("音量调大一点", "audio_volume", {"delta": 3}),
    ("播放我的歌单", "media_play", {"target": "my_playlist"}),
    ("导航回家", "nav_set_destination", {"target": "home"}),
    ("找附近的咖啡店", "nav_search", {"keyword": "咖啡"}),
    ("打开香氛", "fragrance", {"enabled": True}),
    ("后视镜加热", "mirror_heating", {"enabled": True}),
    ("查询当前车况", "vehicle_status", {"item": "all"}),
]

VARIANTS = ("pending", "after_0.5s", "after_5s", "no_target")
VARIANT_LABELS = {"pending": "未落地", "after_0.5s": "已落地 0.5s",
                  "after_5s": "已落地 5s", "no_target": "无最近动作"}


# ---------------------------------------------------------------- 假实现

def eot_complete(text):
    """立即判 complete、confidence 0.55（弱）：不抢跑，收口靠 confirm_silence=700ms。"""
    return {"complete": 0.90, "cut": 0.06, "wait": 0.04, "label": "complete", "choice": "complete",
            "confidence": 0.55, "margin": 0.84, "model": "fake-c2", "wall_ms": 1.0}


def make_select(tool):
    schema = {"name": tool, "description": tool,
              "parameters": {"type": "object", "properties": {}, "additionalProperties": False}}

    def _select(text, tool_set, context):
        return {"scores": {"tool:" + tool: 0.95}, "selected": [tool], "schemas": [schema],
                "native_schemas": [schema], "domains": [],
                "timing": {"stage1_ms": 0.0, "stage2_ms": 0.0, "total_ms": 0.0}}
    return _select


def make_decide(tool, arguments):
    def _decide(utterance, selected, schemas, tool_set="core_score", context=None, **kwargs):
        time.sleep(0.25)      # 故意留「决策在途」窗口：未落地档要靠它
        return {"decision": {"kind": "tool_call", "tool": tool,
                             "arguments": dict(arguments)},
                "llm_ms": 250.0, "log_fields": {"decision_head": "c2_mock"}}
    return _decide


# ---------------------------------------------------------------- 回放

class Scene:
    def __init__(self, tool, arguments, merge_window_ms=2500):
        self.started = time.monotonic() * 1000.0
        self.brain = SessionBrain(make_decide(tool, arguments),
                                  merge_window_ms=merge_window_ms)
        self.duo = DualSeatSession(TurnConfig(), None, eot_fn=eot_complete,
                                   select_tools_fn=make_select(tool),
                                   session_id="c2-" + str(int(self.started)),
                                   evidence_root=None, brain=self.brain)

    def now(self):
        return int(round(time.monotonic() * 1000.0 - self.started))

    def wait(self, at):
        while self.now() < at:
            self.duo.tick(self.now())
            time.sleep(0.003)
        self.duo.tick(self.now())

    def feed(self, mic, text, t0, interval=150):
        for k in range(1, len(text) + 1):
            self.wait(t0 + (k - 1) * interval)
            self.duo.on_asr(self.now(), mic, text[:k], k == len(text))
        self.wait(t0 + len(text) * interval + 18)
        self.duo.on_vad(self.now(), mic, "speech_end")

    def wait_for(self, predicate, limit_ms=12000):
        end = self.now() + limit_ms
        while self.now() < end:
            self.duo.tick(self.now())
            hit = predicate()
            if hit:
                return hit
            time.sleep(0.003)
        return None

    def event(self, name, seat=None, to=None):
        for ev in self.duo.events:
            if ev.get("event") != name:
                continue
            if seat is not None and ev.get("seat_id") != seat:
                continue
            if to is not None and ev.get("to") != to:
                continue
            return ev
        return None

    def close(self):
        self.duo.close()


def run_variant(scene_text, tool, arguments, variant):
    scene = Scene(tool, arguments)
    if variant == "no_target":
        scene.feed("mic-P", "我也要", 0)
    else:
        scene.feed("mic-D", scene_text, 0)
        confirm = scene.wait_for(lambda: scene.event("state_change", seat="D", to="CONFIRMED"))
        if confirm is None:
            scene.close()
            return {"variant": variant, "error": "D 没有确认", "checks": []}
        if variant == "pending":
            t_p = confirm["t_ms"] + 150
        else:
            decision = scene.wait_for(lambda: scene.event("llm_decision", seat="D"))
            if decision is None:
                scene.close()
                return {"variant": variant, "error": "D 没有决策", "checks": []}
            t_p = decision["t_ms"] + (500 if variant == "after_0.5s" else 5000)
        scene.feed("mic-P", "我也要", t_p)
        want = 1 if variant == "pending" else 2
        scene.wait_for(lambda: scene.brain.snapshot()["au_executed"] >= want
                       and not any(au.state == "held" for au in scene.brain.aus))

    if variant == "no_target":
        # 只有一路 ASR：「我也要」要走到回问为止，别在决策在途时就收口
        scene.wait_for(lambda: any(ev.get("event") == "llm_decision"
                                   and (ev.get("decision") or {}).get("kind") == "ask_user"
                                   for ev in scene.duo.events))
    scene.wait(2 * 400 + scene.now())      # 宽限：等两席回合收口、账落地

    scene.duo.stop(scene.now())
    audit = scene.brain.audit()
    duo = scene.duo
    checks = []
    executed = [au for au in audit["aus"] if au["state"] == "executed"
                and au["kind"] not in ("ask_user", "done")]
    ask = sum(1 for ev in duo.events
              if ev.get("event") == "llm_decision"
              and (ev.get("decision") or {}).get("kind") == "ask_user")
    eq = {seat: duo.seats[seat].counters["llm_calls"]
          == duo.seats[seat].counters["consumed"] + duo.seats[seat].counters["llm_wasted"]
          for seat in ("D", "P")}

    def add(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})

    add("每席 llm_calls == consumed + wasted", all(eq.values()), json.dumps(eq))
    add("iu_open==0 且 iu_committed==au_executed+au_abandoned", audit["iu_eq"],
        "iu_open=%s iu_committed=%s au_executed=%s au_abandoned=%s"
        % (audit["iu_open"], audit["iu_committed"], audit["au_executed"], audit["au_abandoned"]))

    if variant == "pending":
        add("1 个 AU 执行", audit["au_executed"] == 1, str(audit["au_executed"]))
        add("AU 作用两座", [au["scope"] for au in executed] == [["D", "P"]],
            str([au["scope"] for au in executed]))
        add("参与者含两席", executed and executed[0]["participants"] == ["D", "P"],
            str(executed[0]["participants"]) if executed else "无")
        add("合并回执含两席", audit["receipt_has_both"] is True)
        add("D 座只下发 1 次（重复动作 0）", audit["repeat_action"] == 0, str(audit["repeat_action"]))
        add("错座 0", audit["wrong_seat"] == 0)
        add("有最近动作时回问 0", ask == 0, str(ask))
        add("编排层真值 D 1 / P 0", audit.get("decisions") == {"D": 1, "P": 0},
            str(audit.get("decisions")))
    elif variant in ("after_0.5s", "after_5s"):
        add("2 个 AU 执行", audit["au_executed"] == 2, str(audit["au_executed"]))
        add("作用域各一座（D 一次、P 一次）",
            sorted([au["scope"] for au in executed]) == [["D"], ["P"]],
            str([au["scope"] for au in executed]))
        inherited = [au for au in executed if au["inherited_from"]]
        add("P 的 AU 指代了 D 的动作", len(inherited) == 1
            and inherited[0]["initiator"] == "P", str([au["au_id"] for au in executed]))
        add("D 座没有重发（重复动作 0）", audit["repeat_action"] == 0, str(audit["repeat_action"]))
        add("错座 0", audit["wrong_seat"] == 0)
        add("回问 0", ask == 0, str(ask))
        add("编排层真值 D 1 / P 1", audit.get("decisions") == {"D": 1, "P": 1},
            str(audit.get("decisions")))
    else:
        add("恰 1 次回问", ask == 1, str(ask))
        add("执行动作 0", audit["au_executed"] == 0 and audit["actions_effective"] == 0,
            "au=%s eff=%s" % (audit["au_executed"], audit["actions_effective"]))
        add("编排层真值 P 0", (audit.get("decisions") or {}).get("P") == 0,
            str(audit.get("decisions")))

    latencies = [item["latency_ms"] for seat in ("D", "P")
                 for item in duo.confirms.get(seat, []) if item["why"] == "confirm_silence"]
    result = {"variant": variant, "variant_label": VARIANT_LABELS[variant],
              "checks": checks, "passed": all(c["ok"] for c in checks),
              "audit": {key: audit[key] for key in
                        ("actions_effective", "repeat_action", "receipt_has_both", "au_executed",
                         "au_abandoned", "wrong_seat", "iu_open", "iu_committed", "iu_eq")},
              "decisions_truth": audit.get("decisions"),
              "violations": audit.get("wrong_seat"),
              "ask_user": ask,
              "confirm_p50_ms": round(statistics.median(latencies), 1) if latencies else None,
              "eq": eq,
              "executed": [{"au_id": au["au_id"], "initiator": au["initiator"],
                            "scope": au["scope"], "tool": au["tool"],
                            "inherited_from": au["inherited_from"], "receipt": au["receipt"]}
                           for au in executed]}
    scene.close()
    return result


# ---------------------------------------------------------------- 既有回归（A6 的「不变」）

REGRESSIONS = [
    ("单人 多段消费（C1–C4 共 36/36，合计 54/54）", "tests/poc-streaming-c1c4.py",
     r"C1-C5/F2 RESULT: (PASS|FAIL)"),
    ("二次确认端到端 5 条路径", "tests/poc_confirm_e2e.py", r"端到端全链路：(PASS|FAIL)"),
    ("二次确认流程（mock）", "tests/poc_confirm_flow.py", r"端到端确认流程：(PASS|FAIL)"),
    ("8 池确认闸门 53/53", "tests/test_confirm_gate.py", r"合计：(\d+) 项通过"),
    ("判停 score 四分支 24/24", "tests/test_eot_judge.py", r"(?m)^(OK|FAILED)"),
    ("双席隔离 8/8", "tests/test_dual_seat.py", r"(\d+)/(\d+) 例通过"),
    ("双席接 UI 5/5", "tests/test_dual_seat_ui.py", r"(\d+)/(\d+) 例通过"),
    ("会话脑单测 55/55", "tests/test_orchestra.py", r"(\d+)/(\d+) 例通过"),
    ("断言面自检 6/6", "tests/test_assert_surface.py", r"(\d+)/(\d+) 例通过"),
]


def run_regressions():
    out = []
    for label, script, pattern in REGRESSIONS:
        path = ROOT / script
        try:
            proc = subprocess.run([sys.executable, "-B", str(path)], capture_output=True,
                                  text=True, timeout=600, cwd=str(ROOT))
            text = (proc.stdout or "") + (proc.stderr or "")
            hits = re.findall(pattern, text)
            ok = proc.returncode == 0 and bool(hits)
            tail = hits[-1] if hits else "（没找到期望输出）"
            out.append({"name": label, "script": script, "ok": ok,
                        "matched": tail if isinstance(tail, str) else "/".join(tail),
                        "returncode": proc.returncode,
                        "tail": text.strip().splitlines()[-1] if text.strip() else ""})
        except Exception as error:  # noqa: BLE001
            out.append({"name": label, "script": script, "ok": False, "matched": repr(error),
                        "returncode": None, "tail": ""})
        print("   %-6s %s（%s）" % ("PASS" if out[-1]["ok"] else "FAIL", label,
                                   out[-1]["matched"]))
    return out


# ---------------------------------------------------------------- main

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--only", type=int, default=0, help="只跑前 N 组剧本（0=全量）")
    parser.add_argument("--variants", default=",".join(VARIANTS))
    parser.add_argument("--no-regression", action="store_true")
    parser.add_argument("--regression-only", action="store_true", help="只跑既有回归（A6 不变式）")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()

    if args.regression_only:
        print("A6 既有回归（单人 36/36 与 5 条端到端不变）")
        regressions = run_regressions()
        payload = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
                   "regressions": regressions}
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
        print("回归：%d/%d 不变｜证据：%s"
              % (sum(1 for r in regressions if r["ok"]), len(regressions), out))
        return 0 if all(r["ok"] for r in regressions) else 1

    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    scenes = SCENES[:args.only] if args.only else SCENES
    results = []
    print("C2 会话脑回放：%d 组 × %d 档｜判停＝假实现（立即 complete、confidence 0.55）"
          % (len(scenes), len(variants)))
    for index, (text, tool, arguments) in enumerate(scenes, start=1):
        for variant in variants:
            result = run_variant(text, tool, arguments, variant)
            result.update({"scene": index, "text": text, "tool": tool, "arguments": arguments})
            results.append(result)
            bad = [c["check"] for c in result["checks"] if not c["ok"]]
            print("  [%02d/%s] %-9s %-6s au=%s eff=%s repeat=%s ask=%s%s"
                  % (index, result["variant"], text,
                     "PASS" if result["passed"] else "FAIL",
                     result["audit"]["au_executed"], result["audit"]["actions_effective"],
                     result["audit"]["repeat_action"], result["ask_user"],
                     "" if not bad else "  ✗ " + "；".join(bad)))

    regressions = [] if args.no_regression else run_regressions()

    total = len(results)
    passed = sum(1 for item in results if item["passed"])
    ledger = sum(1 for item in results if item["audit"]["iu_eq"])
    eq_ok = sum(1 for item in results if all(item["eq"].values()))
    payload = {"generated_at": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
               "judge": "fake-immediate-complete(0.90/0.55)", "merge_window_ms": 2500,
               "scenes": len(scenes), "variants": variants,
               "summary": {"scenes_passed": "%d/%d" % (passed, total),
                           "ledger_balanced": "%d/%d" % (ledger, total),
                           "seat_eq": "%d/%d" % (eq_ok, total),
                           "regressions": "%d/%d" % (sum(1 for r in regressions if r["ok"]),
                                                     len(regressions))},
               "results": results, "regressions": regressions}
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print("\nA2 指代/作用域：%d/%d PASS｜A6 账目：%d/%d 平｜席级 eq：%d/%d"
          % (passed, total, ledger, total, eq_ok, total))
    if regressions:
        print("既有回归：%d/%d 不变"
              % (sum(1 for r in regressions if r["ok"]), len(regressions)))
    print("证据：%s" % out)
    return 0 if (passed == total and all(r["ok"] for r in regressions)) else 1


if __name__ == "__main__":
    sys.exit(main())
