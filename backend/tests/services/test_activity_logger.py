from __future__ import annotations

import uuid

import pytest


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
