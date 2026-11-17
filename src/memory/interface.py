"""记忆接口位：Agent 与中枢各自需要的那两个动作（实现在 `memory/memos.py`）。

     store = memos.build_store(session_id=...)   # 真实现在 memos.py（memos 云端真写真查）
     store.write(turn)                           # 中枢收住那一刻：这句原话 + 这一刻的场景
     store.recall(t_ms, limit=5, query=本句)      # Agent 发起请求那一刻：当时能查到的记忆
     store.remember_request(request)             # 这一次请求带了什么（对账用）

本文件只声明形状与时机，不承载实现、也不做任何判断。时机口径见 wiki
`四模块职责与接口契约` §1 记忆行 / §2 接口 3 / §4 开闸分流：**只在话收住之后写**（只进上下文的话也写）；
**Agent 在发起请求那一刻查**（不是开闸那一刻、不是会话开始一次性预取）。
"""
from __future__ import annotations

from typing import Protocol

from shared.contracts import AgentRequest, DialogueTurn


class MemoryStore(Protocol):
    """记忆读写接口：写入按收住时刻，读取按请求时刻。"""

    def write(self, turn: DialogueTurn) -> dict:
        """写入一刀：`turn` 必须带「收住时刻 + 当时场景」；返回回执（status / latency / error）。"""
        ...

    def recall(self, t_ms: int | None = None, limit: int = 5, query: str = "") -> list[str]:
        """按请求时刻 + 这句原话取当时能查到的记忆（最新/最相关在前）；查不到就空列表。"""
        ...

    def remember_request(self, request: AgentRequest) -> dict:
        """Agent 接手时把「这一刻的场景 + 记忆」记成一次请求（用于后续对账）。"""
        ...

    def status(self) -> dict:
        """开没开、挂在哪个用户空间、成功/失败计数（不含密钥）。"""
        ...

    def close(self) -> None:
        """会话收口：不再写。"""
        ...
