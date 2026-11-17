"""装配：把「会话里谁是谁」接起来，不承载任何判断规则。

原来这些接线写在 `ui/server.py`（页面进程里）。整理后拆到本文件，页面只 import 现成的接线函数：

    mind    一份会话一份读心（两席共用，见 `ui/server.py:_hub_mind`：它还要用页面的引擎缓存）
    gates   每席一份闸门，但两席取同一份读心场景（中枢仍按席判说完没完 / 接不接，规则不改）
    ledger  一本会话级旁听台账（两席共用、跨席可见）
    memory  一份会话一份记忆（两席写进同一份用户记忆空间；真写真查走 memos，没 key 就退化成空实现）
    seat_extra  每席把闸门 + 台账 + 读心 + 记忆挂进 TurnSession

接线口径与 wiki `四模块职责与接口契约` 一致；这里只做「把对象连起来」，
判停/开闸/读心/任务的规则分别留在 `gate/`、`mind/`、`agent/`。
"""
from __future__ import annotations

from typing import Any, Iterable

DUAL_SEATS: tuple[str, ...] = ("D", "P")


def hub_gates(mind, seats: Iterable[str] = DUAL_SEATS) -> dict[str, Any]:
    """每席一份闸门，但两席取**同一份**读心场景（中枢仍按席判说完没完 / 接不接，规则不改）。

    期一（2026-11-16）再加第三路：同一版字上并发「要不要应一声」（`hub_ack.build_ack_fn()`）。
    它只判「要不要出声」，出不出声、说哪句与三道闸（频次 / 字数 / 不要回答）在 `TurnSession` 里，
    不进 Agent、不开闸；`HUB_ACK=rule` 只是离线演示替身。

    期二（2026-11-16）再加两路：同一版字上并发「现在是什么节奏」（`hub_pace.build_pace_fn()`），
    以及**只有节奏＝积极且已连续说满 1.5 秒**才发一次的「要不要垫一句」
    （`hub_pace.build_cushion_fn()`）。节奏只用于问不问垫句（不改任何等待时长 / 开闸线）；
    垫句进上下文、不进 Agent；`HUB_PACE=rule` / `HUB_CUSHION=rule` 只是离线演示替身。

    期三（2026-11-16）再加一路「两人同时说四选」（`hub_floor.build_floor_fn()`）——它**不是每版都问**，
    只在「这边正在说 / 正在准备说，人又开口」的那一次插话上问一次；四条出口里「让路」＝改动前那条。
    `HUB_FLOOR=rule`（`HUB_FLOOR_RULE=yield|finish_first|continue|cushion_back`）只是离线演示替身。

    期四（2026-11-16）再加一路「冷场三选」（`hub_quiet.build_quiet_fn()`）——它**不是每版都问**，
    只在「安静到刻度、且真的没人说话」时按刻度问一次（首问约 4 秒；约 10 秒之后才允许问一句）；
    三条出口里只有「再等」不出声；开口只接上文、不进 Agent。
    `HUB_QUIET=rule`（`HUB_QUIET_RULE=wait|follow|check`）只是离线演示替身。
    """
    from gate.hub_accept import build_accept_fn
    from gate.hub_ack import build_ack_fn
    from gate.hub_floor import build_floor_fn
    from gate.hub_pace import build_cushion_fn, build_pace_fn
    from gate.hub_quiet import build_quiet_fn
    from gate.hub_gate import GateKeeper
    return {seat: GateKeeper(accept_fn=build_accept_fn(), scene_fn=mind.scene_snapshot, seat=seat,
                             ack_fn=build_ack_fn(), pace_fn=build_pace_fn(),
                             cushion_fn=build_cushion_fn(), floor_fn=build_floor_fn(),
                             quiet_fn=build_quiet_fn())
            for seat in seats}


def hub_ledger() -> Any:
    """一本会话级旁听台账：两席共用（跨席可见），「说完但不接」的原话按席留最近 3 句。

    见 `src/overhear_ledger.py` 与 wiki `[[中枢并发闸门]]` §13（方案 B）。
    """
    from overhear_ledger import OverhearLedger
    return OverhearLedger()


def hub_memory(session_id: str, user_id: str | None = None, **kwargs) -> Any:
    """一份会话一份记忆：两席写进同一份用户记忆空间，真写真查走 memos 云端。

    - 挂哪个用户空间：`POC_MEMORY_USER`（默认 `poc-eot-cabin`），跨会话可查；
    - 没有 `MEMOS_API_KEY`（进程环境 / `MEMOS_ENV_FILE` 指向的已有文件）→ 退化成空实现，
      主链照常跑（写查都明确回 `disabled`，不抛、不猜）。
    - 凭据只读就地取，不复制、不落盘、不打印（见 `memory/memos.py` 头部）。
    """
    from memory.memos import build_store
    return build_store(session_id=session_id, user_id=user_id, **kwargs)


def hub_seat_extra(gates: dict[str, Any], ledger=None, mind=None, memory=None):
    """每席拿到自己的闸门（场景由这份读心统一给），并挂上同一份旁听台账、读心与记忆。

    中枢把一句收住（判停给说完 / 静音窗收口）时，会调 `mind.on_utterance_close(席别)` 让它用
    「完整出字 + 当时画面」读一次（wiki `[[读心请求时机]]`）。单向通知，读心仍不等中枢。
    同一刻记忆写一刀（原话 + 这一刻的场景）；Agent 发起请求那一刻再按这句原话查一次。
    """
    return lambda seat: {"gate": gates[seat],
                         **({"overhear": ledger} if ledger is not None else {}),
                         **({"mind": mind} if mind is not None else {}),
                         **({"memory": memory} if memory is not None else {})}
