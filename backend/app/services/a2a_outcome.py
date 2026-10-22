"""Typed terminal outcomes for Agent-to-Agent operations."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Literal

A2AOperation = Literal["consult", "delegate", "notify"]


def queued_delegation_outcome(*, handle, target_agent_id, child_session_id, arguments):
    """Render the native admission facts shared by the emitter and its receipt reader."""
    return A2AOutcome.success(operation='delegate', payload={
        'task_id': handle.task_id, 'runtime_task_id': handle.task_id,
        'session_id': child_session_id, 'child_session_id': child_session_id,
        'status': getattr(handle, 'status', 'running'), 'target_agent': handle.target_name, 'target_agent_id': str(target_agent_id),
        'target_artifact_path': str(arguments.get('target_artifact_path') or '').strip() or None,
        'target_artifacts': arguments.get('target_artifacts') if isinstance(arguments.get('target_artifacts'), list) else [],
        'edit_mode': str(arguments.get('edit_mode') or '').strip() or None,
        'trace_id': handle.trace_id, 'receipt': getattr(handle, 'receipt', None),
        'continuation_tool': 'send_agent_session_message',
        'next_action': 'Use send_agent_session_message with child_session_id to continue this delegated agent session; use check_async_task only for runtime status.',
    })


@dataclass(frozen=True, slots=True)
class A2AOutcome:
    """One machine-readable truth for synchronous and asynchronous A2A calls."""

    ok: bool
    operation: A2AOperation
    status: str
    message: str
    payload: dict[str, Any] = field(default_factory=dict)
    error_code: str | None = None
    retryable: bool = False

    @classmethod
    def success(
        cls,
        *,
        operation: A2AOperation,
        payload: dict[str, Any],
        message: str | None = None,
    ) -> "A2AOutcome":
        status = str(payload.get("status") or ("completed" if operation == "consult" else "running"))
        return cls(
            ok=True,
            operation=operation,
            status=status,
            message=str(message or payload.get("message") or ""),
            payload=dict(payload),
        )

    @classmethod
    def failure(
        cls,
        *,
        operation: A2AOperation,
        error_code: str,
        message: str,
        retryable: bool = False,
        status: str = "failed",
        payload: dict[str, Any] | None = None,
    ) -> "A2AOutcome":
        return cls(
            ok=False,
            operation=operation,
            status=status,
            message=str(message).strip(),
            payload=dict(payload or {}),
            error_code=error_code,
            retryable=retryable,
        )

    def to_payload(self) -> dict[str, Any]:
        value = dict(self.payload)
        value["ok"] = self.ok
        value.setdefault("operation", self.operation)
        value["status"] = self.status
        if self.message:
            value.setdefault("message", self.message)
        if self.error_code:
            value["error_code"] = self.error_code
        if not self.ok:
            value["retryable"] = self.retryable
        return value

    def to_tool_result(self) -> str:
        """Render the compatibility string expected by the LLM tool surface."""
        if self.ok:
            return json.dumps(self.to_payload(), ensure_ascii=False, default=str)
        marker = "⚠️" if self.retryable else "❌"
        return f"{marker} {self.message}"
