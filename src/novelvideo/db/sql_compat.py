"""后端无关的 SQL 兼容层(SQLite / MySQL 双渲染)。

存储后端由 ``NOVELVIDEO_DB_DSN`` 选择:不配置即 SQLite(默认,行为与
社区版历史版本完全一致);配置 ``mysql://...`` 即 MySQL。调用点一律引用
本模块的渲染与判定助手,而不是把方言写死在 SQL 字符串里——切换后端时
只改这里,不动 42 个触库模块。

占位符统一用 ``?``;MySQL 侧由连接适配层统一改写成 ``%s``。
"""

from __future__ import annotations

import datetime as _dt
import os
import sqlite3
from contextlib import asynccontextmanager, contextmanager
from typing import AsyncIterator, Iterator, Optional, Sequence

BACKEND_SQLITE = "sqlite"
BACKEND_MYSQL = "mysql"

_DSN_MYSQL_PREFIXES = ("mysql://", "mariadb://")


def active_backend() -> str:
    """按 ``NOVELVIDEO_DB_DSN`` 返回 ``sqlite`` 或 ``mysql``。"""

    dsn = os.environ.get("NOVELVIDEO_DB_DSN", "").strip()
    if not dsn:
        return BACKEND_SQLITE
    if dsn.lower().startswith(_DSN_MYSQL_PREFIXES):
        return BACKEND_MYSQL
    raise RuntimeError(
        f"不支持的 NOVELVIDEO_DB_DSN(当前仅支持 mysql://): {dsn[:32]}..."
    )


def is_mysql() -> bool:
    return active_backend() == BACKEND_MYSQL


# ---------------------------------------------------------------------------
# 时间戳:两种格式对应 SQLite 的两个内置函数,两个后端共用同一套应用侧
# 生成,避免依赖数据库时钟与时区(服务器时区可能不是 UTC)。
# ---------------------------------------------------------------------------


def utc_datetime_str() -> str:
    """UTC ``YYYY-MM-DD HH:MM:SS`` —— 等价 SQLite ``datetime('now')``。"""

    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def utc_iso_ms() -> str:
    """UTC ``YYYY-MM-DDTHH:MM:SS.SSSZ`` —— 等价 SQLite ``strftime('%Y-%m-%dT%H:%M:%fZ','now')``。"""

    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


# ---------------------------------------------------------------------------
# 写入语句渲染。所有助手只产出 SQL 文本,参数仍由调用点按位置传入。
# ---------------------------------------------------------------------------


def _placeholders(n: int) -> str:
    return ", ".join("?" * n) if n else ""


def _conflict_set_clause(
    cols: Sequence[str],
    key_cols: Sequence[str],
    set_sql: Optional[str],
    touch_cols: Sequence[str],
) -> str:
    if set_sql is not None:
        items = [set_sql]
    else:
        update_cols = [c for c in cols if c not in key_cols]
        items = [f"{c} = excluded.{c}" for c in update_cols]
    items.extend(f"{c} = ?" for c in touch_cols)
    return ", ".join(items)


def upsert_sql(
    table: str,
    key_cols: Sequence[str],
    cols: Sequence[str],
    *,
    set_sql: Optional[str] = None,
    touch_cols: Sequence[str] = (),
) -> str:
    """渲染 ``INSERT ... ON CONFLICT`` 幂等写入。

    * SQLite: ``INSERT INTO t (...) VALUES (...) ON CONFLICT(k) DO UPDATE SET ...``
      (``excluded`` 为 SQLite 的冲突行别名)。
    * MySQL 8.0.19+: ``INSERT INTO t (...) VALUES (...) AS new ON DUPLICATE KEY
      UPDATE ...`` —— ``set_sql``/自动生成子句中的 ``excluded.`` 会改写为 ``new.``。

    ``set_sql`` 传入时使用原始 SET 表达式(如 ``"hits = hits + 1"``),
    否则为全部非键列生成 ``col = excluded.col``;``touch_cols`` 中的列追加
    ``col = ?``(调用方在参数末尾按 ``utc_datetime_str()`` 等应用侧值补参)。
    """

    cols_sql = ", ".join(cols)
    keys_sql = ", ".join(key_cols)
    values_sql = _placeholders(len(cols))
    conflict_clause = _conflict_set_clause(cols, key_cols, set_sql, touch_cols)
    if is_mysql():
        conflict_clause = conflict_clause.replace("excluded.", "new.")
        return (
            f"INSERT INTO {table} ({cols_sql}) VALUES ({values_sql}) AS new "
            f"ON DUPLICATE KEY UPDATE {conflict_clause}"
        )
    return (
        f"INSERT INTO {table} ({cols_sql}) VALUES ({values_sql}) "
        f"ON CONFLICT({keys_sql}) DO UPDATE SET {conflict_clause}"
    )


