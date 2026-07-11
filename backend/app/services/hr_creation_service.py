"""State transitions and persistence for governed HR creation drafts."""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.hr_creation import HrCreationDraft, HrProvisioningStep


class HrCreationConflict(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class HrCreationClaim:
    state: str
    token: uuid.UUID | None
    version: int
    expires_at: datetime | None


@dataclass(frozen=True, slots=True)
class HrProvisioningReadiness:
    ready: bool
    creation_state: str
    blocking_step_keys: tuple[str, ...]
    warning_step_keys: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class HrProvisioningStepSpec:
    step_key: str
    step_kind: str
    order_index: int
    required: bool
    input_hash: str
    source_key: str | None = None


def validate_hr_creation_blueprint(blueprint: dict[str, Any]) -> None:
    """Validate immutable creation input before acquiring a worker lease."""
    if not isinstance(blueprint, dict):
        raise HrCreationConflict("invalid_blueprint", "Canonical blueprint must be an object.")
    name = str(blueprint.get("name") or "").strip()
    if len(name) < 2 or len(name) > 100:
        raise HrCreationConflict("invalid_blueprint", "Canonical blueprint name must contain 2 to 100 characters.")


def _assert_hr_creation_claim(
    draft: HrCreationDraft,
    claim: HrCreationClaim,
    *,
    now: datetime | None = None,
) -> datetime:
    current = now or _now()
    if (
        claim.state != "claimed"
        or claim.token is None
        or draft.claim_token != claim.token
        or int(draft.claim_version or 0) != claim.version
    ):
        raise HrCreationConflict("stale_claim", "HR creation worker has a stale claim and may not mutate state.")
    if draft.claim_expires_at is None or draft.claim_expires_at <= current:
        raise HrCreationConflict("claim_expired", "HR creation claim expired before the worker recorded evidence.")
    return current


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
) -> HrCreationClaim:
    if draft.id is None:
        raise HrCreationConflict("invalid_draft", "Blueprint must be persisted before creation.")
    idempotency_key = f"hr-draft:{draft.id}"
    current = now or _now()
    if draft.status == "completed" and draft.created_agent_id:
        return HrCreationClaim(
            state="completed",
            token=None,
            version=int(draft.claim_version or 0),
            expires_at=None,
        )
    if draft.creation_idempotency_key and draft.creation_idempotency_key != idempotency_key:
        raise HrCreationConflict("idempotency_conflict", "Blueprint has an invalid creation identity binding.")
    if draft.status in {"creating", "provisioning"} and draft.claim_expires_at and draft.claim_expires_at > current:
        raise HrCreationConflict("creation_in_progress", "Blueprint creation is already in progress.")
    if draft.status not in {"confirmed", "failed", "creating", "provisioning"}:
        raise HrCreationConflict("not_confirmed", "Blueprint must be confirmed by the requesting user before creation.")
    draft.status = "creating"
    draft.creation_idempotency_key = idempotency_key
    draft.claim_token = uuid.uuid4()
    draft.claim_version = int(draft.claim_version or 0) + 1
    draft.claim_heartbeat_at = current
    draft.claim_expires_at = current + timedelta(seconds=max(30, lease_seconds))
    draft.attempt_count = int(draft.attempt_count or 0) + 1
    draft.failure_code = None
    draft.failure_message = None
    return HrCreationClaim(
        state="claimed",
        token=draft.claim_token,
        version=draft.claim_version,
        expires_at=draft.claim_expires_at,
    )


def renew_hr_creation_claim_record(
    draft: HrCreationDraft,
    *,
    claim: HrCreationClaim,
    now: datetime | None = None,
    lease_seconds: int = 300,
) -> datetime:
    current = _assert_hr_creation_claim(draft, claim, now=now)
    draft.claim_heartbeat_at = current
    draft.claim_expires_at = current + timedelta(seconds=max(30, lease_seconds))
    return draft.claim_expires_at


def release_hr_creation_claim_record(
    draft: HrCreationDraft,
    *,
    claim: HrCreationClaim,
    status: str = "provisioning",
    now: datetime | None = None,
) -> None:
    _assert_hr_creation_claim(draft, claim, now=now)
    draft.status = status
    draft.claim_token = None
    draft.claim_heartbeat_at = None
    draft.claim_expires_at = None


