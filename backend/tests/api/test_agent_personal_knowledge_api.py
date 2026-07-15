from __future__ import annotations

import io
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
        assert kwargs["principal"].principal_type == "human_browser"
        assert kwargs["principal"].user_id == owner_id


def test_current_user_personal_knowledge_ingest_never_accepts_browser_owner_id(monkeypatch):
    owner_id = uuid4()
    document_id = uuid4()
    user = SimpleNamespace(id=owner_id, role="member", tenant_id=uuid4(), is_active=True)
    captured = {}
    background_calls = []

    class _FakeService:
        async def queue_markdown_import(self, session, **kwargs):
            captured.update({"session": session, **kwargs})
            return SimpleNamespace(
                document_id=document_id,
                job_id=uuid4(),
                source_sha256="a" * 64,
                artifact_hash="b" * 64,
                canonical_md_path="persons/owner/kb/doc.md",
                segment_count=0,
                status="queued",
                warnings=[],
            )

    async def fake_process_jobs(**kwargs):
        background_calls.append(kwargs)

    monkeypatch.setattr(agent_knowledge_api, "PersonalKnowledgeService", lambda: _FakeService(), raising=False)
    monkeypatch.setattr(agent_knowledge_api, "_process_current_user_personal_import_jobs", fake_process_jobs)
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
    assert response.json()["status"] == "queued"
    assert fake_db.commit_count == 1
    assert captured["tenant_id"] == user.tenant_id
    assert captured["owner_user_id"] == owner_id
    assert captured["created_by_user_id"] == owner_id
    assert background_calls == [{"tenant_id": user.tenant_id, "owner_user_id": owner_id}]


def test_current_user_personal_knowledge_file_import_uses_owner_scope(monkeypatch):
    owner_id = uuid4()
    document_id = uuid4()
    job_id = uuid4()
    user = SimpleNamespace(id=owner_id, role="member", tenant_id=uuid4(), is_active=True)
    captured = {}
    background_calls = []

    class _FakeService:
        async def queue_source_bytes_import(self, session, **kwargs):
            captured.update({"session": session, **kwargs})
            return SimpleNamespace(
                document_id=document_id,
                job_id=job_id,
                source_sha256="c" * 64,
                artifact_hash="d" * 64,
                canonical_md_path="",
                segment_count=0,
                status="queued",
                warnings=[],
            )

    async def fake_process_jobs(**kwargs):
        background_calls.append(kwargs)

    monkeypatch.setattr(agent_knowledge_api, "PersonalKnowledgeService", lambda: _FakeService(), raising=False)
    monkeypatch.setattr(agent_knowledge_api, "_process_current_user_personal_import_jobs", fake_process_jobs)
    client, fake_db, _user = _personal_client(monkeypatch, user=user)

    response = client.post(
        "/knowledge/personal/imports",
        data={"title": "Imported report", "agent_searchable": "true", "sensitivity": "internal"},
        files={"file": ("report.html", io.BytesIO(b"<h1>Report</h1>"), "text/html")},
    )

    assert response.status_code == 200
    assert response.json()["job_id"] == str(job_id)
    assert fake_db.commit_count == 1
    assert captured["tenant_id"] == user.tenant_id
    assert captured["owner_user_id"] == owner_id
    assert captured["filename"] == "report.html"
    assert captured["source_kind"] == "upload"
    assert captured["data"] == b"<h1>Report</h1>"
    assert response.json()["status"] == "queued"
    assert background_calls == [{"tenant_id": user.tenant_id, "owner_user_id": owner_id}]


