"""Hub 单元测试 — 路由、错误隔离、生命周期顺序。

只测纯框架行为，不依赖任何外部模块或 pytest-asyncio。
每个测试用例自己 ``asyncio.run`` 驱动 hub。
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from hub import Context, Hub, Module
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
