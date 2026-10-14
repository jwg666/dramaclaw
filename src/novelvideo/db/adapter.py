"""MySQL 连接适配层。

让按 aiosqlite/sqlite3 形态写的调用点跑在 MySQL 上:
* :class:`MysqlConnection` / :class:`MysqlSyncConnection` 模拟被实际用到的
  连接子集(execute/executemany/commit/rollback/close、cursor.fetchall/
  fetchone/rowcount/lastrowid、``async with db.execute(...)``);
* 行对象 :class:`Row` 模拟 sqlite3.Row(键名访问 + ``keys()`` + ``dict(row)``);
* SQL 语句在执行前统一改写:``?`` → ``%s``、裸表名 → 作用域前缀 + 表名
  (引号内的字符串字面量不动);
* 串行化写事务用 ``GET_LOCK`` + 显式事务(SQLite 的 BEGIN IMMEDIATE 等价物)。

连接池(aiomysql)按数据库实例全局共享;每个逻辑库(原 SQLite 文件)只
决定表名前缀,不独占连接。
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import logging
import re
import threading
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Optional

from novelvideo.db.ddl_translate import known_tables

logger = logging.getLogger(__name__)

_GET_LOCK_TIMEOUT_SECONDS = 30

# ---------------------------------------------------------------------------
# 表名前缀改写
# ---------------------------------------------------------------------------

_TABLE_RE_CACHE: Optional[tuple[int, Any]] = None
_REWRITE_LOCK = threading.Lock()


def _table_rewrite_re():
    global _TABLE_RE_CACHE
    tables = known_tables()
    cached = _TABLE_RE_CACHE
    if cached is not None and cached[0] == len(tables):
        return cached[1]
    with _REWRITE_LOCK:
        cached = _TABLE_RE_CACHE
        if cached is not None and cached[0] == len(tables):
            return cached[1]
        if not tables:
            _TABLE_RE_CACHE = (0, None)
            return None
        # 长名优先,防止短名先匹配吃掉长名的前缀段
        alternation = "|".join(
            sorted((re.escape(t) for t in tables), key=len, reverse=True)
        )
        compiled = re.compile(rf"\b(?:{alternation})\b")
        _TABLE_RE_CACHE = (len(tables), compiled)
        return compiled


# 带捕获组:split 后偶数下标=引号外片段、奇数下标=字符串字面量(必须保留)
_QUOTED_RE = re.compile(r"('(?:[^']|'')*')")


def rewrite_sql(sql: str, prefix: str) -> str:
    """把语句中引号外的裸表名加上作用域前缀。"""

    if not prefix:
        return sql
    pattern = _table_rewrite_re()
    if pattern is None:
        return sql
    parts = _QUOTED_RE.split(sql)
    for i in range(0, len(parts), 2):  # 偶数下标 = 引号外片段
        parts[i] = pattern.sub(prefix + r"\g<0>", parts[i])
    return "".join(parts)


# ---------------------------------------------------------------------------
# 行与游标
# ---------------------------------------------------------------------------


class Row:
    """sqlite3.Row 等价物:键名/位置双访问 + keys()。"""

    __slots__ = ("_values", "_index")

    def __init__(self, values: tuple, columns: tuple):
        self._values = values
        self._index = {name: i for i, name in enumerate(columns)}

    def __getitem__(self, key):
        if isinstance(key, (int, slice)):
            return self._values[key]
        try:
            return self._values[self._index[key]]
        except KeyError:
            raise KeyError(f"no such column: {key}") from None

    def keys(self):
        return list(self._index)

    def __len__(self):
        return len(self._values)

    def __iter__(self):
        return iter(self._index)

    def __repr__(self):  # pragma: no cover
        return f"Row({dict(self._index)})"


def _rows_from_cursor(cur) -> list[Row]:
    columns = tuple(d[0] for d in cur.description or ())
    return [Row(values, columns) for values in cur.fetchall()]


async def _close_cursor(cur) -> None:
    """aiomysql 的 Cursor.close 是协程;pymysql 是同步。两者兼容关闭。"""

    result = cur.close()
    if inspect.isawaitable(result):
        await result


class MysqlCursor:
    """aiosqlite.Cursor 兼容面(实际用到的方法子集)。

    fetch 也必须持有连接的操作锁:游标读取与后续语句共享同一条原始连接。
    """

    def __init__(self, cur, op_lock: Optional[asyncio.Lock] = None):
        self._cur = cur
        self._op_lock = op_lock

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def lastrowid(self):
        return self._cur.lastrowid

    @property
    def description(self):
        return self._cur.description

    async def fetchone(self) -> Optional[Row]:
        columns = tuple(d[0] for d in self._cur.description or ())
        if self._op_lock is not None:
            async with self._op_lock:
                values = await self._cur.fetchone()
        else:
            values = await self._cur.fetchone()
        return Row(values, columns) if values is not None else None

    async def fetchall(self) -> list[Row]:
        columns = tuple(d[0] for d in self._cur.description or ())
        if self._op_lock is not None:
            async with self._op_lock:
                raw_rows = await self._cur.fetchall()
        else:
            raw_rows = await self._cur.fetchall()
        return [Row(values, columns) for values in raw_rows]

    async def close(self) -> None:
        await _close_cursor(self._cur)

    def __aiter__(self):
        return self

    async def __anext__(self) -> Row:
        row = await self.fetchone()
        if row is None:
            raise StopAsyncIteration
        return row

    async def __aenter__(self) -> "MysqlCursor":
        return self

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        await self.close()
        return False


class _PendingCursor:
    """同时支持 ``await db.execute(...)`` 与 ``async with db.execute(...)``。"""

    __slots__ = ("_factory", "_cursor")

    def __init__(self, factory):
        self._factory = factory
        self._cursor: Optional[MysqlCursor] = None

    async def _get(self) -> MysqlCursor:
        if self._cursor is None:
            self._cursor = await self._factory()
        return self._cursor

    def __await__(self):
        return self._get().__await__()

    async def __aenter__(self) -> MysqlCursor:
        return await self._get()

    async def __aexit__(self, exc_type, exc, tb) -> bool:
        if self._cursor is not None:
            await self._cursor.close()
        return False


# ---------------------------------------------------------------------------
# 异步连接
# ---------------------------------------------------------------------------


class MysqlConnection:
    """aiosqlite.Connection 兼容面,绑定一个作用域(表名前缀)。

    aiosqlite 通过单后台线程把同一连接上的操作天然串行化;这里用 asyncio.Lock
    复刻该语义——并发协程共用一条原始连接时,游标数据流不能交织。
    """

    def __init__(self, raw_conn, prefix: str):
        self._raw = raw_conn
        self._prefix = prefix
        self._op_lock = asyncio.Lock()

    @property
    def prefix(self) -> str:
        return self._prefix

    def execute(self, sql: str, parameters: Any = None) -> _PendingCursor:
        translated = _translate_sql(sql, self._prefix)

        async def _run() -> MysqlCursor:
            async with self._op_lock:
                cur = await self._raw.cursor()
                try:
                    if parameters is None:
                        await cur.execute(translated)
                    else:
                        await cur.execute(translated, tuple(parameters))
                except Exception:
                    await _close_cursor(cur)
                    raise
                return MysqlCursor(cur, self._op_lock)

        return _PendingCursor(_run)

    def executemany(self, sql: str, seq_of_parameters: Any) -> _PendingCursor:
        translated = _translate_sql(sql, self._prefix)

        async def _run() -> MysqlCursor:
            async with self._op_lock:
                cur = await self._raw.cursor()
                try:
                    await cur.executemany(
                        translated, [tuple(p) for p in seq_of_parameters]
                    )
                except Exception:
                    await _close_cursor(cur)
                    raise
                return MysqlCursor(cur, self._op_lock)

        return _PendingCursor(_run)

    async def commit(self) -> None:
        async with self._op_lock:
            await self._raw.commit()

    async def rollback(self) -> None:
        async with self._op_lock:
            await self._raw.rollback()

    async def close(self) -> None:
        await _release_async(self._raw)

    def executescript(self, script: str):
        """DDL 脚本执行(仅 schema runner 用;逐句执行并返回最终游标)。"""

        async def _run() -> MysqlCursor:
            async with self._op_lock:
                cur = await self._raw.cursor()
                for statement in _split_ddl(script):
                    await cur.execute(statement)
                return MysqlCursor(cur)

        return _PendingCursor(_run)


def _translate_sql(sql: str, prefix: str) -> str:
    sql = rewrite_sql(sql, prefix)
    return sql.replace("?", "%s")


def _split_ddl(script: str) -> list[str]:
    from novelvideo.db.ddl_translate import _split_statements

    return _split_statements(script)


# 连接池(按 DSN 缓存)与作用域绑定
_async_pool = None
_async_pool_lock = asyncio.Lock()
_async_pool_loop: Optional[asyncio.AbstractEventLoop] = None


async def _get_async_pool():
    global _async_pool, _async_pool_loop
    if _async_pool is not None and _async_pool_loop is asyncio.get_running_loop():
        return _async_pool
    async with _async_pool_lock:
        if _async_pool is None or _async_pool_loop is not asyncio.get_running_loop():
            import aiomysql

            from novelvideo.db.dsn import database_config

            cfg = database_config()
            _async_pool = await aiomysql.create_pool(
                host=cfg.host,
                port=cfg.port,
                user=cfg.user,
                password=cfg.password,
                db=cfg.database,
                charset=cfg.charset,
                autocommit=True,
                minsize=0,
                maxsize=cfg.pool_max,
                pool_recycle=1800,
            )
            _async_pool_loop = asyncio.get_running_loop()
    return _async_pool


async def acquire_async(scope_key: str) -> MysqlConnection:
    """从池中取一条连接并绑定到作用域(表名前缀)。"""

    pool = await _get_async_pool()
    raw = await pool.acquire()
    return MysqlConnection(raw, scope_key)


async def _release_async(raw) -> None:
    global _async_pool
    if _async_pool is not None:
        _async_pool.release(raw)
    else:  # pragma: no cover - 池尚未建立即释放,防御
        raw.close()


# ---------------------------------------------------------------------------
# 同步连接(pymysql)
# ---------------------------------------------------------------------------


class MysqlSyncCursor:
    """sqlite3.Cursor 兼容面子集。"""

    def __init__(self, cur):
        self._cur = cur
        self._executed = False

    @property
    def rowcount(self) -> int:
        return self._cur.rowcount

    @property
    def lastrowid(self):
        return self._cur.lastrowid

    def fetchone(self) -> Optional[Row]:
        columns = tuple(d[0] for d in self._cur.description or ())
        values = self._cur.fetchone()
        return Row(values, columns) if values is not None else None

    def fetchall(self) -> list[Row]:
        columns = tuple(d[0] for d in self._cur.description or ())
        return [Row(values, columns) for values in self._cur.fetchall()]

    def close(self) -> None:
        self._cur.close()


class MysqlSyncConnection:
    """sqlite3.Connection 兼容面子集(execute 返回已执行的游标)。"""

    def __init__(self, raw_conn, prefix: str):
        self._raw = raw_conn
        self._prefix = prefix

    @property
    def prefix(self) -> str:
        return self._prefix

    def cursor(self) -> "MysqlSyncRawCursor":
        return MysqlSyncRawCursor(self._raw.cursor(), self._prefix)

    def execute(self, sql: str, parameters: Any = None) -> MysqlSyncCursor:
        cur = self._raw.cursor()
        translated = _translate_sql(sql, self._prefix)
        try:
            if parameters is None:
                cur.execute(translated)
            else:
                cur.execute(translated, tuple(parameters))
        except Exception:
            cur.close()
            raise
        return MysqlSyncCursor(cur)

    def executemany(self, sql: str, seq_of_parameters: Any) -> MysqlSyncCursor:
        cur = self._raw.cursor()
        translated = _translate_sql(sql, self._prefix)
        try:
            cur.executemany(translated, [tuple(p) for p in seq_of_parameters])
        except Exception:
            cur.close()
            raise
        return MysqlSyncCursor(cur)

    def executescript(self, script: str) -> None:
        for statement in _split_ddl(script):
            cur = self._raw.cursor()
            try:
                cur.execute(statement)
            finally:
                cur.close()

    def commit(self) -> None:
        self._raw.commit()

    def rollback(self) -> None:
        self._raw.rollback()

    def close(self) -> None:
        # 线程本地缓存连接:归还而非关闭
        _SYNC_LOCAL.release(self._prefix, self._raw)


class MysqlSyncRawCursor:
    def __init__(self, cur, prefix: str):
        self._cur = cur
        self._prefix = prefix

    def execute(self, sql: str, parameters: Any = None) -> "MysqlSyncRawCursor":
        translated = _translate_sql(sql, self._prefix)
        if parameters is None:
            self._cur.execute(translated)
        else:
            self._cur.execute(translated, tuple(parameters))
        return self

    def fetchone(self) -> Optional[Row]:
        columns = tuple(d[0] for d in self._cur.description or ())
        values = self._cur.fetchone()
        return Row(values, columns) if values is not None else None

    def fetchall(self) -> list[Row]:
        columns = tuple(d[0] for d in self._cur.description or ())
        return [Row(values, columns) for values in self._cur.fetchall()]

    def close(self) -> None:
        self._cur.close()


class _SyncConnectionCache:
    """线程本地 pymysql 连接缓存(每作用域一条,复用 TCP)。"""

    def __init__(self) -> None:
        self._local = threading.local()

    def acquire(self, scope_key: str):
        cache = getattr(self._local, "conns", None)
        if cache is None:
            cache = self._local.conns = {}
        raw = cache.get(scope_key)
        if raw is not None:
            try:
                raw.ping(reconnect=True)
                return raw
            except Exception:
                try:
                    raw.close()
                except Exception:
                    pass
                cache.pop(scope_key, None)
        import pymysql

        from novelvideo.db.dsn import database_config

        cfg = database_config()
        raw = pymysql.connect(
            host=cfg.host,
            port=cfg.port,
            user=cfg.user,
            password=cfg.password,
            database=cfg.database,
            charset=cfg.charset,
            autocommit=True,
            connect_timeout=10,
        )
        cache[scope_key] = raw
        return raw

    def release(self, scope_key: str, raw) -> None:
        # 保持连接在线,等待线程本地复用;不做任何关闭
        return


_SYNC_LOCAL = _SyncConnectionCache()


def acquire_sync(scope_key: str) -> MysqlSyncConnection:
    raw = _SYNC_LOCAL.acquire(scope_key)
    return MysqlSyncConnection(raw, scope_key)


# ---------------------------------------------------------------------------
# 串行化写事务(GET_LOCK + 显式事务)
# ---------------------------------------------------------------------------


def _lock_name(lock: str) -> str:
    if len(lock) <= 64:
        return lock
    digest = hashlib.sha256(lock.encode("utf-8")).hexdigest()[:16]
    return f"{lock[:40]}:{digest}"


def begin_immediate_mysql_sync(conn, lock: Optional[str]):
    if not lock:
        raise ValueError("MySQL immediate_tx 必须提供 lock 名(SQLite 忽略该参数)")
    name = _lock_name(lock)
    prefix = getattr(conn, "_prefix", "")
    cur = conn._raw.cursor()
    try:
        cur.execute(_translate_sql("SELECT GET_LOCK(?, %s)", prefix), (name, _GET_LOCK_TIMEOUT_SECONDS))
        got = cur.fetchone()[0]
    finally:
        cur.close()
    if not got:
        raise TimeoutError(f"获取 MySQL 命名锁超时: {name}")

    @contextmanager
    def _tx():
        try:
            conn.execute("START TRANSACTION")
            try:
                yield
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        finally:
            cur2 = conn._raw.cursor()
            try:
                cur2.execute(_translate_sql("SELECT RELEASE_LOCK(?)", prefix), (name,))
            finally:
                cur2.close()

    return _tx()


async def begin_immediate_mysql_async(db, lock: Optional[str]):
    if not lock:
        raise ValueError("MySQL immediate_tx 必须提供 lock 名(SQLite 忽略该参数)")
    name = _lock_name(lock)
    pending = db.execute(
        "SELECT GET_LOCK(?, ?)", (name, _GET_LOCK_TIMEOUT_SECONDS)
    )
    row = await pending
    got = (await row.fetchone())[0]
    if not got:
        raise TimeoutError(f"获取 MySQL 命名锁超时: {name}")

    @asynccontextmanager
    async def _tx():
        try:
            await db.execute("START TRANSACTION")
            try:
                yield
                await db.commit()
            except BaseException:
                await db.rollback()
                raise
        finally:
            release_cur = await db.execute(
                "SELECT RELEASE_LOCK(?)", (name,)
            )
            await release_cur.fetchone()

    return _tx()


# ---------------------------------------------------------------------------
# 内省
# ---------------------------------------------------------------------------


def _resolve_table(conn, table: str) -> str:
    prefix = getattr(conn, "_prefix", "") or getattr(conn, "prefix", "")
    return f"{prefix}{table}"


def table_columns_mysql_sync(conn, table: str) -> list[str]:
    resolved = _resolve_table(conn, table)
    cur = conn._raw.cursor()
    try:
        cur.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s ORDER BY ordinal_position",
            (resolved,),
        )
        return [row[0] for row in cur.fetchall()]
    finally:
        cur.close()


async def table_columns_mysql_async(db, table: str) -> list[str]:
    resolved = _resolve_table(db, table)
    cur = await db.execute(
        "SELECT column_name FROM information_schema.columns "
        "WHERE table_schema = DATABASE() AND table_name = ? ORDER BY ordinal_position",
        (resolved,),
    )
    try:
        return [row[0] for row in await cur.fetchall()]
    finally:
        await cur.close()


async def table_exists_mysql_async(db, table: str) -> bool:
    resolved = _resolve_table(db, table)
    cur = await db.execute(
        "SELECT 1 FROM information_schema.tables "
        "WHERE table_schema = DATABASE() AND table_name = ?",
        (resolved,),
    )
    try:
        return (await cur.fetchone()) is not None
    finally:
        await cur.close()


async def close_async_pool() -> None:
    """进程退出时回收异步连接池。"""

    global _async_pool, _async_pool_loop
    if _async_pool is not None:
        try:
            _async_pool.close()
            await _async_pool.wait_closed()
        except Exception:  # pragma: no cover
            logger.debug("关闭 MySQL 连接池异常(忽略)", exc_info=True)
    _async_pool = None
    _async_pool_loop = None


__all__ = [
    "MysqlConnection",
    "MysqlCursor",
    "MysqlSyncConnection",
    "MysqlSyncCursor",
    "Row",
    "acquire_async",
    "acquire_sync",
    "begin_immediate_mysql_async",
    "begin_immediate_mysql_sync",
    "close_async_pool",
    "rewrite_sql",
    "table_columns_mysql_async",
    "table_columns_mysql_sync",
    "table_exists_mysql_async",
]
