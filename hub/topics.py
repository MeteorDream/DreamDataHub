"""规范化 topic 名称 — 模块之间约定通过这些字符串通信。

约定：扁平、点分、小写。命名空间：
- ``im.*``     IM 平台（QQ/TG/...）相关
- ``llm.*``    大模型交互相关
- ``system.*`` 框架自身事件

只在真有订阅者时新增 topic；不要预先大量定义。
"""

from __future__ import annotations

# IM 类 — payload 为 message.bot.BotEvent
IM_MESSAGE = "im.message"  # 入站：IM 模块 → 任意订阅者
IM_REPLY = "im.reply"  # 出站：业务模块 → IM 模块

# LLM 类 — payload 为 dict {"prompt", "response", "meta"}
LLM_EXCHANGE = "llm.exchange"

# 框架类
SYSTEM_READY = "system.ready"  # payload: {} — 所有 on_startup 完成
SYSTEM_HEARTBEAT = "system.heartbeat"  # payload: {} — 框架心跳
SYSTEM_ERROR = "system.error"  # payload: {"module", "topic", "exc"}

__all__ = [
    "IM_MESSAGE",
    "IM_REPLY",
    "LLM_EXCHANGE",
    "SYSTEM_ERROR",
    "SYSTEM_READY",
]
