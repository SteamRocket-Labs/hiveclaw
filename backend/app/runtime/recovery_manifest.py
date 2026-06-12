"""Lightweight Recovery Manifest for high-fidelity post-compaction restoration.

Captures structured state about what to restore after context compression,
instead of relying solely on natural language summaries. Built from
SessionContext runtime tracking fields.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.services.session_memory import load_session_memory

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RecoveryManifest:
    """Structured record of what to restore after compaction."""

    session_id: str | None = None

    # Files the agent recently read or wrote
    recent_reads: list[str] = field(default_factory=list)
    recent_writes: list[str] = field(default_factory=list)
    file_snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)

    # Tool execution outcomes worth preserving
    recent_tool_outcomes: list[dict[str, str]] = field(default_factory=list)

    # Skills and runtime tool groups currently active
    active_skills: list[str] = field(default_factory=list)
    active_tool_groups: list[str] = field(default_factory=list)

    # External resources referenced
    recent_external_refs: list[str] = field(default_factory=list)

    # Unfinished work
    pending_items: list[str] = field(default_factory=list)

    # Blocked patterns from evolution (do-not-retry list)
    blocked_patterns: list[str] = field(default_factory=list)

    def is_empty(self) -> bool:
        return not any([
            self.recent_reads, self.recent_writes,
            self.recent_tool_outcomes, self.active_skills,
            self.active_tool_groups, self.recent_external_refs,
            self.pending_items, self.blocked_patterns,
        ])

    def to_restoration_text(self, *, budget_chars: int = 20000) -> str:
        """Render manifest as structured text for prompt injection."""
        sections: list[str] = []
        total = 0

        def _add(title: str, items: list[str]) -> None:
            nonlocal total
            if not items or total >= budget_chars:
                return
            block = f"### {title}\n" + "\n".join(f"- {item}" for item in items)
            if total + len(block) < budget_chars:
                sections.append(block)
                total += len(block)

        def _format_file_item(path: str) -> str:
            snapshot = self.file_snapshots.get(path, {})
            version = ""
            if snapshot.get("exists") is True:
                version = f" [size={snapshot.get('size')}, mtime_ns={snapshot.get('mtime_ns')}]"
            elif snapshot.get("exists") is False:
                version = " [missing at last snapshot]"
            return f'{path}{version} — reload with read_file("{path}")'

        _add("Recent Reads", [_format_file_item(path) for path in self.recent_reads[-5:]])
        _add("Recent Writes", [_format_file_item(path) for path in self.recent_writes[-5:]])
        _add("Recent Tool Results", [
            f"{o.get('tool', '?')}: {o.get('summary', '')}"
            for o in self.recent_tool_outcomes[-5:]
        ])
        _add("Active Skills", self.active_skills)
        _add("Active Runtime Tool Groups", self.active_tool_groups)
        _add("External References", self.recent_external_refs[-5:])
        _add("Pending Work", self.pending_items[-5:])
        _add("Blocked Patterns (DO NOT retry)", self.blocked_patterns[-5:])

        if not sections:
            return ""
        return "\n\n".join(sections)


def build_recovery_manifest(session_context: Any) -> RecoveryManifest:
    """Build a RecoveryManifest from the current SessionContext state."""
    if session_context is None:
        return RecoveryManifest()

    tool_group_names = []
    for p in getattr(session_context, "active_tool_groups", []):
        if isinstance(p, dict):
            tool_group_names.append(p.get("name", "?"))

    return RecoveryManifest(
        session_id=getattr(session_context, "session_id", None),
        recent_reads=list(getattr(session_context, "recent_files", [])),
        recent_writes=list(getattr(session_context, "recent_writes", [])),
        file_snapshots=dict(getattr(session_context, "file_snapshots", {}) or {}),
        recent_tool_outcomes=list(getattr(session_context, "recent_tool_outcomes", [])),
        active_skills=list(getattr(session_context, "active_skills", [])),
        active_tool_groups=tool_group_names,
        recent_external_refs=list(getattr(session_context, "recent_external_refs", [])),
        pending_items=list(getattr(session_context, "pending_items", [])),
    )


def merge_session_memory_into_manifest(
    manifest: RecoveryManifest,
    *,
    agent_id: Any,
    data_root: str | Path | None = None,
) -> RecoveryManifest:
    """Merge structured session-memory artifact into the recovery manifest."""
    try:
        payload = load_session_memory(agent_id, data_root=data_root)
    except Exception as exc:
        logger.warning("Failed to load session memory for recovery manifest: %s", exc)
        payload = None
    if payload is None:
        return manifest

    for item in payload.important_files:
        if item not in manifest.recent_reads:
            manifest.recent_reads.append(item)
    for item in payload.pending_work:
        if item not in manifest.pending_items:
            manifest.pending_items.append(item)
    if payload.current_state:
        combined_summary = payload.current_state
        if payload.key_results:
            combined_summary = f"{combined_summary}\nKey Results: {'; '.join(payload.key_results)}"
        manifest.recent_tool_outcomes.append({"tool": "session_memory", "summary": combined_summary})
    for item in payload.key_results:
        manifest.recent_tool_outcomes.append({"tool": "session_memory:key_result", "summary": item})
    if payload.last_successful_step and payload.last_successful_step != payload.current_state:
        manifest.recent_tool_outcomes.append(
            {"tool": "session_memory:last_successful_step", "summary": payload.last_successful_step}
        )
    return manifest
