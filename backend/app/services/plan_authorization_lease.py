"""Single-use Plan Mode authorization leases on the canonical approval ledger.

Plan confirmation is a decision about one immutable plan version.  It is not a
reusable capability token.  This module represents each hash-covered action
scope as an ``ApprovalRequest(action_type='plan_authorization')`` so Plan Mode
and ordinary tool approval share one durable, row-lockable ticket mechanism.

Trusted runtime entrypoints may pre-arm exact scopes in
``plan_json.authorization_scopes``; model-authored widening is discarded.  When
no exact scope is pre-armed, the harness derives one deterministic handoff scope
from the plan itself.  It normalizes and hashes each scope, binds it to the
authenticated requester and execution context, signs it with the confirmed plan
hash, and consumes it at most once.  Authorization proof fields are removed
before hashing so stamping a consumed lease onto a durable artifact does not
change the action it authorized.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
from typing import Any
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.services.approval_ticket import canonical_json, hash_policy_snapshot, hash_tool_input


LEASE_SCHEMA = "hive.plan_authorization_lease.v1"
LEASE_ACTION_TYPE = "plan_authorization"
DEFAULT_LEASE_TTL = timedelta(hours=24)

_PROOF_FIELDS = frozenset(
    {
        "confirmed_plan_id",
        "confirmed_plan_version",
        "confirmed_plan_hash",
        "confirmed_plan_session_id",
        "plan_id",
        "plan_version",
        "plan_hash",
        "plan_authorization",
        "_plan_authorization",
        "plan_authorization_lease_id",
        "plan_authorization_hash",
        "plan_exempt_reason",
        "plan_mode_decision",
        "plan_recommendation_id",
    }
)


class PlanAuthorizationLeaseError(RuntimeError):
    """Typed fail-closed error for issuing or consuming a plan lease."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = str(code)
        self.message = str(message)


@dataclass(frozen=True, slots=True)
class PlanAuthorizationBinding:
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    plan_id: uuid.UUID
    plan_version: int
    plan_hash: str
    requester_user_id: uuid.UUID
    confirmed_by_user_id: uuid.UUID
    session_id: str | None
    runtime_task_id: uuid.UUID | None
    execution_context_type: str
    execution_context_ref: str
    action_kind: str
    target_ref: str
    action_artifact: dict[str, Any]
    canonical_args_hash: str
    idempotency_key: str


@dataclass(frozen=True, slots=True)
class PlanAuthorizationSpec:
    binding: PlanAuthorizationBinding
    summary: str
    expires_at: datetime
    max_uses: int = 1

    @property
    def action_kind(self) -> str:
        return self.binding.action_kind

    @property
    def target_ref(self) -> str:
        return self.binding.target_ref


@dataclass(frozen=True, slots=True)
class PlanAuthorizationLease:
    lease_id: uuid.UUID
    binding: PlanAuthorizationBinding
    expires_at: datetime
    consumed_at: datetime | None
    evidence_id: str | None


def _uuid(value: Any, *, field: str) -> uuid.UUID:
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError) as exc:
        raise PlanAuthorizationLeaseError(f"missing_{field}", f"{field} must be a UUID") from exc


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def canonical_action_artifact(value: Any) -> Any:
    """Return the semantic action payload with authorization proof removed.

    Only exact reserved keys are removed.  Domain fields such as
    ``business_plan`` remain intact, and list order remains significant.
    """

    if isinstance(value, dict):
        return {
            str(key): canonical_action_artifact(item)
            for key, item in sorted(value.items(), key=lambda pair: str(pair[0]))
            if str(key) not in _PROOF_FIELDS
        }
    if isinstance(value, list):
        return [canonical_action_artifact(item) for item in value]
    if isinstance(value, tuple):
        return [canonical_action_artifact(item) for item in value]
    return value


def require_active_plan_authorization(plan: Any) -> dict[str, Any]:
    """Return the consumed handoff proof or fail closed.

    ``PlanModeService`` writes this evidence immediately after consuming the
    single-use ticket and before invoking a registered handoff handler.  Every
    handler copies the proof onto the durable artifact it creates so restart
    backstops verify the original decision instead of consuming a second token.
    """

    metadata = getattr(plan, "metadata_json", None)
    evidence = metadata.get("active_plan_authorization") if isinstance(metadata, dict) else None
    required = ("lease_id", "canonical_args_hash", "target_ref", "requester_user_id", "evidence_id")
    if (
        not isinstance(evidence, dict)
        or evidence.get("schema") != "hive.plan_authorization_evidence.v1"
        or any(not str(evidence.get(key) or "").strip() for key in required)
    ):
        raise PlanAuthorizationLeaseError(
            "active_evidence_missing",
            "confirmed plan handoff has no consumed authorization evidence",
        )
    return dict(evidence)


