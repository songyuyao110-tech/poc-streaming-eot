"""读心模块的真多模态引擎：litellm OpenAI 兼容 `chat/completions`，页面可在三档间切换。

口径来源：`tmp/poc-mind-model-picker-验收.md`（2026-11-10 用户在界面上选引擎，三选一）＋
`tmp/poc-mind-default-deepseek-验收.md`（2026-11-11 用户拍「默认换成 deepseek」：默认档从
`cc/glm-5.2` 改为 `deepseek-v4.1-flash`，另两档仍可手选、行为不变；规则不得当主路径；失败/超时可
回落规则或明确 error）。三档的实际能力**全部由本单实测决定**（见 `tmp/poc-mind-model-picker-DETAIL.md`）：

- `cc/glm-5.2`（可选；原默认）：文本判断快且准；但**当前端点读不懂图**（图被后端接受、token 照扣，答案却与
  图无关；本单多图多次实测复现，前序「翻转帧验过真吃图」是 2 个样本的偶然命中）。页面按
  **「视觉不可用」**标注，`engine.vision.capability=unavailable`，不再显示成「真多模态」。
- `cc/glm-5.3-flash`：能读懂图；**始终思考、不支持关闭**（`"thinking": {"type": "disabled"}` 被后端
  400 `[1210] 该模型始终思考，不支持关闭思考`），所以请求体**不加** thinking 开关，时延按实测标「慢」。
- `deepseek-v4.1-flash`（**默认**）：能读懂图；**只对这一档加** `"thinking": {"type": "disabled"}`
  （不套到另外两个模型）。默认开思考时中位 3.4～3.9s，关思考后墙钟见 DETAIL。

共同口径一律不动：超时 30s、`max_tokens` 768。**画面口径（2026-11-11 11:53 拍板）**：送进模型的就是
页面采到并压好的那张**彩色 JPEG**（默认 320×180，原图 16:9；页面把同一段 data URI 交给 8765，本模块
原样拼进 `image_url`，不二次编码、不转灰阶）。先前「16×9 灰阶 → 160×90 最近邻放大」的口径作废：
信息量太少，模型只会说「画面很模糊」。2026-11-10 16:54 起
**产物改成一句 ≤30 字的场景**（不自称模块、不判说完、不判接不接）；2026-11-11 16:57 用户拍板把
**固定稿换成稿 E**（2026-11-11 拍板「调通内心戏记人名」）：在轻松口吻之外把「人名 / 称呼对席」写清楚
（被叫到的那位才是这个人，别算在开口那一席头上），底稿逐字照 `tmp/poc-mind-prompt-E.txt`
（含示例，不删），见 `SYSTEM_PROMPT`；请求末尾指令 `REQUEST_INSTRUCTION` 保持不动。
`MIND_LLM_MODEL` 仍可临时覆盖（默认 `deepseek-v4.1-flash`）；页面按请求带模型 id，
服务端按 id 取引擎（`engine_for_model`），不重启。

本模块只做一件事：**把「ASR 文本 + 视频帧」真送进多模态模型，拿回那句场景**。
**不判停、不判接不接**：两件事都归对话中枢（`hub_gate.py`，与判停同一版出字并发）。
结构化字段（intent/target/topic/cues/confidence）只作内部兼容位，**不再要求模型产出**，
也不再是页面主产物；页面 / 中枢 / 判接不接用的都是 `scene` 这一句。

- 输入：ASR 文本（partial / final）、席别、**当次送模型的彩色帧**（`frame.image`：页面给的
  `data:image/jpeg;base64,…`，可带 `w/h`；旧调用方不给图就退化成纯文本一次）、页面算好的像素统计
  （亮度 / 运动）、本席最近 2 句已落定文本（上下文，用于把「导航去 X」的后续分句「尽量走高速」按
  同一轮理解）。这一帧**原样**拼进 OpenAI `image_url`（同一段 data URI，不重编码），
  文本与画面统计同时作为 text part 送进去；`engine.frame` 回带这张图的信息，页面照它显示。
  输出：**一句 ≤30 字的现场旁白**（`result["scene"]`，稿 E 口吻、不第一人称）。结构化字段
  `intent/target/topic/cues/confidence` 只留空壳兼容位，不要求模型产出；上层（`mind_reading.MindReading`）
  只取这句，不用它判接不接（那是中枢的事）。
- 失败处理（不把失败当成功）：HTTP / 超时 / 解析失败（含模型没按「只输出这一句」返回）抛
  `MindEngineError`，由上层回落规则并
  在 `engine` 里写明 `rule_fallback` + 错误类别；**视觉通道被后端拒绝时**，本模块用同一模型
  做一次 text-only 重试并在 `engine.vision` 里显式记 `degraded`（降级不是成功）。注意「后端接受
  并 200」不等于「读懂图」：`cc/glm-5.2` 就走这条「假多模态」路，故 catalog 用 `vision` 字段把
  能力单独标出来，UI 才不会再拿 `status=ok` 冒充多模态成功。

配置（全部从环境变量 / 既有私有凭据位置读，本文件不落盘任何密钥、也不打印）：
- `MIND_LLM_MODEL` 默认 `deepseek-v4.1-flash`（无页面选择时的兜底；思考开关按目录该档口径带）
- `MIND_LLM_BASE_URL` 默认 `https://litellm.swfcn.cn.bg.corpintra.net/v1`
- `MIND_LLM_TIMEOUT` 默认 30（秒，单次调用读超时；三档实测中位见 DETAIL，最慢一档仍留足排队 /
  冷启动余量；不再用 8b 的 210s）
- `MIND_LLM_MAX_TOKENS` 默认 768（落验收口径 512～768；三档 completion 实测见 DETAIL，768 足够
  出完 JSON，不靠放大额度换速度）
- 凭据：`LITELLM_API_KEY`（进程环境优先）；缺失时读 `~/.dsh/.credentials.yaml` 的
  `refs.SWF_LITELLM_API_KEY`（与 `core_pipeline_run.existing_slow_configuration` 同一既有位置）
- `MIND_VISION_BACKOFF_S` 默认 600：视觉通道被后端判死后的冷却期，期内不再每轮重试图片

未定 / 本单不代拍：融合权重、判定阈值、音频模态（wiki「拒识」§未决 1）。本单只把真模型送进
主路径，阈值仍由模型自己给 confidence，不新增产品阈值。
"""
from __future__ import annotations

