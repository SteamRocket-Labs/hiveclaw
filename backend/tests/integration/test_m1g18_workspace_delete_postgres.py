"""A parent replacement must not redirect a granted deletion."""

import json
import os
import subprocess
import sys
import uuid

import pytest

from app.database import pin_rls_tenant_context
from tests.integration.test_m1g15_workspace_actions_postgres import read_granted_file  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture
async def deletion(read_granted_file, owner_sessionmaker, app_user_sessionmaker):  # noqa: F811
    from app.models.security_audit import ResourcePermission
    from app.services.workspace_resource_authority import load_workspace_authority_scope
    from app.tools.runtime import ToolExecutionContext

    tenant, user, agent, session, root, path, mine, _scope, grant_id = read_granted_file
    async with owner_sessionmaker() as db:
        (await db.get(ResourcePermission, grant_id)).actions = ["read", "delete"]
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, tenant)
        scope = await load_workspace_authority_scope(db, user, agent_id=agent, session_id=session)
    assert scope.can_read(path) and scope.can_perform(path, "delete")
    foreign = root.parent / "other-agent/workspace/shared/report.txt"
    foreign.parent.mkdir(parents=True)
    foreign.write_text("UNRELATED_RESOURCE_MUST_SURVIVE")
    context = ToolExecutionContext(agent_id=agent, user_id=user.id, tenant_id=str(tenant), workspace=root,
        session_id=str(session), workspace_authority_scope=scope)
    return root, path, mine, foreign, scope, context


@pytest.mark.parametrize("tool", ["delete_file", "delete_directory", "execute_code", "run_command"])
@pytest.mark.parametrize("phase", ["permission", "unlink", None])
async def test_registered_deletion_keeps_the_checked_parent(deletion, monkeypatch, tool, phase):
    from app.services import agent_tools
    from app.services.agent_tool_domains import workspace
    from app.services.workspace_resource_authority import WorkspaceAuthorityScope
    from app.tools.adapters import adapt_and_call
    from app.tools.handlers import filesystem
    from app.tools.runtime import ToolExecutionRequest

    root, path, mine, foreign, scope, context = deletion
    original_allow = workspace._authority_allows_path
    original_perform = WorkspaceAuthorityScope.can_perform
    original_unlink = os.unlink
    replaced = []
    executions = []

    def replace():
        (root / "workspace").rename(root / "held-workspace")
        (root / "workspace").symlink_to(foreign.parent.parent, target_is_directory=True)
        replaced.append(True)

    def checked_allow(loaded, resource_path, **kwargs):
        allowed = original_allow(loaded, resource_path, **kwargs)
        if allowed and phase == "permission" and tool.startswith("delete_") and resource_path == path and kwargs.get("action") == "delete" and not replaced:
            replace()
        return allowed

    def checked_perform(self, resource_path, action):
        allowed = original_perform(self, resource_path, action)
        if allowed and phase == "permission" and not tool.startswith("delete_") and self is scope and resource_path == path and action == "delete" and not replaced:
            replace()
        return allowed

    def checked_unlink(target, **kwargs):
        if phase == "unlink" and str(target).endswith("report.txt") and not replaced:
            replace()
        return original_unlink(target, **kwargs)

    async def native_fixture(execution_workspace, _arguments, **_kwargs):
        subprocess.run([sys.executable, "-I", "-c", f"from pathlib import Path; Path({path!r}).unlink()"],
            cwd=execution_workspace, env={"HOME": str(execution_workspace)}, capture_output=True, timeout=10, check=True)
        executions.append(execution_workspace)
        return "Fixture execution finished"

    monkeypatch.setattr(workspace, "_authority_allows_path", checked_allow)
    monkeypatch.setattr(WorkspaceAuthorityScope, "can_perform", checked_perform)
    monkeypatch.setattr(os, "unlink", checked_unlink)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {checked_unlink})
    monkeypatch.setattr(agent_tools, "_execute_code", native_fixture)
    monkeypatch.setattr(agent_tools, "_run_command", native_fixture)
    name = "delete_file" if tool == "delete_directory" else tool
    handler = getattr(filesystem, name)
    result = await adapt_and_call(handler.meta, handler, ToolExecutionRequest(tool_name=name,
        arguments={"path": "workspace/shared" if tool == "delete_directory" else path}, context=context))
    assert foreign.exists() and foreign.read_text() == "UNRELATED_RESOURCE_MUST_SURVIVE"
    if phase:
        assert replaced
        payload = json.loads(str(result).split("<tool_error>", 1)[1].split("</tool_error>", 1)[0])
        assert payload["http_status"] == (403 if phase == "permission" else 409)
        assert payload["outcome"] == ("denied" if phase == "permission" else "unknown")
        assert payload["retryable"] is False and payload["actionable_hint"]
        assert (root / "held-workspace/shared/report.txt").exists() == (phase == "permission")
    else:
        assert not replaced and not mine.exists() and "<tool_error>" not in str(result)
    assert len(executions) == (0 if tool.startswith("delete_") else 1)


