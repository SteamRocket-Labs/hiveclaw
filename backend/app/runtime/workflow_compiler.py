"""Workflow compiler (§9 P2): structural validation beyond schema parsing.

This is the layer that makes "agent-generated structured data" safe to
accept at runtime (§3.2): step-type allowlisting happens in the schema
(discriminated union); here we verify everything that needs cross-step
context — reference resolution (no forward/dangling references), leaf
catalog binding, gate enforcement for external/irreversible effects, and the
reversible-only retry rule. Pure logic; the admission layer (budget/quota
preflight) runs separately with tenant context.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from app.runtime.workflow_definition import (
    AgentStep,
    FanoutStep,
    WorkflowDefinition,
    WorkflowDefinitionError,
    compute_definition_hash,
    parse_workflow_definition,
)

_TEMPLATE_REF_RE = re.compile(r"\{\{\s*([A-Za-z0-9_][A-Za-z0-9_.\-]*)\s*\}\}")


class WorkflowCompileError(ValueError):
    """Raised when a definition is schema-valid but structurally unsound."""


@dataclass(slots=True)
class CompiledWorkflow:
    definition: WorkflowDefinition
    definition_hash: str
    leaf_names: set[str] = field(default_factory=set)


def _template_refs(text: str) -> list[str]:
    return _TEMPLATE_REF_RE.findall(text)


def _check_reference(ref: str, *, args_keys: set[str], prior_steps: set[str], allow_item: bool, where: str) -> None:
    parts = ref.split(".")
    root = parts[0]
    if root == "args":
        if len(parts) < 2 or parts[1] not in args_keys:
            raise WorkflowCompileError(f"{where}: unknown args reference {ref!r}")
        return
    if root == "steps":
        if len(parts) < 2 or parts[1] not in prior_steps:
            raise WorkflowCompileError(
                f"{where}: reference {ref!r} must point at a PRIOR step (forward/unknown refs forbidden)"
            )
        return
    if root == "item" and allow_item:
        return
    raise WorkflowCompileError(f"{where}: unsupported reference root {ref!r}")


def compile_workflow(
    data: dict | WorkflowDefinition,
    *,
    known_leaves: set[str] | None = None,
) -> CompiledWorkflow:
    try:
        definition = parse_workflow_definition(data)
    except WorkflowDefinitionError as exc:
        raise WorkflowCompileError(str(exc)) from exc

    raw_for_hash = data if isinstance(data, dict) else definition.canonical_dict()
    definition_hash = compute_definition_hash(raw_for_hash)

    args_keys = set(definition.args_schema.keys())
    prior_steps: set[str] = set()
    seen_gate = False
    leaf_names: set[str] = set()

    for step in definition.steps:
        where = f"step {step.id!r}"

        # Conditions may only reference args or PRIOR step outputs.
        if step.when is not None:
            for predicate in step.when.iter_predicates():
                _check_reference(
                    predicate.field, args_keys=args_keys, prior_steps=prior_steps, allow_item=False, where=where
                )

        if isinstance(step, AgentStep):
            leaf_names.add(step.leaf.name)
            for ref in _template_refs(step.task):
                _check_reference(ref, args_keys=args_keys, prior_steps=prior_steps, allow_item=False, where=where)
        elif isinstance(step, FanoutStep):
            leaf_names.add(step.leaf.name)
            items_ref = step.items_from
            _check_reference(items_ref, args_keys=args_keys, prior_steps=prior_steps, allow_item=False, where=where)
            for ref in _template_refs(step.per_item_task):
                _check_reference(ref, args_keys=args_keys, prior_steps=prior_steps, allow_item=True, where=where)

        # 对外/不可逆步骤强制 gate（§3.2): an external/irreversible step must
        # have a gate_step somewhere before it in the sequence.
        if step.effects in ("external", "irreversible") and not seen_gate:
            raise WorkflowCompileError(
                f"{where}: effects={step.effects!r} requires a preceding gate_step (§10 invariant ⑤)"
            )

        # v1 decision 1: retry is reversible-only.
        retry = getattr(step, "retry", None)
        if retry is not None and step.effects in ("external", "irreversible"):
            raise WorkflowCompileError(f"{where}: retry is reversible-only; effects={step.effects!r} cannot retry")

        if step.type == "gate_step":
            seen_gate = True
        prior_steps.add(step.id)

    if known_leaves is not None:
        unknown = leaf_names - known_leaves
        if unknown:
            raise WorkflowCompileError(f"unknown leaves (not in catalog): {sorted(unknown)}")

    return CompiledWorkflow(definition=definition, definition_hash=definition_hash, leaf_names=leaf_names)
