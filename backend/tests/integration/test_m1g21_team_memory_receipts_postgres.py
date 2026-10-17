"""Team-memory bodies must match a trusted revision and retain their inputs."""

import hashlib
import asyncio
import uuid
from pathlib import Path
from datetime import datetime, timedelta, timezone

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.organization_identity import ContentInputReceipt

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def memory_clients(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path):
    from app.api import memory
    from app.core.security import get_current_user
    from app.database import get_db
    from app.models.tenant import Tenant
    from app.models.user import User
    from tests.api.test_memory_api import _review_team_memory_semantics

    _review_team_memory_semantics.__wrapped__(monkeypatch)
    tenant = uuid.uuid4()
    users = [User(id=uuid.uuid4(), tenant_id=tenant, username=f"receipt-{uuid.uuid4().hex}",
        email=f"{uuid.uuid4().hex}@fixture.test", password_hash="fixture", display_name="Fixture member", role="member", is_active=True) for _ in range(2)]
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=tenant, name="Receipt fixture", slug=f"receipt-{tenant.hex}"))
        await db.flush()
        db.add_all(users)
        await db.commit()
    from app.config import get_settings

    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))

    async def database():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    clients = []
    for user in users:
        app = FastAPI()
        app.include_router(memory.router)
        app.dependency_overrides[get_current_user] = lambda user=user: user
        app.dependency_overrides[get_db] = database
        clients.append(AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture.local"))
    yield tenant, users, clients, tmp_path
    for client in clients:
        await client.aclose()


def body(content="Team result", **kwargs):
    return {"workspace_key": "business_team:00000000-0000-0000-0000-000000000001", "key": "report", "title": "Weekly result", "content": content, **kwargs}


async def test_plain_http_write_without_an_agent_has_a_readable_committed_receipt(memory_clients, app_user_sessionmaker):
    tenant, _users, (writer, reader), _root = memory_clients
    response = await writer.put("/enterprise/memory/shared", json=body())
    assert response.status_code == 200, response.text
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        receipt = (await db.scalars(select(ContentInputReceipt).where(ContentInputReceipt.tenant_id == tenant))).one()
        assert receipt.carrier_kind == "team_memory" and receipt.carrier_ref == body()["workspace_key"] + "\nreport"
        assert receipt.revision == response.json()["revision"] == 1
        assert receipt.content_sha256 == hashlib.sha256(b"Team result").hexdigest() and receipt.dependencies_json == []
    for endpoint in ("shared", "shared/search", "shared/report"):
        read = await reader.get("/enterprise/memory/" + endpoint, params={"workspace_key": body()["workspace_key"], "query": "Team"})
        assert read.status_code == 200 and "Team result" in read.text


@pytest.mark.parametrize("tamper", ["body", "body_and_frontmatter", "old_revision"])
async def test_unreceipted_rewrite_is_withheld_from_get_list_and_search(memory_clients, tamper):
    from app.services.team_memory import TeamMemoryStore, _compute_checksum

    tenant, _users, (writer, reader), root = memory_clients
    first = await writer.put("/enterprise/memory/shared", json=body())
    assert first.status_code == 200
    store = TeamMemoryStore(data_root=root)
    entry = store.get_entry(str(tenant), body()["workspace_key"], "report")
    path = Path(entry.absolute_path)
    original = path.read_text()
    if tamper == "old_revision":
        assert (await writer.put("/enterprise/memory/shared", json=body("New revision", base_revision=1))).status_code == 200
        path.write_text(original)
        forbidden = "Team result"
    else:
        forbidden = "Unreceipted private rewrite"
        changed = original.replace("Team result", forbidden)
        if tamper == "body_and_frontmatter":
            changed = changed.replace(_compute_checksum("Team result"), _compute_checksum(forbidden))
        path.write_text(changed)
    for endpoint in ("shared/report", "shared", "shared/search"):
        response = await reader.get("/enterprise/memory/" + endpoint, params={"workspace_key": body()["workspace_key"], "query": forbidden})
        assert forbidden not in response.text, response.text
        assert response.status_code == (409 if endpoint == "shared/report" else 200)
    assert path.exists() and forbidden in path.read_text()
    rewrite = await writer.put("/enterprise/memory/shared", json=body("Cannot erase missing provenance"))
    assert rewrite.status_code == 409 and forbidden in path.read_text()


@pytest.mark.parametrize("source_loss", ["revoked", "expired", "bytes_changed", "version_changed"])
async def test_replace_and_append_keep_source_union_and_revoke_consumption(memory_clients, owner_sessionmaker, source_loss):
    from app.models.knowledge import KnowledgeDocument, KnowledgeGrant

    tenant, users, (writer, reader), root = memory_clients
    created = await writer.put("/enterprise/memory/shared", json=body("Derived result"))
    assert created.status_code == 200
    canonical = root / "source.md"
    canonical.write_text("Authorized source")
    digest = hashlib.sha256(canonical.read_bytes()).hexdigest()
    document = KnowledgeDocument(id=uuid.uuid4(), tenant_id=tenant, scope_type="person", scope_id=users[0].id,
        owner_user_id=users[0].id, source_kind="text", source_sha256=digest, title="Source", status="ready",
        canonical_md_path="source.md", canonical_md_sha256=digest)
    grant = KnowledgeGrant(id=uuid.uuid4(), tenant_id=tenant, scope_id=users[0].id, resource_type="document",
        resource_id=document.id, document_id=document.id, grantee_type="user", grantee_id=users[1].id, permission="read")
    dependency = {"source_kind": "knowledge_document", "source_id": str(document.id), "source_version": digest}
    async with owner_sessionmaker() as db:
        db.add(document)
        await db.flush()
        db.add(grant)
        receipt = (await db.scalars(select(ContentInputReceipt).where(ContentInputReceipt.tenant_id == tenant))).one()
        receipt.dependencies_json = [dependency, dependency]
        await db.commit()
    for revision, mode in ((1, "replace"), (2, "append")):
        response = await writer.put("/enterprise/memory/shared", json=body("Human edit", mode=mode, base_revision=revision))
        assert response.status_code == 200, response.text
    async with owner_sessionmaker() as db:
        receipts = (await db.scalars(select(ContentInputReceipt).where(ContentInputReceipt.tenant_id == tenant).order_by(ContentInputReceipt.revision))).all()
        assert len(receipts) == 3 and all(row.dependencies_json == [dependency] for row in receipts[1:])
    params = {"workspace_key": body()["workspace_key"]}
    assert (await reader.get("/enterprise/memory/shared/report", params=params)).status_code == 200
    if source_loss == "bytes_changed":
        canonical.write_text("Unverified source rewrite")
    else:
        async with owner_sessionmaker() as db:
            if source_loss == "version_changed":
                (await db.get(KnowledgeDocument, document.id)).canonical_md_sha256 = "0" * 64
            elif source_loss == "revoked":
                (await db.get(KnowledgeGrant, grant.id)).revoked_at = datetime.now(timezone.utc)
            else:
                (await db.get(KnowledgeGrant, grant.id)).expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
            await db.commit()
    for endpoint in ("shared/report", "shared", "shared/search"):
        response = await reader.get("/enterprise/memory/" + endpoint, params={**params, "query": "Human"})
        assert "Human edit" not in response.text
        assert response.status_code == (403 if endpoint == "shared/report" else 200)


@pytest.mark.parametrize("field", ["dependencies", "dependencies_json", "audience_kind", "agent_id"])
async def test_http_cannot_supply_receipt_authority(memory_clients, field):
    _tenant, _users, (writer, _reader), _root = memory_clients
    response = await writer.put("/enterprise/memory/shared", json=body(**{field: [] if "dependencies" in field else "public"}))
    assert response.status_code == 422


async def test_failed_receipt_commit_does_not_make_the_saved_file_consumable(memory_clients, monkeypatch):
    from sqlalchemy.ext.asyncio import AsyncSession

    _tenant, _users, (writer, reader), _root = memory_clients
    created = await writer.put("/enterprise/memory/shared", json=body())
    assert created.status_code == 200
    original_commit = AsyncSession.commit
    failed = False

    async def fail_once(db):
        nonlocal failed
        if not failed:
            failed = True
            raise RuntimeError("Fixture receipt commit failure")
        return await original_commit(db)

    monkeypatch.setattr(AsyncSession, "commit", fail_once)
    saved = await writer.put("/enterprise/memory/shared", json=body("Unconfirmed draft", base_revision=1))
    assert saved.status_code == 503 and saved.json()["detail"]["outcome"] == "unknown"
    assert saved.json()["detail"]["retryable"] is False
    read = await reader.get("/enterprise/memory/shared/report", params={"workspace_key": body()["workspace_key"]})
    assert read.status_code == 409 and "Unconfirmed draft" not in read.text


async def test_concurrent_saves_commit_one_matching_receipt(memory_clients, app_user_sessionmaker):
    tenant, _users, (writer, reader), _root = memory_clients
    assert (await writer.put("/enterprise/memory/shared", json=body())).status_code == 200
    responses = await asyncio.gather(*(
        client.put("/enterprise/memory/shared", json=body(content, base_revision=1))
        for client, content in ((writer, "Saved A"), (reader, "Saved B"))
    ))
    assert sorted(response.status_code for response in responses) == [200, 409]
    read = await reader.get("/enterprise/memory/shared/report", params={"workspace_key": body()["workspace_key"]})
    assert read.status_code == 200 and read.json()["content"] in {"Saved A", "Saved B"}
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        receipts = (await db.scalars(select(ContentInputReceipt).where(ContentInputReceipt.tenant_id == tenant).order_by(ContentInputReceipt.revision))).all()
        assert [row.revision for row in receipts] == [1, 2]
        assert receipts[-1].content_sha256 == hashlib.sha256(read.json()["content"].encode()).hexdigest()


async def test_tombstone_retains_a_verified_revision(memory_clients, owner_sessionmaker):
    from app.models.user import User

    tenant, users, (writer, reader), _root = memory_clients
    users[0].role = "org_admin"
    async with owner_sessionmaker() as db:
        (await db.get(User, users[0].id)).role = "org_admin"
        await db.commit()
    assert (await writer.put("/enterprise/memory/shared", json=body())).status_code == 200
    params = {"workspace_key": body()["workspace_key"], "base_revision": 1}
    deleted = await writer.delete("/enterprise/memory/shared/report", params=params)
    assert deleted.status_code == 200, deleted.text
    assert (await reader.get("/enterprise/memory/shared", params=params)).json() == []
    retained = await reader.get("/enterprise/memory/shared/report", params={**params, "include_deleted": True})
    assert retained.status_code == 200 and retained.json()["revision"] == 2 and retained.json()["deleted"] is True


async def test_rewrite_during_semantic_review_cannot_gain_a_receipt(memory_clients, owner_sessionmaker, monkeypatch):
    from app.services.team_memory import TeamMemoryStore

    tenant, _users, (writer, reader), root = memory_clients
    assert (await writer.put("/enterprise/memory/shared", json=body())).status_code == 200
    entry = TeamMemoryStore(data_root=root).get_entry(str(tenant), body()["workspace_key"], "report")
    path = Path(entry.absolute_path)
    original_prepare = TeamMemoryStore._prepare_content_for_write_with_llm

    async def rewrite_during_review(store, **kwargs):
        prepared = await original_prepare(store, **kwargs)
        path.write_text(path.read_text().replace("Team result", "Unreceipted review-time rewrite"))
        return prepared

    monkeypatch.setattr(TeamMemoryStore, "_prepare_content_for_write_with_llm", rewrite_during_review)
    response = await writer.put("/enterprise/memory/shared", json=body("Human appendix", mode="append", base_revision=1))
    assert response.status_code == 409 and "Unreceipted review-time rewrite" not in response.text
    async with owner_sessionmaker() as db:
        receipts = (await db.scalars(select(ContentInputReceipt).where(ContentInputReceipt.tenant_id == tenant))).all()
        assert len(receipts) == 1
    read = await reader.get("/enterprise/memory/shared/report", params={"workspace_key": body()["workspace_key"]})
    assert read.status_code == 409 and "Unreceipted review-time rewrite" not in read.text
