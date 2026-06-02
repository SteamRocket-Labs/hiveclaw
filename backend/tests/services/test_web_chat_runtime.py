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


# ---------------------------------------------------------------------------
# Auto-sync gate (§9.0 task auto-sync / §9.2): a regex-detected "create a task"
# must NOT silently background-execute; without a confirmed plan it creates an
# awaiting PlanRequest and tells the user to confirm.
# ---------------------------------------------------------------------------


class _AutoSyncDB:
    """Minimal session for _maybe_sync_created_task: records adds/commits."""

    def __init__(self):
        self.added = []
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_a):
        return False

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid4()
        self.added.append(obj)

    async def commit(self):
        self.commits += 1

    async def refresh(self, _obj):
        return None


class _RecordingIntake:
    def __init__(self, plan):
        self.plan = plan
        self.calls = []

    async def ensure_awaiting_plan(self, **kwargs):
        self.calls.append(kwargs)
        return self.plan


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
async def test_maybe_sync_created_task_without_plan_creates_plan_and_skips_execution(monkeypatch):
    import app.services.web_chat_runtime as runtime
    from app.models.task import Task

    agent_id = uuid4()
    user_id = uuid4()
    executed = {"n": 0}

    async def fake_execute_task(_task_id, _agent_id):  # must NOT run
        executed["n"] += 1

    monkeypatch.setattr("app.services.task_executor.execute_task", fake_execute_task)

    db = _AutoSyncDB()
    monkeypatch.setattr(runtime, "_async_session", lambda: db)

    plan = SimpleNamespace(id=uuid4(), plan_version=1, plan_hash="sha256:abc")
    intake = _RecordingIntake(plan)
    monkeypatch.setattr(runtime, "get_plan_mode_service", lambda: intake)

    scheduled = []
    monkeypatch.setattr(runtime.asyncio, "create_task", lambda coro: scheduled.append(coro) or coro.close())

    result = await runtime._maybe_sync_created_task(
        agent_id=agent_id,
        user_id=user_id,
        content="帮我创建一个任务：每天整理新闻",
        assistant_response="好的",
    )

    # No Task persisted (the old behaviour created one and ran it).
    assert not any(isinstance(o, Task) for o in db.added)
    # execute_task never fired.
    assert executed["n"] == 0
    assert scheduled == []
    # A plan was materialised from the detected task title.
    assert intake.calls and intake.calls[0]["action_kind"] == "start_long_task"
    assert "每天整理新闻" in intake.calls[0]["arguments"]["title"]
    # User is told to confirm, with the plan id surfaced.
    assert str(plan.id) in result
    assert "确认" in result or "confirm" in result.lower()


@pytest.mark.asyncio
async def test_plan_mode_accepts_latest_recommendation_and_activates_interactive_mode(monkeypatch):
    import app.services.web_chat_runtime as runtime

    agent_id = uuid4()
    user_id = uuid4()
    tenant_id = uuid4()
    plan = SimpleNamespace(id=uuid4(), plan_version=1, plan_hash="sha256:abc")
    intake = _RecordingIntake(plan)
    monkeypatch.setattr(runtime, "get_plan_mode_service", lambda: intake)

    recommendation = SimpleNamespace(
        id=uuid4(),
        agent_id=agent_id,
        tenant_id=tenant_id,
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
        tenant_id=tenant_id,
        session_id="sess-accept",
        runtime_task_id=uuid4(),
        content="进入计划模式",
        runtime_session_context=session_context,
    )

    assert response is None
    assert recommendation.status == "accepted"
    assert recommendation.accepted_by_user_id == user_id
    assert intake.calls == []
    metadata = session_context.metadata["plan_mode"]
    assert metadata["original_request"] == recommendation.original_request
    assert metadata["intent_type"] == "autonomous_wake"
    assert metadata["action_kind"] == "create_enabled_trigger"
    assert metadata["tool_name"] == "set_trigger"


@pytest.mark.asyncio
async def test_maybe_sync_created_task_no_match_is_untouched(monkeypatch):
    import app.services.web_chat_runtime as runtime

    intake = _RecordingIntake(SimpleNamespace(id=uuid4(), plan_version=1, plan_hash="x"))
    monkeypatch.setattr(runtime, "get_plan_mode_service", lambda: intake)

    original = "这是普通回复，没有任务关键词"
    result = await runtime._maybe_sync_created_task(
        agent_id=uuid4(),
        user_id=uuid4(),
        content="你好，今天天气怎么样",
        assistant_response=original,
    )

    assert result == original
    assert intake.calls == []  # not a task-creation intent -> gate not invoked


@pytest.mark.asyncio
async def test_maybe_sync_created_task_intake_failure_is_non_fatal(monkeypatch):
    import app.services.web_chat_runtime as runtime
    from app.models.task import Task

    executed = {"n": 0}

    async def fake_execute_task(_task_id, _agent_id):
        executed["n"] += 1

    monkeypatch.setattr("app.services.task_executor.execute_task", fake_execute_task)
    db = _AutoSyncDB()
    monkeypatch.setattr(runtime, "_async_session", lambda: db)

    class _Boom:
        async def ensure_awaiting_plan(self, **_k):
            raise RuntimeError("db down")

    monkeypatch.setattr(runtime, "get_plan_mode_service", lambda: _Boom())

    original = "好的"
    result = await runtime._maybe_sync_created_task(
        agent_id=uuid4(),
        user_id=uuid4(),
        content="创建任务：清理日志",
        assistant_response=original,
    )

    # Fail-closed: still no execution, still no Task persisted; reply not crashed.
    assert executed["n"] == 0
    assert not any(isinstance(o, Task) for o in db.added)
    assert isinstance(result, str)


