"""MySQL 后端门控测试套件。

仅当 ``NOVELVIDEO_DB_DSN`` 指向一个真实 MySQL 8(≥8.0.19)时运行
(``-m mysql`` 显式选中;默认套件不依赖外部数据库)。

作用域策略:每个测试用唯一用户名在真实 ``STATE_DIR`` 下派生独立表名前缀,
与生产路径解析逻辑完全一致;结束时清理该前缀下全部表并删除状态目录。
"""

from __future__ import annotations

import itertools
import os
import shutil

import pytest

from novelvideo.db.ddl_translate import translate_ddl_script

_DSN = os.environ.get("NOVELVIDEO_DB_DSN", "").strip()

pytestmark = [
    pytest.mark.mysql,
    pytest.mark.skipif(not _DSN, reason="NOVELVIDEO_DB_DSN 未配置,跳过 MySQL 门控套件"),
    pytest.mark.skipif(
        _DSN and not _DSN.lower().startswith(("mysql://", "mariadb://")),
        reason="NOVELVIDEO_DB_DSN 不是 MySQL DSN",
    ),
]

_USER_SEQ = itertools.count(1)


# ---------------------------------------------------------------------------
# 纯单元:SQL 渲染(不连库,固定后端)
# ---------------------------------------------------------------------------


def test_upsert_sql_render_both_backends(monkeypatch):
    from novelvideo.db import sql_compat

    monkeypatch.setattr(sql_compat, "is_mysql", lambda: False)
    assert sql_compat.upsert_sql(
        "characters", ("name",), ("name", "role"), touch_cols=("updated_at",)
    ) == (
        "INSERT INTO characters (name, role) VALUES (?, ?) "
        "ON CONFLICT(name) DO UPDATE SET role = excluded.role, updated_at = ?"
    )

    monkeypatch.setattr(sql_compat, "is_mysql", lambda: True)
    assert sql_compat.upsert_sql(
        "characters", ("name",), ("name", "role"), touch_cols=("updated_at",)
    ) == (
        "INSERT INTO characters (name, role) VALUES (?, ?) AS new "
        "ON DUPLICATE KEY UPDATE role = new.role, updated_at = ?"
    )


def test_insert_helpers_render_both_backends(monkeypatch):
    from novelvideo.db import sql_compat

    monkeypatch.setattr(sql_compat, "is_mysql", lambda: False)
    assert sql_compat.insert_ignore_sql("t", ("a", "b")) == (
        "INSERT OR IGNORE INTO t (a, b) VALUES (?, ?)"
    )
    assert sql_compat.replace_into_sql("t", ("a",)) == "INSERT OR REPLACE INTO t (a) VALUES (?)"
    assert sql_compat.insert_do_nothing_sql("t", ("a",), ("a",)) == (
        "INSERT INTO t (a) VALUES (?) ON CONFLICT(a) DO NOTHING"
    )

    monkeypatch.setattr(sql_compat, "is_mysql", lambda: True)
    assert sql_compat.insert_ignore_sql("t", ("a",)) == "INSERT IGNORE INTO t (a) VALUES (?)"
    assert sql_compat.replace_into_sql("t", ("a",)) == "REPLACE INTO t (a) VALUES (?)"
    assert sql_compat.insert_do_nothing_sql("t", ("a",), ("a",)) == (
        "INSERT IGNORE INTO t (a) VALUES (?)"
    )


def test_rewrite_sql_preserves_string_literals():
    from novelvideo.sqlite_store import SQLITE_SCHEMA_SQL
    from novelvideo.db.adapter import rewrite_sql
    from novelvideo.db.ddl_translate import known_tables

    translate_ddl_script(SQLITE_SCHEMA_SQL, "t_ut__")
    assert "characters" in known_tables()
    sql = rewrite_sql(
        "UPDATE characters SET role = 'failed', "
        "description = COALESCE(NULLIF(description, ''), ?) WHERE name = ?",
        "t_xx__",
    )
    assert "'failed'" in sql and "''" in sql
    assert "t_xx__characters" in sql


def test_scope_key_stable_and_prefixed(tmp_path, monkeypatch):
    import novelvideo.config as config
    from novelvideo.db.dsn import scope_key_for_path

    base = tmp_path / "state"
    key = scope_key_for_path(base / "u" / "p" / "data.db")
    assert key == scope_key_for_path(base / "u" / "p" / "data.db")
    assert key.startswith("t_") and key.endswith("__") and len(key) == 14

    # 全局库固定前缀:路径位于 STATE_DIR 之下时按相对路径匹配
    monkeypatch.setattr(config, "STATE_DIR", str(base))
    assert scope_key_for_path(base / "local" / "settings.db") == "g_settings__"
    assert scope_key_for_path(base / "newapi" / "one-api.db") == "g_newapi__"
    assert scope_key_for_path(base / "other" / "data.db") != "g_settings__"


