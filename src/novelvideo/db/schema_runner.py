"""MySQL schema 迁移 runner。

替代 ``sqlite_schema.py`` 的 fcntl 文件锁 + WAL + 进程内 LRU 缓存机制:
* 跨进程串行化用 ``GET_LOCK('novelvideo_schema:<scope>')``;
* 迁移台账是全局单表 ``mv_schema_migrations``(scope_key, component, version);
* DDL 由 :mod:`novelvideo.db.ddl_translate` 从 SQLite schema 脚本翻译而来,
  顺带把表名登记进 adapter 的 DML 改写器注册表;
* 逐列升级用 information_schema 探测 + ALTER(并发冲突由 1060/1061 容忍)。

注意:本 runner 是同步实现——调用方与 SQLite 路径一致,放在
``asyncio.to_thread`` 里跑,避免阻塞事件循环。
"""

from __future__ import annotations

import logging
import threading
from typing import Iterable, Sequence

from novelvideo.db.adapter import acquire_sync
from novelvideo.db.ddl_translate import translate_column_definition, translate_ddl_script
from novelvideo.db.sql_compat import (
    is_duplicate_column_error,
    is_duplicate_index_error,
    utc_datetime_str,
)

logger = logging.getLogger(__name__)

_MIGRATIONS_TABLE = "mv_schema_migrations"

_MIGRATIONS_DDL = f"""
CREATE TABLE IF NOT EXISTS {_MIGRATIONS_TABLE} (
    scope_key  VARCHAR(20) NOT NULL,
    component  VARCHAR(64) NOT NULL,
    version    INT NOT NULL DEFAULT 0,
    applied_at VARCHAR(40) NOT NULL,
    PRIMARY KEY (scope_key, component)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
"""

# 作用域登记表:MySQL 下没有文件可枚举,跨作用域操作(如 list_tasks_for_user)
# 以登记的 (scope_key, relpath) 为准。幂等登记,首次 ensure 时写入。
_SCOPES_TABLE = "mv_scopes"

_SCOPES_DDL = f"""
CREATE TABLE IF NOT EXISTS {_SCOPES_TABLE} (
    scope_key  VARCHAR(20) NOT NULL PRIMARY KEY,
    relpath    VARCHAR(512) NOT NULL,
    created_at VARCHAR(40) NOT NULL
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_0900_ai_ci
"""

_SCHEMA_LOCK_TIMEOUT_SECONDS = 60

# 进程内已确保的 (scope, component, version):chat 等模块逐调用开连接,
# 每次都打 GET_LOCK + 查台账太贵;首次成功后本进程内直接跳过。
# 跨进程的首次并发仍由 GET_LOCK 串行化。
_ENSURED: set[tuple[str, str, int]] = set()
_ENSURED_LOCK = threading.Lock()


def ensure_mysql_schema(
    scope_key: str,
    component: str,
    version: int,
    ddl_scripts: Sequence[str],
    column_upgrades: Iterable[tuple[str, str, str]] = (),
    *,
    relpath: str = "",
) -> None:
    """确保 ``scope_key`` 逻辑库上 ``component`` 的 schema 达到 ``version``。

    ``ddl_scripts`` 是原始 SQLite schema 脚本(内部翻译成带前缀的 MySQL DDL);
    ``column_upgrades`` 是 ``(table, column, sqlite_definition)`` 逐列升级;
    ``relpath`` 登记进 mv_scopes(跨作用域枚举的依据)。幂等:台账已记录
    >= version 或本进程已确保过时直接返回。
    """

    memo = (scope_key, component, version)
    with _ENSURED_LOCK:
        if memo in _ENSURED:
            return

    conn = acquire_sync(scope_key)
    prefix = conn.prefix
    lock_name = f"novelvideo_schema:{scope_key}"

    # 无论是否需要执行,都要先翻译一遍:表名→前缀改写注册表由翻译过程填充,
    # 早退路径跳过翻译会导致本进程后续 DML 改写不到表名。
    translated: list[str] = []
    for script in ddl_scripts:
        translated.extend(translate_ddl_script(script, prefix))

    raw = conn._raw
    cur = raw.cursor()
    try:
        cur.execute("SELECT GET_LOCK(%s, %s)", (lock_name, _SCHEMA_LOCK_TIMEOUT_SECONDS))
        got = cur.fetchone()[0]
    finally:
        cur.close()
    if not got:
        raise TimeoutError(f"获取 schema 迁移锁超时: {lock_name}")

    try:
        conn.executescript(_MIGRATIONS_DDL)
        conn.executescript(_SCOPES_DDL)
        cur = raw.cursor()
        try:
            cur.execute(
                f"INSERT IGNORE INTO {_SCOPES_TABLE} (scope_key, relpath, created_at) "
                "VALUES (%s, %s, %s)",
                (scope_key, relpath, utc_datetime_str()),
            )
            cur.execute(
                f"SELECT version FROM {_MIGRATIONS_TABLE} WHERE scope_key = %s AND component = %s",
                (scope_key, component),
            )
            row = cur.fetchone()
        finally:
            cur.close()
        if row is not None and int(row[0]) >= version:
            with _ENSURED_LOCK:
                _ENSURED.add(memo)
            return

        for statement in translated:
            _exec_tolerating(conn, statement)
        for table, column, definition in column_upgrades:
            _ensure_column(conn, prefix, table, column, definition)

        cur = raw.cursor()
        try:
            cur.execute(
                f"INSERT INTO {_MIGRATIONS_TABLE} (scope_key, component, version, applied_at) "
                "VALUES (%s, %s, %s, %s) AS new "
                "ON DUPLICATE KEY UPDATE version = new.version, applied_at = new.applied_at",
                (scope_key, component, version, utc_datetime_str()),
            )
        finally:
            cur.close()
    finally:
        cur = raw.cursor()
        try:
            cur.execute("SELECT RELEASE_LOCK(%s)", (lock_name,))
        finally:
            cur.close()
        # acquire_sync 的线程本地连接不关闭;这里仅结束本函数的作用


def _exec_tolerating(conn, statement: str) -> None:
    """执行单条 DDL;幂等性冲突(列/索引已存在)容忍,其他错误上抛。"""

    cur = conn._raw.cursor()
    try:
        cur.execute(statement)
    except Exception as exc:
        if is_duplicate_column_error(exc) or is_duplicate_index_error(exc):
            logger.debug("MySQL schema 语句幂等跳过: %s", statement[:120])
            return
        raise
    finally:
        cur.close()


def _ensure_column(conn, prefix: str, table: str, column: str, definition: str) -> None:
    cur = conn._raw.cursor()
    try:
        cur.execute(
            "SELECT 1 FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = %s AND column_name = %s",
            (f"{prefix}{table}", column),
        )
        if cur.fetchone() is not None:
            return
    finally:
        cur.close()

    mysql_def = translate_column_definition(table, column, definition)
    _exec_tolerating(
        conn, f"ALTER TABLE `{prefix}{table}` ADD COLUMN {mysql_def}"
    )