def test_current_user_personal_knowledge_url_import_and_jobs(monkeypatch):
    owner_id = uuid4()
    document_id = uuid4()
    job_id = uuid4()
    user = SimpleNamespace(id=owner_id, role="member", tenant_id=uuid4(), is_active=True)
    captured = []
    background_calls = []

    class _FakeService:
        async def queue_url_import(self, session, **kwargs):
            captured.append(("url", kwargs))
            return SimpleNamespace(
                document_id=document_id,
                job_id=job_id,
                source_sha256="e" * 64,
                artifact_hash="f" * 64,
                canonical_md_path="",
                segment_count=0,
                status="queued",
                warnings=[],
            )

        async def list_import_jobs(self, session, **kwargs):
            captured.append(("jobs", kwargs))
            return [
                SimpleNamespace(
                    job_id=job_id,
                    document_id=document_id,
                    stage="indexed",
                    status="ready",
                    artifact_hash="f" * 64,
                    error_message=None,
                    attempt_count=1,
                    metadata={"source_kind": "url"},
                    created_at=None,
                    updated_at=None,
                )
            ]

        async def retry_import_job(self, session, **kwargs):
            captured.append(("retry", kwargs))
            return SimpleNamespace(
                document_id=document_id,
                job_id=job_id,
                source_sha256="e" * 64,
                artifact_hash="f" * 64,
                canonical_md_path="persons/owner/kb/url.md",
                segment_count=1,
                status="ready",
                warnings=[],
            )

    async def fake_process_jobs(**kwargs):
        background_calls.append(kwargs)

    monkeypatch.setattr(agent_knowledge_api, "PersonalKnowledgeService", lambda: _FakeService(), raising=False)
    monkeypatch.setattr(agent_knowledge_api, "_process_current_user_personal_import_jobs", fake_process_jobs)
    client, fake_db, _user = _personal_client(monkeypatch, user=user)

    imported = client.post(
        "/knowledge/personal/import-url",
        json={"url": "https://example.com/report.html", "title": "URL report", "agent_searchable": True},
    )
    listed = client.get("/knowledge/personal/import-jobs")
    retried = client.post(f"/knowledge/personal/import-jobs/{job_id}/retry")

    assert imported.status_code == 200
    assert listed.status_code == 200
    assert retried.status_code == 200
    assert fake_db.commit_count == 2
    assert imported.json()["status"] == "queued"
    assert [name for name, _kwargs in captured] == ["url", "jobs", "retry"]
    for _name, kwargs in captured:
        assert kwargs["tenant_id"] == user.tenant_id
        assert kwargs["owner_user_id"] == owner_id
    assert background_calls == [{"tenant_id": user.tenant_id, "owner_user_id": owner_id}]


def test_current_user_personal_knowledge_document_actions(monkeypatch):
    owner_id = uuid4()
    document_id = uuid4()
    job_id = uuid4()
    user = SimpleNamespace(id=owner_id, role="member", tenant_id=uuid4(), is_active=True)
    captured = []

    class _FakeService:
        async def patch_personal_document(self, session, **kwargs):
            captured.append(("patch", kwargs))
            return SimpleNamespace(
                document_id=document_id,
                title="Updated",
                source_kind="paste",
                source_uri=None,
                source_sha256="a" * 64,
                source_ref=f"kb://person/{owner_id}/documents/{document_id}",
                canonical_md_path="persons/owner/kb/doc.md",
                status="archived",
                sensitivity="private",
                agent_searchable=False,
                segment_count=2,
                created_at=None,
                updated_at=None,
                metadata={},
            )

        async def rebuild_personal_document_index(self, session, **kwargs):
            captured.append(("rebuild", kwargs))
            return SimpleNamespace(
                document_id=document_id,
                job_id=job_id,
                source_sha256="a" * 64,
                artifact_hash="b" * 64,
                canonical_md_path="persons/owner/kb/doc.md",
                segment_count=2,
                status="ready",
                warnings=[],
            )

    monkeypatch.setattr(agent_knowledge_api, "PersonalKnowledgeService", lambda: _FakeService(), raising=False)
    client, fake_db, _user = _personal_client(monkeypatch, user=user)

    patched = client.patch(
        f"/knowledge/personal/documents/{document_id}",
        json={"agent_searchable": False, "sensitivity": "private", "status": "archived"},
    )
    rebuilt = client.post(f"/knowledge/personal/documents/{document_id}/rebuild-index")

    assert patched.status_code == 200
    assert patched.json()["status"] == "archived"
    assert rebuilt.status_code == 200
    assert rebuilt.json()["job_id"] == str(job_id)
    assert fake_db.commit_count == 2
    assert [name for name, _kwargs in captured] == ["patch", "rebuild"]
    for _name, kwargs in captured:
        assert kwargs["tenant_id"] == user.tenant_id
        assert kwargs["owner_user_id"] == owner_id


def test_current_user_personal_knowledge_source_preview_streams_owner_image(monkeypatch):
    owner_id = uuid4()
    document_id = uuid4()
    user = SimpleNamespace(id=owner_id, role="member", tenant_id=uuid4(), is_active=True)
    captured = []

    class _FakeService:
        async def get_personal_document_source_preview(self, session, **kwargs):
            captured.append(kwargs)
            return SimpleNamespace(filename="112233.png", mime_type="image/png", content=b"\x89PNG\r\nsource")

    monkeypatch.setattr(agent_knowledge_api, "PersonalKnowledgeService", lambda: _FakeService(), raising=False)
    client, _fake_db, _user = _personal_client(monkeypatch, user=user)

    response = client.get(f"/knowledge/personal/documents/{document_id}/source-preview")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/png")
    assert response.content == b"\x89PNG\r\nsource"
    assert len(captured) == 1
    assert captured[0]["tenant_id"] == user.tenant_id
    assert captured[0]["owner_user_id"] == owner_id
    assert captured[0]["document_id"] == document_id
    assert captured[0]["principal"].principal_type == "human_browser"
    assert captured[0]["principal"].user_id == owner_id


