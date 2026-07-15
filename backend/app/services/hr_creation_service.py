"""State transitions and persistence for governed HR creation drafts."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.hr_creation import HrCreationDraft, HrProvisioningStep


HR_CREATION_PREVIEW_TTL = timedelta(days=7)
RECOVERABLE_HR_DRAFT_STATUSES = frozenset({"awaiting_confirmation", "confirmed", "creating", "provisioning", "failed"})


class HrCreationConflict(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _now() -> datetime:
    return datetime.now(UTC)


def canonical_hr_blueprint_json(blueprint: dict[str, Any]) -> str:
    return json.dumps(blueprint, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def canonical_hr_blueprint_payload_hash(blueprint: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_hr_blueprint_json(blueprint).encode("utf-8")).hexdigest()


def canonical_hr_blueprint_hash(blueprint: dict[str, Any]) -> str:
    return f"bp_{canonical_hr_blueprint_payload_hash(blueprint)[:24]}"


_SAFE_FAILED_REVISION_TASK_STATUSES = frozenset({"failed", "killed", "skipped"})


def _failed_revision_blockers(draft: HrCreationDraft, task: Any | None) -> list[str]:
    """Return mechanical evidence that makes a successor unsafe.

    This deliberately does not infer whether a completed step was harmless.
    A successor is allowed only when the durable facts prove execution never
    started; every unknown or partially journaled state stays recoverable via
    operator reconciliation.
    """

    blockers: list[str] = []
    if task is None:
        blockers.append("runtime_task_missing")
        return blockers
    if task.status not in _SAFE_FAILED_REVISION_TASK_STATUSES:
        blockers.append("runtime_task_not_safely_terminal")
    if task.claimed_by is not None or task.claim_expires_at is not None:
        blockers.append("runtime_task_claim_present")
    if str((task.metadata_json or {}).get("side_effect_risk") or "") != "not_started":
        blockers.append("runtime_task_side_effect_risk")
    if draft.created_agent_id is not None:
        blockers.append("created_asset_present")
    for step in draft.provisioning_steps:
        if (
            step.status != "pending"
            or int(step.attempt_count or 0) > 0
            or bool(step.receipt_json)
            or step.started_at is not None
            or step.completed_at is not None
        ):
            blockers.append("provisioning_step_execution_evidence")
            break
    return blockers


async def _resolve_exact_hr_revision_retry(
    db: AsyncSession,
    *,
    superseded_draft: HrCreationDraft,
    preview_payload: dict[str, Any],
) -> HrCreationDraft:
    raw_successor_id = str((superseded_draft.provisioning_json or {}).get("superseded_by_draft_id") or "")
    try:
        successor_id = uuid.UUID(raw_successor_id)
    except (TypeError, ValueError) as exc:
        raise HrCreationConflict(
            "reconciliation_required",
            "Superseded HR blueprint has no valid successor evidence.",
        ) from exc
    successor = (
        await db.execute(
            select(HrCreationDraft)
            .options(selectinload(HrCreationDraft.provisioning_steps))
            .where(
                HrCreationDraft.id == successor_id,
                HrCreationDraft.tenant_id == superseded_draft.tenant_id,
                HrCreationDraft.hr_agent_id == superseded_draft.hr_agent_id,
                HrCreationDraft.session_id == superseded_draft.session_id,
                HrCreationDraft.requested_by_user_id == superseded_draft.requested_by_user_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
    ).scalar_one_or_none()
    if successor is None:
        raise HrCreationConflict(
            "reconciliation_required",
            "Superseded HR blueprint successor is missing.",
        )
    if (
        successor.blueprint_version != superseded_draft.blueprint_version + 1
        or successor.blueprint_hash != str(preview_payload["blueprint_hash"])
        or dict(successor.blueprint_json or {}) != dict(preview_payload["blueprint"])
        or str((successor.preview_json or {}).get("supersedes_blueprint_id") or "") != str(superseded_draft.id)
    ):
        raise HrCreationConflict(
            "superseded",
            "This immutable blueprint was already superseded by a different revision.",
        )
    return successor


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
    blueprint_hash: str | None = None,
    now: datetime | None = None,
) -> None:
    current = now or _now()
    if draft.requested_by_user_id != confirming_user_id:
        raise HrCreationConflict("requester_mismatch", "Only the requesting user can confirm this blueprint.")
    if draft.blueprint_version != blueprint_version:
        raise HrCreationConflict("version_mismatch", "Blueprint version changed; review the latest preview.")
    # Compatibility only: old clients may still echo the canonical hash, but
    # it is not an authority claim. The server-owned row/version binds this
    # authenticated confirmation; provisioning continues to use
    # ``draft.blueprint_hash`` internally for immutable step hashes.
    del blueprint_hash
    if draft.status == "awaiting_confirmation" and draft.expires_at is not None and draft.expires_at <= current:
        raise HrCreationConflict("expired", "This HR blueprint preview expired. Ask HR to generate a fresh preview.")
    if draft.status != "awaiting_confirmation":
        if (
            draft.status in {"confirmed", "creating", "provisioning", "completed", "failed"}
            and draft.confirmed_by_user_id == confirming_user_id
            and draft.confirmed_at is not None
        ):
            # Exact network retries are idempotent. The API-level row lock and
            # provisioning_task_id decide whether the durable job already exists.
            return
        raise HrCreationConflict("invalid_status", f"Blueprint cannot be confirmed from status {draft.status}.")
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
    draft.confirmed_at = current
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
    if draft.status == "confirmed" and draft.provisioning_task_id is not None:
        raise HrCreationConflict(
            "use_cancel",
            "Confirmed provisioning has a durable job; cancel it through the provisioning cancellation endpoint.",
        )
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
    step.error_message = error_message if error_message else None


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
    superseded_draft: HrCreationDraft | None = None
    if blueprint_id is not None:
        scope = (
            HrCreationDraft.id == blueprint_id,
            HrCreationDraft.tenant_id == tenant_id,
            HrCreationDraft.hr_agent_id == hr_agent_id,
            HrCreationDraft.session_id == session_id,
            HrCreationDraft.requested_by_user_id == requested_by_user_id,
        )
        candidate = (await db.execute(select(HrCreationDraft).where(*scope))).scalar_one_or_none()
        if candidate is None:
            raise HrCreationConflict("not_found", "HR creation draft was not found in this session.")

        # Runtime workers lock RuntimeTask before HrCreationDraft. Preserve the
        # same order when a failed immutable revision supersedes its old job so
        # user editing cannot deadlock or race the durable worker.
        linked_task_id = candidate.provisioning_task_id if candidate.status == "failed" else None
        linked_task = None
        if linked_task_id is not None:
            from app.models.runtime_task import RuntimeTask

            linked_task = await db.get(RuntimeTask, linked_task_id, with_for_update=True)
        result = await db.execute(
            select(HrCreationDraft)
            .options(selectinload(HrCreationDraft.provisioning_steps))
            .where(*scope)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        draft = result.scalar_one_or_none()
        if draft is None:
            raise HrCreationConflict("not_found", "HR creation draft was not found in this session.")
        if draft.status == "superseded":
            return await _resolve_exact_hr_revision_retry(
                db,
                superseded_draft=draft,
                preview_payload=preview_payload,
            )
        if draft.status == "failed" and draft.provisioning_task_id != linked_task_id:
            raise HrCreationConflict(
                "retry_required",
                "Provisioning state changed while preparing the revision; reload the latest blueprint and retry.",
            )
        if draft.status not in {"awaiting_confirmation", "failed"}:
            raise HrCreationConflict(
                "immutable",
                f"A {draft.status} blueprint cannot be revised in place.",
            )
        if draft.status == "failed":
            revision_blockers = _failed_revision_blockers(draft, linked_task)
            if revision_blockers:
                raise HrCreationConflict(
                    "reconciliation_required",
                    "The failed blueprint has execution evidence or unknown side effects and requires operator "
                    "reconciliation before a successor can be created: " + ", ".join(revision_blockers),
                )

            current = _now()
            next_draft_id = uuid.uuid4()
            if linked_task is not None:
                task_metadata = dict(linked_task.metadata_json or {})
                if linked_task.status not in {"failed", "killed", "skipped", "needs_reconciliation"}:
                    linked_task.status = "killed"
                    linked_task.completed_at = current
                    linked_task.result_summary = "Superseded by an explicitly revised HR blueprint."
                linked_task.claim_version = int(linked_task.claim_version or 0) + 1
                linked_task.claimed_by = None
                linked_task.claim_expires_at = None
                task_metadata.update(
                    {
                        "phase": "terminal",
                        "automatic_retry_allowed": False,
                        "superseded_at": current.isoformat(),
                        "superseded_by_draft_id": str(next_draft_id),
                        "superseded_by_blueprint_version": int(draft.blueprint_version) + 1,
                    }
                )
                linked_task.metadata_json = task_metadata

            superseded_draft = draft
            superseded_draft.status = "superseded"
            superseded_draft.claim_token = None
            superseded_draft.claim_version = int(superseded_draft.claim_version or 0) + 1
            superseded_draft.claim_heartbeat_at = None
            superseded_draft.claim_expires_at = None
            superseded_draft.provisioning_json = {
                **dict(superseded_draft.provisioning_json or {}),
                "superseded_at": current.isoformat(),
                "superseded_by_draft_id": str(next_draft_id),
                "superseded_by_blueprint_version": int(superseded_draft.blueprint_version) + 1,
            }
            draft = HrCreationDraft(
                id=next_draft_id,
                tenant_id=tenant_id,
                hr_agent_id=hr_agent_id,
                session_id=session_id,
                requested_by_user_id=requested_by_user_id,
                blueprint_version=int(superseded_draft.blueprint_version) + 1,
                provisioning_steps=[],
            )
            db.add(draft)
        else:
            draft.blueprint_version += 1
    else:
        draft = HrCreationDraft(
            tenant_id=tenant_id,
            hr_agent_id=hr_agent_id,
            session_id=session_id,
            requested_by_user_id=requested_by_user_id,
            blueprint_version=1,
            provisioning_steps=[],
        )
        db.add(draft)

    draft.status = "awaiting_confirmation"
    draft.blueprint_hash = str(preview_payload["blueprint_hash"])
    draft.blueprint_json = dict(preview_payload["blueprint"])
    draft.preview_json = {
        **dict(preview_payload),
        **({"supersedes_blueprint_id": str(superseded_draft.id)} if superseded_draft is not None else {}),
    }
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
    draft.expires_at = _now() + HR_CREATION_PREVIEW_TTL
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
    statement = (
        select(HrCreationDraft)
        .options(selectinload(HrCreationDraft.provisioning_steps))
        .where(
            HrCreationDraft.id == draft_id,
            HrCreationDraft.tenant_id == tenant_id,
            HrCreationDraft.hr_agent_id == hr_agent_id,
            HrCreationDraft.requested_by_user_id == requested_by_user_id,
        )
    )
    if session_id is not None:
        statement = statement.where(HrCreationDraft.session_id == session_id)
    if for_update:
        statement = statement.with_for_update().execution_options(populate_existing=True)
    result = await db.execute(statement)
    draft = result.scalar_one_or_none()
    if draft is None:
        raise HrCreationConflict("not_found", "HR creation draft was not found for this user and session.")
    return draft


def hr_creation_recovery_payload(
    draft: HrCreationDraft,
    runtime_task: Any | None = None,
) -> dict[str, Any]:
    task_status = str(getattr(runtime_task, "status", None) or "") or None
    requires_operator = task_status == "needs_reconciliation" or draft.failure_code in {
        "cancellation_reconciliation_required",
        "runtime_task_needs_reconciliation",
        "runtime_task_draft_divergence",
    }
    can_retry = (
        not requires_operator
        and draft.status in {"failed", "provisioning"}
        and task_status in {"failed", "killed", "resumable"}
    )
    return {
        "task_status": task_status,
        "can_resume": draft.status in RECOVERABLE_HR_DRAFT_STATUSES and draft.session_id is not None,
        "can_retry": can_retry,
        "can_abandon": draft.status in RECOVERABLE_HR_DRAFT_STATUSES,
        "requires_operator": requires_operator,
        "reason": draft.failure_message or getattr(runtime_task, "result_summary", None),
    }


def hr_creation_draft_payload(
    draft: HrCreationDraft,
    *,
    runtime_task: Any | None = None,
) -> dict[str, Any]:
    steps = list(getattr(draft, "provisioning_steps", ()) or ())
    readiness = derive_hr_provisioning_readiness(steps) if steps else None
    preview = dict(draft.preview_json or {})
    preview.setdefault("blueprint", dict(draft.blueprint_json or {}))
    return {
        **preview,
        "blueprint_id": str(draft.id),
        "blueprint_version": draft.blueprint_version,
        "blueprint_hash": draft.blueprint_hash,
        "draft_status": draft.status,
        "hr_agent_id": str(draft.hr_agent_id),
        "session_id": str(draft.session_id),
        "requested_by_user_id": str(draft.requested_by_user_id),
        "expires_at": draft.expires_at.isoformat() if draft.expires_at else None,
        "created_at": (
            draft.__dict__["created_at"].isoformat() if draft.__dict__.get("created_at") is not None else None
        ),
        "updated_at": (
            draft.__dict__["updated_at"].isoformat() if draft.__dict__.get("updated_at") is not None else None
        ),
        "confirmed_by_user_id": str(draft.confirmed_by_user_id) if draft.confirmed_by_user_id else None,
        "confirmed_at": draft.confirmed_at.isoformat() if draft.confirmed_at else None,
        "created_agent_id": str(draft.created_agent_id) if draft.created_agent_id else None,
        "provisioning_task_id": str(draft.provisioning_task_id) if draft.provisioning_task_id else None,
        "provisioning": dict(draft.provisioning_json or {}),
        "provisioning_steps": [hr_provisioning_step_payload(step) for step in steps],
        "creation_state": readiness.creation_state if readiness else draft.status,
        "failure": (
            {"code": draft.failure_code, "message": draft.failure_message}
            if draft.failure_code or draft.failure_message
            else None
        ),
        "recovery": hr_creation_recovery_payload(draft, runtime_task),
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
    draft.failure_message = message
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
