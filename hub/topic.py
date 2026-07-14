"""Topic — 事件总线通道的 marker 基类。

一个 ``Topic`` 子类同时充当四件东西：

1. **注册键**：Hub 内部 ``dict[type[Topic], list[Subscriber]]`` 用 marker class
   本身做键，跨模块 import 冲突被 Python identity 立即发现
2. **类型锚点**：IDE 能沿 ``ctx.publish(IMMessage, ...)`` / ``@mod.on(IMMessage)``
   跳转到 marker 类，从而找到 ``Payload`` 的定义和文档
3. **人类友好命名**：``name`` 字段用于启动日志、错误消息（形如 ``"im.message"``）
4. **文档载体**：``description`` 描述该 topic 的语义（谁发、谁听、何时触发）

订阅与发布语义**不做框架级约束**：允许 0 个订阅者、N 个订阅者，publish
时如果无订阅者只记 DEBUG 日志。这跟 Capability（单一 provider）不同——
Topic 就是纯广播。

用法::

    class IMEventPayload(BaseModel):
        event: BotEvent

    class IMMessage(Topic):
        name = "im.message"
        description = "跨 IM 平台的入站消息广播"
        Payload = BotEvent    # 也可以直接用一个已有的 pydantic 模型

    # 发布方：
    await ctx.publish(IMMessage, event)   # event: BotEvent 会被 model_validate 归一化

    # 订阅方：
    @mod.on(IMMessage)
    async def on_msg(ctx: Context, event: BotEvent) -> None: ...
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

__all__ = ["Topic"]


class Topic:
    """事件总线通道契约基类。**不应实例化**；子类只用来做注册键和类型锚点。

    子类必须定义::

        name:        ClassVar[str]           人类可读的 topic 名（用于日志）
        Payload:     ClassVar[type[BaseModel]]   载荷 Pydantic 模型

    可选::

        description: ClassVar[str]           一句话描述（默认空串）

    Hub 在注册时会检查这些字段是否齐备且类型正确。
    """

    name: ClassVar[str]
    Payload: ClassVar[type[BaseModel]]
    description: ClassVar[str] = ""

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # 允许中间抽象基类不填字段；只要发布/订阅时用到就必须齐备。
        if "name" in cls.__dict__ and not isinstance(cls.__dict__["name"], str):
            raise TypeError(f"{cls.__name__}.name must be a str")
        if "description" in cls.__dict__ and not isinstance(cls.__dict__["description"], str):
            raise TypeError(f"{cls.__name__}.description must be a str")
        if "Payload" in cls.__dict__:
            val = cls.__dict__["Payload"]
            if not (isinstance(val, type) and issubclass(val, BaseModel)):
                raise TypeError(
                    f"{cls.__name__}.Payload must be a pydantic BaseModel subclass, "
                    f"got {val!r}"
                )

    def __init__(self) -> None:
        raise TypeError(
            f"{type(self).__name__} is a Topic marker class and should not be instantiated"
        )

