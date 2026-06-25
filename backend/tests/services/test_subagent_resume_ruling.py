"""CCPlus V1 D-16 ruling test: completed-subagent-resume is Hive-native non-parity.

Makes ``pytest -k subagent_resume`` collect a REAL, revert-sensitive test proving
the deliberate new-spawn-only ruling (see
docs/ccplus-v1-subagent-resume-ruling-2026-06-24.md). A terminal subagent session
is sealed audit truth: continuation is rejected in place and explicitly redirected
to a fresh spawn, rather than reopening the sealed session.
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


async def test_subagent_resume_terminal_session_rejected_with_new_spawn_redirect(monkeypatch):
    recorded: list[dict] = []

    async def _fake_append(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(asc, "append_session_event", _fake_append)
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

    # The ruling: terminal session is rejected AND explicitly redirected to new spawn
    # (revert-sensitive: removing the D-16 ruling fields makes these assertions fail).
    assert result["ok"] is False
    assert result["status"] == "rejected"
    assert result["reason"] == "terminal_agent_session"
    assert result["resumable"] is False
    assert result["redirect"] == "spawn_new_session"
    assert db.committed is True

    # The rejection is durably recorded as a mailbox event carrying the redirect ruling.
    assert recorded, "a rejection mailbox event must be appended"
    assert recorded[0]["event_type"] == "agent_session_message_rejected"
    assert recorded[0]["metadata"]["redirect"] == "spawn_new_session"
    assert recorded[0]["metadata"]["resumable"] is False


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


def test_subagent_resume_ruling_boundary_only_terminal_states_sealed():
    # The new-spawn-only ruling applies ONLY to terminal states; open sessions still
    # resume in place (proves the ruling is scoped, not a blanket block).
    assert "completed" in asc._TERMINAL_SESSION_STATES
    assert "failed" in asc._TERMINAL_SESSION_STATES
    assert "cancelled" in asc._TERMINAL_SESSION_STATES
    assert "open" not in asc._TERMINAL_SESSION_STATES
