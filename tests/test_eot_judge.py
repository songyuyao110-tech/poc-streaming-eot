"""判停调用契约（eot-judge-api，飞书 2026-11-03）＋文档双阈值当前生效的四个分支。

判停每个 ASR 段只发一次：单个 `choice` 问题 `eot`、`think_mode: "never"`、请求体不带 `model`、
`state = {"utterance", "last_agent_reply"?}`。回合判定不看 argmax：`probabilities.complete >=
EOT_COMPLETE_MIN_SCORE` 才算「说完」，`confidence >= EOT_CONFIDENCE_MIN` 才抢跑（提前起决策）。

A0 要求的四个分支都在这里钉住：强 complete（确认 + 抢跑）／弱 complete（确认但不抢跑，
静默确认后串行决策）／cut／wait。弱 complete 在现有两批语料里没有样本（`-DETAIL` §4.4），
所以只能靠这里的单测固定。

全部 HTTP 是假客户端：不连隧道、不连 GPU、不连车机。
运行：python3 -B tests/test_eot_judge.py   （本仓库无 pytest，用标准库 unittest）
"""
from __future__ import annotations

import json
import os
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from turn_eot import (EOT_CRITERIA, EOT_INSTRUCTIONS, parse_response, request_body,  # noqa: E402
                      score_eot)
from turn_session import TurnConfig, TurnSession, _env_float  # noqa: E402

PROBS_COMPLETE = {"complete": 0.96, "cut": 0.03, "wait": 0.01}
SCHEMA = {"name": "climate_set_power", "description": "开启或关闭座舱空调系统",
          "parameters": {"type": "object", "properties": {"enabled": {"type": "boolean"}},
                         "required": ["enabled"], "additionalProperties": False}}


class FakeResponse:
    def __init__(self, status_code=200, body=None, content=None):
        self.status_code = status_code
        self.content = content if content is not None else json.dumps(body).encode()


class FakeClient:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def post(self, url, json=None):
        self.calls.append({"url": url, "body": json})
        return self.response


def answer(choice="complete", confidence=0.95, probabilities=None, **extra):
    payload = {"type": "choice", "choice": choice, "confidence": confidence,
               "probabilities": dict(probabilities or PROBS_COMPLETE)}
    payload.update(extra)
    return {"answers": {"eot": payload}, "model": "StartLux-Decision-v1.1-9B-AutoThink",
            "usage": {"input_tokens": 277, "output_tokens": 0}}


def verdict(complete=0.96, cut=0.03, wait=0.01, confidence=0.95):
    """一次判停的 result（`score_eot` 的形状，含 argmax label 与 margin）。"""
    scores = {"complete": complete, "cut": cut, "wait": wait}
    return {**scores, "label": max(scores, key=lambda name: scores[name]),
            "confidence": confidence, "margin": complete - max(cut, wait), "wall_ms": 1.0,
            "kev_ms": None}


def select(text, tool_set, context):
    return {"scores": {"tool:climate_set_power": 0.95}, "selected": ["climate_set_power"],
            "schemas": [SCHEMA], "timing": {"stage1_ms": 1.0, "stage2_ms": 1.0, "total_ms": 2.0},
            "native_schemas": [SCHEMA], "domains": []}


def decide(utterance, selected, schemas, tool_set="core_score", context=None, **kwargs):
    return {"decision": {"kind": "tool_call", "tool": "climate_set_power",
                         "arguments": {"enabled": True}},
            "llm_ms": 5.0, "log_fields": {"decision_head": "unit_test"}}


def new_session(eot_fn, **config):
    config.setdefault("debounce_ms", 0)
    config.setdefault("confirm_silence_ms", 700)
    cfg = TurnConfig(**config)
    return TurnSession(cfg, lambda event: None, eot_fn=eot_fn, select_tools_fn=select,
                       decide_fn=decide, evidence_root=None, session_id="judge")


def drive(session, timeline, *, budget_ms=4000):
    """按毫秒时间线喂 ASR / VAD，直到回合结束或预算用完（真实时钟，同 tests/ 其他脚本）。"""
    began = time.monotonic() * 1000
    now = lambda: time.monotonic() * 1000 - began
    for at, text, final in timeline:
        while now() < at and not session.turn_done:
            session.tick(now())
            time.sleep(0.002)
        if session.turn_done:
            break
        session.on_asr(at, text, final)
    limit = now() + budget_ms
    while not session.turn_done and now() < limit:
        session.tick(now())
        time.sleep(0.002)
    return session


def events_of(session, name):
    return [event for event in session.events if event["event"] == name]


