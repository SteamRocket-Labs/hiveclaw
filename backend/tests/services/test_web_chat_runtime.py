from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalars(self):
        return SimpleNamespace(first=lambda: self._value, all=lambda: [self._value] if self._value else [])


class _FakeDB:
    def __init__(self, active_run=None):
        self.active_run = active_run
        self.added = []
        self.commits = 0

    async def execute(self, _stmt):
        return _ScalarResult(self.active_run)

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        self.commits += 1

    async def flush(self):
        return None


def test_clear_interactive_plan_mode_clears_typed_state_and_metadata_mirror():
    import app.services.web_chat_runtime as runtime
    from app.runtime.session import PlanModeState, SessionContext

    context = SessionContext(session_id="session-1", source="web_chat", channel="web")
    context.plan_mode = PlanModeState(
        active=True,
        original_request="schedule daily brief",
        intent_type="autonomous_wake",
        action_kind="create_enabled_trigger",
        tool_name="set_trigger",
        plan_file_path="workspace/plans/session-1.plan.md",
    )
    context.metadata["plan_mode"] = context.plan_mode.to_metadata()

    runtime._clear_interactive_plan_mode(context)

    assert context.plan_mode.active is False
    assert context.plan_mode.original_request is None
    assert "plan_mode" not in context.metadata


def test_clear_stale_plan_mode_for_plain_new_turn():
    import app.services.web_chat_runtime as runtime
    from app.runtime.session import PlanModeState, SessionContext

    context = SessionContext(session_id="session-1", source="web_chat", channel="web")
    context.plan_mode = PlanModeState(
        active=True,
        original_request="draft a plan",
        intent_type="in_session_execution",
        action_kind="start_long_task",
        tool_name="continue_current_session",
    )
    context.metadata["plan_mode"] = context.plan_mode.to_metadata()

    runtime._clear_stale_plan_mode_for_new_turn(
        context,
        plan_mode_requested=False,
        history_messages=[],
    )

    assert context.plan_mode.active is False
    assert "plan_mode" not in context.metadata


def test_preserve_plan_mode_for_blocking_clarification_reply():
    import app.services.web_chat_runtime as runtime
    from app.runtime.session import PlanModeState, SessionContext

    context = SessionContext(session_id="session-1", source="web_chat", channel="web")
    context.plan_mode = PlanModeState(
        active=True,
        original_request="draft a plan",
        intent_type="in_session_execution",
        action_kind="start_long_task",
        tool_name="continue_current_session",
    )
    context.metadata["plan_mode"] = context.plan_mode.to_metadata()
    history_messages = [
        SimpleNamespace(
            role="assistant",
            content='{"status":"awaiting_user_clarification","blocking":true,"question":"范围是什么？"}',
        )
    ]

    runtime._clear_stale_plan_mode_for_new_turn(
        context,
        plan_mode_requested=False,
        history_messages=history_messages,
    )

    assert context.plan_mode.active is True
    assert context.metadata["plan_mode"]["active"] is True


def test_interactive_pause_summary_accepts_structured_tool_payloads():
    import app.services.web_chat_runtime as runtime

    assert runtime._interactive_pause_summary_for_tool_call(
        {
            "name": "ask_user_question",
            "status": "done",
            "result": {
                "status": "awaiting_user_clarification",
                "blocking": True,
                "questions": [{"question": "Scope?", "options": [{"label": "A"}]}],
            },
        }
    ) == "awaiting_user_clarification"
    assert runtime._interactive_pause_summary_for_tool_call(
        {
            "name": "create_digital_employee",
            "status": "done",
            "result": {
                "status": "success",
                "agent_id": "7a5b31cb-89b4-4053-a48e-6dfb42a8af20",
                "message": "Successfully created digital employee.",
            },
        }
    ) == "create_digital_employee_success"


def test_explicit_plan_mode_request_does_not_clear_existing_plan_state_before_reactivation():
    import app.services.web_chat_runtime as runtime
    from app.runtime.session import PlanModeState, SessionContext

    context = SessionContext(session_id="session-1", source="web_chat", channel="web")
    context.plan_mode = PlanModeState(
        active=True,
        original_request="draft a plan",
        intent_type="in_session_execution",
        action_kind="start_long_task",
        tool_name="continue_current_session",
    )
    context.metadata["plan_mode"] = context.plan_mode.to_metadata()

    runtime._clear_stale_plan_mode_for_new_turn(
        context,
        plan_mode_requested=True,
        history_messages=[],
    )

    assert context.plan_mode.active is True
    assert context.metadata["plan_mode"]["active"] is True


def test_record_skill_runtime_usage_for_invocation_collects_web_chat_loaded_skill(monkeypatch, tmp_path):
    import app.services.skill_runtime_telemetry as telemetry
    from app.runtime.session import SessionContext

    agent_id = uuid4()
    calls = []

    monkeypatch.setattr(telemetry, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    def fake_record_skill_runtime_usage(workspace, **kwargs):
        calls.append((workspace, kwargs))
        return {"decision": "promote_candidate", "workflow_signature": "load_skill -> web_search"}

    monkeypatch.setattr(telemetry, "record_skill_runtime_usage", fake_record_skill_runtime_usage)

    result = telemetry.record_skill_runtime_usage_for_invocation(
        agent_id=agent_id,
        session_context=SessionContext(session_id="session-1", source="web_chat", channel="web"),
        tool_events=[
            {"name": "load_skill", "args": {"name": "Deployment Review"}, "status": "done"},
            {"name": "web_search", "args": {"query": "railway logs"}, "status": "done"},
        ],
        terminal_status="completed",
        assistant_text="Finished production deployment review.",
        note="Finished production deployment review.",
    )

    assert result == {"decision": "promote_candidate", "workflow_signature": "load_skill -> web_search"}
    assert calls == [
        (
            tmp_path / str(agent_id),
            {
                "skill_name": "Deployment Review",
                "loaded_skill_names": ["Deployment Review"],
                "tool_names": ["load_skill", "web_search"],
                "status": "success",
                "note": "Finished production deployment review.",
                "source": "web_chat",
                "session_id": "session-1",
                "runtime_task_id": None,
                "trace_id": None,
                "blocker": None,
            },
        )
    ]


@pytest.mark.asyncio
async def test_disconnect_does_not_cancel_registered_web_chat_run():
    from app.services.web_chat_runtime import (
        handle_web_chat_disconnect,
        register_web_chat_run_for_test,
        unregister_web_chat_run_for_test,
    )

    run_id = uuid4().hex
    cancel_event = asyncio.Event()
    register_web_chat_run_for_test(run_id, cancel_event=cancel_event)

    try:
        await handle_web_chat_disconnect(run_id)
        assert cancel_event.is_set() is False
    finally:
        unregister_web_chat_run_for_test(run_id)


@pytest.mark.asyncio
async def test_cancel_web_chat_run_sets_cancel_event_and_marks_runtime_task_killed(monkeypatch):
    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    session_id = uuid4()
    user_id = uuid4()
    runtime_task = SimpleNamespace(
        id=run_id,
        task_type="web_chat_turn",
        status="running",
        parent_agent_id=agent_id,
        parent_session_id=str(session_id),
        metadata_json={"user_id": str(user_id), "session_id": str(session_id)},
        result_summary=None,
        completed_at=None,
    )
    db = _FakeDB(active_run=runtime_task)
    cancel_event = asyncio.Event()
    runtime.register_web_chat_run_for_test(run_id.hex, cancel_event=cancel_event)

    try:
        result = await runtime.cancel_web_chat_run(
            db=db,
            agent_id=agent_id,
            session_id=session_id,
            run_id=run_id,
            user_id=user_id,
        )

        assert result["run_id"] == run_id.hex
        assert result["status"] == "killed"
        assert cancel_event.is_set() is True
        assert runtime_task.status == "killed"
        assert runtime_task.metadata_json["cancelled_by_user"] is True
        assert db.commits == 1
    finally:
        runtime.unregister_web_chat_run_for_test(run_id.hex)


@pytest.mark.asyncio
async def test_persist_assistant_message_stores_thinking_signature(monkeypatch):
    import app.services.tenant_resolver as tenant_resolver
    import app.services.web_chat_runtime as runtime

    agent_id = uuid4()
    tenant_id = uuid4()
    added = []

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def add(self, value):
            added.append(value)

        async def commit(self):
            return None

    async def fake_resolve_tenant_for_agent(_agent_id):
        return tenant_id

    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda _tenant_id: _Session())

    await runtime._persist_assistant_message(
        agent_id=agent_id,
        user_id=uuid4(),
        session_id=uuid4().hex,
        content="answer",
        thinking="private thinking",
        thinking_signature="sig-db",
    )

    assert added[0].tenant_id == tenant_id
    assert added[0].thinking == "private thinking"
    assert added[0].thinking_signature == "sig-db"


