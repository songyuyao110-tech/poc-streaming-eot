"""写记忆前的快判断（`memory/write_judge.py`）离线契约：不连 8009，只验请求形状 / 解析 / 离线替身。

口径来源：`tmp/poc-neixinxi-memory-write-验收.md` 第 6、7 条与「离线 / 非麦（必须过）」：

- 用**判停同一套模型 / 同一个 8009 服务**（`/v1/systemone`、`think_mode: "never"`、不带 `model`），
  另准备一份短稿，只做是非：这份内容有没有必要写进记忆；
- 稿里必须约束「应该写 / 不应该写」两类（逐字照验收第 6 条）；分数线是临时线，不是已拍的正式线；
- 判断失败 / 超时 / 分不清 → 宁可不写（调方抛错，不回落编一刀）；
- 离线替身（默认 `?eot=` 不是 real 时）本地规则：太短 / 纯应声不写；并成完整且有有效信息的才写。

运行：`python3 -B tests/memory/test_memory_write_judge.py`（纯离线）
"""
from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))
sys.dont_write_bytecode = True

from memory import write_judge  # noqa: E402


class FakeResponse:
    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self.content = json.dumps(body or {}, ensure_ascii=False).encode("utf-8")


class FakeClient:
    """只记账的 HTTP 客户端：`posts` 里留每次 URL / 请求体，测试直接看送了什么。"""

    def __init__(self, status_code=200, body=None):
        self.status_code = status_code
        self.body = body
        self.posts = []

    def post(self, url, json=None):
        self.posts.append({"url": url, "json": json})
        return FakeResponse(self.status_code, self.body)


def answer(choice="write", write=0.9, skip=0.1, confidence=0.9):
    return {"answers": {"memory_write": {
        "type": "choice", "choice": choice, "confidence": confidence,
        "probabilities": {"write": write, "skip": skip}}}}


class RequestShape(unittest.TestCase):
    """[1] 请求形状：判停同一套（choice / think_mode never / 不带 model / 同一个 systemone 端点）。"""

    def test_body_is_a_two_choice_question_on_the_same_service(self):
        client = FakeClient(body=answer())
        write_judge.judge_memory_write("张先生，我给您介绍一下。", seat="D",
                                       scene={"summary": "副驾刚说「张先生…」"},
                                       prev="", client=client, base="http://127.0.0.1:8123")
        self.assertEqual(len(client.posts), 1)
        post = client.posts[0]
        self.assertEqual(urlsplit(post["url"]).path, "/v1/systemone",
                         "与判停同一个 8009 /v1/systemone")
        body = post["json"]
        self.assertEqual(body["think_mode"], "never", "关思考（与判停同一套口径）")
        self.assertNotIn("model", body, "请求不带 model（服务端只有一个模型）")
        self.assertEqual(set(body["questions"]), {"memory_write"})
        question = body["questions"]["memory_write"]
        self.assertEqual(question["type"], "choice", "只做是非（写 / 不写）")
        self.assertEqual(set(question["criteria"]), {"write", "skip"})
        self.assertIn("人名对上了哪一席", question["instructions"])
        self.assertIn("明确的事实或约定", question["instructions"])
        self.assertIn("当场纠正了谁是谁", question["instructions"])
        self.assertIn("有新的有效信息", question["instructions"])
        self.assertIn("单字或半句不成意思", question["instructions"])
        self.assertIn("和上一刀重复", question["instructions"])
        self.assertIn("分不清是谁说的", question["instructions"])
        self.assertIn("「哦」「嗯」", question["instructions"])
        self.assertEqual(body["state"]["content"], "张先生，我给您介绍一下。")
        self.assertEqual(body["state"]["seat"], "主驾")
        self.assertIn("副驾刚说", body["state"]["scene"])

    def test_prev_cut_is_put_into_the_state(self):
        client = FakeClient(body=answer(choice="skip", write=0.1, skip=0.9))
        write_judge.judge_memory_write("搞好了还蛮有意思", seat="P", prev="搞好了还蛮有意思",
                                       client=client, base="http://127.0.0.1:8123")
        self.assertEqual(client.posts[0]["json"]["state"]["last_memory"], "搞好了还蛮有意思")
        self.assertEqual(client.posts[0]["json"]["state"]["seat"], "副驾")


