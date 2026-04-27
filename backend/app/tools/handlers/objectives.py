"""Objective ledger tools — create, inspect, update, and complete goals."""

from __future__ import annotations

import uuid

from app.tools.decorator import ToolMeta, tool


@tool(ToolMeta(
    name="list_objectives",
    description=(
        "List your durable objective ledger. Use this instead of treating focus.md as the source of truth. "
        "Results include key, status, priority, autonomy class, and description."
    ),
    parameters={
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Optional status filter, e.g. active, proposed, blocked, completed."},
        },
    },
    category="objectives",
    display_name="List Objectives",
    icon="\U0001f4cb",
    read_only=True,
    parallel_safe=True,
    governance="safe",
    adapter="agent_args",
))
async def list_objectives(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.agent_tool_domains.objectives import _handle_list_objectives
    return await _handle_list_objectives(agent_id, arguments)


@tool(ToolMeta(
    name="propose_objective",
    description=(
        "Create or update an objective candidate in the durable objective ledger. "
        "Explicit user requests may become active; inferred or external-side-effect goals stay proposed until approved."
    ),
    parameters={
        "type": "object",
        "properties": {
            "description": {"type": "string", "description": "Objective description."},
            "objective_key": {"type": "string", "description": "Optional stable key; otherwise generated from description."},
            "success_criteria": {"type": "string", "description": "What evidence proves the objective is complete."},
            "autonomy_class": {
                "type": "string",
                "enum": [
                    "explicit_user_request",
                    "confirmed_hr_blueprint",
                    "internal_self_improvement",
                    "implicit_inference",
                    "external_side_effect",
                ],
                "description": "Why this objective may or may not activate autonomously.",
            },
            "risk_level": {"type": "string", "enum": ["low", "medium", "high"]},
            "confidence": {"type": "number"},
            "priority": {"type": "integer"},
            "wake_policy": {"type": "object", "description": "Optional wake policy: {type: cron|once|interval, config: {...}}."},
            "evidence": {"type": "object", "description": "Evidence that justifies this objective."},
            "source": {"type": "string", "description": "Source label, default agent_tool."},
        },
        "required": ["description"],
    },
    category="objectives",
    display_name="Propose Objective",
    icon="\U0001f3af",
    governance="sensitive",
    adapter="agent_args",
))
async def propose_objective(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.agent_tool_domains.objectives import _handle_propose_objective
    return await _handle_propose_objective(agent_id, arguments)


@tool(ToolMeta(
    name="update_objective",
    description=(
        "Update an existing objective's status, priority, success criteria, blocked reason, or wake policy. "
        "Use complete_objective, not this tool, when marking work completed."
    ),
    parameters={
        "type": "object",
        "properties": {
            "objective_id": {"type": "string"},
            "objective_key": {"type": "string"},
            "status": {"type": "string", "enum": ["proposed", "active", "running", "blocked", "snoozed", "rejected"]},
            "description": {"type": "string"},
            "success_criteria": {"type": "string"},
            "blocked_reason": {"type": "string"},
            "priority": {"type": "integer"},
            "wake_policy": {"type": "object"},
            "metadata": {"type": "object"},
        },
    },
    category="objectives",
    display_name="Update Objective",
    icon="\u270f\ufe0f",
    governance="sensitive",
    adapter="agent_args",
))
async def update_objective(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.agent_tool_domains.objectives import _handle_update_objective
    return await _handle_update_objective(agent_id, arguments)


@tool(ToolMeta(
    name="complete_objective",
    description=(
        "Mark an objective completed, but only with concrete evidence. "
        "Evidence can be an artifact path, sent-message confirmation, test output, or other verifiable result."
    ),
    parameters={
        "type": "object",
        "properties": {
            "objective_id": {"type": "string"},
            "objective_key": {"type": "string"},
            "evidence": {"type": "string", "description": "Required evidence proving completion."},
            "result_summary": {"type": "string"},
        },
        "required": ["evidence"],
    },
    category="objectives",
    display_name="Complete Objective",
    icon="\u2705",
    governance="sensitive",
    adapter="agent_args",
))
async def complete_objective(agent_id: uuid.UUID, arguments: dict) -> str:
    from app.services.agent_tool_domains.objectives import _handle_complete_objective
    return await _handle_complete_objective(agent_id, arguments)
