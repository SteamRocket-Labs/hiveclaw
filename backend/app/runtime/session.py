"""Explicit session context types for runtime entrypoints."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

# P1-W2-7: skills loaded via the `load_skill` tool need lifecycle hygiene.
# Without it, a long-running session accumulates every skill it ever
# touched into the prompt prefix and into recovery manifests, even after
# the agent has moved on. Default TTL keeps a skill in the active set for
# 1 hour after its last use; subsequent prunes drop it. Refcounts let an
# explicit unload_skill call symmetrically pair with each load.
_DEFAULT_SKILL_TTL_SECONDS = 3600


@dataclass(slots=True)
class PlanModeState:
    """First-class typed Plan Mode runtime state (paradigm-convergence doc §6.1).

    Replaces the untyped ``SessionContext.metadata["plan_mode"]`` dict as the
    source of truth for runtime injection. The dict is still written as a
    backward-compatible mirror (see :meth:`to_metadata`) because the interactive
    ContextVar, the ``exit_plan_mode`` tool, the prompt suffix, and the frontend
    plan card all still read it.

    ``entered_round`` / ``reminded_full`` are Phase 2 per-round reminder
    bookkeeping and live ONLY here — they are deliberately excluded from
    :meth:`to_metadata` so the legacy mirror cannot drift on values it never
    owned (the ContextVar and ``exit_plan_mode`` consume neither).
    """

    active: bool = False
    plan_id: str | None = None
    intent_type: str | None = None
    action_kind: str | None = None
    tool_name: str | None = None
    original_request: str | None = None
    handoff_target: str | None = None
    reason: str | None = None
    deep_research: bool = False
    deep_research_args: dict[str, Any] = field(default_factory=dict)
    # Phase 2 per-round reminder bookkeeping (runtime-only; never mirrored).
    entered_round: int = 0
    reminded_full: bool = False
    # Phase 4B plan-file writing target (reserved; unused until that phase).
    plan_file_path: str | None = None
    source: str = "web_chat"

    def to_metadata(self) -> dict[str, Any]:
        """Render the legacy ``metadata['plan_mode']`` dict shape.

        Byte-compatible with ``_activate_interactive_plan_mode``: deep-research
        keys are emitted only when ``deep_research`` is set, matching the
        conditional update the legacy code performed.
        """
        data: dict[str, Any] = {
            "active": self.active,
            "original_request": self.original_request,
            "intent_type": self.intent_type,
            "action_kind": self.action_kind,
            "tool_name": self.tool_name,
            "reason": self.reason,
            "handoff_target": self.handoff_target,
        }
        if self.deep_research:
            data["deep_research"] = True
            data["deep_research_args"] = dict(self.deep_research_args)
        return data

    @classmethod
    def from_metadata(cls, data: Any) -> PlanModeState:
        """Rebuild typed state from a legacy mirror dict; degrade safely.

        A ``None`` or non-dict payload returns an inactive state rather than
        raising, so callers can read ``metadata.get("plan_mode")`` blindly.
        """
        if not isinstance(data, dict):
            return cls()
        return cls(
            active=bool(data.get("active")),
            plan_id=data.get("plan_id"),
            intent_type=data.get("intent_type"),
            action_kind=data.get("action_kind"),
            tool_name=data.get("tool_name"),
            original_request=data.get("original_request"),
            handoff_target=data.get("handoff_target"),
            reason=data.get("reason"),
            deep_research=bool(data.get("deep_research")),
            deep_research_args=dict(data.get("deep_research_args") or {}),
            source=str(data.get("source") or "web_chat"),
        )


@dataclass(slots=True)
class SessionContext:
    session_id: str | None = None
    source: str = "runtime"
    channel: str | None = None
    active_packs: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    # Plan Mode as a first-class runtime state. The legacy mirror still lives in
    # ``metadata["plan_mode"]`` (written by _activate_interactive_plan_mode) for
    # the ContextVar / exit_plan_mode / suffix / frontend; this is the typed
    # source of truth for per-round reminder injection (Phase 2) and the
    # unified read-only gate (Phase 3).
    plan_mode: PlanModeState = field(default_factory=PlanModeState)
    # Prompt cache: frozen prefix reused within the same session
    prompt_prefix: str | None = None
    prompt_fingerprint: str | None = None
    # Legacy compatibility field. Memory now flows through the dynamic suffix,
    # so it must not invalidate the frozen prompt prefix.
    _memory_hash: str | None = None
    # Post-compact restoration: track session runtime events
    recent_files: list[str] = field(default_factory=list)  # file paths read by agent
    active_skills: list[str] = field(default_factory=list)  # skill names loaded via load_skill
    # P1-W2-7: per-skill bookkeeping (refcount + last_used_at_ts) parallel
    # to active_skills. The list is the public read surface (kept as
    # `list[str]` for backwards compat); this dict drives unload + prune.
    _skill_metadata: dict[str, dict[str, float]] = field(default_factory=dict)
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

    def track_skill_loaded(self, skill_name: str, *, now: float | None = None) -> None:
        """Record a skill activation. Bumps refcount and refreshes last_used_at."""
        ts = now if now is not None else time.time()
        entry = self._skill_metadata.get(skill_name)
        if entry is None:
            self._skill_metadata[skill_name] = {
                "refcount": 1.0,
                "loaded_at": ts,
                "last_used_at": ts,
            }
            self.active_skills.append(skill_name)
        else:
            entry["refcount"] += 1.0
            entry["last_used_at"] = ts

    def unload_skill(self, skill_name: str) -> bool:
        """Decrement refcount; remove from active set when it hits zero.

        Returns True if the skill was fully unloaded (last reference released),
        False if it remained referenced or wasn't loaded to begin with.
        """
        entry = self._skill_metadata.get(skill_name)
        if entry is None:
            return False
        entry["refcount"] -= 1.0
        if entry["refcount"] <= 0:
            self._skill_metadata.pop(skill_name, None)
            if skill_name in self.active_skills:
                self.active_skills.remove(skill_name)
            return True
        return False

    def prune_expired_skills(
        self,
        *,
        ttl_seconds: int = _DEFAULT_SKILL_TTL_SECONDS,
        now: float | None = None,
    ) -> list[str]:
        """Drop skills whose last_used_at + ttl < now. Returns the names dropped.

        Called periodically (e.g. heartbeat tick) to keep prompt prefix and
        recovery manifests from accumulating stale skill names.
        """
        ts = now if now is not None else time.time()
        expired: list[str] = []
        for name, entry in list(self._skill_metadata.items()):
            if ts - entry["last_used_at"] >= ttl_seconds:
                expired.append(name)
        for name in expired:
            self._skill_metadata.pop(name, None)
            if name in self.active_skills:
                self.active_skills.remove(name)
        return expired

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
