from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest


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
        attempts=[],
        include_diagnostics=False,
        now=datetime.now(timezone.utc),
    )

    assert view["display_kind"] == "scheduled_job"
    assert view["display_title"] == "Send the daily report"
    assert view["attention_state"] == "backoff_active"
    assert "retry" in view["next_action"]
    assert view["schedule"]["kind"] == "cron"
    assert "trigger_class" not in view
    assert "objective_id" not in view
    assert "metadata_json" not in view
    assert "diagnostics" not in view


def test_trigger_view_exposes_structured_schedule_for_localized_rendering():
    from app.services.autonomy_overview import build_trigger_view

    def make_trigger(trigger_type: str, config: dict):
        return SimpleNamespace(
            id=uuid4(),
            name="wake",
            type=trigger_type,
            config=config,
            reason="",
            is_enabled=True,
            fire_count=0,
            max_fires=None,
            cooldown_seconds=60,
            last_fired_at=None,
            created_at=None,
            expires_at=None,
        )

    interval = build_trigger_view(make_trigger("interval", {"minutes": 15}), attempts=[], include_diagnostics=False)
    assert interval["schedule"] == {"kind": "interval", "minutes": 15}
    assert interval["display_schedule"] == "Every 15 minutes"

    once = build_trigger_view(
        make_trigger("once", {"at": "2026-08-30T09:00:00Z"}), attempts=[], include_diagnostics=False
    )
    assert once["schedule"] == {"kind": "once", "at": "2026-08-30T09:00:00Z"}

    on_message = build_trigger_view(make_trigger("on_message", {}), attempts=[], include_diagnostics=False)
    assert on_message["schedule"] == {"kind": "on_message"}


def test_trigger_view_diagnostics_are_explicitly_separated():
    from app.services.autonomy_overview import build_trigger_view

    trigger = SimpleNamespace(
        id=uuid4(),
        name="send_report_wake",
        type="once",
        config={"trigger_class": "scheduled_job", "failure_count": 0},
        reason="Follow up on the requested report",
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
        attempts=[],
        include_diagnostics=True,
    )

    assert view["display_kind"] == "scheduled_job"
    assert view["display_title"] == "Follow up on the requested report"
    # Internal config is only exposed under the explicit diagnostics block.
    assert "config" not in view
    assert view["diagnostics"]["trigger_class"] == "scheduled_job"
    assert "objective_id" not in view["diagnostics"]
    assert "focus_ref" not in view["diagnostics"]


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


def test_trigger_view_surfaces_restart_reconciliation_attention():
    from app.services.autonomy_overview import build_trigger_view

    trigger_id = uuid4()
    trigger = SimpleNamespace(
        id=trigger_id,
        name="daily_news",
        type="cron",
        config={"trigger_class": "scheduled_job"},
        reason="Compile the daily AI news feed",
        is_enabled=True,
        fire_count=0,
        max_fires=None,
        cooldown_seconds=60,
        last_fired_at=None,
        created_at=None,
        expires_at=None,
    )
    task = SimpleNamespace(
        id=uuid4(),
        task_type="trigger",
        status="needs_reconciliation",
        result_summary="Trigger run was interrupted after session start; manual reconciliation required.",
        metadata_json={
            "trigger_ids": [str(trigger_id)],
            "needs_reconciliation": True,
            "reconciliation_reason": "session_bound_mutating_trigger",
            "restart_resume_blocker": "session_bound_mutating_trigger",
        },
        created_at=datetime.now(timezone.utc),
        started_at=datetime.now(timezone.utc),
        completed_at=datetime.now(timezone.utc),
        child_session_id="session-1",
    )

    view = build_trigger_view(trigger, attempts=[task])

    assert view["attention_state"] == "needs_reconciliation"
    assert view["attention_reason"] == "Session-bound mutating trigger needs manual reconciliation after restart."
    assert view["next_action"] == "inspect_reconciliation"
    assert view["last_attempt"]["attention_reason"] == (
        "Session-bound mutating trigger needs manual reconciliation after restart."
    )


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


@pytest.mark.asyncio
async def test_artifact_reader_uses_canonical_runtime_task_path(tmp_path, monkeypatch):
    from app.services import autonomy_overview
    from app.services.trigger_artifacts import write_trigger_output_artifact

    agent_id = uuid4()
    task_id = uuid4()
    write_trigger_output_artifact(
        agent_data_dir=tmp_path,
        agent_id=agent_id,
        runtime_task_id=str(task_id),
        triggers=[{"id": str(uuid4()), "name": "daily", "type": "cron", "config": {}}],
        final_reply="Done.",
    )
    monkeypatch.setattr(autonomy_overview, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    view = await autonomy_overview.read_agent_trigger_artifact_view(
        agent_id=agent_id,
        runtime_task_id=str(task_id),
    )

    assert view is not None
    assert view["final_reply"] == "Done."


@pytest.mark.asyncio
async def test_artifact_reader_rejects_foreign_payload_at_canonical_path(tmp_path, monkeypatch):
    import json

    from app.services import autonomy_overview
    from app.services.trigger_artifacts import trigger_output_artifact_ref

    agent_id = uuid4()
    task_id = uuid4()
    artifact = trigger_output_artifact_ref(str(task_id))
    assert artifact is not None
    artifact_path = tmp_path / str(agent_id) / artifact["path"]
    artifact_path.parent.mkdir(parents=True)
    artifact_path.write_text(
        json.dumps(
            {
                "schema": "trigger_output_artifact.v1",
                "runtime_task_id": uuid4().hex,
                "agent_id": str(uuid4()),
                "triggers": [{"name": "foreign"}],
                "final_reply": "foreign content",
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(autonomy_overview, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    view = await autonomy_overview.read_agent_trigger_artifact_view(
        agent_id=agent_id,
        runtime_task_id=str(task_id),
    )

    assert view is None


def test_trigger_view_never_falls_back_to_raw_kind_code_in_display_title():
    """The user-facing title must not borrow the machine kind code.

    When name/reason are empty, display_title stays empty so clients render
    their localized kind label; unknown kinds never enter the user DOM.
    """
    from app.services.autonomy_overview import build_trigger_view

    trigger = SimpleNamespace(
        id=uuid4(),
        name="",
        type="cron",
        config={},
        reason="",
        is_enabled=True,
        fire_count=0,
        max_fires=None,
        cooldown_seconds=60,
        last_fired_at=None,
        created_at=None,
        expires_at=None,
    )

    view = build_trigger_view(trigger, attempts=[], include_diagnostics=False)

    assert view["display_title"] == ""
    assert view["display_kind"] == "scheduled_job"
