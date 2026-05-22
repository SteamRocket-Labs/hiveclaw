"""Runtime coordination primitives for agent-agent work."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Callable


@dataclass(frozen=True, slots=True)
class Lease:
    id: str
    task_key: str
    agent_id: str
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class LeaseAcquireResult:
    acquired: bool
    lease: Lease | None = None
    existing_lease_id: str | None = None


@dataclass(frozen=True, slots=True)
class Signal:
    id: str
    from_agent_id: str
    to_agent_id: str
    content: str
    signal_type: str
    thread_id: str
    created_at: datetime


@dataclass(slots=True)
class Checkpoint:
    id: str
    action: str
    approver_id: str
    escalation_chain: list[str]
    deadline_at: datetime
    current_approver_id: str
    status: str = "pending"
    metadata: dict[str, str] = field(default_factory=dict)


class CoordinationRuntime:
    def __init__(self, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or (lambda: datetime.now(UTC))
        self._leases: dict[str, Lease] = {}
        self._signals: list[Signal] = []
        self._checkpoints: dict[str, Checkpoint] = {}

    def acquire_lease(self, *, task_key: str, agent_id: str, ttl_seconds: int) -> LeaseAcquireResult:
        current = self._now()
        existing = self._leases.get(task_key)
        if existing and existing.expires_at > current:
            return LeaseAcquireResult(acquired=False, existing_lease_id=existing.id)
        lease = Lease(
            id=str(uuid.uuid4()),
            task_key=task_key,
            agent_id=agent_id,
            expires_at=current + timedelta(seconds=ttl_seconds),
        )
        self._leases[task_key] = lease
        return LeaseAcquireResult(acquired=True, lease=lease)

    def send_signal(
        self,
        *,
        from_agent_id: str,
        to_agent_id: str,
        content: str,
        signal_type: str,
        thread_id: str | None = None,
    ) -> Signal:
        signal = Signal(
            id=str(uuid.uuid4()),
            from_agent_id=from_agent_id,
            to_agent_id=to_agent_id,
            content=content,
            signal_type=signal_type,
            thread_id=thread_id or str(uuid.uuid4()),
            created_at=self._now(),
        )
        self._signals.append(signal)
        return signal

    def read_signals(self, agent_id: str, *, thread_id: str | None = None) -> list[Signal]:
        return [
            signal
            for signal in self._signals
            if signal.to_agent_id == agent_id and (thread_id is None or signal.thread_id == thread_id)
        ]

    def create_checkpoint(
        self,
        *,
        action: str,
        approver_id: str,
        escalation_chain: list[str],
        deadline_at: datetime,
    ) -> Checkpoint:
        checkpoint = Checkpoint(
            id=str(uuid.uuid4()),
            action=action,
            approver_id=approver_id,
            escalation_chain=list(escalation_chain),
            deadline_at=deadline_at,
            current_approver_id=approver_id,
        )
        self._checkpoints[checkpoint.id] = checkpoint
        return checkpoint

    def get_checkpoint(self, checkpoint_id: str) -> Checkpoint:
        return self._checkpoints[checkpoint_id]

    def escalate_expired_checkpoints(self) -> list[str]:
        current = self._now()
        escalated: list[str] = []
        for checkpoint in self._checkpoints.values():
            if checkpoint.status != "pending" or checkpoint.deadline_at > current or not checkpoint.escalation_chain:
                continue
            checkpoint.current_approver_id = checkpoint.escalation_chain.pop(0)
            checkpoint.metadata["escalated_at"] = current.isoformat()
            escalated.append(checkpoint.id)
        return escalated

