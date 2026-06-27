"""WorkflowContext — 工作流运行期上下文。

Workflow handler 通过 ``WorkflowContext`` 与引擎及其它模块交互：
- ``invoke()`` 调用其他模块的 provides 能力
- ``publish()`` 发布事件到总线
- ``spawn()`` 启动后台任务
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any


@dataclass
class WorkflowContext:
    """工作流运行期上下文 — handler 唯一的运行环境。

    所有必填字段在前，带默认值的字段在后。

    :param workflow_name: 当前 workflow 名称
    :param trace_id: 全链路追踪 ID（UUID hex）
    :param origin_topic: 触发来源 topic（主动触发时为 ``"__manual__"``）
    :param origin_payload: 原始的触发载荷
    :param _hub: Hub 引用（内部使用）
    :param data: handler 自由读写的流程数据
    :param state: 运行时临时状态（SimpleNamespace）
    """

    workflow_name: str
    trace_id: str
    origin_topic: str
    origin_payload: Any
    _hub: Any = field(repr=False)

    data: Any = None
    state: SimpleNamespace = field(default_factory=SimpleNamespace)

    def __post_init__(self) -> None:
        self._logger = logging.getLogger(f"workflow.{self.workflow_name}")

    @property
    def logger(self) -> logging.Logger:
        return self._logger

    async def invoke(self, capability: str, params: Any = None) -> Any:
        """调用其他模块提供的能力。

        参数:
            capability: 能力名称，例如 ``"weather.forecast"``、``"llm.chat"``
            params: 传递给能力 handler 的参数

        返回:
            能力 handler 的返回值

        异常:
            CapabilityNotFoundError: 能力不存在
        """
        return await self._hub.invoke_capability(
            capability, params, trace_id=self.trace_id
        )

    async def publish(self, topic: str, payload: Any) -> None:
        """发布事件到 Hub 总线。"""
        await self._hub.publish(topic, payload)

    def spawn(
        self, coro: Any, *, name: str | None = None
    ) -> asyncio.Task[Any]:
        """在 Hub 中注册一个后台任务。"""
        return self._hub.spawn(coro, name=name or f"wf:{self.workflow_name}")
