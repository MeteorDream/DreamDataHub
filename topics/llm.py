"""LLM 相关的跨模块 Topic —— 一轮问答完成后广播，交给 store / audit 类模块订阅。

发布方：``llm_openai`` 等 LLM 模块
潜在订阅方：``mysql`` (落库) / 分析类模块 / 计费类模块
"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from hub.topic import Topic

__all__ = ["LLMExchange", "LLMExchangePayload"]


class LLMExchangePayload(BaseModel):
    """``LLMExchange`` 载荷 —— 一轮对话的输入输出快照。"""

    session_id: str
    prompt: str
    response: str
    meta: dict[str, Any] = Field(default_factory=dict, description="模型名、平台等元信息")


class LLMExchange(Topic):
    """一轮 LLM 问答完成后广播，交给 store 类模块落库或分析。"""

    name: ClassVar[str] = "llm.exchange"
    description: ClassVar[str] = "LLM 问答落库事件（可选订阅）"
    Payload: ClassVar[type[BaseModel]] = LLMExchangePayload
