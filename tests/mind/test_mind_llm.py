"""读心真多模态引擎（mind_llm）离线验收：请求形状 / 视觉降级 / 失败分类（不连外部服务）。

运行：python3 -B tests/test_mind_llm.py
"""
from __future__ import annotations

import base64
import json
import os
import sys
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

from mind.mind_llm import (DEFAULT_MODEL, MAX_SCENE_CHARS, MODEL_BY_ID, MODEL_CHOICES,
# noqa: E402

MODEL_DEFAULT_ID, PROMPT_VERSION, REQUEST_INSTRUCTION, SYSTEM_PROMPT, MindEngineError,
                      MindLLMEngine, build_messages, config_for_model, engine_for_model,
                      engine_from_env, frame_info, image_ok, model_choice, resolve_config)

def jpeg(tag: str, size: int = 4096) -> str:
    """一张「页面采好的彩色帧」：读心把这段 data URI 原样送进 image_url。"""
    return "data:image/jpeg;base64," + base64.b64encode((tag.encode() * size))[:size].decode()

FRAME = jpeg("color-frame")
FRAME_W, FRAME_H = 320, 180

GOOD_SENTENCE = "主驾说「导航去望京SOHO」，像在对车说。"


def ok_response(content=GOOD_SENTENCE):
    return {"choices": [{"message": {"content": content},
                         "finish_reason": "stop"}],
            "usage": {"completion_tokens": 60}}


class FakePost:
    """记录请求体；按 `behaviour` 决定返回 / 抛错。"""

    def __init__(self, behaviour=("ok",)):
        self.bodies = []
        self.behaviour = list(behaviour)

    def __call__(self, body):
        self.bodies.append(body)
        action = self.behaviour.pop(0) if len(self.behaviour) > 1 else self.behaviour[0]
        if action == "ok":
            return ok_response()
        if action == "vision_backend":
            raise MindEngineError("vision_backend", "HTTP 500: failed to process inputs: unexpected EOF")
        if action == "http":
            raise MindEngineError("http", "HTTP 503")
        if isinstance(action, dict):
            return action
        raise AssertionError(action)


def engine(post, **overrides):
    config = {"model": DEFAULT_MODEL, "base": "https://example.invalid/v1", "timeout": 5,
              "key": "test-key-not-real", "vision_backoff_s": 600}
    config.update(overrides)
    return MindLLMEngine(config, post=post)


def load(user_text: str, image=FRAME) -> list[dict]:
    return build_messages(user_text, {"brightness": 0.4, "motion": 0.1, "note": "画面平稳",
                                     "has_frame": True}, "D", True, image)


