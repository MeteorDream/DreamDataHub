# Workflow 架构设计

## 1. 核心认知修正

### 1.1 Workflow 是什么

> **Workflow 就是一个"模块"——它拥有模块的一切特性（Context、config、生命周期），但它的核心不是监听 topic，而是执行一段"流程编排"。**

### 1.2 Workflow 与普通 Module 的关系

```
Module —— 提供能力的单元（能力提供方）
  ├── @mod.on(topic)        → 被动响应事件
  ├── @mod.on_startup       → 启动任务
  ├── @mod.on_shutdown      → 关闭清理
  └── @mod.provides("xxx")  → 声明能力（被 workflow 或其他模块调用）

Workflow —— 编排能力的单元（流程组织者）
  ├── 订阅 topic 触发       → 像 @mod.on 一样监听
  ├── 被模块主动调用触发     → 通过 ctx.start_workflow()
  ├── 内部通过 ctx.invoke()  → 调用其他模块的 provides 能力
  └── 拥有完整的生命周期     → 像模块一样有 startup/shutdown/config
```

**关键1：Workflow 不是一个 Step 容器，它本身就是一个像 Module 一样的实体——有 name、有 config、有生命周期，只不过它的 handlers 是"流程编排函数"。**

**关键2：Workflow 是一个"动态编排单元"——它内部用 Python 代码（不是配置）编排能力调用，可以根据步骤 A 的结果决定是否调用 B 还是 C。**

---

## 2. Module 能力声明（Capability）

### 2.1 能力声明装饰器

在 `hub/module.py` 中新增：

```python
class Module:
    def __init__(self, name: str):
        ...
        self._provides: dict[str, Handler] = {}

    def provides(self, capability: str) -> Callable[[Handler], Handler]:
        """声明模块提供的能力，可被 workflow 或其他模块通过 ctx.invoke() 调用

        用法:
            @mod.provides("weather.forecast")
            async def my_handler(ctx: Context, params: Any) -> Any:
                ...
        """
        def decorator(fn: Handler) -> Handler:
            self._provides[capability] = fn
            return fn
        return decorator

    @property
    def capabilities(self) -> dict[str, Handler]:
        return dict(self._provides)
```

**模块示例：**

```python
# module/weather.py
@mod.provides("weather.forecast")
async def forecast_capability(ctx: Context, params: dict) -> dict:
    _, data = await Weather.amap_weather(params["adcode"])
    return {"forecasts": data, "city": params.get("city")}

@mod.provides("weather.location")
async def location_capability(ctx: Context, params: dict) -> dict:
    msg, data = await Weather.amap_location(params["longitude"], params["latitude"])
    if msg:
        raise RuntimeError(msg)
    return data

# module/llm_openai.py
@mod.provides("llm.chat")
async def chat_capability(ctx: Context, params: dict) -> dict:
    resp = await ctx.state.client.chat.completions.create(
        model=params.get("model", ctx.state.model),
        messages=params["messages"],
    )
    return {"reply": resp.choices[0].message.content or ""}
```

---

## 3. Workflow 定义

### 3.1 Workflow 类

```python
# hub/workflow.py

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

# Workflow handler 签名：async (wf_ctx: WorkflowContext) -> Any
WorkflowHandler = Callable[["WorkflowContext"], Awaitable[Any]]


@dataclass
class Workflow:
    """工作流定义

    一个 Workflow 像一个 Module，但它是一段可编排的能力调用序列。

    两种触发方式:
    1. topic 触发 — 订阅一个或多个 topic，当消息到达时自动触发
    2. 主动触发 — 其他模块通过 ctx.start_workflow(name, params) 调用

    执行流程:
        触发 → 创建 WorkflowContext → 执行 handler → 结束
    """
    name: str                          # 工作流名称（全局唯一）
    description: str = ""              # 描述
    subscribe: str | list[str] = ""    # 触发的 topic(s)，空字符串表示仅主动触发
    handler: WorkflowHandler | None = None  # 编排处理函数
    timeout: float = 60.0              # 超时时间
    enabled: bool = True               # 是否启用
```

### 3.2 Workflow handler 的作用

> **Workflow handler 就是整个 Workflow 的"编排逻辑体"。它是一段 async 函数，负责：**

1. **接收触发上下文** —— 通过 `WorkflowContext` 拿到触发数据和运行时环境
2. **调用模块能力** —— 通过 `wf_ctx.invoke("module.capability", params)` 调用其他模块
3. **条件判断和动态分支** —— 用普通 Python 代码做分支（if/for/while），根据上一步结果决定下一步
4. **发布中间/最终结果** —— 通过 `wf_ctx.publish(topic, payload)` 对外发射事件
5. **返回最终结果** —— handler 的返回值会被 worklow 引擎收集，作为整个 workflow 的结果