def _execution_context(
    *,
    plan_id: uuid.UUID,
    session_id: str | None,
    runtime_task_id: uuid.UUID | None,
) -> tuple[str, str]:
    clean_session_id = str(session_id or "").strip()
    if clean_session_id:
        return "session", clean_session_id
    if runtime_task_id is not None:
        return "runtime_task", str(runtime_task_id)
    return "plan", str(plan_id)


def _binding_payload(binding: PlanAuthorizationBinding) -> dict[str, Any]:
    return {
        "schema": LEASE_SCHEMA,
        "tenant_id": str(binding.tenant_id),
        "agent_id": str(binding.agent_id),
        "plan_id": str(binding.plan_id),
        "plan_version": binding.plan_version,
        "plan_hash": binding.plan_hash,
        "requester_user_id": str(binding.requester_user_id),
        "confirmed_by_user_id": str(binding.confirmed_by_user_id),
        "session_id": binding.session_id,
        "runtime_task_id": str(binding.runtime_task_id) if binding.runtime_task_id else None,
        "execution_context_type": binding.execution_context_type,
        "execution_context_ref": binding.execution_context_ref,
        "action_kind": binding.action_kind,
        "target_ref": binding.target_ref,
        "canonical_args_hash": binding.canonical_args_hash,
    }


def build_plan_authorization_binding(
    *,
    tenant_id: Any,
    agent_id: Any,
    plan_id: Any,
    plan_version: int,
    plan_hash: str,
    requester_user_id: Any,
    confirmed_by_user_id: Any,
    session_id: str | None,
    runtime_task_id: Any | None,
    action_kind: str,
    target_ref: str,
    action_artifact: dict[str, Any],
) -> PlanAuthorizationBinding:
    tenant_uuid = _uuid(tenant_id, field="tenant_id")
    agent_uuid = _uuid(agent_id, field="agent_id")
    plan_uuid = _uuid(plan_id, field="plan_id")
    requester_uuid = _uuid(requester_user_id, field="requester_user_id")
    confirmer_uuid = _uuid(confirmed_by_user_id, field="confirmed_by_user_id")
    clean_session_id = str(session_id or "").strip() or None
    # A session is the stable execution authority. RuntimeTask ids inside that
    # session are ephemeral attempts and must not invalidate an otherwise exact
    # confirmation on retry/resume.
    runtime_uuid = (
        _uuid(runtime_task_id, field="runtime_task_id")
        if runtime_task_id is not None and clean_session_id is None
        else None
    )
    clean_action_kind = str(action_kind or "").strip()
    clean_target_ref = str(target_ref or "").strip()
    clean_plan_hash = str(plan_hash or "").strip()
    if not clean_action_kind:
        raise PlanAuthorizationLeaseError("missing_action_kind", "plan authorization scope requires action_kind")
    if not clean_target_ref:
        raise PlanAuthorizationLeaseError("missing_target_ref", "plan authorization scope requires target_ref")
    if not clean_plan_hash:
        raise PlanAuthorizationLeaseError("missing_plan_hash", "confirmed plan requires a canonical hash")
    if not isinstance(action_artifact, dict):
        raise PlanAuthorizationLeaseError("invalid_arguments", "plan authorization arguments must be an object")

    normalized_artifact = canonical_action_artifact(action_artifact)
    canonical_args_hash = hashlib.sha256(canonical_json(normalized_artifact).encode("utf-8")).hexdigest()
    context_type, context_ref = _execution_context(
        plan_id=plan_uuid,
        session_id=clean_session_id,
        runtime_task_id=runtime_uuid,
    )
    preimage = {
        "schema": LEASE_SCHEMA,
        "tenant_id": str(tenant_uuid),
        "agent_id": str(agent_uuid),
        "plan_id": str(plan_uuid),
        "plan_version": int(plan_version),
        "plan_hash": clean_plan_hash,
        "requester_user_id": str(requester_uuid),
        "confirmed_by_user_id": str(confirmer_uuid),
        "execution_context_type": context_type,
        "execution_context_ref": context_ref,
        "action_kind": clean_action_kind,
        "target_ref": clean_target_ref,
        "canonical_args_hash": canonical_args_hash,
    }
    digest = hashlib.sha256(canonical_json(preimage).encode("utf-8")).hexdigest()
    return PlanAuthorizationBinding(
        tenant_id=tenant_uuid,
        agent_id=agent_uuid,
        plan_id=plan_uuid,
        plan_version=int(plan_version),
        plan_hash=clean_plan_hash,
        requester_user_id=requester_uuid,
        confirmed_by_user_id=confirmer_uuid,
        session_id=clean_session_id,
        runtime_task_id=runtime_uuid,
        execution_context_type=context_type,
        execution_context_ref=context_ref,
        action_kind=clean_action_kind,
        target_ref=clean_target_ref,
        action_artifact=normalized_artifact,
        canonical_args_hash=canonical_args_hash,
        idempotency_key=f"plan-lease:{digest}",
    )


