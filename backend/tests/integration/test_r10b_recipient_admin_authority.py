"""Recipient reads require member grants while browser inspection retains its authority."""

import uuid

import pytest

from app.database import pin_rls_tenant_context
from tests.integration.test_m2n_project_context_postgres import (
    _input,
    _project,
    _sealed_read,
    native_workspace,  # noqa: F401
    project_http,  # noqa: F401
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("tool", ["read_file", "grep_search"])
async def test_revoked_project_grant_withholds_admin_working_copy_but_keeps_browser_inspection(
    project_http, owner_sessionmaker, app_user_sessionmaker, tool,  # noqa: F811
):
    from app.models.security_audit import ResourcePermission
    from app.models.user import User
    from app.models.workbench_project import WorkbenchProject
    from app.services.content_input_receipts import validate_browser_input_dependencies
    from app.services.session_model_inputs import _origin_dependencies
    from tests.integration.test_artifact_publication_transactions import stage_report
    from tests.integration.test_r10b_workspace_execution_readers import _enable_commands, _execute

    p = project_http
    n = p.n
    selected = await _project(p, owner_sessionmaker)
    grant_id = uuid.uuid4()
    async with owner_sessionmaker() as db:
        await _enable_commands(db, n)
        project = await db.get(WorkbenchProject, uuid.UUID(selected["project_id"]))
        project.owner_user_id = n.foreign
        (await db.get(User, n.user)).role = "org_admin"
        db.add(ResourcePermission(id=grant_id, tenant_id=n.tenant, principal_type="user", principal_id=n.user,
            resource_type="workbench_project", resource_id=project.id, actions=["read"], conditions={}))
        await db.commit()
    input_id = await _input(p, selected, app_user_sessionmaker)
    invocation_id = await _sealed_read(p, selected, input_id, app_user_sessionmaker)
    path = "workspace/project-working-copy.md"
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        sources, complete = await _origin_dependencies(db, tenant_id=n.tenant, agent_id=n.agent,
            ref=f"session-tool-result:{invocation_id}")
        assert complete and any(source["source_kind"] == "project_resource" for source in sources), sources
        await stage_report(db, tenant=n.tenant, agent=n.agent, user=n.user, session=n.session,
            root=n.root, path=path, dependencies=sources)
        await db.commit()
    assert "COMPANY_SOURCE_MARKER" in str(await _execute(n, tool, path))
    async with owner_sessionmaker() as db:
        await db.delete(await db.get(ResourcePermission, grant_id))
        await db.commit()
    denied = str(await _execute(n, tool, path))
    assert "COMPANY_SOURCE_MARKER" not in denied, denied
    if tool == "read_file":
        assert "project_access_denied" in denied, denied
    # The same source remains inspectable through the administrator's browser authority.
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        await validate_browser_input_dependencies(db, n.tenant, await db.get(User, n.user), sources)
    inspected = await p.request("GET", f"/{selected['project_id']}")
    assert inspected.status_code == 200 and inspected.json()["id"] == selected["project_id"], inspected.text


async def test_directory_scope_returns_typed_denial_for_malformed_task_session(
    native_workspace, owner_sessionmaker, app_user_sessionmaker,  # noqa: F811
):
    from app.models.runtime_task import RuntimeTask
    from app.services.agent_directory_sources import _scope
    from app.services.content_input_receipts import ContentInputReceiptError

    n = native_workspace
    async with owner_sessionmaker() as db:
        task = await db.get(RuntimeTask, n.run)
        task.child_session_id = "malformed-task-session"
        await db.commit()
    async with app_user_sessionmaker() as db:
        await pin_rls_tenant_context(db, n.tenant)
        with pytest.raises(ContentInputReceiptError) as denied:
            await _scope(db, tenant_id=n.tenant, agent_id=n.agent, session_id=n.session, user_id=n.user, run_id=n.run)
        assert denied.value.code == "content_source_access_denied" and denied.value.status_code == 403