import base64
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any

DEFAULT_MODEL = "deepseek-v4.1-flash"
DEFAULT_BASE = "https://litellm.swfcn.cn.bg.corpintra.net/v1"
DEFAULT_TIMEOUT = 30.0    # 三档实测中位见 DETAIL；最慢一档（cc/glm-5.3-flash 始终思考）仍需排队余量
DEFAULT_MAX_TOKENS = 768  # 落验收口径 512～768；三档 completion 实测见 DETAIL
MAX_SCENE_CHARS = 30      # 2026-11-10 16:54 口径：场景不超过 30 个字（超了就截到 30，仍只这一句）
# 2026-11-11 21:50 用户拍板：读心**每次判断**除了当前画面和两路已经出到的字，还要带上最近 5 次
# 已经出过的读心结果（不够 5 次就有几份带几份，不要编造更早的），让前后旁白能连上。
MAX_PRIOR_RESULTS = 5
VISION_BACKOFF_S = 600.0
# 送模型的画面就是页面采好的彩色 JPEG（默认 320×180）：原样透传，不重编码、不转灰阶。
# 尺寸上限只用来挡住异常帧，不是产品口径（页面会按 16:9 压好再送）。
FRAME_FORMATS = ("data:image/jpeg;base64,", "data:image/png;base64,")
FRAME_MAX_CHARS = 600_000        # data URI 长度上限（≈600KB，一帧 320×180 JPEG 远小于它）
FRAME_MAX_SIDE = 2048

SEAT_LABEL = {"D": "主驾", "P": "副驾"}

