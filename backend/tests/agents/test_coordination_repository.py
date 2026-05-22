"""Phase 14 redo: PostgreSQL-backed coordination repository tests.

These tests follow Hive's existing pattern (see
`tests/services/test_objective_service.py`) of stubbing the async session
rather than spinning up a real Postgres. Cross-worker mutex semantics
are guaranteed by the `UNIQUE(tenant_id, task_key)` + `INSERT ...
ON CONFLICT` clause in the SQL itself (verified at migration time and
by manual smoke against asyncpg), not by these tests.
"""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.agents.coordination_repository import CoordinationRepository


class _ExecuteResult:
    def __init__(
        self,
        *,
        first: Any = None,
        scalar: Any = None,
        scalars: list[Any] | None = None,
    ) -> None:
        self._first = first
        self._scalar = scalar
        self._scalars = scalars

    def first(self):
        return self._first

    def scalar_one_or_none(self):
        return self._scalar

    def scalars(self):
        return _ScalarsView(self._scalars or [])


class _ScalarsView:
    def __init__(self, values: list[Any]) -> None:
        self._values = values

    def all(self):
        return list(self._values)


class _FakeSession:
    def __init__(self) -> None:
        self.added: list[Any] = []
        self.flushes = 0
        self.execute_calls: list[Any] = []
        self.results: list[_ExecuteResult] = []

    def queue(self, *results: _ExecuteResult) -> None:
        self.results.extend(results)

    async def execute(self, stmt):
        self.execute_calls.append(stmt)
        if self.results:
            return self.results.pop(0)
        return _ExecuteResult()

    def add(self, row):
        self.added.append(row)

    async def flush(self):
        self.flushes += 1


@pytest.fixture
def session() -> _FakeSession:
    return _FakeSession()


@pytest.fixture
def tenant_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)


