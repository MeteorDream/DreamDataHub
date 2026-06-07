from enum import StrEnum

from pydantic import BaseModel


class BotMessageType(StrEnum):
    # Basic Segment Types
    Plain = "Plain"  # plain text message
    Image = "Image"  # image
    Audio = "Audio"  # audio
    Video = "Video"  # video
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


class BotMessage(BaseModel):
    type: BotMessageType
    text: str
    name: str = ""
    url: str = ""
    size: int = 0


class BotEvent(BaseModel):
    # 事件唯一标识符
    id: str
    # 消息平台
    platform: str
    # 事件发生时间（Unix 时间戳）
    time: float
    # 事件类型，必须是 `meta`、`message`、`notice`、`request` 中的一个，分别表示元事件、消息事件、通知事件和请求事件
    type: str
    # 事件详细类型
    detail_type: str
    # 事件子类型（详细类型的下一级类型）
    sub_type: str
    # 消息id
    message_id: str
    # 消息内容(json 格式)
    message: list[BotMessage]

    # 事件来源 bot 的 id
    bot_id: str
    # 事件用户ID
    user_id: str
    user_name: str
    # 会话消息
    session_id: str
    session_name: str


if __name__ == "__main__":
    event = BotEvent(
        id="b6e65187-5ac0-489c-b431-53078e9d2bbb",
        platform="napcat",
        time=1780837539.8344839,
        type="message",
        detail_type="private",
        sub_type="normal",
        message_id="191486285",
        message=[BotMessage(type=BotMessageType.Plain, text="", url="", size=0)],
        bot_id="12312432",
        user_id="eraser",
        user_name="aesrawer",
        session_id="ewrwserase",
        session_name="dfsfasd",
    )
