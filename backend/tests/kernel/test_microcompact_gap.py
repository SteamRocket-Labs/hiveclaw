"""P1-W2-3 — context-pressure-aware microcompact gap.

The kernel chooses how aggressively to evict aging tool-result messages
based on current context utilization:
  - <60% of model window → 60-min gap (default, conservative)
  - ≥60% of model window → 10-min gap (aggressive, sheds bloat early so
    we don't slide into the heavy-compaction zone at 75%)

The helper is isolated so the threshold logic can be unit-tested without
booting a full kernel session.
"""

from __future__ import annotations

from app.kernel.engine import (
    _MICROCOMPACT_GAP_SECONDS,
    _MICROCOMPACT_GAP_UNDER_PRESSURE_SECONDS,
    _MICROCOMPACT_PRESSURE_THRESHOLD,
    _MIDLOOP_COMPACT_THRESHOLD,
    _compute_microcompact_gap,
)


# ── Threshold value sanity ─────────────────────────────────────


def test_midloop_compact_threshold_is_75pct() -> None:
    """Was tightened from 0.90 to 0.75 so a single bursty round can't push
    past the limit before the next check fires."""
    assert _MIDLOOP_COMPACT_THRESHOLD == 0.75


def test_microcompact_pressure_threshold_is_60pct() -> None:
    """Pressure threshold sits 15 pp below heavy compaction so we shed
    aging tool results before triggering the more expensive path."""
    assert _MICROCOMPACT_PRESSURE_THRESHOLD == 0.60


def test_under_pressure_gap_is_strictly_shorter_than_default() -> None:
    """Pressure mode must actually be more aggressive than default."""
    assert _MICROCOMPACT_GAP_UNDER_PRESSURE_SECONDS < _MICROCOMPACT_GAP_SECONDS


# ── Gap selection logic ───────────────────────────────────────


def test_below_pressure_returns_default_gap() -> None:
    """50% utilization on a 100K window — well below the 60% trip line."""
    gap = _compute_microcompact_gap(used_tokens=50_000, model_window=100_000)
    assert gap == _MICROCOMPACT_GAP_SECONDS


def test_at_pressure_threshold_returns_short_gap() -> None:
    """Exactly 60% utilization should already trip pressure mode."""
    gap = _compute_microcompact_gap(used_tokens=60_000, model_window=100_000)
    assert gap == _MICROCOMPACT_GAP_UNDER_PRESSURE_SECONDS


def test_above_pressure_returns_short_gap() -> None:
    """80% utilization — solidly in pressure mode."""
    gap = _compute_microcompact_gap(used_tokens=80_000, model_window=100_000)
    assert gap == _MICROCOMPACT_GAP_UNDER_PRESSURE_SECONDS


def test_unknown_model_window_returns_default_gap() -> None:
    """Without a window size we can't compute %, so stay conservative."""
    assert _compute_microcompact_gap(used_tokens=50_000, model_window=None) == _MICROCOMPACT_GAP_SECONDS
    assert _compute_microcompact_gap(used_tokens=50_000, model_window=0) == _MICROCOMPACT_GAP_SECONDS
    assert _compute_microcompact_gap(used_tokens=50_000, model_window=-100) == _MICROCOMPACT_GAP_SECONDS


def test_zero_used_tokens_returns_default_gap() -> None:
    """Empty conversation — no pressure."""
    gap = _compute_microcompact_gap(used_tokens=0, model_window=100_000)
    assert gap == _MICROCOMPACT_GAP_SECONDS
