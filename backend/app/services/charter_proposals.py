"""Charter calibration proposal store (Phase 5/6 residual).

Dream produces charter-calibration suggestions but is forbidden from
mutating the charter itself. This module is the durable hand-off between
dream's `propose_charter_calibrations_from_feedback()` and an owner /
company admin who approves or rejects each proposal.

The store is sqlite-backed so proposals survive restart and an owner can
review them on their own schedule. Approval / rejection is recorded with
the deciding principal and an optional reason so dream / future calibration
audits can show why a clause did or did not move.
"""

from __future__ import annotations

import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path


class ProposalKind(StrEnum):
    CONSIDER_FULL_AUTHORITY = "consider_full_authority"
    TIGHTEN_TO_CONFIRM_FIRST = "tighten_to_confirm_first"


class ProposalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    EXPIRED = "expired"


class ProposalAlreadyDecided(Exception):
    pass


@dataclass(slots=True)
class CharterProposal:
    id: str
    agent_id: str
    decision_id: str
    action: str
    proposal_kind: str
    reason: str
    status: str
    created_at: datetime
    decided_at: datetime | None = None
    decided_by: str | None = None
    decision_reason: str | None = None


_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS charter_proposals (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    decision_id TEXT NOT NULL,
    action TEXT NOT NULL,
    proposal_kind TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    decided_at TEXT,
    decided_by TEXT,
    decision_reason TEXT
)
"""


class CharterProposalStore:
    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as conn:
            conn.execute(_CREATE_TABLE)

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def submit(
        self,
        *,
        agent_id: str,
        decision_id: str,
        action: str,
        proposal_kind: ProposalKind | str,
        reason: str,
        created_at: datetime | None = None,
    ) -> CharterProposal:
        kind = ProposalKind(proposal_kind) if not isinstance(proposal_kind, ProposalKind) else proposal_kind
        proposal = CharterProposal(
            id=str(uuid.uuid4()),
            agent_id=agent_id,
            decision_id=decision_id,
            action=action,
            proposal_kind=kind.value,
            reason=reason,
            status=ProposalStatus.PENDING.value,
            created_at=(created_at or datetime.now(timezone.utc)).astimezone(timezone.utc),
        )
        with self._conn() as conn:
            conn.execute(
                "INSERT INTO charter_proposals(id, agent_id, decision_id, action, proposal_kind, reason, status, "
                "created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    proposal.id,
                    proposal.agent_id,
                    proposal.decision_id,
                    proposal.action,
                    proposal.proposal_kind,
                    proposal.reason,
                    proposal.status,
                    proposal.created_at.isoformat(),
                ),
            )
        return proposal

    def get(self, proposal_id: str) -> CharterProposal | None:
        with self._conn() as conn:
            row = conn.execute("SELECT * FROM charter_proposals WHERE id = ?", (proposal_id,)).fetchone()
        if row is None:
            return None
        return _row_to_proposal(row)

    def list_pending(self, *, agent_id: str | None = None) -> list[CharterProposal]:
        with self._conn() as conn:
            if agent_id is None:
                rows = conn.execute(
                    "SELECT * FROM charter_proposals WHERE status = 'pending' ORDER BY created_at"
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM charter_proposals WHERE status = 'pending' AND agent_id = ? ORDER BY created_at",
                    (agent_id,),
                ).fetchall()
        return [_row_to_proposal(row) for row in rows]

    def approve(self, proposal_id: str, *, by: str, decision_reason: str | None = None) -> CharterProposal:
        return self._decide(proposal_id, ProposalStatus.APPROVED, by=by, decision_reason=decision_reason)

    def reject(self, proposal_id: str, *, by: str, decision_reason: str | None = None) -> CharterProposal:
        return self._decide(proposal_id, ProposalStatus.REJECTED, by=by, decision_reason=decision_reason)

    def _decide(
        self,
        proposal_id: str,
        target_status: ProposalStatus,
        *,
        by: str,
        decision_reason: str | None,
    ) -> CharterProposal:
        existing = self.get(proposal_id)
        if existing is None:
            raise KeyError(f"Unknown charter proposal: {proposal_id}")
        if existing.status != ProposalStatus.PENDING.value:
            raise ProposalAlreadyDecided(f"Proposal {proposal_id} already {existing.status}")
        decided_at = datetime.now(timezone.utc)
        with self._conn() as conn:
            conn.execute(
                "UPDATE charter_proposals SET status = ?, decided_at = ?, decided_by = ?, decision_reason = ? "
                "WHERE id = ?",
                (target_status.value, decided_at.isoformat(), by, decision_reason, proposal_id),
            )
        existing.status = target_status.value
        existing.decided_at = decided_at
        existing.decided_by = by
        existing.decision_reason = decision_reason
        return existing

    def expire_stale(self, *, max_age_days: int = 7, now: datetime | None = None) -> list[str]:
        boundary = (now or datetime.now(timezone.utc)).astimezone(timezone.utc) - timedelta(days=max_age_days)
        with self._conn() as conn:
            rows = conn.execute("SELECT id, created_at FROM charter_proposals WHERE status = 'pending'").fetchall()
            expired_ids: list[str] = []
            for row in rows:
                created_at = datetime.fromisoformat(row["created_at"])
                if created_at <= boundary:
                    expired_ids.append(row["id"])
            for pid in expired_ids:
                conn.execute("UPDATE charter_proposals SET status = 'expired' WHERE id = ?", (pid,))
        return expired_ids


def _row_to_proposal(row: sqlite3.Row) -> CharterProposal:
    return CharterProposal(
        id=row["id"],
        agent_id=row["agent_id"],
        decision_id=row["decision_id"],
        action=row["action"],
        proposal_kind=row["proposal_kind"],
        reason=row["reason"],
        status=row["status"],
        created_at=datetime.fromisoformat(row["created_at"]),
        decided_at=datetime.fromisoformat(row["decided_at"]) if row["decided_at"] else None,
        decided_by=row["decided_by"],
        decision_reason=row["decision_reason"],
    )
