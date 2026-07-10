"""mysql — 通用数据库写入模块。

订阅 :class:`DatabaseWrite`（payload: ``DatabaseWritePayload{table, row}``）。
按 ``message/db.py:TABLES`` 里注册的 Pydantic 模型校验 row、生成参数化 INSERT。

启动时按 ``enabled_tables`` 自动 ``CREATE TABLE IF NOT EXISTS``——schema 改字段
仍需手动 ALTER（v1 不带迁移）。

依赖：``aiomysql``（懒 import；模块不启用就不需要装）。
"""

from __future__ import annotations

import aiomysql
from pydantic import ValidationError

from hub import Context, Module
from message.db import TABLES, DBRecord, build_create_ddl, build_insert
from topics.database import DatabaseWrite, DatabaseWritePayload

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
        sql, params = build_insert(model, row)
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
                "mysql: insert ok table=%s lastrowid=%s rows=%d",
                table,
                cur.lastrowid,
                cur.rowcount,
            )
    except Exception:
        ctx.logger.exception("mysql: insert failed table=%s", table)
