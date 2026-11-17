"""读心模块 · 只产「当前场景」：一个会话一份，同时吃主驾 / 副驾两路出字 + 画面。

口径来源：wiki `[[中枢并发闸门]]`（2026-11-10 15:12 拍板，现行）＋ `tmp/poc-mind-dual-asr-验收.md`
（2026-11-11 12:52 用户拍「接下来需要是两路 ASR 输入」）。

- **一个会话只有一份读心、一份场景**（`scene_snapshot()`）：不再「每席一份、互相看不见对方的话」。
  每次更新都同时带上 **这一张彩色画面 ＋ 主驾这一刻出到的字 ＋ 副驾这一刻出到的字**，两路都标席别；
  谁没说话那一路就空着（不假装有字），也不因此不读——只有画面时仍然只凭图来读。
- **只在两处请求**（2026-11-11 13:13 拍板，wiki `[[读心请求时机]]`）：① **这句说完了**——对话中枢把这句
  收住（判停给「说完」/ 静音窗收住）后调 `on_utterance_close()`，读心用那一刻**最完整的字 ＋ 当时的画面**
  读一次；② **没在说话、画面变了**——`ingest` 收到新帧时直接读一次（`_maybe_frame_read`）。**还在说话时
  即使画面变了也先不请求**（等这句说完再一起读）；**出字还在变 / 字稳住一小段 / ASR 自己报了一段结束
  都不触发请求**。请求不积压：同一会话最多 1 个在途 + 1 个排队，新请求覆盖排队的旧请求（上一次还没回来
  又该读了，以最新这一次为准）。
- **只产场景**（谁在说、说什么、更像朝谁说、画面里在做什么）：每次读心落地就刷新场景
  （`revision` + `updated_at_ms`），**不等中枢锁字**、也不被中枢的节奏带着走。
- **没开口也读**：只要画面在，画面一变就读一次当前场景（`_maybe_frame_read`）。
- **出了结果就推**：场景一变，上层（`ui/server.py` 的 driver）立刻推一条 `mind_scene` 给对话流，
  不等下一句出字、也不等下一帧画面才看见（2026-11-11 拍板：只要有结果就推）。
- **不判停**：这句话说完没说完归对话中枢（判停一路）——读心**只听中枢的收住**（`on_utterance_close`），
  不自己判断、也不拿 ASR 的 final 当「说完」；**不判接不接**：要不要进 Agent 也归对话中枢
  （`hub_accept.py` 的可替换接口 + `hub_gate.py` 的并发开闸）。读心没有「已消费 / 旁听 / 进不进 Agent」
  这些字段——那是中枢的结论，不是读心的。
- **每次判断都带上前几次结果**（2026-11-11 21:50 用户拍板，最近 **5** 次）：读心自己记下**已经出过**的
  结果（`_history`，最多 5 份、更早 → 更近，不够 5 份就有几份带几份、不编造更早的），每次发起的判断都
  把这一份连同当前画面、两路已经出到的字一起送进模型（`prior_results`），让后出的旁白能接上前面已经
  发生的事，而不是每次都像第一次看。这份上下文只给「判断」用，**不改**中枢 / 开闸 / 记忆的任何结论；
  离线规则替身不用它（`prior_used=False`，不冒充已经用过）。
- **中枢要场景时取「此刻已有的那份」**：`scene_snapshot()` 直接返回缓存场景，不调用模型、不重算；
  中枢不等待读心，读心也不等中枢（两边互不等待，wiki §4）。两席的闸门取的是**同一份**场景。

通路：

- 画面：**页面采好的彩色帧原样进模型**（`frame.image`，默认 320×180 JPEG；不转灰阶、不放大）。
  亮度 / 运动由页面从同一帧的像素算好随帧送来（只作提示词里的统计与页面显示，不改像素）。
- 出字：两路各自一路 ASR；本模块按席记「这一刻已经出到的字」（还在说就是 partial，这句刚收住就是
  刚落定的那句），并把**两路一起**写进同一次读心请求（`speech` 分栏，标席别、标「还在说 / 刚说完 /
  没出字」）。前文（各席最近已落定的句子）只作上下文，不当「这一刻出字」。
- 默认（`engine=None`）：离线规则引擎，只吃 ASR 文本与视频帧统计，不连外部服务，任何机器都能复现。
- 真模型（`engine=mind_llm.MindLLMEngine(...)`）：主路径按页面选的引擎送 litellm（`cc/glm-5.2` /
  `cc/glm-5.3-flash` / `deepseek-v4.1-flash`），真多模态（两路 ASR 文本 + 帧）。模型调用在后台线程跑，
  `ingest()` 不等模型；失败 / 超时回落规则，并在 `engine{name, mode, fallback, error}` 里写明。
- `/api/mind/tick` 只回场景；中枢两路与闸门在 `/api/hub/tick`（`hub_gate.GateKeeper`）。

未拍板（wiki §8，本卡不代拍）：接不接用哪个模型/服务；场景快照允许多旧。本模块不设「场景超龄就当无场景」
的硬阈值——`scene_snapshot()` 带 `age_ms`，让中枢与调试页自己看见新鲜度。
"""
from __future__ import annotations

import os
import re
import threading
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from typing import Any

# 引擎没写明 model 时的兜底名（与线上默认档同一处口径；不在这里硬写第二份默认）
from mind.mind_llm import DEFAULT_MODEL, MAX_PRIOR_RESULTS
SEAT_LABEL = {"D": "主驾", "P": "副驾"}
# 两路 ASR 的固定顺序（同一份场景里先主驾、后副驾，页面与提示词都照这个顺序读）
SEAT_ORDER = ("D", "P")

from shared.lexicon import CAR_CUES, CHAT_CUES, NEGATION_CUES


# 场景置信度的显示口径（离线替身的稳定口径，不是产品拍板阈值）。
PARTIAL_CONF_CAP = 0.72   # 还在出字时，场景置信度上限（显示「暂定」）
# 2026-11-10 16:54 口径：读心的产物是一句 ≤30 字的场景（暂定、不是硬门槛）。
# 2026-11-11 16:57 拍板把固定稿换成 v04「轻松有趣」：车舱旁白口吻、不第一人称（离线替身同口径）。
# 两路都在时这一句也守 30 字：写谁在说什么，不逐字抄两路原文。
SCENE_MAX_CHARS = 30


def _seats(seat: str) -> str:
    return SEAT_LABEL.get(str(seat).upper(), seat)


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _match(text: str, pattern: str) -> bool:
    return re.search(pattern, text) is not None


