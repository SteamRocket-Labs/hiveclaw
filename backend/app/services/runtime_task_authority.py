"""Canonical root-principal authority for RuntimeTask control surfaces."""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any, Mapping

from app.core.execution_context import ExecutionPrincipal


@dataclass(frozen=True, slots=True)
class RuntimeTaskAuthorityDecision:
    allowed: bool
    action: str
    reason: str
    authority_source: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)


class RuntimeTaskRequesterUnavailable(ValueError):
    """Canonical requester identity is missing, invalid, or internally inconsistent."""

    def __init__(self, *, reason_code: str, evidence: Mapping[str, Any] | None = None) -> None:
        self.reason_code = str(reason_code or "runtime_task_requester_unavailable")
        self.evidence = dict(evidence or {})
        super().__init__(self.reason_code)


def execution_principal_from_tool_context(context: Any) -> ExecutionPrincipal:
    """Build control authority only from runtime-owned tool context."""
    tenant_id = str(getattr(context, "tenant_id", None) or "").strip()
    if not tenant_id:
        raise ValueError("RuntimeTask control requires a tenant-bound tool context")
    carried = getattr(context, "execution_principal", None)
    if carried is not None:
        if isinstance(carried, Mapping):
            carried = ExecutionPrincipal.from_evidence(carried)
        if not isinstance(carried, ExecutionPrincipal):
            raise ValueError("A2A execution principal is invalid")
        if str(carried.tenant_id) != tenant_id:
            raise ValueError("A2A execution principal tenant does not match the tool context")
        if str(carried.source_agent_id) != str(context.agent_id):
            raise ValueError("A2A execution principal source Agent does not match the tool context")
        if str(carried.requester_user_id or "") != str(getattr(context, "user_id", None) or ""):
            raise ValueError("A2A execution principal requester does not match the tool context")
        return carried
    if bool(getattr(context, "authority_frame_required", False)):
        raise ValueError("A2A execution principal is required")
    return ExecutionPrincipal(
        tenant_id=tenant_id,
        source_agent_id=uuid.UUID(str(context.agent_id)),
        requester_user_id=uuid.UUID(str(context.user_id)) if getattr(context, "user_id", None) else None,
        root_session_id=getattr(context, "session_id", None),
        root_runtime_task_id=getattr(context, "runtime_task_id", None),
        origin="runtime_control_tool",
    )


def _metadata(record: Mapping[str, Any]) -> dict[str, Any]:
    raw = record.get("metadata") or record.get("metadata_json")
    return dict(raw) if isinstance(raw, Mapping) else {}


def runtime_task_requester_user_id(record: Mapping[str, Any]) -> uuid.UUID:
    """Return the durable requester from ``runtime_tasks.root_user_id`` only.

    Metadata remains useful consistency evidence, but it is never allowed to
    repair or replace a missing canonical column during worker replay.
    """

    canonical_raw = str(record.get("root_user_id") or "").strip()
    metadata = _metadata(record)
    principal = metadata.get("execution_principal")
    principal = dict(principal) if isinstance(principal, Mapping) else {}
    evidence = {
        "authority_source": "runtime_tasks.root_user_id",
        "task_id": str(record.get("task_id") or record.get("id") or "") or None,
        "root_user_id": canonical_raw or None,
        "metadata_root_user_id": str(metadata.get("root_user_id") or "").strip() or None,
        "principal_requester_user_id": str(principal.get("requester_user_id") or "").strip() or None,
    }
    if not canonical_raw:
        raise RuntimeTaskRequesterUnavailable(reason_code="root_user_id_missing", evidence=evidence)
    try:
        requester_user_id = uuid.UUID(canonical_raw)
    except ValueError as exc:
        raise RuntimeTaskRequesterUnavailable(reason_code="root_user_id_invalid", evidence=evidence) from exc

    for candidate in (evidence["metadata_root_user_id"], evidence["principal_requester_user_id"]):
        if candidate is None:
            continue
        try:
            candidate_user_id = uuid.UUID(candidate)
        except ValueError as exc:
            raise RuntimeTaskRequesterUnavailable(reason_code="root_user_id_mismatch", evidence=evidence) from exc
        if candidate_user_id != requester_user_id:
            raise RuntimeTaskRequesterUnavailable(reason_code="root_user_id_mismatch", evidence=evidence)
    return requester_user_id


