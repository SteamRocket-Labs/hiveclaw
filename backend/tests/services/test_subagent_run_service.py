"""Tests for durable background-subagent run records (Step 8)."""

from __future__ import annotations

import json
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.agents.subagent import SUBAGENT_TYPE_WORKER, SubagentResult
from app.database import tenant_scoped_session
from app.models.chat_session import ChatSession
from app.models.runtime_task import RuntimeTask
from app.services import subagent_run_service as svc


@pytest.mark.asyncio
async def test_start_subagent_run_records_running_subagent_task(monkeypatch):
    captured: dict = {}

    async def _fake_create(**kwargs):
        captured.update(kwargs)
        return kwargs["task_id"]

    monkeypatch.setattr(svc, "create_runtime_task_record", _fake_create)
    parent = uuid.uuid4()
    started = await svc.start_subagent_run(
        parent_agent_id=parent, spec_name="scout", spec_type=SUBAGENT_TYPE_WORKER, task="do x"
    )
    assert started.run_id == captured["task_id"]
    assert started.child_session_id is None
    assert captured["task_type"] == svc.SUBAGENT_RUN_TASK_TYPE == "subagent"
    assert captured["status"] == "running"
    assert captured["parent_agent_id"] == parent
    assert captured["child_agent_name"] == "scout"
    assert captured["metadata_json"]["subagent_type"] == SUBAGENT_TYPE_WORKER
    assert captured["metadata_json"]["resumable_subagent"] is True
    assert captured["metadata_json"]["resume_after_restart"] is True
    assert captured["metadata_json"]["side_effect_risk"] == "mutating"
    assert captured["metadata_json"]["restart_replay_contract"]["schema"] == "runtime_restart_replay_contract.v1"
    assert (
        captured["metadata_json"]["restart_replay_contract"]["idempotency_key"] == f"subagent:{started.run_id}:restart"
    )
    assert captured["metadata_json"]["restart_replay_journal"][0]["phase"] == "spawn_intent_recorded"
    assert captured["metadata_json"]["restart_replay_journal"][0]["idempotency_key"] == (
        f"subagent:{started.run_id}:restart:spawn_intent_recorded"
    )
    assert "restart_resume_blocker" not in captured["metadata_json"]


@pytest.mark.asyncio
async def test_start_subagent_run_creates_child_session_and_records_session_contract(monkeypatch):
    captured: dict = {}

    async def _fake_create_child_session(**kwargs):
        captured["child_session_kwargs"] = kwargs
        return "child-session"

    async def _fake_create(**kwargs):
        captured["runtime_task"] = kwargs
        return kwargs["task_id"]

    monkeypatch.setattr(svc, "create_subagent_child_session", _fake_create_child_session, raising=False)
    monkeypatch.setattr(svc, "create_runtime_task_record", _fake_create)

    parent = uuid.uuid4()
    user = uuid.uuid4()
    started = await svc.start_subagent_run(
        parent_agent_id=parent,
        parent_user_id=user,
        spec_name="scout",
        spec_type="explorer",
        task="read x",
        parent_session_id="parent-session",
        trace_id="trace-1",
        context_mode="none",
    )

    assert started.run_id == captured["runtime_task"]["task_id"]
    assert started.child_session_id == "child-session"
    assert captured["child_session_kwargs"]["parent_agent_id"] == parent
    assert captured["child_session_kwargs"]["parent_user_id"] == user
    assert captured["child_session_kwargs"]["parent_session_id"] == "parent-session"
    assert captured["child_session_kwargs"]["spec_name"] == "scout"
    assert captured["child_session_kwargs"]["spec_type"] == "explorer"
    assert captured["child_session_kwargs"]["run_id"] == started.run_id
    assert captured["runtime_task"]["child_session_id"] == "child-session"
    metadata = captured["runtime_task"]["metadata_json"]
    assert metadata["child_session_id"] == "child-session"
    assert metadata["context_mode"] == "none"
    assert metadata["session_contract"]["kind"] == "subagent_child_session"
    assert metadata["session_contract"]["continuation_address"] == "child-session"
    assert metadata["session_contract"]["run_id"] == started.run_id