class RequestShapeTests(unittest.TestCase):
    def test_request_is_the_documented_choice_call(self):
        body = request_body("导航去天安门")
        self.assertEqual(body["think_mode"], "never")
        self.assertEqual(body["state"], {"utterance": "导航去天安门"})
        self.assertNotIn("model", body)
        self.assertEqual(set(body["questions"]), {"eot"})
        question = body["questions"]["eot"]
        self.assertEqual(question["type"], "choice")
        self.assertEqual(question["criteria"], EOT_CRITERIA)
        self.assertEqual(question["instructions"], EOT_INSTRUCTIONS)
        # 文档的三个标签就是 criteria 的键，别的都不许加。
        self.assertEqual(set(question["criteria"]), {"complete", "cut", "wait"})
        self.assertIn("判断方法", question["instructions"])

    def test_last_agent_reply_is_optional(self):
        self.assertNotIn("last_agent_reply", request_body("导航到")["state"])
        body = request_body("好的", "要不要导航回家？")
        self.assertEqual(body["state"]["last_agent_reply"], "要不要导航回家？")

    def test_every_call_goes_to_the_single_systemone_endpoint(self):
        client = FakeClient(FakeResponse(body=answer()))
        result = score_eot("导航到", client=client, base="http://127.0.0.1:8123")
        self.assertEqual(client.calls[0]["url"], "http://127.0.0.1:8123/v1/systemone")
        self.assertEqual(result["label"], "complete")


class ResponseTests(unittest.TestCase):
    def test_the_documented_fields_become_the_turn_verdict(self):
        result = parse_response(answer())
        self.assertEqual(result["label"], "complete")
        self.assertAlmostEqual(result["margin"], 0.93)
        self.assertAlmostEqual(result["confidence"], 0.95)
        self.assertEqual((result["complete"], result["cut"], result["wait"]), (0.96, 0.03, 0.01))
        self.assertEqual(result["model"], "StartLux-Decision-v1.1-9B-AutoThink")

    def test_label_is_the_argmax_and_not_the_reported_choice(self):
        result = parse_response(answer(choice="cut", probabilities={"complete": 0.9, "cut": 0.05,
                                                                    "wait": 0.05}))
        self.assertEqual(result["label"], "complete")
        self.assertEqual(result["choice"], "cut")

    def test_live_extra_fields_are_tolerated(self):
        raw = answer(none_probability=0.004)
        raw["latency_ms"] = 141.2
        self.assertAlmostEqual(parse_response(raw)["complete"], 0.96)

    def test_score_eot_records_wall_and_service_latency(self):
        raw = answer()
        raw["latency_ms"] = 141.2
        result = score_eot("导航去天安门", client=FakeClient(FakeResponse(body=raw)))
        self.assertEqual(result["kev_ms"], 141.2)
        self.assertGreater(result["wall_ms"], 0.0)

    def test_protocol_violations_are_rejected(self):
        good = answer()
        bad = {
            "non-200": None,
            "no-eot": {"answers": {"intent": good["answers"]["eot"]}},
            "extra-answer": {"answers": {"eot": good["answers"]["eot"],
                                         "sentiment": {"type": "choice"}}},
            "wrong-type": {"answers": {"eot": dict(good["answers"]["eot"], type="noul")}},
            "bad-choice": answer(choice="finish"),
            "missing-probability": answer(probabilities={"complete": 0.9, "cut": 0.1}),
            "extra-probability": answer(probabilities={"complete": 0.9, "cut": 0.1, "wait": 0.0,
                                                       "other": 0.0}),
            "probability-over-one": answer(probabilities={"complete": 1.2, "cut": -0.1,
                                                          "wait": 1.0}),
            "probability-nan": answer(probabilities={"complete": float("nan"), "cut": 0.1,
                                                      "wait": 0}),
            "confidence-nan": answer(confidence=float("nan")),
            "confidence-bool": answer(confidence=True),
            "confidence-over-one": answer(confidence=1.5),
            "extra-field": answer(probability=0.9),
        }
        for name, payload in bad.items():
            with self.subTest(name=name):
                with self.assertRaises(ValueError):
                    if name == "non-200":
                        score_eot("导航到", client=FakeClient(FakeResponse(status_code=500,
                                                                         body=answer())))
                    else:
                        parse_response(payload)

    def test_duplicate_json_keys_are_rejected(self):
        raw = (b'{"answers":{"eot":{"type":"choice","choice":"complete","confidence":0.9,'
               b'"probabilities":{"complete":0.9,"cut":0.1,"wait":0.0}}},'
               b'"answers":{"eot":{"type":"choice","choice":"cut","confidence":0.9,'
               b'"probabilities":{"complete":0.0,"cut":0.1,"wait":0.9}}}}')
        with self.assertRaises(ValueError):
            score_eot("导航到", client=FakeClient(FakeResponse(content=raw)))

    def test_non_json_body_is_rejected(self):
        with self.assertRaises(json.JSONDecodeError):
            score_eot("导航到", client=FakeClient(FakeResponse(content=b"not json")))


