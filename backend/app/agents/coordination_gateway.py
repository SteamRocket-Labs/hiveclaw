"""CoordinationGateway: unified async protocol for production wiring.

Two implementations satisfy the same `async` surface:

  * `InProcessCoordinationGateway` wraps the in-memory `CoordinationRuntime`
    singleton so single-process / test code can keep using sync semantics
    under the hood while the call site reads `await gateway.xxx(...)`.

  * `CoordinationRepository` (PostgreSQL-backed) is natively async; this
    module just re-exports it as a satisfying implementation.

Production callers — `orchestrator.delegate_async` and
`tools/service` confirm-first preflight — accept an optional
`coordination_gateway`. When `None`, they fall back to the sync
in-process runtime (current behaviour); when provided, they go through
the gateway, which lets a deployment choose:

  * single process: stay on `InProcessCoordinationGateway`
  * multi-worker:   build a `CoordinationRepository(session, tenant_id)`
                    per request scope and pass it in
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any, Protocol, runtime_checkable

from app.agents.coordination import (
    Checkpoint,
    CoordinationRuntime,
    Lease,  # re-export for typing
    LeaseAcquireResult,
    Signal,
    coordination_runtime,
)


__all__ = [
    "CoordinationGateway",
    "InProcessCoordinationGateway",
    "default_in_process_gateway",
    "Lease",
    "LeaseAcquireResult",
    "Signal",
    "Checkpoint",
]


logger = logging.getLogger(__name__)


@runtime_checkable
class CoordinationGateway(Protocol):
    """Async interface every production caller depends on."""

    async def acquire_lease(self, *, task_key: str, agent_id: str, ttl_seconds: int) -> LeaseAcquireResult: ...

    async def release_lease(
        self,
        *,
        task_key: str,
        agent_id: str,
        lease_id: str | None = None,
    ) -> bool: ...

    async def send_signal(
        self,
        *,
        from_agent_id: str,
        to_agent_id: str,
        content: str,
        signal_type: str,
        thread_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> Signal: ...

    async def read_signals(self, agent_id: str, *, thread_id: str | None = None) -> list[Signal]: ...

    async def create_checkpoint(
        self,
        *,
        action: str,
        approver_id: str,
        escalation_chain: list[str],
        deadline_at: datetime,
        metadata: dict[str, str] | None = None,
    ) -> Checkpoint: ...

    async def get_checkpoint(self, checkpoint_id: str) -> Checkpoint | None: ...

    async def escalate_expired_checkpoints(self) -> list[str]: ...


class InProcessCoordinationGateway:
    """Async adapter over the sync `CoordinationRuntime` singleton."""

    def __init__(self, runtime: CoordinationRuntime | None = None) -> None:
        self._runtime = runtime or coordination_runtime

    async def acquire_lease(self, *, task_key: str, agent_id: str, ttl_seconds: int) -> LeaseAcquireResult:
        return self._runtime.acquire_lease(task_key=task_key, agent_id=agent_id, ttl_seconds=ttl_seconds)

    async def release_lease(
        self,
        *,
        task_key: str,
        agent_id: str,
        lease_id: str | None = None,
    ) -> bool:
        return self._runtime.release_lease(task_key=task_key, agent_id=agent_id, lease_id=lease_id)

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
        return self._runtime.send_signal(
            from_agent_id=from_agent_id,
            to_agent_id=to_agent_id,
            content=content,
            signal_type=signal_type,
            thread_id=thread_id,
            metadata=metadata,
        )

    async def read_signals(self, agent_id: str, *, thread_id: str | None = None) -> list[Signal]:
        return self._runtime.read_signals(agent_id, thread_id=thread_id)

    async def create_checkpoint(
        self,
        *,
        action: str,
        approver_id: str,
        escalation_chain: list[str],
        deadline_at: datetime,
        metadata: dict[str, str] | None = None,
    ) -> Checkpoint:
        return self._runtime.create_checkpoint(
            action=action,
            approver_id=approver_id,
            escalation_chain=escalation_chain,
            deadline_at=deadline_at,
            metadata=metadata,
        )

    async def get_checkpoint(self, checkpoint_id: str) -> Checkpoint | None:
        # Sync runtime raises KeyError for "not found"; async gateway protocol
        # uses None to match repository semantics. Translation, not a swallow.
        try:
            return self._runtime.get_checkpoint(checkpoint_id)
        except KeyError:
            logger.debug("coordination_gateway: checkpoint %s not found in in-process runtime", checkpoint_id)
            return None

    async def escalate_expired_checkpoints(self) -> list[str]:
        return self._runtime.escalate_expired_checkpoints()


def default_in_process_gateway() -> InProcessCoordinationGateway:
    """Module-level default gateway used by production callers as the fallback."""
    return InProcessCoordinationGateway()
