from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

from fastapi import FastAPI
from fastapi.testclient import TestClient

import app.api.agent_knowledge as agent_knowledge_api
from app.core.security import get_current_user
from app.database import get_db


class _FakeDB:
    def __init__(self) -> None:
        self.commit_count = 0

    async def commit(self) -> None:
        self.commit_count += 1


def _client(monkeypatch, *, user=None, agent=None):
    app = FastAPI()
    app.include_router(agent_knowledge_api.router)
    fake_db = _FakeDB()
    user = user or SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4(), is_active=True)
    agent = agent or SimpleNamespace(
        id=uuid4(),
        tenant_id=user.tenant_id,
        owner_user_id=user.id,
        sponsor_user_id=user.id,
        creator_id=user.id,
    )

    async def override_user():
        return user

    async def override_db():
        yield fake_db

    async def allow_access(db_session, current_user, agent_id):
        assert db_session is fake_db
        assert current_user is user
        return agent, "manage"

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(agent_knowledge_api, "check_agent_access", allow_access)
    return TestClient(app, raise_server_exceptions=False), fake_db, user, agent


def _personal_client(monkeypatch, *, user=None):
    app = FastAPI()
    app.include_router(agent_knowledge_api.personal_router)
    fake_db = _FakeDB()
    user = user or SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4(), is_active=True)

    async def override_user():
        return user

    async def override_db():
        yield fake_db

    app.dependency_overrides[get_current_user] = override_user
    app.dependency_overrides[get_db] = override_db
    return TestClient(app, raise_server_exceptions=False), fake_db, user


def test_current_user_personal_knowledge_routes_use_owner_scope_without_agent(monkeypatch):
    owner_id = uuid4()
    document_id = uuid4()
    segment_id = uuid4()
    user = SimpleNamespace(id=owner_id, role="member", tenant_id=uuid4(), is_active=True)
    captured = []

    class _FakeService:
        async def list_personal_documents(self, session, **kwargs):
            captured.append(("list", kwargs))
            return [
                SimpleNamespace(
                    document_id=document_id,
                    title="Owner notes",
                    source_kind="paste",
                    source_uri=None,
                    source_sha256="a" * 64,
                    source_ref=f"kb://person/{owner_id}/documents/{document_id}",
                    canonical_md_path="persons/owner/kb/doc.md",
                    status="ready",
                    sensitivity="internal",
                    agent_searchable=True,
                    segment_count=1,
                    created_at=None,
                    updated_at=None,
                    metadata={},
                )
            ]

        async def search_personal(self, session, **kwargs):
            captured.append(("search", kwargs))
            return [
                SimpleNamespace(
                    document_id=document_id,
                    segment_id=segment_id,
                    title="Owner notes",
                    snippet="Use owner scope.",
                    source_ref=f"kb://person/{owner_id}/documents/{document_id}#segment={segment_id}",
                    score=0.9,
                    heading_path=["Owner"],
                    sensitivity="internal",
                    metadata={},
                )
            ]

        async def get_personal_document(self, session, **kwargs):
            captured.append(("detail", kwargs))
            return SimpleNamespace(
                document_id=document_id,
                title="Owner notes",
                source_kind="paste",
                source_uri=None,
                source_sha256="a" * 64,
                source_ref=f"kb://person/{owner_id}/documents/{document_id}",
                canonical_md_path="persons/owner/kb/doc.md",
                status="ready",
                sensitivity="internal",
                agent_searchable=True,
                segment_count=1,
                created_at=None,
                updated_at=None,
                metadata={},
                segments=[
                    SimpleNamespace(
                        segment_id=segment_id,
                        position=0,
                        heading_path=["Owner"],
                        content="Use owner scope.",
                        token_count=4,
                    )
                ],
            )

    monkeypatch.setattr(agent_knowledge_api, "PersonalKnowledgeService", lambda: _FakeService(), raising=False)
    client, _db, _user = _personal_client(monkeypatch, user=user)

    listed = client.get("/knowledge/personal/documents")
    searched = client.get("/knowledge/personal/search", params={"q": "owner scope"})
    detailed = client.get(f"/knowledge/personal/documents/{document_id}")

    assert listed.status_code == 200
    assert searched.status_code == 200
    assert detailed.status_code == 200
    assert listed.json()["documents"][0]["document_id"] == str(document_id)
    assert searched.json()["results"][0]["segment_id"] == str(segment_id)
    assert detailed.json()["segments"][0]["content"] == "Use owner scope."
    assert [name for name, _kwargs in captured] == ["list", "search", "detail"]
    for _name, kwargs in captured:
        assert kwargs["tenant_id"] == user.tenant_id
        assert kwargs["owner_user_id"] == owner_id
        assert kwargs["current_user_id"] == owner_id
        assert kwargs["agent_id"] is None


