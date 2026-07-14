from __future__ import annotations

from types import SimpleNamespace

from app.runtime.dynamic_workflow import (
    attach_workflow_decision_outcome,
    build_dynamic_workflow_repair_plan,
    build_dynamic_workflow_run_metadata,
    build_workflow_decision_entry,
    summarize_dynamic_workflow_outcome,
    validate_dynamic_workflow_proposal,
)
from app.runtime.workflow_compiler import compile_workflow


def _definition() -> dict:
    return {
        "name": "repo-audit",
        "args_schema": {"slices": {"type": "array", "required": True}},
        "steps": [
            {
                "id": "scan",
                "type": "fanout_step",
                "leaf": {"name": "scanner", "type": "explorer"},
                "items_from": "args.slices",
                "per_item_task": "Scan {{item}}",
            }
        ],
    }


def test_validate_dynamic_workflow_proposal_lowers_candidates():
    proposal = validate_dynamic_workflow_proposal(
        {
            "goal": "Audit repository slices.",
            "why_workflow": "Needs fanout plus critic verification.",
            "success_criteria": ["Each slice cites evidence."],
            "args": {"slices": ["api", "runtime"]},
            "candidates": [
                {
                    "candidate_id": "fanout-critic",
                    "name": "Fanout then critic",
                    "pattern_mix": ["fanout_synthesize"],
                    "failure_policy": {"leaf_failure": "record_and_continue", "repair_rounds": 1},
                    "lowered_definition": _definition(),
                }
            ],
            "recommended_candidate_id": "fanout-critic",
        }
    )

    assert proposal["status"] == "dynamic_workflow_proposed"
    assert proposal["recommended_candidate_id"] == "fanout-critic"
    assert proposal["candidates"][0]["definition_hash"]
    assert proposal["candidates"][0]["preview_args"] == {"slices": ["api", "runtime"]}


