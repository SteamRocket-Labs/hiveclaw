"""Runtime budget admission service.

This service owns the run-level budget envelope for autonomous work. It is
deliberately separate from account token quotas: quotas cap who may spend at an
account level, while runtime budgets cap how much one continuation chain may
amplify.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Mapping
import contextlib
from dataclasses import dataclass
from datetime import UTC, datetime
import logging
from typing import Any
import uuid

from sqlalchemy import Select, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.database import async_session, enter_rls_bypass
from app.models.runtime_budget import RuntimeBudgetEvent, RuntimeBudgetPolicy, RuntimeBudgetRun
from app.models.runtime_task import RuntimeTask

logger = logging.getLogger(__name__)

_DIMENSIONS = (
    "tokens",
    "cache_miss_tokens",
    "subagents",
    "team_sessions",
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

# §10 breaker defaults are calibrated from the Ivy incident thresholds that were
# previously hardcoded in subagent_wake_consumer (needs_reconciliation=3,
# failures=5, child_failure_ratio=0.5). §9.1 does not tabulate these three, so
# they carry the incident-calibrated values across every profile; max_parent_invocations
# follows the §9.1 column (interactive/scheduled=16, workflow=64, agent_team=24).
_BUILTIN_BREAKER_MAX_FAILURES = 5
_BUILTIN_BREAKER_MAX_NEEDS_RECONCILIATION = 3
_BUILTIN_BREAKER_MAX_CHILD_FAILURE_RATIO = 0.5
_MIN_CHILDREN_FOR_FAILURE_RATIO = 8

_BUILTIN_PROFILE_DEFAULTS: dict[str, dict[str, int | float | str]] = {
    "interactive": {
        "max_tokens": 50_000_000,
        "max_cache_miss_tokens": 10_000_000,
        "max_subagents": 24,
        "max_team_sessions": 4,
        "max_delegations": 16,
        "max_background_tasks": 24,
        "max_continuation_wakes": 64,
        "max_provider_calls": 300,
        "max_parent_invocations": 16,
        "max_failures": _BUILTIN_BREAKER_MAX_FAILURES,
        "max_needs_reconciliation": _BUILTIN_BREAKER_MAX_NEEDS_RECONCILIATION,
        "max_child_failure_ratio": _BUILTIN_BREAKER_MAX_CHILD_FAILURE_RATIO,
        "default_child_token_reservation": 200_000,
        "default_llm_call_token_reservation": 200_000,
        "fail_mode": "require_confirmation",
    },
    "scheduled": {
        "max_tokens": 40_000_000,
        "max_cache_miss_tokens": 8_000_000,
        "max_subagents": 32,
        "max_team_sessions": 0,
        "max_delegations": 12,
        "max_background_tasks": 32,
        "max_continuation_wakes": 64,
        "max_provider_calls": 240,
        "max_parent_invocations": 16,
        "max_failures": _BUILTIN_BREAKER_MAX_FAILURES,
        "max_needs_reconciliation": _BUILTIN_BREAKER_MAX_NEEDS_RECONCILIATION,
        "max_child_failure_ratio": _BUILTIN_BREAKER_MAX_CHILD_FAILURE_RATIO,
        "default_child_token_reservation": 250_000,
        "default_llm_call_token_reservation": 250_000,
        "fail_mode": "summary_only",
    },
    "workflow": {
        "max_tokens": 250_000_000,
        "max_cache_miss_tokens": 80_000_000,
        "max_subagents": 256,
        "max_team_sessions": 0,
        "max_delegations": 64,
        "max_background_tasks": 256,
        "max_continuation_wakes": 512,
        "max_provider_calls": 2_000,
        "max_parent_invocations": 64,
        "max_failures": _BUILTIN_BREAKER_MAX_FAILURES,
        "max_needs_reconciliation": _BUILTIN_BREAKER_MAX_NEEDS_RECONCILIATION,
        "max_child_failure_ratio": _BUILTIN_BREAKER_MAX_CHILD_FAILURE_RATIO,
        "default_child_token_reservation": 300_000,
        "default_llm_call_token_reservation": 300_000,
        "fail_mode": "hard_stop",
    },
    "agent_team": {
        "max_tokens": 80_000_000,
        "max_cache_miss_tokens": 16_000_000,
        "max_subagents": 16,
        "max_team_sessions": 4,
        "max_delegations": 16,
        "max_background_tasks": 16,
        "max_continuation_wakes": 96,
        "max_provider_calls": 500,
        "max_parent_invocations": 24,
        "max_failures": _BUILTIN_BREAKER_MAX_FAILURES,
        "max_needs_reconciliation": _BUILTIN_BREAKER_MAX_NEEDS_RECONCILIATION,
        "max_child_failure_ratio": _BUILTIN_BREAKER_MAX_CHILD_FAILURE_RATIO,
        "default_child_token_reservation": 250_000,
        "default_llm_call_token_reservation": 250_000,
        "fail_mode": "require_confirmation",
    },
}

_BUILTIN_PROFILE_ALIASES = {
    "web": "interactive",
    "web_chat": "interactive",
    "web_chat_turn": "interactive",
    "chat": "interactive",
}


def _canonical_runtime_profile(value: str | None) -> str | None:
    if value is None:
        return None
    return _BUILTIN_PROFILE_ALIASES.get(value, value)


class RuntimeBudgetDenied(Exception):
    """Raised when a budget reservation is denied."""

    def __init__(self, message: str, *, budget_run_id: uuid.UUID | None = None, dimensions: list[str] | None = None):
        self.budget_run_id = budget_run_id
        self.dimensions = dimensions or []
        super().__init__(message)


class RuntimeBudgetNotFound(RuntimeBudgetDenied):
    """Raised when the target budget run does not exist."""


class RuntimeBudgetApprovalRequired(RuntimeBudgetDenied):
    """Raised when exact queued work is frozen pending a human budget decision."""


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
    max_team_sessions: int | None = None
    max_delegations: int | None = None
    max_background_tasks: int | None = None
    max_continuation_wakes: int | None = None
    max_provider_calls: int | None = None
    max_failures: int | None = None
    max_needs_reconciliation: int | None = None
    max_child_failure_ratio: float | None = None
    max_parent_invocations: int | None = None
    expires_at: datetime | None = None
    policy_snapshot: dict | None = None


@dataclass(frozen=True, slots=True)
class RuntimeBudgetReservation:
    budget_run_id: uuid.UUID
    reservation_key: str
    tokens: int = 0
    cache_miss_tokens: int = 0
    subagents: int = 0
    team_sessions: int = 0
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
    actual_team_sessions: int = 0
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
        for dimension in ("subagents", "team_sessions", "delegations", "background_tasks", "continuation_wakes")
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


# §10 circuit breaker: child terminal statuses used to materialize the failure /
# reconciliation counters from ground truth at the parent-wake boundary.
_BREAKER_CHILD_TERMINAL_STATUSES = ("completed", "failed", "killed", "needs_reconciliation")
_BREAKER_FAILED_STATUSES = ("failed", "killed")


def evaluate_circuit_breaker(
    *,
    used: Mapping[str, int],
    reserved: Mapping[str, int],
    maxes: Mapping[str, float | int | None],
    failures: int,
    needs_reconciliation_count: int,
    parent_invocations: int,
    child_failure_ratio: float | None = None,
    total_children: int = 0,
    min_children_for_ratio: int = _MIN_CHILDREN_FOR_FAILURE_RATIO,
) -> list[str]:
    """Pure §10 circuit-breaker decision.

    Returns the list of tripped dimension reasons (empty when the run is still
    within budget). Token dimensions count ``used + reserved`` risk; count
    dimensions count ``used`` only. ``child_failure_ratio`` is evaluated only
    when the caller supplies it (parent-wake path) and enough children have
    reached a terminal state to make the ratio statistically meaningful.
    """

    tripped: list[str] = []
    for dimension in _DIMENSIONS:
        max_value = maxes.get(dimension)
        if max_value is None:
            continue
        consumed = int(used.get(dimension, 0) or 0)
        if dimension in ("tokens", "cache_miss_tokens"):
            consumed += int(reserved.get(dimension, 0) or 0)
        if consumed >= max_value:
            tripped.append(f"{dimension}:{consumed}>={int(max_value)}")

    max_parent = maxes.get("parent_invocations")
    if max_parent is not None and parent_invocations >= max_parent:
        tripped.append(f"parent_invocations:{parent_invocations}>={int(max_parent)}")

    max_failures = maxes.get("failures")
    if max_failures is not None and failures >= max_failures:
        tripped.append(f"failures:{failures}>={int(max_failures)}")

    max_needs = maxes.get("needs_reconciliation")
    if max_needs is not None and needs_reconciliation_count >= max_needs:
        tripped.append(f"needs_reconciliation:{needs_reconciliation_count}>={int(max_needs)}")

    max_ratio = maxes.get("child_failure_ratio")
    if (
        max_ratio is not None
        and child_failure_ratio is not None
        and total_children >= min_children_for_ratio
        and child_failure_ratio >= max_ratio
    ):
        tripped.append(f"child_failure_ratio:{child_failure_ratio:.3f}>={max_ratio}")

    return tripped


def _breaker_status_for_fail_mode(fail_mode: str | None) -> str:
    """Map a policy fail_mode to the run status a tripped breaker transitions to.

    ``summary_only`` leaves one final summarizing lane. ``require_confirmation``
    freezes claimable work without cancelling it. Every other mode
    (``hard_stop`` and the ``fail_closed`` default) hard-stops the run.
    """

    if fail_mode == "require_confirmation":
        return "waiting_budget_approval"
    if fail_mode == "summary_only":
        return "summary_only"
    return "hard_stopped"


def _positive_amounts(payload: RuntimeBudgetReservation | RuntimeBudgetSettlement) -> dict[str, int]:
    if isinstance(payload, RuntimeBudgetSettlement):
        values = {
            "tokens": payload.actual_tokens,
            "cache_miss_tokens": payload.actual_cache_miss_tokens,
            "subagents": payload.actual_subagents,
            "team_sessions": payload.actual_team_sessions,
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
        lookup_source = _canonical_runtime_profile(lookup.source)
        lookup_profile = _canonical_runtime_profile(lookup.profile)
        source_matches = policy.source is None or policy.source in {lookup.source, lookup_source}
        profile_matches = policy.profile is None or policy.profile in {lookup.profile, lookup_profile}
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
    requested_profile = lookup.profile or lookup.source or "scheduled"
    profile = _canonical_runtime_profile(requested_profile) or "scheduled"
    if profile not in _BUILTIN_PROFILE_DEFAULTS:
        profile = "scheduled"
    defaults = _BUILTIN_PROFILE_DEFAULTS[profile]
    return RuntimeBudgetPolicy(
        id=uuid.uuid4(),
        tenant_id=lookup.tenant_id,
        name=f"built-in {profile} runtime default",
        scope_type="tenant_default" if lookup.tenant_id else "platform_default",
        enforcement_mode="enforce",
        fail_mode=str(defaults["fail_mode"]),
        max_tokens=int(defaults["max_tokens"]),
        max_cache_miss_tokens=int(defaults["max_cache_miss_tokens"]),
        max_subagents=int(defaults["max_subagents"]),
        max_team_sessions=int(defaults["max_team_sessions"]),
        max_delegations=int(defaults["max_delegations"]),
        max_background_tasks=int(defaults["max_background_tasks"]),
        max_continuation_wakes=int(defaults["max_continuation_wakes"]),
        max_provider_calls=int(defaults["max_provider_calls"]),
        max_failures=int(defaults["max_failures"]),
        max_needs_reconciliation=int(defaults["max_needs_reconciliation"]),
        max_child_failure_ratio=float(defaults["max_child_failure_ratio"]),
        max_parent_invocations=int(defaults["max_parent_invocations"]),
        default_child_token_reservation=int(defaults["default_child_token_reservation"]),
        default_llm_call_token_reservation=int(defaults["default_llm_call_token_reservation"]),
        policy_json={"source": "built_in_fallback", "profile": profile, "requested_profile": requested_profile},
    )


def _normalize_run_policy_reference(
    policy_id: uuid.UUID | None,
    policy_snapshot: dict | None,
) -> tuple[uuid.UUID | None, dict | None]:
    if policy_snapshot is None:
        return policy_id, None

    normalized_snapshot = dict(policy_snapshot)
    policy_json = normalized_snapshot.get("policy_json")
    is_builtin_fallback = isinstance(policy_json, dict) and policy_json.get("source") == "built_in_fallback"
    if is_builtin_fallback:
        normalized_snapshot["policy_id"] = None
        return None, normalized_snapshot

    if policy_id is not None:
        normalized_snapshot.setdefault("policy_id", str(policy_id))
    return policy_id, normalized_snapshot


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
            policy_id, policy_snapshot = _normalize_run_policy_reference(payload.policy_id, payload.policy_snapshot)
            run = RuntimeBudgetRun(
                tenant_id=payload.tenant_id,
                policy_id=policy_id,
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
                max_team_sessions=payload.max_team_sessions,
                max_delegations=payload.max_delegations,
                max_background_tasks=payload.max_background_tasks,
                max_continuation_wakes=payload.max_continuation_wakes,
                max_provider_calls=payload.max_provider_calls,
                max_failures=payload.max_failures,
                max_needs_reconciliation=payload.max_needs_reconciliation,
                max_child_failure_ratio=payload.max_child_failure_ratio,
                max_parent_invocations=payload.max_parent_invocations,
                expires_at=payload.expires_at,
                policy_snapshot=policy_snapshot,
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
            pending_denial = await self._existing_event(db, run.id, reservation.reservation_key, "denial")
            if pending_denial is not None and run.status == "waiting_budget_approval":
                raise RuntimeBudgetApprovalRequired(
                    str(pending_denial.reason or "runtime budget approval required"),
                    budget_run_id=run.id,
                    dimensions=list((pending_denial.metadata_json or {}).get("denied_dimensions") or []),
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
                error_type = (
                    RuntimeBudgetApprovalRequired if run.status == "waiting_budget_approval" else RuntimeBudgetDenied
                )
                raise error_type(str(event.reason), budget_run_id=run.id)

            # §2 finalization lane: the single summarizing turn is admitted on a
            # summary_only run even though its token dimensions are exhausted —
            # otherwise "leave a lane for one final summarizing invocation" is a
            # dead comment. Work amplification was already rejected above, and
            # exactly-once issuance is enforced by mark_summary_turn_state.
            # Usage still settles honestly for accounting.
            if run.status == "summary_only" and bool((reservation.metadata or {}).get("budget_summary_turn")):
                self._increment_reserved(run, amounts)
                db.add(
                    self._event(
                        run,
                        event_type="reservation",
                        reservation_key=reservation.reservation_key,
                        allowed=True,
                        would_deny=True,
                        reason="budget_summary_turn_lane",
                        amounts=amounts,
                        runtime_task_id=reservation.runtime_task_id,
                        metadata=reservation.metadata,
                    )
                )
                await db.commit()
                return RuntimeBudgetReservationResult(
                    allowed=True,
                    would_deny=True,
                    idempotent=False,
                    budget_run_id=run.id,
                )

            denied_dimensions = self._denied_dimensions(run, amounts)
            would_deny = bool(denied_dimensions)
            allowed = run.enforcement_mode == "observe" or not would_deny
            if not allowed:
                if run.status == "active":
                    if run.fail_mode == "summary_only":
                        run.status = "summary_only"
                        run.terminal_reason = "runtime_budget_summary_only:" + ",".join(denied_dimensions)
                        run.completed_at = datetime.now(UTC)
                        await self._cancel_pending_unclaimed_tasks(
                            db,
                            run,
                            terminal_reason="runtime_budget_summary_only",
                            result_summary="Runtime budget moved to summary-only before this work was claimed.",
                        )
                    elif run.fail_mode == "require_confirmation":
                        run.status = "waiting_budget_approval"
                        run.terminal_reason = "runtime_budget_approval_required:" + ",".join(denied_dimensions)
                        run.completed_at = None
                        if reservation.runtime_task_id is not None:
                            await db.execute(
                                update(RuntimeTask)
                                .where(
                                    RuntimeTask.id == reservation.runtime_task_id,
                                    RuntimeTask.budget_run_id == run.id,
                                    RuntimeTask.status.in_(("pending", "resumable")),
                                    RuntimeTask.claimed_by.is_(None),
                                )
                                .values(
                                    budget_admission_status="waiting_budget_approval",
                                    budget_terminal_reason=run.terminal_reason,
                                )
                            )
                    else:
                        run.status = "exhausted"
                        run.terminal_reason = "runtime_budget_exhausted:" + ",".join(denied_dimensions)
                        run.completed_at = datetime.now(UTC)
                        await self._cancel_pending_unclaimed_tasks(
                            db,
                            run,
                            terminal_reason="runtime_budget_exhausted",
                            result_summary="Runtime budget exhausted before this work was claimed.",
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
                if run.status == "waiting_budget_approval":
                    raise RuntimeBudgetApprovalRequired(
                        "runtime budget approval required: " + ",".join(denied_dimensions),
                        budget_run_id=run.id,
                        dimensions=denied_dimensions,
                    )
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
            existing_settlement = await self._existing_event(
                db,
                run.id,
                settlement.reservation_key,
                "settlement",
            )
            if existing_settlement is not None:
                return
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
            # §10 breaker: after real usage is applied, stop/summary-only the run
            # if any accumulation or counter dimension is now at/over its cap.
            tripped = self._breaker_tripped(run)
            if tripped:
                await self._apply_breaker(db, run, tripped, actor="settlement", source=settlement.reason)
            await db.commit()

    async def reap_expired_runs(self, *, now: datetime | None = None, limit: int = 100) -> int:
        current = now or datetime.now(UTC)
        async with self._budget_session("reap_expired_runs") as db:
            result = await db.execute(
                select(RuntimeBudgetRun)
                .where(
                    RuntimeBudgetRun.status.in_(("active", "waiting_budget_approval")),
                    RuntimeBudgetRun.expires_at.is_not(None),
                    RuntimeBudgetRun.expires_at <= current,
                )
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
        max_team_sessions: int | None = None,
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
                max_team_sessions=max_team_sessions,
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
            "max_team_sessions",
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
        enforcement_mode: str = "enforce",
        max_tokens: int | None = None,
        max_cache_miss_tokens: int | None = None,
        max_subagents: int | None = None,
        max_team_sessions: int | None = None,
        max_delegations: int | None = None,
        max_background_tasks: int | None = None,
        max_continuation_wakes: int | None = None,
        max_provider_calls: int | None = None,
    ) -> RuntimeBudgetRun | None:
        resumed_task_ids: list[uuid.UUID] = []
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
                "max_team_sessions": max_team_sessions,
                "max_delegations": max_delegations,
                "max_background_tasks": max_background_tasks,
                "max_continuation_wakes": max_continuation_wakes,
                "max_provider_calls": max_provider_calls,
            }.items():
                if value is not None:
                    setattr(run, field, value)
            waiting_tasks = list(
                (
                    await db.execute(
                        select(RuntimeTask)
                        .where(
                            RuntimeTask.budget_run_id == run.id,
                            RuntimeTask.status.in_(("pending", "resumable")),
                            RuntimeTask.claimed_by.is_(None),
                            RuntimeTask.budget_admission_status == "waiting_budget_approval",
                        )
                        .with_for_update()
                    )
                )
                .scalars()
                .all()
            )
            waiting_tasks_by_key = {
                str(task.budget_reservation_key or "").strip(): task
                for task in waiting_tasks
                if str(task.budget_reservation_key or "").strip()
            }
            waiting_tasks_by_id = {task.id: task for task in waiting_tasks}
            pending_denials = list(
                (
                    await db.execute(
                        select(RuntimeBudgetEvent)
                        .where(
                            RuntimeBudgetEvent.budget_run_id == run.id,
                            RuntimeBudgetEvent.event_type == "denial",
                        )
                        .order_by(RuntimeBudgetEvent.created_at)
                    )
                )
                .scalars()
                .all()
            )
            for denial in pending_denials:
                reservation_key = str(denial.reservation_key or "").strip()
                if (
                    not reservation_key
                    or await self._existing_event(db, run.id, reservation_key, "reservation") is not None
                ):
                    continue
                amounts = {key: max(0, int(value or 0)) for key, value in (denial.amounts_json or {}).items()}
                for dimension, amount in amounts.items():
                    if dimension not in _DIMENSIONS or amount <= 0:
                        continue
                    current_max = getattr(run, f"max_{dimension}", None)
                    required_max = (
                        int(getattr(run, f"used_{dimension}", 0) or 0)
                        + int(getattr(run, f"reserved_{dimension}", 0) or 0)
                        + amount
                    )
                    if current_max is not None and int(current_max) < required_max:
                        setattr(run, f"max_{dimension}", required_max)
                task = waiting_tasks_by_key.get(reservation_key)
                if task is None and denial.runtime_task_id is not None:
                    task = waiting_tasks_by_id.get(denial.runtime_task_id)
                if task is None:
                    # Foreground/Team admission has no queued RuntimeTask to
                    # resume. Raising the exact denied limits makes the same
                    # idempotency key safe to retry after approval without
                    # reserving resources for work that does not yet exist.
                    continue
                self._increment_reserved(run, amounts)
                db.add(
                    self._event(
                        run,
                        event_type="reservation",
                        reservation_key=reservation_key,
                        allowed=True,
                        would_deny=True,
                        reason="approved_budget_overrun_reservation",
                        amounts=amounts,
                        runtime_task_id=task.id,
                        metadata={
                            **(denial.metadata_json or {}),
                            "approved_from_denial_event_id": str(denial.id),
                            "actor_user_id": str(actor_user_id) if actor_user_id else None,
                        },
                    )
                )
                task.budget_admission_status = "approved"
                task.budget_terminal_reason = None
                task.completed_at = None
                resumed_task_ids.append(task.id)
            run.status = "resuming" if resumed_task_ids else "active"
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
                        "transition": ["waiting_budget_approval", "approved", "resuming", "active"],
                        "resumed_runtime_task_ids": [str(task_id) for task_id in resumed_task_ids],
                    },
                )
            )
            await self._append_session_status_event(
                db,
                run,
                event_type="runtime_budget_approved",
                content="运行额度已获批准，等待中的工作将自动继续。",
                actor_user_id=actor_user_id,
                metadata={
                    "reason": reason,
                    "resumed_runtime_task_ids": [str(task_id) for task_id in resumed_task_ids],
                },
            )
            run.status = "active"
            await db.commit()
            await db.refresh(run)
        if resumed_task_ids:
            from app.services.runtime_task_worker import notify_runtime_task_worker

            for task_id in resumed_task_ids:
                try:
                    await notify_runtime_task_worker(reason="runtime_budget_approved", runtime_task_id=task_id)
                except Exception:
                    # The persisted task remains the authority and the worker
                    # poller can still claim it. One failed fast wake must not
                    # prevent the remaining approved tasks from being notified.
                    logger.warning(
                        "Runtime budget approval wake failed for task %s",
                        task_id,
                        exc_info=True,
                    )
        return run

    async def reject_overrun(
        self,
        *,
        tenant_id: uuid.UUID | None,
        budget_run_id: uuid.UUID,
        reason: str,
        actor_user_id: uuid.UUID | None = None,
    ) -> RuntimeBudgetRun | None:
        current = datetime.now(UTC)
        async with self._budget_session("reject_overrun") as db:
            run = (
                await db.execute(
                    select(RuntimeBudgetRun)
                    .where(RuntimeBudgetRun.id == budget_run_id, RuntimeBudgetRun.tenant_id == tenant_id)
                    .with_for_update()
                )
            ).scalar_one_or_none()
            if run is None:
                return None
            run.status = "stopped"
            run.terminal_reason = "runtime_budget_approval_rejected"
            run.completed_at = current
            self._clear_reserved(run)
            await db.execute(
                update(RuntimeTask)
                .where(
                    RuntimeTask.budget_run_id == run.id,
                    RuntimeTask.status.in_(("pending", "resumable")),
                    RuntimeTask.claimed_by.is_(None),
                    RuntimeTask.budget_admission_status == "waiting_budget_approval",
                )
                .values(
                    status="killed",
                    completed_at=current,
                    budget_admission_status="rejected",
                    budget_terminal_reason="runtime_budget_approval_rejected",
                    result_summary="Runtime budget approval was rejected before this work was claimed.",
                )
            )
            db.add(
                self._event(
                    run,
                    event_type="overrun_rejected",
                    reservation_key=None,
                    allowed=False,
                    would_deny=True,
                    reason=reason,
                    amounts={},
                    metadata={"actor_user_id": str(actor_user_id) if actor_user_id else None},
                )
            )
            await self._append_session_status_event(
                db,
                run,
                event_type="runtime_budget_rejected",
                content="运行额度申请未获批准，等待中的工作已停止；已完成结果仍可查看。",
                actor_user_id=actor_user_id,
                metadata={"reason": reason},
            )
            await db.commit()
            await db.refresh(run)
            return run

    async def _append_session_status_event(
        self,
        db: AsyncSession,
        run: RuntimeBudgetRun,
        *,
        event_type: str,
        content: str,
        actor_user_id: uuid.UUID | None,
        metadata: dict[str, Any],
    ) -> None:
        if run.root_agent_id is None or not str(run.root_session_id or "").strip():
            return
        try:
            from app.services.chat_transcript import append_session_event

            await append_session_event(
                db=db,
                agent_id=run.root_agent_id,
                tenant_id=run.tenant_id,
                session_id=str(run.root_session_id),
                actor_type="system",
                event_type=event_type,
                content=content,
                role="system",
                user_id=run.root_user_id or actor_user_id,
                run_id=run.root_runtime_task_id,
                runtime_task_id=run.root_runtime_task_id,
                root_session_id=str(run.root_session_id),
                metadata={"budget_run_id": str(run.id), **metadata},
                visibility_scope="direct_user",
                listed_surface="chat",
                source="runtime_budget",
                bridge_to_t0=False,
            )
        except Exception:
            # A missing legacy session must not corrupt the budget decision;
            # RuntimeBudgetEvent remains the mechanical truth and the outbox
            # projection can reconcile the user notification later.
            return

    async def mark_summary_turn_state(
        self,
        *,
        tenant_id: uuid.UUID | None,
        budget_run_id: uuid.UUID,
        expected_states: tuple[str | None, ...],
        new_state: str,
        extra: dict[str, Any] | None = None,
    ) -> bool:
        """Row-locked CAS on ``metadata_json.summary_turn_state`` (§2 exactly-once).

        Returns True when the transition was applied; False when the current
        state is not in ``expected_states`` (a concurrent issuer already won).
        """
        async with self._budget_session("mark_summary_turn_state") as db:
            result = await db.execute(
                select(RuntimeBudgetRun)
                .where(RuntimeBudgetRun.id == budget_run_id, RuntimeBudgetRun.tenant_id == tenant_id)
                .with_for_update()
            )
            run = result.scalar_one_or_none()
            if run is None:
                return False
            metadata = dict(run.metadata_json or {})
            current = metadata.get("summary_turn_state")
            if current not in expected_states:
                return False
            metadata["summary_turn_state"] = new_state
            metadata["summary_turn_state_at"] = datetime.now(UTC).isoformat()
            for key, value in (extra or {}).items():
                metadata[key] = value
            run.metadata_json = metadata
            db.add(
                self._event(
                    run,
                    event_type="summary_turn",
                    reservation_key=None,
                    allowed=None,
                    would_deny=False,
                    reason=f"summary_turn_state:{new_state}",
                    amounts={},
                    metadata={"from": current, "to": new_state, **(extra or {})},
                )
            )
            await db.commit()
            return True

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
            # summary_only is a legal precursor: after the finalization turn the
            # lane is sealed by transitioning summary_only -> hard_stopped (§2).
            if run.status not in {"active", "summary_only"}:
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

    async def evaluate_wake_breaker(
        self,
        *,
        tenant_id: uuid.UUID | None,
        budget_run_id: uuid.UUID,
    ) -> str | None:
        """Parent-wake §10 breaker (enforcement point ②).

        Materializes the child failure / reconciliation counters from ground
        truth, counts this parent invocation, then trips the policy-driven
        breaker if any dimension is exhausted. Returns the circuit-break reason
        when the wake must not proceed, else ``None`` (parent may continue).

        Thresholds come from the run's policy snapshot (``max_failures`` /
        ``max_needs_reconciliation`` / ``max_child_failure_ratio`` /
        ``max_parent_invocations``), not hardcoded constants.
        """

        async with self._budget_session("evaluate_wake_breaker") as db:
            run = await self._lock_run(db, budget_run_id)
            if tenant_id is not None and run.tenant_id is not None and run.tenant_id != tenant_id:
                return None
            if run.status != "active":
                return run.terminal_reason or f"budget run is {run.status}"
            rows = (
                await db.execute(
                    select(RuntimeTask.status).where(
                        RuntimeTask.budget_run_id == budget_run_id,
                        RuntimeTask.task_type == "subagent",
                        RuntimeTask.status.in_(_BREAKER_CHILD_TERMINAL_STATUSES),
                    )
                )
            ).all()
            statuses = [str(row[0]) for row in rows]
            total_children = len(statuses)
            failed = sum(1 for status in statuses if status in _BREAKER_FAILED_STATUSES)
            needs_reconciliation = sum(1 for status in statuses if status == "needs_reconciliation")
            # parent_invocations is the one persisted breaker counter (real write
            # point); failures / needs_reconciliation are derived from the query
            # above and passed straight into the breaker, never persisted.
            run.parent_invocations = (run.parent_invocations or 0) + 1
            ratio = (failed / total_children) if total_children else None
            tripped = self._breaker_tripped(
                run,
                failures=failed,
                needs_reconciliation_count=needs_reconciliation,
                child_failure_ratio=ratio,
                total_children=total_children,
            )
            if not tripped:
                await db.commit()
                return None
            await self._apply_breaker(db, run, tripped, actor="subagent_wake_consumer", source="parent_wake")
            await db.commit()
            return "runtime_budget_circuit_break:" + ",".join(tripped)

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
        result = await db.execute(
            select(RuntimeBudgetRun).where(RuntimeBudgetRun.id == budget_run_id).with_for_update()
        )
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

    def _breaker_tripped(
        self,
        run: RuntimeBudgetRun,
        *,
        failures: int = 0,
        needs_reconciliation_count: int = 0,
        child_failure_ratio: float | None = None,
        total_children: int = 0,
    ) -> list[str]:
        """Return tripped §10 breaker dimensions for a run.

        Accumulation dimensions and ``parent_invocations`` come off the run row.
        ``failures`` / ``needs_reconciliation`` / ``child_failure_ratio`` are
        supplied by the caller from the ground-truth child-status query at the
        wake boundary (they are not persisted columns), and default to 0/None so
        the settlement path evaluates only the persisted dimensions.
        """

        used = {dimension: getattr(run, f"used_{dimension}", 0) or 0 for dimension in _DIMENSIONS}
        reserved = {
            dimension: getattr(run, f"reserved_{dimension}", 0) or 0 for dimension in ("tokens", "cache_miss_tokens")
        }
        maxes: dict[str, float | int | None] = {
            dimension: getattr(run, f"max_{dimension}", None) for dimension in _DIMENSIONS
        }
        maxes["parent_invocations"] = run.max_parent_invocations
        maxes["failures"] = run.max_failures
        maxes["needs_reconciliation"] = run.max_needs_reconciliation
        maxes["child_failure_ratio"] = run.max_child_failure_ratio
        return evaluate_circuit_breaker(
            used=used,
            reserved=reserved,
            maxes=maxes,
            failures=failures,
            needs_reconciliation_count=needs_reconciliation_count,
            parent_invocations=run.parent_invocations or 0,
            child_failure_ratio=child_failure_ratio,
            total_children=total_children,
        )

    async def _apply_breaker(
        self,
        db: AsyncSession,
        run: RuntimeBudgetRun,
        tripped: list[str],
        *,
        actor: str,
        source: str | None = None,
    ) -> str | None:
        """Transition a tripped run per fail_mode and record the decision."""

        if run.status != "active":
            return None
        target = _breaker_status_for_fail_mode(run.fail_mode)
        reason = "runtime_budget_circuit_break:" + ",".join(tripped)
        run.status = target
        run.terminal_reason = reason
        run.completed_at = None if target == "waiting_budget_approval" else datetime.now(UTC)
        if target == "hard_stopped":
            self._clear_reserved(run)
        if target == "waiting_budget_approval":
            await db.execute(
                update(RuntimeTask)
                .where(
                    RuntimeTask.budget_run_id == run.id,
                    RuntimeTask.status.in_(("pending", "resumable")),
                    RuntimeTask.claimed_by.is_(None),
                )
                .values(budget_admission_status="waiting_budget_approval", budget_terminal_reason=reason)
            )
        else:
            await self._cancel_pending_unclaimed_tasks(
                db,
                run,
                terminal_reason=reason,
                result_summary=(
                    "Runtime budget circuit breaker moved this run to summary-only before this work was claimed."
                    if target == "summary_only"
                    else "Runtime budget circuit breaker stopped this work before it was claimed."
                ),
            )
        db.add(
            self._event(
                run,
                event_type="circuit_break",
                reservation_key=None,
                allowed=False,
                would_deny=True,
                reason=reason,
                amounts={},
                metadata={
                    "actor": actor,
                    "source": source,
                    "fail_mode": run.fail_mode,
                    "target_status": target,
                    "tripped_dimensions": tripped,
                },
            )
        )
        return target

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
