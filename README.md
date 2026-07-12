# DreamDataHub

Dream's Personal Data Hub — 一个基于 asyncio 的**微内核事件总线**，把 IM 网关（QQ / Telegram）、LLM、数据库、天气等能力当作可插拔"模块"接入同一个 Hub，通过 topic 广播 + Capability RPC 组合成完整业务链。

```
┌───────────┐  im.message   ┌─────────────────┐  invoke     ┌──────────────┐
│  im_qq    │──────────────▶│ weather_assistant│───────────▶│  llm_openai  │
│ telegram  │               │  (编排 module)   │            │  (LLMChat)   │
└───────────┘◀─── im.reply ─┴─────────────────┴──── invoke ─┴──────────────┘
                                                              weather
```

## Hub 抽象层

Hub 只有三个原语：

- **Module** — 插件单元，每个 `module/<name>.py` 顶层声明**唯一一个** `Module` 实例
- **Topic** — 事件通道 marker class，集中定义在 `topics/` 目录（一个领域一个文件）；`publish` 广播、多订阅者并发处理，Payload 走 Pydantic 契约校验
- **Capability** — 类型化的点对点 RPC 契约（marker class + Pydantic `Params` / `Result`）

三者语义严格分开：
- Topic 是**广播**（无返回值，多订阅者），适合"A 发生了 X，任何关心的都可以响应"
- Capability 是 **RPC**（有返回值，唯一实现），适合"A 需要 B 给个结果才能继续"
- Module 内可同时订阅 topic + 提供 capability + 依赖别的 capability

**Topic vs Capability 的归属对称**：Capability 由声明方 = 唯一实现方持有，marker class 就近声明在实现模块里；Topic 是任何模块都可以发 / 收的中立通道，marker class 集中在 `topics/` 目录，不属于任何单个模块。

### 目录结构

```
hub/                    # 微内核：Hub / Module / Context / Topic / Capability 基类
├── topic.py            #   Topic 基类
├── capabilities.py     #   Capability 基类
├── core.py             #   Hub 主对象
├── module.py           #   Module 装饰器工厂
├── context.py          #   模块运行时门面
└── loader.py           #   模块加载 + 依赖拓扑排序

topics/                 # 所有 Topic 契约（跨模块通道，一个领域一个文件）
├── system.py           #   SystemReady / SystemHeartbeat / SystemError
├── im.py               #   IMMessage / IMReply / PLATFORM_TOPICS
├── qq.py               #   QQMessage / QQReply
├── telegram.py         #   TelegramMessage / TelegramReply
├── database.py         #   DatabaseWrite
└── llm.py              #   LLMExchange

message/                # 消息协议模型（Pydantic BotEvent + DB 表 schema）
├── bot.py
└── db.py

services/               # 外部服务客户端（不依赖 hub，可独立使用）
├── weibo.py            #   Weibo 类（微博 Web API 客户端 + QR 登录）
└── weather/            #   多 provider 天气客户端
    ├── base.py         #     WeatherProvider ABC + LocationData/ForecastData 统一模型
    ├── amap.py         #     AMapProvider 具体实现
    ├── formatter.py    #     消息格式化（HTML / MarkdownV2 / 纯文本）
    └── __init__.py     #     PROVIDERS registry（加新 provider 在这里注册）

module/                 # 具体业务模块（一个 Module 实例一个文件）
├── heartbeat.py / echo.py
├── im_qq.py / telegram_bot.py
├── llm_openai.py       #   Capability: LLMChatService
├── weather.py          #   Capability: WeatherForecast / WeatherLocation（provider 分发到 services/weather/）
├── weather_assistant.py#   编排：subscribe IMMessage, invoke LLM + weather
└── mysql.py
```

**层次约定**：`module/` 使用 `services/`，反之不成立。Module 层负责 hub 生命周期
（config → Config 对象 → client 实例 → 挂到 ctx.state），services 层是纯客户端库。

### 声明一个 Module

```python
# module/my_module.py
from hub import Context, Module
from topics.im import IMMessage, IMReply

mod = Module("my_module")

@mod.on_startup
async def setup(ctx: Context) -> None:
    ctx.state.counter = 0            # 模块状态挂在 ctx.state (SimpleNamespace)

@mod.on(IMMessage)                   # 订阅 topic（传 Topic 类）
async def handle(ctx: Context, event) -> None:
    ctx.state.counter += 1
    await ctx.publish(IMReply, ...)  # 发另一个 topic

@mod.on_shutdown
async def teardown(ctx: Context) -> None:
    ctx.logger.info("bye, saw %d msgs", ctx.state.counter)
```

启用模块只要在 `config.toml` 里加一段：

```toml
[module.my_module]
enabled = true
# ... 模块自定义字段
```

### 声明一个 Topic

