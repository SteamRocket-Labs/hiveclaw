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
from app.database import tenant_scoped_session
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


async def drain_subagent_completion_wakes(
    *,
    session_factory: Any = None,
    invoke_parent: ParentWakeInvoker | None = None,
    limit: int = 50,
) -> list[SubagentWakeResult]:
    """Consume completed-subagent signals and wake idle parents once.

    ``invoke_parent`` is injectable so tests and production wiring can share
    the same atomic consume logic. When no invoker is configured, this function
    leaves signals untouched instead of losing wake work.
    """
    if invoke_parent is None:
        logger.debug("[SubagentWake] no parent invoker configured; leaving completion signals untouched")
        return []

    async with tenant_scoped_session(None, session_factory=session_factory) as session:
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
    for signal_id, tenant_id, parent_value, from_agent_id, thread_id, content in candidates:
        try:
            parent_agent_id = uuid.UUID(str(parent_value))
        except (TypeError, ValueError):
            logger.warning("[SubagentWake] skipping malformed parent agent id on signal %s: %r", signal_id, parent_value)
            continue

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
