"""天气展示层 —— provider 无关的格式化函数。

接受统一的 :class:`ForecastData` 模型（不是 provider 原始 dict），输出 HTML /
MarkdownV2 (Telegram) / 纯文本三种格式。

供 IM 模块（如 telegram_bot）在收到 forecast 后直接调用。
"""

from __future__ import annotations

import html

from services.weather.base import DailyForecast, ForecastData

__all__ = [
    "WEATHER_EMOJIS",
    "build_weather_message",
    "escape_markdown_v2",
    "get_weather_emoji",
    "get_week_name",
]


# 天气现象 Emoji 映射表
WEATHER_EMOJIS: dict[str, str] = {
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

_WEEK_MAP = {
    "1": "周一",
    "2": "周二",
    "3": "周三",
    "4": "周四",
    "5": "周五",
    "6": "周六",
    "7": "周日",
}


def get_weather_emoji(weather_str: str) -> str:
    """模糊匹配天气描述并返回对应 emoji。命中不到时返回 🌈。"""
    for key, emoji in WEATHER_EMOJIS.items():
        if key in weather_str:
            return emoji
    return "🌈"


def get_week_name(week_str: str) -> str:
    """把星期数字符串（'1'..'7'）转成 '周一'..'周日'。"""
    return _WEEK_MAP.get(str(week_str), f"周{week_str}")


def escape_markdown_v2(text: str) -> str:
    """Telegram MarkdownV2 严格转义。"""
    escape_chars = r"_*[]()~`>#+-=|{}.!"
    return "".join("\\" + char if char in escape_chars else char for char in str(text))


def _weather_status(cast: DailyForecast) -> str:
    """把 day/night 天气拼成 '多云' 或 '多云转晴' 形式。空字段兜底为 '未知'。"""
    day = cast.day_weather or "未知"
    night = cast.night_weather or "未知"
    return f"{day}转{night}" if day != night else day


def _temp_range(cast: DailyForecast) -> str:
    return f"{cast.night_temp or '--'} ~ {cast.day_temp or '--'}"


def _wind_status(cast: DailyForecast) -> str:
    day_wind = f"{cast.day_wind or '未知'}风{cast.day_power or '--'}级"
    night_wind = f"{cast.night_wind or '未知'}风{cast.night_power or '--'}级"
    return day_wind if day_wind == night_wind else f"{day_wind} 转 {night_wind}"


def build_weather_message(
    forecast: ForecastData | None,
    *,
    parse_mode: str = "html",
    address: str | None = None,
) -> str:
    """构建天气推送消息。

    :param forecast: 统一 :class:`ForecastData` 模型（provider 无关）
    :param parse_mode: ``"html"`` / ``"markdown"`` / ``"text"``
    :param address: 可选详细地址（如具体街道 / 大厦名），显示时替代 city 名
    :return: 格式化后的消息字符串
    """
    if not forecast or not forecast.casts:
        return "⚠️ 暂无有效的天气预报数据。"

    mode = parse_mode.lower()
    city_name = forecast.city or "未知城市"
    report_time = forecast.report_time or "未知时间"
    today = forecast.casts[0]
    future = forecast.casts[1:]

    weather_status = _weather_status(today)
    temp_range = _temp_range(today)
    wind_status = _wind_status(today)
    emoji = get_weather_emoji(today.day_weather or "")

    if mode == "html":
        addr = html.escape(address) if address else html.escape(city_name)
        msg = f"<b>📍 城市天气推送: {addr}</b>\n"
        msg += f"更新时间: <code>{html.escape(report_time)}</code>\n"
        msg += "━━━━━━━━━━━━━━━━━━\n"
        msg += f"{emoji} <b>今日天气</b>: {html.escape(weather_status)}\n"
        msg += f"🌡️ <b>气温范围</b>: <code>{html.escape(temp_range)}</code> ℃\n"
        msg += f"💨 <b>风向风力</b>: {html.escape(wind_status)}\n"
        if future:
            msg += "\n🔮 <b>未来几天预报:</b>\n"
            for cast in future:
                c_date = (cast.date or "")[5:]
                c_week = get_week_name(cast.week)
                c_weat = _weather_status(cast)
                c_emoji = get_weather_emoji(cast.day_weather or "")
                msg += (
                    f"• <code>{c_date}</code> ({c_week}): "
                    f"{c_emoji} {html.escape(c_weat)} | "
                    f"🌡️ {cast.night_temp}~{cast.day_temp}℃\n"
                )
        return msg

    if mode == "markdown":
        city_esc = escape_markdown_v2(address) if address else escape_markdown_v2(city_name)
        time_esc = escape_markdown_v2(report_time)
        weat_esc = escape_markdown_v2(weather_status)
        temp_esc = escape_markdown_v2(temp_range)
        wind_esc = escape_markdown_v2(wind_status)
        msg = f"*📍 城市天气推送: {city_esc}*\n"
        msg += f"更新时间: `{time_esc}`\n"
        msg += "──────────────────\n"
        msg += f"{emoji} *今日天气*: {weat_esc}\n"
        msg += f"🌡️ *气温范围*: `{temp_esc}` ℃\n"
        msg += f"💨 *风向风力*: {wind_esc}\n"
        if future:
            msg += "\n*🔮 未来几天预报:*\n"
            for cast in future:
                c_date = escape_markdown_v2((cast.date or "")[5:])
                c_week = escape_markdown_v2(get_week_name(cast.week))
                c_weat = _weather_status(cast)
                c_emoji = get_weather_emoji(cast.day_weather or "")
                msg += (
                    f"• `{c_date}` \\({c_week}\\): "
                    f"{c_emoji} {escape_markdown_v2(c_weat)} "
                    f"\\| 🌡️ {cast.night_temp}~{cast.day_temp}℃\n"
                )
        return msg

    # 纯文本
    addr = f"{city_name} ({address})" if address else city_name
    msg = f"📍 城市天气推送: {addr}\n"
    msg += f"更新时间: {report_time}\n"
    msg += "━━━━━━━━━━━━━━━━━━\n"
    msg += f"{emoji} 今日天气: {weather_status}\n"
    msg += f"🌡️ 气温范围: {temp_range} ℃\n"
    msg += f"💨 风向风力: {wind_status}\n"
    if future:
        msg += "\n🔮 未来几天预报:\n"
        for cast in future:
            c_date = (cast.date or "")[5:]
            c_week = get_week_name(cast.week)
            c_weat = _weather_status(cast)
            msg += f"• {c_date} ({c_week}): {c_weat} | {cast.night_temp}~{cast.day_temp}℃\n"
    return msg
