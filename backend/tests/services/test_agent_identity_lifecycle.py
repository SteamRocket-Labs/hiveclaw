from __future__ import annotations

import ast
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.parametrize(
    "relative_path",
    [
        "app/api/agents.py",
        "app/api/desktop_agents.py",
        "app/services/auto_provision.py",
        "app/services/agent_seeder.py",
        "app/services/hr_provisioning_runner.py",
    ],
)
def test_new_agent_identity_bootstraps_are_explicit_audited_rls_boundaries(relative_path: str) -> None:
    source = (Path(__file__).resolve().parents[2] / relative_path).read_text(encoding="utf-8")
    tree = ast.parse(source)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "ensure_agent_identity"
    ]
    assert calls, f"{relative_path} must create an agent identity through the lifecycle owner"
    for call in calls:
        keywords = {keyword.arg for keyword in call.keywords}
        assert "rls_bypass_reason" in keywords, f"{relative_path}:{call.lineno} lacks an audited bootstrap reason"
        assert "rls_bypass_actor_id" in keywords, f"{relative_path}:{call.lineno} lacks the creating actor"


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


def test_lifecycle_block_reason_uses_current_owner_not_historical_sponsor() -> None:
    from app.services.agent_identity_lifecycle import get_agent_lifecycle_block_reason

    assert (
        get_agent_lifecycle_block_reason(
            SimpleNamespace(deleted_at=object(), deactivated_at=None, owner=SimpleNamespace(is_active=True))
        )
        == "deleted"
    )
    assert (
        get_agent_lifecycle_block_reason(
            SimpleNamespace(deleted_at=None, deactivated_at=object(), owner=SimpleNamespace(is_active=True))
        )
        == "deactivated"
    )
    assert (
        get_agent_lifecycle_block_reason(
            SimpleNamespace(
                deleted_at=None,
                deactivated_at=None,
                owner_user_id=uuid4(),
                owner=SimpleNamespace(is_active=False),
                sponsor=SimpleNamespace(is_active=True),
            )
        )
        == "inactive_owner"
    )
    assert (
        get_agent_lifecycle_block_reason(
            SimpleNamespace(
                deleted_at=None,
                deactivated_at=None,
                owner_user_id=uuid4(),
                owner=SimpleNamespace(is_active=True),
                sponsor=SimpleNamespace(is_active=False),
            )
        )
        is None
    )


def test_lifecycle_legacy_owner_fallback_uses_creator_activity() -> None:
    from app.services.agent_identity_lifecycle import get_agent_lifecycle_block_reason

    assert (
        get_agent_lifecycle_block_reason(
            SimpleNamespace(
                deleted_at=None,
                deactivated_at=None,
                owner_user_id=None,
                owner=None,
                creator=SimpleNamespace(is_active=False),
            )
        )
        == "inactive_owner"
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
async def test_ensure_agent_identity_never_invents_autonomous_personal_knowledge_authority() -> None:
    from app.models.knowledge import KnowledgeGrant
    from app.services.agent_identity_lifecycle import ensure_agent_identity

    tenant_id = uuid4()
    owner_id = uuid4()
    agent = SimpleNamespace(
        id=uuid4(),
        name="Research Analyst",
        avatar_url=None,
        creator_id=owner_id,
        owner_user_id=owner_id,
        sponsor_user_id=None,
        participant_id=None,
        tenant_id=tenant_id,
    )
    db = _LifecycleDb(participant=None, require_no_autoflush=True)

    await ensure_agent_identity(db, agent)

    grants = [item for item in db.added if isinstance(item, KnowledgeGrant)]
    assert grants == []


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
