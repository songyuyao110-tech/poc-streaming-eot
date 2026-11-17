"""真麦复现（2026-11-11 11:36–11:38 那一轮）钉死：被消费的字不许叠到后一句开头。

火山在同一个 ASR 会话里不切 utterance 时，会把**整段话**反复重发。改动前 `_reset_turn` 把
`consumed_text` 清掉，重发里已经消费过的前缀就当成了新话：后一句气泡开头叠着前一句，
判停 / 接不接也把旧话一起判（证据 `duo-mic-20261111-113629-d9ffe7-D/timeline.json`）。

现在跨轮记住「这段语音已经消费到哪」：新的一轮只认它后面的新字；整句就是已消费前缀本身时
按重复跳过（不重开一轮、不重判一次）；用户真正新开口（不以已消费前缀开头）时不误剪。

    python tests/test_turn_reopen_prefix.py
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))
from app import bootstrap
bootstrap.install()
sys.dont_write_bytecode = True
from gate.turn_session import TurnConfig, TurnSession  # noqa: E402
# 与线上默认同口径：决策落地即关回合（`close_turn_on_decision=True`），判停用固定替身，
# 让「消费 → 收口 → 再开口」这条路径完全离线可复现。
CFG = {"speculative_llm": False, "confirm_silence_ms": 300, "max_silence_ms": 1500,
       "debounce_ms": 0, "shortlist_reissue": False, "warm_llm": False, "eot_max_inflight": 1,
       "tools_max_inflight": 1, "eot_policy": "argmax", "tool_set": "core_score",
       "close_turn_on_decision": True}
EOT_STUB = {"complete": 0.9, "cut": 0.05, "wait": 0.05, "label": "complete", "margin": 0.9,
            "wall_ms": 1.0, "kev_ms": 1.0}
STEP = 20


class Harness:
    def __init__(self, session_id):
        self.now = 0.0
        self.events = []
        self.decide_inputs = []
        self.session = TurnSession(TurnConfig.from_dict(CFG), self.events.append,
                                   eot_fn=lambda text: dict(EOT_STUB),
                                   select_tools_fn=self._tools, decide_fn=self._decide,
                                   session_id=session_id)

    @staticmethod
    def _tools(text, tool_set, context):
        return {"scores": {"tool:car_navi_to_destination": 0.9},
                "selected": ["car_navi_to_destination"],
                "schemas": [{"name": "car_navi_to_destination", "description": "d",
                             "parameters": {"type": "object"}}],
                "native_schemas": None, "domains": None, "timing": {"total_ms": 1.0}}

    def _decide(self, utterance, selected, schemas, tool_set, context, **kwargs):
        self.decide_inputs.append(utterance)
        return {"decision": {"kind": "tool_call", "tool": "car_navi_to_destination", "arguments": {}},
                "llm_ms": 1.0, "log_fields": {"decision_head": "stub"}}

    def pump(self, t_ms):
        while self.now < t_ms and not self.session.turn_done:
            self.now = min(float(t_ms), self.now + STEP)
            self.session.tick(self.now)
            time.sleep(0.002)

    def pump_open(self, t_ms):
        while self.now < t_ms:
            self.now = min(float(t_ms), self.now + STEP)
            self.session.tick(self.now)
            time.sleep(0.002)

    def named(self, name):
        return [event for event in self.events if event["event"] == name]

    def last(self, name):
        items = self.named(name)
        return items[-1] if items else {}

    def close(self):
        self.session.close()


def check(name, ok, note=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {name} {note}")
    return bool(ok)


def main():
    print("=" * 100)
    print("P1 · 第一句被消费后收口；同一段话接着重发 → 新一轮只留没消费过的新字")
    h = Harness("prefix-carry-1")
    h.session.on_vad(0, "speech_start")
    h.session.on_asr(100, "大概", False)
    h.session.on_asr(300, "大概座椅按摩", False)
    h.pump(800)                       # 判停「说完」→ 开闸 → 决策落地 → 消费 → 收口
    done = h.named("turn_done")
    results = [
        check("P1 第一句被消费（consumed=1）", h.session.consumed_count == 1,
              f"consumed={h.session.consumed_count}"),
        check("P1 决策拿到的就是这句原话", h.decide_inputs[-1:] == ["大概座椅按摩"],
              f"{h.decide_inputs}"),
        check("P1 回合收口", bool(done), f"done={bool(done)}"),
    ]

    # 火山整段重发：开头还带着已消费的「大概座椅按摩」
    h.session.on_asr(5000, "大概座椅按摩嗯今天的天气还不错", False)
    reopen = h.last("turn_reopen")
    update = h.last("asr_update")
    results += [
        check("P1 重发算新开口（turn_reopen）", bool(reopen), f"{reopen.get('why')}"),
        check("P1 新一轮只显示新字（气泡不再叠前一句）",
              update.get("text") == "嗯今天的天气还不错", f"{update.get('text')!r}"),
        check("P1 turn_text 也不带已消费前缀",
              update.get("turn_text") == "嗯今天的天气还不错", f"{update.get('turn_text')!r}"),
        check("P1 判停输入只剩新话（不把座椅按摩一起判）",
              h.session.eot_input() == "嗯今天的天气还不错", f"{h.session.eot_input()!r}"),
        check("P1 接不接那一路也只剩新话", h.session.suffix_text() == "嗯今天的天气还不错",
              f"{h.session.suffix_text()!r}"),
        check("P1 版本单调递增、不回到 1", h.session.version > 2, f"version={h.session.version}"),
    ]

    print("=" * 100)
    print("P2 · 第二句再被消费 → 第三轮再剪一次（累计前缀，不叠两次）")
    h.pump_open(9000)                 # 第二句「说完且要接」→ 决策 → 消费 → 收口
    results += [
        check("P2 第二句也进了 Agent（decide 输入是新话）",
              h.decide_inputs[-1:] == ["嗯今天的天气还不错"], f"{h.decide_inputs}"),
    ]
    # 第二次整段重发：前缀更长，含前两句
    h.session.on_asr(12000, "大概座椅按摩嗯今天的天气还不错我说完了", False)
    update = h.last("asr_update")
    results += [
        check("P2 第三轮只剩「我说完了」", update.get("text") == "我说完了", f"{update.get('text')!r}"),
        check("P2 累计前缀没有互相吃掉（判停输入）", h.session.eot_input() == "我说完了",
              f"{h.session.eot_input()!r}"),
        check("P2 整句就是已消费前缀的重发 → 跳过、不重开一轮",
              (h.session.on_asr(12300, "大概座椅按摩嗯今天的天气还不错", True) or True)
              and h.last("eot_skipped").get("reason") == "consumed_duplicate"
              and h.session.suffix_text() == "我说完了",
              f"{h.last('eot_skipped').get('reason')} suffix={h.session.suffix_text()!r}"),
    ]

    print("=" * 100)
    print("P3 · 用户真正新开口（不以已消费前缀开头）→ 不误剪、前缀记忆归零")
    h.session.on_asr(20000, "帮我查一下今天的天气", False)
    update = h.last("asr_update")
    results += [
        check("P3 新话原样保留（整句都算新字）", update.get("text") == "帮我查一下今天的天气",
              f"{update.get('text')!r}"),
        check("P3 前缀记忆归零（下次重发不会被旧前缀误剪）",
              h.session.utterance_consumed_text == "", f"{h.session.utterance_consumed_text!r}"),
    ]
    h.close()

    print("=" * 100)
    passed = all(results)
    print(f"跨轮前缀 RESULT: {'PASS' if passed else 'FAIL'} ({sum(results)}/{len(results)})")
    raise SystemExit(0 if passed else 1)


if __name__ == "__main__":
    main()