@pytest.mark.asyncio
async def test_finalize_web_chat_run_skips_assistant_when_run_already_terminal(monkeypatch):
    import app.services.tenant_resolver as tenant_resolver
    import app.services.web_chat_runtime as runtime

    agent_id = uuid4()
    tenant_id = uuid4()
    run_id = uuid4()
    task = SimpleNamespace(
        id=run_id,
        task_type="web_chat_turn",
        status="completed",
        metadata_json={},
        result_summary="already done",
        completed_at=None,
    )
    added = []

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _stmt):
            return _ScalarResult(task)

        def add(self, value):
            added.append(value)

        async def commit(self):
            raise AssertionError("terminal duplicate finalization must not commit")

    async def fake_resolve_tenant_for_agent(_agent_id):
        return tenant_id

    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda _tenant_id: _Session())

    finalized = await runtime._finalize_web_chat_run_with_assistant(
        run_uuid=run_id,
        agent_id=agent_id,
        user_id=uuid4(),
        session_id=uuid4().hex,
        content="duplicate answer",
        thinking=None,
        thinking_signature=None,
        status="completed",
        result_summary="duplicate answer",
        metadata_json={"cancelled_by_user": False},
    )

    assert finalized is False
    assert added == []


@pytest.mark.asyncio
async def test_finalize_web_chat_run_sets_run_scoped_assistant_marker(monkeypatch, tmp_path):
    import app.services.tenant_resolver as tenant_resolver
    import app.services.web_chat_runtime as runtime
    from app.memory.t0.ledger import replay_t0_session_events

    agent_id = uuid4()
    tenant_id = uuid4()
    run_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    task = SimpleNamespace(
        id=run_id,
        task_type="web_chat_turn",
        status="running",
        metadata_json={},
        result_summary=None,
        completed_at=None,
    )
    added = []

    class _Session:
        def __init__(self):
            self.results = [task, None, None]
            self.commits = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _stmt):
            return _ScalarResult(self.results.pop(0))

        def add(self, value):
            added.append(value)

        async def commit(self):
            self.commits += 1

    session = _Session()

    async def fake_resolve_tenant_for_agent(_agent_id):
        return tenant_id

    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda _tenant_id: session)
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    finalized = await runtime._finalize_web_chat_run_with_assistant(
        run_uuid=run_id,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        content="final answer",
        thinking="private reasoning",
        thinking_signature="sig-final",
        status="completed",
        result_summary="final answer",
        metadata_json={"cancelled_by_user": False},
    )

    assert finalized is True
    chat_messages = [item for item in added if getattr(item, "role", None) == "assistant"]
    transcript_events = [item for item in added if getattr(item, "event_type", None) == "assistant_message"]
    assert len(chat_messages) == 1
    assert len(transcript_events) == 1
    assert chat_messages[0].decision_trace_id == f"web_chat_final:{run_id.hex}"
    assert chat_messages[0].thinking == "private reasoning"
    assert chat_messages[0].thinking_signature == "sig-final"
    assert transcript_events[0].parts_json[:2] == [
        {"type": "reasoning", "text": "private reasoning"},
        {"type": "text", "text": "final answer"},
    ]
    assert task.status == "completed"
    assert task.result_summary == "final answer"
    assert session.commits == 1
    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert [(event.event_type, event.role, event.content) for event in events] == [
        ("assistant_message", "assistant", "final answer")
    ]
    assert events[0].runtime_task_id == run_id.hex


@pytest.mark.asyncio
async def test_finalize_web_chat_run_binds_recent_workspace_artifacts(monkeypatch, tmp_path):
    from app.memory.t0.ledger import replay_t0_session_events
    import app.services.tenant_resolver as tenant_resolver
    import app.services.web_chat_runtime as runtime

    agent_id = uuid4()
    tenant_id = uuid4()
    run_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    task = SimpleNamespace(
        id=run_id,
        task_type="web_chat_turn",
        status="running",
        metadata_json={},
        result_summary=None,
        completed_at=None,
    )
    added = []
    artifact_calls = []

    class _Session:
        def __init__(self):
            self.results = [task, None, None]
            self.commits = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _stmt):
            return _ScalarResult(self.results.pop(0))

        def add(self, value):
            added.append(value)

        async def flush(self):
            return None

        async def commit(self):
            self.commits += 1

    session = _Session()

    async def fake_resolve_tenant_for_agent(_agent_id):
        return tenant_id

    def fake_create_chat_artifacts_for_message(**kwargs):
        artifact_calls.append(kwargs)
        return [
            {
                "artifact_id": "artifact-1",
                "path": "workspace/report.md",
                "name": "report.md",
                "preview_kind": "markdown",
                "source": "workspace_write",
            }
        ]

    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda _tenant_id: session)
    monkeypatch.setattr(runtime, "create_chat_artifacts_for_message", fake_create_chat_artifacts_for_message)
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    metadata_update = {"cancelled_by_user": False}
    finalized = await runtime._finalize_web_chat_run_with_assistant(
        run_uuid=run_id,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        content="final answer",
        thinking=None,
        status="completed",
        result_summary="final answer",
        metadata_json=metadata_update,
        artifact_paths=["workspace/report.md"],
    )

    assert finalized is True
    chat_messages = [item for item in added if getattr(item, "role", None) == "assistant"]
    transcript_events = [item for item in added if getattr(item, "event_type", None)]
    assert len(chat_messages) == 1
    assert [event.event_type for event in transcript_events] == ["assistant_message", "artifact_delivery"]
    assert artifact_calls
    assert artifact_calls[0]["message_id"] == chat_messages[0].id
    assert artifact_calls[0]["runtime_task_id"] == run_id
    assert artifact_calls[0]["paths"] == ["workspace/report.md"]
    assert task.metadata_json["artifact_ids"] == ["artifact-1"]
    assert metadata_update["artifact_ids"] == ["artifact-1"]
    assert metadata_update["artifacts"][0]["path"] == "workspace/report.md"
    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert any(event.event_type == "artifact_delivery" for event in events)


@pytest.mark.asyncio
async def test_finalize_web_chat_run_reuses_kernel_persisted_terminal_message(monkeypatch):
    import app.services.tenant_resolver as tenant_resolver
    import app.services.web_chat_runtime as runtime

    agent_id = uuid4()
    user_id = uuid4()
    tenant_id = uuid4()
    run_id = uuid4()
    session_id = uuid4().hex
    created_at = datetime.now(timezone.utc)
    task = SimpleNamespace(
        id=run_id,
        task_type="web_chat_turn",
        status="running",
        metadata_json={},
        result_summary=None,
        created_at=created_at,
        started_at=created_at,
        completed_at=None,
    )
    kernel_message = SimpleNamespace(
        decision_trace_id=None,
        content="budget stopped",
        created_at=created_at,
    )
    added = []

    class _Session:
        def __init__(self):
            self.results = [task, None, kernel_message]
            self.commits = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _stmt):
            return _ScalarResult(self.results.pop(0))

        def add(self, value):
            added.append(value)

        async def commit(self):
            self.commits += 1

    session = _Session()

    async def fake_resolve_tenant_for_agent(_agent_id):
        return tenant_id

    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda _tenant_id: session)

    finalized = await runtime._finalize_web_chat_run_with_assistant(
        run_uuid=run_id,
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        content="budget stopped",
        thinking=None,
        thinking_signature=None,
        status="completed",
        result_summary="budget stopped",
        metadata_json={"cancelled_by_user": False},
    )

    assert finalized is True
    assert not [item for item in added if getattr(item, "role", None) == "assistant"]
    assert [item.event_type for item in added if getattr(item, "event_type", None)] == ["assistant_message"]
    assert kernel_message.decision_trace_id == f"web_chat_final:{run_id.hex}"
    assert task.status == "completed"
    assert task.result_summary == "budget stopped"
    assert task.completed_at is not None
    assert session.commits == 1


@pytest.mark.asyncio
async def test_finalize_web_chat_run_skips_existing_final_assistant_marker(monkeypatch):
    import app.services.tenant_resolver as tenant_resolver
    import app.services.web_chat_runtime as runtime

    agent_id = uuid4()
    tenant_id = uuid4()
    run_id = uuid4()
    task = SimpleNamespace(
        id=run_id,
        task_type="web_chat_turn",
        status="running",
        metadata_json={},
        result_summary=None,
        completed_at=None,
    )
    existing_message = SimpleNamespace(id=uuid4(), decision_trace_id=f"web_chat_final:{run_id.hex}")
    added = []

    class _Session:
        def __init__(self):
            self.results = [task, existing_message]
            self.commits = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def execute(self, _stmt):
            return _ScalarResult(self.results.pop(0))

        def add(self, value):
            added.append(value)

        async def commit(self):
            self.commits += 1

    session = _Session()

    async def fake_resolve_tenant_for_agent(_agent_id):
        return tenant_id

    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda _tenant_id: session)

    finalized = await runtime._finalize_web_chat_run_with_assistant(
        run_uuid=run_id,
        agent_id=agent_id,
        user_id=uuid4(),
        session_id=uuid4().hex,
        content="duplicate final answer",
        thinking=None,
        thinking_signature=None,
        status="completed",
        result_summary="duplicate final answer",
        metadata_json={"cancelled_by_user": False},
    )

    assert finalized is False
    assert added == []
    assert task.status == "completed"
    assert task.result_summary == "duplicate final answer"
    assert task.completed_at is not None
    assert session.commits == 1


