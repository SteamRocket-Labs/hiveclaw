from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4


def test_apply_stale_sla_marks_active_objective_blocked():
    from app.services.objective_lifecycle import apply_stale_sla

    objective = SimpleNamespace(
        id=uuid4(),
        objective_key="daily_report",
        status="active",
        blocked_reason=None,
        updated_at=datetime.now(timezone.utc) - timedelta(hours=49),
        metadata_json={"stale_after_hours": 24},
    )

    changed = apply_stale_sla(objective, now=datetime.now(timezone.utc))

    assert changed is True
    assert objective.status == "blocked"
    assert "stale" in objective.blocked_reason.lower()
    assert objective.metadata_json["stale"]["reason"] == "stale_sla_exceeded"


def test_apply_trigger_failure_policy_sets_exponential_backoff():
    from app.services.objective_lifecycle import apply_trigger_failure_policy

    now = datetime.now(timezone.utc)
    trigger = SimpleNamespace(
        id=uuid4(),
        name="daily_report",
        config={"trigger_class": "scheduled_job", "failure_count": 1},
    )

    metadata = apply_trigger_failure_policy(trigger, error="provider unavailable", now=now)

    assert trigger.config["failure_count"] == 2
    assert datetime.fromisoformat(trigger.config["backoff_until"]) > now
    assert metadata["failure_count"] == 2
    assert metadata["backoff_seconds"] >= 120


def test_should_create_recovery_objective_after_repeated_failures():
    from app.services.objective_lifecycle import should_create_recovery_objective

    trigger = SimpleNamespace(config={"failure_count": 3})

    assert should_create_recovery_objective(trigger) is True
