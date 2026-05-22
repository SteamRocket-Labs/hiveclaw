from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.memory.retention import RetentionInputs, RetentionTier, compute_retention_score, retention_tier


def test_retention_hot_for_recent_feedback_approved_memory() -> None:
    now = datetime(2026, 5, 22, tzinfo=UTC)
    score = compute_retention_score(
        RetentionInputs(
            salience=0.8,
            created_at=now - timedelta(days=1),
            accessed_at=[now - timedelta(hours=2)],
            feedback_reinforce=0.4,
        ),
        now=now,
    )

    assert score >= 0.8
    assert retention_tier(score) == RetentionTier.HOT


def test_retention_cold_for_old_unaccessed_low_salience_memory() -> None:
    now = datetime(2026, 5, 22, tzinfo=UTC)
    score = compute_retention_score(
        RetentionInputs(
            salience=0.2,
            created_at=now - timedelta(days=365),
            accessed_at=[],
            feedback_reinforce=0.0,
        ),
        now=now,
    )

    assert score < 0.3
    assert retention_tier(score) == RetentionTier.COLD

