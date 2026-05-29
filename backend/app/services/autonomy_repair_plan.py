"""Read-only dry-run repair planner for autonomous system audit findings."""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.llm import LLMModel
from app.models.objective import AgentObjective
from app.models.tenant_setting import TenantSetting
from app.models.trigger import AgentTrigger
from app.services.autonomous_audit import build_autonomous_audit_report
from app.services.focus_state import normalize_focus_task_id
from app.services.objective_wake_reconciler import build_objective_trigger_payload, objective_has_enabled_wake


_ACTIVE_OBJECTIVE_STATUSES = {"active", "open", "running"}
_RISK_RANK = {"low": 0, "medium": 1, "high": 2}


def _as_str(value: Any) -> str | None:
    return str(value) if value is not None else None


def _value(item: Any, name: str, default: Any = None) -> Any:
    if isinstance(item, dict):
        return item.get(name, default)
    return getattr(item, name, default)


def _config(trigger: Any | None) -> dict[str, Any]:
    if trigger is None:
        return {}
    raw = _value(trigger, "config", {}) or {}
    return dict(raw) if isinstance(raw, dict) else {}


def _stable_action_id(
    action_type: str,
    *,
    agent_id: str | None = None,
    trigger_id: str | None = None,
    objective_id: str | None = None,
    focus_ref: str | None = None,
) -> str:
    seed = "|".join([
        action_type,
        agent_id or "",
        trigger_id or "",
        objective_id or "",
        focus_ref or "",
    ])
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:16]


def _agent_label(agent: Any | None) -> tuple[str | None, str | None]:
    if agent is None:
        return None, None
    return _as_str(_value(agent, "tenant_id")), _value(agent, "name")


def _normalize_ref(value: Any) -> str | None:
    raw = str(value or "").strip()
    return normalize_focus_task_id(raw) if raw else None


def _index_by_id(items: list[Any]) -> dict[str, Any]:
    indexed: dict[str, Any] = {}
    for item in items:
        item_id = _as_str(_value(item, "id"))
        if item_id:
            indexed[item_id] = item
    return indexed


def _objective_indexes(objectives: list[Any]) -> tuple[dict[str, Any], dict[tuple[str, str], Any]]:
    by_id = _index_by_id(objectives)
    by_agent_ref: dict[tuple[str, str], Any] = {}
    for objective in objectives:
        agent_id = _as_str(_value(objective, "agent_id"))
        ref = _normalize_ref(_value(objective, "objective_key"))
        if agent_id and ref:
            by_agent_ref[(agent_id, ref)] = objective
    return by_id, by_agent_ref


def _resolve_objective(
    finding: dict[str, Any],
    *,
    objectives_by_id: dict[str, Any],
    objectives_by_agent_ref: dict[tuple[str, str], Any],
) -> Any | None:
    evidence = finding.get("evidence") if isinstance(finding.get("evidence"), dict) else {}
    objective_id = _as_str(evidence.get("objective_id"))
    if objective_id and objective_id in objectives_by_id:
        return objectives_by_id[objective_id]
    agent_id = _as_str(finding.get("agent_id"))
    focus_ref = _normalize_ref(finding.get("focus_ref"))
    if agent_id and focus_ref:
        return objectives_by_agent_ref.get((agent_id, focus_ref))
    return None


def _merge_action(actions: dict[str, dict[str, Any]], action: dict[str, Any], category: str) -> None:
    action_id = action["action_id"]
    if action_id not in actions:
        action["finding_categories"] = [category]
        actions[action_id] = action
        return
    existing = actions[action_id]
    categories = set(existing.get("finding_categories", []))
    categories.add(category)
    existing["finding_categories"] = sorted(categories)


