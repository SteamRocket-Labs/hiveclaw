"""Workflow definition schema + canonical hash (§9 P2, §3.2).

The definition is **serializable structured data** — it can only orchestrate
platform primitives and governed leaves. There is no code-execution surface:
no eval, no Jinja, no Python/JS expressions, no template evaluation. Task
strings may carry ``{{args.x}}`` / ``{{steps.<id>.output...}}`` placeholders,
which the engine resolves by pure key lookup (P3) — reference substitution,
not expression evaluation.

v1 expressiveness (§10 decision 1): ``sequence`` + bounded ``fanout/map`` +
structured ``condition`` + ``gate`` + ``wait_until``/time suspend; ``retry``
only on reversible steps. Conditions are comparison ASTs (§10 decision 2):
atoms are ``{field, op, value}`` with ``op ∈ {eq,ne,gt,lt,gte,lte,contains,
exists,in}``, combined only via ``all``/``any``/``not`` with bounded depth.
"""

from __future__ import annotations

import hashlib
import json
import re
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator, model_validator

MAX_CONDITION_DEPTH = 5

_FIELD_PATH_RE = re.compile(r"^(args|steps)\.[A-Za-z0-9_][A-Za-z0-9_.\-]*$")
_STEP_ID_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]{0,99}$")

ConditionOp = Literal["eq", "ne", "gt", "lt", "gte", "lte", "contains", "exists", "in"]
EffectsLevel = Literal["read_only", "workspace_write", "external", "irreversible"]


class WorkflowDefinitionError(ValueError):
    """Raised when a definition payload fails schema validation."""


class ConditionPredicate(BaseModel):
    """Atomic predicate — the ONLY computable form a condition may take."""

    model_config = ConfigDict(extra="forbid")

    field: str
    op: ConditionOp
    value: Any = None

    @field_validator("field")
    @classmethod
    def _field_targets_args_or_steps(cls, v: str) -> str:
        if not _FIELD_PATH_RE.match(v):
            raise ValueError(f"condition field must be an 'args.*' or 'steps.<id>.*' path, got {v!r}")
        return v


class Condition(BaseModel):
    """Boolean combinator node: exactly one of predicate / all / any / not."""

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    predicate: ConditionPredicate | None = None
    all: list["Condition"] | None = None
    any: list["Condition"] | None = None
    not_: "Condition | None" = Field(None, alias="not")

    @model_validator(mode="after")
    def _exactly_one_branch(self) -> "Condition":
        branches = [self.predicate, self.all, self.any, self.not_]
        if sum(1 for b in branches if b is not None) != 1:
            raise ValueError("condition must have exactly one of: predicate, all, any, not")
        return self

    def depth(self) -> int:
        if self.predicate is not None:
            return 1
        if self.not_ is not None:
            return 1 + self.not_.depth()
        children = self.all if self.all is not None else (self.any or [])
        return 1 + max((c.depth() for c in children), default=0)

    def iter_predicates(self):
        if self.predicate is not None:
            yield self.predicate
        if self.not_ is not None:
            yield from self.not_.iter_predicates()
        for child in (self.all or []) + (self.any or []):
            yield from child.iter_predicates()


class LeafRef(BaseModel):
    """Reference to a governed leaf executor (axis-1 subagent spec or a
    persistent 定义.md name). Only orchestratable primitives — never code."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=200)
    type: Literal["explorer", "worker", "critic"] = "worker"
    max_tool_rounds: int | None = Field(None, ge=1, le=64)


class RetrySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_attempts: int = Field(ge=1, le=5)


class _StepBase(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    when: Condition | None = None
    effects: EffectsLevel = "read_only"

    @field_validator("id")
    @classmethod
    def _id_shape(cls, v: str) -> str:
        if not _STEP_ID_RE.match(v):
            raise ValueError(f"step id must match {_STEP_ID_RE.pattern}, got {v!r}")
        return v

    @model_validator(mode="after")
    def _condition_depth(self) -> "_StepBase":
        if self.when is not None and self.when.depth() > MAX_CONDITION_DEPTH:
            raise ValueError(f"condition nesting exceeds max depth {MAX_CONDITION_DEPTH}")
        return self


class AgentStep(_StepBase):
    type: Literal["agent_step"]
    leaf: LeafRef
    task: str = Field(min_length=1)
    retry: RetrySpec | None = None


class FanoutStep(_StepBase):
    type: Literal["fanout_step"]
    leaf: LeafRef
    items_from: str
    per_item_task: str = Field(min_length=1)
    max_concurrency: int = Field(default=4, ge=1, le=64)

    @field_validator("items_from")
    @classmethod
    def _items_from_args(cls, v: str) -> str:
        if not v.startswith("args."):
            raise ValueError(f"items_from must be a bounded 'args.*' array reference, got {v!r}")
        return v


class GateStep(_StepBase):
    type: Literal["gate_step"]
    reason: str = Field(min_length=1)


class WaitUntilStep(_StepBase):
    type: Literal["wait_until_step"]
    delay_seconds: int | None = Field(None, ge=1)
    until: str | None = None  # ISO timestamp literal or an args.* reference

    @model_validator(mode="after")
    def _exactly_one_time(self) -> "WaitUntilStep":
        if (self.delay_seconds is None) == (self.until is None):
            raise ValueError("wait_until_step requires exactly one of delay_seconds / until")
        return self


Step = Annotated[Union[AgentStep, FanoutStep, GateStep, WaitUntilStep], Field(discriminator="type")]


class BudgetSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_total_tokens: int = Field(default=200_000, ge=1)
    max_wall_clock_seconds: int | None = Field(None, ge=1)


class ArgSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["string", "number", "boolean", "array", "object"]
    required: bool = False


class WorkflowDefinition(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    name: str = Field(min_length=1, max_length=200)
    description: str = ""
    args_schema: dict[str, ArgSpec] = Field(default_factory=dict)
    default_budget: BudgetSpec = Field(default_factory=BudgetSpec)
    steps: list[Step] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_step_ids(self) -> "WorkflowDefinition":
        seen: set[str] = set()
        for step in self.steps:
            if step.id in seen:
                raise ValueError(f"duplicate step id {step.id!r}")
            seen.add(step.id)
        return self

    def canonical_dict(self) -> dict:
        """Round-trip-stable representation: exactly what the author wrote
        (unset defaults excluded), aliases honored — hash-equal to the raw
        payload it was parsed from."""
        return self.model_dump(by_alias=True, exclude_unset=True, exclude_none=True)


def parse_workflow_definition(data: dict | WorkflowDefinition) -> WorkflowDefinition:
    if isinstance(data, WorkflowDefinition):
        return data
    if not isinstance(data, dict):
        raise WorkflowDefinitionError(f"definition must be a mapping, got {type(data).__name__}")
    try:
        return WorkflowDefinition.model_validate(data)
    except ValidationError as exc:
        raise WorkflowDefinitionError(str(exc)) from exc


def compute_definition_hash(data: dict | WorkflowDefinition) -> str:
    """Canonical content hash: key-order independent, content sensitive."""
    payload = data.canonical_dict() if isinstance(data, WorkflowDefinition) else data
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()
