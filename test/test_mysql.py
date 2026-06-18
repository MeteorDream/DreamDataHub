"""mysql / message.db 单元测试 — DDL 反射、INSERT 拼接、payload 校验。

不连真实 DB；fake aiomysql 用 monkeypatch 注入。
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from typing import Annotated
from unittest.mock import AsyncMock, MagicMock

import pytest

from message.bot import AtSegment, BotEvent, PlainSegment
from message.db import (
    BotMessageRecord,
    DBRecord,
    LLMExchangeRecord,
    build_create_ddl,
    build_insert,
)
from module import mysql as mysql_mod


# ---------------------------------------------------------------------------
# DDL 反射
# ---------------------------------------------------------------------------


def test_create_ddl_for_llm_exchange() -> None:
    ddl = build_create_ddl(LLMExchangeRecord)
    # 表名、字段名、SQL 类型、默认值都在
    assert "CREATE TABLE IF NOT EXISTS `llm_exchange`" in ddl
    assert "`id` BIGINT AUTO_INCREMENT PRIMARY KEY" in ddl
    assert "`session_id` VARCHAR(128) NOT NULL" in ddl
    assert "`prompt` TEXT NOT NULL" in ddl
    assert "`response` TEXT NOT NULL" in ddl
    assert "`model` VARCHAR(64) NOT NULL" in ddl
    assert "`platform` VARCHAR(32) NOT NULL" in ddl
    assert "`created_at` DATETIME NULL DEFAULT CURRENT_TIMESTAMP" in ddl
    assert "ENGINE=InnoDB DEFAULT CHARSET=utf8mb4" in ddl


def test_create_ddl_optional_field_uses_null() -> None:
    """Optional 字段 → NULL；required → NOT NULL。"""
    class T1(DBRecord):
        __table__ = "t1"
        id: int | None = None
        opt_str: Annotated[str, "VARCHAR(64)"] | None = None
        req_str: Annotated[str, "VARCHAR(64)"] = ""

    ddl = build_create_ddl(T1)
    assert "`opt_str` VARCHAR(64) NULL" in ddl
    assert "`req_str` VARCHAR(64) NOT NULL" in ddl


def test_create_ddl_falls_back_to_text_for_unknown_type() -> None:
    class T2(DBRecord):
        __table__ = "t2"
        id: int | None = None
        weird: list[dict] = []  # noqa: RUF012

    ddl = build_create_ddl(T2)
    # list → JSON
    assert "`weird` JSON NOT NULL" in ddl


def test_create_ddl_custom_primary_key() -> None:
    class T3(DBRecord):
        __table__ = "t3"
        __primary_key__ = "uid"
        uid: Annotated[str, "VARCHAR(36)"] = ""

    ddl = build_create_ddl(T3)
    assert "`uid` VARCHAR(36) PRIMARY KEY" in ddl


# ---------------------------------------------------------------------------
# INSERT 拼接
# ---------------------------------------------------------------------------


def test_insert_uses_placeholder_and_skips_none() -> None:
    sql, params = build_insert(LLMExchangeRecord, {
        "session_id": "qq:group:123",
        "prompt": "hi",
        "response": "hello",
        "model": "gpt-4o",
        "platform": "qq",
    })
    # id / created_at 都是 None → 跳过让 DB 走默认
    assert "`id`" not in sql
    assert "`created_at`" not in sql
    # 5 个非 None 列，5 个 %s
    assert sql.count("%s") == 5
    assert len(params) == 5
    assert params == ("qq:group:123", "hi", "hello", "gpt-4o", "qq")


def test_insert_drops_unknown_payload_keys() -> None:
    """payload 里多余的键被 Pydantic extra='ignore' 丢掉，不会进 SQL。"""
    sql, params = build_insert(LLMExchangeRecord, {
        "session_id": "x",
        "prompt": "y",
        "response": "z",
        "model": "m",
        "platform": "p",
        "drop_table": "DROP TABLE users;--",  # 注入企图被 model 丢掉
        "another_extra": 42,
    })
    assert "drop_table" not in sql
    assert "another_extra" not in sql
    assert params == ("x", "y", "z", "m", "p")


def test_insert_with_only_none_columns_raises() -> None:
    """全是 None 时 build_insert 抛 ValueError。"""
    class T4(DBRecord):
        __table__ = "t4"
        id: int | None = None

    with pytest.raises(ValueError, match="no non-null columns"):
        build_insert(T4, {})


def test_insert_validation_error_propagates() -> None:
    """Pydantic 校验错（类型不对）应该抛 ValidationError，给调用方决定。"""
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        build_insert(LLMExchangeRecord, {
            "session_id": "x",
            "prompt": ["not", "a", "string"],  # type: ignore[dict-item]
            "response": "z",
            "model": "m",
            "platform": "p",
        })


# ---------------------------------------------------------------------------
# BotMessageRecord — 对应 BotEvent 的入站记录
# ---------------------------------------------------------------------------


def test_bot_message_ddl_uses_backtick_for_reserved_words() -> None:
    """BotEvent 有 id/time/type 三个 SQL 保留字字段，DDL 必须用反引号围起来。"""
    ddl = build_create_ddl(BotMessageRecord)
    assert "CREATE TABLE IF NOT EXISTS `bot_message`" in ddl
    # row_id 是自增主键
    assert "`row_id` BIGINT AUTO_INCREMENT PRIMARY KEY" in ddl
    # 保留字字段都被反引号围
    assert "`id` VARCHAR(64) NOT NULL" in ddl
    assert "`time` DOUBLE NOT NULL" in ddl
    assert "`type` VARCHAR(16) NOT NULL" in ddl
    # message 列是 JSON
    assert "`message` JSON NOT NULL" in ddl
    # 故意不再有 created_at 字段
    assert "`created_at`" not in ddl


def test_bot_message_from_event_round_trip() -> None:
    """BotEvent → BotMessageRecord.from_event → build_insert 链路顺畅，
    message 列被 json.dumps 序列化成字符串。"""
    evt = BotEvent(
        id="evt-1", platform="qq", time=1781666540.5,
        type="message", detail_type="group", sub_type="mentioned",
        message_id="12345",
        message=[PlainSegment(text="hello"), AtSegment(user_id="999")],
        bot_id="1228531751", user_id="2423428733", user_name="梦蝶",
        session_id="qq:group:1098814820", session_name="の、梦蝶",
    )
    record = BotMessageRecord.from_event(evt)
    assert record.id == "evt-1"
    assert record.platform == "qq"
    assert len(record.message) == 2

    sql, params = build_insert(BotMessageRecord, record.model_dump())
    # row_id / created_at 是 None → 不入 SQL
    assert "`row_id`" not in sql
    assert "`created_at`" not in sql
    # 13 个非 None 列
    assert sql.count("%s") == 13
    assert len(params) == 13
    # message 列被序列化成 JSON 字符串
    message_param = params[7]  # message 在第 8 个位置（按声明顺序）
    assert isinstance(message_param, str)
    assert '"Plain"' in message_param
    assert '"hello"' in message_param
    assert '"At"' in message_param


def test_bot_message_json_serialization_handles_strenum() -> None:
    """BotSegment.type 是 StrEnum，json.dumps 时通过 default=str 兜底。"""
    evt = BotEvent(
        id="x", platform="qq", time=0.0,
        type="message", detail_type="private", sub_type="",
        message_id="x", message=[PlainSegment(text="hi")],
        bot_id="0", user_id="0", user_name="", session_id="private:0", session_name="",
    )
    sql, params = build_insert(BotMessageRecord, BotMessageRecord.from_event(evt).model_dump())
    msg_json = params[7]
    # JSON 里 type 应该是字符串 "Plain"，不是 StrEnum repr
    assert '"type": "Plain"' in msg_json or '"type":"Plain"' in msg_json


# ---------------------------------------------------------------------------
# 模块订阅 — payload 形状校验
# ---------------------------------------------------------------------------


def _make_ctx(pool=None, tables=None) -> SimpleNamespace:
    state = SimpleNamespace(
        tables=tables if tables is not None else {"llm_exchange": LLMExchangeRecord},
        pool=pool,
        dsn={"host": "fake", "db": "fake"},
        pool_size=1,
    )
    return SimpleNamespace(
        state=state,
        config={},
        logger=logging.getLogger("test.mysql"),
    )


def test_on_write_drops_non_dict_payload() -> None:
    ctx = _make_ctx()
    # 非 dict、缺 table、row 不是 dict、未知 table、pool=None — 都不该抛
    asyncio.run(mysql_mod.on_write(ctx, "not a dict"))
    asyncio.run(mysql_mod.on_write(ctx, {}))
    asyncio.run(mysql_mod.on_write(ctx, {"table": "llm_exchange", "row": "bad"}))
    asyncio.run(mysql_mod.on_write(ctx, {"table": "no_such_table", "row": {}}))
    asyncio.run(mysql_mod.on_write(
        ctx,
        {"table": "llm_exchange", "row": {"prompt": "x", "response": "y", "model": "m", "platform": "p"}},
    ))  # pool=None → drop


def test_on_write_with_fake_pool_executes_insert() -> None:
    """模拟 aiomysql pool：检查 cursor.execute 被以正确 sql/params 调用。"""
    cursor = MagicMock()
    cursor.execute = AsyncMock()
    cursor.lastrowid = 42
    cursor.rowcount = 1
    cursor.__aenter__ = AsyncMock(return_value=cursor)
    cursor.__aexit__ = AsyncMock(return_value=None)

    conn = MagicMock()
    conn.cursor = MagicMock(return_value=cursor)
    conn.__aenter__ = AsyncMock(return_value=conn)
    conn.__aexit__ = AsyncMock(return_value=None)

    pool = MagicMock()
    pool.acquire = MagicMock(return_value=conn)

    ctx = _make_ctx(pool=pool)
    payload = {
        "table": "llm_exchange",
        "row": {
            "session_id": "qq:group:1",
            "prompt": "hi",
            "response": "hello",
            "model": "gpt-4o",
            "platform": "qq",
        },
    }
    asyncio.run(mysql_mod.on_write(ctx, payload))

    cursor.execute.assert_awaited_once()
    sql, params = cursor.execute.call_args.args
    assert "INSERT INTO `llm_exchange`" in sql
    assert sql.count("%s") == 5
    assert params == ("qq:group:1", "hi", "hello", "gpt-4o", "qq")
