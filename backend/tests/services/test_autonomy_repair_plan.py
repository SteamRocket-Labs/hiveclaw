from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4


def _finding(category: str, *, agent_id, severity: str = "error", trigger_id=None, focus_ref=None, evidence=None):
    return {
        "severity": severity,
        "category": category,
        "agent_id": str(agent_id),
        "trigger_id": str(trigger_id) if trigger_id else None,
        "focus_ref": focus_ref,
        "message": f"{category} message",
        "evidence": evidence or {},
        "recommendation": f"{category} recommendation",
    }


def _audit_report(*findings, lookback_hours: int = 24):
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_hours": lookback_hours,
        "totals": {
            "agents": 1,
            "findings": len(findings),
            "errors": sum(1 for item in findings if item["severity"] == "error"),
            "warnings": sum(1 for item in findings if item["severity"] == "warning"),
            "infos": sum(1 for item in findings if item["severity"] == "info"),
        },
        "findings": list(findings),
        "agents": [],
    }


def _agent(**overrides):
    values = {
        "id": uuid4(),
        "name": "Ops Agent",
        "tenant_id": uuid4(),
        "primary_model_id": uuid4(),
        "heartbeat_enabled": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _objective(**overrides):
    values = {
        "id": uuid4(),
        "agent_id": uuid4(),
        "tenant_id": uuid4(),
        "objective_key": "send_report",
        "description": "Send the report",
        "status": "open",
        "success_criteria": "Report sent",
        "metadata_json": {"wake_policy": {"type": "cron", "config": {"expr": "0 9 * * *"}}},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _trigger(**overrides):
    values = {
        "id": uuid4(),
        "agent_id": uuid4(),
        "name": "daily_report",
        "type": "cron",
        "config": {"expr": "0 9 * * *"},
        "focus_ref": None,
        "is_enabled": True,
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _action_types(plan: dict) -> set[str]:
    return {action["action_type"] for action in plan["actions"]}


def test_active_objective_and_orphan_focus_dedupe_to_one_wake_policy_action() -> None:
    from app.services.autonomy_repair_plan import plan_autonomy_repair_actions

    agent = _agent()
    objective = _objective(agent_id=agent.id, tenant_id=agent.tenant_id, objective_key="send_report")
    audit = _audit_report(
        _finding(
            "active_objective_without_wake_policy",
            agent_id=agent.id,
            focus_ref="send_report",
            evidence={"objective_id": str(objective.id), "status": "open"},
        ),
        _finding("orphan_focus_task", agent_id=agent.id, focus_ref="send_report"),
    )

    plan = plan_autonomy_repair_actions(
        audit_report=audit,
        agents=[agent],
        triggers=[],
        objectives=[objective],
        default_models_by_tenant={},
    )

    assert _action_types(plan) == {"create_objective_wake_policy"}
    action = plan["actions"][0]
    assert action["auto_apply"] is True
    assert action["risk"] == "low"
    assert action["agent_id"] == str(agent.id)
    assert action["objective_id"] == str(objective.id)
    assert action["focus_ref"] == "send_report"
    assert action["finding_categories"] == ["active_objective_without_wake_policy", "orphan_focus_task"]
    assert action["proposed_change"]["create_trigger"]["config"]["trigger_class"] == "objective_task"
    assert action["proposed_change"]["create_trigger"]["config"]["objective_id"] == str(objective.id)


def test_scheduled_trigger_without_focus_ref_can_be_classified_as_scheduled_job() -> None:
    from app.services.autonomy_repair_plan import plan_autonomy_repair_actions

    agent = _agent()
    trigger = _trigger(agent_id=agent.id, config={"expr": "0 9 * * *"})
    audit = _audit_report(
        _finding(
            "scheduled_trigger_without_focus_ref",
            agent_id=agent.id,
            severity="warning",
            trigger_id=trigger.id,
        )
    )

    plan = plan_autonomy_repair_actions(
        audit_report=audit,
        agents=[agent],
        triggers=[trigger],
        objectives=[],
        default_models_by_tenant={},
    )

    action = plan["actions"][0]
    assert action["action_type"] == "classify_scheduled_trigger"
    assert action["auto_apply"] is True
    assert action["risk"] == "low"
    assert action["trigger_id"] == str(trigger.id)
    assert action["proposed_change"]["set_config"]["trigger_class"] == "scheduled_job"


def test_agent_without_model_uses_tenant_default_model_when_available() -> None:
    from app.services.autonomy_repair_plan import plan_autonomy_repair_actions

    tenant_id = uuid4()
    model_id = uuid4()
    agent = _agent(tenant_id=tenant_id, primary_model_id=None)
    audit = _audit_report(_finding("agent_no_model_blocking_autonomy", agent_id=agent.id))

    plan = plan_autonomy_repair_actions(
        audit_report=audit,
        agents=[agent],
        triggers=[],
        objectives=[],
        default_models_by_tenant={
            str(tenant_id): SimpleNamespace(id=model_id, label="Default Claude", provider="anthropic", model="claude")
        },
    )

    action = plan["actions"][0]
    assert action["action_type"] == "assign_default_primary_model"
    assert action["auto_apply"] is True
    assert action["risk"] == "medium"
    assert action["proposed_change"]["set_agent"]["primary_model_id"] == str(model_id)
    assert action["evidence"]["model_label"] == "Default Claude"


def test_missing_trigger_focus_ref_can_be_repaired_from_bound_objective_id() -> None:
    from app.services.autonomy_repair_plan import plan_autonomy_repair_actions

    agent = _agent()
    objective = _objective(agent_id=agent.id, tenant_id=agent.tenant_id, objective_key="weekly_report")
    trigger = _trigger(
        agent_id=agent.id,
        focus_ref="stale_ref",
        config={"expr": "0 9 * * 1", "trigger_class": "objective_task", "objective_id": str(objective.id)},
    )
    audit = _audit_report(
        _finding(
            "trigger_focus_ref_missing",
            agent_id=agent.id,
            trigger_id=trigger.id,
            focus_ref="stale_ref",
        )
    )

    plan = plan_autonomy_repair_actions(
        audit_report=audit,
        agents=[agent],
        triggers=[trigger],
        objectives=[objective],
        default_models_by_tenant={},
    )

    action = plan["actions"][0]
    assert action["action_type"] == "repair_trigger_focus_ref_from_objective"
    assert action["auto_apply"] is True
    assert action["risk"] == "low"
    assert action["proposed_change"]["set_trigger"]["focus_ref"] == "weekly_report"


def test_missing_trigger_focus_ref_creates_objective_for_existing_wake_policy() -> None:
    from app.services.autonomy_repair_plan import plan_autonomy_repair_actions

    agent = _agent()
    trigger = _trigger(
        agent_id=agent.id,
        name="daily_scan",
        focus_ref="daily_scan",
        reason="Scan the source daily",
        config={"expr": "0 9 * * *"},
    )
    audit = _audit_report(
        _finding(
            "trigger_focus_ref_missing",
            agent_id=agent.id,
            trigger_id=trigger.id,
            focus_ref="daily_scan",
        )
    )

    plan = plan_autonomy_repair_actions(
        audit_report=audit,
        agents=[agent],
        triggers=[trigger],
        objectives=[],
        default_models_by_tenant={},
    )

    action = plan["actions"][0]
    assert action["action_type"] == "create_objective_for_existing_trigger"
    assert action["auto_apply"] is True
    assert action["risk"] == "medium"
    assert action["focus_ref"] == "daily_scan"
    assert action["proposed_change"]["create_objective"]["objective_key"] == "daily_scan"
    assert action["proposed_change"]["update_trigger_config"]["trigger_class"] == "objective_task"


def test_agent_without_model_and_without_default_requires_model_configuration() -> None:
    from app.services.autonomy_repair_plan import plan_autonomy_repair_actions

    agent = _agent(primary_model_id=None, heartbeat_enabled=True)
    audit = _audit_report(
        _finding(
            "agent_no_model_blocking_autonomy",
            agent_id=agent.id,
            evidence={"heartbeat_enabled": True, "enabled_triggers": 0},
        )
    )

    plan = plan_autonomy_repair_actions(
        audit_report=audit,
        agents=[agent],
        triggers=[],
        objectives=[],
        default_models_by_tenant={},
    )

    action = plan["actions"][0]
    assert action["action_type"] == "configure_primary_model"
    assert action["auto_apply"] is False
    assert action["risk"] == "high"
    assert "set_agent" not in action.get("proposed_change", {})


def test_completed_focus_trigger_active_proposes_disabling_trigger() -> None:
    from app.services.autonomy_repair_plan import plan_autonomy_repair_actions

    agent = _agent()
    trigger = _trigger(agent_id=agent.id, focus_ref="done_task")
    audit = _audit_report(
        _finding(
            "completed_focus_trigger_active",
            agent_id=agent.id,
            trigger_id=trigger.id,
            focus_ref="done_task",
        )
    )

    plan = plan_autonomy_repair_actions(
        audit_report=audit,
        agents=[agent],
        triggers=[trigger],
        objectives=[],
        default_models_by_tenant={},
    )

    action = plan["actions"][0]
    assert action["action_type"] == "disable_completed_focus_trigger"
    assert action["auto_apply"] is True
    assert action["risk"] == "low"
    assert action["proposed_change"]["set_trigger"]["is_enabled"] is False
    assert plan["totals"]["auto_applyable_actions"] == 1


class _FakeDB:
    def __init__(self):
        self.added = []
        self.flushed = 0
        self.committed = 0

    def add(self, item):
        self.added.append(item)

    async def flush(self):
        self.flushed += 1

    async def commit(self):
        self.committed += 1


async def test_apply_autonomy_repair_actions_updates_safe_existing_objects() -> None:
    from app.services.autonomy_repair_plan import apply_autonomy_repair_actions

    tenant_id = uuid4()
    model_id = uuid4()
    agent = _agent(tenant_id=tenant_id, primary_model_id=None)
    scheduled = _trigger(agent_id=agent.id, focus_ref=None, config={"expr": "0 9 * * *"})
    missing = _trigger(agent_id=agent.id, focus_ref="daily_scan", config={"expr": "0 10 * * *"})
    audit = _audit_report(
        _finding("scheduled_trigger_without_focus_ref", agent_id=agent.id, severity="warning", trigger_id=scheduled.id),
        _finding("trigger_focus_ref_missing", agent_id=agent.id, trigger_id=missing.id, focus_ref="daily_scan"),
        _finding("agent_no_model_blocking_autonomy", agent_id=agent.id),
    )
    fake_db = _FakeDB()

    result = await apply_autonomy_repair_actions(
        db=fake_db,
        audit_report=audit,
        agents=[agent],
        triggers=[scheduled, missing],
        objectives=[],
        default_models_by_tenant={
            str(tenant_id): SimpleNamespace(id=model_id, label="Default", provider="openai", model="gpt")
        },
        commit=True,
    )

    assert result["totals"]["applied"] == 3
    assert scheduled.config["trigger_class"] == "scheduled_job"
    assert missing.config["trigger_class"] == "objective_task"
    assert missing.config["objective_id"]
    assert missing.focus_ref == "daily_scan"
    assert agent.primary_model_id == model_id
    assert len(fake_db.added) == 1
    assert fake_db.committed == 1


async def test_apply_objective_wake_policy_reuses_existing_trigger_name() -> None:
    from app.services.objective_wake_reconciler import build_objective_trigger_payload
    from app.services.autonomy_repair_plan import apply_autonomy_repair_actions

    agent = _agent()
    objective = _objective(agent_id=agent.id, tenant_id=agent.tenant_id, objective_key="task_1")
    payload = build_objective_trigger_payload(objective)
    existing = _trigger(
        agent_id=agent.id,
        name=payload["name"],
        type="once",
        config={"at": "2026-04-27T09:00:00+00:00"},
        focus_ref=None,
        is_enabled=False,
    )
    audit = _audit_report(
        _finding(
            "active_objective_without_wake_policy",
            agent_id=agent.id,
            focus_ref="task_1",
            evidence={"objective_id": str(objective.id), "status": "open"},
        )
    )
    fake_db = _FakeDB()

    result = await apply_autonomy_repair_actions(
        db=fake_db,
        audit_report=audit,
        agents=[agent],
        triggers=[existing],
        objectives=[objective],
        default_models_by_tenant={},
        commit=True,
    )

    assert result["totals"]["applied"] == 1
    assert fake_db.added == []
    assert existing.is_enabled is True
    assert existing.focus_ref == "task_1"
    assert existing.config["trigger_class"] == "objective_task"
    assert existing.config["objective_id"] == str(objective.id)


async def test_apply_autonomy_repair_actions_skips_manual_actions_by_default() -> None:
    from app.services.autonomy_repair_plan import apply_autonomy_repair_actions

    agent = _agent(primary_model_id=None, heartbeat_enabled=False)
    audit = _audit_report(
        _finding(
            "agent_no_model_blocking_autonomy",
            agent_id=agent.id,
            evidence={"heartbeat_enabled": False, "enabled_triggers": 1},
        )
    )

    result = await apply_autonomy_repair_actions(
        db=_FakeDB(),
        audit_report=audit,
        agents=[agent],
        triggers=[],
        objectives=[],
        default_models_by_tenant={},
        commit=True,
    )

    assert result["totals"]["applied"] == 0
    assert result["totals"]["skipped"] == 1
    assert result["results"][0]["status"] == "skipped"
