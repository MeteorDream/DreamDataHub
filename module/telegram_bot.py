"""telegram_bot — Telegram Bot 模块（python-telegram-bot 22.x）。

订阅 ``TELEGRAM_REPLY``，把消息发回到对应 chat；
入站消息转成 ``BotEvent`` 发到 ``TELEGRAM_MESSAGE``。

**依赖**：``/location`` / ``/weather`` 命令需要 weather 模块的两个 capability，
``/location`` 命令的读路径依赖 mysql 模块的 ``UserQueryService`` 从 user 表查
上次分享的位置信息。所以本模块 ``requires`` 这三个 capability。
如果不需要这些命令，未来可以把 weather / user 相关的 handler 拆到独立模块。
"""

from __future__ import annotations

from hub import Context, Module
from message.bot import BotEvent
from module.mysql import UserQueryService
from module.weather import WeatherForecastService, WeatherLocationService
from topics.telegram import TelegramReply

from .telegram_bot_handle.dream_bot import DreamBotHandle

mod = Module(
    "telegram_bot",
    requires=[WeatherLocationService, WeatherForecastService, UserQueryService],
)

DEVELOPER_CHAT_ID = -5579430112


@mod.on_startup
async def setup(ctx: Context) -> None:

    bot = DreamBotHandle(ctx)
    ctx.state.bot = bot
    if not bot.enable:
        return

    await bot.initalize()


@mod.on_shutdown
async def teardown(ctx: Context) -> None:
    await ctx.state.bot.shutdown()


@mod.on(TelegramReply)
async def on_reply_direct(ctx: Context, event: BotEvent) -> None:
    """业务方显式定向到 Telegram 时使用。"""
    await ctx.state.bot.send_message(event)