def _action(
    *,
    action_type: str,
    agent: Any | None,
    trigger: Any | None = None,
    objective: Any | None = None,
    focus_ref: str | None = None,
    summary: str,
    auto_apply: bool,
    risk: str,
    evidence: dict[str, Any] | None = None,
    proposed_change: dict[str, Any] | None = None,
    preconditions: list[str] | None = None,
    manual_steps: list[str] | None = None,
) -> dict[str, Any]:
    agent_id = _as_str(_value(agent, "id")) if agent is not None else None
    trigger_id = _as_str(_value(trigger, "id")) if trigger is not None else None
    objective_id = _as_str(_value(objective, "id")) if objective is not None else None
    tenant_id, agent_name = _agent_label(agent)
    normalized_focus_ref = _normalize_ref(focus_ref) or _normalize_ref(_value(objective, "objective_key", None))
    return {
        "action_id": _stable_action_id(
            action_type,
            agent_id=agent_id,
            trigger_id=trigger_id,
            objective_id=objective_id,
            focus_ref=normalized_focus_ref,
        ),
        "action_type": action_type,
        "auto_apply": auto_apply,
        "risk": risk,
        "agent_id": agent_id,
        "agent_name": agent_name,
        "tenant_id": tenant_id,
        "trigger_id": trigger_id,
        "objective_id": objective_id,
        "focus_ref": normalized_focus_ref,
        "summary": summary,
        "finding_categories": [],
        "evidence": evidence or {},
        "proposed_change": proposed_change or {},
        "preconditions": preconditions or [],
        "manual_steps": manual_steps or [],
    }


def _model_evidence(model: Any) -> dict[str, Any]:
    return {
        "model_id": _as_str(_value(model, "id")),
        "model_label": _value(model, "label"),
        "provider": _value(model, "provider"),
        "model": _value(model, "model"),
    }


def _totals(*, agents: list[Any], audit_findings: list[dict[str, Any]], actions: list[dict[str, Any]]) -> dict[str, Any]:
    by_action_type: dict[str, int] = {}
    by_risk: dict[str, int] = {}
    for action in actions:
        action_type = str(action.get("action_type") or "unknown")
        risk = str(action.get("risk") or "unknown")
        by_action_type[action_type] = by_action_type.get(action_type, 0) + 1
        by_risk[risk] = by_risk.get(risk, 0) + 1
    return {
        "agents": len(agents),
        "audit_findings": len(audit_findings),
        "actions": len(actions),
        "auto_applyable_actions": sum(1 for action in actions if action.get("auto_apply") is True),
        "manual_actions": sum(1 for action in actions if action.get("auto_apply") is not True),
        "by_action_type": by_action_type,
        "by_risk": by_risk,
    }