def _handoff_scope(plan: Any) -> dict[str, Any]:
    plan_json = dict(getattr(plan, "plan_json", None) or {})
    handoff = plan_json.get("handoff") if isinstance(plan_json.get("handoff"), dict) else {}
    target = str(handoff.get("target") or "continue_current_session").strip() or "continue_current_session"
    action_by_target = {
        "continue_current_session": "continue_plan_session",
        "scheduled_trigger": "create_enabled_trigger",
        "detached": "create_enabled_trigger",
        "detached_runtime_task": "create_enabled_trigger",
        "delegation": "start_delegation",
        "agent_team": "start_agent_team",
        "workflow": "start_workflow",
    }
    action_kind = action_by_target.get(target, f"handoff:{target}")
    execution_contract = plan_json.get("execution_contract")
    return {
        "action_kind": action_kind,
        "target_ref": f"plan:{plan.id}:handoff:{target}",
        "arguments": {
            "approved_plan_id": str(plan.id),
            "approved_plan_version": int(plan.plan_version),
            "approved_plan_hash": str(plan.plan_hash or ""),
            "handoff_target": target,
            "execution_contract": dict(execution_contract) if isinstance(execution_contract, dict) else {},
        },
        "summary": f"Execute the confirmed plan through its {target} handoff",
        "max_uses": 1,
    }


def build_plan_handoff_authorization_scope(plan: Any) -> dict[str, Any]:
    """Return the exact default handoff scope for issuance and consumption."""

    return _handoff_scope(plan)


def build_plan_authorization_specs(
    *,
    plan: Any,
    confirming_user_id: Any,
    now: datetime | None = None,
) -> list[PlanAuthorizationSpec]:
    """Build immutable, hash-covered lease specs for one confirmed plan."""

    if str(getattr(plan, "status", "")) != "confirmed":
        raise PlanAuthorizationLeaseError("plan_not_confirmed", "only a confirmed plan can issue authorization")
    current = _utc(now or datetime.now(timezone.utc))
    plan_expiry = getattr(plan, "expires_at", None)
    expires_at = _utc(plan_expiry) if isinstance(plan_expiry, datetime) else current + DEFAULT_LEASE_TTL
    if expires_at <= current:
        raise PlanAuthorizationLeaseError("expired", "confirmed plan authorization has expired")

    plan_json = dict(getattr(plan, "plan_json", None) or {})
    raw_scopes = plan_json.get("authorization_scopes")
    if raw_scopes is None:
        scopes = [_handoff_scope(plan)]
    elif not isinstance(raw_scopes, list):
        raise PlanAuthorizationLeaseError(
            "invalid_authorization_scopes",
            "plan authorization_scopes must be an array",
        )
    elif not raw_scopes:
        scopes = [_handoff_scope(plan)]
    else:
        scopes = raw_scopes

    specs: list[PlanAuthorizationSpec] = []
    seen_keys: set[str] = set()
    for raw_scope in scopes:
        if not isinstance(raw_scope, dict):
            raise PlanAuthorizationLeaseError("invalid_scope", "each plan authorization scope must be an object")
        action_kind = str(raw_scope.get("action_kind") or "").strip()
        if not action_kind:
            raise PlanAuthorizationLeaseError("missing_action_kind", "plan authorization scope requires action_kind")
        target_ref = str(raw_scope.get("target_ref") or "").strip()
        if not target_ref:
            raise PlanAuthorizationLeaseError("missing_target_ref", "plan authorization scope requires target_ref")
        arguments = raw_scope.get("arguments")
        if not isinstance(arguments, dict):
            raise PlanAuthorizationLeaseError("invalid_arguments", "plan authorization arguments must be an object")
        max_uses = int(raw_scope.get("max_uses", 1) or 1)
        if max_uses != 1:
            raise PlanAuthorizationLeaseError(
                "invalid_max_uses",
                "each plan authorization scope is single-use; duplicate actions require separate scopes",
            )
        summary = str(raw_scope.get("summary") or "").strip()
        if not summary:
            raise PlanAuthorizationLeaseError(
                "missing_summary",
                "plan authorization scope requires a user-facing summary",
            )
        binding = build_plan_authorization_binding(
            tenant_id=getattr(plan, "tenant_id", None),
            agent_id=getattr(plan, "agent_id", None),
            plan_id=getattr(plan, "id", None),
            plan_version=int(getattr(plan, "plan_version", 0) or 0),
            plan_hash=str(getattr(plan, "plan_hash", "") or ""),
            requester_user_id=getattr(plan, "requested_by_user_id", None),
            confirmed_by_user_id=confirming_user_id,
            session_id=getattr(plan, "session_id", None),
            runtime_task_id=getattr(plan, "runtime_task_id", None),
            action_kind=action_kind,
            target_ref=target_ref,
            action_artifact=arguments,
        )
        if binding.idempotency_key in seen_keys:
            raise PlanAuthorizationLeaseError("duplicate_scope", "duplicate plan authorization scope")
        seen_keys.add(binding.idempotency_key)
        specs.append(
            PlanAuthorizationSpec(
                binding=binding,
                summary=summary,
                expires_at=expires_at,
                max_uses=1,
            )
        )
    return specs


