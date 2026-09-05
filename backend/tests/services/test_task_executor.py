from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeSession:
    def __init__(self, execute_values):
        self._execute_values = list(execute_values)
        self.added = []
        self.commits = 0
        self.flushes = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def execute(self, _query):
        # tenant_scoped_session emits a `SET LOCAL app.current_tenant_id` before
        # the business query — it must not consume a prepared result.
        if "app.current_tenant_id" in str(_query):
            return _FakeScalarResult(None)
        # The pre-execution boundary re-reads the owning Tenant's liveness;
        # these unit fixtures always describe a live company.
        if "tenants.is_active" in str(_query):
            return _FakeScalarResult(True)
        if not self._execute_values:
            raise AssertionError("No fake execute result prepared")
        return _FakeScalarResult(self._execute_values.pop(0))

    def add(self, item):
        self.added.append(item)
        if getattr(item, "id", None) is None:
            item.id = uuid4()

    async def commit(self):
        self.commits += 1

    async def flush(self):
        self.flushes += 1


async def _fake_resolve_tenant(_agent_id, **_kwargs):
    # execute_task resolves the owning tenant via an audited bypass read (its own
    # session) before pinning the work sessions; stub it so the detached-task flow
    # never touches a real DB in these unit tests.
    return uuid4()


async def _verified_plan_authorization(task, **_kwargs):
    return SimpleNamespace(lease_id=task.plan_authorization["lease_id"])


def _completed_invocation(request, content: str, *, terminal_reason: str = "turn_stop"):
    return SimpleNamespace(
        content=content,
        terminal_reason=terminal_reason,
        failure_code=None,
        response_complete_payload={
            "agent_id": request.agent_id,
            "session_id": request.memory_session_id,
            "messages": request.messages,
            "source": request.session_context.source,
            "metadata": {
                "tenant_id": str(request.tenant_id),
                "final_response": content,
            },
        },
    )


