"""SQLite persistence for the legacy project chat message database."""

from __future__ import annotations

import json
import shutil
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

from novelvideo.db.adapter import acquire_sync
from novelvideo.db.dsn import scope_identity
from novelvideo.db.schema_runner import ensure_mysql_schema
from novelvideo.db.sql_compat import (
    BACKEND_MYSQL,
    active_backend,
    upsert_sql,
)
from novelvideo.chat.store import (
    CHAT_DB_COLUMN_UPGRADES,
    CHAT_DB_SCHEMA_COMPONENT,
    CHAT_DB_SCHEMA_SQL,
    CHAT_DB_SCHEMA_VERSION,
)
from novelvideo.sqlite_pragmas import configure_sqlite_connection


def migrate_legacy_chat_db(
    legacy_db_path: Path, new_db_path: Path, *, create_parent: bool = True
) -> None:
    if new_db_path.exists() or not legacy_db_path.exists():
        return
    if not create_parent and not new_db_path.parent.exists():
        return
    if create_parent:
        new_db_path.parent.mkdir(parents=True, exist_ok=True)
    for suffix in ("", "-wal", "-shm"):
        src = Path(f"{legacy_db_path}{suffix}")
        if not src.exists():
            continue
        dst = Path(f"{new_db_path}{suffix}")
        if dst.exists():
            continue
        shutil.move(str(src), str(dst))

    legacy_dir = legacy_db_path.parent
    try:
        if legacy_dir.exists() and not any(legacy_dir.iterdir()):
            legacy_dir.rmdir()
    except OSError:
        pass


def connect(db_path: Path):
    """Open the legacy project chat DB on the active storage backend.

    The legacy database targets the same ``chat.db`` file as
    :mod:`novelvideo.chat.store`, so on MySQL both modules must agree on one
    canonical schema: whoever ensures the scope first creates the full table
    set (including the ``turn_id``/``metadata_json``/``idempotency_key``
    columns the legacy writer never uses) and the other ensure is a cached
    no-op. Ensuring only this module's narrower five-column script here would
    race ``chat.store``'s partial index creation against a missing
    ``idempotency_key`` column.
    """

    if active_backend() == BACKEND_MYSQL:
        key, relpath = scope_identity(db_path)
        ensure_mysql_schema(
            key,
            component=CHAT_DB_SCHEMA_COMPONENT,
            version=CHAT_DB_SCHEMA_VERSION,
            ddl_scripts=[CHAT_DB_SCHEMA_SQL],
            column_upgrades=CHAT_DB_COLUMN_UPGRADES,
            relpath=relpath,
        )
        return acquire_sync(key)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    configure_sqlite_connection(conn)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_settings (
          key TEXT PRIMARY KEY,
          value TEXT NOT NULL,
          updated_at TEXT NOT NULL
        )
        """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS chat_messages (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          role TEXT NOT NULL,
          content TEXT NOT NULL,
          media_json TEXT NOT NULL DEFAULT '[]',
          created_at TEXT NOT NULL
        )
        """)
    conn.commit()
    return conn


def load_chat_input_history(path: Path) -> list[str]:
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    if not isinstance(payload, list):
        return []
    history: list[str] = []
    for item in payload:
        text = str(item or "").strip()
        if text:
            history.append(text)
    return history


def save_chat_input_history(
    path: Path, history: list[str], *, limit: int = 200
) -> None:
    cleaned: list[str] = []
    for item in history:
        text = str(item or "").strip()
        if text:
            cleaned.append(text)
    if limit > 0:
        cleaned = cleaned[-limit:]
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_suffix(".tmp")
    tmp_path.write_text(
        json.dumps(cleaned, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    tmp_path.replace(path)


def get_setting(conn: sqlite3.Connection, key: str) -> str | None:
    # `key` 是 MySQL 保留字,反引号引用(SQLite 同样接受反引号标识符)。
    row = conn.execute(
        "SELECT value FROM chat_settings WHERE `key` = ?", (key,)
    ).fetchone()
    return str(row["value"]) if row else None


def set_setting(
    conn: sqlite3.Connection,
    key: str,
    value: str,
    *,
    now_iso: Callable[[], str],
) -> None:
    conn.execute(
        upsert_sql(
            "chat_settings",
            ("`key`",),
            ("`key`", "value", "updated_at"),
        ),
        (key, value, now_iso()),
    )
    conn.commit()


def append_message(
    conn: sqlite3.Connection,
    role: str,
    content: str,
    media: list[dict[str, Any]] | None = None,
    *,
    now_iso: Callable[[], str],
) -> dict[str, Any]:
    media = media or []
    created_at_iso = now_iso()
    cursor = conn.execute(
        """
        INSERT INTO chat_messages(role, content, media_json, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (role, content, json.dumps(media, ensure_ascii=False), created_at_iso),
    )
    conn.commit()
    return {
        "id": int(cursor.lastrowid),
        "role": role,
        "content": content,
        "media": media,
        "created_at": created_at_iso,
    }


def replace_trace_messages(
    conn: sqlite3.Connection,
    messages: list[dict[str, Any]],
    *,
    now_iso: Callable[[], str],
) -> None:
    conn.execute("DELETE FROM chat_messages WHERE role = 'trace'")
    for message in messages:
        conn.execute(
            """
            INSERT INTO chat_messages(role, content, media_json, created_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                str(message.get("role") or "assistant"),
                str(message.get("content") or ""),
                json.dumps(message.get("media") or [], ensure_ascii=False),
                str(message.get("created_at") or now_iso()),
            ),
        )
    conn.commit()


def history_contents(conn: sqlite3.Connection, role: str, *, limit: int) -> list[str]:
    rows = conn.execute(
        """
        SELECT content
          FROM chat_messages
         WHERE role = ?
         ORDER BY id DESC
         LIMIT ?
        """,
        (role, limit),
    ).fetchall()
    return [str(row["content"] or "") for row in reversed(rows)]


def recent_messages(conn: sqlite3.Connection, *, limit: int) -> list[sqlite3.Row]:
    # 派生表带别名:MySQL 要求 FROM 子查询必须有别名(SQLite 两者皆可)。
    return conn.execute(
        """
        SELECT id, role, content, media_json, created_at
          FROM (
                SELECT id, role, content, media_json, created_at
                  FROM chat_messages
                 WHERE role <> 'trace'
                 ORDER BY id DESC
                 LIMIT ?
               ) AS recent_messages
         ORDER BY id ASC
        """,
        (max(1, int(limit)),),
    ).fetchall()
