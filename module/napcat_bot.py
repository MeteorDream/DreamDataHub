"""napcat_bot — napcat 正向 WebSocket 接入模块（骨架）。

当前仅实现生命周期：
- ``on_startup``：读取配置、初始化 state、启动 WS 收帧循环
- ``on_shutdown``：取消 pending 动作、关闭 WS 与 HTTP 客户端

后续再补：入站帧解析、段映射、出站订阅、action RPC 等。
参考实现见 ``module/im_qq.py``。
"""

from __future__ import annotations

from hub import Context, Module

mod = Module("napcat_bot")


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

    ctx.state.ws = None
    ctx.state.pending_actions = {}
    ctx.state.http = None

    ctx.logger.info(
        "napcat_bot: startup ws_url=%s whitelist groups=%d users=%d",
        ctx.state.ws_url,
        len(ctx.state.whitelist_groups),
        len(ctx.state.whitelist_users),
    )


@mod.on_shutdown
async def teardown(ctx: Context) -> None:
    pending = ctx.state.pending_actions
    if pending:
        for echo, fut in list(pending.items()):
            if not fut.done():
                fut.set_exception(RuntimeError(f"action {echo} cancelled: shutdown"))
            pending.pop(echo, None)

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
