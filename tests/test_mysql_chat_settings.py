"""MySQL 后端门控测试:chat 存储 / 网关设置 / 项目注册表。

仅当 ``NOVELVIDEO_DB_DSN`` 指向真实 MySQL 8 时运行(``-m mysql`` 显式选中;
默认套件不依赖外部数据库)。结构沿用 tests/test_mysql_backend.py:

* chat scope:唯一用户名在真实 STATE_DIR 下派生独立表名前缀(与
  ``chat_store.db_for`` 的生产路径解析完全一致),结束清理该前缀全部表并
  删除 mv_scopes 登记行;
* settings.db / projects.db 是节点级全局作用域(g_settings__/g_projects__),
  **不可整表清理** —— 只删除本测试写入的键/行。
"""

from __future__ import annotations

import itertools
import os
import shutil
import threading

import pytest

_USER_SEQ = itertools.count(1)

_DSN = os.environ.get("NOVELVIDEO_DB_DSN", "").strip()

pytestmark = [
    pytest.mark.mysql,
    pytest.mark.skipif(not _DSN, reason="NOVELVIDEO_DB_DSN 未配置,跳过 MySQL 门控套件"),
    pytest.mark.skipif(
        _DSN and not _DSN.lower().startswith(("mysql://", "mariadb://")),
        reason="NOVELVIDEO_DB_DSN 不是 MySQL DSN",
    ),
]


def _unique_token() -> str:
    return f"mt{os.getpid():x}{next(_USER_SEQ):04x}"


def _drop_scope_tables(conn, scope: str) -> None:
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


# ---------------------------------------------------------------------------
# chat store
# ---------------------------------------------------------------------------


@pytest.fixture()
def mysql_chat_scope():
    """唯一用户 → chat home scope;真实 db_for 路径派生作用域,结束清理。"""

    from novelvideo.chat.store import chat_store
    from novelvideo.db.adapter import acquire_sync
    from novelvideo.db.dsn import scope_identity

    user = _unique_token()
    scope = chat_store.db_for(user, _home_scope(user))
    key, relpath = scope_identity(scope)
    conn = acquire_sync(key)
    try:
        yield {
            "user": user,
            "db_path": scope,
            "key": key,
            "relpath": relpath,
            "conn": conn,
        }
    finally:
        _drop_scope_tables(conn, key)
        shutil.rmtree(scope.parent.parent, ignore_errors=True)


def _home_scope(user: str):
    from novelvideo.chat.store import ChatScope

    return ChatScope(kind="home")


async def test_chat_store_message_roundtrip_on_mysql(mysql_chat_scope):
    """MySQL 上的消息写入/读取闭环 + 幂等键去重 + 清空。"""

    from novelvideo.chat.store import chat_store

    info = mysql_chat_scope
    user = info["user"]
    scope = _home_scope(user)

    first = await chat_store.append_message_async(
        user, scope, "user", "你好，虾导", media=[{"type": "text", "text": "你好"}]
    )
    assert first["id"] > 0
    assert first["role"] == "user"

    second = await chat_store.append_message_async(
        user, scope, "assistant", "收到", turn_id=first.get("turn_id")
    )
    messages = await chat_store.list_messages_async(user, scope, limit=50)
    contents = [m["content"] for m in messages]
    assert "你好，虾导" in contents
    assert "收到" in contents
    assert len(messages) == 2
    by_id = {m["id"]: m for m in messages}
    assert by_id[first["id"]]["media"] == [{"type": "text", "text": "你好"}]

    # 幂等键:同键重复写入返回已存在行,不产生新消息(首写 1 行 + 去重 1 行)
    dup = await chat_store.append_message_async(
        user,
        scope,
        "user",
        "你好，虾导",
        idempotency_key="idem-mysql-1",
    )
    dup_again = await chat_store.append_message_async(
        user,
        scope,
        "user",
        "你好，虾导",
        idempotency_key="idem-mysql-1",
    )
    assert dup["id"] == dup_again["id"]
    assert len(await chat_store.list_messages_async(user, scope, limit=50)) == 3

    # 清空:MySQL 上没有文件存在性短路,必须真的删
    removed = await chat_store.clear_messages_async(user, scope)
    assert removed >= 3
    assert await chat_store.list_messages_async(user, scope, limit=50) == []