@pytest.mark.asyncio
async def test_execute_web_chat_run_does_not_broadcast_done_when_finalization_loses_race(monkeypatch):
    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    runtime_task = SimpleNamespace(
        id=run_id,
        parent_agent_id=agent_id,
        parent_session_id=session_id,
        prompt="latest question",
        metadata_json={"user_id": str(user_id), "session_id": session_id},
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="Web3研究员",
        role_description="",
        primary_model_id=uuid4(),
        fallback_model_id=None,
        tenant_id=uuid4(),
        agent_type="native",
    )
    user = SimpleNamespace(id=user_id, display_name="Rocky", username="rocky")
    llm_model = SimpleNamespace(provider="openai", model="gpt-4.1", supports_vision=False)
    broadcasts: list[dict] = []

    async def fake_load_context(_run_uuid):
        return runtime_task, agent, user, llm_model, None, []

    async def fake_invoke(_request):
        return SimpleNamespace(content="same final answer", reasoning_signature=None)

    async def fake_finalize(**_kwargs):
        return False

    async def fake_broadcast(_agent_id, _session_id, event):
        broadcasts.append(event)

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_load_runtime_context", fake_load_context)
    monkeypatch.setattr(runtime, "_maybe_handle_plan_mode_entry", noop_async)
    monkeypatch.setattr(runtime, "invoke_agent", fake_invoke)
    monkeypatch.setattr(runtime, "_finalize_web_chat_run_with_assistant", fake_finalize)
    monkeypatch.setattr(runtime, "_persist_runtime_event", noop_async)
    monkeypatch.setattr(runtime, "_persist_tool_call", noop_async)
    monkeypatch.setattr(runtime, "_update_runtime_task", noop_async)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)
    monkeypatch.setattr(runtime, "_resume_queued_plan_handoffs", noop_async)
    monkeypatch.setattr(runtime, "_deliver_run_result_to_channel", noop_async)

    await runtime.execute_web_chat_run(run_id)

    assert all(event.get("type") != "done" for event in broadcasts)


@pytest.mark.asyncio
async def test_execute_web_chat_run_finalizes_blocking_clarification_without_empty_assistant(monkeypatch):
    import json

    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    runtime_task = SimpleNamespace(
        id=run_id,
        parent_agent_id=agent_id,
        parent_session_id=session_id,
        prompt="Create an RWA monitoring agent",
        metadata_json={"user_id": str(user_id), "session_id": session_id},
        trace_id=None,
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="RWA Researcher",
        role_description="",
        primary_model_id=uuid4(),
        fallback_model_id=None,
        tenant_id=uuid4(),
        agent_type="native",
    )
    user = SimpleNamespace(id=user_id, display_name="Rocky", username="rocky")
    llm_model = SimpleNamespace(provider="openai", model="gpt-4.1", supports_vision=False)
    broadcasts: list[dict] = []
    persisted_tools: list[dict] = []
    ordering: list[str] = []
    finalized_without_assistant: list[dict] = []
    delivered_channel_replies: list[str] = []

    async def fake_load_context(_run_uuid):
        return runtime_task, agent, user, llm_model, None, []

    async def fake_invoke(request):
        await request.on_tool_call(
            {
                "name": "ask_user_question",
                "args": {
                    "questions": [
                        {
                            "question": "How often should the RWA report run?",
                            "options": [{"label": "Weekly", "description": "Weekly report"}],
                        }
                    ]
                },
                "status": "done",
                "result": json.dumps(
                    {
                        "status": "awaiting_user_clarification",
                        "blocking": True,
                        "questions": [
                            {
                                "question": "How often should the RWA report run?",
                                "options": [{"label": "Weekly", "description": "Weekly report"}],
                            }
                        ],
                    }
                ),
            }
        )
        return SimpleNamespace(content="", reasoning_signature=None)

    async def fail_empty_assistant_finalize(**kwargs):
        raise AssertionError(f"blocking clarification must not persist assistant content={kwargs.get('content')!r}")

    async def fake_finalize_without_assistant(**kwargs):
        finalized_without_assistant.append(kwargs)
        return True

    async def fake_broadcast(_agent_id, _session_id, event):
        broadcasts.append(event)
        if event.get("type") == "tool_call":
            ordering.append(f"broadcast:{event.get('name')}")

    async def fake_persist_tool_call(**kwargs):
        persisted_tools.append(kwargs["data"])
        ordering.append(f"persist:{kwargs['data'].get('name')}")

    async def fake_deliver(_agent_id, _session_id, content):
        delivered_channel_replies.append(content)

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_load_runtime_context", fake_load_context)
    monkeypatch.setattr(runtime, "_maybe_handle_plan_mode_entry", noop_async)
    monkeypatch.setattr(runtime, "_claim_pending_reply_suffix_for_session", noop_async)
    monkeypatch.setattr(runtime, "invoke_agent", fake_invoke)
    monkeypatch.setattr(runtime, "_finalize_web_chat_run_with_assistant", fail_empty_assistant_finalize)
    monkeypatch.setattr(runtime, "_finalize_web_chat_run_without_assistant", fake_finalize_without_assistant, raising=False)
    monkeypatch.setattr(runtime, "_persist_runtime_event", noop_async)
    monkeypatch.setattr(runtime, "_persist_tool_call", fake_persist_tool_call)
    monkeypatch.setattr(runtime, "_update_runtime_task", noop_async)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)
    monkeypatch.setattr(runtime, "_resume_queued_plan_handoffs", noop_async)
    monkeypatch.setattr(runtime, "_deliver_run_result_to_channel", fake_deliver)

    await runtime.execute_web_chat_run(run_id)

    assert [tool["name"] for tool in persisted_tools] == ["ask_user_question"]
    assert ordering.index("persist:ask_user_question") < ordering.index("broadcast:ask_user_question")
    assert len(finalized_without_assistant) == 1
    assert finalized_without_assistant[0]["status"] == "completed"
    assert finalized_without_assistant[0]["result_summary"] == "awaiting_user_clarification"
    assert any(event.get("type") == "done" and event.get("content") == "" for event in broadcasts)
    assert delivered_channel_replies == []


@pytest.mark.asyncio
async def test_execute_web_chat_run_interrupts_kernel_after_terminal_tool_card(monkeypatch):
    import json

    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    runtime_task = SimpleNamespace(
        id=run_id,
        parent_agent_id=agent_id,
        parent_session_id=session_id,
        prompt="Create an RWA employee",
        metadata_json={"user_id": str(user_id), "session_id": session_id},
        trace_id=None,
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="HR",
        role_description="",
        primary_model_id=uuid4(),
        fallback_model_id=None,
        tenant_id=uuid4(),
        agent_type="native",
    )
    user = SimpleNamespace(id=user_id, display_name="Rocky", username="rocky")
    llm_model = SimpleNamespace(provider="openai", model="gpt-4.1", supports_vision=False)
    broadcasts: list[dict] = []
    finalized_without_assistant: list[dict] = []

    async def fake_load_context(_run_uuid):
        return runtime_task, agent, user, llm_model, None, []

    async def fake_invoke(request):
        await request.on_tool_call(
            {
                "name": "ask_user_question",
                "args": {"questions": [{"question": "Cadence?", "options": [{"label": "Weekly"}]}]},
                "status": "done",
                "result": json.dumps(
                    {
                        "status": "awaiting_user_clarification",
                        "blocking": True,
                        "questions": [{"question": "Cadence?", "options": [{"label": "Weekly"}]}],
                    }
                ),
            }
        )
        raise AssertionError("kernel must be interrupted after a terminal tool card")

    async def fail_assistant_finalize(**kwargs):
        raise AssertionError(f"terminal tool card must not persist assistant content={kwargs.get('content')!r}")

    async def fake_finalize_without_assistant(**kwargs):
        finalized_without_assistant.append(kwargs)
        return True

    async def fake_broadcast(_agent_id, _session_id, event):
        broadcasts.append(event)

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_load_runtime_context", fake_load_context)
    monkeypatch.setattr(runtime, "_maybe_handle_plan_mode_entry", noop_async)
    monkeypatch.setattr(runtime, "invoke_agent", fake_invoke)
    monkeypatch.setattr(runtime, "_finalize_web_chat_run_with_assistant", fail_assistant_finalize)
    monkeypatch.setattr(runtime, "_finalize_web_chat_run_without_assistant", fake_finalize_without_assistant, raising=False)
    monkeypatch.setattr(runtime, "_persist_runtime_event", noop_async)
    monkeypatch.setattr(runtime, "_persist_tool_call", noop_async)
    monkeypatch.setattr(runtime, "_update_runtime_task", noop_async)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)
    monkeypatch.setattr(runtime, "_resume_queued_plan_handoffs", noop_async)
    monkeypatch.setattr(runtime, "_deliver_run_result_to_channel", noop_async)

    await runtime.execute_web_chat_run(run_id)

    assert len(finalized_without_assistant) == 1
    assert finalized_without_assistant[0]["status"] == "completed"
    assert finalized_without_assistant[0]["result_summary"] == "awaiting_user_clarification"
    assert any(event.get("type") == "done" and event.get("content") == "" for event in broadcasts)


