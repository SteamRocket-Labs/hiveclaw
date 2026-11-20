"""Shared execution inputs require live authorization for their source chains."""

import uuid

import pytest

from app.core.resource_authority import workspace_resource_id
from app.database import tenant_scoped_session
from app.models.chat_session import ChatSession
from app.models.security_audit import ResourcePermission
from app.models.user import User
from app.services import content_input_receipts as sources
from app.services.workspace_resource_authority import load_workspace_authority_scope
from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest
from tests.integration.test_a2w_source_graph_reads_postgres import _measure
from tests.integration.test_artifact_publication_transactions import stage_report
from tests.integration.test_m1g24_workspace_artifact_authority_postgres import native_workspace  # noqa: F401

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


@pytest.mark.parametrize("withdraw_source", [False, True], ids=["unchanged", "withdrawn"])
async def test_shared_execution_inputs_require_live_sources(
    native_workspace, owner_sessionmaker, app_user_sessionmaker, monkeypatch, withdraw_source,  # noqa: F811
):
    n = native_workspace
    foreign_session = uuid.uuid4()
    paths = [f"workspace/copy-{name}.md" for name in "abcd"]
    x_path = "workspace/source-x.md"
    y_path = "workspace/source-y.md"
    y_grant = uuid.uuid4()
    async with owner_sessionmaker() as db:
        db.add(ChatSession(id=foreign_session, tenant_id=n.tenant, agent_id=n.agent, user_id=n.foreign))
        await db.flush()

        for path in [x_path, y_path, *paths]:
            db.add(ResourcePermission(
                id=y_grant if path == y_path else uuid.uuid4(),
                tenant_id=n.tenant, principal_type="user", principal_id=n.user,
                resource_type="workspace_file", resource_id=workspace_resource_id(n.agent, path),
                actions=["read"], conditions={},
            ))

        async def stage(path, parents=()):
            receipt = await stage_report(db, tenant=n.tenant, agent=n.agent, user=n.foreign,
                session=foreign_session, root=n.root, path=path, dependencies=parents)
            return {"source_kind": "workspace_artifact", "source_id": str(receipt.id),
                "source_version": receipt.content_sha256}

        y = await stage(y_path)
        x = await stage(x_path, [y])
        for path, parents in zip(paths, ([y], [x], [x], []), strict=True):
            await stage(path, parents)
        await db.commit()

    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        user = await db.get(User, n.user)
        scope = await load_workspace_authority_scope(
            db, user, agent_id=n.agent, session_id=n.session, manifest_paths=set(paths),
        )
    assert scope.allowed_paths == frozenset(paths)
    assert not scope.owner_paths

    context = ToolExecutionContext(
        agent_id=n.agent, user_id=n.user, tenant_id=str(n.tenant), session_id=str(n.session),
        runtime_task_id=str(n.run), workspace=n.root / "workspace", workspace_authority_scope=scope,
    )
    request = ToolExecutionRequest("execute_code", {"language": "python", "code": "pass"}, context)
    original = sources._validate_workspace_live
    withdrawn = False

    async def validate(*args, **kwargs):
        nonlocal withdrawn
        reader = await original(*args, **kwargs)
        if kwargs["path"] == y_path and not kwargs.get("after") and not withdrawn:
            async with owner_sessionmaker() as writer:
                await writer.delete(await writer.get(ResourcePermission, y_grant))
                await writer.commit()
            withdrawn = True
        return reader

    if withdraw_source:
        monkeypatch.setattr(sources, "_validate_workspace_live", validate)
        denial = await sources.authorize_workspace_execution_inputs(request)
        assert withdrawn
    else:
        denial, _ = await _measure("px1_shared_execution_unchanged",
            lambda: sources.authorize_workspace_execution_inputs(request), monkeypatch, delay=0)

    expected = paths[3:] if withdraw_source else paths
    assert denial is None
    assert context.workspace_authority_scope.allowed_paths == frozenset(expected)
    assert [read.path for read in context.workspace_authority_scope.input_reads] == expected