def insert_ignore_sql(table: str, cols: Sequence[str]) -> str:
    """冲突即跳过的插入:SQLite ``INSERT OR IGNORE`` / MySQL ``INSERT IGNORE``。"""

    cols_sql = ", ".join(cols)
    return f"INSERT {'OR IGNORE ' if not is_mysql() else 'IGNORE '}INTO {table} ({cols_sql}) VALUES ({_placeholders(len(cols))})"


def replace_into_sql(table: str, cols: Sequence[str]) -> str:
    """整行替换插入:SQLite ``INSERT OR REPLACE`` / MySQL ``REPLACE INTO``。"""

    cols_sql = ", ".join(cols)
    if is_mysql():
        return f"REPLACE INTO {table} ({cols_sql}) VALUES ({_placeholders(len(cols))})"
    return f"INSERT OR REPLACE INTO {table} ({cols_sql}) VALUES ({_placeholders(len(cols))})"


def insert_do_nothing_sql(
    table: str, cols: Sequence[str], key_cols: Sequence[str]
) -> str:
    """``ON CONFLICT(k) DO NOTHING`` / MySQL ``INSERT IGNORE``。"""

    if is_mysql():
        return insert_ignore_sql(table, cols)
    cols_sql = ", ".join(cols)
    keys_sql = ", ".join(key_cols)
    return (
        f"INSERT INTO {table} ({cols_sql}) VALUES ({_placeholders(len(cols))}) "
        f"ON CONFLICT({keys_sql}) DO NOTHING"
    )


# ---------------------------------------------------------------------------
# 串行化写事务。SQLite 用 BEGIN IMMEDIATE(写锁提前到事务开始);
# MySQL 用命名锁 GET_LOCK + 事务(``lock`` 参数必传,按 "表:键" 命名)。
# 事务内的提前 return 由 contextmanager 退出路径统一提交/回滚。
# ---------------------------------------------------------------------------


@asynccontextmanager
async def tx(db) -> AsyncIterator[None]:
    """普通事务:SQLite ``BEGIN`` / MySQL ``START TRANSACTION``,退出提交、异常回滚。"""

    await db.execute("START TRANSACTION" if is_mysql() else "BEGIN")
    try:
        yield
        await db.commit()
    except BaseException:
        await db.rollback()
        raise


@asynccontextmanager
async def immediate_tx(
    db, *, lock: Optional[str] = None
) -> AsyncIterator[None]:
    """aiosqlite / MySQL 异步连接上的串行化写事务。"""

    if is_mysql():
        from novelvideo.db.adapter import begin_immediate_mysql_async

        # begin_immediate_mysql_async 是 async def,返回异步 CM —— 必须 await
        # (同步侧 begin_immediate_mysql_sync 是普通 def,直接 with)。
        async with await begin_immediate_mysql_async(db, lock):
            yield
        return
    await db.execute("BEGIN IMMEDIATE")
    try:
        yield
        await db.commit()
    except BaseException:
        await db.rollback()
        raise


@contextmanager
def immediate_tx_sync(conn, *, lock: Optional[str] = None) -> Iterator[None]:
    """sqlite3 / MySQL 同步连接上的串行化写事务。"""

    if is_mysql():
        from novelvideo.db.adapter import begin_immediate_mysql_sync

        with begin_immediate_mysql_sync(conn, lock):
            yield
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.commit()
    except BaseException:
        conn.rollback()
        raise


# ---------------------------------------------------------------------------
# 内省与错误分类:替代 PRAGMA table_info / sqlite_master / 错误字符串匹配。
# ---------------------------------------------------------------------------


def _row_value_by_name(row, name: str, fallback_index: int):
    """sqlite3.Row 支持键名与位置双访问;dict 行只支持键名,裸元组只支持位置。"""

    try:
        return row[name]
    except (KeyError, IndexError, TypeError):
        return row[fallback_index]


async def table_columns_async(db, table: str) -> list[str]:
    """异步连接上的列名列表(SQLite: PRAGMA table_info)。"""

    if is_mysql():
        from novelvideo.db.adapter import table_columns_mysql_async

        return await table_columns_mysql_async(db, table)
    async with db.execute(f"PRAGMA table_info({table})") as cursor:
        rows = await cursor.fetchall()
    return [str(_row_value_by_name(row, "name", 1)) for row in rows]


