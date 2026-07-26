"""Capability — 能力契约的 marker 基类。

一个 ``Capability`` 子类同时充当三件东西：

1. **注册键**：Hub 内部 ``dict[type[Capability], ...]`` 用 marker class 本身做键，
   跨模块 import 冲突会被 Python identity 立即发现（比字符串键更安全）
2. **类型锚点**：IDE 能沿 ``ctx.invoke(LLMChatService, ...)`` 跳转到 marker 类，
   从而找到 ``Params`` / ``Result`` 的定义
3. **人类友好命名**：``name`` 字段用于日志、错误消息、序列化场景

用法::

    class LLMChatParams(BaseModel):
        messages: list[dict[str, str]]


    class LLMChatResult(BaseModel):
        reply: str


    class LLMChatService(Capability):
        name = "llm.chat"
        Params = LLMChatParams
        Result = LLMChatResult


    # 提供方：
    @mod.provides(LLMChatService)
    async def chat(ctx: Context, params: LLMChatParams) -> LLMChatResult: ...


    # 调用方：
    result: LLMChatResult = await ctx.invoke(LLMChatService, LLMChatParams(...))
"""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel

__all__ = ["Capability"]


class Capability:
    """能力契约基类。**不应实例化**；子类只用来做注册键和类型锚点。

    子类必须定义::

        name:   ClassVar[str]           人类可读的能力名
        Params: ClassVar[type[BaseModel]]   入参 Pydantic 模型
        Result: ClassVar[type[BaseModel]]   返回值 Pydantic 模型

    Hub 在注册时会检查这三个字段是否齐备且类型正确。
    """

    name: ClassVar[str]
    Params: ClassVar[type[BaseModel]]
    Result: ClassVar[type[BaseModel]]

    def __init_subclass__(cls, **kwargs: object) -> None:
        super().__init_subclass__(**kwargs)
        # 允许中间抽象基类不填字段；只要 provides/invoke 用到时齐备即可。
        # 但如果字段已经赋值，就必须合法。
        if "name" in cls.__dict__ and not isinstance(cls.__dict__["name"], str):
            raise TypeError(f"{cls.__name__}.name must be a str")
        for attr in ("Params", "Result"):
            if attr in cls.__dict__:
                val = cls.__dict__[attr]
                if not (isinstance(val, type) and issubclass(val, BaseModel)):
                    raise TypeError(
                        f"{cls.__name__}.{attr} must be a pydantic BaseModel subclass, got {val!r}"
                    )

    def __init__(self) -> None:
        raise TypeError(
            f"{type(self).__name__} is a Capability marker class and should not be instantiated"
        )
