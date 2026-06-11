"""im_qq — OneBot v11/v12 正向 WebSocket IM 模块。

职责（IM 层；不含 LLM 业务）：
- 启动时连接 napcat / OneBot 实现端的 WS 服务
- 把 OneBot 协议帧解析成 ``BotEvent``，发布到 ``im.message``
- 订阅 ``im.reply``，把 ``BotEvent`` 序列化回 OneBot ``send_msg`` 调用

依赖：``websockets``（pyproject 里加；目前未声明，按需安装）。

完整协议解析、白名单、动作超时等细节见仓库根目录的 ``qq_bot_example.py``，
本文件先给出最小骨架，业务方按 TODO 逐步迁移：
- 协议解析：``qq_bot_example.py`` 的 ``_on_message_event`` ~ 第 700 行附近
- send_msg：``_call_action`` / ``_send_text`` ~ 第 850 行附近
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

from hub import Context, Module
from hub.topics import IM_MESSAGE, IM_REPLY
from message.bot import BotEvent

mod = Module("im_qq")


@mod.on_startup
async def setup(ctx: Context) -> None:
    ctx.state.ws = None
    ctx.state.ws_url = ctx.config.get("ws_url", "ws://127.0.0.1:3001")
    ctx.state.access_token = ctx.config.get("access_token", "") or None
    ctx.state.whitelist_groups = {str(g) for g in ctx.config.get("whitelist_groups", [])}
    ctx.state.whitelist_users = {str(u) for u in ctx.config.get("whitelist_users", [])}
    ctx.state.action_futures: dict[str, asyncio.Future[dict]] = {}
    ctx.spawn(_recv_loop(ctx), name="im_qq:ws_loop")
    ctx.logger.info("im_qq: connecting to %s", ctx.state.ws_url)


@mod.on_shutdown
async def teardown(ctx: Context) -> None:
    ws = ctx.state.ws
    if ws is not None:
        try:
            await ws.close()
        except Exception:  # noqa: BLE001
            pass
    ctx.logger.info("im_qq: closed")


@mod.on(IM_REPLY)
async def on_reply(event: BotEvent, ctx: Context) -> None:
    """把 BotEvent 反向序列化成 OneBot send_msg 动作。"""
    if ctx.state.ws is None:
        ctx.logger.warning("im_qq: ws not connected, drop reply")
        return
    payload = _build_send_action(event)
    try:
        await ctx.state.ws.send(json.dumps(payload))
    except Exception:  # noqa: BLE001
        ctx.logger.exception("im_qq: failed to send reply")


# ---------------------------------------------------------------------------
# 内部
# ---------------------------------------------------------------------------


async def _recv_loop(ctx: Context) -> None:
    """连 WS、收帧、发 im.message。断线自动重连。"""
    try:
        import websockets  # noqa: PLC0415  延迟导入：模块未启用时无需依赖
    except ImportError:
        ctx.logger.error("im_qq: websockets 未安装，请 `uv add websockets`")
        return

    headers = {}
    if ctx.state.access_token:
        headers["Authorization"] = f"Bearer {ctx.state.access_token}"

    while not ctx.hub_event.is_set():
        try:
            async with websockets.connect(ctx.state.ws_url, additional_headers=headers) as ws:
                ctx.state.ws = ws
                ctx.logger.info("im_qq: ws connected")
                async for raw in ws:
                    if ctx.hub_event.is_set():
                        break
                    await _on_ws_frame(raw, ctx)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            ctx.logger.exception("im_qq: ws loop error, will reconnect")
        finally:
            ctx.state.ws = None
        # 轻量退避；shutdown 时立即返回
        try:
            await asyncio.wait_for(ctx.hub_event.wait(), timeout=3.0)
        except asyncio.TimeoutError:
            pass


async def _on_ws_frame(raw: str | bytes, ctx: Context) -> None:
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    try:
        data: dict[str, Any] = json.loads(raw)
    except json.JSONDecodeError:
        ctx.logger.warning("im_qq: non-JSON frame, drop")
        return

    # action 响应 — TODO：需要时接入 ctx.state.action_futures 做 RPC 风格调用
    if "echo" in data and "status" in data:
        return

    # 仅处理 message 事件
    if data.get("post_type") != "message":
        return

    if not _is_in_whitelist(data, ctx):
        return

    event = _onebot_to_botevent(data)
    if event is not None:
        await ctx.publish(IM_MESSAGE, event)


def _is_in_whitelist(data: dict, ctx: Context) -> bool:
    msg_type = data.get("message_type")
    if msg_type == "group":
        return str(data.get("group_id")) in ctx.state.whitelist_groups
    if msg_type == "private":
        return str(data.get("user_id")) in ctx.state.whitelist_users
    return False


def _onebot_to_botevent(data: dict) -> BotEvent | None:
    """OneBot v11 message 事件 → BotEvent。

    TODO：完整段类型映射见 ``qq_bot_example.py`` 中对 ``message`` 数组的处理逻辑
    （text / image / at / reply / face / forward / ...）。当前骨架只识别纯文本，
    够把 LLM 链路跑通；其余段标记为 Unknown 以便下游自行决定。
    """
    from message.bot import PlainSegment, UnknownSegment  # noqa: PLC0415

    raw_segments = data.get("message") or []
    segments: list = []
    for seg in raw_segments:
        if not isinstance(seg, dict):
            continue
        stype = seg.get("type")
        sdata = seg.get("data") or {}
        if stype == "text":
            text = sdata.get("text", "")
            if text:
                segments.append(PlainSegment(text=text))
        else:
            segments.append(UnknownSegment(raw=seg))

    if not segments:
        return None

    msg_type = data.get("message_type", "")
    is_group = msg_type == "group"
    session_key = f"group:{data.get('group_id')}" if is_group else f"private:{data.get('user_id')}"
    sender = data.get("sender") or {}
    return BotEvent(
        id=str(data.get("message_id", "")),
        platform="qq",
        time=float(data.get("time", 0)),
        type="message",
        detail_type=msg_type,
        sub_type=str(data.get("sub_type", "")),
        message_id=str(data.get("message_id", "")),
        message=segments,
        bot_id=str(data.get("self_id", "")),
        user_id=str(data.get("user_id", "")),
        user_name=str(sender.get("nickname") or sender.get("card") or ""),
        session_id=session_key,
        session_name=str(data.get("group_name") or sender.get("nickname") or session_key),
    )


def _build_send_action(event: BotEvent) -> dict:
    """BotEvent → OneBot send_msg 动作。

    TODO：图片/at/reply 等段反向映射见 ``qq_bot_example.py`` 的发送逻辑。
    """
    text_parts: list[str] = []
    for seg in event.message:
        if seg.type == "Plain":
            text_parts.append(seg.text)  # type: ignore[attr-defined]
    text = "".join(text_parts) or "..."

    if event.session_id.startswith("group:"):
        gid = event.session_id.removeprefix("group:")
        return {
            "action": "send_group_msg",
            "params": {"group_id": int(gid), "message": [{"type": "text", "data": {"text": text}}]},
        }
    uid = event.session_id.removeprefix("private:")
    return {
        "action": "send_private_msg",
        "params": {"user_id": int(uid), "message": [{"type": "text", "data": {"text": text}}]},
    }
