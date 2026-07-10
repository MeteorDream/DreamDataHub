"""hub 生命周期相关的 Topic —— 由 Hub 内部发布，任何模块都可以订阅。"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field

from hub.topic import Topic

__all__ = [
    "SystemError",
    "SystemErrorPayload",
    "SystemHeartbeat",
    "SystemHeartbeatPayload",
    "SystemReady",
    "SystemReadyPayload",
]


# ---------------------------------------------------------------------------
# Payloads
# ---------------------------------------------------------------------------


class SystemReadyPayload(BaseModel):
    """``SystemReady`` 载荷 —— 目前是空对象，预留字段以后可加。"""


class SystemHeartbeatPayload(BaseModel):
    """``SystemHeartbeat`` 载荷 —— 心跳节拍信息。"""

    count: int = Field(description="累计心跳次数，从 1 开始")
    state: str = Field(default="alive", description="模块自报状态")
    message: str = Field(default="", description="附加描述文本")
    timestamp: float = Field(description="Unix 时间戳（秒）")


class SystemErrorPayload(BaseModel):
    """``SystemError`` 载荷 —— 由 Hub 在 handler 抛异常时自动 publish。"""

    module: str = Field(description="出错的模块名")
    topic: str = Field(description="handler 订阅的 topic 名（Topic.name）")
    exc: str = Field(description="异常的 repr")


# ---------------------------------------------------------------------------
# Topics
# ---------------------------------------------------------------------------


class SystemReady(Topic):
    """所有模块的 on_startup 钩子跑完后，Hub 广播一次此 topic。"""

    name: ClassVar[str] = "system.ready"
    description: ClassVar[str] = "所有模块启动完成后由 Hub 广播一次"
    Payload: ClassVar[type[BaseModel]] = SystemReadyPayload


class SystemHeartbeat(Topic):
    """框架心跳 —— 由 heartbeat 模块按 interval 定时发布。"""

    name: ClassVar[str] = "system.heartbeat"
    description: ClassVar[str] = "由 heartbeat 模块周期性广播"
    Payload: ClassVar[type[BaseModel]] = SystemHeartbeatPayload


class SystemError(Topic):
    """topic handler 抛异常时由 Hub 自动 publish，方便集中监控/上报。"""

    name: ClassVar[str] = "system.error"
    description: ClassVar[str] = "handler 抛异常时由 Hub 自动广播"
    Payload: ClassVar[type[BaseModel]] = SystemErrorPayload
