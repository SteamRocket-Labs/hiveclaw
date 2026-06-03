"""Runtime coordination primitives for agent-agent work."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Callable, Literal


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


SentinelRuntimePath = Literal["signal", "checkpoint"]


@dataclass(frozen=True, slots=True)
class Sentinel:
    id: str
    owner_agent_id: str
    target_agent_id: str
    condition: str
    runtime_path: SentinelRuntimePath
    checkpoint_approver_id: str | None = None
    escalation_chain: list[str] = field(default_factory=list)
    deadline_seconds: int = 1800


@dataclass(frozen=True, slots=True)
class SentinelEmission:
    kind: SentinelRuntimePath
    sentinel: Sentinel
    signal: Signal | None = None
    checkpoint: Checkpoint | None = None


class CoordinationRuntime:
    def __init__(self, now: Callable[[], datetime] | None = None) -> None:
        self._now = now or (lambda: datetime.now(UTC))
        self._leases: dict[str, Lease] = {}
        self._signals: list[Signal] = []
        self._checkpoints: dict[str, Checkpoint] = {}
        self._sentinels: dict[str, Sentinel] = {}

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

    def reset(self) -> None:
        self._leases.clear()
        self._signals.clear()
        self._checkpoints.clear()
        self._sentinels.clear()

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

    def consume_signals(
        self,
        agent_id: str,
        *,
        thread_id: str | None = None,
        signal_type: str | None = None,
    ) -> list[Signal]:
        """Return matching signals and remove them from the in-memory queue."""

        consumed: list[Signal] = []
        remaining: list[Signal] = []
        for signal in self._signals:
            matches = (
                signal.to_agent_id == agent_id
                and (thread_id is None or signal.thread_id == thread_id)
                and (signal_type is None or signal.signal_type == signal_type)
            )
            if matches:
                consumed.append(signal)
            else:
                remaining.append(signal)
        self._signals = remaining
        return consumed

    def create_checkpoint(
        self,
        *,
        action: str,
        approver_id: str,
        escalation_chain: list[str],
        deadline_at: datetime,
        metadata: dict[str, str] | None = None,
    ) -> Checkpoint:
        checkpoint = Checkpoint(
            id=str(uuid.uuid4()),
            action=action,
            approver_id=approver_id,
            escalation_chain=list(escalation_chain),
            deadline_at=deadline_at,
            current_approver_id=approver_id,
            metadata=dict(metadata or {}),
        )
        self._checkpoints[checkpoint.id] = checkpoint
        return checkpoint

    def get_checkpoint(self, checkpoint_id: str) -> Checkpoint:
        return self._checkpoints[checkpoint_id]

    def register_sentinel(
        self,
        *,
        sentinel_id: str,
        owner_agent_id: str,
        target_agent_id: str,
        condition: str,
        runtime_path: SentinelRuntimePath,
        checkpoint_approver_id: str | None = None,
        escalation_chain: list[str] | None = None,
        deadline_seconds: int = 1800,
    ) -> Sentinel:
        if runtime_path not in ("signal", "checkpoint"):
            raise ValueError(f"Unsupported sentinel runtime_path: {runtime_path}")
        sentinel = Sentinel(
            id=sentinel_id,
            owner_agent_id=owner_agent_id,
            target_agent_id=target_agent_id,
            condition=condition,
            runtime_path=runtime_path,
            checkpoint_approver_id=checkpoint_approver_id,
            escalation_chain=list(escalation_chain or []),
            deadline_seconds=deadline_seconds,
        )
        self._sentinels[sentinel.id] = sentinel
        return sentinel

    def fire_sentinel(
        self,
        sentinel_id: str,
        *,
        content: str,
        thread_id: str | None = None,
        metadata: dict[str, str] | None = None,
    ) -> SentinelEmission:
        sentinel = self._sentinels[sentinel_id]
        if sentinel.runtime_path == "signal":
            signal = self.send_signal(
                from_agent_id=sentinel.owner_agent_id,
                to_agent_id=sentinel.target_agent_id,
                content=content,
                signal_type=f"sentinel:{sentinel.condition}",
                thread_id=thread_id,
            )
            return SentinelEmission(kind="signal", sentinel=sentinel, signal=signal)

        if not sentinel.checkpoint_approver_id:
            raise ValueError("Checkpoint sentinel requires checkpoint_approver_id")
        checkpoint_metadata = {
            "sentinel_id": sentinel.id,
            "target_agent_id": sentinel.target_agent_id,
            "content": content,
            **{key: str(value) for key, value in (metadata or {}).items()},
        }
        checkpoint = self.create_checkpoint(
            action=sentinel.condition,
            approver_id=sentinel.checkpoint_approver_id,
            escalation_chain=sentinel.escalation_chain,
            deadline_at=self._now() + timedelta(seconds=max(sentinel.deadline_seconds, 1)),
            metadata=checkpoint_metadata,
        )
        return SentinelEmission(kind="checkpoint", sentinel=sentinel, checkpoint=checkpoint)

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


coordination_runtime = CoordinationRuntime()