def _normalized_ticket_arguments(binding: PlanAuthorizationBinding) -> dict[str, Any]:
    return {
        "binding": _binding_payload(binding),
        "action_artifact": binding.action_artifact,
    }


def _ticket_policy_snapshot(binding: PlanAuthorizationBinding) -> dict[str, Any]:
    return {
        "schema": "hive.plan_authorization_policy_snapshot.v1",
        "plan_id": str(binding.plan_id),
        "plan_version": binding.plan_version,
        "plan_hash": binding.plan_hash,
        "confirmed_by_user_id": str(binding.confirmed_by_user_id),
        "execution_context_type": binding.execution_context_type,
        "execution_context_ref": binding.execution_context_ref,
        "action_kind": binding.action_kind,
        "target_ref": binding.target_ref,
        "canonical_args_hash": binding.canonical_args_hash,
    }


def _lease_from_row(row: Any, binding: PlanAuthorizationBinding) -> PlanAuthorizationLease:
    receipt = dict(getattr(row, "execution_receipt", None) or {})
    return PlanAuthorizationLease(
        lease_id=row.id,
        binding=binding,
        expires_at=row.expires_at,
        consumed_at=row.consumed_at,
        evidence_id=str(receipt.get("evidence_id") or "") or None,
    )


async def issue_plan_authorization_leases(
    *,
    db: AsyncSession,
    plan: Any,
    confirming_user_id: Any,
    now: datetime | None = None,
) -> list[PlanAuthorizationLease]:
    """Persist single-use plan tickets inside the caller's confirmation transaction."""

    from app.models.audit import ApprovalRequest

    current = _utc(now or datetime.now(timezone.utc))
    specs = build_plan_authorization_specs(plan=plan, confirming_user_id=confirming_user_id, now=current)
    leases: list[PlanAuthorizationLease] = []
    for spec in specs:
        binding = spec.binding
        existing_stmt = select(ApprovalRequest).where(
            ApprovalRequest.execution_idempotency_key == binding.idempotency_key
        )
        existing_stmt._plan_lease_lookup_key = binding.idempotency_key  # type: ignore[attr-defined]
        existing = (await db.execute(existing_stmt)).scalar_one_or_none()
        if existing is not None:
            if existing.action_type != LEASE_ACTION_TYPE or existing.input_hash != hash_tool_input(
                f"plan:{binding.action_kind}", _normalized_ticket_arguments(binding)
            ):
                raise PlanAuthorizationLeaseError("lease_key_conflict", "plan lease idempotency key conflict")
            leases.append(_lease_from_row(existing, binding))
            continue

        normalized_arguments = _normalized_ticket_arguments(binding)
        policy_snapshot = _ticket_policy_snapshot(binding)
        row = ApprovalRequest(
            tenant_id=binding.tenant_id,
            agent_id=binding.agent_id,
            action_type=LEASE_ACTION_TYPE,
            details={
                **_binding_payload(binding),
                "summary": spec.summary,
                "max_uses": spec.max_uses,
                "use_count": 0,
            },
            status="approved",
            resolved_at=current,
            resolved_by=binding.confirmed_by_user_id,
            requested_by=binding.requester_user_id,
            decision_id=f"plan:{binding.plan_id}:{binding.action_kind}",
            tool_name=f"plan:{binding.action_kind}",
            normalized_arguments=normalized_arguments,
            input_hash=hash_tool_input(f"plan:{binding.action_kind}", normalized_arguments),
            policy_snapshot=policy_snapshot,
            policy_snapshot_hash=hash_policy_snapshot(policy_snapshot),
            expires_at=spec.expires_at,
            consumed_at=None,
            execution_status="approved",
            execution_idempotency_key=binding.idempotency_key,
            execution_receipt={"schema": LEASE_SCHEMA, "status": "issued"},
        )
        db.add(row)
        await db.flush()
        leases.append(_lease_from_row(row, binding))
    return leases