class RequestShape(unittest.TestCase):
    def test_frame_is_sent_as_openai_image_url(self):
        post = FakePost()
        eng = engine(post)
        eng.judge(text="导航去望京SOHO", visual={"brightness": 0.4, "motion": 0.1,
                                                 "note": "画面平稳", "has_frame": True},
                  image=FRAME, image_w=FRAME_W, image_h=FRAME_H)
        body = post.bodies[0]
        self.assertEqual(body["model"], DEFAULT_MODEL, "默认档（deepseek-v4.1-flash）必须写进请求")
        self.assertNotIn("thinking", body, "engine 直接用 config，不套目录里的关思考写法（由 config_for_model 决定）")
        parts = body["messages"][-1]["content"]
        self.assertEqual(parts[0]["type"], "text")
        self.assertEqual(parts[1]["type"], "image_url")
        self.assertEqual(parts[1]["image_url"]["url"], FRAME,
                         "送模型的必须就是页面采好的那张彩色帧（同一段 data URI，不重编码、不转灰阶）")
        self.assertIn("导航去望京SOHO", parts[0]["text"])

    def test_prior_texts_are_sent_as_seat_context(self):
        post = FakePost()
        eng = engine(post)
        eng.judge(text="尽量走高速，别堵在路上", visual={"has_frame": True}, image=FRAME, image_w=FRAME_W, image_h=FRAME_H,
                  prior_texts=["导航去望京SOHO"])
        text = post.bodies[0]["messages"][-1]["content"][0]["text"]
        self.assertIn("前文", text)
        self.assertIn("导航去望京SOHO", text, "本席上句要作为上下文送给模型")
        self.assertIn("尽量走高速", text)

    def test_color_frame_is_the_one_model_gets_and_the_one_reported(self):
        """不做灰阶：送进模型的彩色帧，原样回带（页面照它显示，与模型看到的像素一一对齐）。"""
        self.assertTrue(image_ok(FRAME))
        self.assertTrue(image_ok("data:image/png;base64," + "x" * 10), "PNG 也是同一套透传口径")
        self.assertFalse(image_ok(None))
        self.assertFalse(image_ok("luma: [1,2,3]"), "灰阶数组不再是合法帧")
        self.assertFalse(image_ok("data:image/jpeg;base64," + "x" * 700000), "超长帧挡在入口")
        post = FakePost()
        eng = engine(post)
        out = eng.judge(text="导航去望京SOHO", visual={"has_frame": True},
                        image=FRAME, image_w=FRAME_W, image_h=FRAME_H)
        sent = out["frame"]
        self.assertEqual(sent["data_uri"], FRAME, "回带的就是送出去的那一段 data URI")
        self.assertEqual((sent["w"], sent["h"]), (FRAME_W, FRAME_H))
        self.assertEqual(sent["format"], "jpeg", "彩色 JPEG，不再有灰阶 PNG 放大")
        self.assertGreater(sent["bytes"], 0)

    def test_jpeg_geometry_is_read_from_header_when_page_omits_it(self):
        """页面没带 w/h 时，从 JPEG 的 SOF 里读出尺寸，不拿 16×9 猜。"""
        raw = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00" \
              + b"\xff\xc0\x00\x11\x08\x00\x5a\x01\x40\x03\x01\x22\x00\x02\x11\x01\x03\x11\x01\xff\xd9"
        uri = "data:image/jpeg;base64," + base64.b64encode(raw).decode()
        info = frame_info(uri)
        self.assertEqual((info["w"], info["h"]), (320, 90))

    def test_result_is_that_one_human_sentence(self):
        eng = engine(FakePost())
        out = eng.judge(text="导航去望京SOHO", visual={"has_frame": True}, image=FRAME, image_w=FRAME_W, image_h=FRAME_H)
        result, meta = out["result"], out["engine"]
        self.assertEqual(result["scene"], GOOD_SENTENCE, "产物就是这一句人话场景")
        self.assertLessEqual(len(result["scene"]), MAX_SCENE_CHARS, "不超过 30 个字")
        self.assertNotIn("读心模块", result["scene"])
        self.assertNotIn("need_response", result, "读心不判接不接（归对话中枢）")
        self.assertEqual(meta["name"], DEFAULT_MODEL)
        self.assertEqual(meta["mode"], "litellm_vl")
        self.assertEqual(meta["source"], "llm")
        self.assertEqual(meta["vision"]["status"], "ok")
        self.assertTrue(meta["vision"]["used"])


class VisionDegradation(unittest.TestCase):
    def test_backend_rejects_image_then_text_only_degraded(self):
        post = FakePost(["vision_backend", "ok"])
        eng = engine(post)
        out = eng.judge(text="导航去公司", visual={"has_frame": True}, image=FRAME, image_w=FRAME_W, image_h=FRAME_H)
        self.assertEqual(len(post.bodies), 2, "视觉失败后同一模型 text-only 重试一次")
        self.assertEqual(post.bodies[0]["messages"][-1]["content"][-1]["type"], "image_url")
        self.assertEqual(len(post.bodies[1]["messages"][-1]["content"]), 1, "重试不带图")
        meta = out["engine"]
        self.assertEqual(meta["mode"], "litellm_text_degraded")
        self.assertFalse(meta["fallback"], "降级不是规则回落，但也不是多模态成功")
        self.assertTrue(meta["vision"]["attempted"])
        self.assertFalse(meta["vision"]["used"])
        self.assertEqual(meta["vision"]["status"], "degraded")
        self.assertIn("unexpected EOF", meta["vision"]["error"])
        # 冷却期内不再每轮重试图片（省掉 10s 级无效开销）
        eng.judge(text="导航去公司", visual={"has_frame": True}, image=FRAME, image_w=FRAME_W, image_h=FRAME_H)
        self.assertEqual(len(post.bodies), 3)
        self.assertEqual(len(post.bodies[2]["messages"][-1]["content"]), 1)

    def test_http_error_is_engine_failure_not_success(self):
        eng = engine(FakePost(["http"]))
        with self.assertRaises(MindEngineError) as caught:
            eng.judge(text="导航去公司", visual={"has_frame": True}, image=FRAME, image_w=FRAME_W, image_h=FRAME_H)
        self.assertEqual(caught.exception.kind, "http")
        self.assertEqual(eng.failures, 1)


