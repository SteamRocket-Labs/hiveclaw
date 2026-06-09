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

    Re-invokes the parent agent so it consumes its completed background
    subagents' results, instead of waiting for its own next run / heartbeat
    backfill. Follows the unattended-invoke pattern: load the agent, confirm it
    is runnable, resolve its model, then call ``invoke_agent`` under an agent_bot
    identity with a dedicated ``subagent_wake`` source so the run is auditable
    and distinct from user/trigger turns.
    """

    async def _wake_parent(request: SubagentWakeRequest) -> Any:
        from app.core.execution_context import set_agent_bot_identity
        from app.kernel.contracts import ExecutionIdentityRef
        from app.models.agent import Agent
        from app.models.llm import LLMModel
        from app.runtime.invoker import AgentInvocationRequest, invoke_agent
        from app.runtime.session import SessionContext

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
            model_id = agent.primary_model_id or agent.fallback_model_id
            if not model_id:
                logger.warning("[SubagentWake] parent agent %s has no model — skipping wake", request.parent_agent_id)
                return None
            model = (
                await session.execute(
                    select(LLMModel).where(LLMModel.id == model_id, LLMModel.tenant_id == agent.tenant_id)
                )
            ).scalar_one_or_none()
            if model is None:
                logger.warning(
                    "[SubagentWake] parent agent %s model %s missing — skipping wake", request.parent_agent_id, model_id
                )
                return None
            fallback_model = None
            if agent.primary_model_id and agent.fallback_model_id:
                fallback_model = (
                    await session.execute(
                        select(LLMModel).where(
                            LLMModel.id == agent.fallback_model_id, LLMModel.tenant_id == agent.tenant_id
                        )
                    )
                ).scalar_one_or_none()
            agent_name = agent.name
            role_description = agent.role_description or ""
            creator_id = agent.creator_id
            max_tool_rounds = getattr(agent, "max_tool_rounds", None)

        set_agent_bot_identity(request.parent_agent_id, agent_name, source="subagent_wake")

        wake_message = (
            f"Your background subagent ({request.from_agent_id}) has finished.\n\n"
            f"Result:\n{request.content}\n\n"
            "Review this result and either continue the task or close the loop. "
            "If you have other background workers still reporting in, use "
            "consume_subagent_signals to collect their results too."
        )
        runtime_messages = [{"role": "user", "content": wake_message}]

        return await invoke_agent(
            AgentInvocationRequest(
                model=model,
                fallback_model=fallback_model,
                messages=runtime_messages,
                memory_messages=runtime_messages,
                agent_name=agent_name,
                role_description=role_description,
                agent_id=request.parent_agent_id,
                user_id=creator_id,
                execution_identity=ExecutionIdentityRef(
                    identity_type="agent_bot",
                    identity_id=request.parent_agent_id,
                    label=f"Agent: {agent_name} (subagent wake)",
                ),
                session_context=SessionContext(
                    source="subagent_wake",
                    channel="subagent_wake",
                    metadata={"woken_by_signal": str(request.signal_id), "thread_id": request.thread_id},
                ),
                core_tools_only=True,
                max_tool_rounds=max_tool_rounds,
            )
        )

    return _wake_parent
