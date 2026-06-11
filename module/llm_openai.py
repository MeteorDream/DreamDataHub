"""llm_openai — 调用 OpenAI 兼容服务的 LLM 模块。

订阅 ``im.message``：抽取文本 → 调 chat.completions → 构造一条新的 ``BotEvent``
作为回复发到 ``im.reply``；顺手把这次问答发到 ``llm.exchange``，由 store 类模块
负责持久化。

不感知 IM 协议的存在：所有平台细节都封在 IM 模块里。

依赖：``openai`` SDK；目前 pyproject 未声明，按需 ``uv add openai``。
"""

from __future__ import annotations

from hub import Context, Module
from hub.topics import IM_MESSAGE, IM_REPLY, LLM_EXCHANGE
from message.bot import BotEvent, PlainSegment

mod = Module("llm_openai")


@mod.on_startup
async def setup(ctx: Context) -> None:
    try:
        from openai import AsyncOpenAI  # noqa: PLC0415
    except ImportError:
        ctx.logger.error("llm_openai: 未安装 openai；`uv add openai`")
        ctx.state.client = None
        return

    api_key = ctx.config.get("api_key", "")
    base_url = ctx.config.get("base_url") or None
    ctx.state.model = ctx.config.get("model", "gpt-4o-mini")
    ctx.state.system_prompt = ctx.config.get("system_prompt", "You are a helpful assistant.")
    ctx.state.client = AsyncOpenAI(api_key=api_key, base_url=base_url)
    # 简陋的会话上下文：session_id → list[message]
    ctx.state.contexts = {}
    ctx.logger.info("llm_openai: ready (model=%s, base_url=%s)", ctx.state.model, base_url)


@mod.on_shutdown
async def teardown(ctx: Context) -> None:
    client = ctx.state.client
    if client is not None:
        try:
            await client.close()
        except Exception:  # noqa: BLE001
            pass


@mod.on(IM_MESSAGE)
async def reply(event: BotEvent, ctx: Context) -> None:
    if ctx.state.client is None:
        return

    user_text = _extract_text(event)
    if not user_text:
        return

    history: list = ctx.state.contexts.setdefault(
        event.session_id,
        [{"role": "system", "content": ctx.state.system_prompt}],
    )
    history.append({"role": "user", "content": user_text})

    try:
        resp = await ctx.state.client.chat.completions.create(
            model=ctx.state.model,
            messages=history,
        )
    except Exception:  # noqa: BLE001
        ctx.logger.exception("llm_openai: chat.completions failed")
        return

    answer = resp.choices[0].message.content or ""
    history.append({"role": "assistant", "content": answer})

    # 简陋裁剪：超长就丢最旧的非 system 条目
    max_keep = int(ctx.config.get("max_messages", 40))
    if len(history) > max_keep:
        del history[1 : len(history) - max_keep + 1]

    reply_event = BotEvent(
        id=f"reply:{event.message_id}",
        platform=event.platform,
        time=0.0,
        type="message",
        detail_type=event.detail_type,
        sub_type="",
        message_id=f"reply:{event.message_id}",
        message=[PlainSegment(text=answer)],
        bot_id=event.bot_id,
        user_id=event.bot_id,
        user_name="bot",
        session_id=event.session_id,
        session_name=event.session_name,
    )
    await ctx.publish(IM_REPLY, reply_event)
    await ctx.publish(
        LLM_EXCHANGE,
        {
            "session_id": event.session_id,
            "prompt": user_text,
            "response": answer,
            "meta": {"model": ctx.state.model, "platform": event.platform},
        },
    )


def _extract_text(event: BotEvent) -> str:
    parts = [seg.text for seg in event.message if seg.type == "Plain"]  # type: ignore[attr-defined]
    return "".join(parts).strip()
