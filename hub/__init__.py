"""hub — DataHub 轻量异步插件框架。

公开对象：
- ``Hub``          总线 + 生命周期 + 能力注册
- ``Module``       装饰器工厂（在每个模块文件顶层创建一个）
- ``Context``      模块运行期门面（handler 第二参数）
- ``Capability``   能力契约 marker 基类（RPC 语义，唯一实现）
- ``Topic``        事件通道 marker 基类（广播语义，多订阅者）

辅助：
- ``load_modules``     从 config 加载模块清单（含依赖拓扑排序）
- ``system_topics``    Hub 内部维护的系统 topic (SystemReady/Heartbeat/Error)
"""

from hub.capabilities import Capability
from hub.context import Context
from hub.core import Hub
from hub.loader import load_modules
from hub.module import Module
from hub.topic import Topic

__all__ = [
    "Capability",
    "Context",
    "Hub",
    "Module",
    "Topic",
    "load_modules",
]
