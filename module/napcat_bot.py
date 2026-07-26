"""napcat_bot — napcat 正向 WebSocket 接入模块。

职责（仅 IM 协议层）：
- 连接 napcat 实现端的 WS 服务，断线自动重连
- 解析 OneBot 协议帧 → 构造 ``BotEvent``，按 18 类 ``BotSegment`` 双向映射
- 对群消息做白名单过滤；对 @ 机器人/未 @ 分别打 ``sub_type='mentioned' / 'overhear'``
- 双发：``im.message``（跨平台总线）+ ``qq.message``（平台专属）
- 订阅 ``im.reply`` / ``qq.reply``：``BotEvent`` 反向序列化为 OneBot ``send_msg`` 动作
- ``send_msg`` / ``get_file`` 之类需要回包的动作通过 echo + Future 字典实现 RPC

不做的事（在别的模块里）：
- 系统提示词、token 估算、上下文存储、LLM 调用 → ``module/llm_openai.py``
- reply 段引用摘要索引 → 后续可独立做 ``module/store_reply_index.py``

依赖：``websockets``、``httpx``。模块不启用时这些 import 不会触发。
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import time
import uuid
from collections import deque
from typing import Any

from hub import Context, Module
from message.bot import (
    AtSegment,
    AudioSegment,
    BotEvent,
    BotSegment,
    ContactSegment,
    FaceSegment,
    FileSegment,
    ForwardSegment,
    ImageSegment,
    JsonSegment,
    LocationSegment,
    MusicSegment,
    PokeSegment,
    ReplySegment,
    ShareSegment,
    TextSegment,
    UnknownSegment,
    VideoSegment,
)
from topics.im import IMReply
from topics.qq import QQReply

mod = Module("napcat_bot")


# ---------------------------------------------------------------------------
# 生命周期
# ---------------------------------------------------------------------------


@mod.on_startup
async def setup(ctx: Context) -> None:
    cfg = ctx.config
    ctx.state.ws_url = cfg.get("ws_url", "ws://127.0.0.1:3001")
    ctx.state.access_token = cfg.get("access_token", "") or None
    ctx.state.bot_id = str(cfg.get("bot_id", "") or "")
    ctx.state.bot_name = cfg.get("bot_name", "bot")
    ctx.state.whitelist_groups = {str(g) for g in cfg.get("whitelist_groups", [])}
    ctx.state.whitelist_users = {str(u) for u in cfg.get("whitelist_users", [])}
    ctx.state.reconnect_interval = float(cfg.get("reconnect_interval", 3.0))
    ctx.state.action_timeout = float(cfg.get("action_timeout", 30.0))
    ctx.state.ws_max_size = int(cfg.get("ws_max_size", 2**24))
    ctx.state.supported_image_mimes = set(
        cfg.get("supported_image_mimes", ["image/jpeg", "image/png", "image/webp"])
    )
    ctx.state.image_download_timeout = float(cfg.get("image_download_timeout", 30.0))

    ctx.state.history = {}

    ctx.state.ws = None
    # echo → Future[response_dict]
    ctx.state.pending_actions = {}
    # httpx 客户端延迟到首次下载图片时再创建（避免没图片时也建连接池）
    ctx.state.http = None

    ctx.spawn(_recv_loop(ctx), name="napcat_bot:ws_loop")
    ctx.logger.info(
        "napcat_bot: startup ws_url=%s whitelist groups=%d users=%d",
        ctx.state.ws_url,
        len(ctx.state.whitelist_groups),
        len(ctx.state.whitelist_users),
    )


@mod.on_shutdown
async def teardown(ctx: Context) -> None:
    _cancel_pending_actions(ctx, "shutdown")
    ws = ctx.state.ws
    if ws is not None:
        try:
            await ws.close()
        except Exception:
            pass
    http = ctx.state.http
    if http is not None:
        try:
            await http.aclose()
        except Exception:
            pass
    ctx.logger.info("napcat_bot: closed")


# ---------------------------------------------------------------------------
# 出站订阅
# ---------------------------------------------------------------------------


@mod.on(IMReply)
async def on_reply_broadcast(ctx: Context, event: BotEvent) -> None:
    """跨平台广播 reply：仅承接 session_id 是 group:/private: 的事件。"""
    if not event.session_id.startswith(("group:", "private:")):
        return
    await _send_reply(event, ctx)


@mod.on(QQReply)
async def on_reply_direct(ctx: Context, event: BotEvent) -> None:
    """业务方显式定向到 QQ 时使用。"""
    await _send_reply(event, ctx)


async def _send_reply(event: BotEvent, ctx: Context) -> None:
    if ctx.state.ws is None:
        ctx.logger.warning("napcat_bot: ws not connected, drop reply mid=%s", event.message_id)
        return
    try:
        params = _build_send_msg_params(event)
    except ValueError as exc:
        ctx.logger.warning("napcat_bot: build send_msg failed: %s", exc)
        return
    try:
        resp = await _call_action(ctx, "send_msg", params)
    except (TimeoutError, RuntimeError):
        ctx.logger.exception("napcat_bot: send_msg failed")
        return
    if resp.get("status") == "ok":
        sent_mid = (resp.get("data") or {}).get("message_id")
        ctx.logger.info(
            "napcat_bot: sent reply session=%s mid=%s",
            event.session_id,
            sent_mid,
        )
    else:
        ctx.logger.warning(
            "napcat_bot: send_msg non-ok retcode=%s msg=%r",
            resp.get("retcode"),
            resp.get("message"),
        )


# ---------------------------------------------------------------------------
# WS 主循环
# ---------------------------------------------------------------------------


async def _recv_loop(ctx: Context) -> None:
    """连 WS、收帧、断线重连。整个 loop 直到 hub_event 被置位才退出。"""
    try:
        import websockets
    except ImportError:
        ctx.logger.error("napcat_bot: websockets 未安装，请 `uv add websockets`")
        return

    headers = {}
    if ctx.state.access_token:
        headers["Authorization"] = f"Bearer {ctx.state.access_token}"

    while not ctx.hub_event.is_set():
        try:
            ctx.logger.info("napcat_bot: ws.connecting url=%s", ctx.state.ws_url)
            async with websockets.connect(
                ctx.state.ws_url,
                additional_headers=headers,
                max_size=ctx.state.ws_max_size,
                open_timeout=10,
                ping_interval=20,
                ping_timeout=20,
            ) as ws:
                ctx.state.ws = ws
                ctx.logger.info("napcat_bot: ws.connected url=%s", ctx.state.ws_url)
                async for raw in ws:
                    if ctx.hub_event.is_set():
                        break
                    if isinstance(raw, bytes):
                        ctx.logger.debug("napcat_bot: ws binary frame (%d bytes), drop", len(raw))
                        continue
                    _on_ws_frame(raw, ctx)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            ctx.logger.warning("napcat_bot: ws error %s: %s", type(exc).__name__, exc)
        finally:
            ctx.state.ws = None
            _cancel_pending_actions(ctx, "ws closed")

        if ctx.hub_event.is_set():
            break
        ctx.logger.info("napcat_bot: reconnect in %.1fs", ctx.state.reconnect_interval)
        try:
            await asyncio.wait_for(ctx.hub_event.wait(), timeout=ctx.state.reconnect_interval)
        except TimeoutError:
            pass


def _on_ws_frame(raw: str, ctx: Context) -> None:
    """派发一帧 —— 严格非阻塞。

    - action 响应：同步 fut.set_result，就地处理。
    - message 事件：spawn 独立 task。原因：_on_message_event 内会通过
      _call_action(get_file) 发起 RPC 并 await 响应，而响应帧只能由**本函数**
      的调用者 _recv_loop 派发；若在这里 await，就是自死锁。
    """
    try:
        data: dict[str, Any] = json.loads(raw)
        ctx.logger.debug("napcat_bot: ws.frame data: %s", json.dumps(data, ensure_ascii=False))
    except json.JSONDecodeError:
        ctx.logger.warning("napcat_bot: non-JSON frame, drop (%d bytes)", len(raw))
        return
    if not isinstance(data, dict):
        return

    if ctx.logger.isEnabledFor(logging.DEBUG):
        ctx.logger.debug("napcat_bot: frame keys=%s", sorted(data.keys()))

    # 1. action 响应 —— 同步派发（仅 fut.set_result，无 await）
    if "retcode" in data and "status" in data:
        _on_action_response(data, ctx)
        return

    # 2. 事件类型（兼容 v11 / v12）
    ev_type = data.get("type") or data.get("post_type")
    if ev_type in ("meta", "meta_event"):
        return
    if ev_type == "message":
        ctx.spawn(_dispatch_message_event(data, ctx), name="napcat_bot:msg_event")
        return
    # 其他事件 (notice / request)：暂不处理，只记录
    ctx.logger.info(
        "napcat_bot: event.other type=%s detail=%s",
        ev_type,
        data.get("detail_type") or data.get("notice_type") or data.get("request_type"),
    )


async def _dispatch_message_event(ev: dict, ctx: Context) -> None:
    """spawn 出来的 message 任务包装 —— 兜底日志，避免 'exception never retrieved'。"""
    try:
        await _on_message_event(ev, ctx)
    except asyncio.CancelledError:
        raise
    except Exception:
        ctx.logger.exception(
            "napcat_bot: message event handler crashed mid=%s",
            ev.get("message_id"),
        )


# ---------------------------------------------------------------------------
# 入站消息：协议帧 → BotEvent
# ---------------------------------------------------------------------------


async def _on_message_event(ev: dict, ctx: Context) -> None:
    detail = ev.get("detail_type") or ev.get("message_type") or ""
    message_id = str(ev.get("message_id", ""))
    user_id = str(ev.get("user_id", ""))
    group_id_raw = ev.get("group_id")
    group_id = str(group_id_raw) if group_id_raw is not None else ""

    self_user_id = _resolve_self_user_id(ev, ctx)
    raw_segments: list[dict] = ev.get("message") or []
    raw_text = ev.get("alt_message") or ev.get("raw_message") or ""

    # 白名单过滤
    if detail == "group":
        session_id = f"group:{group_id}"
        if group_id not in ctx.state.whitelist_groups:
            ctx.logger.debug(
                "napcat_bot: msg.skip not_whitelisted group=%s mid=%s", group_id, message_id
            )
            return
    elif detail == "private":
        session_id = f"private:{user_id}"
        if user_id not in ctx.state.whitelist_users:
            ctx.logger.debug(
                "napcat_bot: msg.skip not_whitelisted user=%s mid=%s", user_id, message_id
            )
            return
    else:
        ctx.logger.debug("napcat_bot: msg.skip unsupported_detail=%s mid=%s", detail, message_id)
        return

    # 段映射
    segments = await _onebot_to_segments(raw_segments, ctx, message_id)

    # @ 检测
    is_mentioned = detail == "private" or _is_mentioned(
        raw_segments, raw_text, self_user_id, ctx.state.bot_name
    )
    sub_type_label = "mentioned" if is_mentioned else "overhear"

    sender = ev.get("sender") or {}
    nickname = ""
    if isinstance(sender, dict):
        nickname = (sender.get("card") or sender.get("nickname") or "").strip()
    if not nickname:
        nickname = user_id or "unknown"

    event = BotEvent(
        id=message_id,
        platform="qq",
        time=float(ev.get("time", 0) or 0),
        type="message",
        detail_type=detail,
        sub_type=sub_type_label,
        message_id=message_id,
        message=segments,
        bot_id=self_user_id,
        user_id=user_id,
        user_name=nickname,
        session_id=session_id,
        session_name=str(ev.get("group_name") or nickname or session_id),
    )

    ctx.logger.info(
        "napcat_bot: msg.recv mid=%s session=%s sub_type=%s segs=%d nickname=%r",
        message_id,
        session_id,
        sub_type_label,
        len(segments),
        nickname,
    )

    history = ctx.state.history.setdefault(session_id, deque(maxlen=5))
    history.append(event)
    if _should_echo_repeat(history, raw_text):
        ctx.logger.info("napcat_bot: repeat.detect session=%s text=%r", session_id, raw_text[:80])
        await _send_reply(event, ctx)

    # await ctx.publish(IMMessage, event)
    # await ctx.publish(QQMessage, event)


def _should_echo_repeat(history: deque[BotEvent], raw_text: str) -> bool:
    """群里连续三条相同消息触发一次复读；跨过后就不再重复触发。

    条件：
    - 至少 3 条历史，最后 3 条 message 相同
    - 且第 4 条（若存在）与之不同 —— 保证一串重复只触发一次
    - 排除含 'cq' 的消息（避免撞上 CQ 码带引号的复读）
    """
    if len(history) < 3:
        return False
    if "cq" in raw_text.lower():
        return False
    if history[-1].message != history[-2].message or history[-2].message != history[-3].message:
        return False
    # 只有一串刚出现三连的第一时刻触发；已经复读过的（第四条仍相同）不再触发
    if len(history) >= 4 and history[-4].message == history[-1].message:
        return False
    return True


def _resolve_self_user_id(ev: dict, ctx: Context) -> str:
    """从事件里提自身 user_id；失败兜底 ctx.config.bot_id。"""
    self_info = ev.get("self") or {}
    if isinstance(self_info, dict):
        sid = self_info.get("user_id")
        if sid not in (None, ""):
            return str(sid)
    if ev.get("self_id") not in (None, ""):
        return str(ev.get("self_id"))
    return ctx.state.bot_id


# ---------------------------------------------------------------------------
# 段映射：OneBot → BotSegment
# ---------------------------------------------------------------------------


async def _onebot_to_segments(
    raw_segments: list[dict], ctx: Context, message_id: str
) -> list[BotSegment]:
    out: list[BotSegment] = []
    for seg in raw_segments:
        if not isinstance(seg, dict):
            continue
        bs = await _one_segment(seg, ctx, message_id)
        if bs is not None:
            out.append(bs)
    return out


async def _one_segment(seg: dict, ctx: Context, message_id: str) -> BotSegment | None:
    """OneBot 单段 → BotSegment；解析失败 fallback 到 UnknownSegment。"""
    t = seg.get("type")
    data = seg.get("data") or {}
    try:
        if t == "text":
            text = str(data.get("text", ""))
            return TextSegment(text=text) if text else None

        if t == "image":
            return await _build_image_segment(data, ctx, message_id)

        if t in ("record", "voice"):
            return AudioSegment(
                url=data.get("url") or data.get("file") or None,
                name=str(data.get("file") or ""),
                size=_to_int(data.get("file_size")),
            )

        if t == "video":
            return VideoSegment(
                url=data.get("url") or data.get("file") or None,
                name=str(data.get("file") or ""),
                size=_to_int(data.get("file_size")),
            )

        if t == "file":
            return FileSegment(
                url=data.get("url") or None,
                filename=str(data.get("name") or data.get("file") or ""),
                size=_to_int(data.get("file_size") or data.get("size")),
            )

        if t == "face":
            return FaceSegment(
                face_id=str(data.get("id", "") or ""),
                name=str(data.get("name") or ""),
            )

        if t in ("at", "mention"):
            target = _seg_at_target(seg)
            if target == "all":
                return AtSegment(at_all=True)
            return AtSegment(user_id=target or "")

        if t in ("at_all", "mention_all"):
            return AtSegment(at_all=True)

        if t == "reply":
            mid = str(data.get("id") or data.get("message_id") or "")
            uid = data.get("user_id")
            return ReplySegment(
                message_id=mid,
                user_id=str(uid) if uid not in (None, "") else None,
            )

        if t == "forward":
            return ForwardSegment(
                forward_id=str(data.get("id") or data.get("forward_id") or "") or None,
            )

        if t == "share":
            return ShareSegment(
                url=str(data.get("url") or ""),
                title=str(data.get("title") or ""),
                description=str(data.get("content") or data.get("description") or ""),
                image=data.get("image") or None,
            )

        if t == "contact":
            ctype = str(data.get("type") or "")
            return ContactSegment(
                contact_type="group" if ctype == "group" else "user",
                contact_id=str(data.get("id") or data.get("user_id") or data.get("group_id") or ""),
                name=str(data.get("name") or ""),
            )

        if t == "location":
            return LocationSegment(
                latitude=float(data.get("lat") or 0.0),
                longitude=float(data.get("lon") or 0.0),
                title=str(data.get("title") or ""),
                address=str(data.get("content") or data.get("address") or ""),
            )

        if t == "music":
            return MusicSegment(
                music_platform=str(data.get("type") or "qq"),
                song_id=data.get("id") or None,
                url=data.get("url") or None,
                title=str(data.get("title") or ""),
                artist=str(data.get("artist") or data.get("singer") or ""),
                cover=data.get("image") or None,
            )

        if t == "json":
            payload = data.get("data")
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except json.JSONDecodeError:
                    payload = {"raw": payload}
            return JsonSegment(data=payload if isinstance(payload, dict) else {"raw": payload})

        if t == "poke":
            return PokeSegment(
                user_id=str(data.get("qq") or data.get("user_id") or ""),
                poke_type=str(data.get("type") or "") or None,
            )
    except Exception:
        ctx.logger.exception("napcat_bot: segment parse failed type=%s", t)

    return UnknownSegment(raw=seg)


async def _build_image_segment(data: dict, ctx: Context, message_id: str) -> ImageSegment:
    """图片：优先用段内 url 直接下载；没 url 走 v12 get_file 兜底。"""
    seg_url = data.get("url") or ""
    if not seg_url and isinstance(data.get("file"), str) and data["file"].startswith("http"):
        seg_url = data["file"]

    fallback_name = str(data.get("file") or "")
    width = _to_int(data.get("width") or data.get("pic_width"))
    height = _to_int(data.get("height") or data.get("pic_height"))
    size = _to_int(data.get("file_size") or data.get("size"))

    data_uri: str | None = None
    mime: str | None = None

    if seg_url:
        data_uri, mime = await _download_image_to_data_uri(seg_url, fallback_name, ctx, message_id)
    else:
        file_id = str(data.get("file_id") or data.get("file") or "")
        if file_id:
            data_uri, mime = await _fetch_image_via_get_file(file_id, ctx, message_id)
        else:
            ctx.logger.warning(
                "napcat_bot: image.no_source mid=%s keys=%s", message_id, sorted(data.keys())
            )

    return ImageSegment(
        url=data_uri or seg_url or None,
        name=fallback_name,
        size=size,
        mime=mime or _guess_image_mime_from_url(seg_url, fallback_name),
        width=width,
        height=height,
    )


async def _download_image_to_data_uri(
    url: str, hint_name: str, ctx: Context, message_id: str
) -> tuple[str | None, str | None]:
    """HTTP 直下 → base64 data URI；返回 (data_uri, mime)。失败 (None, None)。"""
    http = ctx.state.http
    if http is None:
        try:
            import httpx
        except ImportError:
            ctx.logger.error("napcat_bot: httpx 未安装，无法下载图片")
            return None, None
        http = httpx.AsyncClient(timeout=ctx.state.image_download_timeout, follow_redirects=True)
        ctx.state.http = http

    try:
        t0 = time.perf_counter()
        resp = await http.get(url)
        resp.raise_for_status()
        content = resp.content
        dt = (time.perf_counter() - t0) * 1000
    except Exception as exc:
        ctx.logger.warning(
            "napcat_bot: image.download_failed mid=%s url=%s err=%s",
            message_id,
            url[:120],
            exc,
        )
        return None, None

    b64 = base64.b64encode(content).decode("ascii")
    ctype = resp.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
    mime = ctype if ctype.startswith("image/") else _guess_image_mime_from_url(url, hint_name)
    ctx.logger.info(
        "napcat_bot: image.downloaded mid=%s in %.1fms bytes=%d mime=%s",
        message_id,
        dt,
        len(content),
        mime,
    )
    return f"data:{mime};base64,{b64}", mime


async def _fetch_image_via_get_file(
    file_id: str, ctx: Context, message_id: str
) -> tuple[str | None, str | None]:
    """v12 兜底：get_file(type=data) 拿 base64。"""
    try:
        resp = await _call_action(ctx, "get_file", {"file_id": file_id, "type": "data"})
    except (TimeoutError, RuntimeError) as exc:
        ctx.logger.warning(
            "napcat_bot: image.get_file_failed mid=%s file_id=%s err=%s",
            message_id,
            file_id,
            exc,
        )
        return None, None
    if resp.get("status") != "ok":
        ctx.logger.warning(
            "napcat_bot: image.get_file_non_ok mid=%s file_id=%s retcode=%s",
            message_id,
            file_id,
            resp.get("retcode"),
        )
        return None, None
    d = resp.get("data") or {}
    b64 = d.get("data") or ""
    if isinstance(b64, (bytes, bytearray)):
        b64 = base64.b64encode(b64).decode("ascii")
    if not isinstance(b64, str) or not b64:
        return None, None
    mime = _guess_image_mime_from_url(str(d.get("name") or ""))
    return f"data:{mime};base64,{b64}", mime


# ---------------------------------------------------------------------------
# 出站：BotEvent → OneBot send_msg params
# ---------------------------------------------------------------------------


def _build_send_msg_params(event: BotEvent) -> dict:
    """BotEvent → send_msg 动作 params（不含 action / echo 字段）。"""
    onebot_segs: list[dict] = []
    for seg in event.message:
        ob = _bot_segment_to_onebot(seg)
        if ob is not None:
            onebot_segs.append(ob)
    if not onebot_segs:
        # 兜底，避免发空消息
        onebot_segs.append({"type": "text", "data": {"text": "..."}})

    if event.session_id.startswith("group:"):
        gid = event.session_id.removeprefix("group:")
        try:
            group_id: Any = int(gid)
        except ValueError:
            group_id = gid
        return {
            "message_type": "group",
            "group_id": group_id,
            "message": onebot_segs,
        }
    if event.session_id.startswith("private:"):
        uid = event.session_id.removeprefix("private:")
        try:
            user_id: Any = int(uid)
        except ValueError:
            user_id = uid
        return {
            "message_type": "private",
            "user_id": user_id,
            "message": onebot_segs,
        }
    raise ValueError(f"napcat_bot: unsupported session_id {event.session_id!r}")


def _bot_segment_to_onebot(seg: BotSegment) -> dict | None:
    t = seg.type
    if t == "Text":
        return {"type": "text", "data": {"text": seg.text}}  # type: ignore[union-attr]
    if t == "Image":
        # 已经是 data: URI 或 http url 都直接传给 OneBot
        url = seg.url  # type: ignore[union-attr]
        if not url:
            return None
        return {"type": "image", "data": {"file": url}}
    if t == "At":
        if seg.at_all:  # type: ignore[union-attr]
            return {"type": "at", "data": {"qq": "all"}}
        return {"type": "at", "data": {"qq": seg.user_id}}  # type: ignore[union-attr]
    if t == "Reply":
        return {"type": "reply", "data": {"id": seg.message_id}}  # type: ignore[union-attr]
    if t == "Face":
        return {"type": "face", "data": {"id": seg.face_id}}  # type: ignore[union-attr]
    if t == "Audio":
        url = seg.url  # type: ignore[union-attr]
        if not url:
            return None
        return {"type": "record", "data": {"file": url}}
    if t == "Video":
        url = seg.url  # type: ignore[union-attr]
        if not url:
            return None
        return {"type": "video", "data": {"file": url}}
    if t == "File":
        url = seg.url  # type: ignore[union-attr]
        if not url:
            return None
        return {"type": "file", "data": {"file": url}}
    if t == "Share":
        return {
            "type": "share",
            "data": {
                "url": seg.url,  # type: ignore[union-attr]
                "title": seg.title,  # type: ignore[union-attr]
                "content": seg.description,  # type: ignore[union-attr]
            },
        }
    if t == "Unknown":
        # 直接转发原始段
        raw = seg.raw  # type: ignore[union-attr]
        if isinstance(raw, dict) and raw.get("type"):
            return raw
        return None
    # 其他段（Forward/Music/Json/Location/Contact/Poke/Node/Nodes）暂不下发
    return None


# ---------------------------------------------------------------------------
# action RPC
# ---------------------------------------------------------------------------


async def _call_action(
    ctx: Context,
    action: str,
    params: dict,
    *,
    timeout: float | None = None,
) -> dict:
    ws = ctx.state.ws
    if ws is None:
        raise RuntimeError("napcat_bot: ws not connected")
    echo = uuid.uuid4().hex
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[dict] = loop.create_future()
    ctx.state.pending_actions[echo] = fut
    payload = json.dumps({"action": action, "params": params, "echo": echo}, ensure_ascii=False)
    try:
        await ws.send(payload)
    except Exception:
        ctx.state.pending_actions.pop(echo, None)
        raise
    try:
        return await asyncio.wait_for(fut, timeout=timeout or ctx.state.action_timeout)
    finally:
        ctx.state.pending_actions.pop(echo, None)


def _on_action_response(resp: dict, ctx: Context) -> None:
    echo = resp.get("echo") or ""
    fut: asyncio.Future[dict] | None = ctx.state.pending_actions.get(echo) if echo else None
    if fut is not None and not fut.done():
        fut.set_result(resp)


def _cancel_pending_actions(ctx: Context, reason: str) -> None:
    pending = ctx.state.pending_actions
    if not pending:
        return
    for echo, fut in list(pending.items()):
        if not fut.done():
            fut.set_exception(RuntimeError(f"action {echo} cancelled: {reason}"))
        pending.pop(echo, None)


# ---------------------------------------------------------------------------
# 协议工具（部分搬自 qq_bot_example.py）
# ---------------------------------------------------------------------------


def _seg_at_target(seg: dict) -> str | None:
    """从 at / mention 段里取出被 @ 用户 ID（字符串），或 'all'。"""
    if seg.get("type") not in ("at", "mention"):
        return None
    data = seg.get("data") or {}
    target = data.get("qq")
    if target in (None, ""):
        target = data.get("user_id")
    if target in (None, ""):
        return None
    return str(target)


def _is_mentioned(
    raw_segments: list[dict],
    raw_text: str,
    self_user_id: str,
    bot_name: str,
) -> bool:
    """命中条件：at/mention 段指向自己，或纯文本中包含 @自身ID / @机器人名。"""
    for seg in raw_segments or []:
        target = _seg_at_target(seg)
        if target == "all":
            continue  # @全体不算单独 @ 机器人
        if target and self_user_id and target == str(self_user_id):
            return True
    haystack = raw_text or ""
    candidates: list[str] = []
    if self_user_id:
        candidates.append(f"@{self_user_id}")
        candidates.append(f"[CQ:at,qq={self_user_id}]")
    if bot_name:
        candidates.append(f"@{bot_name}")
    return any(kw and kw in haystack for kw in candidates)


def _guess_image_mime_from_url(url: str | None, fallback_name: str | None = None) -> str:
    src = (url or "").lower().split("?", 1)[0]
    for suf, mime in (
        (".png", "image/png"),
        (".gif", "image/gif"),
        (".webp", "image/webp"),
        (".bmp", "image/bmp"),
        (".jpeg", "image/jpeg"),
        (".jpg", "image/jpeg"),
    ):
        if src.endswith(suf):
            return mime
    if fallback_name:
        return _guess_image_mime_from_url(fallback_name)
    return "image/jpeg"


def _to_int(v: Any) -> int | None:
    if v is None or v == "":
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        return None