def _enabled_trigger_count_by_agent(triggers: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for trigger in triggers:
        if not bool(_value(trigger, "is_enabled", False)):
            continue
        agent_id = _as_str(_value(trigger, "agent_id"))
        if agent_id:
            counts[agent_id] = counts.get(agent_id, 0) + 1
    return counts


def _trigger_description(trigger: Any) -> str:
    reason = str(_value(trigger, "reason", "") or "").strip()
    if reason:
        return reason.splitlines()[0][:500]
    name = str(_value(trigger, "name", "") or "").strip()
    return name or "Objective from existing trigger"


def plan_autonomy_repair_actions(
    *,
    audit_report: dict[str, Any],
    agents: list[Any],
    triggers: list[Any],
    objectives: list[Any],
    default_models_by_tenant: dict[str, Any],
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    """Convert audit findings into a deterministic dry-run repair plan.

    This function is intentionally side-effect free. Auto-applyable only means
    that the proposed mutation has enough evidence to be applied by a future
    explicit apply endpoint; this function never mutates DB or files.
    """
    now = generated_at or datetime.now(timezone.utc)
    findings = [item for item in audit_report.get("findings", []) if isinstance(item, dict)]
    agents_by_id = _index_by_id(agents)
    triggers_by_id = _index_by_id(triggers)
    objectives_by_id, objectives_by_agent_ref = _objective_indexes(objectives)
    actions_by_id: dict[str, dict[str, Any]] = {}

    for finding in findings:
        category = str(finding.get("category") or "").strip()
        agent_id = _as_str(finding.get("agent_id"))
        trigger_id = _as_str(finding.get("trigger_id"))
        focus_ref = _normalize_ref(finding.get("focus_ref"))
        agent = agents_by_id.get(agent_id or "")
        trigger = triggers_by_id.get(trigger_id or "")
        objective = _resolve_objective(
            finding,
            objectives_by_id=objectives_by_id,
            objectives_by_agent_ref=objectives_by_agent_ref,
        )

        if category in {"active_objective_without_wake_policy", "orphan_focus_task"}:
            if objective is not None:
                payload = build_objective_trigger_payload(objective, now=now)
                action = _action(
                    action_type="create_objective_wake_policy",
                    agent=agent,
                    objective=objective,
                    focus_ref=focus_ref,
                    summary="Create an enabled objective_task wake policy for the active objective.",
                    auto_apply=True,
                    risk="low",
                    evidence={
                        "objective_status": _value(objective, "status"),
                        "objective_key": _value(objective, "objective_key"),
                    },
                    proposed_change={"create_trigger": payload},
                    preconditions=[
                        "Objective is still active/open/running.",
                        "No enabled objective_task trigger is already bound to the objective.",
                    ],
                )
            else:
                action = _action(
                    action_type="review_orphan_focus_task",
                    agent=agent,
                    focus_ref=focus_ref,
                    summary="Review orphan focus task before creating a wake policy.",
                    auto_apply=False,
                    risk="medium",
                    evidence={"finding": finding},
                    manual_steps=[
                        "Confirm whether the focus item is still a real objective.",
                        "Create an objective and wake policy, or mark it manual/no-wake.",
                    ],
                )
            _merge_action(actions_by_id, action, category)
            continue

        if category == "scheduled_trigger_without_focus_ref":
            if trigger is not None:
                config = _config(trigger)
                action = _action(
                    action_type="classify_scheduled_trigger",
                    agent=agent,
                    trigger=trigger,
                    summary="Mark standalone scheduled trigger as scheduled_job.",
                    auto_apply=True,
                    risk="low",
                    evidence={"trigger_name": _value(trigger, "name"), "trigger_type": _value(trigger, "type")},
                    proposed_change={"set_config": {**config, "trigger_class": "scheduled_job"}},
                    preconditions=[
                        "Trigger still has no focus_ref.",
                        "Trigger is still a standalone scheduled job rather than objective work.",
                    ],
                )
            else:
                action = _action(
                    action_type="review_missing_trigger",
                    agent=agent,
                    focus_ref=focus_ref,
                    summary="Audit finding references a trigger that was not found in the current snapshot.",
                    auto_apply=False,
                    risk="medium",
                    evidence={"finding": finding},
                    manual_steps=["Reload audit snapshot and confirm whether the trigger was deleted or changed."],
                )
            _merge_action(actions_by_id, action, category)
            continue

        if category == "agent_no_model_blocking_autonomy":
            tenant_id = _as_str(_value(agent, "tenant_id")) if agent is not None else None
            default_model = default_models_by_tenant.get(tenant_id or "")
            if default_model is not None:
                evidence = _model_evidence(default_model)
                action = _action(
                    action_type="assign_default_primary_model",
                    agent=agent,
                    summary="Assign the tenant default enabled model as the agent primary model.",
                    auto_apply=True,
                    risk="medium",
                    evidence=evidence,
                    proposed_change={"set_agent": {"primary_model_id": evidence["model_id"]}},
                    preconditions=[
                        "Agent still has no primary_model_id.",
                        "Default model remains enabled for the same tenant.",
                    ],
                )
            else:
                action = _action(
                    action_type="configure_primary_model",
                    agent=agent,
                    summary="Configure an enabled primary model before autonomous or background maintenance runs can execute.",
                    auto_apply=False,
                    risk="high",
                    evidence={"tenant_id": tenant_id},
                    manual_steps=[
                        "Add or enable a tenant LLM model.",
                        "Set tenant default_model_id or assign agent.primary_model_id.",
                    ],
                )
            _merge_action(actions_by_id, action, category)
            continue

        if category == "trigger_focus_ref_missing":
            config = _config(trigger)
            bound_objective = objectives_by_id.get(str(config.get("objective_id") or "").strip())
            if trigger is not None and bound_objective is not None:
                objective_key = _normalize_ref(_value(bound_objective, "objective_key"))
                action = _action(
                    action_type="repair_trigger_focus_ref_from_objective",
                    agent=agent,
                    trigger=trigger,
                    objective=bound_objective,
                    focus_ref=objective_key,
                    summary="Repair trigger.focus_ref from its bound objective_id.",
                    auto_apply=True,
                    risk="low",
                    evidence={
                        "trigger_name": _value(trigger, "name"),
                        "old_focus_ref": focus_ref,
                        "objective_key": objective_key,
                    },
                    proposed_change={"set_trigger": {"focus_ref": objective_key}},
                    preconditions=[
                        "Trigger still points to the same objective_id.",
                        "Bound objective still exists and is visible in the focus projection.",
                    ],
                )
            elif trigger is not None and focus_ref:
                description = _trigger_description(trigger)
                objective_payload = {
                    "agent_id": agent_id,
                    "tenant_id": _as_str(_value(agent, "tenant_id")) if agent is not None else None,
                    "objective_key": focus_ref,
                    "description": description,
                    "status": "open",
                    "source": "trigger_repair",
                    "metadata_json": {
                        "source": "trigger_repair",
                        "trigger_id": _as_str(_value(trigger, "id")),
                        "trigger_name": _value(trigger, "name"),
                    },
                }
                action = _action(
                    action_type="create_objective_for_existing_trigger",
                    agent=agent,
                    trigger=trigger,
                    focus_ref=focus_ref,
                    summary="Create an objective ledger entry for an existing enabled trigger.",
                    auto_apply=True,
                    risk="medium",
                    evidence={
                        "trigger_name": _value(trigger, "name"),
                        "trigger_type": _value(trigger, "type"),
                        "old_focus_ref": focus_ref,
                    },
                    proposed_change={
                        "create_objective": objective_payload,
                        "update_trigger_config": {**config, "trigger_class": "objective_task"},
                    },
                    preconditions=[
                        "Trigger still exists and is enabled.",
                        "No objective with the same focus_ref exists for the agent.",
                        "Creating the objective only records already-enabled wake behavior.",
                    ],
                )
            else:
                action = _action(
                    action_type="review_missing_focus_ref",
                    agent=agent,
                    trigger=trigger,
                    focus_ref=focus_ref,
                    summary="Review trigger with missing focus_ref before changing business behavior.",
                    auto_apply=False,
                    risk="medium",
                    evidence={"finding": finding, "trigger_config": config},
                    manual_steps=[
                        "Create the missing objective if the trigger is valid task work.",
                        "Update focus_ref to an existing objective if it was renamed.",
                        "Disable or delete the trigger if the objective is obsolete.",
                    ],
                )
            _merge_action(actions_by_id, action, category)
            continue

        if category == "completed_focus_trigger_active":
            action = _action(
                action_type="disable_completed_focus_trigger",
                agent=agent,
                trigger=trigger,
                focus_ref=focus_ref,
                summary="Disable trigger bound to a completed focus objective.",
                auto_apply=trigger is not None,
                risk="low" if trigger is not None else "medium",
                evidence={"trigger_name": _value(trigger, "name") if trigger is not None else None},
                proposed_change={"set_trigger": {"is_enabled": False}} if trigger is not None else {},
                preconditions=["Trigger is still enabled and bound to the completed focus_ref."] if trigger is not None else [],
                manual_steps=[] if trigger is not None else ["Reload audit snapshot and locate the trigger manually."],
            )
            _merge_action(actions_by_id, action, category)
            continue

        if category in {"trigger_runtime_gap", "heartbeat_runtime_gap"}:
            action = _action(
                action_type="verify_runtime_ledger_after_deploy",
                agent=agent,
                summary="Runtime ledger gaps are historical; verify with a short lookback after the deployed P1 code runs.",
                auto_apply=False,
                risk="low",
                evidence={"finding": finding},
                manual_steps=[
                    "Run autonomous-audit with lookback_hours=1 after trigger/heartbeat activity.",
                    "Only fix code if the short-window runtime gap still reproduces.",
                ],
            )
            _merge_action(actions_by_id, action, category)
            continue

        action = _action(
            action_type=f"manual_review_{category or 'unknown'}",
            agent=agent,
            trigger=trigger,
            focus_ref=focus_ref,
            summary="Manual review required for this audit finding.",
            auto_apply=False,
            risk="medium",
            evidence={"finding": finding},
            manual_steps=["Inspect the audit evidence and choose the least invasive repair."],
        )
        _merge_action(actions_by_id, action, category or "unknown")

    actions = sorted(
        actions_by_id.values(),
        key=lambda item: (
            0 if item.get("auto_apply") else 1,
            {"low": 0, "medium": 1, "high": 2}.get(str(item.get("risk")), 3),
            str(item.get("action_type")),
            str(item.get("agent_name") or item.get("agent_id") or ""),
        ),
    )

    return {
        "schema": "autonomy_repair_plan.v1",
        "mode": "dry_run",
        "generated_at": now.isoformat(),
        "lookback_hours": int(audit_report.get("lookback_hours") or 0),
        "totals": _totals(agents=agents, audit_findings=findings, actions=actions),
        "actions": actions,
        "audit_totals": audit_report.get("totals", {}),
    }


def _risk_allowed(risk: str, max_risk: str) -> bool:
    return _RISK_RANK.get(risk, 99) <= _RISK_RANK.get(max_risk, 1)


def _result(action: dict[str, Any], status: str, message: str, evidence: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "action_id": action.get("action_id"),
        "action_type": action.get("action_type"),
        "status": status,
        "message": message,
        "agent_id": action.get("agent_id"),
        "trigger_id": action.get("trigger_id"),
        "objective_id": action.get("objective_id"),
        "focus_ref": action.get("focus_ref"),
        "evidence": evidence or {},
    }


def _apply_totals(results: list[dict[str, Any]], planned: int) -> dict[str, int]:
    return {
        "planned": planned,
        "applied": sum(1 for result in results if result.get("status") == "applied"),
        "skipped": sum(1 for result in results if result.get("status") == "skipped"),
        "failed": sum(1 for result in results if result.get("status") == "failed"),
    }


def _existing_objective_for_ref(objectives: list[Any], agent_id: str, focus_ref: str) -> Any | None:
    for objective in objectives:
        if _as_str(_value(objective, "agent_id")) != agent_id:
            continue
        if _normalize_ref(_value(objective, "objective_key")) == focus_ref:
            return objective
    return None


def _existing_trigger_for_name(triggers: list[Any], agent_id: str, name: str) -> Any | None:
    for trigger in triggers:
        if _as_str(_value(trigger, "agent_id")) != agent_id:
            continue
        if str(_value(trigger, "name", "") or "") == name:
            return trigger
    return None


async def _flush_if_possible(db: Any) -> None:
    flush = getattr(db, "flush", None)
    if flush is not None:
        await flush()


async def apply_autonomy_repair_actions(
    *,
    db: Any,
    audit_report: dict[str, Any],
    agents: list[Any],
    triggers: list[Any],
    objectives: list[Any],
    default_models_by_tenant: dict[str, Any],
    action_ids: list[str] | None = None,
    max_risk: str = "medium",
    commit: bool = True,
) -> dict[str, Any]:
    """Apply auto-approved autonomy repair actions with precondition checks."""
    plan = plan_autonomy_repair_actions(
        audit_report=audit_report,
        agents=agents,
        triggers=triggers,
        objectives=objectives,
        default_models_by_tenant=default_models_by_tenant,
    )
    selected = set(action_ids or [])
    agents_by_id = _index_by_id(agents)
    triggers_by_id = _index_by_id(triggers)
    objectives_by_id = _index_by_id(objectives)
    results: list[dict[str, Any]] = []

    for action in plan["actions"]:
        if selected and action["action_id"] not in selected:
            continue
        if not action.get("auto_apply"):
            results.append(_result(action, "skipped", "Action is not auto-applyable."))
            continue
        if not _risk_allowed(str(action.get("risk") or "medium"), max_risk):
            results.append(_result(action, "skipped", f"Risk exceeds max_risk={max_risk}."))
            continue

        try:
            action_type = action["action_type"]
            agent = agents_by_id.get(str(action.get("agent_id") or ""))
            trigger = triggers_by_id.get(str(action.get("trigger_id") or ""))
            objective = objectives_by_id.get(str(action.get("objective_id") or ""))
            focus_ref = _normalize_ref(action.get("focus_ref"))

            if action_type == "classify_scheduled_trigger":
                if trigger is None:
                    results.append(_result(action, "skipped", "Trigger no longer exists."))
                    continue
                if _normalize_ref(_value(trigger, "focus_ref")):
                    results.append(_result(action, "skipped", "Trigger now has focus_ref."))
                    continue
                config = _config(trigger)
                if config.get("trigger_class") == "scheduled_job":
                    results.append(_result(action, "skipped", "Trigger already classified as scheduled_job."))
                    continue
                config["trigger_class"] = "scheduled_job"
                trigger.config = config
                results.append(_result(action, "applied", "Trigger classified as scheduled_job."))
                continue

            if action_type == "create_objective_wake_policy":
                if objective is None:
                    results.append(_result(action, "skipped", "Objective no longer exists."))
                    continue
                if str(_value(objective, "status", "open")) not in _ACTIVE_OBJECTIVE_STATUSES:
                    results.append(_result(action, "skipped", "Objective is no longer active."))
                    continue
                if objective_has_enabled_wake(objective, triggers):
                    results.append(_result(action, "skipped", "Objective already has enabled wake policy."))
                    continue
                payload = build_objective_trigger_payload(objective)
                existing_trigger = _existing_trigger_for_name(
                    triggers,
                    str(_value(objective, "agent_id")),
                    payload["name"],
                )
                if existing_trigger is not None:
                    existing_trigger.type = payload["type"]
                    existing_trigger.config = payload["config"]
                    existing_trigger.reason = payload["reason"]
                    existing_trigger.focus_ref = payload["focus_ref"]
                    existing_trigger.is_enabled = True
                    results.append(_result(action, "applied", "Existing objective wake policy reused and enabled."))
                    continue
                trigger_obj = AgentTrigger(
                    agent_id=_value(objective, "agent_id"),
                    name=payload["name"],
                    type=payload["type"],
                    config=payload["config"],
                    reason=payload["reason"],
                    focus_ref=payload["focus_ref"],
                )
                db.add(trigger_obj)
                triggers.append(trigger_obj)
                triggers_by_id[str(trigger_obj.id)] = trigger_obj
                results.append(_result(action, "applied", "Objective wake policy created."))
                continue

            if action_type == "assign_default_primary_model":
                if agent is None:
                    results.append(_result(action, "skipped", "Agent no longer exists."))
                    continue
                if _value(agent, "primary_model_id") is not None:
                    results.append(_result(action, "skipped", "Agent already has primary_model_id."))
                    continue
                tenant_id = _as_str(_value(agent, "tenant_id"))
                model = default_models_by_tenant.get(tenant_id or "")
                if model is None:
                    results.append(_result(action, "skipped", "Tenant default model is no longer available."))
                    continue
                agent.primary_model_id = _value(model, "id")
                results.append(_result(action, "applied", "Default primary model assigned.", _model_evidence(model)))
                continue

            if action_type == "repair_trigger_focus_ref_from_objective":
                if trigger is None or objective is None:
                    results.append(_result(action, "skipped", "Trigger or objective no longer exists."))
                    continue
                config = _config(trigger)
                if str(config.get("objective_id") or "") != str(_value(objective, "id")):
                    results.append(_result(action, "skipped", "Trigger objective binding changed."))
                    continue
                objective_key = _normalize_ref(_value(objective, "objective_key"))
                trigger.focus_ref = objective_key
                results.append(_result(action, "applied", "Trigger focus_ref repaired from objective."))
                continue

            if action_type == "disable_completed_focus_trigger":
                if trigger is None:
                    results.append(_result(action, "skipped", "Trigger no longer exists."))
                    continue
                if not bool(_value(trigger, "is_enabled", False)):
                    results.append(_result(action, "skipped", "Trigger is already disabled."))
                    continue
                trigger.is_enabled = False
                results.append(_result(action, "applied", "Completed focus trigger disabled."))
                continue

            if action_type == "create_objective_for_existing_trigger":
                if agent is None or trigger is None or not focus_ref:
                    results.append(_result(action, "skipped", "Agent, trigger, or focus_ref no longer exists."))
                    continue
                if not bool(_value(trigger, "is_enabled", False)):
                    results.append(_result(action, "skipped", "Trigger is no longer enabled."))
                    continue
                objective = _existing_objective_for_ref(objectives, str(_value(agent, "id")), focus_ref)
                if objective is None:
                    objective = AgentObjective(
                        id=uuid.uuid4(),
                        agent_id=_value(agent, "id"),
                        tenant_id=_value(agent, "tenant_id"),
                        objective_key=focus_ref,
                        description=_trigger_description(trigger),
                        status="open",
                        source="trigger_repair",
                        metadata_json={
                            "source": "trigger_repair",
                            "trigger_id": str(_value(trigger, "id")),
                            "trigger_name": _value(trigger, "name"),
                        },
                    )
                    db.add(objective)
                    objectives.append(objective)
                    objectives_by_id[str(_value(objective, "id"))] = objective
                    await _flush_if_possible(db)
                config = _config(trigger)
                config["trigger_class"] = "objective_task"
                config["objective_id"] = str(_value(objective, "id"))
                trigger.config = config
                trigger.focus_ref = focus_ref
                results.append(_result(
                    action,
                    "applied",
                    "Objective created or reused for existing trigger.",
                    {"objective_id": str(_value(objective, "id"))},
                ))
                continue

            results.append(_result(action, "skipped", "Unsupported action type."))
        except Exception as exc:
            results.append(_result(action, "failed", str(exc)))

    if commit and any(result.get("status") == "applied" for result in results):
        await _flush_if_possible(db)
        commit_fn = getattr(db, "commit", None)
        if commit_fn is not None:
            await commit_fn()

    return {
        "schema": "autonomy_repair_apply.v1",
        "mode": "apply",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "lookback_hours": int(audit_report.get("lookback_hours") or 0),
        "totals": _apply_totals(results, planned=len(plan["actions"])),
        "results": results,
        "pre_apply_plan": plan,
    }


async def _default_model_for_tenant(db: AsyncSession, tenant_id: uuid.UUID) -> LLMModel | None:
    setting_result = await db.execute(
        select(TenantSetting.value).where(
            TenantSetting.tenant_id == tenant_id,
            TenantSetting.key == "default_model_id",
        )
    )
    setting_value = setting_result.scalar_one_or_none()
    if isinstance(setting_value, dict) and setting_value.get("model_id"):
        try:
            model_id = uuid.UUID(str(setting_value["model_id"]))
        except (TypeError, ValueError):
            model_id = None
        if model_id is not None:
            model_result = await db.execute(
                select(LLMModel).where(
                    LLMModel.id == model_id,
                    LLMModel.tenant_id == tenant_id,
                    LLMModel.enabled.is_(True),
                )
            )
            model = model_result.scalar_one_or_none()
            if model is not None:
                return model

    fallback_result = await db.execute(
        select(LLMModel)
        .where(LLMModel.tenant_id == tenant_id, LLMModel.enabled.is_(True))
        .order_by(LLMModel.created_at.asc())
        .limit(1)
    )
    return fallback_result.scalar_one_or_none()


async def _load_default_models_by_tenant(db: AsyncSession, agents: list[Agent]) -> dict[str, LLMModel]:
    tenant_ids = sorted({agent.tenant_id for agent in agents if agent.tenant_id is not None})
    models: dict[str, LLMModel] = {}
    for tenant_id in tenant_ids:
        model = await _default_model_for_tenant(db, tenant_id)
        if model is not None:
            models[str(tenant_id)] = model
    return models


async def build_autonomy_repair_plan(
    *,
    db: AsyncSession,
    tenant_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    lookback_hours: int = 24,
) -> dict[str, Any]:
    """Build a read-only dry-run repair plan for autonomous audit findings."""
    audit_report = await build_autonomous_audit_report(
        db=db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        lookback_hours=lookback_hours,
    )

    agent_stmt = select(Agent)
    if tenant_id is not None:
        agent_stmt = agent_stmt.where(Agent.tenant_id == tenant_id)
    if agent_id is not None:
        agent_stmt = agent_stmt.where(Agent.id == agent_id)
    agent_result = await db.execute(agent_stmt)
    agents = list(agent_result.scalars().all())
    if not agents:
        return plan_autonomy_repair_actions(
            audit_report=audit_report,
            agents=[],
            triggers=[],
            objectives=[],
            default_models_by_tenant={},
        )

    agent_ids = [agent.id for agent in agents]
    trigger_result = await db.execute(select(AgentTrigger).where(AgentTrigger.agent_id.in_(agent_ids)))
    objective_result = await db.execute(select(AgentObjective).where(AgentObjective.agent_id.in_(agent_ids)))
    default_models_by_tenant = await _load_default_models_by_tenant(db, agents)

    return plan_autonomy_repair_actions(
        audit_report=audit_report,
        agents=agents,
        triggers=list(trigger_result.scalars().all()),
        objectives=list(objective_result.scalars().all()),
        default_models_by_tenant=default_models_by_tenant,
    )


async def apply_autonomy_repair_plan(
    *,
    db: AsyncSession,
    tenant_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    lookback_hours: int = 24,
    action_ids: list[str] | None = None,
    max_risk: str = "medium",
) -> dict[str, Any]:
    """Apply auto-applyable repair actions selected from the current audit snapshot."""
    audit_report = await build_autonomous_audit_report(
        db=db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        lookback_hours=lookback_hours,
    )

    agent_stmt = select(Agent)
    if tenant_id is not None:
        agent_stmt = agent_stmt.where(Agent.tenant_id == tenant_id)
    if agent_id is not None:
        agent_stmt = agent_stmt.where(Agent.id == agent_id)
    agent_result = await db.execute(agent_stmt)
    agents = list(agent_result.scalars().all())
    if not agents:
        return await apply_autonomy_repair_actions(
            db=db,
            audit_report=audit_report,
            agents=[],
            triggers=[],
            objectives=[],
            default_models_by_tenant={},
            action_ids=action_ids,
            max_risk=max_risk,
        )

    agent_ids = [agent.id for agent in agents]
    trigger_result = await db.execute(select(AgentTrigger).where(AgentTrigger.agent_id.in_(agent_ids)))
    objective_result = await db.execute(select(AgentObjective).where(AgentObjective.agent_id.in_(agent_ids)))
    default_models_by_tenant = await _load_default_models_by_tenant(db, agents)

    result = await apply_autonomy_repair_actions(
        db=db,
        audit_report=audit_report,
        agents=agents,
        triggers=list(trigger_result.scalars().all()),
        objectives=list(objective_result.scalars().all()),
        default_models_by_tenant=default_models_by_tenant,
        action_ids=action_ids,
        max_risk=max_risk,
        commit=True,
    )
    result["post_apply_plan"] = await build_autonomy_repair_plan(
        db=db,
        tenant_id=tenant_id,
        agent_id=agent_id,
        lookback_hours=lookback_hours,
    )
    return result