class SceneSentence(unittest.TestCase):
    def test_prompt_is_draft_e_verbatim(self):
        """2026-11-11 拍板（调通内心戏记人名）：现网默认稿换成**稿 E**，逐字落地、示例不许删。

        稿 E 的正文（含示例）与 `tmp/poc-mind-prompt-E.txt` 同一份；测试里照样逐字对照，
        防止「顺手改写 / 抽掉示例」把已拍的稿换掉。
        """
        draft_e = """你是车舱里的现场旁白。语气轻松有趣，像朋友在旁边嘀咕一句，但不要油腻，也不要嘲讽车上的人。根据画面和正在说的话，写出此刻最有意思的一点。

人名、称呼能对上哪一席时，要写清那一席是谁；被叫到的那位才是这个人，别把名字算在开口的那一席头上。对得上就点明，对不上或只是电话那头、后排的人，就别把名字安到主驾或副驾身上。原话听着像在叫身边的人、其实对方已经否认或另有所指，按能定准的写，别跟着叫错的那句走。前面已经点过谁是谁，这一句可以接着用，但不要写成「主驾是谁、副驾是谁」这种对号入座。

不要写成「我看到」「我听到」「我知道」，不要第一人称。不要介绍自己，不要提到任何模块名称。不要判断这句话有没有说完，也不要判断要不要接话。只输出一句短旁白，不要解释。

例如：

主驾说：「张先生，我给您介绍一下。」副驾说：「好的。」
→ 主驾正给副驾的张先生做介绍，张先生应了声好。

副驾说：「李总这车开得稳。」主驾说：「路况还行。」
→ 副驾夸主驾李总开得稳，李总自己倒先乐了。

主驾说：「张先生堵在三环了。」副驾说：「那就再等一会儿。」
→ 主驾说张先生堵在三环，副驾让先等一会儿。

主驾说：「张先生，把空调调低点。」副驾说：「我不是，张先生在后排。」
→ 主驾对着副驾喊张先生，正主其实在后排。

主驾说：「王总，您这趟辛苦了。」副驾说：「李总客气了，您开车注意安全。」
→ 李总正跟副驾的王总互相客气。"""
        self.assertEqual(SYSTEM_PROMPT, draft_e, "稿 E 逐字落地（不改写、不删示例）")
        self.assertEqual(PROMPT_VERSION, "v05")
        self.assertIn("张先生，我给您介绍一下", SYSTEM_PROMPT, "示例里「副驾是张先生」那条要留着")
        self.assertIn("张先生在后排", SYSTEM_PROMPT, "示例里「别跟着叫错的那句走」那条要留着")
        self.assertNotIn("读心模块", SYSTEM_PROMPT, "底稿明确不要让它知道自己是读心模块")
        self.assertIn("不要第一人称", SYSTEM_PROMPT, "只在禁止条款里出现「第一人称」，不作为要求")

    def test_request_tail_instruction_drops_first_person(self):
        """请求末尾那句也跟着换口径：不再写「用第一人称说一句」，30 字上限按现网保留。"""
        self.assertIn(REQUEST_INSTRUCTION, build_messages(
            "导航去公司", {"has_frame": True}, "D", True, image=FRAME)[-1]["content"][0]["text"])
        for banned in ("第一人称", "我看到", "我听到", "我知道"):
            self.assertNotIn(banned, REQUEST_INSTRUCTION, "末尾指令不再示范旧说法")
        self.assertIn("30 个字", REQUEST_INSTRUCTION, "30 字上限维持现网原样（本卡不改长度）")

    def test_system_message_is_sent_verbatim_in_every_request(self):
        """发出去的 system 就是 SYSTEM_PROMPT 原文（多模态 / 纯文本两条请求都一样）。"""
        post = FakePost()
        engine(post).judge(text="导航去公司", visual={"has_frame": True}, image=FRAME,
                           image_w=FRAME_W, image_h=FRAME_H)
        self.assertEqual(post.bodies[0]["messages"][0], {"role": "system", "content": SYSTEM_PROMPT})
        self.assertEqual(post.bodies[0]["messages"][0]["content"], SYSTEM_PROMPT)

    def test_dual_speech_is_two_labelled_lanes_in_one_request(self):
        """两路出字在**同一条请求**里分栏写清（主驾、副驾各一栏；没出字的那一路明写「没出字」）。"""
        speech = {"D": {"text": "那就去那家", "settled": "", "speaking": True},
                  "P": {"text": "", "settled": "咱晚上去哪吃", "speaking": False}}
        messages = build_messages("那就去那家", {"has_frame": True}, "D", False,
                                  image=FRAME, speech=speech)
        part = messages[-1]["content"][0]["text"]
        self.assertIn("这一刻只有主驾在出字", part)
        self.assertIn("主驾（D）：这一刻出字「那就去那家」（还在说）", part)
        self.assertIn("副驾（P）：这一刻没出字；刚说完「咱晚上去哪吃」", part)
        self.assertEqual(len(messages[-1]["content"]), 2, "两路文本 + 同一张帧：仍然只有一条请求")

    def test_lane_without_speech_is_left_empty_not_invented(self):
        speech = {"D": {"text": "", "settled": "", "speaking": False},
                  "P": {"text": "", "settled": "", "speaking": False}}
        part = build_messages("", {"has_frame": True}, "D", False, image=FRAME, speech=speech)[-1]["content"][0]["text"]
        self.assertIn("这一刻两路都没出字（只凭画面读）", part)
        self.assertIn("主驾（D）：没出字", part)
        self.assertIn("副驾（P）：没出字", part)
        self.assertNotIn("「", part.split("画面统计")[0].replace("（D）", ""), "没出字那一路不编一句人话")

    def test_long_sentence_is_cut_to_30(self):
        long_sentence = "主驾在说今天我们要先去望京SOHO然后再去三里屯吃饭"
        post = FakePost([{"choices": [{"message": {"content": long_sentence},
                                       "finish_reason": "stop"}]}])
        scene = engine(post).judge(text="x", visual={"has_frame": True}, image=FRAME, image_w=FRAME_W, image_h=FRAME_H)["result"]["scene"]
        self.assertLessEqual(len(scene), MAX_SCENE_CHARS)
        self.assertTrue(long_sentence.startswith(scene[:10]))

    def test_module_self_reference_is_removed(self):
        post = FakePost([{"choices": [{"message": {"content": "读心模块：主驾在聊天"},
                                       "finish_reason": "stop"}]}])
        scene = engine(post).judge(text="x", visual={"has_frame": True}, image=FRAME, image_w=FRAME_W, image_h=FRAME_H)["result"]["scene"]
        self.assertNotIn("读心模块", scene)
        self.assertIn("主驾在聊天", scene)