@pytest.mark.usefixtures("migrated_pg_url")
@pytest.mark.asyncio
async def test_start_subagent_run_real_pg_creates_child_session_and_runtime_task(owner_sessionmaker, monkeypatch):
    from app.models.agent import Agent
    from app.models.tenant import Tenant
    from app.models.user import User
    import app.services.runtime_task_service as runtime_task_service

    tenant_id = uuid.uuid4()
    parent_agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    parent_session_id = uuid.uuid4()

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tenant_id, name="subagent-real", slug=f"sa-{tenant_id.hex[:10]}"))
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            User(
                id=user_id,
                username=f"sa-u-{user_id.hex[:10]}",
                email=f"{user_id.hex[:10]}@subagent.test",
                password_hash="x",
                display_name="Subagent Owner",
                tenant_id=tenant_id,
            )
        )
        await session.flush()
        session.add(
            Agent(
                id=parent_agent_id,
                tenant_id=tenant_id,
                name="parent-agent",
                role_description="parent",
                creator_id=user_id,
                sponsor_user_id=user_id,
            )
        )
        await session.flush()
        session.add(
            ChatSession(
                id=parent_session_id,
                agent_id=parent_agent_id,
                tenant_id=tenant_id,
                user_id=user_id,
                title="Parent Session",
                source_channel="web",
            )
        )

    def scoped_session(tenant=None, **_kwargs):
        return tenant_scoped_session(tenant, session_factory=owner_sessionmaker)

    async def resolve_tenant(_agent_id, *_args, **_kwargs):
        return tenant_id

    monkeypatch.setattr(svc, "tenant_scoped_session", scoped_session)
    monkeypatch.setattr(svc, "resolve_tenant_for_agent", resolve_tenant)
    monkeypatch.setattr(runtime_task_service, "tenant_scoped_session", scoped_session)
    monkeypatch.setattr(runtime_task_service, "resolve_tenant_for_agent", resolve_tenant)

    started = await svc.start_subagent_run(
        parent_agent_id=parent_agent_id,
        parent_user_id=user_id,
        spec_name="researcher",
        spec_type="explorer",
        task="inspect the evidence",
        parent_session_id=str(parent_session_id),
        trace_id="trace-skill-fork",
        context_mode="none",
    )

    assert started.child_session_id
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        child_session = (
            await session.execute(select(ChatSession).where(ChatSession.id == uuid.UUID(started.child_session_id)))
        ).scalar_one()
        runtime_task = (
            await session.execute(select(RuntimeTask).where(RuntimeTask.id == uuid.UUID(started.run_id)))
        ).scalar_one()

    assert child_session.source_channel == "subagent"
    assert child_session.session_kind == "subagent"
    assert child_session.parent_session_id == parent_session_id
    assert child_session.transcript_metadata_json["session_contract"]["continuation_address"] == started.child_session_id
    assert child_session.transcript_metadata_json["session_contract"]["run_id"] == started.run_id
    assert runtime_task.task_type == svc.SUBAGENT_RUN_TASK_TYPE
    assert runtime_task.status == "running"
    assert runtime_task.parent_agent_id == parent_agent_id
    assert runtime_task.parent_session_id == str(parent_session_id)
    assert runtime_task.child_session_id == started.child_session_id
    assert runtime_task.metadata_json["session_contract"]["continuation_address"] == started.child_session_id
    assert runtime_task.metadata_json["session_contract"]["run_id"] == started.run_id


