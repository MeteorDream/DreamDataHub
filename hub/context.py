"""Context — 每个模块在运行期看到的 facade。

模块作者通过 ``ctx`` 与 Hub 交互：发布事件、起后台任务、读自己的配置、写
自己的状态。Context 是一个**轻量门面**，不持有业务数据；模块状态请放
``ctx.state``（``SimpleNamespace``）。
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, Coroutine

if TYPE_CHECKING:
    from hub.core import Hub


class Context:
    """暴露给模块的运行时门面。一个模块对应一个 Context 实例。"""

    __slots__ = ("_hub", "name", "config", "logger", "state", "hub_event")

    def __init__(
        self,
        hub: Hub,
        name: str,
        config: dict[str, Any],
        hub_event: asyncio.Event,
    ) -> None:
        self._hub = hub
        self.name = name
        self.config: dict[str, Any] = config
        self.logger = logging.getLogger(f"module.{name}")
        self.state = SimpleNamespace()
        self.hub_event = hub_event  # shutdown 时被置位

    async def publish(self, topic: str, payload: Any) -> None:
        """投递一条事件到总线。立即返回（fire-and-forget）。"""
        await self._hub.publish(topic, payload)

    def spawn(self, coro: Coroutine[Any, Any, Any], *, name: str | None = None) -> asyncio.Task[Any]:
        """注册一个长任务到 Hub 的 TaskGroup。

        返回 Task 便于模块自己持有/取消，但**不需要** await — Hub 会在 shutdown
        时统一取消。
        """
        return self._hub.spawn(coro, name=name or f"{self.name}:task")
