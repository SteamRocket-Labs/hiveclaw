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


@dataclass(frozen=True, slots=True)
class SubagentWakeResult:
    tenant_id: uuid.UUID
    parent_agent_id: uuid.UUID
    signal_id: uuid.UUID
    status: str
    detail: str = ""


ParentWakeInvoker = Callable[[SubagentWakeRequest], Awaitable[Any]]


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
            )
            for row in rows
        ]

    results: list[SubagentWakeResult] = []
    woken_parents: set[uuid.UUID] = set()  # per-tick per-parent dedup (one wake reads all signals)
    woken_count = 0
    for signal_id, tenant_id, parent_value, from_agent_id, thread_id, content in candidates:
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

        consumed = await _consume_completion_signal(
            tenant_id=tenant_id,
            signal_id=signal_id,
            parent_agent_id=parent_agent_id,
            session_factory=session_factory,
        )
        if consumed is None:
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
        )
        try:
            await invoke_parent(request)
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
                    ") RETURNING id, from_agent_id, thread_id, content"
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
    return {
        "signal_id": row.id,
        "from_agent_id": row.from_agent_id,
        "thread_id": row.thread_id,
        "content": row.content,
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
                await session.execute(select(User).where(User.id == uuid.UUID(str(owner_id))))
            ).scalar_one_or_none() if owner_id else None
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
                },
            )

    return _wake_parent