def test_translate_ddl_grammar():
    from novelvideo.sqlite_store import SQLITE_SCHEMA_SQL
    from novelvideo.task_state import TaskStateManager, _TASK_STATE_SCHEMA_SQL

    stmts = translate_ddl_script(SQLITE_SCHEMA_SQL, "t_ut__")
    for s in stmts:
        if "CREATE TABLE" in s and "AUTO_INCREMENT" not in s:
            assert s.count("PRIMARY KEY") <= 1, s
    ee = next(s for s in stmts if "entity_evidence" in s and "CREATE TABLE" in s)
    assert ee.count("PRIMARY KEY") == 1
    assert "VARCHAR(64)" in ee and "VARCHAR(191)" in ee

    # 索引列中的纯 TEXT 收敛为 VARCHAR;DESC 方向保留且不在反引号内
    task_stmts = translate_ddl_script(
        _TASK_STATE_SCHEMA_SQL + TaskStateManager._TASK_STATE_INDEX_DDL, "t_ut__"
    )
    ts_table = next(s for s in task_stmts if "task_states" in s and "CREATE TABLE" in s)
    assert "`username` VARCHAR" in ts_table
    idx = [s for s in task_stmts if "idx_task_states_project_updated" in s]
    assert idx and "DESC" in idx[0] and "`updated_at DESC`" not in idx[0]


# ---------------------------------------------------------------------------
# 连库测试
# ---------------------------------------------------------------------------


@pytest.fixture()
def mysql_scope(tmp_path):
    """唯一用户名 → 真实 STATE_DIR 下派生独立作用域;结束清理表与状态目录。"""

    import pathlib

    from novelvideo.config import STATE_DIR
    from novelvideo.db.adapter import acquire_sync
    from novelvideo.db.dsn import scope_identity

    user = f"mt{os.getpid():x}{next(_USER_SEQ):03x}"
    project = "proj"
    db_path = pathlib.Path(os.path.abspath(STATE_DIR)) / user / project / "data.db"
    scope, relpath = scope_identity(db_path)
    conn = acquire_sync(scope)
    try:
        yield {
            "scope": scope,
            "relpath": relpath,
            "conn": conn,
            "user": user,
            "project": project,
            "db_path": db_path,
        }
    finally:
        cur = conn._raw.cursor()
        try:
            cur.execute(
                "SELECT table_name FROM information_schema.tables "
                "WHERE table_schema = DATABASE() AND table_name LIKE %s",
                (scope + "%",),
            )
            names = [r[0] for r in cur.fetchall()]
        finally:
            cur.close()
        cur = conn._raw.cursor()
        try:
            for name in names:
                cur.execute(f"DROP TABLE IF EXISTS `{name}`")
            cur.execute("DELETE FROM mv_scopes WHERE scope_key = %s", (scope,))
        finally:
            cur.close()
        shutil.rmtree(db_path.parent.parent, ignore_errors=True)


def _task_state_ddl():
    from novelvideo.task_state import TaskStateManager, _TASK_STATE_SCHEMA_SQL

    return _TASK_STATE_SCHEMA_SQL + TaskStateManager._TASK_STATE_INDEX_DDL


def test_schema_runner_idempotent_and_task_lifecycle(mysql_scope):
    scope_info = mysql_scope
    from novelvideo.db.schema_runner import ensure_mysql_schema
    from novelvideo.task_state import (
        _TASK_STATE_COLUMN_UPGRADES,
        _TASK_STATE_SCHEMA_COMPONENT,
        _TASK_STATE_SCHEMA_VERSION,
        TaskStateManager,
    )

    kwargs = dict(
        scope_key=scope_info["scope"],
        component=_TASK_STATE_SCHEMA_COMPONENT,
        version=_TASK_STATE_SCHEMA_VERSION,
        ddl_scripts=[_task_state_ddl()],
        column_upgrades=[
            ("task_states", c, s.split(" ADD COLUMN ", 1)[1])
            for c, s in _TASK_STATE_COLUMN_UPGRADES.items()
        ],
        relpath=scope_info["relpath"],
    )
    ensure_mysql_schema(**kwargs)
    ensure_mysql_schema(**kwargs)  # 幂等:重跑不抛错

    mgr = TaskStateManager()
    user, project = scope_info["user"], scope_info["project"]
    state = mgr.create_task("mysql_gated_type", user, project, episode=1, metadata={"k": "v"})
    got = mgr.get_task("mysql_gated_type", user, project, 1)
    assert got is not None and got.task_id == state.task_id
    assert got.metadata == {"k": "v"}

    mgr.update_progress("mysql_gated_type", user, project, 1, progress=0.5)
    assert mgr.get_task("mysql_gated_type", user, project, 1).progress == 0.5

    mgr.complete_task("mysql_gated_type", user, project, 1, result={"answer": 42})
    done = mgr.get_task("mysql_gated_type", user, project, 1)
    assert done.status == "completed" and done.result["answer"] == 42
    assert done.completed_at


