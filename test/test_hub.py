"""Hub 单元测试 — 路由、错误隔离、生命周期顺序。

只测纯框架行为，不依赖任何外部模块或 pytest-asyncio。
每个测试用例自己 ``asyncio.run`` 驱动 hub。
"""

from __future__ import annotations

import asyncio
from typing import Any, ClassVar

import pytest
from pydantic import BaseModel, ValidationError

from hub import Capability, Context, Hub, Module
from hub.core import CapabilityNotFoundError
from hub.loader import _resolve_dependencies
from hub.topics import SYSTEM_ERROR, SYSTEM_READY


async def _run_until(hub: Hub, predicate, timeout: float = 2.0) -> None:
    """跑 hub 直到 predicate() 返回 True，然后停。"""
    runner = asyncio.create_task(hub.run())
    try:
        deadline = asyncio.get_running_loop().time() + timeout
        while not predicate():
            if asyncio.get_running_loop().time() > deadline:
                raise TimeoutError(f"predicate not met within {timeout}s")
            await asyncio.sleep(0.01)
    finally:
        hub.stop()
        await runner


def test_publish_routes_to_subscriber() -> None:
    sent: list[Any] = []
    a = Module("publisher")
    b = Module("subscriber")

    @a.on_startup
    async def kick(ctx: Context) -> None:
        await ctx.publish("greet", "hello")

    @b.on("greet")
    async def recv(_ctx: Context, payload: Any) -> None:
        sent.append(payload)

    async def go() -> None:
        hub = Hub()
        hub.register(a)
        hub.register(b)
        await _run_until(hub, lambda: sent == ["hello"])

    asyncio.run(go())
    assert sent == ["hello"]


def test_handler_exception_does_not_kill_hub() -> None:
    errors: list[dict] = []
    survived: list[str] = []

    bad = Module("bad")
    good = Module("good")
    sink = Module("sink")

    @bad.on("topic")
    async def crash(_ctx: Context, _payload: Any) -> None:
        raise RuntimeError("boom")

    @good.on("topic")
    async def ok(_ctx: Context, _payload: Any) -> None:
        survived.append("ok")

    @sink.on(SYSTEM_ERROR)
    async def on_err(_ctx: Context, payload: dict) -> None:
        errors.append(payload)

    @bad.on_startup
    async def kick(ctx: Context) -> None:
        await ctx.publish("topic", None)

    async def go() -> None:
        hub = Hub()
        hub.register(bad)
        hub.register(good)
        hub.register(sink)
        await _run_until(hub, lambda: bool(survived) and bool(errors))

    asyncio.run(go())
    assert survived == ["ok"], "good handler must run despite bad's exception"
    assert errors and errors[0]["module"] == "bad"
    assert "boom" in errors[0]["exc"]


def test_lifecycle_order_lifo_shutdown() -> None:
    log: list[str] = []
    a = Module("aaa")
    b = Module("bbb")

    @a.on_startup
    async def a_up(_ctx: Context) -> None:
        log.append("a:up")

    @b.on_startup
    async def b_up(_ctx: Context) -> None:
        log.append("b:up")

    @a.on_shutdown
    async def a_down(_ctx: Context) -> None:
        log.append("a:down")

    @b.on_shutdown
    async def b_down(_ctx: Context) -> None:
        log.append("b:down")

    async def go() -> None:
        hub = Hub()
        hub.register(a)
        hub.register(b)
        await _run_until(hub, lambda: log == ["a:up", "b:up"])

    asyncio.run(go())
    assert log == ["a:up", "b:up", "b:down", "a:down"]


def test_system_ready_fires_after_all_startups() -> None:
    seen: list[str] = []
    a = Module("xa")
    b = Module("xb")

    @a.on_startup
    async def a_up(_ctx: Context) -> None:
        seen.append("a:up")

    @b.on(SYSTEM_READY)
    async def on_ready(_ctx: Context, _payload: Any) -> None:
        seen.append("b:ready")

    async def go() -> None:
        hub = Hub()
        hub.register(a)
        hub.register(b)
        await _run_until(hub, lambda: "b:ready" in seen)

    asyncio.run(go())
    assert seen.index("a:up") < seen.index("b:ready")


def test_publish_to_unknown_topic_is_quiet() -> None:
    a = Module("solo")

    @a.on_startup
    async def kick(ctx: Context) -> None:
        await ctx.publish("nobody.listening", "oops")

    async def go() -> None:
        hub = Hub()
        hub.register(a)
        await _run_until(hub, lambda: True)

    asyncio.run(go())  # 不抛即 OK


def test_duplicate_module_registration_rejected() -> None:
    hub = Hub()
    hub.register(Module("dup"))
    with pytest.raises(ValueError, match="duplicate"):
        hub.register(Module("dup"))


# ---------------------------------------------------------------------------
# Capability / invoke / requires / topological sort
# ---------------------------------------------------------------------------


class _EchoParams(BaseModel):
    text: str


class _EchoResult(BaseModel):
    echoed: str


