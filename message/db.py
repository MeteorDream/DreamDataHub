"""数据库行模型 + DDL/INSERT 生成。

每张表对应一个 ``DBRecord`` 子类：
- ``__table__`` 决定表名
- ``__primary_key__`` 决定主键列（默认 ``id``）
- 字段顺序 = Pydantic 字段声明顺序 = SQL 列顺序
- 字段 SQL 类型用 ``Annotated[T, "VARCHAR(...)"]`` 内嵌；不写就按基础类型默认映射

mysql 模块在 startup 时调用 ``build_create_ddl`` 反射出 DDL，订阅
``database.write`` 时调用 ``build_insert`` 生成参数化 INSERT。
"""

from __future__ import annotations

import json
import typing
from datetime import datetime
from typing import TYPE_CHECKING, Annotated, Any, ClassVar, Union, get_args, get_origin

from pydantic import BaseModel, ConfigDict, Field

if TYPE_CHECKING:
    from message.bot import BotEvent


class DBRecord(BaseModel):
    """一张表的行模型基类。子类必须设 ``__table__``。

    ``extra="ignore"`` 让多余字段直接被丢掉（不会爆 ValidationError），
    业务方 publish 时给冗余键也安全。
    """

    model_config = ConfigDict(extra="ignore")

    # ClassVar 不会被 Pydantic 视作字段
    __table__: ClassVar[str]
    __primary_key__: ClassVar[str] = "id"
    # 联合唯一索引：每个元素是一组列名。生成 `UNIQUE KEY uk_<cols> (<cols>)`。
    __unique_keys__: ClassVar[list[tuple[str, ...]]] = []


class BotMessageRecord(DBRecord):
    """一条入站/出站的 IM 消息——对应 ``message.bot.BotEvent``。

    字段命名与 ``BotEvent`` 大部分一致，``message`` 列存为 JSON
    （``list[BotSegment]`` 序列化后的数组）。``time`` / ``type`` 是 MySQL
    保留字——SQL 里用反引号围起来即可避免冲突。

    **主键设计**：``id`` 是 DB 自增主键（跟其他表约定一致）。``BotEvent.id``
    是"事件唯一标识"（协议字段），跟 DB 主键概念不同；在 IM 模块的现有实现里，
    ``BotEvent.id`` 只是 ``message_id`` 的复制品，没有独立信息量——``from_event``
    工具会**丢弃 event.id**，让 DB 自增填充。想追溯平台原始消息 ID 走 ``message_id``
    列即可。

    业务侧 publish 这条记录时直接传 ``BotEvent.model_dump()``——见
    ``BotMessageRecord.from_event`` 工具。
    """

    __table__ = "bot_message"

    id: int | None = None  # DB 自增主键（BotEvent.id 会在 from_event 里被丢弃）
    platform: Annotated[str, "VARCHAR(32)"] = ""
    time: float = 0.0  # Unix 时间戳,秒；保留 float 精度
    type: Annotated[str, "VARCHAR(16)"] = ""
    detail_type: Annotated[str, "VARCHAR(32)"] = ""
    sub_type: Annotated[str, "VARCHAR(32)"] = ""
    message_id: Annotated[str, "VARCHAR(64)"] = ""
    message: list[dict[str, Any]] = Field(default_factory=list)  # JSON 列
    bot_id: Annotated[str, "VARCHAR(64)"] = ""
    user_id: Annotated[str, "VARCHAR(64)"] = ""
    user_name: Annotated[str, "VARCHAR(128)"] = ""
    session_id: Annotated[str, "VARCHAR(128)"] = ""
    session_name: Annotated[str, "VARCHAR(128)"] = ""
    # 当前消息引用（quote）的另一条消息 ID —— 跨消息追溯用。None 表示无引用。
    quote_id: Annotated[str, "VARCHAR(64)"] | None = None
    # 补充载荷（schema 自由）—— 平台特有或后续可能补的字段。None 表示无补充。
    addition: dict[str, Any] | None = None
    created_at: datetime | None = None  # DEFAULT CURRENT_TIMESTAMP — 落库时间

    @classmethod
    def from_event(cls, event: BotEvent) -> BotMessageRecord:
        """把 ``BotEvent`` 转成可写入的 record。

        - ``message`` 段用 ``model_dump`` 落成 ``list[dict]``，方便后续 JSON 序列化
        - **丢弃 ``event.id``**：DB 主键 ``id`` 由 AUTO_INCREMENT 生成，不受协议 id
          干扰。平台原始 message ID 在 ``message_id`` 列里保留
        """
        data = event.model_dump(mode="python")
        data.pop("id", None)  # 让 DB 主键 id 走自增，不被 BotEvent.id 覆盖
        return cls(**data)