class PolicyTests(unittest.TestCase):
    """文档规则：complete >= 0.80；抢跑还要求 confidence >= 0.70。"""

    def check(self, scores, expected, **config):
        session = new_session(lambda text: answer(), **config)
        try:
            self.assertEqual(session._eot_verdict(scores), expected)
        finally:
            session.close()

    def test_default_config_is_the_documented_rule(self):
        cfg = TurnConfig()
        self.assertEqual(cfg.eot_policy, "score")
        self.assertEqual((cfg.eot_complete_min, cfg.eot_confidence_min), (0.80, 0.70))

    # ---- A0 四个分支 ----
    def test_strong_complete_confirms_and_starts_early(self):
        self.check(verdict(0.96, 0.03, 0.01, 0.95), ("complete", True, True))

    def test_weak_complete_confirms_without_the_early_start(self):
        # complete 够 0.80，confidence 只有 0.69：判「说完」，但不抢跑（文档的分支）。
        self.check(verdict(0.85, 0.10, 0.05, 0.69), ("complete", True, False))

    def test_below_the_complete_floor_falls_to_cut_or_wait(self):
        self.check(verdict(0.79, 0.15, 0.06, 0.95), ("cut", False, False))
        self.check(verdict(0.30, 0.20, 0.50, 0.95), ("wait", False, False))

    def test_cut_and_wait_below_the_floor_keep_their_label(self):
        self.check(verdict(0.01, 0.99, 0.00, 0.98), ("cut", False, False))
        self.check(verdict(0.13, 0.03, 0.84, 0.76), ("wait", False, False))

    def test_exact_thresholds_pass(self):
        self.check(verdict(0.80, 0.19, 0.01, 0.70), ("complete", True, True))

    def test_argmax_policy_is_kept_for_replay(self):
        self.check(verdict(0.40, 0.35, 0.25, 0.1), ("complete", True, False), eot_policy="argmax")
        self.check({**verdict(0.40, 0.35, 0.25, 0.1), "margin": 0.31},
                   ("complete", True, True), eot_policy="argmax")

    def test_margin_policy_is_kept_for_replay(self):
        self.check({**verdict(0.95, 0.03, 0.02, 0.0), "margin": 0.10},
                   ("cut", False, False), eot_policy="margin")
        self.check({**verdict(0.95, 0.03, 0.02, 0.0), "margin": 0.31},
                   ("complete", True, True), eot_policy="margin")

    def test_unknown_policy_is_rejected_at_config_time(self):
        with self.assertRaises(ValueError):
            TurnConfig(eot_policy="threshold")

    def test_thresholds_accept_the_documented_start_sh_override(self):
        old = os.environ.get("EOT_COMPLETE_MIN_SCORE")
        try:
            os.environ["EOT_COMPLETE_MIN_SCORE"] = "0.9"
            self.assertEqual(_env_float("EOT_COMPLETE_MIN_SCORE", 0.80), 0.9)
            os.environ["EOT_COMPLETE_MIN_SCORE"] = "not-a-number"
            self.assertEqual(_env_float("EOT_COMPLETE_MIN_SCORE", 0.80), 0.80)
        finally:
            if old is None:
                os.environ.pop("EOT_COMPLETE_MIN_SCORE", None)
            else:
                os.environ["EOT_COMPLETE_MIN_SCORE"] = old


