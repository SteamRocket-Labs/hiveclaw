"""B4 functional red→green: workflow leaf authenticated-requester restoration.

Production incident (e11a95d7): every governed leaf of a worker-claimed /
daemon-resumed workflow run failed ``RuntimeTenantPreconditionError:
tool_requester_not_found`` because ``build_resumable_workflow_leaf_executor``
built the spawn context with ``parent_user_id=agent.id`` — an id that is not a
``users.id`` — while the durable RuntimeTask already carried the real
authenticated requester in ``root_user_id``.

These tests exercise the REAL resume executor against real PostgreSQL and push
the restored context through the actual tool-boundary resolver
(``ToolRuntimeResolver.resolve`` → tenant chokepoint → workspace authority),
which is exactly the boundary that failed in production. The injected spawn
only replaces the model provider; the authority path is untouched.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.agents.subagent import SubagentHandle, SubagentResult
from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.llm import LLMModel
from app.models.runtime_task import RuntimeTask
from app.models.tenant import Tenant
from app.models.user import User
from app.runtime.workflow_engine import LeafRequest
from app.services.workflow_launch import (
    build_resumable_workflow_leaf_executor,
    resolve_agent_runtime,
    start_ephemeral_workflow_for_agent,
)

pytestmark = pytest.mark.usefixtures("migrated_pg_url")


def _definition() -> dict:
    return {
        "name": "one-step",
        "args_schema": {"target": {"type": "string", "required": True}},
        "default_budget": {"max_total_tokens": 200_000},
        "steps": [
            {
                "id": "work",
                "type": "agent_step",
                "leaf": {"name": "worker", "type": "worker"},
                "task": "Work on {{args.target}}",
            }
        ],
    }


@pytest.fixture()
async def tenant_id(owner_sessionmaker) -> uuid.UUID:
    tid = uuid.uuid4()
    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tid, name="wf-launch", slug=f"wl-{tid.hex[:10]}"))
    return tid


@pytest.fixture()
async def world(owner_sessionmaker, tenant_id):
    """Real FK-backed actors: owner User, LLMModel, Agent, parent ChatSession."""

    user_id = uuid.uuid4()
    model_id = uuid.uuid4()
    agent_id = uuid.uuid4()
    session_id = uuid.uuid4()
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            User(
                id=user_id,
                username=f"wfl-{user_id.hex[:10]}",
                email=f"wfl-{user_id.hex[:10]}@test.local",
                password_hash="x",
                display_name="Workflow Launch Owner",
                tenant_id=tenant_id,
                role="org_admin",
            )
        )
        await session.flush()
        session.add(
            LLMModel(
                id=model_id,
                tenant_id=tenant_id,
                provider="test",
                model="test-model",
                api_key_encrypted="x",
                label="test-model",
                enabled=True,
            )
        )
        await session.flush()
        session.add(
            Agent(
                id=agent_id,
                tenant_id=tenant_id,
                name="launch-agent",
                role_description="workflow launch actor",
                creator_id=user_id,
                owner_user_id=user_id,
                primary_model_id=model_id,
                status="idle",
            )
        )
        await session.flush()
        session.add(
            ChatSession(
                id=session_id,
                agent_id=agent_id,
                tenant_id=tenant_id,
                user_id=user_id,
                title="workflow parent",
                source_channel="web",
            )
        )
    return SimpleNamespace(user_id=user_id, model_id=model_id, agent_id=agent_id, session_id=session_id)


async def test_headless_runtime_resolves_tenant_under_enforced_rls(app_user_sessionmaker, tenant_id, world):
    from app.database import reset_current_tenant, set_current_tenant

    token = set_current_tenant(None)
    try:
        agent, model = await resolve_agent_runtime(world.agent_id, session_factory=app_user_sessionmaker)
        assert (agent.id, agent.tenant_id, model.id) == (world.agent_id, tenant_id, world.model_id)
        with pytest.raises(LookupError, match="not found"):
            await resolve_agent_runtime(world.agent_id, tenant_id=uuid.uuid4(), session_factory=app_user_sessionmaker)
        foreign_context = set_current_tenant(str(uuid.uuid4()))
        try:
            with pytest.raises(LookupError, match="not found"):
                await resolve_agent_runtime(world.agent_id, session_factory=app_user_sessionmaker)
        finally:
            reset_current_tenant(foreign_context)
    finally:
        reset_current_tenant(token)


async def _insert_workflow_task(
    owner_sessionmaker,
    *,
    tenant_id,
    world,
    root_user_id,
    metadata_extra: dict | None = None,
) -> uuid.UUID:
    run_id = uuid.uuid4()
    metadata = {
        "tenant_id": str(tenant_id),
        "definition_source": "ephemeral",
        "parent_session_id": str(world.session_id),
        "root_session_id": str(world.session_id),
        "user_id": str(root_user_id) if root_user_id else None,
    }
    metadata.update(metadata_extra or {})
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            RuntimeTask(
                id=run_id,
                task_type="workflow",
                tenant_id=tenant_id,
                status="running",
                parent_agent_id=world.agent_id,
                parent_session_id=str(world.session_id),
                child_session_id=str(world.session_id),
                root_user_id=root_user_id,
                root_session_id=str(world.session_id),
                prompt="resume me",
                metadata_json=metadata,
            )
        )
    return run_id


def _patch_tool_boundary_sessions(monkeypatch, owner_sessionmaker):
    """Point the real tool-runtime resolver's module-level sessions at the
    integration container — no behavior change, only the engine under test."""

    from app.services import tenant_resolver
    from app.tools import resolver as tool_resolver

    real_resolve_tenant = tenant_resolver.resolve_tenant_for_agent

    async def scoped_resolve_tenant(agent_id, *, session_factory=None):
        return await real_resolve_tenant(agent_id, session_factory=owner_sessionmaker)

    monkeypatch.setattr(tool_resolver, "async_session", owner_sessionmaker)
    monkeypatch.setattr(tool_resolver, "resolve_tenant_for_agent", scoped_resolve_tenant)


def _probe_spawn(captured: dict, *, expect_user_id=None, expect_session_id=None):
    """Replacement for the model provider side of spawn: push the restored
    spawn context through the REAL governed tool boundary."""

    async def spawn(ctx, spec, task, *, budget=None):
        from app.tools.resolver import ToolRuntimeResolver

        context = await ToolRuntimeResolver().resolve(
            agent_id=ctx.parent_agent_id,
            user_id=ctx.parent_user_id,
            session_id=ctx.parent_session_id,
        )
        captured["ctx_user_id"] = ctx.parent_user_id
        captured["ctx_session_id"] = ctx.parent_session_id
        captured["resolved_tenant_id"] = str(context.tenant_id)
        if expect_user_id is not None:
            assert ctx.parent_user_id == expect_user_id
        if expect_session_id is not None:
            assert ctx.parent_session_id == expect_session_id
        return SubagentHandle(
            name=spec.name,
            trace_id="tr-test",
            depth=1,
            result=SubagentResult(name=spec.name, type=spec.type, status="completed", content="done", tokens_used=3),
        )

    return spawn


def _leaf_request(run_id, tenant_id) -> LeafRequest:
    return LeafRequest(
        run_id=str(run_id),
        step_id="work",
        leaf=SimpleNamespace(name="worker", type="worker", max_tool_rounds=4),
        task="resume work",
        tenant_id=str(tenant_id),
    )


async def test_resume_leaf_restores_requester_and_passes_real_tool_boundary(
    owner_sessionmaker, tenant_id, world, monkeypatch
):
    run_id = await _insert_workflow_task(
        owner_sessionmaker, tenant_id=tenant_id, world=world, root_user_id=world.user_id
    )
    _patch_tool_boundary_sessions(monkeypatch, owner_sessionmaker)

    captured: dict = {}
    executor = build_resumable_workflow_leaf_executor(
        session_factory=owner_sessionmaker,
        spawn=_probe_spawn(captured, expect_user_id=world.user_id, expect_session_id=str(world.session_id)),
    )

    outcome = await executor(_leaf_request(run_id, tenant_id))

    assert outcome.ok, outcome.error
    # The requester is the durable root principal — never the Agent id.
    assert captured["ctx_user_id"] == world.user_id != world.agent_id
    assert captured["ctx_session_id"] == str(world.session_id)
    assert captured["resolved_tenant_id"] == str(tenant_id)


async def test_fresh_start_leaf_executes_authorized_tools_with_authenticated_user(
    owner_sessionmaker, tenant_id, world, monkeypatch
):
    _patch_tool_boundary_sessions(monkeypatch, owner_sessionmaker)

    captured: dict = {}
    handle = await start_ephemeral_workflow_for_agent(
        agent_id=world.agent_id,
        definition=_definition(),
        args={"target": "x"},
        user_id=world.user_id,
        parent_session_id=world.session_id,
        root_session_id=world.session_id,
        session_factory=owner_sessionmaker,
        spawn=_probe_spawn(captured, expect_user_id=world.user_id),
    )

    assert handle.outcome.status == "completed", handle.outcome
    assert captured["ctx_user_id"] == world.user_id != world.agent_id


async def test_fresh_start_headless_falls_back_to_agent_owner(owner_sessionmaker, tenant_id, world, monkeypatch):
    """user_id absent and no parent session: `_ensure_run_session` binds a new
    headless session owned by the agent owner and stamps the task; the leaf
    must consume that persisted session/requester, not a parallel fallback."""

    _patch_tool_boundary_sessions(monkeypatch, owner_sessionmaker)

    captured: dict = {}
    handle = await start_ephemeral_workflow_for_agent(
        agent_id=world.agent_id,
        definition=_definition(),
        args={"target": "x"},
        session_factory=owner_sessionmaker,
        spawn=_probe_spawn(captured, expect_user_id=world.user_id),
    )

    assert handle.outcome.status == "completed", handle.outcome
    assert captured["ctx_user_id"] == world.user_id
    # The headless session created by the service reached the leaf executor.
    assert captured["ctx_session_id"]
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()
        assert captured["ctx_session_id"] == task.parent_session_id
        assert task.root_user_id == world.user_id


async def test_fresh_start_parent_session_user_differs_from_agent_owner(
    owner_sessionmaker, tenant_id, world, monkeypatch
):
    """user_id absent + existing parent session whose user ≠ agent owner: the
    canonical root restored by `_ensure_run_session` is the SESSION user; the
    live leaf must act as exactly that principal, never the owner."""

    other_user_id = uuid.uuid4()
    other_session_id = uuid.uuid4()
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            User(
                id=other_user_id,
                username=f"wfl-{other_user_id.hex[:10]}",
                email=f"wfl-{other_user_id.hex[:10]}@test.local",
                password_hash="x",
                display_name="Session Principal",
                tenant_id=tenant_id,
                role="org_admin",
            )
        )
        await session.flush()
        session.add(
            ChatSession(
                id=other_session_id,
                agent_id=world.agent_id,
                tenant_id=tenant_id,
                user_id=other_user_id,
                title="other principal session",
                source_channel="web",
            )
        )

    _patch_tool_boundary_sessions(monkeypatch, owner_sessionmaker)

    captured: dict = {}
    handle = await start_ephemeral_workflow_for_agent(
        agent_id=world.agent_id,
        definition=_definition(),
        args={"target": "x"},
        parent_session_id=other_session_id,
        root_session_id=other_session_id,
        session_factory=owner_sessionmaker,
        spawn=_probe_spawn(captured, expect_user_id=other_user_id, expect_session_id=str(other_session_id)),
    )

    assert handle.outcome.status == "completed", handle.outcome
    assert captured["ctx_user_id"] == other_user_id != world.user_id
    assert captured["ctx_session_id"] == str(other_session_id)
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == handle.run_id))).scalar_one()
        assert task.root_user_id == other_user_id


async def test_fresh_start_with_unresolvable_requester_fails_typed(owner_sessionmaker, tenant_id, world):
    """agents.creator_id is NOT NULL, so a durable Agent always carries an
    owner candidate; the reachable fresh-start failure is a requester that no
    longer resolves as a User — typed, before any leaf runs."""

    from app.services.runtime_task_authority import RuntimeTaskRequesterUnavailable

    with pytest.raises(RuntimeTaskRequesterUnavailable) as exc_info:
        await start_ephemeral_workflow_for_agent(
            agent_id=world.agent_id,
            definition=_definition(),
            args={"target": "x"},
            user_id=uuid.uuid4(),  # authenticated-looking id with no User row
            session_factory=owner_sessionmaker,
        )
    assert exc_info.value.reason_code == "workflow_requester_user_not_found"


async def test_resume_leaf_missing_requester_fails_typed_without_substitution(owner_sessionmaker, tenant_id, world):
    run_id = await _insert_workflow_task(owner_sessionmaker, tenant_id=tenant_id, world=world, root_user_id=None)

    async def spawn(_ctx, _spec, _task, *, budget=None):
        raise AssertionError("spawn must not run when durable requester authority is missing")

    executor = build_resumable_workflow_leaf_executor(session_factory=owner_sessionmaker, spawn=spawn)
    outcome = await executor(_leaf_request(run_id, tenant_id))

    assert not outcome.ok
    assert "root_user_id_missing" in (outcome.error or "")
    assert "agent" not in (outcome.error or "").replace("parent agent", "")


async def test_resume_leaf_metadata_requester_mismatch_fails_typed(owner_sessionmaker, tenant_id, world):
    run_id = await _insert_workflow_task(
        owner_sessionmaker,
        tenant_id=tenant_id,
        world=world,
        root_user_id=world.user_id,
        metadata_extra={"root_user_id": str(uuid.uuid4())},
    )

    async def spawn(_ctx, _spec, _task, *, budget=None):
        raise AssertionError("spawn must not run when requester evidence mismatches")

    executor = build_resumable_workflow_leaf_executor(session_factory=owner_sessionmaker, spawn=spawn)
    outcome = await executor(_leaf_request(run_id, tenant_id))

    assert not outcome.ok
    assert "root_user_id_mismatch" in (outcome.error or "")


async def test_resume_leaf_unresolvable_requester_user_fails_typed(owner_sessionmaker, tenant_id, world):
    run_id = await _insert_workflow_task(
        owner_sessionmaker, tenant_id=tenant_id, world=world, root_user_id=uuid.uuid4()
    )

    async def spawn(_ctx, _spec, _task, *, budget=None):
        raise AssertionError("spawn must not run when the requester no longer resolves")

    executor = build_resumable_workflow_leaf_executor(session_factory=owner_sessionmaker, spawn=spawn)
    outcome = await executor(_leaf_request(run_id, tenant_id))

    assert not outcome.ok
    assert "no longer resolves" in (outcome.error or "")


async def test_workflow_native_spawn_preserves_run_session_and_permission_authority(
    owner_sessionmaker, tenant_id, world, monkeypatch
):
    from app.agents import subagent
    from app.runtime.invoker import _permission_profile_from_session_context, _tool_frame_kwargs_from_session_context
    from app.runtime.recovery_manifest_store import resolve_recovery_authority
    from app.tools.resolver import ToolRuntimeResolver

    run_id = await _insert_workflow_task(
        owner_sessionmaker, tenant_id=tenant_id, world=world, root_user_id=world.user_id
    )
    profile = {
        "mode": "default",
        "allowed_tools": ["read_file"],
        "writable_roots": ["workspace/b4"],
        "readable_roots": ["workspace/b4"],
        "capability_policy_snapshot": {"session_exact_scope": True},
    }
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as db:
        parent = await db.get(ChatSession, world.session_id)
        parent.transcript_metadata_json = {"permission_profile": profile}
    _patch_tool_boundary_sessions(monkeypatch, owner_sessionmaker)
    seen = []
    from app import database
    from app.tools import workspace

    monkeypatch.setattr(database, "async_session", owner_sessionmaker)
    monkeypatch.setattr(workspace, "async_session", owner_sessionmaker)

    async def invoke(request):
        # Exercise native spawn and the same downstream resolvers as the live
        # kernel. The provider is replaced; no live sandbox claim is made here.
        assert "execute_code" in request.allowed_tool_names
        assert request.session_context.session_id == str(world.session_id)
        frame = _tool_frame_kwargs_from_session_context(request.session_context)
        assert frame["runtime_task_id"] == str(run_id)
        permission = _permission_profile_from_session_context(request.session_context)
        assert permission.capability_policy_snapshot["session_exact_scope"] is True
        assert list(permission.allowed_tools) == ["read_file"]
        runtime = await ToolRuntimeResolver().resolve(
            agent_id=request.agent_id,
            user_id=request.user_id,
            session_id=request.session_context.session_id,
            runtime_task_id=frame["runtime_task_id"],
            permission_profile=permission,
        )
        assert runtime.runtime_task_id == str(run_id)
        authority = resolve_recovery_authority(request, SimpleNamespace(tenant_id=tenant_id))
        assert authority.status == "bound"
        from app.services import agent_tools

        denied = await agent_tools._get_tool_runtime_service().execute(
            "execute_code",
            {"language": "python", "code": "print(31 + 49)"},
            agent_id=request.agent_id,
            user_id=request.user_id,
            session_id=request.session_context.session_id,
            runtime_task_id=frame["runtime_task_id"],
            permission_profile=permission,
        )
        assert "exact_session_tool_scope_denied" in str(denied)
        seen.append(request.session_context.metadata["turn_id"])
        return SimpleNamespace(content="native context bound", tokens_used=1)

    async def spawn(ctx, spec, task, *, budget=None):
        return await subagent.spawn_subagent(ctx, spec, task, budget=budget, invoke=invoke)

    executor = build_resumable_workflow_leaf_executor(session_factory=owner_sessionmaker, spawn=spawn)
    for leaf_id in ("item-0", "item-1"):
        request = _leaf_request(run_id, tenant_id)
        request.leaf_id = leaf_id
        outcome = await executor(request)
        assert outcome.ok, outcome.error
    assert len(set(seen)) == 2


async def test_workflow_leaf_missing_bound_session_fails_before_spawn(owner_sessionmaker, tenant_id, world):
    run_id = await _insert_workflow_task(
        owner_sessionmaker, tenant_id=tenant_id, world=world, root_user_id=world.user_id
    )
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as db:
        task = await db.get(RuntimeTask, run_id)
        task.child_session_id = str(uuid.uuid4())

    async def spawn(*args, **kwargs):
        raise AssertionError("spawn must not run without its bound session")

    executor = build_resumable_workflow_leaf_executor(session_factory=owner_sessionmaker, spawn=spawn)
    outcome = await executor(_leaf_request(run_id, tenant_id))
    assert not outcome.ok
    assert "workflow_session_not_found" in outcome.error