@pytest.mark.parametrize("directory", [False, True])
async def test_native_deletion_rechecks_the_authorized_resource_type(deletion, monkeypatch, directory):
    from app.services.agent_tool_domains import workspace
    from app.tools.handlers.filesystem import delete_file

    root, path, mine, foreign, scope, _context = deletion
    resource = mine.parent if directory else mine
    held = root / "held-source"
    replacement = resource if directory else resource / "private.txt"
    original_allow = workspace._authority_allows_path
    replaced = []

    def change_type(loaded, resource_path, **kwargs):
        allowed = original_allow(loaded, resource_path, **kwargs)
        if allowed and loaded is scope and resource_path == path and kwargs.get("action") == "delete" and not replaced:
            resource.rename(held)
            if not directory:
                resource.mkdir()
            foreign.rename(replacement)
            replaced.append(True)
        return allowed

    monkeypatch.setattr(workspace, "_authority_allows_path", change_type)
    result = delete_file(root, {"path": resource.relative_to(root).as_posix()}, authority_scope=scope)
    assert replaced and replacement.exists() and replacement.read_text() == "UNRELATED_RESOURCE_MUST_SURVIVE"
    assert held.exists()
    payload = json.loads(str(result).split("<tool_error>", 1)[1].split("</tool_error>", 1)[0])
    assert payload["http_status"] == 409 and payload["outcome"] == "unknown" and payload["retryable"] is False


@pytest.mark.parametrize("directory", [False, True])
@pytest.mark.parametrize("phase", ["permission", "unlink", None])
async def test_web_deletion_uses_the_same_storage_boundary(
    deletion, owner_sessionmaker, app_user_sessionmaker, monkeypatch, directory, phase,
):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient
    from app.api import files
    from app.core.security import get_current_user
    from app.database import get_db
    from app.models.user import User
    from app.models.workspace_resource import WorkspaceResourceManifest
    from app.services import workspace_resource_authority as authority
    from sqlalchemy import select

    root, path, mine, foreign, scope, context = deletion
    tenant = uuid.UUID(context.tenant_id)
    async with owner_sessionmaker() as db:
        user = await db.get(User, scope.user_id)

    async def current_user():
        return user

    async def scoped_db():
        async with app_user_sessionmaker() as db:
            await pin_rls_tenant_context(db, tenant)
            try:
                yield db
                await db.commit()
            except Exception:
                await db.rollback()
                raise

    replaced = []
    original_authorize = authority.authorize_workspace_path
    original_unlink = os.unlink

    def replace():
        (root / "workspace").rename(root / "held-workspace")
        (root / "workspace").symlink_to(foreign.parent.parent, target_is_directory=True)
        replaced.append(True)

    async def checked_authorize(*args, **kwargs):
        result = await original_authorize(*args, **kwargs)
        if phase == "permission" and kwargs.get("action") == "delete" and not replaced:
            replace()
        return result

    def checked_unlink(target, **kwargs):
        if phase == "unlink" and str(target).endswith("report.txt") and not replaced:
            replace()
        return original_unlink(target, **kwargs)

    monkeypatch.setattr(authority, "authorize_workspace_path", checked_authorize)
    monkeypatch.setattr(os, "unlink", checked_unlink)
    monkeypatch.setattr(os, "supports_dir_fd", os.supports_dir_fd | {checked_unlink})
    app = FastAPI()
    app.include_router(files.router)
    app.dependency_overrides[get_current_user] = current_user
    app.dependency_overrides[get_db] = scoped_db
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://fixture.local") as client:
        response = await client.delete(f"/agents/{scope.agent_id}/files/content", params={"path": "workspace/shared" if directory else path})
    assert foreign.exists() and foreign.read_text() == "UNRELATED_RESOURCE_MUST_SURVIVE"
    if phase:
        assert replaced and response.status_code == (403 if phase == "permission" else 409), response.text
        detail = response.json()["detail"]
        assert detail["outcome"] == ("denied" if phase == "permission" else "unknown")
        assert detail["retryable"] is False and detail["actionable_hint"]
    else:
        assert response.status_code == 200 and not mine.exists(), response.text
    async with owner_sessionmaker() as db:
        manifest = await db.scalar(select(WorkspaceResourceManifest).where(
            WorkspaceResourceManifest.agent_id == scope.agent_id, WorkspaceResourceManifest.path == path))
        assert (manifest.deleted_at is not None) == (phase is None)
