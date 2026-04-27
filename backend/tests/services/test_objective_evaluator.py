from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


def test_completed_objective_requires_evidence():
    from app.services.objective_evaluator import apply_attempt_evaluation

    objective = SimpleNamespace(
        id=uuid4(),
        objective_key="daily_report",
        status="active",
        metadata_json={},
        completed_at=None,
        blocked_reason=None,
    )

    changed = apply_attempt_evaluation(
        objective,
        attempted_status="completed",
        evidence="",
        result_summary="done",
    )

    assert changed is False
    assert objective.status == "active"
    assert objective.completed_at is None


def test_completed_objective_records_evidence_and_completion_time():
    from app.services.objective_evaluator import apply_attempt_evaluation

    objective = SimpleNamespace(
        id=uuid4(),
        objective_key="daily_report",
        status="running",
        metadata_json={},
        completed_at=None,
        blocked_reason=None,
    )

    changed = apply_attempt_evaluation(
        objective,
        attempted_status="completed",
        evidence="workspace/daily_report.md sent to Feishu",
        result_summary="Report delivered.",
    )

    assert changed is True
    assert objective.status == "completed"
    assert objective.completed_at is not None
    assert objective.metadata_json["completion_evidence"] == "workspace/daily_report.md sent to Feishu"


def test_failed_attempt_blocks_objective_and_preserves_recovery_signal():
    from app.services.objective_evaluator import apply_attempt_evaluation

    objective = SimpleNamespace(
        id=uuid4(),
        objective_key="daily_report",
        status="running",
        metadata_json={},
        completed_at=None,
        blocked_reason=None,
    )

    changed = apply_attempt_evaluation(
        objective,
        attempted_status="failed",
        evidence="Feishu channel missing",
        result_summary="Cannot send report because Feishu is not configured.",
    )

    assert changed is True
    assert objective.status == "blocked"
    assert objective.blocked_reason == "Feishu channel missing"
    assert objective.metadata_json["last_failure_summary"] == "Cannot send report because Feishu is not configured."


@pytest.mark.asyncio
async def test_focus_session_key_lookup_is_agent_scoped():
    from app.services.objective_evaluator import evaluate_trigger_attempt_by_session_key

    objective = SimpleNamespace(
        id=uuid4(),
        agent_id=uuid4(),
        objective_key="daily_report",
        status="running",
        metadata_json={},
        completed_at=None,
        blocked_reason=None,
    )

    class _Result:
        def scalar_one_or_none(self):
            return objective

    class _DB:
        committed = False

        async def execute(self, stmt):
            assert "agent_objectives.agent_id" in str(stmt)
            return _Result()

        async def commit(self):
            self.committed = True

    db = _DB()
    result = await evaluate_trigger_attempt_by_session_key(
        db,
        agent_id=objective.agent_id,
        objective_session_key="objective:focus:daily_report",
        result_summary="[OBJECTIVE_STATUS: completed]\n[OBJECTIVE_EVIDENCE: delivered to workspace/report.md]",
    )

    assert result["changed"] is True
    assert result["status"] == "completed"
    assert db.committed is True
