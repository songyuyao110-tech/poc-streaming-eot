"""双席接 UI 的接线测试：`/api/session/dual/stream` 的契约与「不串席」判据。

覆盖（离线 mock 注入，不连 KEV/LLM，任何机器都能复现）：

    [1] 两路脚本 → 两席各自 CONFIRMED / 决策 / 收口，事件都带 seat_id + mic_id
    [2] 跨席计数 cross_cancel / cross_reset = 0，串席文本 0，而 own_* 非 0（证明计数链路是活的）
    [3] 只给一路 ASR：只该席收口，另一席不出事件、不拖死回放
    [4] 脚本展开 `文本|1500|文本`：停顿切段、时间单调、只有最后一条 final
    [5] 参数与形状错误一律 400（未知席、空 seats、未知 TurnConfig 字段、语速越界）

    /Users/YUYSONG/Work/tmp/jev-reopt-checkouts/dev-streaming-eot/.venv/bin/python -B tests/test_dual_seat_ui.py

（venv 走上游那份：本机 py3.9 缺 httpx2，server.py 会 import 上游 model_clients。）
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "ui"))
sys.path.insert(0, str(ROOT / "src"))
sys.dont_write_bytecode = True

for candidate in ("/Users/YUYSONG/Work/tmp/jev-reopt-checkouts/dev-streaming-eot",
                  "/Users/YUYSONG/Agents/navi-dual-system"):
    if not os.environ.get("POC_UPSTREAM") and (Path(candidate) / "model_clients.py").is_file():
        os.environ["POC_UPSTREAM"] = candidate

import server  # noqa: E402  （import 时会 bootstrap.install() 并装配上游根）

CLIENT = server.app.test_client()


def parse_sse(text):
    """把 SSE 文本拆成 [(event, data), ...]。"""
    events = []
    for packet in text.split("\n\n"):
        packet = packet.strip()
        if not packet:
            continue
        lines = packet.split("\n")
        name = next(line[7:] for line in lines if line.startswith("event: "))
        data = json.loads(next(line[6:] for line in lines if line.startswith("data: ")))
        events.append((name, data))
    return events


def run_dual(payload):
    response = CLIENT.post("/api/session/dual/stream", json=payload)
    if response.status_code != 200:
        raise AssertionError("HTTP %s %s" % (response.status_code, response.get_data(as_text=True)[:400]))
    return parse_sse(response.get_data(as_text=True))


def check(label, condition, detail=""):
    print(("  ok   " if condition else "  FAIL ") + label + (("  | " + str(detail)) if detail and not condition else ""))
    if not condition:
        raise AssertionError(label + ("  | " + str(detail) if detail else ""))


def seat_events(events, seat):
    return [(name, data) for name, data in events if data.get("seat_id") == seat]


# ---------- 1. 两席各自判停 / 决策 / 收口 ----------

def test_two_seats_both_finish():
    print("[1] 两路脚本 → 两席各自 CONFIRMED + 决策 + turn_done")
    events = run_dual({"mock": True, "seats": {"D": "导航回家", "P": "把空调调到24度"}})
    names = [name for name, _ in events]
    check("首个事件是 duo_accepted", names[0] == "duo_accepted", names[:3])
    check("末个事件是 duo_done", names[-1] == "duo_done", names[-3:])
    done = dict(events)["duo_done"]
    check("没有超时", done["timed_out"] is False, done.get("timed_out"))
    for seat in ("D", "P"):
        confirmed = [data for name, data in seat_events(events, seat)
                     if name == "state_change" and data["to"] == "CONFIRMED"]
        decided = [data for name, data in seat_events(events, seat) if name == "llm_decision"]
        ends = [data for name, data in seat_events(events, seat) if name == "turn_done"]
        check("席 %s CONFIRMED 1 次" % seat, len(confirmed) == 1, confirmed)
        check("席 %s 决策 1 次" % seat, len(decided) == 1, decided)
        check("席 %s turn_done" % seat, len(ends) == 1, ends)
        check("席 %s 决策是本席工具" % seat,
              decided and decided[0]["decision"]["tool"] ==
              ("car_navi_to_destination" if seat == "D" else "car_hvac_set_temp"),
              decided)
        check("席 %s 事件带本席 mic_id" % seat,
              all(data.get("mic_id") == "mic-" + seat for _, data in seat_events(events, seat)))
    check("每次确认都带 confirm_latency_ms（双席口径）",
          all(isinstance(data.get("confirm_latency_ms"), (int, float))
              for _, data in events if data.get("event") == "state_change" and data.get("to") == "CONFIRMED"))
    check("plan 只含两席", sorted(done["plan"]) == ["D", "P"], list(done["plan"]))
    return events


def test_no_cross_talk(events):
    print("[2] 跨席计数 0 / 串席文本 0，而 own_* 非 0")
    summary = dict(events)["duo_done"]["summary"]
    texts = summary["seat_texts"]
    check("席 D 只消费自己那路", texts["D"] == ["导航回家"], texts["D"])
    check("席 P 只消费自己那路", texts["P"] == ["把空调调到24度"], texts["P"])
    check("cross_cancel = 0", summary["cross_cancel"] == 0, summary["cross_cancel"])
    check("cross_reset = 0", summary["cross_reset"] == 0, summary["cross_reset"])
    check("own_cancel > 0（计数链路是活的）", summary["own_cancel"] > 0, summary["own_cancel"])
    check("own_reset > 0", summary["own_reset"] > 0, summary["own_reset"])
    check("每席各 1 次决策 / 各 1 段消费", summary["actions"] == 2 and summary["deposits"] == 2, summary)
    check("无错误事件", summary["errors"] == 0 and not [n for n, _ in events if n == "error"])
    check("两席确认时延都已记录",
          len(summary["confirm_latency_ms"]["D"]) == 1 and len(summary["confirm_latency_ms"]["P"]) == 1,
          summary["confirm_latency_ms"])
    # 事件层面也校一遍：任何一条事件都不该把另一席的文本带进来
    for seat, foreign in (("D", "空调"), ("P", "导航")):
        leaked = [data for name, data in seat_events(events, seat)
                  if name in ("asr_update", "segment_consumed") and foreign in str(data.get("text", ""))]
        check("席 %s 的事件里没有另一席的内容" % seat, not leaked, leaked[:2])


# ---------- 3. 单路 ASR ----------

def test_single_channel_only():
    print("[3] 只给席 P 一路 ASR → 只有 P 收口，D 不出事件也不拖死回放")
    events = run_dual({"mock": True, "seats": {"P": "导航去公司"}})
    done = dict(events)["duo_done"]
    check("没有超时（没有让空席拖死）", done["timed_out"] is False, done.get("timed_out"))
    check("plan 只有 P", list(done["plan"]) == ["P"], list(done["plan"]))
    check("席 D 一条事件都没有", not seat_events(events, "D"), seat_events(events, "D")[:2])
    check("席 P 正常收口", len([1 for name, _ in seat_events(events, "P") if name == "turn_done"]) == 1)
    summary = done["summary"]
    check("席 D 没有消费文本", summary["seat_texts"]["D"] == [], summary["seat_texts"])
    check("席 P 消费自己的文本", summary["seat_texts"]["P"] == ["导航去公司"], summary["seat_texts"])


# ---------- 4. 脚本展开 ----------

def test_script_expansion():
    print("[4] `文本|1500|文本` 展开：停顿切段、时间单调、只有最后一条 final")
    updates, vad = server._script_timeline("帮我导航到|1500|人民广场", speed=10.0)
    check("两条 ASR 段（停顿后重新累计 plain）", len(updates) == 5 + 4, [u["text"] for u in updates])
    check("第一段文本 = 帮我导航到", updates[4]["text"] == "帮我导航到", updates[4])
    check("第二段文本只含后半句", updates[-1]["text"] == "人民广场", updates[-1])
    check("时间单调不减", all(a["t_ms"] <= b["t_ms"] for a, b in zip(updates, updates[1:])))
    check("只有最后一条 final", [u["final"] for u in updates].count(True) == 1 and updates[-1]["final"])
    first_end = [v for v in vad if v["kind"] == "speech_end"][0]["t_ms"]
    second_start = [v for v in vad if v["kind"] == "speech_start"][1]["t_ms"]
    check("停顿 1500ms 进 VAD（speech_end → 1500ms → speech_start）",
          second_start - first_end == 1500, (first_end, second_start))
    check("VAD 以 speech_end 收尾", vad[-1]["kind"] == "speech_end" and vad[-1]["t_ms"] > updates[-1]["t_ms"])
    timeline, plan = server._dual_timeline({"seats": {"D": "导航回家", "P": "调低音量"}})
    check("同刻 D 先派发", all(
        (a["t_ms"], 0 if a["seat"] == "D" else 1) <= (b["t_ms"], 0 if b["seat"] == "D" else 1)
        for a, b in zip(timeline, timeline[1:])))
    check("plan 记录了 mic_id 与条数", plan["D"]["mic_id"] == "mic-D" and plan["P"]["updates"] == 4, plan)


# ---------- 5. 参数错误 ----------

def test_bad_payloads():
    print("[5] 参数/形状错误一律 400（不建会话、不跑线程）")
    cases = [
        ("空 seats", {"seats": {}}),
        ("缺 seats", {}),
        ("未知席", {"seats": {"X": "导航回家"}}),
        ("空文本", {"seats": {"D": "   "}}),
        ("未知 TurnConfig 字段", {"seats": {"D": "导航回家"}, "config": {"not_a_field": 1}}),
        ("语速越界", {"seats": {"D": {"script": "导航回家", "speed": 99}}}),
        ("updates 不是数组", {"seats": {"D": {"updates": "导航回家"}}}),
    ]
    for label, payload in cases:
        response = CLIENT.post("/api/session/dual/stream", json=payload)
        check(label + " → 400", response.status_code == 400,
              "%s %s" % (response.status_code, response.get_data(as_text=True)[:160]))


CASES = [test_two_seats_both_finish, test_no_cross_talk, test_single_channel_only,
         test_script_expansion, test_bad_payloads]


def main():
    failures = []
    events = None
    for case in CASES:
        try:
            if case is test_no_cross_talk:
                case(events)
            elif case is test_two_seats_both_finish:
                events = case()
            else:
                case()
        except AssertionError as error:
            print("  FAIL " + str(error))
            failures.append((case.__name__, str(error)))
    print("\n%d/%d 例通过" % (len(CASES) - len(failures), len(CASES)))
    if failures:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