**handler 的简单 vs 复杂示例：**

```python
# 简单的：A → B → C 线性流程
async def simple_handler(wf_ctx: WorkflowContext) -> Any:
    step1 = await wf_ctx.invoke("weather.location", {"longitude": 113, "latitude": 22})
    step2 = await wf_ctx.invoke("weather.forecast", {"adcode": step1["adcode"]})
    return await wf_ctx.invoke("llm.chat", {"messages": [{"role": "user", "content": step2}]})

# 复杂的：动态分支流程
async def complex_handler(wf_ctx: WorkflowContext) -> Any:
    # 先调用 LLM 判断意图
    intent = await wf_ctx.invoke("llm.chat", {"messages": [
        {"role": "system", "content": "判断意图：是查询天气还是通用聊天？返回 JSON。"},
        {"role": "user", "content": wf_ctx.data["text"]},
    ]})
    import json
    parsed = json.loads(intent["reply"])

    if parsed["intent"] == "weather":
        # 天气查询分支
        weather = await wf_ctx.invoke("weather.forecast", {"adcode": parsed["adcode"]})
        reply = await wf_ctx.invoke("llm.chat", {"messages": [
            {"role": "user", "content": f"用中文描述天气：{weather}"},
        ]})
    else:
        # 普通聊天分支
        reply = await wf_ctx.invoke("llm.chat", {"messages": [
            {"role": "user", "content": wf_ctx.data["text"]},
        ]})

    # 发布回复到 im 平台
    await wf_ctx.publish("im.reply", {
        "session_id": wf_ctx.data["session_id"],
        "message": reply["reply"],
    })
    return reply
```

---

## 4. WorkflowContext 的定义和作用

### 4.1 定义

```python
# hub/workflow_context.py

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from hub.module import Handler

if TYPE_CHECKING:
    from hub.core import Hub


@dataclass
class WorkflowContext:
    """工作流运行期上下文

    这是 Workflow handler 唯一能接触到的运行环境，它提供了 handler 在
    编排过程中所需的一切工具。

    核心职责：
    1. 携带触发时的原始数据（origin_payload / data）
    2. 提供调用其他模块能力的入口（invoke）
    3. 提供与 Hub 总线交互的能力（publish / spawn）
    4. 提供链路追踪（trace_id）
    5. 提供临时状态存储（state）
    """

    # --- 识别信息 ---
    workflow_name: str                # 当前 Workflow 名称
    trace_id: str                     # 链路追踪 ID (UUID)

    # --- 触发信息 ---
    origin_topic: str                 # 触发来源 topic（主动触发时为 ""）
    origin_payload: Any               # 原始触发载荷

    # --- 流程数据 ---
    data: Any = None                  # handler 自由读写的流程数据
    state: SimpleNamespace = field(default_factory=SimpleNamespace)  # 临时状态

    # --- Hub 引用（不暴露给 handler 直接使用） ---
    _hub: Hub = field(repr=False)

    def __post_init__(self):
        self.logger = logging.getLogger(f"workflow.{self.workflow_name}")

    async def invoke(self, capability: str, params: Any = None) -> Any:
        """调用其他模块提供的能力

        参数:
            capability: 能力名称，如 "weather.forecast", "llm.chat"
            params: 传给能力 handler 的参数

        返回:
            能力 handler 的返回值

        异常:
            CapabilityNotFoundError: 能力不存在
            TimeoutError: 调用超时
        """
        return await self._hub.invoke_capability(
            capability, params, trace_id=self.trace_id
        )

    async def publish(self, topic: str, payload: Any) -> None:
        """发布事件到 Hub 总线"""
        await self._hub.publish(topic, payload)

    def spawn(self, coro, *, name: str | None = None) -> asyncio.Task:
        """在 Hub 中注册一个后台任务"""
        return self._hub.spawn(coro, name=name or f"wf:{self.workflow_name}")
```

### 4.2 WorkflowContext 的职责矩阵

| 方法/属性 | 作用 | handler 中如何使用 |
|-----------|------|-------------------|
| `trace_id` | 全链路追踪 ID | 日志、调试、监控 |
| `origin_payload` | 原始触发数据 | 拿到用户消息、session 信息 |
| `data` | 当前流程数据 | 跨步骤传递中间结果 |
| `state` | 临时状态 | 计数器、标记位、缓存 |
| `invoke()` | 调用模块能力 | **核心方法**，编排各模块 |
| `publish()` | 发布事件 | 发回复、发中间进度 |
| `spawn()` | 启动后台任务 | 异步推送、定时任务 |
| `logger` | 日志 | 打印流程日志 |

