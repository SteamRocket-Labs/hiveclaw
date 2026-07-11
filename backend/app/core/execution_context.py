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
from typing import Any, Mapping


@dataclass(frozen=True)
class ExecutionIdentity:
    """Represents who triggered/is responsible for the current agent action."""

    identity_type: str  # "agent_bot" | "delegated_user"
    identity_id: uuid.UUID | None  # user UUID for delegated_user, agent UUID for agent_bot
    label: str  # human-readable label, e.g. "张三 via Feishu" or "Agent: 小智 (trigger)"


@dataclass(frozen=True, slots=True)
class ExecutionPrincipal:
    """Immutable authority carried from an entrypoint into child runtimes.

    ``ExecutionIdentity`` describes how an action is represented in generic
    audit events.  ``ExecutionPrincipal`` is the stricter A2A authority
    envelope: it binds the authenticated requester, source Agent, tenant, and
    root run/session so a child execution cannot silently fall back to the
    Agent's historical creator.
    """

    tenant_id: uuid.UUID | str
    source_agent_id: uuid.UUID
    requester_user_id: uuid.UUID | None
    root_session_id: str | None = None
    root_runtime_task_id: str | None = None
    origin: str = "agent_tool"
    delegation_chain: tuple[str, ...] = ()

    SCHEMA = "hive.execution_principal.v1"

    def __post_init__(self) -> None:
        if not str(self.origin or "").strip():
            raise ValueError("execution principal origin is required")
        object.__setattr__(self, "root_session_id", self._clean_optional(self.root_session_id))
        object.__setattr__(self, "root_runtime_task_id", self._clean_optional(self.root_runtime_task_id))
        object.__setattr__(
            self,
            "delegation_chain",
            tuple(str(item).strip() for item in self.delegation_chain if str(item).strip()),
        )

    @staticmethod
    def _clean_optional(value: Any) -> str | None:
        normalized = str(value or "").strip()
        return normalized or None

    def to_evidence(self) -> dict[str, Any]:
        return {
            "schema": self.SCHEMA,
            "tenant_id": str(self.tenant_id),
            "source_agent_id": str(self.source_agent_id),
            "requester_user_id": str(self.requester_user_id) if self.requester_user_id else None,
            "root_session_id": self.root_session_id,
            "root_runtime_task_id": self.root_runtime_task_id,
            "origin": self.origin,
            "delegation_chain": list(self.delegation_chain),
        }

    @classmethod
    def from_evidence(cls, payload: Mapping[str, Any] | None) -> "ExecutionPrincipal | None":
        if not payload:
            return None
        if payload.get("schema") not in (None, cls.SCHEMA):
            raise ValueError("unsupported execution principal schema")
        requester = payload.get("requester_user_id")
        chain = payload.get("delegation_chain")
        if chain is None:
            normalized_chain: tuple[str, ...] = ()
        elif isinstance(chain, (list, tuple)):
            normalized_chain = tuple(str(item) for item in chain)
        else:
            raise ValueError("execution principal delegation_chain must be a list")
        tenant_raw = str(payload["tenant_id"] or "").strip()
        if not tenant_raw:
            raise ValueError("execution principal tenant_id is required")
        try:
            tenant_id: uuid.UUID | str = uuid.UUID(tenant_raw)
        except ValueError:
            tenant_id = tenant_raw
        return cls(
            tenant_id=tenant_id,
            source_agent_id=uuid.UUID(str(payload["source_agent_id"])),
            requester_user_id=uuid.UUID(str(requester)) if requester else None,
            root_session_id=payload.get("root_session_id"),
            root_runtime_task_id=payload.get("root_runtime_task_id"),
            origin=str(payload.get("origin") or "agent_tool"),
            delegation_chain=normalized_chain,
        )

    def assert_scope(self, *, tenant_id: uuid.UUID | str | None, source_agent_id: uuid.UUID | str) -> None:
        if str(self.source_agent_id) != str(source_agent_id):
            raise ValueError("execution principal source Agent does not match the caller")
        if tenant_id is None or str(self.tenant_id) != str(tenant_id):
            raise ValueError("execution principal tenant does not match the source Agent")


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


# ContextVar — current tenant for tool config isolation
_current_tool_tenant_id: ContextVar[uuid.UUID | None] = ContextVar("tool_tenant_id", default=None)


def set_tool_tenant_id(tenant_id: uuid.UUID | None) -> None:
    """Set the tenant_id for tool config resolution in the current async context."""
    _current_tool_tenant_id.set(tenant_id)


def get_tool_tenant_id() -> uuid.UUID | None:
    """Get the current tenant_id for tool config resolution."""
    return _current_tool_tenant_id.get()


def set_delegated_user_identity(user_id: uuid.UUID, user_name: str, channel: str = "feishu") -> None:
    """Convenience: set identity as user-delegated action."""
    set_execution_identity(
        ExecutionIdentity(
            identity_type="delegated_user",
            identity_id=user_id,
            label=f"{user_name} via {channel}",
        )
    )
