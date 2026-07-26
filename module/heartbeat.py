"""heartbeat — 内置心跳模块。

启动后起一个后台任务，每 N 秒发一条 ``SystemHeartbeat`` 消息；
同时订阅 ``SystemReady`` / ``SystemHeartbeat`` / ``SystemError`` 输出到日志。
"""

from __future__ import annotations

import asyncio
import time

from hub import Context, Module
from topics.system import (
    SystemError,
    SystemErrorPayload,
    SystemHeartbeat,
    SystemHeartbeatPayload,
    SystemReady,
    SystemReadyPayload,
)

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


@mod.on(SystemReady)
async def on_ready(ctx: Context, _payload: SystemReadyPayload) -> None:
    ctx.logger.info("SystemReady received")


@mod.on(SystemHeartbeat)
async def on_heartbeat(ctx: Context, payload: SystemHeartbeatPayload) -> None:
    ctx.logger.info(
        "SystemHeartbeat received: count=%d state=%s msg=%s",
        payload.count,
        payload.state,
        payload.message,
    )


@mod.on(SystemError)
async def on_error(ctx: Context, payload: SystemErrorPayload) -> None:
    ctx.logger.error(
        "SystemError received: module=%s topic=%s exc=%s",
        payload.module,
        payload.topic,
        payload.exc,
    )


async def _heartbeat(ctx: Context) -> None:
    """周期性发一条心跳消息到 SystemHeartbeat"""
    try:
        while not ctx.hub_event.is_set():
            ctx.state.counter += 1
            await ctx.publish(
                SystemHeartbeat,
                SystemHeartbeatPayload(
                    count=ctx.state.counter,
                    state="running",
                    message="heartbeat running...",
                    timestamp=time.time(),
                ),
            )
            try:
                await asyncio.wait_for(ctx.hub_event.wait(), timeout=ctx.state.interval)
            except TimeoutError:
                pass
    except asyncio.CancelledError:
        ctx.logger.debug("heartbeat cancelled")
        raise