def test_task_state_serialized_reserve(mysql_scope):
    """并发 reserve 同一业务任务:只允许一个成功(BEGIN IMMEDIATE 语义等价)。"""

    from novelvideo.project_context import ProjectContext
    from novelvideo.task_state import TaskStateManager

    scope_info = mysql_scope
    user, project = scope_info["user"], scope_info["project"]
    root = scope_info["db_path"].parent.parent  # state/<user>

    ctx = ProjectContext(
        project_id=f"proj_{scope_info['scope'][:8]}",
        project_name=project,
        owner_type="user",
        owner_id=user,
        owner_username=user,
        requester_user_id=user,
        requester_username=user,
        requester_principals=(),
        effective_role="owner",
        home_node_id="local",
        output_dir=root / "output",
        state_dir=root / project,
        runtime_dir=root / "runtime",
        is_home_node=True,
    )

    import asyncio

    async def scenario():
        mgr = TaskStateManager()
        results = await asyncio.gather(
            *[
                asyncio.to_thread(
                    mgr.reserve_task_for_project, ctx, "video_composer", 1, queue_kind="default"
                )
                for _ in range(6)
            ],
            return_exceptions=True,
        )
        wins = [r for r in results if not isinstance(r, BaseException) and r[1]]
        errors = [r for r in results if isinstance(r, BaseException)]
        assert not errors, errors
        assert len(wins) == 1, f"应只有一个 reserve 成功,实际 {len(wins)}"

    asyncio.run(scenario())


async def test_sqlite_store_crud_on_mysql(mysql_scope):
    """SQLiteStore 全链路(异步)跑在 MySQL 上:角色/场景/剧集/beat/并发去重。"""

    from novelvideo.models import (
        NovelCharacter,
        NovelEpisode,
        NovelScene,
        NovelVisualBeat,
    )
    from novelvideo.sqlite_store import SQLiteStore

    scope_info = mysql_scope
    user, project = scope_info["user"], scope_info["project"]
    root = scope_info["db_path"].parent.parent

    store = SQLiteStore(
        f"{user}/{project}",
        output_dir=str(root / "output" / project),
        state_dir=str(root / project),
    )
    try:
        await store.initialize()

        char = NovelCharacter(name="林小满", role="protagonist", is_main=True)
        await store.add_character(char)
        await store.add_character(NovelCharacter(name="林小满", role="protagonist", is_main=True))
        assert store.get_character("林小满") is not None

        await store.add_scene(NovelScene(name="便利店_夜"))
        assert await store.get_scene("便利店_夜") is not None

        await store.save_episode_content(1, "第一集正文")
        assert await store.load_episode_content(1) == "第一集正文"
        await store.save_episode_content(1, "第一集正文V2")
        assert await store.load_episode_content(1) == "第一集正文V2"

        await store.add_episodes([NovelEpisode(number=1, title="第一集")])
        assert store.get_episode(1) is not None

        await store.add_visual_beats(
            [NovelVisualBeat(episode_number=1, beat_number=1, narration="旁白")]
        )
        beats = await store.get_beats_for_episode(1)
        assert len(beats) == 1

        import asyncio as _aio

        await _aio.gather(
            *[store.add_scene(NovelScene(name="并发场景")) for _ in range(5)]
        )
        scenes = await store.list_scenes()
        assert sum(1 for s in scenes if s.name == "并发场景") == 1
    finally:
        await store.close()


async def test_project_column_upgrades_applied_on_mysql(mysql_scope):
    """旧库升级路径:建表 + 逐列升级把新列补齐(等价 _add_column_if_missing)。"""

    from novelvideo.sqlite_store import SQLiteStore

    scope_info = mysql_scope
    user, project = scope_info["user"], scope_info["project"]
    root = scope_info["db_path"].parent.parent
    store = SQLiteStore(
        f"{user}/{project}",
        output_dir=str(root / "output" / project),
        state_dir=str(root / project),
    )
    # initialize → _ensure_mysql_schema_sync:建表与逐列升级一并落地
    await store.initialize()
    await store.close()

    from novelvideo.db.adapter import table_columns_mysql_sync

    conn = scope_info["conn"]
    cols = table_columns_mysql_sync(conn, "episodes")
    assert "adapted_content" in cols
    assert "sketch_colors_json" in cols
    beat_cols = table_columns_mysql_sync(conn, "beats")
    assert "seedance2_config_json" in beat_cols
