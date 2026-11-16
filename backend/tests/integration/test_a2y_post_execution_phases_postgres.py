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


@pytest.mark.parametrize("files,versions", [(2, 1), (57, 1), (57, 9)])
async def test_post_tool_statement_cost_is_independent_of_workspace_history(
    native_workspace, app_user_sessionmaker, post_phases, files, versions,  # noqa: F811
):
    n = native_workspace
    await seed_workspace(n, app_user_sessionmaker, files, versions)
    rows = []
    for index, (name, arguments) in enumerate([
        ("write_file", {"path": "workspace/new.md", "content": "New report\n"}),
        ("edit_file", {"path": "workspace/report.md", "old_text": "Revenue: 42", "new_text": "Revenue: 43"}),
    ], start=1):
        row = await measured_call(n, app_user_sessionmaker, post_phases, name, arguments, index)
        post = {name: value for name, value in row["phases"].items() if name.startswith("post.")}
        row.update(files=files, versions=versions, post_statements=sum(p["statements"] for p in post.values()),
            post_seconds=sum(p["seconds"] for p in post.values()))
        rows.append(row)
        print("A2Y_POST", json.dumps(row), flush=True)
    if os.environ.get("FULL_RUN"):
        (Path(os.environ["FULL_RUN"]) / "evidence" / f"{sys.argv[1]}-post-{files}-{versions}.json").write_text(
            json.dumps(rows, indent=2))
    assert {row["tool"]: row["post_statements"] for row in rows} == {"write_file": 37, "edit_file": 37}
    assert all(row["post_statements"] <= 80 and row["post_seconds"] <= 3 for row in rows)


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


async def test_publication_calls_canonical_permission_once_per_agent_checkpoint(
    canonical_publication, owner_sessionmaker, app_user_sessionmaker, monkeypatch,
):
    from app.core import permissions
    from app.models.security_audit import ResourcePermission
    from tests.integration.test_artifact_publication_transactions import publish_report

    fixture = canonical_publication
    n, other, grant_id, path = fixture.native, fixture.other, fixture.grant_id, fixture.path
    original = permissions.check_permission
    calls = []

    async def observe(db, **kwargs):
        if kwargs["resource_id"] == other:
            calls.append(kwargs["action"])
        return await original(db, **kwargs)

    monkeypatch.setattr(permissions, "check_permission", observe)
    publication = SimpleNamespace(tenant=n.tenant, agent=n.agent, users=[n.user], sessions=[n.session],
        root=n.root, paths=[path])
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        parts, errors = await publish_report(db, publication, 0)
        assert len(parts) == 1 and errors == []
        await db.commit()
    # The before and after checkpoints each enter the canonical live decision;
    # the other 56 source nodes reuse only that same checkpoint's observation.
    assert calls == ["manage", "manage"]
    async with owner_sessionmaker() as db:
        permission = await db.get(ResourcePermission, grant_id)
        permission.effect = "deny"
        await db.commit()
    async with tenant_scoped_session(n.tenant, session_factory=app_user_sessionmaker) as db:
        parts, errors = await publish_report(db, publication, 0)
        assert parts == [] and errors[0]["status"] == "denied"
        assert len(calls) > 2


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
