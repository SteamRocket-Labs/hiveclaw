"""Machine-readable return contract for session-local subagent tools."""

from __future__ import annotations

from typing import Any

SUBAGENT_RETURN_CONTRACT_SCHEMA = "hive.ccplus.subagent_return_contract.v1"
INLINE_RESULT = "inline_result"
BACKGROUND_COMPLETION_WAKE = "background_completion_wake"
NEEDS_RECONCILIATION = "needs_reconciliation"
_VALID_RETURN_CONTRACTS = {INLINE_RESULT, BACKGROUND_COMPLETION_WAKE, NEEDS_RECONCILIATION}


def normalize_subagent_return_contract(value: Any, *, status: Any = None) -> str:
    raw = str(value or "").strip()
    status_value = str(status or "").strip().lower()
    if status_value == NEEDS_RECONCILIATION:
        return NEEDS_RECONCILIATION
    if raw in _VALID_RETURN_CONTRACTS:
        return raw
    return BACKGROUND_COMPLETION_WAKE


def build_subagent_return_contract(
    return_contract: Any,
    *,
    run_id: Any = None,
    child_session_id: Any = None,
    parent_session_id: Any = None,
    status: Any = None,
    source_tool: str = "spawn_subagent",
    fallback_tool: str = "check_subagent",
) -> dict[str, Any]:
    contract = normalize_subagent_return_contract(return_contract, status=status)
    result_visibility = {
        INLINE_RESULT: "current_tool_result",
        BACKGROUND_COMPLETION_WAKE: "future_completion_wake",
        NEEDS_RECONCILIATION: "manual_reconciliation_required",
    }[contract]
    normal_wait_path = "completion_wake" if contract == BACKGROUND_COMPLETION_WAKE else None
    parent_next_step = {
        INLINE_RESULT: "consume_tool_result_now",
        BACKGROUND_COMPLETION_WAKE: "continue_parent_work_and_wait_for_completion_wake",
        NEEDS_RECONCILIATION: "inspect_reconciliation_reason_before_retry",
    }[contract]
    return {
        "schema": SUBAGENT_RETURN_CONTRACT_SCHEMA,
        "return_contract": contract,
        "source_tool": source_tool,
        "status": str(status or "").strip() or None,
        "run_id": str(run_id or "").strip() or None,
        "child_session_id": str(child_session_id or "").strip() or None,
        "parent_session_id": str(parent_session_id or "").strip() or None,
        "result_visibility": result_visibility,
        "normal_wait_path": normal_wait_path,
        "fallback_tool": fallback_tool,
        "busy_poll_allowed": False,
        "completion_wake_expected": contract == BACKGROUND_COMPLETION_WAKE,
        "requires_reconciliation": contract == NEEDS_RECONCILIATION,
        "parent_next_step": parent_next_step,
    }


def subagent_return_contract_from_metadata(
    metadata: dict[str, Any] | None,
    *,
    status: Any = None,
    run_id: Any = None,
    child_session_id: Any = None,
    parent_session_id: Any = None,
) -> dict[str, Any]:
    data = dict(metadata or {})
    existing = data.get("subagent_return_contract")
    contract_value = data.get("return_contract")
    if isinstance(existing, dict) and existing.get("schema") == SUBAGENT_RETURN_CONTRACT_SCHEMA:
        contract_value = existing.get("return_contract") or contract_value
        run_id = run_id or existing.get("run_id")
        child_session_id = child_session_id or existing.get("child_session_id")
        parent_session_id = parent_session_id or existing.get("parent_session_id")
        status = status or existing.get("status")
    return build_subagent_return_contract(
        contract_value,
        run_id=run_id,
        child_session_id=child_session_id or data.get("child_session_id"),
        parent_session_id=parent_session_id or data.get("parent_session_id"),
        status=status,
    )
