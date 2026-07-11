"""Trusted execution context carried from a claimed inbox event into a channel run."""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator
import uuid


@dataclass(slots=True)
class ChannelIngressExecutionContext:
    event_id: uuid.UUID
    tenant_id: uuid.UUID
    agent_id: uuid.UUID
    provider: str = ""
    installation_ref: str = ""
    external_principal_id: uuid.UUID | None = None
    runtime_task_id: uuid.UUID | None = None
    session_id: uuid.UUID | None = None


_current_channel_ingress: ContextVar[ChannelIngressExecutionContext | None] = ContextVar(
    "current_channel_ingress",
    default=None,
)


def current_channel_ingress_context() -> ChannelIngressExecutionContext | None:
    return _current_channel_ingress.get()


@contextmanager
def use_channel_ingress_context(
    *,
    event_id: uuid.UUID,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    provider: str = "",
    installation_ref: str = "",
) -> Iterator[ChannelIngressExecutionContext]:
    context = ChannelIngressExecutionContext(
        event_id=event_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        provider=str(provider or "").strip().lower(),
        installation_ref=str(installation_ref or "").strip(),
    )
    token = _current_channel_ingress.set(context)
    try:
        yield context
    finally:
        _current_channel_ingress.reset(token)


def bind_channel_ingress_runtime_result(
    *,
    runtime_task_id: uuid.UUID | str,
    session_id: uuid.UUID | str,
) -> None:
    context = current_channel_ingress_context()
    if context is None:
        return
    context.runtime_task_id = uuid.UUID(str(runtime_task_id))
    context.session_id = uuid.UUID(str(session_id))


def bind_channel_ingress_external_principal(external_principal_id: uuid.UUID | str) -> None:
    context = current_channel_ingress_context()
    if context is None:
        return
    context.external_principal_id = uuid.UUID(str(external_principal_id))


__all__ = [
    "ChannelIngressExecutionContext",
    "bind_channel_ingress_runtime_result",
    "bind_channel_ingress_external_principal",
    "current_channel_ingress_context",
    "use_channel_ingress_context",
]