@pytest.mark.asyncio
async def test_maybe_handle_plan_mode_entry_recommends_schedule_without_creating_plan(monkeypatch):
    import app.services.web_chat_runtime as runtime

    intake = _RecordingIntake(SimpleNamespace(id=uuid4(), plan_version=1, plan_hash="sha256:x"))
    monkeypatch.setattr(runtime, "get_plan_mode_service", lambda: intake)

    async def _noop_recommendation(**_kwargs):
        return None

    monkeypatch.setattr(runtime, "_record_plan_mode_recommendation", _noop_recommendation)

    result = await runtime._maybe_handle_plan_mode_entry(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id=None,
        session_id="session-1",
        runtime_task_id=uuid4(),
        content="每天 9 点帮我整理新闻",
        plan_mode_requested=False,
    )

    assert result is not None
    assert "建议" in result
    assert "计划模式" in result
    assert "不用计划模式" in result
    assert intake.calls == []


@pytest.mark.asyncio
async def test_maybe_handle_plan_mode_entry_activates_interactive_mode_when_explicitly_requested(monkeypatch):
    import app.services.web_chat_runtime as runtime

    plan = SimpleNamespace(id=uuid4(), plan_version=1, plan_hash="sha256:abc")
    intake = _RecordingIntake(plan)
    monkeypatch.setattr(runtime, "get_plan_mode_service", lambda: intake)
    session_context = SimpleNamespace(metadata={})

    result = await runtime._maybe_handle_plan_mode_entry(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id=None,
        session_id="session-1",
        runtime_task_id=uuid4(),
        content="帮我完整调研这个行业",
        plan_mode_requested=True,
        runtime_session_context=session_context,
    )

    assert result is None
    assert intake.calls == []
    assert session_context.metadata["plan_mode"]["active"] is True
    assert session_context.metadata["plan_mode"]["original_request"] == "帮我完整调研这个行业"
    assert session_context.metadata["plan_mode"]["intent_type"] == "long_task"
    assert session_context.metadata["plan_mode"]["action_kind"] == "start_long_task"


@pytest.mark.asyncio
async def test_activate_interactive_plan_mode_writes_typed_state_and_keeps_dict_mirror(monkeypatch):
    """Phase 1: a real SessionContext gets the typed PlanModeState populated,
    and the legacy metadata['plan_mode'] dict stays a byte-exact mirror."""
    import app.services.web_chat_runtime as runtime
    from app.runtime.session import SessionContext

    plan = SimpleNamespace(id=uuid4(), plan_version=1, plan_hash="sha256:abc")
    intake = _RecordingIntake(plan)
    monkeypatch.setattr(runtime, "get_plan_mode_service", lambda: intake)
    session_context = SessionContext()

    result = await runtime._maybe_handle_plan_mode_entry(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id=None,
        session_id="session-1",
        runtime_task_id=uuid4(),
        content="帮我完整调研这个行业",
        plan_mode_requested=True,
        runtime_session_context=session_context,
    )

    assert result is None
    # Typed source of truth populated.
    assert session_context.plan_mode.active is True
    assert session_context.plan_mode.original_request == "帮我完整调研这个行业"
    assert session_context.plan_mode.intent_type == "long_task"
    # Legacy dict mirror stays consistent with the typed state.
    assert session_context.metadata["plan_mode"] == session_context.plan_mode.to_metadata()
    # Runtime-only injection bookkeeping never leaks into the mirror.
    assert "reminded_full" not in session_context.metadata["plan_mode"]
    # Phase 4B: a session-scoped plan file is provisioned and mirrored for the gate.
    assert session_context.plan_mode.plan_file_path == "workspace/plans/session-1.plan.md"
    assert session_context.metadata["plan_mode"]["plan_file_path"] == "workspace/plans/session-1.plan.md"


@pytest.mark.asyncio
async def test_maybe_handle_plan_mode_entry_activates_deep_research_interactive_plan(monkeypatch):
    import app.services.web_chat_runtime as runtime

    intake = SimpleNamespace(generic_calls=[], deep_research_calls=[])
    monkeypatch.setattr(runtime, "get_plan_mode_service", lambda: intake)
    session_context = SimpleNamespace(metadata={})

    result = await runtime._maybe_handle_plan_mode_entry(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id=None,
        session_id="session-1",
        runtime_task_id=uuid4(),
        content="使用 deepresearch做一个web3的全景报告",
        plan_mode_requested=False,
        runtime_session_context=session_context,
    )

    assert result is None
    assert intake.generic_calls == []
    assert intake.deep_research_calls == []
    assert session_context.metadata["plan_mode"]["active"] is True
    assert session_context.metadata["plan_mode"]["handoff_target"] == "deep_research"
    assert session_context.metadata["plan_mode"]["deep_research"] is True
    assert session_context.metadata["plan_mode"]["deep_research_args"]["question"] == "使用 deepresearch做一个web3的全景报告"


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