---

## 5. 两种触发方式

### 5.1 Topic 触发（被动）

Workflow 订阅一个或多个 topic，当这些 topic 有事件发布时自动触发。

```python
# 定义
wf = Workflow(
    name="weather_assistant",
    description="天气查询助理",
    subscribe="im.message",       # 监听 im.message
    # subscribe=["im.message", "qq.message"],  # 也支持监听多个
    handler=weather_assistant_handler,
)

# 在 main.py 注册
hub.register_workflow(wf)
```

**内部机制**：`Hub.register_workflow()` 会把 workflow 注册为一个特殊的 subscriber，当 topic 有消息时，引擎创建 `WorkflowContext` 并执行 handler。

### 5.2 主动触发（模块调用）

模块可以在任意时刻通过 `Context` 启动一个 workflow：

```python
# 在模块 handler 中
@mod.on("im.message")
async def on_message(ctx: Context, event: BotEvent) -> None:
    # ... 一些前置逻辑 ...
    # 主动触发某个 workflow
    result = await ctx.start_workflow(
        "weather_assistant",       # workflow name
        {"text": "深圳天气", "session_id": event.session_id},  # params → origin_payload
    )
    ctx.logger.info("workflow result: %s", result)
```

`Context` 新增的方法：

```python
class Context:
    async def start_workflow(self, name: str, params: Any = None) -> Any:
        """主动触发一个 workflow 并等待执行结果

        参数:
            name: Workflow 名称
            params: 传给 workflow 的参数（成为 WorkflowContext.origin_payload）

        返回:
            Workflow handler 的返回值
        """
        return await self._hub.start_workflow(name, params)
```

**典型场景**：

| 场景 | 说明 |
|------|------|
| 前置过滤 | 模块先判断消息类型，再决定是否启动某个 workflow |
| 中间步骤 | 模块在流程中启动一个子 workflow |
| 定时触发 | heartbeat 模块定时启动一个 workflow |
| 手动触发 | 通过 API/命令行的方式启动 workflow |

---

## 6. Hub 新增的核心方法

```python
class Hub:
    # 1. 能力注册与调用
    def register_capability(self, capability: str, module_name: str, fn: Handler) -> None
    async def invoke_capability(self, capability: str, params: Any, *, trace_id: str) -> Any

    # 2. Workflow 注册
    def register_workflow(self, workflow: Workflow) -> None

    # 3. Workflow 主动触发
    async def start_workflow(self, name: str, params: Any = None) -> Any
```

**Workflow 分发流程：**

```
Topic 触发:
  Hub.publish("im.message", event)
    → _workflow_engine.on_topic("im.message", event)
      → 找到 subcribe="im.message" 的 workflow
      → 创建 WorkflowContext(trace_id=uuid, origin_payload=event)
      → asyncio.create_task(handler(WorkflowContext))

主动触发:
  模块调用 ctx.start_workflow("weather_assistant", params)
    → Hub.start_workflow("weather_assistant", params)
      → 找到 name="weather_assistant" 的 workflow
      → 创建 WorkflowContext(trace_id=uuid, origin_payload=params, origin_topic="")
      → await handler(WorkflowContext)
      → 返回 handler 的返回值
```

---

## 7. 完整示例

### 7.1 定义 Workflow