class TurnBehaviourTests(unittest.TestCase):
    """四个分支在真实状态机里的落点（不只是 `_eot_verdict` 的返回值）。"""

    def test_strong_complete_confirms_immediately_without_the_confirm_window(self):
        """2026-11-04 拍板：强 complete 当刻认，不再停一档 MAYBE_DONE 等整段 700ms 确认窗。"""
        session = new_session(lambda text: verdict(0.96, 0.03, 0.01, 0.95))
        try:
            drive(session, [(0, "打开空调", True)])
        finally:
            session.close()
        states = [(event["from"], event["to"]) for event in events_of(session, "state_change")]
        self.assertIn(("LISTENING", "CONFIRMED"), states)
        self.assertNotIn(("LISTENING", "MAYBE_DONE"), states, "强 complete 不许先进「可能说完」态")
        confirms = [event for event in events_of(session, "state_change")
                    if event["to"] == "CONFIRMED"]
        self.assertEqual([event["why"] for event in confirms], ["eot_complete"])
        starts = events_of(session, "llm_start")
        self.assertTrue(starts, "强 complete 应当当刻起决策")
        emitted = events_of(session, "eot_scores")[-1]
        self.assertEqual(emitted["applied_label"], "complete")
        self.assertTrue(emitted["may_start_early"])
        # 判完即认：确认与生成启动都不落在 confirm_silence_ms 之后（旧口径恒 ≥700ms）。
        summary = session.summary()
        self.assertLess(summary["t_confirm_latency_ms"], session.cfg.confirm_silence_ms)
        self.assertLess(summary["t_llm_start_latency_ms"], session.cfg.confirm_silence_ms)

    def test_weak_complete_keeps_listening_when_the_user_continues(self):
        """弱 complete 只等约 0.7s：这期间用户接着说 → 回 LISTENING 继续听，半句不被当说完。"""
        session = new_session(lambda text: verdict(0.85, 0.10, 0.05, 0.69))
        try:
            drive(session, [(0, "打开空调", False), (300, "打开空调制热", True)])
        finally:
            session.close()
        states = [(event["from"], event["to"]) for event in events_of(session, "state_change")]
        self.assertIn(("LISTENING", "MAYBE_DONE"), states, "弱 complete 要进「可能说完」态等收敛")
        self.assertIn(("MAYBE_DONE", "LISTENING"), states, "续句要把「可能说完」打回继续听")
        self.assertEqual([event["why"] for event in events_of(session, "state_change")
                          if event["to"] == "CONFIRMED"], ["confirm_silence"])
        # 落地的那次决策用的是续完的整句，不是被打回的那半句。
        self.assertTrue(session.consumptions, "续完的整句仍要落地一次")
        self.assertIn("制热", session.consumptions[0]["text"])

    def test_the_weak_confirm_wait_is_still_capped_by_max_silence(self):
        """「可能说完」这一档的约 0.7s，上限仍是 max_silence_ms(2s)。"""
        session = new_session(lambda text: verdict(0.85, 0.10, 0.05, 0.69),
                              confirm_silence_ms=5000, max_silence_ms=2000)
        try:
            drive(session, [(0, "打开空调", True)])
        finally:
            session.close()
        confirms = [event for event in events_of(session, "state_change")
                    if event["to"] == "CONFIRMED"]
        self.assertEqual([event["why"] for event in confirms], ["max_silence"])

    def test_weak_complete_waits_for_silence_then_decides_serially(self):
        session = new_session(lambda text: verdict(0.85, 0.10, 0.05, 0.69))
        try:
            drive(session, [(0, "打开空调", True)])
        finally:
            session.close()
        starts = events_of(session, "llm_start")
        self.assertTrue(starts, "弱 complete 也应当最终起决策")
        self.assertFalse(starts[0]["speculative"], "弱 complete 不许抢跑")
        confirms = [event for event in events_of(session, "state_change")
                    if event["to"] == "CONFIRMED"]
        self.assertEqual([event["why"] for event in confirms], ["confirm_silence"])
        # 串行：决策发生在 CONFIRMED 之后，不是 MAYBE_DONE 期间。
        self.assertGreaterEqual(starts[0]["t_ms"], confirms[0]["t_ms"])
        emitted = events_of(session, "eot_scores")[-1]
        self.assertTrue(emitted["may_confirm"])
        self.assertFalse(emitted["may_start_early"])

    def test_below_floor_cut_waits_on_the_cut_timer(self):
        session = new_session(lambda text: verdict(0.10, 0.85, 0.05, 0.9))
        try:
            drive(session, [(0, "导航到", True)], budget_ms=1200)
        finally:
            session.close()
        states = [(event["from"], event["to"]) for event in events_of(session, "state_change")]
        self.assertIn(("LISTENING", "CUT_OFF_WAIT"), states)
        self.assertFalse([event for event in events_of(session, "eot_scores")
                          if event["applied_label"] == "complete"])

    def test_below_floor_wait_waits_on_the_think_timer(self):
        session = new_session(lambda text: verdict(0.13, 0.03, 0.84, 0.9))
        try:
            drive(session, [(0, "嗯", True)], budget_ms=800)
        finally:
            session.close()
        states = [(event["from"], event["to"]) for event in events_of(session, "state_change")]
        self.assertIn(("LISTENING", "THINK_WAIT"), states)


if __name__ == "__main__":
    unittest.main(verbosity=2)
