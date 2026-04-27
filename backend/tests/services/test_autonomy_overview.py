from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4


def test_trigger_view_default_hides_internal_fields_and_surfaces_backoff():
    from app.services.autonomy_overview import build_trigger_view

    future = datetime.now(timezone.utc) + timedelta(minutes=20)
    trigger = SimpleNamespace(
        id=uuid4(),
        name="daily_report",
        type="cron",
        config={
            "trigger_class": "scheduled_job",
            "backoff_until": future.isoformat(),
            "failure_count": 2,
            "model_id": str(uuid4()),
            "toolset": ["send_email"],
            "workdir": "reports",
        },
        reason="Send the daily report",
        focus_ref=None,
        is_enabled=True,
        fire_count=4,
        max_fires=None,
        cooldown_seconds=60,
        last_fired_at=None,
        created_at=None,
        expires_at=None,
    )

    view = build_trigger_view(
        trigger,
        objectives_by_id={},
        objectives_by_key={},
        attempts=[],
        include_diagnostics=False,
        now=datetime.now(timezone.utc),
    )

    assert view["display_kind"] == "scheduled_job"
    assert view["display_title"] == "Send the daily report"
    assert view["attention_state"] == "backoff_active"
    assert "retry" in view["next_action"]
    assert "trigger_class" not in view
    assert "objective_id" not in view
    assert "metadata_json" not in view
    assert "diagnostics" not in view


def test_trigger_view_diagnostics_are_explicitly_separated():
    from app.services.autonomy_overview import build_trigger_view

    objective_id = uuid4()
    objective = SimpleNamespace(
        id=objective_id,
        objective_key="send_report",
        description="Send report to the team",
        status="proposed",
        priority=1,
        source="conversation",
        success_criteria="Message sent with confirmation",
        blocked_reason=None,
        metadata_json={"requires_approval": True},
        created_at=None,
        updated_at=None,
        completed_at=None,
    )
    trigger = SimpleNamespace(
        id=uuid4(),
        name="send_report_wake",
        type="once",
        config={"trigger_class": "objective_task", "objective_id": str(objective_id)},
        reason="Follow up on the requested report",
        focus_ref="send_report",
        is_enabled=True,
        fire_count=0,
        max_fires=1,
        cooldown_seconds=60,
        last_fired_at=None,
        created_at=None,
        expires_at=None,
    )

    view = build_trigger_view(
        trigger,
        objectives_by_id={str(objective_id): objective},
        objectives_by_key={"send_report": objective},
        attempts=[],
        include_diagnostics=True,
    )

    assert view["display_kind"] == "objective_task"
    assert view["display_title"] == "Send report to the team"
    assert view["attention_state"] == "waiting_approval"
    assert view["next_action"] == "approve_or_reject_objective"
    assert view["linked_objective"]["id"] == str(objective_id)
    assert view["diagnostics"]["trigger_class"] == "objective_task"
    assert view["diagnostics"]["objective_id"] == str(objective_id)
    assert view["diagnostics"]["focus_ref"] == "send_report"


def test_runtime_task_view_maps_internal_skip_reason_to_user_reason():
    from app.services.autonomy_overview import build_runtime_task_view

    task = SimpleNamespace(
        id=uuid4(),
        task_type="trigger",
        status="skipped",
        result_summary="Skipped trigger because no model is configured.",
        metadata_json={
            "skip_reason": "no_model",
            "trigger_ids": ["trigger-1"],
            "objective_ids": ["objective-1"],
            "output_artifact": {"path": "runtime_artifacts/triggers/run.json"},
        },
        created_at=datetime.now(timezone.utc),
        started_at=None,
        completed_at=datetime.now(timezone.utc),
        child_session_id="session-1",
    )

    view = build_runtime_task_view(task, include_diagnostics=False)

    assert view["status"] == "skipped"
    assert view["attention_reason"] == "No model is configured for this autonomous run."
    assert view["artifact"]["path"] == "runtime_artifacts/triggers/run.json"
    assert "metadata_json" not in view
    assert "diagnostics" not in view

    diagnostic_view = build_runtime_task_view(task, include_diagnostics=True)
    assert diagnostic_view["diagnostics"]["skip_reason"] == "no_model"
    assert diagnostic_view["diagnostics"]["runtime_task_id"] == str(task.id)
    assert diagnostic_view["diagnostics"]["trigger_ids"] == ["trigger-1"]


def test_artifact_view_defaults_to_output_not_internal_metadata():
    from app.services.autonomy_overview import build_artifact_view

    raw_payload = {
        "schema": "trigger_output_artifact.v1",
        "runtime_task_id": "runtime-1",
        "created_at": "2026-04-27T09:00:00+00:00",
        "triggers": [{"id": "trigger-1", "name": "daily_report", "trigger_class": "scheduled_job"}],
        "metadata": {"model_id": "model-1", "execution_class": "scheduled_job"},
        "final_reply": "Report delivered.\n[OBJECTIVE_EVIDENCE: workspace/report.md]",
    }

    view = build_artifact_view(raw_payload, include_diagnostics=False)

    assert view["title"] == "daily_report"
    assert view["summary"] == "Report delivered."
    assert view["final_reply"].startswith("Report delivered")
    assert "metadata" not in view
    assert "runtime_task_id" not in view
    assert "diagnostics" not in view

    diagnostic_view = build_artifact_view(raw_payload, include_diagnostics=True)
    assert diagnostic_view["diagnostics"]["runtime_task_id"] == "runtime-1"
    assert diagnostic_view["diagnostics"]["schema"] == "trigger_output_artifact.v1"
