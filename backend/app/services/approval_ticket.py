"""Single-use approval tickets bound to principals, input, and policy snapshots."""

from __future__ import annotations

import ast
from dataclasses import asdict, dataclass, is_dataclass
from datetime import datetime, timezone
from enum import Enum
import hashlib
import hmac
import json
from pathlib import Path
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession


class ApprovalTicketError(RuntimeError):
    """Raised when an approval cannot authorize exactly one execution."""


_APPROVAL_EXECUTION_ENVELOPE_SCHEMA = "hive.approval_execution_envelope.v2"


@dataclass(frozen=True, slots=True)
class ApprovalExecutionEnvelope:
    """Immutable authority and recovery context captured before approval.

    The envelope contains no executable callback. It is a JSON-safe snapshot of
    the trusted runtime fields needed to re-enter the normal tool kernel after a
    human decision. The database hash binds every field against later mutation.
    """

    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    requester_user_id: uuid.UUID
    execution_identity: Any | None
    workspace: Path
    tool_call_id: str
    session_id: str | None
    permission_profile: Any
    delegation_token: Any | None
    turn_id: str | None
    runtime_task_id: str | None
    budget_run_id: str | None
    origin_channel: str | None
    round_state: dict[str, Any]
    t0_refs: tuple[str, ...]
    emit_runtime_hooks: bool
    plan_mode_interactive_available: bool
    plan_mode_unattended_available: bool
    resolved_asset_refs: tuple[Any, ...]


@dataclass(frozen=True, slots=True)
class ApprovalDecisionSet:
    """The sole policy override contributed by one consumed approval."""

    approval_id: uuid.UUID
    tenant_id: uuid.UUID
    action_type: str
    tool_name: str
    input_hash: str
    policy_snapshot_hash: str
    envelope_hash: str
    decision_id: str
    requested_by_user_id: uuid.UUID
    approved_by_user_id: uuid.UUID
    plan_authorization: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ApprovalExecutionTicket:
    approval_id: uuid.UUID
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    requested_by_user_id: uuid.UUID
    approved_by_user_id: uuid.UUID
    tool_name: str
    arguments: dict[str, Any]
    input_hash: str
    policy_snapshot_hash: str
    idempotency_key: str
    decision_id: str
    action_type: str
    execution_envelope: dict[str, Any]
    execution_envelope_hash: str


def _json_safe(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _json_safe(asdict(value))
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, uuid.UUID | Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_json_safe(item) for item in value), key=lambda item: str(item))
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise ApprovalTicketError(f"approval execution envelope contains unsupported value: {type(value).__name__}")


