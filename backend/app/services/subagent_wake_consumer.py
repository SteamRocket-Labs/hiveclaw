"""Wake parent agents when background subagents complete.

This mirrors the workflow signal consumer's core invariant: a durable Signal
is consumed atomically before exactly one wake attempt proceeds. If the parent
already has an active RuntimeTask, the signal is left in place for a later tick
or the in-run consumer path.
"""

from __future__ import annotations

import logging
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select, text

from app.agents.subagent import SUBAGENT_COMPLETION_SIGNAL
from app.database import enter_rls_bypass, tenant_scoped_session
from app.models.coordination import CoordinationSignal
from app.models.runtime_task import RuntimeTask
from app.services.execution_admission import ExecutionAdmission, ExecutionAdmissionDecision
from app.services.runtime_budget_service import (
    RuntimeBudgetDenied,
    RuntimeBudgetReservation,
    RuntimeBudgetService,
)

logger = logging.getLogger(__name__)

_ACTIVE_PARENT_RUN_STATUSES = ("pending", "running", "suspended")


@dataclass(frozen=True, slots=True)
class SubagentWakeRequest:
    tenant_id: uuid.UUID
    parent_agent_id: uuid.UUID
    signal_id: uuid.UUID
    from_agent_id: str
    thread_id: str
    content: str
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class SubagentWakeResult:
    tenant_id: uuid.UUID
    parent_agent_id: uuid.UUID
    signal_id: uuid.UUID
    status: str
    detail: str = ""


ParentWakeInvoker = Callable[[SubagentWakeRequest], Awaitable[Any]]


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    text_value = str(value).strip()
    if not text_value:
        return None
    try:
        return uuid.UUID(text_value)
    except (TypeError, ValueError):
        return None


async def _reserve_continuation_wake_budget(
    *,
    signal_id: uuid.UUID,
    metadata: dict[str, Any] | None,
    session_factory: Any = None,
) -> tuple[ExecutionAdmission, ExecutionAdmissionDecision] | None:
    budget_run_id = _uuid_or_none((metadata or {}).get("budget_run_id"))
    if budget_run_id is None:
        return None
    reservation = RuntimeBudgetReservation(
        budget_run_id=budget_run_id,
        reservation_key=f"subagent_wake:{signal_id}:continuation",
        continuation_wakes=1,
        reason="subagent_completion_wake",
        metadata={"signal_id": str(signal_id), **(metadata or {})},
    )
    # Thread the drain's session factory through: the budget rows live in the
    # same database the drain is operating on, not the process-default engine.
    service_kwargs = {"session_factory": session_factory} if session_factory is not None else {}
    admission = ExecutionAdmission(RuntimeBudgetService(**service_kwargs))
    decision = await admission.admit(reservation)
    return admission, decision


async def _settle_continuation_wake_budget(
    reservation_pair: tuple[ExecutionAdmission, ExecutionAdmissionDecision] | None,
    *,
    actual_wakes: int,
    reason: str,
) -> None:
    if reservation_pair is None:
        return
    admission, decision = reservation_pair
    try:
        await admission.settle(
            decision,
            actual_continuation_wakes=actual_wakes,
            reason=reason,
        )
    except Exception as exc:
        logger.warning("[SubagentWake] failed to settle continuation wake budget reservation: %s", exc)


async def _trip_child_failure_breaker_if_needed(
    *,
    tenant_id: uuid.UUID,
    budget_run_id: uuid.UUID,
    session_factory: Any = None,
) -> str | None:
    """Policy-driven §10 parent-wake breaker (enforcement point ②).

    Delegates to ``RuntimeBudgetService.evaluate_wake_breaker`` so the failure /
    reconciliation / child-failure-ratio / parent-invocation thresholds come
    from the run's policy snapshot instead of hardcoded module constants, and so
    the run's ``failures`` / ``needs_reconciliation_count`` / ``parent_invocations``
    counters are materialized from ground truth at the wake boundary.
    """

    service_kwargs = {"session_factory": session_factory} if session_factory is not None else {}
    return await RuntimeBudgetService(**service_kwargs).evaluate_wake_breaker(
        tenant_id=tenant_id,
        budget_run_id=budget_run_id,
    )


