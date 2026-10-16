"""Read grants must not become grants for other workspace effects."""

import uuid

import pytest

from app.database import pin_rls_tenant_context
from tests.services.test_session_input_control_v2 import _seed_session

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def read_granted_file(owner_sessionmaker, app_user_sessionmaker, monkeypatch, tmp_path):
    from app.config import get_settings
    from app.core.resource_authority import workspace_resource_id
    from app.models.security_audit import ResourcePermission
    from app.models.user import User
    from app.services.workspace_resource_authority import load_workspace_authority_scope, register_workspace_path

    tenant, user_id, agent, session, _run = await _seed_session(owner_sessionmaker)
    path = "workspace/shared/report.txt"
    root = tmp_path / str(agent)
    target = root / path
    target.parent.mkdir(parents=True)
    target.write_text("Other owner's original contents")
    monkeypatch.setattr(get_settings(), "AGENT_DATA_DIR", str(tmp_path))
    async with owner_sessionmaker() as db:
        user = await db.get(User, user_id)
        user.role = "member"
        other = uuid.uuid4()
        db.add(User(id=other, tenant_id=tenant, username=f"action-{other.hex}", email=f"{other.hex}@fixture.test",
            password_hash="fixture", display_name="Other owner", role="member"))
        await db.flush()
        await register_workspace_path(db, tenant_id=tenant, agent_id=agent, path=path,
            owner_user_id=other, root_session_id=None, source="fixture")
        grant = ResourcePermission(tenant_id=tenant, principal_type="user", principal_id=user_id,
            resource_type="workspace_file", resource_id=workspace_resource_id(agent, path), actions=["read"], conditions={})
        db.add(grant)
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        scope = await load_workspace_authority_scope(db, user, agent_id=agent, session_id=session)
    return tenant, user, agent, session, root, path, target, scope, grant.id


@pytest.mark.parametrize("action", ["write", "delete", "create", "deliver"])
async def test_read_grant_keeps_canonical_and_native_action_checks_identical(read_granted_file, app_user_sessionmaker, action):
    from app.services.workspace_resource_authority import WorkspaceAuthorityError, authorize_workspace_path, authorize_workspace_tool_path

    tenant, user, agent, _session, root, path, _target, scope, _grant = read_granted_file
    assert scope.can_read(path)
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        await authorize_workspace_path(db, user, agent_id=agent, path=path, action="read", path_exists=True)
        with pytest.raises(WorkspaceAuthorityError):
            await authorize_workspace_path(db, user, agent_id=agent, path=path, action=action, path_exists=True)
    with pytest.raises(WorkspaceAuthorityError) as denied:
        authorize_workspace_tool_path(root, scope, path, action=action)
    assert denied.value.code == "workspace_resource_forbidden"


@pytest.mark.parametrize("tool", ["write_file", "delete_file"])
async def test_registered_mutation_does_not_use_a_read_grant(read_granted_file, tool):
    from app.tools.adapters import adapt_and_call
    from app.tools.handlers import filesystem
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    tenant, user, agent, session, root, path, target, scope, _grant = read_granted_file
    context = ToolExecutionContext(agent_id=agent, user_id=user.id, tenant_id=str(tenant), workspace=root,
        session_id=str(session), workspace_authority_scope=scope)
    read = filesystem.read_file
    body = await adapt_and_call(read.meta, read, ToolExecutionRequest(tool_name="read_file", arguments={"path": path}, context=context))
    assert "Other owner's original contents" in str(body)
    handler = getattr(filesystem, tool)
    result = await adapt_and_call(handler.meta, handler, ToolExecutionRequest(tool_name=tool,
        arguments={"path": path, "content": "Unauthorized write"}, context=context))
    assert target.exists() and target.read_text() == "Other owner's original contents"
    assert "auth_or_permission" in str(result)


@pytest.mark.parametrize("actions", [
    ["write"], ["read", "write"], ["read", "delete"], ["read", "create"], ["read", "deliver"],
])
async def test_exact_effect_grants_preserve_only_their_declared_actions(
    read_granted_file, owner_sessionmaker, app_user_sessionmaker, actions,
):
    from app.models.security_audit import ResourcePermission
    from app.services.workspace_resource_authority import load_workspace_authority_scope
    from app.tools.handlers.filesystem import read_file, write_file

    tenant, user, agent, session, root, path, target, _scope, grant_id = read_granted_file
    async with owner_sessionmaker() as db:
        grant = await db.get(ResourcePermission, grant_id)
        grant.actions = actions
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        scope = await load_workspace_authority_scope(db, user, agent_id=agent, session_id=session)
    assert scope.can_read(path) == ("read" in actions)
    for action in ("write", "delete", "create", "deliver"):
        assert scope.can_perform(path, action) == (action in actions)
    if actions == ["write"]:
        assert "auth_or_permission" in str(read_file(root, {"path": path}, authority_scope=scope))
        result = write_file(root, {"path": path, "content": "Explicitly authorized write"}, authority_scope=scope)
        assert "Written" in result and target.read_text() == "Explicitly authorized write"


