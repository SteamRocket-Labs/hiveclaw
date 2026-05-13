from __future__ import annotations

import uuid

import pytest


def test_activity_action_enum_includes_tool_runtime_approval_events():
    from app.models.activity_log import AgentActivityLog

    action_enum = AgentActivityLog.__table__.c.action_type.type

    assert "tool_call_direct" in action_enum.enums
    assert "tool_call_approved" in action_enum.enums
    assert "llm_error" in action_enum.enums


class _FailingSession:
    def __init__(self, *, fail_on_commit: bool):
        self.fail_on_commit = fail_on_commit
        self.rollback_calls = 0
        self.added = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        if self.fail_on_commit:
            raise RuntimeError("commit failed")

    async def rollback(self):
        self.rollback_calls += 1


class _EnumDriftSession(_FailingSession):
    async def commit(self):
        raise RuntimeError('invalid input value for enum activity_action_enum: "tool_call_approved"')


class _LlmErrorEnumDriftSession(_FailingSession):
    async def commit(self):
        raise RuntimeError('invalid input value for enum activity_action_enum: "llm_error"')


@pytest.mark.asyncio
async def test_log_activity_rolls_back_on_commit_error(monkeypatch):
    from app.services.activity_logger import log_activity

    fake_session = _FailingSession(fail_on_commit=True)
    monkeypatch.setattr("app.services.activity_logger.async_session", lambda: fake_session)

    await log_activity(
        agent_id=uuid.uuid4(),
        action_type="delegation_started",
        summary="Delegation started",
        detail={"task_id": "task-1"},
    )

    assert fake_session.rollback_calls == 1


@pytest.mark.asyncio
async def test_log_activity_commits_successfully(monkeypatch):
    from app.services.activity_logger import log_activity

    fake_session = _FailingSession(fail_on_commit=False)
    monkeypatch.setattr("app.services.activity_logger.async_session", lambda: fake_session)

    await log_activity(
        agent_id=uuid.uuid4(),
        action_type="delegation_completed",
        summary="Delegation completed",
        detail={"task_id": "task-1"},
    )

    assert len(fake_session.added) == 1
    assert fake_session.rollback_calls == 0


@pytest.mark.asyncio
async def test_log_activity_falls_back_when_runtime_enum_value_is_missing(monkeypatch):
    from app.services.activity_logger import log_activity

    first_session = _EnumDriftSession(fail_on_commit=True)
    second_session = _FailingSession(fail_on_commit=False)
    sessions = iter([first_session, second_session])
    monkeypatch.setattr("app.services.activity_logger.async_session", lambda: next(sessions))

    await log_activity(
        agent_id=uuid.uuid4(),
        action_type="tool_call_approved",
        summary="Approved tool execution",
        detail={"tool": "write_file", "approved": True},
    )

    assert first_session.rollback_calls == 1
    assert len(second_session.added) == 1
    assert second_session.added[0].action_type == "tool_call"
    assert second_session.added[0].detail_json["activity_type_original"] == "tool_call_approved"
    assert second_session.added[0].detail_json["activity_type_fallback"] == "tool_call"


@pytest.mark.asyncio
async def test_log_activity_falls_back_when_llm_error_enum_value_is_missing(monkeypatch):
    from app.services.activity_logger import log_activity

    first_session = _LlmErrorEnumDriftSession(fail_on_commit=True)
    second_session = _FailingSession(fail_on_commit=False)
    sessions = iter([first_session, second_session])
    monkeypatch.setattr("app.services.activity_logger.async_session", lambda: next(sessions))

    await log_activity(
        agent_id=uuid.uuid4(),
        action_type="llm_error",
        summary="LLM failed",
        detail={"channel": "web"},
    )

    assert first_session.rollback_calls == 1
    assert len(second_session.added) == 1
    assert second_session.added[0].action_type == "error"
    assert second_session.added[0].detail_json["activity_type_original"] == "llm_error"
    assert second_session.added[0].detail_json["activity_type_fallback"] == "error"