def test_current_user_personal_knowledge_grant_routes_use_owner_scope(monkeypatch):
    owner_id = uuid4()
    grant_id = uuid4()
    agent_grantee_id = uuid4()
    user = SimpleNamespace(id=owner_id, role="member", tenant_id=uuid4(), is_active=True)
    captured = []

    class _FakeService:
        async def list_personal_grants(self, session, **kwargs):
            captured.append(("list_grants", kwargs))
            return [
                SimpleNamespace(
                    grant_id=grant_id,
                    resource_type="scope",
                    resource_id=owner_id,
                    document_id=None,
                    grantee_type="agent",
                    grantee_id=agent_grantee_id,
                    permission="search",
                    requester_user_id=owner_id,
                    session_id=None,
                    purpose="autonomous_agent",
                    delegation_id=None,
                    sensitivity_ceiling="PL3_sensitive",
                    binding_key="pkb:test",
                    expires_at="2099-01-01T00:00:00+00:00",
                    revoked_at=None,
                    revoked_by_user_id=None,
                    active=True,
                    metadata={"reason": "research agent"},
                    created_at=None,
                )
            ]

        async def create_personal_grant(self, session, **kwargs):
            captured.append(("create_grant", kwargs))
            return SimpleNamespace(
                grant_id=grant_id,
                resource_type=kwargs["resource_type"],
                resource_id=kwargs["resource_id"],
                document_id=kwargs["document_id"],
                grantee_type=kwargs["grantee_type"],
                grantee_id=kwargs["grantee_id"],
                permission=kwargs["permission"],
                requester_user_id=kwargs["requester_user_id"],
                session_id=kwargs["session_id"],
                purpose=kwargs["purpose"],
                delegation_id=kwargs["delegation_id"],
                sensitivity_ceiling=kwargs["sensitivity_ceiling"],
                binding_key="pkb:test",
                expires_at=kwargs["expires_at"],
                revoked_at=None,
                revoked_by_user_id=None,
                active=True,
                metadata=kwargs["grant_metadata"],
                created_at=None,
            )

        async def delete_personal_grant(self, session, **kwargs):
            captured.append(("delete_grant", kwargs))
            return True

    monkeypatch.setattr(agent_knowledge_api, "PersonalKnowledgeService", lambda: _FakeService(), raising=False)
    client, fake_db, _user = _personal_client(monkeypatch, user=user)

    listed = client.get("/knowledge/personal/grants")
    created = client.post(
        "/knowledge/personal/grants",
        json={
            "resource_type": "scope",
            "grantee_type": "agent",
            "grantee_id": str(agent_grantee_id),
            "permission": "search",
            "requester_user_id": str(owner_id),
            "purpose": "autonomous_agent",
            "sensitivity_ceiling": "PL3_sensitive",
            "expires_at": "2099-01-01T00:00:00Z",
            "metadata": {"reason": "research agent"},
        },
    )
    deleted = client.delete(f"/knowledge/personal/grants/{grant_id}")

    assert listed.status_code == 200
    assert listed.json()["grants"][0]["grant_id"] == str(grant_id)
    assert created.status_code == 200
    assert created.json()["grantee_id"] == str(agent_grantee_id)
    assert created.json()["purpose"] == "autonomous_agent"
    assert created.json()["sensitivity_ceiling"] == "PL3_sensitive"
    assert deleted.status_code == 200
    assert deleted.json() == {"revoked": True, "deleted": False}
    assert fake_db.commit_count == 2
    assert [name for name, _kwargs in captured] == ["list_grants", "create_grant", "delete_grant"]
    for _name, kwargs in captured:
        assert kwargs["tenant_id"] == user.tenant_id
        assert kwargs["owner_user_id"] == owner_id
        assert kwargs["current_user_id"] == owner_id