# 页面可选引擎（三选一；本单不扩到其它模型）。字段直接给前端渲染，避免页面与后端两处文案打架。
#   vision: "ok" = 本单多图多次实测能读懂；"unavailable" = 图被后端接受但读不懂（实测）。
#   thinking: 只有 deepseek-v4.1-flash 带 `{"type": "disabled"}`；另外两档为 None（请求体不加该字段）。
#   speed_label / vision_label 按本单墙钟中位写，用户看选项就能判断快慢、能不能看图。
MODEL_CHOICES: tuple[dict[str, Any], ...] = (
    {
        "id": "cc/glm-5.2",
        "label": "cc/glm-5.2",
        "default": False,
        "vision": "unavailable",
        "vision_label": "读不懂图",
        "speed_label": "文本快 · 中位约 2～3.5s",
        "thinking": None,
        "note": "文本判断快且准；帧照发但当前端点读不懂图（数字恒错、纯红答白、翻转帧不跟图翻），视觉按不可用标注",
    },
    {
        "id": "cc/glm-5.3-flash",
        "label": "cc/glm-5.3-flash",
        "default": False,
        "vision": "ok",
        "vision_label": "能读懂图",
        "speed_label": "慢 · 中位约 5～12s（始终思考）",
        "thinking": None,
        "note": "能读懂图；该模型始终思考、后端 1210 明确不支持关闭，故请求体不加 thinking 开关",
    },
    {
        "id": "deepseek-v4.1-flash",
        "label": "deepseek-v4.1-flash",
        "default": True,
        "vision": "ok",
        "vision_label": "能读懂图",
        "speed_label": "中 · 关思考中位约 2.3～3.4s",
        "thinking": {"type": "disabled"},
        "note": "能读懂图；选中时请求体只对这一档带 thinking:disabled（另外两档不带）",
    },
)
MODEL_BY_ID = {item["id"]: item for item in MODEL_CHOICES}
MODEL_DEFAULT_ID = next(item["id"] for item in MODEL_CHOICES if item["default"])


def model_choice(model_id: str) -> dict[str, Any] | None:
    """按 id 取页面选项（未知 id 返回 None，调用方按兜底处理，不偷偷换模型）。"""
    return MODEL_BY_ID.get(model_id)



class MindEngineError(RuntimeError):
    """一次真模型判定失败。`kind` 用于上层汇总，`detail` 不含密钥。"""

    def __init__(self, kind: str, detail: str = ""):
        super().__init__(f"{kind}: {detail}" if detail else kind)
        self.kind = kind
        self.detail = detail


def _resolve_key() -> str | None:
    key = os.environ.get("LITELLM_API_KEY")
    if key:
        return key
    path = Path(os.environ.get("MIND_LLM_CREDENTIALS") or (Path.home() / ".dsh/.credentials.yaml"))
    try:
        text = path.read_text()
    except OSError:
        return None
    try:
        import yaml
        doc = yaml.safe_load(text) or {}
    except Exception:
        return None
    value = (doc.get("refs") or {}).get("SWF_LITELLM_API_KEY")
    return value if isinstance(value, str) and value.strip() else None


def resolve_config() -> dict[str, Any] | None:
    """读模型 / 地址 / 超时 / 凭据；凭据缺失返回 None（上层回落规则，页面不白屏）。"""
    key = _resolve_key()
    if not key:
        return None
    model_id = os.environ.get("MIND_LLM_MODEL", DEFAULT_MODEL)
    return {
        "model": model_id,
        "base": os.environ.get("MIND_LLM_BASE_URL", DEFAULT_BASE).rstrip("/"),
        "timeout": float(os.environ.get("MIND_LLM_TIMEOUT", DEFAULT_TIMEOUT)),
        "max_tokens": int(os.environ.get("MIND_LLM_MAX_TOKENS", DEFAULT_MAX_TOKENS)),
        "key": key,
        "vision_backoff_s": float(os.environ.get("MIND_VISION_BACKOFF_S", VISION_BACKOFF_S)),
        # 思考开关按目录里该档的口径带：默认档 deepseek 关思考，另两档为 None（请求体不加）；
        # 环境覆盖成什么档就按那档口径（未知 id 不加）。页面建引擎走 config_for_model，同一份口径。
        "thinking": (model_choice(model_id) or {}).get("thinking"),
    }


def config_for_model(model_id: str) -> dict[str, Any] | None:
    """按页面选项覆盖模型 + 思考开关；凭据缺失返回 None（调用方回落规则）。"""
    config = resolve_config()
    if config is None:
        return None
    spec = model_choice(model_id)
    if spec is None:
        raise MindEngineError("config", f"未知读心引擎 {model_id!r}；可选：{' / '.join(MODEL_BY_ID)}")
    return dict(config, model=spec["id"], thinking=spec.get("thinking"))


