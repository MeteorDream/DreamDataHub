"""QQ 平台专属 Topic —— 需要仅针对 QQ 逻辑（如 QQ 群指令）时订阅这里。"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from hub.topic import Topic
from message.bot import BotEvent

__all__ = ["QQMessage", "QQReply"]


class QQMessage(Topic):
    """QQ 平台专属入站 —— 需要仅针对 QQ 逻辑（如 QQ 群指令）时订阅这里。"""

    name: ClassVar[str] = "qq.message"
    description: ClassVar[str] = "QQ 入站；napcat_bot 双发之一"
    Payload: ClassVar[type[BaseModel]] = BotEvent


class QQReply(Topic):
    """QQ 平台专属出站 —— 业务显式定向到 QQ 时发这里，napcat_bot 直接处理。"""

    name: ClassVar[str] = "qq.reply"
    description: ClassVar[str] = "QQ 出站，仅 napcat_bot 处理"
    Payload: ClassVar[type[BaseModel]] = BotEvent