Topic 用 marker class 承载 `name` / `Payload` / `description` 三元组，marker class 本身作为注册键，同时是 IDE 类型锚点。**所有 Topic 都放在 `topics/` 目录**——因为按定义 Topic 就是跨模块通道，没有"单模块 own"这种东西。

```python
# topics/my_domain.py
from typing import ClassVar
from pydantic import BaseModel
from hub import Topic

class MyEventPayload(BaseModel):
    session_id: str
    text: str

class MyEvent(Topic):
    name: ClassVar[str] = "my.event"
    description: ClassVar[str] = "示例事件"
    Payload: ClassVar[type[BaseModel]] = MyEventPayload
```

Hub 在 `publish` 时会对 payload 做 `Payload.model_validate` 校验，契约违反立即抛 `pydantic.ValidationError`——订阅方拿到的 payload 一定是 `Payload` 实例。

### 声明与实现一个 Capability

Capability 用 marker class 承载 `name` / `Params` / `Result` 三元组，**就近声明在实现模块里**（声明方 = 唯一实现方，天然绑定）。

```python
# module/llm_openai.py（节选）
from typing import ClassVar
from pydantic import BaseModel
from hub import Capability, Context, Module

class LLMChatParams(BaseModel):
    messages: list[dict[str, str]]
    model: str | None = None

class LLMChatResult(BaseModel):
    reply: str
    model: str

class LLMChatService(Capability):
    name: ClassVar[str] = "llm.chat"
    Params: ClassVar[type[BaseModel]] = LLMChatParams
    Result: ClassVar[type[BaseModel]] = LLMChatResult

mod = Module("llm_openai")

@mod.provides(LLMChatService)
async def chat(ctx: Context, params: LLMChatParams) -> LLMChatResult:
    resp = await ctx.state.client.chat.completions.create(
        model=params.model or ctx.state.model,
        messages=params.messages,
    )
    return LLMChatResult(reply=resp.choices[0].message.content or "", model=params.model or ctx.state.model)
```

Hub 在注册时会检查 marker class 全局唯一；在调用时会对 `Params` / `Result` 双向做 `model_validate`，契约违反立即报错。

### 调用别的 Capability + 声明依赖

模块通过 `ctx.invoke(MarkerClass, ParamsInstance)` 调用其他模块的能力。**必须**通过构造参数 `requires=[...]` 显式声明，loader 会做严格校验。

```python
# module/weather_assistant.py（节选）
from hub import Context, Module
from topics.im import IMMessage, IMReply
from module.llm_openai import LLMChatService, LLMChatParams
from module.weather import WeatherForecastService, WeatherForecastParams

mod = Module(
    "weather_assistant",
    requires=[LLMChatService, WeatherForecastService],   # 显式声明
)

@mod.on(IMMessage)
async def entry(ctx: Context, event) -> None:
    intent = await ctx.invoke(
        LLMChatService,
        LLMChatParams(messages=[...]),
    )
    # intent 是 LLMChatResult 实例，IDE 能补全 .reply
    if not is_weather_query(intent.reply):
        return
    forecast = await ctx.invoke(
        WeatherForecastService,
        WeatherForecastParams(adcode="440305"),
    )
    reply = await ctx.invoke(LLMChatService, LLMChatParams(messages=[...]))
    await ctx.publish(IMReply, build_reply_event(reply.reply))
```

### 依赖校验与拓扑排序（严格模式）

Loader 加载模块清单时会：

1. **收集所有 provides**：`provider: Capability → Module` 反向索引
2. **严格依赖校验**：任一模块的 `requires` 未被覆盖 → `RuntimeError` 拒绝启动
3. **Kahn 拓扑排序**：`provider → requirer` 有向图，保证提供者先于依赖者启动
4. **循环依赖检测**：图有环 → `RuntimeError`

配置里模块顺序可以任意，启动序由拓扑决定。示例日志：

```
[INFO] load order (topological): heartbeat -> llm_openai -> weather -> weather_assistant
[INFO] hub: topic subscriptions (5 topics):
[INFO]   im.message        (IMMessage      )  [3 subs]  <- [echo, llm_openai, weather_assistant]
[INFO]   im.reply          (IMReply        )  [1 sub]   <- [echo]
[INFO]   system.error      (SystemError    )  [1 sub]   <- [heartbeat]
[INFO]   system.heartbeat  (SystemHeartbeat)  [1 sub]   <- [heartbeat]
[INFO]   system.ready      (SystemReady    )  [2 subs]  <- [echo, heartbeat]
```

### 错误处理约定

