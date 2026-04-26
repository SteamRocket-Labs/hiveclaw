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


def test_unfinished_focus_without_enabled_trigger_is_orphan() -> None:
    from app.services.autonomous_audit import audit_agent_autonomy_snapshot

    agent = _agent()
    report = audit_agent_autonomy_snapshot(
        agent=agent,
        focus_text="# Focus\n\n## Tasks\n- [ ] send_invite :: Send the calendar invite\n",
        triggers=[],
    )

    assert "orphan_focus_task" in _categories(report)
    finding = report["findings"][0]
    assert finding["severity"] == "error"
    assert finding["focus_ref"] == "send_invite"
    assert finding["agent_id"] == str(agent.id)


def test_trigger_focus_ref_missing_is_reported() -> None:
    from app.services.autonomous_audit import audit_agent_autonomy_snapshot

    trigger = _trigger(focus_ref="missing_task")
    report = audit_agent_autonomy_snapshot(
        agent=_agent(),
        focus_text="# Focus\n\n## Tasks\n- [ ] existing_task :: Existing task\n",
        triggers=[trigger],
    )

    assert "trigger_focus_ref_missing" in _categories(report)
    finding = next(item for item in report["findings"] if item["category"] == "trigger_focus_ref_missing")
    assert finding["trigger_id"] == str(trigger.id)
    assert finding["focus_ref"] == "missing_task"


def test_completed_focus_task_with_enabled_trigger_is_reported() -> None:
    from app.services.autonomous_audit import audit_agent_autonomy_snapshot

    report = audit_agent_autonomy_snapshot(
        agent=_agent(),
        focus_text="# Focus\n\n## Tasks\n- [x] send_invite :: Send the calendar invite\n",
        triggers=[_trigger(focus_ref="send_invite")],
    )

    assert "completed_focus_trigger_active" in _categories(report)


def test_scheduled_trigger_without_focus_ref_is_warning() -> None:
    from app.services.autonomous_audit import audit_agent_autonomy_snapshot

    report = audit_agent_autonomy_snapshot(
        agent=_agent(),
        focus_text="# Focus\n\n## Tasks\n",
        triggers=[_trigger(type="interval", config={"minutes": 30}, focus_ref=None)],
    )

    finding = next(item for item in report["findings"] if item["category"] == "scheduled_trigger_without_focus_ref")
    assert finding["severity"] == "warning"


def test_autonomous_agent_without_model_is_error() -> None:
    from app.services.autonomous_audit import audit_agent_autonomy_snapshot

    agent = _agent(primary_model_id=None, heartbeat_enabled=False)
    report = audit_agent_autonomy_snapshot(
        agent=agent,
        focus_text="# Focus\n",
        triggers=[_trigger(type="once", config={"at": "2026-04-27T09:00:00+08:00"})],
    )

    assert "agent_no_model_blocking_autonomy" in _categories(report)


def test_trigger_session_without_runtime_task_is_reported() -> None:
    from app.services.autonomous_audit import audit_agent_autonomy_snapshot

    report = audit_agent_autonomy_snapshot(
        agent=_agent(),
        focus_text="# Focus\n",
        triggers=[],
        trigger_session_count=2,
        trigger_runtime_count=0,
    )

    assert "trigger_runtime_gap" in _categories(report)


def test_heartbeat_session_without_runtime_task_is_reported() -> None:
    from app.services.autonomous_audit import audit_agent_autonomy_snapshot

    report = audit_agent_autonomy_snapshot(
        agent=_agent(),
        focus_text="# Focus\n",
        triggers=[],
        heartbeat_session_count=3,
        heartbeat_runtime_count=0,
    )

    assert "heartbeat_runtime_gap" in _categories(report)


def test_noncanonical_focus_item_is_reported() -> None:
    from app.services.autonomous_audit import audit_agent_autonomy_snapshot

    report = audit_agent_autonomy_snapshot(
        agent=_agent(),
        focus_text="# Focus\n\n## Tasks\n- Send the invite later\n- [ ] canonical_task :: Canonical task\n",
        triggers=[_trigger(focus_ref="canonical_task")],
    )

    finding = next(item for item in report["findings"] if item["category"] == "noncanonical_focus_item")
    assert finding["severity"] == "warning"
    assert "Send the invite later" in finding["evidence"]["line"]


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
                        "category": "orphan_focus_task",
                        "agent_id": str(agent_id),
                        "trigger_id": None,
                        "focus_ref": "send_invite",
                        "message": "Missing trigger",
                        "evidence": {},
                        "recommendation": "Create a trigger",
                    }
                ],
                "counts": {
                    "enabled_triggers": 0,
                    "focus_tasks": 1,
                    "trigger_sessions": 0,
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
    assert report["findings"][0]["category"] == "orphan_focus_task"
