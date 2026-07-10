"""Telegram 平台专属 Topic。"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

from hub.topic import Topic
from message.bot import BotEvent

__all__ = ["TelegramMessage", "TelegramReply"]


class TelegramMessage(Topic):
    """Telegram 平台专属入站。"""

    name: ClassVar[str] = "telegram.message"
    description: ClassVar[str] = "Telegram 入站；telegram_bot 双发之一"
    Payload: ClassVar[type[BaseModel]] = BotEvent


class TelegramReply(Topic):
    """Telegram 平台专属出站。"""

    name: ClassVar[str] = "telegram.reply"
    description: ClassVar[str] = "Telegram 出站，仅 telegram_bot 处理"
    Payload: ClassVar[type[BaseModel]] = BotEvent