class UserRecord(DBRecord):
    """一个 IM 平台账号 —— (platform, user_id) 联合唯一标识一个用户。

    ``user_name`` 存最近观察到的平台原始名；``meta`` 存平台特有的杂项
    （如 QQ 群角色、TG is_bot、weibo verified/followers_count 等），schema 自由。
    """

    __table__ = "user"
    __unique_keys__ = [("platform", "user_id")]

    id: int | None = None  # AUTO_INCREMENT
    platform: Annotated[str, "VARCHAR(32)"] = ""
    user_id: Annotated[str, "VARCHAR(64)"] = ""
    user_name: Annotated[str, "VARCHAR(128)"] = ""
    meta: dict[str, Any] = Field(default_factory=dict)  # JSON 列
    created_at: datetime | None = None  # DEFAULT CURRENT_TIMESTAMP
    updated_at: datetime | None = None  # DEFAULT CURRENT_TIMESTAMP ON UPDATE ...


# mysql 模块按 config.enabled_tables 过滤后从这里取
TABLES: dict[str, type[DBRecord]] = {
    BotMessageRecord.__table__: BotMessageRecord,
    UserRecord.__table__: UserRecord,
}


# ---------------------------------------------------------------------------
# 内部：类型反射
# ---------------------------------------------------------------------------


