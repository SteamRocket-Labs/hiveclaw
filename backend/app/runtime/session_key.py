"""Unified session identity contract for runtime entrypoints."""

from __future__ import annotations

import uuid
from dataclasses import asdict, dataclass

from app.runtime.session import SessionContext


@dataclass(frozen=True, slots=True)
class SessionKey:
    stable_id: str
    source: str
    channel: str | None
    agent_id: str | None
    tenant_id: str | None = None
    external_conv_id: str | None = None
    objective_id: str | None = None
    runtime_task_id: str | None = None
    trace_id: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def _string_or_none(value) -> str | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value.hex
    text = str(value).strip()
    return text or None


def build_session_key(
    *,
    agent_id,
    source: str,
    channel: str | None = None,
    tenant_id=None,
    external_conv_id: str | None = None,
    objective_id: str | None = None,
    runtime_task_id: str | None = None,
    trace_id: str | None = None,
) -> SessionKey:
    normalized_source = (source or "runtime").strip() or "runtime"
    normalized_channel = (channel or "default").strip() or "default"
    normalized_agent_id = _string_or_none(agent_id)
    normalized_tenant_id = _string_or_none(tenant_id)
    normalized_external = _string_or_none(external_conv_id)
    normalized_objective = _string_or_none(objective_id)
    normalized_runtime_task = _string_or_none(runtime_task_id)
    normalized_trace = _string_or_none(trace_id)

    if normalized_objective:
        stable_id = f"objective:{normalized_objective}"
    elif normalized_runtime_task:
        stable_id = f"{normalized_source}:{normalized_channel}:runtime:{normalized_runtime_task}"
    elif normalized_external:
        stable_id = f"{normalized_source}:{normalized_channel}:{normalized_external}"
    elif normalized_agent_id:
        stable_id = f"{normalized_source}:{normalized_channel}:agent:{normalized_agent_id}"
    else:
        stable_id = f"{normalized_source}:{normalized_channel}:anonymous"

    return SessionKey(
        stable_id=stable_id,
        source=normalized_source,
        channel=normalized_channel,
        agent_id=normalized_agent_id,
        tenant_id=normalized_tenant_id,
        external_conv_id=normalized_external,
        objective_id=normalized_objective,
        runtime_task_id=normalized_runtime_task,
        trace_id=normalized_trace,
    )


def ensure_session_key(session_context: SessionContext, key: SessionKey) -> SessionContext:
    session_context.session_id = session_context.session_id or key.stable_id
    if key.objective_id:
        session_context.session_id = key.stable_id
    metadata = session_context.metadata if isinstance(session_context.metadata, dict) else {}
    metadata["session_key"] = key.to_dict()
    session_context.metadata = metadata
    return session_context