@pytest.mark.asyncio
async def test_execute_web_chat_run_stops_after_create_employee_success_card(monkeypatch):
    import json

    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    created_agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    runtime_task = SimpleNamespace(
        id=run_id,
        parent_agent_id=agent_id,
        parent_session_id=session_id,
        prompt="Create the employee",
        metadata_json={"user_id": str(user_id), "session_id": session_id},
        trace_id=None,
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="HR",
        role_description="",
        primary_model_id=uuid4(),
        fallback_model_id=None,
        tenant_id=uuid4(),
        agent_type="native",
    )
    user = SimpleNamespace(id=user_id, display_name="Rocky", username="rocky")
    llm_model = SimpleNamespace(provider="openai", model="gpt-4.1", supports_vision=False)
    broadcasts: list[dict] = []
    finalized_without_assistant: list[dict] = []

    async def fake_load_context(_run_uuid):
        return runtime_task, agent, user, llm_model, None, []

    async def fake_invoke(request):
        await request.on_tool_call(
            {
                "name": "create_digital_employee",
                "args": {"name": "RWA项目与营销专员"},
                "status": "done",
                "result": json.dumps(
                    {
                        "status": "success",
                        "agent_id": str(created_agent_id),
                        "agent_name": "RWA项目与营销专员",
                        "message": "Successfully created digital employee.",
                    }
                ),
            }
        )
        raise AssertionError("kernel must stop after a successful create_digital_employee card")

    async def fail_assistant_finalize(**kwargs):
        raise AssertionError(f"create employee success must not wait for assistant content={kwargs.get('content')!r}")

    async def fake_finalize_without_assistant(**kwargs):
        finalized_without_assistant.append(kwargs)
        return True

    async def fake_broadcast(_agent_id, _session_id, event):
        broadcasts.append(event)

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_load_runtime_context", fake_load_context)
    monkeypatch.setattr(runtime, "_maybe_handle_plan_mode_entry", noop_async)
    monkeypatch.setattr(runtime, "invoke_agent", fake_invoke)
    monkeypatch.setattr(runtime, "_finalize_web_chat_run_with_assistant", fail_assistant_finalize)
    monkeypatch.setattr(runtime, "_finalize_web_chat_run_without_assistant", fake_finalize_without_assistant, raising=False)
    monkeypatch.setattr(runtime, "_persist_runtime_event", noop_async)
    monkeypatch.setattr(runtime, "_persist_tool_call", noop_async)
    monkeypatch.setattr(runtime, "_update_runtime_task", noop_async)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)
    monkeypatch.setattr(runtime, "_resume_queued_plan_handoffs", noop_async)
    monkeypatch.setattr(runtime, "_deliver_run_result_to_channel", noop_async)

    await runtime.execute_web_chat_run(run_id)

    assert len(finalized_without_assistant) == 1
    assert finalized_without_assistant[0]["status"] == "completed"
    assert finalized_without_assistant[0]["result_summary"] == "create_digital_employee_success"
    assert any(event.get("type") == "done" and event.get("content") == "" for event in broadcasts)


@pytest.mark.asyncio
async def test_start_web_chat_run_creates_runtime_task_and_user_message(monkeypatch, tmp_path):
    import app.services.web_chat_runtime as runtime
    from app.memory.t0.ledger import replay_t0_session_events
    from app.models.audit import ChatMessage
    from app.models.runtime_task import RuntimeTask

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, name="Agent", tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, username="rocky", display_name="Rocky")
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=user_id,
        title="Session 05-21",
        last_message_at=None,
    )
    db = _FakeDB(active_run=None)
    scheduled = []

    def fake_create_task(coro):
        scheduled.append(coro)
        coro.close()
        return SimpleNamespace(done=lambda: False, add_done_callback=lambda _cb: None)

    monkeypatch.setattr(runtime.asyncio, "create_task", fake_create_task)

    async def fake_broadcast(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    result = await runtime.start_web_chat_run(
        db=db,
        agent=agent,
        user=user,
        session=session,
        content="请规划一个长任务",
        display_content="请规划一个长任务",
        file_name="",
    )

    assert result["run_id"]
    assert result["status"] == "running"
    assert any(isinstance(item, ChatMessage) and item.role == "user" for item in db.added)
    task = next(item for item in db.added if isinstance(item, RuntimeTask))
    assert task.task_type == "web_chat_turn"
    assert task.parent_agent_id == agent_id
    assert task.child_agent_id == agent_id
    assert task.tenant_id == agent.tenant_id
    assert task.parent_session_id == str(session_id)
    assert task.child_session_id == str(session_id)
    assert task.metadata_json["user_id"] == str(user_id)
    assert task.metadata_json["runtime_task_id"] == task.id.hex
    assert task.metadata_json["request_id"] == str(task.id)
    assert task.metadata_json["trace_id"] == task.trace_id
    assert db.commits == 1
    assert scheduled
    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert [(event.sequence, event.event_type, event.role, event.content) for event in events] == [
        (1, "user_message", "user", "请规划一个长任务")
    ]
    assert events[0].message_id
    assert events[0].metadata["source"] == "web"


@pytest.mark.asyncio
async def test_start_web_chat_run_flushes_runtime_task_before_transcript_event(monkeypatch):
    import app.services.web_chat_runtime as runtime
    from app.models.runtime_task import RuntimeTask

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, name="Agent", tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, username="rocky", display_name="Rocky")
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=user_id,
        title="Session 05-21",
        last_message_at=None,
    )

    class _FlushTrackingDB(_FakeDB):
        def __init__(self):
            super().__init__(active_run=None)
            self.runtime_task_flushed = False

        async def flush(self):
            self.runtime_task_flushed = any(isinstance(item, RuntimeTask) for item in self.added)

    db = _FlushTrackingDB()
    append_calls = []
    scheduled = []

    async def fake_append_session_event(**kwargs):
        assert db.runtime_task_flushed is True
        append_calls.append(kwargs)

    def fake_create_task(coro):
        scheduled.append(coro)
        coro.close()
        return SimpleNamespace(done=lambda: False, add_done_callback=lambda _cb: None)

    async def fake_broadcast(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "append_session_event", fake_append_session_event)
    monkeypatch.setattr(runtime.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)

    await runtime.start_web_chat_run(
        db=db,
        agent=agent,
        user=user,
        session=session,
        content="hello",
    )

    assert append_calls
    assert scheduled


@pytest.mark.asyncio
async def test_start_channel_chat_run_from_saved_turn_creates_runtime_task_without_duplicate_user_message(
    monkeypatch, tmp_path
):
    import app.services.web_chat_runtime as runtime
    from app.memory.t0.ledger import replay_t0_session_events
    from app.models.audit import ChatMessage
    from app.models.runtime_task import RuntimeTask

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, name="Agent", tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, username="feishu_u", display_name="Feishu User")
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=user_id,
        title="Feishu Session",
        last_message_at=None,
        delivery_target_json={"channel": "feishu", "receive_id": "ou_1"},
    )
    db = _FakeDB(active_run=None)
    scheduled = []

    def fake_create_task(coro):
        scheduled.append(coro)
        coro.close()
        return SimpleNamespace(done=lambda: False, add_done_callback=lambda _cb: None)

    monkeypatch.setattr(runtime.asyncio, "create_task", fake_create_task)

    async def fake_broadcast(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    result = await runtime.start_channel_chat_run_from_saved_turn(
        db=db,
        agent=agent,
        user=user,
        session=session,
        content="处理这条飞书消息",
        source_channel="feishu",
    )

    assert result["run_id"]
    assert result["status"] == "running"
    assert not any(isinstance(item, ChatMessage) for item in db.added)
    task = next(item for item in db.added if isinstance(item, RuntimeTask))
    assert task.task_type == "web_chat_turn"
    assert task.tenant_id == agent.tenant_id
    assert task.parent_session_id == str(session_id)
    assert task.prompt == "处理这条飞书消息"
    assert task.metadata_json["runtime_task_id"] == task.id.hex
    assert task.metadata_json["request_id"] == str(task.id)
    assert task.metadata_json["trace_id"] == task.trace_id
    assert task.metadata_json["source"] == "feishu"
    assert task.metadata_json["channel"] == "feishu"
    assert task.metadata_json["delivery_target_json"] == session.delivery_target_json
    assert scheduled
    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert [(event.event_type, event.role, event.content) for event in events] == [
        ("user_message", "user", "处理这条飞书消息")
    ]
    assert events[0].metadata["existing_user_message_saved"] is True


@pytest.mark.asyncio
async def test_start_channel_chat_run_from_saved_turn_flushes_runtime_task_before_transcript_event(monkeypatch):
    import app.services.web_chat_runtime as runtime
    from app.models.runtime_task import RuntimeTask

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    agent = SimpleNamespace(id=agent_id, name="Agent", tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, username="feishu_u", display_name="Feishu User")
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=user_id,
        title="Feishu Session",
        last_message_at=None,
        delivery_target_json={"channel": "feishu", "receive_id": "ou_1"},
    )

    class _FlushTrackingDB(_FakeDB):
        def __init__(self):
            super().__init__(active_run=None)
            self.runtime_task_flushed = False

        async def flush(self):
            self.runtime_task_flushed = any(isinstance(item, RuntimeTask) for item in self.added)

    db = _FlushTrackingDB()
    append_calls = []
    scheduled = []

    async def fake_append_session_event(**kwargs):
        assert db.runtime_task_flushed is True
        append_calls.append(kwargs)

    def fake_create_task(coro):
        scheduled.append(coro)
        coro.close()
        return SimpleNamespace(done=lambda: False, add_done_callback=lambda _cb: None)

    async def fake_broadcast(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "append_session_event", fake_append_session_event)
    monkeypatch.setattr(runtime.asyncio, "create_task", fake_create_task)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)

    await runtime.start_channel_chat_run_from_saved_turn(
        db=db,
        agent=agent,
        user=user,
        session=session,
        content="处理这条飞书消息",
        source_channel="feishu",
    )

    assert append_calls
    assert scheduled


