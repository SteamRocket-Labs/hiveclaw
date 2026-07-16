"""Crash recovery for durable Agent Team fan-out admission intents.

The Team runtime commits the complete requested set before starting any child.
This worker consumes only the mechanically incomplete rows left by a process
crash.  It never invents or summarizes work: recovery requires the exact
stored message, member, operation, principal, budget, and root identity.
"""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any
import uuid

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import async_session, enter_rls_bypass, tenant_scoped_session
from app.models.agent import Agent
from app.models.agent_team import AgentTeam, AgentTeamMember
from app.models.chat_session import ChatSession
from app.models.runtime_root_item import RuntimeRootItem
from app.models.user import User
from app.services.runtime_budget_service import RuntimeBudgetService


class TeamFanoutRecoveryInvariantError(RuntimeError):
    """Stored intent is incomplete, so mechanical recovery must hold it."""


@dataclass(frozen=True, slots=True)
class ClaimedTeamFanoutItem:
    id: uuid.UUID
    tenant_id: uuid.UUID
    root_runtime_task_id: uuid.UUID
    source_agent_id: uuid.UUID | None
    root_user_id: uuid.UUID | None
    root_session_id: str | None
    intent_key: str
    team_id: uuid.UUID | None
    member_id: uuid.UUID | None
    operation_id: str
    message: str
    display_content: str
    source: str
    ordinal: int
    budget_run_id: uuid.UUID | None
    reserve_new_team_sessions: bool
    interrupt_requested: bool
    attempt_count: int
    metadata: dict[str, Any]
    validation_error: str | None = None


def _uuid(value: Any, *, field: str, required: bool = True) -> uuid.UUID | None:
    if value is None and not required:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value or ""))
    except (TypeError, ValueError, AttributeError) as exc:
        if not required and not str(value or "").strip():
            return None
        raise ValueError(f"team fanout recovery {field} must be a UUID") from exc


def claimed_team_fanout_item(row: Any) -> ClaimedTeamFanoutItem:
    """Build one exact recovery command without adding semantic defaults."""

    metadata = dict(getattr(row, "metadata_json", None) or {})
    if metadata.get("schema") != "hive.runtime_root_team_intent.v1":
        raise ValueError("team fanout recovery schema is missing or unsupported")
    operation_id = str(metadata.get("operation_id") or "").strip()
    message = str(metadata.get("message") or "").strip()
    source = str(metadata.get("source") or "").strip()
    if not operation_id:
        raise ValueError("team fanout recovery operation_id is required")
    if not message:
        raise ValueError("team fanout recovery message is required")
    if not source:
        raise ValueError("team fanout recovery source is required")
    try:
        ordinal = int(metadata.get("ordinal"))
    except (TypeError, ValueError) as exc:
        raise ValueError("team fanout recovery ordinal is required") from exc
    if ordinal < 0:
        raise ValueError("team fanout recovery ordinal must be non-negative")
    return ClaimedTeamFanoutItem(
        id=_uuid(getattr(row, "id", None), field="id"),  # type: ignore[arg-type]
        tenant_id=_uuid(getattr(row, "tenant_id", None), field="tenant_id"),  # type: ignore[arg-type]
        root_runtime_task_id=_uuid(getattr(row, "root_runtime_task_id", None), field="root_runtime_task_id"),  # type: ignore[arg-type]
        source_agent_id=_uuid(getattr(row, "source_agent_id", None), field="source_agent_id", required=False),
        root_user_id=_uuid(getattr(row, "root_user_id", None), field="root_user_id", required=False),
        root_session_id=str(getattr(row, "root_session_id", None) or "").strip() or None,
        intent_key=str(getattr(row, "intent_key", "") or "").strip(),
        team_id=_uuid(metadata.get("team_id"), field="team_id"),
        member_id=_uuid(metadata.get("member_id"), field="member_id"),
        operation_id=operation_id,
        message=message,
        display_content=str(metadata.get("display_content") or ""),
        source=source,
        ordinal=ordinal,
        budget_run_id=_uuid(metadata.get("budget_run_id"), field="budget_run_id", required=False),
        reserve_new_team_sessions=bool(metadata.get("reserve_new_team_sessions")),
        interrupt_requested=bool(metadata.get("interrupt_requested")),
        attempt_count=int(getattr(row, "recovery_attempt_count", 0) or 0),
        metadata=metadata,
    )