async def _candidate_plan_leases(
    db: AsyncSession,
    *,
    agent_id: uuid.UUID,
    plan_id: uuid.UUID,
) -> list[Any]:
    from app.models.audit import ApprovalRequest

    stmt = (
        select(ApprovalRequest)
        .where(
            ApprovalRequest.agent_id == agent_id,
            ApprovalRequest.action_type == LEASE_ACTION_TYPE,
            ApprovalRequest.details["plan_id"].as_string() == str(plan_id),
        )
        .with_for_update()
    )
    stmt._plan_lease_candidates_plan_id = plan_id  # type: ignore[attr-defined]
    result = await db.execute(stmt)
    return list(result.scalars().all())


def _diagnose_binding_mismatch(rows: list[Any], expected: PlanAuthorizationBinding) -> None:
    for row in rows:
        details = dict(row.details or {})
        if str(details.get("action_kind") or "") != expected.action_kind:
            continue
        if str(details.get("requester_user_id") or "") != str(expected.requester_user_id):
            raise PlanAuthorizationLeaseError("requester_mismatch", "plan authorization requester mismatch")
        if str(details.get("session_id") or "") != str(expected.session_id or ""):
            raise PlanAuthorizationLeaseError("session_mismatch", "plan authorization session mismatch")
        if str(details.get("runtime_task_id") or "") != str(expected.runtime_task_id or ""):
            raise PlanAuthorizationLeaseError("runtime_task_mismatch", "plan authorization runtime task mismatch")
    raise PlanAuthorizationLeaseError("lease_not_found", "no exact plan authorization lease matches this action")


async def consume_plan_authorization_lease(
    *,
    tenant_id: Any,
    agent_id: Any,
    plan_id: Any,
    requester_user_id: Any,
    session_id: str | None,
    runtime_task_id: Any | None,
    action_kind: str,
    target_ref: str,
    action_artifact: dict[str, Any],
    evidence_id: str,
    confirmed_by_user_id: Any | None = None,
    plan_version: int | None = None,
    plan_hash: str | None = None,
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    db: AsyncSession | None = None,
    now: datetime | None = None,
    allow_idempotent_resume: bool = False,
) -> PlanAuthorizationLease:
    """Atomically consume the exact plan ticket; every mismatch fails closed.

    Callers that create the authorized resource pass their existing ``db``
    transaction.  That makes ticket consumption and the business write one
    atomic commit.  Boundary callers may omit ``db`` and receive a dedicated
    tenant-scoped transaction.
    """

    from app.database import tenant_scoped_session

    tenant_uuid = _uuid(tenant_id, field="tenant_id")
    agent_uuid = _uuid(agent_id, field="agent_id")
    plan_uuid = _uuid(plan_id, field="plan_id")
    requester_uuid = _uuid(requester_user_id, field="requester_user_id")
    current = _utc(now or datetime.now(timezone.utc))

    if db is not None:
        return await _consume_plan_authorization_lease_in_db(
            db=db,
            tenant_uuid=tenant_uuid,
            agent_uuid=agent_uuid,
            plan_uuid=plan_uuid,
            requester_uuid=requester_uuid,
            session_id=session_id,
            runtime_task_id=runtime_task_id,
            action_kind=action_kind,
            target_ref=target_ref,
            action_artifact=action_artifact,
            evidence_id=evidence_id,
            confirmed_by_user_id=confirmed_by_user_id,
            plan_version=plan_version,
            plan_hash=plan_hash,
            current=current,
            allow_idempotent_resume=allow_idempotent_resume,
        )

    async with tenant_scoped_session(
        tenant_uuid,
        session_factory=session_factory,
        require_tenant=True,
        source="consume_plan_authorization_lease",
    ) as owned_db:
        return await _consume_plan_authorization_lease_in_db(
            db=owned_db,
            tenant_uuid=tenant_uuid,
            agent_uuid=agent_uuid,
            plan_uuid=plan_uuid,
            requester_uuid=requester_uuid,
            session_id=session_id,
            runtime_task_id=runtime_task_id,
            action_kind=action_kind,
            target_ref=target_ref,
            action_artifact=action_artifact,
            evidence_id=evidence_id,
            confirmed_by_user_id=confirmed_by_user_id,
            plan_version=plan_version,
            plan_hash=plan_hash,
            current=current,
            allow_idempotent_resume=allow_idempotent_resume,
        )