def test_current_user_personal_knowledge_ingest_never_accepts_browser_owner_id(monkeypatch):
    owner_id = uuid4()
    document_id = uuid4()
    user = SimpleNamespace(id=owner_id, role="member", tenant_id=uuid4(), is_active=True)
    captured = {}

    class _FakeService:
        async def ingest_markdown(self, session, **kwargs):
            captured.update({"session": session, **kwargs})
            return SimpleNamespace(
                document_id=document_id,
                source_sha256="a" * 64,
                artifact_hash="b" * 64,
                canonical_md_path="persons/owner/kb/doc.md",
                segment_count=1,
                status="ready",
            )

    monkeypatch.setattr(agent_knowledge_api, "PersonalKnowledgeService", lambda: _FakeService(), raising=False)
    client, fake_db, _user = _personal_client(monkeypatch, user=user)

    response = client.post(
        "/knowledge/personal/documents",
        json={
            "title": "Owner note",
            "markdown": "# Owner\n\nDo not trust browser owner ids.",
            "source_kind": "paste",
            "source_uri": "browser://knowledge/personal",
            "owner_user_id": str(uuid4()),
            "agent_searchable": True,
            "sensitivity": "internal",
        },
    )

    assert response.status_code == 200
    assert response.json()["document_id"] == str(document_id)
    assert fake_db.commit_count == 1
    assert captured["tenant_id"] == user.tenant_id
    assert captured["owner_user_id"] == owner_id
    assert captured["created_by_user_id"] == owner_id