def test_business_task_cancel_event_only_latches_for_a_registered_runtime_run() -> None:
    from app.services import task_executor

    runtime_task_id = uuid4()

    assert task_executor.apply_remote_business_task_cancel(runtime_task_id) is False
    event = task_executor.business_task_cancel_event(runtime_task_id)
    assert event.is_set() is False

    assert task_executor.apply_remote_business_task_cancel(runtime_task_id) is True
    assert event.is_set() is True
    task_executor.release_business_task_cancel_event(runtime_task_id, event)

    replacement = task_executor.business_task_cancel_event(runtime_task_id)
    try:
        assert replacement is not event
        assert replacement.is_set() is False
    finally:
        task_executor.release_business_task_cancel_event(runtime_task_id, replacement)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("terminal_reason", "expected_status"),
    [("turn_stop", "succeeded"), ("provider_error", "failed")],
)
async def test_execute_task_delegates_to_runtime_invoker(monkeypatch, terminal_reason, expected_status):
    from app.services.task_executor import execute_task

    task_id = uuid4()
    agent_id = uuid4()
    model_id = uuid4()
    creator_id = uuid4()
    plan_id = uuid4()

    task = SimpleNamespace(
        id=task_id,
        title="整理周报",
        description="汇总本周关键进展",
        type="todo",
        status="pending",
        completed_at=None,
        plan_id=plan_id,
        plan_version=1,
        plan_hash="sha256:task",
        plan_exempt_reason=None,
        plan_authorization={
            "schema": "hive.plan_authorization_evidence.v1",
            "lease_id": str(uuid4()),
            "canonical_args_hash": "args-hash",
            "target_ref": f"task:{task_id}:run",
            "requester_user_id": str(creator_id),
            "session_id": "task-session",
            "runtime_task_id": None,
            "evidence_id": f"task-run:{task_id}:request-1",
        },
    )
    tenant_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="Ops Agent",
        role_description="Operations",
        primary_model_id=model_id,
        fallback_model_id=None,
        creator_id=creator_id,
        tenant_id=tenant_id,
    )
    model = SimpleNamespace(
        id=model_id,
        provider="openai",
        model="gpt-4.1",
        api_key="key",
        base_url=None,
        max_output_tokens=None,
        tenant_id=tenant_id,
    )

    setup_session = _FakeSession([task])
    model_session = _FakeSession([agent, model, None])
    final_session = _FakeSession([task])
    sessions = [setup_session, model_session, final_session]

    captured = {}
    activity_calls = []
    verified_authorizations = []
    premature_t0_seals = []

    async def fake_invoke_agent(request):
        captured["request"] = request
        return _completed_invocation(request, "任务已完成", terminal_reason=terminal_reason)

    async def fake_log_activity(*args, **kwargs):
        activity_calls.append((args, kwargs))

    async def fake_verify_authorization(**kwargs):
        verified_authorizations.append(kwargs)
        return SimpleNamespace(lease_id=task.plan_authorization["lease_id"])

    monkeypatch.setattr("app.services.task_executor.resolve_tenant_for_agent", _fake_resolve_tenant)
    monkeypatch.setattr("app.services.task_executor.tenant_scoped_session", lambda *a, **k: sessions.pop(0))
    monkeypatch.setattr("app.services.task_executor.TaskLog", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr("app.services.task_executor.invoke_agent", fake_invoke_agent)
    monkeypatch.setattr(
        "app.services.task_executor._seal_task_t0_segment",
        lambda **kwargs: premature_t0_seals.append(kwargs),
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.task_executor.verify_consumed_plan_authorization_lease",
        fake_verify_authorization,
    )
    monkeypatch.setattr("app.services.activity_logger.log_activity", fake_log_activity)

    outcome = await execute_task(task_id, agent_id)

    request = captured["request"]
    assert request.model is model
    assert request.agent_id == agent_id
    assert request.user_id == creator_id
    assert request.core_tools_only is True
    # PR-17 rewrote TASK_EXECUTION_ADDENDUM as XML-structured best practice.
    # The identity signal is now `<role>` + "executing an assigned task autonomously".
    assert "executing an assigned task autonomously" in request.system_prompt_suffix
    assert "<final_report_format>" in request.system_prompt_suffix
    assert request.messages == [
        {
            "role": "user",
            "content": "[任务执行] 整理周报\n任务描述: 汇总本周关键进展\n\n请认真完成此任务，给出详细的执行结果。",
        }
    ]
    assert request.memory_messages == request.messages
    assert request.session_context is not None
    assert request.session_context.source == "task"
    assert request.session_context.channel == "task"
    assert request.session_context.metadata["task_id"] == str(task_id)
    assert request.session_context.metadata["task_type"] == "todo"
    assert request.execution_identity is not None
    assert request.execution_identity.identity_type == "agent_bot"
    assert request.execution_identity.identity_id == agent_id
    assert request.execution_identity.label == "Agent: Ops Agent (task)"
    assert not hasattr(request, "emit_turn_stop")

    assert outcome.status.value == expected_status
    if expected_status == "succeeded":
        assert outcome.result == "任务已完成"
        assert activity_calls
    else:
        assert outcome.result is None
        assert outcome.error_code == "provider_error"
        assert activity_calls == []
    # The executor records evidence but cannot invent a terminal Task status.
    # The Task + RuntimeTask atomic finalizer applies that outcome together.
    assert task.status == "doing"
    assert task.completed_at is None
    assert any(getattr(entry, "content", None) == "任务已完成" for entry in final_session.added)
    assert premature_t0_seals == []
    assert len(verified_authorizations) == 1
    assert verified_authorizations[0]["db"] is setup_session
    assert verified_authorizations[0]["plan_id"] == plan_id
    assert verified_authorizations[0]["evidence"] == task.plan_authorization


@pytest.mark.asyncio
async def test_execute_task_blocks_without_confirmed_plan(monkeypatch):
    from app.services.task_executor import execute_task

    task_id = uuid4()
    agent_id = uuid4()
    task = SimpleNamespace(
        id=task_id,
        title="未授权任务",
        description="",
        type="todo",
        status="pending",
        completed_at=None,
        plan_id=None,
        plan_version=None,
        plan_hash=None,
        plan_exempt_reason=None,
        plan_authorization=None,
    )
    setup_session = _FakeSession([task])
    monkeypatch.setattr("app.services.task_executor.resolve_tenant_for_agent", _fake_resolve_tenant)
    monkeypatch.setattr("app.services.task_executor.tenant_scoped_session", lambda *a, **k: setup_session)
    monkeypatch.setattr("app.services.task_executor.TaskLog", lambda **kwargs: SimpleNamespace(**kwargs))

    async def fake_invoke_agent(_request):  # pragma: no cover - must not run
        raise AssertionError("invoke_agent should not run without a confirmed plan")

    monkeypatch.setattr("app.services.task_executor.invoke_agent", fake_invoke_agent)

    outcome = await execute_task(task_id, agent_id)

    assert task.status == "pending"
    assert outcome.status.value == "blocked"
    assert outcome.error_code == "plan_authorization_evidence_missing"
    assert any("Plan Mode blocked" in item.content for item in setup_session.added)


@pytest.mark.asyncio
async def test_execute_task_persists_reflection_session_tool_calls_and_t0_ledger(monkeypatch, tmp_path):
    from app.models.chat_transcript_event import ChatTranscriptEvent
    from app.services.task_executor import execute_task

    task_id = uuid4()
    agent_id = uuid4()
    model_id = uuid4()
    creator_id = uuid4()
    participant_id = uuid4()
    plan_id = uuid4()

    task = SimpleNamespace(
        id=task_id,
        title="准备竞品分析",
        description="整理最近 3 家竞品动态",
        type="todo",
        status="pending",
        completed_at=None,
        plan_id=plan_id,
        plan_version=1,
        plan_hash="sha256:task",
        plan_exempt_reason=None,
        plan_authorization={
            "schema": "hive.plan_authorization_evidence.v1",
            "lease_id": str(uuid4()),
            "canonical_args_hash": "args-hash",
            "target_ref": f"task:{task_id}:run",
            "requester_user_id": str(creator_id),
            "session_id": "task-session",
            "runtime_task_id": None,
            "evidence_id": f"task-run:{task_id}:request-2",
        },
    )
    tenant_id = uuid4()
    agent = SimpleNamespace(
        id=agent_id,
        name="Research Agent",
        role_description="Research",
        primary_model_id=model_id,
        fallback_model_id=None,
        creator_id=creator_id,
        tenant_id=tenant_id,
        max_tool_rounds=12,
    )
    model = SimpleNamespace(
        id=model_id,
        provider="openai",
        model="gpt-4.1",
        api_key="key",
        base_url=None,
        max_output_tokens=None,
        tenant_id=tenant_id,
    )
    participant = SimpleNamespace(id=participant_id)

    setup_session = _FakeSession([task])
    prepare_session = _FakeSession([agent, model, participant])
    tool_call_session = _FakeSession([])
    final_session = _FakeSession([task])
    sessions = [setup_session, prepare_session, tool_call_session, final_session]
    long_tool_result = "3 sources found " + ("完整证据" * 600) + " END_OF_TOOL_RESULT"

    async def fake_invoke_agent(request):
        await request.on_tool_call(
            {
                "status": "done",
                "name": "web_search",
                "args": {"query": "competitor updates"},
                "result": long_tool_result,
            }
        )
        return _completed_invocation(request, "任务已完成，已整理竞品动态。")

    monkeypatch.setattr("app.services.task_executor.resolve_tenant_for_agent", _fake_resolve_tenant)
    monkeypatch.setattr("app.services.task_executor.tenant_scoped_session", lambda *a, **k: sessions.pop(0))
    monkeypatch.setattr("app.services.task_executor.TaskLog", lambda **kwargs: SimpleNamespace(**kwargs))
    monkeypatch.setattr("app.services.task_executor.invoke_agent", fake_invoke_agent)
    monkeypatch.setattr(
        "app.services.task_executor.verify_consumed_plan_authorization_lease",
        lambda **kwargs: _verified_plan_authorization(task, **kwargs),
    )
    monkeypatch.setattr(
        "app.memory.t0.ledger.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    async def fake_log_activity(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.activity_logger.log_activity", fake_log_activity)

    await execute_task(task_id, agent_id)

    created_session = next(item for item in prepare_session.added if getattr(item, "source_channel", None) == "task")
    user_prompt = next(item for item in prepare_session.added if getattr(item, "role", None) == "user")
    tool_call = next(item for item in tool_call_session.added if getattr(item, "role", None) == "tool_call")
    assistant_reply = next(item for item in final_session.added if getattr(item, "role", None) == "assistant")

    assert created_session.title.startswith("🧾 Task:")
    assert created_session.session_kind == "user_task"
    assert created_session.actor_type == "agent"
    assert created_session.runtime_source == "runtime_task"
    assert created_session.visibility_scope == "agent_owner"
    assert created_session.listed_surface == "task_updates"
    assert user_prompt.conversation_id == str(created_session.id)
    assert "准备竞品分析" in user_prompt.content
    assert '"name": "web_search"' in tool_call.content
    assert "END_OF_TOOL_RESULT" in tool_call.content
    assert assistant_reply.conversation_id == str(created_session.id)
    assert "任务已完成" in assistant_reply.content

    transcript_events = [
        item
        for session in (prepare_session, tool_call_session, final_session)
        for item in session.added
        if isinstance(item, ChatTranscriptEvent)
    ]
    assert [(event.event_type, event.actor_type, event.message_id) for event in transcript_events] == [
        ("user_message", "user", user_prompt.id),
        ("tool_result", "agent", tool_call.id),
        ("assistant_message", "assistant", assistant_reply.id),
    ]
    assert all(event.session_id == created_session.id for event in transcript_events)
    assert all(event.listed_surface == "task_updates" for event in transcript_events)
    assert all(event.projection_status == "pending" for event in transcript_events)
    assert transcript_events[0].metadata_json["task_id"] == str(task_id)