async def _consume_plan_authorization_lease_in_db(
    *,
    db: AsyncSession,
    tenant_uuid: uuid.UUID,
    agent_uuid: uuid.UUID,
    plan_uuid: uuid.UUID,
    requester_uuid: uuid.UUID,
    session_id: str | None,
    runtime_task_id: Any | None,
    action_kind: str,
    target_ref: str,
    action_artifact: dict[str, Any],
    evidence_id: str,
    confirmed_by_user_id: Any | None,
    plan_version: int | None,
    plan_hash: str | None,
    current: datetime,
    allow_idempotent_resume: bool,
) -> PlanAuthorizationLease:
    """Consume a lease using the caller-owned tenant transaction."""

    from app.models.audit import ApprovalRequest
    from app.models.plan_request import AgentPlanRequest

    plan_stmt = select(AgentPlanRequest).where(AgentPlanRequest.id == plan_uuid).with_for_update()
    plan_stmt._plan_lookup_id = plan_uuid  # type: ignore[attr-defined]
    plan = (await db.execute(plan_stmt)).scalar_one_or_none()
    if plan is None:
        raise PlanAuthorizationLeaseError("plan_not_found", "confirmed plan not found in tenant")
    if plan.agent_id != agent_uuid:
        raise PlanAuthorizationLeaseError("agent_mismatch", "plan authorization agent mismatch")
    if plan.status != "confirmed":
        raise PlanAuthorizationLeaseError("plan_not_confirmed", "plan is no longer confirmed")
    if isinstance(plan.expires_at, datetime) and _utc(plan.expires_at) <= current:
        raise PlanAuthorizationLeaseError("expired", "confirmed plan authorization has expired")
    stored_plan_version = int(plan.plan_version)
    stored_plan_hash = str(plan.plan_hash or "")
    if plan_version is not None and int(plan_version) != stored_plan_version:
        raise PlanAuthorizationLeaseError("version_mismatch", "plan authorization version mismatch")
    if plan_hash is not None and str(plan_hash) != stored_plan_hash:
        raise PlanAuthorizationLeaseError("hash_mismatch", "plan authorization hash mismatch")
    confirmer = confirmed_by_user_id or plan.confirmed_by_user_id
    if confirmer is None:
        raise PlanAuthorizationLeaseError("missing_confirmer", "confirmed plan has no confirmer")

    expected = build_plan_authorization_binding(
        tenant_id=tenant_uuid,
        agent_id=agent_uuid,
        plan_id=plan_uuid,
        plan_version=stored_plan_version,
        plan_hash=stored_plan_hash,
        requester_user_id=requester_uuid,
        confirmed_by_user_id=confirmer,
        session_id=session_id,
        runtime_task_id=runtime_task_id,
        action_kind=action_kind,
        target_ref=target_ref,
        action_artifact=action_artifact,
    )
    row_stmt = (
        select(ApprovalRequest)
        .where(ApprovalRequest.execution_idempotency_key == expected.idempotency_key)
        .with_for_update()
    )
    row_stmt._plan_lease_lookup_key = expected.idempotency_key  # type: ignore[attr-defined]
    row = (await db.execute(row_stmt)).scalar_one_or_none()
    if row is None:
        rows = await _candidate_plan_leases(db, agent_id=agent_uuid, plan_id=plan_uuid)
        _diagnose_binding_mismatch(rows, expected)
    assert row is not None

    details = dict(row.details or {})
    if row.tenant_id != tenant_uuid or row.agent_id != agent_uuid:
        raise PlanAuthorizationLeaseError("authority_mismatch", "plan lease authority mismatch")
    if row.requested_by != requester_uuid:
        raise PlanAuthorizationLeaseError("requester_mismatch", "plan authorization requester mismatch")
    if row.resolved_by != expected.confirmed_by_user_id:
        raise PlanAuthorizationLeaseError("confirmer_mismatch", "plan authorization confirmer mismatch")
    if row.status != "approved":
        raise PlanAuthorizationLeaseError("not_approved", "plan authorization is not approved")
    if row.expires_at is None or _utc(row.expires_at) <= current:
        raise PlanAuthorizationLeaseError("expired", "plan authorization lease expired")
    receipt = dict(row.execution_receipt or {})
    resuming = row.consumed_at is not None
    if resuming and not (
        allow_idempotent_resume
        and str(receipt.get("evidence_id") or "") == str(evidence_id)
        and str(receipt.get("status") or "") == "consumed"
    ):
        raise PlanAuthorizationLeaseError("already_consumed", "plan authorization lease already consumed")
    expected_use_count = 1 if resuming else 0
    if int(details.get("max_uses") or 0) != 1 or int(details.get("use_count") or 0) != expected_use_count:
        raise PlanAuthorizationLeaseError("invalid_use_state", "plan authorization use state is invalid")

    normalized_arguments = _normalized_ticket_arguments(expected)
    expected_input_hash = hash_tool_input(f"plan:{expected.action_kind}", normalized_arguments)
    if row.input_hash != expected_input_hash:
        raise PlanAuthorizationLeaseError("input_hash_mismatch", "plan authorization input hash mismatch")
    policy_snapshot = _ticket_policy_snapshot(expected)
    if row.policy_snapshot_hash != hash_policy_snapshot(policy_snapshot):
        raise PlanAuthorizationLeaseError("policy_snapshot_mismatch", "plan authorization snapshot mismatch")

    if resuming:
        return _lease_from_row(row, expected)

    row.consumed_at = current
    row.execution_status = "succeeded"
    row.details = {**details, "use_count": 1}
    row.execution_receipt = {
        "schema": LEASE_SCHEMA,
        "status": "consumed",
        "evidence_id": str(evidence_id),
        "consumed_at": current.isoformat(),
        "action_kind": expected.action_kind,
        "target_ref": expected.target_ref,
        "canonical_args_hash": expected.canonical_args_hash,
    }
    await db.flush()
    return _lease_from_row(row, expected)