def _parent_session_id_from_wake_thread(thread_id: str | None) -> uuid.UUID | None:
    if not thread_id:
        return None
    try:
        return uuid.UUID(str(thread_id))
    except (TypeError, ValueError):
        pass
    parts = str(thread_id).split(":")
    if len(parts) >= 2 and parts[0] == "subagent":
        try:
            return uuid.UUID(parts[1])
        except (TypeError, ValueError):
            return None
    return None


async def drain_subagent_completion_wakes(
    *,
    session_factory: Any = None,
    invoke_parent: ParentWakeInvoker | None = None,
    limit: int = 50,
    max_wakes: int = 10,
) -> list[SubagentWakeResult]:
    """Consume completed-subagent signals and wake idle parents once.

    ``invoke_parent`` is injectable so tests and production wiring can share
    the same atomic consume logic. When no invoker is configured, this function
    leaves signals untouched instead of losing wake work.

    Wake-storm guards (B2): each parent is woken at most ONCE per tick (it reads
    all of its completion signals on wake, so N completed children = 1 wake, not
    N — the surplus signals stay in PG for a later tick); and a single tick wakes
    at most ``max_wakes`` parents total, so one burst cannot fan out unboundedly.
    Recursion is additionally bounded by ``DEFAULT_MAX_SUBAGENT_DEPTH`` on the
    spawn side.
    """
    if invoke_parent is None:
        logger.debug("[SubagentWake] no parent invoker configured; leaving completion signals untouched")
        return []

    async with tenant_scoped_session(None, session_factory=session_factory) as session:
        async with enter_rls_bypass(session, reason="subagent wake daemon — enumerate completion signals"):
            rows = (
                (
                    await session.execute(
                        select(CoordinationSignal)
                        .where(CoordinationSignal.signal_type == SUBAGENT_COMPLETION_SIGNAL)
                        .order_by(CoordinationSignal.created_at)
                        .limit(max(limit, 1))
                    )
                )
                .scalars()
                .all()
            )
        candidates = [
            (
                row.id,
                row.tenant_id,
                row.to_agent_id,
                row.from_agent_id,
                row.thread_id,
                row.content,
                dict(getattr(row, "metadata_json", None) or {}),
            )
            for row in rows
        ]

    results: list[SubagentWakeResult] = []
    woken_parents: set[uuid.UUID] = set()  # per-tick per-parent dedup (one wake reads all signals)
    woken_count = 0
    for signal_id, tenant_id, parent_value, from_agent_id, thread_id, content, metadata in candidates:
        try:
            parent_agent_id = uuid.UUID(str(parent_value))
        except (TypeError, ValueError):
            logger.warning(
                "[SubagentWake] skipping malformed parent agent id on signal %s: %r", signal_id, parent_value
            )
            continue

        if parent_agent_id in woken_parents:
            # already woke this parent this tick — leave the duplicate signal for the next tick
            continue
        if woken_count >= max_wakes:
            # per-tick budget cap reached — remaining signals wait for the next tick
            break

        if await _parent_has_active_run(
            tenant_id=tenant_id,
            parent_agent_id=parent_agent_id,
            session_factory=session_factory,
        ):
            continue

        budget_run_id = _uuid_or_none(metadata.get("budget_run_id"))
        if budget_run_id is not None:
            breaker_reason = await _trip_child_failure_breaker_if_needed(
                tenant_id=tenant_id,
                budget_run_id=budget_run_id,
                session_factory=session_factory,
            )
            if breaker_reason is not None:
                service_kwargs = {"session_factory": session_factory} if session_factory is not None else {}
                breaker_run = await RuntimeBudgetService(**service_kwargs).get_run(
                    tenant_id=tenant_id,
                    budget_run_id=budget_run_id,
                )
                if breaker_run is not None and breaker_run.status == "waiting_budget_approval":
                    results.append(
                        SubagentWakeResult(
                            tenant_id=tenant_id,
                            parent_agent_id=parent_agent_id,
                            signal_id=signal_id,
                            status="waiting_budget_approval",
                            detail=breaker_reason,
                        )
                    )
                    continue
                await _consume_completion_signal(
                    tenant_id=tenant_id,
                    signal_id=signal_id,
                    parent_agent_id=parent_agent_id,
                    session_factory=session_factory,
                )
                results.append(
                    SubagentWakeResult(
                        tenant_id=tenant_id,
                        parent_agent_id=parent_agent_id,
                        signal_id=signal_id,
                        status="breaker",
                        detail=breaker_reason,
                    )
                )
                continue

        try:
            wake_budget = await _reserve_continuation_wake_budget(
                signal_id=signal_id, metadata=metadata, session_factory=session_factory
            )
        except RuntimeBudgetDenied as exc:
            await _consume_completion_signal(
                tenant_id=tenant_id,
                signal_id=signal_id,
                parent_agent_id=parent_agent_id,
                session_factory=session_factory,
            )
            results.append(
                SubagentWakeResult(
                    tenant_id=tenant_id,
                    parent_agent_id=parent_agent_id,
                    signal_id=signal_id,
                    status="denied",
                    detail=str(exc)[:300],
                )
            )
            continue
        if wake_budget is not None and wake_budget[1].waiting:
            results.append(
                SubagentWakeResult(
                    tenant_id=tenant_id,
                    parent_agent_id=parent_agent_id,
                    signal_id=signal_id,
                    status="waiting_budget_approval",
                    detail=wake_budget[1].user_message or "runtime budget approval required",
                )
            )
            continue

        consumed = await _consume_completion_signal(
            tenant_id=tenant_id,
            signal_id=signal_id,
            parent_agent_id=parent_agent_id,
            session_factory=session_factory,
        )
        if consumed is None:
            await _settle_continuation_wake_budget(
                wake_budget,
                actual_wakes=0,
                reason="subagent_completion_wake_already_consumed",
            )
            continue

        # A wake is committed for this parent: count it toward both guards even
        # if the invoke below fails, so a failing parent can't be retried in a
        # tight loop within the same tick.
        woken_parents.add(parent_agent_id)
        woken_count += 1

        request = SubagentWakeRequest(
            tenant_id=tenant_id,
            parent_agent_id=parent_agent_id,
            signal_id=consumed["signal_id"],
            from_agent_id=consumed["from_agent_id"],
            thread_id=consumed["thread_id"],
            content=consumed["content"],
            metadata=consumed["metadata"],
        )
        try:
            await invoke_parent(request)
            await _settle_continuation_wake_budget(
                wake_budget,
                actual_wakes=1,
                reason="subagent_completion_wake_completed",
            )
            results.append(
                SubagentWakeResult(
                    tenant_id=tenant_id,
                    parent_agent_id=parent_agent_id,
                    signal_id=request.signal_id,
                    status="woken",
                )
            )
        except Exception as exc:
            logger.error("[SubagentWake] parent wake failed for signal %s: %s", signal_id, exc, exc_info=True)
            await _settle_continuation_wake_budget(
                wake_budget,
                actual_wakes=1,
                reason="subagent_completion_wake_failed",
            )
            results.append(
                SubagentWakeResult(
                    tenant_id=tenant_id,
                    parent_agent_id=parent_agent_id,
                    signal_id=request.signal_id,
                    status="failed",
                    detail=str(exc)[:300],
                )
            )
    return results