@pytest.mark.usefixtures("migrated_pg_url")
@pytest.mark.asyncio
async def test_kernel_skill_fork_handoff_calls_real_spawn_tool_and_records_child_t0(
    owner_sessionmaker,
    monkeypatch,
    tmp_path,
):
    from app.kernel.contracts import InvocationRequest, RuntimeConfig
    from app.kernel.engine import _execute_tool_with_hooks
    from app.memory.t0.ledger import replay_t0_session_events
    from app.models.agent import Agent
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.models.tenant import Tenant
    from app.models.user import User
    from app.runtime.session import SessionContext
    from app.tools.handlers import subagent as subagent_handler
    from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest
    import app.services.runtime_task_service as runtime_task_service

    tenant_id = uuid.uuid4()
    parent_agent_id = uuid.uuid4()
    user_id = uuid.uuid4()
    parent_session_id = uuid.uuid4()

    async with tenant_scoped_session(None, session_factory=owner_sessionmaker) as session:
        session.add(Tenant(id=tenant_id, name="subagent-kernel", slug=f"sak-{tenant_id.hex[:10]}"))
    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        session.add(
            User(
                id=user_id,
                username=f"sak-u-{user_id.hex[:10]}",
                email=f"{user_id.hex[:10]}@subagent-kernel.test",
                password_hash="x",
                display_name="Subagent Kernel Owner",
                tenant_id=tenant_id,
            )
        )
        await session.flush()
        session.add(
            Agent(
                id=parent_agent_id,
                tenant_id=tenant_id,
                name="parent-agent",
                role_description="parent",
                creator_id=user_id,
                sponsor_user_id=user_id,
            )
        )
        await session.flush()
        session.add(
            ChatSession(
                id=parent_session_id,
                agent_id=parent_agent_id,
                tenant_id=tenant_id,
                user_id=user_id,
                title="Parent Session",
                source_channel="web",
            )
        )

    def scoped_session(tenant=None, **_kwargs):
        return tenant_scoped_session(tenant, session_factory=owner_sessionmaker)

    async def resolve_tenant(_agent_id, *_args, **_kwargs):
        return tenant_id

    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    monkeypatch.setattr(svc, "tenant_scoped_session", scoped_session)
    monkeypatch.setattr(svc, "resolve_tenant_for_agent", resolve_tenant)
    monkeypatch.setattr(subagent_handler, "tenant_scoped_session", scoped_session)
    monkeypatch.setattr(subagent_handler, "resolve_tenant_for_agent", resolve_tenant)
    monkeypatch.setattr(runtime_task_service, "tenant_scoped_session", scoped_session)
    monkeypatch.setattr(runtime_task_service, "resolve_tenant_for_agent", resolve_tenant)
    monkeypatch.setattr(subagent_handler, "memory_store_for_agent", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(subagent_handler, "memory_store_for_tenant", lambda *_args, **_kwargs: None)

    async def fake_resolve_parent_runtime(_agent_id):
        return (
            SimpleNamespace(provider="openai", model="gpt-4.1", api_key="test", base_url=None),
            None,
            SimpleNamespace(id=parent_agent_id, name="parent-agent", tenant_id=tenant_id, creator_id=user_id),
        )

    spawned: dict[str, object] = {}

    async def fake_spawn_subagent(ctx, spec, task, **kwargs):
        spawned["ctx"] = ctx
        spawned["spec"] = spec
        spawned["task"] = task
        spawned["kwargs"] = kwargs
        return SimpleNamespace(result=None)

    async def fake_emit_hook(*_args, **_kwargs):
        return None

    monkeypatch.setattr(subagent_handler, "_resolve_parent_runtime", fake_resolve_parent_runtime)
    monkeypatch.setattr(subagent_handler, "spawn_subagent", fake_spawn_subagent)
    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)

    session_context = SessionContext(
        session_id=str(parent_session_id),
        metadata={
            "pending_skill_handoffs": [
                {
                    "skill": "Research",
                    "skill_slug": "research",
                    "source": "skills/research/SKILL.md",
                    "execution_tool": "spawn_subagent",
                    "tool_arguments": {
                        "prompt": "Use the loaded skill `Research`.",
                        "description": "Skill fork worker for Research",
                        "skill": "Research",
                        "subagent_type": "explorer",
                        "permission_profile": {"mode": "auto", "allowed_tools": ["web_search", "read_file"]},
                    },
                    "permission_profile": {"mode": "auto", "allowed_tools": ["web_search", "read_file"]},
                }
            ]
        },
    )
    request = InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "load research"}],
        agent_name="Agent",
        role_description="role",
        agent_id=parent_agent_id,
        user_id=user_id,
        session_context=session_context,
        memory_session_id=str(parent_session_id),
    )

    async def execute_tool(tool_name, args, _request, _emit_event, *, tool_call_id=None):
        if tool_name == "load_skill":
            return "Loaded Research"
        if tool_name == "spawn_subagent":
            context = ToolExecutionContext(
                agent_id=parent_agent_id,
                user_id=user_id,
                tenant_id=str(tenant_id),
                workspace=tmp_path,
                session_id=str(parent_session_id),
            )
            return await subagent_handler.spawn_subagent_tool(
                ToolExecutionRequest(tool_name=tool_name, arguments=dict(args), context=context)
            )
        raise AssertionError(f"unexpected tool {tool_name}")

    async def emit_event(_event):
        return None

    result, _effective_args, executed = await _execute_tool_with_hooks(
        execute_tool=execute_tool,
        request=request,
        runtime_config=RuntimeConfig(tenant_id=tenant_id, max_tool_rounds=3),
        tool_name="load_skill",
        tool_args={"name": "Research"},
        tool_call_id="call-load-skill",
        emit_event=emit_event,
    )

    assert executed is True
    handoff_result = json.loads(session_context.metadata["executed_skill_handoffs"][0]["result"])
    child_session_id = uuid.UUID(handoff_result["child_session_id"])
    run_id_text = handoff_result["run_id"]
    run_id = uuid.UUID(run_id_text)
    assert handoff_result["mode"] == "background"
    assert "Skill fork worker `Research` executed through `spawn_subagent`." in result
    assert spawned["task"] == "Use the loaded skill `Research`."
    assert spawned["ctx"].child_session_id == str(child_session_id)

    async with tenant_scoped_session(str(tenant_id), session_factory=owner_sessionmaker) as session:
        child_session = (await session.execute(select(ChatSession).where(ChatSession.id == child_session_id))).scalar_one()
        runtime_task = (await session.execute(select(RuntimeTask).where(RuntimeTask.id == run_id))).scalar_one()
        start_event = (
            await session.execute(
                select(ChatTranscriptEvent).where(
                    ChatTranscriptEvent.session_id == child_session_id,
                    ChatTranscriptEvent.event_type == "subagent_task_started",
                )
            )
        ).scalar_one()

    assert child_session.source_channel == "subagent"
    assert child_session.parent_session_id == parent_session_id
    assert runtime_task.task_type == svc.SUBAGENT_RUN_TASK_TYPE
    assert runtime_task.child_session_id == str(child_session_id)
    assert start_event.metadata_json["session_contract"]["run_id"] == run_id_text
    t0_events = replay_t0_session_events(agent_id=parent_agent_id, session_id=child_session_id, data_root=tmp_path)
    assert [(event.event_type, event.role, event.content) for event in t0_events] == [
        ("subagent_task_started", "user", "Use the loaded skill `Research`.")
    ]


