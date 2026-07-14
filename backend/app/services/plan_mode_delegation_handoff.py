"""Plan Mode delegation handoff: confirmed plan -> async agent delegation."""

from __future__ import annotations

import uuid
from typing import Any

from app.services.plan_authorization_lease import require_active_plan_authorization

DELEGATION_HANDOFF_TARGET = "delegation"


def _handoff_payload(plan: Any) -> dict[str, Any]:
    plan_json = plan.plan_json or {}
    handoff = plan_json.get("handoff") if isinstance(plan_json, dict) else {}
    payload = handoff.get("payload") if isinstance(handoff, dict) else None
    if not isinstance(payload, dict):
        raise ValueError("Delegation plan is missing handoff.payload")
    return dict(payload)


def _optional_uuid(value: Any) -> uuid.UUID | None:
    if value in (None, ""):
        return None
    return uuid.UUID(str(value))


def _optional_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Delegation handoff max_tool_rounds must be an integer") from exc


def _timeout_seconds(value: Any) -> float:
    if value in (None, ""):
        return 120.0
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("Delegation handoff timeout_seconds must be numeric") from exc


def _clean_error(value: str) -> str:
    return str(value or "").strip() or "delegation target resolution failed"


async def delegation_handoff_handler(_db: Any, plan: Any) -> dict[str, Any]:
    """Start the async delegation authorized by a confirmed Plan Mode plan."""
    if getattr(plan, "status", None) != "confirmed":
        raise ValueError(f"delegation handoff requires confirmed plan (status={getattr(plan, 'status', None)!r})")

    payload = _handoff_payload(plan)
    target_agent_id = _optional_uuid(payload.get("target_agent_id"))
    agent_name = str(payload.get("agent_name") or payload.get("target_agent") or "").strip()
    message = str(
        payload.get("message")
        or payload.get("instructions")
        or (plan.plan_json or {}).get("objective")
        or getattr(plan, "original_request", "")
    ).strip()

    if target_agent_id is None and not agent_name:
        raise ValueError("Delegation handoff requires agent_name or target_agent_id")
    if not message:
        raise ValueError("Delegation handoff requires message")

    from app.services.agent_tool_domains.messaging import _resolve_target_agent_runtime

    source_agent, target, target_model, error = await _resolve_target_agent_runtime(
        plan.agent_id,
        agent_name,
        target_agent_id=target_agent_id,
    )
    if error:
        raise ValueError(_clean_error(error))
    if source_agent is None or target is None or target_model is None:
        raise ValueError("Delegation handoff target runtime could not be resolved")

    user_id = getattr(plan, "confirmed_by_user_id", None) or getattr(plan, "requested_by_user_id", None)
    if user_id is None:
        raise ValueError("Delegation handoff requires a confirmed user id")

    from app.agents.orchestrator import OrchestrationPolicy, delegate_async
    from app.core.execution_context import ExecutionPrincipal

    tool_profile = str(payload.get("tool_profile") or "worker_safe").strip() or "worker_safe"
    plan_authorization = require_active_plan_authorization(plan)
    execution_principal = ExecutionPrincipal(
        tenant_id=plan.tenant_id,
        source_agent_id=plan.agent_id,
        requester_user_id=user_id,
        root_session_id=payload.get("parent_session_id") or getattr(plan, "session_id", None),
        root_runtime_task_id=plan_authorization.get("runtime_task_id"),
        origin="confirmed_plan_handoff",
    )
    handle = await delegate_async(
        target=target,
        target_model=target_model,
        conversation_messages=[
            {
                "role": "user",
                "content": f"[Plan Mode confirmed delegation] {message}",
            }
        ],
        owner_id=user_id,
        session_id=uuid.uuid4().hex,
        parent_agent_id=plan.agent_id,
        parent_session_id=payload.get("parent_session_id") or getattr(plan, "session_id", None),
        max_tool_rounds=_optional_int(payload.get("max_tool_rounds")),
        policy=OrchestrationPolicy(
            timeout_seconds=_timeout_seconds(payload.get("timeout_seconds")),
            tool_profile=tool_profile,
        ),
        tenant_id=getattr(plan, "tenant_id", None),
        confirmed_plan_id=plan.id,
        confirmed_plan_version=plan.plan_version,
        confirmed_plan_hash=plan.plan_hash,
        confirmed_plan_session_id=getattr(plan, "session_id", None),
        plan_authorization=plan_authorization,
        execution_principal=execution_principal.to_evidence(),
        root_runtime_task_id=execution_principal.root_runtime_task_id,
    )
    if str(getattr(handle, "status", "")).startswith("plan_required"):
        raise ValueError(f"Delegation handoff was blocked by Plan Mode: {handle.status}")
    return {
        "runtime_task_id": handle.task_id,
        "trace_id": handle.trace_id,
        "target_agent": handle.target_name,
        "status": handle.status,
    }


def register_delegation_handoff(service: Any) -> None:
    service.register_handoff_handler(DELEGATION_HANDOFF_TARGET, delegation_handoff_handler)


__all__ = [
    "DELEGATION_HANDOFF_TARGET",
    "delegation_handoff_handler",
    "register_delegation_handoff",
]
