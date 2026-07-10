"""DreamBot 的实现

消息链路为: 注册handel -> 接收消息并推送到指定的 topic
"""

from __future__ import annotations

import html
import json
import traceback

from telebot import util
from telegram import (
    Bot,
    BotCommand,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeDefault,
    ReplyKeyboardRemove,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from hub import Context
from message.bot import BotEvent, TextSegment
from message.db import BotMessageRecord
from topics.database import DatabaseWrite, DatabaseWritePayload
from topics.telegram import TelegramMessage
from module.weather import Weather

from .base_bot import BaseTelegramHandle


class DreamBotHandle(BaseTelegramHandle):
    """DreamBot 实现

    实现功能:
    1.
    """

    DEVELOPER_CHAT_ID = -1003847258572
    DEVELOPER_MESSAGE_THREAD_ID = 7

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

        self.app.add_handler(CommandHandler("start", self.start))
        self.app.add_handler(CommandHandler("help", self.help))
        self.app.add_handler(CommandHandler("location", self.get_location))
        self.app.add_handler(CommandHandler("weather", self.get_weather))
        self.app.add_handler(CommandHandler("cancel", self.cancel))
        self.app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, self.text_message))
        self.app.add_handler(MessageHandler(filters.LOCATION, self.location_message))
        self.app.add_error_handler(self.error_handler)  # type: ignore

    async def initalize(self) -> None:
        await self.app.initialize()
        await self.app.start()
        if self.app.updater is not None:
            await self.app.updater.start_polling()
        self.ctx.logger.info("telegram_bot: polling started")

        await self.set_command()

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
            "Sorry! The developers of the bot did not leave help message"
        )

    async def cancel(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            return
        await update.message.reply_text("Success cancel.", reply_markup=ReplyKeyboardRemove())

    async def set_command(self):
        commands = [
            BotCommand("start", "First enter and get hello message"),
            BotCommand("help", "Get bot help information"),
            BotCommand("location", "Get your last shared location"),
            BotCommand("weather", "Get weather information based on your last shared location"),
            BotCommand("cancel", "Cancel the current operation"),
        ]
        for scope_cls in (
            BotCommandScopeDefault,
            BotCommandScopeAllPrivateChats,
            BotCommandScopeAllGroupChats,
        ):
            scope_name = scope_cls.__name__
            result = await self.bot.set_my_commands(commands, scope=scope_cls())
            self.ctx.logger.info(
                "telegram: set %d commands for scope %s result: %s",
                len(commands),
                scope_name,
                result,
            )

    async def text_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """消息投递到 TelegramMessage"""
        msg = update.effective_message
        if msg is None or msg.text is None:
            return
        chat_id = update.effective_chat.id if update.effective_chat else 0
        thread_id = (
            update.message.message_thread_id
            if update.message and update.message.message_thread_id
            else ""
        )
        user = update.effective_user
        event = BotEvent(
            id=str(msg.message_id),
            platform="telegram",
            time=float(msg.date.timestamp()) if msg.date else 0.0,
            type="message",
            detail_type="group" if (chat_id < 0) else "private",
            sub_type=str(thread_id),
            message_id=str(msg.message_id),
            message=[TextSegment(text=msg.text)],
            bot_id=str(self.bot.id),
            user_id=str(user.id) if user else "",
            user_name=(user.full_name if user else "") or "",
            session_id=f"tg:{chat_id}",
            session_name=str(update.effective_chat.title) if update.effective_chat else "",
        )
        await self.ctx.publish(TelegramMessage, event)
        await self.ctx.publish(
            DatabaseWrite,
            DatabaseWritePayload(
                table=BotMessageRecord.__table__,
                row=BotMessageRecord.from_event(event).model_dump(),
            ),
        )
        self.ctx.logger.info(
            "[Telegram] User: %s chat: %s(%s) Text message: %s",
            user.full_name if user else "",
            chat_id,
            thread_id,
            msg.text,
        )

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
            f"<pre>update = {html.escape(json.dumps(update_str, indent=2, ensure_ascii=False))}"
            "</pre>\n\n"
            f"<pre>context.chat_data = {html.escape(str(context.chat_data))}</pre>\n\n"
            f"<pre>context.user_data = {html.escape(str(context.user_data))}</pre>\n\n"
            f"<pre>{html.escape(tb_string)}</pre>"
        )

        # Finally, send the message
        for message_segment in util.smart_split(message, 4000):
            await context.bot.send_message(
                chat_id=self.DEVELOPER_CHAT_ID,
                text=message_segment,
                parse_mode=ParseMode.HTML,
                message_thread_id=self.DEVELOPER_MESSAGE_THREAD_ID,
            )

    async def send_message(self, event: BotEvent) -> None:
        if not self.enable:
            return
        if not event.session_id.startswith("tg:"):
            return
        chat_id = int(event.session_id.removeprefix("tg:"))
        message_thread_id = int(event.sub_type) if event.sub_type else None
        text = "".join(seg.text for seg in event.message if seg.type == "Text")  # type: ignore[attr-defined]
        if not text:
            return
        try:
            await self.bot.send_message(
                chat_id=chat_id, text=text, message_thread_id=message_thread_id
            )
        except Exception:
            self.ctx.logger.exception("telegram_bot: send_message failed")

    async def location_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        msg = update.effective_message
        if msg is None or msg.location is None:
            self.ctx.logger.info("telegram_bot: location_message not message location, skip")
            return
        chat_id = update.effective_chat.id if update.effective_chat else 0
        thread_id = (
            update.message.message_thread_id
            if update.message and update.message.message_thread_id
            else ""
        )
        user = update.effective_user
        location = msg.location
        self.ctx.logger.info(
            "[Telegram] User: %s chat: %s(%s) location message: %s",
            user.full_name,
            chat_id,
            thread_id,
            (location.latitude, location.longitude),
        )
        context.user_data["location"] = (location.latitude, location.longitude)
        await update.message.reply_text(
            f"Update Location success, Latitude: {location.latitude}, Longitude: {location.longitude}"
        )
        msg, location_info = await Weather.amap_location(location.longitude, location.latitude)
        context.user_data["location_info"] = location_info
        if msg:
            await update.message.reply_text(f"Get address from location failed, message: {msg}")
        else:
            address = location_info.get("formatted_address", "未知")
            await update.message.reply_text(
                f"Get address from location success, Address: {address}"
            )

    async def get_location(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            self.ctx.logger.info("telegram_bot: get location don't have message, skip")
            return
        location = context.user_data.get("location")
        if not location:
            await update.message.reply_text(
                "You have not shared your location yet. Please share your location with me!"
            )
            return
        address = context.user_data.get("location_info", {}).get("formatted_address", "未知")
        await update.message.reply_text(
            f"Your current location as Latitude: {location[0]}, Longitude: {location[1]}, Address: {address}"
        )

    async def get_weather(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            self.ctx.logger.info("telegram_bot: get weather don't have message, skip")
            return

        location_info = context.user_data.get("location_info", {})

        if not location_info:
            await update.message.reply_text(
                "There is no location infomation in system, please send a location first."
            )
            return

        country = location_info.get("addressComponent", {}).get("country")
        city = location_info.get("addressComponent", {}).get("city")
        province = location_info.get("addressComponent", {}).get("province")
        district = location_info.get("addressComponent", {}).get("district")

        address = f"{country}{province}{city}{district}"

        message, weather_info = await Weather.amap_weather(
            location_info.get("addressComponent", {}).get("adcode")
        )
        if message:
            await update.message.reply_text(
                f"Get weather from location failed, address: {address}, message: {message}"
            )
            return

        weather_message = Weather.build_weather_message(
            weather_info, parse_mode="html", address=address
        )
        await update.message.reply_html(weather_message)
