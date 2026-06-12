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

    Per-round reminder bookkeeping (FULL-once / SPARSE-cooldown) moved to
    ``kernel/reminder_scheduler.py`` (T-G1) — the scheduler owns those clocks
    per invocation, so this state carries no reminder fields.
    """

    active: bool = False
    plan_id: str | None = None
    intent_type: str | None = None
    action_kind: str | None = None
    tool_name: str | None = None
    # P1 binding: the action artifact computed at gate-check time (definition
    # hash / args hash / risk reasons for ``start_workflow``). Rides the state
    # into the metadata mirror so ``exit_plan_mode`` can bind the authored plan
    # to the exact blocked action; without it the confirmed plan is rejected
    # with ``action_artifact_missing`` and the high-risk launch deadlocks.
    action_artifact: dict[str, Any] | None = None
    original_request: str | None = None
    handoff_target: str | None = None
    reason: str | None = None
    deep_research: bool = False
    deep_research_args: dict[str, Any] = field(default_factory=dict)
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
        # Path-unification cut ③: a system_plan_run launcher pre-arms Plan Mode
        # with a draft ``plan_id`` so ``exit_plan_mode`` fills THAT draft instead
        # of creating a new awaiting plan. Emitted only when present so live chat
        # / unattended tool-intercept (no pre-created plan_id) keep the legacy
        # "create new" mirror byte-for-byte.
        if self.plan_id:
            data["plan_id"] = self.plan_id
        # Emitted only when present (same rule as plan_id) so mirrors without a
        # bound action stay byte-compatible with the legacy shape.
        if self.action_artifact:
            data["action_artifact"] = dict(self.action_artifact)
        if self.deep_research:
            data["deep_research"] = True
            data["deep_research_args"] = dict(self.deep_research_args)
        # Phase 4B: the read-only gate reads the plan file off the ContextVar
        # mirror, so a provisioned plan_file_path must round-trip.
        if self.plan_file_path:
            data["plan_file_path"] = self.plan_file_path
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
            action_artifact=dict(data["action_artifact"]) if isinstance(data.get("action_artifact"), dict) else None,
            original_request=data.get("original_request"),
            handoff_target=data.get("handoff_target"),
            reason=data.get("reason"),
            deep_research=bool(data.get("deep_research")),
            deep_research_args=dict(data.get("deep_research_args") or {}),
            plan_file_path=data.get("plan_file_path"),
            source=str(data.get("source") or "web_chat"),
        )


@dataclass(slots=True)
class SessionContext:
    session_id: str | None = None
    source: str = "runtime"
    channel: str | None = None
    active_tool_groups: list[dict[str, Any]] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)
    # Plan Mode as a first-class runtime state. The legacy mirror still lives in
    # ``metadata["plan_mode"]`` (written by _activate_interactive_plan_mode) for
    # the ContextVar / exit_plan_mode / suffix / frontend; this is the typed
    # source of truth for Plan Mode eligibility/content while reminder clocks
    # live in kernel/reminder_scheduler.py.
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
    # C1/T3a: tools discovered through tool_search become callable for this
    # session, and the list is mirrored into metadata for compaction/recovery.
    discovered_tools: list[str] = field(default_factory=list)
    # Version snapshots for files touched by the session. Keys are the tool
    # path strings the model used (for example workspace/report.md); values
    # contain exists/size/mtime_ns captured after the read/write. The kernel
    # compares these against live stat data before later rounds so external
    # file edits can be surfaced as runtime attachments.
    file_snapshots: dict[str, dict[str, Any]] = field(default_factory=dict)
    # P1-W2-7: per-skill bookkeeping (refcount + last_used_at_ts) parallel
    # to active_skills. The list is the public read surface (kept as
    # `list[str]` for backwards compat); this dict drives unload + prune.
    _skill_metadata: dict[str, dict[str, float]] = field(default_factory=dict)
    recent_writes: list[str] = field(default_factory=list)  # file paths written by agent
    recent_tool_outcomes: list[dict[str, str]] = field(default_factory=list)  # [{tool, summary}]
    recent_external_refs: list[str] = field(default_factory=list)  # URLs/resources fetched
    pending_items: list[str] = field(default_factory=list)  # unfinished work items

    def __post_init__(self) -> None:
        mirrored = self.metadata.get("discovered_tools")
        if isinstance(mirrored, list):
            self.discovered_tools = [str(name).strip() for name in mirrored if str(name).strip()]
        elif self.discovered_tools:
            self.metadata["discovered_tools"] = list(self.discovered_tools)

    def track_file_read(self, path: str, *, snapshot: dict[str, Any] | None = None) -> None:
        """Record a file read for post-compact restoration. Keeps last 10 unique paths."""
        if path in self.recent_files:
            self.recent_files.remove(path)
        self.recent_files.append(path)
        if snapshot is not None:
            self.file_snapshots[path] = dict(snapshot)
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

    def track_discovered_tools(self, tool_names: list[str] | tuple[str, ...]) -> list[str]:
        """Record deferred tools made callable by tool_search. Returns newly added names."""
        added: list[str] = []
        seen = set(self.discovered_tools)
        for raw_name in tool_names:
            name = str(raw_name).strip()
            if not name or name in seen:
                continue
            self.discovered_tools.append(name)
            added.append(name)
            seen.add(name)
        self.metadata["discovered_tools"] = list(self.discovered_tools)
        return added

    def track_file_write(self, path: str, *, snapshot: dict[str, Any] | None = None) -> None:
        """Record a file write for post-compact restoration. Keeps last 5."""
        if path in self.recent_writes:
            self.recent_writes.remove(path)
        self.recent_writes.append(path)
        if snapshot is not None:
            self.file_snapshots[path] = dict(snapshot)
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


# Live interactive user channels eligible for tool-intercept → interactive Plan
# Mode. Real runtime web-chat sessions use source="web"; IM channel messages use
# their channel slug as source/channel. Unattended paths (trigger/heartbeat) get
# their own eligibility below.
_INTERACTIVE_PLAN_CHAT_SURFACES = frozenset({
    "web",
    "web_chat",
    "chat",
    "feishu",
    "wechat_personal",
    "wecom",
    "telegram",
    "dingtalk",
    "slack",
    "discord",
    "teams",
})

# Unattended agent runs eligible for tool-intercept → main-loop Plan Mode
# (path-unification §5.3 / cut ②, made unconditional in cut ④). These are
# multi-round kernel loops with no live user stream: a blocked gated tool flips
# the run into the SAME Plan Mode runtime as live chat (read-only policy +
# scheduler-driven transient reminder + exit_plan_mode), and the authored plan lands
# awaiting_confirmation for asynchronous user confirmation from the plan queue.
_UNATTENDED_PLAN_RUN_SOURCES = frozenset({"trigger", "heartbeat"})


def is_interactive_plan_eligible(session_context: Any | None) -> bool:
    """Single source of truth for the tool-intercept → interactive Plan Mode
    boundary, shared by the invoker tool-runtime gate and kernel activation so
    the two can never drift (Phase 5 follow-up: the prior duplicated checks
    disagreed on ``chat``/channel — harmless while source is always ``"web"``,
    but unified here before flag rollout).
    """
    if session_context is None:
        return False
    source = str(getattr(session_context, "source", "") or "").lower()
    channel = str(getattr(session_context, "channel", "") or "").lower()
    return source in _INTERACTIVE_PLAN_CHAT_SURFACES or channel in _INTERACTIVE_PLAN_CHAT_SURFACES


def is_unattended_plan_eligible(session_context: Any | None) -> bool:
    """True for an unattended agent run (trigger/heartbeat) that is still a
    multi-round kernel loop, so a blocked gated tool can flip into main-loop
    Plan Mode and let the agent author the plan in its own loop — deferring
    confirmation to the next time a user is present (path-unification §5.3).

    Distinct from :func:`is_interactive_plan_eligible` (live chat, synchronous
    confirmation). The kernel activation and the invoker tool-runtime
    RPC-fallback decision both consult this so the two can never drift.
    """
    if session_context is None:
        return False
    source = str(getattr(session_context, "source", "") or "").lower()
    return source in _UNATTENDED_PLAN_RUN_SOURCES
