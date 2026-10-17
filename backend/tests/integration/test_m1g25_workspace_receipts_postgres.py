"""Authenticated human saves produce byte-bound workspace receipts."""

import hashlib
import uuid
from datetime import datetime, timezone

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.organization_identity import ContentInputReceipt
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def receipts(db, n, path):
    return (await db.scalars(select(ContentInputReceipt).where(
        ContentInputReceipt.tenant_id == n.tenant, ContentInputReceipt.carrier_kind == "workspace_artifact",
        ContentInputReceipt.carrier_ref == f"{n.agent}\n{path}",
    ).order_by(ContentInputReceipt.revision))).all()


async def test_human_create_and_replace_commit_exact_body_revisions(native_workspace, app_user_sessionmaker):  # noqa: F811
    n = native_workspace
    path = "workspace/human.txt"
    url = f"/agents/{n.agent}/files/content"
    for revision, content in ((1, "Original 你好\r\n"), (2, "Human replacement")):
        response = await n.client.put(url, params={"path": path}, json={"content": content})
        assert response.status_code == 200, response.text
        digest = hashlib.sha256(content.encode()).hexdigest()
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, n.tenant)
            rows = await receipts(db, n, path)
            assert len(rows) == revision
            assert rows[-1].revision == revision and rows[-1].content_sha256 == digest
            assert rows[-1].dependencies_json == []
        assert response.json()["content_revision"] == revision
        read = await n.client.get(url, params={"path": path})
        assert read.status_code == 200 and read.json()["content_hash"] == digest
        assert read.json()["content_revision"] == revision


@pytest.mark.parametrize("field", ["dependencies", "dependencies_json", "audience_kind"])
async def test_browser_cannot_supply_receipt_authority(native_workspace, field):  # noqa: F811
    n = native_workspace
    response = await n.client.put(f"/agents/{n.agent}/files/content", params={"path": "workspace/new.txt"},
        json={"content": "Preserve scope", field: []})
    assert response.status_code == 422, response.text
    assert not (n.root / "workspace/new.txt").exists()


async def test_unreceipted_legacy_file_cannot_be_resigned_by_ordinary_save(native_workspace, app_user_sessionmaker):  # noqa: F811
    from app.models.security_audit import ResourcePermission
    from app.core.resource_authority import workspace_resource_id

    n = native_workspace
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        db.add(ResourcePermission(tenant_id=n.tenant, principal_type="user", principal_id=n.user,
            resource_type="workspace_file", resource_id=workspace_resource_id(n.agent, n.path),
            actions=["write"], conditions={}))
        await db.commit()
    before = n.target.read_bytes()
    response = await n.client.put(f"/agents/{n.agent}/files/content", params={"path": n.path},
        json={"content": "Unverified replacement", "expected_current_hash": hashlib.sha256(before).hexdigest()})
    assert response.status_code == 409, response.text
    assert response.json()["detail"]["code"] == "workspace_content_unverified"
    assert n.target.read_bytes() == before
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        assert not await receipts(db, n, n.path)


async def test_replacement_retains_trusted_source_union_and_rechecks_revocation(
    native_workspace, app_user_sessionmaker, owner_sessionmaker, tmp_path,  # noqa: F811
):
    from app.models.knowledge import KnowledgeDocument, KnowledgeGrant

    n = native_workspace
    path, content = "workspace/derived.txt", "Derived result"
    url = f"/agents/{n.agent}/files/content"
    assert (await n.client.put(url, params={"path": path}, json={"content": content})).status_code == 200
    canonical = tmp_path / "source.md"
    canonical.write_text("Private authorized source")
    digest = hashlib.sha256(canonical.read_bytes()).hexdigest()
    document = KnowledgeDocument(id=uuid.uuid4(), tenant_id=n.tenant, scope_type="person", scope_id=n.foreign,
        owner_user_id=n.foreign, source_kind="text", source_sha256="f" * 64, title="Source", status="ready",
        canonical_md_path="source.md", canonical_md_sha256=digest)
    grant = KnowledgeGrant(id=uuid.uuid4(), tenant_id=n.tenant, scope_id=n.foreign, resource_type="document",
        resource_id=document.id, document_id=document.id, grantee_type="user", grantee_id=n.user, permission="read")
    dependency = {"source_kind": "knowledge_document", "source_id": str(document.id), "source_version": digest}
    async with owner_sessionmaker() as db:
        db.add(document)
        await db.flush()
        db.add(grant)
        row = (await receipts(db, n, path))[0]
        row.dependencies_json = [dependency, dependency]
        await db.commit()
    for content in ("Human replacement", "Second edit without an inline source marker"):
        response = await n.client.put(url, params={"path": path}, json={"content": content})
        assert response.status_code == 200, response.text
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        rows = await receipts(db, n, path)
        assert len(rows) == 3 and all(row.dependencies_json == [dependency] for row in rows[1:])
    async with owner_sessionmaker() as db:
        (await db.get(KnowledgeGrant, grant.id)).revoked_at = datetime.now(timezone.utc)
        await db.commit()
    for method in ("get", "put"):
        response = await n.client.request(method, url, params={"path": path},
            **({"json": {"content": "Cannot drop restrictions"}} if method == "put" else {}))
        assert response.status_code == 403, response.text
        assert content not in response.text
    assert (n.root / path).read_text() == content


