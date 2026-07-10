"""Pure, typed final authorization decision for one tool execution."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
import hashlib
import json
import uuid
from typing import Any


class ToolDecisionOutcome(str, Enum):
    ALLOW = "allow"
    ALLOW_PREPARE_ONLY = "allow_prepare_only"
    REQUIRE_APPROVAL = "require_approval"
    DENY = "deny"


def _hash(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class ToolDecision:
    decision_id: str
    tenant_id: str | None
    agent_id: str
    actor_user_id: str
    delegated_by: str | None
    tool_name: str
    input_hash: str
    policy_snapshot_hash: str
    capability_snapshot_hash: str
    outcome: ToolDecisionOutcome
    reason_codes: tuple[str, ...]
    approval_id: str | None = None
    expires_at: datetime | None = None
    consumed_at: datetime | None = None
    runtime_task_id: str | None = None
    session_id: str | None = None
    trace_id: str | None = None
    idempotency_key: str | None = None

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["schema"] = "hive.tool_decision.v1"
        payload["outcome"] = self.outcome.value
        payload["expires_at"] = self.expires_at.isoformat() if self.expires_at else None
        payload["consumed_at"] = self.consumed_at.isoformat() if self.consumed_at else None
        return payload


def build_tool_decision(
    *,
    decision_id: str | None = None,
    tenant_id: Any,
    agent_id: Any,
    actor_user_id: Any,
    tool_name: str,
    arguments: dict[str, Any],
    policy_snapshot: dict[str, Any],
    capability_snapshot: dict[str, Any],
    outcome: ToolDecisionOutcome,
    reason_codes: tuple[str, ...],
    delegated_by: Any | None = None,
    approval_id: Any | None = None,
    runtime_task_id: Any | None = None,
    session_id: Any | None = None,
    trace_id: Any | None = None,
    idempotency_key: str | None = None,
) -> ToolDecision:
    normalized_input = {"tool_name": str(tool_name), "arguments": dict(arguments)}
    return ToolDecision(
        decision_id=str(decision_id or uuid.uuid4()),
        tenant_id=str(tenant_id) if tenant_id is not None else None,
        agent_id=str(agent_id),
        actor_user_id=str(actor_user_id),
        delegated_by=str(delegated_by) if delegated_by is not None else None,
        tool_name=str(tool_name),
        input_hash=_hash(normalized_input),
        policy_snapshot_hash=_hash(dict(policy_snapshot)),
        capability_snapshot_hash=_hash(dict(capability_snapshot)),
        outcome=outcome,
        reason_codes=tuple(dict.fromkeys(str(code) for code in reason_codes if str(code))),
        approval_id=str(approval_id) if approval_id is not None else None,
        runtime_task_id=str(runtime_task_id) if runtime_task_id is not None else None,
        session_id=str(session_id) if session_id is not None else None,
        trace_id=str(trace_id) if trace_id is not None else None,
        idempotency_key=idempotency_key,
    )
