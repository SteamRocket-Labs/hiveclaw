"""Charter calibration proposal store (Phase 15 redo).

PostgreSQL-backed durable hand-off between dream's
`propose_charter_calibrations_from_feedback()` and the owner /
company admin who approves or rejects each proposal. Replaces the
local-sqlite shim from the first Phase 15 implementation.

The store does not mutate any charter. Applying an approved proposal
back to soul / company charter remains the sketch->active path (Phase
6) and is out of scope here.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Callable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.charter_proposal import CharterProposal as CharterProposalRow


class ProposalKind(StrEnum):
    CONSIDER_FULL_AUTHORITY = "consider_full_authority"
    TIGHTEN_TO_CONFIRM_FIRST = "tighten_to_confirm_first"


class ProposalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ProposalAlreadyDecided(Exception):
    pass


@dataclass(slots=True)
class CharterProposal:
    id: str
    agent_id: str
    decision_id: str
    action: str
    proposal_kind: str
    reason: str
    status: str
    created_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_reason: str | None = None


class CharterProposalStore:
    """Tenant-scoped charter proposal repo backed by AsyncSession + asyncpg."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def submit(
        self,
        *,
        agent_id: str,
        decision_id: str,
        action: str,
        proposal_kind: ProposalKind | str,
        reason: str,
        created_at: datetime | None = None,
    ) -> CharterProposal:
        kind = ProposalKind(proposal_kind) if not isinstance(proposal_kind, ProposalKind) else proposal_kind
        new_id = uuid.uuid4()
        when = (created_at or self._now()).astimezone(timezone.utc)
        row = CharterProposalRow(
            id=new_id,
            tenant_id=self._tenant_id,
            agent_id=agent_id,
            decision_id=decision_id,
            action=action,
            proposal_kind=kind.value,
            reason=reason,
            status=ProposalStatus.PENDING.value,
            created_at=when,
        )
        self._session.add(row)
        await self._session.flush()
        return _row_to_dataclass(row)

    async def get(self, proposal_id: str) -> CharterProposal | None:
        result = await self._session.execute(
            select(CharterProposalRow).where(
                CharterProposalRow.tenant_id == self._tenant_id,
                CharterProposalRow.id == uuid.UUID(proposal_id),
            )
        )
        row = result.scalar_one_or_none()
        return _row_to_dataclass(row) if row else None

    async def list_pending(self, *, agent_id: str | None = None) -> list[CharterProposal]:
        stmt = select(CharterProposalRow).where(
            CharterProposalRow.tenant_id == self._tenant_id,
            CharterProposalRow.status == ProposalStatus.PENDING.value,
        )
        if agent_id is not None:
            stmt = stmt.where(CharterProposalRow.agent_id == agent_id)
        stmt = stmt.order_by(CharterProposalRow.created_at)
        result = await self._session.execute(stmt)
        return [_row_to_dataclass(row) for row in result.scalars().all()]

    async def approve(self, proposal_id: str, *, by: str, decision_reason: str | None = None) -> CharterProposal:
        return await self._decide(proposal_id, ProposalStatus.APPROVED, by=by, decision_reason=decision_reason)

    async def reject(self, proposal_id: str, *, by: str, decision_reason: str | None = None) -> CharterProposal:
        return await self._decide(proposal_id, ProposalStatus.REJECTED, by=by, decision_reason=decision_reason)

    async def _decide(
        self,
        proposal_id: str,
        target_status: ProposalStatus,
        *,
        by: str,
        decision_reason: str | None,
    ) -> CharterProposal:
        result = await self._session.execute(
            select(CharterProposalRow).where(
                CharterProposalRow.tenant_id == self._tenant_id,
                CharterProposalRow.id == uuid.UUID(proposal_id),
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise KeyError(f"Unknown charter proposal: {proposal_id}")
        if row.status != ProposalStatus.PENDING.value:
            raise ProposalAlreadyDecided(f"Proposal {proposal_id} already {row.status}")
        decided_at = self._now()
        await self._session.execute(
            update(CharterProposalRow)
            .where(CharterProposalRow.id == row.id)
            .values(
                status=target_status.value,
                decided_at=decided_at,
                decided_by=by,
                decision_reason=decision_reason,
            )
        )
        await self._session.flush()
        row.status = target_status.value
        row.decided_at = decided_at
        row.decided_by = by
        row.decision_reason = decision_reason
        return _row_to_dataclass(row)

    async def expire_stale(self, *, max_age_days: int = 7, now: datetime | None = None) -> list[str]:
        boundary = (now or self._now()).astimezone(timezone.utc) - timedelta(days=max_age_days)
        result = await self._session.execute(
            select(CharterProposalRow).where(
                CharterProposalRow.tenant_id == self._tenant_id,
                CharterProposalRow.status == ProposalStatus.PENDING.value,
                CharterProposalRow.created_at <= boundary,
            )
        )
        rows = result.scalars().all()
        expired_ids: list[str] = []
        for row in rows:
            await self._session.execute(
                update(CharterProposalRow)
                .where(CharterProposalRow.id == row.id)
                .values(status=ProposalStatus.EXPIRED.value)
            )
            expired_ids.append(str(row.id))
        if expired_ids:
            await self._session.flush()
        return expired_ids


def _row_to_dataclass(row: CharterProposalRow) -> CharterProposal:
    return CharterProposal(
        id=str(row.id),
        agent_id=row.agent_id,
        decision_id=row.decision_id,
        action=row.action,
        proposal_kind=row.proposal_kind,
        reason=row.reason,
        status=row.status,
        created_at=row.created_at,
        decided_at=row.decided_at,
        decided_by=row.decided_by,
        decision_reason=row.decision_reason,
    )