class _Row:
    def __init__(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


class TestLease:
    @pytest.mark.asyncio
    async def test_acquire_inserts_new_lease_when_none_exists(
        self, session: _FakeSession, tenant_id: uuid.UUID, now: datetime
    ) -> None:
        repo = CoordinationRepository(session, tenant_id=tenant_id, now=lambda: now)
        # First execute is INSERT ... ON CONFLICT ... RETURNING — fake returns
        # a row whose `id` matches the just-generated id by inspecting the
        # generated statement after the fact. We pre-stage so the upsert
        # returns a synthetic row that signals "we inserted" — repository
        # logic compares returned id to its locally-generated new_id, so we
        # set the row to a sentinel id and override via test_acquire_returns_true.
        # Simpler: stage upsert to return None so the repo treats it as
        # "did not insert" and we exercise the SELECT path below in the
        # `not_acquired` test. For acquired=True, we rely on the repository's
        # own new_id; we wrap it by patching uuid4.
        await _acquire_with_known_id(repo, session, task_key="task-1", agent_id="agent_a", ttl_seconds=600)

    @pytest.mark.asyncio
    async def test_acquire_not_acquired_when_conflict_is_live(
        self, session: _FakeSession, tenant_id: uuid.UUID, now: datetime
    ) -> None:
        repo = CoordinationRepository(session, tenant_id=tenant_id, now=lambda: now)
        # Upsert returns no row (WHERE expires_at <= current was false) →
        # repository follows up with a SELECT to find the existing lease id.
        existing_id = uuid.uuid4()
        session.queue(
            _ExecuteResult(first=None),
            _ExecuteResult(scalar=existing_id),
        )
        result = await repo.acquire_lease(task_key="task-1", agent_id="agent_b", ttl_seconds=600)
        assert result.acquired is False
        assert result.existing_lease_id == str(existing_id)


async def _acquire_with_known_id(
    repo: CoordinationRepository,
    session: _FakeSession,
    *,
    task_key: str,
    agent_id: str,
    ttl_seconds: int,
) -> None:
    """Stage upsert to look like our generated row was inserted.

    The repository creates a fresh uuid inside `acquire_lease` and compares
    it to the row id returned by ON CONFLICT RETURNING. We can't know that
    id in advance, so we monkey-patch uuid4 to return a stable value, then
    pre-stage that exact id back to the fake.
    """
    import app.agents.coordination_repository as repo_module

    stable_id = uuid.uuid4()
    original_uuid4 = repo_module.uuid.uuid4
    repo_module.uuid.uuid4 = lambda: stable_id
    try:
        session.queue(_ExecuteResult(first=_Row(id=stable_id, agent_id=agent_id, expires_at=None)))
        result = await repo.acquire_lease(task_key=task_key, agent_id=agent_id, ttl_seconds=ttl_seconds)
        assert result.acquired is True
        assert result.lease is not None
        assert result.lease.id == str(stable_id)
        assert result.lease.task_key == task_key
        assert result.lease.agent_id == agent_id
        assert session.flushes >= 1
    finally:
        repo_module.uuid.uuid4 = original_uuid4


class TestSignal:
    @pytest.mark.asyncio
    async def test_send_signal_persists_row_and_returns_dataclass(
        self, session: _FakeSession, tenant_id: uuid.UUID, now: datetime
    ) -> None:
        repo = CoordinationRepository(session, tenant_id=tenant_id, now=lambda: now)
        signal = await repo.send_signal(
            from_agent_id="agent_a",
            to_agent_id="agent_b",
            content="started",
            signal_type="delegation_started",
            thread_id="thread-1",
        )
        assert signal.from_agent_id == "agent_a"
        assert signal.to_agent_id == "agent_b"
        assert signal.content == "started"
        assert signal.signal_type == "delegation_started"
        assert signal.thread_id == "thread-1"
        assert len(session.added) == 1
        added = session.added[0]
        assert added.tenant_id == tenant_id
        assert session.flushes == 1

    @pytest.mark.asyncio
    async def test_read_signals_returns_decoded_rows(
        self, session: _FakeSession, tenant_id: uuid.UUID, now: datetime
    ) -> None:
        repo = CoordinationRepository(session, tenant_id=tenant_id, now=lambda: now)
        row = _Row(
            id=uuid.uuid4(),
            from_agent_id="agent_a",
            to_agent_id="agent_b",
            content="started",
            signal_type="delegation_started",
            thread_id="t-1",
            created_at=now,
        )
        session.queue(_ExecuteResult(scalars=[row]))
        signals = await repo.read_signals("agent_b", thread_id="t-1")
        assert len(signals) == 1
        assert signals[0].content == "started"
        assert signals[0].thread_id == "t-1"


class TestCheckpoint:
    @pytest.mark.asyncio
    async def test_create_checkpoint_persists_row(
        self, session: _FakeSession, tenant_id: uuid.UUID, now: datetime
    ) -> None:
        repo = CoordinationRepository(session, tenant_id=tenant_id, now=lambda: now)
        deadline = now + timedelta(seconds=300)
        checkpoint = await repo.create_checkpoint(
            action="send_feishu_message",
            approver_id="alice",
            escalation_chain=["company_admin"],
            deadline_at=deadline,
            metadata={"reason": "external_visible"},
        )
        assert checkpoint.action == "send_feishu_message"
        assert checkpoint.approver_id == "alice"
        assert checkpoint.current_approver_id == "alice"
        assert checkpoint.escalation_chain == ["company_admin"]
        assert checkpoint.metadata == {"reason": "external_visible"}
        assert len(session.added) == 1
        assert session.added[0].tenant_id == tenant_id

    @pytest.mark.asyncio
    async def test_get_checkpoint_returns_none_when_missing(
        self, session: _FakeSession, tenant_id: uuid.UUID, now: datetime
    ) -> None:
        repo = CoordinationRepository(session, tenant_id=tenant_id, now=lambda: now)
        session.queue(_ExecuteResult(scalar=None))
        result = await repo.get_checkpoint(str(uuid.uuid4()))
        assert result is None

    @pytest.mark.asyncio
    async def test_get_checkpoint_decodes_row(self, session: _FakeSession, tenant_id: uuid.UUID, now: datetime) -> None:
        repo = CoordinationRepository(session, tenant_id=tenant_id, now=lambda: now)
        deadline = now + timedelta(seconds=300)
        row = _Row(
            id=uuid.uuid4(),
            action="send_feishu_message",
            approver_id="alice",
            escalation_chain=["company_admin"],
            deadline_at=deadline,
            current_approver_id="alice",
            status="pending",
            extra_metadata={"reason": "external_visible"},
        )
        session.queue(_ExecuteResult(scalar=row))
        result = await repo.get_checkpoint(str(row.id))
        assert result is not None
        assert result.action == "send_feishu_message"
        assert result.escalation_chain == ["company_admin"]
        assert result.metadata == {"reason": "external_visible"}

    @pytest.mark.asyncio
    async def test_escalate_expired_advances_approver(
        self, session: _FakeSession, tenant_id: uuid.UUID, now: datetime
    ) -> None:
        repo = CoordinationRepository(session, tenant_id=tenant_id, now=lambda: now)
        row = _Row(
            id=uuid.uuid4(),
            action="approve refund",
            approver_id="alice",
            escalation_chain=["company_admin"],
            deadline_at=now - timedelta(seconds=60),
            current_approver_id="alice",
            status="pending",
            extra_metadata={},
        )
        session.queue(
            _ExecuteResult(scalars=[row]),
            _ExecuteResult(),  # the UPDATE statement
        )
        escalated = await repo.escalate_expired_checkpoints()
        assert escalated == [str(row.id)]
        # repo issued: SELECT pending → UPDATE row
        assert len(session.execute_calls) == 2
        assert session.flushes >= 1
