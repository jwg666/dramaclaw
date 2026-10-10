"""剧本设计端点契约测试。

路由层用 FakeStore + dependency_overrides（与 test_api_beats_media_urls.py 同模式）；
存储层用真实 SQLiteStore 跑 tmp_path 里的项目库，验证 schema v5 迁移与 CRUD 语义。
AI 端点 monkeypatch screenplay_assistant 的具体函数，不触网。
"""

from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from novelvideo.api.deps import ProjectResolution
from novelvideo.api.routes import screenplays

pytestmark = pytest.mark.m04


# ── FakeStore（路由层） ──────────────────────────────────────────────────────


class FakeScreenplayStore:
    """内存版 store：只实现路由触达的方法，语义对齐 SQLiteStore。"""

    def __init__(self) -> None:
        self.documents: dict[str, dict] = {}
        self.counter = 0

    def _next_id(self) -> str:
        self.counter += 1
        return f"01TEST{self.counter:020d}"

    async def list_screenplays(self):
        items = [
            {
                "id": doc["id"],
                "title": doc["title"],
                "synopsis": doc["synopsis"],
                "status": doc["status"],
                "char_count": len(doc["content"]),
                "created_at": doc["created_at"],
                "updated_at": doc["updated_at"],
            }
            for doc in self.documents.values()
        ]
        return sorted(items, key=lambda item: item["id"], reverse=True)

    async def get_screenplay(self, screenplay_id: str):
        document = self.documents.get(screenplay_id)
        return dict(document) if document else None

    async def create_screenplay(self, *, title: str, synopsis: str = "", content: str = ""):
        screenplay_id = self._next_id()
        self.documents[screenplay_id] = {
            "id": screenplay_id,
            "title": title,
            "synopsis": synopsis,
            "content": content,
            "status": "draft",
            "created_at": "2026-01-01 00:00:00",
            "updated_at": "2026-01-01 00:00:00",
        }
        return dict(self.documents[screenplay_id])

    async def update_screenplay(self, screenplay_id: str, **fields):
        document = self.documents.get(screenplay_id)
        if document is None:
            return None
        for key, value in fields.items():
            if value is not None:
                document[key] = value
        return dict(document)

    async def delete_screenplay(self, screenplay_id: str) -> bool:
        return self.documents.pop(screenplay_id, None) is not None


def _client(monkeypatch, tmp_path, store: FakeScreenplayStore) -> TestClient:
    app = FastAPI()
    app.include_router(screenplays.router, prefix="/api/v1")
    app.dependency_overrides[screenplays.get_api_user] = lambda: {"username": "alice"}
    app.dependency_overrides[screenplays.get_sqlite_store] = lambda: store

    async def fake_resolve_project_scope(project, user, *, required_role="viewer"):
        return ProjectResolution(
            ctx=None,
            username="alice",
            project_name="demo",
            project_dir=tmp_path,
            output_dir=str(tmp_path),
            state_dir=str(tmp_path / "state"),
            runtime_dir=str(tmp_path / "runtime"),
        )

    monkeypatch.setattr(screenplays, "resolve_project_scope", fake_resolve_project_scope)
    return TestClient(app)


def _create(client: TestClient, **overrides) -> dict:
    body: dict = {
        "title": "归灵司·第一稿",
        "synopsis": "悬疑",
        "content": "第1场 城墙外 黄昏\n...",
    }
    body.update(overrides)
    response = client.post("/api/v1/projects/demo/screenplays", json=body)
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    return payload["data"]


# ── CRUD ─────────────────────────────────────────────────────────────────────


def test_create_list_get_update_delete_roundtrip(monkeypatch, tmp_path):
    store = FakeScreenplayStore()
    client = _client(monkeypatch, tmp_path, store)

    document = _create(client)
    assert document["id"]
    assert document["title"] == "归灵司·第一稿"

    listed = client.get("/api/v1/projects/demo/screenplays")
    assert listed.status_code == 200
    items = listed.json()["data"]
    assert len(items) == 1
    assert items[0]["char_count"] == len(document["content"])
    assert "content" not in items[0]  # 列表不携带正文

    fetched = client.get(f"/api/v1/projects/demo/screenplays/{document['id']}")
    assert fetched.status_code == 200
    assert fetched.json()["data"]["content"] == document["content"]

    saved = client.put(
        f"/api/v1/projects/demo/screenplays/{document['id']}",
        json={"content": "第2场 大殿 夜\n…"},
    )
    assert saved.status_code == 200
    assert saved.json()["data"]["content"] == "第2场 大殿 夜\n…"
    # 列级更新：未传的 title 不变
    assert saved.json()["data"]["title"] == "归灵司·第一稿"

    deleted = client.delete(f"/api/v1/projects/demo/screenplays/{document['id']}")
    assert deleted.status_code == 200
    assert (
        client.get(f"/api/v1/projects/demo/screenplays/{document['id']}").status_code
        == 404
    )