@pytest.mark.asyncio
async def test_record_subagent_child_tool_frame_persists_and_clears_pending_frame(monkeypatch):
    updates: list[tuple[str, dict]] = []

    async def _fake_update(run_id, **kwargs):
        updates.append((run_id, kwargs))
        return True

    monkeypatch.setattr(svc, "update_runtime_task_record", _fake_update)

    await svc.record_subagent_child_tool_frame(
        run_id="run-1",
        tool_name="read_file",
        tool_args={"path": "workspace/a.md"},
        tool_call_id="call-read",
        status="running",
        child_session_id="child-session",
        parent_session_id="parent-session",
        trace_id="trace-1",
    )

    running_metadata = updates[-1][1]["metadata_json"]
    assert running_metadata["child_pending_tool_frame"]["tool_call_id"] == "call-read"
    assert running_metadata["child_pending_tool_frame"]["tool_name"] == "read_file"
    assert running_metadata["child_pending_tool_frame"]["arguments"] == {"path": "workspace/a.md"}
    assert running_metadata["child_pending_tool_frame"]["origin_channel"] == "subagent"
    assert running_metadata["child_pending_tool_frame"]["subagent_run_id"] == "run-1"
    assert running_metadata["child_pending_tool_frames"] == [running_metadata["child_pending_tool_frame"]]

    await svc.record_subagent_child_tool_frame(
        run_id="run-1",
        tool_name="read_file",
        tool_args={"path": "workspace/a.md"},
        tool_call_id="call-read",
        status="done",
        child_session_id="child-session",
        parent_session_id="parent-session",
        trace_id="trace-1",
    )

    done_metadata = updates[-1][1]["metadata_json"]
    assert done_metadata["child_pending_tool_frame"] is None
    assert done_metadata["child_pending_tool_frames"] == []
    assert done_metadata["last_child_tool_frame"]["status"] == "done"
    assert done_metadata["last_child_tool_frame"]["tool_call_id"] == "call-read"
    assert done_metadata["last_child_tool_frame"]["subagent_run_id"] == "run-1"


@pytest.mark.asyncio
async def test_start_subagent_run_marks_readonly_types_restart_resumable(monkeypatch):
    captured: dict = {}

    async def _fake_create(**kwargs):
        captured.update(kwargs)
        return kwargs["task_id"]

    monkeypatch.setattr(svc, "create_runtime_task_record", _fake_create)
    parent = uuid.uuid4()
    await svc.start_subagent_run(parent_agent_id=parent, spec_name="scout", spec_type="explorer", task="read x")

    assert captured["metadata_json"]["subagent_type"] == "explorer"
    assert captured["metadata_json"]["resumable_subagent"] is True
    assert captured["metadata_json"]["resume_after_restart"] is True
    assert captured["metadata_json"]["subagent_name"] == "scout"


@pytest.mark.asyncio
async def test_run_completer_maps_ok_to_completed(monkeypatch):
    captured: dict = {}

    async def _fake_update(run_id, **fields):
        captured["run_id"] = run_id
        captured.update(fields)
        return True

    async def _fake_session_state(**kwargs):
        captured["session_state_update"] = kwargs

    monkeypatch.setattr(svc, "update_runtime_task_record", _fake_update)
    monkeypatch.setattr(svc, "update_subagent_child_session_state_for_run", _fake_session_state)
    completer = svc.make_run_completer("run-1")
    await completer(SubagentResult(name="scout", type="worker", status="completed", content="done", tokens_used=42))
    assert captured["run_id"] == "run-1"
    assert captured["status"] == "completed"
    assert captured["result_summary"] == "done"
    assert captured["token_usage"] == {"total_tokens": 42}
    assert captured["metadata_json"]["completion_journal"][-1]["status"] == "completed"
    assert captured["metadata_json"]["completion_journal"][-1]["idempotency_key"] == "subagent:run-1:completed"
    assert captured["session_state_update"]["run_id"] == "run-1"
    assert captured["session_state_update"]["status"] == "completed"
    assert captured["session_state_update"]["summary"] == "done"


@pytest.mark.asyncio
async def test_run_completer_maps_failure_to_failed(monkeypatch):
    captured: dict = {}

    async def _fake_update(run_id, **fields):
        captured.update(fields)
        return True

    async def _fake_session_state(**_kwargs):
        return None

    monkeypatch.setattr(svc, "update_runtime_task_record", _fake_update)
    monkeypatch.setattr(svc, "update_subagent_child_session_state_for_run", _fake_session_state)
    completer = svc.make_run_completer("run-2")
    await completer(SubagentResult(name="scout", type="worker", status="failed", error="boom"))
    assert captured["status"] == "failed"
    assert "boom" in captured["result_summary"]
    assert captured["metadata_json"]["completion_journal"][-1]["status"] == "failed"