def test_current_user_personal_knowledge_rejects_unbounded_agent_grant(monkeypatch):
    owner_id = uuid4()
    user = SimpleNamespace(id=owner_id, role="member", tenant_id=uuid4(), is_active=True)

    class _FakeService:
        async def create_personal_grant(self, session, **kwargs):  # pragma: no cover - schema must reject first
            raise AssertionError("unbounded agent grant reached the service")

    monkeypatch.setattr(agent_knowledge_api, "PersonalKnowledgeService", lambda: _FakeService(), raising=False)
    client, fake_db, _user = _personal_client(monkeypatch, user=user)

    response = client.post(
        "/knowledge/personal/grants",
        json={
            "resource_type": "scope",
            "grantee_type": "agent",
            "grantee_id": str(uuid4()),
            "permission": "search",
        },
    )

    assert response.status_code == 422
    assert fake_db.commit_count == 0


def test_current_user_personal_knowledge_graph_route_uses_owner_scope(monkeypatch):
    owner_id = uuid4()
    entity_id = uuid4()
    user = SimpleNamespace(id=owner_id, role="member", tenant_id=uuid4(), is_active=True)
    captured = []

    class _FakeService:
        async def list_personal_graph(self, session, **kwargs):
            captured.append(kwargs)
            return SimpleNamespace(
                entities=[
                    SimpleNamespace(
                        entity_id=entity_id,
                        canonical_name="Personal KB",
                        entity_type="system",
                        aliases=["PKB"],
                        description="Owner scope knowledge",
                        confidence=0.9,
                        source_refs=[],
                    )
                ],
                links=[],
                assertions=[],
            )

    monkeypatch.setattr(agent_knowledge_api, "PersonalKnowledgeService", lambda: _FakeService(), raising=False)
    client, _fake_db, _user = _personal_client(monkeypatch, user=user)

    response = client.get("/knowledge/personal/graph")

    assert response.status_code == 200
    assert response.json()["entities"][0]["entity_id"] == str(entity_id)
    assert response.json()["entities"][0]["canonical_name"] == "Personal KB"
    assert captured == [
        {
            "tenant_id": user.tenant_id,
            "owner_user_id": owner_id,
            "current_user_id": owner_id,
            "limit": 100,
        }
    ]