# ---- 画面：页面采好的彩色帧，原样透传 -------------------------------------------------------
def image_ok(value: Any) -> bool:
    """是不是一张可直接送模型的帧（页面给的 data URI，JPEG / PNG）。"""
    if not isinstance(value, str) or not value:
        return False
    if not value.startswith(FRAME_FORMATS):
        return False
    return len(value) <= FRAME_MAX_CHARS


def image_format(value: str) -> str:
    return "jpeg" if str(value or "").startswith("data:image/jpeg;") else "png"


def frame_info(image: str, width: Any = None, height: Any = None) -> dict[str, Any]:
    """这一帧的形状：宽高（页面给的；没给就从 JPEG 头里读）+ 编码 + 压缩后大小。

    只描述「送出去的就是这一张」，不在这里改像素：页面显示用的也是同一段 data URI。
    """
    w = int(width) if isinstance(width, (int, float)) and 0 < int(width) <= FRAME_MAX_SIDE else None
    h = int(height) if isinstance(height, (int, float)) and 0 < int(height) <= FRAME_MAX_SIDE else None
    if (w is None or h is None) and str(image or "").startswith("data:image/jpeg;"):
        w, h = _jpeg_size(image) if w is None or h is None else (w, h)
    payload = image.split(",", 1)[1] if "," in (image or "") else ""
    return {
        "w": w, "h": h,
        "format": image_format(image),
        "bytes": max(0, int(len(payload) * 3 / 4)),
        "data_uri": image,
    }


def _jpeg_size(image: str) -> tuple[int | None, int | None]:
    """从 JPEG 的 base64 里读 SOF 宽高（页面没带 w/h 时兜底，不引第三方库）。"""
    payload = image.split(",", 1)[1] if "," in (image or "") else ""
    try:
        raw = base64.b64decode(payload[:20000], validate=False)
    except Exception:
        return None, None
    index = 2
    while index + 9 < len(raw):
        if raw[index] != 0xFF:
            index += 1
            continue
        marker = raw[index + 1]
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            height = int.from_bytes(raw[index + 5:index + 7], "big")
            width = int.from_bytes(raw[index + 7:index + 9], "big")
            return width, height
        segment = int.from_bytes(raw[index + 2:index + 4], "big")
        index += 2 + segment
    return None, None


def _seats(seat: str) -> str:
    return SEAT_LABEL.get(seat, seat or "D")


