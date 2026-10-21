"""A selected-company runtime keeps its exact reader and source grants."""

import uuid
from datetime import datetime, timezone

import pytest

from app.database import pin_rls_tenant_context
from app.tools.runtime import ToolExecutionRequest
from tests.integration.test_m4s_original_attachments_postgres import original_api, delivered  # noqa: F401
from tests.integration.test_m4ad_original_tool_reads_postgres import read

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


async def test_native_selected_company_reader_uses_only_owned_or_granted_originals(
    original_api, owner_sessionmaker, app_user_sessionmaker, monkeypatch,  # noqa: F811
):
    from app.config import get_settings
    from app.core.security import create_access_token, decode_access_token
    from app.database import get_db
    from fastapi import Request
    from app.models.security_audit import ResourcePermission
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.runtime.tenant_admission import RuntimeTenantPreconditionError
    from app.services.content_input_receipts import ContentInputReceiptError
    from app.services.original_tool_sources import validate_original_dependency
    from app.tools.resolver import ToolRuntimeResolver

    d, client = original_api
    async def selected_db(request: Request):
        claims = decode_access_token(request.headers["authorization"].removeprefix("Bearer "))
        selected = request.headers.get("x-tenant-id") if claims["role"] == "platform_admin" else None
        async with app_user_sessionmaker() as db:
            # Match TenantMiddleware's optimistic signed-token scope. The real
            # authentication dependency still checks the canonical live role.
            await pin_rls_tenant_context(db, uuid.UUID(selected or claims["tid"]))
            try:
                yield db
                await db.commit()
            except BaseException:
                await db.rollback()
                raise

    client._transport.app.dependency_overrides[get_db] = selected_db
    home, other = uuid.uuid4(), uuid.uuid4()
    monkeypatch.setattr(get_settings(), "BLOB_STORAGE_PROVIDER", "filesystem")
    for module in ("app.tools.resolver", "app.tools.workspace"):
        monkeypatch.setattr(f"{module}.async_session", app_user_sessionmaker)
    monkeypatch.setattr("app.tools.workspace.WORKSPACE_ROOT", d.root.parent)
    async with owner_sessionmaker() as db:
        db.add(Tenant(id=home, name="Reader home", slug=f"reader-{home.hex}"))
        db.add(User(id=other, tenant_id=d.tenant, username=f"other-{other.hex}",
            email=f"{other}@fixture.test", display_name="Other source owner", password_hash="fixture", role="member"))
        await db.flush()
        actor = await db.get(User, d.user.id)
        actor.tenant_id, actor.role = home, "platform_admin"
        await db.commit()
    headers = {"Authorization": "Bearer " + create_access_token(str(d.user.id), "platform_admin", str(home)),
        "X-Tenant-Id": str(d.tenant)}
    own = await client.post("/chat/upload", headers=headers, files={"file": ("own.txt", b"OWN_SELECTED_ORIGINAL")},
        data={"agent_id": str(d.agent), "skip_personal_kb": "true"})
    assert own.status_code == 200, own.text
    # Browser inspection is permitted, but must not become a native source grant.
    other_headers = {"Authorization": "Bearer " + create_access_token(str(other), "member", str(d.tenant))}
    async with owner_sessionmaker() as db:
        from app.models.agent import AgentPermission

        db.add(AgentPermission(tenant_id=d.tenant, agent_id=d.agent, scope_type="user", scope_id=other, access_level="use"))
        await db.commit()
    foreign = await client.post("/chat/upload", headers=other_headers,
        files={"file": ("other.txt", b"OTHER_SELECTED_ORIGINAL")},
        data={"agent_id": str(d.agent), "skip_personal_kb": "true"})
    assert foreign.status_code == 200, foreign.text
    rows = own.json(), foreign.json()
    for row in rows:
        (d.root / row["workspace_path"]).unlink()

    async def frame():
        return await ToolRuntimeResolver().resolve(agent_id=d.agent, user_id=d.user.id,
            session_id=str(d.session), runtime_task_id=str(d.run))

    current = await frame()
    assert current.workspace_authority_scope.user_id == d.user.id
    assert not current.workspace_authority_scope.operator_view
    result = await read(current, rows[0])
    assert "OWN_SELECTED_ORIGINAL" in str(result), str(result)
    denied = await read(current, rows[1])
    assert "auth_or_permission" in str(denied) and "OTHER_SELECTED_ORIGINAL" not in str(denied), str(denied)
    dependency = {"source_kind": "original_attachment", "source_id": rows[1]["original_attachment"]["id"],
        "source_version": rows[1]["original_attachment"]["content_sha256"]}
    request = ToolExecutionRequest(tool_name="fs_read", arguments={}, context=current)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        with pytest.raises(ContentInputReceiptError):
            await validate_original_dependency(db, d.tenant, dependency, runtime_request=request)
        grant = ResourcePermission(tenant_id=d.tenant, principal_type="user", principal_id=d.user.id,
            resource_type="upload_attachment", resource_id=uuid.UUID(dependency["source_id"]),
            actions=["read"], effect="allow", conditions={})
        db.add(grant)
        await db.commit()
        grant_id = grant.id
    assert "OTHER_SELECTED_ORIGINAL" in str(await read(current, rows[1]))
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        await validate_original_dependency(db, d.tenant, dependency, runtime_request=request)
        (await db.get(ResourcePermission, grant_id)).revoked_at = datetime.now(timezone.utc)
        await db.commit()
    denied = await read(current, rows[1])
    assert "OTHER_SELECTED_ORIGINAL" not in str(denied) and "auth_or_permission" in str(denied)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, d.tenant)
        with pytest.raises(ContentInputReceiptError):
            await validate_original_dependency(db, d.tenant, dependency, runtime_request=request)
    async with owner_sessionmaker() as db:
        actor = await db.get(User, d.user.id)
        assert actor.tenant_id == home and actor.role == "platform_admin"
        actor.role = "member"
        await db.commit()
    with pytest.raises(RuntimeTenantPreconditionError):
        await frame()
    denied = await read(current, rows[0])
    assert "OWN_SELECTED_ORIGINAL" not in str(denied) and "auth_or_permission" in str(denied)
