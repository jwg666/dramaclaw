"""User-level shared `verification.db` — canonical source for failure defs.

Lives at `state/<user>/_shared/verification.db`. Holds only the
`sketch_failure_mode_defs` table — the knowledge layer of the director
OS. Project-local `data.db` stores only per-project hits and
convergence_rounds; mixing the two is forbidden by design (see
`plans/frolicking-hopping-karp.md` for the rationale).

`ensure_defs_seeded` refreshes the canonical rows from
`failure_registry._SEED_FAILURE_MODES` with UPSERT semantics so seed
edits ship with the next app start. Project-local historical rows are
not consulted from here; the one-shot `seed_mirror_once` CLI handles
that migration path separately.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import aiosqlite

from novelvideo.db.sql_compat import (
    BACKEND_MYSQL,
    active_backend,
    upsert_sql,
    utc_datetime_str,
)
from novelvideo.sqlite_pragmas import configure_sqlite_connection_async


DEFS_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS sketch_failure_mode_defs (
    code                     TEXT PRIMARY KEY,
    layer                    TEXT NOT NULL,
    detection                TEXT NOT NULL,
    prevention_rule          TEXT DEFAULT '',
    correction_template      TEXT DEFAULT '',
    negative_prompt_clause   TEXT DEFAULT '',
    gate_enabled             INTEGER DEFAULT 0,
    fixture_path             TEXT DEFAULT '',
    created_at               TEXT DEFAULT (datetime('now')),
    updated_at               TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_defs_layer ON sketch_failure_mode_defs(layer);
CREATE INDEX IF NOT EXISTS idx_defs_gate_enabled ON sketch_failure_mode_defs(gate_enabled);
"""

_DEFS_SCHEMA_COMPONENT = "verification_defs"
# MIGRATION CONTRACT: increment this whenever DEFS_SCHEMA_SQL changes.
_DEFS_SCHEMA_VERSION = 1


def ensure_defs_schema_mysql(db_path: Path) -> None:
    """MySQL 后端:在 verification.db 逻辑库(g_verification__ 作用域)上
    确保失败模式定义表 schema(阻塞,建议在 to_thread 中调用)。"""

    from novelvideo.db.dsn import scope_identity
    from novelvideo.db.schema_runner import ensure_mysql_schema

    key, relpath = scope_identity(db_path)
    ensure_mysql_schema(
        key,
        component=_DEFS_SCHEMA_COMPONENT,
        version=_DEFS_SCHEMA_VERSION,
        ddl_scripts=[DEFS_SCHEMA_SQL],
        relpath=relpath,
    )


async def open_defs_db(db_path: Path):
    """Open (or create) the shared verification DB.

    Ensures the parent directory exists and the schema is present.
    Callers are responsible for closing the connection.
    Returns an aiosqlite connection on SQLite, an adapter async
    connection (same execute/commit/close surface) on MySQL.
    """
    db_path = Path(db_path).expanduser()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    if active_backend() == BACKEND_MYSQL:
        from novelvideo.db.adapter import acquire_async
        from novelvideo.db.dsn import scope_identity

        await asyncio.to_thread(ensure_defs_schema_mysql, db_path)
        key, _relpath = scope_identity(db_path)
        return await acquire_async(key)
    db = await aiosqlite.connect(str(db_path))
    db.row_factory = aiosqlite.Row
    await configure_sqlite_connection_async(db)
    await db.executescript(DEFS_SCHEMA_SQL)
    await db.commit()
    return db


async def ensure_defs_seeded(
    db: aiosqlite.Connection,
    seeds: list[dict[str, Any]],
) -> None:
    """Idempotently UPSERT the seed list into the defs table.

    Canonical descriptive columns (layer / detection / prevention_rule /
    correction_template / negative_prompt_clause / gate_enabled) always
    refresh from the seed so editing the seed list in code ships on
    next start. Timestamps bump; fixture_path is not touched once
    stored (fixtures are tracked separately).
    """
    for entry in seeds:
        await db.execute(
            upsert_sql(
                "sketch_failure_mode_defs",
                ("code",),
                (
                    "code", "layer", "detection", "prevention_rule",
                    "correction_template", "negative_prompt_clause",
                    "gate_enabled", "fixture_path", "created_at", "updated_at",
                ),
                set_sql=(
                    "layer = excluded.layer, "
                    "detection = excluded.detection, "
                    "prevention_rule = excluded.prevention_rule, "
                    "correction_template = excluded.correction_template, "
                    "negative_prompt_clause = excluded.negative_prompt_clause, "
                    "gate_enabled = excluded.gate_enabled"
                ),
                touch_cols=("updated_at",),
            ),
            (
                entry["code"],
                entry["layer"],
                entry["detection"],
                entry.get("prevention_rule", ""),
                entry.get("correction_template", ""),
                entry.get("negative_prompt_clause", ""),
                int(entry.get("gate_enabled", 0)),
                "",
                utc_datetime_str(),
                utc_datetime_str(),
                utc_datetime_str(),
            ),
        )
    await db.commit()
