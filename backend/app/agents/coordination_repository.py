"""PostgreSQL-backed coordination repository (Phase 14 redo).

Replaces the local-sqlite shim. All four primitives — Lease, Signal,
Checkpoint, and Sentinel-emitted variants — share a single async
session, are tenant-scoped via `tenant_id`, and rely on PostgreSQL
semantics (`UNIQUE` + `INSERT ... ON CONFLICT`, row locks) for
cross-worker correctness.

Sentinel state stays in-process and is not persisted — it is re-derived
per proactive-loop tick.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Callable

from sqlalchemy import delete, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.coordination import Checkpoint, Lease, LeaseAcquireResult, Signal
from app.models.charter_proposal import CharterProposal as CharterProposalRow
from app.models.coordination import (
    CoordinationCheckpoint,
    CoordinationLease,
    CoordinationSignal,
)


class CoordinationRepository:
    """Tenant-scoped coordination repo using AsyncSession + asyncpg."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._now = now or (lambda: datetime.now(UTC))

    async def acquire_lease(self, *, task_key: str, agent_id: str, ttl_seconds: int) -> LeaseAcquireResult:
        """Race-safe lease acquisition.

        Strategy: `INSERT ... ON CONFLICT (tenant_id, task_key) DO UPDATE`
        — only steals an existing lease when its `expires_at` has already
        passed, atomically inside one statement. The `RETURNING` clause
        tells us whether we got the new row or saw an existing live one.
        """
        current = self._now()
        new_id = uuid.uuid4()
        expires_at = current + timedelta(seconds=ttl_seconds)

        stmt = (
            pg_insert(CoordinationLease)
            .values(
                id=new_id,
                tenant_id=self._tenant_id,
                task_key=task_key,
                agent_id=agent_id,
                expires_at=expires_at,
            )
            .on_conflict_do_update(
                index_elements=["tenant_id", "task_key"],
                set_={
                    "id": new_id,
                    "agent_id": agent_id,
                    "expires_at": expires_at,
                },
                where=CoordinationLease.expires_at <= current,
            )
            .returning(
                CoordinationLease.id,
                CoordinationLease.agent_id,
                CoordinationLease.expires_at,
            )
        )
        result = await self._session.execute(stmt)
        row = result.first()
        await self._session.flush()

        if row is not None and row.id == new_id:
            return LeaseAcquireResult(
                acquired=True,
                lease=Lease(id=str(new_id), task_key=task_key, agent_id=agent_id, expires_at=expires_at),
            )

        existing = await self._session.execute(
            select(
                CoordinationLease.id,
                CoordinationLease.agent_id,
                CoordinationLease.expires_at,
            ).where(
                CoordinationLease.tenant_id == self._tenant_id,
                CoordinationLease.task_key == task_key,
            )
        )
        existing_row = existing.first()
        existing_lease = (
            Lease(
                id=str(existing_row.id),
                task_key=task_key,
                agent_id=str(existing_row.agent_id),
                expires_at=existing_row.expires_at,
            )
            if existing_row is not None
            else None
        )
        return LeaseAcquireResult(
            acquired=False,
            lease=existing_lease,
            existing_lease_id=existing_lease.id if existing_lease is not None else None,
        )

    async def release_lease(
        self,
        *,
        task_key: str,
        agent_id: str,
        lease_id: str | None = None,
    ) -> bool:
        """Delete only a tenant-scoped lease still owned by this exact task."""
        conditions = [
            CoordinationLease.tenant_id == self._tenant_id,
            CoordinationLease.task_key == task_key,
            CoordinationLease.agent_id == agent_id,
        ]
        if lease_id is not None:
            try:
                conditions.append(CoordinationLease.id == uuid.UUID(str(lease_id)))
            except (TypeError, ValueError, AttributeError):
                return False
        result = await self._session.execute(delete(CoordinationLease).where(*conditions))
        released = bool(result.rowcount)
        if released:
            await self._session.flush()
        return released

    async def send_signal(
        self,
        *,
        from_agent_id: str,
        to_agent_id: str,
        content: str,
        signal_type: str,
        thread_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Signal:
        new_id = uuid.uuid4()
        created_at = self._now()
        thread = thread_id or str(uuid.uuid4())
        metadata_payload = dict(metadata or {})
        row = CoordinationSignal(
            id=new_id,
            tenant_id=self._tenant_id,
            from_agent_id=from_agent_id,
            to_agent_id=to_agent_id,
            content=content,
            signal_type=signal_type,
            thread_id=thread,
            created_at=created_at,
            metadata_json=metadata_payload,
        )
        self._session.add(row)
        await self._session.flush()
        return Signal(
            id=str(new_id),
            from_agent_id=from_agent_id,
            to_agent_id=to_agent_id,
            content=content,
            signal_type=signal_type,
            thread_id=thread,
            created_at=created_at,
            metadata=metadata_payload,
        )

    async def read_signals(self, agent_id: str, *, thread_id: str | None = None) -> list[Signal]:
        stmt = select(CoordinationSignal).where(
            CoordinationSignal.tenant_id == self._tenant_id,
            CoordinationSignal.to_agent_id == agent_id,
        )
        if thread_id is not None:
            stmt = stmt.where(CoordinationSignal.thread_id == thread_id)
        stmt = stmt.order_by(CoordinationSignal.created_at)
        result = await self._session.execute(stmt)
        rows = result.scalars().all()
        return [
            Signal(
                id=str(row.id),
                from_agent_id=row.from_agent_id,
                to_agent_id=row.to_agent_id,
                content=row.content,
                signal_type=row.signal_type,
                thread_id=row.thread_id,
                created_at=row.created_at,
                metadata=dict(getattr(row, "metadata_json", None) or {}),
            )
            for row in rows
        ]

    async def create_checkpoint(
        self,
        *,
        action: str,
        approver_id: str,
        escalation_chain: list[str],
        deadline_at: datetime,
        metadata: dict[str, str] | None = None,
    ) -> Checkpoint:
        new_id = uuid.uuid4()
        chain = list(escalation_chain)
        meta = dict(metadata or {})
        row = CoordinationCheckpoint(
            id=new_id,
            tenant_id=self._tenant_id,
            action=action,
            approver_id=approver_id,
            escalation_chain=chain,
            deadline_at=deadline_at,
            current_approver_id=approver_id,
            status="pending",
            extra_metadata=meta,
        )
        self._session.add(row)
        await self._session.flush()
        return Checkpoint(
            id=str(new_id),
            action=action,
            approver_id=approver_id,
            escalation_chain=chain,
            deadline_at=deadline_at,
            current_approver_id=approver_id,
            status="pending",
            metadata=meta,
        )

    async def get_checkpoint(self, checkpoint_id: str) -> Checkpoint | None:
        result = await self._session.execute(
            select(CoordinationCheckpoint).where(
                CoordinationCheckpoint.tenant_id == self._tenant_id,
                CoordinationCheckpoint.id == uuid.UUID(checkpoint_id),
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            return None
        return Checkpoint(
            id=str(row.id),
            action=row.action,
            approver_id=row.approver_id,
            escalation_chain=list(row.escalation_chain or []),
            deadline_at=row.deadline_at,
            current_approver_id=row.current_approver_id,
            status=row.status,
            metadata=dict(row.extra_metadata or {}),
        )

    async def escalate_expired_checkpoints(self) -> list[str]:
        current = self._now()
        result = await self._session.execute(
            select(CoordinationCheckpoint).where(
                CoordinationCheckpoint.tenant_id == self._tenant_id,
                CoordinationCheckpoint.status == "pending",
                CoordinationCheckpoint.deadline_at <= current,
            )
        )
        rows = result.scalars().all()
        escalated: list[str] = []
        for row in rows:
            chain = list(row.escalation_chain or [])
            if not chain:
                continue
            next_approver = chain.pop(0)
            new_meta = dict(row.extra_metadata or {})
            new_meta["escalated_at"] = current.isoformat()
            await self._session.execute(
                update(CoordinationCheckpoint)
                .where(CoordinationCheckpoint.id == row.id)
                .values(
                    current_approver_id=next_approver,
                    escalation_chain=chain,
                    extra_metadata=new_meta,
                )
            )
            escalated.append(str(row.id))
        if escalated:
            await self._session.flush()
        return escalated


__all__ = ["CoordinationRepository", "CharterProposalRow"]
