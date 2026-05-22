"""Phase 15 redo: PostgreSQL-backed charter proposal store tests."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from app.services.charter_proposals import (
    CharterProposalStore,
    ProposalAlreadyDecided,
    ProposalKind,
    ProposalStatus,
)


class _ExecuteResult:
    def __init__(
        self,
        *,
        scalar: Any = None,
        scalars: list[Any] | None = None,
    ) -> None:
        self._scalar = scalar
        self._scalars = scalars

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


class _Row:
    def __init__(self, **kwargs) -> None:
        for key, value in kwargs.items():
            setattr(self, key, value)


@pytest.fixture
def session() -> _FakeSession:
    return _FakeSession()


@pytest.fixture
def tenant_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def now() -> datetime:
    return datetime(2026, 5, 22, 12, 0, tzinfo=timezone.utc)


class TestSubmit:
    @pytest.mark.asyncio
    async def test_submit_persists_row_with_pending_status(
        self, session: _FakeSession, tenant_id: uuid.UUID, now: datetime
    ) -> None:
        store = CharterProposalStore(session, tenant_id=tenant_id, now=lambda: now)
        proposal = await store.submit(
            agent_id="agent-1",
            decision_id="decision/dec-1",
            action="send_feishu_message",
            proposal_kind=ProposalKind.CONSIDER_FULL_AUTHORITY,
            reason="Owner approved twice in confirm_first",
        )
        assert proposal.status == "pending"
        assert proposal.decided_at is None
        assert proposal.proposal_kind == "consider_full_authority"
        assert len(session.added) == 1
        assert session.added[0].tenant_id == tenant_id
        assert session.flushes == 1


class TestGetAndList:
    @pytest.mark.asyncio
    async def test_get_decodes_row_or_returns_none(
        self, session: _FakeSession, tenant_id: uuid.UUID, now: datetime
    ) -> None:
        store = CharterProposalStore(session, tenant_id=tenant_id, now=lambda: now)
        row_id = uuid.uuid4()
        row = _Row(
            id=row_id,
            agent_id="agent-1",
            decision_id="decision/dec-1",
            action="send_feishu_message",
            proposal_kind="consider_full_authority",
            reason="repeat approval",
            status="pending",
            created_at=now,
            decided_at=None,
            decided_by=None,
            decision_reason=None,
        )
        session.queue(_ExecuteResult(scalar=row))
        loaded = await store.get(str(row_id))
        assert loaded is not None
        assert loaded.id == str(row_id)
        assert loaded.reason == "repeat approval"

        session.queue(_ExecuteResult(scalar=None))
        missing = await store.get(str(uuid.uuid4()))
        assert missing is None

    @pytest.mark.asyncio
    async def test_list_pending_returns_rows(self, session: _FakeSession, tenant_id: uuid.UUID, now: datetime) -> None:
        store = CharterProposalStore(session, tenant_id=tenant_id, now=lambda: now)
        row = _Row(
            id=uuid.uuid4(),
            agent_id="agent-1",
            decision_id="decision/dec-1",
            action="send_feishu_message",
            proposal_kind="consider_full_authority",
            reason="r",
            status="pending",
            created_at=now,
            decided_at=None,
            decided_by=None,
            decision_reason=None,
        )
        session.queue(_ExecuteResult(scalars=[row]))
        pending = await store.list_pending(agent_id="agent-1")
        assert len(pending) == 1
        assert pending[0].agent_id == "agent-1"


class TestApproveReject:
    @pytest.mark.asyncio
    async def test_approve_sets_decision_metadata(
        self, session: _FakeSession, tenant_id: uuid.UUID, now: datetime
    ) -> None:
        store = CharterProposalStore(session, tenant_id=tenant_id, now=lambda: now)
        row = _Row(
            id=uuid.uuid4(),
            agent_id="agent-1",
            decision_id="decision/dec-1",
            action="x",
            proposal_kind="consider_full_authority",
            reason="r",
            status="pending",
            created_at=now,
            decided_at=None,
            decided_by=None,
            decision_reason=None,
        )
        session.queue(
            _ExecuteResult(scalar=row),
            _ExecuteResult(),  # UPDATE statement
        )
        approved = await store.approve(str(row.id), by="alice", decision_reason="LGTM")
        assert approved.status == "approved"
        assert approved.decided_by == "alice"
        assert approved.decision_reason == "LGTM"
        assert approved.decided_at is not None
        assert session.flushes >= 1

    @pytest.mark.asyncio
    async def test_double_decision_raises(self, session: _FakeSession, tenant_id: uuid.UUID, now: datetime) -> None:
        store = CharterProposalStore(session, tenant_id=tenant_id, now=lambda: now)
        already_decided_row = _Row(
            id=uuid.uuid4(),
            agent_id="agent-1",
            decision_id="d",
            action="x",
            proposal_kind="consider_full_authority",
            reason="r",
            status="approved",
            created_at=now,
            decided_at=now,
            decided_by="alice",
            decision_reason=None,
        )
        session.queue(_ExecuteResult(scalar=already_decided_row))
        with pytest.raises(ProposalAlreadyDecided):
            await store.approve(str(already_decided_row.id), by="alice")

    @pytest.mark.asyncio
    async def test_decide_unknown_raises_keyerror(
        self, session: _FakeSession, tenant_id: uuid.UUID, now: datetime
    ) -> None:
        store = CharterProposalStore(session, tenant_id=tenant_id, now=lambda: now)
        session.queue(_ExecuteResult(scalar=None))
        with pytest.raises(KeyError):
            await store.approve(str(uuid.uuid4()), by="alice")


class TestExpire:
    @pytest.mark.asyncio
    async def test_expire_stale_marks_old_pending(
        self, session: _FakeSession, tenant_id: uuid.UUID, now: datetime
    ) -> None:
        store = CharterProposalStore(session, tenant_id=tenant_id, now=lambda: now)
        old_row = _Row(
            id=uuid.uuid4(),
            agent_id="agent-1",
            decision_id="d",
            action="x",
            proposal_kind="consider_full_authority",
            reason="stale",
            status="pending",
            created_at=now - timedelta(days=14),
            decided_at=None,
            decided_by=None,
            decision_reason=None,
        )
        session.queue(
            _ExecuteResult(scalars=[old_row]),
            _ExecuteResult(),  # UPDATE
        )
        expired = await store.expire_stale(max_age_days=7)
        assert expired == [str(old_row.id)]


class TestEnums:
    def test_proposal_status_values(self) -> None:
        assert ProposalStatus.PENDING.value == "pending"
        assert ProposalStatus.APPROVED.value == "approved"
        assert ProposalStatus.REJECTED.value == "rejected"
        assert ProposalStatus.EXPIRED.value == "expired"

    def test_proposal_kind_values(self) -> None:
        assert ProposalKind.CONSIDER_FULL_AUTHORITY.value == "consider_full_authority"
        assert ProposalKind.TIGHTEN_TO_CONFIRM_FIRST.value == "tighten_to_confirm_first"
