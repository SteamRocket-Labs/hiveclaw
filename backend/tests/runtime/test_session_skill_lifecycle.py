"""P1-W2-7 — skill lifecycle on SessionContext.

Skills loaded via the `load_skill` tool used to accumulate forever in
`active_skills`, bloating the prompt prefix and recovery manifest. The
new contract:
  - track_skill_loaded bumps a per-skill refcount and refreshes
    last_used_at; the public `active_skills` list still reflects names.
  - unload_skill decrements; on zero the skill drops out symmetrically.
  - prune_expired_skills removes anything whose last_used_at + ttl < now.

These tests pin each behaviour so future load_skill / unload_skill
plumbing has a stable contract to lean on.
"""

from __future__ import annotations

from app.runtime.session import SessionContext, _DEFAULT_SKILL_TTL_SECONDS


# ── Refcount ──────────────────────────────────────────────────


def test_first_load_adds_to_active_skills_with_refcount_1() -> None:
    s = SessionContext()
    s.track_skill_loaded("memory-guide", now=1000.0)

    assert s.active_skills == ["memory-guide"]
    assert s._skill_metadata["memory-guide"]["refcount"] == 1.0
    assert s._skill_metadata["memory-guide"]["loaded_at"] == 1000.0
    assert s._skill_metadata["memory-guide"]["last_used_at"] == 1000.0


def test_repeat_load_bumps_refcount_and_refreshes_last_used() -> None:
    s = SessionContext()
    s.track_skill_loaded("memory-guide", now=1000.0)
    s.track_skill_loaded("memory-guide", now=1500.0)
    s.track_skill_loaded("memory-guide", now=2000.0)

    # Only one entry in the public list (idempotent surface).
    assert s.active_skills == ["memory-guide"]
    meta = s._skill_metadata["memory-guide"]
    assert meta["refcount"] == 3.0
    assert meta["loaded_at"] == 1000.0  # first load wins
    assert meta["last_used_at"] == 2000.0  # latest activity


def test_independent_skills_tracked_separately() -> None:
    s = SessionContext()
    s.track_skill_loaded("a", now=1.0)
    s.track_skill_loaded("b", now=2.0)
    s.track_skill_loaded("a", now=3.0)

    assert sorted(s.active_skills) == ["a", "b"]
    assert s._skill_metadata["a"]["refcount"] == 2.0
    assert s._skill_metadata["b"]["refcount"] == 1.0


def test_discovered_tools_are_tracked_and_mirrored_to_metadata() -> None:
    s = SessionContext()

    added = s.track_discovered_tools(["web_search", "web_fetch", "web_search"])

    assert added == ["web_search", "web_fetch"]
    assert s.discovered_tools == ["web_search", "web_fetch"]
    assert s.metadata["discovered_tools"] == ["web_search", "web_fetch"]


# ── Unload ────────────────────────────────────────────────────


def test_unload_below_zero_returns_false_and_drops_skill() -> None:
    s = SessionContext()
    s.track_skill_loaded("x", now=10.0)
    s.track_skill_loaded("x", now=20.0)

    # First unload: refcount 2 → 1, still active.
    assert s.unload_skill("x") is False
    assert s.active_skills == ["x"]

    # Second unload: refcount → 0, drop.
    assert s.unload_skill("x") is True
    assert s.active_skills == []
    assert "x" not in s._skill_metadata


def test_unload_unknown_skill_is_no_op() -> None:
    s = SessionContext()
    assert s.unload_skill("never-loaded") is False
    assert s.active_skills == []


def test_unload_does_not_disturb_other_skills() -> None:
    s = SessionContext()
    s.track_skill_loaded("a", now=1.0)
    s.track_skill_loaded("b", now=2.0)

    s.unload_skill("a")
    assert s.active_skills == ["b"]
    assert s._skill_metadata == {"b": {"refcount": 1.0, "loaded_at": 2.0, "last_used_at": 2.0}}


# ── Prune by TTL ─────────────────────────────────────────────


def test_default_ttl_is_one_hour() -> None:
    """1h default keeps recently-used skills around through normal idle
    bursts but drops genuinely abandoned ones."""
    assert _DEFAULT_SKILL_TTL_SECONDS == 3600


def test_prune_drops_skills_past_ttl() -> None:
    s = SessionContext()
    s.track_skill_loaded("old", now=0.0)
    s.track_skill_loaded("fresh", now=3500.0)

    expired = s.prune_expired_skills(ttl_seconds=3600, now=3700.0)

    assert expired == ["old"]
    assert s.active_skills == ["fresh"]
    assert "old" not in s._skill_metadata
    assert "fresh" in s._skill_metadata


def test_prune_returns_empty_when_nothing_expired() -> None:
    s = SessionContext()
    s.track_skill_loaded("a", now=100.0)
    expired = s.prune_expired_skills(ttl_seconds=3600, now=200.0)
    assert expired == []
    assert s.active_skills == ["a"]


def test_prune_uses_last_used_not_loaded_at() -> None:
    """A skill that was loaded long ago but recently re-used must survive."""
    s = SessionContext()
    s.track_skill_loaded("warm", now=0.0)
    # Re-use 50 minutes later, well before the 1h TTL.
    s.track_skill_loaded("warm", now=3000.0)

    # Now we're at "loaded_at + 1h" — would expire if prune used loaded_at,
    # but last_used_at is fresher, so it stays.
    expired = s.prune_expired_skills(ttl_seconds=3600, now=3600.0)

    assert expired == []
    assert s.active_skills == ["warm"]


def test_prune_with_custom_ttl_overrides_default() -> None:
    s = SessionContext()
    s.track_skill_loaded("a", now=0.0)

    expired = s.prune_expired_skills(ttl_seconds=60, now=70.0)
    assert expired == ["a"]