@pytest.mark.asyncio
async def test_start_web_chat_run_queues_user_message_when_run_is_active(monkeypatch, tmp_path):
    import app.services.web_chat_runtime as runtime
    from app.memory.t0.ledger import replay_t0_session_events
    from app.models.audit import ChatMessage

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    existing_run_id = uuid4()
    agent = SimpleNamespace(id=agent_id, name="Agent", tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, username="rocky", display_name="Rocky")
    session = SimpleNamespace(id=session_id, agent_id=agent_id, user_id=user_id)
    active_run = SimpleNamespace(
        id=existing_run_id,
        status="running",
        created_at=None,
        started_at=None,
        completed_at=None,
        result_summary=None,
        metadata_json={},
    )
    db = _FakeDB(active_run=active_run)
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    with pytest.raises(runtime.ActiveWebChatRunExists) as exc_info:
        await runtime.start_web_chat_run(
            db=db,
            agent=agent,
            user=user,
            session=session,
            content="second message",
        )

    assert exc_info.value.run["run_id"] == existing_run_id.hex
    assert exc_info.value.run["status"] == "running"
    assert exc_info.value.run["queued_user_message"]["content"] == "second message"
    assert any(isinstance(item, ChatMessage) and item.role == "user" for item in db.added)
    assert active_run.metadata_json["pending_user_messages"][0]["content"] == "second message"
    assert db.commits == 1
    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert [(event.event_type, event.role, event.content) for event in events] == [
        ("user_message", "user", "second message")
    ]
    assert events[0].runtime_task_id == existing_run_id.hex


@pytest.mark.asyncio
async def test_persist_tool_call_appends_t0_tool_result(monkeypatch, tmp_path):
    import app.services.tenant_resolver as tenant_resolver
    import app.services.web_chat_runtime as runtime
    from app.memory.t0.ledger import replay_t0_session_events

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    added = []

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def add(self, value):
            added.append(value)

        async def commit(self):
            return None

    async def fake_resolve_tenant_for_agent(_agent_id):
        return tenant_id

    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda _tenant_id: _Session())
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    await runtime._persist_tool_call(
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        data={"name": "read_file", "args": {"path": "workspace/a.md"}, "result": "file content"},
    )

    assert added[0].role == "tool_call"
    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert [(event.event_type, event.role) for event in events] == [("tool_result", "tool")]
    assert events[0].metadata["tool_name"] == "read_file"
    assert events[0].metadata["status"] == "done"
    assert '"result": "file content"' in events[0].content


@pytest.mark.asyncio
async def test_persist_tool_call_appends_running_step_contract(monkeypatch, tmp_path):
    import app.services.tenant_resolver as tenant_resolver
    import app.services.web_chat_runtime as runtime
    from app.memory.t0.ledger import replay_t0_session_events

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    run_id = uuid4()
    added = []

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def add(self, value):
            added.append(value)

        async def commit(self):
            return None

    async def fake_resolve_tenant_for_agent(_agent_id):
        return tenant_id

    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda _tenant_id: _Session())
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    persisted = await runtime._persist_tool_call(
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        data={
            "id": "toolu_123",
            "name": "read_file",
            "args": {"path": "workspace/a.md"},
            "status": "running",
            "runtime_task_id": str(run_id),
            "started_at": "2026-06-22T10:00:00Z",
        },
    )

    assert persisted is not None
    assert added[0].role == "tool_call"
    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert [(event.event_type, event.role) for event in events] == [("tool_call", "tool")]
    assert events[0].runtime_task_id == run_id.hex
    assert events[0].metadata["tool_name"] == "read_file"
    assert events[0].metadata["status"] == "running"
    assert events[0].metadata["tool_call_id"] == "toolu_123"
    assert events[0].metadata["step_id"] == "tool:toolu_123"
    assert events[0].metadata["visibility"] == "collapsed"
    assert '"status": "running"' in events[0].content
    assert '"tool_call_id": "toolu_123"' in events[0].content
    assert '"step_id": "tool:toolu_123"' in events[0].content
    assert '"duration_ms"' not in events[0].content


@pytest.mark.asyncio
async def test_persist_tool_call_done_contract_includes_stable_ids_and_duration(monkeypatch, tmp_path):
    import app.services.tenant_resolver as tenant_resolver
    import app.services.web_chat_runtime as runtime
    from app.memory.t0.ledger import replay_t0_session_events

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    run_id = uuid4()

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        def add(self, _value):
            return None

        async def commit(self):
            return None

    async def fake_resolve_tenant_for_agent(_agent_id):
        return tenant_id

    monkeypatch.setattr(tenant_resolver, "resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda _tenant_id: _Session())
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    await runtime._persist_tool_call(
        agent_id=agent_id,
        user_id=user_id,
        session_id=session_id,
        data={
            "id": "toolu_123",
            "name": "read_file",
            "args": {"path": "workspace/a.md"},
            "status": "done",
            "result": "file content",
            "runtime_task_id": str(run_id),
            "started_at": "2026-06-22T10:00:00Z",
            "completed_at": "2026-06-22T10:00:02.500Z",
        },
    )

    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert [(event.event_type, event.role) for event in events] == [("tool_result", "tool")]
    assert events[0].metadata["tool_call_id"] == "toolu_123"
    assert events[0].metadata["step_id"] == "tool:toolu_123"
    assert events[0].metadata["duration_ms"] == 2500
    assert events[0].metadata["visibility"] == "collapsed"
    assert '"duration_ms": 2500' in events[0].content
    assert '"result": "file content"' in events[0].content


@pytest.mark.asyncio
async def test_start_web_chat_run_queues_when_active_run_unique_index_conflicts(monkeypatch, tmp_path):
    import app.services.web_chat_runtime as runtime
    from app.memory.t0.ledger import replay_t0_session_events
    from app.models.audit import ChatMessage
    from app.models.runtime_task import RuntimeTask
    from sqlalchemy.exc import IntegrityError

    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4()
    existing_run_id = uuid4()
    agent = SimpleNamespace(id=agent_id, name="Agent", tenant_id=uuid4())
    user = SimpleNamespace(id=user_id, username="rocky", display_name="Rocky")
    session = SimpleNamespace(
        id=session_id,
        agent_id=agent_id,
        user_id=user_id,
        title="Session 06-12",
        last_message_at=None,
    )
    active_run = SimpleNamespace(
        id=existing_run_id,
        status="running",
        created_at=None,
        started_at=None,
        completed_at=None,
        result_summary=None,
        metadata_json={},
    )

    class _Orig:
        diag = SimpleNamespace(constraint_name="uq_runtime_tasks_active_web_chat_session")

    class _ConflictThenActiveDB(_FakeDB):
        def __init__(self):
            super().__init__(active_run=None)
            self.execute_calls = 0
            self.rollbacks = 0

        async def execute(self, _stmt):
            self.execute_calls += 1
            return _ScalarResult(None if self.execute_calls == 1 else active_run)

        async def commit(self):
            self.commits += 1
            if self.commits == 1:
                raise IntegrityError("insert runtime_tasks", {}, _Orig())

        async def rollback(self):
            self.rollbacks += 1
            self.added.clear()

    db = _ConflictThenActiveDB()
    broadcasts = []

    async def fake_broadcast(_agent_id, _session_id, event):
        broadcasts.append(event)

    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)
    monkeypatch.setattr("app.memory.t0.ledger.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    with pytest.raises(runtime.ActiveWebChatRunExists) as exc_info:
        await runtime.start_web_chat_run(
            db=db,
            agent=agent,
            user=user,
            session=session,
            content="race message",
        )

    assert exc_info.value.run["run_id"] == existing_run_id.hex
    assert exc_info.value.run["status"] == "running"
    assert exc_info.value.run["queued_user_message"]["content"] == "race message"
    assert db.rollbacks == 1
    assert db.commits == 2
    assert not any(isinstance(item, ChatMessage) for item in db.added)
    assert not any(isinstance(item, RuntimeTask) for item in db.added)
    assert active_run.metadata_json["pending_user_messages"][0]["content"] == "race message"
    assert broadcasts[-1]["type"] == "user_message_queued"
    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)
    assert [(event.event_type, event.role, event.content) for event in events] == [
        ("user_message", "user", "race message")
    ]


