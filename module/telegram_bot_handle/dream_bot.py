"""DreamBot 的实现

消息链路为: 注册handel -> 接收消息并推送到指定的 topic
"""

from __future__ import annotations

import asyncio
import html
import json
import re
import traceback

from deep_translator import GoogleTranslator
from telebot import util
from telegram import (
    Bot,
    BotCommand,
    BotCommandScopeAllChatAdministrators,
    BotCommandScopeAllGroupChats,
    BotCommandScopeAllPrivateChats,
    BotCommandScopeDefault,
    InputMediaPhoto,
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
from services.weibo import (
    Weibo,
    WeiboApiError,
    WeiboAuthRequiredError,
    WeiboSessionExpiredError,
)
from topics.database import DatabaseWrite, DatabaseWritePayload
from topics.telegram import TelegramMessage

from .base_bot import BaseTelegramHandle

# 平台名常量 —— (platform, user_id) 联合唯一标识 user 表的一行
_PLATFORM = "telegram"

# /weibo login QR 扫码轮询参数
_WEIBO_QR_POLL_INTERVAL = 2.0
_WEIBO_QR_TIMEOUT = 180.0  # 秒；passport 二维码大约 3 分钟过期，留一点余量以内的上限

# /weibo hot 输出条目数（对齐 data.jsonl 里的 top5 示例）
_WEIBO_HOT_TOP_N = 10

# 单条微博最多回复的图片数 —— Telegram media group 硬上限就是 10
_WEIBO_MAX_PHOTOS = 10


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
        self.app.add_handler(CommandHandler("weibo", self.weibo_command))
        self.app.add_handler(CommandHandler("translate", self.translate_command))
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
        help_text = (
            "🤖 <b>DreamBot Commands</b>\n\n"
            "<b>/start</b> — First enter and get hello message\n"
            "<b>/help</b> — Show this help message\n"
            "<b>/location</b> — Get your last shared location\n"
            "<b>/weather</b> — Get weather forecast based on your last shared location\n"
            "<b>/weibo</b> — Weibo commands:\n"
            "  • <code>/weibo login</code> — Bind Weibo account by QR code\n"
            "  • <code>/weibo hot</code> — Show Weibo hot search list\n"
            "  • <code>/weibo &lt;url&gt;</code> — Fetch a Weibo post\n"
            "<b>/translate</b> — Translate text between English and Chinese (auto detect)\n"
            "<b>/cancel</b> — Cancel the current operation\n\n"
            "💡 Send me a <b>location</b> to save it for weather queries.\n"
            "💬 Send any <b>text message</b> for AI-powered reply."
        )
        await update.message.reply_html(help_text)

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
            BotCommand("weibo", "Weibo commands (e.g. /weibo login to bind account by QR)"),
            BotCommand("translate", "Translate text between English and Chinese, auto detect language"),
            BotCommand("cancel", "Cancel the current operation"),
        ]
        for scope_cls in (
            BotCommandScopeDefault,
            BotCommandScopeAllPrivateChats,
            BotCommandScopeAllGroupChats,
            BotCommandScopeAllChatAdministrators,
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
        quote_id = str(msg.reply_to_message.message_id) if msg.reply_to_message else None
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
                WeatherLocationParams(longitude=location.longitude, latitude=location.latitude),
            )
        except RuntimeError as exc:
            await update.message.reply_text(f"Get address from location failed: {exc}")
            return
        address = location_info.formatted_address or "未知"
        await update.message.reply_text(f"Get address from location success, Address: {address}")

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
            await update.message.reply_text(f"Get weather failed: no adcode for address {address}")
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

        weather_message = build_weather_message(forecast, parse_mode="html", address=address)
        await update.message.reply_html(weather_message)

    async def weibo_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/weibo <subcommand | url>

        子命令：
        - ``login``: 申请二维码 → 发给用户扫码 → 轮询扫码状态直至完成或超时 →
          cookies 落到 user 表的 ``meta.weibo_cookies``
        - ``hot``: 拉取微博热搜榜（``band_list``）—— 公开接口，不需要登录
        - ``<weibo url>``: 抓取微博原文 + 图片，视频 / 投票 / 卡片等其他富媒体一律忽略
        """
        if not update.message:
            return
        args = context.args or []
        sub = args[0].lower() if args else ""
        if sub == "login":
            await self._weibo_login(update, context)
            return
        if sub == "hot":
            await self._weibo_hot(update, context)
            return
        if args:
            mblogid = Weibo.parse_detail_url(args[0])
            if mblogid:
                await self._weibo_detail(update, context, mblogid)
                return
        await update.message.reply_text(
            "Usage:\n"
            "  /weibo login — bind Weibo account to this Telegram user by QR code\n"
            "  /weibo hot   — show Weibo hot search band\n"
            "  /weibo <url> — fetch a Weibo post (text + images) by its URL"
        )

    async def _weibo_login(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        assert update.message is not None
        user = update.effective_user
        if user is None:
            await update.message.reply_text("Cannot identify user.")
            return

        try:
            session = await Weibo.qr_login_start()
        except WeiboApiError as exc:
            self.ctx.logger.warning("weibo login: qr_login_start failed: %s", exc)
            await update.message.reply_text(f"Failed to start Weibo QR login: {exc}")
            return
        except Exception as exc:
            self.ctx.logger.exception("weibo login: qr_login_start unexpected error")
            await update.message.reply_text(f"Failed to start Weibo QR login: {exc}")
            return

        try:
            await update.message.reply_photo(
                photo=session.image_url,
                caption=(
                    "Scan the QR code with Weibo app to login.\n"
                    f"Timeout: {int(_WEIBO_QR_TIMEOUT)}s.\n"
                    f"Fallback link: {session.scan_url}"
                ),
            )
        except Exception:
            # 兜底：send_photo 失败（比如二维码 URL 不能被 TG 直接抓取时）
            self.ctx.logger.exception("weibo login: reply_photo failed, fallback to url")
            await update.message.reply_text(
                "Scan the QR code with Weibo app to login "
                f"(timeout {int(_WEIBO_QR_TIMEOUT)}s):\n{session.image_url}"
            )

        loop = asyncio.get_running_loop()
        deadline = loop.time() + _WEIBO_QR_TIMEOUT
        notified_scanned = False
        cookies: dict[str, str] = {}
        final_status = "timeout"
        error_message = ""

        while loop.time() < deadline:
            try:
                result = await Weibo.qr_login_check(session)
            except WeiboApiError as exc:
                self.ctx.logger.warning("weibo login: qr_login_check failed: %s", exc)
                error_message = str(exc)
                final_status = "error"
                break
            except Exception as exc:
                self.ctx.logger.exception("weibo login: qr_login_check unexpected error")
                error_message = str(exc)
                final_status = "error"
                break

            if result.status == "success":
                cookies = dict(result.cookies)
                final_status = "success"
                break
            if result.status == "expired":
                final_status = "expired"
                break
            if result.status == "scanned" and not notified_scanned:
                notified_scanned = True
                try:
                    await update.message.reply_text("QR scanned. Please confirm on your phone.")
                except Exception:
                    self.ctx.logger.exception("weibo login: notify scanned failed")

            await asyncio.sleep(_WEIBO_QR_POLL_INTERVAL)

        if final_status != "success":
            reason = {
                "expired": "QR code expired, please try /weibo login again.",
                "timeout": "Login timed out, please try /weibo login again.",
                "error": f"Login failed: {error_message}",
            }.get(final_status, "Login failed.")
            await update.message.reply_text(reason)
            return

        # 成功 —— 合并到 user.meta.weibo_cookies，保留 location / location_info 等已有字段
        existing_meta: dict = {}
        try:
            existing = await self.ctx.invoke(
                UserQueryService,
                UserQueryParams(platform=_PLATFORM, user_id=str(user.id)),
            )
            if existing.found and existing.user is not None:
                existing_meta = dict(existing.user.meta or {})
        except Exception:
            self.ctx.logger.exception("weibo login: read existing user meta failed")

        existing_meta["weibo_cookies"] = cookies
        await self.ctx.publish(
            DatabaseWrite,
            DatabaseWritePayload(
                table=UserRecord.__table__,
                row={
                    "platform": _PLATFORM,
                    "user_id": str(user.id),
                    "user_name": user.full_name or "",
                    "meta": existing_meta,
                },
                upsert=True,
            ),
        )
        await update.message.reply_text("Weibo login success, cookies saved.")

    async def _weibo_hot(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        assert update.message is not None
        # get_hot_band 是公开接口 —— 不需要登录/ cookie；直接匿名调用即可
        try:
            async with Weibo() as wb:
                band = await wb.get_hot_band()
        except WeiboApiError as exc:
            self.ctx.logger.warning("weibo hot: get_hot_band failed: %s", exc)
            await update.message.reply_text(f"Get Weibo hot band failed: {exc}")
            return
        except Exception as exc:
            self.ctx.logger.exception("weibo hot: unexpected error")
            await update.message.reply_text(f"Get Weibo hot band failed: {exc}")
            return

        band_list = band.get("band_list") or []
        if not band_list:
            await update.message.reply_text("Weibo hot band is empty.")
            return

        # 格式对齐 data.jsonl 里的 "[hot_band] band_list top5" 示例，只是条数抬到 top-N
        lines = [f"[hot_band] band_list top{_WEIBO_HOT_TOP_N} of {len(band_list)}:"]
        for i, item in enumerate(band_list[:_WEIBO_HOT_TOP_N], 1):
            label = str(item.get("label_name") or "")
            word = str(item.get("word") or "?")
            lines.append(f"  {i}. [{label:<3}] {word}")
        await update.message.reply_text("\n".join(lines))

    async def _load_user_weibo_cookies(self, user_id: str) -> dict[str, str] | None:
        """从 user 表读取 ``meta.weibo_cookies``。缺失返回 None。"""
        try:
            result = await self.ctx.invoke(
                UserQueryService,
                UserQueryParams(platform=_PLATFORM, user_id=user_id),
            )
        except Exception:
            self.ctx.logger.exception("weibo: load user cookies failed")
            return None
        if not result.found or result.user is None:
            return None
        cookies = (result.user.meta or {}).get("weibo_cookies")
        if not isinstance(cookies, dict) or not cookies:
            return None
        return {k: str(v) for k, v in cookies.items() if v}

    async def _weibo_detail(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        mblogid: str,
    ) -> None:
        """抓单条微博详情并回复：文本 + 图片；视频 / 投票 / 卡片等富媒体一律忽略。"""
        assert update.message is not None
        user = update.effective_user
        if user is None:
            await update.message.reply_text("Cannot identify user.")
            return

        cookies = await self._load_user_weibo_cookies(str(user.id))
        if not cookies:
            await update.message.reply_text("No Weibo login found. Please run /weibo login first.")
            return

        try:
            async with Weibo(cookies=cookies) as wb:
                detail = await wb.get_weibo_detail(mblogid)
        except (WeiboSessionExpiredError, WeiboAuthRequiredError):
            await update.message.reply_text(
                "Weibo session expired or missing. Please run /weibo login again."
            )
            return
        except WeiboApiError as exc:
            self.ctx.logger.warning("weibo detail: get_weibo_detail failed: %s", exc)
            await update.message.reply_text(f"Get Weibo detail failed: {exc}")
            return
        except Exception as exc:
            self.ctx.logger.exception("weibo detail: unexpected error")
            await update.message.reply_text(f"Get Weibo detail failed: {exc}")
            return

        # 转发微博的正文在 retweeted_status 里；把它的文本 / 图片也拼上，视频等仍忽略
        text = Weibo.format_detail_text(detail)
        photos = Weibo.extract_detail_photos(detail, limit=_WEIBO_MAX_PHOTOS)

        if not text and not photos:
            await update.message.reply_text("Weibo post has no text or images.")
            return

        # 单图 → send_photo 带 caption；多图 → media group（首张挂 caption）；纯文本 → reply_text
        if not photos:
            await update.message.reply_text(text or "(no text)")
            return

        # sinaimg 有 Referer 校验，直连给 TG 服务端会 403 —— 必须本地下载再上传
        downloads = await asyncio.gather(
            *(Weibo.download_photo(url) for url in photos),
            return_exceptions=True,
        )
        blobs: list[bytes] = []
        failed: list[str] = []
        for url, item in zip(photos, downloads, strict=True):
            if isinstance(item, bytes):
                blobs.append(item)
            else:
                self.ctx.logger.warning(
                    "weibo detail: download photo failed url=%s err=%s", url, item
                )
                failed.append(url)

        caption = text or None
        if not blobs:
            # 所有图片都下载失败 —— 兜底成"文本 + 图片 URL"，让用户至少能自己点开看
            fallback = (text + "\n\n" if text else "") + "\n".join(failed)
            await update.message.reply_text(fallback or "Failed to fetch photos.")
            return

        if len(blobs) == 1:
            try:
                await update.message.reply_photo(photo=blobs[0], caption=caption)
                return
            except Exception:
                self.ctx.logger.exception("weibo detail: reply_photo failed")
                # 兜底：文本 + 图片 URL
                fallback = (text + "\n\n" if text else "") + photos[0]
                await update.message.reply_text(fallback)
                return

        media = [
            InputMediaPhoto(media=blob, caption=caption if i == 0 else None)
            for i, blob in enumerate(blobs)
        ]
        try:
            await update.message.reply_media_group(media=media)
        except Exception:
            self.ctx.logger.exception("weibo detail: reply_media_group failed")
            fallback = (text + "\n\n" if text else "") + "\n".join(photos)
            await update.message.reply_text(fallback)

    async def translate_command(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """/translate <text>"""
        if not update.message:
            return
        args = context.args or []
        if not args:
            await update.message.reply_text("Usage: /translate <text>")
            return
        text = " ".join(args)
        try:
            source_language = "zh-CN" if re.search(r"[\u4e00-\u9fff]", text) else "en"
            target = "zh-CN" if source_language == "en" else "en"
            translator = GoogleTranslator(
                source=source_language,
                target=target,
            )
            reply_text =  await asyncio.to_thread(translator.translate, text)
        except Exception as exc:
            self.ctx.logger.exception("translate command failed")
            await update.message.reply_text(f"Translate failed: {exc}")
            return
        await update.message.reply_text(reply_text)
