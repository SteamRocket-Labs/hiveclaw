"""Shared runtime failure policy classification."""

from __future__ import annotations

from typing import Any


_DEFAULTS: dict[str, dict[str, Any]] = {
    "provider_error": {
        "retryable": False,
        "side_effect_risk": "none",
        "requires_user": True,
        "requires_reconciliation": False,
        "safe_to_continue": False,
    },
    "tool_failure": {
        "retryable": True,
        "side_effect_risk": "unknown",
        "requires_user": False,
        "requires_reconciliation": False,
        "safe_to_continue": True,
    },
    "hook_block": {
        "retryable": False,
        "side_effect_risk": "external_action_blocked",
        "requires_user": True,
        "requires_reconciliation": False,
        "safe_to_continue": False,
    },
    "preflight_denied": {
        "retryable": False,
        "side_effect_risk": "external_action_blocked",
        "requires_user": True,
        "requires_reconciliation": False,
        "safe_to_continue": False,
    },
    "workflow_leaf_failure": {
        "retryable": False,
        "side_effect_risk": "external_effect_unknown",
        "requires_user": True,
        "requires_reconciliation": True,
        "safe_to_continue": False,
    },
    "subagent_replay_blocker": {
        "retryable": False,
        "side_effect_risk": "unknown",
        "requires_user": True,
        "requires_reconciliation": True,
        "safe_to_continue": False,
    },
    "trigger_preflight_skip": {
        "retryable": True,
        "side_effect_risk": "none",
        "requires_user": False,
        "requires_reconciliation": False,
        "safe_to_continue": True,
    },
    "cancelled": {
        "retryable": False,
        "side_effect_risk": "unknown",
        "requires_user": False,
        "requires_reconciliation": False,
        "safe_to_continue": False,
    },
}


def build_runtime_failure_policy(
    *,
    failure_kind: str,
    message: str = "",
    retryable: bool | None = None,
    side_effect_risk: str | None = None,
    requires_user: bool | None = None,
    requires_reconciliation: bool | None = None,
    safe_to_continue: bool | None = None,
    model_visible_summary: str | None = None,
) -> dict[str, Any]:
    kind = str(failure_kind or "unknown")
    defaults = dict(_DEFAULTS.get(kind, {}))
    summary = model_visible_summary or (str(message or "").strip()[:500] or kind)
    return {
        "schema": "hive.ccplus.runtime_failure_policy.v1",
        "failure_kind": kind,
        "retryable": bool(defaults.get("retryable", False) if retryable is None else retryable),
        "side_effect_risk": str(side_effect_risk or defaults.get("side_effect_risk") or "unknown"),
        "requires_user": bool(defaults.get("requires_user", False) if requires_user is None else requires_user),
        "requires_reconciliation": bool(
            defaults.get("requires_reconciliation", False)
            if requires_reconciliation is None
            else requires_reconciliation
        ),
        "safe_to_continue": bool(
            defaults.get("safe_to_continue", False) if safe_to_continue is None else safe_to_continue
        ),
        "model_visible_summary": summary,
    }
