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
from message.db import BotMessageRecord, UserRecord
from module.mysql import UserQueryParams, UserQueryService
from module.weather import (
    WeatherForecastParams,
    WeatherForecastService,
    WeatherLocationParams,
    WeatherLocationService,
)
from services.weather.base import LocationData
from services.weather.formatter import build_weather_message
from topics.database import DatabaseWrite, DatabaseWritePayload
from topics.telegram import TelegramMessage

from .base_bot import BaseTelegramHandle

# 平台名常量 —— (platform, user_id) 联合唯一标识 user 表的一行
_PLATFORM = "telegram"


class DreamBotHandle(BaseTelegramHandle):
    """DreamBot 实现

    实现功能:
    1.
    """

    DEVELOPER_USER_ID = 7730322591
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
        # 引用消息 ID —— Telegram 的 reply_to_message 对应我们的 quote_id
        quote_id = (
            str(msg.reply_to_message.message_id) if msg.reply_to_message else None
        )
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
            quote_id=quote_id,
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

        if not user or user.id != self.DEVELOPER_USER_ID:
            return

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
        if not user:
            self.ctx.logger.info("telegram_bot: location_message no user, skip")
            return
        location = msg.location
        self.ctx.logger.info(
            "[Telegram] User: %s chat: %s(%s) location message: %s",
            user.full_name,
            chat_id,
            thread_id,
            (location.latitude, location.longitude),
        )
        if not update.message:
            self.ctx.logger.info("telegram_bot: location_message no message, skip")
            return
        await update.message.reply_text(
            f"Update Location success, Latitude: {location.latitude}, Longitude: {location.longitude}"
        )
        try:
            location_info = await self.ctx.invoke(
                WeatherLocationService,
                WeatherLocationParams(
                    longitude=location.longitude, latitude=location.latitude
                ),
            )
        except RuntimeError as exc:
            await update.message.reply_text(f"Get address from location failed: {exc}")
            return
        address = location_info.formatted_address or "未知"
        await update.message.reply_text(
            f"Get address from location success, Address: {address}"
        )

        # 落库：user 表按 (platform, user_id) upsert，location 信息落到 meta 字段
        meta = {
            "location": {
                "latitude": location.latitude,
                "longitude": location.longitude,
            },
            # 存精简的 LocationData（不含 raw，避免 meta 过大）
            "location_info": location_info.model_dump(exclude={"raw"}),
        }
        await self.ctx.publish(
            DatabaseWrite,
            DatabaseWritePayload(
                table=UserRecord.__table__,
                row={
                    "platform": _PLATFORM,
                    "user_id": str(user.id),
                    "user_name": user.full_name or "",
                    "meta": meta,
                },
                upsert=True,
            ),
        )

    async def _load_user_location(
        self, user_id: str
    ) -> tuple[tuple[float, float] | None, LocationData | None]:
        """从 user 表读取用户上次分享的 location + 逆地理编码。

        返回 ``(coords, location_info)``——都可能为 None（用户未曾分享 / 数据缺失）。
        除了 DB 是数据源之外，其他调用侧逻辑不变。
        """
        result = await self.ctx.invoke(
            UserQueryService,
            UserQueryParams(platform=_PLATFORM, user_id=user_id),
        )
        if not result.found or result.user is None:
            return None, None
        meta = result.user.meta or {}
        loc = meta.get("location") or {}
        coords: tuple[float, float] | None = None
        if "latitude" in loc and "longitude" in loc:
            coords = (float(loc["latitude"]), float(loc["longitude"]))
        info_dict = meta.get("location_info")
        location_info = LocationData.model_validate(info_dict) if info_dict else None
        return coords, location_info

    async def get_location(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            self.ctx.logger.info("telegram_bot: get location don't have message, skip")
            return
        user = update.effective_user
        if user is None:
            await update.message.reply_text("Cannot identify user.")
            return
        try:
            coords, location_info = await self._load_user_location(str(user.id))
        except Exception as exc:
            # 网络断 / DB 异常 / Pydantic 校验失败等都在这里兜底，避免 traceback 冒到用户
            self.ctx.logger.exception("get_location: load user location failed")
            await update.message.reply_text(f"Get location failed: {exc}")
            return
        if coords is None:
            await update.message.reply_text(
                "You have not shared your location yet. Please share your location with me!"
            )
            return
        address_str = location_info.formatted_address if location_info else "未知"
        await update.message.reply_text(
            f"Your current location as Latitude: {coords[0]}, Longitude: {coords[1]}, Address: {address_str}"
        )

    async def get_weather(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        if not update.message:
            self.ctx.logger.info("telegram_bot: get weather don't have message, skip")
            return

        user = update.effective_user
        if user is None:
            await update.message.reply_text("Cannot identify user.")
            return
        try:
            _coords, location_info = await self._load_user_location(str(user.id))
        except Exception as exc:
            # 网络断 / DB 异常 / Pydantic 校验失败等都在这里兜底，避免 traceback 冒到用户
            self.ctx.logger.exception("get_weather: load user location failed")
            await update.message.reply_text(f"Get weather failed: {exc}")
            return

        if not location_info:
            await update.message.reply_text(
                "There is no location infomation in system, please send a location first."
            )
            return

        # location_info 是 LocationData 实例
        address = "".join(
            [
                location_info.country,
                location_info.province,
                location_info.city,
                location_info.district,
            ]
        )
        adcode = location_info.adcode
        if not adcode:
            await update.message.reply_text(
                f"Get weather failed: no adcode for address {address}"
            )
            return

        try:
            forecast = await self.ctx.invoke(
                WeatherForecastService,
                WeatherForecastParams(adcode=adcode),
            )
        except RuntimeError as exc:
            await update.message.reply_text(
                f"Get weather from location failed, address: {address}, message: {exc}"
            )
            return

        weather_message = build_weather_message(
            forecast, parse_mode="html", address=address
        )
        await update.message.reply_html(weather_message)
