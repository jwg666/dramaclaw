"""SQLite → MySQL 一次性数据迁移工具。

用法(先停掉 API/worker,保证迁移期间源库不再写入):

    NOVELVIDEO_DB_DSN=mysql://user:pass%40escaped@host:3306/dbname \
        uv run python scripts/migrate_sqlite_to_mysql.py [--state-dir ./state] [--dry-run]

做法:遍历状态树里的每个 SQLite 文件,按与运行时完全一致的规则
(:func:`novelvideo.db.dsn.scope_identity`)派生表名前缀;从源库
``sqlite_master`` 读出**实际生效**的建表/建索引 DDL(天然包含所有历史
ALTER 加过的列),经 ``db.ddl_translate`` 翻译成 MySQL 建表,再逐表搬行。
幂等可重跑:重复执行时行数据用 INSERT IGNORE 跳过已存在主键。

不迁移:``cognee_system/``(LanceDB 向量库)、``-wal/-shm/-journal``、
SQLite 内部表(``sqlite_sequence``)、``novelvideo_schema_components``
(SQLite 侧版本标记,MySQL 侧由 ``db/schema_runner.py`` 自己的台账接管)。
newapi/one-api.db(网关库)默认跳过:网关切 MySQL 时按 compose 注释重建。
"""

from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

# 让脚本可以直接跑(不依赖包安装方式)
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from novelvideo.db.ddl_translate import translate_ddl_script  # noqa: E402
from novelvideo.db.dsn import database_config, scope_identity  # noqa: E402
from novelvideo.db.sql_compat import (  # noqa: E402
    active_backend,
    is_duplicate_column_error,
    is_duplicate_index_error,
)

SKIP_TABLES = {
    "sqlite_sequence",
    "novelvideo_schema_components",
    "sqlite_stat1",
    "sqlite_stat4",
}
BATCH_SIZE = 500


def iter_sqlite_files(state_dir: Path) -> list[Path]:
    files: list[Path] = []
    for path in sorted(state_dir.rglob("*.db")):
        rel = path.relative_to(state_dir).as_posix()
        if rel.startswith("cognee_system/"):
            continue
        if rel == "newapi/one-api.db":
            continue
        if path.name in ("data.db", "chat.db") or path.name.endswith(".db"):
            files.append(path)
    return files


def quoted(name: str) -> str:
    return f"`{name}`"


def create_tables_for_scope(source: sqlite3.Connection, prefix: str, target) -> list[str]:
    """按源库实际 DDL 建目标表与索引,返回已创建的表名(建表顺序)。

    表与索引必须作为**同一份脚本**交给翻译器:它做两遍扫描,先收集每张表
    被索引覆盖的列(纯 TEXT 进索引必须收敛为 VARCHAR,否则 MySQL 1170);
    只翻译表语句会让索引列漏收敛。
    """

    rows = source.execute(
        "SELECT name, type, sql FROM sqlite_master "
        "WHERE type IN ('table', 'index') AND name NOT LIKE 'sqlite_%' "
        "AND name != 'novelvideo_schema_components' ORDER BY rowid"
    ).fetchall()

    script_parts: list[str] = []
    table_names: list[str] = []
    for name, kind, sql in rows:
        if not sql:
            continue  # 自动索引(sqlite_autoindex_*)的 sql 为 NULL
        script_parts.append(sql + ";")
        if kind == "table":
            table_names.append(name)

    statements = translate_ddl_script("\n".join(script_parts), prefix)

    cur = target.cursor()
    cur.execute("SET FOREIGN_KEY_CHECKS=0")
    cur.close()
    for statement in statements:
        cur = target.cursor()
        try:
            cur.execute(statement)
        except Exception as exc:
            if is_duplicate_column_error(exc) or is_duplicate_index_error(exc):
                continue
            raise
        finally:
            cur.close()
    cur = target.cursor()
    cur.execute("SET FOREIGN_KEY_CHECKS=1")
    cur.close()
    return table_names


def copy_table(source: sqlite3.Connection, table: str, prefixed: str, target, *, dry_run: bool) -> tuple[int, int]:
    cur = source.execute(f"SELECT * FROM {quoted(table)}")
    columns = [d[0] for d in cur.description]
    col_sql = ", ".join(quoted(c) for c in columns)
    placeholders = ", ".join(["%s"] * len(columns))
    insert_sql = f"INSERT IGNORE INTO {quoted(prefixed)} ({col_sql}) VALUES ({placeholders})"

    total = 0
    inserted = 0
    while True:
        rows = cur.fetchmany(BATCH_SIZE)
        if not rows:
            break
        total += len(rows)
        if not dry_run:
            tcur = target.cursor()
            try:
                tcur.executemany(insert_sql, [tuple(r) for r in rows])
                target.commit()
                inserted += tcur.rowcount if tcur.rowcount and tcur.rowcount > 0 else len(rows)
            finally:
                tcur.close()
    return total, inserted if not dry_run else total