def build_approval_execution_envelope(
    *,
    context: Any,
    tool_call_id: str,
    emit_runtime_hooks: bool,
    plan_mode_interactive_available: bool = False,
    plan_mode_unattended_available: bool = False,
) -> dict[str, Any]:
    """Capture the exact trusted context needed by a deferred tool action."""

    try:
        tenant_id = uuid.UUID(str(context.tenant_id))
    except (TypeError, ValueError) as exc:
        raise ApprovalTicketError("approval execution envelope requires tenant authority") from exc
    try:
        agent_id = uuid.UUID(str(context.agent_id))
        requester_user_id = uuid.UUID(str(context.user_id))
    except (TypeError, ValueError) as exc:
        raise ApprovalTicketError("approval execution envelope requires agent and requester authority") from exc
    clean_tool_call_id = str(tool_call_id or "").strip()
    if not clean_tool_call_id:
        raise ApprovalTicketError("approval execution envelope requires tool_call_id")
    workspace = str(Path(context.workspace))
    permission_profile = _json_safe(context.permission_profile) if context.permission_profile is not None else {}
    delegation_token = _json_safe(context.delegation_token) if context.delegation_token is not None else None
    execution_identity = getattr(context, "execution_identity", None)
    execution_identity_payload = _json_safe(execution_identity) if execution_identity is not None else None
    if isinstance(execution_identity_payload, dict):
        identity_type = str(execution_identity_payload.get("identity_type") or "").strip()
        identity_id = execution_identity_payload.get("identity_id")
        if identity_type == "external_principal":
            raise ApprovalTicketError("unbound external principal cannot request tool approval")
        if identity_type == "external_principal_bound" and not identity_id:
            raise ApprovalTicketError("bound external principal approval requires principal identity")
        if identity_type == "delegated_user" and str(identity_id or "") != str(requester_user_id):
            raise ApprovalTicketError("delegated approval identity does not match requester")
    return {
        "schema": _APPROVAL_EXECUTION_ENVELOPE_SCHEMA,
        "tenant_id": str(tenant_id),
        "agent_id": str(agent_id),
        "requester_user_id": str(requester_user_id),
        "execution_identity": execution_identity_payload,
        "workspace": workspace,
        "tool_call_id": clean_tool_call_id,
        "session_id": str(context.session_id) if context.session_id is not None else None,
        "permission_profile": permission_profile,
        "delegation_token": delegation_token,
        "turn_id": str(context.turn_id) if context.turn_id is not None else None,
        "runtime_task_id": str(context.runtime_task_id) if context.runtime_task_id is not None else None,
        "budget_run_id": str(context.budget_run_id) if context.budget_run_id is not None else None,
        "origin_channel": str(context.origin_channel) if context.origin_channel is not None else None,
        "round_state": _json_safe(dict(context.round_state or {})),
        "t0_refs": [str(ref) for ref in tuple(context.t0_refs or ()) if str(ref).strip()],
        "resolved_asset_refs": _json_safe(tuple(getattr(context, "resolved_asset_refs", ()) or ())),
        "emit_runtime_hooks": bool(emit_runtime_hooks),
        "plan_mode_interactive_available": bool(plan_mode_interactive_available),
        "plan_mode_unattended_available": bool(plan_mode_unattended_available),
    }


def hash_approval_execution_envelope(envelope: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(_json_safe(dict(envelope))).encode("utf-8")).hexdigest()