def _binding_from_ticket(row: Any) -> PlanAuthorizationBinding:
    normalized = dict(getattr(row, "normalized_arguments", None) or {})
    stored = normalized.get("binding")
    artifact = normalized.get("action_artifact")
    if not isinstance(stored, dict) or not isinstance(artifact, dict):
        raise PlanAuthorizationLeaseError("corrupt_lease", "plan authorization ticket payload is corrupt")
    return build_plan_authorization_binding(
        tenant_id=stored.get("tenant_id"),
        agent_id=stored.get("agent_id"),
        plan_id=stored.get("plan_id"),
        plan_version=int(stored.get("plan_version") or 0),
        plan_hash=str(stored.get("plan_hash") or ""),
        requester_user_id=stored.get("requester_user_id"),
        confirmed_by_user_id=stored.get("confirmed_by_user_id"),
        session_id=stored.get("session_id"),
        runtime_task_id=stored.get("runtime_task_id"),
        action_kind=str(stored.get("action_kind") or ""),
        target_ref=str(stored.get("target_ref") or ""),
        action_artifact=artifact,
    )


async def verify_consumed_plan_authorization_lease(
    *,
    tenant_id: Any,
    agent_id: Any,
    plan_id: Any,
    evidence: dict[str, Any],
    session_factory: async_sessionmaker[AsyncSession] | None = None,
    db: AsyncSession | None = None,
) -> PlanAuthorizationLease:
    """Verify a durable resource points at the exact already-consumed ticket."""

    from app.database import tenant_scoped_session

    if not isinstance(evidence, dict) or evidence.get("schema") != "hive.plan_authorization_evidence.v1":
        raise PlanAuthorizationLeaseError("invalid_evidence", "plan authorization evidence is missing or malformed")
    tenant_uuid = _uuid(tenant_id, field="tenant_id")
    agent_uuid = _uuid(agent_id, field="agent_id")
    plan_uuid = _uuid(plan_id, field="plan_id")
    lease_uuid = _uuid(evidence.get("lease_id"), field="lease_id")

    if db is not None:
        return await _verify_consumed_plan_authorization_lease_in_db(
            db=db,
            tenant_uuid=tenant_uuid,
            agent_uuid=agent_uuid,
            plan_uuid=plan_uuid,
            lease_uuid=lease_uuid,
            evidence=evidence,
        )

    async with tenant_scoped_session(
        tenant_uuid,
        session_factory=session_factory,
        require_tenant=True,
        source="verify_consumed_plan_authorization_lease",
    ) as owned_db:
        return await _verify_consumed_plan_authorization_lease_in_db(
            db=owned_db,
            tenant_uuid=tenant_uuid,
            agent_uuid=agent_uuid,
            plan_uuid=plan_uuid,
            lease_uuid=lease_uuid,
            evidence=evidence,
        )