def _visual_block(brightness: float | None, motion: float | None) -> dict[str, Any]:
    """视频帧统计（亮度 / 运动）：由页面从**同一帧**彩色像素算好送进来，只做画面线索，不做识人。

    统计不是另采一张图：它和送模型的那张帧出自同一段像素，页面显示也用同一段 data URI。
    """
    b = max(0.0, min(1.0, float(brightness))) if isinstance(brightness, (int, float)) else 0.0
    m = max(0.0, float(motion)) if isinstance(motion, (int, float)) else 0.0
    if m >= 0.08:
        note = "画面有明显动作"
    elif m >= 0.02:
        note = "画面轻微变化"
    else:
        note = "画面平稳"
    return {"brightness": round(b, 3), "motion": round(m, 3), "note": note, "has_frame": True}


def _trim(text: str, limit: int) -> str:
    body = (text or "").replace("\n", " ").strip()
    if limit <= 0:
        return ""
    if len(body) <= limit:
        return body
    return body[:limit].rstrip("，。！？；：、,.!?;: ")


def _scene_line(speakers: list[tuple[str, str, bool]], tail: str = "") -> str:
    """把一处场景压成一句车舱旁白，**不超过 30 个字**（离线替身也守同一条口径）。

    v04 口吻（2026-11-11 16:57 拍板）：像朋友在旁边嘀咕一句，**不第一人称**——不写「我看到 / 我听到 /
    我知道」，也不自称模块。`speakers` 是这一次读里被写进场景的那几路（最多两路，按 `SEAT_ORDER` 顺序，
    (席别, 出字, 还在说)）：单路写成「主驾说「…」」/「主驾刚说「…」」；双路写成
    「主驾说「…」，副驾刚说「…」」，长度不够时按比例截两路正文，仍守 30 字。
    """
    if not speakers:
        return ""
    if len(speakers) == 1:
        seat, text, speaking = speakers[0]
        lead = _seats(seat) + ("说「" if speaking else "刚说「")
        close = "」" + tail
        available = SCENE_MAX_CHARS - len(lead) - len(close)
        return lead + _trim(text, available) + close
    (seat_a, text_a, speaking_a), (seat_b, text_b, speaking_b) = speakers[0], speakers[1]
    lead = _seats(seat_a) + ("说「" if speaking_a else "刚说「")
    mid = "」，" + _seats(seat_b) + ("说「" if speaking_b else "刚说「")
    close = "」"
    budget = SCENE_MAX_CHARS - len(lead) - len(mid) - len(close) - len(tail)
    if budget < 6:
        tail = ""
        budget = SCENE_MAX_CHARS - len(lead) - len(mid) - len(close)
    budget = max(4, budget)
    half = budget // 2
    take_a = min(len(text_a), half)
    take_b = min(len(text_b), budget - take_a)
    take_a = min(len(text_a), budget - take_b)     # 短的那一路让出空位给长的那一路
    return (lead + _trim(text_a, take_a) + mid + _trim(text_b, take_b)
            + close + tail)


def _lane_line(lanes: list[dict[str, Any]]) -> str:
    """把这一次读的两路（谁在出字 / 谁刚说完）压成一句给页面看的说明。"""
    parts = []
    for lane in lanes:
        label = lane.get("label") or _seats(lane.get("seat"))
        text = str(lane.get("text") or "").strip()
        settled = str(lane.get("settled") or "").strip()
        if text:
            parts.append(label + "「" + _trim(text, 18) + "」（还在说）")
        elif settled:
            parts.append(label + "刚说完「" + _trim(settled, 18) + "」（这一刻没出字）")
        else:
            parts.append(label + "（没出字）")
    return " · ".join(parts)