async def _parent_has_active_run(
    *,
    tenant_id: uuid.UUID,
    parent_agent_id: uuid.UUID,
    session_factory: Any = None,
) -> bool:
    async with tenant_scoped_session(str(tenant_id), session_factory=session_factory) as session:
        row = (
            await session.execute(
                select(RuntimeTask.id)
                .where(
                    RuntimeTask.parent_agent_id == parent_agent_id,
                    RuntimeTask.status.in_(_ACTIVE_PARENT_RUN_STATUSES),
                )
                .limit(1)
            )
        ).first()
    return row is not None


async def _consume_completion_signal(
    *,
    tenant_id: uuid.UUID,
    signal_id: uuid.UUID,
    parent_agent_id: uuid.UUID,
    session_factory: Any = None,
) -> dict[str, Any] | None:
    async with tenant_scoped_session(str(tenant_id), session_factory=session_factory) as session:
        row = (
            await session.execute(
                text(
                    "DELETE FROM coordination_signals "
                    "WHERE id = ("
                    "  SELECT id FROM coordination_signals "
                    "  WHERE tenant_id = :tenant AND id = :signal_id "
                    "    AND to_agent_id = :parent_agent_id AND signal_type = :signal_type "
                    "  ORDER BY created_at LIMIT 1"
                    ") RETURNING id, from_agent_id, thread_id, content, metadata"
                ),
                {
                    "tenant": tenant_id,
                    "signal_id": signal_id,
                    "parent_agent_id": str(parent_agent_id),
                    "signal_type": SUBAGENT_COMPLETION_SIGNAL,
                },
            )
        ).first()
    if row is None:
        return None
    row_mapping = row._mapping
    return {
        "signal_id": row_mapping["id"],
        "from_agent_id": row_mapping["from_agent_id"],
        "thread_id": row_mapping["thread_id"],
        "content": row_mapping["content"],
        "metadata": dict(row_mapping["metadata"] or {}),
    }


