"""Workflow tools + confirmation-neutral plan-gate registry + capability map.

The agent-facing surface: ``preview_workflow`` (always allowed — pure
compile/admission preview) and ``start_workflow`` (starts through the workflow
runtime, not PlanModeGate). Preview may show confirmation notes, but there is no
low/high risk grade and no automatic Plan Mode entry.

⚠️ Regression anchor for the known trap: every new agent tool MUST be in
services/capability_gate.py CAPABILITY_MAP, or real tenant invocations are
denied under STRICT_CAPABILITY_MAPPING.
"""

from __future__ import annotations

import json
import uuid

from app.tools.plan_gate_registry import hard_gated_action_kind


def _low_risk_definition() -> dict:
    return {
        "name": "read-probe",
        "args_schema": {},
        "steps": [
            {
                "id": "scan",
                "type": "agent_step",
                "leaf": {"name": "scanner", "type": "explorer"},
                "task": "Scan the workspace",
            }
        ],
    }


def _high_risk_definition() -> dict:
    return {
        "name": "external-send",
        "args_schema": {},
        "steps": [
            {"id": "gate", "type": "gate_step", "reason": "external send"},
            {
                "id": "send",
                "type": "agent_step",
                "leaf": {"name": "sender", "type": "worker"},
                "task": "Send the report externally",
                "effects": "external",
            },
        ],
    }


# ── plan-gate registry (early intercept) ──────────────────────────


def test_external_effect_start_workflow_is_not_plan_mode_hard_gated():
    kind = hard_gated_action_kind("start_workflow", {"definition": _high_risk_definition(), "args": {}})
    assert kind is None


def test_workspace_effect_start_workflow_is_not_plan_mode_hard_gated():
    kind = hard_gated_action_kind("start_workflow", {"definition": _low_risk_definition(), "args": {}})
    assert kind is None


def test_invalid_definition_is_not_routed_to_plan_mode_gate():
    kind = hard_gated_action_kind("start_workflow", {"definition": {"steps": "not-a-list"}, "args": {}})
    assert kind is None


def test_missing_arguments_are_not_routed_to_plan_mode_gate():
    assert hard_gated_action_kind("start_workflow", None) is None


# ── capability map (the known trap) ───────────────────────────────


def test_workflow_tools_registered_in_capability_map():
    from app.services.capability_gate import CAPABILITY_MAP

    assert "start_workflow" in CAPABILITY_MAP
    assert "preview_workflow" in CAPABILITY_MAP


# ── tool handlers ─────────────────────────────────────────────────


async def test_preview_workflow_returns_hash_and_confirmation_notes():
    from app.tools.handlers.workflow import preview_workflow

    result = await preview_workflow(uuid.uuid4(), {"definition": _low_risk_definition(), "args": {}})
    payload = json.loads(result)
    assert payload["definition_hash"]
    assert "risk" not in payload
    assert payload["confirmation_required"] is False
    assert payload["confirmation_reasons"] == []
    assert payload["planned_leaf_calls"] == 1


async def test_preview_workflow_external_effects_return_confirmation_notes_without_risk_level():
    from app.tools.handlers.workflow import preview_workflow

    result = await preview_workflow(uuid.uuid4(), {"definition": _high_risk_definition(), "args": {}})
    payload = json.loads(result)
    assert payload["definition_hash"]
    assert "risk" not in payload
    assert "risk_reasons" not in payload
    assert payload["confirmation_required"] is True
    assert payload["confirmation_reasons"]


async def test_preview_workflow_reports_compile_errors():
    from app.tools.handlers.workflow import preview_workflow

    result = await preview_workflow(uuid.uuid4(), {"definition": {"steps": []}, "args": {}})
    payload = json.loads(result)
    assert payload["ok"] is False
    assert payload["error"]


async def test_start_workflow_low_risk_launches(monkeypatch):
    from app.tools.handlers import workflow as workflow_handlers

    captured: dict = {}

    async def fake_launch(**kwargs):
        captured.update(kwargs)
        from app.runtime.workflow_engine import WorkflowRunOutcome
        from app.services.workflow_runtime_service import WorkflowRunHandle

        return WorkflowRunHandle(run_id=uuid.uuid4(), outcome=WorkflowRunOutcome(status="completed"))

    monkeypatch.setattr(workflow_handlers, "start_ephemeral_workflow_for_agent", fake_launch)

    agent_id = uuid.uuid4()
    result = await workflow_handlers.start_workflow(agent_id, {"definition": _low_risk_definition(), "args": {}})
    payload = json.loads(result)

    assert payload["status"] == "completed"
    assert captured["agent_id"] == agent_id
    assert captured["definition"]["name"] == "read-probe"


async def test_start_workflow_passes_ledger_todo_id(monkeypatch):
    from app.tools.handlers import workflow as workflow_handlers

    captured: dict = {}

    async def fake_launch(**kwargs):
        captured.update(kwargs)
        from app.runtime.workflow_engine import WorkflowRunOutcome
        from app.services.workflow_runtime_service import WorkflowRunHandle

        return WorkflowRunHandle(run_id=uuid.uuid4(), outcome=WorkflowRunOutcome(status="completed"))

    monkeypatch.setattr(workflow_handlers, "start_ephemeral_workflow_for_agent", fake_launch)
    await workflow_handlers.start_workflow(
        uuid.uuid4(),
        {"definition": _low_risk_definition(), "args": {}, "ledger_todo_id": "todo-9"},
    )
    assert captured["ledger_todo_id"] == "todo-9"
