"""Module — 装饰器工厂，模块作者用它声明订阅与生命周期钩子。

每个 ``module/<name>.py`` 顶层创建**唯一一个** ``Module(name)`` 实例，
loader 会扫描该 .py 的 globals 找到它。

用法::

    from hub import Module, Context

    mod = Module("llm_openai")


    @mod.on_startup
    async def setup(ctx: Context): ...


    @mod.on("im.message")
    async def reply(event, ctx: Context): ...


    @mod.on_shutdown
    async def teardown(ctx: Context): ...
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from hub.context import Context

# 用户写的 handler 签名：async (ctx, payload) -> None
Handler = Callable[["Context", Any], Awaitable[None]]
# 生命周期钩子签名：async (ctx) -> None
Lifecycle = Callable[["Context"], Awaitable[None]]


class Module:
    """模块声明对象 — 收集 handler、钩子、能力声明。"""

    def __init__(self, name: str) -> None:
        if not name or not name.replace("_", "").isalnum():
            raise ValueError(f"Module name must be alnum/underscore: {name!r}")
        self.name = name
        self._handlers: dict[str, list[Handler]] = {}
        self._startup: list[Lifecycle] = []
        self._shutdown: list[Lifecycle] = []
        self._provides: dict[str, Handler] = {}
        # 由 loader 注入；运行时由 Hub 读取
        self._config: dict[str, Any] = {}

    # ---- 装饰器 ---------------------------------------------------------

    def on(self, topic: str) -> Callable[[Handler], Handler]:
        """订阅一个 topic。同一模块可对同一 topic 注册多个 handler。"""

        def decorator(fn: Handler) -> Handler:
            self._handlers.setdefault(topic, []).append(fn)
            return fn

        return decorator

    def provides(self, capability: str) -> Callable[[Handler], Handler]:
        """声明模块提供的能力，可被 workflow 或其他模块通过 ``invoke`` 调用。

        能力名使用点分命名，例如 ``weather.forecast``、``llm.chat``。
        同一能力名只能被一个模块声明（全局唯一）。

        用法::

            @mod.provides("weather.forecast")
            async def my_forecast(ctx: Context, params: dict) -> dict:
                ...
        """

        def decorator(fn: Handler) -> Handler:
            self._provides[capability] = fn
            return fn

        return decorator

    def on_startup(self, fn: Lifecycle) -> Lifecycle:
        """注册启动钩子。多个钩子按声明顺序顺序 await。"""
        self._startup.append(fn)
        return fn

    def on_shutdown(self, fn: Lifecycle) -> Lifecycle:
        """注册关停钩子。多个钩子按声明逆序 await（LIFO）。"""
        self._shutdown.append(fn)
        return fn

    # ---- loader / hub 内部使用 ------------------------------------------

    def bind_config(self, config: dict[str, Any]) -> None:
        self._config = dict(config)

    @property
    def capabilities(self) -> dict[str, Handler]:
        return dict(self._provides)

    @property
    def handlers(self) -> dict[str, list[Handler]]:
        return self._handlers

    @property
    def startup_hooks(self) -> list[Lifecycle]:
        return list(self._startup)

    @property
    def shutdown_hooks(self) -> list[Lifecycle]:
        return list(self._shutdown)

    @property
    def config(self) -> dict[str, Any]:
        return self._config

    def __repr__(self) -> str:
        topics = ", ".join(self._handlers) or "-"
        return f"Module({self.name!r}, topics=[{topics}])"