@pytest.mark.parametrize("state", ["expired", "revoked", "deny"])
async def test_nonlive_or_denied_write_grants_do_not_change_read_access(
    read_granted_file, owner_sessionmaker, app_user_sessionmaker, state,
):
    from datetime import datetime, timedelta, timezone
    from app.core.resource_authority import workspace_resource_id
    from app.models.security_audit import ResourcePermission
    from app.services.workspace_resource_authority import load_workspace_authority_scope

    tenant, user, agent, session, _root, path, _target, _scope, _grant = read_granted_file
    async with owner_sessionmaker() as db:
        grant = ResourcePermission(tenant_id=tenant, principal_type="user", principal_id=user.id,
            resource_type="workspace_file", resource_id=workspace_resource_id(agent, path), actions=["write"], conditions={})
        if state == "expired":
            grant.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        elif state == "revoked":
            grant.revoked_at = datetime.now(timezone.utc)
        db.add(grant)
        if state == "deny":
            db.add(ResourcePermission(tenant_id=tenant, principal_type="user", principal_id=user.id,
                resource_type="workspace_file", resource_id=workspace_resource_id(agent, path), actions=["write"], effect="deny", conditions={}))
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        scope = await load_workspace_authority_scope(db, user, agent_id=agent, session_id=session)
    assert scope.can_read(path) and not scope.can_perform(path, "write")


@pytest.mark.parametrize("authority", ["resource_owner", "root_session_owner", "org_admin"])
@pytest.mark.parametrize("operator_view", [False, True])
async def test_canonical_owner_and_administrator_actions_are_preserved(
    read_granted_file, owner_sessionmaker, app_user_sessionmaker, authority, operator_view,
):
    from app.models.security_audit import ResourcePermission
    from app.models.user import User
    from app.models.workspace_resource import WorkspaceResourceManifest
    from app.services.workspace_resource_authority import load_workspace_authority_scope
    from sqlalchemy import select

    tenant, user, agent, session, root, path, _target, _scope, _grant = read_granted_file
    async with owner_sessionmaker() as db:
        manifest = await db.scalar(select(WorkspaceResourceManifest).where(
            WorkspaceResourceManifest.agent_id == agent, WorkspaceResourceManifest.path == path))
        if authority == "resource_owner":
            manifest.owner_user_id = user.id
        elif authority == "root_session_owner":
            manifest.root_session_id = session
        else:
            (await db.get(User, user.id)).role = "org_admin"
        if operator_view and authority != "org_admin":
            db.add(ResourcePermission(tenant_id=tenant, principal_type="user", principal_id=user.id,
                resource_type="agent", resource_id=agent, actions=["operator.inspect"],
                conditions={"operator_inspection": {"schema": "hive.agent.operator_inspection.v1"}}))
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        fresh_user = await db.get(User, user.id)
        scope = await load_workspace_authority_scope(db, fresh_user, agent_id=agent, session_id=session,
            operator_view=operator_view, operator_reason="Fixture ownership inspection" if operator_view else None)
    assert scope.can_read(path)
    assert all(scope.can_perform(path, action) for action in ("write", "delete", "create", "deliver"))
    if authority == "org_admin":
        legacy = root / "workspace/legacy.txt"
        legacy.write_text("Unregistered legacy resource")
        assert scope.can_perform("workspace/legacy.txt", "write")


@pytest.mark.parametrize("delete", [False, True])
@pytest.mark.parametrize("operator_view", [False, True])
async def test_code_writeback_does_not_use_read_or_inspection_authority(
    read_granted_file, owner_sessionmaker, app_user_sessionmaker, delete, operator_view,
):
    from app.models.security_audit import ResourcePermission
    from app.services.agent_tool_domains.code_exec import WorkspaceExecutionAuthorityError, authorized_execution_workspace
    from app.services.workspace_resource_authority import load_workspace_authority_scope

    tenant, user, agent, session, root, path, target, scope, _grant = read_granted_file
    if operator_view:
        async with owner_sessionmaker() as db:
            db.add(ResourcePermission(tenant_id=tenant, principal_type="user", principal_id=user.id,
                resource_type="agent", resource_id=agent, actions=["operator.inspect"],
                conditions={"operator_inspection": {"schema": "hive.agent.operator_inspection.v1"}}))
            await db.commit()
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            scope = await load_workspace_authority_scope(db, user, agent_id=agent, session_id=session,
                operator_view=True, operator_reason="Fixture resource inspection")
        (root / "workspace/legacy.txt").write_text("Inspectable legacy content")
    with pytest.raises(WorkspaceExecutionAuthorityError):
        with authorized_execution_workspace(root, scope) as isolated:
            copy = isolated / path
            assert copy.read_text() == "Other owner's original contents"
            if operator_view:
                assert (isolated / "workspace/legacy.txt").read_text() == "Inspectable legacy content"
            copy.unlink() if delete else copy.write_text("Unauthorized code writeback")
    assert target.exists() and target.read_text() == "Other owner's original contents"


async def test_read_grant_can_support_analysis_and_an_independent_new_output(read_granted_file):
    from app.services.agent_tool_domains.code_exec import authorized_execution_workspace

    _tenant, _user, _agent, _session, root, path, target, scope, _grant = read_granted_file
    with authorized_execution_workspace(root, scope) as isolated:
        contents = (isolated / path).read_text()
        (isolated / "workspace/new-draft.txt").write_text("Private analysis: " + contents)
    assert target.read_text() == "Other owner's original contents"
    assert (root / "workspace/new-draft.txt").read_text() == "Private analysis: Other owner's original contents"


async def test_scope_rejects_an_unrelated_prefetched_agent(read_granted_file, app_user_sessionmaker):
    from types import SimpleNamespace
    from fastapi import HTTPException
    from app.services.workspace_resource_authority import load_workspace_authority_scope

    tenant, user, agent, _session, _root, _path, _target, _scope, _grant = read_granted_file
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        with pytest.raises(HTTPException) as denied:
            await load_workspace_authority_scope(db, user, agent_id=agent,
                agent_access=(SimpleNamespace(id=uuid.uuid4(), tenant_id=tenant), "manage"))
    assert denied.value.status_code == 403 and "context mismatch" in denied.value.detail
