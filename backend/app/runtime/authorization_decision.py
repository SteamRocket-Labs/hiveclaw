"""Unified authorization decision entry for runtime governance surfaces."""

from __future__ import annotations

from typing import Any

AUTHORIZATION_DECISION_SCHEMA = "hive.ccplus.authorization_decision.v1"


def _text(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _reason_text(reason: Any) -> str:
    if isinstance(reason, (list, tuple, set)):
        return ",".join(str(item) for item in reason if str(item or "").strip())
    return str(reason or "").strip()


def build_authorization_decision_entry(
    *,
    resource: Any,
    action: Any,
    result: Any,
    reason: Any = None,
    principal: Any = None,
    company: Any = None,
    sensitivity: Any = None,
    policy: Any = None,
    model_visible_message: Any = None,
    source: str = "runtime",
) -> dict[str, Any]:
    return {
        "schema": AUTHORIZATION_DECISION_SCHEMA,
        "resource": _text(resource),
        "action": _text(action),
        "principal": _text(principal),
        "company": _text(company),
        "sensitivity": _text(sensitivity),
        "policy": _text(policy),
        "result": _text(result),
        "reason": _reason_text(reason) or None,
        "model_visible_message": _text(model_visible_message),
        "source": source,
    }
