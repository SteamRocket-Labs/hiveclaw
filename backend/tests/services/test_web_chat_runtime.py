from __future__ import annotations

import asyncio
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
async def test_start_web_chat_run_creates_runtime_task_and_user_message(monkeypatch):
    import app.services.web_chat_runtime as runtime
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
    assert task.parent_session_id == str(session_id)
    assert task.child_session_id == str(session_id)
    assert task.metadata_json["user_id"] == str(user_id)
    assert db.commits == 1
    assert scheduled


@pytest.mark.asyncio
async def test_start_web_chat_run_rejects_duplicate_active_run():
    import app.services.web_chat_runtime as runtime

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
    )
    db = _FakeDB(active_run=active_run)

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
    assert db.added == []
    assert db.commits == 0


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

    monkeypatch.setattr(runtime, "_async_session", lambda: _SessionFactory())
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
