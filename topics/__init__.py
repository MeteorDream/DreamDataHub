"""topics — 项目所有跨模块 Topic 契约的集中定义。

**约定**：任何 Topic 必然是跨模块通道（否则不该走 Hub）。所以本目录集中承载
所有 Topic 声明；模块文件里不再定义 Topic 类。

分文件规则：一个"领域"（IM 平台 / DB / LLM / 系统生命周期）一个文件。加新平台
就加一个新文件，不用改内核。

导入方式（推荐直接从子模块 import）::

    from topics.im import IMMessage, IMReply
    from topics.system import SystemReady, SystemReadyPayload

也可以走顶层 re-export（少数场景方便）::

    from topics import IMMessage, SystemReady
"""

from __future__ import annotations

from topics.database import DatabaseWrite, DatabaseWritePayload
from topics.im import PLATFORM_TOPICS, IMMessage, IMReply
from topics.llm import LLMExchange, LLMExchangePayload
from topics.qq import QQMessage, QQReply
from topics.system import (
    SystemError,
    SystemErrorPayload,
    SystemHeartbeat,
    SystemHeartbeatPayload,
    SystemReady,
    SystemReadyPayload,
)
from topics.telegram import TelegramMessage, TelegramReply

__all__ = [
    "PLATFORM_TOPICS",
    "DatabaseWrite",
    "DatabaseWritePayload",
    "IMMessage",
    "IMReply",
    "LLMExchange",
    "LLMExchangePayload",
    "QQMessage",
    "QQReply",
    "SystemError",
    "SystemErrorPayload",
    "SystemHeartbeat",
    "SystemHeartbeatPayload",
    "SystemReady",
    "SystemReadyPayload",
    "TelegramMessage",
    "TelegramReply",
]
