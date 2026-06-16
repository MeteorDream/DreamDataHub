"""heartbeat — 内置心跳模块。

启动后起一个后台任务，每 N 秒发一条系统 heartbeat 消息
同时订阅 ``system.ready`` `system.heartbeat` `system.error` 输出到日志。
"""

from __future__ import annotations

import asyncio
import time

from hub import Context, Module
from hub.topics import SYSTEM_READY, SYSTEM_HEARTBEAT, SYSTEM_ERROR

mod = Module("heartbeat")


@mod.on_startup
async def setup(ctx: Context) -> None:
    ctx.state.counter = 0
    interval = float(ctx.config.get("interval", 5.0))
    ctx.state.interval = interval
    ctx.spawn(_heartbeat(ctx), name="heartbeat:worker")
    ctx.logger.info("Heartbeat module setup with interval=%.1fs", interval)


@mod.on_shutdown
async def teardown(ctx: Context) -> None:
    ctx.logger.info("Heartbeat module teardown (system sent %d heartbeat)", ctx.state.counter)


@mod.on(SYSTEM_READY)
async def on_ready(_payload, ctx: Context) -> None:
    ctx.logger.info("system.ready received")

@mod.on(SYSTEM_HEARTBEAT)
async def on_heartbeat(_payload, ctx: Context) -> None:
    ctx.logger.info("system.heartbeat received: %s", _payload)

@mod.on(SYSTEM_ERROR)
async def on_error(_payload, ctx: Context) -> None:
    ctx.logger.error("system.error receive: %s", _payload)


async def _heartbeat(ctx: Context) -> None:
    """周期性发一条心跳消息到 system.heartbeat"""
    try:
        while not ctx.hub_event.is_set():
            ctx.state.counter += 1
            await ctx.publish(SYSTEM_HEARTBEAT, {
                "count": ctx.state.counter,
                "state": "running",
                "message": "heartbeat running...",
                "timestamp": time.time(),
            })
            try:
                await asyncio.wait_for(ctx.hub_event.wait(), timeout=ctx.state.interval)
            except asyncio.TimeoutError:
                pass
    except asyncio.CancelledError:
        ctx.logger.debug("heartbeat cancelled")
        raise