def _strip_optional(tp: Any) -> tuple[Any, bool]:
    """``Optional[X] / X | None`` → ``(X, True)``；其他 → ``(tp, False)``。"""
    origin = get_origin(tp)
    if origin is Union or origin is typing.Union or origin is type(int | None):
        args = [a for a in get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
    # PEP 604 X | None — 在 3.10+ 里 origin 是 types.UnionType
    import types

    if isinstance(tp, types.UnionType):
        args = [a for a in get_args(tp) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
    return tp, False


_PY_TO_SQL: dict[type, str] = {
    str: "VARCHAR(255)",
    int: "BIGINT",
    float: "DOUBLE",
    bool: "TINYINT(1)",
    datetime: "DATETIME",
    dict: "JSON",
    list: "JSON",
}


def _column_sql(name: str, annotation: Any, metadata: list[Any], is_pk: bool) -> str:
    """单列 DDL 片段。``metadata`` 来自 ``FieldInfo.metadata``——
    Pydantic v2 已经把 ``Annotated`` 的元信息抽到这里，annotation 只剩裸类型。

    特殊情况：``Annotated[X, ...] | None`` 这种 Optional 包外、Annotated 包内
    的写法，Pydantic 不会展平 metadata，``field.metadata`` 是空的——这里手动从
    annotation 里再剥一次 Annotated 兜底。
    """
    inner, optional = _strip_optional(annotation)

    # 兜底：从 inner 上自取 Annotated metadata（解决 Annotated|None 写法）
    if not metadata and hasattr(inner, "__metadata__"):
        metadata = list(inner.__metadata__)
        inner = getattr(inner, "__origin__", inner)

    # 优先用 metadata 里的字符串元信息；这是用户给的显式 SQL 类型
    sql_type: str | None = next((m for m in metadata if isinstance(m, str) and m.strip()), None)

    if sql_type is None:
        # PEP 604 / typing 里 dict[str, Any] 这种 generic 用 origin 来匹配
        origin = get_origin(inner) or inner
        sql_type = _PY_TO_SQL.get(origin) or _PY_TO_SQL.get(inner)
        if sql_type is None:
            sql_type = "TEXT"  # 最后兜底

    parts = [f"`{name}`", sql_type]

    if is_pk:
        if sql_type.upper().startswith(("BIGINT", "INT")):
            parts.append("AUTO_INCREMENT")
        parts.append("PRIMARY KEY")
    else:
        # 主键列不需要 NULL/NOT NULL 修饰；普通列：Optional → NULL，否则 NOT NULL
        if optional:
            parts.append("NULL")
        else:
            parts.append("NOT NULL")

    # datetime 默认值：
    # - 所有非主键 datetime 列都 DEFAULT CURRENT_TIMESTAMP
    # - 列名为 "updated_at" 的自动加 ON UPDATE CURRENT_TIMESTAMP（约定优于配置）
    if sql_type.upper() == "DATETIME" and not is_pk:
        parts.append("DEFAULT CURRENT_TIMESTAMP")
        if name == "updated_at":
            parts.append("ON UPDATE CURRENT_TIMESTAMP")

    return " ".join(parts)


# ---------------------------------------------------------------------------
# 公开 API
# ---------------------------------------------------------------------------


def model_columns(model: type[DBRecord]) -> list[str]:
    """按声明顺序返回字段名（不含 ClassVar）。"""
    return list(model.model_fields.keys())


def build_create_ddl(model: type[DBRecord]) -> str:
    """反射 ``DBRecord`` 子类 → ``CREATE TABLE IF NOT EXISTS`` DDL。

    支持 ``__unique_keys__`` 声明联合唯一索引；索引名格式 ``uk_<col1>_<col2>_...``。
    """
    pk = model.__primary_key__
    cols: list[str] = []
    for name, field in model.model_fields.items():
        cols.append(_column_sql(name, field.annotation, list(field.metadata), is_pk=name == pk))

    # 联合唯一索引
    for cols_tuple in model.__unique_keys__:
        if not cols_tuple:
            continue
        idx_name = "uk_" + "_".join(cols_tuple)
        col_list = ", ".join(f"`{c}`" for c in cols_tuple)
        cols.append(f"UNIQUE KEY `{idx_name}` ({col_list})")

    cols_sql = ",\n  ".join(cols)
    return (
        f"CREATE TABLE IF NOT EXISTS `{model.__table__}` (\n"
        f"  {cols_sql}\n"
        f") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
    )


def _coerce_for_sql(value: Any) -> Any:
    """把 list/dict 之类自动序列化成 JSON 字符串；其他原样返回。

    没这一步 PyMySQL/aiomysql 会把 list 当多参数展开、dict 直接拒绝。
    """
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, default=str)
    return value


def build_insert(
    model: type[DBRecord],
    row: dict[str, Any],
    *,
    upsert: bool = False,
) -> tuple[str, tuple[Any, ...]]:
    """row → ``(sql, params)``。

    流程：
    1. ``model(**row)`` 校验并丢弃多余字段
    2. ``model_dump`` 拿到所有列的值
    3. 跳过 ``None`` 让数据库走默认值（AUTO_INCREMENT / CURRENT_TIMESTAMP）
    4. list/dict 类型的值用 ``json.dumps`` 序列化（对应 JSON 列）
    5. 拼参数化 SQL，使用 ``%s`` 占位（aiomysql / PyMySQL 标准）

    参数:
        upsert: 为 ``True`` 时生成 ``INSERT ... ON DUPLICATE KEY UPDATE``——
                依赖表上有 UNIQUE KEY（``__unique_keys__``）或主键冲突；
                所有非主键、非 ``created_at`` 的列都会在冲突时被更新为新值。
                ``updated_at`` 列有 ``ON UPDATE CURRENT_TIMESTAMP``，会自动刷新。
    """
    record = model(**row)
    dumped = record.model_dump()

    cols: list[str] = []
    params: list[Any] = []
    for name in model_columns(model):
        value = dumped.get(name)
        if value is None:
            continue
        cols.append(name)
        params.append(_coerce_for_sql(value))

    if not cols:
        raise ValueError(f"build_insert: row produced no non-null columns for {model.__table__}")

    quoted_cols = [f"`{c}`" for c in cols]
    placeholders = ", ".join(["%s"] * len(cols))
    sql = f"INSERT INTO `{model.__table__}` ({', '.join(quoted_cols)}) VALUES ({placeholders})"

    if upsert:
        # UPDATE 部分：跳过主键 + created_at（不应被覆盖）
        pk = model.__primary_key__
        update_cols = [c for c in cols if c not in (pk, "created_at")]
        if update_cols:
            update_clause = ", ".join(f"`{c}` = VALUES(`{c}`)" for c in update_cols)
            sql += f" ON DUPLICATE KEY UPDATE {update_clause}"
        # else: 只有 pk / created_at 列时不生成 ON DUPLICATE，退化为纯 INSERT

    return sql, tuple(params)


__all__ = [
    "TABLES",
    "BotMessageRecord",
    "DBRecord",
    "UserRecord",
    "build_create_ddl",
    "build_insert",
    "model_columns",
]
