from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _DB:
    def __init__(self) -> None:
        self.commits = 0

    async def commit(self) -> None:
        self.commits += 1


def _agent_session(*, state: str = "open"):
    parent_session_id = uuid4()
    return SimpleNamespace(
        id=uuid4(),
        agent_id=uuid4(),
        tenant_id=uuid4(),
        user_id=uuid4(),
        parent_session_id=parent_session_id,
        root_session_id=parent_session_id,
        visibility_scope="team",
        listed_surface="parent",
        session_kind="subagent",
        runtime_source="subagent",
        transcript_metadata_json={"session_state": state},
    )


@pytest.mark.asyncio
async def test_agent_session_continuation_active_run_queues_to_midrun_consumer(monkeypatch):
    import app.services.agent_session_continuation as svc

    db = _DB()
    session = _agent_session(state="running")
    agent = SimpleNamespace(id=session.agent_id, tenant_id=session.tenant_id, name="Lead")
    user = SimpleNamespace(id=session.user_id)
    captured: dict = {}
    active_run = SimpleNamespace(
        id=uuid4(), status="running", metadata_json={}, created_at=None, started_at=None, completed_at=None
    )

    async def fake_append(**kwargs):
        captured["append"] = kwargs
        return SimpleNamespace(event_id=uuid4())

    async def fake_find_active(**kwargs):
        captured["find_active"] = kwargs
        return active_run

    async def fake_queue(**kwargs):
        captured["queue"] = kwargs
        return {"run_id": kwargs["active_run"].id.hex, "status": "running"}

    monkeypatch.setattr(svc, "append_session_event", fake_append)
    monkeypatch.setattr(svc, "_find_active_run", fake_find_active)
    monkeypatch.setattr(svc, "_queue_saved_mid_run_user_message", fake_queue)

    result = await svc.continue_agent_session_from_mailbox(
        db=db,
        agent=agent,
        user=user,
        session=session,
        message="inspect the new evidence",
        parent_session_id=str(session.parent_session_id),
    )

    assert result["status"] == "queued"
    assert result["consumer"] == "mid_run_message_drain"
    assert captured["append"]["event_type"] == "agent_session_message"
    assert captured["append"]["role"] == "user"
    assert captured["append"]["metadata"]["mailbox_kind"] == "followup"
    assert captured["queue"]["message_already_in_t0"] is True
    assert captured["queue"]["content"] == "inspect the new evidence"


@pytest.mark.asyncio
async def test_agent_session_continuation_inactive_open_session_starts_turn(monkeypatch):
    import app.services.agent_session_continuation as svc

    db = _DB()
    session = _agent_session(state="open")
    agent = SimpleNamespace(id=session.agent_id, tenant_id=session.tenant_id, name="Lead")
    user = SimpleNamespace(id=session.user_id)
    captured: dict = {}

    async def fake_append(**kwargs):
        captured["append"] = kwargs
        return SimpleNamespace(event_id=uuid4())

    async def fake_find_active(**kwargs):
        return None

    async def fake_start(**kwargs):
        captured["start"] = kwargs
        return {"run_id": "run-1", "status": "running"}

    monkeypatch.setattr(svc, "append_session_event", fake_append)
    monkeypatch.setattr(svc, "_find_active_run", fake_find_active)
    monkeypatch.setattr(svc, "start_web_chat_run", fake_start)

    result = await svc.continue_agent_session_from_mailbox(
        db=db,
        agent=agent,
        user=user,
        session=session,
        message="continue from the last result",
        parent_session_id=str(session.parent_session_id),
    )

    assert result["status"] == "started"
    assert result["run_id"] == "run-1"
    assert captured["append"]["event_type"] == "agent_session_message"
    assert captured["start"]["append_user_message"] is False
    assert captured["start"]["runtime_task_type"] == "web_chat_turn"
    assert captured["start"]["extra_metadata"]["source"] == "agent_session_mailbox"
    assert captured["start"]["extra_metadata"]["latest_user_prompt_overrides_history"] is True


@pytest.mark.asyncio
async def test_agent_session_continuation_terminal_session_rejects_and_writes_transcript(monkeypatch):
    import app.services.agent_session_continuation as svc

    db = _DB()
    session = _agent_session(state="completed")
    agent = SimpleNamespace(id=session.agent_id, tenant_id=session.tenant_id, name="Lead")
    user = SimpleNamespace(id=session.user_id)
    captured: dict = {}

    async def fake_append(**kwargs):
        captured["append"] = kwargs
        return SimpleNamespace(event_id=uuid4())

    async def fake_start(**_kwargs):
        raise AssertionError("terminal agent sessions must not start a continuation")

    monkeypatch.setattr(svc, "append_session_event", fake_append)
    monkeypatch.setattr(svc, "start_web_chat_run", fake_start)

    result = await svc.continue_agent_session_from_mailbox(
        db=db,
        agent=agent,
        user=user,
        session=session,
        message="reopen this",
        parent_session_id=str(session.parent_session_id),
    )

    assert result["ok"] is False
    assert result["status"] == "rejected"
    assert result["reason"] == "terminal_agent_session"
    assert captured["append"]["event_type"] == "agent_session_message_rejected"
    assert captured["append"]["metadata"]["session_state"] == "completed"
    assert db.commits == 1