@pytest.mark.asyncio
async def test_resume_queued_plan_handoffs_restarts_oldest_confirmed_plan(monkeypatch):
    """A queued Plan Mode handoff must be resumable after the current run exits.

    Returning ``handoff_status='queued'`` from the handoff handler is only honest
    if the web-chat runtime has a recovery hook that calls the handoff again once
    the active run is no longer active.
    """
    import app.services.web_chat_runtime as runtime
    from sqlalchemy.dialects import postgresql

    agent_id = uuid4()
    session_id = "sess-1"
    plan_id = uuid4()
    active_run_id = uuid4().hex

    class _QueuedResult:
        def scalars(self):
            return SimpleNamespace(all=lambda: [plan_id])

    class _QueuedDB:
        async def execute(self, _stmt):
            compiled = _stmt.compile(dialect=postgresql.dialect())
            assert "handoff_payload" in str(compiled)
            assert "active_run_id" in {str(value) for value in compiled.params.values()}
            assert active_run_id in {str(value) for value in compiled.params.values()}
            return _QueuedResult()

    class _SessionFactory:
        async def __aenter__(self):
            return _QueuedDB()

        async def __aexit__(self, exc_type, exc, tb):
            return False

    calls = []

    class _Service:
        async def handoff_confirmed_plan(self, *, plan_id):
            calls.append(plan_id)
            return SimpleNamespace(id=plan_id, handoff_status="completed")

    # RLS stage-2a: the scan now runs under tenant_scoped_session after resolving
    # the agent's tenant (audited bypass). Route both to fakes — the assertions on
    # the queued-handoff statement are unchanged.
    async def _fake_resolve_tenant(*_a, **_k):
        return uuid4()

    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda *a, **k: _SessionFactory())
    monkeypatch.setattr("app.services.tenant_resolver.resolve_tenant_for_agent", _fake_resolve_tenant)
    monkeypatch.setattr("app.services.plan_mode_service.get_plan_mode_service", lambda: _Service())

    resumed = await runtime._resume_queued_plan_handoffs(
        agent_id=agent_id,
        session_id=session_id,
        completed_run_id=active_run_id,
    )

    assert calls == [plan_id]
    assert resumed == [str(plan_id)]


@pytest.mark.asyncio
async def test_execute_web_chat_run_resumes_queued_plan_handoffs_on_terminal_exit(monkeypatch):
    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    session_id = "sess-1"
    runtime_task = SimpleNamespace(
        id=run_id,
        parent_agent_id=agent_id,
        parent_session_id=session_id,
        prompt="hello",
        metadata_json={"user_id": str(user_id), "session_id": session_id},
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="Agent",
        role_description="",
        primary_model_id=None,
        fallback_model_id=None,
        tenant_id=uuid4(),
        agent_type="standard",
    )
    user = SimpleNamespace(id=user_id)
    resumed = []

    async def fake_load_context(_run_uuid):
        return runtime_task, agent, user, None, None, []

    async def fake_resume(**kwargs):
        resumed.append(kwargs)
        return ["plan-1"]

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_load_runtime_context", fake_load_context)
    monkeypatch.setattr(runtime, "_maybe_handle_plan_mode_entry", noop_async)
    monkeypatch.setattr(runtime, "_persist_assistant_message", noop_async)
    monkeypatch.setattr(runtime, "_update_runtime_task", noop_async)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", noop_async)
    monkeypatch.setattr(runtime, "_resume_queued_plan_handoffs", fake_resume)

    await runtime.execute_web_chat_run(run_id)

    assert resumed == [{"agent_id": agent_id, "session_id": session_id, "completed_run_id": run_id.hex}]


@pytest.mark.asyncio
async def test_resume_persisted_web_chat_runs_schedules_running_turns_with_resume_context(monkeypatch):
    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    task = SimpleNamespace(
        id=run_id,
        task_type="web_chat_turn",
        status="running",
        parent_agent_id=agent_id,
        metadata_json={},
    )

    class _Rows:
        def scalars(self):
            return SimpleNamespace(all=lambda: [task])

    class _DB:
        def __init__(self):
            self.calls = 0
            self.commits = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def execute(self, _stmt):
            self.calls += 1
            return _Rows() if self.calls <= 2 else _ScalarResult(None)

        async def commit(self):
            self.commits += 1

    scheduled: list[str] = []

    def fake_create_task(coro, *args, **kwargs):
        scheduled.append(coro.cr_frame.f_locals["run_id"].hex)
        coro.close()
        return SimpleNamespace(add_done_callback=lambda _cb: None)

    monkeypatch.setattr(runtime, "_async_session", lambda: _DB())
    monkeypatch.setattr(runtime, "build_long_task_resume_context", lambda **_kwargs: {"resume_prompt": "resume now"})
    monkeypatch.setattr(runtime.asyncio, "create_task", fake_create_task)

    resumed = await runtime.resume_persisted_web_chat_runs(limit=10)

    assert resumed == [run_id.hex]
    assert scheduled == [run_id.hex]
    assert task.metadata_json["resumed_after_restart"] is True
    assert task.metadata_json["restart_resume_context"]["resume_prompt"] == "resume now"


@pytest.mark.asyncio
async def test_get_active_web_chat_run_reconciles_terminal_transcript_ghost():
    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    session_id = uuid4()
    task = SimpleNamespace(
        id=run_id,
        task_type="web_chat_turn",
        status="running",
        parent_agent_id=agent_id,
        parent_session_id=str(session_id),
        created_at=None,
        started_at=None,
        completed_at=None,
        result_summary=None,
        metadata_json={},
    )
    terminal_event = SimpleNamespace(
        event_type="assistant_message",
        content="final answer",
        metadata_json={"status": "completed"},
    )

    class _DB:
        def __init__(self):
            self.calls = 0
            self.commits = 0

        async def execute(self, _stmt):
            self.calls += 1
            return _ScalarResult(task if self.calls == 1 else terminal_event)

        async def commit(self):
            self.commits += 1

    db = _DB()

    active = await runtime.get_active_web_chat_run(db=db, agent_id=agent_id, session_id=session_id)

    assert active is None
    assert task.status == "completed"
    assert task.result_summary == "final answer"
    assert task.completed_at is not None
    assert db.commits == 1


@pytest.mark.asyncio
async def test_resume_persisted_web_chat_runs_skips_terminal_transcript_ghost(monkeypatch):
    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    session_id = uuid4()
    task = SimpleNamespace(
        id=run_id,
        task_type="web_chat_turn",
        status="running",
        parent_agent_id=agent_id,
        parent_session_id=str(session_id),
        created_at=None,
        started_at=None,
        completed_at=None,
        result_summary=None,
        metadata_json={},
    )
    terminal_event = SimpleNamespace(
        event_type="assistant_message",
        content="final answer",
        metadata_json={"status": "completed"},
    )

    class _Rows:
        def scalars(self):
            return SimpleNamespace(all=lambda: [task])

    class _DB:
        def __init__(self):
            self.calls = 0
            self.commits = 0

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def execute(self, _stmt):
            self.calls += 1
            return _Rows() if self.calls <= 2 else _ScalarResult(terminal_event)

        async def commit(self):
            self.commits += 1

    scheduled: list[str] = []

    def fake_create_task(coro, *args, **kwargs):
        scheduled.append(coro.cr_frame.f_locals["run_id"].hex)
        coro.close()
        return SimpleNamespace(add_done_callback=lambda _cb: None)

    monkeypatch.setattr(runtime, "_async_session", lambda: _DB())
    monkeypatch.setattr(runtime.asyncio, "create_task", fake_create_task)

    resumed = await runtime.resume_persisted_web_chat_runs(limit=10)

    assert resumed == []
    assert scheduled == []
    assert task.status == "completed"
    assert task.result_summary == "final answer"
    assert task.completed_at is not None


@pytest.mark.asyncio
async def test_execute_web_chat_run_injects_restart_resume_context(monkeypatch):
    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    session_id = "sess-resume"
    runtime_task = SimpleNamespace(
        id=run_id,
        parent_agent_id=agent_id,
        parent_session_id=session_id,
        prompt="continue",
        metadata_json={
            "user_id": str(user_id),
            "session_id": session_id,
            "restart_resume_context": {"resume_prompt": "Resume long task with artifact refs."},
        },
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="Agent",
        role_description="",
        primary_model_id=uuid4(),
        fallback_model_id=None,
        tenant_id=uuid4(),
        agent_type="standard",
    )
    user = SimpleNamespace(id=user_id, display_name="Rocky", username="rocky")
    llm_model = SimpleNamespace(provider="openai", model="gpt-4.1", supports_vision=False)
    captured = {}

    async def fake_load_context(_run_uuid):
        return runtime_task, agent, user, llm_model, None, []

    async def fake_invoke(request):
        captured["system_prompt_suffix"] = request.system_prompt_suffix
        return SimpleNamespace(content="done")

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_load_runtime_context", fake_load_context)
    monkeypatch.setattr(runtime, "_maybe_handle_plan_mode_entry", noop_async)
    monkeypatch.setattr(runtime, "invoke_agent", fake_invoke)
    monkeypatch.setattr(runtime, "_persist_assistant_message", noop_async)
    monkeypatch.setattr(runtime, "_persist_runtime_event", noop_async)
    monkeypatch.setattr(runtime, "_persist_tool_call", noop_async)
    monkeypatch.setattr(runtime, "_update_runtime_task", noop_async)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", noop_async)
    monkeypatch.setattr(runtime, "_resume_queued_plan_handoffs", noop_async)
    monkeypatch.setattr(runtime, "_deliver_run_result_to_channel", noop_async)

    await runtime.execute_web_chat_run(run_id)

    assert "Resume long task with artifact refs." in captured["system_prompt_suffix"]


