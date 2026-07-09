"""echo — 内置自检 / 示例模块。

启动后起一个后台任务，每 N 秒发一条假的 ``im.message``；
同时订阅 ``im.message``、``im.reply``、``system.ready`` 打日志。

把它当作 hub 框架的「Hello, World」：单独启用它就能看到 publish / subscribe /
spawn / startup / shutdown 全链路日志。
"""

from __future__ import annotations

import asyncio

from hub import Context, Module
from hub.topics import IM_MESSAGE, IM_REPLY, SYSTEM_READY
from message.bot import BotEvent, TextSegment

mod = Module("echo")


@mod.on_startup
async def setup(ctx: Context) -> None:
    ctx.state.tick = 0
    interval = float(ctx.config.get("interval", 5.0))
    ctx.state.interval = interval
    ctx.spawn(_ticker(ctx), name="echo:ticker")
    ctx.logger.info("echo module up (interval=%.1fs)", interval)


@mod.on_shutdown
async def teardown(ctx: Context) -> None:
    ctx.logger.info("echo module down (sent %d ticks)", ctx.state.tick)


@mod.on(SYSTEM_READY)
async def on_ready(ctx: Context, _payload) -> None:
    ctx.logger.info("system.ready received")


@mod.on(IM_MESSAGE)
async def on_message(ctx: Context, event: BotEvent) -> None:
    ctx.logger.info("got im.message: %s", _summarize(event))


@mod.on(IM_REPLY)
async def on_reply(ctx: Context, event: BotEvent) -> None:
    ctx.logger.info("got im.reply:   %s", _summarize(event))


async def _ticker(ctx: Context) -> None:
    """周期性发一条假消息到 im.message，验证总线在跑。"""
    try:
        while not ctx.hub_event.is_set():
            ctx.state.tick += 1
            tick_id = f"echo-{ctx.state.tick}"
            event = BotEvent(
                id=tick_id,
                platform="echo",
                time=0.0,
                type="message",
                detail_type="private",
                sub_type="",
                message_id=tick_id,
                message=[TextSegment(text=f"tick #{ctx.state.tick}")],
                bot_id="echo-bot",
                user_id="0",
                user_name="echo",
                session_id="echo:self",
                session_name="echo",
            )
            await ctx.publish(IM_MESSAGE, event)
            try:
                await asyncio.wait_for(ctx.hub_event.wait(), timeout=ctx.state.interval)
            except TimeoutError:
                pass
    except asyncio.CancelledError:
        ctx.logger.debug("ticker cancelled")
        raise


def _summarize(event: BotEvent) -> str:
    parts = []
    for seg in event.message:
        if getattr(seg, "type", None) == "Text":
            parts.append(getattr(seg, "text", ""))
        else:
            parts.append(f"[{seg.type}]")
    return f"{event.platform}/{event.session_id}: " + " ".join(parts)