- **Topic handler 抛异常**：Hub 捕获 + 记 log + 转发一条 `SystemError` 事件，**不影响其他订阅者**
- **Publish payload 契约违反**：`ctx.publish` 抛 `pydantic.ValidationError`，不会静默广播错数据
- **Capability invoke 抛异常**：直接冒泡到调用方，调用方自己决定重试 / 降级
- **Capability params/result 契约违反**：`ctx.invoke` 抛 `pydantic.ValidationError`
- **未注册能力**：`ctx.invoke` 抛 `CapabilityNotFoundError`

### Context API 速查

| 方法 | 语义 |
|---|---|
| `await ctx.publish(Topic, payload)` | 广播事件（payload 类型校验后立即返回） |
| `await ctx.invoke(Cap, params)` | RPC 调其他模块能力，返回 `Cap.Result` 实例 |
| `ctx.spawn(coro, name=...)` | 注册后台长任务，shutdown 时统一取消 |
| `ctx.state` | 模块私有状态（`SimpleNamespace`） |
| `ctx.config` | 该模块的 config 字典（来自 `[module.<name>]`） |
| `ctx.logger` | 命名为 `module.<name>` 的 logger |
| `ctx.hub_event` | shutdown 信号的 `asyncio.Event`，长任务循环里用来退出 |

### 多 provider 抽象（services/weather 为例）

某些 module 只是**外部服务的门面**——真正的 API 客户端放在 `services/` 目录里，
Module 只做"config 翻译 + 生命周期管理 + Capability provides"。

**Weather 的多 provider 结构**：

```
services/weather/
├── base.py         # WeatherProvider ABC + LocationData/ForecastData 统一模型
├── amap.py         # AMapProvider + AMapConfig
└── __init__.py     # PROVIDERS = {"amap": (AMapProvider, AMapConfig), ...}
```

`module/weather.py` 里：

```python
provider_name = cfg.get("provider", "amap")
provider_cls, config_cls = PROVIDERS[provider_name]
provider_cfg = config_cls.model_validate(cfg.get(provider_name) or {})
ctx.state.provider = provider_cls(provider_cfg)
```

TOML 里：

```toml
[module.weather]
provider = "amap"

[module.weather.amap]
key = "your_amap_key"
```

**加新 provider 三步**：
1. 建 `services/weather/<name>.py`，实现 `class <Name>Provider(WeatherProvider)` +
   `class <Name>Config(WeatherProviderConfig)`
2. 在 `services/weather/__init__.py:PROVIDERS` 里加一行
3. TOML 里改 `provider = "<name>"` + 加 `[module.weather.<name>]` 段

**关键设计**：所有 provider 的 `forecast()` / `location()` 返回统一的
`ForecastData` / `LocationData`（Pydantic 模型），调用方不感知 provider 差异。
展示层 `services/weather/formatter.py:build_weather_message()` 也接受统一模型。

### 现有内置模块

| 模块 | 订阅 topic | 提供 capability | 说明 |
|---|---|---|---|
| `heartbeat` | `SystemReady` / `SystemHeartbeat` / `SystemError` | — | 系统心跳，定时发 `SystemHeartbeat` |
| `im_qq` | `IMReply` / `QQReply` | — | OneBot v11/napcat WebSocket，跨平台 `IMMessage` 双发 |
| `telegram_bot` | `TelegramReply` | — | python-telegram-bot 22.x |
| `llm_openai` | `IMMessage` | `LLMChatService` | OpenAI 兼容接口；顺带发布 `LLMExchange` |
| `weather` | — | `WeatherForecastService` / `WeatherLocationService` | provider 可切换（amap / baidumap / xinzhi ...），实现在 `services/weather/` |
| `mysql` | `DatabaseWrite` | — | aiomysql 池，按 Pydantic model 自动 DDL |
| `weather_assistant` | `IMMessage` | — | 编排：意图判断 → 查天气 → 生成回复 |
| `echo` | `SystemReady` / `IMMessage` / `IMReply` | — | 冒烟模块 |

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
    TextSegment | ImageSegment | AudioSegment | VideoSegment | FileSegment
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
| `TextSegment` | `Text` | `text` |
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
    { "type": "Text",  "text": " 你好,帮我看看这张图" },
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
        case "Text":
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

seg = SegmentAdapter.validate_python({"type": "Text", "text": "hi"})
# seg: TextSegment
```

`BotSegment` 本身只是类型构造，不能直接当 Pydantic 模型调用；用 `SegmentAdapter`（`TypeAdapter(BotSegment)`）来对单个段做反序列化 / `dump_python` / `dump_json`。

#### 构造事件

```python
from message.bot import BotEvent, TextSegment, AtSegment, ReplySegment

event = BotEvent(
    id="...", platform="napcat", time=..., type="message",
    detail_type="group", sub_type="normal", message_id="...",
    message=[
        ReplySegment(message_id="..."),
        AtSegment(user_id="12345"),
        TextSegment(text="你好"),
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
