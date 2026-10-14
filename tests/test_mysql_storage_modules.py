"""MySQL 门控:同步/辅助存储模块的 MySQL 后端往返。

覆盖范围(与 Phase 2 分支改造一一对应):
- image/video/audio usage 台账(项目 data.db 逻辑库)record + count/summary;
- brainclaw outcome 运行时(insert + _run 的取数 SQL);
- verification failure_registry(bump_hit 项目命中 + load 负向条款,_defs 走
  g_verification__ 全局作用域)。

仅当 ``NOVELVIDEO_DB_DSN`` 指向真实 MySQL 8(≥8.0.19)时运行
(``-m mysql`` 显式选中;默认套件不依赖外部数据库)。fixture 模式复制自
``tests/test_mysql_backend.py``:唯一用户名 → 真实 STATE_DIR 下派生独立
作用域;结束清理该前缀下全部表并删除状态目录。
"""

from __future__ import annotations

import asyncio
import hashlib
import itertools
import os
import pathlib
import shutil
import time

import pytest

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


@pytest.fixture()
def mysql_scope():
    """唯一用户名 → 真实 STATE_DIR 下派生独立作用域;结束清理表与状态目录。"""

    from novelvideo.config import STATE_DIR
    from novelvideo.db.adapter import acquire_sync
    from novelvideo.db.dsn import scope_identity

    user = f"ms{os.getpid():x}{next(_USER_SEQ):03x}"
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


def _project_output_dir(scope_info: dict) -> pathlib.Path:
    """OUTPUT_DIR/<user>/<project>:usage 模块解析回同一 data.db 作用域。"""

    from novelvideo.config import OUTPUT_DIR

    return pathlib.Path(OUTPUT_DIR) / scope_info["user"] / scope_info["project"]


def test_usage_ledger_roundtrip_on_mysql(mysql_scope):
    """image/video/audio usage 台账 + seedance2 语音凭证在同一作用域的往返。"""

    project_output_dir = _project_output_dir(mysql_scope)

    from novelvideo.audio_request_usage import (
        count_audio_scope_attempts,
        record_audio_generation_attempt,
    )
    from novelvideo.image_request_usage import (
        count_image_scope_attempts,
        get_image_usage_summary,
        record_image_request,
        update_image_request_status,
    )
    from novelvideo.video_request_usage import (
        count_video_beat_attempts,
        get_video_usage_summary,
        record_video_request,
    )

    record_image_request(
        project_output_dir=project_output_dir,
        request_id="img-1",
        provider="test",
        model_name="image-model",
        task_type="render",
        scope="beat:1",
        episode=1,
        beat_num=1,
        character_name="林昭",
        identity_name="lin",
    )
    record_image_request(
        project_output_dir=project_output_dir,
        request_id="img-2",
        provider="test",
        model_name="image-model",
        task_type="render",
        scope="beat:1",
        episode=1,
        beat_num=1,
    )
    update_image_request_status(
        project_output_dir=project_output_dir,
        request_id="img-2",
        status="failed",
        error_message="未返回图像数据",
    )
    # 非计费失败(匹配 _NON_BILLABLE_FAILURE_PATTERNS)不计入 attempts
    assert (
        count_image_scope_attempts(
            project_output_dir=project_output_dir,
            task_type="render",
            scope="beat:1",
            episode=1,
        )
        == 1
    )
    summary = get_image_usage_summary(
        project_output_dir=project_output_dir, task_types=("render",)
    )
    assert summary["total_requests"] == 1

    record_video_request(
        project_output_dir=project_output_dir,
        request_id="vid-1",
        provider="test",
        model_name="video-model",
        episode=1,
        beat_num=1,
        task_type="single_video",
        duration_seconds=5.0,
    )
    assert (
        count_video_beat_attempts(
            project_output_dir=project_output_dir, episode=1, beat_num=1
        )
        == 1
    )
    video_summary = get_video_usage_summary(
        project_output_dir=project_output_dir, episode=1
    )
    assert video_summary["total_requests"] == 1
    assert video_summary["total_duration_seconds"] == pytest.approx(5.0)

    record_audio_generation_attempt(
        project_output_dir=project_output_dir,
        request_id="aud-1",
        provider="test",
        model_name="audio-model",
        task_type="voice",
        scope="beat:1",
        episode=1,
        speaker="林昭",
    )
    assert (
        count_audio_scope_attempts(
            project_output_dir=project_output_dir, task_type="voice", scope="beat:1"
        )
        == 1
    )

    # 同作用域上的 seedance2 语音复用凭证(独立组件,同一张 data.db)
    from novelvideo.seedance2_i2v.voice_audio_records import (
        get_seedance2_voice_audio_record,
        upsert_seedance2_voice_audio_record,
    )

    db_path = mysql_scope["db_path"]
    upsert_seedance2_voice_audio_record(
        db_path=db_path,
        episode_number=1,
        beat_number=1,
        speaker="林昭",
        audio_path="voice-1.wav",
        voice_sha256="sha-1",
        mode="reference",
        provider="test",
        model="voice-model",
        status="completed",
    )
    record = get_seedance2_voice_audio_record(
        db_path=db_path, episode_number=1, beat_number=1, speaker="林昭"
    )
    assert record is not None
    assert record.voice_sha256 == "sha-1" and record.status == "completed"
    # 幂等 upsert(同主键覆盖)
    upsert_seedance2_voice_audio_record(
        db_path=db_path,
        episode_number=1,
        beat_number=1,
        speaker="林昭",
        audio_path="voice-2.wav",
        voice_sha256="sha-2",
        mode="reference",
        provider="test",
        model="voice-model",
        status="completed",
    )
    record = get_seedance2_voice_audio_record(
        db_path=db_path, episode_number=1, beat_number=1, speaker="林昭"
    )
    assert record is not None and record.voice_sha256 == "sha-2"