def test_current_user_personal_knowledge_proposal_review_revision_and_rollback_use_owner_scope(monkeypatch):
    owner_id = uuid4()
    proposal_id = uuid4()
    document_id = uuid4()
    revision_id = uuid4()
    rollback_revision_id = uuid4()
    user = SimpleNamespace(id=owner_id, role="member", tenant_id=uuid4(), is_active=True)
    captured = []

    class _FakeProposalService:
        async def list_proposals(self, session, **kwargs):
            captured.append(("list", kwargs))
            return [
                SimpleNamespace(
                    proposal_id=proposal_id,
                    owner_user_id=owner_id,
                    proposed_by_agent_id=uuid4(),
                    delegated_by_agent_id=None,
                    delegation_id=None,
                    title="Incident response",
                    content="Escalate SEV-1 incidents immediately.",
                    content_hash="a" * 64,
                    target_collection="operations",
                    source_refs=["artifact://incident-42"],
                    sensitivity="PL1_public",
                    purpose="Preserve a verified operating rule.",
                    dedupe_key="incident-response",
                    idempotency_key="proposal-key",
                    policy_outcome="ask",
                    policy_reason_codes=[],
                    status="pending",
                    review_reason=None,
                    document_id=None,
                    revision_id=None,
                    rollback_ref=None,
                    created_at=None,
                    updated_at=None,
                )
            ]

        async def review(self, session, **kwargs):
            captured.append(("review", kwargs))
            return SimpleNamespace(
                proposal_id=proposal_id,
                owner_user_id=owner_id,
                proposed_by_agent_id=uuid4(),
                delegated_by_agent_id=None,
                delegation_id=None,
                title="Incident response",
                content="Escalate SEV-1 incidents immediately.",
                content_hash="a" * 64,
                target_collection="operations",
                source_refs=["artifact://incident-42"],
                sensitivity="PL1_public",
                purpose="Preserve a verified operating rule.",
                dedupe_key="incident-response",
                idempotency_key="proposal-key",
                policy_outcome="approve",
                policy_reason_codes=[],
                status="committed",
                review_reason=kwargs["reason"],
                document_id=document_id,
                revision_id=revision_id,
                rollback_ref=f"personal-kb://documents/{document_id}/revisions/1",
                created_at=None,
                updated_at=None,
            )

        async def revision_history(self, session, **kwargs):
            captured.append(("history", kwargs))
            return [
                {
                    "id": revision_id,
                    "version": 1,
                    "change_source": "agent_proposal",
                    "content": {"title": "Incident response"},
                }
            ]

        async def rollback_document(self, session, **kwargs):
            captured.append(("rollback", kwargs))
            return SimpleNamespace(
                document_id=document_id,
                version=2,
                rollback_of_version=1,
                revision_id=rollback_revision_id,
                rollback_ref=f"personal-kb://documents/{document_id}/revisions/2",
            )

    monkeypatch.setattr(
        agent_knowledge_api,
        "PersonalKnowledgeProposalService",
        lambda: _FakeProposalService(),
        raising=False,
    )
    client, fake_db, _user = _personal_client(monkeypatch, user=user)

    listed = client.get("/knowledge/personal/proposals", params={"status": "pending"})
    reviewed = client.post(
        f"/knowledge/personal/proposals/{proposal_id}/decision",
        json={"decision": "approve", "reason": "Verified by owner"},
    )
    history = client.get(f"/knowledge/personal/documents/{document_id}/revisions")
    rolled_back = client.post(
        f"/knowledge/personal/documents/{document_id}/rollback",
        json={"target_version": 1},
    )

    assert listed.status_code == 200
    assert listed.json()["proposals"][0]["content"] == "Escalate SEV-1 incidents immediately."
    assert reviewed.status_code == 200
    assert reviewed.json()["status"] == "committed"
    assert history.status_code == 200
    assert history.json()["revisions"][0]["id"] == str(revision_id)
    assert rolled_back.status_code == 200
    assert rolled_back.json()["rollback_of_version"] == 1
    assert fake_db.commit_count == 2
    assert [name for name, _kwargs in captured] == ["list", "review", "history", "rollback"]
    for _name, kwargs in captured:
        assert kwargs["tenant_id"] == user.tenant_id
        assert kwargs["owner_user_id"] == owner_id
    assert captured[1][1]["reviewer_user_id"] == owner_id
    assert captured[3][1]["reviewer_user_id"] == owner_id


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
    background_calls = []

    class _FakeService:
        async def queue_markdown_import(self, session, **kwargs):
            captured.update({"session": session, **kwargs})
            return SimpleNamespace(
                document_id=document_id,
                job_id=uuid4(),
                source_sha256="a" * 64,
                artifact_hash="b" * 64,
                canonical_md_path="",
                segment_count=0,
                status="queued",
                warnings=[],
            )

    async def fake_process_jobs(**kwargs):
        background_calls.append(kwargs)

    monkeypatch.setattr(agent_knowledge_api, "PersonalKnowledgeService", lambda: _FakeService(), raising=False)
    monkeypatch.setattr(agent_knowledge_api, "_process_current_user_personal_import_jobs", fake_process_jobs)
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
    assert payload["segment_count"] == 0
    assert payload["status"] == "queued"
    assert fake_db.commit_count == 1
    assert captured["tenant_id"] == user.tenant_id
    assert captured["owner_user_id"] == owner_id
    assert captured["created_by_user_id"] == owner_id
    assert captured["title"] == "Taste notes"
    assert background_calls == [{"tenant_id": user.tenant_id, "owner_user_id": owner_id}]


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
        async def queue_markdown_import(self, *args, **kwargs):  # pragma: no cover - must not be called
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
        assert kwargs["principal"].principal_type == "human_browser"
        assert kwargs["principal"].user_id == owner_id
        assert not hasattr(kwargs["principal"], "agent_id")


def test_shared_agent_user_browser_read_does_not_borrow_agent_runtime_authority(monkeypatch):
    owner_id = uuid4()
    shared_user_id = uuid4()
    agent_id = uuid4()
    user = SimpleNamespace(id=shared_user_id, role="member", tenant_id=uuid4(), is_active=True)
    agent = SimpleNamespace(
        id=agent_id,
        tenant_id=user.tenant_id,
        owner_user_id=owner_id,
        sponsor_user_id=owner_id,
        creator_id=owner_id,
    )
    captured = {}

    class _FakeService:
        async def search_personal(self, session, **kwargs):
            captured.update(kwargs)
            return []

    monkeypatch.setattr(agent_knowledge_api, "PersonalKnowledgeService", lambda: _FakeService(), raising=False)
    client, _db, _user, _agent = _client(monkeypatch, user=user, agent=agent)

    response = client.get(f"/agents/{agent_id}/knowledge/personal/search", params={"q": "owner secret"})

    assert response.status_code == 200
    assert response.json() == {"results": []}
    assert captured["owner_user_id"] == owner_id
    assert captured["principal"].principal_type == "human_browser"
    assert captured["principal"].user_id == shared_user_id
    assert not hasattr(captured["principal"], "agent_id")
