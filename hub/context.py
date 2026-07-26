"""Context — 每个模块在运行期看到的 facade。

模块作者通过 ``ctx`` 与 Hub 交互：发布事件、调其他模块能力、起后台任务、读自己
的配置、写自己的状态。Context 是一个**轻量门面**，不持有业务数据；模块状态请放
``ctx.state``（``SimpleNamespace``）。
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Coroutine
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

if TYPE_CHECKING:
    from hub.capabilities import Capability
    from hub.core import Hub
    from hub.topic import Topic


class Context:
    """暴露给模块的运行时门面。一个模块对应一个 Context 实例。"""

    __slots__ = ("_hub", "config", "hub_event", "logger", "name", "state")

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

    async def publish(self, topic: type[Topic], payload: BaseModel | dict[str, Any]) -> None:
        """投递事件到 topic。立即返回（fire-and-forget）。

        参数:
            topic: Topic marker 类（例如 ``IMMessage``）
            payload: ``topic.Payload`` 实例，或任何能被
                     ``topic.Payload.model_validate`` 接受的数据（dict 也可）

        载荷会由 Hub 做一次 ``model_validate`` 归一化，契约违反立即抛
        ``pydantic.ValidationError``（不会静默广播错误数据给订阅者）。
        """
        await self._hub.publish(topic, payload)

    async def invoke(self, cap: type[Capability], params: BaseModel | dict[str, Any]) -> BaseModel:
        """调用另一个模块提供的能力。

        参数:
            cap: Capability marker 类（例如 ``LLMChatService``）
            params: 该 marker 的 ``Params`` 实例，或任何能被 ``Params.model_validate``
                    接受的数据（例如 dict）。Hub 会做一次归一化。

        返回:
            该 marker 的 ``Result`` 类型实例（由 Hub 校验后再返回）

        异常:
            CapabilityNotFoundError: 能力未被任何已启用模块 provides
            ValidationError: 入参或返回值不匹配 marker 声明的 schema
        """
        return await self._hub.invoke_capability(cap, params)

    def spawn(
        self, coro: Coroutine[Any, Any, Any], *, name: str | None = None
    ) -> asyncio.Task[Any]:
        """注册一个长任务到 Hub 的 TaskGroup。

        返回 Task 便于模块自己持有/取消，但**不需要** await — Hub 会在 shutdown
        时统一取消。
        """
        return self._hub.spawn(coro, name=name or f"{self.name}:task")
