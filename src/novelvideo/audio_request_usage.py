"""Project-local audio generation attempt records."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path

from novelvideo.config import OUTPUT_DIR, STATE_DIR
from novelvideo.db.sql_compat import BACKEND_MYSQL, active_backend, insert_do_nothing_sql
from novelvideo.sqlite_pragmas import configure_sqlite_connection
from novelvideo.sqlite_schema import ensure_sqlite_schema


_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS audio_request_usage (
    request_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    model_name TEXT,
    task_type TEXT NOT NULL,
    scope TEXT NOT NULL,
    episode INTEGER,
    speaker TEXT,
    status TEXT NOT NULL DEFAULT 'accepted',
    accepted_at TEXT NOT NULL,
    completed_at TEXT,
    updated_at TEXT NOT NULL,
    error_message TEXT
);
CREATE INDEX IF NOT EXISTS idx_audio_request_usage_scope
ON audio_request_usage(task_type, scope, accepted_at DESC);
"""

_SCHEMA_COMPONENT = "audio_request_usage"
# MIGRATION CONTRACT: increment this whenever _SCHEMA_SQL changes.
_SCHEMA_VERSION = 1


def get_audio_request_usage_db_path(project_output_dir: str | Path) -> Path:
    project_output_dir = Path(project_output_dir).resolve()
    output_root = Path(OUTPUT_DIR).resolve()
    state_root = Path(STATE_DIR).resolve()
    try:
        rel = project_output_dir.relative_to(output_root)
    except ValueError:
        return (project_output_dir / "data.db").resolve()
    return (state_root / rel / "data.db").resolve()


@contextmanager
def _connect(project_output_dir: str | Path):
    db_path = get_audio_request_usage_db_path(project_output_dir)
    if active_backend() == BACKEND_MYSQL:
        # 与项目 data.db 同一逻辑库(表名前缀);schema 走迁移台账
        from novelvideo.db.adapter import acquire_sync
        from novelvideo.db.dsn import scope_identity
        from novelvideo.db.schema_runner import ensure_mysql_schema

        key, relpath = scope_identity(db_path)
        ensure_mysql_schema(
            key,
            component=_SCHEMA_COMPONENT,
            version=_SCHEMA_VERSION,
            ddl_scripts=[_SCHEMA_SQL],
            relpath=relpath,
        )
        conn = acquire_sync(key)
        try:
            yield conn
            conn.commit()
        finally:
            # 线程本地缓存连接,close() 归属 adapter,不真正断开
            conn.close()
        return
    db_path.parent.mkdir(parents=True, exist_ok=True)
    ensure_sqlite_schema(
        db_path,
        component=_SCHEMA_COMPONENT,
        version=_SCHEMA_VERSION,
        initialize=lambda schema_conn: schema_conn.executescript(_SCHEMA_SQL),
    )
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    configure_sqlite_connection(conn, set_journal_mode=False)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def record_audio_generation_attempt(
    *,
    project_output_dir: str | Path,
    request_id: str,
    provider: str,
    model_name: str,
    task_type: str,
    scope: str,
    episode: int | None = None,
    speaker: str | None = None,
) -> None:
    now = datetime.now().isoformat()
    with _connect(project_output_dir) as conn:
        conn.execute(
            insert_do_nothing_sql(
                "audio_request_usage",
                (
                    "request_id", "provider", "model_name", "task_type", "scope",
                    "episode", "speaker", "status", "accepted_at", "updated_at",
                ),
                ("request_id",),
            ),
            (
                request_id,
                provider,
                model_name,
                task_type,
                scope,
                episode,
                speaker,
                "accepted",
                now,
                now,
            ),
        )


def update_audio_generation_attempt(
    *,
    project_output_dir: str | Path,
    request_id: str,
    status: str,
    error_message: str | None = None,
) -> None:
    now = datetime.now().isoformat()
    completed_at = now if status in {"completed", "failed"} else None
    with _connect(project_output_dir) as conn:
        conn.execute(
            """
            UPDATE audio_request_usage
            SET status = ?,
                updated_at = ?,
                completed_at = COALESCE(?, completed_at),
                error_message = COALESCE(?, error_message)
            WHERE request_id = ?
            """,
            (status, now, completed_at, error_message, request_id),
        )


def count_audio_scope_attempts(
    *,
    project_output_dir: str | Path,
    task_type: str,
    scope: str,
    episode: int | None = None,
) -> int:
    where = ["task_type = ?", "scope = ?"]
    params: list[object] = [task_type, scope]
    if episode is not None:
        where.append("episode = ?")
        params.append(int(episode))
    with _connect(project_output_dir) as conn:
        row = conn.execute(
            f"SELECT COUNT(*) FROM audio_request_usage WHERE {' AND '.join(where)}",
            tuple(params),
        ).fetchone()
    return int(row[0] or 0) if row else 0
