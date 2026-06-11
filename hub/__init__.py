"""hub — DataHub 轻量异步插件框架。

公开三个对象就够了：
- ``Hub``     总线 + 生命周期
- ``Module``  装饰器工厂（在每个模块文件顶层创建一个）
- ``Context`` 模块运行期门面（handler 第二参数）

辅助：
- ``load_modules`` 从 config 加载模块清单
- ``topics``       规范化 topic 字符串常量
"""

from hub.context import Context
from hub.core import Hub
from hub.loader import load_modules
from hub.module import Module

__all__ = ["Context", "Hub", "Module", "load_modules"]