class ParseFailures(unittest.TestCase):
    def test_only_self_reference_is_bad_output(self):
        post = FakePost([{"choices": [{"message": {"content": "我是车载读心模块感知引擎"},
                                       "finish_reason": "stop"}]}])
        with self.assertRaises(MindEngineError) as caught:
            engine(post).judge(text="导航去公司", visual={"has_frame": True}, image=FRAME, image_w=FRAME_W, image_h=FRAME_H)
        self.assertEqual(caught.exception.kind, "bad_output")

    def test_fenced_json_scene_is_accepted_for_compat(self):
        post = FakePost([{"choices": [{"message": {"content":
            "```json\n" + json.dumps({"scene": "主驾在导航"}, ensure_ascii=False) + "\n```"},
                                       "finish_reason": "stop"}]}])
        result = engine(post).judge(text="导航", visual={"has_frame": True}, image=FRAME, image_w=FRAME_W, image_h=FRAME_H)["result"]
        self.assertEqual(result["scene"], "主驾在导航")

    def test_reasoning_content_fallback_and_empty_failure(self):
        post = FakePost([{"choices": [{"message": {"content": "", "reasoning_content": GOOD_SENTENCE},
                                       "finish_reason": "stop"}]}])
        result = engine(post).judge(text="导航", visual={"has_frame": True}, image=FRAME, image_w=FRAME_W, image_h=FRAME_H)["result"]
        self.assertEqual(result["scene"], GOOD_SENTENCE)

        post = FakePost([{"choices": [{"message": {"content": "", "reasoning_content": ""},
                                       "finish_reason": "length"}]}])
        with self.assertRaises(MindEngineError) as caught:
            engine(post).judge(text="导航", visual={"has_frame": True}, image=FRAME, image_w=FRAME_W, image_h=FRAME_H)
        self.assertEqual(caught.exception.kind, "empty_content")