def test_brainclaw_outcome_roundtrip_on_mysql(mysql_scope, monkeypatch, tmp_path):
    """BrainClaw outcome 运行时:enqueue 落库 + _run 取数 SQL 的往返。"""

    from novelvideo import brainclaw_outcome_runtime as runtime

    scope_info = mysql_scope
    state_dir = scope_info["db_path"].parent / "brainclaw_outcomes"
    database = state_dir / "outcomes.sqlite3"

    key_file = tmp_path / "outcome.key"
    key_file.write_bytes(b"k" * 64)
    # Windows 无法把文件呈现为 0600:仅放宽运行时的 mode 断言
    monkeypatch.setattr(runtime.stat, "S_IMODE", lambda mode: 0o600)
    monkeypatch.setenv("BRAINCLAW_OUTCOME_STATE_DIR", str(state_dir))
    monkeypatch.setattr(runtime, "_runtime", None)

    class _NullThread:
        """抑制投递线程:本测试只覆盖存储往返,不做 HTTP 投递。"""

        def __init__(self, *args, **kwargs):
            pass

        def start(self):
            pass

    monkeypatch.setattr(runtime.threading, "Thread", _NullThread)

    rt = runtime.OutcomeRuntime(
        endpoint="http://127.0.0.1:9/",
        key_path=key_file,
        database=database,
    )
    try:
        rt.enqueue("v1.test.mysql", passed=True)
        receipt = "v1.test.mysql"
        expected_identity = hashlib.sha256(
            f"{receipt}\0request\0automatic_contract".encode()
        ).hexdigest()
        # 与 OutcomeRuntime._run 相同的取数 SQL(rowid_expr 在 MySQL 下为 id)
        rows = None
        with rt.lock:
            rows = rt.connection.execute(
                f"SELECT identity,body,attempts FROM outcomes "
                f"WHERE next_attempt<=? ORDER BY {runtime.rowid_expr('outcomes')} "
                f"LIMIT 100",
                (time.time() + 10,),
            ).fetchall()
        assert len(rows) == 1
        assert rows[0][0] == expected_identity
        assert receipt in str(rows[0][1])
        # 重复 enqueue(同 identity)被 INSERT IGNORE 吞掉
        rt.enqueue(receipt, passed=False)
        with rt.lock:
            rows = rt.connection.execute(
                "SELECT COUNT(*) FROM outcomes"
            ).fetchall()
        assert rows[0][0] == 1
    finally:
        monkeypatch.setattr(runtime, "_runtime", None)


async def test_failure_registry_roundtrip_on_mysql(mysql_scope):
    """failure_registry:bump_hit(项目命中表)+ load 负向条款(g_verification__)。"""

    from novelvideo.verification import failure_registry

    scope_info = mysql_scope
    project_state_dir = scope_info["db_path"].parent  # state/<user>/<project>

    # 1) bump_hit:get_hits 往返(命中表归项目 store 组件,先确保 schema)
    from novelvideo.db.adapter import acquire_async
    from novelvideo.db.dsn import scope_key_for_path
    from novelvideo.sqlite_store import ensure_project_store_mysql

    await asyncio.to_thread(ensure_project_store_mysql, scope_info["db_path"])
    conn = await acquire_async(scope_key_for_path(scope_info["db_path"]))
    try:
        await failure_registry.bump_hit(conn, "mysql_gated_hit", 3)
        hits = await failure_registry.get_hits(conn, "mysql_gated_hit")
        assert len(hits) == 1
        assert hits[0]["hit_count"] == 1
        assert hits[0]["first_seen_episode"] == 3

        await failure_registry.bump_hit(conn, "mysql_gated_hit", 5)
        all_hits = await failure_registry.get_hits(conn)
        entry = next(h for h in all_hits if h["code"] == "mysql_gated_hit")
        assert entry["hit_count"] == 2
        # CASE WHEN 保住首次出现集数
        assert entry["first_seen_episode"] == 3
    finally:
        await conn.close()

    # 2) load_negative_clause_for_project:async 门面(经 open_defs_db →
    #    g_verification__ 全局作用域,种子幂等)
    clause = await failure_registry.load_negative_clause_for_project(
        str(project_state_dir), "correction"
    )
    assert clause.startswith("NEGATIVE CONSTRAINTS")
    assert "- " in clause

    # 3) load_negative_clause_for_project_sync:同步门面(经 acquire_sync)
    sync_clause = failure_registry.load_negative_clause_for_project_sync(
        str(project_state_dir), "director"
    )
    assert sync_clause.startswith("NEGATIVE CONSTRAINTS")
