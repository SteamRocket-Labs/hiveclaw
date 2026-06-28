"""Shared Agent Team discovery contract for runtime and product payloads."""

from __future__ import annotations

from typing import Any

TEAM_CREATE_SEMANTICS = "container_only"
TEAMMATE_CREATION_TOOL = "spawn_subagent"


def teammate_creation_discovery(team_name: str) -> dict[str, Any]:
    return {
        "team_create_semantics": TEAM_CREATE_SEMANTICS,
        "teammate_creation_tool": TEAMMATE_CREATION_TOOL,
        "teammate_creation_args": {
            "team_name": str(team_name or "").strip(),
            "name": "<member-name>",
            "prompt": "<task>",
        },
    }