def test_personal_knowledge_ingest_uses_agent_owner_and_commits(monkeypatch):
    owner_id = uuid4()
    agent_id = uuid4()
    document_id = uuid4()
    user = SimpleNamespace(id=owner_id, role="member", tenant_id=uuid4(), is_active=True)
    agent = SimpleNamespace(
        id=agent_id,
        tenant_id=user.tenant_id,
        owner_user_id=owner_id,
        sponsor_user_id=owner_id,
        creator_id=owner_id,
    )
    captured = {}

    class _FakeService:
        async def ingest_markdown(self, session, **kwargs):
            captured.update({"session": session, **kwargs})
            return SimpleNamespace(
                document_id=document_id,
                source_sha256="a" * 64,
                artifact_hash="b" * 64,
                canonical_md_path="persons/owner/kb/doc.md",
                segment_count=2,
                status="ready",
            )

    monkeypatch.setattr(agent_knowledge_api, "PersonalKnowledgeService", lambda: _FakeService(), raising=False)
    client, fake_db, _user, _agent = _client(monkeypatch, user=user, agent=agent)

    response = client.post(
        f"/agents/{agent_id}/knowledge/personal/documents",
        json={
            "title": "Taste notes",
            "markdown": "# Taste\n\nPrefer source refs.",
            "source_kind": "paste",
            "source_uri": "clipboard://taste",
            "agent_searchable": True,
            "sensitivity": "internal",
        },
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["document_id"] == str(document_id)
    assert payload["segment_count"] == 2
    assert fake_db.commit_count == 1
    assert captured["tenant_id"] == user.tenant_id
    assert captured["owner_user_id"] == owner_id
    assert captured["created_by_user_id"] == owner_id
    assert captured["title"] == "Taste notes"


def test_personal_knowledge_ingest_requires_current_owner(monkeypatch):
    owner_id = uuid4()
    user = SimpleNamespace(id=uuid4(), role="member", tenant_id=uuid4(), is_active=True)
    agent = SimpleNamespace(
        id=uuid4(),
        tenant_id=user.tenant_id,
        owner_user_id=owner_id,
        sponsor_user_id=owner_id,
        creator_id=owner_id,
    )

    class _UnexpectedService:
        async def ingest_markdown(self, *args, **kwargs):  # pragma: no cover - must not be called
            raise AssertionError("non-owner must not write personal KB")

    monkeypatch.setattr(agent_knowledge_api, "PersonalKnowledgeService", lambda: _UnexpectedService(), raising=False)
    client, fake_db, _user, _agent = _client(monkeypatch, user=user, agent=agent)

    response = client.post(
        f"/agents/{agent.id}/knowledge/personal/documents",
        json={"title": "Denied", "markdown": "secret", "source_kind": "paste"},
    )

    assert response.status_code == 403
    assert fake_db.commit_count == 0


def test_personal_knowledge_list_search_and_detail_use_owner_scope(monkeypatch):
    owner_id = uuid4()
    agent_id = uuid4()
    user = SimpleNamespace(id=owner_id, role="member", tenant_id=uuid4(), is_active=True)
    agent = SimpleNamespace(
        id=agent_id,
        tenant_id=user.tenant_id,
        owner_user_id=owner_id,
        sponsor_user_id=owner_id,
        creator_id=owner_id,
    )
    document_id = uuid4()
    segment_id = uuid4()
    captured = []

    class _FakeService:
        async def list_personal_documents(self, session, **kwargs):
            captured.append(("list", kwargs))
            return [
                SimpleNamespace(
                    document_id=document_id,
                    title="Retrieval notes",
                    source_kind="paste",
                    source_uri=None,
                    source_sha256="a" * 64,
                    source_ref=f"kb://person/{owner_id}/documents/{document_id}",
                    canonical_md_path="persons/owner/kb/doc.md",
                    status="ready",
                    sensitivity="internal",
                    agent_searchable=True,
                    segment_count=1,
                    created_at=None,
                    updated_at=None,
                    metadata={},
                )
            ]

        async def search_personal(self, session, **kwargs):
            captured.append(("search", kwargs))
            return [
                SimpleNamespace(
                    document_id=document_id,
                    segment_id=segment_id,
                    title="Retrieval notes",
                    snippet="Use source refs.",
                    source_ref=f"kb://person/{owner_id}/documents/{document_id}#segment={segment_id}",
                    score=0.82,
                    heading_path=["Retrieval"],
                    sensitivity="internal",
                    metadata={},
                )
            ]

        async def get_personal_document(self, session, **kwargs):
            captured.append(("detail", kwargs))
            return SimpleNamespace(
                document_id=document_id,
                title="Retrieval notes",
                source_kind="paste",
                source_uri=None,
                source_sha256="a" * 64,
                source_ref=f"kb://person/{owner_id}/documents/{document_id}",
                canonical_md_path="persons/owner/kb/doc.md",
                status="ready",
                sensitivity="internal",
                agent_searchable=True,
                segment_count=1,
                created_at=None,
                updated_at=None,
                metadata={},
                segments=[
                    SimpleNamespace(
                        segment_id=segment_id,
                        position=0,
                        heading_path=["Retrieval"],
                        content="Use source refs.",
                        token_count=4,
                    )
                ],
            )

    monkeypatch.setattr(agent_knowledge_api, "PersonalKnowledgeService", lambda: _FakeService(), raising=False)
    client, _db, _user, _agent = _client(monkeypatch, user=user, agent=agent)

    listed = client.get(f"/agents/{agent_id}/knowledge/personal/documents")
    searched = client.get(f"/agents/{agent_id}/knowledge/personal/search", params={"q": "source refs"})
    detailed = client.get(f"/agents/{agent_id}/knowledge/personal/documents/{document_id}")

    assert listed.status_code == 200
    assert searched.status_code == 200
    assert detailed.status_code == 200
    assert listed.json()["documents"][0]["document_id"] == str(document_id)
    assert searched.json()["results"][0]["segment_id"] == str(segment_id)
    assert detailed.json()["segments"][0]["content"] == "Use source refs."
    assert [name for name, _kwargs in captured] == ["list", "search", "detail"]
    for _name, kwargs in captured:
        assert kwargs["tenant_id"] == user.tenant_id
        assert kwargs["owner_user_id"] == owner_id
        assert kwargs["current_user_id"] == owner_id
        assert kwargs["agent_id"] == agent_id
