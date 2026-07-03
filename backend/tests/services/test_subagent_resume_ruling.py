"""CC parity test: completed sub-agent sessions remain resumable.

Makes ``pytest -k subagent_resume`` collect a REAL, revert-sensitive test proving
that ordinary sub-agents can be continued after completion. This follows CC
``SendMessage`` / ``resumeAgentBackground`` semantics: the transcript remains the
truth surface, and a follow-up starts a new continuation turn for the same child
session instead of forcing a fresh spawn.
"""

from __future__ import annotations

import uuid
from types import SimpleNamespace

from app.services import agent_session_continuation as asc


class _FakeDB:
    def __init__(self) -> None:
        self.committed = False

    async def commit(self) -> None:
        self.committed = True


def _session(state: str) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        tenant_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        transcript_metadata_json={"session_state": state},
        session_kind="subagent",
        runtime_source="subagent",
        parent_session_id=uuid.uuid4(),
        root_session_id=uuid.uuid4(),
        visibility_scope="team",
        listed_surface="parent",
    )


async def test_subagent_resume_terminal_session_starts_continuation_turn(monkeypatch):
    recorded: list[dict] = []
    captured: dict[str, dict] = {}

    async def _fake_append(**kwargs):
        recorded.append(kwargs)

    async def _fake_find_active(**_kwargs):
        return None

    async def _fake_start(**kwargs):
        captured["start"] = kwargs
        return {"run_id": "resume-run-1", "status": "running"}

    monkeypatch.setattr(asc, "append_session_event", _fake_append)
    monkeypatch.setattr(asc, "_find_active_run", _fake_find_active)
    monkeypatch.setattr(asc, "start_web_chat_run", _fake_start)
    db = _FakeDB()
    agent = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4())
    user = SimpleNamespace(id=uuid.uuid4())
    session = _session("completed")

    result = await asc.continue_agent_session_from_mailbox(
        db=db,
        agent=agent,
        user=user,
        session=session,
        message="please continue the analysis",
    )

    assert result["ok"] is True
    assert result["status"] == "started"
    assert result["run_id"] == "resume-run-1"
    assert result["consumer"] == "continuation_turn"
    assert result["child_session_id"] == str(session.id)
    assert db.committed is False

    assert recorded, "a follow-up mailbox event must be appended before the continuation turn starts"
    assert recorded[0]["event_type"] == "agent_session_message"
    assert recorded[0]["metadata"]["session_state"] == "completed"
    assert recorded[0]["metadata"]["terminal_session_resume"] is True
    assert recorded[0]["metadata"]["resumed_from_terminal_state"] == "completed"
    assert captured["start"]["append_user_message"] is False
    assert captured["start"]["runtime_task_type"] == asc.WEB_CHAT_TURN_TASK_TYPE
    assert captured["start"]["extra_metadata"]["terminal_session_resume"] is True
    assert captured["start"]["extra_metadata"]["resumed_from_terminal_state"] == "completed"
    assert session.transcript_metadata_json["session_state"] == "open"


async def test_subagent_resume_empty_message_rejected_before_terminal_check():
    db = _FakeDB()
    agent = SimpleNamespace(id=uuid.uuid4(), tenant_id=uuid.uuid4())
    user = SimpleNamespace(id=uuid.uuid4())
    session = _session("completed")

    result = await asc.continue_agent_session_from_mailbox(
        db=db,
        agent=agent,
        user=user,
        session=session,
        message="   ",
    )
    assert result == {"ok": False, "status": "rejected", "reason": "empty_message"}


def test_subagent_resume_boundary_terminal_state_set_is_explicit():
    # Terminal state detection still matters: sub-agent terminal states enter the
    # CC-compatible continuation branch, while unrelated terminal sessions remain sealed.
    assert "completed" in asc._TERMINAL_SESSION_STATES
    assert "failed" in asc._TERMINAL_SESSION_STATES
    assert "cancelled" in asc._TERMINAL_SESSION_STATES
    assert "open" not in asc._TERMINAL_SESSION_STATES