def _step_input_hash(*parts: object) -> str:
    payload = "\x1f".join(str(part) for part in parts)
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _capability_step_key(kind: str, normalized_key: str) -> str:
    # This is a compact identity key, not a security digest. PostgreSQL's md5()
    # can reproduce it during legacy backfill without requiring pgcrypto.
    digest = hashlib.md5(normalized_key.encode("utf-8"), usedforsecurity=False).hexdigest()
    return f"capability:{kind}:{digest}"


def build_hr_provisioning_step_specs(
    *,
    blueprint_hash: str,
    install_plan: list[dict[str, Any]],
) -> list[HrProvisioningStepSpec]:
    specs = [
        HrProvisioningStepSpec("validate", "validate", 10, True, _step_input_hash(blueprint_hash, "validate")),
        HrProvisioningStepSpec("model", "model", 20, True, _step_input_hash(blueprint_hash, "model")),
        HrProvisioningStepSpec("core", "core", 30, True, _step_input_hash(blueprint_hash, "core")),
        HrProvisioningStepSpec("workspace", "workspace", 40, True, _step_input_hash(blueprint_hash, "workspace")),
        HrProvisioningStepSpec("defaults", "defaults", 50, True, _step_input_hash(blueprint_hash, "defaults")),
        HrProvisioningStepSpec("t0_evidence", "t0_evidence", 60, True, _step_input_hash(blueprint_hash, "t0")),
    ]
    for index, item in enumerate(install_plan):
        kind = str(item["kind"])
        source_key = str(item["source_key"])
        normalized_key = str(item.get("normalized_key") or source_key).strip().lower()
        specs.append(
            HrProvisioningStepSpec(
                step_key=_capability_step_key(kind, normalized_key),
                step_kind=kind,
                order_index=100 + index,
                required=bool(item.get("required", True)),
                input_hash=_step_input_hash(blueprint_hash, kind, normalized_key),
                source_key=source_key,
            )
        )
    specs.append(
        HrProvisioningStepSpec("finalize", "finalize", 10_000, True, _step_input_hash(blueprint_hash, "finalize"))
    )
    return specs


async def ensure_hr_provisioning_steps(
    db: AsyncSession,
    *,
    draft: HrCreationDraft,
    claim: HrCreationClaim,
    install_plan: list[dict[str, Any]],
) -> list[HrProvisioningStep]:
    _assert_hr_creation_claim(draft, claim)
    result = await db.execute(
        select(HrProvisioningStep)
        .where(HrProvisioningStep.draft_id == draft.id, HrProvisioningStep.tenant_id == draft.tenant_id)
        .order_by(HrProvisioningStep.order_index.asc())
        .with_for_update()
    )
    existing = {step.step_key: step for step in result.scalars().all()}
    for spec in build_hr_provisioning_step_specs(
        blueprint_hash=draft.blueprint_hash,
        install_plan=install_plan,
    ):
        step = existing.get(spec.step_key)
        if step is None:
            step = HrProvisioningStep(
                tenant_id=draft.tenant_id,
                draft_id=draft.id,
                step_key=spec.step_key,
                step_kind=spec.step_kind,
                order_index=spec.order_index,
                required=spec.required,
                input_hash=spec.input_hash,
                source_key=spec.source_key,
            )
            db.add(step)
            existing[spec.step_key] = step
        elif step.input_hash != spec.input_hash:
            raise HrCreationConflict(
                "provisioning_input_drift",
                f"Provisioning step {spec.step_key} no longer matches the confirmed blueprint.",
            )
    await db.flush()
    return sorted(existing.values(), key=lambda step: step.order_index)


def transition_hr_provisioning_step_record(
    step: HrProvisioningStep,
    *,
    draft: HrCreationDraft,
    claim: HrCreationClaim,
    status: str,
    receipt: dict[str, Any] | None = None,
    error_code: str | None = None,
    error_message: str | None = None,
    now: datetime | None = None,
) -> None:
    current = _assert_hr_creation_claim(draft, claim, now=now)
    if status not in {"running", "completed", "failed", "skipped", "waiting_review"}:
        raise HrCreationConflict("invalid_step_status", f"Unsupported provisioning step status: {status}.")
    if step.status == "completed" and status != "completed":
        return
    if status == "running":
        step.attempt_count = int(step.attempt_count or 0) + 1
        step.started_at = current
        step.completed_at = None
    elif status in {"completed", "skipped", "waiting_review"}:
        step.completed_at = current
    else:
        step.completed_at = None
    step.status = status
    step.claim_version = claim.version
    step.heartbeat_at = current
    step.receipt_json = dict(receipt or step.receipt_json or {})
    step.error_code = error_code[:100] if error_code else None
    step.error_message = error_message[:4000] if error_message else None


