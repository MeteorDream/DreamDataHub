from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

__all__ = [
    "AtSegment",
    "AudioSegment",
    "BotEvent",
    "BotMessageType",
    "BotSegment",
    "ContactSegment",
    "FaceSegment",
    "FileSegment",
    "ForwardSegment",
    "ImageSegment",
    "JsonSegment",
    "LocationSegment",
    "MusicSegment",
    "NodeSegment",
    "NodesSegment",
    "PokeSegment",
    "ReplySegment",
    "SegmentAdapter",
    "ShareSegment",
    "TextSegment",
    "UnknownSegment",
    "VideoSegment",
]


class BotMessageType(StrEnum):
    # Basic Segment Types
    Text = "Text"  # plain text message
    Image = "Image"  # image
    Video = "Video"  # video
    Audio = "Audio"  # audio
    File = "File"  # file attachment

    # IM-specific Segment Types
    Face = "Face"  # Emoji segment for Tencent QQ platform
    At = "At"  # mention a user in IM apps
    Node = "Node"  # a node in a forwarded message
    Nodes = "Nodes"  # a forwarded message consisting of multiple nodes
    Poke = "Poke"  # a poke message for Tencent QQ platform
    Reply = "Reply"  # a reply message segment
    Forward = "Forward"  # a forwarded message segment
    Share = "Share"
    Contact = "Contact"
    Location = "Location"
    Music = "Music"
    Json = "Json"
    Unknown = "Unknown"


class _SegmentBase(BaseModel):
    """Common config for all segment models."""

    model_config = ConfigDict(extra="forbid")


# ---------- Basic segments ----------


class TextSegment(_SegmentBase):
    type: Literal[BotMessageType.Text] = BotMessageType.Text
    text: str


class ImageSegment(_SegmentBase):
    type: Literal[BotMessageType.Image] = BotMessageType.Image
    url: str | None = None
    name: str = ""
    size: int | None = None
    mime: str | None = None
    width: int | None = None
    height: int | None = None


class AudioSegment(_SegmentBase):
    type: Literal[BotMessageType.Audio] = BotMessageType.Audio
    url: str | None = None
    name: str = ""
    size: int | None = None
    mime: str | None = None
    duration: float | None = None  # seconds


class VideoSegment(_SegmentBase):
    type: Literal[BotMessageType.Video] = BotMessageType.Video
    url: str | None = None
    name: str = ""
    size: int | None = None
    mime: str | None = None
    width: int | None = None
    height: int | None = None
    duration: float | None = None  # seconds


class FileSegment(_SegmentBase):
    type: Literal[BotMessageType.File] = BotMessageType.File
    url: str | None = None
    filename: str = ""
    size: int | None = None
    mime: str | None = None
    hash: str | None = None  # md5 / sha256 / etc.


# ---------- IM-specific segments ----------


class FaceSegment(_SegmentBase):
    type: Literal[BotMessageType.Face] = BotMessageType.Face
    face_id: str  # platform-specific emoji id (e.g. QQ face id)
    name: str = ""  # human-readable label, optional


class AtSegment(_SegmentBase):
    type: Literal[BotMessageType.At] = BotMessageType.At
    user_id: str = ""  # empty when at_all is True
    display_name: str = ""
    at_all: bool = False


class NodeSegment(_SegmentBase):
    type: Literal[BotMessageType.Node] = BotMessageType.Node
    user_id: str
    user_name: str = ""
    time: float | None = None
    content: list[BotSegment] = Field(default_factory=list)


class NodesSegment(_SegmentBase):
    type: Literal[BotMessageType.Nodes] = BotMessageType.Nodes
    nodes: list[NodeSegment] = Field(default_factory=list)


class PokeSegment(_SegmentBase):
    type: Literal[BotMessageType.Poke] = BotMessageType.Poke
    user_id: str
    poke_type: str | None = None  # platform-specific subtype


class ReplySegment(_SegmentBase):
    type: Literal[BotMessageType.Reply] = BotMessageType.Reply
    message_id: str
    user_id: str | None = None  # original sender, when known


