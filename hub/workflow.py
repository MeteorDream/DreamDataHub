"""Workflow — 工作流定义。

Workflow 是一个像 ``Module`` 一样的实体，它有一段编排函数（handler），
通过 ``WorkflowContext`` 调用其他模块的 ``provides`` 能力，实现多模块组合。

两种触发方式：
1. 订阅 topic — 当 topic 有消息时自动触发（被动）
2. 主动调用 — 模块通过 ``ctx.start_workflow()`` 主动触发，可获得返回值
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hub.workflow_context import WorkflowContext

# Workflow handler 签名：async (wf_ctx) -> Any
WorkflowHandler = Callable[["WorkflowContext"], Awaitable[Any]]


@dataclass
class Workflow:
    """工作流定义

    一个 Workflow 像一个 Module，但它是一段编排能力调用的序列。

    两种触发方式：
    - **topic 触发**：通过 ``subscribe`` 指定一个或多个 topic，消息到达时自动触发
    - **主动触发**：其他模块通过 ``ctx.start_workflow(name, params)`` 触发

    触发后：
    1. 创建 ``WorkflowContext``（携带触发数据和运行时环境）
    2. 执行 ``handler``（编排逻辑）
    3. handler 的返回值作为整个 workflow 的结果
    """

    name: str  # 工作流名称，全局唯一
    description: str = ""  # 描述
    subscribe: str | list[str] = ""  # 监听的 topic(s)，为空时仅支持主动触发
    handler: WorkflowHandler | None = None  # 编排处理函数
    timeout: float = 60.0  # 超时
    enabled: bool = True

    # loader 注入的配置
    _config: dict[str, Any] = field(default_factory=dict, repr=False)

    def bind_config(self, config: dict[str, Any]) -> None:
        self._config = dict(config)
        # 从配置覆盖字段
        if "timeout" in config:
            self.timeout = float(config["timeout"])
        if "description" in config:
            self.description = str(config["description"])
        if "subscribe" in config:
            raw = config["subscribe"]
            self.subscribe = raw if isinstance(raw, list) else str(raw)
        if "enabled" in config:
            self.enabled = bool(config["enabled"])
