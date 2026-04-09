"""Execution Identity context — tracks WHO is executing an agent action.

Two identity types:
  - agent_bot: Agent acting autonomously (triggers, heartbeat, scheduled tasks)
  - delegated_user: Agent acting on behalf of a specific user (Feishu message, web chat)

The identity is set at the entry point (channel handler, trigger daemon, heartbeat)
and read by write_audit_event() to populate execution_identity_* columns.
"""

import uuid
from contextvars import ContextVar
from dataclasses import dataclass


@dataclass(frozen=True)
class ExecutionIdentity:
    """Represents who triggered/is responsible for the current agent action."""

    identity_type: str  # "agent_bot" | "delegated_user"
    identity_id: uuid.UUID | None  # user UUID for delegated_user, agent UUID for agent_bot
    label: str  # human-readable label, e.g. "张三 via Feishu" or "Agent: 小智 (trigger)"


# ContextVar — set once at request/task entry point, read by audit layer
_current_execution_identity: ContextVar[ExecutionIdentity | None] = ContextVar("execution_identity", default=None)


def set_execution_identity(identity: ExecutionIdentity) -> None:
    """Set the execution identity for the current async context."""
    _current_execution_identity.set(identity)


def get_execution_identity() -> ExecutionIdentity | None:
    """Get the execution identity for the current async context."""
    return _current_execution_identity.get()


def clear_execution_identity() -> None:
    """Clear the execution identity (reset to None)."""
    _current_execution_identity.set(None)


def set_agent_bot_identity(agent_id: uuid.UUID, agent_name: str, source: str = "trigger") -> None:
    """Convenience: set identity as autonomous agent action."""
    set_execution_identity(
        ExecutionIdentity(
            identity_type="agent_bot",
            identity_id=agent_id,
            label=f"Agent: {agent_name} ({source})",
        )
    )


def set_delegated_user_identity(user_id: uuid.UUID, user_name: str, channel: str = "feishu") -> None:
    """Convenience: set identity as user-delegated action."""
    set_execution_identity(
        ExecutionIdentity(
            identity_type="delegated_user",
            identity_id=user_id,
            label=f"{user_name} via {channel}",
        )
    )
