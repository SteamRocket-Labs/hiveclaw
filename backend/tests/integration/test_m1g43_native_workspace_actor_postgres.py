"""Browser administrator authority is not a native workspace source grant."""

import platform
import shlex

import pytest
from fastapi import HTTPException

from app.models.capability_policy import CapabilityPolicy
from app.models.user import User
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.fixture(params=["org_admin", "platform_admin"])
async def admin_workspace(native_workspace, owner_sessionmaker, request):  # noqa: F811
    n = native_workspace
    async with owner_sessionmaker() as db:
        (await db.get(User, n.user)).role = request.param
        for capability in (
            "workspace.file.read",
            "workspace.file.delete",
            "workspace.command.destructive_delete",
            "workspace.code.execute",
            "workspace.command.execute",
        ):
            db.add(CapabilityPolicy(tenant_id=n.tenant, capability=capability, allowed=True, requires_approval=False))
        await db.commit()
    response = await n.client.put(
        f"/agents/{n.agent}/files/content",
        params={"path": "workspace/allowed.txt"},
        json={"content": "OWN_ADMIN_WORKSPACE_INPUT"},
    )
    assert response.status_code == 200, response.text
    return n


async def _tool(n, name, args):
    from app.runtime.ccplus_contracts import PermissionProfileV1
    from app.services.approval_ticket import hash_tool_input

    profile = None
    if name == "delete_file":
        # Confirm the exact tool payload so resource authority is actually exercised.
        profile = PermissionProfileV1(
            session_grant_scope="once",
            session_grant_tool_name=name,
            session_grant_input_hash=hash_tool_input(name, args),
        )
    return await n.service.execute(
        name,
        args,
        agent_id=n.agent,
        user_id=n.user,
        session_id=str(n.session),
        runtime_task_id=str(n.run),
        emit_runtime_hooks=False,
        permission_profile=profile,
    )


@pytest.mark.parametrize(
    "tool",
    [
        "read_file",
        "fs_read",
        "read_document",
        "list_files",
        "glob_search",
        "grep_search",
        "execute_code",
        "run_command",
        "write_file",
        "delete_file",
    ],
)
async def test_admin_browser_inspection_never_materializes_or_mutates_another_owners_native_workspace(
    admin_workspace, tool
):
    n = admin_workspace
    if tool in {"execute_code", "run_command"} and platform.system() != "Darwin":
        pytest.skip("Native macOS process gate; Linux delivery is deferred.")
    original = n.target.read_bytes()
    browser = await n.client.get(f"/agents/{n.agent}/files/content", params={"path": n.path})
    assert browser.status_code == 200 and "PRIVATE_FOREIGN_FILE_BODY" in browser.text, browser.text
    if tool in {"execute_code", "run_command"}:
        code = (
            "from pathlib import Path\nprint(Path('allowed.txt').read_text())\ntry:\n"
            " print(Path('private-report.md').read_text())\nexcept OSError:\n print('FOREIGN_WORKSPACE_DENIED')\n"
        )
        args = (
            {"language": "python", "code": code}
            if tool == "execute_code"
            else {"command": "python3 -c " + shlex.quote(code)}
        )
    elif tool in {"list_files", "glob_search", "grep_search"}:
        args = {"path" if tool == "list_files" else "root": "workspace"}
        if tool != "list_files":
            args["pattern"] = "*" if tool == "glob_search" else "WORKSPACE|FOREIGN_FILE"
    else:
        args = {"path": n.path, **({"content": "UNAUTHORIZED_REPLACEMENT"} if tool == "write_file" else {})}
    raw = await _tool(n, tool, args)
    result = str(raw)
    assert "PRIVATE_FOREIGN_FILE_BODY" not in result, result
    if tool in {"execute_code", "run_command"}:
        assert "OWN_ADMIN_WORKSPACE_INPUT" in result and "FOREIGN_WORKSPACE_DENIED" in result, result
        assert raw.metadata["code_execution_evidence"]["isolation"] == "sandbox-exec"
    elif tool in {"list_files", "glob_search", "grep_search"}:
        assert "allowed.txt" in result and "private-report.md" not in result, result
    else:
        assert "auth_or_permission" in result, result
    assert n.target.is_file() and n.target.read_bytes() == original
    own = await _tool(n, "read_file", {"path": "workspace/allowed.txt"})
    assert "OWN_ADMIN_WORKSPACE_INPUT" in str(own), own


async def test_actual_explicit_read_grant_is_preserved_and_revocation_is_live(admin_workspace, owner_sessionmaker):
    import hashlib
    from app.core.resource_authority import workspace_resource_id
    from app.models.security_audit import ResourcePermission
    from app.services.content_input_receipts import record_workspace_receipt

    n = admin_workspace
    async with owner_sessionmaker() as db:
        record_workspace_receipt(db, n.tenant, n.agent, n.path, hashlib.sha256(n.target.read_bytes()).hexdigest(), None)
        await db.commit()
    assert "PRIVATE_FOREIGN_FILE_BODY" not in str(await _tool(n, "read_file", {"path": n.path}))
    async with owner_sessionmaker() as db:
        grant = ResourcePermission(
            tenant_id=n.tenant,
            principal_type="user",
            principal_id=n.user,
            resource_type="workspace_file",
            resource_id=workspace_resource_id(n.agent, n.path),
            actions=["read"],
            conditions={},
        )
        db.add(grant)
        await db.commit()
        grant_id = grant.id
    assert "PRIVATE_FOREIGN_FILE_BODY" in str(await _tool(n, "read_file", {"path": n.path}))
    denied_write = await _tool(n, "write_file", {"path": n.path, "content": "UNAUTHORIZED_REPLACEMENT"})
    assert "auth_or_permission" in str(denied_write) and n.target.read_text() == "PRIVATE_FOREIGN_FILE_BODY"
    async with owner_sessionmaker() as db:
        (await db.get(ResourcePermission, grant_id)).actions = []
        await db.commit()
    assert "PRIVATE_FOREIGN_FILE_BODY" not in str(await _tool(n, "read_file", {"path": n.path}))


async def test_native_workspace_scope_cannot_select_the_browser_inspection_profile(
    native_workspace,  # noqa: F811
    app_user_sessionmaker,
):
    from app.database import pin_rls_tenant_context
    from app.services.personal_knowledge_access import AgentRuntimePrincipal
    from app.services.workspace_resource_authority import load_workspace_authority_scope

    n = native_workspace
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        user = await db.get(User, n.user)
        with pytest.raises(HTTPException) as denied:
            await load_workspace_authority_scope(
                db,
                user,
                agent_id=n.agent,
                session_id=n.session,
                operator_view=True,
                runtime_principal=AgentRuntimePrincipal(n.agent, n.user, session_id=str(n.session)),
            )
        assert denied.value.status_code == 403
