"""Code-level registry of Plan-Mode-gated tools (docs/plan-mode-design.md §9.2).

The Plan Mode tool gate is the largest slice of the *early-intercept* layer: it
covers every autonomous-enabling tool the agent can call directly. Which tools
those are, and which :data:`~app.services.plan_mode_core.ACTION_KINDS` each maps
onto, is declared *on the tool itself* via ``ToolMeta.plan_gate_action_kind`` —
an import-time, code-level fact, **not** ``Tool.config`` (the DB seeder leaves an
already-non-empty config untouched, so it cannot be the governance source of
truth, §9.2).

This module is the read side of that tag: it scans the decorator registry and
exposes a ``tool_name -> action_kind`` view the :class:`ToolRuntimeService` gate
consults before executing a tool.

Two flavours of tag exist (§9.2):

* A real :data:`~app.services.plan_mode_core.ACTION_KINDS` value — the tool is
  *hard-gated*: the service calls ``PlanModeGate.check`` and refuses to execute
  it without a confirmed plan (``set_trigger``, ``update_trigger``,
  ``delegate_to_agent``, and the auto-executing ``manage_tasks`` create path).
* :data:`BRIDGE_SELF` — the tool is *registered* as plan-governed (visible,
  auditable, future-proof) but keeps its **own** confirmation gate; the service
  must not double-block it. The sole MVP case is ``deep_research_start`` whose
  ``plan_confirmed`` parameter already enforces user confirmation (§9.2:
  "MVP 只桥接登记 PlanRequest,不重构").
"""

from __future__ import annotations

from app.services.plan_mode_core import ACTION_KINDS
from app.tools.decorator import get_all_registered_tools

#: Sentinel ``plan_gate_action_kind`` for tools that own their confirmation gate
#: and must only be *registered* here, never hard-blocked by the service gate.
BRIDGE_SELF: str = "bridge:self"


def _ensure_handlers_imported() -> None:
    """Make sure @tool handlers are registered before we read their metas.

    The decorator registry is populated at handler-import time. Different call
    sites reach this module before or after that import, so we trigger the
    platform's canonical collection (idempotent) when the registry has not yet
    been populated — keeping the gate correct regardless of import order. The
    import is local to dodge an import-time cycle (collector imports handlers
    which import the decorator).
    """
    if get_all_registered_tools():
        return
    from app.tools.collector import collect_tools

    collect_tools()


def plan_gated_tool_action_kinds() -> dict[str, str]:
    """Return ``{tool_name: plan_gate_action_kind}`` for every tagged tool.

    Includes aliases (they share the canonical tool's :class:`ToolMeta`) so a
    call under any registered name is gated identically. The value is either a
    real ``ACTION_KIND`` (hard gate) or :data:`BRIDGE_SELF` (registration only).
    """
    _ensure_handlers_imported()
    return {
        name: meta.plan_gate_action_kind
        for name, (meta, _fn) in get_all_registered_tools().items()
        if meta.plan_gate_action_kind
    }


def _manage_tasks_action_kind(arguments: dict | None) -> str | None:
    if arguments is None:
        return "start_long_task"
    action = str(arguments.get("action") or "").strip()
    if action != "create":
        return None
    task_type = str(arguments.get("task_type") or "todo").strip() or "todo"
    return "start_long_task" if task_type != "supervision" else None


def hard_gated_action_kind(tool_name: str, arguments: dict | None = None) -> str | None:
    """Return the ``ACTION_KIND`` to hard-gate ``tool_name`` on, else ``None``.

    ``None`` means *do not invoke the service gate*: either the tool is not
    plan-governed at all, or it is a :data:`BRIDGE_SELF` tool that gates itself.
    Only a tag resolving to a real :data:`~app.services.plan_mode_core.ACTION_KINDS`
    member yields a hard gate.
    """
    action_kind = plan_gated_tool_action_kinds().get(tool_name)
    if tool_name == "manage_tasks" and action_kind == "start_long_task":
        return _manage_tasks_action_kind(arguments)
    if action_kind and action_kind in ACTION_KINDS:
        return action_kind
    return None


__all__ = ["BRIDGE_SELF", "plan_gated_tool_action_kinds", "hard_gated_action_kind"]
