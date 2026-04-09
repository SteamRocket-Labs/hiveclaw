"""Explicit session context types for runtime entrypoints."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class SessionContext:
    session_id: str | None = None
    source: str = "runtime"
    channel: str | None = None
    active_packs: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    # Prompt cache: frozen prefix reused within the same session
    prompt_prefix: str | None = None
    prompt_fingerprint: str | None = None
    # Memory hash for cache invalidation — rebuilt when memory context changes
    _memory_hash: str | None = None
    # Post-compact restoration: track session runtime events
    recent_files: list[str] = field(default_factory=list)  # file paths read by agent
    active_skills: list[str] = field(default_factory=list)  # skill names loaded via load_skill
    recent_writes: list[str] = field(default_factory=list)  # file paths written by agent
    recent_tool_outcomes: list[dict[str, str]] = field(default_factory=list)  # [{tool, summary}]
    recent_external_refs: list[str] = field(default_factory=list)  # URLs/resources fetched
    pending_items: list[str] = field(default_factory=list)  # unfinished work items

    def track_file_read(self, path: str) -> None:
        """Record a file read for post-compact restoration. Keeps last 10 unique paths."""
        if path in self.recent_files:
            self.recent_files.remove(path)
        self.recent_files.append(path)
        if len(self.recent_files) > 10:
            self.recent_files.pop(0)

    def track_skill_loaded(self, skill_name: str) -> None:
        """Record a skill activation for post-compact restoration."""
        if skill_name not in self.active_skills:
            self.active_skills.append(skill_name)

    def track_file_write(self, path: str) -> None:
        """Record a file write for post-compact restoration. Keeps last 5."""
        if path in self.recent_writes:
            self.recent_writes.remove(path)
        self.recent_writes.append(path)
        if len(self.recent_writes) > 10:
            self.recent_writes.pop(0)

    def track_tool_outcome(self, tool_name: str, summary: str) -> None:
        """Record a high-value tool outcome for post-compact restoration. Keeps last 5."""
        self.recent_tool_outcomes.append({"tool": tool_name, "summary": summary[:300]})
        if len(self.recent_tool_outcomes) > 10:
            self.recent_tool_outcomes.pop(0)

    def track_external_ref(self, ref: str) -> None:
        """Record an external resource reference. Keeps last 5."""
        if ref not in self.recent_external_refs:
            self.recent_external_refs.append(ref)
        if len(self.recent_external_refs) > 5:
            self.recent_external_refs.pop(0)

    def track_pending_item(self, item: str) -> None:
        """Record an unfinished work item for post-compact restoration."""
        if item not in self.pending_items:
            self.pending_items.append(item)
        if len(self.pending_items) > 10:
            self.pending_items.pop(0)
