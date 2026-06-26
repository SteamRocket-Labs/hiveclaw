"""Visibility rules for Hive enterprise approval surfaces.

CCPlus session tool permissions are resolved inside the chat session. Historical
rows created before that boundary was enforced must not appear in enterprise
approval queues or be resolved through enterprise approval APIs.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import and_, not_
from sqlalchemy.sql.elements import ColumnElement

SESSION_TOOL_APPROVAL_ORIGIN_TYPES = frozenset({"agent_session", "approval_request"})


def _details_from_approval(approval: Any) -> Mapping[str, Any]:
    details = getattr(approval, "details", None)
    return details if isinstance(details, Mapping) else {}


def is_session_tool_approval(approval: Any) -> bool:
    """Return True for old tool-permission approvals that belong to session UX."""

    details = _details_from_approval(approval)
    origin = details.get("origin")
    origin_type = origin.get("type") if isinstance(origin, Mapping) else None
    return bool(details.get("tool")) and origin_type in SESSION_TOOL_APPROVAL_ORIGIN_TYPES


def is_visible_enterprise_approval(approval: Any) -> bool:
    """Return whether an ApprovalRequest should be exposed in enterprise surfaces."""

    return not is_session_tool_approval(approval)


def enterprise_visible_approval_filter(model: Any) -> ColumnElement[bool]:
    """SQLAlchemy predicate matching approval rows that belong to enterprise flows."""

    tool_name = model.details["tool"].as_string()
    origin_type = model.details["origin"]["type"].as_string()
    return not_(and_(tool_name.is_not(None), origin_type.in_(SESSION_TOOL_APPROVAL_ORIGIN_TYPES)))
