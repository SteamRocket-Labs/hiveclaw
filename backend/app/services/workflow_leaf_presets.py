"""Leaf preset registry.

A *leaf preset* is system-side capability injection for a workflow leaf,
keyed by leaf name: subagent spec overrides (tool allow/deny lists, system
prompt, the RC11 zero-tools switch) plus deterministic pre/post processing
hooks that run around the REAL ``spawn_subagent`` call.

Design constraints (invariant I3):

* the execution core stays ``spawn_subagent`` — presets wrap it, they never
  replace it, so governance / tenancy / audit / budgets are inherited
  unchanged (same wrapper precedent as ``metered_leaf_executor``);
* presets are CODE, not definition data — a workflow definition only refers
  to a leaf by name (the admission leaf-catalog binding stays the
  authorisation unit), so no definition can inject arbitrary prompts/tools;
* deterministic domain logic (ledger bookkeeping, citation neutralisation,
  quality gates, artifact writes) lives in ``pre_process``/``post_process``
  on the system side — never delegated to LLM goodwill, and the LLM never
  writes artifacts itself.

The registry is process-global on purpose: a daemon resuming a run knows
nothing about the run's domain, but it can resolve the preset from the leaf
name recorded in the archived definition.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:  # import cycle guard: workflow_launch imports this module
    from app.agents.subagent import SubagentResult, SubagentSpawnContext
    from app.runtime.workflow_engine import LeafOutcome, LeafRequest

#: async (request, ctx) -> rewritten task text, or None to keep the original.
PreProcess = Callable[["LeafRequest", "SubagentSpawnContext"], Awaitable[str | None]]
#: async (request, ctx, subagent_result, outcome) -> final LeafOutcome.
PostProcess = Callable[
    ["LeafRequest", "SubagentSpawnContext", "SubagentResult | None", "LeafOutcome"],
    Awaitable["LeafOutcome"],
]


@dataclass(slots=True)
class LeafPreset:
    """System-side capability bundle for one leaf name."""

    allowed_tools: tuple[str, ...] = ()
    excluded_tools: tuple[str, ...] = ()
    system_prompt: str = ""
    disable_tools: bool = False  # RC11: zero tools exposed (synthesis/critic)
    pre_process: PreProcess | None = None
    post_process: PostProcess | None = None
    #: free-form knobs for the pre/post hooks (e.g. artifact subdir name)
    options: dict[str, Any] = field(default_factory=dict)


_REGISTRY: dict[str, LeafPreset] = {}


def register_leaf_preset(leaf_name: str, preset: LeafPreset) -> None:
    """Register (or replace) the preset for ``leaf_name``."""
    _REGISTRY[leaf_name] = preset


def resolve_leaf_preset(leaf_name: str) -> LeafPreset | None:
    """Return the preset for ``leaf_name`` or None (generic spawn)."""
    return _REGISTRY.get(leaf_name)


def reset_leaf_presets() -> None:
    """Test hook: clear the registry."""
    _REGISTRY.clear()
