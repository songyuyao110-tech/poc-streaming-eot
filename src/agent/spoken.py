"""办完之后「对人说的那一句」：这才是这一轮对用户的回复（不是办理记录）。

口径（2026-11-17 用户拍板）：执行完成后必须有一句对用户说的话，上屏的是这句话；
`（已执行：某工具，发起 D，作用 D，合并：否）` 这类办理记录只进台账、**不上屏**。

句子怎么来（不猜、不空转）：

- **用户自己的动词**（更自然）：这句原话里说的是「打开 / 调高 / 调到 / 播放 …」，就用那个动作；
- **工具说明里的对象名**（更准）：工具 `description` 里那个中文对象（「座椅按摩功能（48U型号）」
  → `座椅按摩`），比 ASR 原话更不容易错字；
- **执行参数里的位置 / 值**：`area=主驾` → 位置；`temperature=24` → `24 度`。

拼出来就是「已打开主驾座椅按摩」「已把主驾空调温度调到 24 度」这种一句话。

`_MOCK_FACTS` 是 **PoC 假数据**：查询类工具（天气 / 车外环境）在这个 PoC 里没有真实数据源，
只有 mock 回执；接了真天气服务就把这里换成工具返回的原话。DETAIL 里照实标注。
"""
from __future__ import annotations

import re
import sys
from functools import lru_cache
from pathlib import Path

sys.dont_write_bytecode = True

POC_ROOT = Path(__file__).resolve().parents[2]
TOOL_FIXTURE = POC_ROOT / "fixtures/tools-core-score.yaml"

# PoC 假数据：查询类工具没有真实数据源（mock binding），这句就是「查到的那点情况」。
# 接真服务时这里整块换掉，句子改由工具返回拼。
_MOCK_FACTS = {
    "car_weather_info": "今天多云，气温 18 到 26 度。",
    "car_env_outdoorQuery": "车外 18 度，多云。",
}

# 原话里的动词 → 这一句的动作（长的先匹配，避免「打开」抢走「打开到」这类写法）
_UTTER_VERBS = (
    ("导航到", "导航"), ("导航去", "导航"), ("带我去", "导航"),
    ("打开", "打开"), ("开启", "打开"), ("开一下", "打开"), ("启用", "打开"),
    ("关闭", "关闭"), ("关掉", "关闭"), ("关上", "关闭"), ("合上", "关闭"),
    ("调到", "把"), ("调成", "把"), ("设成", "把"), ("设置成", "把"), ("设为", "把"),
    ("调高", "调高"), ("调大", "调高"), ("调低", "调低"), ("调小", "调低"),
    ("设置", "把"), ("调整", "把"), ("调节", "把"), ("切换", "把"),
    ("播放", "播放"), ("暂停", "暂停"), ("停止", "停止"), ("停下", "停止"),
    ("查询", "查询"), ("查一下", "查询"), ("看看", "查询"), ("看一下", "查询"),
    ("控制", "办好"),
)

_LEADS = {"打开": "已打开", "关闭": "已关闭", "调高": "已调高", "调低": "已调低",
          "播放": "已播放", "暂停": "已暂停", "停止": "已停止", "查询": "已查到"}

_TAIL_NOUN = ("功能", "状态", "详情", "信息", "设置项", "开关", "等级", "档位")
_PARENS = re.compile(r"（[^）]*）|\([^)]*\)")


@lru_cache(maxsize=1)
def tool_descriptions() -> dict:
    """工具名 → 一行中文说明（只读仓库里的工具表；读不到就空表，句子退回「这次操作」）。"""
    try:
        import yaml
        data = yaml.safe_load(TOOL_FIXTURE.read_text())
    except Exception:  # noqa: BLE001 - 只是取一个对象名，读不到不影响办完
        return {}
    rows = data if isinstance(data, list) else (data or {}).get("tools") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return {}
    return {str(item.get("name") or ""): str(item.get("description") or "")
            for item in rows if isinstance(item, dict)}


def _noun(tool: str, description: str = "") -> str:
    """工具说明里的对象名：「控制座椅按摩功能（48U型号）」→「座椅按摩」。"""
    head = (description or tool_descriptions().get(tool) or "").strip()
    head = head.split("，")[0].split(",")[0].split("。")[0].strip()
    head = _PARENS.sub("", head).strip()
    head = re.split(r"[和及与]", head)[0].strip()
    for verb, _ in _UTTER_VERBS:
        if head.startswith(verb):
            head = head[len(verb):].strip()
            break
    for tail in _TAIL_NOUN:
        if head.endswith(tail) and len(head) > len(tail):
            head = head[: -len(tail)].strip()
    return head


def _area(arguments: dict) -> str:
    value = str((arguments or {}).get("area") or "").strip()
    return value


def _value(arguments: dict) -> str:
    """参数里的那个「值」：温度 → `24 度`；档位 → `高档`。没有就空串。"""
    arguments = arguments or {}
    for key in ("temperature", "temp", "target_temperature"):
        if arguments.get(key) is not None:
            raw = arguments.get(key)
            try:
                number = float(raw)
                text = str(int(number)) if number == int(number) else str(number)
            except (TypeError, ValueError):
                text = str(raw)
            return text + " 度"
    level = str(arguments.get("level") or "").strip()
    if level and level not in ("关闭", "关"):
        return level + "档"
    return ""


def _act(utterance: str, description: str) -> str:
    """动作：先按用户原话里出现的动词，原话里没有就按工具说明的头一个动词。"""
    utterance = utterance or ""
    for verb, act in _UTTER_VERBS:
        if verb in utterance:
            return act
    head = (description or "").strip()
    for verb, act in _UTTER_VERBS:
        if head.startswith(verb):
            return act
    return "办好"


def spoken_line(utterance: str, tool: str, arguments: dict, *, description: str = "") -> str:
    """这一次办完之后对用户说的那一句（一句、不超过 30 字左右、不带工具名与参数名）。

    只由「原话的动词 + 工具说明的对象 + 执行参数的位置/值」拼，不编造没给的数据；
    查询类工具用 `_MOCK_FACTS` 里的 PoC 假数据（DETAIL 已标注）。
    """
    tool = str(tool or "").strip()
    fact = _MOCK_FACTS.get(tool)
    if fact:
        return fact
    desc = description or tool_descriptions().get(tool) or ""
    noun = _noun(tool, desc) or "这次操作"
    area = _area(arguments)
    subject = (area + noun) if area and not noun.startswith(area) else noun
    act = _act(utterance, desc)
    value = _value(arguments)
    if act == "导航":
        target = noun
        return "已开始导航到" + target + "。"
    if act == "把":
        if value:
            return "已把" + subject + "调到 " + value + "。"
        act = "办好"
    lead = _LEADS.get(act)
    if lead:
        return lead + subject + "。"
    return "已办好" + subject + "。"


__all__ = ["spoken_line", "tool_descriptions", "POC_ROOT", "TOOL_FIXTURE"]