def _invalid_claimed_team_fanout_item(row: Any, error: ValueError) -> ClaimedTeamFanoutItem:
    metadata = dict(getattr(row, "metadata_json", None) or {})
    try:
        ordinal = max(0, int(metadata.get("ordinal") or 0))
    except (TypeError, ValueError):
        ordinal = 0
    return ClaimedTeamFanoutItem(
        id=getattr(row, "id"),
        tenant_id=getattr(row, "tenant_id"),
        root_runtime_task_id=getattr(row, "root_runtime_task_id"),
        source_agent_id=getattr(row, "source_agent_id", None),
        root_user_id=getattr(row, "root_user_id", None),
        root_session_id=str(getattr(row, "root_session_id", None) or "").strip() or None,
        intent_key=str(getattr(row, "intent_key", "") or "").strip(),
        team_id=None,
        member_id=None,
        operation_id=str(metadata.get("operation_id") or "").strip(),
        message=str(metadata.get("message") or ""),
        display_content=str(metadata.get("display_content") or ""),
        source=str(metadata.get("source") or "").strip(),
        ordinal=ordinal,
        budget_run_id=None,
        reserve_new_team_sessions=bool(metadata.get("reserve_new_team_sessions")),
        interrupt_requested=bool(metadata.get("interrupt_requested")),
        attempt_count=int(getattr(row, "recovery_attempt_count", 0) or 0),
        metadata=metadata,
        validation_error=str(error),
    )


Delivery = Callable[[ClaimedTeamFanoutItem], Awaitable[dict[str, Any]]]


