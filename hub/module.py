"""Module — 装饰器工厂，模块作者用它声明订阅、能力和生命周期钩子。

每个 ``module/<name>.py`` 顶层创建**唯一一个** ``Module(name)`` 实例，
loader 会扫描该 .py 的 globals 找到它。

用法::

    from hub import Capability, Module, Topic

    # 声明能力契约
    class MyParams(BaseModel): ...
    class MyResult(BaseModel): ...
    class MyService(Capability):
        name = "my.service"
        Params = MyParams
        Result = MyResult

    # 声明 topic
    class MyEvent(Topic):
        name = "my.event"
        Payload = MyPayload

    # 声明模块（如果依赖别的能力，用 requires 显式声明）
    mod = Module("my_module", requires=[SomeOtherService])


    @mod.on_startup
    async def setup(ctx): ...


    @mod.on(MyEvent)                       # ← 订阅 topic 用 Topic 类
    async def handle(ctx, payload): ...


    @mod.provides(MyService)
    async def do(ctx, params): ...


    @mod.on_shutdown
    async def teardown(ctx): ...
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from hub.capabilities import Capability
from hub.topic import Topic

if TYPE_CHECKING:
    from hub.context import Context

# 普通订阅 handler 签名：async (ctx, payload) -> None
Handler = Callable[["Context", Any], Awaitable[None]]
# Capability handler 签名：async (ctx, params: Params) -> Result
# 具体的 Params/Result 类型由 Capability 子类声明；这里只做宽松的顶层类型。
CapabilityHandler = Callable[["Context", Any], Awaitable[Any]]
# 生命周期钩子签名：async (ctx) -> None
Lifecycle = Callable[["Context"], Awaitable[None]]


class Module:
    """模块声明对象 — 收集 handler、钩子、能力声明、依赖声明。"""

    def __init__(
        self,
        name: str,
        *,
        requires: list[type[Capability]] | None = None,
    ) -> None:
        if not name or not name.replace("_", "").isalnum():
            raise ValueError(f"Module name must be alnum/underscore: {name!r}")
        self.name = name
        # 显式依赖：本模块启动前必须有别的（已启用）模块 provides 这些能力
        self.requires: list[type[Capability]] = list(requires or [])
        # topic marker class → 该 topic 下的 handler 列表
        self._handlers: dict[type[Topic], list[Handler]] = {}
        self._startup: list[Lifecycle] = []
        self._shutdown: list[Lifecycle] = []
        # capability marker class → handler fn
        self._provides: dict[type[Capability], CapabilityHandler] = {}
        # 由 loader 注入；运行时由 Hub 读取
        self._config: dict[str, Any] = {}

    # ---- 装饰器 ---------------------------------------------------------

    def on(self, topic: type[Topic]) -> Callable[[Handler], Handler]:
        """订阅一个 Topic。同一模块可对同一 Topic 注册多个 handler。

        用法::

            @mod.on(IMMessage)
            async def handle(ctx: Context, event: BotEvent) -> None: ...
        """
        if not (isinstance(topic, type) and issubclass(topic, Topic)):
            raise TypeError(f"on() requires a Topic subclass, got {topic!r}")
        for attr in ("name", "Payload"):
            if not hasattr(topic, attr):
                raise TypeError(
                    f"Topic {topic.__name__} missing required class var {attr!r}"
                )

        def decorator(fn: Handler) -> Handler:
            self._handlers.setdefault(topic, []).append(fn)
            return fn

        return decorator

    def provides(
        self, cap: type[Capability]
    ) -> Callable[[CapabilityHandler], CapabilityHandler]:
        """声明模块提供的能力（marker class），可被其他模块通过 ``ctx.invoke`` 调用。

        marker class 必须已定义 ``name`` / ``Params`` / ``Result`` 三个 ClassVar。
        同一 marker class 全局只能被一个模块 provides。

        用法::

            @mod.provides(WeatherForecastService)
            async def my_forecast(ctx: Context, params: WeatherForecastParams) -> WeatherForecastResult:
                ...
        """
        if not (isinstance(cap, type) and issubclass(cap, Capability)):
            raise TypeError(f"provides() requires a Capability subclass, got {cap!r}")
        # 校验 marker class 必需的三个字段都齐备
        for attr in ("name", "Params", "Result"):
            if not hasattr(cap, attr):
                raise TypeError(
                    f"Capability {cap.__name__} missing required class var {attr!r}"
                )
        if cap in self._provides:
            raise ValueError(
                f"Module {self.name!r} already provides capability {cap.__name__}"
            )

        def decorator(fn: CapabilityHandler) -> CapabilityHandler:
            self._provides[cap] = fn
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
    def capabilities(self) -> dict[type[Capability], CapabilityHandler]:
        return dict(self._provides)

    @property
    def handlers(self) -> dict[type[Topic], list[Handler]]:
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
        topics = ", ".join(t.__name__ for t in self._handlers) or "-"
        provides = ", ".join(cap.__name__ for cap in self._provides) or "-"
        requires = ", ".join(cap.__name__ for cap in self.requires) or "-"
        return (
            f"Module({self.name!r}, topics=[{topics}], "
            f"provides=[{provides}], requires=[{requires}])"
        )