def build_production_parent_wake_invoker() -> ParentWakeInvoker:
    """Build the real ParentWakeInvoker that the daemon wires in production.

    Routes completed background subagent signals into the same CC-style
    task-notification continuation path used by direct subagent/A2A completion.
    This keeps the daemon fallback on the normal RuntimeTask/web-chat/T0 path
    instead of maintaining a second direct ``invoke_agent`` wake implementation.
    """

    async def _wake_parent(request: SubagentWakeRequest) -> Any:
        from app.models.agent import Agent
        from app.models.chat_session import ChatSession
        from app.models.user import User
        from app.services.agent_session_continuation import continue_parent_session_with_task_notification

        parent_session_id = _parent_session_id_from_wake_thread(request.thread_id)
        if parent_session_id is None:
            logger.warning(
                "[SubagentWake] signal %s has no parent session in thread_id=%r — skipping wake",
                request.signal_id,
                request.thread_id,
            )
            return None
        async with tenant_scoped_session(str(request.tenant_id)) as session:
            agent = (
                await session.execute(select(Agent).where(Agent.id == request.parent_agent_id))
            ).scalar_one_or_none()
            if agent is None:
                logger.warning("[SubagentWake] parent agent %s not found — skipping wake", request.parent_agent_id)
                return None
            if agent.status in ("expired", "stopped", "error", "archived"):
                logger.info(
                    "[SubagentWake] parent agent %s not runnable (status=%s) — skipping wake",
                    request.parent_agent_id,
                    agent.status,
                )
                return None
            parent_session = (
                await session.execute(
                    select(ChatSession).where(
                        ChatSession.id == parent_session_id,
                        ChatSession.agent_id == request.parent_agent_id,
                    )
                )
            ).scalar_one_or_none()
            if parent_session is None:
                logger.warning(
                    "[SubagentWake] parent session %s for agent %s not found — skipping wake",
                    parent_session_id,
                    request.parent_agent_id,
                )
                return None
            owner_id = getattr(parent_session, "user_id", None) or getattr(agent, "creator_id", None)
            owner = (
                (await session.execute(select(User).where(User.id == uuid.UUID(str(owner_id))))).scalar_one_or_none()
                if owner_id
                else None
            )
            if owner is None:
                logger.warning(
                    "[SubagentWake] parent session %s for agent %s has no user — skipping wake",
                    parent_session_id,
                    request.parent_agent_id,
                )
                return None
            return await continue_parent_session_with_task_notification(
                db=session,
                agent=agent,
                user=owner,
                session=parent_session,
                task_id=str(request.signal_id),
                task_type="subagent",
                status="completed",
                summary=request.content,
                child_agent_name=request.from_agent_id,
                source="subagent_wake",
                metadata={
                    "signal_id": str(request.signal_id),
                    "from_agent_id": request.from_agent_id,
                    "thread_id": request.thread_id,
                    "parent_agent_id": str(request.parent_agent_id),
                    **(request.metadata or {}),
                },
            )

    return _wake_parent