```python
# workflow/greeter.py
"""一个完整的 workflow 定义文件"""

from hub import Module, Context
from hub.workflow import Workflow, WorkflowContext
from hub.topics import IM_MESSAGE, IM_REPLY

# 声明 workflow 模块（像普通模块一样）
wf = Module("workflow_greeter")


@wf.provides("workflow.greeter")
async def handler(wf_ctx: WorkflowContext) -> dict:
    """流程: 收到消息 → LLM 判断是否问候 → LLM 回复问候 → 发布回复"""
    user_text = _extract_text(wf_ctx.origin_payload)

    # 1. 调用 LLM 判断意图
    intent = await wf_ctx.invoke("llm.chat", {
        "messages": [
            {"role": "system", "content": "判断消息是否为问候。回复 JSON: {\"is_greeting\": bool}"},
            {"role": "user", "content": user_text},
        ]
    })

    import json
    parsed = json.loads(intent["reply"])
    if not parsed.get("is_greeting"):
        return {"handled": False, "reason": "not a greeting"}

    # 2. LLM 生成问候回复
    reply = await wf_ctx.invoke("llm.chat", {
        "messages": [
            {"role": "system", "content": "你是友好热情的客服。简短回复问候。"},
            {"role": "user", "content": user_text},
        ]
    })

    # 3. 通过 LLM 判断是否需要查天气
    needs_weather = await wf_ctx.invoke("llm.chat", {
        "messages": [
            {"role": "system", "content": "回复 ONLY: 'yes' 或 'no'"},
            {"role": "user", "content": f"用户问好并询问天气了吗？消息: {user_text}"},
        ]
    })

    if "yes" in needs_weather["reply"].lower():
        weather = await wf_ctx.invoke("weather.forecast", {"adcode": "440305"})
        final_reply = await wf_ctx.invoke("llm.chat", {
            "messages": [
                {"role": "user", "content": f"结合问候和天气回复用户。天气: {weather}"},
            ]
        })
    else:
        final_reply = reply

    # 4. 发布回复
    from message.bot import BotEvent, TextSegment
    event = wf_ctx.origin_payload
    reply_event = BotEvent(
        id=f"greet:{event.id}",
        platform=event.platform,
        time=0.0,
        type="message",
        detail_type=event.detail_type,
        sub_type="",
        message_id=f"greet:{event.message_id}",
        message=[TextSegment(text=final_reply["reply"])],
        bot_id=event.bot_id,
        user_id=event.bot_id,
        user_name="bot",
        session_id=event.session_id,
        session_name=event.session_name,
    )
    await wf_ctx.publish(IM_REPLY, reply_event)
    return {"handled": True}


# 导出 Workflow 定义
workflow = Workflow(
    name="greeter",
    description="问候助理 — 检测问候并回复，附带天气信息",
    subscribe=IM_MESSAGE,
    handler=handler,
    timeout=30.0,
)


def _extract_text(event) -> str:
    """从 BotEvent 中提取文本"""
    if hasattr(event, "message"):
        return " ".join(
            seg.text for seg in event.message if hasattr(seg, "text") and seg.type == "Text"
        )
    if isinstance(event, dict):
        return event.get("text", "")
    return str(event)
```

### 7.2 在模块中主动触发

```python
# module/im_qq.py — 在 WS 收到消息时
async def _on_message_event(ev: dict, ctx: Context) -> None:
    # ... 现有解析逻辑 ...
    event = BotEvent(...)

    # 方式 A: 通过 topic 触发（现有方式）
    await ctx.publish(IM_MESSAGE, event)

    # 方式 B: 主动触发特定 workflow
if some_condition:
    result = await ctx.start_workflow("greeter", event)
    ctx.logger.info("greeter workflow result: %s", result)
```

---

## 8. 文件结构

```
hub/
├── __init__.py            # 导出 Hub, Module, Context, load_modules, Workflow, WorkflowContext
├── core.py                # Hub: +register_capability +invoke_capability +register_workflow +start_workflow
├── context.py             # Context: +start_workflow
├── module.py              # Module: +provides() +capabilities 属性
├── workflow.py            # Workflow dataclass, WorkflowHandler 类型
├── workflow_context.py    # WorkflowContext: invoke / publish / spawn / state
├── loader.py              # 支持加载 [workflow.*] 配置
└── topics.py              # 不变

workflow/                  # Workflow 定义目录（像 module/ 一样）
├── __init__.py
└── greeter.py             # 示例 workflow

module/
├── weather.py             # +@mod.provides("weather.forecast") 等
└── llm_openai.py          # +@mod.provides("llm.chat")
```

---

## 9. 配置加载

```toml
# config.toml

[workflow.greeter]
enabled = true
subscribe = "im.message"
timeout = 30.0

[workflow.notifier]
enabled = true
# 没有 subscribe，仅支持主动触发
```

Loader 新增逻辑：扫描 `[workflow.*]` 配置段 → 动态导入 `workflow/<name>.py` → 找到 `workflow` 实例 → 绑定配置 → 注册。

---

## 10. 设计总结

| 概念 | 本质 | 示例 |
|------|------|------|
| **Module** | 能力提供方 | weather 提供 forecast、location |
| **Workflow** | 编排组织方 | greeter 编排: LLM→判断→天气→回复 |
| **provides** | 模块暴露能力的入口 | `@mod.provides("weather.forecast")` |
| **invoke** | WorkflowContext 调用能力的方法 | `wf_ctx.invoke("llm.chat", {...})` |
| **handler** | Workflow 的编排逻辑体 | 一段 async 函数，自由编排 invoke |
| **WorkflowContext** | handler 唯一的运行环境 | 提供 invoke/publish/spawn/state |

**一句话总结：**

> **Workflow 是一个像 Module 一样的实体，通过一段 handler 函数（编排逻辑）来组合调用其他模块的 provides 能力，支持 topic 监听触发和模块主动触发两种方式。**
