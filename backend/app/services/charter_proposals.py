"""Charter calibration proposal store (Phase 15 redo).

PostgreSQL-backed durable hand-off between dream's
`propose_charter_calibrations_from_feedback()` and the owner /
company admin who approves or rejects each proposal. Replaces the
local-sqlite shim from the first Phase 15 implementation.

Approved proposals are staged as Soul Candidate signals with an audit line.
The store still never stages pending/rejected proposals and never mutates
company governance or `soul.md` directly.
"""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from pathlib import Path
from typing import Callable

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.charter_proposal import CharterProposal as CharterProposalRow


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


class CharterProposalStore:
    """Tenant-scoped charter proposal repo backed by AsyncSession + asyncpg."""

    def __init__(
        self,
        session: AsyncSession,
        *,
        tenant_id: uuid.UUID,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._now = now or (lambda: datetime.now(timezone.utc))

    async def submit(
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
        new_id = uuid.uuid4()
        when = (created_at or self._now()).astimezone(timezone.utc)
        row = CharterProposalRow(
            id=new_id,
            tenant_id=self._tenant_id,
            agent_id=agent_id,
            decision_id=decision_id,
            action=action,
            proposal_kind=kind.value,
            reason=reason,
            status=ProposalStatus.PENDING.value,
            created_at=when,
        )
        self._session.add(row)
        await self._session.flush()
        return _row_to_dataclass(row)

    async def get(self, proposal_id: str) -> CharterProposal | None:
        result = await self._session.execute(
            select(CharterProposalRow).where(
                CharterProposalRow.tenant_id == self._tenant_id,
                CharterProposalRow.id == uuid.UUID(proposal_id),
            )
        )
        row = result.scalar_one_or_none()
        return _row_to_dataclass(row) if row else None

    async def list_pending(self, *, agent_id: str | None = None) -> list[CharterProposal]:
        stmt = select(CharterProposalRow).where(
            CharterProposalRow.tenant_id == self._tenant_id,
            CharterProposalRow.status == ProposalStatus.PENDING.value,
        )
        if agent_id is not None:
            stmt = stmt.where(CharterProposalRow.agent_id == agent_id)
        stmt = stmt.order_by(CharterProposalRow.created_at)
        result = await self._session.execute(stmt)
        return [_row_to_dataclass(row) for row in result.scalars().all()]

    async def approve(self, proposal_id: str, *, by: str, decision_reason: str | None = None) -> CharterProposal:
        return await self._decide(proposal_id, ProposalStatus.APPROVED, by=by, decision_reason=decision_reason)

    async def reject(self, proposal_id: str, *, by: str, decision_reason: str | None = None) -> CharterProposal:
        return await self._decide(proposal_id, ProposalStatus.REJECTED, by=by, decision_reason=decision_reason)

    async def _decide(
        self,
        proposal_id: str,
        target_status: ProposalStatus,
        *,
        by: str,
        decision_reason: str | None,
    ) -> CharterProposal:
        result = await self._session.execute(
            select(CharterProposalRow).where(
                CharterProposalRow.tenant_id == self._tenant_id,
                CharterProposalRow.id == uuid.UUID(proposal_id),
            )
        )
        row = result.scalar_one_or_none()
        if row is None:
            raise KeyError(f"Unknown charter proposal: {proposal_id}")
        if row.status != ProposalStatus.PENDING.value:
            raise ProposalAlreadyDecided(f"Proposal {proposal_id} already {row.status}")
        decided_at = self._now()
        await self._session.execute(
            update(CharterProposalRow)
            .where(CharterProposalRow.id == row.id)
            .values(
                status=target_status.value,
                decided_at=decided_at,
                decided_by=by,
                decision_reason=decision_reason,
            )
        )
        await self._session.flush()
        row.status = target_status.value
        row.decided_at = decided_at
        row.decided_by = by
        row.decision_reason = decision_reason
        return _row_to_dataclass(row)

    async def expire_stale(self, *, max_age_days: int = 7, now: datetime | None = None) -> list[str]:
        boundary = (now or self._now()).astimezone(timezone.utc) - timedelta(days=max_age_days)
        result = await self._session.execute(
            select(CharterProposalRow).where(
                CharterProposalRow.tenant_id == self._tenant_id,
                CharterProposalRow.status == ProposalStatus.PENDING.value,
                CharterProposalRow.created_at <= boundary,
            )
        )
        rows = result.scalars().all()
        expired_ids: list[str] = []
        for row in rows:
            await self._session.execute(
                update(CharterProposalRow)
                .where(CharterProposalRow.id == row.id)
                .values(status=ProposalStatus.EXPIRED.value)
            )
            expired_ids.append(str(row.id))
        if expired_ids:
            await self._session.flush()
        return expired_ids

    async def apply_approved_to_agent_files(
        self,
        proposal_id: str,
        *,
        agent_dir: Path,
        by: str,
        now: datetime | None = None,
    ) -> dict[str, str]:
        proposal = await self.get(proposal_id)
        if proposal is None:
            raise KeyError(f"Unknown charter proposal: {proposal_id}")
        return apply_approved_proposal_to_soul(agent_dir, proposal, applied_by=by, now=now or self._now())


def apply_approved_proposal_to_soul(
    agent_dir: Path,
    proposal: CharterProposal,
    *,
    applied_by: str,
    now: datetime | None = None,
) -> dict[str, str]:
    """Stage an approved owner-charter calibration for Soul Writer review.

    Approved proposals are semantic identity changes. They must not be
    mechanically appended into `soul.md`; this function writes a governed Soul
    Candidate signal that Dream/Soul Writer can turn into `soul.md.next`, then
    Soul Memory Gate and Platform Soul Gate perform the final exact commit.
    """
    if proposal.status != ProposalStatus.APPROVED.value:
        raise ValueError("Only approved charter proposals can be applied.")

    target_heading = _target_owner_charter_heading(proposal.proposal_kind)
    candidate_path = _stage_charter_proposal_soul_candidate(
        Path(agent_dir),
        proposal,
        staged_by=applied_by,
        target_section=target_heading.strip("*"),
        staged_at=now or datetime.now(timezone.utc),
    )

    applied_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    _append_charter_apply_audit(
        Path(agent_dir),
        proposal,
        applied_by=applied_by,
        applied_at=applied_at,
        target_section=target_heading.strip("*"),
        changed=True,
    )
    return {
        "status": "candidate_staged",
        "target_section": target_heading.strip("*"),
        "proposal_id": proposal.id,
        "candidate_path": candidate_path,
    }


def _safe_candidate_id(raw: str) -> str:
    normalized = "".join(ch.lower() if ch.isalnum() else "-" for ch in raw.strip())
    return "-".join(part for part in normalized.split("-") if part) or str(uuid.uuid4())


def _stage_charter_proposal_soul_candidate(
    agent_dir: Path,
    proposal: CharterProposal,
    *,
    staged_by: str,
    target_section: str,
    staged_at: datetime,
) -> str:
    candidate_id = f"charter-proposal-{_safe_candidate_id(proposal.id)}"
    package_dir = agent_dir / "memory" / ".staging" / "soul_candidates" / candidate_id
    package_dir.mkdir(parents=True, exist_ok=True)
    signal_path = package_dir / "charter_proposal_signal.md"
    signal = (
        "# Charter Proposal Soul Candidate Signal\n\n"
        f"- proposal_id: {proposal.id}\n"
        f"- decision_id: {proposal.decision_id}\n"
        f"- proposal_kind: {proposal.proposal_kind}\n"
        f"- target_path: soul.md\n"
        f"- target_section: {target_section}\n"
        f"- status: pending_soul_writer\n"
        f"- staged_by: {staged_by}\n"
        f"- staged_at: {staged_at.astimezone(timezone.utc).isoformat()}\n\n"
        "## Owner-Approved Action\n\n"
        f"{proposal.action}\n\n"
        "## Reason\n\n"
        f"{proposal.reason}\n\n"
        "## Writer Instruction\n\n"
        "Dream / Soul Writer must inspect the current soul.md, preserve frozen identity/charter "
        "sections, and produce a complete soul.md.next candidate. Platform code must not append "
        "this action directly into soul.md.\n"
    )
    signal_path.write_text(signal, encoding="utf-8")
    manifest = {
        "schema": "soul_charter_candidate_signal.v1",
        "candidate_id": candidate_id,
        "proposal_id": proposal.id,
        "decision_id": proposal.decision_id,
        "proposal_kind": proposal.proposal_kind,
        "target_path": "soul.md",
        "target_section": target_section,
        "status": "pending_soul_writer",
        "signal_path": f"memory/.staging/soul_candidates/{candidate_id}/charter_proposal_signal.md",
        "source_refs": [proposal.decision_id, f"charter_proposal:{proposal.id}"],
        "staged_by": staged_by,
        "staged_at": staged_at.astimezone(timezone.utc).isoformat(),
    }
    (package_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest["signal_path"]


def _row_to_dataclass(row: CharterProposalRow) -> CharterProposal:
    return CharterProposal(
        id=str(row.id),
        agent_id=row.agent_id,
        decision_id=row.decision_id,
        action=row.action,
        proposal_kind=row.proposal_kind,
        reason=row.reason,
        status=row.status,
        created_at=row.created_at,
        decided_at=row.decided_at,
        decided_by=row.decided_by,
        decision_reason=row.decision_reason,
    )


def _target_owner_charter_heading(proposal_kind: str) -> str:
    kind = ProposalKind(proposal_kind)
    if kind == ProposalKind.CONSIDER_FULL_AUTHORITY:
        return "**Full Authority**"
    if kind == ProposalKind.TIGHTEN_TO_CONFIRM_FIRST:
        return "**Confirm First**"
    raise ValueError(f"Unsupported proposal kind: {proposal_kind}")


def _insert_bullet_once(
    text: str,
    *,
    section_heading: str,
    bullet: str,
    unique_text: str,
) -> tuple[str, bool]:
    owner_section = "## Frozen Owner Agency Charter"
    owner_start = text.find(owner_section)
    if owner_start < 0:
        raise ValueError("soul.md is missing Frozen Owner Agency Charter.")
    heading_start = text.find(section_heading, owner_start)
    if heading_start < 0:
        raise ValueError(f"soul.md is missing {section_heading}.")

    next_subheading = text.find("\n**", heading_start + len(section_heading))
    next_section = text.find("\n## ", heading_start + len(section_heading))
    candidates = [idx for idx in (next_subheading, next_section) if idx >= 0]
    insert_at = min(candidates) if candidates else len(text)
    section_text = text[heading_start:insert_at]
    if unique_text in section_text:
        return text, False

    prefix = text[:insert_at].rstrip()
    suffix = text[insert_at:].lstrip("\n")
    return f"{prefix}\n{bullet}\n\n{suffix}", True


def _append_charter_apply_audit(
    agent_dir: Path,
    proposal: CharterProposal,
    *,
    applied_by: str,
    applied_at: datetime,
    target_section: str,
    changed: bool,
) -> None:
    path = agent_dir / "memory" / "charter_calibration.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = (
        path.read_text(encoding="utf-8", errors="replace") if path.exists() else "# Charter Calibration Audit\n\n"
    )
    line = (
        f"- [{applied_at.date().isoformat()}][proposal_id={proposal.id}]"
        f"[decision_id={proposal.decision_id}][kind={proposal.proposal_kind}]"
        f"[target={target_section}][staged_by={applied_by}]"
        "[status=candidate_staged] "
        f"{proposal.action}"
    )
    if f"[proposal_id={proposal.id}]" in existing:
        return
    path.write_text(existing.rstrip() + "\n" + line + "\n", encoding="utf-8")
