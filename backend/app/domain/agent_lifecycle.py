"""Agent lifecycle state machine — pure domain logic, no I/O.

Defines valid states, transitions, and guard predicates.
All callers must route status changes through `transition()`.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class AgentStatus(StrEnum):
    DRAFT = "draft"
    CREATING = "creating"
    IDLE = "idle"
    RUNNING = "running"
    PAUSED = "paused"
    STOPPED = "stopped"
    ERROR = "error"
    EXPIRED = "expired"
    ARCHIVED = "archived"


@dataclass(frozen=True)
class TransitionContext:
    """Context for evaluating transition guards."""

    is_creator: bool = False
    is_admin: bool = False
    is_system: bool = False  # trigger daemon, heartbeat, etc.
    force: bool = False  # force-stop a running agent


class InvalidTransitionError(Exception):
    """Raised when an agent status transition is not allowed."""

    def __init__(self, current: str, target: str, reason: str = "") -> None:
        self.current = current
        self.target = target
        self.reason = reason
        msg = f"Invalid transition: {current} -> {target}"
        if reason:
            msg += f" ({reason})"
        super().__init__(msg)


# ── Valid transitions ──────────────────────────────────────

# Adjacency list: from_state -> set of to_states
VALID_TRANSITIONS: dict[str, set[str]] = {
    AgentStatus.DRAFT: {AgentStatus.CREATING},
    AgentStatus.CREATING: {AgentStatus.IDLE, AgentStatus.ERROR},
    AgentStatus.IDLE: {
        AgentStatus.RUNNING,
        AgentStatus.PAUSED,
        AgentStatus.STOPPED,
        AgentStatus.EXPIRED,
    },
    AgentStatus.RUNNING: {
        AgentStatus.IDLE,
        AgentStatus.ERROR,
        AgentStatus.STOPPED,
        AgentStatus.EXPIRED,
    },
    AgentStatus.PAUSED: {AgentStatus.IDLE, AgentStatus.STOPPED, AgentStatus.EXPIRED},
    AgentStatus.STOPPED: {AgentStatus.IDLE, AgentStatus.ARCHIVED},
    AgentStatus.ERROR: {AgentStatus.IDLE, AgentStatus.STOPPED},
    AgentStatus.EXPIRED: {AgentStatus.IDLE, AgentStatus.ARCHIVED},
    AgentStatus.ARCHIVED: set(),  # terminal state
}


def _guard_requires_privilege(current: str, target: str, ctx: TransitionContext) -> str | None:
    """Transitions that require creator or admin privilege."""
    privileged_targets = {
        AgentStatus.PAUSED,
        AgentStatus.STOPPED,
        AgentStatus.ARCHIVED,
    }
    if target in privileged_targets and not (ctx.is_creator or ctx.is_admin):
        return "requires creator or admin"
    return None


def _guard_force_stop_running(current: str, target: str, ctx: TransitionContext) -> str | None:
    """Force-stopping a running agent requires explicit confirmation."""
    if current == AgentStatus.RUNNING and target == AgentStatus.STOPPED and not ctx.force:
        return "force=True required to stop a running agent"
    return None


def _guard_resume_from_expired(current: str, target: str, ctx: TransitionContext) -> str | None:
    """Only admins can resume an expired agent (extend TTL)."""
    if current == AgentStatus.EXPIRED and target == AgentStatus.IDLE and not ctx.is_admin:
        return "only admin can resume expired agents"
    return None


def _guard_system_transitions(current: str, target: str, ctx: TransitionContext) -> str | None:
    """Running/idle transitions from triggers/chat require system or user context."""
    system_transitions = {
        (AgentStatus.IDLE, AgentStatus.RUNNING),
        (AgentStatus.RUNNING, AgentStatus.IDLE),
        (AgentStatus.IDLE, AgentStatus.EXPIRED),
        (AgentStatus.RUNNING, AgentStatus.EXPIRED),
        (AgentStatus.RUNNING, AgentStatus.ERROR),
        (AgentStatus.CREATING, AgentStatus.IDLE),
        (AgentStatus.CREATING, AgentStatus.ERROR),
        (AgentStatus.DRAFT, AgentStatus.CREATING),
    }
    if (current, target) in system_transitions:
        if not (ctx.is_system or ctx.is_creator or ctx.is_admin):
            return "requires system, creator, or admin context"
    return None


# All guards evaluated in order; first failure stops the transition
_GUARDS = [
    _guard_requires_privilege,
    _guard_force_stop_running,
    _guard_resume_from_expired,
    _guard_system_transitions,
]


# ── Public API ─────────────────────────────────────────────

def transition(current: str, target: str, ctx: TransitionContext | None = None) -> str:
    """Validate and execute a state transition. Returns the new state.

    Raises InvalidTransitionError if the transition is invalid or a guard fails.
    """
    if ctx is None:
        ctx = TransitionContext(is_system=True)

    # Normalize to lowercase
    current = current.lower()
    target = target.lower()

    # Check adjacency
    allowed = VALID_TRANSITIONS.get(current, set())
    if target not in allowed:
        raise InvalidTransitionError(current, target, f"allowed targets: {sorted(allowed)}")

    # Evaluate guards
    for guard in _GUARDS:
        reason = guard(current, target, ctx)
        if reason:
            raise InvalidTransitionError(current, target, reason)

    return target


def can_transition(current: str, target: str, ctx: TransitionContext | None = None) -> bool:
    """Check if a transition is valid without raising."""
    try:
        transition(current, target, ctx)
        return True
    except InvalidTransitionError:
        return False


def available_transitions(current: str, ctx: TransitionContext | None = None) -> list[str]:
    """Return all valid target states from current state given context."""
    current = current.lower()
    return [t for t in VALID_TRANSITIONS.get(current, set()) if can_transition(current, t, ctx)]
