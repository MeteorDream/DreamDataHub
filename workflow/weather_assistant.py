"""workflow.weather_assistant — 天气助理 workflow。

展示如何通过 Workflow 编排多个模块的能力。

流程：收到 im.message → LLM 判断意图 → 提取城市 → 查天气 → LLM 生成回复 → 发布到 im.reply

两种触发方式：
1. Topic 触发：subscribe = "im.message"
2. 主动触发：ctx.start_workflow("weather_assistant", params)
"""

from __future__ import annotations

import json

from hub import Module
from hub.topics import IM_REPLY
from hub.workflow import Workflow, WorkflowContext
from message.bot import BotEvent, TextSegment

mod = Module("workflow_weather_assistant")


@mod.provides("workflow.weather_assistant")
async def handler(wf_ctx: WorkflowContext) -> dict:
    """Workflow handler：编排 LLM 判断意图 → 查天气 → 回复。"""
    # 从触发数据中提取文本
    event = wf_ctx.origin_payload
    if isinstance(event, BotEvent):
        user_text = _extract_text(event)
        session_id = event.session_id
        platform = event.platform
        bot_id = event.bot_id
        message_id = event.message_id
        detail_type = event.detail_type
        session_name = event.session_name
    elif isinstance(event, dict):
        user_text = event.get("text", "")
        session_id = event.get("session_id", "")
        platform = event.get("platform", "unknown")
        bot_id = event.get("bot_id", "")
        message_id = event.get("message_id", "")
        detail_type = event.get("detail_type", "private")
        session_name = event.get("session_name", "")
    else:
        user_text = str(event)
        session_id = ""
        platform = "unknown"
        bot_id = ""
        message_id = ""
        detail_type = "private"
        session_name = ""

    if not user_text:
        return {"handled": False, "reason": "empty text"}

    wf_ctx.logger.info("[%s] user text: %s", wf_ctx.trace_id, user_text[:100])

    # Step 1: LLM 判断意图 + 提取城市
    intent = await wf_ctx.invoke("llm.chat", {
        "messages": [
            {
                "role": "system",
                "content": (
                    "判断用户消息是否是天气查询。如果是，提取城市名。"
                    '回复 ONLY JSON: {"is_weather": bool, "city": str | null}'
                ),
            },
            {"role": "user", "content": user_text},
        ],
    })

    try:
        parsed = json.loads(intent["reply"])
    except (json.JSONDecodeError, KeyError):
        wf_ctx.logger.warning("[%s] intent parse failed: %s", wf_ctx.trace_id, intent)
        return {"handled": False, "reason": "intent parse failed"}

    if not parsed.get("is_weather"):
        wf_ctx.logger.info("[%s] not a weather query, skip", wf_ctx.trace_id)
        return {"handled": False, "reason": "not weather query"}

    city = parsed.get("city", "深圳")
    wf_ctx.logger.info("[%s] weather query: city=%s", wf_ctx.trace_id, city)

    # Step 2: 查天气（使用默认深圳 adcode）
    try:
        weather_data = await wf_ctx.invoke("weather.forecast", {
            "adcode": "440305",
            "city": city,
        })
    except Exception as e:
        wf_ctx.logger.error("[%s] weather query failed: %s", wf_ctx.trace_id, e)
        return {"handled": False, "reason": f"weather query failed: {e}"}

    # Step 3: LLM 生成自然语言回复
    forecasts = weather_data.get("forecasts", [{}])
    weather_str = json.dumps(forecasts, ensure_ascii=False)
    reply = await wf_ctx.invoke("llm.chat", {
        "messages": [
            {
                "role": "system",
                "content": "你是天气助手。用简短自然的中文描述天气，突出温度、天气现象。不要用 markdown。",
            },
            {
                "role": "user",
                "content": f"城市：{city}\n天气数据（JSON）：{weather_str}",
            },
        ],
    })

    final_text = reply.get("reply", "查询天气失败")

    # Step 4: 发布回复到 IM 总线
    reply_event = BotEvent(
        id=f"wf:{message_id}",
        platform=platform,
        time=0.0,
        type="message",
        detail_type=detail_type,
        sub_type="",
        message_id=f"wf:{message_id}",
        message=[TextSegment(text=final_text)],
        bot_id=bot_id,
        user_id=bot_id,
        user_name="bot",
        session_id=session_id,
        session_name=session_name,
    )
    await wf_ctx.publish(IM_REPLY, reply_event)
    wf_ctx.logger.info("[%s] weather reply published", wf_ctx.trace_id)

    return {"handled": True, "city": city}


# 导出 Workflow 定义（loader 会扫描这个变量）
workflow = Workflow(
    name="weather_assistant",
    description="天气助理 — LLM 判断意图 → 查天气 → 生成回复",
    subscribe="im.message",
    handler=handler,
    timeout=30.0,
)


def _extract_text(event: BotEvent) -> str:
    parts = [seg.text for seg in event.message if seg.type == "Text"]
    return "".join(parts).strip()
