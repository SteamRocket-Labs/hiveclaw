"""Runtime budget admission service.

This service owns the run-level budget envelope for autonomous work. It is
deliberately separate from account token quotas: quotas cap who may spend at an
account level, while runtime budgets cap how much one continuation chain may
amplify.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
import uuid

from sqlalchemy import Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import async_session, enter_rls_bypass
from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetPolicy, RuntimeBudgetRun
from app.models.runtime_task import RuntimeTask

_DIMENSIONS = (
    "tokens",
    "cache_miss_tokens",
    "subagents",
    "delegations",
    "background_tasks",
    "continuation_wakes",
    "provider_calls",
)

_TERMINAL_RUNTIME_TASK_STATUSES = {
    "completed",
    "failed",
    "killed",
    "cancelled",
    "needs_reconciliation",
    "unknown_requires_reconciliation",
}

_POLICY_SCOPE_RANK = {
    "agent_trigger": 600,
    "trigger": 500,
    "agent": 400,
    "source_profile": 300,
    "tenant_default": 200,
    "platform_default": 100,
}


class RuntimeBudgetDenied(Exception):
    """Raised when a budget reservation is denied."""

    def __init__(self, message: str, *, budget_run_id: uuid.UUID | None = None, dimensions: list[str] | None = None):
        self.budget_run_id = budget_run_id
        self.dimensions = dimensions or []
        super().__init__(message)


class RuntimeBudgetNotFound(RuntimeBudgetDenied):
    """Raised when the target budget run does not exist."""


@dataclass(frozen=True, slots=True)
class RuntimeBudgetPolicyLookup:
    tenant_id: uuid.UUID | None
    source: str | None = None
    profile: str | None = None
    agent_id: uuid.UUID | None = None
    trigger_id: uuid.UUID | None = None


@dataclass(frozen=True, slots=True)
class RuntimeBudgetRunCreate:
    tenant_id: uuid.UUID | None
    root_run_kind: str
    root_run_key: str
    source: str | None = None
    profile: str | None = None
    policy_id: uuid.UUID | None = None
    root_runtime_task_id: uuid.UUID | None = None
    root_session_id: str | None = None
    root_agent_id: uuid.UUID | None = None
    root_user_id: uuid.UUID | None = None
    enforcement_mode: str = "enforce"
    fail_mode: str = "fail_closed"
    max_tokens: int | None = None
    max_cache_miss_tokens: int | None = None
    max_subagents: int | None = None
    max_delegations: int | None = None
    max_background_tasks: int | None = None
    max_continuation_wakes: int | None = None
    max_provider_calls: int | None = None
    expires_at: datetime | None = None
    policy_snapshot: dict | None = None


@dataclass(frozen=True, slots=True)
class RuntimeBudgetReservation:
    budget_run_id: uuid.UUID
    reservation_key: str
    tokens: int = 0
    cache_miss_tokens: int = 0
    subagents: int = 0
    delegations: int = 0
    background_tasks: int = 0
    continuation_wakes: int = 0
    provider_calls: int = 0
    reason: str | None = None
    runtime_task_id: uuid.UUID | None = None
    metadata: dict | None = None


@dataclass(frozen=True, slots=True)
class RuntimeBudgetSettlement:
    budget_run_id: uuid.UUID
    reservation_key: str
    actual_tokens: int = 0
    actual_cache_miss_tokens: int = 0
    actual_subagents: int = 0
    actual_delegations: int = 0
    actual_background_tasks: int = 0
    actual_continuation_wakes: int = 0
    actual_provider_calls: int = 0
    reason: str | None = None
    runtime_task_id: uuid.UUID | None = None
    metadata: dict | None = None


@dataclass(frozen=True, slots=True)
class RuntimeBudgetReservationResult:
    allowed: bool
    would_deny: bool
    idempotent: bool
    budget_run_id: uuid.UUID
    denied_dimensions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class BudgetFailureContext:
    source: str
    interactive: bool
    work_amplifying: bool


@dataclass(frozen=True, slots=True)
class BudgetFailureDecision:
    fail_open: bool
    fail_closed: bool
    disable_work_amplifying_tools: bool
    reason: str


def _work_amplifying_amounts(amounts: dict[str, int]) -> bool:
    return any(
        amounts.get(dimension, 0) > 0
        for dimension in ("subagents", "delegations", "background_tasks", "continuation_wakes")
    )


def estimate_reservation_tokens(
    *,
    default_tokens: int | None,
    prompt_tokens: int | None = None,
    observed_floor_tokens: int | None = None,
) -> int:
    """Return the safe admission estimate for a token-consuming work unit."""

    candidates = [value for value in (default_tokens, prompt_tokens, observed_floor_tokens) if value is not None]
    return max([0, *[max(0, int(value)) for value in candidates]])


def decide_budget_service_failure(context: BudgetFailureContext) -> BudgetFailureDecision:
    """Fail-mode policy when the budget service cannot decide admission."""

    if context.interactive and not context.work_amplifying:
        return BudgetFailureDecision(
            fail_open=True,
            fail_closed=False,
            disable_work_amplifying_tools=True,
            reason="interactive_direct_response_budget_service_unavailable",
        )
    return BudgetFailureDecision(
        fail_open=False,
        fail_closed=True,
        disable_work_amplifying_tools=True,
        reason=f"{context.source or 'runtime'}_budget_service_unavailable",
    )


def _positive_amounts(payload: RuntimeBudgetReservation | RuntimeBudgetSettlement) -> dict[str, int]:
    if isinstance(payload, RuntimeBudgetSettlement):
        values = {
            "tokens": payload.actual_tokens,
            "cache_miss_tokens": payload.actual_cache_miss_tokens,
            "subagents": payload.actual_subagents,
            "delegations": payload.actual_delegations,
            "background_tasks": payload.actual_background_tasks,
            "continuation_wakes": payload.actual_continuation_wakes,
            "provider_calls": payload.actual_provider_calls,
        }
    else:
        values = {dimension: getattr(payload, dimension) for dimension in _DIMENSIONS}
    return {key: max(0, int(value or 0)) for key, value in values.items()}


def _policy_matches(policy: RuntimeBudgetPolicy, lookup: RuntimeBudgetPolicyLookup) -> bool:
    if policy.tenant_id is not None and policy.tenant_id != lookup.tenant_id:
        return False
    scope = policy.scope_type
    if scope == "platform_default":
        return policy.tenant_id is None
    if scope == "tenant_default":
        return policy.tenant_id == lookup.tenant_id
    if scope == "source_profile":
        if policy.tenant_id != lookup.tenant_id:
            return False
        source_matches = policy.source is None or policy.source == lookup.source
        profile_matches = policy.profile is None or policy.profile == lookup.profile
        return source_matches and profile_matches
    if scope == "agent":
        return policy.tenant_id == lookup.tenant_id and policy.agent_id == lookup.agent_id
    if scope == "trigger":
        return policy.tenant_id == lookup.tenant_id and policy.trigger_id == lookup.trigger_id
    if scope == "agent_trigger":
        return (
            policy.tenant_id == lookup.tenant_id
            and policy.agent_id == lookup.agent_id
            and policy.trigger_id == lookup.trigger_id
        )
    return False


def _policy_rank(policy: RuntimeBudgetPolicy) -> tuple[int, int]:
    return (_POLICY_SCOPE_RANK.get(policy.scope_type, 0), policy.priority or 0)


def _builtin_policy(lookup: RuntimeBudgetPolicyLookup) -> RuntimeBudgetPolicy:
    return RuntimeBudgetPolicy(
        id=uuid.uuid4(),
        tenant_id=lookup.tenant_id,
        name="built-in runtime default",
        scope_type="tenant_default" if lookup.tenant_id else "platform_default",
        enforcement_mode="enforce",
        fail_mode="fail_closed",
        max_tokens=1_000_000,
        max_cache_miss_tokens=250_000,
        max_subagents=32,
        max_delegations=32,
        max_background_tasks=32,
        max_continuation_wakes=64,
        max_provider_calls=128,
        default_child_token_reservation=50_000,
        default_llm_call_token_reservation=50_000,
        policy_json={"source": "built_in_fallback"},
    )


class RuntimeBudgetService:
    """Service boundary for runtime budget policy, reservation, and settlement."""

    def __init__(self, *, session_factory: async_sessionmaker[AsyncSession] | None = None):
        self._session_factory = session_factory or async_session

    @contextlib.asynccontextmanager
    async def _budget_session(self, operation: str) -> AsyncIterator[AsyncSession]:
        async with self._session_factory() as db:
            async with enter_rls_bypass(db, reason=f"runtime_budget_service.{operation}") as bypass_db:
                yield bypass_db

    async def resolve_policy(self, lookup: RuntimeBudgetPolicyLookup) -> RuntimeBudgetPolicy:
        async with self._budget_session("resolve_policy") as db:
            result = await db.execute(
                select(RuntimeBudgetPolicy).where(
                    RuntimeBudgetPolicy.enabled.is_(True),
                    (RuntimeBudgetPolicy.tenant_id == lookup.tenant_id) | (RuntimeBudgetPolicy.tenant_id.is_(None)),
                )
            )
            candidates = [policy for policy in result.scalars().all() if _policy_matches(policy, lookup)]
        if not candidates:
            return _builtin_policy(lookup)
        return sorted(candidates, key=_policy_rank, reverse=True)[0]

    async def create_run(self, payload: RuntimeBudgetRunCreate) -> RuntimeBudgetRun:
        async with self._budget_session("create_run") as db:
            run = RuntimeBudgetRun(
                tenant_id=payload.tenant_id,
                policy_id=payload.policy_id,
                root_run_kind=payload.root_run_kind,
                root_run_key=payload.root_run_key,
                root_runtime_task_id=payload.root_runtime_task_id,
                root_session_id=payload.root_session_id,
                root_agent_id=payload.root_agent_id,
                root_user_id=payload.root_user_id,
                source=payload.source,
                profile=payload.profile,
                enforcement_mode=payload.enforcement_mode,
                fail_mode=payload.fail_mode,
                max_tokens=payload.max_tokens,
                max_cache_miss_tokens=payload.max_cache_miss_tokens,
                max_subagents=payload.max_subagents,
                max_delegations=payload.max_delegations,
                max_background_tasks=payload.max_background_tasks,
                max_continuation_wakes=payload.max_continuation_wakes,
                max_provider_calls=payload.max_provider_calls,
                expires_at=payload.expires_at,
                policy_snapshot=payload.policy_snapshot,
            )
            db.add(run)
            await db.commit()
            await db.refresh(run)
            return run

    async def reserve(self, reservation: RuntimeBudgetReservation) -> RuntimeBudgetReservationResult:
        amounts = _positive_amounts(reservation)
        async with self._budget_session("reserve") as db:
            run = await self._lock_run(db, reservation.budget_run_id)
            existing = await self._existing_event(db, run.id, reservation.reservation_key, "reservation")
            if existing is not None:
                return RuntimeBudgetReservationResult(
                    allowed=bool(existing.allowed),
                    would_deny=bool(existing.would_deny),
                    idempotent=True,
                    budget_run_id=run.id,
                )
            if run.status == "summary_only" and _work_amplifying_amounts(amounts):
                event = self._event(
                    run,
                    event_type="denial",
                    reservation_key=reservation.reservation_key,
                    allowed=False,
                    would_deny=True,
                    reason="summary_only_disallows_work_amplification",
                    amounts=amounts,
                    runtime_task_id=reservation.runtime_task_id,
                    metadata=reservation.metadata,
                )
                db.add(event)
                await db.commit()
                raise RuntimeBudgetDenied(str(event.reason), budget_run_id=run.id)
            if run.status not in {"active", "summary_only"}:
                event = self._event(
                    run,
                    event_type="denial",
                    reservation_key=reservation.reservation_key,
                    allowed=False,
                    would_deny=True,
                    reason=run.terminal_reason or f"budget run is {run.status}",
                    amounts=amounts,
                    runtime_task_id=reservation.runtime_task_id,
                    metadata=reservation.metadata,
                )
                db.add(event)
                await db.commit()
                raise RuntimeBudgetDenied(str(event.reason), budget_run_id=run.id)

            denied_dimensions = self._denied_dimensions(run, amounts)
            would_deny = bool(denied_dimensions)
            allowed = run.enforcement_mode == "observe" or not would_deny
            if not allowed:
                if run.status == "active":
                    if run.fail_mode == "summary_only":
                        run.status = "summary_only"
                        run.terminal_reason = "runtime_budget_summary_only:" + ",".join(denied_dimensions)
                    else:
                        run.status = "exhausted"
                        run.terminal_reason = "runtime_budget_exhausted:" + ",".join(denied_dimensions)
                    run.completed_at = datetime.now(UTC)
                    await self._cancel_pending_unclaimed_tasks(
                        db,
                        run,
                        terminal_reason="runtime_budget_summary_only"
                        if run.status == "summary_only"
                        else "runtime_budget_exhausted",
                        result_summary="Runtime budget moved to summary-only before this work was claimed."
                        if run.status == "summary_only"
                        else "Runtime budget exhausted before this work was claimed.",
                    )
                db.add(
                    self._event(
                        run,
                        event_type="denial",
                        reservation_key=reservation.reservation_key,
                        allowed=False,
                        would_deny=True,
                        reason=reservation.reason or "runtime budget exhausted",
                        amounts=amounts,
                        runtime_task_id=reservation.runtime_task_id,
                        metadata={"denied_dimensions": denied_dimensions, **(reservation.metadata or {})},
                    )
                )
                await db.commit()
                raise RuntimeBudgetDenied(
                    "runtime budget exhausted: " + ",".join(denied_dimensions),
                    budget_run_id=run.id,
                    dimensions=denied_dimensions,
                )

            self._increment_reserved(run, amounts)
            db.add(
                self._event(
                    run,
                    event_type="reservation",
                    reservation_key=reservation.reservation_key,
                    allowed=True,
                    would_deny=would_deny,
                    reason=reservation.reason,
                    amounts=amounts,
                    runtime_task_id=reservation.runtime_task_id,
                    metadata={"denied_dimensions": denied_dimensions, **(reservation.metadata or {})},
                )
            )
            await db.commit()
            return RuntimeBudgetReservationResult(
                allowed=True,
                would_deny=would_deny,
                idempotent=False,
                budget_run_id=run.id,
                denied_dimensions=tuple(denied_dimensions),
            )

    async def settle(self, settlement: RuntimeBudgetSettlement) -> None:
        actual = _positive_amounts(settlement)
        async with self._budget_session("settle") as db:
            run = await self._lock_run(db, settlement.budget_run_id)
            reservation_event = await self._existing_event(db, run.id, settlement.reservation_key, "reservation")
            estimated = dict(reservation_event.amounts_json or {}) if reservation_event else {}
            self._release_reserved(run, estimated)
            self._increment_used(run, actual)
            db.add(
                self._event(
                    run,
                    event_type="settlement",
                    reservation_key=settlement.reservation_key,
                    allowed=True,
                    would_deny=False,
                    reason=settlement.reason,
                    amounts=actual,
                    runtime_task_id=settlement.runtime_task_id,
                    metadata=settlement.metadata,
                )
            )
            await db.commit()

    async def reap_expired_runs(self, *, now: datetime | None = None, limit: int = 100) -> int:
        current = now or datetime.now(UTC)
        async with self._budget_session("reap_expired_runs") as db:
            result = await db.execute(
                select(RuntimeBudgetRun)
                .where(RuntimeBudgetRun.status == "active", RuntimeBudgetRun.expires_at.is_not(None), RuntimeBudgetRun.expires_at <= current)
                .order_by(RuntimeBudgetRun.expires_at)
                .limit(limit)
                .with_for_update(skip_locked=True)
            )
            runs = result.scalars().all()
            for run in runs:
                run.status = "expired"
                run.terminal_reason = "budget_run_expired"
                run.completed_at = current
                self._clear_reserved(run)
                db.add(
                    self._event(
                        run,
                        event_type="expired",
                        reservation_key=None,
                        allowed=None,
                        would_deny=False,
                        reason="budget_run_expired",
                        amounts={},
                    )
                )
                await db.execute(
                    update(RuntimeTask)
                    .where(
                        RuntimeTask.budget_run_id == run.id,
                        RuntimeTask.status.in_(("pending", "resumable")),
                        RuntimeTask.claimed_by.is_(None),
                    )
                    .values(
                        status="killed",
                        completed_at=current,
                        budget_admission_status="cancelled",
                        budget_terminal_reason="budget_run_expired",
                        result_summary="Runtime budget expired before this work was claimed.",
                    )
                )
            await db.commit()
            return len(runs)

    async def reconcile_orphaned_reservations(self, *, limit: int = 100) -> int:
        """Release reservations whose owning runtime task can no longer settle them."""

        reconciled = 0
        async with self._budget_session("reconcile_orphaned_reservations") as db:
            result = await db.execute(
                select(RuntimeBudgetEvent)
                .where(
                    RuntimeBudgetEvent.event_type == "reservation",
                    RuntimeBudgetEvent.reservation_key.is_not(None),
                    RuntimeBudgetEvent.runtime_task_id.is_not(None),
                )
                .order_by(RuntimeBudgetEvent.created_at)
                .limit(limit)
            )
            reservation_events = result.scalars().all()
            for event in reservation_events:
                settlement = await self._existing_event(db, event.budget_run_id, event.reservation_key, "settlement")
                if settlement is not None:
                    continue
                task = (
                    await db.execute(select(RuntimeTask).where(RuntimeTask.id == event.runtime_task_id))
                ).scalar_one_or_none()
                if task is None or str(task.status) not in _TERMINAL_RUNTIME_TASK_STATUSES:
                    continue
                run = await self._lock_run(db, event.budget_run_id)
                self._release_reserved(run, dict(event.amounts_json or {}))
                db.add(
                    self._event(
                        run,
                        event_type="settlement",
                        reservation_key=event.reservation_key,
                        allowed=True,
                        would_deny=False,
                        reason="orphaned_reservation_reconciled",
                        amounts={},
                        runtime_task_id=event.runtime_task_id,
                        metadata={
                            "source_event_id": str(event.id),
                            "runtime_task_status": str(task.status) if task is not None else "missing",
                        },
                    )
                )
                reconciled += 1
            await db.commit()
        return reconciled

    async def list_policies(self, *, tenant_id: uuid.UUID | None) -> list[RuntimeBudgetPolicy]:
        async with self._budget_session("list_policies") as db:
            result = await db.execute(
                select(RuntimeBudgetPolicy)
                .where((RuntimeBudgetPolicy.tenant_id == tenant_id) | (RuntimeBudgetPolicy.tenant_id.is_(None)))
                .order_by(RuntimeBudgetPolicy.tenant_id.nullsfirst(), RuntimeBudgetPolicy.priority.desc())
            )
            return list(result.scalars().all())

    async def list_runs(
        self,
        *,
        tenant_id: uuid.UUID | None,
        status: str | None = None,
        agent_id: uuid.UUID | None = None,
        limit: int = 50,
    ) -> list[RuntimeBudgetRun]:
        stmt = select(RuntimeBudgetRun).where(RuntimeBudgetRun.tenant_id == tenant_id)
        if status:
            stmt = stmt.where(RuntimeBudgetRun.status == status)
        if agent_id:
            stmt = stmt.where(RuntimeBudgetRun.root_agent_id == agent_id)
        async with self._budget_session("list_runs") as db:
            result = await db.execute(stmt.order_by(RuntimeBudgetRun.created_at.desc()).limit(limit))
            return list(result.scalars().all())

    async def get_run(self, *, tenant_id: uuid.UUID | None, budget_run_id: uuid.UUID) -> RuntimeBudgetRun | None:
        async with self._budget_session("get_run") as db:
            result = await db.execute(
                select(RuntimeBudgetRun).where(
                    RuntimeBudgetRun.id == budget_run_id,
                    RuntimeBudgetRun.tenant_id == tenant_id,
                )
            )
            return result.scalar_one_or_none()

    async def list_events(
        self,
        *,
        tenant_id: uuid.UUID | None,
        budget_run_id: uuid.UUID,
        limit: int = 100,
    ) -> list[RuntimeBudgetEvent]:
        async with self._budget_session("list_events") as db:
            result = await db.execute(
                select(RuntimeBudgetEvent)
                .where(RuntimeBudgetEvent.budget_run_id == budget_run_id, RuntimeBudgetEvent.tenant_id == tenant_id)
                .order_by(RuntimeBudgetEvent.created_at.desc())
                .limit(limit)
            )
            return list(result.scalars().all())

    async def create_policy(
        self,
        *,
        tenant_id: uuid.UUID | None,
        name: str,
        scope_type: str,
        source: str | None = None,
        profile: str | None = None,
        agent_id: uuid.UUID | None = None,
        trigger_id: uuid.UUID | None = None,
        enabled: bool = True,
        priority: int = 0,
        enforcement_mode: str = "enforce",
        fail_mode: str = "fail_closed",
        max_tokens: int | None = None,
        max_cache_miss_tokens: int | None = None,
        max_subagents: int | None = None,
        max_delegations: int | None = None,
        max_background_tasks: int | None = None,
        max_continuation_wakes: int | None = None,
        max_provider_calls: int | None = None,
        default_child_token_reservation: int = 50_000,
        default_llm_call_token_reservation: int = 50_000,
        policy_json: dict | None = None,
    ) -> RuntimeBudgetPolicy:
        async with self._budget_session("create_policy") as db:
            policy = RuntimeBudgetPolicy(
                tenant_id=tenant_id,
                name=name,
                enabled=enabled,
                priority=priority,
                scope_type=scope_type,
                source=source,
                profile=profile,
                agent_id=agent_id,
                trigger_id=trigger_id,
                enforcement_mode=enforcement_mode,
                fail_mode=fail_mode,
                max_tokens=max_tokens,
                max_cache_miss_tokens=max_cache_miss_tokens,
                max_subagents=max_subagents,
                max_delegations=max_delegations,
                max_background_tasks=max_background_tasks,
                max_continuation_wakes=max_continuation_wakes,
                max_provider_calls=max_provider_calls,
                default_child_token_reservation=max(0, int(default_child_token_reservation)),
                default_llm_call_token_reservation=max(0, int(default_llm_call_token_reservation)),
                policy_json=policy_json,
            )
            db.add(policy)
            await db.commit()
            await db.refresh(policy)
            return policy

    async def update_policy(
        self,
        *,
        tenant_id: uuid.UUID | None,
        policy_id: uuid.UUID,
        updates: dict,
    ) -> RuntimeBudgetPolicy | None:
        allowed_fields = {
            "name",
            "enabled",
            "priority",
            "scope_type",
            "source",
            "profile",
            "agent_id",
            "trigger_id",
            "enforcement_mode",
            "fail_mode",
            "max_tokens",
            "max_cache_miss_tokens",
            "max_subagents",
            "max_delegations",
            "max_background_tasks",
            "max_continuation_wakes",
            "max_provider_calls",
            "default_child_token_reservation",
            "default_llm_call_token_reservation",
            "policy_json",
        }
        clean_updates = {key: value for key, value in updates.items() if key in allowed_fields}
        if not clean_updates:
            return await self.get_policy(tenant_id=tenant_id, policy_id=policy_id)

        async with self._budget_session("update_policy") as db:
            result = await db.execute(
                select(RuntimeBudgetPolicy)
                .where(RuntimeBudgetPolicy.id == policy_id, RuntimeBudgetPolicy.tenant_id == tenant_id)
                .with_for_update()
            )
            policy = result.scalar_one_or_none()
            if policy is None:
                return None
            for key, value in clean_updates.items():
                setattr(policy, key, value)
            await db.commit()
            await db.refresh(policy)
            return policy

    async def get_policy(self, *, tenant_id: uuid.UUID | None, policy_id: uuid.UUID) -> RuntimeBudgetPolicy | None:
        async with self._budget_session("get_policy") as db:
            result = await db.execute(
                select(RuntimeBudgetPolicy).where(
                    RuntimeBudgetPolicy.id == policy_id,
                    RuntimeBudgetPolicy.tenant_id == tenant_id,
                )
            )
            return result.scalar_one_or_none()

    async def approve_overrun(
        self,
        *,
        tenant_id: uuid.UUID | None,
        budget_run_id: uuid.UUID,
        reason: str,
        actor_user_id: uuid.UUID | None = None,
        enforcement_mode: str = "observe",
        max_tokens: int | None = None,
        max_cache_miss_tokens: int | None = None,
        max_subagents: int | None = None,
        max_delegations: int | None = None,
        max_background_tasks: int | None = None,
        max_continuation_wakes: int | None = None,
        max_provider_calls: int | None = None,
    ) -> RuntimeBudgetRun | None:
        async with self._budget_session("approve_overrun") as db:
            result = await db.execute(
                select(RuntimeBudgetRun)
                .where(RuntimeBudgetRun.id == budget_run_id, RuntimeBudgetRun.tenant_id == tenant_id)
                .with_for_update()
            )
            run = result.scalar_one_or_none()
            if run is None:
                return None
            for field, value in {
                "max_tokens": max_tokens,
                "max_cache_miss_tokens": max_cache_miss_tokens,
                "max_subagents": max_subagents,
                "max_delegations": max_delegations,
                "max_background_tasks": max_background_tasks,
                "max_continuation_wakes": max_continuation_wakes,
                "max_provider_calls": max_provider_calls,
            }.items():
                if value is not None:
                    setattr(run, field, value)
            run.status = "active"
            run.enforcement_mode = enforcement_mode
            run.terminal_reason = None
            run.completed_at = None
            db.add(
                self._event(
                    run,
                    event_type="overrun_approved",
                    reservation_key=None,
                    allowed=True,
                    would_deny=False,
                    reason=reason,
                    amounts={},
                    metadata={
                        "actor_user_id": str(actor_user_id) if actor_user_id else None,
                        "enforcement_mode": enforcement_mode,
                    },
                )
            )
            await db.commit()
            await db.refresh(run)
            return run

    async def hard_stop_run(
        self,
        *,
        tenant_id: uuid.UUID | None,
        budget_run_id: uuid.UUID,
        reason: str,
        actor: str = "runtime_budget_breaker",
    ) -> RuntimeBudgetRun | None:
        current = datetime.now(UTC)
        async with self._budget_session("hard_stop_run") as db:
            result = await db.execute(
                select(RuntimeBudgetRun)
                .where(RuntimeBudgetRun.id == budget_run_id, RuntimeBudgetRun.tenant_id == tenant_id)
                .with_for_update()
            )
            run = result.scalar_one_or_none()
            if run is None:
                return None
            if run.status != "active":
                return run
            run.status = "hard_stopped"
            run.terminal_reason = reason
            run.completed_at = current
            self._clear_reserved(run)
            await self._cancel_pending_unclaimed_tasks(
                db,
                run,
                terminal_reason="runtime_budget_hard_stopped",
                result_summary="Runtime budget circuit breaker stopped this work before it was claimed.",
            )
            db.add(
                self._event(
                    run,
                    event_type="hard_stopped",
                    reservation_key=None,
                    allowed=None,
                    would_deny=False,
                    reason=reason,
                    amounts={},
                    metadata={"actor": actor},
                )
            )
            await db.commit()
            await db.refresh(run)
            return run

    async def set_tenant_enforcement_mode(
        self,
        *,
        tenant_id: uuid.UUID,
        enforcement_mode: str,
        reason: str,
        actor_user_id: uuid.UUID | None = None,
    ) -> int:
        async with self._budget_session("set_tenant_enforcement_mode") as db:
            result = await db.execute(
                select(RuntimeBudgetPolicy)
                .where(RuntimeBudgetPolicy.tenant_id == tenant_id, RuntimeBudgetPolicy.enabled.is_(True))
                .with_for_update()
            )
            policies = list(result.scalars().all())
            if not policies:
                db.add(
                    RuntimeBudgetPolicy(
                        tenant_id=tenant_id,
                        name="Tenant runtime budget emergency default",
                        enabled=True,
                        priority=10_000,
                        scope_type="tenant_default",
                        enforcement_mode=enforcement_mode,
                        fail_mode="fail_closed",
                        max_tokens=1_000_000,
                        max_cache_miss_tokens=250_000,
                        max_subagents=32,
                        max_delegations=32,
                        max_background_tasks=32,
                        max_continuation_wakes=64,
                        max_provider_calls=128,
                        policy_json={
                            "created_by": "tenant_enforcement_mode_switch",
                            "reason": reason,
                            "actor_user_id": str(actor_user_id) if actor_user_id else None,
                        },
                    )
                )
                await db.commit()
                return 1
            for policy in policies:
                policy.enforcement_mode = enforcement_mode
                policy.policy_json = {
                    **(policy.policy_json or {}),
                    "last_enforcement_mode_change": {
                        "reason": reason,
                        "actor_user_id": str(actor_user_id) if actor_user_id else None,
                        "changed_at": datetime.now(UTC).isoformat(),
                    },
                }
            await db.commit()
            return len(policies)

    async def cancel_run(
        self,
        *,
        tenant_id: uuid.UUID | None,
        budget_run_id: uuid.UUID,
        reason: str,
        actor_user_id: uuid.UUID | None = None,
    ) -> RuntimeBudgetRun | None:
        current = datetime.now(UTC)
        async with self._budget_session("cancel_run") as db:
            result = await db.execute(
                select(RuntimeBudgetRun)
                .where(RuntimeBudgetRun.id == budget_run_id, RuntimeBudgetRun.tenant_id == tenant_id)
                .with_for_update()
            )
            run = result.scalar_one_or_none()
            if run is None:
                return None
            run.status = "cancelled"
            run.terminal_reason = reason
            run.completed_at = current
            self._clear_reserved(run)
            db.add(
                self._event(
                    run,
                    event_type="cancelled",
                    reservation_key=None,
                    allowed=None,
                    would_deny=False,
                    reason=reason,
                    amounts={},
                    metadata={"actor_user_id": str(actor_user_id) if actor_user_id else None},
                )
            )
            await db.execute(
                update(RuntimeTask)
                .where(
                    RuntimeTask.budget_run_id == run.id,
                    RuntimeTask.status.in_(("pending", "resumable")),
                    RuntimeTask.claimed_by.is_(None),
                )
                .values(
                    status="killed",
                    completed_at=current,
                    budget_admission_status="cancelled",
                    budget_terminal_reason="budget_run_cancelled",
                    result_summary="Runtime budget run was cancelled before this work was claimed.",
                )
            )
            await db.commit()
            await db.refresh(run)
            return run

    async def _lock_run(self, db: AsyncSession, budget_run_id: uuid.UUID) -> RuntimeBudgetRun:
        result = await db.execute(select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == budget_run_id).with_for_update())
        run = result.scalar_one_or_none()
        if run is None:
            raise RuntimeBudgetNotFound("runtime budget run not found", budget_run_id=budget_run_id)
        return run

    async def _cancel_pending_unclaimed_tasks(
        self,
        db: AsyncSession,
        run: RuntimeBudgetRun,
        *,
        terminal_reason: str,
        result_summary: str,
    ) -> None:
        await db.execute(
            update(RuntimeTask)
            .where(
                RuntimeTask.budget_run_id == run.id,
                RuntimeTask.status.in_(("pending", "resumable")),
                RuntimeTask.claimed_by.is_(None),
            )
            .values(
                status="killed",
                completed_at=datetime.now(UTC),
                budget_admission_status="cancelled",
                budget_terminal_reason=terminal_reason,
                result_summary=result_summary,
            )
        )

    async def _existing_event(
        self,
        db: AsyncSession,
        budget_run_id: uuid.UUID,
        reservation_key: str | None,
        event_type: str,
    ) -> RuntimeBudgetEvent | None:
        if not reservation_key:
            return None
        stmt: Select = select(RuntimeBudgetEvent).where(
            RuntimeBudgetEvent.budget_run_id == budget_run_id,
            RuntimeBudgetEvent.reservation_key == reservation_key,
            RuntimeBudgetEvent.event_type == event_type,
        )
        return (await db.execute(stmt)).scalar_one_or_none()

    def _denied_dimensions(self, run: RuntimeBudgetRun, amounts: dict[str, int]) -> list[str]:
        denied: list[str] = []
        for dimension, amount in amounts.items():
            if amount <= 0:
                continue
            max_value = getattr(run, f"max_{dimension}", None)
            if max_value is None:
                continue
            reserved = getattr(run, f"reserved_{dimension}", 0) or 0
            used = getattr(run, f"used_{dimension}", 0) or 0
            if reserved + used + amount > max_value:
                denied.append(dimension)
        return denied

    def _increment_reserved(self, run: RuntimeBudgetRun, amounts: dict[str, int]) -> None:
        for dimension, amount in amounts.items():
            if amount:
                setattr(run, f"reserved_{dimension}", (getattr(run, f"reserved_{dimension}", 0) or 0) + amount)

    def _increment_used(self, run: RuntimeBudgetRun, amounts: dict[str, int]) -> None:
        for dimension, amount in amounts.items():
            if amount:
                setattr(run, f"used_{dimension}", (getattr(run, f"used_{dimension}", 0) or 0) + amount)

    def _release_reserved(self, run: RuntimeBudgetRun, amounts: dict[str, int]) -> None:
        for dimension, amount in amounts.items():
            clean_amount = max(0, int(amount or 0))
            if clean_amount:
                current = getattr(run, f"reserved_{dimension}", 0) or 0
                setattr(run, f"reserved_{dimension}", max(0, current - clean_amount))

    def _clear_reserved(self, run: RuntimeBudgetRun) -> None:
        for dimension in _DIMENSIONS:
            setattr(run, f"reserved_{dimension}", 0)

    def _event(
        self,
        run: RuntimeBudgetRun,
        *,
        event_type: str,
        reservation_key: str | None,
        allowed: bool | None,
        would_deny: bool,
        reason: str | None,
        amounts: dict[str, int],
        runtime_task_id: uuid.UUID | None = None,
        metadata: dict | None = None,
    ) -> RuntimeBudgetEvent:
        return RuntimeBudgetEvent(
            tenant_id=run.tenant_id,
            budget_run_id=run.id,
            event_type=event_type,
            reservation_key=reservation_key,
            allowed=allowed,
            would_deny=would_deny,
            reason=reason,
            amounts_json=amounts,
            metadata_json=metadata,
            runtime_task_id=runtime_task_id,
        )
