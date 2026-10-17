"""Workspace source versions retain their own live audience and upstream refs."""

import uuid
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.database import pin_rls_tenant_context
from app.models.organization_identity import ContentInputReceipt
from app.models.security_audit import ResourcePermission
from app.models.user import User
from app.models.workspace_resource import WorkspaceResourceManifest
from app.services.content_input_receipts import (
    ContentInputReceiptError, latest_workspace_receipt,
    validate_browser_input_dependencies, validate_runtime_input_dependencies,
)
from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401
from tests.integration.test_m1g28_runtime_source_reads_postgres import _create, _read

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def workspace_source(native_workspace, owner_sessionmaker):  # noqa: F811
    n = native_workspace
    source, derived, body = "workspace/source.md", "workspace/derived.md", "DERIVED_FROM_PINNED_SOURCE"
    await _create(n, source, "Original source body")
    await _create(n, derived, body)
    async with owner_sessionmaker() as db:
        receipt = await latest_workspace_receipt(db, n.tenant, n.agent, source)
        dependency = {"source_kind": "workspace_artifact", "source_id": str(receipt.id), "source_version": receipt.content_sha256}
        (await latest_workspace_receipt(db, n.tenant, n.agent, derived)).dependencies_json = [dependency]
        await db.commit()
    request = ToolExecutionRequest(tool_name="read_file", arguments={"path": derived},
        context=ToolExecutionContext(agent_id=n.agent, user_id=n.user, tenant_id=str(n.tenant),
            session_id=str(n.session), runtime_task_id=str(n.run), workspace=n.root))
    return SimpleNamespace(n=n, source=source, derived=derived, body=body, dependency=dependency, request=request)


async def test_real_readers_accept_the_exact_original_carrier(workspace_source):
    s = workspace_source
    response = await s.n.client.get(f"/agents/{s.n.agent}/files/content", params={"path": s.derived})
    assert response.status_code == 200, response.text
    assert response.json()["content"] == s.body
    assert s.body in await _read(s.n, "read_file", s.derived)


async def test_later_verified_write_does_not_rebase_or_taint_the_selected_old_source(
    workspace_source, owner_sessionmaker,
):
    s = workspace_source
    await _create(s.n, s.source, "A later independently authored source version")
    async with owner_sessionmaker() as db:
        latest = await latest_workspace_receipt(db, s.n.tenant, s.n.agent, s.source)
        assert str(latest.id) != s.dependency["source_id"] and latest.content_sha256 != s.dependency["source_version"]
        latest.dependencies_json = [{"source_kind": "unknown", "source_id": "UNSELECTED_LATER_INPUT", "source_version": "v2"}]
        await db.commit()
    response = await s.n.client.get(f"/agents/{s.n.agent}/files/content", params={"path": s.derived})
    assert response.status_code == 200, response.text
    assert s.body in await _read(s.n, "read_file", s.derived)
    async with owner_sessionmaker() as db:
        assert (await latest_workspace_receipt(db, s.n.tenant, s.n.agent, s.derived)).dependencies_json == [s.dependency]


@pytest.mark.parametrize("change", ["version", "bytes", "quarantine", "deleted", "cycle"])
async def test_unverified_or_unusable_source_never_releases_derived_content(
    workspace_source, owner_sessionmaker, app_user_sessionmaker, change,
):
    s = workspace_source
    dependency = dict(s.dependency)
    async with owner_sessionmaker() as db:
        manifest = await db.scalar(select(WorkspaceResourceManifest).where(
            WorkspaceResourceManifest.agent_id == s.n.agent, WorkspaceResourceManifest.path == s.source))
        if change == "version":
            dependency["source_version"] = "f" * 64
        elif change == "bytes":
            (s.n.root / s.source).write_text("OUT_OF_BAND_SOURCE_REWRITE")
        elif change == "quarantine":
            manifest.authority_state = "quarantined"
        elif change == "deleted":
            manifest.deleted_at = datetime.now(timezone.utc)
        else:
            (await db.get(ContentInputReceipt, uuid.UUID(dependency["source_id"]))).dependencies_json = [dependency]
        (await latest_workspace_receipt(db, s.n.tenant, s.n.agent, s.derived)).dependencies_json = [dependency]
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, s.n.tenant)
        with pytest.raises(ContentInputReceiptError) as error:
            await validate_runtime_input_dependencies(db, s.request, [dependency])
        assert error.value.status_code == (503 if change == "cycle" else 403)
    assert s.body not in await _read(s.n, "read_file", s.derived)


async def test_transitive_grant_revocation_is_live_and_browser_admin_does_not_authorize_native(
    workspace_source, owner_sessionmaker, app_user_sessionmaker,
):
    from app.core.resource_authority import workspace_resource_id

    s = workspace_source
    upstream = "workspace/upstream.md"
    await _create(s.n, upstream, "Private upstream body")
    grant_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        receipt = await latest_workspace_receipt(db, s.n.tenant, s.n.agent, upstream)
        (await db.get(ContentInputReceipt, uuid.UUID(s.dependency["source_id"]))).dependencies_json = [{
            "source_kind": "workspace_artifact", "source_id": str(receipt.id), "source_version": receipt.content_sha256}]
        manifest = await db.scalar(select(WorkspaceResourceManifest).where(
            WorkspaceResourceManifest.agent_id == s.n.agent, WorkspaceResourceManifest.path == upstream))
        manifest.owner_user_id, manifest.root_session_id = s.n.foreign, None
        (await db.get(User, s.n.user)).role = "org_admin"
        db.add(ResourcePermission(id=grant_id, tenant_id=s.n.tenant, principal_type="user", principal_id=s.n.user,
            resource_type="workspace_file", resource_id=workspace_resource_id(s.n.agent, upstream),
            actions=["read"], effect="allow", conditions={}))
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, s.n.tenant)
        retained = await db.get(ResourcePermission, grant_id)
        await validate_runtime_input_dependencies(db, s.request, [s.dependency])
        await db.commit()
        async with owner_sessionmaker() as writer:
            (await writer.get(ResourcePermission, grant_id)).effect = "deny"
            await writer.commit()
        user = await db.get(User, s.n.user)
        await validate_browser_input_dependencies(db, s.n.tenant, user, [s.dependency])
        with pytest.raises(ContentInputReceiptError) as error:
            await validate_runtime_input_dependencies(db, s.request, [s.dependency])
        assert error.value.status_code == 403
        assert retained.id == grant_id  # Keep the ORM row alive across the external revoke.
