"""Workspace input lookup must not issue one database round trip per file."""

from pathlib import Path

import pytest
from sqlalchemy import event

from tests.services.test_session_terminal_outcome_v2 import _seed

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("tool_name", ["run_command", "execute_code", "grep_search"])
@pytest.mark.parametrize("file_count", [10, 100])
async def test_workspace_input_receipts_are_loaded_in_one_query(
    owner_sessionmaker, owner_engine, monkeypatch, tool_name, file_count,
):
    from app import database
    from app.models.runtime_task import RuntimeTask
    from app.services.content_input_receipts import authorize_workspace_execution_inputs
    from app.services.workspace_resource_authority import WorkspaceAuthorityScope
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

    tenant, agent, session, run, _ = await _seed(owner_sessionmaker)
    async with owner_sessionmaker() as db:
        user = (await db.get(RuntimeTask, run)).root_user_id
    paths = frozenset(f"workspace/file-{i}.md" for i in range(file_count))
    controlled = paths - {"workspace/file-0.md"}
    scope = WorkspaceAuthorityScope(agent_id=agent, user_id=user, root_session_id=session,
        allowed_paths=paths, full_control_paths=controlled, operator_view=False, authority_source="owner")
    request = ToolExecutionRequest(tool_name=tool_name, arguments={}, context=ToolExecutionContext(
        agent_id=agent, user_id=user, tenant_id=str(tenant), workspace=Path("/unused"),
        session_id=str(session), runtime_task_id=str(run), workspace_authority_scope=scope))
    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    lookups = []

    def observe(_conn, _cursor, statement, _params, _context, _many):
        if "FROM content_input_receipts" in statement:
            lookups.append(statement)

    event.listen(owner_engine.sync_engine, "before_cursor_execute", observe)
    try:
        assert await authorize_workspace_execution_inputs(request) is None
    finally:
        event.remove(owner_engine.sync_engine, "before_cursor_execute", observe)
    assert {read.path for read in request.context.workspace_authority_scope.input_reads} == controlled
    assert all(read.receipt_id is None for read in request.context.workspace_authority_scope.input_reads)
    assert len(lookups) == 1