@pytest.mark.parametrize("use_expected_hash", [False, True])
async def test_out_of_band_bytes_cannot_receive_a_fresh_receipt(native_workspace, app_user_sessionmaker, use_expected_hash):  # noqa: F811
    from app.models.workspace_resource import WorkspaceResourceManifest

    n = native_workspace
    path = "workspace/current.txt"
    url = f"/agents/{n.agent}/files/content"
    assert (await n.client.put(url, params={"path": path}, json={"content": "Verified original"})).status_code == 200
    (n.root / path).write_text("Unverified external rewrite")
    response = await n.client.put(url, params={"path": path}, json={"content": "Do not resign",
            **({"expected_current_hash": hashlib.sha256(b"Verified original").hexdigest()} if use_expected_hash else {})})
    assert response.status_code == 409, response.text
    assert (n.root / path).read_text() == "Unverified external rewrite"
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        assert len(await receipts(db, n, path)) == 1
        manifest = await db.scalar(select(WorkspaceResourceManifest).where(
            WorkspaceResourceManifest.agent_id == n.agent, WorkspaceResourceManifest.path == path))
        assert manifest.authority_state == "quarantined"


@pytest.mark.parametrize("phase", ["before_save", "after_save"])
async def test_late_external_rewrite_is_preserved_and_quarantined(native_workspace, app_user_sessionmaker, monkeypatch, phase):  # noqa: F811
    from app.api import files
    import app.services.content_input_receipts as service
    from app.models.workspace_resource import WorkspaceResourceManifest

    n = native_workspace
    path = "workspace/late.txt"
    url = f"/agents/{n.agent}/files/content"
    assert (await n.client.put(url, params={"path": path}, json={"content": "Original"})).status_code == 200
    target = n.root / path
    marker = "External rewrite during source validation"
    if phase == "before_save":
        validate = service.validate_browser_workspace_receipt

        async def rewrite_after_validation(*args, **kwargs):
            await validate(*args, **kwargs)
            target.write_text(marker)

        monkeypatch.setattr(service, "validate_browser_workspace_receipt", rewrite_after_validation)
    else:
        hash_file = files._workspace_content_hash
        calls = 0

        def rewrite_before_post_hash(*args):
            nonlocal calls
            calls += 1
            if calls == 2:
                target.write_text(marker)
            return hash_file(*args)

        monkeypatch.setattr(files, "_workspace_content_hash", rewrite_before_post_hash)
    response = await n.client.put(url, params={"path": path}, json={"content": "Attempted save"})
    assert response.status_code == (409 if phase == "before_save" else 503), response.text
    assert target.read_text() == marker
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        assert len(await receipts(db, n, path)) == 1
        manifest = await db.scalar(select(WorkspaceResourceManifest).where(
            WorkspaceResourceManifest.agent_id == n.agent, WorkspaceResourceManifest.path == path))
        assert manifest.authority_state == "quarantined"


async def test_failed_receipt_commit_keeps_possible_effect_unknown_and_unreadable(native_workspace, monkeypatch):  # noqa: F811
    n = native_workspace
    path = "workspace/failed-receipt.txt"
    url = f"/agents/{n.agent}/files/content"
    assert (await n.client.put(url, params={"path": path}, json={"content": "Original"})).status_code == 200

    def fail_record(*_args, **_kwargs):
        raise RuntimeError("Fixture receipt persistence unavailable")

    monkeypatch.setattr("app.services.content_input_receipts.record_workspace_receipt", fail_record)
    response = await n.client.put(url, params={"path": path}, json={"content": "Possibly saved body"})
    assert response.status_code == 503 and response.json()["detail"]["outcome"] == "unknown", response.text
    assert response.json()["detail"]["retryable"] is False
    assert (n.root / path).read_text() == "Possibly saved body"
    read = await n.client.get(url, params={"path": path})
    assert read.status_code == 409 and "Possibly saved body" not in read.text