def migrate_file(source_path: Path, target, *, dry_run: bool, only: str | None) -> None:
    scope_key, relpath = scope_identity(source_path)
    source = sqlite3.connect(f"file:{source_path}?mode=ro", uri=True)
    source.row_factory = None
    try:
        tables = [
            r[0]
            for r in source.execute(
                "SELECT name FROM sqlite_master WHERE type='table' "
                "AND name NOT LIKE 'sqlite_%' AND name != 'novelvideo_schema_components'"
            ).fetchall()
        ]
        if only:
            tables = [t for t in tables if t == only]
        if not tables:
            return

        if not dry_run:
            # 建表 + 登记 mv_scopes
            created = create_tables_for_scope(source, scope_key, target)
            _ = created
            cur = target.cursor()
            try:
                cur.execute(
                    "CREATE TABLE IF NOT EXISTS mv_scopes ("
                    "scope_key VARCHAR(20) NOT NULL PRIMARY KEY, "
                    "relpath VARCHAR(512) NOT NULL, created_at VARCHAR(40) NOT NULL"
                    ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4"
                )
                cur.execute(
                    "INSERT IGNORE INTO mv_scopes (scope_key, relpath, created_at) VALUES (%s, %s, UTC_TIMESTAMP())",
                    (scope_key, relpath),
                )
                target.commit()
            finally:
                cur.close()

        print(f"== {relpath} -> 前缀 {scope_key} ({len(tables)} 表)")
        grand_total = 0
        for table in tables:
            try:
                total, inserted = copy_table(source, table, f"{scope_key}{table}", target, dry_run=dry_run)
            except sqlite3.Error as exc:
                print(f"   !! {table}: 源库读取失败 {exc}")
                continue
            grand_total += total
            print(f"   {table}: {total} 行" + ("" if dry_run else f"(写入 {inserted})"))
        print(f"   小计 {grand_total} 行")
    finally:
        source.close()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--state-dir", default=os.environ.get("NOVELVIDEO_STATE_DIR", "./state"),
                        help="SQLite 状态目录(默认 NOVELVIDEO_STATE_DIR 或 ./state)")
    parser.add_argument("--dry-run", action="store_true", help="只统计行数,不写 MySQL")
    parser.add_argument("--table", default=None, help="只迁移指定表名")
    parser.add_argument("--file", default=None, help="只迁移相对路径匹配的库(如 u/p/data.db)")
    args = parser.parse_args()

    if active_backend() != "mysql":
        print("错误:NOVELVIDEO_DB_DSN 未配置或不是 mysql://", file=sys.stderr)
        return 2
    cfg = database_config()
    print(f"目标 MySQL:{cfg.redacted()}")

    import pymysql

    target = pymysql.connect(
        host=cfg.host,
        port=cfg.port,
        user=cfg.user,
        password=cfg.password,
        database=cfg.database,
        charset=cfg.charset,
        autocommit=False,
        connect_timeout=10,
    )

    state_dir = Path(args.state_dir)
    if not state_dir.exists():
        print(f"错误:状态目录不存在 {state_dir}", file=sys.stderr)
        return 2

    try:
        files = iter_sqlite_files(state_dir)
        if args.file:
            needle = args.file.replace("\\", "/")
            files = [f for f in files if f.relative_to(state_dir).as_posix().find(needle) >= 0]
        print(f"发现 {len(files)} 个 SQLite 文件" + ("(dry-run)" if args.dry_run else ""))
        failures = 0
        for path in files:
            try:
                migrate_file(path, target, dry_run=args.dry_run, only=args.table)
            except Exception as exc:
                failures += 1
                print(f"!! {path}: 迁移失败 {exc}", file=sys.stderr)
        if failures:
            print(f"完成,但 {failures} 个文件失败,见上方日志", file=sys.stderr)
            return 1
        print("全部完成。启动应用后首次访问各逻辑库会自动补齐 schema 台账。")
        return 0
    finally:
        target.close()


if __name__ == "__main__":
    raise SystemExit(main())