class ForwardSegment(_SegmentBase):
    type: Literal[BotMessageType.Forward] = BotMessageType.Forward
    forward_id: str | None = None  # opaque id of the forwarded message chain
    content: list[BotSegment] = Field(default_factory=list)


class ShareSegment(_SegmentBase):
    type: Literal[BotMessageType.Share] = BotMessageType.Share
    url: str
    title: str = ""
    description: str = ""
    image: str | None = None  # preview image url


class ContactSegment(_SegmentBase):
    type: Literal[BotMessageType.Contact] = BotMessageType.Contact
    contact_type: str  # e.g. "user" | "group"
    contact_id: str
    name: str = ""


class LocationSegment(_SegmentBase):
    type: Literal[BotMessageType.Location] = BotMessageType.Location
    latitude: float
    longitude: float
    title: str = ""
    address: str = ""


class MusicSegment(_SegmentBase):
    type: Literal[BotMessageType.Music] = BotMessageType.Music
    music_platform: str  # e.g. "qq", "163", "spotify"
    song_id: str | None = None  # platform song id
    url: str | None = None  # fallback / share url
    title: str = ""
    artist: str = ""
    cover: str | None = None


class JsonSegment(_SegmentBase):
    type: Literal[BotMessageType.Json] = BotMessageType.Json
    data: dict[str, Any] = Field(default_factory=dict)


class UnknownSegment(_SegmentBase):
    type: Literal[BotMessageType.Unknown] = BotMessageType.Unknown
    raw: dict[str, Any] = Field(default_factory=dict)


# ---------- Discriminated union ----------
#
# One type per line so adding/removing a segment is a single-line diff and the
# whole thing fits inside ruff's line-length budget without a noqa escape.

BotSegment = Annotated[
    TextSegment
    | ImageSegment
    | AudioSegment
    | VideoSegment
    | FileSegment
    | FaceSegment
    | AtSegment
    | NodeSegment
    | NodesSegment
    | PokeSegment
    | ReplySegment
    | ForwardSegment
    | ShareSegment
    | ContactSegment
    | LocationSegment
    | MusicSegment
    | JsonSegment
    | UnknownSegment,
    Field(discriminator="type"),
]


# Resolve forward references for recursive segments.
NodeSegment.model_rebuild()
NodesSegment.model_rebuild()
ForwardSegment.model_rebuild()


# Reusable adapter for callers that need to validate a single segment payload
# (e.g. parsing one segment out of a webhook list). `BotSegment` itself is a
# typing construct and cannot be called like a Pydantic model.
SegmentAdapter: TypeAdapter[BotSegment] = TypeAdapter(BotSegment)


class BotEvent(BaseModel):
    # `extra="ignore"` because this object decodes third-party IM webhooks
    # (napcat / OneBot etc.) — upstream may add fields between releases and we
    # don't want a new field to crash the consumer.
    model_config = ConfigDict(extra="ignore")

    # 事件唯一标识符
    id: str
    # 消息平台
    platform: str
    # 事件发生时间（Unix 时间戳，单位：秒）
    time: float
    # 事件类型，必须是 `meta`、`message`、`notice`、`request` 中的一个，
    # 分别表示元事件、消息事件、通知事件和请求事件
    type: str
    # 事件详细类型
    detail_type: str
    # 事件子类型（详细类型的下一级类型）
    sub_type: str
    # 消息id
    message_id: str
    # 消息段列表
    message: list[BotSegment] = Field(default_factory=list)

    # 事件来源 bot 的 id
    bot_id: str
    # 事件用户ID
    user_id: str
    user_name: str
    # 会话消息
    session_id: str
    session_name: str

    # 引用的其他消息 id（可选）—— 当前消息 quote 了哪条消息；不在 OneBot 标准
    # 协议里，属于 hub 内部的扩展字段，用于跨消息追溯。None 表示无引用。
    quote_id: str | None = None
    # 补充载荷（可选）—— schema 自由的 JSON dict，存平台特有或后续可能补的信息。
    # None 表示无补充；不该塞进 message 段的信息就放这里。
    addition: dict[str, Any] | None = None


BotEvent.model_rebuild()