class TeamFanoutRecoveryService:
    """Lease, recover, retry, and visibly hold incomplete Team admissions."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        lease_seconds: int = 60,
        retry_base_seconds: int = 2,
        max_attempts: int = 8,
    ) -> None:
        self._session_factory = session_factory or async_session
        self._lease_seconds = max(1, int(lease_seconds))
        self._retry_base_seconds = max(0, int(retry_base_seconds))
        self._max_attempts = max(1, int(max_attempts))

    @contextlib.asynccontextmanager
    async def _worker_session(self, operation: str):
        async with self._session_factory() as db:
            async with enter_rls_bypass(db, reason=f"team_fanout_recovery.{operation}") as bypass_db:
                yield bypass_db

    async def claim_batch(
        self,
        *,
        worker_id: str,
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[ClaimedTeamFanoutItem]:
        current = now or datetime.now(UTC)
        claim_expiry = current + timedelta(seconds=self._lease_seconds)
        async with self._worker_session("claim") as db:
            rows = list(
                (
                    await db.execute(
                        select(RuntimeRootItem)
                        .where(
                            RuntimeRootItem.work_type == "team_member",
                            RuntimeRootItem.state == "requested",
                            RuntimeRootItem.runtime_task_id.is_(None),
                            or_(
                                RuntimeRootItem.next_recovery_at.is_(None),
                                RuntimeRootItem.next_recovery_at <= current,
                            ),
                            or_(
                                RuntimeRootItem.recovery_claim_expires_at.is_(None),
                                RuntimeRootItem.recovery_claim_expires_at <= current,
                            ),
                        )
                        .order_by(RuntimeRootItem.next_recovery_at.asc().nullsfirst(), RuntimeRootItem.created_at)
                        .limit(max(1, int(limit)))
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            claimed: list[ClaimedTeamFanoutItem] = []
            for row in rows:
                row.recovery_claimed_by = str(worker_id)
                row.recovery_claim_expires_at = claim_expiry
                row.recovery_attempt_count = int(row.recovery_attempt_count or 0) + 1
                row.version = int(row.version or 0) + 1
                try:
                    claimed.append(claimed_team_fanout_item(row))
                except ValueError as exc:
                    claimed.append(_invalid_claimed_team_fanout_item(row, exc))
            await db.commit()
            return claimed

    @staticmethod
    def _release_claim(row: RuntimeRootItem) -> None:
        row.recovery_claimed_by = None
        row.recovery_claim_expires_at = None

    async def _mark_not_admitted(
        self,
        db: AsyncSession,
        row: RuntimeRootItem,
        *,
        reason_code: str,
        missing: list[str],
    ) -> dict[str, Any]:
        now = datetime.now(UTC)
        row.state = "not_admitted"
        row.admission_disposition = "not_admitted"
        row.reason_code = reason_code
        row.terminal_at = now
        self._release_claim(row)
        row.metadata_json = {
            **dict(row.metadata_json or {}),
            "recovery_outcome": {
                "status": "not_admitted",
                "reason_code": reason_code,
                "missing": missing,
                "observed_at": now.isoformat(),
            },
        }
        row.version = int(row.version or 0) + 1
        await db.flush()
        return {"status": "not_admitted", "reason_code": reason_code, "missing": missing}

    async def _deliver(self, item: ClaimedTeamFanoutItem) -> dict[str, Any]:
        if item.validation_error:
            raise TeamFanoutRecoveryInvariantError(item.validation_error)
        if item.team_id is None or item.member_id is None:
            raise TeamFanoutRecoveryInvariantError("team/member identity is missing")
        if item.source_agent_id is None or item.root_user_id is None:
            raise TeamFanoutRecoveryInvariantError("stored principal identity is missing")

        async with tenant_scoped_session(
            item.tenant_id,
            session_factory=self._session_factory,
            require_tenant=True,
            source="team_fanout_recovery_delivery",
        ) as db:
            row = (
                await db.execute(
                    select(RuntimeRootItem)
                    .where(
                        RuntimeRootItem.id == item.id,
                        RuntimeRootItem.tenant_id == item.tenant_id,
                    )
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                return {"status": "already_recovered", "reason_code": "root_item_missing"}
            if row.state != "requested" or row.runtime_task_id is not None:
                self._release_claim(row)
                await db.flush()
                return {"status": "already_recovered", "state": row.state}

            team = (
                await db.execute(
                    select(AgentTeam).where(
                        AgentTeam.id == item.team_id,
                        AgentTeam.tenant_id == item.tenant_id,
                        AgentTeam.lead_agent_id == item.source_agent_id,
                        AgentTeam.status == "active",
                    )
                )
            ).scalar_one_or_none()
            member = (
                await db.execute(
                    select(AgentTeamMember).where(
                        AgentTeamMember.id == item.member_id,
                        AgentTeamMember.team_id == item.team_id,
                    )
                )
            ).scalar_one_or_none()
            agent = (
                await db.execute(
                    select(Agent).where(
                        Agent.id == item.source_agent_id,
                        Agent.tenant_id == item.tenant_id,
                    )
                )
            ).scalar_one_or_none()
            user = (
                await db.execute(
                    select(User).where(
                        User.id == item.root_user_id,
                        User.tenant_id == item.tenant_id,
                    )
                )
            ).scalar_one_or_none()
            session = None
            if member is not None:
                session = (
                    await db.execute(
                        select(ChatSession).where(
                            ChatSession.id == member.chat_session_id,
                            ChatSession.tenant_id == item.tenant_id,
                            ChatSession.agent_id == item.source_agent_id,
                        )
                    )
                ).scalar_one_or_none()
            missing = [
                name
                for name, value in (
                    ("team", team),
                    ("member", member),
                    ("agent", agent),
                    ("user", user),
                    ("session", session),
                )
                if value is None
            ]
            if missing:
                return await self._mark_not_admitted(
                    db,
                    row,
                    reason_code="team_fanout_recovery_authority_unavailable",
                    missing=missing,
                )

            from app.services.agent_team_runtime_service import message_agent_team_members_runtime

            payload = await message_agent_team_members_runtime(
                db=db,
                agent=agent,
                user=user,
                team=team,
                members=[member],
                member_sessions=[session],
                message=item.message,
                display_content=item.display_content,
                interrupt_requested=item.interrupt_requested,
                source=item.source,
                budget_run_id=item.budget_run_id,
                budget_service=(
                    RuntimeBudgetService(session_factory=self._session_factory)
                    if item.budget_run_id is not None
                    else None
                ),
                root_runtime_task_id=item.root_runtime_task_id,
                operation_id=item.operation_id,
                reserve_new_team_sessions=item.reserve_new_team_sessions,
                fanout_ordinal_overrides={item.member_id: item.ordinal},
            )
            statuses = {str(result.get("status") or "") for result in payload.get("results") or []}
            if "deferred" in statuses:
                raise RuntimeError("team member admission remained deferred after recovery")
            if "waiting_budget_approval" in statuses:
                return {"status": "waiting_budget_approval", "payload": payload}
            if "rejected" in statuses:
                return {"status": "not_admitted", "payload": payload}
            return {"status": "recovered", "payload": payload}

    async def _ack_recovered(
        self,
        item: ClaimedTeamFanoutItem,
        *,
        receipt: dict[str, Any],
        now: datetime | None = None,
    ) -> str:
        current = now or datetime.now(UTC)
        async with self._worker_session("ack") as db:
            row = (
                await db.execute(select(RuntimeRootItem).where(RuntimeRootItem.id == item.id).with_for_update())
            ).scalar_one_or_none()
            if row is None:
                return "recovered"
            if row.state == "requested" and row.runtime_task_id is None:
                row.state = "needs_reconciliation"
                row.admission_disposition = "deferred"
                row.reason_code = "team_fanout_delivery_missing_durable_enqueue"
                outcome = "needs_reconciliation"
            else:
                outcome = "recovered"
            self._release_claim(row)
            row.next_recovery_at = None
            row.metadata_json = {
                **dict(row.metadata_json or {}),
                "recovery_receipt": {
                    "status": str(receipt.get("status") or outcome),
                    "attempt_count": item.attempt_count,
                    "observed_at": current.isoformat(),
                },
            }
            row.version = int(row.version or 0) + 1
            await db.commit()
            return outcome

    async def _hold(
        self,
        item: ClaimedTeamFanoutItem,
        *,
        error: str,
        now: datetime | None = None,
    ) -> str:
        current = now or datetime.now(UTC)
        async with self._worker_session("hold") as db:
            row = (
                await db.execute(select(RuntimeRootItem).where(RuntimeRootItem.id == item.id).with_for_update())
            ).scalar_one_or_none()
            if row is None:
                return "recovered"
            if row.state != "requested" or row.runtime_task_id is not None:
                self._release_claim(row)
                await db.commit()
                return "recovered"
            row.state = "needs_reconciliation"
            row.admission_disposition = "deferred"
            row.reason_code = "team_fanout_recovery_invariant_missing"
            self._release_claim(row)
            row.next_recovery_at = None
            row.metadata_json = {
                **dict(row.metadata_json or {}),
                "recovery_failure": {
                    "status": "needs_reconciliation",
                    "error": error,
                    "attempt_count": item.attempt_count,
                    "observed_at": current.isoformat(),
                },
            }
            row.version = int(row.version or 0) + 1
            await db.commit()
            return "needs_reconciliation"

    async def _retry_or_hold(
        self,
        item: ClaimedTeamFanoutItem,
        *,
        error: str,
        now: datetime | None = None,
    ) -> str:
        current = now or datetime.now(UTC)
        async with self._worker_session("retry") as db:
            row = (
                await db.execute(select(RuntimeRootItem).where(RuntimeRootItem.id == item.id).with_for_update())
            ).scalar_one_or_none()
            if row is None or row.state != "requested" or row.runtime_task_id is not None:
                if row is not None:
                    self._release_claim(row)
                    await db.commit()
                return "recovered"
            if item.attempt_count >= self._max_attempts:
                row.state = "needs_reconciliation"
                row.admission_disposition = "deferred"
                row.reason_code = "team_fanout_recovery_exhausted"
                row.next_recovery_at = None
                outcome = "needs_reconciliation"
            else:
                retry_seconds = min(300, self._retry_base_seconds * (2 ** max(0, item.attempt_count - 1)))
                row.reason_code = "team_fanout_recovery_retry"
                row.next_recovery_at = current + timedelta(seconds=retry_seconds)
                outcome = "retried"
            self._release_claim(row)
            row.metadata_json = {
                **dict(row.metadata_json or {}),
                "recovery_failure": {
                    "status": outcome,
                    "error": error,
                    "attempt_count": item.attempt_count,
                    "observed_at": current.isoformat(),
                },
            }
            row.version = int(row.version or 0) + 1
            await db.commit()
            return outcome

    async def drain_once(
        self,
        *,
        worker_id: str,
        limit: int = 100,
        deliver: Delivery | None = None,
    ) -> dict[str, int]:
        items = await self.claim_batch(worker_id=worker_id, limit=limit)
        counts = {
            "claimed": len(items),
            "recovered": 0,
            "retried": 0,
            "needs_reconciliation": 0,
        }
        callback = deliver or self._deliver
        for item in items:
            try:
                receipt = await callback(item)
            except TeamFanoutRecoveryInvariantError as exc:
                outcome = await self._hold(item, error=str(exc))
            except Exception as exc:  # noqa: BLE001 - each durable intent retries independently.
                outcome = await self._retry_or_hold(
                    item,
                    error=f"{type(exc).__name__}: {exc}",
                )
            else:
                outcome = await self._ack_recovered(item, receipt=receipt)
            counts[outcome] = int(counts.get(outcome) or 0) + 1
        return counts
