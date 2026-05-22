"""SQLite-backed coordination store (§17.2 Phase 7 residual).

`CoordinationRuntime` is process-local. When two Hive workers need to
share lease, signal, and checkpoint state — most importantly so that a
duplicate cross-process delegation cannot acquire the same task lease,
and so an unanswered checkpoint persists across restarts — they point
at the same SQLite file via `SqliteCoordinationStore`.

The store mirrors the runtime's public API surface for the four
primitives it covers (Lease / Signal / Checkpoint). Sentinel state is
intentionally not persisted yet; it is owned by the in-process
proactive loop and re-derived per tick.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Callable

from app.agents.coordination import Checkpoint, Lease, LeaseAcquireResult, Signal


_CREATE_LEASES = """
CREATE TABLE IF NOT EXISTS coordination_leases (
    task_key TEXT PRIMARY KEY,
    id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    expires_at TEXT NOT NULL
)
"""

_CREATE_SIGNALS = """
CREATE TABLE IF NOT EXISTS coordination_signals (
    id TEXT PRIMARY KEY,
    from_agent_id TEXT NOT NULL,
    to_agent_id TEXT NOT NULL,
    content TEXT NOT NULL,
    signal_type TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    created_at TEXT NOT NULL
)
"""

_CREATE_CHECKPOINTS = """
CREATE TABLE IF NOT EXISTS coordination_checkpoints (
    id TEXT PRIMARY KEY,
    action TEXT NOT NULL,
    approver_id TEXT NOT NULL,
    escalation_chain TEXT NOT NULL,
    deadline_at TEXT NOT NULL,
    current_approver_id TEXT NOT NULL,
    status TEXT NOT NULL,
    metadata TEXT NOT NULL
)
"""


class SqliteCoordinationStore:
    def __init__(self, db_path: Path, *, now: Callable[[], datetime] | None = None) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._now = now or (lambda: datetime.now(UTC))
        self._connect_and_init()

    def _connect_and_init(self) -> None:
        with self._conn() as conn:
            conn.execute(_CREATE_LEASES)
            conn.execute(_CREATE_SIGNALS)
            conn.execute(_CREATE_CHECKPOINTS)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, isolation_level="DEFERRED")
        conn.row_factory = sqlite3.Row
        return conn

    def acquire_lease(self, *, task_key: str, agent_id: str, ttl_seconds: int) -> LeaseAcquireResult:
        current = self._now()
        lease_id = str(uuid.uuid4())
        expires_at = current + timedelta(seconds=ttl_seconds)
        with self._conn() as conn:
            cur = conn.execute(
                "SELECT id, agent_id, expires_at FROM coordination_leases WHERE task_key = ?",
                (task_key,),
            )
            existing = cur.fetchone()
            if existing is not None:
                existing_expiry = datetime.fromisoformat(existing["expires_at"])
                if existing_expiry > current:
                    return LeaseAcquireResult(acquired=False, existing_lease_id=existing["id"])
                conn.execute("DELETE FROM coordination_leases WHERE task_key = ?", (task_key,))
            conn.execute(
                "INSERT INTO coordination_leases(task_key, id, agent_id, expires_at) VALUES (?, ?, ?, ?)",
                (task_key, lease_id, agent_id, expires_at.isoformat()),
            )
        lease = Lease(id=lease_id, task_key=task_key, agent_id=agent_id, expires_at=expires_at)
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
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO coordination_signals(id, from_agent_id, to_agent_id, content, signal_type, thread_id, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    signal.id,
                    signal.from_agent_id,
                    signal.to_agent_id,
                    signal.content,
                    signal.signal_type,
                    signal.thread_id,
                    signal.created_at.isoformat(),
                ),
            )
        return signal

    def read_signals(self, agent_id: str, *, thread_id: str | None = None) -> list[Signal]:
        with self._conn() as conn:
            if thread_id is None:
                cur = conn.execute(
                    "SELECT id, from_agent_id, to_agent_id, content, signal_type, thread_id, created_at "
                    "FROM coordination_signals WHERE to_agent_id = ? ORDER BY created_at",
                    (agent_id,),
                )
            else:
                cur = conn.execute(
                    "SELECT id, from_agent_id, to_agent_id, content, signal_type, thread_id, created_at "
                    "FROM coordination_signals WHERE to_agent_id = ? AND thread_id = ? ORDER BY created_at",
                    (agent_id, thread_id),
                )
            return [
                Signal(
                    id=row["id"],
                    from_agent_id=row["from_agent_id"],
                    to_agent_id=row["to_agent_id"],
                    content=row["content"],
                    signal_type=row["signal_type"],
                    thread_id=row["thread_id"],
                    created_at=datetime.fromisoformat(row["created_at"]),
                )
                for row in cur.fetchall()
            ]

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
            status="pending",
            metadata=dict(metadata or {}),
        )
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO coordination_checkpoints(id, action, approver_id, escalation_chain, deadline_at, "
                "current_approver_id, status, metadata) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    checkpoint.id,
                    checkpoint.action,
                    checkpoint.approver_id,
                    json.dumps(checkpoint.escalation_chain),
                    checkpoint.deadline_at.isoformat(),
                    checkpoint.current_approver_id,
                    checkpoint.status,
                    json.dumps(checkpoint.metadata),
                ),
            )
        return checkpoint

    def get_checkpoint(self, checkpoint_id: str) -> Checkpoint | None:
        with self._conn() as conn:
            row = conn.execute(
                "SELECT id, action, approver_id, escalation_chain, deadline_at, "
                "current_approver_id, status, metadata FROM coordination_checkpoints WHERE id = ?",
                (checkpoint_id,),
            ).fetchone()
        if row is None:
            return None
        return Checkpoint(
            id=row["id"],
            action=row["action"],
            approver_id=row["approver_id"],
            escalation_chain=json.loads(row["escalation_chain"]),
            deadline_at=datetime.fromisoformat(row["deadline_at"]),
            current_approver_id=row["current_approver_id"],
            status=row["status"],
            metadata=json.loads(row["metadata"]),
        )

    def escalate_expired_checkpoints(self) -> list[str]:
        current = self._now()
        escalated: list[str] = []
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT id, escalation_chain, deadline_at, status, metadata, current_approver_id "
                "FROM coordination_checkpoints WHERE status = 'pending'"
            ).fetchall()
            for row in rows:
                if datetime.fromisoformat(row["deadline_at"]) > current:
                    continue
                escalation_chain = json.loads(row["escalation_chain"])
                if not escalation_chain:
                    continue
                next_approver = escalation_chain.pop(0)
                metadata = json.loads(row["metadata"])
                metadata["escalated_at"] = current.isoformat()
                conn.execute(
                    "UPDATE coordination_checkpoints SET current_approver_id = ?, escalation_chain = ?, metadata = ? "
                    "WHERE id = ?",
                    (next_approver, json.dumps(escalation_chain), json.dumps(metadata), row["id"]),
                )
                escalated.append(row["id"])
        return escalated