def test_validate_dynamic_workflow_proposal_accepts_json_schema_args_and_item_wrapped_arrays():
    definition = _definition()
    definition["args_schema"] = {
        "type": "object",
        "properties": {
            "slices": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["slices"],
    }

    proposal = validate_dynamic_workflow_proposal(
        {
            "goal": "Audit repository slices.",
            "why_workflow": "Needs fanout plus synthesis.",
            "success_criteria": ["Each slice cites evidence."],
            "args": {"slices": {"item": ["api", "runtime"]}},
            "candidates": [
                {
                    "candidate_id": "fanout-json-schema",
                    "name": "Fanout",
                    "lowered_definition": definition,
                }
            ],
            "recommended_candidate_id": "fanout-json-schema",
        }
    )

    candidate = proposal["candidates"][0]

    assert proposal["ok"] is True
    assert candidate["preview_args"] == {"slices": ["api", "runtime"]}
    assert candidate["lowered_definition"]["args_schema"] == {"slices": {"type": "array", "required": True}}
    assert candidate["definition_hash"] == compile_workflow(candidate["lowered_definition"]).definition_hash


def test_validate_dynamic_workflow_proposal_rejects_code_surfaces():
    proposal = validate_dynamic_workflow_proposal(
        {
            "goal": "Audit repository slices.",
            "why_workflow": "Needs fanout.",
            "success_criteria": ["No code runner."],
            "candidates": [
                {
                    "candidate_id": "bad",
                    "script": "await agent('do it')",
                    "lowered_definition": _definition(),
                }
            ],
        }
    )

    assert proposal["ok"] is False
    assert "executable code fields" in proposal["error"]


def test_dynamic_metadata_preserves_candidate_failure_policy():
    metadata = build_dynamic_workflow_run_metadata(
        proposal_id="proposal-1",
        candidate_id="fanout-critic",
        preview_id="preview-1",
        definition_hash="hash-1",
        args_hash="args-1",
        candidate={
            "pattern_mix": ["fanout_synthesize"],
            "success_criteria": ["Each slice cites evidence."],
            "failure_policy": {"leaf_failure": "record_and_continue", "repair_rounds": 1},
        },
    )

    assert metadata["dynamic_workflow"]["proposal_id"] == "proposal-1"
    assert metadata["dynamic_workflow"]["failure_policy"]["repair_rounds"] == 1
    assert metadata["dynamic_workflow"]["success_criteria"] == ["Each slice cites evidence."]


def test_dynamic_outcome_summary_and_repair_plan_are_leaf_level():
    task = SimpleNamespace(
        status="failed", metadata_json={"dynamic_workflow": {"failure_policy": {"repair_rounds": 1}}}
    )
    steps = [SimpleNamespace(step_id="scan", status="failed", error="item-1: failed")]
    leaves = [
        SimpleNamespace(step_id="scan", leaf_id="item-0", status="done", error=None),
        SimpleNamespace(step_id="scan", leaf_id="item-1", status="failed", error="timeout"),
    ]

    evidence = summarize_dynamic_workflow_outcome(task=task, steps=steps, leaf_calls=leaves)
    repair = build_dynamic_workflow_repair_plan(task=task, steps=steps, leaf_calls=leaves)

    assert evidence["leaf_total"] == 2
    assert evidence["leaf_done"] == 1
    assert evidence["leaf_failed"] == 1
    assert "promotion_eligible" not in evidence
    assert evidence["model_promotion_review"] == "not_requested"
    assert repair["repairable"] is True
    assert repair["strategy"] == "resume_failed_leaves"
    assert repair["failed_leaves"][0]["leaf_id"] == "item-1"


def test_workflow_decision_entry_links_candidate_preview_run_outcome_and_repair():
    entry = build_workflow_decision_entry(
        dynamic_workflow={
            "proposal_id": "proposal-1",
            "candidate_id": "candidate-1",
            "preview_id": "preview-1",
            "definition_hash": "hash-1",
            "failure_policy": {"repair_rounds": 1},
        },
        run_id="run-1",
        outcome={"status": "failed", "model_promotion_review": "not_requested"},
        repair_plan={"repairable": True, "strategy": "resume_failed_leaves"},
    )

    assert entry["schema"] == "hive.ccplus.workflow_decision.v1"
    assert entry["proposal_id"] == "proposal-1"
    assert entry["candidate_id"] == "candidate-1"
    assert entry["preview_id"] == "preview-1"
    assert entry["run_id"] == "run-1"
    assert entry["hash"] == "hash-1"
    assert entry["failure_policy"] == {"repair_rounds": 1}
    assert entry["outcome"]["status"] == "failed"
    assert entry["repair_plan"]["strategy"] == "resume_failed_leaves"
    assert entry["model_promotion_review"] == "not_requested"
    assert entry["agent_cycle_decision_entry"]["decision"] == "repair"


def test_attach_workflow_decision_outcome_preserves_single_decision_chain():
    dynamic = {
        "proposal_id": "proposal-1",
        "candidate_id": "candidate-1",
        "preview_id": "preview-1",
        "definition_hash": "hash-1",
        "failure_policy": {"repair_rounds": 1},
    }

    updated = attach_workflow_decision_outcome(
        dynamic_workflow=dynamic,
        run_id="run-1",
        outcome_evidence={"status": "failed", "leaf_failed": 1, "model_promotion_review": "not_requested"},
        repair_plan={"repairable": True, "strategy": "resume_failed_leaves"},
    )

    assert updated["run_id"] == "run-1"
    assert updated["outcome_evidence"]["leaf_failed"] == 1
    assert updated["repair_plan"]["strategy"] == "resume_failed_leaves"
    entry = updated["workflow_decision_entry"]
    assert entry["run_id"] == "run-1"
    assert entry["outcome"]["status"] == "failed"
    assert entry["repair_plan"]["repairable"] is True
    assert entry["model_promotion_review"] == "not_requested"


def test_repair_plan_preserves_every_failed_leaf_for_model_and_operator_review():
    task = SimpleNamespace(
        status="failed", metadata_json={"dynamic_workflow": {"failure_policy": {"repair_rounds": 1}}}
    )
    leaves = [
        SimpleNamespace(step_id="scan", leaf_id=f"item-{index}", status="failed", error=f"failure-{index}")
        for index in range(25)
    ]

    repair = build_dynamic_workflow_repair_plan(task=task, steps=[], leaf_calls=leaves)

    assert len(repair["failed_leaves"]) == 25
    assert repair["failed_leaves"][-1]["leaf_id"] == "item-24"
