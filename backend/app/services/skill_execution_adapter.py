"""Skill frontmatter execution-plan adapter.

Skill loading remains progressive disclosure. This adapter converts executable
frontmatter hints into an auditable runtime plan so later execution still goes
through governed tools instead of hidden skill-specific paths.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from app.runtime.ccplus_contracts import PermissionProfileV1
from app.skills.types import ParsedSkill


_FORK_CONTEXTS = {"fork", "agent", "subagent", "worker", "isolated"}


@dataclass(frozen=True, slots=True)
class SkillExecutionPlan:
    skill: str
    skill_slug: str
    source: str
    execution_mode: str
    execution_tool: str
    agent_type: str | None
    permission_profile: dict[str, Any]
    tool_arguments: dict[str, Any]
    hook_events: tuple[str, ...] = ()


def _json_ready(value: Any) -> Any:
    return json.loads(json.dumps(value, ensure_ascii=False, default=str))


def _normalize_slug(value: str) -> str:
    return value.strip().lower().replace("_", "-").replace(" ", "-")


def _execution_mode(skill: ParsedSkill) -> str:
    context = str(skill.metadata.context or "").strip().lower()
    if context in _FORK_CONTEXTS or str(skill.metadata.agent or "").strip():
        return "fork"
    return "inline"


def build_skill_execution_plan(skill: ParsedSkill) -> SkillExecutionPlan:
    """Build the governed execution plan implied by a parsed Skill capsule."""
    profile = PermissionProfileV1(allowed_tools=tuple(skill.metadata.allowed_tools or ()))
    profile_payload = _json_ready(asdict(profile))
    mode = _execution_mode(skill)
    agent_type = str(skill.metadata.agent or "").strip() or None
    skill_name = skill.metadata.name
    skill_slug = _normalize_slug(skill_name)
    if mode == "fork":
        execution_tool = "spawn_subagent"
        tool_arguments = {
            "prompt": (
                f"Use the loaded skill `{skill_name}` as your operating instructions. "
                "Stay inside its scoped allowed tools and return a concise result digest with evidence."
            ),
            "description": f"Skill fork worker for {skill_name}",
            "subagent_type": "general-purpose",
            "isolation": "all",
            "skill": skill_name,
            "skill_source": skill.relative_path,
            "permission_profile": profile_payload,
        }
        if agent_type:
            tool_arguments["name"] = agent_type
    else:
        execution_tool = "load_skill"
        tool_arguments = {
            "name": skill_name,
            "skill_source": skill.relative_path,
            "permission_profile": profile_payload,
        }
    return SkillExecutionPlan(
        skill=skill_name,
        skill_slug=skill_slug,
        source=skill.relative_path,
        execution_mode=mode,
        execution_tool=execution_tool,
        agent_type=agent_type,
        permission_profile=profile_payload,
        tool_arguments=tool_arguments,
        hook_events=tuple(skill.metadata.hooks or ()),
    )


def skill_execution_plan_payload(skill: ParsedSkill) -> dict[str, Any]:
    """Serialize a SkillExecutionPlan for session metadata and workbench views."""
    return _json_ready(asdict(build_skill_execution_plan(skill)))
