"""Runtime-only Plan Mode trust context.

The Plan Mode functional core intentionally stays pure. This module carries the
small per-tool execution context used when a real runtime has already verified
that a user declined a Plan Mode recommendation. LLM tool arguments must never
be treated as this trust root.
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from typing import Any


_trusted_plan_mode_user_declined: ContextVar[dict[str, Any] | None] = ContextVar(
    "trusted_plan_mode_user_declined",
    default=None,
)
_interactive_plan_mode: ContextVar[dict[str, Any] | None] = ContextVar(
    "interactive_plan_mode",
    default=None,
)


def set_trusted_plan_mode_user_declined(value: bool | dict[str, Any] = True) -> Token[dict[str, Any] | None]:
    """Mark the current async tool execution as carrying a trusted user opt-out."""
    metadata = value if isinstance(value, dict) else ({"trusted": True} if value else None)
    return _trusted_plan_mode_user_declined.set(metadata)


def reset_trusted_plan_mode_user_declined(token: Token[dict[str, Any] | None]) -> None:
    """Restore the previous trusted opt-out context."""
    _trusted_plan_mode_user_declined.reset(token)


def trusted_plan_mode_user_declined() -> bool:
    """Return whether the runtime, not the LLM, verified a user opt-out."""
    return bool(_trusted_plan_mode_user_declined.get())


def trusted_plan_mode_user_decline_metadata() -> dict[str, Any]:
    """Return metadata for the trusted opt-out, such as recommendation id."""
    return dict(_trusted_plan_mode_user_declined.get() or {})


def set_interactive_plan_mode(metadata: dict[str, Any] | None) -> Token[dict[str, Any] | None]:
    """Mark the current async runtime as Claude-style interactive Plan Mode."""
    return _interactive_plan_mode.set(dict(metadata or {}))


def reset_interactive_plan_mode(token: Token[dict[str, Any] | None]) -> None:
    """Restore the previous interactive Plan Mode runtime context."""
    _interactive_plan_mode.reset(token)


def interactive_plan_mode_active() -> bool:
    """Return whether this async runtime is inside interactive Plan Mode."""
    return bool(_interactive_plan_mode.get())


def interactive_plan_mode_metadata() -> dict[str, Any]:
    """Return the active interactive Plan Mode metadata."""
    return dict(_interactive_plan_mode.get() or {})


__all__ = [
    "reset_trusted_plan_mode_user_declined",
    "reset_interactive_plan_mode",
    "set_interactive_plan_mode",
    "set_trusted_plan_mode_user_declined",
    "interactive_plan_mode_active",
    "interactive_plan_mode_metadata",
    "trusted_plan_mode_user_decline_metadata",
    "trusted_plan_mode_user_declined",
]
