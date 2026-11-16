"""Post-tool publication costs and canonical live permission decisions."""

from contextvars import ContextVar
from functools import wraps
import inspect
import json
import os
from pathlib import Path
import sys
from types import SimpleNamespace
import uuid

import pytest

from app.database import tenant_scoped_session
from tests.integration.test_a2y_tool_effect_phases_postgres import (
    _active, measured_call, native_workspace, phases, seed_workspace,  # noqa: F401
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")
_stage = ContextVar("a2y_post_stage", default="tool")
POST_STATEMENT_CEILING = 38


@pytest.fixture
def post_phases(phases, monkeypatch):  # noqa: F811
    from app.services import chat_artifact_delivery as artifacts, session_tool_runtime as runtime
    from app.services import content_input_receipts as receipts, web_chat_runtime as web
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.models.session_v2 import SessionEventOutbox

    def instrument(module, symbol, name, *, aliases=(), post=False):
        original = getattr(module, symbol)
        if inspect.iscoroutinefunction(original):
            @wraps(original)
            async def wrapped(*args, **kwargs):
                clock = _active.get()
                if clock is None:
                    return await original(*args, **kwargs)
                token = _stage.set("post") if post else None
                try:
                    with clock.phase(f"{_stage.get()}.{name}"):
                        return await original(*args, **kwargs)
                finally:
                    if token is not None:
                        _stage.reset(token)
        else:
            @wraps(original)
            def wrapped(*args, **kwargs):
                clock = _active.get()
                if clock is None:
                    return original(*args, **kwargs)
                with clock.phase(f"{_stage.get()}.{name}"):
                    return original(*args, **kwargs)
        monkeypatch.setattr(module, symbol, wrapped)
        for alias in aliases:
            if getattr(alias, symbol, None) is original:
                monkeypatch.setattr(alias, symbol, wrapped)

    instrument(web, "_persist_tool_call", "settlement_transaction", post=True)
    instrument(runtime, "complete_tool_invocation", "complete_tool_invocation")
    instrument(artifacts, "create_chat_artifacts_for_message", "artifact_publication", aliases=(runtime,))
    instrument(artifacts, "build_artifact_candidate", "snapshot_blob")
    instrument(receipts, "validate_published_workspace_receipt", "publication_sources")
    instrument(runtime, "append_session_events", "result_events")
    original_flush = AsyncSession.flush

    async def flush(db, *args, **kwargs):
        clock = _active.get()
        if clock is not None and _stage.get() == "post" and any(
            isinstance(row, SessionEventOutbox) for row in db.new
        ):
            with clock.phase("post.outbox_write"):
                return await original_flush(db, *args, **kwargs)
        return await original_flush(db, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "flush", flush)
    return phases


@pytest.fixture(scope="module")
def post_statement_counts():
    return []


@pytest.mark.parametrize("files,versions", [(2, 1), (57, 1), (57, 9)])
@pytest.mark.parametrize("history_size", [3, 30])
@pytest.mark.parametrize("tool", ["write_file", "edit_file"])
async def test_post_tool_statement_cost_is_independent_of_workspace_history(
    native_workspace, app_user_sessionmaker, post_phases, post_statement_counts, monkeypatch,
    files, versions, history_size, tool,  # noqa: F811
):
    from sqlalchemy import select
    from app.models.organization_identity import ContentInputReceipt
    from app.models.session_v2 import SessionModelResult, SessionToolInvocation
    from app.services.llm_client import LLMMessage
    from app.services.session_model_inputs import _model_dependencies
    from app.services.session_semantic_history import load_session_semantic_history
    from tests.integration import test_a2y_tool_effect_phases_postgres as effect_phases
    from tests.integration.test_m4c_explicit_memory_scope_postgres import memory_call

    n = native_workspace
    await seed_workspace(n, app_user_sessionmaker, files, versions)
    prior_invocations = set()
    for _ in range(history_size):
        content, invocation = await memory_call(n, app_user_sessionmaker, "read_file",
            {"path": "workspace/input-01.md"}, command="Read the authorized reporting input")
        assert "Revenue: 42" in content
        prior_invocations.add(str(invocation.id))
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        history = await load_session_semantic_history(db, tenant_id=n.tenant, agent_id=n.agent,
            session_id=n.session)
    prior = [LLMMessage(role=m.role, content=m.content, tool_calls=m.tool_calls,
        tool_call_id=m.tool_call_id, source_ref=m.id) for m in history.messages]
    assert sum(message.role == "tool" for message in prior) == history_size
    admitted = effect_phases._admitted_write_messages

    async def with_history(db, namespace, **kwargs):
        return [*prior, *await admitted(db, namespace, **kwargs)]

    monkeypatch.setattr(effect_phases, "_admitted_write_messages", with_history)
    arguments = ({"path": "workspace/new.md", "content": "New report\n"} if tool == "write_file" else
        {"path": "workspace/report.md", "old_text": "Revenue: 42", "new_text": "Revenue: 43"})
    row = await measured_call(n, app_user_sessionmaker, post_phases, tool, arguments, history_size + 1)
    post = {name: value for name, value in row["phases"].items() if name.startswith("post.")}
    row.update(files=files, versions=versions, history_size=history_size,
        post_statements=sum(p["statements"] for p in post.values()),
        post_seconds=sum(p["seconds"] for p in post.values()))
    print("A2Y_POST", json.dumps(row), flush=True)
    if os.environ.get("FULL_RUN"):
        (Path(os.environ["FULL_RUN"]) / "evidence" /
            f"{sys.argv[1]}-post-{files}-{versions}-{history_size}-{tool}.json").write_text(json.dumps(row, indent=2))
    # Check the measured model really consumed every settled tool result, and
    # that the executor's receipt inherited them rather than omitting history.
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        invocation = await db.get(SessionToolInvocation, uuid.UUID(row["invocation_id"]))
        model = await db.scalar(select(SessionModelResult).where(SessionModelResult.tenant_id == n.tenant,
            SessionModelResult.provider_request_id == invocation.provider_request_id))
        dependencies, complete = _model_dependencies(model)
        assert complete
        assert {d["source_id"] for d in dependencies if d["source_kind"] == "session_tool_invocation"} == prior_invocations
        receipt = await db.get(ContentInputReceipt, uuid.uuid5(invocation.id, f"workspace-content:{arguments['path']}"))
        assert receipt is not None
        assert prior_invocations <= {d["source_id"] for d in receipt.dependencies_json
            if d["source_kind"] == "session_tool_invocation"}
    post_statement_counts.append(row["post_statements"])
    assert len(set(post_statement_counts)) == 1, post_statement_counts
    assert row["post_statements"] <= POST_STATEMENT_CEILING <= 80, row
    assert row["post_seconds"] <= 3, row


@pytest.fixture
async def canonical_publication(native_workspace, owner_sessionmaker):  # noqa: F811
    from app.models.agent import Agent
    from app.models.security_audit import ResourcePermission
    from tests.integration.test_artifact_publication_transactions import stage_report

    n = native_workspace
    other = uuid.uuid4()
    grant_id = uuid.uuid4()
    path = "workspace/canonical-permissions.md"
    async with owner_sessionmaker() as db:
        db.add(Agent(id=other, tenant_id=n.tenant, creator_id=n.foreign, name="Source employee"))
        await db.flush()
        dependencies = []
        for index in range(57):
            receipt = await stage_report(db, tenant=n.tenant, agent=other, user=n.user, session=None,
                root=n.root.parent / str(other), path=f"workspace/source-{index}.md")
            dependencies.append(dict(source_kind="workspace_artifact", source_id=str(receipt.id),
                source_version=receipt.content_sha256))
        await stage_report(db, tenant=n.tenant, agent=n.agent, user=n.user, session=n.session,
            root=n.root, path=path, dependencies=dependencies)
        db.add(ResourcePermission(id=grant_id, tenant_id=n.tenant, principal_type="user", principal_id=n.user,
            resource_type="agent", resource_id=other, actions=["manage"], effect="allow", conditions={}))
        await db.commit()
    return SimpleNamespace(native=n, other=other, grant_id=grant_id, path=path)


@pytest.mark.parametrize("revoke_between", [False, True])
async def test_publication_checks_canonical_permission_before_and_after(
    canonical_publication, owner_sessionmaker, app_user_sessionmaker, monkeypatch, revoke_between,
):
    from datetime import datetime, timezone
    from app.core import permissions
    from app.models.security_audit import ResourcePermission
    from app.services import content_input_receipts as receipts
    from tests.integration.test_artifact_publication_transactions import publish_report

    fixture = canonical_publication
    n, other, grant_id, path = fixture.native, fixture.other, fixture.grant_id, fixture.path
    original = permissions.check_permission
    checkpoint_rows = receipts._workspace_checkpoint_rows
    calls = []
    revoked = False

    def is_after():
        phase = receipts._DISCLOSURE_SOURCE_VALIDATIONS.get().checkpoint[1]
        return isinstance(phase, tuple) and phase[1] == "workspace_after"

    async def read_checkpoint(*args, **kwargs):
        nonlocal revoked
        if revoke_between and is_after() and not revoked:
            assert any(not after and allowed for after, action, allowed in calls if action == "manage")
            async with owner_sessionmaker() as writer:
                (await writer.get(ResourcePermission, grant_id)).revoked_at = datetime.now(timezone.utc)
                await writer.commit()
            revoked = True
        return await checkpoint_rows(*args, **kwargs)

    async def observe(db, **kwargs):
        allowed = await original(db, **kwargs)
        if kwargs["resource_id"] == other:
            calls.append((is_after(), kwargs["action"], allowed))
        return allowed

    monkeypatch.setattr(permissions, "check_permission", observe)
    monkeypatch.setattr(receipts, "_workspace_checkpoint_rows", read_checkpoint)
    publication = SimpleNamespace(tenant=n.tenant, agent=n.agent, users=[n.user], sessions=[n.session],
        root=n.root, paths=[path])
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        parts, errors = await publish_report(db, publication, 0)
        if revoke_between:
            assert revoked and parts == [] and errors[0]["status"] == "denied"
        else:
            assert len(parts) == 1 and errors == []
        await db.commit()
    assert any(not after and allowed for after, action, allowed in calls if action == "manage")
    assert any(after and allowed == (not revoke_between) for after, action, allowed in calls if action == "manage")


async def test_canonical_sql_failure_cannot_fall_through_to_a_loaded_execute_grant(
    canonical_publication, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from sqlalchemy import text
    from app.core import permissions
    from app.models.security_audit import ResourcePermission
    from tests.integration.test_artifact_publication_transactions import stage_report, publish_report

    fixture = canonical_publication
    n = fixture.native
    independent = "workspace/independent-card.md"
    async with owner_sessionmaker() as db:
        permission = await db.get(ResourcePermission, fixture.grant_id)
        permission.actions = ["execute", "read"]
        await stage_report(db, tenant=n.tenant, agent=n.agent, user=n.user, session=n.session,
            root=n.root, path=independent)
        await db.commit()
    original = permissions.check_permission
    injected = False

    async def fail_manage(db, **kwargs):
        nonlocal injected
        if kwargs["resource_id"] == fixture.other and kwargs["action"] == "manage" and not injected:
            injected = True
            await db.execute(text("SELECT 1 / 0"))
        return await original(db, **kwargs)

    monkeypatch.setattr(permissions, "check_permission", fail_manage)
    publication = SimpleNamespace(tenant=n.tenant, agent=n.agent, users=[n.user], sessions=[n.session],
        root=n.root, paths=[fixture.path, independent])
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        parts, errors = await publish_report(db, publication, 0, paths=publication.paths)
        assert injected
        assert [part["path"] for part in parts] == [independent]
        assert [(error["path"], error["status"]) for error in errors] == [(fixture.path, "denied")]
        assert await db.scalar(text("SELECT 42")) == 42
        await db.commit()
