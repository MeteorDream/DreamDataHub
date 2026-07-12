"""mysql — 通用数据库写入模块。

订阅 :class:`DatabaseWrite`（payload: ``DatabaseWritePayload{table, row, upsert}``）。
按 ``message/db.py:TABLES`` 里注册的 Pydantic 模型校验 row、生成参数化 INSERT
（或 upsert）。

对外提供 :class:`UserQueryService` 能力 —— 按 ``(platform, user_id)`` 查 user 表。
不做通用的"任意查询"接口，避免 SQL 注入面 + 让调用方直接依赖强类型契约。

启动时按 ``enabled_tables`` 自动 ``CREATE TABLE IF NOT EXISTS``——schema 改字段
仍需手动 ALTER（v1 不带迁移）。

依赖：``aiomysql``（懒 import；模块不启用就不需要装）。
"""

from __future__ import annotations

import json
from typing import ClassVar

import aiomysql
from pydantic import BaseModel, Field, ValidationError

from hub import Capability, Context, Module
from message.db import TABLES, DBRecord, UserRecord, build_create_ddl, build_insert
from topics.database import DatabaseWrite, DatabaseWritePayload

# ---------------------------------------------------------------------------
# UserQueryService — 按 (platform, user_id) 查 user 表
# ---------------------------------------------------------------------------


class UserQueryParams(BaseModel):
    """``UserQueryService`` 入参 —— (platform, user_id) 联合唯一。"""

    platform: str = Field(min_length=1)
    user_id: str = Field(min_length=1)


class UserQueryResult(BaseModel):
    """``UserQueryService`` 返回值。

    ``found=False`` 时 ``user`` 为 None；调用方按需处理"用户未记录"场景。
    """

    found: bool = False
    user: UserRecord | None = None


class UserQueryService(Capability):
    """按 (platform, user_id) 查 user 表；找不到时 found=False。"""

    name: ClassVar[str] = "user.query"
    Params: ClassVar[type[BaseModel]] = UserQueryParams
    Result: ClassVar[type[BaseModel]] = UserQueryResult


mod = Module("mysql")


@mod.on_startup
async def setup(ctx: Context) -> None:
    cfg = ctx.config

    enabled_names: list[str] = list(cfg.get("enabled_tables") or TABLES.keys())
    tables: dict[str, type[DBRecord]] = {}
    for name in enabled_names:
        if name not in TABLES:
            ctx.logger.warning("mysql: unknown table in config: %r — skip", name)
            continue
        tables[name] = TABLES[name]
    ctx.state.tables = tables

    ctx.state.dsn = {
        "host": cfg.get("host", "127.0.0.1"),
        "port": int(cfg.get("port", 3306)),
        "user": cfg.get("user", "root"),
        "password": cfg.get("password", "") or "",
        "db": cfg.get("database", "datahub"),
        "autocommit": True,
        "charset": "utf8mb4",
    }
    ctx.state.pool_size = int(cfg.get("pool_size", 5))
    ctx.state.pool = None

    try:
        ctx.state.pool = await aiomysql.create_pool(
            minsize=1,
            maxsize=ctx.state.pool_size,
            **ctx.state.dsn,
        )
    except Exception:
        ctx.logger.exception(
            "mysql: create_pool failed (host=%s db=%s); 模块降级",
            ctx.state.dsn["host"],
            ctx.state.dsn["db"],
        )
        ctx.state.pool = None
        return

    ctx.logger.info(
        "mysql: pool ready (host=%s db=%s pool=%d) tables=%s",
        ctx.state.dsn["host"],
        ctx.state.dsn["db"],
        ctx.state.pool_size,
        list(tables.keys()),
    )

    # 启动时按 model 自动 CREATE TABLE IF NOT EXISTS
    async with ctx.state.pool.acquire() as conn, conn.cursor() as cur:
        for name, model in tables.items():
            ddl = build_create_ddl(model)
            try:
                await cur.execute(ddl)
                ctx.logger.info("mysql: ensured table %s", name)
            except Exception:
                ctx.logger.exception("mysql: ensure table failed: %s", name)


@mod.on_shutdown
async def teardown(ctx: Context) -> None:
    pool = ctx.state.pool
    if pool is not None:
        try:
            pool.close()
            await pool.wait_closed()
        except Exception:
            ctx.logger.exception("mysql: pool close error")
    ctx.logger.info("mysql: closed")


@mod.on(DatabaseWrite)
async def on_write(ctx: Context, payload: DatabaseWritePayload) -> None:
    table = payload.table
    row = payload.row

    model = ctx.state.tables.get(table)
    if model is None:
        ctx.logger.warning("mysql: unknown table %r — skip", table)
        return

    pool = ctx.state.pool
    if pool is None:
        # setup 时已经记过 ERROR；这里只 debug，避免每条 publish 都刷
        ctx.logger.debug("mysql: pool is None, drop write to %s", table)
        return

    try:
        sql, params = build_insert(model, row, upsert=payload.upsert)
    except ValidationError as exc:
        ctx.logger.warning("mysql: row validation failed for %s: %s", table, exc)
        return
    except ValueError as exc:
        ctx.logger.warning("mysql: build_insert failed for %s: %s", table, exc)
        return

    try:
        async with pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(sql, params)
            ctx.logger.info(
                "mysql: %s ok table=%s lastrowid=%s rows=%d",
                "upsert" if payload.upsert else "insert",
                table,
                cur.lastrowid,
                cur.rowcount,
            )
    except Exception:
        ctx.logger.exception("mysql: write failed table=%s", table)


@mod.provides(UserQueryService)
async def query_user(ctx: Context, params: UserQueryParams) -> UserQueryResult:
    """按 (platform, user_id) 查 user 表。找不到时 found=False。

    调用方必须启用 ``user`` 表（``enabled_tables`` 里包含 ``"user"``），否则
    抛 RuntimeError（配置错误）。
    """
    if UserRecord.__table__ not in ctx.state.tables:
        raise RuntimeError(
            f"UserQueryService: table {UserRecord.__table__!r} not enabled "
            f"(add to config.enabled_tables)"
        )
    pool = ctx.state.pool
    if pool is None:
        raise RuntimeError("UserQueryService: mysql pool unavailable")

    # 参数化查询防注入；列顺序按 UserRecord 字段声明顺序，方便 dict 组装
    col_names = list(UserRecord.model_fields.keys())
    cols_sql = ", ".join(f"`{c}`" for c in col_names)
    sql = (
        f"SELECT {cols_sql} FROM `{UserRecord.__table__}` "
        f"WHERE `platform` = %s AND `user_id` = %s LIMIT 1"
    )

    try:
        async with pool.acquire() as conn, conn.cursor() as cur:
            await cur.execute(sql, (params.platform, params.user_id))
            row = await cur.fetchone()
    except Exception:
        ctx.logger.exception(
            "user.query failed: platform=%s user_id=%s",
            params.platform,
            params.user_id,
        )
        raise

    if not row:
        return UserQueryResult(found=False, user=None)

    # aiomysql 默认 cursor 返回 tuple，按 col_names 顺序组回 dict
    raw = dict(zip(col_names, row, strict=True))
    # JSON 列（meta）从 DB 拿回来可能是 str（未解析），要反序列化
    meta_raw = raw.get("meta")
    if isinstance(meta_raw, str):
        try:
            raw["meta"] = json.loads(meta_raw)
        except json.JSONDecodeError:
            ctx.logger.warning("user.query: meta not valid JSON, using empty dict")
            raw["meta"] = {}
    return UserQueryResult(found=True, user=UserRecord.model_validate(raw))
