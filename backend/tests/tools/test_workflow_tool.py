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
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.tools.plan_gate_registry import hard_gated_action_kind
from app.tools.runtime import ToolExecutionContext, ToolExecutionRequest

_DEFAULT_SESSION = object()


def _identity_for(agent_id: uuid.UUID) -> tuple[uuid.UUID, uuid.UUID, str]:
    tenant_id = uuid.uuid5(uuid.NAMESPACE_URL, f"hive:test:tenant:{agent_id}")
    user_id = uuid.uuid5(uuid.NAMESPACE_URL, f"hive:test:user:{agent_id}")
    session_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"hive:test:session:{agent_id}"))
    return tenant_id, user_id, session_id


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


def _dynamic_workflow_proposal() -> dict:
    return {
        "goal": "Audit the repository with independent slices and a critic pass.",
        "why_workflow": "The work needs bounded fan-out, synthesis, and verification without polluting parent context.",
        "success_criteria": ["Each slice reports evidence refs.", "A critic verifies the synthesized result."],
        "args": {"slices": ["api", "runtime"]},
        "candidates": [
            {
                "candidate_id": "fanout-critic",
                "name": "Fanout then critic",
                "pattern_mix": ["fanout_synthesize", "adversarial_verify"],
                "risk_level": "medium",
                "budget": {
                    "max_steps": 3,
                    "max_leaf_calls": 8,
                    "max_concurrency": 2,
                    "max_tokens": 12000,
                    "max_wall_clock_seconds": 1800,
                },
                "failure_policy": {
                    "leaf_failure": "record_and_continue",
                    "repair_rounds": 1,
                    "no_full_chain_rerun": True,
                },
                "lowered_definition": {
                    "name": "repo-audit-fanout",
                    "description": "Audit repository slices and verify the synthesis.",
                    "args_schema": {"slices": {"type": "array", "required": True}},
                    "default_budget": {"max_total_tokens": 12000, "max_wall_clock_seconds": 1800},
                    "steps": [
                        {
                            "id": "slice",
                            "type": "fanout_step",
                            "leaf": {"name": "slice-auditor", "type": "explorer"},
                            "items_from": "args.slices",
                            "per_item_task": "Audit {{item}} and return evidence refs.",
                            "max_concurrency": 2,
                        },
                        {
                            "id": "critic",
                            "type": "agent_step",
                            "leaf": {"name": "critic", "type": "critic"},
                            "task": "Verify the slice outputs independently.",
                        },
                    ],
                },
            }
        ],
        "recommended_candidate_id": "fanout-critic",
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


def _start_request(
    agent_id: uuid.UUID,
    arguments: dict,
    *,
    session_id: str | None | object = _DEFAULT_SESSION,
    budget_run_id: str | None = None,
    round_state: dict | None = None,
) -> ToolExecutionRequest:
    tenant_id, user_id, default_session_id = _identity_for(agent_id)
    resolved_session_id = default_session_id if session_id is _DEFAULT_SESSION else session_id
    return ToolExecutionRequest(
        tool_name="start_workflow",
        arguments=arguments,
        context=ToolExecutionContext(
            agent_id=agent_id,
            user_id=user_id,
            tenant_id=str(tenant_id),
            workspace=Path("/tmp/hive-workflow-test"),
            session_id=resolved_session_id,
            turn_id="turn-workflow-confirmation",
            budget_run_id=budget_run_id,
            round_state=round_state,
        ),
    )


def _tool_request(
    tool_name: str,
    agent_id: uuid.UUID,
    arguments: dict,
    *,
    session_id: str | None | object = _DEFAULT_SESSION,
) -> ToolExecutionRequest:
    tenant_id, user_id, default_session_id = _identity_for(agent_id)
    resolved_session_id = default_session_id if session_id is _DEFAULT_SESSION else session_id
    return ToolExecutionRequest(
        tool_name=tool_name,
        arguments=arguments,
        context=ToolExecutionContext(
            agent_id=agent_id,
            user_id=user_id,
            tenant_id=str(tenant_id),
            workspace=Path("/tmp/hive-workflow-test"),
            session_id=resolved_session_id,
            turn_id="turn-workflow-confirmation",
        ),
    )


@pytest.fixture(autouse=True)
def _durable_workflow_confirmation_backend(monkeypatch):
    """Exercise tool orchestration without replacing production persistence with a cache."""

    from app.services.workflow_confirmation_service import (
        WorkflowStartClaim,
        claim_workflow_preview_record,
        mark_workflow_preview_failed_record,
        mark_workflow_preview_started_record,
    )
    from app.models.workflow_confirmation import WorkflowPreviewArtifact
    from app.tools.handlers import workflow as workflow_handlers

    proposals: dict[uuid.UUID, SimpleNamespace] = {}
    previews: dict[uuid.UUID, WorkflowPreviewArtifact] = {}

    async def persist_proposal(request, proposal):
        proposal_id = uuid.uuid4()
        canonical = {**proposal, "proposal_id": str(proposal_id)}
        artifact = SimpleNamespace(id=proposal_id, proposal_json=canonical, status="open")
        proposals[proposal_id] = artifact
        return canonical

    async def load_candidate(request, proposal_id, candidate_id):
        _ = request
        if not proposal_id or not candidate_id:
            return None, None
        proposal = proposals.get(uuid.UUID(str(proposal_id)))
        if proposal is None:
            return None, None
        candidate = next(
            (item for item in proposal.proposal_json["candidates"] if item["candidate_id"] == candidate_id),
            None,
        )
        return proposal, candidate

    async def persist_preview(
        request,
        *,
        definition,
        args,
        definition_hash,
        args_hash,
        preview_payload,
        proposal,
        candidate_id,
    ):
        tenant_id, agent_id, session_id, user_id = workflow_handlers._request_identity(request)
        preview_id = uuid.uuid4()
        preview = WorkflowPreviewArtifact(
            id=preview_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            requested_by_user_id=user_id,
            proposal_id=getattr(proposal, "id", None),
            candidate_id=candidate_id,
            status="ready",
            artifact_version=1,
            artifact_hash=f"artifact:{preview_id}",
            definition_hash=definition_hash,
            args_hash=args_hash,
            definition_json=definition,
            args_json=args,
            preview_json=preview_payload,
            expires_at=datetime.now(UTC) + timedelta(hours=24),
        )
        previews[preview_id] = preview
        return {
            "preview_id": str(preview_id),
            "preview_status": "ready",
            "artifact_version": 1,
            "artifact_hash": preview.artifact_hash,
            **preview_payload,
        }

    async def claim_start(request, preview_id):
        tenant_id, agent_id, session_id, user_id = workflow_handlers._request_identity(request)
        preview = previews[preview_id]
        outcome = claim_workflow_preview_record(
            preview,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            user_id=user_id,
            confirmation_source="agent_current_turn_no_confirmation_required",
            confirmation_evidence_id=str(request.context.turn_id or ""),
        )
        return WorkflowStartClaim(outcome=outcome, preview=preview)

    async def finish_start(_request, *, preview_id, claim_token, run_id):
        mark_workflow_preview_started_record(
            previews[preview_id],
            run_id=run_id,
            claim_token=claim_token,
        )

    async def fail_start(_request, *, preview_id, claim_token, code, message):
        mark_workflow_preview_failed_record(
            previews[preview_id],
            claim_token=claim_token,
            code=code,
            message=message,
        )

    monkeypatch.setattr(workflow_handlers, "_persist_dynamic_proposal", persist_proposal)
    monkeypatch.setattr(workflow_handlers, "_load_dynamic_candidate", load_candidate)
    monkeypatch.setattr(workflow_handlers, "_persist_workflow_preview", persist_preview)
    monkeypatch.setattr(workflow_handlers, "_claim_workflow_start", claim_start)
    monkeypatch.setattr(workflow_handlers, "_finish_workflow_start", finish_start)
    monkeypatch.setattr(workflow_handlers, "_fail_workflow_start", fail_start)


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
    assert "propose_dynamic_workflow" in CAPABILITY_MAP


def test_start_workflow_schema_accepts_only_durable_preview_reference():
    from app.tools.decorator import get_all_registered_tools
    import app.tools.handlers.workflow  # noqa: F401

    meta, _handler = get_all_registered_tools()["start_workflow"]

    assert meta.parameters["required"] == ["preview_id"]
    assert meta.parameters["additionalProperties"] is False
    assert set(meta.parameters["properties"]) == {"preview_id", "ledger_todo_id"}


async def test_preview_workflow_registered_adapter_matches_agent_arguments_signature():
    from app.tools.adapters import adapt_and_call
    from app.tools.decorator import get_all_registered_tools
    import app.tools.handlers.workflow  # noqa: F401

    meta, handler = get_all_registered_tools()["preview_workflow"]
    result = await adapt_and_call(
        meta,
        handler,
        _tool_request(
            "preview_workflow",
            uuid.uuid4(),
            {"definition": _low_risk_definition(), "args": {}},
        ),
    )
    payload = json.loads(result)

    assert payload["preview_id"]
    assert payload["planned_leaf_calls"] == 1


async def test_propose_dynamic_workflow_registered_adapter_matches_agent_arguments_signature():
    from app.tools.adapters import adapt_and_call
    from app.tools.decorator import get_all_registered_tools
    import app.tools.handlers.workflow  # noqa: F401

    meta, handler = get_all_registered_tools()["propose_dynamic_workflow"]
    result = await adapt_and_call(
        meta,
        handler,
        _tool_request("propose_dynamic_workflow", uuid.uuid4(), _dynamic_workflow_proposal()),
    )
    payload = json.loads(result)

    assert payload["ok"] is True
    assert payload["status"] == "dynamic_workflow_proposed"


# ── tool handlers ─────────────────────────────────────────────────


async def test_preview_workflow_returns_hash_and_confirmation_notes():
    from app.tools.handlers.workflow import preview_workflow

    agent_id = uuid.uuid4()
    result = await preview_workflow(
        _tool_request("preview_workflow", agent_id, {"definition": _low_risk_definition(), "args": {}})
    )
    payload = json.loads(result)
    assert payload["preview_id"]
    assert payload["definition_hash"]
    assert payload["args_hash"]
    assert "risk" not in payload
    assert payload["confirmation_required"] is False
    assert payload["confirmation_reasons"] == []
    assert payload["planned_leaf_calls"] == 1


async def test_preview_workflow_external_effects_return_confirmation_notes_without_risk_level():
    from app.tools.handlers.workflow import preview_workflow

    agent_id = uuid.uuid4()
    result = await preview_workflow(
        _tool_request("preview_workflow", agent_id, {"definition": _high_risk_definition(), "args": {}})
    )
    payload = json.loads(result)
    assert payload["preview_id"]
    assert payload["definition_hash"]
    assert payload["args_hash"]
    assert "risk" not in payload
    assert "risk_reasons" not in payload
    assert payload["confirmation_required"] is True
    assert payload["confirmation_reasons"]


async def test_preview_workflow_reports_compile_errors():
    from app.tools.handlers.workflow import preview_workflow

    agent_id = uuid.uuid4()
    result = await preview_workflow(
        _tool_request("preview_workflow", agent_id, {"definition": {"steps": []}, "args": {}})
    )
    payload = json.loads(result)
    assert payload["ok"] is False
    assert payload["error"]


async def test_preview_workflow_allows_explicit_workflow_profile_fanout_preview():
    from app.tools.handlers.workflow import preview_workflow

    definition = {
        "name": "explicit-workflow-fanout",
        "args_schema": {"targets": {"type": "array", "required": True}},
        "steps": [
            {
                "id": "scan",
                "type": "fanout_step",
                "leaf": {"name": "scanner", "type": "explorer"},
                "items_from": "args.targets",
                "per_item_task": "Scan {{item}}.",
                "max_concurrency": 1,
            }
        ],
    }
    agent_id = uuid.uuid4()
    result = await preview_workflow(
        _tool_request(
            "preview_workflow",
            agent_id,
            {"definition": definition, "args": {"targets": [f"t{i}" for i in range(64)]}},
        )
    )
    payload = json.loads(result)

    assert payload["ok"] is True
    assert payload["preview_id"]
    assert payload["planned_leaf_calls"] == 64
    assert payload["budget_tokens"] > 0


async def test_propose_dynamic_workflow_lowers_candidates_without_starting_runtime():
    from app.tools.handlers.workflow import propose_dynamic_workflow

    agent_id = uuid.uuid4()
    result = await propose_dynamic_workflow(
        _tool_request("propose_dynamic_workflow", agent_id, _dynamic_workflow_proposal())
    )
    payload = json.loads(result)

    assert payload["ok"] is True
    assert payload["status"] == "dynamic_workflow_proposed"
    assert payload["proposal_id"]
    assert payload["recommended_candidate_id"] == "fanout-critic"
    assert payload["next_action"].startswith("Call preview_workflow")
    assert len(payload["candidates"]) == 1
    candidate = payload["candidates"][0]
    assert candidate["candidate_id"] == "fanout-critic"
    assert candidate["definition_hash"]
    assert candidate["args_hash"]
    assert candidate["planned_leaf_calls"] == 3
    assert candidate["lowered_definition"]["name"] == "repo-audit-fanout"
    assert candidate["preview_args"] == {"slices": ["api", "runtime"]}


async def test_propose_dynamic_workflow_rejects_invalid_lowered_definition():
    from app.tools.handlers.workflow import propose_dynamic_workflow

    proposal = _dynamic_workflow_proposal()
    proposal["candidates"][0]["lowered_definition"] = {"steps": []}

    agent_id = uuid.uuid4()
    result = await propose_dynamic_workflow(_tool_request("propose_dynamic_workflow", agent_id, proposal))
    payload = json.loads(result)

    assert payload["ok"] is False
    assert "lowered_definition" in payload["error"]


async def test_preview_workflow_rejects_dynamic_candidate_artifact_mismatch():
    from app.tools.handlers import workflow as workflow_handlers

    agent_id = uuid.uuid4()
    proposal = json.loads(
        await workflow_handlers.propose_dynamic_workflow(
            _tool_request("propose_dynamic_workflow", agent_id, _dynamic_workflow_proposal())
        )
    )
    candidate = proposal["candidates"][0]
    mutated_definition = json.loads(json.dumps(candidate["lowered_definition"]))
    mutated_definition["name"] = "repo-audit-mutated"

    result = await workflow_handlers.preview_workflow(
        _tool_request(
            "preview_workflow",
            agent_id,
            {
                "definition": mutated_definition,
                "args": candidate["preview_args"],
                "proposal_id": proposal["proposal_id"],
                "candidate_id": candidate["candidate_id"],
            },
        )
    )
    payload = json.loads(result)

    assert payload["ok"] is False
    assert "candidate" in payload["error"]


async def test_start_workflow_low_risk_launches(monkeypatch):
    from app.tools.handlers import workflow as workflow_handlers

    captured: dict = {}

    async def fake_launch(**kwargs):
        captured.update(kwargs)
        from app.runtime.workflow_engine import WorkflowRunOutcome
        from app.services.workflow_runtime_service import WorkflowRunHandle

        return WorkflowRunHandle(run_id=kwargs["run_id"], outcome=WorkflowRunOutcome(status="completed"))

    monkeypatch.setattr(workflow_handlers, "start_ephemeral_workflow_for_agent", fake_launch)

    agent_id = uuid.uuid4()
    preview = json.loads(
        await workflow_handlers.preview_workflow(
            _tool_request("preview_workflow", agent_id, {"definition": _low_risk_definition(), "args": {}})
        )
    )
    result = await workflow_handlers.start_workflow(
        _start_request(
            agent_id,
            {
                "preview_id": preview["preview_id"],
            },
        )
    )
    payload = json.loads(result)

    assert payload["status"] == "completed"
    assert captured["agent_id"] == agent_id
    assert captured["definition"]["name"] == "read-probe"
    assert captured["parent_session_id"] == _identity_for(agent_id)[2]
    assert captured["enqueue_only"] is True
    assert captured["run_metadata"]["workflow_confirmation"] == {
        "preview_id": preview["preview_id"],
        "artifact_version": 1,
        "artifact_hash": preview["artifact_hash"],
        "confirmed_by_user_id": None,
        "confirmation_source": "agent_current_turn_no_confirmation_required",
        "confirmation_evidence_id": "turn-workflow-confirmation",
    }


async def test_start_workflow_requires_exact_user_action_for_confirmation_preview(monkeypatch):
    from app.tools.handlers import workflow as workflow_handlers

    async def fake_launch(**_kwargs):
        raise AssertionError("confirmation-required workflow must not launch from the agent tool")

    monkeypatch.setattr(workflow_handlers, "start_ephemeral_workflow_for_agent", fake_launch)

    agent_id = uuid.uuid4()
    preview = json.loads(
        await workflow_handlers.preview_workflow(
            _tool_request("preview_workflow", agent_id, {"definition": _high_risk_definition(), "args": {}})
        )
    )
    result = await workflow_handlers.start_workflow(
        _start_request(
            agent_id,
            {
                "preview_id": preview["preview_id"],
            },
        )
    )
    payload = json.loads(result)

    assert payload == {
        "ok": False,
        "status": "requires_confirmation",
        "requires_confirmation": True,
        "error_code": "explicit_user_confirmation_required",
        "error": "This workflow preview requires an authenticated user to confirm and run the exact preview.",
        "preview_id": preview["preview_id"],
        "confirmation_reasons": preview["confirmation_reasons"],
        "next_action": "Wait for the user to select Confirm and run on this exact workflow preview.",
    }


async def test_start_workflow_returns_execution_shape_admission_warning(monkeypatch):
    from app.tools.handlers import workflow as workflow_handlers

    async def fake_launch(**kwargs):
        from app.runtime.workflow_engine import WorkflowRunOutcome
        from app.services.workflow_runtime_service import WorkflowRunHandle

        return WorkflowRunHandle(run_id=kwargs["run_id"], outcome=WorkflowRunOutcome(status="completed"))

    monkeypatch.setattr(workflow_handlers, "start_ephemeral_workflow_for_agent", fake_launch)

    agent_id = uuid.uuid4()
    preview = json.loads(
        await workflow_handlers.preview_workflow(
            _tool_request("preview_workflow", agent_id, {"definition": _low_risk_definition(), "args": {}})
        )
    )
    result = await workflow_handlers.start_workflow(
        _start_request(
            agent_id,
            {
                "preview_id": preview["preview_id"],
            },
            round_state={"execution_shape": "one_off_parallel"},
        )
    )
    payload = json.loads(result)

    assert payload["ok"] is True
    assert payload["execution_shape_decision"]["tool_name"] == "start_workflow"
    assert payload["execution_shape_decision"]["execution_shape"] == "one_off_parallel"
    assert payload["execution_shape_decision"]["recommendation"] == "use_spawn_subagent"
    assert payload["execution_shape_decision"]["severity"] == "warning"


async def test_start_workflow_persists_dynamic_proposal_binding(monkeypatch):
    from app.tools.handlers import workflow as workflow_handlers

    captured: dict = {}

    async def fake_launch(**kwargs):
        captured.update(kwargs)
        from app.runtime.workflow_engine import WorkflowRunOutcome
        from app.services.workflow_runtime_service import WorkflowRunHandle

        return WorkflowRunHandle(run_id=kwargs["run_id"], outcome=WorkflowRunOutcome(status="completed"))

    monkeypatch.setattr(workflow_handlers, "start_ephemeral_workflow_for_agent", fake_launch)
    agent_id = uuid.uuid4()
    proposal = json.loads(
        await workflow_handlers.propose_dynamic_workflow(
            _tool_request("propose_dynamic_workflow", agent_id, _dynamic_workflow_proposal())
        )
    )
    candidate = proposal["candidates"][0]
    preview = json.loads(
        await workflow_handlers.preview_workflow(
            _tool_request(
                "preview_workflow",
                agent_id,
                {
                    "definition": candidate["lowered_definition"],
                    "args": candidate["preview_args"],
                    "proposal_id": proposal["proposal_id"],
                    "candidate_id": candidate["candidate_id"],
                },
            )
        )
    )

    await workflow_handlers.start_workflow(
        _start_request(
            agent_id,
            {
                "preview_id": preview["preview_id"],
            },
        )
    )

    assert captured["definition_source"] == "dynamic_workflow"
    assert captured["run_metadata"]["dynamic_workflow"]["proposal_id"] == proposal["proposal_id"]
    assert captured["run_metadata"]["dynamic_workflow"]["candidate_id"] == "fanout-critic"


async def test_start_workflow_rejects_dynamic_ids_without_dynamic_preview(monkeypatch):
    from app.tools.handlers import workflow as workflow_handlers

    async def fake_launch(**_kwargs):
        raise AssertionError("start_workflow must not launch when dynamic ids were not bound by preview_workflow")

    monkeypatch.setattr(workflow_handlers, "start_ephemeral_workflow_for_agent", fake_launch)

    agent_id = uuid.uuid4()
    preview = json.loads(
        await workflow_handlers.preview_workflow(
            _tool_request("preview_workflow", agent_id, {"definition": _low_risk_definition(), "args": {}})
        )
    )
    result = await workflow_handlers.start_workflow(
        _start_request(
            agent_id,
            {
                "preview_id": preview["preview_id"],
                "proposal_id": "proposal-1",
                "candidate_id": "candidate-1",
            },
        )
    )
    payload = json.loads(result)

    assert payload["ok"] is False
    assert payload["error_code"] == "invalid_start_arguments"


async def test_start_workflow_rejects_missing_preview_binding():
    from app.tools.handlers import workflow as workflow_handlers

    result = await workflow_handlers.start_workflow(_start_request(uuid.uuid4(), {}))
    payload = json.loads(result)

    assert payload["ok"] is False
    assert "preview_id" in payload["error"]


async def test_start_workflow_requires_current_session(monkeypatch):
    from app.tools.handlers import workflow as workflow_handlers

    async def fake_launch(**_kwargs):
        raise AssertionError("sessionless workflow must not launch")

    monkeypatch.setattr(workflow_handlers, "start_ephemeral_workflow_for_agent", fake_launch)
    agent_id = uuid.uuid4()
    preview = json.loads(
        await workflow_handlers.preview_workflow(
            _tool_request("preview_workflow", agent_id, {"definition": _low_risk_definition(), "args": {}})
        )
    )

    result = await workflow_handlers.start_workflow(
        _start_request(
            agent_id,
            {
                "preview_id": preview["preview_id"],
            },
            session_id=None,
        )
    )
    payload = json.loads(result)

    assert payload["ok"] is False
    assert payload["error_code"] == "missing_workflow_session"


async def test_start_workflow_rejects_restatement_after_preview(monkeypatch):
    from app.tools.handlers import workflow as workflow_handlers

    async def fake_launch(**_kwargs):
        raise AssertionError("a restated workflow must not launch")

    monkeypatch.setattr(workflow_handlers, "start_ephemeral_workflow_for_agent", fake_launch)
    agent_id = uuid.uuid4()
    preview = json.loads(
        await workflow_handlers.preview_workflow(
            _tool_request("preview_workflow", agent_id, {"definition": _low_risk_definition(), "args": {}})
        )
    )
    mutated = _low_risk_definition()
    mutated["steps"][0]["task"] = "Different task"

    result = await workflow_handlers.start_workflow(
        _start_request(
            agent_id,
            {
                "preview_id": preview["preview_id"],
                "definition": mutated,
            },
        )
    )
    payload = json.loads(result)

    assert payload["ok"] is False
    assert payload["error_code"] == "invalid_start_arguments"
    assert "durable preview" in payload["error"]


async def test_start_workflow_passes_ledger_todo_id(monkeypatch):
    from app.tools.handlers import workflow as workflow_handlers

    captured: dict = {}

    async def fake_launch(**kwargs):
        captured.update(kwargs)
        from app.runtime.workflow_engine import WorkflowRunOutcome
        from app.services.workflow_runtime_service import WorkflowRunHandle

        return WorkflowRunHandle(run_id=kwargs["run_id"], outcome=WorkflowRunOutcome(status="completed"))

    monkeypatch.setattr(workflow_handlers, "start_ephemeral_workflow_for_agent", fake_launch)
    agent_id = uuid.uuid4()
    preview = json.loads(
        await workflow_handlers.preview_workflow(
            _tool_request("preview_workflow", agent_id, {"definition": _low_risk_definition(), "args": {}})
        )
    )
    await workflow_handlers.start_workflow(
        _start_request(
            agent_id,
            {
                "ledger_todo_id": "todo-9",
                "preview_id": preview["preview_id"],
            },
        )
    )
    assert captured["ledger_todo_id"] == "todo-9"
    assert captured["parent_session_id"] == _identity_for(agent_id)[2]


async def test_start_workflow_threads_runtime_budget_to_workflow_launch(monkeypatch):
    from app.tools.handlers import workflow as workflow_handlers

    captured: dict = {}

    async def fake_launch(**kwargs):
        captured.update(kwargs)
        from app.runtime.workflow_engine import WorkflowRunOutcome
        from app.services.workflow_runtime_service import WorkflowRunHandle

        return WorkflowRunHandle(run_id=kwargs["run_id"], outcome=WorkflowRunOutcome(status="completed"))

    monkeypatch.setattr(workflow_handlers, "start_ephemeral_workflow_for_agent", fake_launch)
    agent_id = uuid.uuid4()
    budget_run_id = uuid.uuid4()
    preview = json.loads(
        await workflow_handlers.preview_workflow(
            _tool_request("preview_workflow", agent_id, {"definition": _low_risk_definition(), "args": {}})
        )
    )
    await workflow_handlers.start_workflow(
        _start_request(
            agent_id,
            {
                "preview_id": preview["preview_id"],
            },
            budget_run_id=str(budget_run_id),
        )
    )

    assert captured["budget_run_id"] == str(budget_run_id)
