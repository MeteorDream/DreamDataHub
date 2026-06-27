from __future__ import annotations

import asyncio
import html
from datetime import datetime
from typing import Any, ClassVar

import httpx
from croniter import croniter

from hub import Context, Module

mod = Module("weather")


@mod.on_startup
async def setup(ctx: Context) -> None:
    cfg = ctx.config
    Weather.AMAP_KEY = cfg.get("amap_key", "")
    schedule = cfg.get("schedule", "")  # cron 表达式
    ctx.state.schedule = schedule
    if schedule:
        ctx.spawn(_push(ctx), name="weather:push")
        ctx.logger.info("Weather module setup scheuler")


@mod.on_shutdown
async def teardown(ctx: Context) -> None:
    ctx.logger.info("Weather module teardown")


async def _push(ctx: Context) -> None:
    """周期推送天气信息到指定的 topic"""

    schedule = ctx.state.schedule
    if not croniter.is_valid(schedule):
        ctx.logger.warning("Weather module: invalid cron schedule: %r", schedule)
        return

    try:
        while not ctx.hub_event.is_set():
            try:
                now = datetime.now()
                cron = croniter(schedule, now)
                next_time = cron.get_next(datetime)
                await asyncio.wait_for(
                    ctx.hub_event.wait(), timeout=(next_time - now).total_seconds()
                )
            except TimeoutError:
                pass
            try:
                ctx.logger.info("Weather push: schedule triggered, fetching weather data...")
            except Exception as e:
                ctx.logger.exception("Weather push error: %s", e)
    except asyncio.CancelledError:
        ctx.logger.debug("weather push cancelled")
        raise


