"""Shared runtime outcome contract."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True, slots=True)
class RuntimeOutcome:
    status: str
    terminal_reason: str | None
    next_action: str
    reason: str = ""
    details: dict[str, Any] = field(default_factory=dict)

    def to_event(self) -> dict[str, Any]:
        return {
            "schema": "hive.ccplus.runtime_outcome.v1",
            "status": self.status,
            "terminal_reason": self.terminal_reason,
            "next_action": self.next_action,
            "reason": self.reason,
            "details": dict(self.details),
        }
