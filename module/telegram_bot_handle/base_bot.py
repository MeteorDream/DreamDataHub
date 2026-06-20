
from __future__ import annotations

import traceback
import json

from telegram import Update, Bot
from telegram.ext import Application, ContextTypes, CommandHandler, MessageHandler, filters

from hub import Context
from hub.topics import TELEGRAM_MESSAGE
from message.bot import BotEvent, TextSegment


class BaseTelegramHandle:
    """Telegram Bot 的 handle 基类实现"""

    def __init__(self, ctx: Context):
        self.enable = True
        self.ctx = ctx

        token = ctx.config.get("token", "")
        if not token:
            self.enable = False
            ctx.logger.error("telegram_bot: token 未配置")
            return

        self.app: Application = Application.builder().token(token).build()
        self.bot: Bot = self.app.bot

        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_message))
        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CommandHandler("help", self.help))
        self.app.add_error_handler(self.error_handler)    # type: ignore

    async def initalize(self) -> None:
        await self.app.initialize()
        await self.app.start()
        if self.app.updater is not None:
            await self.app.updater.start_polling()
        self.ctx.logger.info("telegram_bot: polling started")

    async def shutdown(self) -> None:
        if not self.enable:
            return
        try:
            if self.app.updater is not None:
                await self.app.updater.stop()
            await self.app.stop()
            await self.app.shutdown()
            self.ctx.logger.info("telegram_bot shutdown")
        except Exception:  # noqa: BLE001
            self.ctx.logger.exception("telegram_bot: shutdown error")

    async def start(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/start"""
        if not update.message:
            return 
        user = update.message.from_user
        user_name = "Dear"
        if user:
            user_name = user.full_name
        await update.message.reply_text(
            f"Hi {user_name}! My name is {self.bot.name}, welcome to use our sevice~"
        )

    async def help(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/help"""
        if not update.message:
            return 
        await update.message.reply_text(
            f"Sorry! The developers of the bot did not leave help message"
        )

    async def text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """消息投递到 TELEGRAM_MESSAGE"""
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
            message=[TextSegment(text=msg.text)],
            bot_id=str(self.bot.id),
            user_id=str(user.id) if user else "",
            user_name=(user.full_name if user else "") or "",
            session_id=f"tg:{chat_id}",
            session_name=update.effective_chat.title if update.effective_chat else "",
        )
        await self.ctx.publish(TELEGRAM_MESSAGE, event)
        self.ctx.logger.info("[Telegram] User: %s chat: %s Text message: %s", user.username, chat_id, msg.text)

    async def error_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Log the error and send a telegram message to notify the developer."""
        # Log the error before we do anything else, so we can see it even if something breaks.
        self.ctx.logger.error("Exception while handling an update:", exc_info=context.error)

        # traceback.format_exception returns the usual python message about an exception, but as a
        # list of strings rather than a single string, so we have to join them together.
        tb_string = ""
        if context.error:
            tb_list = traceback.format_exception(None, context.error, context.error.__traceback__)
            tb_string = "".join(tb_list)

        # Build the message with some markup and additional information about what happened.
        # You might need to add some logic to deal with messages longer than the 4096 character limit.
        update_str = update.to_dict() if isinstance(update, Update) else str(update)
        message = (
            "An exception was raised while handling an update\n"
            f"update = {json.dumps(update_str, indent=2, ensure_ascii=False)}"
            "\n\n"
            f"context.chat_data = {str(context.chat_data)}\n\n"
            f"context.user_data = {str(context.user_data)}\n\n"
            f"{tb_string}"
        )

        self.ctx.logger.error(message)

    async def send_message(self, event: BotEvent) -> None:
        if not self.enable:
            return
        if not event.session_id.startswith("tg:"):
            return
        chat_id = int(event.session_id.removeprefix("tg:"))
        text = "".join(seg.text for seg in event.message if seg.type == "Plain")  # type: ignore[attr-defined]
        if not text:
            return
        try:
            await self.bot.send_message(chat_id=chat_id, text=text)
        except Exception:  # noqa: BLE001
            self.ctx.logger.exception("telegram_bot: send_message failed")