class Weather:
    """各种平台天气 API 接口"""

    # 高德 API KEY
    AMAP_KEY: ClassVar[str] = ""

    # 天气现象 Emoji 映射表
    WEATHER_EMOJIS = {
        "晴": "☀️",
        "少云": "🌤️",
        "晴间多云": "⛅",
        "多云": "☁️",
        "阴": "☁️",
        "阵雨": "🌦️",
        "雷阵雨": "⛈️",
        "雷阵雨并伴有冰雹": "⛈️ℹ️",
        "小雨": "🌧️",
        "毛毛雨/细雨": "🌧️",
        "雨": "🌧️",
        "中雨": "🌧️",
        "大雨": "🌧️",
        "暴雨": "⛈️",
        "大暴雨": "⛈️",
        "特大暴雨": "⛈️",
        "小雪": "🌨️",
        "中雪": "🌨️",
        "大雪": "❄️",
        "暴雪": "❄️",
        "雪": "❄️",
        "雨夹雪": "🌨️",
        "冻雨": "❄️",
        "霾": "😷",
        "中度霾": "😷",
        "重度霾": "😷",
        "严重霾": "😷",
        "雾": "🌫️",
        "浓雾": "🌫️",
        "大雾": "🌫️",
        "轻雾": "🌫️",
        "浮尘": "🍂",
        "扬沙": "🍂",
        "沙尘暴": "🌪️",
        "强沙尘暴": "🌪️",
        "龙卷风": "🌪️",
        "有风": "💨",
        "微风": "🍃",
        "和风": "🍃",
        "清风": "🍃",
        "大风": "💨",
        "强风/劲风": "💨",
        "热": "🌡️🔥",
        "冷": "🌡️❄️",
        "未知": "❓",
    }

    @staticmethod
    def get_weather_emoji(weather_str: str) -> str:
        """模糊匹配天气并返回对应 Emoji"""
        for key, emoji in Weather.WEATHER_EMOJIS.items():
            if key in weather_str:
                return emoji
        return "🌈"

    @staticmethod
    def get_week_name(week_str: str) -> str:
        """转换星期数"""
        week_map = {
            "1": "周一",
            "2": "周二",
            "3": "周三",
            "4": "周四",
            "5": "周五",
            "6": "周六",
            "7": "周日",
        }
        return week_map.get(str(week_str), f"周{week_str}")

    @staticmethod
    def escape_markdown_v2(text: str) -> str:
        """Telegram MarkdownV2 严格转义函数"""
        escape_chars = r"_*[]()~`>#+-=|{}.!"
        return "".join("\\" + char if char in escape_chars else char for char in str(text))

    @staticmethod
    async def amap_location(longitude: float, latitude: float) -> tuple[str, dict]:
        """逆地理编码, 根据经纬度返回位置信息, api 文档: https://lbs.amap.com/api/webservice/guide/api/georegeo

        Args:
            longitude: 经度
            latitude: 纬度

        Returns:
            msg, data: 1. 错误信息, 成功时为空字符串; 2. 位置信息, 成功时包含 'formatted_address' 等字段, 失败时为空字典,
        """
        async with httpx.AsyncClient() as client:
            url = "https://restapi.amap.com/v3/geocode/regeo"
            params = {
                "key": Weather.AMAP_KEY,
                "location": f"{longitude:.4f},{latitude:.4f}",
                "output": "JSON",
            }
            response = await client.get(url, params=params, timeout=15.0)

            # 检查响应状态码
            if response.status_code != 200:
                return f"请求失败, 状态码: {response.status_code}", {}
            data = response.json()
            if data.get("status", "0") != "1":
                return (
                    f"请求失败, status: {data.get('status')}, code: {data.get('infocode')}, info: {data.get('info')}, code 说明可参考: https://lbs.amap.com/api/webservice/guide/tools/info",
                    {},
                )
            return "", data.get("regeocode")
        # response reference:  {"addressComponent": {"city": "深圳市", "province": "广东省", "adcode": "440305", "district": "南山区", "towncode": "440305001000", "streetNumber": {"number": "164号", "location": "113.927994,22.528178", "direction": "西北", "distance": "46.1741", "street": "南光路"}, "country": "中国", "township": "南头街道", "businessAreas": [{"location": "113.922350,22.523562", "name": "桂庙路口", "id": "440305"}, {"location": "113.920308,22.532088", "name": "桃园", "id": "440305"}, {"location": "113.948639,22.545900", "name": "科技园", "id": "440305"}], "building": {"name": "荟芳园", "type": "商务住宅;住宅区;住宅小区"}, "neighborhood": {"name": "荟芳园", "type": "商务住宅;住宅区;住宅小区"}, "citycode": "0755"}, "formatted_address": "广东省深圳市南山区南头街道荟芳园"}

    @staticmethod
    async def amap_weather(city: str) -> tuple[str, dict]:
        """天气查询, 根据 city 返回天气信息, api 文档: https://lbs.amap.com/api/webservice/guide/api-advanced/weatherinfo

        Args:
            city: 城市编码, 输入城市的 adcode，adcode 信息可通过 amap_location 获取

        Returns:
            msg, data: 1. 错误信息, 成功时为空字符串; 2. 天气信息, 成功时包含 'lives' 等字段, 失败时为空字典,
        """
        async with httpx.AsyncClient() as client:
            url = "https://restapi.amap.com/v3/weather/weatherInfo"
            params = {"key": Weather.AMAP_KEY, "city": city, "extensions": "all", "output": "JSON"}
            response = await client.get(url, params=params, timeout=15.0)

            # 检查响应状态码
            if response.status_code != 200:
                return f"请求失败, 状态码: {response.status_code}", {}
            data = response.json()
            if data.get("status", "0") != "1":
                return (
                    f"请求失败, status: {data.get('status')}, code: {data.get('infocode')}, info: {data.get('info')}, code 说明可参考: https://lbs.amap.com/api/webservice/guide/tools/info",
                    {},
                )
            return "", data.get("forecasts")
        # response reference:  {"status": "1", "count": "1", "info": "OK", "infocode": "10000", "forecasts": [{"city": "南山区", "adcode": "440305", "province": "广东", "reporttime": "2026-06-23 16:00:42", "casts": [{"date": "2026-06-23", "week": "2", "dayweather": "晴", "nightweather": "阴", "daytemp": "33", "nighttemp": "28", "daywind": "北", "nightwind": "北", "daypower": "1-3", "nightpower": "1-3", "daytemp_float": "33.0", "nighttemp_float": "28.0"}, {"date": "2026-06-24", "week": "3", "dayweather": "多云", "nightweather": "多云", "daytemp": "34", "nighttemp": "28", "daywind": "北", "nightwind": "北", "daypower": "1-3", "nightpower": "1-3", "daytemp_float": "34.0", "nighttemp_float": "28.0"}, {"date": "2026-06-25", "week": "4", "dayweather": "雷阵雨", "nightweather": "多云", "daytemp": "33", "nighttemp": "28", "daywind": "北", "nightwind": "北", "daypower": "1-3", "nightpower": "1-3", "daytemp_float": "33.0","nighttemp_float": "28.0"}, {"date": "2026-06-26", "week": "5", "dayweather": "雷阵雨", "nightweather": "雷阵雨", "daytemp": "31", "nighttemp": "26", "daywind": "北", "nightwind": "北", "daypower": "1-3", "nightpower": "1-3", "daytemp_float": "31.0", "nighttemp_float": "26.0"}]}]}

    @staticmethod
    def build_weather_message(
        forecasts: list[dict[str, Any]] | None, parse_mode: str = "html", address: str | None = None
    ) -> str:
        """
        根据给定的 forecasts 列表数据构建天气通知消息
        :param forecasts: 传入的 forecasts 列表数据（支持 None 或空列表）
        :param parse_mode: 支持 "text", "markdown", "html"
        :param address: 可选的详细地址字符串（例如具体的街道或大厦名）
        :return: 格式化后的天气消息文本
        """
        mode = parse_mode.lower()

        # 1. 安全校验：检查列表是否为空或 None
        if not forecasts or not isinstance(forecasts, list):
            return "⚠️ 暂无有效的天气预报数据。"

        # 提取核心层
        fc_data = forecasts[0]
        city_name = fc_data.get("city", "未知城市")
        report_time = fc_data.get("reporttime", "未知时间")
        casts_list = fc_data.get("casts", [])

        if not casts_list:
            return f"⚠️ 城市 [{city_name}] 暂无具体的预报详情。"

        # 2. 提取当天数据作为主显示
        today = casts_list[0]
        day_weather = today.get("dayweather", "未知")
        night_weather = today.get("nightweather", "未知")

        # 组合当前天气表现
        weather_status = (
            f"{day_weather}转{night_weather}" if day_weather != night_weather else day_weather
        )
        temp_range = f"{today.get('nighttemp', '--')} ~ {today.get('daytemp', '--')}"
        day_wind_str = f"{today.get('daywind', '未知')}风{today.get('daypower', '--')}级"
        night_wind_str = f"{today.get('nightwind', '未知')}风{today.get('nightpower', '--')}级"
        wind_status = (
            day_wind_str
            if day_wind_str == night_wind_str
            else f"{day_wind_str} 转 {night_wind_str}"
        )

        emoji = Weather.get_weather_emoji(day_weather)

        # 3. 根据不同的格式组装文本
        if mode == "html":
            addr = f"{html.escape(address)}" if address else html.escape(city_name)
            msg = f"<b>📍 城市天气推送: {addr}</b>\n"
            msg += f"更新时间: <code>{html.escape(report_time)}</code>\n"
            msg += "━━━━━━━━━━━━━━━━━━\n"
            msg += f"{emoji} <b>今日天气</b>: {html.escape(weather_status)}\n"
            msg += f"🌡️ <b>气温范围</b>: <code>{html.escape(temp_range)}</code> ℃\n"
            msg += f"💨 <b>风向风力</b>: {html.escape(wind_status)}\n"

            if len(casts_list) > 1:
                msg += "\n🔮 <b>未来几天预报:</b>\n"
                for cast in casts_list[1:]:
                    c_date = cast.get("date", "")[5:]
                    c_week = Weather.get_week_name(cast.get("week", ""))
                    c_d_weat = cast.get("dayweather", "未知")
                    c_n_weat = cast.get("nightweather", "未知")
                    c_weat = f"{c_d_weat}转{c_n_weat}" if c_d_weat != c_n_weat else c_d_weat

                    c_emoji = Weather.get_weather_emoji(c_d_weat)
                    msg += f"• <code>{c_date}</code> ({c_week}): {c_emoji} {html.escape(c_weat)} | 🌡️ {cast.get('nighttemp')}~{cast.get('daytemp')}℃\n"

        elif mode == "markdown":
            city_esc = (
                f"{Weather.escape_markdown_v2(address)}"
                if address
                else Weather.escape_markdown_v2(city_name)
            )
            time_esc = Weather.escape_markdown_v2(report_time)
            weat_esc = Weather.escape_markdown_v2(weather_status)
            temp_esc = Weather.escape_markdown_v2(temp_range)
            wind_esc = Weather.escape_markdown_v2(wind_status)

            msg = f"*📍 城市天气推送: {city_esc}*\n"
            msg += f"更新时间: `{time_esc}`\n"
            msg += "──────────────────\n"
            msg += f"{emoji} *今日天气*: {weat_esc}\n"
            msg += f"🌡️ *气温范围*: `{temp_esc}` ℃\n"
            msg += f"💨 *风向风力*: {wind_esc}\n"

            if len(casts_list) > 1:
                msg += "\n*🔮 未来几天预报:*\n"
                for cast in casts_list[1:]:
                    c_date = Weather.escape_markdown_v2(cast.get("date", "")[5:])
                    c_week = Weather.escape_markdown_v2(Weather.get_week_name(cast.get("week", "")))
                    c_d_weat = cast.get("dayweather", "未知")
                    c_n_weat = cast.get("nightweather", "未知")
                    c_weat = f"{c_d_weat}转{c_n_weat}" if c_d_weat != c_n_weat else c_d_weat

                    c_emoji = Weather.get_weather_emoji(c_d_weat)
                    msg += f"• `{c_date}` \\({c_week}\\): {c_emoji} {Weather.escape_markdown_v2(c_weat)} \\| 🌡️ {cast.get('nighttemp')}~{cast.get('daytemp')}℃\n"

        else:  # text 纯文本
            addr = f" ({address})" if address else city_name

            msg = f"📍 城市天气推送: {addr}\n"
            msg += f"更新时间: {report_time}\n"
            msg += "━━━━━━━━━━━━━━━━━━\n"
            msg += f"{emoji} 今日天气: {weather_status}\n"
            msg += f"🌡️ 气温范围: {temp_range} ℃\n"
            msg += f"💨 风向风力: {wind_status}\n"

            if len(casts_list) > 1:
                msg += "\n🔮 未来几天预报:\n"
                for cast in casts_list[1:]:
                    c_date = cast.get("date", "")[5:]
                    c_week = Weather.get_week_name(cast.get("week", ""))
                    c_d_weat = cast.get("dayweather", "未知")
                    c_n_weat = cast.get("nightweather", "未知")
                    c_weat = f"{c_d_weat}转{c_n_weat}" if c_d_weat != c_n_weat else c_d_weat
                    msg += f"• {c_date} ({c_week}): {c_weat} | {cast.get('nighttemp')}~{cast.get('daytemp')}℃\n"

        return msg


@mod.provides("weather.location")
async def location_capability(ctx: Context, params: dict) -> dict:
    """提供逆地理编码能力（workflow 可调用）。

    params:
        longitude: float
        latitude: float

    returns:
        regeocode dict（包含 formatted_address, addressComponent 等字段）
    """
    longitude = params.get("longitude", 0.0)
    latitude = params.get("latitude", 0.0)
    msg, data = await Weather.amap_location(longitude, latitude)
    if msg:
        raise RuntimeError(f"weather.location failed: {msg}")
    return data


@mod.provides("weather.forecast")
async def forecast_capability(ctx: Context, params: dict) -> dict:
    """提供天气预报能力（workflow 可调用）。

    params:
        adcode: str — 城市编码
        city: str | None — 城市名（可选，仅用于返回中携带）

    returns:
        {"forecasts": list, "city": str | None}
    """
    adcode = params.get("adcode", "")
    if not adcode:
        raise ValueError("weather.forecast: adcode is required")
    msg, forecasts = await Weather.amap_weather(adcode)
    if msg:
        raise RuntimeError(f"weather.forecast failed: {msg}")
    return {"forecasts": forecasts, "city": params.get("city")}