class Configuration(unittest.TestCase):
    def test_engine_from_env_honours_explicit_rule(self):
        with patch.dict(os.environ, {"MIND_ENGINE": "rule", "LITELLM_API_KEY": "x"}, clear=False):
            self.assertIsNone(engine_from_env())

    def test_missing_credentials_returns_none_for_auto(self):
        env = {k: v for k, v in os.environ.items() if k != "LITELLM_API_KEY"}
        env["MIND_ENGINE"] = "auto"
        env["MIND_LLM_CREDENTIALS"] = "/nonexistent/credentials.yaml"
        with patch.dict(os.environ, env, clear=True):
            self.assertIsNone(engine_from_env())
            self.assertIsNone(resolve_config())

    def test_explicit_litellm_without_credentials_raises_config(self):
        env = {k: v for k, v in os.environ.items() if k != "LITELLM_API_KEY"}
        env["MIND_ENGINE"] = "litellm"
        env["MIND_LLM_CREDENTIALS"] = "/nonexistent/credentials.yaml"
        with patch.dict(os.environ, env, clear=True):
            with self.assertRaises(MindEngineError) as caught:
                engine_from_env()
            self.assertEqual(caught.exception.kind, "config")

    def test_defaults_are_deepseek_with_probe_limits(self):
        env = {k: v for k, v in os.environ.items()
               if k not in {"MIND_LLM_MODEL", "MIND_LLM_TIMEOUT", "MIND_LLM_MAX_TOKENS"}}
        env["LITELLM_API_KEY"] = "x"
        with patch.dict(os.environ, env, clear=True):
            config = resolve_config()
        self.assertEqual(config["model"], "deepseek-v4.1-flash", "2026-11-11 用户拍：默认换成 deepseek")
        self.assertEqual(config["thinking"], {"type": "disabled"}, "默认这一档仍按目录口径关思考")
        self.assertEqual(config["timeout"], 30.0, "三档实测中位见 DETAIL（最慢一档尾部 13.7s）；30s 留余量")
        self.assertEqual(config["max_tokens"], 768, "验收口径 512～768；该模型 completion 实测 181～260 token")

    def test_model_env_override(self):
        with patch.dict(os.environ, {"LITELLM_API_KEY": "x", "MIND_LLM_MODEL": "qwen3-vl:8b"}, clear=False):
            config = resolve_config()
        self.assertEqual(config["model"], "qwen3-vl:8b", "环境变量仍可临时覆盖（本单不删开关）")