# ---------------------------------------------------------------------------
# Auto-sync gate (§9.0 task auto-sync / §9.2): a regex-detected "create a task"
# must NOT silently background-execute; without a confirmed plan it creates an
# awaiting PlanRequest and tells the user to confirm.
# ---------------------------------------------------------------------------


class _RecommendationSession:
    def __init__(self, recommendation):
        self.recommendation = recommendation
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    async def execute(self, _stmt):
        return _ScalarResult(self.recommendation)

    async def commit(self):
        self.commits += 1


@pytest.mark.asyncio
async def test_plan_mode_accepts_latest_recommendation_and_activates_interactive_mode(monkeypatch):
    import app.services.web_chat_runtime as runtime

    agent_id = uuid4()
    user_id = uuid4()
    recommendation = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        session_id="sess-accept",
        runtime_task_id=uuid4(),
        recommended_to_user_id=user_id,
        status="recommended",
        original_request="每天 13:00 自动检查 Reddit 帖子并总结投资观点",
        title="每天 13:00 自动检查 Reddit 帖子",
        intent_type="autonomous_wake",
        action_kind="create_enabled_trigger",
        tool_name="set_trigger",
        accepted_by_user_id=None,
        accepted_at=None,
    )
    recommendation_db = _RecommendationSession(recommendation)
    monkeypatch.setattr(runtime, "_async_session", lambda: recommendation_db)
    # RLS 阶段2a/2b: _accept_latest_plan_mode_recommendation resolves the agent's
    # tenant and opens a tenant-scoped session. Route it through the same fake DB
    # and stub the resolver so no real DB / bypass read happens.
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda *_a, **_k: recommendation_db)

    async def _fake_resolve(*_a, **_k):
        return uuid4()

    monkeypatch.setattr("app.services.tenant_resolver.resolve_tenant_for_agent", _fake_resolve)

    session_context = SimpleNamespace(metadata={})
    response = await runtime._maybe_handle_plan_mode_entry(
        agent_id=agent_id,
        user_id=user_id,
        session_id="sess-accept",
        content="进入计划模式",
        runtime_session_context=session_context,
    )

    assert response is None
    assert recommendation.status == "accepted"
    assert recommendation.accepted_by_user_id == user_id
    metadata = session_context.metadata["plan_mode"]
    assert metadata["original_request"] == recommendation.original_request
    assert metadata["intent_type"] == "autonomous_wake"
    assert metadata["action_kind"] == "create_enabled_trigger"
    assert metadata["tool_name"] == "set_trigger"


@pytest.mark.asyncio
async def test_maybe_handle_plan_mode_entry_does_not_pre_empt_schedule_intent():
    """P0-5: schedule wording no longer pre-empts the turn with a canned template.
    classify returns 'none' and the entry handler falls through (returns None) so
    the agent handles the message and suggests Plan Mode in its own reply."""
    import app.services.web_chat_runtime as runtime

    result = await runtime._maybe_handle_plan_mode_entry(
        agent_id=uuid4(),
        user_id=uuid4(),
        session_id="session-1",
        content="每天 9 点帮我整理新闻",
        plan_mode_requested=False,
    )

    assert result is None  # no canned recommendation; falls through to the agent


@pytest.mark.asyncio
async def test_maybe_handle_plan_mode_entry_activates_interactive_mode_when_explicitly_requested(monkeypatch):
    import app.services.web_chat_runtime as runtime

    session_context = SimpleNamespace(metadata={})

    result = await runtime._maybe_handle_plan_mode_entry(
        agent_id=uuid4(),
        user_id=uuid4(),
        session_id="session-1",
        content="帮我完整调研这个行业",
        plan_mode_requested=True,
        runtime_session_context=session_context,
    )

    assert result is None
    assert session_context.metadata["plan_mode"]["active"] is True
    assert session_context.metadata["plan_mode"]["original_request"] == "帮我完整调研这个行业"
    assert session_context.metadata["plan_mode"]["intent_type"] == "in_session_execution"
    assert session_context.metadata["plan_mode"]["action_kind"] == "start_long_task"
    # CC-align §4.2: a normal live-chat plan defaults to continuing in THIS session
    # after confirmation — NOT the old detached ``long_task`` (which had no handler
    # and resolved to skipped). intent_type stays long_task; only the target moved.
    assert session_context.metadata["plan_mode"]["handoff_target"] == "continue_current_session"


@pytest.mark.asyncio
async def test_maybe_handle_plan_mode_entry_classifies_visible_request_not_attachment_body(monkeypatch):
    import app.services.web_chat_runtime as runtime

    session_context = SimpleNamespace(metadata={})

    result = await runtime._maybe_handle_plan_mode_entry(
        agent_id=uuid4(),
        user_id=uuid4(),
        session_id="session-file-update",
        content=(
            "[File: bank_statement.pdf]\n"
            "Monthly statement body text extracted from the uploaded PDF.\n\n"
            "Question: 帮我更新到5月"
        ),
        classification_content="[📎 bank_statement.pdf]\n帮我更新到5月",
        plan_mode_requested=True,
        runtime_session_context=session_context,
    )

    assert result is None
    state = session_context.metadata["plan_mode"]
    assert state["intent_type"] == "in_session_execution"
    assert state["action_kind"] == "start_long_task"
    assert state["tool_name"] == "continue_current_session"
    assert state["handoff_target"] == "continue_current_session"
    assert state["original_request"].startswith("[File: bank_statement.pdf]")


@pytest.mark.asyncio
async def test_activate_interactive_plan_mode_writes_typed_state_and_keeps_dict_mirror(monkeypatch):
    """Phase 1: a real SessionContext gets the typed PlanModeState populated,
    and the legacy metadata['plan_mode'] dict stays a byte-exact mirror."""
    import app.services.web_chat_runtime as runtime
    from app.runtime.session import SessionContext

    session_context = SessionContext()

    result = await runtime._maybe_handle_plan_mode_entry(
        agent_id=uuid4(),
        user_id=uuid4(),
        session_id="session-1",
        content="帮我完整调研这个行业",
        plan_mode_requested=True,
        runtime_session_context=session_context,
    )

    assert result is None
    # Typed source of truth populated.
    assert session_context.plan_mode.active is True
    assert session_context.plan_mode.original_request == "帮我完整调研这个行业"
    assert session_context.plan_mode.intent_type == "in_session_execution"
    # Legacy dict mirror stays consistent with the typed state.
    assert session_context.metadata["plan_mode"] == session_context.plan_mode.to_metadata()
    # Runtime-only injection bookkeeping never leaks into the mirror.
    assert "reminded_full" not in session_context.metadata["plan_mode"]
    # Phase 4B: a session-scoped plan file is provisioned and mirrored for the gate.
    assert session_context.plan_mode.plan_file_path == "workspace/plans/session-1.plan.md"
    assert session_context.metadata["plan_mode"]["plan_file_path"] == "workspace/plans/session-1.plan.md"


