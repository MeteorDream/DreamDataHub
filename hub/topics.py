"""规范化 topic 名称 — 模块之间约定通过这些字符串通信。

约定：扁平、点分、小写。命名空间：
- ``im.*``           跨 IM 平台的「广播」通道；任意 IM 模块入站时都会双发到这里，
                     LLM/store 等业务模块订阅这里即可拿到所有平台的输入。
                     业务模块向这里 publish reply 也会被所有 IM 模块收到——
                     每个 IM 模块自己看 ``event.session_id`` 前缀决定是否处理。
- ``qq.*`` / ``telegram.*`` 等  各 IM 平台的「专属」通道。需要只针对某一平台的逻辑订阅这里
                     （例如某 QQ 群专属指令、TG 专属命令）。IM 模块入站时除了双发
                     ``im.message`` 也会发自己的 ``<plat>.message``；业务模块若只想
                     回复某一平台就 publish ``<plat>.reply``。
- ``llm.*``          大模型交互相关。
- ``system.*``       框架自身事件。

只在真有订阅者时新增 topic；不要预先大量定义。
"""

from __future__ import annotations

# IM 跨平台广播 — payload 为 message.bot.BotEvent
IM_MESSAGE = "im.message"  # 入站广播：所有 IM 模块都会发，业务模块（LLM 等）订阅这里
IM_REPLY = "im.reply"  # 出站广播：业务模块 publish；所有 IM 模块按 session_id 决定是否承接

# IM 平台专属 topic — payload 同样是 BotEvent
QQ_MESSAGE = "qq.message"  # QQ 入站，仅 QQ 业务订阅
QQ_REPLY = "qq.reply"  # 仅由 im_qq 处理；业务可显式定向到 QQ
TELEGRAM_MESSAGE = "telegram.message"
TELEGRAM_REPLY = "telegram.reply"

# LLM 类 — payload 为 dict {"prompt", "response", "meta"}
LLM_EXCHANGE = "llm.exchange"

# 数据库类 — payload 为 dict {"table": str, "row": dict}；mysql 模块订阅
DATABASE_WRITE = "database.write"

# 框架类
SYSTEM_READY = "system.ready"  # payload: {} — 所有 on_startup 完成
SYSTEM_HEARTBEAT = "system.heartbeat"  # payload: {"count", "state", "message", "timestamp"} — 框架心跳
SYSTEM_ERROR = "system.error"  # payload: {"module", "topic", "exc"}


# 平台 → (message_topic, reply_topic) 的查找表，IM 模块用它做双发。
PLATFORM_TOPICS: dict[str, tuple[str, str]] = {
    "qq": (QQ_MESSAGE, QQ_REPLY),
    "telegram": (TELEGRAM_MESSAGE, TELEGRAM_REPLY),
}


__all__ = [
    "DATABASE_WRITE",
    "IM_MESSAGE",
    "IM_REPLY",
    "LLM_EXCHANGE",
    "PLATFORM_TOPICS",
    "QQ_MESSAGE",
    "QQ_REPLY",
    "SYSTEM_ERROR",
    "SYSTEM_HEARTBEAT",
    "SYSTEM_READY",
    "TELEGRAM_MESSAGE",
    "TELEGRAM_REPLY",
]
