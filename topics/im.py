"""跨 IM 平台的广播通道 —— 所有 IM 模块入站时双发到这里，业务模块订阅这里。

``PLATFORM_TOPICS`` 是"平台名 → (message_topic, reply_topic)"的分发表，IM 模块
和业务模块可以按平台名查表拿到对应 topic 对。加新平台：在这里增加一个条目 +
建 ``topics/<platform>.py``。
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from hub.topic import Topic
from message.bot import BotEvent
from topics.qq import QQMessage, QQReply
from topics.telegram import TelegramMessage, TelegramReply

__all__ = [
    "PLATFORM_TOPICS",
    "IMMessage",
    "IMReply",
]


class IMMessage(Topic):
    """跨 IM 平台入站广播 —— 所有 IM 模块入站都发这里，业务模块订阅这里。"""

    name: ClassVar[str] = "im.message"
    description: ClassVar[str] = "跨平台入站消息，任意 IM 模块入站时都会发"
    Payload: ClassVar[type[BaseModel]] = BotEvent


class IMReply(Topic):
    """跨 IM 平台出站广播 —— 业务模块 publish，所有 IM 模块按 session_id 前缀承接。"""

    name: ClassVar[str] = "im.reply"
    description: ClassVar[str] = "跨平台出站消息，IM 模块按 session_id 前缀决定是否处理"
    Payload: ClassVar[type[BaseModel]] = BotEvent


# 平台 → (message_topic, reply_topic) 分发表 —— IM 模块用于双发/定向出站
PLATFORM_TOPICS: dict[str, tuple[type[Topic], type[Topic]]] = {
    "qq": (QQMessage, QQReply),
    "telegram": (TelegramMessage, TelegramReply),
}
