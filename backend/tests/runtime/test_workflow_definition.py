"""§9 P2 red tests: definition schema + canonical hash.

Pure Functional Core — no DB, no mocks. The definition is serializable
structured DATA (§3.2): no code execution surface, ever.
"""

from __future__ import annotations

import pytest

from app.runtime.workflow_definition import (
    WorkflowDefinition,
    WorkflowDefinitionError,
    compute_definition_hash,
    parse_workflow_definition,
)


def _minimal_definition(**overrides) -> dict:
    data = {
        "name": "contract-review",
        "description": "review contracts then summarize",
        "args_schema": {
            "doc_path": {"type": "string", "required": True},
            "risk_threshold": {"type": "number", "required": False},
        },
        "steps": [
            {
                "id": "extract",
                "type": "agent_step",
                "leaf": {"name": "extractor", "type": "worker"},
                "task": "Extract clauses from {{args.doc_path}}",
            },
            {
                "id": "summarize",
                "type": "agent_step",
                "leaf": {"name": "summarizer", "type": "worker"},
                "task": "Summarize findings from {{steps.extract.output}}",
                "when": {"predicate": {"field": "steps.extract.output.clause_count", "op": "gt", "value": 0}},
            },
        ],
    }
    data.update(overrides)
    return data


def test_valid_sequence_with_condition_parses():
    definition = parse_workflow_definition(_minimal_definition())
    assert isinstance(definition, WorkflowDefinition)
    assert [s.id for s in definition.steps] == ["extract", "summarize"]


def test_unknown_top_level_field_rejected():
    with pytest.raises(WorkflowDefinitionError):
        parse_workflow_definition(_minimal_definition(python_hook="import os"))


def test_unknown_step_field_rejected():
    data = _minimal_definition()
    # NB: attack-payload STRING — never executed; the test asserts the schema
    # rejects unknown fields so no such payload can even be carried.
    data["steps"][0]["exec"] = "os.system('rm -rf /')"
    with pytest.raises(WorkflowDefinitionError):
        parse_workflow_definition(data)


def test_string_condition_expression_rejected():
    """`condition: "output.risk > 3"` — the v0-style string expression is the
    exact thing v1 decision 2 forbids."""
    data = _minimal_definition()
    data["steps"][1]["when"] = "output.risk > 3"
    with pytest.raises(WorkflowDefinitionError):
        parse_workflow_definition(data)


def test_non_predicate_shape_rejected():
    """Anything but {field, op, value} atoms (or and/any/not combinators) is
    rejected — no lambdas, no eval, no jinja."""
    data = _minimal_definition()
    data["steps"][1]["when"] = {"eval": "args.x > 3"}
    with pytest.raises(WorkflowDefinitionError):
        parse_workflow_definition(data)


def test_unknown_condition_op_rejected():
    data = _minimal_definition()
    data["steps"][1]["when"] = {"predicate": {"field": "args.x", "op": "regex_exec", "value": ".*"}}
    with pytest.raises(WorkflowDefinitionError):
        parse_workflow_definition(data)


def test_condition_depth_capped():
    deep: dict = {"predicate": {"field": "args.x", "op": "exists"}}
    for _ in range(10):  # exceeds the documented max nesting depth
        deep = {"not": deep}
    data = _minimal_definition()
    data["steps"][1]["when"] = deep
    with pytest.raises(WorkflowDefinitionError):
        parse_workflow_definition(data)


def test_condition_field_must_target_args_or_steps():
    data = _minimal_definition()
    data["steps"][1]["when"] = {"predicate": {"field": "__import__.os", "op": "exists"}}
    with pytest.raises(WorkflowDefinitionError):
        parse_workflow_definition(data)


def test_duplicate_step_ids_rejected():
    data = _minimal_definition()
    data["steps"][1]["id"] = "extract"
    with pytest.raises(WorkflowDefinitionError):
        parse_workflow_definition(data)


def test_hash_is_field_order_invariant():
    a = _minimal_definition()
    b = dict(reversed(list(a.items())))  # same content, different key order
    assert compute_definition_hash(a) == compute_definition_hash(b)


def test_hash_changes_with_content():
    a = _minimal_definition()
    b = _minimal_definition()
    b["steps"][0]["task"] = "Extract ALL clauses from {{args.doc_path}}"
    assert compute_definition_hash(a) != compute_definition_hash(b)


def test_hash_is_stable_across_parse_roundtrip():
    data = _minimal_definition()
    parsed = parse_workflow_definition(data)
    assert compute_definition_hash(data) == compute_definition_hash(parsed.canonical_dict())