def table_columns_sync(conn, table: str) -> list[str]:
    """同步连接上的列名列表。"""

    if is_mysql():
        from novelvideo.db.adapter import table_columns_mysql_sync

        return table_columns_mysql_sync(conn, table)
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return [str(_row_value_by_name(row, "name", 1)) for row in rows]


async def table_exists_async(db, table: str) -> bool:
    """异步连接上的表存在性(SQLite: sqlite_master)。"""

    if is_mysql():
        from novelvideo.db.adapter import table_exists_mysql_async

        return await table_exists_mysql_async(db, table)
    async with db.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ) as cursor:
        return await cursor.fetchone() is not None


def json_extract_text(col_sql: str, path: str) -> str:
    """取 JSON 文本标量并**去掉引号**(SQL 字符串语义)。

    SQLite ``json_extract`` 本就返回裸文本;MySQL ``JSON_EXTRACT`` 返回 JSON
    标量(``"inline"`` 带引号),必须包一层 ``JSON_UNQUOTE`` 才能与裸字符串比较。
    """

    if is_mysql():
        return f"JSON_UNQUOTE(JSON_EXTRACT({col_sql}, '{path}'))"
    return f"json_extract({col_sql}, '{path}')"


def json_valid_expr(col_sql: str) -> str:
    """JSON 合法性判定(两侧函数同名,包装只为集中方言点)。"""

    if is_mysql():
        return f"JSON_VALID({col_sql})"
    return f"json_valid({col_sql})"


def rowid_expr(table: str) -> str:
    """行标识表达式:SQLite 原生 ``rowid``;MySQL 用该表显式自增列 ``id``。"""

    return "id" if is_mysql() else "rowid"


def is_busy_error(exc: BaseException) -> bool:
    """锁等待/死锁类错误(SQLite "database is locked" ↔ MySQL 1205/1213)。"""

    if isinstance(exc, sqlite3.OperationalError):
        text = str(exc).lower()
        return "locked" in text or "busy" in text
    return _mysql_error_in(exc, {1205, 1213})


def is_duplicate_column_error(exc: BaseException) -> bool:
    """列已存在(SQLite "duplicate column name" ↔ MySQL 1060)。"""

    if isinstance(exc, sqlite3.OperationalError):
        return "duplicate column name" in str(exc).lower()
    return _mysql_error_in(exc, {1060})


def is_duplicate_index_error(exc: BaseException) -> bool:
    """索引已存在(SQLite 无此错——靠 IF NOT EXISTS;MySQL 1061)。"""

    return _mysql_error_in(exc, {1061}) or (
        isinstance(exc, sqlite3.OperationalError)
        and "duplicate key name" in str(exc).lower()
    )


def is_duplicate_key_error(exc: BaseException) -> bool:
    """唯一键冲突(SQLite IntegrityError ↔ MySQL 1062)。"""

    if isinstance(exc, sqlite3.IntegrityError):
        return True
    return _mysql_error_in(exc, {1062})


def _mysql_error_in(exc: BaseException, codes: set[int]) -> bool:
    for cause, code in _iter_mysql_errors(exc):
        if code in codes:
            return True
        text = str(cause).lower()
        if 1205 in codes and ("lock wait timeout" in text or "deadlock" in text):
            return True
        if 1060 in codes and "duplicate column" in text:
            return True
        if 1062 in codes and "duplicate entry" in text:
            return True
    return False


def _iter_mysql_errors(exc: BaseException) -> Iterator[tuple[BaseException, Optional[int]]]:
    seen: set[int] = set()
    current: Optional[BaseException] = exc
    while current is not None and id(current) not in seen:
        seen.add(id(current))
        code: Optional[int] = None
        if current.args and isinstance(current.args[0], int):
            code = current.args[0]
        yield current, code
        current = current.__cause__ or current.__context__


__all__ = [
    "BACKEND_MYSQL",
    "BACKEND_SQLITE",
    "active_backend",
    "immediate_tx",
    "immediate_tx_sync",
    "insert_do_nothing_sql",
    "insert_ignore_sql",
    "is_busy_error",
    "is_duplicate_column_error",
    "is_duplicate_index_error",
    "is_duplicate_key_error",
    "is_mysql",
    "json_extract_text",
    "json_valid_expr",
    "replace_into_sql",
    "rowid_expr",
    "table_columns_async",
    "table_columns_sync",
    "table_exists_async",
    "tx",
    "upsert_sql",
    "utc_datetime_str",
    "utc_iso_ms",
]
