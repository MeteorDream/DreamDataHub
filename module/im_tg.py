"""im_tg — Telegram Bot 模块（python-telegram-bot 22.x）。

订阅 ``im.reply``，把消息发回到对应 chat；
入站消息转成 ``BotEvent`` 发到 ``im.message``。

完整功能（命令、过滤器、重启动）见仓库根目录 ``tg_bot_example.py``，
本文件给出最小骨架。
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from hub import Context, Module
from hub.topics import IM_MESSAGE, IM_REPLY
from message.bot import BotEvent, PlainSegment

if TYPE_CHECKING:
    from telegram import Update
    from telegram.ext import Application, ContextTypes

mod = Module("im_tg")


@mod.on_startup
async def setup(ctx: Context) -> None:
    try:
        from telegram.ext import Application, MessageHandler, filters  # noqa: PLC0415
    except ImportError:
        ctx.logger.error("im_tg: python-telegram-bot 未安装")
        ctx.state.app = None
        return

    token = ctx.config.get("token", "")
    if not token:
        ctx.logger.error("im_tg: token 未配置")
        ctx.state.app = None
        return

    app: Application = Application.builder().token(token).build()
    ctx.state.app = app
    ctx.state.publish = ctx.publish  # 让 handler 闭包访问

    async def on_message(update: Update, _tg_ctx: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if msg is None or msg.text is None:
            return
        chat_id = update.effective_chat.id if update.effective_chat else 0
        user = update.effective_user
        event = BotEvent(
            id=str(msg.message_id),
            platform="telegram",
            time=float(msg.date.timestamp()) if msg.date else 0.0,
            type="message",
            detail_type="group" if (chat_id < 0) else "private",
            sub_type="",
            message_id=str(msg.message_id),
            message=[PlainSegment(text=msg.text)],
            bot_id=str(app.bot.id) if app.bot.id else "",
            user_id=str(user.id) if user else "",
            user_name=(user.full_name if user else "") or "",
            session_id=f"tg:{chat_id}",
            session_name=update.effective_chat.title if update.effective_chat else "",
        )
        await ctx.publish(IM_MESSAGE, event)

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_message))

    await app.initialize()
    await app.start()
    if app.updater is not None:
        await app.updater.start_polling()
    ctx.logger.info("im_tg: polling started")


@mod.on_shutdown
async def teardown(ctx: Context) -> None:
    app: Any = ctx.state.app
    if app is None:
        return
    try:
        if app.updater is not None:
            await app.updater.stop()
        await app.stop()
        await app.shutdown()
    except Exception:  # noqa: BLE001
        ctx.logger.exception("im_tg: shutdown error")


@mod.on(IM_REPLY)
async def on_reply(event: BotEvent, ctx: Context) -> None:
    app: Any = ctx.state.app
    if app is None or not event.session_id.startswith("tg:"):
        return
    chat_id = int(event.session_id.removeprefix("tg:"))
    text = "".join(seg.text for seg in event.message if seg.type == "Plain")  # type: ignore[attr-defined]
    if not text:
        return
    try:
        await app.bot.send_message(chat_id=chat_id, text=text)
    except Exception:  # noqa: BLE001
        ctx.logger.exception("im_tg: send_message failed")