async def test_chat_store_history_contents_and_ui_events_on_mysql(mysql_chat_scope):
    from novelvideo.chat.store import chat_store

    info = mysql_chat_scope
    user = info["user"]
    scope = _home_scope(user)

    await chat_store.append_ui_event_async(user, scope, "turn-1", {"type": "tool_started"})
    await chat_store.append_message_async(user, scope, "assistant", "回答", turn_id="turn-1")

    history = await chat_store.history_contents_async(user, scope, "assistant", limit=10)
    assert history == ["回答"]

    messages = await chat_store.list_messages_async(user, scope, limit=10)
    assert messages[0]["ui_events"][0]["type"] == "tool_started"


# ---------------------------------------------------------------------------
# model gateway settings
# ---------------------------------------------------------------------------


@pytest.fixture()
def mysql_settings_env(monkeypatch):
    """CE 运行时环境 + 本测试唯一的设置键前缀;结束删除写入的键。"""

    from novelvideo.db.adapter import acquire_sync

    monkeypatch.delenv("ST_CONTROL_PLANE_DSN", raising=False)
    monkeypatch.delenv("ST_EDITION", raising=False)

    prefix = f"mtx_{_unique_token()}_"

    def cleanup() -> None:
        conn = acquire_sync("g_settings__")
        cur = conn._raw.cursor()
        try:
            cur.execute(
                "DELETE FROM g_settings__runtime_settings WHERE `key` LIKE %s",
                (prefix + "%",),
            )
        finally:
            cur.close()

    yield prefix
    cleanup()


def test_settings_write_read_roundtrip_on_mysql(mysql_settings_env):
    from novelvideo.model_gateway_settings import _read_all, _write_many

    prefix = mysql_settings_env
    keys = {
        f"{prefix}alpha": "值一",
        f"{prefix}beta": "",
        f"{prefix}gamma": "x" * 500,
    }
    _write_many(keys)
    read_back = {
        key: value
        for key, value in _read_all().items()
        if key.startswith(prefix)
    }
    assert read_back == keys

    # 覆盖写:同一键再次写入取最新值
    _write_many({f"{prefix}alpha": "值二"})
    assert _read_all()[f"{prefix}alpha"] == "值二"


def test_settings_concurrent_writes_serialize_on_mysql(mysql_settings_env):
    """并发 _write_many:GET_LOCK 串行化,全部落盘、互不丢失。"""

    from novelvideo.model_gateway_settings import _read_all, _write_many

    prefix = mysql_settings_env
    errors: list[BaseException] = []

    def write(index: int) -> None:
        try:
            _write_many({f"{prefix}w{index}": str(index)})
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=write, args=(i,)) for i in range(6)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not errors, errors

    read_back = _read_all()
    for index in range(6):
        assert read_back[f"{prefix}w{index}"] == str(index)


# ---------------------------------------------------------------------------
# project registry
# ---------------------------------------------------------------------------


@pytest.fixture()
def mysql_project_registry():
    """全局 g_projects__ 作用域:记录创建的 project id,结束逐行删除。"""

    from novelvideo.db.adapter import acquire_sync

    token = _unique_token()
    created: list[str] = []

    def cleanup() -> None:
        if not created:
            return
        conn = acquire_sync("g_projects__")
        cur = conn._raw.cursor()
        try:
            marks = ", ".join(["%s"] * len(created))
            cur.execute(
                f"DELETE FROM g_projects__projects WHERE id IN ({marks})",
                tuple(created),
            )
        finally:
            cur.close()

    yield {"token": token, "created": created}
    cleanup()


async def test_project_registry_create_list_on_mysql(mysql_project_registry):
    from novelvideo.ports.local.project import SQLiteProjectRegistry

    info = mysql_project_registry
    token = info["token"]
    created = info["created"]
    registry = SQLiteProjectRegistry()

    record = await registry.create_project(
        owner_user_id="local",
        owner_username=token,
        name=f"proj-{token}",
    )
    created.append(record.id)
    assert record.owner_username == token
    assert record.status == "active"
    assert record.state_dir and record.output_dir

    fetched = await registry.get_project(record.id)
    assert fetched is not None and fetched.id == record.id

    listed = await registry.list_accessible_projects([("user", "local")])
    assert any(item.id == record.id for item in listed)

    # 重名冲突:MySQL 1062 → ValueError(与 SQLite IntegrityError 同语义)
    with pytest.raises(ValueError, match="already exists"):
        await registry.create_project(
            owner_user_id="local",
            owner_username=token,
            name=f"proj-{token}",
        )

    updated = await registry.update_project_status(record.id, "archived")
    assert updated is not None and updated.status == "archived"

    await registry.delete_uncommitted_project(record.id)
    assert await registry.get_project(record.id) is None
    created.remove(record.id)