def runtime_task_root_authority(record: Mapping[str, Any]) -> dict[str, Any]:
    metadata = _metadata(record)
    principal = metadata.get("execution_principal")
    principal = dict(principal) if isinstance(principal, Mapping) else {}
    chain = record.get("delegation_chain")
    if not isinstance(chain, list):
        chain = metadata.get("delegation_chain")
    normalized_chain = [str(item) for item in chain] if isinstance(chain, list) else []
    return {
        "tenant_id": str(record.get("tenant_id") or principal.get("tenant_id") or "").strip() or None,
        "parent_agent_id": str(record.get("parent_agent_id") or "").strip() or None,
        "root_user_id": str(record.get("root_user_id") or principal.get("requester_user_id") or "").strip() or None,
        "root_session_id": str(
            record.get("root_session_id") or principal.get("root_session_id") or metadata.get("root_session_id") or ""
        ).strip()
        or None,
        "root_runtime_task_id": str(
            record.get("root_runtime_task_id")
            or principal.get("root_runtime_task_id")
            or metadata.get("root_runtime_task_id")
            or ""
        ).strip()
        or None,
        "delegation_chain": normalized_chain,
    }


def _deny(action: str, reason: str, evidence: dict[str, Any]) -> RuntimeTaskAuthorityDecision:
    return RuntimeTaskAuthorityDecision(
        allowed=False,
        action=action,
        reason=reason,
        evidence=evidence,
    )


def authorize_runtime_task_record(
    record: Mapping[str, Any],
    *,
    principal: ExecutionPrincipal,
    action: str,
    allow_operator_override: bool = False,
    operator_user_id: uuid.UUID | str | None = None,
    operator_reason: str | None = None,
    require_root_session: bool = True,
) -> RuntimeTaskAuthorityDecision:
    """Authorize one RuntimeTask read/control action against its root owner.

    Agent access is necessary but never sufficient.  Ordinary tool callers
    must match tenant, parent Agent, root user, root session, and a persisted
    delegation chain.  A control-plane manager can cross root ownership only
    through the explicit reasoned override branch; callers must separately
    persist the returned evidence in their audit transaction.
    """

    normalized_action = str(action or "runtime_task_action").strip() or "runtime_task_action"
    evidence = runtime_task_root_authority(record)
    evidence.update(
        {
            "schema": "hive.runtime_task_authority.v1",
            "task_id": str(record.get("task_id") or record.get("id") or "") or None,
            "action": normalized_action,
            "principal": principal.to_evidence(),
        }
    )

    if evidence["tenant_id"] != str(principal.tenant_id):
        return _deny(normalized_action, "tenant_mismatch", evidence)
    if evidence["parent_agent_id"] != str(principal.source_agent_id):
        return _deny(normalized_action, "parent_agent_mismatch", evidence)

    root_user_id = evidence["root_user_id"]
    root_session_id = evidence["root_session_id"]
    chain = evidence["delegation_chain"]
    if not root_user_id or not root_session_id or not chain:
        if not allow_operator_override:
            return _deny(normalized_action, "root_authority_missing", evidence)

    requester_id = str(principal.requester_user_id or "").strip() or None
    ordinary_match = (
        requester_id is not None
        and root_user_id == requester_id
        and (not require_root_session or root_session_id == principal.root_session_id)
    )
    if ordinary_match:
        return RuntimeTaskAuthorityDecision(
            allowed=True,
            action=normalized_action,
            reason="root_owner_match",
            authority_source="root_owner",
            evidence=evidence,
        )

    if not allow_operator_override:
        reason = "root_user_mismatch" if root_user_id != requester_id else "root_session_mismatch"
        return _deny(normalized_action, reason, evidence)

    reason = str(operator_reason or "").strip()
    operator_id = str(operator_user_id or "").strip()
    if not reason:
        return _deny(normalized_action, "operator_reason_required", evidence)
    if not operator_id:
        return _deny(normalized_action, "operator_identity_required", evidence)
    evidence.update({"operator_user_id": operator_id, "operator_reason": reason})
    return RuntimeTaskAuthorityDecision(
        allowed=True,
        action=normalized_action,
        reason="operator_override",
        authority_source="operator_override",
        evidence=evidence,
    )
