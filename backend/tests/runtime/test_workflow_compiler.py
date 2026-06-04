"""§9 P2 red tests: compiler — structural validation beyond schema parsing.

The compiler is what makes "structured data" safe to accept from an agent at
runtime (§3.2): step allowlist, condition AST reference checks, leaf binding,
gate enforcement for external/irreversible effects. Pure logic, no DB.
"""

from __future__ import annotations

import pytest

from app.runtime.workflow_compiler import CompiledWorkflow, WorkflowCompileError, compile_workflow


def _definition(**overrides) -> dict:
    data = {
        "name": "pipeline",
        "args_schema": {"target": {"type": "string", "required": True}},
        "steps": [
            {
                "id": "scan",
                "type": "agent_step",
                "leaf": {"name": "scanner", "type": "explorer"},
                "task": "Scan {{args.target}}",
            },
            {
                "id": "report",
                "type": "agent_step",
                "leaf": {"name": "reporter", "type": "worker"},
                "task": "Report on {{steps.scan.output}}",
            },
        ],
    }
    data.update(overrides)
    return data


def test_valid_definition_compiles_with_stable_hash():
    compiled = compile_workflow(_definition())
    assert isinstance(compiled, CompiledWorkflow)
    assert compiled.definition_hash == compile_workflow(_definition()).definition_hash
    assert [s.id for s in compiled.definition.steps] == ["scan", "report"]


def test_condition_referencing_future_step_rejected():
    """Conditions may only reference args or PRIOR step outputs — forward
    references would make deterministic sequential interpretation impossible."""
    data = _definition()
    data["steps"][0]["when"] = {"predicate": {"field": "steps.report.output.x", "op": "exists"}}
    with pytest.raises(WorkflowCompileError):
        compile_workflow(data)


def test_condition_referencing_unknown_step_rejected():
    data = _definition()
    data["steps"][1]["when"] = {"predicate": {"field": "steps.ghost.output.x", "op": "exists"}}
    with pytest.raises(WorkflowCompileError):
        compile_workflow(data)


def test_task_template_referencing_unknown_step_rejected():
    data = _definition()
    data["steps"][1]["task"] = "Report on {{steps.ghost.output}}"
    with pytest.raises(WorkflowCompileError):
        compile_workflow(data)


def test_unknown_leaf_rejected_when_catalog_given():
    """Leaf capability binding: with a known-leaf catalog, an unlisted leaf
    fails compilation (the admission layer passes the tenant's real catalog)."""
    with pytest.raises(WorkflowCompileError):
        compile_workflow(_definition(), known_leaves={"scanner"})  # 'reporter' missing


def test_known_leaves_pass_catalog_check():
    compiled = compile_workflow(_definition(), known_leaves={"scanner", "reporter"})
    assert compiled.leaf_names == {"scanner", "reporter"}


def test_external_step_without_gate_rejected():
    """对外/不可逆步骤强制 gate_step（§3.2 admission 校验之一）: an
    external-visible step must be preceded by a gate_step."""
    data = _definition()
    data["steps"][1]["effects"] = "external"
    with pytest.raises(WorkflowCompileError):
        compile_workflow(data)


def test_external_step_with_preceding_gate_compiles():
    data = _definition()
    data["steps"][1]["effects"] = "external"
    data["steps"].insert(1, {"id": "approve", "type": "gate_step", "reason": "sending report externally"})
    compiled = compile_workflow(data)
    assert [s.id for s in compiled.definition.steps] == ["scan", "approve", "report"]


def test_irreversible_step_cannot_have_retry():
    """v1 decision 1: retry(max_attempts) is reversible-only."""
    data = _definition()
    data["steps"].insert(1, {"id": "approve", "type": "gate_step", "reason": "irreversible op"})
    data["steps"][2]["effects"] = "irreversible"
    data["steps"][2]["retry"] = {"max_attempts": 3}
    with pytest.raises(WorkflowCompileError):
        compile_workflow(data)


def test_fanout_step_compiles_and_counts_leaves():
    data = _definition()
    data["steps"].append(
        {
            "id": "fan",
            "type": "fanout_step",
            "leaf": {"name": "scanner", "type": "explorer"},
            "items_from": "args.target",
            "per_item_task": "Scan item {{item}}",
            "max_concurrency": 4,
        }
    )
    compiled = compile_workflow(data)
    fan = compiled.definition.steps[-1]
    assert fan.type == "fanout_step"
    assert fan.max_concurrency == 4