# 2026-11-11 23:04 用户拍板：现网读心默认稿换成**稿 E**（人名 / 称呼对席），**逐字**照
# `tmp/poc-mind-prompt-E.txt`（与 `tmp/poc-neixinxi-name-land-验收.md` 文末同一份），
# 不改写、不删示例；对话流页用同一份（用户已拍）。请求末尾那句用户指令 `REQUEST_INSTRUCTION` 保持不动。
PROMPT_VERSION = "v05"
SYSTEM_PROMPT = """你是车舱里的现场旁白。语气轻松有趣，像朋友在旁边嘀咕一句，但不要油腻，也不要嘲讽车上的人。根据画面和正在说的话，写出此刻最有意思的一点。

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

# 请求末尾那句用户指令：与稿 E 同一口径（**不再**写「用第一人称说一句」；这一句不动）。
# 30 字上限按现网原样保留（2026-11-10 口径，本卡不改长度），只在这里如实告诉模型。
REQUEST_INSTRUCTION = "请按上面的要求，写一句此刻最有意思的短旁白（不超过 30 个字，只输出这一句）。"

# 模型偶尔不按「只输出这一句」走（回 JSON / 自我介绍 / 提到模块名）：这些词必须从产物里拿掉。
_SELF_REFERENCE = (
    "读心模块", "多模态场景感知引擎", "场景感知引擎", "感知引擎", "读心模块的", "我是车载",
)
_JSON_SCENE = re.compile(r'"scene"\s*:\s*"([^"]+)"')


def _lane_words(lane: dict[str, Any]) -> str:
    """一路的出字写进提示词：还在说 → 「这一刻出字」；刚收住 → 明写「刚说完」；没有字就不假装有字。"""
    seat = str(lane.get("seat") or "D").upper()
    label = _seats(seat)
    text = str(lane.get("text") or "").strip()
    settled = str(lane.get("settled") or "").strip()
    if text:
        return f"- {label}（{seat}）：这一刻出字「{text}」（还在说）"
    if settled:
        return f"- {label}（{seat}）：这一刻没出字；刚说完「{settled}」（这一句刚落定）"
    return f"- {label}（{seat}）：没出字"


def _speech_part(speech: dict[str, dict[str, Any]]) -> str:
    """两路出字分栏：主驾、副驾各一栏，谁没说话那一路就空着（标「没出字」）。"""
    lanes = [{"seat": seat, "label": _seats(seat),
              "text": str((speech or {}).get(seat, {}).get("text") or "").strip(),
              "settled": str((speech or {}).get(seat, {}).get("settled") or "").strip(),
              "speaking": bool((speech or {}).get(seat, {}).get("speaking"))}
             for seat in ("D", "P")]
    talking = [lane["label"] for lane in lanes if lane["speaking"] and lane["text"]]
    settled = [lane["label"] for lane in lanes if lane["settled"] and not lane["text"]]
    if len(talking) > 1:
        head = "这一刻两路都在出字：" + "、".join(talking) + "。\n"
    elif talking:
        head = "这一刻只有" + talking[0] + "在出字。\n"
    elif settled:
        # 「这句说完了」那一刻：没人还在说，刚收住的那句字是完整的（不是只凭画面）。
        head = "这一刻没人还在说；" + "、".join(settled) + "这句刚说完（用它的字，不是只凭画面）。\n"
    else:
        head = "这一刻两路都没出字（只凭画面读）。\n"
    return ("车里这一刻的出字（两路分开写，谁说的就标谁；没出字的这一路是空的）：\n"
            + head + "\n".join(_lane_words(lane) for lane in lanes) + "\n")


def _prior_part(prior_results: list[dict[str, Any]] | None) -> str:
    """「最近已经出过的读心结果」那一段：最多 5 份、更早 → 更近、带相对时间。

    只写**真的已经出过**的那几份（调用方按 `mind_reading.MAX_PRIOR_RESULTS` 截好传进来）；
    一份都没有时整段不出现，不编一句更早的。它只作上下文：接着前面已经发生的事写，
    不重复它们、也不把它们当成这一刻的新话或新指令。
    """
    if not prior_results:
        return ""
    lines = []
    for item in list(prior_results)[-MAX_PRIOR_RESULTS:]:
        if not isinstance(item, dict):
            continue
        scene = str(item.get("scene") or "").strip()
        if not scene:
            continue
        seconds = max(0, int(round(float(item.get("ago_ms") or 0) / 1000.0)))
        lines.append("- " + (("约 %d 秒前" % seconds) if seconds else "刚刚") + f"「{scene}」")
    if not lines:
        return ""
    return ("之前已经出过的旁白（更早 → 更近，最多 %d 份，只有这几份；只作上下文——接着前面已经发生的"
            "事写，不要重复它们，也不要把它们当成这一刻的新话或新指令）：\n" % MAX_PRIOR_RESULTS
            + "\n".join(lines) + "\n")


def _text_part(text: str, visual: dict[str, Any], seat: str, final: bool,
               prior_texts: list[str] | None = None,
               speech: dict[str, dict[str, Any]] | None = None,
               prior_results: list[dict[str, Any]] | None = None) -> str:
    prefix = "" if final else "（还在出字）"
    context = ""
    if prior_texts:
        context = ("前文（本席最近已落定的句子，只作上下文；当前这句是对它的继续时按同一轮理解，"
                   "不要当成新指令）：「" + "」「".join(prior_texts[-2:]) + "」\n")
    speech_block = _speech_part(speech) if speech else ""
    if not speech_block:
        speech_block = f"席别：{_seats(seat)}（{seat}）\n{context}ASR 文本{prefix}：「{text}」\n"
    return (
        f"{speech_block}"
        + _prior_part(prior_results)
        + f"画面统计：亮度 {visual.get('brightness', 0)}，运动 {visual.get('motion', 0)}，"
        f"{visual.get('note', '无画面')}；有画面：{'是' if visual.get('has_frame') else '否'}。\n"
        + REQUEST_INSTRUCTION
    )


def build_messages(text: str, visual: dict[str, Any], seat: str, final: bool,
                   image: str | None = None,
                   prior_texts: list[str] | None = None,
                   speech: dict[str, dict[str, Any]] | None = None,
                   prior_results: list[dict[str, Any]] | None = None) -> list[dict[str, Any]]:
    """一条读心请求：两路文本（`speech`，一次带齐主驾/副驾）+ 当次那张彩色帧（原样 data URI）
    + 最近已经出过的读心结果（`prior_results`，最多 5 份，只作上下文）。

    `speech` 不给时按旧的单路写法（`text` + `seat`）——旧调用方与假引擎不受影响。
    """
    content: list[dict[str, Any]] = [
        {"type": "text",
         "text": _text_part(text, visual, seat, final, prior_texts, speech, prior_results)}]
    if image_ok(image):
        content.append({"type": "image_url", "image_url": {"url": image}})
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": content}]


def _one_sentence(raw: str) -> str:
    """把模型输出压成那一句场景：容忍 JSON / 围栏 / 前后解释，只取第一句，≤30 字。

    - 结构化输出（模型没按底稿走）→ 取 `scene` 那一格；
    - 其它情况 → 取第一行（去掉代码块围栏、引号、列表标记）；
    - 去掉「读心模块」这类自称（底稿明确不许提），再截到 30 个字。
    """
    text = (raw or "").strip()
    if not text:
        raise MindEngineError("empty_content", "模型返回空内容（可能被 max_tokens 截断）")
    match = _JSON_SCENE.search(text)
    if match:
        text = match.group(1)
    else:
        fence = re.search(r"```(?:json)?\s*(.*?)\s*```", text, re.S)
        if fence:
            text = fence.group(1)
        text = text.splitlines()[0] if text.splitlines() else text
    text = text.strip().strip('"\'“”‘’').strip()
    for token in _SELF_REFERENCE:
        text = text.replace(token, "")
    text = re.sub(r"\s+", "", text).lstrip("，。！？；：、,.!?;: ")
    if not text:
        raise MindEngineError("bad_output", "模型只回了自称/空话，不是一句场景")
    if len(text) > MAX_SCENE_CHARS:
        text = text[:MAX_SCENE_CHARS].rstrip("，。！？；：、,.!?;: ")
    return text


def _normalize(raw: str, text: str, seat: str, final: bool) -> dict[str, Any]:
    """产物 = 那一句场景。结构化字段只留兼容位，不再让模型交一堆字段。"""
    scene = _one_sentence(raw)
    return {
        "scene": scene,
        "intent": "—",
        "target": "—",
        "topic": "—",
        "confidence": 0.0,
        "reason": "读心只产这一句场景（不判停、不判接不接）。",
        "cues": [],
        "finalized": bool(final),
        "read_by": "mind",
    }


class MindLLMEngine:
    """一次真多模态判定：`judge(...) -> {result, engine}`；失败抛 `MindEngineError`。"""

    def __init__(self, config: dict[str, Any], *, post=None):
        self.model = config["model"]
        self.base = config["base"]
        self.timeout = float(config["timeout"])
        self.vision_backoff_s = float(config.get("vision_backoff_s", VISION_BACKOFF_S))
        self.max_tokens = int(config.get("max_tokens", DEFAULT_MAX_TOKENS))
        self.thinking = config.get("thinking")      # 只有页面选 deepseek 档时才非 None
        self._key = config["key"]
        self._post = post or self._default_post
        self._client = None
        self._lock = threading.Lock()
        self._vision_off_until = 0.0
        self._vision_error: str | None = None
        self.calls = 0
        self.failures = 0
        self.last_latency_ms: float | None = None

    @property
    def choice(self) -> dict[str, Any]:
        """页面选项（未登记的模型名按「能力未知」描述，不冒充视觉可用）。"""
        spec = model_choice(self.model)
        if spec:
            return spec
        return {"id": self.model, "label": self.model, "vision": "unknown",
                "vision_label": "视觉未核", "speed_label": "未实测", "thinking": self.thinking,
                "note": "环境变量指定的模型，不在页面三选一目录内"}

    # -- transport -------------------------------------------------------------------------
    def _default_post(self, body: dict[str, Any]) -> dict[str, Any]:
        import httpx2
        if self._client is None:
            # trust_env=False：本机 NO_PROXY 里的 "[::1]" 会让 httpx2 解析成非法 URL（既有坑）。
            self._client = httpx2.Client(timeout=self.timeout, trust_env=False, follow_redirects=False)
        response = self._client.post(
            self.base + "/chat/completions",
            headers={"Authorization": "Bearer " + self._key, "Content-Type": "application/json"},
            json=body,
        )
        if response.status_code >= 400:
            detail = response.text[:240].replace("\n", " ")
            kind = "vision_backend" if ("failed to process inputs" in detail
                                        or "image" in detail.lower()) else "http"
            raise MindEngineError(kind, f"HTTP {response.status_code}: {detail}")
        return response.json()

    def _request(self, messages: list[dict[str, Any]], *, max_tokens: int | None = None) -> dict[str, Any]:
        body = {"model": self.model, "messages": messages, "temperature": 0,
                "max_tokens": int(max_tokens or self.max_tokens), "stream": False}
        if self.thinking:   # 只对 catalog 里声明的那一档加；另外两档请求体不带 thinking
            body["thinking"] = dict(self.thinking)
        began = time.monotonic()
        data = self._post(body)
        latency = (time.monotonic() - began) * 1000
        self.last_latency_ms = round(latency, 1)
        choices = data.get("choices") or []
        if not choices:
            raise MindEngineError("bad_response", "响应缺 choices")
        message = choices[0].get("message") or {}
        content = message.get("content") or ""
        reasoning = message.get("reasoning_content") or ""
        finish = choices[0].get("finish_reason")
        if not str(content).strip():
            # 部分 qwen3-vl 部署把答案落在 reasoning_content；先试它，再按截断报错。
            content = reasoning
            if not str(content).strip():
                raise MindEngineError("empty_content", f"finish_reason={finish}")
        return {"content": str(content), "finish": finish, "latency_ms": round(latency, 1),
                # 思考段长度：UI 调试行用它证明「关思考确实没思考」（0 = 无思考段）
                "reasoning_chars": len(reasoning), "usage": data.get("usage")}

    # -- judge ------------------------------------------------------------------------------
    def judge(self, *, text: str, visual: dict[str, Any], seat: str = "D", final: bool = False,
              image: str | None = None, image_w: int | None = None, image_h: int | None = None,
              prior_texts: list[str] | None = None,
              speech: dict[str, dict[str, Any]] | None = None,
              prior_results: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        """一次读心：`speech` = 这次一起送的主驾/副驾两路出字（不给就退回单路 `text`）。

        `prior_results` = 最近已经出过的读心结果（最多 5 份，只作上下文），两条请求（多模态 /
        text-only 降级）都带上同一份——降级换了通道，不换这次判断的上下文。
        """
        has_image = image_ok(image)
        sent = frame_info(image, image_w, image_h) if has_image else None
        vision_attempted = False
        vision_error = None
        if has_image and time.monotonic() >= self._vision_off_until:
            vision_attempted = True
            try:
                raw = self._request(build_messages(text, visual, seat, final, image, prior_texts,
                                                   speech, prior_results))
                result = _normalize(raw["content"], text, seat, final)
                payload = {"result": result, "route": "vision", "vision": "ok", "frame": sent,
                           "model": self.model, "latency_ms": raw["latency_ms"], "finish": raw["finish"],
                           "reasoning_chars": raw.get("reasoning_chars", 0), "usage": raw["usage"]}
                self.calls += 1
                return self._envelope(payload, vision_attempted=True, vision_error=None)
            except MindEngineError as error:
                if error.kind != "vision_backend":
                    self.failures += 1
                    raise
                vision_error = error.detail
                self._vision_error = error.detail
                self._vision_off_until = time.monotonic() + self.vision_backoff_s
                # 视觉通道后端不支持 → 同一模型 text-only 重试，显式降级，不冒充多模态成功。

        try:
            raw = self._request(build_messages(text, visual, seat, final, None, prior_texts, speech,
                                               prior_results))
            result = _normalize(raw["content"], text, seat, final)
        except MindEngineError:
            self.failures += 1
            raise
        payload = {"result": result, "route": "text_only", "vision": "degraded", "frame": None,
                   "model": self.model, "latency_ms": raw["latency_ms"], "finish": raw["finish"],
                   "reasoning_chars": raw.get("reasoning_chars", 0), "usage": raw["usage"]}
        self.calls += 1
        return self._envelope(payload, vision_attempted=vision_attempted,
                              vision_error=vision_error or self._vision_error)

    def _envelope(self, payload: dict[str, Any], *, vision_attempted: bool, vision_error: str | None) -> dict[str, Any]:
        mode = "litellm_vl" if payload["route"] == "vision" else "litellm_text_degraded"
        spec = self.choice
        capability = spec.get("vision", "unknown")
        if payload["route"] != "vision":
            vision_note = "视觉通道被后端拒绝，同一模型 text-only 降级（不是多模态成功）"
        elif capability == "unavailable":
            vision_note = ("彩色帧已送出且被后端接受，但该模型在此端点读不懂图"
                           "（本单多图多次实测），视觉按不可用标注")
        elif capability == "ok":
            vision_note = "真多模态：文本 + 帧一起送模型（本单多图多次实测能读懂）"
        else:
            vision_note = "帧已送出且被后端接受；该模型视觉能力未核"
        engine = {
            "name": self.model,
            "model": self.model,
            "mode": mode,
            "source": "llm",
            "base": self.base,
            "thinking": self.thinking,
            "prompt_version": PROMPT_VERSION,   # 非麦路径可据此证明「发出去的就是稿 E」
            "vision": {
                "attempted": vision_attempted,
                "used": payload["route"] == "vision",
                "status": payload["vision"],
                "capability": capability,
                "error": vision_error,
                "note": vision_note,
            },
            "latency_ms": payload["latency_ms"],
            "finish": payload["finish"],
            "reasoning_chars": payload.get("reasoning_chars", 0),
            "calls": self.calls,
            "failures": self.failures,
            "fallback": False,
            "note": f"litellm OpenAI 兼容 chat/completions；{self.model}",
        }
        return {"result": payload["result"], "engine": engine, "frame": payload.get("frame")}

    # -- status -----------------------------------------------------------------------------
    def describe(self) -> dict[str, Any]:
        spec = self.choice
        with self._lock:
            return {
                "name": self.model, "model": self.model, "mode": "litellm_vl",
                "source": "llm", "base": self.base,
                "label": spec.get("label", self.model),
                "vision_label": spec.get("vision_label", "视觉未核"),
                "speed_label": spec.get("speed_label", "未实测"),
                "thinking": self.thinking,
                "prompt_version": PROMPT_VERSION,
                "vision": {"status": "backoff" if time.monotonic() < self._vision_off_until else "unknown",
                           "capability": spec.get("vision", "unknown"),
                           "error": self._vision_error,
                           "note": spec.get("note") or "视觉通道状态按最近一次真调用记录"},
                "calls": self.calls, "failures": self.failures, "fallback": False,
            }


def engine_from_env() -> "MindLLMEngine | None":
    """按 `MIND_ENGINE`（auto|litellm|rule，默认 auto）建引擎；缺凭据返回 None（回落规则）。"""
    choice = os.environ.get("MIND_ENGINE", "auto").strip().lower()
    if choice in {"rule", "offline", "none", "0", "false"}:
        return None
    config = resolve_config()
    if config is None:
        if choice in {"litellm", "llm", "real", "1", "true"}:
            raise MindEngineError("config", "MIND_ENGINE=litellm 但找不到 LITELLM_API_KEY / ~/.dsh 凭据")
        return None
    return MindLLMEngine(config)


def engine_for_model(model_id: str) -> "MindLLMEngine | None":
    """按页面选择的模型 id 建引擎（不重启、不读改写进程全局）；缺凭据返回 None（回落规则）。"""
    config = config_for_model(model_id)
    if config is None:
        return None
    return MindLLMEngine(config)


__all__ = ["MindEngineError", "MindLLMEngine", "engine_from_env", "engine_for_model",
           "config_for_model", "resolve_config", "MODEL_CHOICES", "MODEL_BY_ID", "MODEL_DEFAULT_ID",
           "model_choice", "build_messages", "image_ok", "frame_info", "DEFAULT_MODEL", "DEFAULT_BASE",
           "DEFAULT_TIMEOUT", "DEFAULT_MAX_TOKENS", "SYSTEM_PROMPT", "REQUEST_INSTRUCTION",
           "PROMPT_VERSION", "MAX_PRIOR_RESULTS"]
