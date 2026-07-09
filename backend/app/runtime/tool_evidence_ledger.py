"""Structured per-turn tool evidence ledger."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Any


_KNOWN_STATUSES = {
    "model_requested",
    "args_malformed",
    "running",
    "done",
    "completed",
    "failed",
    "timeout",
    "denied",
    "blocked",
    "terminal_pause",
    "replay_repair",
}


def _normalize_status(value: Any) -> str:
    status = str(value or "completed").strip().lower()
    return status if status in _KNOWN_STATUSES else status or "completed"


@dataclass(slots=True)
class ToolEvidenceLedger:
    """Collects the tool facts that a final answer is allowed to cite."""

    events: list[dict[str, Any]] = field(default_factory=list)

    @classmethod
    def from_parts(cls, parts: list[dict[str, Any]] | tuple[dict[str, Any], ...] | None) -> "ToolEvidenceLedger":
        ledger = cls()
        for part in parts or []:
            if not isinstance(part, dict) or part.get("type") != "tool_call":
                continue
            ledger.record_event(
                tool_name=str(part.get("name") or "unknown"),
                status=_normalize_status(part.get("status")),
                result=part.get("result"),
                args=part.get("args"),
                source="chat_part",
            )
        return ledger

    def record_event(
        self,
        *,
        tool_name: str,
        status: str,
        result: Any = None,
        args: Any = None,
        reason: str | None = None,
        tool_call_id: str | None = None,
        source: str = "runtime",
    ) -> None:
        event = {
            "tool_name": str(tool_name or "unknown"),
            "status": _normalize_status(status),
            "source": source,
        }
        if tool_call_id:
            event["tool_call_id"] = str(tool_call_id)
        if args is not None:
            event["args"] = args
        if result is not None:
            event["result_preview"] = str(result)[:500]
        if reason:
            event["reason"] = str(reason)
        self.events.append(event)

    def record_replay_repair(self, *, tool_name: str = "unknown", reason: str) -> None:
        self.record_event(tool_name=tool_name, status="replay_repair", reason=reason, source="history_replay")

    def has_evidence_for(self, tool_name: str) -> bool:
        needle = str(tool_name or "").strip()
        return bool(needle and any(event.get("tool_name") == needle for event in self.events))

    def to_summary(self) -> dict[str, Any]:
        status_counts = Counter(str(event.get("status") or "unknown") for event in self.events)
        tool_names = sorted({str(event.get("tool_name") or "unknown") for event in self.events})
        return {
            "schema": "hive.ccplus.tool_evidence_ledger.v1",
            "has_tool_evidence": bool(self.events),
            "tool_names": tool_names,
            "status_counts": dict(sorted(status_counts.items())),
            "events": list(self.events),
        }