def derive_hr_provisioning_readiness(
    steps: list[HrProvisioningStep],
    *,
    ignore_finalize: bool = False,
) -> HrProvisioningReadiness:
    considered = [step for step in steps if not (ignore_finalize and step.step_kind == "finalize")]
    blockers = tuple(step.step_key for step in considered if step.required and step.status != "completed")
    warnings = tuple(
        step.step_key
        for step in considered
        if not step.required and step.status in {"failed", "skipped", "waiting_review"}
    )
    failed_required = any(step.required and step.status == "failed" for step in considered)
    if blockers:
        state = "provisioning_failed" if failed_required else "provisioning"
        return HrProvisioningReadiness(False, state, blockers, warnings)
    return HrProvisioningReadiness(True, "ready_with_warnings" if warnings else "ready", (), warnings)


def hr_provisioning_step_payload(step: HrProvisioningStep) -> dict[str, Any]:
    if step.status == "failed":
        if step.required and step.step_kind in {
            "platform_skill",
            "mcp_server",
            "clawhub_skill",
            "external_skill_url",
        }:
            public_error = "This required capability is not ready. Resolve it, then resume provisioning."
        elif step.required:
            public_error = "This required provisioning step failed. Resolve it, then resume provisioning."
        else:
            public_error = "This optional capability is not ready. The employee can continue with a warning."
    else:
        public_error = None
    return {
        "step_key": step.step_key,
        "step_kind": step.step_kind,
        "required": bool(step.required),
        "status": step.status,
        "attempt_count": int(step.attempt_count or 0),
        "source_key": step.source_key,
        "evidence_available": bool(step.receipt_json),
        "error_code": (
            "required_step_failed"
            if step.status == "failed" and step.required
            else "optional_step_failed"
            if step.status == "failed"
            else None
        ),
        "error_message": public_error,
        "started_at": step.started_at.isoformat() if step.started_at else None,
        "completed_at": step.completed_at.isoformat() if step.completed_at else None,
    }


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
    draft.claim_token = None
    draft.claim_version = 0
    draft.claim_heartbeat_at = None
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
    steps = list(getattr(draft, "provisioning_steps", ()) or ())
    readiness = derive_hr_provisioning_readiness(steps) if steps else None
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
        "provisioning_steps": [hr_provisioning_step_payload(step) for step in steps],
        "creation_state": readiness.creation_state if readiness else draft.status,
        "failure": (
            {"code": draft.failure_code, "message": draft.failure_message}
            if draft.failure_code or draft.failure_message
            else None
        ),
    }


def mark_hr_creation_failed_record(
    draft: HrCreationDraft,
    *,
    code: str,
    message: str,
    claim: HrCreationClaim | None = None,
) -> None:
    if claim is not None:
        _assert_hr_creation_claim(draft, claim)
    draft.status = "failed"
    draft.failure_code = code[:100]
    draft.failure_message = message[:4000]
    draft.claim_expires_at = None
    draft.claim_token = None
    draft.claim_heartbeat_at = None


def mark_hr_creation_completed_record(
    draft: HrCreationDraft,
    *,
    claim: HrCreationClaim,
    steps: list[HrProvisioningStep],
    agent_id: uuid.UUID,
    provisioning: dict[str, Any],
) -> None:
    _assert_hr_creation_claim(draft, claim)
    readiness = derive_hr_provisioning_readiness(steps, ignore_finalize=True)
    if not readiness.ready:
        raise HrCreationConflict(
            "required_steps_incomplete",
            "Cannot complete HR creation while required provisioning steps are incomplete: "
            + ", ".join(readiness.blocking_step_keys),
        )
    finalize = next((step for step in steps if step.step_kind == "finalize"), None)
    if finalize is None:
        raise HrCreationConflict("missing_finalize_step", "HR provisioning journal has no finalize step.")
    transition_hr_provisioning_step_record(
        finalize,
        draft=draft,
        claim=claim,
        status="completed",
        receipt={"agent_id": str(agent_id), "required_steps_verified": True},
    )
    draft.status = "completed"
    draft.created_agent_id = agent_id
    draft.claim_expires_at = None
    draft.claim_token = None
    draft.claim_heartbeat_at = None
    draft.provisioning_json = dict(provisioning)
    draft.failure_code = None
    draft.failure_message = None
