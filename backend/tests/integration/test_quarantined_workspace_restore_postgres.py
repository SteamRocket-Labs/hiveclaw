"""Explicit version restoration preserves quarantined bytes without resigning them."""

import hashlib
import json

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.audit import AuditLog
from app.models.user import User
from app.models.workspace_resource import WorkspaceResourceManifest
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m1g25_workspace_receipts_postgres import receipts
from tests.integration.test_m1g28_runtime_source_reads_postgres import _read

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def quarantined_file(native_workspace, owner_sessionmaker):  # noqa: F811
    n = native_workspace
    n.path = "workspace/recovery.md"
    n.base = f"/agents/{n.agent}/files"
    for body in ("First authenticated version", "Human saved revision"):
        saved = await n.client.put(f"{n.base}/content", params={"path": n.path}, json={"content": body})
        assert saved.status_code == 200, saved.text
    n.saved = saved.json()
    history = await n.client.get(f"{n.base}/versions", params={"path": n.path})
    assert history.status_code == 200, history.text
    n.version = next(row for row in history.json()["versions"] if row["content_hash"] == n.saved["content_hash"])
    n.restore_url = f"{n.base}/versions/{n.version['version_id']}/restore"
    n.target = n.root / n.path
    n.unverified = b"UNRECEIPTED_EDIT_PRESERVE_THIS"
    n.target.write_bytes(n.unverified)
    read = await _read(n, "read_file", n.path)
    assert n.unverified.decode() not in read and "workspace_content_unverified" in read, read
    n.restore_request = {
        "expected_current_exists": True,
        "expected_current_hash": hashlib.sha256(n.unverified).hexdigest(),
    }
    async with owner_sessionmaker() as db:
        manifest = await db.scalar(
            select(WorkspaceResourceManifest).where(
                WorkspaceResourceManifest.agent_id == n.agent, WorkspaceResourceManifest.path == n.path
            )
        )
        assert manifest.authority_state == "quarantined"
        n.identity = (manifest.id, manifest.owner_user_id, manifest.root_session_id)
        n.receipts = [
            (row.id, row.revision, row.content_sha256, row.dependencies_json) for row in await receipts(db, n, n.path)
        ]
        (await db.get(User, n.user)).role = "org_admin"
        await db.commit()
    return n


async def test_quarantined_version_restore_preserves_evidence_identity_and_native_read(
    quarantined_file,
    app_user_sessionmaker,
):
    n = quarantined_file
    restored = await n.client.post(n.restore_url, params={"path": n.path}, json=n.restore_request)
    assert restored.status_code == 200, restored.text
    assert n.target.read_text() == "Human saved revision"
    current = await n.client.get(f"{n.base}/content", params={"path": n.path})
    assert current.status_code == 200 and current.json()["content_revision"] == 3, current.text
    assert current.json()["content_hash"] == n.saved["content_hash"]
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        manifest = await db.scalar(
            select(WorkspaceResourceManifest).where(
                WorkspaceResourceManifest.agent_id == n.agent, WorkspaceResourceManifest.path == n.path
            )
        )
        assert (manifest.id, manifest.owner_user_id, manifest.root_session_id) == n.identity
        assert manifest.authority_state == "owned"
        assert manifest.content_hash == n.saved["content_hash"]
        rows = await receipts(db, n, n.path)
        assert [(row.id, row.revision, row.content_sha256, row.dependencies_json) for row in rows[:2]] == n.receipts
        assert len(rows) == 3 and rows[-1].content_sha256 == n.saved["content_hash"]
        assert all(row.content_sha256 != n.restore_request["expected_current_hash"] for row in rows)
        audit = await db.scalar(
            select(AuditLog).where(AuditLog.agent_id == n.agent, AuditLog.action == "workspace_file_version_restored")
        )
        assert audit.details["previous_authority_state"] == "quarantined"
        retained = audit.details["retained_quarantined_version"]
        assert retained["content_hash"] == n.restore_request["expected_current_hash"]
        # Failure bytes stay outside every Agent's workspace and are not content receipts.
        directory = n.root.parent / ".workspace-quarantine" / str(n.agent) / retained["id"]
        assert (directory / "content").read_bytes() == n.unverified
        metadata = json.loads((directory / "manifest.json").read_text())
        assert metadata["status"] == "unverified"
        assert metadata["path"] == n.path and metadata["version_id"] == n.version["version_id"]
        assert metadata["tenant_id"] == str(n.tenant) and metadata["actor_user_id"] == str(n.user)
    assert "Human saved revision" in await _read(n, "read_file", n.path)
    rejected = await _read(n, "read_file", f"../.workspace-quarantine/{n.agent}/{retained['id']}/content")
    assert n.unverified.decode() not in rejected and "<tool_error>" in rejected