def test_create_rejects_blank_title(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, FakeScreenplayStore())
    response = client.post("/api/v1/projects/demo/screenplays", json={"title": "   "})
    assert response.status_code == 400


def test_get_update_delete_unknown_screenplay_returns_404(monkeypatch, tmp_path):
    client = _client(monkeypatch, tmp_path, FakeScreenplayStore())
    base = "/api/v1/projects/demo/screenplays/01UNKNOWN"
    assert client.get(base).status_code == 404
    assert client.put(base, json={"content": "x"}).status_code == 404
    assert client.delete(base).status_code == 404


# ── 作为源文导出 ─────────────────────────────────────────────────────────────


def test_export_source_writes_uploads_txt_with_format_check(monkeypatch, tmp_path):
    store = FakeScreenplayStore()
    client = _client(monkeypatch, tmp_path, store)
    content = (
        "第1场 城墙外 黄昏\n沈砚抬手推开城门，风卷起披风。\n"
        "沈砚（低声）：果然在这里。\n"
        "第2场 城墙外 夜\n火把亮起。"
    )
    document = _create(client, content=content)

    response = client.post(
        f"/api/v1/projects/demo/screenplays/{document['id']}/export-source"
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["ok"] is True
    data = payload["data"]
    assert data["filename"].endswith(".txt")
    assert data["chapter_count"] >= 1

    written = tmp_path / "uploads" / data["filename"]
    assert written.is_file()
    assert written.read_text(encoding="utf-8").startswith("第1场")
    assert data["format_check"] is not None


def test_export_source_rejects_empty_screenplay(monkeypatch, tmp_path):
    store = FakeScreenplayStore()
    client = _client(monkeypatch, tmp_path, store)
    document = _create(client, content="   ")
    response = client.post(
        f"/api/v1/projects/demo/screenplays/{document['id']}/export-source"
    )
    assert response.status_code == 400


# ── AI 辅助 ──────────────────────────────────────────────────────────────────


def test_ai_polish_returns_text_without_touching_store(monkeypatch, tmp_path):
    store = FakeScreenplayStore()
    client = _client(monkeypatch, tmp_path, store)
    document = _create(client, content="第1场 城墙外 黄昏\n沈砚走进城门。")

    async def fake_polish(text, *, instruction=""):
        return "第1场 城墙外 黄昏\n沈砚缓缓推开斑驳的城门。"

    from novelvideo.agents import screenplay_assistant

    monkeypatch.setattr(screenplay_assistant, "polish_screenplay_text", fake_polish)

    response = client.post(
        f"/api/v1/projects/demo/screenplays/{document['id']}/ai/polish",
        json={"selection": "", "instruction": "更冷一些"},
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["action"] == "polish"
    assert "缓缓推开" in data["text"]
    # AI 不落库：原稿保持不变
    assert store.documents[document["id"]]["content"] == "第1场 城墙外 黄昏\n沈砚走进城门。"


def test_ai_assist_rejects_unknown_action_and_empty_content(monkeypatch, tmp_path):
    store = FakeScreenplayStore()
    client = _client(monkeypatch, tmp_path, store)
    document = _create(client, content="")

    assert (
        client.post(
            f"/api/v1/projects/demo/screenplays/{document['id']}/ai/magic",
            json={"selection": "", "instruction": ""},
        ).status_code
        == 400
    )
    empty = _create(client, title="空白稿", content="")
    response = client.post(
        f"/api/v1/projects/demo/screenplays/{empty['id']}/ai/continue",
        json={"selection": "", "instruction": ""},
    )
    assert response.status_code == 400


# ── 存储层（真实 SQLiteStore，验证 schema v5 迁移） ─────────────────────────


async def test_sqlite_store_screenplay_crud_roundtrip(tmp_path):
    from novelvideo.sqlite_store import SQLiteStore

    store = SQLiteStore(str(tmp_path))
    try:
        documents = await store.list_screenplays()
        assert documents == []

        created = await store.create_screenplay(
            title="第一稿", synopsis="悬念", content="第1场 城墙外 黄昏"
        )
        screenplay_id = created["id"]
        assert created["status"] == "draft"

        updated = await store.update_screenplay(
            screenplay_id, content="第1场 城墙外 黄昏\n第2场 大殿 夜"
        )
        assert updated is not None
        assert updated["content"].endswith("第2场 大殿 夜")
        assert updated["title"] == "第一稿"  # 列级更新不触碰未传字段

        listed = await store.list_screenplays()
        assert len(listed) == 1
        assert listed[0]["char_count"] == len(updated["content"])
        assert "content" not in listed[0]

        assert await store.delete_screenplay(screenplay_id) is True
        assert await store.get_screenplay(screenplay_id) is None
        assert await store.delete_screenplay(screenplay_id) is False
    finally:
        await store.close()
