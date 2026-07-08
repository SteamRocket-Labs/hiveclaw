from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _LifecycleDb:
    def __init__(self, participant=None, *, require_no_autoflush: bool = False) -> None:
        self.participant = participant
        self.require_no_autoflush = require_no_autoflush
        self.added: list[object] = []
        self.statements: list[str] = []
        self.flushes = 0
        self.no_autoflush_entries = 0
        self._no_autoflush_depth = 0

    @property
    def no_autoflush(self):
        db = self

        class _NoAutoflush:
            def __enter__(self):
                db.no_autoflush_entries += 1
                db._no_autoflush_depth += 1

            def __exit__(self, exc_type, exc, tb):
                db._no_autoflush_depth -= 1

        return _NoAutoflush()

    async def execute(self, stmt):
        if self.require_no_autoflush:
            assert self._no_autoflush_depth > 0
        self.statements.append(str(stmt))
        return _ScalarResult(self.participant)

    def add(self, obj):
        if getattr(obj, "id", None) is None:
            obj.id = uuid4()
        self.added.append(obj)

    async def flush(self):
        self.flushes += 1


def test_lifecycle_block_reason_covers_deleted_deactivated_and_inactive_sponsor() -> None:
    from app.services.agent_identity_lifecycle import get_agent_lifecycle_block_reason

    assert (
        get_agent_lifecycle_block_reason(
            SimpleNamespace(deleted_at=object(), deactivated_at=None, sponsor=SimpleNamespace(is_active=True))
        )
        == "deleted"
    )
    assert (
        get_agent_lifecycle_block_reason(
            SimpleNamespace(deleted_at=None, deactivated_at=object(), sponsor=SimpleNamespace(is_active=True))
        )
        == "deactivated"
    )
    assert (
        get_agent_lifecycle_block_reason(
            SimpleNamespace(deleted_at=None, deactivated_at=None, sponsor=SimpleNamespace(is_active=False))
        )
        == "inactive_sponsor"
    )
    assert (
        get_agent_lifecycle_block_reason(
            SimpleNamespace(deleted_at=None, deactivated_at=None, sponsor=SimpleNamespace(is_active=True))
        )
        is None
    )


@pytest.mark.asyncio
async def test_ensure_agent_identity_backfills_sponsor_and_participant() -> None:
    from app.models.participant import Participant
    from app.services.agent_identity_lifecycle import ensure_agent_identity

    owner_id = uuid4()
    agent = SimpleNamespace(
        id=uuid4(),
        name="Finance Analyst",
        avatar_url="https://example.test/avatar.png",
        creator_id=uuid4(),
        owner_user_id=owner_id,
        sponsor_user_id=None,
        participant_id=None,
    )
    db = _LifecycleDb(participant=None, require_no_autoflush=True)

    participant_id = await ensure_agent_identity(db, agent)

    assert agent.sponsor_user_id == owner_id
    assert agent.participant_id == participant_id
    assert len(db.added) == 1
    assert isinstance(db.added[0], Participant)
    assert db.added[0].type == "agent"
    assert db.added[0].ref_id == agent.id
    assert db.added[0].display_name == "Finance Analyst"
    assert db.flushes == 1
    assert db.no_autoflush_entries == 1


@pytest.mark.asyncio
async def test_ensure_agent_identity_can_use_audited_rls_bypass(monkeypatch) -> None:
    import contextlib

    import app.services.agent_identity_lifecycle as lifecycle

    owner_id = uuid4()
    actor_id = uuid4()
    reason = "hr digital employee identity bootstrap"
    agent = SimpleNamespace(
        id=uuid4(),
        name="EventPilot",
        avatar_url=None,
        creator_id=uuid4(),
        owner_user_id=owner_id,
        sponsor_user_id=None,
        participant_id=None,
    )
    db = _LifecycleDb(participant=None, require_no_autoflush=True)
    bypass_calls = []

    @contextlib.asynccontextmanager
    async def fake_enter_rls_bypass(session, *, reason, actor_id=None):
        bypass_calls.append({"session": session, "reason": reason, "actor_id": actor_id})
        yield session

    monkeypatch.setattr(lifecycle, "enter_rls_bypass", fake_enter_rls_bypass)

    participant_id = await lifecycle.ensure_agent_identity(
        db,
        agent,
        rls_bypass_reason=reason,
        rls_bypass_actor_id=str(actor_id),
    )

    assert agent.participant_id == participant_id
    assert bypass_calls == [{"session": db, "reason": reason, "actor_id": str(actor_id)}]
    assert db.flushes == 1


@pytest.mark.asyncio
async def test_soft_delete_agent_preserves_identity_and_disables_execution_entrypoints() -> None:
    from app.services.agent_identity_lifecycle import soft_delete_agent

    agent = SimpleNamespace(
        id=uuid4(),
        status="idle",
        deleted_at=None,
        deactivated_at=None,
        deactivation_reason=None,
        container_id="container-123",
        container_port=18888,
        participant_id=uuid4(),
    )
    db = _LifecycleDb()

    await soft_delete_agent(db, agent, actor_id=uuid4(), reason="owner offboarded")

    assert agent.deleted_at is not None
    assert agent.deactivated_at == agent.deleted_at
    assert agent.deactivation_reason == "owner offboarded"
    assert agent.status == "stopped"
    assert agent.container_id is None
    assert agent.container_port is None
    assert agent.participant_id is not None
    sql = "\n".join(db.statements)
    assert "UPDATE agent_triggers SET is_enabled" in sql
    assert "UPDATE agent_schedules SET is_enabled" in sql
    assert "UPDATE runtime_tasks SET status" in sql
    assert "status IN" in sql
    assert db.flushes == 1
