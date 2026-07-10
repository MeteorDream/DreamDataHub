"""结构化落库请求 Topic —— 任意模块可发，DB 模块（mysql / postgres 等）订阅。"""

from __future__ import annotations

from typing import Any, ClassVar

from pydantic import BaseModel, Field

from hub.topic import Topic

__all__ = ["DatabaseWrite", "DatabaseWritePayload"]


class DatabaseWritePayload(BaseModel):
    """``DatabaseWrite`` 载荷。表名 + 行数据 dict，具体 schema 由 ``message/db.py`` 校验。"""

    table: str = Field(min_length=1, description="目标表名（须在 message/db.py:TABLES 里）")
    row: dict[str, Any] = Field(
        default_factory=dict, description="行数据 dict，按 Pydantic model 校验"
    )


class DatabaseWrite(Topic):
    """结构化落库请求 —— 任意模块可发，DB 模块（mysql / postgres 等）订阅。"""

    name: ClassVar[str] = "database.write"
    description: ClassVar[str] = "结构化落库请求，payload 里带 table + row dict"
    Payload: ClassVar[type[BaseModel]] = DatabaseWritePayload
