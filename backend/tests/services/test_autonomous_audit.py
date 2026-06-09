from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4


def _trigger(**overrides):
    values = {
        "id": uuid4(),
        "name": "daily_followup",
        "type": "cron",
        "config": {"expr": "0 9 * * *"},
        "reason": "Follow up",
        "focus_ref": None,
        "is_enabled": True,
        "fire_count": 0,
        "last_fired_at": None,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _agent(**overrides):
    values = {
        "id": uuid4(),
        "name": "Ops Agent",
        "tenant_id": uuid4(),
        "heartbeat_enabled": True,
        "primary_model_id": uuid4(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _categories(report: dict) -> set[str]:
    return {finding["category"] for finding in report["findings"]}


# The objective/focus.md audit categories (orphan_focus_task, trigger_focus_ref_missing,
# completed_focus_trigger_active, noncanonical_focus_item) were retired with the
# objective subsystem + focus.md projection. The audit is now trigger + runtime only.


def test_scheduled_trigger_without_focus_ref_is_warning() -> None:
    from app.services.autonomous_audit import audit_agent_autonomy_snapshot

    report = audit_agent_autonomy_snapshot(
        agent=_agent(),
        triggers=[_trigger(type="interval", config={"minutes": 30}, focus_ref=None)],
    )

    finding = next(item for item in report["findings"] if item["category"] == "scheduled_trigger_without_focus_ref")
    assert finding["severity"] == "warning"


def test_autonomous_agent_without_model_is_error() -> None:
    from app.services.autonomous_audit import audit_agent_autonomy_snapshot

    agent = _agent(primary_model_id=None, heartbeat_enabled=False)
    report = audit_agent_autonomy_snapshot(
        agent=agent,
        triggers=[_trigger(type="once", config={"at": "2026-04-27T09:00:00+08:00"})],
    )

    assert "agent_no_model_blocking_autonomy" in _categories(report)


def test_trigger_session_without_runtime_task_is_reported() -> None:
    from app.services.autonomous_audit import audit_agent_autonomy_snapshot

    report = audit_agent_autonomy_snapshot(
        agent=_agent(),
        triggers=[],
        trigger_session_count=2,
        trigger_runtime_count=0,
    )

    assert "trigger_runtime_gap" in _categories(report)


def test_heartbeat_session_without_runtime_task_is_reported() -> None:
    from app.services.autonomous_audit import audit_agent_autonomy_snapshot

    report = audit_agent_autonomy_snapshot(
        agent=_agent(),
        triggers=[],
        heartbeat_session_count=3,
        heartbeat_runtime_count=0,
    )

    assert "heartbeat_runtime_gap" in _categories(report)


def test_build_audit_report_aggregates_agent_totals() -> None:
    from app.services.autonomous_audit import build_autonomous_audit_payload

    agent_id = uuid4()
    report = build_autonomous_audit_payload(
        generated_at=datetime.now(timezone.utc),
        lookback_hours=24,
        agent_reports=[
            {
                "agent_id": str(agent_id),
                "agent_name": "Ops Agent",
                "tenant_id": str(uuid4()),
                "findings": [
                    {
                        "severity": "error",
                        "category": "trigger_runtime_gap",
                        "agent_id": str(agent_id),
                        "trigger_id": None,
                        "message": "Trigger sessions without runtime tasks",
                        "evidence": {},
                        "recommendation": "Investigate the trigger daemon",
                    }
                ],
                "counts": {
                    "enabled_triggers": 0,
                    "trigger_sessions": 2,
                    "heartbeat_sessions": 0,
                    "trigger_runtime_tasks": 0,
                    "heartbeat_runtime_tasks": 0,
                },
            }
        ],
    )

    assert report["totals"]["agents"] == 1
    assert report["totals"]["findings"] == 1
    assert report["totals"]["errors"] == 1
    assert report["findings"][0]["category"] == "trigger_runtime_gap"
