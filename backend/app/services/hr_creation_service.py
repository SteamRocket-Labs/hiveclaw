"""State transitions and persistence for governed HR creation drafts."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.hr_creation import HrCreationDraft


class HrCreationConflict(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> datetime:
    return datetime.now(UTC)


def confirm_hr_creation_draft_record(
    draft: HrCreationDraft,
    *,
    confirming_user_id: uuid.UUID,
    blueprint_version: int,
    blueprint_hash: str,
    now: datetime | None = None,
) -> None:
    if draft.requested_by_user_id != confirming_user_id:
        raise HrCreationConflict("requester_mismatch", "Only the requesting user can confirm this blueprint.")
    if draft.status != "awaiting_confirmation":
        raise HrCreationConflict("invalid_status", f"Blueprint cannot be confirmed from status {draft.status}.")
    if draft.blueprint_version != blueprint_version:
        raise HrCreationConflict("version_mismatch", "Blueprint version changed; review the latest preview.")
    if draft.blueprint_hash != blueprint_hash:
        raise HrCreationConflict("hash_mismatch", "Blueprint hash changed; review the latest preview.")
    missing_gates = [
        str(gate)
        for gate in (draft.preview_json or {}).get("missing_gates", [])
        if str(gate).strip() and str(gate) != "confirmation"
    ]
    if missing_gates:
        raise HrCreationConflict(
            "missing_gates",
            "Blueprint has unresolved creation gates: " + ", ".join(missing_gates),
        )
    draft.status = "confirmed"
    draft.confirmed_by_user_id = confirming_user_id
    draft.confirmed_at = now or _now()
    draft.rejected_by_user_id = None
    draft.rejected_at = None


def reject_hr_creation_draft_record(
    draft: HrCreationDraft,
    *,
    rejecting_user_id: uuid.UUID,
    now: datetime | None = None,
) -> None:
    if draft.requested_by_user_id != rejecting_user_id:
        raise HrCreationConflict("requester_mismatch", "Only the requesting user can reject this blueprint.")
    if draft.status not in {"awaiting_confirmation", "confirmed"}:
        raise HrCreationConflict("invalid_status", f"Blueprint cannot be rejected from status {draft.status}.")
    draft.status = "rejected"
    draft.rejected_by_user_id = rejecting_user_id
    draft.rejected_at = now or _now()


def claim_hr_creation_draft_record(
    draft: HrCreationDraft,
    *,
    now: datetime | None = None,
    lease_seconds: int = 300,
) -> str:
    if draft.id is None:
        raise HrCreationConflict("invalid_draft", "Blueprint must be persisted before creation.")
    idempotency_key = f"hr-draft:{draft.id}"
    current = now or _now()
    if draft.status == "completed" and draft.created_agent_id:
        return "completed"
    if draft.creation_idempotency_key and draft.creation_idempotency_key != idempotency_key:
        raise HrCreationConflict("idempotency_conflict", "Blueprint has an invalid creation identity binding.")
    if draft.status in {"creating", "provisioning"} and draft.claim_expires_at and draft.claim_expires_at > current:
        raise HrCreationConflict("creation_in_progress", "Blueprint creation is already in progress.")
    if draft.status not in {"confirmed", "failed", "creating", "provisioning"}:
        raise HrCreationConflict("not_confirmed", "Blueprint must be confirmed by the requesting user before creation.")
    draft.status = "creating"
    draft.creation_idempotency_key = idempotency_key
    draft.claim_expires_at = current + timedelta(seconds=max(30, lease_seconds))
    draft.attempt_count = int(draft.attempt_count or 0) + 1
    draft.failure_code = None
    draft.failure_message = None
    return "claimed"


async def upsert_hr_creation_draft(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    hr_agent_id: uuid.UUID,
    session_id: uuid.UUID,
    requested_by_user_id: uuid.UUID,
    preview_payload: dict[str, Any],
    blueprint_id: uuid.UUID | None = None,
) -> HrCreationDraft:
    draft: HrCreationDraft | None = None
    if blueprint_id is not None:
        result = await db.execute(
            select(HrCreationDraft)
            .where(
                HrCreationDraft.id == blueprint_id,
                HrCreationDraft.tenant_id == tenant_id,
                HrCreationDraft.hr_agent_id == hr_agent_id,
                HrCreationDraft.session_id == session_id,
                HrCreationDraft.requested_by_user_id == requested_by_user_id,
            )
            .with_for_update()
        )
        draft = result.scalar_one_or_none()
        if draft is None:
            raise HrCreationConflict("not_found", "HR creation draft was not found in this session.")
        if draft.status in {"creating", "provisioning", "completed"}:
            raise HrCreationConflict("immutable", "A started or completed blueprint cannot be revised.")
        draft.blueprint_version += 1
    else:
        draft = HrCreationDraft(
            tenant_id=tenant_id,
            hr_agent_id=hr_agent_id,
            session_id=session_id,
            requested_by_user_id=requested_by_user_id,
            blueprint_version=1,
        )
        db.add(draft)

    draft.status = "awaiting_confirmation"
    draft.blueprint_hash = str(preview_payload["blueprint_hash"])
    draft.blueprint_json = dict(preview_payload["blueprint"])
    draft.preview_json = dict(preview_payload)
    draft.confirmed_by_user_id = None
    draft.confirmed_at = None
    draft.rejected_by_user_id = None
    draft.rejected_at = None
    draft.creation_idempotency_key = None
    draft.claim_expires_at = None
    draft.failure_code = None
    draft.failure_message = None
    await db.flush()
    return draft


async def load_hr_creation_draft(
    db: AsyncSession,
    *,
    draft_id: uuid.UUID,
    tenant_id: uuid.UUID,
    hr_agent_id: uuid.UUID,
    requested_by_user_id: uuid.UUID,
    session_id: uuid.UUID | None = None,
    for_update: bool = False,
) -> HrCreationDraft:
    statement = select(HrCreationDraft).where(
        HrCreationDraft.id == draft_id,
        HrCreationDraft.tenant_id == tenant_id,
        HrCreationDraft.hr_agent_id == hr_agent_id,
        HrCreationDraft.requested_by_user_id == requested_by_user_id,
    )
    if session_id is not None:
        statement = statement.where(HrCreationDraft.session_id == session_id)
    if for_update:
        statement = statement.with_for_update()
    result = await db.execute(statement)
    draft = result.scalar_one_or_none()
    if draft is None:
        raise HrCreationConflict("not_found", "HR creation draft was not found for this user and session.")
    return draft


def hr_creation_draft_payload(draft: HrCreationDraft) -> dict[str, Any]:
    return {
        **dict(draft.preview_json or {}),
        "blueprint_id": str(draft.id),
        "blueprint_version": draft.blueprint_version,
        "blueprint_hash": draft.blueprint_hash,
        "draft_status": draft.status,
        "confirmed_by_user_id": str(draft.confirmed_by_user_id) if draft.confirmed_by_user_id else None,
        "confirmed_at": draft.confirmed_at.isoformat() if draft.confirmed_at else None,
        "created_agent_id": str(draft.created_agent_id) if draft.created_agent_id else None,
        "provisioning": dict(draft.provisioning_json or {}),
        "failure": (
            {"code": draft.failure_code, "message": draft.failure_message}
            if draft.failure_code or draft.failure_message
            else None
        ),
    }


def mark_hr_creation_failed_record(draft: HrCreationDraft, *, code: str, message: str) -> None:
    draft.status = "failed"
    draft.failure_code = code[:100]
    draft.failure_message = message[:4000]
    draft.claim_expires_at = None


def mark_hr_creation_completed_record(
    draft: HrCreationDraft,
    *,
    agent_id: uuid.UUID,
    provisioning: dict[str, Any],
) -> None:
    draft.status = "completed"
    draft.created_agent_id = agent_id
    draft.claim_expires_at = None
    draft.provisioning_json = dict(provisioning)
    draft.failure_code = None
    draft.failure_message = None