def restore_approval_execution_envelope(
    envelope: Any,
    *,
    expected_hash: str,
) -> ApprovalExecutionEnvelope:
    """Verify and restore a persisted envelope without trusting its values."""

    from app.agents.delegation_token import DelegationToken
    from app.core.execution_context import ExecutionIdentity
    from app.runtime.ccplus_contracts import ResolvedAssetRefV1, SandboxProfile, build_permission_profile

    if not isinstance(envelope, dict):
        raise ApprovalTicketError("approval execution envelope must be an object")
    if envelope.get("schema") not in {
        "hive.approval_execution_envelope.v1",
        _APPROVAL_EXECUTION_ENVELOPE_SCHEMA,
    }:
        raise ApprovalTicketError("unsupported approval execution envelope schema")
    actual_hash = hash_approval_execution_envelope(envelope)
    if not expected_hash or not hmac.compare_digest(actual_hash, str(expected_hash)):
        raise ApprovalTicketError("approval execution envelope hash mismatch")
    try:
        tenant_id = uuid.UUID(str(envelope["tenant_id"]))
        agent_id = uuid.UUID(str(envelope["agent_id"]))
        requester_user_id = uuid.UUID(str(envelope["requester_user_id"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise ApprovalTicketError("approval execution envelope authority is invalid") from exc
    tool_call_id = str(envelope.get("tool_call_id") or "").strip()
    workspace_text = str(envelope.get("workspace") or "").strip()
    if not tool_call_id or not workspace_text:
        raise ApprovalTicketError("approval execution envelope recovery fields are incomplete")

    execution_identity = None
    raw_execution_identity = envelope.get("execution_identity")
    if raw_execution_identity is not None:
        if not isinstance(raw_execution_identity, dict):
            raise ApprovalTicketError("approval execution identity is invalid")
        identity_type = str(raw_execution_identity.get("identity_type") or "").strip()
        identity_label = str(raw_execution_identity.get("label") or "").strip()
        raw_identity_id = raw_execution_identity.get("identity_id")
        if identity_type not in {
            "agent_bot",
            "delegated_user",
            "external_principal",
            "external_principal_bound",
        }:
            raise ApprovalTicketError("approval execution identity type is invalid")
        try:
            identity_id = uuid.UUID(str(raw_identity_id)) if raw_identity_id else None
        except (TypeError, ValueError) as exc:
            raise ApprovalTicketError("approval execution identity id is invalid") from exc
        if identity_type == "external_principal":
            raise ApprovalTicketError("unbound external principal cannot hold tool approval")
        if identity_type in {"delegated_user", "external_principal_bound"} and identity_id is None:
            raise ApprovalTicketError("approval execution identity is incomplete")
        if identity_type == "delegated_user" and identity_id != requester_user_id:
            raise ApprovalTicketError("approval execution delegated identity mismatch")
        execution_identity = ExecutionIdentity(
            identity_type=identity_type,
            identity_id=identity_id,
            label=identity_label,
        )

    raw_profile = dict(envelope.get("permission_profile") or {})
    sequence_fields = (
        "writable_roots",
        "readable_roots",
        "denied_reads",
        "allowed_tools",
        "denied_actions",
    )
    for field_name in sequence_fields:
        if field_name in raw_profile:
            raw_profile[field_name] = tuple(str(item) for item in raw_profile[field_name])
    if "sandbox" in raw_profile:
        try:
            raw_profile["sandbox"] = SandboxProfile(str(raw_profile["sandbox"]))
        except ValueError as exc:
            raise ApprovalTicketError("approval execution envelope sandbox profile is invalid") from exc
    permission_profile = build_permission_profile(raw_profile)

    delegation_token = None
    raw_token = envelope.get("delegation_token")
    if raw_token is not None:
        if not isinstance(raw_token, dict):
            raise ApprovalTicketError("approval execution envelope delegation token is invalid")
        try:
            delegation_token = DelegationToken(
                delegation_id=str(raw_token["delegation_id"]),
                parent_agent_id=uuid.UUID(str(raw_token["parent_agent_id"])),
                child_agent_id=uuid.UUID(str(raw_token["child_agent_id"])),
                issued_at=float(raw_token["issued_at"]),
                expires_at=float(raw_token["expires_at"]),
                granted_capabilities=frozenset(str(item) for item in raw_token.get("granted_capabilities", ())),
                inherit_parent_capabilities=bool(raw_token.get("inherit_parent_capabilities", False)),
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise ApprovalTicketError("approval execution envelope delegation token is invalid") from exc
        if delegation_token.child_agent_id != agent_id:
            raise ApprovalTicketError("approval execution envelope delegation authority mismatch")

    round_state = envelope.get("round_state") or {}
    if not isinstance(round_state, dict):
        raise ApprovalTicketError("approval execution envelope round state is invalid")
    t0_refs = envelope.get("t0_refs") or []
    if not isinstance(t0_refs, list):
        raise ApprovalTicketError("approval execution envelope evidence refs are invalid")
    raw_asset_refs = envelope.get("resolved_asset_refs") or []
    if not isinstance(raw_asset_refs, list):
        raise ApprovalTicketError("approval execution envelope asset refs are invalid")
    resolved_asset_refs = []
    try:
        for item in raw_asset_refs:
            if not isinstance(item, dict):
                raise TypeError("asset ref must be an object")
            resolved_asset_refs.append(
                ResolvedAssetRefV1(
                    asset_id=str(item["asset_id"]),
                    asset_type=str(item["asset_type"]),
                    native_key=str(item["native_key"]),
                    revision_id=str(item["revision_id"]),
                    revision_version=int(item["revision_version"]),
                    content_hash=str(item["content_hash"]),
                    source_ref=str(item["source_ref"]) if item.get("source_ref") is not None else None,
                )
            )
    except (KeyError, TypeError, ValueError) as exc:
        raise ApprovalTicketError("approval execution envelope asset refs are invalid") from exc
    return ApprovalExecutionEnvelope(
        tenant_id=tenant_id,
        agent_id=agent_id,
        requester_user_id=requester_user_id,
        execution_identity=execution_identity,
        workspace=Path(workspace_text),
        tool_call_id=tool_call_id,
        session_id=str(envelope["session_id"]) if envelope.get("session_id") is not None else None,
        permission_profile=permission_profile,
        delegation_token=delegation_token,
        turn_id=str(envelope["turn_id"]) if envelope.get("turn_id") is not None else None,
        runtime_task_id=(str(envelope["runtime_task_id"]) if envelope.get("runtime_task_id") is not None else None),
        budget_run_id=str(envelope["budget_run_id"]) if envelope.get("budget_run_id") is not None else None,
        origin_channel=str(envelope["origin_channel"]) if envelope.get("origin_channel") is not None else None,
        round_state=dict(round_state),
        t0_refs=tuple(str(ref) for ref in t0_refs if str(ref).strip()),
        emit_runtime_hooks=bool(envelope.get("emit_runtime_hooks", True)),
        plan_mode_interactive_available=bool(envelope.get("plan_mode_interactive_available", False)),
        plan_mode_unattended_available=bool(envelope.get("plan_mode_unattended_available", False)),
        resolved_asset_refs=tuple(resolved_asset_refs),
    )


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def normalize_tool_arguments(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            try:
                parsed = ast.literal_eval(value)
            except (ValueError, SyntaxError):
                parsed = None
        if isinstance(parsed, dict):
            return dict(parsed)
    raise ApprovalTicketError("approval tool arguments must be an object")


def hash_tool_input(tool_name: str, arguments: dict[str, Any]) -> str:
    payload = {"tool_name": str(tool_name).strip(), "arguments": dict(arguments)}
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()


def hash_policy_snapshot(snapshot: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(dict(snapshot)).encode("utf-8")).hexdigest()


async def build_live_approval_policy_snapshot(
    *,
    db: AsyncSession,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    tool_name: str,
) -> dict[str, Any]:
    """Read the exact live policy inputs whose drift invalidates a ticket."""
    from app.models.guard_policy import GuardPolicy
    from app.services.capability_gate import check_capability

    capability = await check_capability(db, tenant_id, agent_id, tool_name)
    guard_result = await db.execute(select(GuardPolicy).where(GuardPolicy.tenant_id == tenant_id))
    guard = guard_result.scalar_one_or_none()
    return {
        "schema": "hive.approval_policy_snapshot.v1",
        "tenant_id": str(tenant_id),
        "agent_id": str(agent_id),
        "tool_name": str(tool_name),
        "capability": {
            "allowed": bool(getattr(capability, "allowed", False)),
            "denied": bool(getattr(capability, "denied", False)),
            "escalate_to_l3": bool(getattr(capability, "escalate_to_l3", False)),
            "name": str(getattr(capability, "capability", "") or ""),
            "reason": str(getattr(capability, "reason", "") or ""),
            "policy_found": bool(getattr(capability, "policy_found", False)),
        },
        "guard_policy": {
            "version": int(getattr(guard, "version", 0) or 0),
            "zone_guard": dict(getattr(guard, "zone_guard", {}) or {}),
            "egress_guard": dict(getattr(guard, "egress_guard", {}) or {}),
        },
    }


async def validate_approval_execution_authority(
    *,
    db: AsyncSession,
    envelope: ApprovalExecutionEnvelope,
) -> None:
    """Recheck mutable resource/cancellation/budget authority at consume time."""

    from app.agents.delegation_token import validate_delegation_token
    from app.models.chat_session import ChatSession
    from app.models.external_principal import ExternalPrincipal
    from app.models.runtime_budget import RuntimeBudgetRun
    from app.models.runtime_task import RuntimeTask

    identity = envelope.execution_identity
    if identity is not None and identity.identity_type == "external_principal_bound":
        principal = (
            await db.execute(
                select(ExternalPrincipal).where(
                    ExternalPrincipal.id == identity.identity_id,
                    ExternalPrincipal.tenant_id == envelope.tenant_id,
                    ExternalPrincipal.linked_user_id == envelope.requester_user_id,
                    ExternalPrincipal.status == "active",
                )
            )
        ).scalar_one_or_none()
        if principal is None:
            raise ApprovalTicketError("approval external principal binding is no longer valid")

    if envelope.delegation_token is not None:
        token_result = validate_delegation_token(
            envelope.delegation_token,
            child_agent_id=envelope.agent_id,
        )
        if not token_result.valid:
            raise ApprovalTicketError(f"approval delegation authority is no longer valid: {token_result.reason}")

    if envelope.session_id:
        try:
            session_uuid = uuid.UUID(envelope.session_id)
        except ValueError:
            session_uuid = None
        if session_uuid is not None:
            session = (
                await db.execute(
                    select(ChatSession).where(
                        ChatSession.id == session_uuid,
                        ChatSession.tenant_id == envelope.tenant_id,
                        ChatSession.agent_id == envelope.agent_id,
                        ChatSession.user_id == envelope.requester_user_id,
                    )
                )
            ).scalar_one_or_none()
            if session is None:
                raise ApprovalTicketError("approval execution session authority no longer exists")

    if envelope.runtime_task_id:
        try:
            runtime_task_uuid = uuid.UUID(envelope.runtime_task_id)
        except ValueError as exc:
            raise ApprovalTicketError("approval execution runtime task id is invalid") from exc
        runtime_task = (
            await db.execute(
                select(RuntimeTask).where(
                    RuntimeTask.id == runtime_task_uuid,
                    RuntimeTask.tenant_id == envelope.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if runtime_task is None:
            raise ApprovalTicketError("approval execution runtime task no longer exists")
        if envelope.agent_id not in {runtime_task.parent_agent_id, runtime_task.child_agent_id}:
            raise ApprovalTicketError("approval execution runtime task agent mismatch")
        if runtime_task.status == "killed":
            raise ApprovalTicketError("approval execution runtime task was cancelled")

    if envelope.budget_run_id:
        try:
            budget_run_uuid = uuid.UUID(envelope.budget_run_id)
        except ValueError as exc:
            raise ApprovalTicketError("approval execution budget run id is invalid") from exc
        budget_run = (
            await db.execute(
                select(RuntimeBudgetRun).where(
                    RuntimeBudgetRun.id == budget_run_uuid,
                    RuntimeBudgetRun.tenant_id == envelope.tenant_id,
                )
            )
        ).scalar_one_or_none()
        if budget_run is None:
            raise ApprovalTicketError("approval execution budget run no longer exists")
        if budget_run.root_agent_id is not None and budget_run.root_agent_id != envelope.agent_id:
            raise ApprovalTicketError("approval execution budget agent mismatch")
        if budget_run.root_user_id is not None and budget_run.root_user_id != envelope.requester_user_id:
            raise ApprovalTicketError("approval execution budget requester mismatch")
        if budget_run.status not in {"active", "resuming"}:
            raise ApprovalTicketError(f"approval execution budget is not active: {budget_run.status}")


async def consume_approval_ticket(
    *,
    approval_id: uuid.UUID,
    expected_agent_id: uuid.UUID | None,
    expected_user_id: uuid.UUID | None,
) -> ApprovalExecutionTicket:
    """Claim one approved ticket through locator -> tenant-scoped execution."""
    from app.database import async_session, enter_rls_bypass, tenant_scoped_session
    from app.models.audit import ApprovalRequest

    async with async_session() as db:
        async with enter_rls_bypass(db, reason="approval ticket tenant locator") as locator_db:
            located = (
                await locator_db.execute(
                    select(ApprovalRequest.tenant_id, ApprovalRequest.agent_id).where(ApprovalRequest.id == approval_id)
                )
            ).one_or_none()
        await db.rollback()
    if located is None:
        raise ApprovalTicketError("approval ticket not found")
    tenant_id, agent_id = located
    if tenant_id is None:
        raise ApprovalTicketError("approval ticket has no tenant")
    if expected_agent_id is not None and agent_id != expected_agent_id:
        raise ApprovalTicketError("approval ticket agent mismatch")

    async with tenant_scoped_session(tenant_id) as db:
        result = await db.execute(select(ApprovalRequest).where(ApprovalRequest.id == approval_id).with_for_update())
        approval = result.scalar_one_or_none()
        if approval is None:
            raise ApprovalTicketError("approval ticket not visible in tenant")
        if expected_user_id is not None and approval.resolved_by != expected_user_id:
            raise ApprovalTicketError("approval ticket approver mismatch")
        if approval.status != "approved":
            raise ApprovalTicketError(f"approval ticket is not approved: {approval.status}")
        now = datetime.now(timezone.utc)
        if approval.expires_at is None or approval.expires_at <= now:
            raise ApprovalTicketError("approval ticket expired")
        if approval.consumed_at is not None:
            raise ApprovalTicketError("approval ticket already consumed")
        if approval.execution_status not in {"approved", "queued"}:
            raise ApprovalTicketError(
                f"approval ticket execution state requires reapproval: {approval.execution_status}"
            )
        tool_name = str(approval.tool_name or "").strip()
        if not tool_name:
            raise ApprovalTicketError("approval ticket has no tool")
        if (
            tool_name in {"load_skill", "run_skill_tool", "spawn_subagent", "call_mcp_tool"}
            or tool_name.startswith("mcp__")
        ) and (approval.execution_envelope or {}).get("schema") != _APPROVAL_EXECUTION_ENVELOPE_SCHEMA:
            raise ApprovalTicketError("approval ticket asset revision binding requires reapproval")
        arguments = normalize_tool_arguments(approval.normalized_arguments)
        input_hash = hash_tool_input(tool_name, arguments)
        if not approval.input_hash or input_hash != approval.input_hash:
            raise ApprovalTicketError("approval ticket input hash mismatch")
        policy_snapshot = dict(approval.policy_snapshot or {})
        policy_snapshot_hash = hash_policy_snapshot(policy_snapshot)
        if not approval.policy_snapshot_hash or policy_snapshot_hash != approval.policy_snapshot_hash:
            raise ApprovalTicketError("approval ticket policy snapshot mismatch")
        live_policy_snapshot = await build_live_approval_policy_snapshot(
            db=db,
            tenant_id=tenant_id,
            agent_id=approval.agent_id,
            tool_name=tool_name,
        )
        if hash_policy_snapshot(live_policy_snapshot) != approval.policy_snapshot_hash:
            raise ApprovalTicketError("approval ticket policy changed after approval request")
        raw_execution_envelope = approval.execution_envelope
        execution_envelope_hash = str(approval.execution_envelope_hash or "")
        restored_envelope = restore_approval_execution_envelope(
            raw_execution_envelope,
            expected_hash=execution_envelope_hash,
        )
        if (
            restored_envelope.tenant_id != tenant_id
            or restored_envelope.agent_id != approval.agent_id
            or restored_envelope.requester_user_id != approval.requested_by
        ):
            raise ApprovalTicketError("approval execution envelope authority mismatch")
        await validate_approval_execution_authority(db=db, envelope=restored_envelope)
        if approval.requested_by is None or approval.resolved_by is None:
            raise ApprovalTicketError("approval ticket principal binding is incomplete")
        if not str(approval.decision_id or "").strip():
            raise ApprovalTicketError("approval ticket decision binding is incomplete")

        approval.consumed_at = now
        approval.execution_status = "executing"
        idempotency_key = str(approval.execution_idempotency_key or f"approval:{approval.id}")
        approval.execution_idempotency_key = idempotency_key
        await db.flush()
        return ApprovalExecutionTicket(
            approval_id=approval.id,
            tenant_id=tenant_id,
            agent_id=approval.agent_id,
            requested_by_user_id=approval.requested_by,
            approved_by_user_id=approval.resolved_by,
            tool_name=tool_name,
            arguments=arguments,
            input_hash=input_hash,
            policy_snapshot_hash=policy_snapshot_hash,
            idempotency_key=idempotency_key,
            decision_id=str(approval.decision_id),
            action_type=str(approval.action_type),
            execution_envelope=dict(raw_execution_envelope),
            execution_envelope_hash=execution_envelope_hash,
        )


async def complete_approval_ticket(
    *,
    approval_id: uuid.UUID,
    tenant_id: uuid.UUID,
    status: str,
    result: str,
    receipt: dict[str, Any],
) -> None:
    from app.database import tenant_scoped_session
    from app.models.audit import ApprovalRequest

    async with tenant_scoped_session(tenant_id) as db:
        row = await db.get(ApprovalRequest, approval_id, with_for_update=True)
        if row is None or row.consumed_at is None:
            raise ApprovalTicketError("approval ticket completion has no consumed ticket")
        if row.execution_status != "executing":
            raise ApprovalTicketError(f"approval ticket completion lost execution ownership: {row.execution_status}")
        row.execution_status = str(status)
        row.execution_result = str(result)[:20_000]
        row.execution_receipt = dict(receipt)
        await db.flush()


async def reconcile_stuck_approval_tickets(
    *,
    older_than: datetime,
    limit: int = 100,
) -> int:
    """Quarantine crash-window tickets without replaying unknown side effects."""
    from collections import defaultdict

    from app.database import async_session, enter_rls_bypass, tenant_scoped_session
    from app.models.audit import ApprovalRequest

    async with async_session() as db:
        async with enter_rls_bypass(
            db,
            reason="approval ticket execution reconciliation locator",
        ) as locator_db:
            rows = (
                await locator_db.execute(
                    select(ApprovalRequest.id, ApprovalRequest.tenant_id)
                    .where(
                        ApprovalRequest.execution_status == "executing",
                        ApprovalRequest.consumed_at < older_than,
                        ApprovalRequest.tenant_id.is_not(None),
                    )
                    .order_by(ApprovalRequest.consumed_at.asc())
                    .limit(max(1, min(int(limit), 1000)))
                )
            ).all()
        await db.rollback()

    by_tenant: dict[uuid.UUID, list[uuid.UUID]] = defaultdict(list)
    for approval_id, tenant_id in rows:
        if tenant_id is not None:
            by_tenant[tenant_id].append(approval_id)

    reconciled = 0
    for tenant_id, approval_ids in by_tenant.items():
        async with tenant_scoped_session(tenant_id) as db:
            result = await db.execute(
                select(ApprovalRequest)
                .where(
                    ApprovalRequest.id.in_(approval_ids),
                    ApprovalRequest.execution_status == "executing",
                    ApprovalRequest.consumed_at < older_than,
                )
                .with_for_update()
            )
            for row in result.scalars().all():
                receipt = dict(row.execution_receipt or {})
                receipt.update(
                    {
                        "status": "needs_reconciliation",
                        "side_effect_state": "unknown",
                        "automatic_replay": False,
                        "reconciled_at": datetime.now(timezone.utc).isoformat(),
                    }
                )
                row.execution_status = "needs_reconciliation"
                row.execution_receipt = receipt
                reconciled += 1
            await db.flush()
    return reconciled
