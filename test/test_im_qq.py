"""im_qq 单元测试 — 协议解析、@ 检测、出站段反向映射。

不联网；不依赖 websockets/httpx（图片下载路径被 monkeypatch 短路）。
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from types import SimpleNamespace

from message.bot import (
    AtSegment,
    BotEvent,
    ImageSegment,
    PlainSegment,
    ReplySegment,
)
from module import im_qq


REPO_ROOT = Path(__file__).resolve().parent.parent


# 把 _download_image_to_data_uri 短路掉，避免单元测试真的去拉 multimedia.nt.qq.com.cn
async def _no_download(url, hint_name, ctx, message_id):  # noqa: ARG001
    return None, None


im_qq._download_image_to_data_uri = _no_download  # type: ignore[assignment]


REPO_ROOT = Path(__file__).resolve().parent.parent


def _make_ctx(**overrides) -> SimpleNamespace:
    """构造一个最小 ctx 替身，足以喂给 im_qq 的纯函数路径。

    我们不用真实 hub.context.Context，因为这里测试的是不需要 publish/spawn 的纯函数。
    """
    state = SimpleNamespace(
        ws_url="ws://127.0.0.1:3001",
        access_token=None,
        bot_id="1228531751",
        bot_name="bot",
        whitelist_groups={"1098814820"},
        whitelist_users=set(),
        reconnect_interval=3.0,
        action_timeout=30.0,
        ws_max_size=2**24,
        supported_image_mimes={"image/jpeg", "image/png", "image/webp"},
        image_download_timeout=30.0,
        ws=None,
        pending_actions={},
        http=None,
    )
    state.__dict__.update(overrides)
    return SimpleNamespace(
        state=state,
        config={},
        logger=logging.getLogger("test.im_qq"),
        publish=None,
        spawn=None,
    )


# ---------------------------------------------------------------------------
# fixture：仓库根目录的真实 NapCat 帧
# ---------------------------------------------------------------------------


def _load_sample(name: str) -> dict:
    return json.loads((REPO_ROOT / name).read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# 入站解析
# ---------------------------------------------------------------------------


def test_parse_data_json_image_message() -> None:
    """data.json：群消息 + 单段 image。下载会失败（无 httpx 或网络），
    但解析仍应得到 ImageSegment（url 字段保留），session_id/sub_type 正确。"""
    ev = _load_sample("data.json")
    ctx = _make_ctx()

    segments = asyncio.run(
        im_qq._onebot_to_segments(ev["message"], ctx, str(ev["message_id"]))
    )

    assert len(segments) == 1
    assert isinstance(segments[0], ImageSegment)
    # 下载失败时仍至少保留 mime guess + 原始 url 兜底
    assert segments[0].mime in {"image/jpeg", "image/png", "image/webp", "image/gif", "image/bmp"}


def test_parse_data2_json_reply_and_text() -> None:
    """data2.json：reply + text 段。解析得 [ReplySegment, PlainSegment]，
    raw_message 里的 [CQ:reply,id=...] 不会重复出现。"""
    ev = _load_sample("data2.json")
    ctx = _make_ctx()

    segments = asyncio.run(
        im_qq._onebot_to_segments(ev["message"], ctx, str(ev["message_id"]))
    )

    assert len(segments) == 2
    assert isinstance(segments[0], ReplySegment)
    assert segments[0].message_id == "574064206"
    assert isinstance(segments[1], PlainSegment)
    assert segments[1].text == "引用"


def test_napcat_raw_field_is_ignored_by_botevent() -> None:
    """NapCat 帧含 raw 字段（NT QQ 内部数据），BotEvent.extra='ignore' 应该接住，
    不影响构造。"""
    ev = _load_sample("data.json")
    ctx = _make_ctx()
    segments = asyncio.run(
        im_qq._onebot_to_segments(ev["message"], ctx, str(ev["message_id"]))
    )
    # 直接构造一个 BotEvent，确保字段都能填上
    e = BotEvent(
        id=str(ev["message_id"]),
        platform="qq",
        time=float(ev.get("time", 0)),
        type="message",
        detail_type=ev["message_type"],
        sub_type="overhear",
        message_id=str(ev["message_id"]),
        message=segments,
        bot_id=str(ev["self_id"]),
        user_id=str(ev["user_id"]),
        user_name=ev["sender"]["nickname"],
        session_id=f"group:{ev['group_id']}",
        session_name=ev["group_name"],
    )
    assert e.session_id == "group:1098814820"
    assert e.user_name == "梦蝶"


# ---------------------------------------------------------------------------
# @ 检测 — sub_type 标注的核心
# ---------------------------------------------------------------------------


def test_is_mentioned_by_at_segment() -> None:
    segs = [
        {"type": "at", "data": {"qq": "1228531751"}},
        {"type": "text", "data": {"text": " 在么"}},
    ]
    assert im_qq._is_mentioned(segs, "[CQ:at,qq=1228531751] 在么", "1228531751", "bot")


def test_is_mentioned_by_bot_name_keyword() -> None:
    segs = [{"type": "text", "data": {"text": "@cat 在干嘛"}}]
    assert im_qq._is_mentioned(segs, "@cat 在干嘛", "1228531751", "cat")


def test_is_mentioned_at_all_does_not_count() -> None:
    segs = [{"type": "at", "data": {"qq": "all"}}]
    assert not im_qq._is_mentioned(segs, "[CQ:at,qq=all]", "1228531751", "bot")


def test_overhear_when_no_at() -> None:
    """data.json 是用户自己发的图片，没 @ 任何人 → 应判为 overhear。"""
    ev = _load_sample("data.json")
    raw_segs = ev["message"]
    raw_text = ev.get("raw_message") or ""
    assert not im_qq._is_mentioned(raw_segs, raw_text, str(ev["self_id"]), "bot")


# ---------------------------------------------------------------------------
# 出站：BotEvent → send_msg params
# ---------------------------------------------------------------------------


def test_build_send_group_msg() -> None:
    event = BotEvent(
        id="r1", platform="qq", time=0.0, type="message",
        detail_type="group", sub_type="", message_id="r1",
        message=[
            ReplySegment(message_id="123"),
            AtSegment(user_id="2423428733"),
            PlainSegment(text=" 你好喵～"),
        ],
        bot_id="1228531751", user_id="1228531751", user_name="bot",
        session_id="group:1098814820", session_name="の、梦蝶",
    )
    params = im_qq._build_send_msg_params(event)
    assert params["message_type"] == "group"
    assert params["group_id"] == 1098814820
    types = [s["type"] for s in params["message"]]
    assert types == ["reply", "at", "text"]
    assert params["message"][0]["data"]["id"] == "123"
    assert params["message"][1]["data"]["qq"] == "2423428733"
    assert params["message"][2]["data"]["text"] == " 你好喵～"


def test_build_send_private_msg() -> None:
    event = BotEvent(
        id="r2", platform="qq", time=0.0, type="message",
        detail_type="private", sub_type="", message_id="r2",
        message=[PlainSegment(text="hi")],
        bot_id="1228531751", user_id="1228531751", user_name="bot",
        session_id="private:2423428733", session_name="梦蝶",
    )
    params = im_qq._build_send_msg_params(event)
    assert params["message_type"] == "private"
    assert params["user_id"] == 2423428733
    assert params["message"] == [{"type": "text", "data": {"text": "hi"}}]


def test_build_send_unsupported_session_raises() -> None:
    event = BotEvent(
        id="r3", platform="echo", time=0.0, type="message",
        detail_type="private", sub_type="", message_id="r3",
        message=[PlainSegment(text="hi")],
        bot_id="0", user_id="0", user_name="echo",
        session_id="echo:self", session_name="echo",
    )
    try:
        im_qq._build_send_msg_params(event)
    except ValueError as exc:
        assert "echo:self" in str(exc)
    else:
        raise AssertionError("expected ValueError for non-QQ session_id")