class Verdict(unittest.TestCase):
    """[2] 判定：只认「模型也选写」且分数过临时线；失败 / 超时照验收第 7 条不编记忆。"""

    def test_choice_write_above_line_writes(self):
        client = FakeClient(body=answer(choice="write", write=0.8, skip=0.2))
        out = write_judge.judge_memory_write("副驾是张先生", seat="P", client=client,
                                             base="http://127.0.0.1:8123")
        self.assertTrue(out["write"])
        self.assertEqual(out["mode"], "kev_choice")
        self.assertAlmostEqual(out["score"], 0.8)
        self.assertEqual(out["threshold"], write_judge.MEMORY_WRITE_MIN_SCORE)
        self.assertIn("未拍", out["threshold_note"], "分数线是临时线，不是已拍的正式线")

    def test_choice_skip_never_writes_even_with_high_write_score(self):
        client = FakeClient(body=answer(choice="skip", write=0.7, skip=0.3))
        out = write_judge.judge_memory_write("哦", seat="D", client=client,
                                             base="http://127.0.0.1:8123")
        self.assertFalse(out["write"], "模型选「不写」就不写（不只看分数）")

    def test_write_score_below_line_does_not_write(self):
        client = FakeClient(body=answer(choice="write", write=0.55, skip=0.45))
        out = write_judge.judge_memory_write("今天天气不错", seat="D", client=client,
                                             base="http://127.0.0.1:8123")
        self.assertFalse(out["write"], "宁可不写：分数没过临时线也不写")

    def test_non_200_raises_for_the_caller_to_skip(self):
        client = FakeClient(status_code=503)
        with self.assertRaises(ValueError):
            write_judge.judge_memory_write("张先生", seat="D", client=client,
                                           base="http://127.0.0.1:8123")

    def test_malformed_response_raises(self):
        client = FakeClient(body={"answers": {}})
        with self.assertRaises(ValueError):
            write_judge.judge_memory_write("张先生", seat="D", client=client,
                                           base="http://127.0.0.1:8123")


class OfflineRule(unittest.TestCase):
    """[3] 离线替身（非麦验收不依赖 8009）：太短 / 纯应声 / 重复不写；并成完整且有信息才写。"""

    def test_single_char_and_half_sentence_are_skipped(self):
        for text in ("搞", "我给您介", "然后那个"):
            out = write_judge.rule_memory_write(text, seat="D")
            self.assertFalse(out["write"], "单字 / 半句不成意思：不写（%s）" % text)
            self.assertEqual(out["source"], write_judge.TEMP_SOURCE, "明确标离线替身")

    def test_pure_backchannel_is_skipped(self):
        for text in ("哦", "嗯", "好的。", "知道了"):
            out = write_judge.rule_memory_write(text, seat="P")
            self.assertFalse(out["write"], "纯应声不写（%s）" % text)

    def test_duplicate_of_last_cut_is_skipped(self):
        out = write_judge.rule_memory_write("张先生，我给您介绍一下。", seat="D",
                                            prev="张先生，我给您介绍一下。")
        self.assertFalse(out["write"], "和上一刀重复 / 没有新信息：不写")
        out = write_judge.rule_memory_write("张先生，我给您介绍一下。他姓张。", seat="D",
                                            prev="张先生，我给您介绍一下。")
        self.assertFalse(out["write"], "前缀补全的下一版也不重写")

    def test_merged_complete_sentence_with_information_writes(self):
        for text in ("搞好了还蛮有意思", "张先生，我给您介绍一下。"):
            out = write_judge.rule_memory_write(text, seat="D")
            self.assertTrue(out["write"], "并成完整且有有效信息：写（%s）" % text)
            self.assertEqual(out["mode"], "rules_offline")
            self.assertIn("离线本地规则", out["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
