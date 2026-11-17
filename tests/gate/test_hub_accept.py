"""中枢「接不接」真判断（8009 /v1/systemone，noul 是非分数）离线验收。

不连外部服务：假 HTTP 客户端记录请求体、返回构造好的响应。钉住
`tmp/poc-mind-accept-judge-验收.md` 的可验部分：

1. 题面是「是不是在对车说」的是非判断（noul，0..1），与判停同一套服务/模型；
2. 动态输入带上：这一版出字 + 读心此刻已有的那句场景；没有场景也照常判；
3. 开闸线未拍：显式临时 0.5（可 env 覆盖），界面口径写「未拍板」；
4. 失败 / 超时 / 解析错 → 抛错，不回落关键词规则，不冒充「不接」。

运行：python3 -B tests/test_hub_accept.py
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from gate.hub_accept import (ACCEPT_INSTRUCTIONS, ACCEPT_MIN_SCORE, accept_meta, accept_question,
# noqa: E402

accept_state, build_accept_fn, score_accept)

SCENE = {"summary": "我听到主驾说「导航去公司」，像在对车说。", "revision": 4, "age_ms": 320}


class FakeResponse:
    def __init__(self, payload, status=200):
        self.status_code = status
        self.content = json.dumps(payload).encode("utf-8")
        self.text = self.content.decode("utf-8")

    def json(self):
        return json.loads(self.content)


class FakeClient:
    def __init__(self, score=0.82, payload=None, status=200):
        self.score = score
        self.payload = payload
        self.status = status
        self.bodies = []

    def post(self, url, json=None):
        self.bodies.append({"url": url, "body": json})
        if self.payload is not None:
            return FakeResponse(self.payload, self.status)
        return FakeResponse({
            "model": "StartLux-Decision-v1.1-9B-AutoThink",
            "answers": {"accept": {"type": "noul", "noul": self.score}},
        }, self.status)


class RequestShape(unittest.TestCase):
    def test_question_is_boolean_noul_with_verbatim_draft(self):
        self.assertEqual(accept_question()["type"], "noul", "是非判断：0..1 分数，不是三选一")
        self.assertEqual(accept_question()["instructions"], ACCEPT_INSTRUCTIONS)
        self.assertIn("是不是在对车说", ACCEPT_INSTRUCTIONS)
        self.assertIn("和旁边人聊天、自言自语", ACCEPT_INSTRUCTIONS)

    def test_call_uses_same_service_shape_as_eot(self):
        client = FakeClient(score=0.82)
        score_accept("导航去公司", scene=SCENE, client=client)
        call = client.bodies[0]
        self.assertTrue(call["url"].endswith("/v1/systemone"))
        body = call["body"]
        self.assertEqual(body["think_mode"], "never", "与判停同一套口径：只打分不思考")
        self.assertNotIn("model", body, "服务端只有一个模型，请求不带 model")
        self.assertEqual(set(body["questions"]), {"accept"})
        self.assertEqual(body["questions"]["accept"]["type"], "noul")

    def test_state_carries_utterance_and_scene(self):
        client = FakeClient()
        score_accept("导航去公司", scene=SCENE, client=client)
        state = json.loads(client.bodies[0]["body"]["state"])
        self.assertEqual(state["utterance"], "导航去公司")
        self.assertEqual(state["scene"], SCENE["summary"], "读心那句场景必须真进判据")

    def test_no_scene_still_judges(self):
        client = FakeClient(score=0.4)
        out = score_accept("咱晚上去哪吃", scene=None, client=client)
        state = json.loads(client.bodies[0]["body"]["state"])
        self.assertEqual(state["scene"], "", "没有场景也照常判，不得卡住")
        self.assertFalse(out["accept"])
        self.assertTrue(out["scene_used"]["missing"])


class Threshold(unittest.TestCase):
    def test_temp_line_is_half_and_marked_unpinned(self):
        self.assertEqual(ACCEPT_MIN_SCORE, 0.5)
        meta = accept_meta()
        self.assertFalse(meta["decided"], "开闸线未拍，不得写成已拍板")
        self.assertEqual(meta["threshold_note"], "开闸线未拍，本次临时用 0.5")
        self.assertEqual(meta["threshold"], 0.5)
        self.assertIn("8009", meta["source"])

    def test_score_above_line_accepts_below_declines(self):
        high = score_accept("导航去公司", scene=SCENE, client=FakeClient(score=0.62))
        self.assertTrue(high["accept"])
        self.assertEqual(high["score"], 0.62)
        self.assertEqual(high["confidence"], 0.62, "0..1 的分就是那一个数，页面直接显示")
        self.assertIn("≥", high["reason"])

        low = score_accept("咱晚上去哪吃", scene=SCENE, client=FakeClient(score=0.31))
        self.assertFalse(low["accept"])
        self.assertIn("不接", low["reason"])
        self.assertEqual(low["threshold_note"], "开闸线未拍，本次临时用 0.5")

    def test_explicit_threshold_override(self):
        out = score_accept("也许是导航", scene=None, client=FakeClient(score=0.62), threshold=0.8)
        self.assertFalse(out["accept"], "临时线可覆盖（仍未拍板）")
        self.assertEqual(out["threshold"], 0.8)


class Failures(unittest.TestCase):
    def test_http_error_raises_not_rule_fallback(self):
        with self.assertRaises(ValueError):
            score_accept("导航去公司", client=FakeClient(status=503,
                                                         payload={"error": "down"}))

    def test_malformed_answer_raises(self):
        payload = {"model": "StartLux-Decision-v1.1-9B-AutoThink",
                   "answers": {"accept": {"type": "choice", "choice": "yes"}}}
        with self.assertRaises(ValueError):
            score_accept("导航去公司", client=FakeClient(payload=payload))

    def test_missing_answer_raises(self):
        payload = {"model": "StartLux-Decision-v1.1-9B-AutoThink", "answers": {}}
        with self.assertRaises(ValueError):
            score_accept("导航去公司", client=FakeClient(payload=payload))


class Factory(unittest.TestCase):
    def test_default_factory_is_real_judge(self):
        fn = build_accept_fn()
        self.assertEqual(fn.meta["mode"], "kev_noul")
        self.assertIn("8009", fn.meta["source"])

    def test_rule_factory_is_explicit_offline_only(self):
        fn = build_accept_fn(mode="rule")
        self.assertEqual(fn.meta["mode"], "rule")
        self.assertIn("离线", fn.meta["note"])

    def test_unknown_mode_rejected(self):
        with self.assertRaises(ValueError):
            build_accept_fn(mode="keyword")


if __name__ == "__main__":
    unittest.main(verbosity=2)
