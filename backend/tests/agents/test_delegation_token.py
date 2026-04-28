"""P1-W3-1 — DelegationToken contract tests.

The data shape ships first; governance integration arrives in W3-3. These
tests pin the invariants downstream code can lean on:
  - tokens carry an explicit expiry (not "session lifetime")
  - explicit-grant tokens narrow capability set; None means "inherit
    parent's set" (legacy behaviour)
  - validate_delegation_token rejects expired or out-of-scope calls with
    a non-empty reason so audit logs stay informative
"""

from __future__ import annotations

import time
import uuid

import pytest

from app.agents.delegation_token import (
    DEFAULT_DELEGATION_TTL_SECONDS,
    DelegationToken,
    issue_delegation_token,
    validate_delegation_token,
)


# ── Default TTL ────────────────────────────────────────────────


def test_default_ttl_is_300_seconds() -> None:
    """5min is long enough for a normal LLM round + tool calls but short
    enough that a stuck child can't keep spending parent capacity."""
    assert DEFAULT_DELEGATION_TTL_SECONDS == 300.0


# ── Issuance ──────────────────────────────────────────────────


def test_issue_with_explicit_capabilities_freezes_grant_set() -> None:
    parent = uuid.uuid4()
    child = uuid.uuid4()
    granted = frozenset({"workspace.file.read", "agent.memory.read"})

    token = issue_delegation_token(
        parent_agent_id=parent,
        child_agent_id=child,
        granted_capabilities=granted,
        ttl_seconds=120.0,
        now=1000.0,
    )

    assert token.parent_agent_id == parent
    assert token.child_agent_id == child
    assert token.issued_at == 1000.0
    assert token.expires_at == 1120.0
    assert token.granted_capabilities == granted
    assert token.inherit_parent_capabilities is False


def test_issue_without_capabilities_marks_inherit() -> None:
    """`granted_capabilities=None` is the legacy shape — child inherits
    parent's full set (governance still applies)."""
    token = issue_delegation_token(
        parent_agent_id=uuid.uuid4(),
        child_agent_id=uuid.uuid4(),
        granted_capabilities=None,
        now=10.0,
    )
    assert token.inherit_parent_capabilities is True
    assert token.granted_capabilities == frozenset()


def test_issue_with_empty_set_grants_no_tools() -> None:
    """An explicit empty frozenset means "no tools" — useful for plain-
    chat delegations where the child should only produce text."""
    token = issue_delegation_token(
        parent_agent_id=uuid.uuid4(),
        child_agent_id=uuid.uuid4(),
        granted_capabilities=frozenset(),
        now=10.0,
    )
    assert token.inherit_parent_capabilities is False
    assert token.allows_capability("workspace.file.read") is False


def test_token_is_frozen_dataclass() -> None:
    token = issue_delegation_token(
        parent_agent_id=uuid.uuid4(),
        child_agent_id=uuid.uuid4(),
    )
    # Frozen dataclass: assigning to any field must raise.
    with pytest.raises((AttributeError, Exception)):
        token.expires_at = 0.0  # type: ignore[misc]


def test_issue_uses_real_time_when_now_omitted() -> None:
    before = time.time()
    token = issue_delegation_token(
        parent_agent_id=uuid.uuid4(),
        child_agent_id=uuid.uuid4(),
        ttl_seconds=60.0,
    )
    after = time.time()
    assert before <= token.issued_at <= after
    assert token.expires_at - token.issued_at == pytest.approx(60.0)


# ── Capability check ──────────────────────────────────────────


def test_inherited_token_allows_any_capability() -> None:
    token = issue_delegation_token(
        parent_agent_id=uuid.uuid4(),
        child_agent_id=uuid.uuid4(),
        granted_capabilities=None,
    )
    assert token.allows_capability("anything.at.all") is True


def test_explicit_token_allows_only_listed_capabilities() -> None:
    token = issue_delegation_token(
        parent_agent_id=uuid.uuid4(),
        child_agent_id=uuid.uuid4(),
        granted_capabilities=frozenset({"workspace.file.read"}),
    )
    assert token.allows_capability("workspace.file.read") is True
    assert token.allows_capability("workspace.file.write") is False
    assert token.allows_capability("anything.else") is False


# ── Expiry ────────────────────────────────────────────────────


def test_token_not_expired_before_ttl() -> None:
    token = issue_delegation_token(
        parent_agent_id=uuid.uuid4(),
        child_agent_id=uuid.uuid4(),
        ttl_seconds=100.0,
        now=0.0,
    )
    assert token.is_expired(now=99.0) is False


def test_token_expired_at_ttl_boundary() -> None:
    """Expiry is inclusive — at exactly issued+ttl the token is dead."""
    token = issue_delegation_token(
        parent_agent_id=uuid.uuid4(),
        child_agent_id=uuid.uuid4(),
        ttl_seconds=100.0,
        now=0.0,
    )
    assert token.is_expired(now=100.0) is True


# ── Validate (combined expiry + capability) ───────────────────


def test_validate_accepts_in_scope_unexpired_call() -> None:
    token = issue_delegation_token(
        parent_agent_id=uuid.uuid4(),
        child_agent_id=uuid.uuid4(),
        granted_capabilities=frozenset({"agent.memory.read"}),
        ttl_seconds=100.0,
        now=0.0,
    )
    result = validate_delegation_token(
        token, capability="agent.memory.read", now=10.0
    )
    assert result.valid is True
    assert result.reason == ""


def test_validate_rejects_expired_token_with_reason() -> None:
    token = issue_delegation_token(
        parent_agent_id=uuid.uuid4(),
        child_agent_id=uuid.uuid4(),
        granted_capabilities=frozenset({"agent.memory.read"}),
        ttl_seconds=100.0,
        now=0.0,
    )
    result = validate_delegation_token(
        token, capability="agent.memory.read", now=200.0
    )
    assert result.valid is False
    assert "expired" in result.reason


def test_validate_rejects_out_of_scope_capability() -> None:
    token = issue_delegation_token(
        parent_agent_id=uuid.uuid4(),
        child_agent_id=uuid.uuid4(),
        granted_capabilities=frozenset({"agent.memory.read"}),
        ttl_seconds=100.0,
        now=0.0,
    )
    result = validate_delegation_token(
        token, capability="workspace.file.write", now=10.0
    )
    assert result.valid is False
    assert "not in delegation grant" in result.reason


def test_validate_without_capability_only_checks_expiry() -> None:
    """Calling validate without `capability=` is the "is this token still
    fresh?" check used by callers that handle scope themselves."""
    token = issue_delegation_token(
        parent_agent_id=uuid.uuid4(),
        child_agent_id=uuid.uuid4(),
        granted_capabilities=frozenset(),
        ttl_seconds=100.0,
        now=0.0,
    )
    fresh = validate_delegation_token(token, now=50.0)
    assert fresh.valid is True

    stale = validate_delegation_token(token, now=500.0)
    assert stale.valid is False
    assert "expired" in stale.reason


def test_inherited_token_validates_for_any_capability() -> None:
    token = issue_delegation_token(
        parent_agent_id=uuid.uuid4(),
        child_agent_id=uuid.uuid4(),
        granted_capabilities=None,
        ttl_seconds=100.0,
        now=0.0,
    )
    for cap in ("workspace.file.read", "channel.email.send", "agent.memory.write"):
        assert validate_delegation_token(token, capability=cap, now=10.0).valid is True