@pytest.mark.parametrize("failure", ["stale", "member", "changed_snapshot"])
async def test_quarantine_restore_denial_preserves_original_and_receipts(
    quarantined_file,
    owner_sessionmaker,
    failure,
):
    n = quarantined_file
    request = dict(n.restore_request)
    if failure == "stale":
        request["expected_current_hash"] = "0" * 64
    elif failure == "member":
        async with owner_sessionmaker() as db:
            (await db.get(User, n.user)).role = "member"
            await db.commit()
    else:
        snapshot = n.root / f"runtime_artifacts/workspace_content_versions/{n.receipts[-1][0]}/files/recovery.md"
        snapshot.write_text("Substituted snapshot")
    denied = await n.client.post(n.restore_url, params={"path": n.path}, json=request)
    assert denied.status_code == {"stale": 409, "member": 403, "changed_snapshot": 503}[failure], denied.text
    assert n.target.read_bytes() == n.unverified
    assert not (n.root.parent / ".workspace-quarantine").exists()
    async with owner_sessionmaker() as db:
        rows = await receipts(db, n, n.path)
        assert [(row.id, row.revision, row.content_sha256, row.dependencies_json) for row in rows] == n.receipts
        manifest = await db.scalar(
            select(WorkspaceResourceManifest).where(
                WorkspaceResourceManifest.agent_id == n.agent, WorkspaceResourceManifest.path == n.path
            )
        )
        assert manifest.authority_state == "quarantined"


async def test_restore_cannot_drop_a_removed_dependency_of_the_current_receipt(
    quarantined_file,
    owner_sessionmaker,
):
    n = quarantined_file
    source_path = "workspace/source.md"
    created = await n.client.put(
        f"{n.base}/content", params={"path": source_path}, json={"content": "Restricted input"}
    )
    assert created.status_code == 200, created.text
    async with owner_sessionmaker() as db:
        source = (await receipts(db, n, source_path))[-1]
        current = (await receipts(db, n, n.path))[-1]
        current.dependencies_json = [
            {
                "source_kind": "workspace_artifact",
                "source_id": str(source.id),
                "source_version": source.content_sha256,
            }
        ]
        await db.commit()
    removed = await n.client.delete(f"{n.base}/content", params={"path": source_path})
    assert removed.status_code == 200, removed.text
    history = await n.client.get(f"{n.base}/versions", params={"path": n.path})
    assert history.status_code == 200, history.text
    original = next(row for row in history.json()["versions"] if row["content_hash"] == n.receipts[0][2])
    response = await n.client.post(
        f"{n.base}/versions/{original['version_id']}/restore", params={"path": n.path}, json=n.restore_request
    )
    assert response.status_code == 403, response.text
    assert n.target.read_bytes() == n.unverified
    assert not (n.root.parent / ".workspace-quarantine").exists()
    async with owner_sessionmaker() as db:
        assert len(await receipts(db, n, n.path)) == 2


@pytest.mark.parametrize("failure", ["retention", "late_change"])
async def test_restore_does_not_replace_bytes_without_exact_retention(
    quarantined_file,
    owner_sessionmaker,
    monkeypatch,
    failure,
):
    from app.services import workspace_receipt_versions

    n = quarantined_file
    retain = workspace_receipt_versions.retain_quarantined_workspace_version

    def unavailable_or_changed(**kwargs):
        if failure == "retention":
            raise OSError("Fixture retained storage unavailable")
        result = retain(**kwargs)
        n.target.write_bytes(b"LATER_UNVERIFIED_BYTES")
        return result

    monkeypatch.setattr(workspace_receipt_versions, "retain_quarantined_workspace_version", unavailable_or_changed)
    response = await n.client.post(n.restore_url, params={"path": n.path}, json=n.restore_request)
    assert response.status_code == (503 if failure == "retention" else 409), response.text
    assert n.target.read_bytes() == (n.unverified if failure == "retention" else b"LATER_UNVERIFIED_BYTES")
    async with owner_sessionmaker() as db:
        rows = await receipts(db, n, n.path)
        assert [(row.id, row.revision, row.content_sha256, row.dependencies_json) for row in rows] == n.receipts
        manifest = await db.scalar(
            select(WorkspaceResourceManifest).where(
                WorkspaceResourceManifest.agent_id == n.agent, WorkspaceResourceManifest.path == n.path
            )
        )
        assert manifest.authority_state == "quarantined"


async def test_database_failure_rolls_back_restored_file_and_authority(
    quarantined_file,
    app_user_sessionmaker,
    monkeypatch,
):
    from app.api.files import FileVersionRestoreRequest, restore_file_version

    n = quarantined_file
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        user = await db.get(User, n.user)

        async def fail_commit():
            raise RuntimeError("Fixture restore commit unavailable")

        monkeypatch.setattr(db, "commit", fail_commit)
        with pytest.raises(RuntimeError, match="Fixture restore commit unavailable"):
            await restore_file_version(
                agent_id=n.agent,
                version_id=n.version["version_id"],
                path=n.path,
                data=FileVersionRestoreRequest(**n.restore_request),
                current_user=user,
                db=db,
            )
    assert n.target.read_bytes() == n.unverified
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        rows = await receipts(db, n, n.path)
        assert [(row.id, row.revision, row.content_sha256, row.dependencies_json) for row in rows] == n.receipts
        manifest = await db.scalar(
            select(WorkspaceResourceManifest).where(
                WorkspaceResourceManifest.agent_id == n.agent, WorkspaceResourceManifest.path == n.path
            )
        )
        assert manifest.authority_state == "quarantined"
        assert (
            await db.scalar(
                select(AuditLog.id).where(
                    AuditLog.agent_id == n.agent, AuditLog.action == "workspace_file_version_restored"
                )
            )
            is None
        )
    retained = list((n.root.parent / ".workspace-quarantine" / str(n.agent)).iterdir())
    assert len(retained) == 1 and (retained[0] / "content").read_bytes() == n.unverified
