"""§9 P2 red tests: admission — budget / fanout / wall-clock / args preflight.

Thresholds come from Settings (config), never hardcoded in the engine.
Pure logic against an injected config snapshot — no DB.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.runtime.workflow_admission import (
    AdmissionLimits,
    WorkflowAdmissionError,
    admit_workflow,
)
from app.runtime.workflow_compiler import compile_workflow


def _definition(**overrides) -> dict:
    data = {
        "name": "fan-pipeline",
        "args_schema": {
            "targets": {"type": "array", "required": True},
            "label": {"type": "string", "required": False},
        },
        "default_budget": {"max_total_tokens": 100_000},
        "steps": [
            {
                "id": "fan",
                "type": "fanout_step",
                "leaf": {"name": "scanner", "type": "explorer"},
                "items_from": "args.targets",
                "per_item_task": "Scan {{item}}",
                "max_concurrency": 4,
            },
        ],
    }
    data.update(overrides)
    return data


def _limits(**overrides) -> AdmissionLimits:
    defaults = dict(
        max_run_budget_tokens=1_000_000,
        max_fanout_items=16,
        max_concurrency=8,
        max_leaf_calls=64,
        max_wall_clock_seconds=86_400,
    )
    defaults.update(overrides)
    return AdmissionLimits(**defaults)


def test_valid_run_admitted():
    compiled = compile_workflow(_definition())
    result = admit_workflow(compiled, args={"targets": ["a", "b", "c"]}, limits=_limits())
    assert result.admitted is True
    assert result.planned_leaf_calls == 3


def test_fanout_over_item_cap_rejected():
    compiled = compile_workflow(_definition())
    with pytest.raises(WorkflowAdmissionError, match="fanout"):
        admit_workflow(compiled, args={"targets": [f"t{i}" for i in range(40)]}, limits=_limits(max_fanout_items=16))


def test_budget_over_cap_rejected():
    compiled = compile_workflow(_definition(default_budget={"max_total_tokens": 5_000_000}))
    with pytest.raises(WorkflowAdmissionError, match="budget"):
        admit_workflow(compiled, args={"targets": ["a"]}, limits=_limits(max_run_budget_tokens=1_000_000))


def test_concurrency_over_cap_rejected():
    data = _definition()
    data["steps"][0]["max_concurrency"] = 32
    compiled = compile_workflow(data)
    with pytest.raises(WorkflowAdmissionError, match="concurrency"):
        admit_workflow(compiled, args={"targets": ["a"]}, limits=_limits(max_concurrency=8))


def test_unauthorized_leaf_rejected():
    compiled = compile_workflow(_definition())
    with pytest.raises(WorkflowAdmissionError, match="leaf"):
        admit_workflow(
            compiled,
            args={"targets": ["a"]},
            limits=_limits(),
            allowed_leaves={"other-leaf"},
        )


def test_missing_required_arg_rejected():
    compiled = compile_workflow(_definition())
    with pytest.raises(WorkflowAdmissionError, match="args"):
        admit_workflow(compiled, args={}, limits=_limits())


def test_wrong_arg_type_rejected():
    compiled = compile_workflow(_definition())
    with pytest.raises(WorkflowAdmissionError, match="args"):
        admit_workflow(compiled, args={"targets": "not-an-array"}, limits=_limits())


def test_wait_until_beyond_wall_clock_rejected():
    data = _definition()
    data["steps"].append({"id": "wait", "type": "wait_until_step", "delay_seconds": 7 * 86_400})
    compiled = compile_workflow(data)
    with pytest.raises(WorkflowAdmissionError, match="wall"):
        admit_workflow(compiled, args={"targets": ["a"]}, limits=_limits(max_wall_clock_seconds=86_400))


def test_wait_until_absolute_timestamp_beyond_wall_clock_rejected():
    data = _definition()
    data["steps"].append(
        {
            "id": "wait",
            "type": "wait_until_step",
            "until": (datetime.now(UTC) + timedelta(days=7)).isoformat(),
        }
    )
    compiled = compile_workflow(data)
    with pytest.raises(WorkflowAdmissionError, match="wall"):
        admit_workflow(compiled, args={"targets": ["a"]}, limits=_limits(max_wall_clock_seconds=86_400))


def test_wait_until_args_timestamp_beyond_wall_clock_rejected():
    data = _definition()
    data["args_schema"]["resume_at"] = {"type": "string", "required": True}
    data["steps"].append({"id": "wait", "type": "wait_until_step", "until": "args.resume_at"})
    compiled = compile_workflow(data)
    with pytest.raises(WorkflowAdmissionError, match="wall"):
        admit_workflow(
            compiled,
            args={"targets": ["a"], "resume_at": (datetime.now(UTC) + timedelta(days=7)).isoformat()},
            limits=_limits(max_wall_clock_seconds=86_400),
        )


def test_wait_until_invalid_absolute_timestamp_rejected_by_admission():
    data = _definition()
    data["steps"].append({"id": "wait", "type": "wait_until_step", "until": "not-a-date"})
    compiled = compile_workflow(data)
    with pytest.raises(WorkflowAdmissionError, match="wait_until"):
        admit_workflow(compiled, args={"targets": ["a"]}, limits=_limits())


def test_limits_from_settings_factory():
    """AdmissionLimits.from_settings reads the WORKFLOW_* knobs — thresholds
    live in config, not in code."""
    from app.config import get_settings

    limits = AdmissionLimits.from_settings(get_settings())
    assert limits.max_fanout_items > 0
    assert limits.max_run_budget_tokens > 0
    assert limits.max_concurrency > 0
    assert limits.max_leaf_calls > 0
    assert limits.max_wall_clock_seconds > 0
