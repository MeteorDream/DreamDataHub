# DreamDataHub
Dream's Personal Data Hub

## Bot 消息通信协议

`message/bot.py` 定义了一套与 IM 平台（QQ / napcat / OneBot 等）解耦的消息通信协议，用 [Pydantic v2](https://docs.pydantic.dev/) 的 **判别联合（discriminated union）** 描述每一段消息的结构。所有公开符号通过 `__all__` 显式导出。

### 总体结构

一次外部事件（webhook 推送、回调）被建模为一个 `BotEvent`，其 `message` 字段是一个**消息段列表** `list[BotSegment]`：

```
BotEvent
├── id / platform / time / type / detail_type / sub_type
├── message_id
├── message: list[BotSegment]   ← 消息段列表（按 type 字段判别）
├── bot_id
├── user_id / user_name
└── session_id / session_name
```

`BotSegment` 是 18 种具体段类型的判别联合：

```python
BotSegment = Annotated[
    PlainSegment | ImageSegment | AudioSegment | VideoSegment | FileSegment
    | FaceSegment | AtSegment | NodeSegment | NodesSegment | PokeSegment
    | ReplySegment | ForwardSegment | ShareSegment | ContactSegment
    | LocationSegment | MusicSegment | JsonSegment | UnknownSegment,
    Field(discriminator="type"),
]
```

反序列化时，Pydantic 会按 `type` 字段把 dict 自动分发到对应的段类。

### `BotEvent` 顶层字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | `str` | 事件唯一标识 |
| `platform` | `str` | 上游平台名（如 `napcat`、`onebot`） |
| `time` | `float` | Unix 时间戳（秒） |
| `type` | `str` | 事件大类：`meta` / `message` / `notice` / `request` |
| `detail_type` | `str` | 事件细分类型（如 `private`、`group`） |
| `sub_type` | `str` | 子类型（如 `normal`、`anonymous`） |
| `message_id` | `str` | 消息 id |
| `message` | `list[BotSegment]` | 消息段列表 |
| `bot_id` | `str` | 接收事件的 bot id |
| `user_id` / `user_name` | `str` / `str` | 触发用户 |
| `session_id` / `session_name` | `str` / `str` | 会话标识 |

`BotEvent` 设置了 `extra="ignore"`：上游协议升级新增字段不会让消费者炸掉。

### 消息段类型（`BotSegment`）

所有段都继承自一个 `extra="forbid"` 的基类，每段都带一个 `Literal` 类型的 `type` 判别字段。

#### 基础多媒体段

| 段类型 | `type` 值 | 主要字段 |
|---|---|---|
| `PlainSegment` | `Plain` | `text` |
| `ImageSegment` | `Image` | `url` / `name` / `size` / `mime` / `width` / `height` |
| `AudioSegment` | `Audio` | `url` / `name` / `size` / `mime` / `duration` |
| `VideoSegment` | `Video` | `url` / `name` / `size` / `mime` / `width` / `height` / `duration` |
| `FileSegment` | `File` | `url` / `filename` / `size` / `mime` / `hash` |

`size`、`mime`、`width/height`、`duration` 等字段使用 `None` 表示"未知/不适用"，避免用 `0` / 空字符串这种魔术值。

#### IM 专用段

| 段类型 | `type` 值 | 主要字段 |
|---|---|---|
| `FaceSegment` | `Face` | `face_id` / `name` |
| `AtSegment` | `At` | `user_id` / `display_name` / `at_all` |
| `PokeSegment` | `Poke` | `user_id` / `poke_type` |
| `ReplySegment` | `Reply` | `message_id` / `user_id` |
| `ShareSegment` | `Share` | `url` / `title` / `description` / `image` |
| `ContactSegment` | `Contact` | `contact_type` / `contact_id` / `name` |
| `LocationSegment` | `Location` | `latitude` / `longitude` / `title` / `address` |
| `MusicSegment` | `Music` | `music_platform` / `song_id` / `url` / `title` / `artist` / `cover` |

#### 嵌套 / 转发段（递归结构）

| 段类型 | `type` 值 | 说明 |
|---|---|---|
| `NodeSegment` | `Node` | 转发消息中的一个节点，含 `user_id` / `user_name` / `time` / `content: list[BotSegment]` |
| `NodesSegment` | `Nodes` | 多节点转发集合，`nodes: list[NodeSegment]` |
| `ForwardSegment` | `Forward` | 转发消息引用，`forward_id` / `content: list[BotSegment]` |

`Node` / `Forward` 内嵌的 `content` 是 `list[BotSegment]`，因此可以无限层嵌套。

#### 兜底段

| 段类型 | `type` 值 | 说明 |
|---|---|---|
| `JsonSegment` | `Json` | 平台原生卡片 / JSON payload，`data: dict[str, Any]` |
| `UnknownSegment` | `Unknown` | 协议中暂未建模的段，原始 payload 保留在 `raw: dict[str, Any]` 以便回放 / 转发 |

### 序列化示例

```json
{
  "id": "b6e65187-5ac0-489c-b431-53078e9d2bbb",
  "platform": "napcat",
  "time": 1780837539.834,
  "type": "message",
  "detail_type": "private",
  "sub_type": "normal",
  "message_id": "191486285",
  "message": [
    { "type": "Reply",  "message_id": "191486284" },
    { "type": "At",     "user_id": "12312432", "display_name": "bot" },
    { "type": "Plain",  "text": " 你好,帮我看看这张图" },
    {
      "type": "Image",
      "url": "https://example.com/img.png",
      "name": "img.png",
      "size": 20480,
      "mime": "image/png",
      "width": 1024,
      "height": 768
    }
  ],
  "bot_id": "12312432",
  "user_id": "eraser",
  "user_name": "aesrawer",
  "session_id": "ewrwserase",
  "session_name": "dfsfasd"
}
```

### 使用方式

#### 解析整个事件

```python
from message.bot import BotEvent

event = BotEvent.model_validate(payload)         # payload: dict（来自 webhook）
for seg in event.message:
    match seg.type:
        case "Plain":
            print(seg.text)
        case "At":
            print("at user:", seg.user_id, "all=", seg.at_all)
        case "Image":
            print("image url:", seg.url)
        # ...
```

判别联合让 `seg` 的类型在 `match` 分支里被静态推断为对应的具体段类，IDE 自动补全准确。

#### 解析 / 校验单个段

```python
from message.bot import SegmentAdapter

seg = SegmentAdapter.validate_python({"type": "Plain", "text": "hi"})
# seg: PlainSegment
```

`BotSegment` 本身只是类型构造，不能直接当 Pydantic 模型调用；用 `SegmentAdapter`（`TypeAdapter(BotSegment)`）来对单个段做反序列化 / `dump_python` / `dump_json`。

#### 构造事件

```python
from message.bot import BotEvent, PlainSegment, AtSegment, ReplySegment

event = BotEvent(
    id="...", platform="napcat", time=..., type="message",
    detail_type="group", sub_type="normal", message_id="...",
    message=[
        ReplySegment(message_id="..."),
        AtSegment(user_id="12345"),
        PlainSegment(text="你好"),
    ],
    bot_id="...", user_id="...", user_name="...",
    session_id="...", session_name="...",
)
event.model_dump_json(indent=2)
```

### 设计要点

- **判别联合**：所有段共享 `type: Literal[BotMessageType.X]`，`BotSegment` 用 `Field(discriminator="type")` 让 Pydantic 在 list 中按 `type` 字段直接分发到对应类，不必手写工厂。
- **类型特定字段**：`At` 不再用 `name` 表示被 at 用户、`File` 不再用 `name` 表示文件名；每个段只暴露和它语义相关的字段。
- **`None` 表示"未知"**：`size`/`mime`/`width`/`duration` 等可选字段统一用 `None` 默认值，杜绝 `0` / `""` 这种语义二义。
- **递归段**：`Node` / `Forward` 通过 `from __future__ import annotations` + `model_rebuild()` 支持任意嵌套。
- **兼顾上游协议演进**：`BotEvent` 用 `extra="ignore"` 容忍 webhook 新字段；段模型用 `extra="forbid"` 在内部代码中尽早暴露字段拼写错误。
- **未知段保留原 payload**：协议层来不及建模的新段落入 `UnknownSegment`，原始 dict 留在 `raw` 字段，可继续转发 / 回放。
