"""文本归一：四模块共用同一套口径（判停/开闸/记忆对齐都按它比字）。

只做「去掉标点与空白」这一件事，不改字、不换词、不判语义。
"""
from __future__ import annotations

import re

# 去重口径与 turn_session 既有实现逐字一致（含中英文标点与成对引号/括号、破折号与省略号）。
PUNCT = re.compile(r"[\s，。！？、；：,.!?;:\"'“”‘’()（）\[\]【】\-—…~]+")


def normalize(text: str) -> str:
    return PUNCT.sub("", text or "")