@pytest.mark.asyncio
async def test_subagent_completion_projects_child_session_event_to_parent(monkeypatch):
    child_session_id = uuid.uuid4()
    parent_session_id = uuid.uuid4()
    parent_agent_id = uuid.uuid4()
    parent_user_id = uuid.uuid4()
    tenant_id = uuid.uuid4()
    captured_events: list[dict] = []

    session = SimpleNamespace(
        id=child_session_id,
        agent_id=parent_agent_id,
        tenant_id=tenant_id,
        user_id=parent_user_id,
        transcript_metadata_json={"session_contract": {"kind": "subagent_child_session"}},
        root_session_id=parent_session_id,
        parent_session_id=parent_session_id,
        visibility_scope="team",
        listed_surface="parent",
    )

    class _Scalar:
        def scalar_one_or_none(self):
            return session

    class _FakeSession:
        async def execute(self, _stmt):
            return _Scalar()

        async def commit(self):
            return None

    class _Ctx:
        async def __aenter__(self):
            return _FakeSession()

        async def __aexit__(self, *_exc):
            return False

    async def fake_get_runtime_task_record(_run_id):
        return {
            "task_id": "run-1",
            "parent_agent_id": str(parent_agent_id),
            "child_session_id": str(child_session_id),
            "parent_session_id": str(parent_session_id),
            "metadata": {"child_session_id": str(child_session_id)},
        }

    async def fake_resolve_tenant_for_agent(_agent_id):
        return tenant_id

    async def fake_append_session_event(**kwargs):
        captured_events.append(kwargs)
        return SimpleNamespace(event_id=uuid.uuid4(), sequence=len(captured_events), message_id=None)

    monkeypatch.setattr(svc, "get_runtime_task_record", fake_get_runtime_task_record)
    monkeypatch.setattr(svc, "resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr(svc, "tenant_scoped_session", lambda _tenant_id: _Ctx())
    monkeypatch.setattr(svc, "append_session_event", fake_append_session_event)

    await svc.update_subagent_child_session_state_for_run(run_id="run-1", status="completed", summary="done")

    parent_event = next(event for event in captured_events if event["session_id"] == parent_session_id)
    assert parent_event["event_type"] == "child_session"
    assert parent_event["role"] == "system"
    assert parent_event["parts"] == [{
        "type": "event",
        "event_type": "child_session",
        "title": "Child Session",
        "text": "done",
        "status": "completed",
        "runtime_task_id": "run-1",
        "child_session_id": str(child_session_id),
        "parent_session_id": str(parent_session_id),
        "root_session_id": str(parent_session_id),
        "reason": "subagent_task_completed",
    }]


@pytest.mark.asyncio
async def test_get_subagent_run_is_ownership_scoped(monkeypatch):
    owner = uuid.uuid4()
    other = uuid.uuid4()

    async def _fake_get(_run_id):
        return {"task_type": "subagent", "parent_agent_id": str(owner), "status": "completed", "result": "r"}

    monkeypatch.setattr(svc, "get_runtime_task_record", _fake_get)
    assert await svc.get_subagent_run("rid", owner) is not None
    assert await svc.get_subagent_run("rid", other) is None  # another agent cannot read it


@pytest.mark.asyncio
async def test_get_subagent_run_rejects_non_subagent_task(monkeypatch):
    owner = uuid.uuid4()

    async def _fake_get(_run_id):
        return {"task_type": "web_chat_turn", "parent_agent_id": str(owner), "status": "running"}

    monkeypatch.setattr(svc, "get_runtime_task_record", _fake_get)
    assert await svc.get_subagent_run("rid", owner) is None


def test_spawn_schema_exposes_run_in_background_and_check_tool_registered():
    from app.tools.handlers.subagent import _SPAWN_PARAMETERS, check_subagent, spawn_subagent_tool  # noqa: F401

    assert "run_in_background" in _SPAWN_PARAMETERS["properties"]
    assert "prompt" in _SPAWN_PARAMETERS["properties"]
    assert "subagent_type" in _SPAWN_PARAMETERS["properties"]
    assert "general-purpose" in _SPAWN_PARAMETERS["properties"]["subagent_type"]["enum"]
    # check_subagent is a registered @tool (callable handler).
    assert callable(check_subagent)


def test_subagent_task_type_uses_metadata_resumability():
    # Restart-resumable subagent records are preserved for the restart pump; old
    # records without explicit resumability still fail closed.
    from app.services.runtime_task_service import _is_restart_resumable_runtime_task

    resumable = type(
        "RuntimeTaskStub",
        (),
        {
            "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
            "metadata_json": {"resume_after_restart": True, "resumable_subagent": True},
        },
    )()
    unsafe = type(
        "RuntimeTaskStub",
        (),
        {
            "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
            "metadata_json": {"resume_after_restart": False, "resumable_subagent": False},
        },
    )()

    assert _is_restart_resumable_runtime_task(resumable) is True
    assert _is_restart_resumable_runtime_task(unsafe) is False


@pytest.mark.asyncio
async def test_resume_persisted_subagent_runs_rehydrates_readonly_worker(monkeypatch):
    run_id = uuid.uuid4().hex
    parent = uuid.uuid4()
    calls: dict[str, object] = {}

    async def fake_list_active_runtime_task_records(limit=50, statuses=("pending", "running")):
        return [
            {
                "task_id": run_id,
                "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
                "parent_agent_id": str(parent),
                "child_agent_name": "scout",
                "prompt": "read x",
                "trace_id": "trace-subagent",
                "parent_session_id": "parent-session",
                "metadata": {
                    "subagent_type": "explorer",
                    "subagent_name": "scout",
                    "resume_after_restart": True,
                    "resumable_subagent": True,
                },
            }
        ]

    async def fake_resolve_parent_runtime(parent_agent_id):
        calls["resolved_parent"] = parent_agent_id
        return {
            "ctx_kwargs": {
                "parent_agent_id": parent,
                "parent_user_id": uuid.uuid4(),
                "model": object(),
                "parent_agent_name": "Parent",
                "tenant_id": uuid.uuid4(),
            }
        }

    async def fake_spawn_subagent(ctx, spec, task, **kwargs):
        calls["ctx"] = ctx
        calls["spec"] = spec
        calls["task"] = task
        calls["kwargs"] = kwargs
        return object()

    async def fake_update_runtime_task_record(task_id, **kwargs):
        calls.setdefault("updates", []).append((task_id, kwargs))
        return True

    monkeypatch.setattr(svc, "list_active_runtime_task_records", fake_list_active_runtime_task_records)
    monkeypatch.setattr(svc, "_resolve_parent_runtime", fake_resolve_parent_runtime, raising=False)
    monkeypatch.setattr(svc, "spawn_subagent", fake_spawn_subagent, raising=False)
    monkeypatch.setattr(svc, "update_runtime_task_record", fake_update_runtime_task_record)

    resumed = await svc.resume_persisted_subagent_runs()

    assert resumed == [run_id]
    assert calls["resolved_parent"] == parent
    assert calls["spec"].type == "explorer"
    assert calls["task"] == "read x"
    assert calls["kwargs"]["run_in_background"] is True
    assert callable(calls["kwargs"]["on_complete"])


@pytest.mark.asyncio
async def test_resume_persisted_subagent_runs_restores_readonly_child_pending_frame(monkeypatch):
    run_id = uuid.uuid4().hex
    parent = uuid.uuid4()
    child_session_id = str(uuid.uuid4())
    calls: dict[str, object] = {}

    async def fake_list_active_runtime_task_records(limit=50, statuses=("pending", "running")):
        return [
            {
                "task_id": run_id,
                "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
                "parent_agent_id": str(parent),
                "child_agent_name": "scout",
                "prompt": "read x",
                "trace_id": "trace-subagent",
                "parent_session_id": "parent-session",
                "child_session_id": child_session_id,
                "metadata": {
                    "subagent_type": "explorer",
                    "subagent_name": "scout",
                    "resume_after_restart": True,
                    "resumable_subagent": True,
                    "child_pending_tool_frame": {
                        "tool_call_id": "call-read",
                        "tool_name": "read_file",
                        "arguments": {"path": "workspace/a.md"},
                        "status": "running",
                        "origin_channel": "subagent",
                    },
                },
            }
        ]

    async def fake_resolve_parent_runtime(parent_agent_id):
        calls["resolved_parent"] = parent_agent_id
        return {
            "ctx_kwargs": {
                "parent_agent_id": parent,
                "parent_user_id": uuid.uuid4(),
                "model": object(),
                "parent_agent_name": "Parent",
                "tenant_id": uuid.uuid4(),
            }
        }

    async def fake_spawn_subagent(ctx, spec, task, **kwargs):
        calls["ctx"] = ctx
        calls["spec"] = spec
        calls["task"] = task
        calls["kwargs"] = kwargs
        return object()

    async def fake_update_runtime_task_record(task_id, **kwargs):
        calls.setdefault("updates", []).append((task_id, kwargs))
        return True

    monkeypatch.setattr(svc, "list_active_runtime_task_records", fake_list_active_runtime_task_records)
    monkeypatch.setattr(svc, "_resolve_parent_runtime", fake_resolve_parent_runtime, raising=False)
    monkeypatch.setattr(svc, "spawn_subagent", fake_spawn_subagent, raising=False)
    monkeypatch.setattr(svc, "update_runtime_task_record", fake_update_runtime_task_record)

    resumed = await svc.resume_persisted_subagent_runs()

    assert resumed == [run_id]
    assert calls["resolved_parent"] == parent
    assert calls["ctx"].subagent_run_id == run_id
    assert calls["ctx"].child_session_id == child_session_id
    assert calls["ctx"].recovery_metadata["pending_tool_frame"]["tool_call_id"] == "call-read"
    assert calls["ctx"].recovery_metadata["pending_tool_frame"]["tool_name"] == "read_file"
    assert calls["ctx"].recovery_metadata["pending_tool_frame"]["subagent_run_id"] == run_id
    assert calls["updates"][-1][1]["metadata_json"]["child_frame_recovered_after_restart"] is True
    assert calls["updates"][-1][1]["metadata_json"]["restart_replay_journal"][-1]["phase"] == (
        "child_frame_resume_intent_recorded"
    )


@pytest.mark.asyncio
async def test_resume_persisted_subagent_runs_reconciles_mutating_child_pending_frame(monkeypatch):
    run_id = uuid.uuid4().hex
    updates: list[tuple[str, dict]] = []
    session_updates: list[dict] = []

    async def fake_list_active_runtime_task_records(limit=50, statuses=("pending", "running")):
        return [
            {
                "task_id": run_id,
                "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
                "parent_agent_id": str(uuid.uuid4()),
                "child_agent_name": "worker",
                "prompt": "write x",
                "trace_id": "trace-subagent",
                "parent_session_id": "parent-session",
                "child_session_id": "child-session",
                "metadata": {
                    "subagent_type": "explorer",
                    "subagent_name": "worker",
                    "resume_after_restart": True,
                    "resumable_subagent": True,
                    "child_pending_tool_frame": {
                        "tool_call_id": "call-write",
                        "tool_name": "write_file",
                        "arguments": {"path": "workspace/a.md", "content": "x"},
                        "status": "running",
                        "origin_channel": "subagent",
                    },
                },
            }
        ]

    async def fake_spawn_subagent(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("mutating child pending frame must not replay")

    async def fake_update_runtime_task_record(task_id, **kwargs):
        updates.append((task_id, kwargs))
        return True

    async def fake_update_child_session_state(**kwargs):
        session_updates.append(kwargs)

    monkeypatch.setattr(svc, "list_active_runtime_task_records", fake_list_active_runtime_task_records)
    monkeypatch.setattr(svc, "spawn_subagent", fake_spawn_subagent, raising=False)
    monkeypatch.setattr(svc, "update_runtime_task_record", fake_update_runtime_task_record)
    monkeypatch.setattr(svc, "update_subagent_child_session_state_for_run", fake_update_child_session_state)

    resumed = await svc.resume_persisted_subagent_runs()

    assert resumed == []
    assert updates[-1][0] == run_id
    assert updates[-1][1]["status"] == "needs_reconciliation"
    assert updates[-1][1]["metadata_json"]["restart_resume_blocker"] == "child_pending_tool_frame_not_replay_safe"
    assert updates[-1][1]["metadata_json"]["child_pending_tool_frame"]["tool_name"] == "write_file"
    assert session_updates[-1]["run_id"] == run_id
    assert session_updates[-1]["status"] == "needs_reconciliation"


@pytest.mark.asyncio
async def test_resume_persisted_subagent_runs_marks_mutating_record_for_reconciliation(monkeypatch):
    run_id = uuid.uuid4().hex
    updates: list[tuple[str, dict]] = []

    async def fake_list_active_runtime_task_records(limit=50, statuses=("pending", "running")):
        return [
            {
                "task_id": run_id,
                "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
                "parent_agent_id": str(uuid.uuid4()),
                "child_agent_name": "worker",
                "prompt": "write x",
                "metadata": {
                    "subagent_type": "worker",
                    "subagent_name": "worker",
                    "resume_after_restart": True,
                    "resumable_subagent": True,
                },
            }
        ]

    async def fake_spawn_subagent(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("mutating subagent must not be replayed")

    async def fake_update_runtime_task_record(task_id, **kwargs):
        updates.append((task_id, kwargs))
        return True

    monkeypatch.setattr(svc, "list_active_runtime_task_records", fake_list_active_runtime_task_records)
    monkeypatch.setattr(svc, "spawn_subagent", fake_spawn_subagent, raising=False)
    monkeypatch.setattr(svc, "update_runtime_task_record", fake_update_runtime_task_record)

    resumed = await svc.resume_persisted_subagent_runs()

    assert resumed == []
    assert updates[-1][0] == run_id
    assert updates[-1][1]["status"] == "needs_reconciliation"
    assert updates[-1][1]["metadata_json"]["needs_reconciliation"] is True
    assert updates[-1][1]["metadata_json"]["side_effect_risk"] == "mutating"


@pytest.mark.asyncio
async def test_resume_persisted_subagent_runs_reconciles_mutating_worker_even_with_spawn_journal(monkeypatch):
    run_id = uuid.uuid4().hex
    parent = uuid.uuid4()
    updates: list[tuple[str, dict]] = []

    async def fake_list_active_runtime_task_records(limit=50, statuses=("pending", "running")):
        return [
            {
                "task_id": run_id,
                "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
                "parent_agent_id": str(parent),
                "child_agent_name": "worker",
                "prompt": "write x",
                "trace_id": "trace-subagent",
                "parent_session_id": "parent-session",
                "metadata": {
                    "subagent_type": "worker",
                    "subagent_name": "worker",
                    "resume_after_restart": True,
                    "resumable_subagent": True,
                    "side_effect_risk": "mutating",
                    "restart_replay_contract": {
                        "schema": "runtime_restart_replay_contract.v1",
                        "idempotency_key": f"subagent:{run_id}:restart",
                        "task_type": "subagent",
                    },
                    "restart_replay_journal": [
                        {
                            "schema": "runtime_restart_replay_journal.v1",
                            "idempotency_key": f"subagent:{run_id}:restart:spawn_intent_recorded",
                            "task_type": "subagent",
                            "task_id": run_id,
                            "phase": "spawn_intent_recorded",
                            "side_effect_risk": "mutating",
                        }
                    ],
                },
            }
        ]

    async def fake_resolve_parent_runtime(_parent_agent_id):  # pragma: no cover - must not run
        raise AssertionError("mutating subagent must not resolve runtime for replay")

    async def fake_spawn_subagent(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("mutating subagent must not be replayed from spawn intent")

    async def fake_update_runtime_task_record(task_id, **kwargs):
        updates.append((task_id, kwargs))
        return True

    monkeypatch.setattr(svc, "list_active_runtime_task_records", fake_list_active_runtime_task_records)
    monkeypatch.setattr(svc, "_resolve_parent_runtime", fake_resolve_parent_runtime, raising=False)
    monkeypatch.setattr(svc, "spawn_subagent", fake_spawn_subagent, raising=False)
    monkeypatch.setattr(svc, "update_runtime_task_record", fake_update_runtime_task_record)

    resumed = await svc.resume_persisted_subagent_runs()

    assert resumed == []
    assert updates[-1][0] == run_id
    assert updates[-1][1]["status"] == "needs_reconciliation"
    assert updates[-1][1]["metadata_json"]["needs_reconciliation"] is True
    assert updates[-1][1]["metadata_json"]["restart_resume_blocker"] == "non_idempotent_subagent_type"


@pytest.mark.asyncio
async def test_resume_persisted_subagent_runs_refuses_mutating_worker_without_replay_journal(monkeypatch):
    run_id = uuid.uuid4().hex
    updates: list[tuple[str, dict]] = []

    async def fake_list_active_runtime_task_records(limit=50, statuses=("pending", "running")):
        return [
            {
                "task_id": run_id,
                "task_type": svc.SUBAGENT_RUN_TASK_TYPE,
                "parent_agent_id": str(uuid.uuid4()),
                "child_agent_name": "worker",
                "prompt": "write x",
                "metadata": {
                    "subagent_type": "worker",
                    "subagent_name": "worker",
                    "resume_after_restart": True,
                    "resumable_subagent": True,
                    "side_effect_risk": "mutating",
                    "restart_replay_contract": {
                        "schema": "runtime_restart_replay_contract.v1",
                        "idempotency_key": f"subagent:{run_id}:restart",
                        "task_type": "subagent",
                    },
                },
            }
        ]

    async def fake_spawn_subagent(*_args, **_kwargs):  # pragma: no cover - must not run
        raise AssertionError("mutating subagent without replay journal must not be replayed")

    async def fake_resolve_parent_runtime(_parent_agent_id):
        return {
            "ctx_kwargs": {
                "parent_agent_id": uuid.uuid4(),
                "parent_user_id": uuid.uuid4(),
                "model": object(),
                "parent_agent_name": "Parent",
                "tenant_id": uuid.uuid4(),
            }
        }

    async def fake_update_runtime_task_record(task_id, **kwargs):
        updates.append((task_id, kwargs))
        return True

    monkeypatch.setattr(svc, "list_active_runtime_task_records", fake_list_active_runtime_task_records)
    monkeypatch.setattr(svc, "_resolve_parent_runtime", fake_resolve_parent_runtime, raising=False)
    monkeypatch.setattr(svc, "spawn_subagent", fake_spawn_subagent, raising=False)
    monkeypatch.setattr(svc, "update_runtime_task_record", fake_update_runtime_task_record)

    resumed = await svc.resume_persisted_subagent_runs()

    assert resumed == []
    assert updates[-1][0] == run_id
    assert updates[-1][1]["status"] == "needs_reconciliation"
    assert updates[-1][1]["metadata_json"]["restart_resume_blocker"] == "non_idempotent_subagent_type"