def _speech_lanes(speech: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """把按席的出字状态排成两路（固定主驾、副驾顺序）：谁没说话那一路就空着。"""
    lanes = []
    for seat in SEAT_ORDER:
        state = (speech or {}).get(seat) or {}
        text = str(state.get("text") or "").strip()
        settled = str(state.get("settled") or "").strip()
        lanes.append({
            "seat": seat,
            "label": _seats(seat),
            "text": text,
            "speaking": bool(state.get("speaking")) and bool(text),
            "settled": settled,
            "settled_at_ms": state.get("settled_at_ms"),
        })
    return lanes


def _echo_lanes(lanes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """这一次场景要写进去的那几路：有字的那几路都写（还在说标「说」、刚落定标「刚说」）。

    - 只有一路有字（单席开口 / 另一路没出过字）：与旧口径一致，场景只写那一路；
    - 两路都有字（正在说 / 刚说完的上一句）：**两路都写进这一句**，不再各席各一份；
    - 谁都没开口：这一句只凭画面（没字就不编字）。
    """
    return [lane for lane in lanes if lane["text"] or lane["settled"]]


def _read_scene(speech: dict[str, dict[str, Any]],
                visual: dict[str, Any]) -> dict[str, Any]:
    """离线规则引擎：两路出字 + 视频线索 → **当前场景**（一句车舱旁白 + 内部线索位）。

    v04 口吻：不第一人称（不写「我看到 / 我听到 / 我知道」），只描述谁在说什么、更像朝谁说。
    只描述场景，**不给「要不要接」的结论**：接不接由对话中枢判（真判断走 8009，见
    `hub_accept.score_accept`）。真模型接入点是 `mind_llm.MindLLMEngine`，产物同为那一句场景。
    """
    lanes = _speech_lanes(speech)
    echo = _echo_lanes(lanes)
    text = " ".join(lane["text"] or lane["settled"] for lane in echo).strip()
    text_all = " ".join(str((speech or {}).get(seat, {}).get("text") or "")
                        + " " + str((speech or {}).get(seat, {}).get("settled") or "")
                        for seat in SEAT_ORDER).strip()
    if not text:
        return {
            "scene": f"车里没人说话，{visual['note']}。"[:SCENE_MAX_CHARS],
            "intent": "无语音", "target": "无人说话", "topic": "安静",
            "confidence": 0.30 if visual["has_frame"] else 0.20,
            "reason": "两路都没有出字。",
            "cues": [],
            "_speakers": [],
        }

    car_hits: list[tuple[float, str, str, str]] = []
    for pattern, weight, intent, obj in CAR_CUES:
        hit = re.search(pattern, text_all or text)
        if hit:
            car_hits.append((weight, intent, obj, hit.group(0)))
    chat_hits: list[tuple[float, str]] = []
    for pattern, weight in CHAT_CUES:
        hit = re.search(pattern, text_all or text)
        if hit:
            chat_hits.append((weight, hit.group(0)))

    car_score = sum(item[0] for item in car_hits)
    chat_score = sum(item[0] for item in chat_hits)
    negated = _match(text_all or text, NEGATION_CUES)
    if negated:
        car_score = 0.0

    final = bool(not any(lane["speaking"] for lane in lanes))
    confidence = _clamp(
        0.58 + abs(car_score - chat_score) * 0.18 + (0.06 if final else 0.0),
        0.30, 0.97,
    )
    if not final:
        confidence = min(confidence, PARTIAL_CONF_CAP)

    intent, obj, cue = "闲聊/自语", "无", "无车控线索"
    if car_hits:
        best = max(car_hits, key=lambda item: item[0])
        intent, obj, cue = best[1], best[2], best[3]
    if chat_hits and car_score < 0.9:
        intent = "对人闲聊"

    topic = "—"
    if "导航" in intent:
        dest = re.search(r"(?:导航去|去|到|回|前往|带我(?:去|到))\s*([^，。？！,.?!]{2,16})", text)
        if dest:
            topic = dest.group(1).strip()
    if car_score >= 0.9 and topic == "—":
        topic = obj

    cues = sorted({item[3] for item in car_hits} | {item[1] for item in chat_hits})
    if negated:
        basis = "句子里出现了「不是跟你说」这类明确否认，像是同乘对话。"
        target = "乘客"
    elif chat_score > car_score and chat_score > 0:
        basis = "更像和同车乘客聊天（对人线索多于车控线索）。"
        target = "乘客"
    elif car_hits:
        basis = "出现了车控线索「" + "、".join(sorted({item[3] for item in car_hits}))[:24] + "」。"
        target = "车机"
    elif car_score > 0:
        basis = "有车控词，但还不像完整指令。"
        target = "不确定"
    else:
        basis = "没听出明确诉求，像自语 / 环境话。"
        target = "自语/环境"

    target = target if not negated else "乘客"
    # 一句车舱旁白（≤30 字，v04 口吻）：说清「谁在说什么、更像朝谁说」；不出模块名、不第一人称、不给接不接结论。
    # 注意：`tail` 是对「更像朝谁说」的**推断**（页面上给人看的那半句），不是事实。
    # 接不接（`hub_accept`）只拿 `desc` 那半句（谁在说什么），不把这条推断送回去当证据——
    # 否则判断变成复述自己：2026-11-17 实测「今天的天气怎么样」带 tail 只有 0.26、剥掉是 0.86。
    if negated:
        tail = "不是在跟车说话。"
    elif car_score >= 0.9:
        tail = "像在对车说。"
    elif chat_score > car_score and chat_score > 0:
        tail = "像在跟旁边人聊天。"
    elif car_score > 0:
        tail = "还没听全。"
    else:
        tail = "像在自言自语。"
    speakers = [(lane["seat"], lane["text"] or lane["settled"], bool(lane["speaking"]))
                for lane in echo]
    scene = _scene_line(speakers, tail)
    # `desc` = 同一句旁白去掉那条「更像朝谁说」的推断（只剩谁在说什么）：只给「接不接」当输入。
    desc = _scene_line(speakers, "")
    return {
        "scene": scene, "desc": desc, "intent": intent, "target": target, "topic": topic,
        "confidence": round(confidence, 3), "reason": basis,
        "cues": cues, "_speakers": [lane["seat"] for lane in echo],
    }


# 一次真模型调用按现有三档的实测档（glm-5.2 中位 2.27～2.45s、尾部出过 13.7s），用一个全局小线程池兜住：
# 每个会话最多 1 个在途请求，最新输入覆盖排队中的旧输入（防积压），页面节拍不被拖死。
_LLM_EXECUTOR = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mind-llm")

# 请求时机（2026-11-11 13:13 拍板，wiki `[[读心请求时机]]`）：只在「中枢把这句收住」与「没在说话、
# 画面确实变了」两处发请求。**不再按「partial 稳住一小段」或「ASR 自己报 final」触发**（那两条都不算
# 说完）；说话过程中不出暂定场景，等这句收住再读一次（带最完整的字 + 当时的画面）。

# 没开口（两路都没有在出字）时画面一变也要读一次场景：画面约每秒一帧，两次画面读之间至少隔这么久，
# 配合「最多 1 个在途 + 1 个排队（新画面覆盖排队旧画面）」不把请求堆死（画面一直在变也不每一帧都打）。
LLM_FRAME_MIN_INTERVAL = float(os.environ.get("MIND_FRAME_READ_INTERVAL", "1.0"))
# 循环口径第 ④ 种：什么都没变、距上一份场景满 10 秒，兜底再出一份，维持场景流不断线。
# 这是**读心循环自己的**节奏，与冷场检查（`QUIET_CHECK_AFTER_MS`）无关，不得混用。
MIND_IDLE_FALLBACK_MS = int(float(os.environ.get("MIND_IDLE_FALLBACK_MS", "10000")) or 10000)

_PENDING_TEXT = "（这一刻没有出字）"


class MindReading:
    """一个会话一份读心：同时吃主驾 / 副驾两路出字 + 同一张画面，只吐**一份**场景。

    `seat` 只作「没显式给席别时」的默认席（调试页 `/api/mind/tick` 一次只推一路用），
    真实两路（测试台双麦）都显式带 `seat=`。
    """

    def __init__(self, seat: str = "D", session_id: str = "", engine=None):
        self.seat = "D" if str(seat).upper() != "P" else "P"
        self.session_id = session_id or f"mind-{int(time.time() * 1000)}"
        self.started = time.monotonic()
        self.engine = engine
        self._lock = threading.RLock()
        self.reset()

    # ---- 输入 -------------------------------------------------------------
    def reset(self) -> None:
        self.tick_no = 0
        self.revision = 0
        # 两路各记自己这一刻出到的字：还在说 = partial；这句刚收住 = finals[-1]。
        self._seats: dict[str, dict[str, Any]] = {
            seat: {"partial": "", "finals": [], "at_ms": 0}
            for seat in SEAT_ORDER
        }
        # 画面：最近一帧**原样**（页面给的彩色 data URI）+ 形状；送模型与页面显示用的是同一段。
        self.frame: dict[str, Any] | None = None
        self.frame_prev: dict[str, Any] | None = None
        self.visual: dict[str, Any] = {"brightness": 0.0, "motion": 0.0,
                                       "note": "尚未收到画面", "has_frame": False}
        self.snapshot_data: dict[str, Any] | None = None
        # 缓存的视图对应第几版状态（revision）；结果落地 / 出字 / 画面变化后视图要重排才取得到最新的
        self._snapshot_rev = -1
        # 场景版本：出字或画面一变就 +1；中枢取「此刻已有的那份」时只用缓存，不重算
        self.scene_at_ms = 0
        # 真模型通路：只保留最新一次结果 + 一个在途 / 一个排队
        self._closed = False
        self._llm_last: dict[str, Any] | None = None
        self._llm_state = "idle"          # idle|pending|ok|fallback
        self._llm_error: dict[str, Any] | None = None
        self._llm_inflight: dict[str, Any] | None = None
        self._llm_queued: dict[str, Any] | None = None
        self._llm_seq = 0
        # 画面读（没开口也有画面）最近一次发出去的时刻：同样不积压、不每秒都发。
        # 初值 -inf：会话刚开就来的第一帧要能立刻读（而不是被节流窗压掉第一秒）。
        self._frame_read_at = float("-inf")
        # 最近一次「场景真的出了」的时刻（毫秒，会话起点起算）：10 秒兜底按它算间隔。
        self._last_scene_ms = 0
        # 这一份场景是因为什么出的：close / frame_change / idle_10s（页面与详情照实标）。
        self._trigger = "close"
        # 上一次「中枢把这句收住」时的输入指纹：同一句只读一次（判停说完后静音窗再收一次不重复发）。
        self._last_close_stamp = None
        # 已经出过的读心结果（2026-11-11 21:50 拍板：每次判断带上最近 5 次，不够就有几份带几份）。
        # 只记**真的出过**的那些（真模型落地的那一句 / 离线替身产出的那一句），不编造更早的。
        self._history: list[dict[str, Any]] = []
        # 已经**落地**的真模型结果序号（每落一份 +1）：给「收住那一刻要等这次读心回来再写记忆」用
        # （页面拿它等「这句收住触发的那次读心」，不拿上一句那份场景）。离线替身恒为 0。
        self._result_seq = 0
        # 「中枢把这句收住」触发的那几份结果（只追加、最多留几条）：内心戏页据此认出「我这次收住
        # 触发的那份读心」已经落地，再决定要不要写记忆。别的页不读它，行为不变。
        self._close_results: list[dict[str, Any]] = []

    def close(self) -> None:
        """会话被淘汰 / 重置：丢掉排队请求，在途结果回来后直接作废。"""
        with self._lock:
            self._closed = True
            self._llm_queued = None

    def result_seq(self) -> int:
        """已经落地的真模型结果序号（每落一份 +1；离线替身恒 0，不冒充「等到了」）。"""
        with self._lock:
            return self._result_seq

    def close_results(self) -> list[dict[str, Any]]:
        """「中枢收住」触发的那几份读心结果（按落地先后，最多几条）：只读、不改读心行为。

        每项：`{seq, seat, text, at_ms, scene, source, fallback}`。内心戏页用它认出「我这次收住
        触发的那一份」已经落地（`seq` 与收住那一刻的 `result_seq()` 比），再决定要不要写记忆。
        """
        with self._lock:
            return [dict(item) for item in self._close_results]

    def set_engine(self, engine) -> None:
        """换页面选的引擎：丢掉上一模型的在途/排队与结论，避免把旧模型判定显示成新模型结果。

        只换引擎、不动 ASR：用户切模型只是换判断通道，不是重置对话。
        """
        with self._lock:
            if engine is self.engine:
                return
            self.engine = engine
            self._llm_last = None
            self._llm_state = "idle"
            self._llm_error = None
            self._llm_queued = None
            self._llm_inflight = None
            # 旧引擎算出来的场景不能再被中枢取走（新引擎出结果前，中枢按「无场景」照常判）
            self.snapshot_data = None

    @property
    def partial(self) -> str:
        """默认席这一刻还在出的字（调试页 / 旧调用方兼容位；两路都在时请看 `seats_snapshot()`）。"""
        return self._seats[self.seat]["partial"]

    @property
    def finals(self) -> list[dict[str, Any]]:
        """默认席最近已落定的句子（兼容位）。"""
        return self._seats[self.seat]["finals"]

    def _now_ms(self) -> int:
        return int((time.monotonic() - self.started) * 1000)

    def _seat_of(self, seat: str | None) -> str:
        chosen = "D" if str(seat or self.seat).upper() != "P" else "P"
        return chosen if chosen in self._seats else self.seat

    def _speaking(self) -> bool:
        return any(state["partial"] for state in self._seats.values())

    def _speech_block(self, closed: tuple[str, str] | None = None) -> dict[str, dict[str, Any]]:
        """这一次读心要一起送进模型的两路出字（按席：这一刻出字 / 刚说完的上一句 / 在不在说）。

        `closed=(席别, 最完整的字)`：中枢刚把这句收住 → 这一席按「刚说完」写（还在出的字清空），
        用的就是那句最完整的字（不管 ASR 有没有给 final）。
        """
        block = {}
        for seat, state in self._seats.items():
            finals = state["finals"]
            text = state["partial"]
            settled = finals[-1]["text"] if finals else ""
            settled_at = finals[-1]["t_ms"] if finals else None
            if closed and closed[0] == seat:
                text, settled, settled_at = "", closed[1], self._now_ms()
            block[seat] = {
                "text": text,                        # 这一刻已经出到的字（没出字就是空）
                "settled": settled,                  # 刚说完的上一句（收住 / 只作上下文）
                "settled_at_ms": settled_at,
                "speaking": bool(text),
            }
        return block

    def _lanes(self) -> list[dict[str, Any]]:
        return _speech_lanes(self._speech_block())

    def _prior_texts(self, seat: str) -> list[str]:
        """本席最近已落定的句子（不含当前这句）：给真模型当上下文，只留两路各自的，不跨席混。"""
        finals = self._seats[seat]["finals"]
        return [item["text"] for item in finals[-3:-1]][-2:]

    # ---- 前几次的读心结果（2026-11-11 21:50 用户拍板：每次判断都带上最近 5 次） ----
    def _remember_result(self, scene: str) -> None:
        """记一份**已经出过**的读心结果；最多留最近 `MAX_PRIOR_RESULTS` 份。

        只记真的出过的那一份（真模型落地、或离线替身这一版产出的那一句）；同一句连着记（画面没变、
        出字没变）只留一份，不把同一句重复算成几次。不够 5 份时就有几份算几份，不补编更早的。
        """
        body = (scene or "").strip()
        if not body:
            return
        if self._history and self._history[-1]["scene"] == body:
            return
        self._history.append({"scene": body, "at_ms": self._now_ms()})
        while len(self._history) > MAX_PRIOR_RESULTS:
            self._history.pop(0)

    def _prior_results(self) -> list[dict[str, Any]]:
        """这一次判断要带上的前几次结果（更早 → 更近，最多 5 份）：给请求上下文用（带相对时间）。"""
        now = self._now_ms()
        return [{"scene": item["scene"], "at_ms": item["at_ms"],
                 "ago_ms": max(0, now - int(item["at_ms"]))}
                for item in self._history[-MAX_PRIOR_RESULTS:]]

    def _stamp(self) -> tuple:
        """这一刻的输入指纹（两路各：还在出的字 / 刚说完 / 时间）：判断「这一版读过没」。"""
        return tuple(
            (seat, state["partial"], state["finals"][-1]["text"] if state["finals"] else "",
             state["at_ms"])
            for seat, state in self._seats.items()
        )

    def ingest(self, *, text: str | None = None, final: bool = False, seat: str | None = None,
               image: str | None = None, width: int | None = None, height: int | None = None,
               brightness: float | None = None, motion: float | None = None) -> dict[str, Any]:
        """一次输入事件（某一路 ASR 增量 或 视频帧），返回最新快照。

        `seat` 是哪一路在出字（不给就用默认席；两路真麦必须显式给）。
        `image` 就是页面采好、要送模型的那张**彩色帧**（data URI，原样透传）；`width/height` 是它的
        尺寸，`brightness/motion` 是页面从同一帧算出的统计（没有就给 0，不另采一张图）。
        """
        with self._lock:
            seat_id = self._seat_of(seat)
            state = self._seats[seat_id]
            if image:
                from mind.mind_llm import frame_info, image_ok
                if not image_ok(image):
                    raise ValueError("frame.image must be a jpeg/png data URI")
                self.frame = frame_info(image, width, height)
                self.frame_prev = dict(self.frame)
                self.visual = _visual_block(brightness, motion)
                self.tick_no += 1
                # 画面一变就更新场景（不等中枢锁字）
                self.scene_at_ms = self._now_ms()
                self.revision += 1
                # 没在说话（两路都没有在出字）才有「画面变了就读」：还在说话时先不请求，
                # 等这句被中枢收住再读一次（那一次会带上最新画面 + 完整出字）。
                # 同一拍还带着出字时也不在这里读：那次画面只是给这句备着的「当时的画面」，
                # 请求等中枢收住再发（否则 ASR 的每一版字都会被当成一次「画面变了」）。
                if self.engine is not None and text is None and not self._speaking():
                    self._maybe_frame_read(seat_id)
            if text is not None:
                text = text.strip()
                if final:
                    state["partial"] = ""
                    if text:
                        state["finals"].append({"text": text, "t_ms": self._now_ms(), "final": True})
                        state["at_ms"] = self._now_ms()
                        self.scene_at_ms = self._now_ms()
                        self.revision += 1
                        # ASR 自己报的一段结束**不是**「说完了」：不在这里发请求，等中枢收住
                        # （`on_utterance_close`）再读；这里只把这句字记成「刚落定」。
                else:
                    if text != state["partial"]:
                        state["at_ms"] = self._now_ms()
                        self.scene_at_ms = self._now_ms()
                        self.revision += 1
                    state["partial"] = text
                    self.tick_no += 1
                    # 出字还在变 / 字稳住一小段，都不是请求理由：说话过程中不出「暂定场景」。
        return self.snapshot()

    def on_utterance_close(self, seat: str | None = None, *, why: str = "") -> None:
        """对话中枢把这句收住的那一刻（判停给「说完」/ 静音窗收口）：读一次「完整出字 + 当时画面」。

        **读心不自己判断说完没说完**：本方法只由中枢调用（`turn_session.TurnSession._confirm` 与调试页
        `/api/hub/tick` 的闸门结论）。带的是这句**最完整的字**（有 ASR final 就是那句末稿，没有就是最后
        一版 partial）＋ 收住这一刻**最近的那张画面**；这一席标「刚说完」（还在出的字清空），另一席如果
        还在说就照「还在说」一起带上——一份场景两路都在。

        同一句只读一次：同一份输入指纹重复收住（判停说完后静音窗再收一次 / 闸门反复取结论）不重复发。
        收住这次也占掉画面读的节流点：收住之后再来的帧不会立刻又打一次请求。
        """
        with self._lock:
            if self._closed or self.engine is None:
                return
            stamp = self._stamp()
            if stamp == self._last_close_stamp:
                return
            self._last_close_stamp = stamp
            closed_seat = self._seat_of(seat)
            state = self._seats[closed_seat]
            text = (state["partial"] or
                    (state["finals"][-1]["text"] if state["finals"] else "")).strip()
            self._frame_read_at = time.monotonic()
            self._schedule(closed_seat, True, closed=(closed_seat, text), trigger="close")

    def maybe_idle_fallback(self) -> str | None:
        """循环第 ④ 种：什么都没变、距上一份场景满 `MIND_IDLE_FALLBACK_MS`（默认 10 秒）→ 兜底再出一份。

        只在**没人还在说话**时兜（说着话等说完，与 §1 第 5 行同一条），且只补「场景流别断线」，
        不判停、不判要不要回答。返回这一次的触发原因（没发就返回 None），给上层逐次记账。
        这是**读心循环自己的**节奏，与冷场检查无关：不读 `QUIET_CHECK_AFTER_MS`，也不由冷场触发。
        """
        with self._lock:
            if self._closed or self.engine is None:
                return None
            if self._speaking():
                return None
            now = self._now_ms()
            last = self._last_scene_ms
            if now - last < MIND_IDLE_FALLBACK_MS:
                return None
            if self._llm_inflight is not None or self._llm_queued is not None:
                # 上一份还没回来 / 已排着：以最新这一次为准，不再叠加一份
                return None
            self._schedule(self.seat, True, trigger="idle_10s")
            return "idle_10s"

    def _maybe_frame_read(self, seat: str) -> None:
        """没在说话（两路都没有在出字）、但画面变了：按这一帧读一次场景。

        读心的通路不是「只有出字才工作」：没开口只要有画面，就要持续给出当前场景，模型回话后
        立刻推到对话流（推的活在上层 `ui/server.py` 的 driver 做，本模块只产场景）。
        画面约每秒一帧、而且一直在变：不每一帧都打——两次画面读之间至少隔 `LLM_FRAME_MIN_INTERVAL`；
        不与出字抢（还有人在说时这一路根本不会进来，等这句被中枢收住再读）。
        刚收住的上一句只作上下文带进去（两路各自的），但这不是「这句说完没完」的判断。
        """
        with self._lock:
            if self._closed:
                return
            now = time.monotonic()
            if now - self._frame_read_at < LLM_FRAME_MIN_INTERVAL:
                return
            self._frame_read_at = now
            # 没人在说：这一版不是「还在出字」，按「画面为主、刚收住的字作上下文」读。
            self._schedule(seat, True, trigger="frame_change")

    # ---- 真模型调度（后台线程；ingest 不阻塞）---------------------------------
    def _schedule(self, seat: str, final: bool,
                  closed: tuple[str, str] | None = None, trigger: str = "close") -> None:
        with self._lock:
            if self._closed:
                return
            self._llm_seq += 1
            frame = dict(self.frame) if self.frame else None  # 本拍没带帧就用最近一帧，保证每次判定都是多模态
            speech = self._speech_block(closed)
            lanes = _speech_lanes(speech)
            # 这一次读心里的「主文本」：正在出字的那一路（多路时按主驾优先），只给不吃 `speech` 的老引擎兜底。
            text = next((lane["text"] for lane in lanes if lane["speaking"]), "") \
                or next((lane["settled"] for lane in lanes if lane["settled"]), "")
            pivot = next((lane["seat"] for lane in lanes if lane["speaking"] and lane["text"]),
                         seat)
            self._llm_queued = {
                "seq": self._llm_seq, "seat": pivot, "text": text, "final": bool(final),
                "speech": speech, "lanes": lanes, "stamp": self._stamp(),
                "image": (frame or {}).get("data_uri"),
                "image_w": (frame or {}).get("w"), "image_h": (frame or {}).get("h"),
                # 这一拍真正送进模型的那一帧（页面「详情」照它显示：与模型看到的像素一一对应）。
                # 原来只留 image/w/h、不留这一份，快照那边取 `_llm_last["frame"]` 永远是空，
                # 页面就无从证明「这一次是多模态还是纯文字」——补上，供逐次追溯。
                "frame": ({"image": frame.get("data_uri"), "w": frame.get("w"), "h": frame.get("h"),
                           "bytes": frame.get("bytes"), "format": frame.get("format")}
                          if frame else None),
                "visual": dict(self.visual),
                "prior_texts": self._prior_texts(pivot),
                # 这次判断要带上的前几次结果（最多 5 份；不够就有几份带几份）：发起这一刻记下来，
                # 结果回来时页面照这份显示「那一次判断实际带了哪几份」，不在事后补算。
                "prior_results": self._prior_results(),
                # 记录发起时的引擎：页面中途换模型时，在途结果不能算到新模型头上
                "engine": self.engine,
                # 这一次是「中枢把这句收住」触发的（带席别 + 那一句）：结果落地时记下来，
                # 内心戏页据此等「自己这次收住」的那份读心（只读字段，不改判断）。
                "closed": closed,
                # 这一份是哪种情况出的（句子收住 / 画面变了没人说话 / 满 10 秒兜底）：逐次可追溯
                "trigger": trigger,
            }
            self._trigger = trigger
            self._llm_state = "pending"
            if self._llm_inflight is None:
                self._take_and_submit()

    @staticmethod
    def _accepts_param(judge, name: str) -> bool:
        """这个引擎吃不吃某个关键字参数（`speech` / `prior_results`）：不吃就只传旧的字段，
        不为难旧实现 / 假引擎（老引擎按老签名照样能跑）。"""
        try:
            import inspect
            parameters = inspect.signature(judge).parameters.values()
        except (TypeError, ValueError):
            return False
        return any(item.name == name or item.kind is inspect.Parameter.VAR_KEYWORD
                   for item in parameters)

    @classmethod
    def _accepts_speech(cls, judge) -> bool:
        """这个引擎吃不吃 `speech=`（两路分栏）。"""
        return cls._accepts_param(judge, "speech")

    def _take_and_submit(self) -> None:
        request = self._llm_queued
        self._llm_queued = None
        if not request:
            return
        self._llm_inflight = request
        _LLM_EXECUTOR.submit(self._llm_work, request)

    def _llm_work(self, request: dict[str, Any]) -> None:
        engine = request.get("engine") or self.engine
        extra = {"speech": request.get("speech")} if self._accepts_speech(engine.judge) else {}
        # 前几次结果只给吃这条上下文的引擎（真模型走 `build_messages(prior_results=…)`）
        if request.get("prior_results") and self._accepts_param(engine.judge, "prior_results"):
            extra["prior_results"] = list(request["prior_results"])
        try:
            out = engine.judge(text=request["text"], visual=request["visual"],
                               seat=request["seat"], final=request["final"], image=request.get("image"),
                               image_w=request.get("image_w"), image_h=request.get("image_h"),
                               prior_texts=request.get("prior_texts"), **extra)
        except Exception as error:  # 失败也要落判：回落规则，但显式记账，不冒充模型结果
            out = self._fallback_out(request, error, engine)
        with self._lock:
            if self._closed or self._llm_inflight is not request:
                return   # 已关会话 / 已换引擎：这份结果不作数（页面按新引擎重新判定）
            merged = {**request, **out, "at_ms": self._now_ms()}
            # 引擎回填的 frame 只有 data_uri / w / h / bytes；页面「详情」按 frame.image 显示
            # 「读心那次请求送的图」。这里以请求里那一份为准（它带 image），只把统计合进来，
            # 不让回填把 image 盖掉——否则多模态与纯文字在页面上分不出来。
            asked, echoed = request.get("frame"), out.get("frame")
            if asked or echoed:
                frame = dict(asked or {})
                for key, value in (echoed or {}).items():
                    if key not in ("image",) and frame.get(key) is None:
                        frame[key] = value
                frame.setdefault("image", (echoed or {}).get("data_uri") or (echoed or {}).get("image"))
                if (echoed or {}).get("data_uri"):
                    frame.setdefault("data_uri", echoed["data_uri"])
                merged["frame"] = frame
            self._llm_last = merged
            self._llm_state = "fallback" if out["engine"].get("fallback") else "ok"
            self._llm_error = out["engine"].get("error")
            self.scene_at_ms = self._now_ms()      # 模型回话也算一次场景更新
            self._last_scene_ms = self.scene_at_ms  # 10 秒兜底按「上一份场景」算间隔
            self.revision += 1
            self._result_seq += 1                  # 这一份读心已经落地（收住那一刻等的就是它）
            closed = request.get("closed")
            if closed:
                # 这份是「中枢收住某一句」触发的那一份：记下来，给内心戏页等「自己这次收住」的读心。
                self._close_results.append({
                    "seq": self._result_seq, "seat": closed[0], "text": closed[1],
                    "at_ms": self._now_ms(),
                    "scene": ((out.get("result") or {}).get("scene") or ""),
                    "source": (out.get("engine") or {}).get("source"),
                    "fallback": bool((out.get("engine") or {}).get("fallback")),
                })
                del self._close_results[:-8]
            self._llm_inflight = None
            # 这一份结果已经出过：记进「前几次的结果」，下一判断要把它带上（自身不重复带自己，
            # 因为下一次请求的 prior_results 是在 `_schedule` 那一刻取的，见下）。
            self._remember_result((out.get("result") or {}).get("scene") or "")
            if self._llm_queued:
                # 排队中的下一拍如果是在这份结果落地**之前**排进去的，它带着的是旧的前几次列表；
                # 这里补一次：让它带上刚出的这一份（不重发请求，只换上下文指纹）。
                queued = self._llm_queued
                queued["prior_results"] = self._prior_results()
                self._take_and_submit()

    def _fallback_out(self, request: dict[str, Any], error: Exception, engine=None) -> dict[str, Any]:
        judged = _read_scene(request.get("speech") or {}, request["visual"])
        kind = getattr(error, "kind", type(error).__name__)
        detail = getattr(error, "detail", "") or str(error)
        model = getattr(engine or self.engine, "model", DEFAULT_MODEL)
        return {"result": judged, "engine": {
            "name": model, "model": model, "mode": "rule_fallback", "source": "rules_fallback",
            "fallback": True, "error": {"kind": kind, "detail": str(detail)[:200]},
            "note": "真模型失败/超时 → 回落离线规则；这不是真模型结果",
        }}

    # ---- 输出 -------------------------------------------------------------
    def _pending_judgement(self, lanes: list[dict[str, Any]]) -> dict[str, Any]:
        model = getattr(self.engine, "model", DEFAULT_MODEL)
        text = next((lane["text"] for lane in lanes if lane["speaking"]), "") \
            or next((lane["settled"] for lane in lanes if lane["settled"]), "")
        echo = _echo_lanes(lanes)
        who = "、".join(lane["label"] for lane in echo) or "车里"
        return {
            "scene": f"{who}：『{text or _PENDING_TEXT}』→ 真模型（{model}）读场景中…",
            "intent": "读取中", "target": "不确定", "topic": "—",
            "confidence": 0.0,
            "reason": "真模型读场景中；结果回来后替换（这不是规则结论）。",
            "cues": [], "finalized": False, "_speakers": [lane["seat"] for lane in echo],
        }

    def _engine_block(self, pending: bool) -> dict[str, Any]:
        last = self._llm_last
        if last:
            block = dict(last["engine"])
            block["pending"] = pending
            block["latency_ms"] = last["engine"].get("latency_ms")
            block["at_ms"] = last["at_ms"]
            return block
        try:
            block = dict(self.engine.describe())
        except Exception:  # 描述失败不影响主通路
            block = {"name": DEFAULT_MODEL, "mode": "litellm_vl", "source": "llm"}
        block.update(pending=True, mode=block.get("mode", "litellm_vl"),
                     note=block.get("note") or "真模型首轮判断中")
        return block

    def _scene_block(self, judged: dict[str, Any], lanes: list[dict[str, Any]],
                     final: bool, pending: bool,
                     prior_results: list[dict[str, Any]] | None = None,
                     prior_used: bool = False) -> dict[str, Any]:
        """读心唯一产物：当前场景（含版本与更新时间，供中枢取「此刻已有的那份」）。

        `seats` 是**这一次读心里带进去的两路**（谁在出字 / 谁刚说完 / 谁没出字）——页面据此看得见
        「同一份场景里两路都在」。场景本身的席别：只写了一路就是那一路，两路都写了就是「两席」。

        `prior_results` = 这一次判断实际带在上下文里的前几次结果（最多 5 份，更早 → 更近）：
        `prior_used=True` 表示真模型这次判断真的读到了它们；离线规则替身只是把它们一起记账给页面看
        （`prior_used=False`，不冒充已用）。
        """
        echo = _echo_lanes(lanes)
        speakers = [lane["seat"] for lane in echo]
        if len(speakers) > 1:
            seat, seat_label = "shared", "主驾和副驾"
        elif speakers:
            seat, seat_label = speakers[0], _seats(speakers[0])
        else:
            seat, seat_label = "shared", "车里"
        carried = [{"scene": item["scene"], "ago_ms": item["ago_ms"]}
                   for item in (prior_results or [])][-MAX_PRIOR_RESULTS:]
        return {
            "summary": judged["scene"],
            # 只描述「谁在说什么」，不含「更像朝谁说」的推断：`hub_accept` 只拿它当输入，
            # 免得判断变成复述读心自己下的结论（页面上仍显示 `summary`）。
            "desc": str(judged.get("desc") or ""),
            "intent": judged["intent"],
            "target": judged["target"],
            "topic": judged["topic"],
            "cues": judged["cues"],
            "confidence": judged["confidence"],
            "basis": judged["reason"],
            "visual": dict(self.visual),
            "seat": seat,
            "seat_label": seat_label,
            "speakers": [_seats(speaker) for speaker in speakers],
            "seats": [dict(lane) for lane in lanes],
            "reading": _lane_line(lanes),
            # 这一次判断带上的前几次结果（页面照它显示「前后画面连上了没有」）
            "prior_results": carried,
            "prior_count": len(carried),
            "prior_used": bool(prior_used),
            "prior_limit": MAX_PRIOR_RESULTS,
            "revision": self.revision,
            "updated_at_ms": self.scene_at_ms,
            "finalized": bool(final) and not pending,
            "pending": pending,
        }

    def snapshot(self) -> dict[str, Any]:
        """页面快照：两路出字 + 当前场景 + 引擎信息（**没有「接不接 / 进不进 Agent」结论**）。"""
        with self._lock:
            speech = self._speech_block()
            lanes = _speech_lanes(speech)
            final = bool(not any(lane["speaking"] for lane in lanes))
            pending = False
            prior_used = False
            if self.engine is None:
                judged = _read_scene(speech, self.visual)
                source = "rules_offline"
                engine_block = {"name": "fast_multimodal_stub", "mode": "offline",
                                "note": "离线规则替身；换真模型时协议不变"}
                # 离线段没有「判断」这回事：前几次结果只记给页面看，不冒充已经用过（prior_used=False）。
                prior_results = self._prior_results()
            else:
                last = self._llm_last
                if last is None:
                    judged = self._pending_judgement(lanes)
                    source = "llm_pending"
                    pending = True
                    prior_results = []
                else:
                    judged = last["result"]
                    source = last["engine"].get("source", "llm")
                    pending = (self._llm_state == "pending"
                               or last.get("stamp") != self._stamp())
                    # 这一次判断发起那一刻带上的那几份（不在事后补算）
                    prior_results = list(last.get("prior_results") or [])
                    prior_used = bool(not last["engine"].get("fallback") and prior_results)
                engine_block = self._engine_block(pending)
                engine_block["state"] = self._llm_state
                if self._llm_error:
                    engine_block.setdefault("error", self._llm_error)

            scene = self._scene_block(judged, lanes, final, pending,
                                      prior_results=prior_results, prior_used=prior_used)
            if self.engine is None:
                # 离线替身每产出一句也是「已经出过的读心结果」：记进前几次（同一句只记一次）。
                self._remember_result(judged.get("scene") or "")
            scene["source"] = source
            scene["trigger"] = self._trigger
            scene["latency_ms"] = engine_block.get("latency_ms")
            # 这一句场景**真正送进模型的那张彩色帧**：页面照它显示，与模型看到的像素一一对应；
            # 场景还在读（pending）或这一版是规则兜底时没有图，就给 None，不拿旧图冒充。
            frame = None
            if not pending and self._llm_last is not None:
                frame = dict(self._llm_last.get("frame") or {}) or None
                if frame:
                    frame["at_ms"] = self._llm_last.get("at_ms")
            focus = self._seats[self.seat]
            snapshot = {
                "session_id": self.session_id,
                "seat": self.seat,
                "seat_label": _seats(self.seat),
                "tick": self.tick_no,
                "t_ms": self._now_ms(),
                "asr": {"partial": focus["partial"], "finals": list(focus["finals"])},
                # 两路各自出到的字（调试页 / 上层想知道「另一路现在在说什么」时读这里）
                "seats": {seat: {"label": _seats(seat),
                                 "partial": state["partial"],
                                 "finals": list(state["finals"]),
                                 "speaking": bool(state["partial"])}
                          for seat, state in self._seats.items()},
                "scene": scene,
                "frame": frame,
                "engine": engine_block,
            }
            self.snapshot_data = snapshot
            self._snapshot_rev = self.revision   # 这份视图对应第几版状态（结果落地后能被再取到）
            return snapshot

    def last_frame(self) -> dict[str, Any] | None:
        """最近一次真的落地了的读心请求送进去的那张图（与 `snapshot()["frame"]` 同一份数据）。

        `snapshot()` 在「还在读下一版」时会把 frame 置空（不拿旧图冒充这一次）；页面推场景时要的是
        「这份场景配的那张图」，所以单独给一个不受 pending 影响的读法。
        """
        with self._lock:
            frame = dict((self._llm_last or {}).get("frame") or {})
            return frame or None

    def scene_snapshot(self) -> dict[str, Any] | None:
        """中枢取「读心此刻已有的场景」用：**不调模型、不阻塞**，只把已有结果读出来。

        中枢（`hub_gate.GateKeeper`）在发起两路判断的那一拍调一次；读心自己按出字 / 画面变化更新，
        两边互不等待（wiki §4）。没有场景时返回 `None` → 中枢按「无场景」照常判。
        两席的闸门取的是**同一份**（这一份里已经同时带了两路出字）。

        缓存视图比状态旧（典型：真模型刚把结果落下来，上一次视图还是「读场景中…」那一版）时，
        就地重排一次视图再取——**场景本身来自缓存，不重新调模型**。这条是「读完立刻能看见」的关键：
        不重排就会一直等下一次出字 / 下一帧才把结果露出来。
        """
        with self._lock:
            snap = self.snapshot_data
            if snap is None or self._snapshot_rev != self.revision:
                snap = self.snapshot()
            if snap is None:
                return None      # 真模型还没读过：不假装有场景，也不为了取场景把模型堵在这里
            if snap.get("scene", {}).get("source") == "llm_pending":
                # 真模型还没读过这一版：这是「读场景中…」占位句，不是场景。
                # 中枢按「无场景」照常判（不把占位句当成读心给出的场景）。
                return None
            scene = dict(snap["scene"])
            scene["session_id"] = snap["session_id"]
            # 新鲜度由读心自己算（读心的时钟与中枢的会话时钟起点不同，不能拿中枢的 t_ms 相减）
            scene["age_ms"] = max(0, self._now_ms() - int(self.scene_at_ms or 0))
            return scene


_UNSET = object()   # 区分「没传引擎」（沿用 registry 默认）与「显式传 None」（规则离线档）


class MindRegistry:
    """UI 侧会话仓：按 session_id 存**一个会话的一份读心**（两路共用），超出保留数淘汰最旧。

    `engine` 可以每拍带进来（页面选择器切换的模型）：幂等，同一个引擎对象不会重置会话状态。
    `seat` 只是「这个会话的默认席」（调试页一次只推一路时用）；同一个 session_id 里换席不重建
    （两路本来就该是同一份读心），要清掉一路的字就 `reset` / `drop`。
    """

    def __init__(self, keep: int = 64, engine=None):
        self.keep = keep
        self.engine = engine
        self.sessions: "OrderedDict[str, MindReading]" = OrderedDict()

    def get(self, session_id: str, seat: str = "D", engine=_UNSET) -> MindReading:
        chosen = self.engine if engine is _UNSET else engine
        session = self.sessions.get(session_id)
        if session is None:
            session = MindReading(seat=seat, session_id=session_id, engine=chosen)
            self.sessions[session_id] = session
        else:
            # 同一个会话不因为「这次问的是另一席」重建：两路本来就在同一份读心里。
            # 只把默认席切过去（调试页一次只推一路，页面读出字用的就是这个默认席）。
            session.seat = "D" if str(seat).upper() != "P" else "P"
            if chosen is not session.engine:
                session.set_engine(chosen)
        self.sessions.move_to_end(session_id)
        while len(self.sessions) > self.keep:
            old = self.sessions.popitem(last=False)[1]
            old.close()
        return session

    def drop(self, session_id: str) -> None:
        session = self.sessions.pop(session_id, None)
        if session is not None:
            session.close()


# ---- 两条可复现演示（录好的 ASR 回放；视频由页面合成回放，帧统计照常进模块） -------------
DEMOS: list[dict[str, Any]] = [
    {
        "id": "chat",
        "name": "演示 A · 主驾和副驾闲聊",
        "desc": "主驾在和副驾聊天（「咱晚上去哪吃」）。读心只把场景读出来（朝乘客 / 闲聊）；接不接、进不进 Agent 由对话中枢在说完那一刻判。",
        "seat": "D",
        "video": "chat",
        "script": [
            {"t_ms": 200, "text": "你说", "final": False},
            {"t_ms": 550, "text": "你说今天这天气", "final": False},
            {"t_ms": 950, "text": "你说今天这天气可真舒服", "final": False},
            {"t_ms": 1450, "text": "你说今天这天气可真舒服，咱晚上", "final": False},
            {"t_ms": 2000, "text": "你说今天这天气可真舒服，咱晚上去哪吃", "final": True},
            {"t_ms": 3000, "text": "我听说望京", "final": False},
            {"t_ms": 3500, "text": "我听说望京新开了一家火锅", "final": True},
        ],
    },
    {
        "id": "command",
        "name": "演示 B · 主驾对车下指令",
        "desc": "主驾对车机下指令（「导航去望京SOHO」）。读心只读场景（意图 导航 / 朝车机）；说完没说完与接不接都在中枢并发判。",
        "seat": "D",
        "video": "command",
        "script": [
            {"t_ms": 200, "text": "导航", "final": False},
            {"t_ms": 600, "text": "导航去", "final": False},
            {"t_ms": 1100, "text": "导航去望京", "final": False},
            {"t_ms": 1600, "text": "导航去望京SOHO", "final": True},
            {"t_ms": 2600, "text": "尽量走高速", "final": False},
            {"t_ms": 3200, "text": "尽量走高速，别堵在路上", "final": True},
        ],
    },
    {
        "id": "cut",
        "name": "演示 C · 话还没说完",
        "desc": "主驾话说了一半（「帮我看看前面那个…」）。判停这一路说「还没说完」→ 中枢先不开闸；这期间接不接的结论不算数，等说完那一刻才认。",
        "seat": "D",
        "video": "command",
        "script": [
            {"t_ms": 200, "text": "帮我看看", "final": False},
            {"t_ms": 700, "text": "帮我看看前面", "final": False},
            {"t_ms": 1200, "text": "帮我看看前面那个", "final": True},
        ],
    },
]
