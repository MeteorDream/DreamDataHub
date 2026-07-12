"""weather_assistant — 天气助理模块（编排型）。

订阅 ``im.message`` → 用 ``llm.chat`` 判断意图 → 命中天气则调 ``weather.forecast``
→ 再调 ``llm.chat`` 生成自然语言回复 → 发布到 ``im.reply``。

此前是 workflow，重构后作为一个显式声明 ``requires`` 的普通 Module。
"""

from __future__ import annotations

import json

from hub import Context, Module
from message.bot import BotEvent, TextSegment
from module.llm_openai import LLMChatParams, LLMChatService
from module.weather import (
    WeatherForecastParams,
    WeatherForecastService,
)
from topics.im import IMMessage, IMReply

# 通过 requires 声明本模块依赖的能力：loader 会做严格校验和拓扑排序。
mod = Module(
    "weather_assistant",
    requires=[LLMChatService, WeatherForecastService],
)


@mod.on(IMMessage)
async def entry(ctx: Context, event: BotEvent) -> None:
    """入口：判断意图 → 查天气 → 回复。"""
    if event.sub_type == "overhear":
        # 群内没被 @ 的消息不主动接管
        return
    user_text = _extract_text(event)
    if not user_text:
        return

    ctx.logger.info("user text: %s", user_text[:100])

    # Step 1: LLM 判断意图 + 提取城市
    intent_raw = await ctx.invoke(
        LLMChatService,
        LLMChatParams(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "判断用户消息是否是天气查询。如果是，提取城市名。"
                        '回复 ONLY JSON: {"is_weather": bool, "city": str | null}'
                    ),
                },
                {"role": "user", "content": user_text},
            ],
        ),
    )
    # invoke 返回的 BaseModel（LLMChatResult），拿 reply 字段
    try:
        parsed = json.loads(getattr(intent_raw, "reply", ""))
    except (json.JSONDecodeError, TypeError):
        ctx.logger.warning("intent parse failed: %r", intent_raw)
        return

    if not parsed.get("is_weather"):
        ctx.logger.debug("not a weather query, skip")
        return

    city = parsed.get("city") or "深圳"
    ctx.logger.info("weather query: city=%s", city)

    # Step 2: 查天气（默认深圳 adcode）
    try:
        forecast = await ctx.invoke(
            WeatherForecastService,
            WeatherForecastParams(adcode="440305"),
        )
    except Exception:
        ctx.logger.exception("weather query failed")
        return

    # forecast 是 ForecastData 实例（services 层的统一模型）
    weather_str = forecast.model_dump_json(exclude={"raw"})

    # Step 3: 生成自然语言回复
    reply = await ctx.invoke(
        LLMChatService,
        LLMChatParams(
            messages=[
                {
                    "role": "system",
                    "content": (
                        "你是天气助手。用简短自然的中文描述天气，突出温度、"
                        "天气现象。不要用 markdown。"
                    ),
                },
                {
                    "role": "user",
                    "content": f"城市：{city}\n天气数据（JSON）：{weather_str}",
                },
            ],
        ),
    )

    final_text = getattr(reply, "reply", "") or "查询天气失败"

    # Step 4: 发布回复
    reply_event = BotEvent(
        id=f"wa:{event.message_id}",
        platform=event.platform,
        time=0.0,
        type="message",
        detail_type=event.detail_type,
        sub_type="",
        message_id=f"wa:{event.message_id}",
        message=[TextSegment(text=final_text)],
        bot_id=event.bot_id,
        user_id=event.bot_id,
        user_name="bot",
        session_id=event.session_id,
        session_name=event.session_name,
    )
    await ctx.publish(IMReply, reply_event)
    ctx.logger.info("weather reply published for city=%s", city)


def _extract_text(event: BotEvent) -> str:
    parts = [seg.text for seg in event.message if seg.type == "Text"]
    return "".join(parts).strip()