async def _verify_consumed_plan_authorization_lease_in_db(
    *,
    db: AsyncSession,
    tenant_uuid: uuid.UUID,
    agent_uuid: uuid.UUID,
    plan_uuid: uuid.UUID,
    lease_uuid: uuid.UUID,
    evidence: dict[str, Any],
) -> PlanAuthorizationLease:
    """Verify a persisted lease proof in the caller-owned tenant transaction."""

    from app.models.audit import ApprovalRequest
    from app.models.plan_request import AgentPlanRequest

    lease_stmt = select(ApprovalRequest).where(ApprovalRequest.id == lease_uuid)
    lease_stmt._plan_lease_lookup_id = lease_uuid  # type: ignore[attr-defined]
    row = (await db.execute(lease_stmt)).scalar_one_or_none()
    if row is None or row.action_type != LEASE_ACTION_TYPE:
        raise PlanAuthorizationLeaseError("lease_not_found", "plan authorization lease not found")
    if row.tenant_id != tenant_uuid or row.agent_id != agent_uuid:
        raise PlanAuthorizationLeaseError("authority_mismatch", "plan authorization authority mismatch")
    if row.status != "approved" or row.consumed_at is None or row.execution_status != "succeeded":
        raise PlanAuthorizationLeaseError("not_consumed", "plan authorization lease is not consumed")

    binding = _binding_from_ticket(row)
    if binding.plan_id != plan_uuid:
        raise PlanAuthorizationLeaseError("plan_mismatch", "plan authorization plan mismatch")
    expected_evidence = {
        "canonical_args_hash": binding.canonical_args_hash,
        "target_ref": binding.target_ref,
        "requester_user_id": str(binding.requester_user_id),
        "session_id": binding.session_id,
        "runtime_task_id": str(binding.runtime_task_id) if binding.runtime_task_id else None,
    }
    for key, expected_value in expected_evidence.items():
        actual_value = evidence.get(key)
        if (str(actual_value) if actual_value is not None else None) != (
            str(expected_value) if expected_value is not None else None
        ):
            raise PlanAuthorizationLeaseError("evidence_mismatch", f"plan authorization {key} mismatch")
    receipt = dict(row.execution_receipt or {})
    if str(receipt.get("evidence_id") or "") != str(evidence.get("evidence_id") or ""):
        raise PlanAuthorizationLeaseError("evidence_mismatch", "plan authorization evidence id mismatch")
    if str(receipt.get("canonical_args_hash") or "") != binding.canonical_args_hash:
        raise PlanAuthorizationLeaseError("corrupt_receipt", "plan authorization receipt hash mismatch")

    plan_stmt = select(AgentPlanRequest).where(AgentPlanRequest.id == plan_uuid)
    plan_stmt._plan_lookup_id = plan_uuid  # type: ignore[attr-defined]
    plan = (await db.execute(plan_stmt)).scalar_one_or_none()
    if plan is None or plan.status != "confirmed":
        raise PlanAuthorizationLeaseError("plan_not_confirmed", "plan authorization source is not confirmed")
    if int(plan.plan_version) != binding.plan_version or str(plan.plan_hash or "") != binding.plan_hash:
        raise PlanAuthorizationLeaseError("plan_drift", "plan authorization source has drifted")
    return _lease_from_row(row, binding)


__all__ = [
    "DEFAULT_LEASE_TTL",
    "LEASE_ACTION_TYPE",
    "LEASE_SCHEMA",
    "PlanAuthorizationBinding",
    "PlanAuthorizationLease",
    "PlanAuthorizationLeaseError",
    "PlanAuthorizationSpec",
    "build_plan_authorization_binding",
    "build_plan_authorization_specs",
    "build_plan_handoff_authorization_scope",
    "canonical_action_artifact",
    "consume_plan_authorization_lease",
    "issue_plan_authorization_leases",
    "require_active_plan_authorization",
    "verify_consumed_plan_authorization_lease",
]