class ModelPicker(unittest.TestCase):
    """页面三选一（本单）：目录 / 思考开关只对 deepseek 加 / 按 id 建引擎 / 视觉能力标注。"""

    def test_catalog_is_exactly_three_choices(self):
        self.assertEqual([item["id"] for item in MODEL_CHOICES],
                         ["cc/glm-5.2", "cc/glm-5.3-flash", "deepseek-v4.1-flash"],
                         "不扩选其它模型：页面只有这三档")
        self.assertEqual(MODEL_DEFAULT_ID, "deepseek-v4.1-flash", "打开页面先用线上默认 deepseek-v4.1-flash")
        self.assertEqual([item["id"] for item in MODEL_CHOICES if item["default"]],
                         ["deepseek-v4.1-flash"], "默认标记只在那一档（另两档仍可选，不带默认标记）")
        self.assertEqual(set(MODEL_BY_ID), {item["id"] for item in MODEL_CHOICES})

    def test_catalog_labels_vision_and_speed(self):
        for item in MODEL_CHOICES:
            self.assertIn("speed_label", item, "选项要能看出快慢")
            self.assertIn("vision_label", item, "选项要能看出能不能读图")
        self.assertEqual(model_choice("cc/glm-5.2")["vision"], "unavailable",
                         "glm-5.2 读不懂图，必须标视觉不可用")
        for mid in ("cc/glm-5.3-flash", "deepseek-v4.1-flash"):
            self.assertEqual(model_choice(mid)["vision"], "ok")

    def test_thinking_only_on_deepseek(self):
        self.assertEqual(model_choice("deepseek-v4.1-flash")["thinking"], {"type": "disabled"})
        self.assertIsNone(model_choice("cc/glm-5.2")["thinking"])
        self.assertIsNone(model_choice("cc/glm-5.3-flash")["thinking"],
                          "5.3-flash 始终思考、后端 1210 不支持关闭，不能照搬 DeepSeek 写法")

    def test_request_body_thinking_only_for_deepseek(self):
        env = {"LITELLM_API_KEY": "x", "MIND_LLM_CREDENTIALS": "/nonexistent/credentials.yaml"}
        with patch.dict(os.environ, env, clear=False):
            for mid, expect_thinking in (("cc/glm-5.2", False),
                                         ("cc/glm-5.3-flash", False),
                                         ("deepseek-v4.1-flash", True)):
                post = FakePost()
                eng = engine_for_model(mid)
                eng._post = post
                eng.judge(text="导航去公司", visual={"has_frame": True}, image=FRAME, image_w=FRAME_W, image_h=FRAME_H)
                body = post.bodies[0]
                self.assertEqual(body["model"], mid, "请求必须打到页面选的那一档")
                self.assertEqual("thinking" in body, expect_thinking, mid)
                self.assertEqual(body["max_tokens"], 768, "max_tokens 口径不动（三档一致）")
                self.assertEqual(body["temperature"], 0)

    def test_unknown_model_is_config_error(self):
        with patch.dict(os.environ, {"LITELLM_API_KEY": "x"}, clear=False):
            with self.assertRaises(MindEngineError) as caught:
                engine_for_model("cc/glm-5.9-flash")
        self.assertEqual(caught.exception.kind, "config", "不偷偷换模型，未知 id 明确报错")

    def test_describe_and_envelope_flag_glm52_vision_unavailable(self):
        post = FakePost()
        eng = engine(post, model="cc/glm-5.2")     # 这一档不是默认；本测只钉它的视觉能力标注
        self.assertEqual(eng.describe()["vision"]["capability"], "unavailable")
        out = eng.judge(text="导航去公司", visual={"has_frame": True}, image=FRAME, image_w=FRAME_W, image_h=FRAME_H)
        self.assertEqual(out["engine"]["vision"]["capability"], "unavailable")
        self.assertNotIn("真多模态", out["engine"]["vision"]["note"],
                         "后端 200 但读不懂图，不许再把 vision=ok 冒充多模态成功")
        eng53 = MindLLMEngine({"model": "cc/glm-5.3-flash", "base": "https://example.invalid/v1",
                               "timeout": 5, "key": "test-key-not-real", "vision_backoff_s": 600},
                              post=FakePost())
        self.assertEqual(eng53.describe()["vision"]["capability"], "ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
