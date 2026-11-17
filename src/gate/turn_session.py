"""Streaming turn session: ASR updates + VAD → KEV EOT judging + tool shortlist → per-segment decisions.

One turn can consume several ASR segments: a landed decision seals the text it was made on (the
"consumption cursor"), emits `segment_consumed` and returns to LISTENING; whatever the user says next
is judged as the unconsumed suffix only. By default the turn is over the moment a decision lands on an
empty suffix (`close_turn_on_decision`); the old behaviour — keep the turn open for `max_silence_ms`
so a follow-up sentence joins the same turn — is still available by turning that flag off.

All state transitions happen on the caller's thread (the driver that calls tick()); the injected
KEV/LLM callables run in worker threads and their results are applied only if text_version is
still the newest one, otherwise they are dropped as stale.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass, fields
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.dont_write_bytecode = True

from core_pipeline_run import POC_ROOT, TOOL_SET, decide, select_tools
from gate.hub_gate import (GATE_CONTEXT, ROUTE_ACCEPT_UNAVAILABLE, ROUTE_AGENT, ROUTE_CONTEXT_ONLY,
ROUTE_WAIT)
# 期一（2026-11-16）应一声：判断在 gate 里并发跑，出不出声、说哪句由本席按三道闸决定。
from gate.hub_ack import ACK_ECHO_MS, ACK_STALE_MS, ack_chars, ack_text, decide_ack
# 期二（2026-11-16）节奏 + 垫句：节奏每版都判（在 gate 里），垫句只在「积极 + 已满 1.5 秒」问；
# 出不出、说哪句仍由本席决定。节奏不外溢：三个等待时长与开闸线一行不改。
from gate.hub_pace import (CUSHION_MIN_SPEECH_MS, CUSHION_STALE_MS, SPEAK_HOLD_MS, cushion_chars,
                           cushion_text, decide_cushion)
# 期三（2026-11-16）两人同时说四选：这边正在说 / 正在准备说，人又开口时问一次；
# 「让路」＝现网那条（逐字段不变），另外三条是新增出口。判断在 gate 里并发跑，
# 走哪条出口、插话放哪里（回到收听 / 排队 / 只进旁听 / 垫句后排队）仍由本席决定。
from gate.hub_floor import (FLOOR_CONTINUE, FLOOR_CUSHION_BACK, FLOOR_FINISH_FIRST, FLOOR_YIELD,
                            decide_floor, floor_label)
# 期四（2026-11-16）冷场三选：只在「安静到刻度、且真的没人说话」时按刻度问一次；
# 有人开口立刻清零；只接上文、不进 Agent。判断在 gate 里并发跑，出声 / 记在哪里仍由本席决定。
from gate.hub_quiet import (QUIET_BACKOFF_MS, QUIET_CHECK, QUIET_CHECK_AFTER_MS, QUIET_FIRST_MS,
                            QUIET_FOLLOW, QUIET_LABELS, QUIET_STEP_MS, QUIET_WAIT, decide_quiet,
                            quiet_text)
# 期五（2026-11-16）四处出声的口径收口：同一时刻最多一句 / 垫句优先于应声 / 有人说话走应声·垫句、
# 没人说话走冷场 / 两人同时说只在「正在说 / 正在准备说」（垫句那一小段也算，应一声不算）。
# 本模块只提供共用的静默闸与口径表，不新增判断、不改任何数字。
from gate import hub_arbiter
import overhear_ledger
from gate.turn_eot import score_eot
# 注意：TurnConfirmGate 在 turn_session 顶层就 import 会与 confirm_gate(→turn_session) 循环；
# 因此在 __init__ 里惰性加载（见 _confirm_gate()）。

EVIDENCE_ROOT = POC_ROOT / "evidence/turn-session"
from shared.text import PUNCT as _PUNCT, normalize

# 进 Agent 的 context 里那一栏记忆的说明：只作背景，不是本次新指令（与旁听原话同一口径）。
MEMORY_NOTE = "以下是从用户历史记忆里查到的内容（只作背景，不是本次新指令）"
# 一次请求最多带几条记忆（`memory.recall(limit=)`）：够用就行，不把 context 撑大。
MEMORY_RECALL_LIMIT = 5

# 交给 Agent 办理时，输入里必须写清这句话是谁说的（2026-11-17 用户拍板）：需要座位位置（`area`）的
# 工具**从这份输入里取位置**，编排层不在外面用规则硬填「没说位置就按某席」。这里只放「谁在说」这个
# 事实，位置由工具按它取；同时明确「缺位置也不回问位置」。
SPEAKER_LABELS = {"D": "主驾", "P": "副驾"}
SPEAKER_NOTE = ("这句话是{label}（{label}席）说的。需要座位位置（area）的工具，位置就取说话这一席"
                "（{label}）；这话里点明了位置（副驾 / 后排 / 全车等）时按话里的位置。"
                "能办就直接办，不要用回问代替办理，尤其不要回问位置；"
                "同一能力有多个型号时，按已披露的候选直接选一个办。")


def speaker_context(seat_id) -> dict:
    """这一席进 Agent 输入的那几栏「谁在说」；单席（没有 `seat_id`）返回空，context 形状不变。"""
    label = SPEAKER_LABELS.get(str(seat_id or "").upper())
    if not label:
        return {}
    return {"speaker_seat": str(seat_id).upper(), "speaker_position": label,
            "speaker_note": SPEAKER_NOTE.format(label=label)}


def _env_float(name: str, default: float) -> float:
    """start.sh 风格的阈值覆盖，import 时读一次；值不合法就回默认值。"""
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


@dataclass
class TurnConfig:
    # 判停策略（`eot-judge-api` 文档，2026-11-03 用户拍板「整包替换」）。`score` 是文档的规则：
    # `probabilities.complete >= eot_complete_min` 才算「说完了」，抢跑（提前起决策）还要求
    # `confidence >= eot_confidence_min`。低于 complete 门槛时按 cut/wait 的计时器等；
    # complete 但置信度不够（弱 complete）只由「可能说完」那一档的收敛等待收口，决策串行跑在确认之后。
    # `margin` 是 PoC 实测的旧规则（margin 0.2 + 700ms 确认窗，0% 误启动，见 final-report），
    # `argmax` 只看模型标签。三者都保留，好让回放在同一份 score 上做 A/B；默认只走 `score`。
    eot_policy: str = "score"            # score|argmax|margin
    eot_margin: float = 0.2
    spec_margin: float = 0.3
    eot_complete_min: float = _env_float("EOT_COMPLETE_MIN_SCORE", 0.80)
    eot_confidence_min: float = _env_float("EOT_CONFIDENCE_MIN", 0.70)
    speculative_llm: bool = True     # intentional: start the decision early, cancel it on reversal
    eot_input: str = "last_segment"     # last_segment|turn_text
    # 2026-11-04 用户拍板（[[无确认窗流程]] §1.1 三态挂窗）：撤掉「判完→动手之间整条链路无条件等
    # 700ms」的确认窗——强 complete（说完 + confidence 过线）当刻 CONFIRMED，直接进选工具/生成。
    # 这个值只剩一个作用：弱 complete（「可能说完」那一档）的收敛等待，约 0.7s 看它是不是真说完；
    # 期间有续句就回 LISTENING 继续听，整段时间仍由 max_silence_ms(2s) 封顶。
    # 不许拿它当「整条链路的确认窗」再装回来（[[忌讳]] §1 第 30 条）；它也不是 cut/wait 那套。
    confirm_silence_ms: int = 700
    cut_wait_ms: int = 1500
    think_wait_ms: int = 3000
    max_silence_ms: int = 2000
    # 用户 2026-11-03 拍板「去掉回合 2s 尾巴」：决策一落地（后缀已空、没有在途决策）就当回合结束，
    # 不再等「空后缀 + 静音 max_silence_ms」才收口（实测那一段恒 ~2002ms，纯等待）。
    # 代价：用户紧接着补一句会变成新一轮（on_asr → turn_reopen → _reset_turn），前端要按新一轮渲染。
    # False 保留改动前的行为（最后一次消费后再撑 max_silence_ms，这期间补一句并进同一轮），
    # 多段消费引擎的验收用例（tests/poc-streaming-c1c4.py C2/C4/C5）仍跑这个口径。
    close_turn_on_decision: bool = True
    eot_timeout_ms: int = 900
    eot_max_inflight: int = 3        # >1 removes single-flight starvation: every prefix gets scored
    # Must stay below the ASR partial interval, otherwise each pending call is replaced before it fires
    # and prefixes stop being judged at all (measured at 150 ms/prefix).
    debounce_ms: int = 80
    reuse_tools_if_text_unchanged: bool = True
    # M3/M4 latency work: tools run concurrently (single-flight starves the newest text) and fire
    # without debounce, so the shortlist is usually ready before the EOT verdict lands; if it is not,
    # the decision may start on a slightly older shortlist and is reissued once it changes.
    tools_max_inflight: int = 1        # measured: concurrency does NOT help (see spec 的矩阵)
    allow_stale_shortlist: bool = True
    shortlist_reissue: bool = True
    warm_llm: bool = True               # one tiny completion on the first partial (hides cold start)
    tool_set: str = TOOL_SET              # the PoC's only tool set: core_score (242 tools / 17 domains)
    # 期一（2026-11-16）：应一声——中枢的「陪着说」，不是回答。`ack_enabled=False` 只用于 A/B 回放
    # （对照「收住时刻、开闸结论与改动前一致」）；频次与字数两道闸的数值在 `gate/hub_ack.py`。
    ack_enabled: bool = True
    ack_gap_ms: int = 3000
    # 期二（2026-11-16）：垫一句——同一句「陪着说」，但**记住**（进上下文）、仍不开闸。
    # `cushion_enabled=False` 只用于 A/B 回放（对照「收口时刻、开闸结论与期一逐毫秒一致」），
    # 关掉时**连垫句这一问都不发**（节奏照判，它不影响任何计时）。
    cushion_enabled: bool = True
    cushion_gap_ms: int = 1000      # 卡面只给了「出声后 1 秒内不再出声」；垫句不再另加间隔（不发明数）
    # 期三（2026-11-16）：两人同时说四选——这边正在说 / 正在准备说，人又开口时问一次。
    # `floor_enabled=False` 只用于 A/B 回放（对照「让路与改动前逐字段一致」）：关掉时
    # **连这一问都不发**，回到改动前的「人一开口就作废本轮」那条老路。
    floor_enabled: bool = True
    # 期四（2026-11-16）：冷场三选——安静到刻度、真的没人说话才问一次（再等 / 主动接一句 / 问一句）。
    # `quiet_enabled=False` 只用于 A/B 回放（对照「没有冷场需要开口的那一段，行为与关掉冷场时一致」）。
    quiet_enabled: bool = True

    def __post_init__(self):
        if self.tool_set != TOOL_SET:
            raise ValueError("Unknown tool_set: only " + TOOL_SET + " is available")
        if self.eot_policy not in ("score", "argmax", "margin"):
            raise ValueError("Unknown eot_policy: " + str(self.eot_policy))

    @classmethod
    def from_dict(cls, data):
        known = {item.name for item in fields(cls)}
        unknown = set(data or {}) - known
        if unknown:
            raise ValueError("Unknown TurnConfig fields: " + ", ".join(sorted(unknown)))
        return cls(**data)


class TurnSession:
    def __init__(self, cfg: TurnConfig, on_event, *, clock=None, eot_fn=score_eot,
                 select_tools_fn=select_tools, decide_fn=decide, session_id=None,
                 evidence_root=EVIDENCE_ROOT, warm_fn=None, confirm_gate=None,
                 seat_id=None, mic_id=None, gate=None, overhear=None, mind=None, memory=None):
        self.cfg = cfg
        self.on_event = on_event
        self.clock = clock or (lambda: time.monotonic() * 1000.0)
        self.eot_fn = eot_fn
        self.select_tools_fn = select_tools_fn
        self.decide_fn = decide_fn
        self.warm_fn = warm_fn
        # 双席（C1）：一席 = 一份 TurnSession，状态机/锚点/版本/消费游标都是本席私有的。
        # `seat_id` / `mic_id` 只作标记（单席调用保持 None，事件形状不变）；串话去重不在本卡
        # 范围（用户 2026-11-03 拍板：前端各自给出干净的两路 ASR，Agent 侧只保证不串席）。
        self.seat_id = seat_id
        self.mic_id = mic_id
        # 对话式二次确认编排器（默认 mock：不连真实 KEV）。TurnSession 只在第一轮决策
        # 命中池内高危工具时启用它，叠加在主循环上，不改变既有分段/消费机制。
        self.confirm_gate = confirm_gate if confirm_gate is not None else self._default_confirm_gate()
        self.session_id = session_id or ("s-" + str(int(time.time() * 1000)))
        self.evidence_root = Path(evidence_root) if evidence_root else None
        self._pool = ThreadPoolExecutor(max_workers=4 + max(0, max(cfg.eot_max_inflight, cfg.tools_max_inflight) - 1))
        self.context = {"home_address_configured": True, "company_address_configured": True, "synthetic": True}
        # 「这句话是主驾还是副驾说的」跟着这份 context 进 Agent（`_request_context` / `_open_agent_context`
        # 都是从它起拼）：位置由工具从这份输入里取，编排层不替它填。
        self.context.update(speaker_context(seat_id))
        self.events = []
        self.turns = []
        self.state = "LISTENING"
        self.turn_start_ms = None
        self.segment = ""
        self.segments = []
        self.version = 0
        self.last_change_ms = None
        self.last_speech_end_ms = None
        self.speech_start_ms = None
        self.t_last_word = None
        self.t_confirmed = None
        self.t_llm_start = None
        self.t_decision = None
        self.t_decision_last = None
        # Consumption cursor: segments[:consumed_segments] were sealed by a landed decision, so the
        # unconsumed suffix is segments[consumed_segments:] + the in-use segment (see suffix_text()).
        self.consumed_version = 0
        self.consumed_segments = 0
        # Dedup basis: the *whole* consumed text, not just the last sealed segment. Volcengine
        # re-sends a whole utterance, so once several segments were consumed the re-send prefixes
        # the full consumed text while `consumed_tail` (kept for compatibility/metrics) does not.
        self.consumed_text = ""
        self.consumed_tail = None
        # 跨轮前缀（2026-11-11 真麦实测）：同一个 ASR 会话不切 utterance 时，火山会把**整段话**反复
        # 重发，重发里还带着上一轮已经消费掉的前缀。记住「这段语音已经消费到哪」，下一轮只认它
        # 后面的新字——否则后一句开头会把前一句的字再叠一遍，判停 / 接不接也会拿旧话一起判。
        self.utterance_consumed_text = ""    # 这段语音累计已消费（normalize 后）
        self._turn_prefix = ""               # 本轮开始时，这段语音已经消费掉的累计前缀
        self._text_hold = []                 # 上一句还在判时发来的文字输入（排队等收口，不并进同轮）
        self.consumptions = []
        self.consumed_count = 0
        self.version_t_ms = None
        # A landed decision is "work just done", not silence: the end-of-turn silence window restarts
        # when a segment is consumed. Only relevant while the turn stays open after a decision
        # (`close_turn_on_decision=False`); with the default the turn closes right at that decision
        # (otherwise a decision slower than max_silence_ms would close the turn the moment it lands
        # and the user's next segment could never arrive).
        self.t_last_consumed = None
        self._stop_requested = False
        self.timeline_path = None
        self.turn_done = False
        self.turn_index = 1
        self.segment_closed = False
        self._jobs = {"llm": None}
        self._eot_jobs = []
        self._tools_jobs = []           # M3: concurrent shortlist calls (was a single-flight slot)
        self._eot_pending = None
        self._tools_pending = None
        self._tools_cache = None
        self._tools_result = None
        self._llm_prepared = None
        self.t_tools_ready = None
        self.t_first_partial = None
        self.t_llm_start_last = None
        self.warm_ms = None
        self._warmed = False
        self._warm_future = None
        self._shortlist_source = None
        self._shortlist_age_ms = None
        self.counters = {"eot_calls": 0, "eot_skipped": 0, "eot_stale": 0, "tool_calls": 0,
                         "llm_calls": 0, "llm_wasted": 0, "n_reissue": 0, "consumed": 0,
                         "turn_voids": 0, "acks": 0, "ack_suppressed": 0, "ack_echo_drops": 0,
                         "cushions": 0, "cushion_suppressed": 0,
                         # 期三：四选（每一回插话一次）与四条出口各自走了几次
                         "floor_asks": 0, "floor_yields": 0, "floor_finish_first": 0,
                         "floor_continues": 0, "floor_cushion_backs": 0, "floor_timeouts": 0,
                         "floor_queued": 0, "floor_released": 0, "floor_cushions": 0,
                         "floor_cushions_blocked": 0,
                         "quiet_asks": 0, "quiet_wait": 0, "quiet_follows": 0,
                         "quiet_checks": 0, "quiet_speak": 0, "quiet_blocked": 0,
                         "quiet_cleared": 0, "quiet_timeouts": 0}
        self.eot_scores = None
        self.decision = None
        # 对话式二次确认：挂起的第一轮决策 + 解析结果（CONFIRM_WAIT 期间保存）
        self.confirm_pending = None
        self.confirm_resolved = None
        # 中枢并发闸门（可选，见 `hub_gate.GateKeeper`）：一次出字上并发「说没说完」（本席判停）
        # 与「接不接」（中枢），只在「判定说完」那一刻、同一版字上开闸。**开闸前不进 Agent**：
        # 工具披露与决策都只在 `_gate_open_version == self.version` 时发起。不传 gate 时行为与旧链一致。
        self.gate = gate
        self._gate_open_version = 0
        self._gate_waiting_version = None
        self._gate_verdict = None
        self.context_consumed = 0
        # 旁听台账（方案 B，wiki [[中枢并发闸门]] §13）：一本会话级台账、两席共用（跨席可见）。
        # 「说完但不接」的原话按席留最近 3 句、标「不是对车说的」；只在开闸那一轮由本席拼一次
        # 两栏（场景 ＋ 已标记原话）带进 Agent。不传就退化成旧行为（不留、不带）。
        self.overhear = overhear
        # 读心（可选，wiki `[[读心请求时机]]`）：中枢把这句收住的那一刻（`_confirm`）通知它读一次，
        # 「完整出字 + 当时画面」。不传就不通知（读心仍只在「没说话且画面变了」时自己读）；
        # 通知是单向的，读心不反过来驱动中枢，两边互不等待。
        self.mind = mind
        # 记忆（wiki `四模块职责与接口契约` §1 记忆行）：中枢把这句**收住那一刻**写一刀
        # （完整原话 + 这一刻的场景；开闸进 Agent / 只进上下文两条路由都写），Agent **发起请求那一刻**
        # 按这句原话查一次（不冻结在开闸那一刻）。不传就完全不碰记忆（离线回归照旧）。
        self.memory = memory
        self._memory_versions = set()    # 这一版字已经写过记忆（静音窗再收一次不写第二刀）
        self._agent_lines = []           # 开闸那一刻台账里的原话行（请求时刻重拼场景栏时用）
        self._agent_context = None       # 开闸那一刻拼好的「进 Agent 的上下文」（每轮一份）
        self._overhear_version = 0       # 已记进台账的最近一版字（同一版只记一次）
        # 期一（2026-11-16）应一声：中枢的「陪着说」。出声出口 = 台面出字（`ack_spoken` 事件）＋
        # 播报桩；**不接真 TTS**（[[边界]]「不做 TTS」不变）、不进上下文、不进 Agent、不开闸。
        # 判断在 gate 里与同一版字并发跑（`gate.ack(version)`）；出不出声由这里按三道闸决定。
        self.ack_spoken_total = 0        # 跨轮累计（台面上要能数出应声次数）
        self.ack_log = []                # 每次出声：{t_ms, text, chars, gap_ms, version, turn}
        self._ack_last_ms = None         # 上一次出声的时刻（同一轮至少隔 3 秒 / 出声后 1 秒内不再出声）
        self._ack_last_text = None       # 上一次出声的那句（回声丢弃要跟它比）
        self._ack_tried = set()          # 已经问过 / 已经看过的那几版字（同一版只算一次）
        # 期二（2026-11-16）节奏 + 垫句：垫一句也是「陪着说」，但它**记住**（进本席上下文台账
        # `hub_said`，跨轮留存），仍**不进 Agent、不选工具、不执行、不开闸**。
        self.pace_last = None            # 这一拍实际的节奏（`hub_pace` 事件回填，台面/摘要可见）
        self.cushion_spoken_total = 0    # 跨轮累计垫了几句
        self.cushion_log = []            # 每次垫句：{t_ms, text, chars, gap_ms, version, turn, pace, speech_ms}
        self.hub_said = []               # 本机说过的话（垫句）——「记住」的落点：进上下文，不进 Agent
        self._cushion_tried = set()      # 已经看过垫句结论的那几版字（同一版只算一次）
        # 期三（2026-11-16）两人同时说四选：**这边正在说 / 正在准备说**（开闸之后正在准备说 / 正在回答）
        # 人又开口时，先问一次「这一句插话怎么处理」，再决定：让路（现网那条）/ 把这句说完再接 /
        # 继续说 / 垫句抢回。判断在 gate 里并发跑（`gate.start_floor`）；插话的落点全在这里。
        self._floor_seq = 0              # 插话编号（每一回插话一个 episode，同一段插话只问一次）
        self._floor_episode = None       # 这一回插话：{id,text,norm,t_ms,asked_ms,done,outlet,reason,...}
        self._floor_hold_version = None  # 四选还在飞时，当前这句**先不落地**（否则「让路」无从作废）
        self._floor_queue = []           # 说完再接 / 垫句抢回：等当前这句说完再作为下一轮发出的插话（FIFO）
        self._floor_pending_cushion = None   # 垫句抢回：当前这句说完、紧接的那一句垫句
        self._floor_continued = None     # 继续说：只进旁听的那一句（同一段插话续版只更新文本）
        self._floor_replaying = False    # 正在把插话按原路喂回去（让路 / 排队放行）：不再触发四选
        self._floor_in_step = False      # 四选落地中（避免 tick 重入时反复处理同一回）
        self.floor_asks_total = 0        # 跨轮累计问了几次四选
        self.floor_cushions = 0          # 跨轮累计「垫句抢回」垫了几句
        self.floor_released_total = 0    # 跨轮累计「说完再接 / 垫句抢回」真的放行了几段插话
        self.floor_log = []              # 每次四选结论：{t_ms, episode, outlet, text, ...}
        self.floor_context = []          # 继续说：只进旁听上下文的插话（不进 Agent）
        # 期四（2026-11-16）冷场三选：中枢的「陪着说」——安静到刻度才问一次，开口只接上文；
        # 记住（进本席上下文台账 `hub_said`），但不进 Agent、不选工具、不执行、不开闸。
        self._quiet_seq = 0              # 刻度这一回的编号（一个刻度只问一次，不会连着问）
        self._quiet_episode = None       # 这一回冷场判断：{id, quiet_ms, tick_ms, asked_ms, done, outlet}
        self._quiet_anchor_ms = None     # 中枢最后一句说完的时刻（冷场从这里起算；人开口清零）
        self._quiet_tick_next = 0        # 下一拍是第几个刻度（0 = 首问约 4 秒）
        self._quiet_last_open_ms = None  # 上一次冷场开口的时刻（两次开口之间退避）
        self._quiet_last_text = None     # 上一次冷场开口说的那句（同一句不重复说）
        self._quiet_topic = ""           # 刚才在聊的那件事（「只接上文」的锚）
        self._quiet_asks_total = 0       # 跨轮累计：问了几次冷场判断
        self._quiet_spoken_total = 0     # 跨轮累计：冷场真开口了几次
        self._quiet_log = []             # 每一拍的结论 / 每一次开口
        self._hub_last_said_ms = None    # 中枢最后一句说完的时刻（与冷场起算点同一口径）
        self._hub_last_text = ""         # 中枢最后一句的字（本机回答没有台词时为空）
        if gate is not None:
            gate.on_event = self._on_gate_event

    @staticmethod
    def _default_confirm_gate():
        """惰性加载 TurnConfirmGate，绕开 turn_session <-> confirm_gate 的循环 import。"""
        from confirm_orchestrator import TurnConfirmGate
        return TurnConfirmGate()

    # ---------- public API ----------

    def on_asr(self, t_ms: int, text: str, final: bool) -> None:
        # 跨轮前缀先剪：整段重发里已经消费过的部分不算新话（重复时只回一条 unchanged，不重开一轮）。
        text = self._trim_utterance_prefix(text)
        if not text:
            self.counters["eot_skipped"] += 1
            self._emit("asr_update", t_ms, text="", final=bool(final), segment=self.segment,
                       turn_text=self.turn_text(), changed=False)
            self._emit("eot_skipped", t_ms, reason="consumed_duplicate",
                       text=self.utterance_consumed_text)
            return
        if self._ack_is_echo(text, t_ms):
            # 自己刚说的那句被本机麦收回来：当回声丢掉——不停听、不改计时、不动这一版字。
            # 只有「出声后 1.2 秒内 + 这一版字基本就是那句应声」的极短情况才认，正常说话一个字不动。
            self.counters["ack_echo_drops"] += 1
            self._emit("ack_echo_dropped", t_ms, text=text, last_ack=self._ack_last_text,
                       note="与本机刚说过的话高度重合：当回声丢掉（不停听、不改收口）")
            return
        # 期四（2026-11-16）：人开口 → 冷场计时立刻清零（不是顺延）。安静还没到点 / 刚到点还没开口 /
        # 判断还在飞，这一句人话都让这一回作废——不在这里抢答。
        self._quiet_clear(t_ms, "human_speech")
        if self.turn_done:
            if normalize(text) == normalize(self.turn_text()):
                # A repeated/duplicate final update after the decision is not a turn reopen.
                self.counters["eot_skipped"] += 1
                self._emit("asr_update", t_ms, text=text, final=bool(final), segment=self.segment,
                           turn_text=self.turn_text(), changed=False)
                self._emit("eot_skipped", t_ms, reason="unchanged_after_turn_done", text=text)
                return
            self._emit("turn_reopen", t_ms, why="asr_after_action", text=text)
            self._reset_turn(t_ms)
        # Volcengine re-sends the whole sentence of an utterance: after one or more segments were
        # consumed, an exact re-send is a duplicate and a re-send that continues the consumed text
        # only contributes the tail. The judgement uses the *full* consumed text — with several
        # consumptions the last sealed segment alone no longer prefixes the re-send (spec 问题 1).
        if self.consumed_text:
            norm_text = normalize(text)
            if norm_text and norm_text.startswith(self.consumed_text):
                if norm_text == self.consumed_text:
                    self.counters["eot_skipped"] += 1
                    self._emit("asr_update", t_ms, text=text, final=bool(final), segment=self.segment,
                               turn_text=self.turn_text(), changed=False)
                    self._emit("eot_skipped", t_ms, reason="consumed_duplicate", text=text)
                    return
                text = self._tail_after_prefix(text, self.consumed_text)
        # 期三（2026-11-16）：这边正在说 / 正在准备说，人又开口 → 先问一次四选；插话这一段
        # **不进当前这句**（不改 segment / 版号 / 计时），等判断回来再按出口放（让路时按原路喂回去，
        # 与改动前逐字段一致；其余三条各走各的落点）。
        if self._floor_catch(t_ms, text, final):
            return
        if self.turn_start_ms is None:
            self.turn_start_ms = t_ms
        if self.segment_closed:
            self.segments.append(self.segment)
            self.segment = ""
            self.segment_closed = False
        before = normalize(self.suffix_text())
        self.segment = text
        changed = normalize(self.suffix_text()) != before
        if changed:
            self.version += 1
            self.version_t_ms = t_ms
        self._emit("asr_update", t_ms, text=text, final=bool(final), segment=self.segment,
                   turn_text=self.turn_text(), changed=changed)
        if not changed:
            # No new text: keep the current state and any in-flight judgement for the same version.
            self.counters["eot_skipped"] += 1
            self._emit("eot_skipped", t_ms, reason="unchanged", text=text)
            return
        if self.state not in ("LISTENING", "CONFIRM_WAIT"):
            self._to_listening(t_ms, "new_text")
        self.last_change_ms = t_ms
        self.t_last_word = t_ms
        if self.t_first_partial is None:
            self.t_first_partial = t_ms
            if self.cfg.warm_llm and self.warm_fn is not None and not self._warmed:
                self._warmed = True
                self._warm_future = self._pool.submit(self.warm_fn)
        self._cancel_inflight_llm(t_ms, "new_text")
        # EOT keeps the debounce (it must not fire per keystroke); tools fire immediately and the newest
        # text wins the single pending slot, so the shortlist starts one round earlier than before.
        # Both now look at the unconsumed suffix only.
        self._eot_pending = {"version": self.version, "text": self.eot_input(), "due_ms": t_ms + self.cfg.debounce_ms}
        if self.gate is None:
            self._tools_pending = {"version": self.version, "text": self.suffix_text()}
        else:
            # 中枢并发：同一版字上，判停（上面这条）与接不接（gate 立刻发起）同时在飞。
            # 工具披露是 Agent 的第一段：闸门开出之前不排、不发。
            self._tools_pending = None
            self.gate.on_text(self.version, self.suffix_text(), t_ms, final=bool(final),
                              speech_ms=(self._speech_run_ms(t_ms)
                                         if self.cfg.cushion_enabled else None),
                              turn=self.turn_index)
            self._gate_verdict = None       # 这一版刚提出判断，结论还没出来
        self.tick(t_ms)
        self.eot_scores = None
        self.tick(t_ms)

    @staticmethod
    def _tail_after_prefix(text: str, prefix: str) -> str:
        """Raw tail left after the consumed prefix, aligned on normalized characters.

        The re-send need not match byte-for-byte (Volcengine re-punctuates), so walk `text` counting
        only characters that survive `normalize()` until the consumed text's length is reached.
        """
        seen = 0
        for index, char in enumerate(text):
            if _PUNCT.fullmatch(char):
                continue
            seen += 1
            if seen == len(prefix):
                return text[index + 1:]
        return text

    def _trim_utterance_prefix(self, text: str) -> str:
        """这段语音里已经消费过的前缀不再算新话：返回只剩新字的文本（整句重复时返回空串）。

        - 重发的整段还是「已消费前缀 + 新尾巴」→ 只留尾巴（后一句开头不再叠前一句的字）；
        - 整段就是已消费前缀本身 → 空串（调用方按重复跳过，不重开一轮、不重判一次）；
        - 不以已消费前缀开头 → 这是用户新开口的一句，前缀记忆归零（不让旧前缀误剪新话）。
        """
        if not self.utterance_consumed_text:
            return text
        norm = normalize(text)
        if not norm:
            return text
        if not norm.startswith(self.utterance_consumed_text):
            self.utterance_consumed_text = ""
            self._turn_prefix = ""
            return text
        if norm == self.utterance_consumed_text:
            return ""
        return self._tail_after_prefix(text, self.utterance_consumed_text)

    def on_vad(self, t_ms: int, kind: str) -> None:
        self._emit("vad", t_ms, kind=kind)
        if kind == "speech_start":
            # 期四：VAD 先开口也算「人开口」——冷场计时立刻清零（安静的起算点回到中枢下一句）。
            self._quiet_clear(t_ms, "speech_start")
            if self.turn_done:
                self._reset_turn(t_ms)
            elif self.state not in ("LISTENING", "CONFIRM_WAIT"):
                # 期三：这边正在说 / 正在准备说 → **不先作废**；先问一次四选（让路时再按原路作废）。
                if not self._floor_vad_start(t_ms):
                    self._to_listening(t_ms, "speech_start")
            if self.segment:
                self.segment_closed = True
            # The user is speaking again: the previous silence interval is over.
            self.speech_start_ms = t_ms
            if not self._floor_holding():
                self._cancel_inflight_llm(t_ms, "speech_start")
        elif kind == "speech_end":
            self.last_speech_end_ms = t_ms
        self.tick(t_ms)

    # ---------- 文字输入：发送即收住（键盘/输入框，不是麦） ----------

    def on_text_send(self, t_ms: int, text: str) -> None:
        """某席输入框里的一整句原话发出来：**发送即收住**，之后走同一条真链。

        与开口相同的部分：这句原话按 final 出字（对话流里有这一条，判断标签打在它上面）；
        同一版字上并发发起「要不要回答」（判断规则与临时开闸线都不动）；收住那一刻照旧读一次心、
        写一刀记忆，再按闸门结论进 Agent / 只进上下文。

        唯一不同：**不跑判停模型**。点发送本身就是「这句说完了」，所以不再等 debounce / 判停三态 /
        静音窗——直接把这一版字按「说完」交给闸门。交给闸门的那份 `hub_eot` 带 `settled_by=text_send`，
        界面据此写「文字发送即收住」，不冒充判停模型的结论。

        同一席上一句还在判（等接不接 / 排工具 / 决策没落地）时又发一句：这一句**排队**，等上一句
        收口后自动发出。不能当场并进同一轮——那样上一句的决策会按新字落地（消费掉的是下一句）、
        或在途决策被判成 stale 丢掉。排队只影响先后，不丢字、不并句。
        """
        text = (text or "").strip()
        if not text:
            return
        if not self.turn_done and self.state != "LISTENING":
            self._text_hold.append((t_ms, text))
            self._emit("text_held", t_ms, text=text, state=self.state,
                       note="上一句还在走（等接不接 / 排工具 / 决策没落地）：这句排队，上一句收口后自动发出")
            return
        self._text_send_now(t_ms, text)

    def _text_send_now(self, t_ms: int, text: str) -> None:
        # 打字一句一句发：不背麦那条「整段重发」的前缀记忆（否则重复同一句会被当成重发剪成空）。
        self.utterance_consumed_text = ""
        self._turn_prefix = ""
        if self.turn_done:
            # 上一轮已收口：这一句是新一轮（同一句重复也是新的一句，不像麦那样当重发）。
            self._emit("turn_reopen", t_ms, why="text_send", text=text)
            self._reset_turn(t_ms)
        self.on_asr(t_ms, text, True)
        self._eot_pending = None          # 发送即收住：不排判停，这一次也不调判停模型
        self._emit("text_send", t_ms, text=text, version=self.version,
                   note="文字输入：发送即收住（不跑判停模型）")
        if self.gate is not None:
            # 同一版字上的「说完」这一路：文字输入已经收住，按说完回填给闸门；
            # 接不接仍在 on_asr 里并发发起、照旧等它自己回来（不等就不开闸）。
            self.gate.on_eot(self.version, t_ms, {
                "label": "complete", "may_confirm": True, "may_start_early": True,
                "settled_by": "text_send", "latency_ms": 0.0,
            })
        self._confirm(t_ms, "text_send")
        self.tick(t_ms)

    def tick(self, now_ms: int) -> None:
        if self.turn_start_ms is None:
            return
        # 期三：四选回来就按出口办（放在 `_poll` 之前：让路要能取消「还在飞」的那一句）；
        # 接着放行「说完再接 / 垫句抢回」——当前这句一说完就换下一轮，插话不在这里丢。
        if self.gate is not None and self.cfg.floor_enabled:
            self._floor_step(now_ms)
        self._floor_release_step(now_ms)
        # 排队中的文字输入：上一句收口（turn_done）或回到 LISTENING 后，一次只放一句（一句一轮）。
        if self._text_hold and (self.turn_done or self.state == "LISTENING"):
            held_text = self._text_hold.pop(0)[1]
            self._text_send_now(int(now_ms), held_text)
        self._poll(now_ms)
        if self.gate is not None:
            self.gate.poll(now_ms)
            self._gate_resolve_wait(now_ms)
            # 垫句 / 应一声都只在「还没说完」的三个等待档里才可能出声；**垫句优先**，同一时刻
            # 最多一句。两者都不碰下面这些计时（不延长也不缩短任何一档）。
            cushion_spoke = self._maybe_cushion(now_ms)
            self._maybe_ack(now_ms, cushion_spoke=cushion_spoke)
        silence = self._silence_ms(now_ms)
        if self.state == "THINK_WAIT":
            # A thinking pause is allowed to outlast max_silence_ms; only think_wait_ms confirms it.
            if silence >= self.cfg.think_wait_ms:
                self._confirm(now_ms, "think_wait")
        elif self.state == "MAYBE_DONE":
            if not normalize(self.suffix_text()):
                # Nothing left to confirm: close the turn like the LISTENING empty-suffix branch
                # instead of waiting for a _confirm() that can never act (spec 问题 2.2). This is the
                # safety net for paths this fix does not change (e.g. a timed-out EOT still reporting).
                if silence >= self.cfg.confirm_silence_ms:
                    self._maybe_finish_session(now_ms)
            elif silence >= self.cfg.confirm_silence_ms:
                self._confirm(now_ms, "confirm_silence")
            elif silence >= self.cfg.max_silence_ms:
                self._confirm(now_ms, "max_silence")
        elif self.state == "CUT_OFF_WAIT":
            if silence >= self.cfg.cut_wait_ms:
                self._confirm(now_ms, "cut_wait")
            elif silence >= self.cfg.max_silence_ms:
                self._confirm(now_ms, "max_silence")
        elif self.state == "CONFIRM_WAIT":
            # 确认等待：只会在第二轮回复判停收敛后被 _resolve_confirm 移出。
            # 若超时（默认沿用 think_wait_ms 作为确认等待窗）都没有二轮回，安全优先放弃，
            # 绝不静默执行（spec §5.3 超时默认不执行）。
            if self.confirm_pending is not None and silence >= self.cfg.think_wait_ms:
                self._cancel_inflight_llm(now_ms, "confirm_timeout")
                self._emit("confirm_resolve", now_ms, user_reply="", intent="timeout",
                           action="abandon", message="确认超时，安全优先放弃，不执行",
                           pending_tool=self.confirm_pending.get("tool"))
                self.confirm_pending = None
                self._to_state("LISTENING", now_ms, "confirm_timeout")
        elif self.state == "LISTENING" and silence >= self.cfg.max_silence_ms:
            if normalize(self.suffix_text()):
                self._confirm(now_ms, "max_silence")
            else:
                # Nothing left to consume and the user has been quiet: close the whole turn (once).
                self._maybe_finish_session(now_ms)
        for job in self._eot_jobs:
            if not job.get("timed_out") and now_ms - job["started_ms"] > self.cfg.eot_timeout_ms:
                # Design doc: a KEV failure/timeout falls back to the max_silence rule below; the late
                # result is still scored and reported, it just no longer gates the state machine.
                job["timed_out"] = True
                self.counters["eot_skipped"] += 1
                self._emit("eot_skipped", now_ms, reason="timeout", text=job["text"])
        while len(self._eot_jobs) < max(1, self.cfg.eot_max_inflight) and self._eot_pending \
                and now_ms >= self._eot_pending["due_ms"]:
            pending, self._eot_pending = self._eot_pending, None
            self._fire_eot(now_ms, pending)
        if self.gate is not None and self._gate_open_version != self.version:
            # 开闸前不进 Agent：闸门没开（还没说完 / 只进上下文）就一行工具披露都不发。
            self._tools_pending = None
        while len(self._tools_jobs) < max(1, self.cfg.tools_max_inflight) and self._tools_pending:
            self._fire_tools(now_ms)
        if self._warm_future is not None and self._warm_future.done() and self.warm_ms is None:
            try:
                self.warm_ms = (self._warm_future.result() or {}).get("warm_ms")
            except Exception:
                self.warm_ms = None
            self._warm_future = None
        if self.state == "CONFIRMED":
            self._maybe_start_llm(now_ms)
        # 期四（2026-11-16）：冷场——安静到刻度、且真的没人说话才问一次（只出声、不进 Agent、
        # 不碰任何计时；判断没回来 / 失败 / 超时 → 不出声）。
        self._quiet_step(now_ms)

    def wait_until(self, now_ms: int) -> None:
        self.tick(now_ms)

    def idle(self) -> bool:
        return (not any(self._jobs.values()) and not self._eot_jobs and not self._tools_jobs
                and not self._eot_pending and not self._tools_pending)

    def summary(self) -> dict:
        return {"session_id": self.session_id, "seat_id": self.seat_id, "mic_id": self.mic_id,
                "turns": self.turn_index,
                "t_last_word": self.t_last_word, "t_confirmed": self.t_confirmed,
                "t_llm_start": self.t_llm_start, "t_decision": self.t_decision,
                # t_* keep their "first time in this turn" meaning; t_last_* are the latest occurrence
                # (one turn can now hold several decisions).
                "t_last_llm_start": self.t_llm_start_last, "t_last_decision": self.t_decision_last,
                "t_confirm_latency_ms": self._delta(self.t_confirmed, self.t_last_word),
                "t_llm_start_latency_ms": self._delta(self.t_llm_start, self.t_last_word),
                "t_decision_latency_ms": self._delta(self.t_decision, self.t_last_word),
                # M0 instrumentation: where the "last word → LLM request" time actually went.
                "t_first_partial": self.t_first_partial,
                "t_tools_ready": self.t_tools_ready,
                "t_first_partial_to_last_word_ms": self._delta(self.t_last_word, self.t_first_partial),
                "shortlist_source": self._shortlist_source,
                "shortlist_age_ms": self._shortlist_age_ms,
                "warm_ms": self.warm_ms,
                "consumed_count": self.consumed_count, "consumptions": self.consumptions,
                "context_consumed": self.context_consumed,
                "t_last_consumed": self.t_last_consumed,
                # 期一：应声（台面上要能数出次数与间隔；这句话不进上下文、不开闸）
                "ack_enabled": bool(self.cfg.ack_enabled),
                "ack_gap_ms": int(self.cfg.ack_gap_ms),
                "ack_spoken_total": self.ack_spoken_total,
                "ack_last_ms": self._ack_last_ms,
                "ack_last_text": self._ack_last_text,
                "ack_log": list(self.ack_log),
                # 期二：节奏 + 垫句（节奏只用于问不问垫句；垫句进上下文、不进 Agent、不开闸）
                "cushion_enabled": bool(self.cfg.cushion_enabled),
                "cushion_gap_ms": int(self.cfg.cushion_gap_ms),
                "cushion_min_speech_ms": CUSHION_MIN_SPEECH_MS,
                "cushion_spoken_total": self.cushion_spoken_total,
                "cushion_log": list(self.cushion_log),
                "pace": (self.pace_last or {}).get("pace"),
                "pace_label": (self.pace_last or {}).get("label"),
                "pace_held": (self.pace_last or {}).get("held"),
                "hub_said": list(self.hub_said),
                # 期三：两人同时说四选（让路＝现网那条；另外三条只改插话的落点，不改收口 / 开闸）
                "floor_enabled": bool(self.cfg.floor_enabled),
                "floor_asks_total": self.floor_asks_total,
                "floor_cushions": self.floor_cushions,
                "floor_released_total": self.floor_released_total,
                "floor_active": bool(self._floor_episode is not None
                                     and not self._floor_episode["done"]),
                "floor_episode": ({key: value for key, value in self._floor_episode.items()
                                   if key != "hub_sentence"} if self._floor_episode else None),
                "floor_queue": [dict(item) for item in self._floor_queue],
                "floor_pending_cushion": (dict(self._floor_pending_cushion)
                                          if self._floor_pending_cushion else None),
                "floor_context": list(self.floor_context),
                "floor_log": list(self.floor_log),
                # 期四：冷场三选（从「中枢最后一句说完」起算；只接上文、不出声就不进任何地方）
                "quiet_enabled": bool(self.cfg.quiet_enabled),
                "quiet_first_ms": int(QUIET_FIRST_MS),
                "quiet_check_after_ms": int(QUIET_CHECK_AFTER_MS),
                "quiet_backoff_ms": int(QUIET_BACKOFF_MS),
                "quiet_asks_total": self._quiet_asks_total,
                "quiet_spoken_total": self._quiet_spoken_total,
                "quiet_anchor_ms": self._quiet_anchor_ms,
                "hub_last_said_ms": self._hub_last_said_ms,
                "hub_last_text": self._hub_last_text,
                "quiet_last_open_ms": self._quiet_last_open_ms,
                "quiet_last_text": self._quiet_last_text,
                "quiet_topic": self._quiet_topic,
                "quiet_active": bool(self._quiet_episode is not None
                                     and not self._quiet_episode["done"]),
                "quiet_episode": dict(self._quiet_episode) if self._quiet_episode else None,
                "quiet_log": list(self._quiet_log),
                # 期五：四处出声的同一套口径（一页表 + 这一席已出声几次 + 上一声）；只读快照，
                # 不新增判断、不改任何数字（四处各自的问法与出口仍在期一~期四的模块里）。
                "hub_policy": hub_arbiter.policy_snapshot(
                    ack_spoken=self.ack_spoken_total,
                    cushion_spoken=self.cushion_spoken_total,
                    quiet_spoken=self._quiet_spoken_total,
                    floor_asks=self.floor_asks_total,
                    last_spoken_ms=self._ack_last_ms,
                    last_text=self._ack_last_text or ""),
                **self.counters,
                **({"cerebrum": self.gate.snapshot()} if self.gate is not None else {})}

    # ---------- text ----------

    def turn_text(self) -> str:
        return self._join(self.segments + ([self.segment] if self.segment else []))

    def suffix_segments(self):
        """Sealed-but-not-yet-consumed segments (the consumed ones can no longer change)."""
        return self.segments[self.consumed_segments:]

    def suffix_text(self) -> str:
        """The unconsumed suffix: everything the next decision is allowed to see."""
        return self._join(self.suffix_segments() + ([self.segment] if self.segment else []))

    def eot_input(self) -> str:
        if self.cfg.eot_input == "turn_text":
            return self.suffix_text()
        if len(normalize(self.segment)) >= 4 or not self.suffix_segments():
            return self.segment
        return self._join(self.suffix_segments()[-1:] + [self.segment])

    @staticmethod
    def _join(parts):
        text = ""
        for part in parts:
            if not part:
                continue
            if text and text[-1].isascii() and part[0].isascii():
                text += " "
            text += part
        return text

    # ---------- workers ----------

    def _fire_eot(self, now_ms, pending):
        self.counters["eot_calls"] += 1
        self._emit("eot_start", now_ms, text=pending["text"], version=pending["version"])
        self._eot_jobs.append({"future": self._pool.submit(self.eot_fn, pending["text"]),
                               "version": pending["version"], "started_ms": now_ms, "text": pending["text"]})

    def _fire_tools(self, now_ms):
        pending, self._tools_pending = self._tools_pending, None
        self.counters["tool_calls"] += 1
        self._emit("tools_start", now_ms, text=pending["text"], version=pending["version"])
        self._tools_jobs.append(
            {"future": self._pool.submit(self.select_tools_fn, pending["text"], self.cfg.tool_set, self.context),
             "version": pending["version"], "started_ms": now_ms, "text": pending["text"]})

    def _start_llm(self, now_ms, speculative):
        self.counters["llm_calls"] += 1
        # 第一次发出才是"启动"时刻；短名单变化后的重发不覆盖它（否则指标被推后，看起来更慢）
        if self.t_llm_start is None:
            self.t_llm_start = now_ms
        self.t_llm_start_last = now_ms
        result = self._tools_result or {}
        cache = self._tools_cache or {}
        self._shortlist_source = "fresh" if self._tools_ready() else "stale"
        self._shortlist_age_ms = 0 if self._tools_ready() else now_ms - cache.get("ready_ms", now_ms)
        # The decision only ever sees the unconsumed suffix (no cross-decision context in this PoC).
        utterance = self.suffix_text()
        # 发起请求这一刻才定上下文：场景取当时最新一份、记忆按这句原话查一次（不冻在开闸那一刻）。
        context = self._request_context(now_ms, utterance)
        self._emit("llm_start", now_ms, speculative=bool(speculative), selected=result.get("selected", []),
                   shortlist_source=self._shortlist_source, shortlist_age_ms=self._shortlist_age_ms,
                   utterance=utterance, consumed_segments=self.consumed_segments,
                   memory_count=len(context.get("memory") or []))
        self._jobs["llm"] = {"shortlist_norm": cache.get("norm"),
                             "shortlist_selected": tuple(result.get("selected") or []),
                             "future": self._pool.submit(
            self.decide_fn, utterance, result.get("selected", []), result.get("schemas", []),
            self.cfg.tool_set, context, native_schemas=result.get("native_schemas"),
            available_domains=result.get("domains")),
            "version": self.version, "utterance": utterance,
            "started_ms": now_ms, "speculative": bool(speculative), "canceled": False}

    def _poll(self, now_ms):
        for job in [job for job in self._eot_jobs if job["future"].done()]:
            self._eot_jobs.remove(job)
            try:
                result = job["future"].result()
            except Exception as error:  # PoC: surface, do not retry
                self._emit("error", now_ms, stage="eot", error_type=type(error).__name__, error=str(error))
                continue
            self._on_eot_result(now_ms, job, result)
        for job in [job for job in self._tools_jobs if job["future"].done()]:
            self._tools_jobs.remove(job)
            try:
                result = job["future"].result()
            except Exception as error:  # PoC: surface, do not retry
                self._emit("error", now_ms, stage="tools", error_type=type(error).__name__, error=str(error))
                continue
            self._on_tools_result(now_ms, job, result)
        job = self._jobs["llm"]
        if job is not None and job["future"].done():
            self._jobs["llm"] = None
            try:
                result = job["future"].result()
            except Exception as error:  # PoC: surface, do not retry
                self._emit("error", now_ms, stage="llm", error_type=type(error).__name__, error=str(error))
            else:
                self._on_llm_result(now_ms, job, result)

    def _eot_verdict(self, scores: dict) -> tuple:
        """`eot_policy` 下的 (生效标签, 能否确认, 能否抢跑)。

        三套策略只在这里判一次：状态机（`_on_eot_result`）与二次确认（CONFIRM_WAIT 分支）
        必须用同一条规则，否则会出现「状态机认为说完了、确认闸门认为没说完」的双口径。
        「能确认但不能抢跑」就是文档里「判 complete 但不抢跑」那条分支。
        """
        label = scores["label"]
        if self.cfg.eot_policy == "argmax":
            # 改动前的 argmax 口径：label 决定「说完」，抢跑仍看 margin。
            return (label, label == "complete",
                    label == "complete" and (scores.get("margin") or 0.0) >= self.cfg.spec_margin)
        if self.cfg.eot_policy == "margin":
            if label != "complete":
                return label, False, False
            margin = scores.get("margin") or 0.0
            if margin < self.cfg.eot_margin:
                return ("cut" if (scores.get("cut") or 0.0) >= (scores.get("wait") or 0.0)
                        else "wait"), False, False
            return "complete", True, margin >= self.cfg.spec_margin
        # score：文档的两阈值规则（EOT_COMPLETE_MIN_SCORE / EOT_CONFIDENCE_MIN）。
        if (scores.get("complete") or 0.0) >= self.cfg.eot_complete_min:
            return "complete", True, (scores.get("confidence") or 0.0) >= self.cfg.eot_confidence_min
        return ("cut" if (scores.get("cut") or 0.0) >= (scores.get("wait") or 0.0) else "wait",
                False, False)

    def _on_eot_result(self, now_ms, job, result):
        stale = job["version"] != self.version
        if stale:
            # The judgement was paid for but no longer describes the newest text: report it (metrics
            # need every prefix score) and drop it from the state machine.
            self.counters["eot_skipped"] += 1
            self.counters["eot_stale"] += 1
        self.eot_scores = result if not stale else self.eot_scores
        label, may_confirm, may_start_early = self._eot_verdict(result)
        self._emit("eot_scores", now_ms, stale=stale, text=job["text"],
                   applied_label=label, may_confirm=may_confirm, may_start_early=may_start_early,
                   **result)
        if stale:
            self._emit("eot_skipped", now_ms, reason="stale", text=job["text"])
            return
        if self.gate is not None:
            self.gate.on_eot(job["version"], now_ms, {
                "label": label, "may_confirm": may_confirm, "may_start_early": may_start_early,
                "complete": result.get("complete"), "cut": result.get("cut"),
                "wait": result.get("wait"), "confidence": result.get("confidence"),
                "latency_ms": result.get("wall_ms", result.get("latency_ms")),
            })
            if not may_confirm:
                # 判停这一路当场就说了「还没到能开闸的那一刻」：让闸门把这一条记成事件（界面可见），
                # 而不是拖到静音窗收口才第一次有声音。
                self._gate_verdict = self.gate.gate(job["version"], now_ms)
        if not normalize(self.suffix_text()):
            # Everything this judgement could speak about is already consumed: keep the score and
            # emit, but never drive the state machine. A late `complete` used to park the turn in
            # MAYBE_DONE with an empty suffix, where _confirm() can never act (spec 问题 2.1).
            self.counters["eot_skipped"] += 1
            self._emit("eot_skipped", now_ms, reason="empty_suffix", text=job["text"],
                       label=result["label"])
            return
        if self.state == "CONFIRMED":
            return
        # ---- 对话式二次确认：第二轮回（CONFIRM_WAIT 内）判停收敛后进意图判定 ----
        if self.state == "CONFIRM_WAIT":
            # 只有「能确认」的判停（同一套 `_eot_verdict`）才把当前未消费后缀当完整第二轮回；
            # 没说完就继续听（spec §3.2：先判说完，再说确认意图）。
            if may_confirm:
                self._resolve_confirm(now_ms, self.suffix_text())
            else:
                self._to_state("CUT_OFF_WAIT" if label == "cut" else "THINK_WAIT",
                               now_ms, "confirm_wait_eot_" + label)
            return
        if label == "complete" and may_confirm and may_start_early:
            # 三态挂窗的「说完」这一态（[[无确认窗流程]] §1.1，2026-11-04 拍板）：强 complete
            # 当刻认——不再停一档 MAYBE_DONE 等 confirm_silence_ms，选工具（多已在途）→ 生成
            # → 落地立刻往下走。`why=eot_complete` 让观测面把这条路径与弱 complete 的
            # `confirm_silence` 分开，别再拿 confirm_silence 当「说完→动手」的判据。
            self._confirm(now_ms, "eot_complete")
            return
        if label == "complete":
            # 「可能说完」这一态：判了 complete 但 confidence 没过抢跑线（弱 complete），只有这一档
            # 等 confirm_silence_ms 的收敛窗（约 0.7s）；期间新文本 / speech_start 就回 LISTENING
            # 继续听，整段时间仍由 max_silence_ms(2s) 封顶。弱 complete 不抢跑，决策串行跑在确认之后。
            self._to_state("MAYBE_DONE", now_ms, "eot_complete")
            return
        self._to_state("CUT_OFF_WAIT" if label == "cut" else "THINK_WAIT", now_ms, "eot_" + label)

    def _may_start_early_now(self) -> bool:
        """工具短名单到位时的抢跑许可：`score` 口径必须由判停的 confidence 门槛点头。

        改动前 `argmax` / `margin` 两套策略里，工具短名单回来本身就是一次独立的抢跑触发器
        （`spec_margin` 只管判停触发的那次），这里保留原样做 A/B；`score` 口径下文档把抢跑
        交给 `confidence >= EOT_CONFIDENCE_MIN`，工具链路不能绕过它。
        """
        scores = self.eot_scores
        if self.cfg.eot_policy != "score" or not isinstance(scores, dict):
            return True
        return self._eot_verdict(scores)[2]

    def _on_tools_result(self, now_ms, job, result):
        newest = self._tools_cache is None or job["version"] >= self._tools_cache["version"]
        if newest:
            self._tools_cache = {"norm": normalize(job["text"]), "text": job["text"],
                                 "version": job["version"], "ready_ms": now_ms}
            self._tools_result = result
            self.t_tools_ready = now_ms
        scores = result.get("scores") or {}
        top = [{"name": name.replace("tool:", ""), "score": value} for name, value in
               sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:3]]
        self._emit("tools_scores", now_ms, reused=False, text=job["text"], version=job["version"],
                   newest=newest, selected=result.get("selected", []), timing=result.get("timing"), top=top)
        if self.cfg.reuse_tools_if_text_unchanged and not self._tools_ready():
            self._tools_pending = {"version": self.version, "text": self.suffix_text()}
        # M4: an in-flight decision that used an older shortlist is reissued once the new one lands.
        llm_job = self._jobs["llm"]
        # 只有"入选的 top-k 真的变了"才重发：文本 lengthen 了但短名单没变时重发纯属浪费。
        changed = set(result.get("selected") or []) != set(llm_job.get("shortlist_selected") or []) if llm_job else False
        if (newest and self.cfg.shortlist_reissue and changed and llm_job is not None and not llm_job["canceled"]
                and llm_job["version"] == self.version and self.state in ("MAYBE_DONE", "CONFIRMED")):
            self._cancel_inflight_llm(now_ms, "shortlist_changed")
            self.counters["n_reissue"] += 1
        self._maybe_start_llm(now_ms, speculative=self.cfg.speculative_llm
                              and self.state == "MAYBE_DONE" and self._may_start_early_now())

    def _on_llm_result(self, now_ms, job, result):
        if job["canceled"]:
            return
        if job["version"] != self.version:
            self.counters["llm_wasted"] += 1
            self._emit("llm_cancel", now_ms, reason="stale_text")
            return
        if self.state != "CONFIRMED":
            # Speculative result arrived early; keep it until the turn is confirmed.
            self._llm_prepared = {"version": job["version"], "result": result,
                                  "speculative": bool(job.get("speculative")), "utterance": job.get("utterance")}
            return
        if self._floor_hold_version is not None and job["version"] == self._floor_hold_version:
            # 期三：这一回四选还在飞——这句先不落地（先落地就没有「作废本轮」可作了）。
            # 判断一回来就放行（继续说 / 说完再接 / 垫句抢回）或原路作废（让路）。
            self._llm_prepared = {"version": job["version"], "result": result,
                                  "speculative": bool(job.get("speculative")),
                                  "utterance": job.get("utterance")}
            self._emit("floor_hold", now_ms, layer="hub",
                       episode=(self._floor_episode or {}).get("id"), version=job["version"],
                       utterance=job.get("utterance"),
                       note="四选还在飞：当前这句先不落地，等判断回来再定（不改收口时刻）")
            return
        self._apply_llm(now_ms, result, job)

    def _apply_llm(self, now_ms, result, job):
        """A decision landed: consume the suffix it was made on and keep listening for more."""
        self.decision = result["decision"]
        self.t_decision = now_ms if self.t_decision is None else self.t_decision
        self.t_decision_last = now_ms
        # job["version"] == self.version was checked before this call, so the text below is byte-for-byte
        # what decide() received: sealing it cannot truncate or shift the segment.
        consumed_text = self.suffix_text()
        # 期四：中枢把这一句答完 = 「中枢最后一句说完」，冷场从这里起算；这句原话是「刚才在聊的事」。
        self._quiet_topic = consumed_text or self._quiet_topic
        self._quiet_note_hub_said(now_ms, "", kind="answer")
        self._seal_consumed()
        self.consumed_version = job["version"]
        record = {"index": len(self.consumptions) + 1, "version": job["version"],
                  "segments": self.consumed_segments, "text": consumed_text,
                  "t_ms": now_ms, "asr_t_ms": self.version_t_ms,
                  "speculative": bool(job.get("speculative")), "llm_ms": result.get("llm_ms"),
                  "decision_kind": result["decision"].get("kind"),
                  "shortlist_source": self._shortlist_source}
        self.consumptions.append(record)
        self.consumed_count = len(self.consumptions)
        self.counters["consumed"] = self.consumed_count
        self.t_last_consumed = now_ms
        self._emit("llm_decision", now_ms, decision=result["decision"], llm_ms=result.get("llm_ms"),
                   utterance=consumed_text, consumed_index=record["index"],
                   consumed_version=job["version"], consumed_segments=self.consumed_segments,
                   decision_head=(result.get("log_fields") or {}).get("decision_head"))
        # `record["t_ms"]` is the same instant _emit stamps, so it is not passed twice.
        self._emit("segment_consumed", now_ms, **{key: value for key, value in record.items() if key != "t_ms"})
        # 对话式二次确认：第一轮决策命中池内高危工具 -> 挂起该决策进 CONFIRM_WAIT，等用户二轮回。
        # 不立即消费为普通执行；EOT 判停照常跑（用于判断二轮回"说完没"），但不再 advance decide()。
        if self.confirm_gate.ready(result["decision"]):
            self._begin_confirm(now_ms, result["decision"])
            if self._stop_requested:
                self._maybe_finish_session(now_ms, force=True)
            return
        self._to_state("LISTENING", now_ms, "consumed")
        if self.cfg.close_turn_on_decision:
            # 用户 2026-11-03 拍板：决策落地即收口，不再等 max_silence_ms 的 2s 尾巴。
            # 后缀已空、在途决策已应用，`_maybe_finish_session` 只会成功；用户紧接着补一句
            # 会走 on_asr 的 turn_reopen 分支 → _reset_turn，变成新一轮（前端按新一轮渲染）。
            self._maybe_finish_session(now_ms)
        if self._stop_requested:
            # Explicit stop(): consume what was left, then close the turn.
            self._maybe_finish_session(now_ms, force=True)

    def _begin_confirm(self, now_ms, decision):
        """第一轮决策命中待确认池：挂起决策、发出确认请求、进入 CONFIRM_WAIT。

        EOT 判停/选工具照常跑（为二轮回做准备、判断是否说完），但 decide() 被 gating：
        因为 `_maybe_start_llm` 只在 CONFIRMED/MAYBE_DONE 下 advance，CONFIRM_WAIT 不在其中，
        所以二轮回期间 agent 不会自作主张重复决策。
        """
        self.confirm_pending = dict(decision or {})
        self.confirm_resolved = None
        ask = self.confirm_gate.ask_for(decision)
        self._emit("confirm_request", now_ms, tool=decision.get("tool"),
                   arguments=decision.get("arguments"), ask=ask, **self.summary())
        self._to_state("CONFIRM_WAIT", now_ms, "pooled_tool")
        # 清掉上一轮的 LLM/工具中间态，避免挂起后误用
        self._cancel_inflight_llm(now_ms, "confirm_wait")

    def _resolve_confirm(self, now_ms, user_reply):
        """第二轮用户回复（已由 EOT 判停判为"说完"）收敛 -> 判意图 -> 落四分支。

        结束后 segment 已在 _seal_consumed 消费，回 LISTENING；takeover 分支把 user_reply
        当新诉求再走 select_tools/decide 一轮（若再命中池内工具会再进 CONFIRM_WAIT）。
        """
        if self.confirm_pending is None:
            self._to_state("LISTENING", now_ms, "no_pending")
            return
        pending = self.confirm_pending
        resolved = self.confirm_gate.resolve(user_reply, pending)
        self.confirm_resolved = resolved
        # 二轮回这句话同样是「收住的一句」：先写一刀记忆，再封口（封口顺序不变）。
        self._memory_write_close(now_ms, "confirm_reply")
        # 第二轮回已被意图判定"消费"：封口该 segment，避免随后被 Listener 当新指令再决策。
        self._consume_suffix(now_ms, "confirm_reply")
        self._emit("confirm_scores", now_ms, **resolved.get("scores", {}), user_reply=user_reply,
                   confirm_ask=resolved.get("confirm_ask"), pending_tool=resolved.get("pending_tool"))
        intent = resolved["intent"]
        self._emit("confirm_resolve", now_ms, user_reply=user_reply, intent=intent,
                   action=resolved["action"], message=resolved["message"],
                   pending_tool=resolved.get("pending_tool"), scores=resolved.get("scores"),
                   confirm_ask=resolved.get("confirm_ask"))
        # 先落地"放弃当前待确认动作"的底（except agree 会执行它）
        self.confirm_pending = None
        if intent == "takeover":
            # 转新诉求：用用户回复重跑选工具+决策（注入链），不耗新 segment
            self._emit("confirm_reroute", now_ms, request=user_reply,
                       message=f"把第 {self.turn_index} 轮的新诉求交给 decide 处理")
            self._run_takeover(now_ms, user_reply)
            return
        if intent == "agree":
            # 已确认：记录 `executed`(mock)。不真实触发业务工具；可在 UI 端展示 mock 执行。
            self._emit("confirm_exec", now_ms, tool=resolved.get("pending_tool"),
                       mock_only=True, message="用户已确认，mock 执行 " + str(resolved.get("pending_tool")))
        self._to_state("LISTENING", now_ms, "confirm_" + intent)

    def _run_takeover(self, now_ms, request):
        """takeover：把第二轮回文本当新诉求，走注入的 select_tools + decide。

        供 mock/真实注入链复用的同一段异步落链：捞出短名单后交给 decide_fn，再把结果
        以 `reroute_decision` 事件发出；若新决策也命中池内工具会再次进入 CONFIRM_WAIT。
        """
        context = self._decide_context()
        try:
            selection = self.select_tools_fn(request, self.cfg.tool_set, context)
            self._emit("reroute_tools", now_ms, request=request,
                       selected=selection.get("selected", []))
            outcome = self.decide_fn(request, selection.get("selected", []),
                                     selection.get("schemas", []), self.cfg.tool_set, context,
                                     native_schemas=selection.get("native_schemas"),
                                     available_domains=selection.get("domains"))
            decision = outcome["decision"]
        except Exception as error:  # PoC: 接管失败不执行旧动作即可, 报出但保持安全
            self._emit("error", now_ms, stage="takeover", error_type=type(error).__name__,
                       error=str(error))
            self._to_state("LISTENING", now_ms, "takeover_error")
            return
        self._emit("reroute_decision", now_ms, request=request, decision=decision,
                   message="接管：转去处理新诉求 " + str(request))
        if self.confirm_gate.ready(decision):
            # 新诉求又命中池内高危工具 -> 再进 CONFIRM_WAIT（可再确认一次）
            self._begin_confirm(now_ms, decision)
        else:
            self._emit("confirm_exec", now_ms, tool=decision.get("tool"), request=request,
                       mock_only=True, message="接管后直接执行(池外) " + str(decision.get("tool")))
            self._to_state("LISTENING", now_ms, "reroute_done")

    def _seal_consumed(self):
        """Seal the in-use segment into `segments` so later partials can no longer rewrite it."""
        if self.segment:
            self.segments.append(self.segment)
            self.segment = ""
        self.segment_closed = False
        self.consumed_segments = len(self.segments)
        self.consumed_text = normalize(self._join(self.segments[:self.consumed_segments]))
        # 这段语音累计消费到哪（跨轮留存）：本轮开头那截前缀 + 本轮已消费的这一截。
        self.utterance_consumed_text = normalize(self._turn_prefix + self.consumed_text)
        # Kept for compatibility (metrics, older tests); dedup is judged on `consumed_text`.
        self.consumed_tail = self.segments[-1] if self.segments else None

    def _consume_suffix(self, now_ms, why):
        """把当前未消费后缀当作"已处理完的消费"（确认轮第二轮回用）：封口 + 记账 + 发事件。

        复用 _seal_consumed 的封口逻辑，避免该 segment 被当作新一轮待决策文本。
        """
        consumed_text = self.suffix_text()
        self._seal_consumed()
        self.consumed_version = self.version
        self.t_last_consumed = now_ms
        self.consumptable_count = self.consumed_count = self.consumed_count + 1
        self.counters["consumed"] = self.consumed_count
        self._emit("confirm_consumed", now_ms, index=self.consumed_count, text=consumed_text,
                   why=why, consumed_segments=self.consumed_segments)

    # ---------- state ----------

    def _silence_ms(self, now_ms):
        anchors = [value for value in (self.last_change_ms, self.last_speech_end_ms,
                                       self.speech_start_ms, self.turn_start_ms,
                                       self.t_last_consumed) if value is not None]
        return now_ms - max(anchors) if anchors else 0

    def _tools_ready(self):
        return self._tools_cache is not None and self._tools_cache["norm"] == normalize(self.suffix_text())

    def _maybe_start_llm(self, now_ms, speculative=False):
        if self.turn_done or self.state not in ("CONFIRMED", "MAYBE_DONE"):
            return
        if self._floor_hold_version is not None and self._floor_hold_version == self.version:
            # 期三：四选还在飞——当前这句先不落地（判断一回来就放行 / 或原路作废）。
            return
        if self.gate is not None and self._gate_open_version != self.version:
            # 开闸前不进 Agent：投机决策也不得越过闸门（开闸只认说完那一刻那份接不接）。
            return
        if not normalize(self.suffix_text()):
            return
        if self.state == "CONFIRMED" and self._llm_prepared and self._llm_prepared["version"] == self.version:
            prepared, self._llm_prepared = self._llm_prepared, None
            job = {"version": prepared["version"], "speculative": prepared.get("speculative", False),
                   "utterance": prepared.get("utterance"), "canceled": False}
            self._apply_llm(now_ms, prepared["result"], job)
            return
        job = self._jobs["llm"]
        if job is not None and not job["canceled"] and job["version"] == self.version:
            return
        if not self._tools_ready():
            # M4: a shortlist from this turn is good enough to start with (it is reissued if it changes);
            # only a turn with no shortlist at all has to wait.
            usable = self.cfg.allow_stale_shortlist and self._tools_result is not None and self._tools_cache is not None
            if not usable:
                if self.state == "CONFIRMED" and not self._tools_jobs and self._tools_pending is None:
                    self._tools_pending = {"version": self.version, "text": self.suffix_text()}
                return
        if self.state == "MAYBE_DONE" and self._llm_prepared and self._llm_prepared["version"] == self.version:
            return
        if self.state == "MAYBE_DONE" and not speculative:
            return
        self._start_llm(now_ms, speculative)

    # 生成中打断（作废本轮）的可观测 reason 白名单：只有本席再开口/新话触发；
    # stale_text / confirm_wait / confirm_timeout / shortlist_changed 是内部收敛或二次确认，
    # 不是用户打断，不得发 turn_void（尤其 CONFIRM_WAIT 二轮回不是「作废本轮」）。
    _VOID_REASONS = ("speech_start", "new_text")

    def _cancel_inflight_llm(self, now_ms, reason):
        """Drop a decision that the user's continuation invalidated (in flight or already finished).

        P0-2 生成中打断 = 一等动作：当取消确由本席再开口触发（reason∈{speech_start,new_text}）且
        真的有东西被作废（在途 job 或已备好的 speculative 结果），发一次 `turn_void` —— 本轮决策
        作废、继续听本席。与 `turn_reopen`（动作已落地后再开口 → 开新一轮）分开。
        CONFIRM_WAIT 下的同因取消发生在二次确认收口路径上（状态机不回 LISTENING），
        不算打断作废，同样不发 turn_void。
        """
        cancelled_something = False
        prepared_speculative = None
        prepared, self._llm_prepared = self._llm_prepared, None
        if prepared is not None:
            # A speculative result that already finished is discarded too, and counted as waste.
            self.counters["llm_wasted"] += 1
            cancelled_something = True
            prepared_speculative = prepared.get("speculative")
            self._emit("llm_cancel", now_ms, reason=reason, speculative=True, prepared=True)
        job = self._jobs["llm"]
        job_speculative = None
        if job is not None and not job["canceled"]:
            job["canceled"] = True
            self.counters["llm_wasted"] += 1
            cancelled_something = True
            job_speculative = job["speculative"]
            self._emit("llm_cancel", now_ms, reason=reason, speculative=job["speculative"], prepared=False)
        if cancelled_something and reason in self._VOID_REASONS and self.state != "CONFIRM_WAIT":
            # 一次打断动作只发一次 turn_void（即便 prepared+job 两路都被作废）；
            # 纯观测事件，不动账目（llm_calls == consumed + llm_wasted 不变式不受影响）。
            self.counters["turn_voids"] += 1
            self._emit("turn_void", now_ms, reason=reason,
                       speculative=prepared_speculative if job is None else job_speculative,
                       voided_prepared=prepared is not None,
                       voided_inflight=job is not None and job["canceled"])

    def _to_state(self, state, now_ms, why):
        if state == self.state:
            return
        previous, self.state = self.state, state
        self._emit("state_change", now_ms, **{"from": previous, "to": state, "why": why})

    def _to_listening(self, now_ms, why):
        self._to_state("LISTENING", now_ms, why)

    def _confirm(self, now_ms, why):
        if self.state == "CONFIRMED":
            return
        if not normalize(self.suffix_text()):
            # Nothing unconsumed to decide on: never ask the LLM about an empty utterance.
            return
        # 「这句说完了」这一刻到了（判停给「说完」/ 静音窗收口）：读数心用这句话最完整的字 + 当时的画面
        # 重新看一次场景（wiki `[[读心请求时机]]` 第二条）。读心只看中枢的收住、不自己判断说完没完；
        # **不等待读心**：这次请求在后台跑，开闸照常走（中枢不因为等场景挂起）。
        if self.mind is not None:
            self.mind.on_utterance_close(self.seat_id, why=why)
        # 记忆：这句话**收住这一刻**写一刀（原话 + 这一刻的场景）。放在闸门之前：
        # 开闸进 Agent / 说完但不回答只进上下文 **两条路由都要写**（wiki §4）；闸门这一侧
        # 的判据（锁字那一帧）不受影响。
        self._memory_write_close(now_ms, why)
        if self.gate is not None:
            self._gate_confirm(now_ms, why)
            return
        self.t_confirmed = now_ms if self.t_confirmed is None else self.t_confirmed
        self._to_state("CONFIRMED", now_ms, why)
        self._maybe_start_llm(now_ms)

    # ---------- 中枢并发闸门 ----------

    def _on_gate_event(self, event):
        """中枢（闸门）事件与本席事件同流：回放 / 调试页按同一个来源读。

        事件形状与本席事件对齐（`seat_id` / `mic_id` / `text_version`），前端一套过滤逻辑就够。
        """
        event.setdefault("seat_id", self.seat_id)
        if self.mic_id is not None:
            event.setdefault("mic_id", self.mic_id)
        event.setdefault("text_version", event.get("version", self.version))
        if event.get("event") == "hub_pace" and event.get("state") == "ok":
            # 这一拍实际的节奏（已按「开场稳住 / 平滑换档」压过）留在本席，摘要与台面都用它。
            self.pace_last = {"pace": event.get("pace"), "label": event.get("label"),
                              "raw_pace": event.get("raw_pace"), "held": event.get("held"),
                              "hold_reason": event.get("hold_reason"),
                              "speech_ms": event.get("speech_ms"),
                              "t_ms": event.get("t_ms")}
        self.events.append(event)
        if self.on_event:
            self.on_event(event)

    def _gate_confirm(self, now_ms, why):
        """「判定说完」这一刻到：用同一版字上的那份接不接决定开不开闸。

        - 开闸（说完且接）→ CONFIRMED，才排工具披露 / 决策（进 Agent）
        - 只进上下文（说完但不接）→ 不开闸，文本封口只作上下文，回到 LISTENING
        - 还没说完 → 先不开闸；等同一版字的接不接（或超时 → 不进 Agent）
        """
        assume_done = why != "eot_complete"
        verdict = self.gate.gate(self.version, now_ms, assume_done=assume_done)
        self._gate_verdict = verdict
        route = verdict.get("route")
        if route == ROUTE_AGENT:
            self._open_agent_context(now_ms, verdict)
            self._gate_open_version = self.version
            self._gate_waiting_version = None
            self.t_confirmed = now_ms if self.t_confirmed is None else self.t_confirmed
            self._emit("gate_open", now_ms, reason=verdict.get("reason"), why=why,
                       accept=verdict.get("accept"), scene=verdict.get("scene"),
                       two_route_ms=verdict.get("two_route_ms"))
            self._to_state("CONFIRMED", now_ms, "gate_open")
            self._maybe_start_llm(now_ms)
            return True
        if route == ROUTE_CONTEXT_ONLY:
            self._gate_absorb(now_ms, verdict)
            return False
        if route == ROUTE_ACCEPT_UNAVAILABLE:
            # 接不接超时 / 失败了：**先不开闸，不当成「不接」**。文本不封口、不进上下文，
            # 继续等同一版字的这份结论（晚回来仍可开闸）；界面能看见是超时/失败。
            self._gate_waiting_version = self.version
            self._emit("gate_pending", now_ms, why=why, state=verdict.get("state"),
                       reason=verdict.get("reason"), accept=verdict.get("accept"),
                       text_version=self.version)
            return False
        # 还没说完 / 接不接还在飞：先不开闸（不排工具、不发起决策），等这一份。
        self._gate_waiting_version = self.version
        self._emit("gate_pending", now_ms, why=why, state=verdict.get("state"),
                   reason=verdict.get("reason"), eot=verdict.get("eot"),
                   text_version=self.version)
        return False

    def _gate_resolve_wait(self, now_ms):
        """闸门在等同一版字的接不接：结果到了就按开闸结论继续（不重新取场景）。"""
        if self.gate is None or self._gate_waiting_version is None:
            return
        if self._gate_waiting_version != self.version or self.turn_done:
            self._gate_waiting_version = None
            return
        if self.state == "CONFIRMED":
            self._gate_waiting_version = None
            return
        verdict = self.gate.gate(self.version, now_ms, assume_done=True)
        self._gate_verdict = verdict
        route = verdict.get("route")
        if route in (ROUTE_WAIT, ROUTE_ACCEPT_UNAVAILABLE):
            # 还在等接不接（含超时/失败：先不开闸，也不当成「不接」）。晚回来下一拍继续判。
            return
        self._gate_waiting_version = None
        if route == ROUTE_AGENT:
            self._open_agent_context(now_ms, verdict)
            self._gate_open_version = self.version
            self.t_confirmed = now_ms if self.t_confirmed is None else self.t_confirmed
            self._emit("gate_open", now_ms, reason=verdict.get("reason"), why="gate_resolved",
                       accept=verdict.get("accept"), scene=verdict.get("scene"),
                       two_route_ms=verdict.get("two_route_ms"))
            self._to_state("CONFIRMED", now_ms, "gate_open")
            self._maybe_start_llm(now_ms)
        else:
            self._gate_absorb(now_ms, verdict)

    def _gate_absorb(self, now_ms, verdict):
        """说完但不接：只进上下文，不进 Agent；原话按席留进旁听台账（方案 B）。

        文本封口（不能被后面的 partial 改写、也不复活成待执行请求），记 `context_absorbed`；
        不选工具、不决策、不 running、不打相关分。**只有「说完但不接」这一支记账**——
        判断未回 / 超时 / 失败 / 版本不齐都不按「不接」处置（wiki §5 第 4 行、§14）。
        """
        text = self.suffix_text()
        overheard = self._record_overhear(now_ms, text, verdict)
        self._seal_consumed()
        self.consumed_version = self.version
        self.t_last_consumed = now_ms
        self.context_consumed += 1
        self._emit("context_absorbed", now_ms, text=text, reason=verdict.get("reason"),
                   route=verdict.get("route"), state=verdict.get("state"),
                   accept=verdict.get("accept"), scene=verdict.get("scene"),
                   text_version=self.version)
        self._to_state("LISTENING", now_ms, "gate_context_only")
        self._maybe_finish_session(now_ms)

    # ---------- 应一声（期一：中枢的「陪着说」） ----------

    # 允许应声的三个等待档：弱 complete（0.7s）/ 话断了（1.5s）/ 还在想（3s）。「说完了当刻」不在内。
    _ACK_WAIT_STATES = ("MAYBE_DONE", "CUT_OFF_WAIT", "THINK_WAIT")

    def _ack_is_echo(self, text: str, t_ms: int) -> bool:
        """刚说过的那句被本机麦收回来：与本机最近一句应声高度重合的字当回声丢掉。

        只认「出声后 `ACK_ECHO_MS` 内 + 这一版字基本就是那句应声」的极短情况（字数不超过应声 +2、
        且应声的字有八成出现在这一版里）；正常说话一个字不动，极短应声也不必停听（见拍板第 2 条）。
        """
        if self._ack_last_ms is None or self._ack_last_text is None:
            return False
        if t_ms - self._ack_last_ms > ACK_ECHO_MS:
            return False
        spoken = normalize(self._ack_last_text)
        heard = normalize(text)
        if not spoken or not heard:
            return False
        if len(heard) > len(spoken) + 2:
            return False
        overlap = sum(min(spoken.count(char), heard.count(char)) for char in set(spoken))
        return overlap / len(spoken) >= 0.8

    # ---------- 垫一句（期二：中枢的「陪着说」，记住但不进 Agent） ----------

    def _speech_blocked(self, now_ms, *, gap_ms=None):
        """期五：四处共用的静默闸——同一时刻最多一句；出声后 1 秒内谁都不再出声。

        应声 / 垫句 / 垫句抢回 / 冷场开口共用同一个「上次出声」锚点 `_ack_last_ms`；这里只把
        那条规则收在一处（期五的口径收口），不新增判断、不改 1 秒这个数。
        """
        return hub_arbiter.speech_blocked(now_ms=now_ms, last_spoken_ms=self._ack_last_ms,
                                          gap_ms=gap_ms)

    def _speech_run_ms(self, now_ms) -> int:
        """这一轮对方**已经连续说了多久**（垫句「说满 1.5 秒」的那把尺）。

        口径（方案 §10 第 4 条）：按**连续有声时长**算——从这一段连续出声的起点算到这一版字。
        起点取本席的 `speech_start_ms`（VAD 说「开口」那一刻；重开一轮时 `_reset_turn` 也会把它
        设成本轮开头），没有 VAD 事件时退回 `turn_start_ms`（这一轮的开头）。只读，不写任何锚点。
        """
        start = self.speech_start_ms if self.speech_start_ms is not None else self.turn_start_ms
        if start is None:
            return 0
        return max(0, int(now_ms) - int(start))

    def _maybe_cushion(self, now_ms: int) -> bool:
        """三个等待档里问一次「要不要垫一句」：**只有积极 + 已连续说满 1.5 秒**那一版才问。

        与应声的分工（拍板 5）：**同一时刻最多一句，垫句优先**；出声后 1 秒内不再出声
        （两者共用同一个「上次出声」锚点 `_ack_last_ms`）。垫句**记住**——进本席上下文台账
        `hub_said`（跨轮留存），但**不进 Agent、不选工具、不执行、不开闸**。

        返回这一拍有没有真的垫了一句（交给 `_maybe_ack` 做「垫句优先」）。
        """
        if self.gate is None or not self.cfg.cushion_enabled or self.turn_done:
            return False
        if self.state not in self._ACK_WAIT_STATES:
            return False
        ready = self.gate.cushion_inputs()
        fresh = [item for item in ready if int(item["version"]) not in self._cushion_tried]
        if not fresh:
            return False
        marks = fresh[0]                     # 从新到旧：只认最近这一版
        for item in fresh:
            self._cushion_tried.add(int(item["version"]))
        if len(self._cushion_tried) > 32:
            self._cushion_tried = set(sorted(self._cushion_tried)[-16:])
        version = int(marks["version"])
        age_ms = now_ms - int(marks.get("t_start_ms") or now_ms)
        pace = marks.get("pace") or {}
        speech_ms = marks.get("speech_ms")
        tier = pace.get("pace")
        text = cushion_text(self.cushion_spoken_total)
        if age_ms > CUSHION_STALE_MS:
            self.counters["cushion_suppressed"] += 1
            self._emit("cushion_gate", now_ms, layer="hub", spoken=False, pace=tier,
                       reason="这一版已经过去 %.0fms（> %dms）：不追着旧话垫句"
                              % (age_ms, CUSHION_STALE_MS),
                       text=text, chars=cushion_chars(text), version=version, age_ms=age_ms,
                       speech_ms=speech_ms)
            return False
        say, reason = decide_cushion(pace=pace, cushion=marks.get("cushion"),
                                     eot=marks.get("eot"), accept=marks.get("accept"),
                                     speech_ms=speech_ms, last_spoken_ms=self._ack_last_ms,
                                     now_ms=now_ms, gap_ms=self.cfg.cushion_gap_ms, text=text)
        if not say:
            self.counters["cushion_suppressed"] += 1
            self._emit("cushion_gate", now_ms, layer="hub", spoken=False, pace=tier,
                       asked=bool((marks.get("cushion") or {}).get("asked")), reason=reason,
                       text=text, chars=cushion_chars(text), version=version, age_ms=age_ms,
                       speech_ms=speech_ms)
            return False
        gap_ms = None if self._ack_last_ms is None else now_ms - self._ack_last_ms
        self.cushion_spoken_total += 1
        # 与应声共用「上次出声」锚点：同一时刻最多一句；出声后 1 秒内谁都不再出声（拍板 5 的原话）。
        self._ack_last_ms = now_ms
        self._ack_last_text = text
        # 期四：垫一句也是「中枢说过一句」——冷场从这一句说完起算。
        self._quiet_note_hub_said(now_ms, text, kind="cushion")
        self.counters["cushions"] += 1
        entry = {"t_ms": int(now_ms), "text": text, "chars": cushion_chars(text),
                 "gap_ms": gap_ms, "version": version, "turn": self.turn_index,
                 "seat": self.seat_id, "pace": tier, "speech_ms": speech_ms,
                 "say": (marks.get("cushion") or {}).get("score")}
        self.cushion_log.append(entry)
        # 「记住」：进本席对话上下文台账（跨轮留存，摘要 / 台面 / 后续期次都能读到）；
        # **不进 Agent**：不选工具、不执行、不开闸，也不拼进 Agent 的请求上下文。
        self.hub_said.append({"t_ms": int(now_ms), "text": text, "kind": "cushion",
                              "version": version, "turn": self.turn_index,
                              "seat": self.seat_id, "pace": tier, "chars": entry["chars"]})
        if len(self.hub_said) > 8:
            self.hub_said = self.hub_said[-8:]
        # 出声出口（最小版）：台面出字 + 播报桩。本期不接真 TTS，也不进 Agent / 不开闸。
        self._emit("cushion_spoken", now_ms, layer="hub", text=text, chars=entry["chars"],
                   gap_ms=gap_ms, spoken_total=self.cushion_spoken_total, version=version,
                   turn=self.turn_index, age_ms=age_ms, pace=tier, speech_ms=speech_ms,
                   source=(marks.get("cushion") or {}).get("source"),
                   model=(marks.get("cushion") or {}).get("model"),
                   score=entry["say"], reason=reason, context=True, agent=False,
                   speech="台面出字 + 播报桩（本期不接真 TTS）",
                   note="垫一句＝陪着说：记住（进上下文），但不开闸、不进 Agent、不选工具、不执行")
        return True

    def _maybe_ack(self, now_ms: int, cushion_spoke: bool = False) -> None:
        """三个等待档里问一次「要不要应一声」：出不出声由 `hub_ack.decide_ack` 的三道闸决定。

        - 只认**同一版字**上判断都回来的那一版（从新到旧，只取最近这一版；更老的算看过不补声）；
        - 这一版已经过去 1.5 秒 → 不追着旧话出声；
        - 出声这一下只发事件（台面出字 + 播报桩），**不碰任何计时器**——应声不延长也不缩短等待；
        - **垫句优先**（拍板 5）：这一拍刚垫了一句，就不应声（仍照原样记一条「没出声」）。
        """
        if self.gate is None or not self.cfg.ack_enabled or self.turn_done:
            return
        if self.state not in self._ACK_WAIT_STATES:
            return
        ready = self.gate.ack_inputs()
        if cushion_spoke:
            fresh = [item for item in ready if int(item["version"]) not in self._ack_tried]
            if fresh:
                for item in fresh:
                    self._ack_tried.add(int(item["version"]))
                self.counters["ack_suppressed"] += 1
                self._emit("ack_gate", now_ms, layer="hub", spoken=False,
                           reason="同一时刻最多一句：垫句优先（这一拍刚垫了一句）",
                           version=int(fresh[0]["version"]))
            return
        fresh = [item for item in ready if int(item["version"]) not in self._ack_tried]
        if not fresh:
            return
        marks = fresh[0]                     # 从新到旧：只认最近这一版
        for item in fresh:
            self._ack_tried.add(int(item["version"]))
        if len(self._ack_tried) > 32:
            self._ack_tried = set(sorted(self._ack_tried)[-16:])
        version = int(marks["version"])
        age_ms = now_ms - int(marks.get("t_start_ms") or now_ms)
        text = ack_text(self.ack_spoken_total)
        if age_ms > ACK_STALE_MS:
            self.counters["ack_suppressed"] += 1
            self._emit("ack_gate", now_ms, layer="hub", spoken=False, say=marks["ack"].get("say"),
                       reason="这一版已经过去 %.0fms（> %dms）：不追着旧话出声" % (age_ms, ACK_STALE_MS),
                       text=text, chars=ack_chars(text), version=version, age_ms=age_ms)
            return
        say, reason = decide_ack(eot=marks.get("eot"), accept=marks.get("accept"),
                                 ack=marks.get("ack"), last_spoken_ms=self._ack_last_ms,
                                 now_ms=now_ms, gap_ms=self.cfg.ack_gap_ms, text=text)
        if not say:
            self.counters["ack_suppressed"] += 1
            self._emit("ack_gate", now_ms, layer="hub", spoken=False, say=marks["ack"].get("say"),
                       reason=reason,
                       text=text, chars=ack_chars(text), version=version, age_ms=age_ms)
            return
        gap_ms = None if self._ack_last_ms is None else now_ms - self._ack_last_ms
        self.ack_spoken_total += 1
        self._ack_last_ms = now_ms
        self._ack_last_text = text
        # 期四：应一声也是「中枢说过一句」（它不记忆，但冷场照样从这一声起算）。
        self._quiet_note_hub_said(now_ms, text, kind="ack")
        self.counters["acks"] += 1
        entry = {"t_ms": int(now_ms), "text": text, "chars": ack_chars(text),
                 "gap_ms": gap_ms, "version": version, "turn": self.turn_index,
                 "seat": self.seat_id, "say": 0.8 if (marks["ack"].get("score") is None)
                 else marks["ack"].get("score")}
        self.ack_log.append(entry)
        # 出声出口（最小版）：台面出字 + 播报桩。本期不接真 TTS，也不进上下文 / 不开闸。
        self._emit("ack_spoken", now_ms, layer="hub", text=text, chars=ack_chars(text), gap_ms=gap_ms,
                   spoken_total=self.ack_spoken_total, version=version, turn=self.turn_index,
                   age_ms=age_ms, source=marks["ack"].get("source"), model=marks["ack"].get("model"),
                   score=marks["ack"].get("score"), reason=reason,
                   speech="台面出字 + 播报桩（本期不接真 TTS）",
                   note="应一声＝陪着说：不记住、不开闸、不进 Agent、不选工具、不执行")

    # ---------- 两人同时说四选（期三）：这边正在说 / 正在准备说，人又开口 ----------
    #
    # 「这边正在说」按期三已收下的读法有**两个入口**（期五沿用，不收窄）：
    #   窗口一 = **开闸之后正在准备说 / 正在回答**（`CONFIRMED` 且这一句还没落地：决策在飞 / 已备好 /
    #           短名单在飞）——这正是改动前「生成中人又开口 → 作废本轮」的那个状态。
    #   窗口二 = **中枢正在说一句垫句**（期二垫句 / 期三垫句抢回，取 1 秒窗口）——拍板 3 明确算进来。
    # 应一声**不算**（它是极短的一声，不是一轮回答，拍板 3）；「还在判停 / 还没开闸」那一侧也不算
    # （那里本来就按 0.7 / 1.5 / 3 秒收住，人在同一句续说时不该打断）。
    #
    # 插话这一段**不进当前这句**：不改 segment / 版号 / 任何计时（0.7 / 1.5 / 3 秒、2 秒上限都不动），
    # 四选回来再按出口放：
    #   让路     → 按原路把插话喂回去（`on_asr` 老路：作废本轮 / 话留下 / 回到收听，逐字段同改动前）；
    #   说完再接 → 插话进 FIFO 队列，当前这句照旧落地；这句收口时把插话**作为下一轮**发出；
    #   继续说   → 插话只进旁听上下文（与「不是对车说」同一口径），当前这句照旧落地；
    #   垫句抢回 → 插话进队列 + 这句收口那一刻紧接一句垫句（记住、不开闸），随后把插话作为下一轮发出。
    #
    # 判断没回来 / 失败 / 超时 → **继续说**（拍板 5）。四选还在飞时当前这句**先不落地**
    # （`_floor_hold_version`），否则「让路」就没有东西可作废、四条出口也不再各是各的。

    def _floor_answer_pending(self) -> bool:
        """这一句「要说的」还没落地：决策在飞 / 已备好 / 短名单还在飞。"""
        if self._llm_prepared is not None:
            return True
        job = self._jobs.get("llm")
        if job is not None and not job.get("canceled"):
            return True
        return self._tools_pending is not None or bool(self._tools_jobs)

    def _floor_cushion_speaking(self, now_ms=None):
        """中枢正在说一句垫句（期二垫句 / 期三垫句抢回）的那一小段（拍板 3 把它算「这边正在说」）。

        锚点用 `hub_said` 里最近一条垫句（应声不进这张表，所以应声那一声不会被当成「正在说」），
        时长用 `SPEAK_HOLD_MS`（与「出声后 1 秒内不再出声」同一个常量）。
        """
        if not self.hub_said:
            return None
        last = self.hub_said[-1]
        if last.get("kind") not in ("cushion", "cushion_back"):
            return None
        if now_ms is None:
            return last
        if int(now_ms) - int(last.get("t_ms") or 0) < int(SPEAK_HOLD_MS):
            return last
        return None

    def _floor_turn_window(self) -> bool:
        """窗口一：这边正在准备说 / 正在回答（闸门已开、这一句还没落地）——改动前「让路」那个状态。"""
        if self.turn_done or self.state != "CONFIRMED":
            return False
        if not normalize(self.suffix_text()):
            return False
        return self._floor_answer_pending()

    def _floor_available(self, now_ms=None) -> bool:
        """这一版是不是「这边正在说 / 正在准备说、人又开口」的那一版（四选只在这种版上问）。"""
        if not self.cfg.floor_enabled or self.gate is None:
            return False
        if getattr(self.gate, "floor_fn", None) is None:
            return False
        if self._floor_turn_window():
            return True
        # 窗口二：中枢正在说一句垫句（期二垫句 / 期三垫句抢回）——拍板 3 明确算进来。
        return now_ms is not None and self._floor_cushion_speaking(now_ms) is not None

    def _floor_holding(self) -> bool:
        """VAD 那一侧：已经问过、且出口不是「让路」→ 当前这句照旧说完，这里不作废。"""
        episode = self._floor_episode
        if episode is None:
            return False
        if not episode["done"]:
            return True
        return episode.get("outlet") in (FLOOR_FINISH_FIRST, FLOOR_CONTINUE, FLOOR_CUSHION_BACK)

    @staticmethod
    def _floor_same_episode(item, norm: str) -> bool:
        """同一段插话的续版（ASR 反复重发整句）：只更新文本，不再问一次。"""
        body = item.get("norm") or ""
        return bool(body) and bool(norm) and (norm.startswith(body) or body.startswith(norm))

    def _floor_extend(self, item, t_ms, text):
        item["text"] = text
        item["norm"] = normalize(text)
        item["t_ms"] = t_ms
        self._emit("floor_insertion", t_ms, layer="hub", episode=item.get("id") or item.get("episode"),
                   text=text, note="同一段插话的续版：只更新这一句的字，不再问四选（同一段只问一次）")
        if item is self._floor_continued:
            # 继续说：这一句还在往外冒字——旁听台账里那一行补全成整句（同一句只长不改，别的行不动）。
            amended = None
            if self.overhear is not None:
                amended = self.overhear.amend_last(self.seat_id or "D", text)
                if amended is None:
                    # 判断回来时还没有字（VAD 先开口）：这一行现在才第一次记下来。
                    amended = self.overhear.record(self.seat_id or "D", text, t_ms)
            for row in self.floor_context[-1:]:
                if row.get("episode") == item.get("id"):
                    row["text"] = text
                    row["amended"] = True
            self._emit("floor_context", t_ms, layer="hub", episode=item["id"], text=text,
                       label=overhear_ledger.LINE_LABEL, amended=True,
                       kept=(amended or {}).get("kept"), context=True, agent=False,
                       note="继续说：同一句补全（旁听台账里那一行只长不改），仍不进 Agent、不开闸")

    def _floor_catch(self, t_ms, text, final) -> bool:
        """人又开口的那一版：拦下来问一次四选（返回 True = 这一段不进当前这句）。"""
        if self._floor_replaying or not self._floor_available(t_ms):
            return False
        norm = normalize(text)
        if not norm:
            return False
        episode = self._floor_episode
        if episode is not None and not episode["done"]:
            if not episode["norm"]:
                # VAD 先开的口、这是同一段插话的第一版字：接到同一回上（不重复问一次）
                self._floor_extend(episode, t_ms, text)
                return True
            if self._floor_same_episode(episode, norm):
                self._floor_extend(episode, t_ms, text)
                return True
        # 已经排队 / 已经只进旁听的那一段的续版：只更新文本，不重复问、不重复排、不重复记。
        for item in ([self._floor_queue[-1]] if self._floor_queue else []) + \
                    ([self._floor_continued] if self._floor_continued else []):
            # 空的这一条 = VAD 先开口、判断先回来（还没出字）：第一版字就接到这一条上。
            if not item.get("norm") or self._floor_same_episode(item, norm):
                self._floor_extend(item, t_ms, text)
                return True
        self._floor_start(t_ms, text, final, source="asr")
        return True

    def _floor_vad_start(self, t_ms) -> bool:
        """VAD 说人开口了：这边正在说 → 不先作废，先问四选（让路时再按原路作废）。"""
        if self._floor_replaying or not self._floor_available(t_ms):
            return False
        episode = self._floor_episode
        if episode is not None and not episode["done"]:
            self._emit("floor_vad", t_ms, layer="hub", episode=episode["id"], held=True,
                       note="人又开口（VAD）：这一回四选还在飞，不先作废当前这句")
            return True
        self._floor_start(t_ms, "", False, source="vad")
        return True

    def _floor_start(self, t_ms, text, final, *, source: str) -> None:
        self._floor_seq += 1
        cushion = None if self._floor_turn_window() else self._floor_cushion_speaking(t_ms)
        window = "speech" if cushion is not None else "turn"
        hub_state = "speaking" if cushion is not None else "preparing"
        hub_text = (cushion or {}).get("text") if cushion is not None else \
            (self.suffix_text() or self.turn_text())
        episode = {"id": self._floor_seq, "text": text, "norm": normalize(text), "t_ms": int(t_ms),
                   "asked_ms": int(t_ms), "final": bool(final), "done": False, "outlet": None,
                   "reason": None, "source": source, "turn": self.turn_index, "version": self.version,
                   "window": window, "hub_state": hub_state, "hub_sentence": hub_text}
        self._floor_episode = episode
        if window == "turn":
            # 只有「正在准备说」这一路需要冻结：让路要作废的就是这一版结论。
            self._floor_hold_version = self.version
        self.floor_asks_total += 1
        self.counters["floor_asks"] += 1
        self._emit("floor_asked", t_ms, layer="hub", episode=episode["id"], text=text, source=source,
                   version=self.version, turn=self.turn_index, hub_state=hub_state,
                   hub_sentence=hub_text, window=window,
                   note=("这边正在说 / 正在准备说，人又开口：问一次四选"
                         "（同一段插话只问一次；让路＝改动前那条）" if window == "turn" else
                         "中枢正在说一句垫句，人又开口：同样问一次四选（拍板 3：垫句也算「正在说」）"))
        self.gate.start_floor(episode["id"], text, hub_text, self._scene_now(), t_ms,
                             hub_state=hub_state, version=self.version)

    def _floor_step(self, now_ms) -> None:
        """四选回来就按出口办（在 tick 的最前面做：先于 `_poll`，保证「让路」取消还在飞的那一句）。"""
        episode = self._floor_episode
        if episode is None or episode["done"] or self._floor_in_step or self.gate is None:
            return
        if not self.cfg.floor_enabled:
            return
        verdict = self.gate.floor_verdict(episode["id"], now_ms)
        if verdict is None or verdict.get("state") == "pending":
            return
        outlet, reason = decide_floor(floor=verdict)
        self._floor_in_step = True
        try:
            self._floor_apply(now_ms, episode, outlet, reason, verdict)
        finally:
            self._floor_in_step = False

    def _floor_apply(self, now_ms, episode, outlet, reason, verdict) -> None:
        window = episode.get("window") or "turn"
        if window == "speech" and outlet == FLOOR_CUSHION_BACK:
            # 当前这句本身就是一句垫句：再叠一句就成两句连着垫（同一时刻最多一句），
            # 所以这一路按「说完再接」落地——插话排队、随后作为下一轮回答，不丢。
            outlet = FLOOR_FINISH_FIRST
            reason = (reason or "") + "（当前这句本身就是垫句，不再叠第二句：插话排队、随后回答）"
        if outlet != FLOOR_YIELD and not episode["text"]:
            # VAD 先开口、四选回来时还没出字：没有可排的字（排一个空句、垫一句给谁都没意义），
            # 按「继续说」兜底——字到了再按旁听记下来（`_floor_extend` 会补上这一行）。
            outlet = FLOOR_CONTINUE
            reason = (reason or "") + "（VAD 先开口、四选回来时还没出字 → 按「继续说」：没有可排的字）"
        episode["done"] = True
        episode["outlet"] = outlet
        episode["reason"] = reason
        episode["decided_ms"] = int(now_ms)
        if self._floor_hold_version == episode["version"]:
            self._floor_hold_version = None
        counter = {"yield": "floor_yields", "finish_first": "floor_finish_first",
                   "continue": "floor_continues", "cushion_back": "floor_cushion_backs"}.get(outlet)
        if counter:
            self.counters[counter] += 1
        if verdict.get("state") == "timeout":
            self.counters["floor_timeouts"] += 1
        self._emit("floor_outcome", now_ms, layer="hub", episode=episode["id"], outlet=outlet,
                   label=floor_label(outlet), text=episode["text"], reason=reason,
                   verdict_state=verdict.get("state"), choice=verdict.get("choice"),
                   asked_t_ms=episode["t_ms"], waited_ms=int(now_ms) - episode["asked_ms"],
                   replay=outlet == FLOOR_YIELD,
                   void=outlet == FLOOR_YIELD and window == "turn", window=window,
                   queued=outlet in (FLOOR_FINISH_FIRST, FLOOR_CUSHION_BACK),
                   context_only=outlet == FLOOR_CONTINUE,
                   cushion=outlet == FLOOR_CUSHION_BACK, agent=False,
                   note="四选结论：插话不进当前这句；四条出口都不改收口时长与开闸时机")
        self.floor_log.append({"t_ms": int(now_ms), "asked_t_ms": episode["t_ms"],
                               "episode": episode["id"], "outlet": outlet,
                               "label": floor_label(outlet), "text": episode["text"],
                               "reason": reason, "verdict_state": verdict.get("state"),
                               "turn": episode["turn"]})
        if outlet == FLOOR_YIELD:
            if window == "speech":
                # 让路在垫句那一小段：先把这句垫句停下（本机只记一笔，不接真 TTS、不做真中止播报），
                # 再按原来那条把插话喂回去（回到收听、话留下）。
                self._floor_stop_cushion(now_ms, episode)
            # 让路＝现网那条：按**原来的路径**把插话喂回去（作废本轮 / 话留下 / 回到收听），
            # 事件字段与改动前逐字段一致（`_floor_replaying` 保证不被再拦一次）。
            self._floor_replaying = True
            try:
                if episode["text"]:
                    self.on_asr(episode["t_ms"], episode["text"], episode.get("final", True))
                else:
                    self._to_listening(episode["t_ms"], "speech_start")
                    self._cancel_inflight_llm(episode["t_ms"], "speech_start")
            finally:
                self._floor_replaying = False
            return
        if outlet in (FLOOR_FINISH_FIRST, FLOOR_CUSHION_BACK):
            not_before = None
            if window == "speech":
                cushion = self._floor_cushion_speaking(episode["t_ms"]) or {}
                not_before = int(cushion.get("t_ms") or 0) + int(SPEAK_HOLD_MS)
            self._floor_enqueue(now_ms, episode, outlet, not_before_ms=not_before)
            if outlet == FLOOR_CUSHION_BACK:
                # 「垫句抢回」= 当前这句**不中止**：垫句叠在它后面（这句收口那一刻再垫）。
                self._floor_pending_cushion = {"episode": episode["id"],
                                               "text": episode["text"],
                                               "t_ms": episode["t_ms"]}
            self._maybe_start_llm(now_ms)     # 放行：当前这句照旧落地
            return
        # 继续说：插话只进旁听上下文（不是对车说），当前这句照旧说完并继续。
        self._floor_record_context(now_ms, episode)
        self._maybe_start_llm(now_ms)

    def _floor_enqueue(self, now_ms, episode, outlet, *, not_before_ms=None) -> None:
        item = {"episode": episode["id"], "text": episode["text"], "norm": episode["norm"],
                "t_ms": episode["t_ms"], "final": episode.get("final", True), "outlet": outlet,
                "turn": episode["turn"], "index": len(self._floor_queue) + 1}
        if not_before_ms:
            # 垫句那一小段：等这句垫句「说完」（SPEAK_HOLD_MS）再放行（不再往前挤）。
            item["not_before_ms"] = int(not_before_ms)
        self._floor_queue.append(item)
        self.counters["floor_queued"] += 1
        self._emit("floor_queued", now_ms, layer="hub", episode=episode["id"], text=episode["text"],
                   index=item["index"], queued=len(self._floor_queue), outlet=outlet,
                   label=floor_label(outlet),
                   note=("当前这句照旧说完（不作废）：插话排队，等这句说完后作为下一轮回答"
                         if outlet == FLOOR_FINISH_FIRST else
                         "当前这句说完、垫句紧随其后：插话排队，随后作为下一轮回答（字不丢）"))

    def _floor_stop_cushion(self, now_ms, episode) -> bool:
        """让路落在垫句那一小段：把这句垫句停下（台面记一笔；本期不接真 TTS、不做真中止播报）。"""
        cushion = self._floor_cushion_speaking(episode["t_ms"]) or (self.hub_said[-1] if self.hub_said else {})
        text = cushion.get("text")
        if not text:
            return False
        self._emit("cushion_stopped", now_ms, layer="hub", episode=episode["id"], text=text,
                   kind=cushion.get("kind"), reason="让路：人又开口，这句垫句停下、回到收听",
                   context=True, agent=False,
                   note="让路落在垫句那一小段：这句垫句停下（本机只记一笔，不接真 TTS、不做真中止播报），"
                        "插话留下、继续听这一席")
        return True

    def _floor_record_context(self, now_ms, episode) -> None:
        """继续说：插话只进旁听上下文——与「不是对车说」同一口径（同一本台账、同一句标签）。"""
        entry = None
        if self.overhear is not None:
            entry = self.overhear.record(self.seat_id or "D", episode["text"], now_ms)
        self._floor_continued = episode
        self.floor_context.append({"t_ms": int(now_ms), "text": episode["text"],
                                   "episode": episode["id"], "label": overhear_ledger.LINE_LABEL})
        self._emit("floor_context", now_ms, layer="hub", episode=episode["id"], text=episode["text"],
                   label=overhear_ledger.LINE_LABEL, kept=(entry or {}).get("kept"),
                   ledger=(self.overhear.snapshot() if self.overhear is not None else None),
                   context=True, agent=False,
                   note="继续说：插话只进旁听上下文（与「不是对车说」同一口径），"
                        "不进 Agent、不选工具、不执行、不开闸")
        if entry is not None:
            self._emit("overhear_recorded", now_ms, layer="hub", seat=entry["seat"],
                       seat_label=entry["seat_label"], text=entry["text"], label=entry["label"],
                       kept=entry.get("kept"), keep_per_seat=self.overhear.keep,
                       ledger=self.overhear.snapshot(),
                       note="继续说：插话按「不是对车说」记进本席台账（最近 %d 句）"
                            % self.overhear.keep)

    def _floor_speak_cushion(self, now_ms, item) -> bool:
        """垫句抢回的那一句：与期二垫句同一套「陪着说」——记住（进上下文），不开闸、不进 Agent。"""
        text = cushion_text(self.cushion_spoken_total)
        chars = cushion_chars(text)
        hold = max(int(self.cfg.cushion_gap_ms), int(SPEAK_HOLD_MS))
        gap_ms = None if self._ack_last_ms is None else int(now_ms) - int(self._ack_last_ms)
        blocked, why = self._speech_blocked(now_ms, gap_ms=hold)
        if blocked:
            self.counters["floor_cushions_blocked"] += 1
            self._emit("floor_gate", now_ms, layer="hub", episode=(item or {}).get("episode"),
                       spoken=False, text=text, chars=chars, reason=why,
                       note="这次不垫，但插话照旧排队、随后作为下一轮回答（字不丢）")
            return False
        self.cushion_spoken_total += 1
        self.floor_cushions += 1
        self.counters["floor_cushions"] += 1
        self._ack_last_ms = int(now_ms)
        self._ack_last_text = text
        # 期四：垫句抢回也是「中枢说过一句」——冷场从这一句说完起算。
        self._quiet_note_hub_said(int(now_ms), text, kind="cushion_back")
        entry = {"t_ms": int(now_ms), "text": text, "chars": chars, "gap_ms": gap_ms,
                 "version": self.version, "turn": self.turn_index, "kind": "cushion_back",
                 "pace": (self.pace_last or {}).get("pace")}
        self.cushion_log.append(entry)
        self.hub_said.append({"t_ms": int(now_ms), "text": text, "chars": chars,
                              "kind": "cushion_back", "turn": self.turn_index,
                              "note": "垫句抢回：记住（进上下文），不进 Agent"})
        self._emit("cushion_spoken", now_ms, layer="hub", text=text, chars=chars, gap_ms=gap_ms,
                   spoken_total=self.cushion_spoken_total, version=self.version,
                   turn=self.turn_index, kind="cushion_back", episode=(item or {}).get("episode"),
                   pace=(self.pace_last or {}).get("pace"), speech_ms=None,
                   reason="垫句抢回：当前这句说完，紧接一句把话头抢回来",
                   context=True, agent=False,
                   speech="台面出字 + 播报桩（本期不接真 TTS）",
                   note="垫句抢回＝陪着说：记住（进上下文），但不进 Agent、不选工具、不执行、不开闸")
        return True

    def _floor_release_step(self, now_ms) -> None:
        """说完再接 / 垫句抢回：当前这句说完就换下一轮——把排队的插话**真的发出去**（不丢、顺序不变）。"""
        if not self._floor_queue or self._floor_replaying or self._floor_in_step:
            return
        if self._floor_episode is not None and not self._floor_episode["done"]:
            return                              # 这一回四选还在飞：当前这句先不落地
        if self._floor_hold_version is not None:
            return
        if normalize(self.suffix_text()):
            return                              # 当前这句还有没消费的字
        job = self._jobs.get("llm")
        if job is not None and not job.get("canceled"):
            return                              # 决策还在飞：等它落地
        if self._llm_prepared is not None and self.state == "CONFIRMED":
            return                              # 已备好但还没落地：先让它落地
        not_before = self._floor_queue[0].get("not_before_ms")
        if not_before and now_ms < int(not_before):
            return                              # 垫句那一小段还没说完：等它说完再放行
        if not self.turn_done:
            if not (self.consumed_count or self.context_consumed):
                return                          # 当前这句还没被消费过：等它落地再收口
            self._finish_turn(now_ms)           # 当前这句真的说完 → 收口
        if self._floor_pending_cushion is not None:
            pending = self._floor_pending_cushion
            self._floor_pending_cushion = None
            self._floor_speak_cushion(now_ms, pending)   # 垫句紧随这句之后
        item = self._floor_queue.pop(0)
        self.counters["floor_released"] += 1
        self.floor_released_total += 1
        self._emit("floor_released", now_ms, layer="hub", episode=item.get("episode"),
                   text=item["text"], index=item.get("index"), queued_left=len(self._floor_queue),
                   released_total=self.floor_released_total,
                   note="当前这句已说完：插话作为下一轮发出（字不丢、顺序不变、随后真的被回答）")
        self._floor_replaying = True
        try:
            self.on_asr(now_ms, item["text"], True)      # 下一轮：照常判停 / 接不接 / 开闸 / 回答
        finally:
            self._floor_replaying = False

    # ---------- 冷场三选（期四）：安静到刻度、真的没人说话才问一次 ----------
    #
    # 起算点＝**中枢最后一句说完**（验收卡已拍板，不是用户最后一次出字）：`_hub_last_said_ms`
    # 就是那一句的时刻；应声 / 垫句 / 垫句抢回 / 回答（决策落地）都算「中枢说过一句」。
    # 冷场自己那一句**不重新起算**（否则同一段安静会被自己的接话重置，「约 10 秒之后才允许
    # 问一句」永远到不了）；人开口立刻清零（`_quiet_clear`），回到「等中枢下一句」。
    # 刻度：首问约 4 秒；约 10 秒之后才允许「问一句」；之后每 8 秒一拍；两次开口退避 6 秒。
    # 冷场开口**只接上文**（台词带「刚才那句」的短片段），记住（`hub_said`），但不进 Agent。

    def _quiet_tick_at(self, index: int) -> int:
        """第几个刻度的安静时长：4s / 10s / 18s / 26s …（之后每 8 秒一拍）。"""
        index = int(index)
        if index <= 0:
            return int(QUIET_FIRST_MS)
        if index == 1:
            return int(QUIET_CHECK_AFTER_MS)
        return int(QUIET_CHECK_AFTER_MS) + int(QUIET_STEP_MS) * (index - 1)

    def _quiet_note_hub_said(self, now_ms, text="", *, kind, anchor=True) -> None:
        """中枢刚说完一句：记下这一刻（冷场从这里起算）。`anchor=False` 只给冷场自己那一句用。"""
        self._hub_last_said_ms = int(now_ms)
        self._hub_last_text = text or ""
        if anchor:
            self._quiet_anchor_ms = int(now_ms)
            self._quiet_tick_next = 0

    def _quiet_clear(self, now_ms, why="human_speech") -> None:
        """有人开口：冷场计时清零（判断在飞的那一回也作废，迟到的结论不再出声）。"""
        if not self.cfg.quiet_enabled:
            return
        had = self._quiet_anchor_ms is not None or self._quiet_episode is not None
        self._quiet_anchor_ms = None
        self._quiet_episode = None
        self._quiet_tick_next = 0
        if had:
            self.counters["quiet_cleared"] += 1
            self._emit("quiet_cleared", now_ms, layer="hub", why=why,
                       note="有人开口：冷场计时清零（不顺延、不在有人说话时抢答）")

    def _quiet_context(self) -> str:
        """「刚才在聊的那件事」：最近一次被中枢回答的原话 → 中枢最近一句 → 这一轮的字。"""
        if self._quiet_topic:
            return self._quiet_topic
        if self.hub_said:
            return self.hub_said[-1].get("text") or ""
        return self.turn_text() or ""

    def _quiet_idle(self) -> bool:
        """真的没人说话：这一轮没有没消费的字、没有在飞的判停 / 工具 / 决策、也没在说垫句。"""
        if self.state != "LISTENING":
            return False
        if normalize(self.suffix_text()):
            return False
        if self._text_hold:
            return False
        job = self._jobs.get("llm")
        if job is not None and not job.get("canceled"):
            return False
        if self._llm_prepared is not None:
            return False
        if self._tools_pending is not None or self._tools_jobs:
            return False
        if self._eot_pending is not None or self._eot_jobs:
            return False
        if self._floor_episode is not None and not self._floor_episode["done"]:
            return False
        if self._floor_queue:
            return False
        if self.confirm_pending is not None:
            return False
        return True

    def _quiet_step(self, now_ms) -> None:
        """每个 tick 一次：先把在飞的结论落地；再看要不要在「这一拍」问一次。"""
        if self.gate is None or not self.cfg.quiet_enabled:
            return
        if getattr(self.gate, "quiet_fn", None) is None:
            return
        episode = self._quiet_episode
        if episode is not None and not episode["done"]:
            verdict = self.gate.quiet_verdict(episode["id"], now_ms)
            if verdict is None or verdict.get("state") == "pending":
                return
            self._quiet_apply(now_ms, episode, verdict)
            return
        anchor = self._quiet_anchor_ms
        if anchor is None or not self._quiet_idle():
            return
        quiet_ms = int(now_ms) - int(anchor)
        due = self._quiet_tick_at(self._quiet_tick_next)
        if quiet_ms < due:
            return
        # 同一时刻最多一句 / 出声后 1 秒内不再出声：这一拍先不开口（记一笔，等下一个刻度）。
        blocked, why = self._speech_blocked(now_ms)
        if blocked:
            self._quiet_skip(now_ms, due, quiet_ms, why)
            return
        # 两次开口之间要退避：还没到就等下一个刻度（不在这里硬等，也不重复问同一句）。
        if self._quiet_last_open_ms is not None \
                and int(now_ms) - int(self._quiet_last_open_ms) < int(QUIET_BACKOFF_MS):
            self._quiet_skip(now_ms, due, quiet_ms,
                             "离上一次冷场开口不到 %.1f 秒（两次开口之间要退避）"
                             % (QUIET_BACKOFF_MS / 1000.0))
            return
        # 这一拍问一次（一个刻度只问一次，不会连着问）。
        self._quiet_tick_next += 1
        self._quiet_seq += 1
        self._quiet_asks_total += 1
        self.counters["quiet_asks"] += 1
        episode = {"id": self._quiet_seq, "anchor_ms": int(anchor), "quiet_ms": quiet_ms,
                   "tick_ms": int(due), "asked_ms": int(now_ms), "done": False,
                   "outlet": None, "reason": None, "turn": self.turn_index,
                   "check_allowed": quiet_ms >= int(QUIET_CHECK_AFTER_MS)}
        self._quiet_episode = episode
        context = self._quiet_context()
        self._emit("quiet_asked", now_ms, layer="hub", episode=episode["id"],
                   quiet_ms=quiet_ms, anchor_ms=int(anchor), tick_ms=int(due),
                   check_allowed=episode["check_allowed"], topic=context,
                   turn=self.turn_index,
                   note="安静到刻度、且真的没人说话：问一次「要不要开口」"
                        "（三条出口只接上文；有人开口立刻清零）")
        self.gate.start_quiet(episode["id"], quiet_ms=quiet_ms, text=context,
                              hub_last=self._hub_last_text,
                              check_allowed=episode["check_allowed"],
                              scene=self._scene_now(), t_ms=int(now_ms))

    def _quiet_skip(self, now_ms, due, quiet_ms, reason) -> None:
        """这一拍不开口（退避 / 1 秒静默）：把这一拍算过去、等下一个刻度（不重复问同一拍）。"""
        self._quiet_tick_next += 1
        self.counters["quiet_blocked"] += 1
        self._emit("quiet_gate", now_ms, layer="hub", spoken=False, tick_ms=int(due),
                   quiet_ms=int(quiet_ms), reason=reason,
                   note="这一拍不出声；等下一个刻度（收口与开闸不受影响）")

    def _quiet_apply(self, now_ms, episode, verdict) -> None:
        """这一拍的结论回来：按 `decide_quiet` 的唯一判据走（再等不出声；另外两条才开口）。"""
        outlet, reason = decide_quiet(quiet=verdict, quiet_ms=episode.get("quiet_ms") or 0)
        episode["done"] = True
        episode["outlet"] = outlet
        episode["reason"] = reason
        episode["done_ms"] = int(now_ms)
        if verdict.get("state") == "timeout":
            self.counters["quiet_timeouts"] += 1
        if outlet == QUIET_WAIT:
            self.counters["quiet_wait"] += 1
            self._emit("quiet_gate", now_ms, layer="hub", episode=episode["id"], spoken=False,
                       outlet=outlet, label=QUIET_LABELS.get(outlet), reason=reason,
                       verdict_state=verdict.get("state"), quiet_ms=episode.get("quiet_ms"),
                       note="这一拍不出声（再等下一个刻度；收口与开闸不受影响）")
            self._quiet_log.append({"t_ms": int(now_ms), "episode": episode["id"],
                                    "outlet": outlet, "spoken": False, "reason": reason,
                                    "quiet_ms": episode.get("quiet_ms"),
                                    "verdict_state": verdict.get("state"),
                                    "source": verdict.get("source"), "model": verdict.get("model")})
            return
        self._quiet_speak(now_ms, episode, outlet, reason, verdict)

    def _quiet_speak(self, now_ms, episode, outlet, reason, verdict) -> None:
        """冷场开口：只接上文的一句（带「刚才那句」的短片段），记住但不进 Agent、不开闸。"""
        text = quiet_text(outlet, self._quiet_context(), index=self._quiet_spoken_total,
                          avoid=self._quiet_last_text)
        chars = len(normalize(text))
        blocked, why = self._speech_blocked(now_ms)
        if blocked:
            self.counters["quiet_blocked"] += 1
            self._emit("quiet_gate", now_ms, layer="hub", episode=episode["id"], spoken=False,
                       outlet=outlet, text=text, chars=chars, reason=why,
                       note="这一拍不出声（等下一个刻度）")
            return
        gap_ms = None if self._ack_last_ms is None else int(now_ms) - int(self._ack_last_ms)
        self._quiet_spoken_total += 1
        self.counters["quiet_speak"] += 1
        if outlet == QUIET_CHECK:
            self.counters["quiet_checks"] += 1
        else:
            self.counters["quiet_follows"] += 1
        kind = "quiet_check" if outlet == QUIET_CHECK else "quiet_follow"
        self._ack_last_ms = int(now_ms)          # 与应声 / 垫句共用「上次出声」锚点（1 秒内谁都不再出声）
        self._ack_last_text = text               # 回声丢弃也认这一句（本机麦收回来当回声丢掉）
        self._quiet_last_open_ms = int(now_ms)   # 两次开口之间退避，从这里算
        self._quiet_last_text = text             # 同一句不重复说
        entry = {"t_ms": int(now_ms), "episode": episode["id"], "outlet": outlet,
                 "label": QUIET_LABELS.get(outlet), "text": text, "chars": chars,
                 "version": self.version, "quiet_ms": episode.get("quiet_ms"),
                 "gap_ms": gap_ms, "turn": self.turn_index, "seat": self.seat_id, "kind": kind,
                 "reason": reason, "source": verdict.get("source"),
                 "model": verdict.get("model")}
        self._quiet_log.append(entry)
        # 「记住」：进本席对话上下文台账（跨轮留存）；冷场那一句不重新起算（见本节开头）。
        self.hub_said.append({"t_ms": int(now_ms), "text": text, "chars": chars, "kind": kind,
                              "turn": self.turn_index, "version": self.version,
                              "seat": self.seat_id,
                              "note": "冷场开口：只接上文，记住（进上下文），不进 Agent"})
        if len(self.hub_said) > 8:
            self.hub_said = self.hub_said[-8:]
        self._emit("quiet_spoken", now_ms, layer="hub", text=text, chars=chars,
                   outlet=outlet, label=QUIET_LABELS.get(outlet), episode=episode["id"],
                   quiet_ms=episode.get("quiet_ms"), gap_ms=gap_ms,
                   spoken_total=self._quiet_spoken_total, turn=self.turn_index,
                   source=verdict.get("source"), model=verdict.get("model"),
                   reason=reason, context=True, agent=False,
                   speech="台面出字 + 播报桩（本期不接真 TTS）",
                   note="冷场开口＝陪着说：只接上文，记住（进上下文），"
                        "但不进 Agent、不选工具、不执行、不开闸")

    # ---------- 旁听台账：留 3 句 / 开闸带进 Agent（wiki [[中枢并发闸门]] §13） ----------

    def _record_overhear(self, now_ms, text, verdict):
        """把「说完但不是对车说」的原话按席记进台账（最近 3 句、带标签、跨席可见）。

        只认真正「说完且不接」的结论：`state=只进上下文` 且接不接那一路成功、结论就是不接。
        超时 / 失败 / 版本不齐这些「没有结论」的情形一律不记（wiki §14：没有结论 ≠ 不接）。
        """
        if self.overhear is None:
            return None
        if verdict.get("state") != GATE_CONTEXT:
            return None
        accept = verdict.get("accept") or {}
        if accept.get("state") != "ok" or accept.get("accept") is not False:
            return None
        version = int(verdict.get("version") or 0)
        if version and version <= self._overhear_version:
            return None                      # 同一版字已经记过：不重复记账
        entry = self.overhear.record(self.seat_id or "D", text, now_ms)
        if entry is None:
            return None
        if version:
            self._overhear_version = version
        self._emit("overhear_recorded", now_ms, layer="hub", seat=entry["seat"],
                   seat_label=entry["seat_label"], text=entry["text"], label=entry["label"],
                   kept=entry.get("kept"), keep_per_seat=self.overhear.keep,
                   ledger=self.overhear.snapshot(),
                   note="不是对车说 → 按席留最近 %d 句，不复活成待执行请求" % self.overhear.keep)
        return entry

    def _open_agent_context(self, now_ms, verdict):
        """开闸那一刻：把「此刻场景 ＋ 已标记原话」两栏拼成**唯一一份**进 Agent 的上下文。

        只在开闸算数（不接那一轮不拼、没说完不拼）；整条链路上只有这一处拼（一个口子）。
        场景用的是**同一版字上取的那份快照**（`verdict["scene"]`），不再向读心要新的；
        场景没有就不出场景栏，也不为等场景挂住闸门。原话只作背景，不是新指令。
        """
        base = dict(self.context or {})
        scene = (verdict.get("scene") or {}).get("summary")
        ledger = self.overhear.snapshot() if self.overhear is not None else None
        lines = ledger.get("seats") if ledger is not None else None
        all_lines = []
        if lines:
            for seat in ("D", "P"):
                all_lines.extend(lines[seat]["lines"])
        self._agent_lines = list(all_lines)
        columns = overhear_ledger.render_columns(scene, all_lines)
        if columns:
            # 只放拼好的两栏（同一段文字不在上下文里出现第二遍）。
            base["overhear_context"] = columns
            base["overhear_note"] = overhear_ledger.COLUMNS_NOTE
            base["overhear_scene"] = (scene or "").strip() or None
        self._agent_context = base
        self._emit("agent_context", now_ms, layer="hub", columns=columns,
                   scene=(scene or "").strip() or None, overhear=ledger, one_entry=True,
                   note="只在开闸这一轮拼一次；第二栏是背景，不是新指令",
                   text_version=self.version)

    def _decide_context(self):
        """本席 decide 用的上下文：开闸后是那份拼好的两栏，其余时候仍是原来的基础 context。"""
        return self._agent_context if self._agent_context is not None else self.context

    # ---------- 记忆：收住那一刻写 / 发起请求那一刻查（wiki `四模块职责与接口契约` §1 §2 §4） ----------

    def _scene_now(self) -> dict | None:
        """**这一刻**场景流里已有的最新一份：不等待、不重算、取不到就 None。

        闸门判据那一帧仍冻结在锁字那一刻（`GateDecision.scene`）；这里另取一次，是给 Agent 用的
        那一份（wiki 接口 4：不冻结在开闸那一刻）。
        """
        source = None
        if self.gate is not None:
            source = getattr(self.gate, "scene_fn", None)
        if not callable(source):
            source = getattr(self.mind, "scene_snapshot", None)
        if not callable(source):
            return None
        try:
            return source() or None
        except Exception:      # noqa: BLE001 - 取不到场景就按「无场景」继续，绝不挂住请求
            return None

    def _memory_write_close(self, now_ms, why):
        """中枢把这句**收住**那一刻写一刀记忆：完整原话 + 这一刻对应的场景。**全链只有这一处写。**

        - 只进上下文的话也写：写完照旧走闸门，不接那一支不会绕过这里。
        - 话还没收住不写：`_confirm` 只在判停给「说完」/ 静音窗收口 / stop 时才被调用；
          「只有画面变化、没有新的收住的话」根本走不到这里（10 秒兜底未落地也不影响这条）。
        - 同一版字只写一刀：静音窗再收一次、闸门晚回来再判一次都不重复写。
        """
        if self.memory is None:
            return
        text = self.suffix_text()
        if not normalize(text):
            return
        if self.version in self._memory_versions:
            return
        self._memory_versions.add(self.version)
        scene = self._scene_now()
        if scene is None and self._gate_verdict:
            scene = self._gate_verdict.get("scene")
        turn = {"t_ms": now_ms, "seat": self.seat_id or "D", "text": text,
                "version": self.version, "route": "", "scene": scene, "gate": None}
        try:
            receipt = self.memory.write(turn) or {}
        except Exception as error:      # noqa: BLE001 - 记忆失败不许拖垮收住 / 开闸
            receipt = {"status": "error", "error": {"kind": type(error).__name__,
                                                    "detail": str(error)[:120]}}
        self._emit("memory_write", now_ms, why=why, status=receipt.get("status"),
                   reason=receipt.get("reason"), seat=receipt.get("seat"),
                   text=receipt.get("text"), scene=receipt.get("scene"),
                   latency_ms=receipt.get("latency_ms"), error=receipt.get("error"),
                   note="收住这一刻写：这句原话 + 这一刻的场景；记成什么由 memos 自己抽取")

    def _request_context(self, now_ms, utterance):
        """Agent **发起请求那一刻**的上下文：这一刻最新场景 + 已标记原话 + 刚查到的记忆。

        - 场景：这一刻最新一份（场景栏重拼；旁听原话仍是开闸那一刻拼好的那几行）。
        - 记忆：`memory.recall(query=本句原话)`，同步、有上限（`MEMOS_RECALL_TIMEOUT_MS`）；
          查不到 / 失败 / 没开记忆就不带记忆栏——记忆不许拖住这次请求。
        """
        scene = self._scene_now()
        summary = ((scene or {}).get("summary") or "").strip() or None
        base = dict(self._agent_context) if self._agent_context is not None else dict(self.context or {})
        if self._agent_context is not None:
            columns = overhear_ledger.render_columns(summary, self._agent_lines or [])
            if columns:
                base["overhear_context"] = columns
                base["overhear_scene"] = summary
            else:       # 这一刻连场景都没了：不留上一拍的场景栏（旁听原话也空了才走到这里）
                for key in ("overhear_context", "overhear_scene", "overhear_note"):
                    base.pop(key, None)
        memory_hits: list[str] = []
        receipt: dict = {}
        if self.memory is not None:
            try:
                memory_hits = self.memory.recall(now_ms, limit=MEMORY_RECALL_LIMIT,
                                                 query=utterance) or []
                receipt = getattr(self.memory, "last_recall", None) or {}
            except Exception as error:      # noqa: BLE001 - 查记忆失败不许拖住这次请求
                memory_hits = []
                receipt = {"status": "error",
                           "error": {"kind": type(error).__name__, "detail": str(error)[:120]}}
            if memory_hits:
                base["memory"] = list(memory_hits)
                base["memory_note"] = MEMORY_NOTE
            try:
                self.memory.remember_request({"t_ms": now_ms, "seat": self.seat_id or "D",
                                              "text": utterance, "scene": scene,
                                              "memory": memory_hits})
            except Exception:      # noqa: BLE001 - 对账记录失败不影响这次请求
                pass
            self._emit("memory_recall", now_ms, query=utterance, count=len(memory_hits),
                       status=receipt.get("status"), reason=receipt.get("reason"),
                       latency_ms=receipt.get("latency_ms"), error=receipt.get("error"),
                       items=[item[:120] for item in memory_hits],
                       note="Agent 发起请求这一刻按这句原话查一次（不是开闸那一刻）")
        self._emit("agent_request", now_ms, seat=self.seat_id, utterance=utterance,
                   scene=summary, scene_revision=(scene or {}).get("revision"),
                   scene_age_ms=(scene or {}).get("age_ms"),
                   memory_count=len(memory_hits),
                   memory_preview=[item[:80] for item in memory_hits[:3]],
                   memory_status=receipt.get("status"), memory_reason=receipt.get("reason"),
                   memory_latency_ms=receipt.get("latency_ms"), memory_error=receipt.get("error"),
                   speaker_seat=base.get("speaker_seat"), speaker=base.get("speaker_position"),
                   speaker_in_input=bool(base.get("speaker_position")),
                   text_version=self.version,
                   note="Agent 发起请求这一刻：场景取当时最新一份，记忆按这句原话查一次；"
                        "输入里带着「这句话是主驾还是副驾说的」，位置由工具从这份输入里取")
        return base

    def stop(self, now_ms):
        """Explicit stop: decide on whatever suffix is left, then close the turn."""
        if self.turn_done:
            return
        self._stop_requested = True
        self._emit("stop_requested", now_ms, text=self.suffix_text(), consumed_segments=self.consumed_segments)
        if normalize(self.suffix_text()):
            self._confirm(now_ms, "stop")
        self._maybe_finish_session(now_ms, force=True)

    def _maybe_finish_session(self, now_ms, force=False):
        """Close the whole turn once there is nothing left to consume and no decision is in flight."""
        if self.turn_done:
            return False
        if normalize(self.suffix_text()):
            return False
        if not force and not (self.consumed_count or self.context_consumed):
            # The user has not consumed anything yet: keep the turn open and wait for speech.
            return False
        job = self._jobs["llm"]
        if job is not None and not job["canceled"]:
            return False
        if self._llm_prepared is not None and self._llm_prepared["version"] == self.version:
            return False
        if self._floor_queue:
            # 期三：还有「说完再接 / 垫句抢回」的插话在等这一句说完——先不收口，
            # 收口与「作为下一轮发出」在同一处（`_floor_release_step`）办，插话不会丢。
            return False
        self._finish_turn(now_ms)
        return True

    def _finish_turn(self, now_ms):
        self.turn_done = True
        summary = self.summary()
        # 决策落地 → 回合收口的间隔：默认（close_turn_on_decision）应为 0，旧口径恒 ~max_silence_ms。
        summary["turn_tail_ms"] = self._delta(now_ms, self.t_decision_last)
        self.turns.append(summary)
        path = self._timeline_path()
        self._emit("turn_done", now_ms, **summary, timeline_path=str(path) if path else None)
        self._dump_timeline()

    def _timeline_path(self):
        return Path(self.evidence_root) / self.session_id / "timeline.json" if self.evidence_root else None

    def _reset_turn(self, now_ms):
        self.turn_index += 1
        self.turn_done = False
        self.segment = ""
        self.segments = []
        self.segment_closed = False
        self.last_change_ms = None
        self.last_speech_end_ms = None
        self.speech_start_ms = now_ms
        self.t_last_word = None
        self.t_confirmed = None
        self.t_llm_start = None
        self.t_decision = None
        self.t_decision_last = None
        self.consumed_version = 0
        self.consumed_segments = 0
        self.consumed_text = ""
        self.consumed_tail = None
        # 跨轮：新的这一轮只认「这段语音已消费前缀」后面的新字（不把整段重发当新话）。
        self._turn_prefix = self.utterance_consumed_text
        self.consumptions = []
        self.consumed_count = 0
        self.version_t_ms = None
        self.t_last_consumed = None
        self._stop_requested = False
        self.turn_start_ms = now_ms
        self.state = "LISTENING"
        self._jobs["llm"] = None
        self._tools_jobs = []
        self._eot_jobs = []
        self._eot_pending = None
        self._tools_pending = None
        self._tools_cache = None
        self._tools_result = None
        self._llm_prepared = None
        self.t_tools_ready = None
        self.t_llm_start_last = None
        self._shortlist_source = None
        self._shortlist_age_ms = None
        self.eot_scores = None
        self.decision = None
        self.confirm_pending = None
        self.confirm_resolved = None
        self._gate_open_version = 0
        self._gate_waiting_version = None
        self._gate_verdict = None
        self._agent_context = None       # 新一轮：开闸时重新拼（旁听台账本身跨轮留存）
        self._agent_lines = []           # 新一轮没有「开闸那一刻的台账行」，等开闸再拼
        # 应一声：新一轮重新计「这一轮两次至少隔 3 秒」，但「出声后 1 秒内不再出声」与回声保护跨轮保留。
        self._ack_tried = set()
        # 垫句：同一版只算一次；「已经说过的话」（hub_said）与上次出声时刻跨轮保留（记住、别连着说）。
        self._cushion_tried = set()
        # 期三：插话排队（`_floor_queue`）跨轮保留——它等的就是「这一句说完」，收口不能把它带走；
        # 这一回插话（episode）、冻结的那一版、待垫的那一句是「当前这句」的东西，随收口清掉。
        episode = self._floor_episode
        # 垫句那一小段挂在「中枢正在说」上（不挂本轮），收口别把它带走；其余照旧清掉。
        self._floor_episode = (episode if (episode is not None and not episode.get("done")
                                           and episode.get("window") == "speech") else None)
        self._floor_hold_version = None
        self._floor_pending_cushion = None
        self._floor_continued = None
        self.context_consumed = 0
        self.counters = {"eot_calls": 0, "eot_skipped": 0, "eot_stale": 0, "tool_calls": 0,
                         "llm_calls": 0, "llm_wasted": 0, "n_reissue": 0, "consumed": 0,
                         "turn_voids": 0, "acks": 0, "ack_suppressed": 0, "ack_echo_drops": 0,
                         "cushions": 0, "cushion_suppressed": 0,
                         "floor_asks": 0, "floor_yields": 0, "floor_finish_first": 0,
                         "floor_continues": 0, "floor_cushion_backs": 0, "floor_timeouts": 0,
                         "floor_queued": 0, "floor_released": 0, "floor_cushions": 0,
                         "floor_cushions_blocked": 0,
                         "quiet_asks": 0, "quiet_wait": 0, "quiet_follows": 0,
                         "quiet_checks": 0, "quiet_speak": 0, "quiet_blocked": 0,
                         "quiet_cleared": 0, "quiet_timeouts": 0}

    # ---------- events / evidence ----------

    def _emit(self, name, t_ms, **payload):
        event = {"event": name, "t_ms": t_ms, "text_version": self.version}
        if self.seat_id is not None:
            event["seat_id"] = self.seat_id
        if self.mic_id is not None:
            event["mic_id"] = self.mic_id
        event.update(payload)
        self.events.append(event)
        if self.on_event:
            self.on_event(event)

    @staticmethod
    def _delta(later, earlier):
        return None if later is None or earlier is None else later - earlier

    def _dump_timeline(self):
        if self.evidence_root is None:
            return
        folder = Path(self.evidence_root) / self.session_id
        folder.mkdir(parents=True, exist_ok=True)
        payload = {"session_id": self.session_id, "config": asdict(self.cfg),
                   "events": self.events, "turns": self.turns, "summary": self.summary()}
        (folder / "timeline.json").write_text(json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2) + "\n")
        self.timeline_path = folder / "timeline.json"
        return self.timeline_path

    def close(self):
        self._pool.shutdown(wait=False)