@pytest.mark.asyncio
async def test_activate_interactive_plan_mode_provisions_markdown_plan_file(tmp_path, monkeypatch):
    """MD-first Plan Mode should create the session plan file up front so the
    agent can write the plan body there instead of discovering a missing
    workspace/plans directory and falling back to long JSON arguments."""
    import app.services.web_chat_runtime as runtime
    from app.runtime.session import SessionContext

    agent_id = uuid4()
    monkeypatch.setattr(runtime, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    session_context = SessionContext()

    result = await runtime._maybe_handle_plan_mode_entry(
        agent_id=agent_id,
        user_id=uuid4(),
        session_id="session-1",
        content="进入计划模式，做一个关于跨链桥的报告",
        plan_mode_requested=True,
        runtime_session_context=session_context,
    )

    assert result is None
    plan_path = tmp_path / str(agent_id) / "workspace" / "plans" / "session-1.plan.md"
    assert plan_path.is_file()
    assert plan_path.read_text(encoding="utf-8") == ""


@pytest.mark.asyncio
async def test_maybe_handle_plan_mode_entry_activates_deep_research_interactive_plan(monkeypatch):
    import app.services.web_chat_runtime as runtime

    session_context = SimpleNamespace(metadata={})

    # A: entry is user-explicit (plan_mode_requested=True). On EXPLICIT entry the
    # deep-research detection still flips handoff_target to deep_research.
    result = await runtime._maybe_handle_plan_mode_entry(
        agent_id=uuid4(),
        user_id=uuid4(),
        session_id="session-1",
        content="使用 deepresearch做一个web3的全景报告",
        plan_mode_requested=True,
        runtime_session_context=session_context,
    )

    assert result is None
    assert session_context.metadata["plan_mode"]["active"] is True
    assert session_context.metadata["plan_mode"]["handoff_target"] == "deep_research"
    assert session_context.metadata["plan_mode"]["deep_research"] is True
    assert (
        session_context.metadata["plan_mode"]["deep_research_args"]["question"]
        == "使用 deepresearch做一个web3的全景报告"
    )


@pytest.mark.asyncio
async def test_maybe_handle_plan_mode_entry_does_not_auto_enter_for_deep_research_text(monkeypatch):
    """A (user correction): deep-research wording alone must NOT auto-enter Plan
    Mode. Without an explicit request the agent's judgment never triggers entry —
    no plan_mode state is written."""
    import app.services.web_chat_runtime as runtime

    session_context = SimpleNamespace(metadata={})

    result = await runtime._maybe_handle_plan_mode_entry(
        agent_id=uuid4(),
        user_id=uuid4(),
        session_id="session-1",
        content="使用 deepresearch做一个web3的全景报告",
        plan_mode_requested=False,
        runtime_session_context=session_context,
    )

    assert result is None
    assert "plan_mode" not in session_context.metadata


@pytest.mark.asyncio
async def test_execute_web_chat_run_persists_visible_error_on_uncancelled_exception(monkeypatch):
    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    session_id = uuid4().hex
    runtime_task = SimpleNamespace(
        id=run_id,
        parent_agent_id=agent_id,
        parent_session_id=session_id,
        prompt="latest question",
        metadata_json={"user_id": str(user_id), "session_id": session_id},
        trace_id=None,
    )
    agent = SimpleNamespace(
        id=agent_id,
        name="Web3研究员",
        role_description="",
        primary_model_id=uuid4(),
        fallback_model_id=None,
        tenant_id=uuid4(),
        agent_type="native",
    )
    user = SimpleNamespace(id=user_id, display_name="Rocky", username="rocky")
    llm_model = SimpleNamespace(provider="openai", model="gpt-4.1", supports_vision=False)
    finalized: list[dict] = []
    updates: list[tuple] = []
    broadcasts: list[dict] = []

    async def fake_load_context(_run_uuid):
        return runtime_task, agent, user, llm_model, None, []

    async def fake_invoke(_request):
        raise RuntimeError("provider stream closed")

    async def fake_finalize(**kwargs):
        finalized.append(kwargs)
        return True

    async def fake_update(run_uuid, **kwargs):
        updates.append((run_uuid, kwargs))

    async def fake_broadcast(_agent_id, _session_id, event):
        broadcasts.append(event)

    async def noop_async(*_args, **_kwargs):
        return None

    monkeypatch.setattr(runtime, "_load_runtime_context", fake_load_context)
    monkeypatch.setattr(runtime, "_maybe_handle_plan_mode_entry", noop_async)
    monkeypatch.setattr(runtime, "invoke_agent", fake_invoke)
    monkeypatch.setattr(runtime, "_finalize_web_chat_run_with_assistant", fake_finalize)
    monkeypatch.setattr(runtime, "_update_runtime_task", fake_update)
    monkeypatch.setattr(runtime, "broadcast_web_chat_event", fake_broadcast)
    monkeypatch.setattr(runtime, "_resume_queued_plan_handoffs", noop_async)

    await runtime.execute_web_chat_run(run_id)

    assert finalized == [
        {
            "run_uuid": run_id,
            "agent_id": agent_id,
            "user_id": user_id,
            "session_id": session_id,
            "content": "[LLM Error] AI 模型调用异常，请稍后重试。",
            "thinking": None,
            "thinking_signature": None,
            "status": "failed",
            "result_summary": "Web chat run failed: RuntimeError",
            "metadata_json": {"error": "provider stream closed"},
        }
    ]
    assert updates == []
    assert broadcasts[-1] == {"type": "error", "content": "[LLM Error] AI 模型调用异常，请稍后重试。"}


@pytest.mark.asyncio
async def test_execute_web_chat_run_keeps_cancelled_exception_as_killed(monkeypatch):
    import app.services.web_chat_runtime as runtime

    run_id = uuid4()
    cancel_event = asyncio.Event()
    cancel_event.set()
    updates = []

    async def fake_load_context(_run_uuid):
        raise RuntimeError("cancelled by runtime")

    async def fake_update(run_uuid, **kwargs):
        updates.append((run_uuid, kwargs))

    monkeypatch.setattr(runtime, "_load_runtime_context", fake_load_context)
    monkeypatch.setattr(runtime, "_update_runtime_task", fake_update)

    await runtime.execute_web_chat_run(run_id, cancel_event=cancel_event)

    assert updates == [
        (
            run_id,
            {
                "status": "killed",
                "result_summary": "Generation stopped by user.",
                "metadata_json": {"cancelled_by_user": True},
            },
        )
    ]


@pytest.mark.asyncio
async def test_deliver_run_result_pushes_to_im_channel(monkeypatch):
    """P1-2: a run whose session carries a delivery target pushes the final text
    back to that IM channel (the in-session plan-continuation case)."""
    import app.services.web_chat_runtime as runtime

    target = {"channel": "feishu", "chat_id": "oc_x"}
    session = SimpleNamespace(delivery_target_json=target)

    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def execute(self, _stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: session)

    monkeypatch.setattr(runtime, "_async_session", lambda: _DB())
    # RLS 阶段2b: _deliver_run_result_to_channel resolves the agent's tenant and
    # opens a tenant-scoped session. Route it through the same fake DB and stub
    # the resolver so no real DB / bypass read happens.
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda *_a, **_k: _DB())

    async def _fake_resolve(*_a, **_k):
        return uuid4()

    monkeypatch.setattr("app.services.tenant_resolver.resolve_tenant_for_agent", _fake_resolve)

    sent = {}

    async def fake_send_text(*, db, agent_id, reply_target, text, **_kwargs):
        sent["reply_target"] = reply_target
        sent["text"] = text
        return SimpleNamespace()

    monkeypatch.setattr("app.services.channel_delivery_service.ChannelDeliveryService.send_text", fake_send_text)

    await runtime._deliver_run_result_to_channel(uuid4(), uuid4(), "执行完成")
    assert sent["reply_target"] == target
    assert sent["text"] == "执行完成"


@pytest.mark.asyncio
async def test_deliver_run_result_skips_web_session(monkeypatch):
    """A web-origin session has no delivery target → no channel delivery."""
    import app.services.web_chat_runtime as runtime

    session = SimpleNamespace(delivery_target_json=None)

    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def execute(self, _stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: session)

    monkeypatch.setattr(runtime, "_async_session", lambda: _DB())
    # RLS 阶段2b: route the tenant-scoped session through the same fake DB and
    # stub the resolver so this exercises the real no-target branch (not an
    # accidental pass via a real-DB-down exception).
    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda *_a, **_k: _DB())

    async def _fake_resolve(*_a, **_k):
        return uuid4()

    monkeypatch.setattr("app.services.tenant_resolver.resolve_tenant_for_agent", _fake_resolve)

    calls = {"n": 0}

    async def fake_send_text(**_kwargs):
        calls["n"] += 1

    monkeypatch.setattr("app.services.channel_delivery_service.ChannelDeliveryService.send_text", fake_send_text)

    await runtime._deliver_run_result_to_channel(uuid4(), uuid4(), "执行完成")
    assert calls["n"] == 0  # web session → no channel delivery


@pytest.mark.asyncio
async def test_deliver_run_result_skips_current_web_session_delivery_target(monkeypatch):
    """Web sessions carry delivery metadata for explicit web pushes, but a web-chat
    run's own final response is already persisted by the run finalizer."""
    import app.services.web_chat_runtime as runtime

    session_id = uuid4()
    session = SimpleNamespace(
        delivery_target_json={
            "channel": "web",
            "username": "alice",
            "session_id": str(session_id),
        }
    )

    class _DB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_a):
            return False

        async def execute(self, _stmt):
            return SimpleNamespace(scalar_one_or_none=lambda: session)

    monkeypatch.setattr(runtime, "tenant_scoped_session", lambda *_a, **_k: _DB())

    async def _fake_resolve(*_a, **_k):
        return uuid4()

    monkeypatch.setattr("app.services.tenant_resolver.resolve_tenant_for_agent", _fake_resolve)

    calls = {"n": 0}

    async def fake_send_text(**_kwargs):
        calls["n"] += 1

    monkeypatch.setattr("app.services.channel_delivery_service.ChannelDeliveryService.send_text", fake_send_text)

    await runtime._deliver_run_result_to_channel(uuid4(), session_id, "执行完成")
    assert calls["n"] == 0