class _EchoService(Capability):
    name: ClassVar[str] = "test.echo"
    Params: ClassVar[type[BaseModel]] = _EchoParams
    Result: ClassVar[type[BaseModel]] = _EchoResult


def test_ctx_invoke_calls_capability() -> None:
    """provider 模块 @provides，consumer 模块通过 ctx.invoke 调用。"""
    result_holder: list[_EchoResult] = []

    provider = Module("provider")
    consumer = Module("consumer", requires=[_EchoService])

    @provider.provides(_EchoService)
    async def echo(_ctx: Context, params: _EchoParams) -> _EchoResult:
        return _EchoResult(echoed=params.text.upper())

    @consumer.on_startup
    async def kick(ctx: Context) -> None:
        r = await ctx.invoke(_EchoService, _EchoParams(text="hi"))
        assert isinstance(r, _EchoResult)
        result_holder.append(r)

    async def go() -> None:
        hub = Hub()
        hub.register(provider)
        hub.register(consumer)
        await _run_until(hub, lambda: bool(result_holder))

    asyncio.run(go())
    assert result_holder[0].echoed == "HI"


def test_invoke_missing_capability_raises() -> None:
    """未注册的 capability 抛 CapabilityNotFoundError。"""
    m = Module("solo")

    @m.on_startup
    async def kick(ctx: Context) -> None:
        # invoke 里能力未注册 → 应抛 CapabilityNotFoundError
        try:
            await ctx.invoke(_EchoService, _EchoParams(text="x"))
        except CapabilityNotFoundError:
            pass
        else:
            raise AssertionError("expected CapabilityNotFoundError")

    async def go() -> None:
        hub = Hub()
        hub.register(m)
        await _run_until(hub, lambda: True)

    asyncio.run(go())


def test_invoke_validates_params() -> None:
    """入参不符合 schema 抛 ValidationError。"""
    provider = Module("prov")
    checker = Module("checker", requires=[_EchoService])
    raised: list[bool] = []

    @provider.provides(_EchoService)
    async def echo(_ctx: Context, params: _EchoParams) -> _EchoResult:
        return _EchoResult(echoed=params.text)

    @checker.on_startup
    async def kick(ctx: Context) -> None:
        # 传 dict 里缺 text 字段 → Pydantic 校验失败
        try:
            await ctx.invoke(_EchoService, {"wrong_key": 1})
        except ValidationError:
            raised.append(True)

    async def go() -> None:
        hub = Hub()
        hub.register(provider)
        hub.register(checker)
        await _run_until(hub, lambda: raised == [True])

    asyncio.run(go())
    assert raised == [True]


def test_duplicate_provides_rejected() -> None:
    """两个模块都 provides 同一 Capability → loader 拒绝。"""
    m1 = Module("m1")
    m2 = Module("m2")

    @m1.provides(_EchoService)
    async def _a(_ctx: Context, _p: _EchoParams) -> _EchoResult:
        return _EchoResult(echoed="")

    @m2.provides(_EchoService)
    async def _b(_ctx: Context, _p: _EchoParams) -> _EchoResult:
        return _EchoResult(echoed="")

    with pytest.raises(RuntimeError, match="provided by both"):
        _resolve_dependencies([m1, m2])


def test_missing_requires_rejected() -> None:
    """模块 requires 一个能力但没模块 provides → loader 拒绝。"""
    m = Module("hungry", requires=[_EchoService])

    with pytest.raises(RuntimeError, match="requires capability"):
        _resolve_dependencies([m])


def test_topological_sort_orders_provider_first() -> None:
    """provider 模块被拓扑排序到 requirer 之前。"""
    consumer = Module("consumer", requires=[_EchoService])
    provider = Module("provider")

    @provider.provides(_EchoService)
    async def _echo(_ctx: Context, _p: _EchoParams) -> _EchoResult:
        return _EchoResult(echoed="")

    # 故意把 consumer 放前面，测试排序会颠倒它们
    ordered = _resolve_dependencies([consumer, provider])
    names = [m.name for m in ordered]
    assert names == ["provider", "consumer"]


def test_cyclic_dependency_detected() -> None:
    """A -> B -> A 循环依赖 → loader 拒绝。"""

    class _AParams(BaseModel):
        pass

    class _AResult(BaseModel):
        pass

    class _CapA(Capability):
        name: ClassVar[str] = "test.a"
        Params: ClassVar[type[BaseModel]] = _AParams
        Result: ClassVar[type[BaseModel]] = _AResult

    class _CapB(Capability):
        name: ClassVar[str] = "test.b"
        Params: ClassVar[type[BaseModel]] = _AParams
        Result: ClassVar[type[BaseModel]] = _AResult

    ma = Module("ma", requires=[_CapB])
    mb = Module("mb", requires=[_CapA])

    @ma.provides(_CapA)
    async def _a(_ctx: Context, _p: _AParams) -> _AResult:
        return _AResult()

    @mb.provides(_CapB)
    async def _b(_ctx: Context, _p: _AParams) -> _AResult:
        return _AResult()

    with pytest.raises(RuntimeError, match="cyclic"):
        _resolve_dependencies([ma, mb])

