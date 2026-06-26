"""Code-level registry of confirmation-gated tools.

The tool gate covers autonomous-enabling tools the agent can call directly. Which
tools those are, and which :data:`~app.services.plan_mode_core.ACTION_KINDS`
each maps onto, is declared *on the tool itself* via
``ToolMeta.plan_gate_action_kind`` — an import-time, code-level fact, **not**
``Tool.config``.

This module is the read side of that tag: it scans the decorator registry and
exposes a ``tool_name -> action_kind`` view the :class:`ToolRuntimeService` gate
consults before executing a tool.

Two flavours of tag exist:

* A real :data:`~app.services.plan_mode_core.ACTION_KINDS` value — the tool is
  *confirmation-gated*: the service calls ``PlanModeGate.check`` and refuses to
  execute it without confirmed authorization (``set_trigger`` and
  ``update_trigger``).
* :data:`BRIDGE_SELF` — the tool is *registered* as plan-governed (visible,
  auditable, future-proof) but keeps its **own** confirmation gate; the service
  must not double-block it. ``delegate_to_agent`` owns a runtime backstop that can inspect
  whether the delegation came from a real user or an unattended agent wake.
"""

from __future__ import annotations

from app.services.plan_mode_core import ACTION_KINDS, trigger_is_autonomous
from app.services.plan_mode_runtime_context import trusted_plan_mode_user_declined
from app.tools.decorator import get_all_registered_tools

#: Sentinel ``plan_gate_action_kind`` for tools that own their confirmation gate
#: and must only be *registered* here, never hard-blocked by the service gate.
BRIDGE_SELF: str = "bridge:self"
_PLAN_GATED_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "set_trigger",
        "update_trigger",
        "delegate_to_agent",
        # manage_tasks removed (F-2): agent-facing DB-Task tool retired.
        # start_long_task action_kind kept — REST api/tasks.py and manual trigger
        # still use it for the plan gate on human-initiated task creation.
    }
)


def _ensure_handlers_imported() -> None:
    """Make sure @tool handlers are registered before we read their metas.

    The decorator registry is populated at handler-import time. Different call
    sites reach this module before or after that import, so we trigger the
    platform's canonical collection (idempotent) when the registry has not yet
    been populated — keeping the gate correct regardless of import order. The
    import is local to dodge an import-time cycle (collector imports handlers
    which import the decorator).
    """
    registered = get_all_registered_tools()
    if registered and _PLAN_GATED_TOOL_NAMES.issubset(registered.keys()):
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


def _trigger_class_from_arguments(arguments: dict | None) -> str | None:
    if not isinstance(arguments, dict):
        return None
    config = arguments.get("config")
    if isinstance(config, dict) and config.get("trigger_class") is not None:
        return str(config.get("trigger_class") or "").strip()
    if arguments.get("trigger_class") is not None:
        return str(arguments.get("trigger_class") or "").strip()
    return None


def _set_trigger_action_kind(arguments: dict | None) -> str | None:
    if trusted_plan_mode_user_declined():
        return None
    if arguments is None:
        return "create_enabled_trigger"
    trigger_type = str(arguments.get("type") or "").strip()
    trigger_class = _trigger_class_from_arguments(arguments)
    return (
        "create_enabled_trigger"
        if trigger_is_autonomous(trigger_type=trigger_type, trigger_class=trigger_class)
        else None
    )


def _update_trigger_action_kind(arguments: dict | None) -> str | None:
    """Tool-layer update is advisory; only explicit autonomous reconfiguration gates.

    REST knows whether a trigger is being enabled and gates there. The tool does
    not carry current enabled/type state, so reason/lifecycle edits should not be
    hard-blocked. If a caller explicitly submits a replacement config that marks
    an autonomous trigger, we keep the safety gate.
    """
    if trusted_plan_mode_user_declined():
        return None
    if not isinstance(arguments, dict):
        return None
    config = arguments.get("config")
    if not isinstance(config, dict):
        return None
    trigger_type = str(arguments.get("type") or config.get("type") or "").strip()
    trigger_class = _trigger_class_from_arguments(arguments)
    if not trigger_type:
        return None
    return (
        "create_enabled_trigger"
        if trigger_is_autonomous(trigger_type=trigger_type, trigger_class=trigger_class)
        else None
    )


def hard_gated_action_kind(tool_name: str, arguments: dict | None = None) -> str | None:
    """Return the ``ACTION_KIND`` to hard-gate ``tool_name`` on, else ``None``.

    ``None`` means *do not invoke the service gate*: either the tool is not
    plan-governed at all, or it is a :data:`BRIDGE_SELF` tool that gates itself.
    Only a tag resolving to a real :data:`~app.services.plan_mode_core.ACTION_KINDS`
    member yields a hard gate.
    """
    action_kind = plan_gated_tool_action_kinds().get(tool_name)
    if tool_name == "set_trigger" and action_kind == "create_enabled_trigger":
        return _set_trigger_action_kind(arguments)
    if tool_name == "update_trigger" and action_kind == "create_enabled_trigger":
        return _update_trigger_action_kind(arguments)
    if action_kind and action_kind in ACTION_KINDS:
        return action_kind
    return None


__all__ = ["BRIDGE_SELF", "plan_gated_tool_action_kinds", "hard_gated_action_kind"]
