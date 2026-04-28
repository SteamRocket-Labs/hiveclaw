"""P1-W3-1 — Delegation token: scope-limited, TTL-bounded auth slip.

When a parent agent delegates to a child via `delegate_to_agent`, the
child currently inherits the parent's full tool surface. That means a
"summarize this paragraph" delegation can in principle call any tool the
parent could — including high-risk ones the user never agreed to expose
through the delegation chain.

A delegation token narrows the child's authority to:
  - a finite set of granted capabilities (or `None` meaning "inherit
    parent's set", for backwards compatibility)
  - an explicit expiry timestamp; the kernel rejects calls after expiry
    so a runaway child can't hold on to a bygone grant

This module owns only the data + validation. Wiring the token into the
governance pipeline lives in P1-W3-3 — kept separate so the data shape
ships first and downstream code can lean on it incrementally.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass

# Default token lifetime — long enough for a normal delegation tick
# (LLM round + tool calls), short enough that a stuck child can't keep
# spending parent capacity indefinitely.
_DEFAULT_TTL_SECONDS = 300.0


@dataclass(slots=True, frozen=True)
class DelegationToken:
    """Capability slip handed from parent to child for a single delegation.

    Frozen so the child cannot tamper with grants after issuance.
    """

    delegation_id: str
    parent_agent_id: uuid.UUID
    child_agent_id: uuid.UUID
    issued_at: float
    expires_at: float
    granted_capabilities: frozenset[str]
    inherit_parent_capabilities: bool

    def is_expired(self, *, now: float | None = None) -> bool:
        ts = now if now is not None else time.time()
        return ts >= self.expires_at

    def allows_capability(self, capability: str) -> bool:
        """Grant check — does NOT cover expiry. Callers must check both."""
        if self.inherit_parent_capabilities:
            return True
        return capability in self.granted_capabilities


@dataclass(slots=True, frozen=True)
class TokenValidationResult:
    valid: bool
    reason: str = ""


def issue_delegation_token(
    *,
    parent_agent_id: uuid.UUID,
    child_agent_id: uuid.UUID,
    granted_capabilities: frozenset[str] | None = None,
    ttl_seconds: float = _DEFAULT_TTL_SECONDS,
    now: float | None = None,
) -> DelegationToken:
    """Mint a fresh token for a delegation.

    `granted_capabilities=None` means "inherit parent's set" (legacy
    behaviour). An explicit empty frozenset narrows the child to *no*
    capabilities — useful for "just answer in chat, no tools" delegations.
    """
    issued = now if now is not None else time.time()
    return DelegationToken(
        delegation_id=str(uuid.uuid4()),
        parent_agent_id=parent_agent_id,
        child_agent_id=child_agent_id,
        issued_at=issued,
        expires_at=issued + ttl_seconds,
        granted_capabilities=frozenset() if granted_capabilities is None else granted_capabilities,
        inherit_parent_capabilities=granted_capabilities is None,
    )


def validate_delegation_token(
    token: DelegationToken,
    *,
    capability: str | None = None,
    child_agent_id: uuid.UUID | None = None,
    now: float | None = None,
) -> TokenValidationResult:
    """Confirm a token is usable for the requested capability.

    Returns a result object with a `reason` string so callers can log
    the rejection (audit) without having to reconstruct context.
    """
    if token.is_expired(now=now):
        return TokenValidationResult(False, "delegation token expired")
    if child_agent_id is not None and token.child_agent_id != child_agent_id:
        return TokenValidationResult(False, "delegation token child agent mismatch")
    if capability is not None and not token.allows_capability(capability):
        return TokenValidationResult(
            False,
            f"capability {capability!r} not in delegation grant",
        )
    return TokenValidationResult(True, "")


# Convenience constants for callers / tests.
DEFAULT_DELEGATION_TTL_SECONDS = _DEFAULT_TTL_SECONDS
