"""Skill frontmatter execution-plan adapter.

Skill loading remains progressive disclosure. This adapter converts executable
frontmatter hints into an auditable runtime plan so later execution still goes
through governed tools instead of hidden skill-specific paths.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Any

from app.runtime.ccplus_contracts import PermissionMode, PermissionProfileV1
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
    profile = PermissionProfileV1(mode=PermissionMode.AUTO, allowed_tools=tuple(skill.metadata.allowed_tools or ()))
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
            "run_in_background": True,
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


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, (list, tuple, set)):
        return []
    return [str(item) for item in value if str(item).strip()]


def apply_skill_execution_plans_to_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Apply stored skill execution plans to runtime-consumed metadata.

    This is the runtime consumer for frontmatter execution plans. It keeps skill
    loading progressive-disclosure only, but makes scoped allowed tools and fork
    handoffs visible to the governed tool loop.
    """
    plans = metadata.get("skill_execution_plans")
    if not isinstance(plans, list):
        return metadata

    permission_profile = dict(metadata.get("permission_profile") or {})
    capability_snapshot = permission_profile.get("capability_policy_snapshot")
    if isinstance(capability_snapshot, dict) and capability_snapshot.get("session_exact_scope") is True:
        return metadata
    merged_allowed = _string_list(permission_profile.get("allowed_tools"))
    handoffs_by_slug = {
        str(item.get("skill_slug") or item.get("skill")): dict(item)
        for item in metadata.get("pending_skill_handoffs", [])
        if isinstance(item, dict)
    }

    for raw_plan in plans:
        if not isinstance(raw_plan, dict):
            continue
        profile = raw_plan.get("permission_profile")
        if isinstance(profile, dict):
            permission_profile.setdefault("mode", profile.get("mode") or "auto")
            for tool_name in _string_list(profile.get("allowed_tools")):
                if tool_name not in merged_allowed:
                    merged_allowed.append(tool_name)
        if raw_plan.get("execution_tool") == "spawn_subagent":
            skill_slug = str(raw_plan.get("skill_slug") or raw_plan.get("skill") or "").strip()
            if not skill_slug:
                continue
            handoffs_by_slug[skill_slug] = {
                "skill": str(raw_plan.get("skill") or skill_slug),
                "skill_slug": skill_slug,
                "source": str(raw_plan.get("source") or ""),
                "execution_tool": "spawn_subagent",
                "tool_arguments": {
                    "run_in_background": True,
                    **dict(raw_plan.get("tool_arguments") or {}),
                },
                "permission_profile": dict(profile) if isinstance(profile, dict) else {},
            }

    if merged_allowed:
        permission_profile["allowed_tools"] = merged_allowed
        permission_profile.setdefault("mode", "auto")
        metadata["permission_profile"] = permission_profile
    if handoffs_by_slug:
        metadata["pending_skill_handoffs"] = list(handoffs_by_slug.values())
    return metadata


def pending_skill_handoffs_for_execution(metadata: dict[str, Any]) -> list[dict[str, Any]]:
    """Return not-yet-executed fork handoffs after normalizing skill plans."""
    apply_skill_execution_plans_to_metadata(metadata)
    handoffs = metadata.get("pending_skill_handoffs")
    if not isinstance(handoffs, list):
        return []
    executed = {
        str(item.get("skill_slug") or item.get("skill") or "").strip()
        for item in metadata.get("executed_skill_handoffs", [])
        if isinstance(item, dict)
    }
    return [
        dict(item)
        for item in handoffs
        if isinstance(item, dict)
        and str(item.get("execution_tool") or "") == "spawn_subagent"
        and str(item.get("skill_slug") or item.get("skill") or "").strip()
        and str(item.get("skill_slug") or item.get("skill") or "").strip() not in executed
    ]


def record_skill_handoff_execution(
    metadata: dict[str, Any],
    handoff: dict[str, Any],
    *,
    tool_call_id: str | None,
    result: Any,
) -> None:
    """Move one handoff from pending to executed with an auditable result digest."""
    skill_slug = str(handoff.get("skill_slug") or handoff.get("skill") or "").strip()
    if not skill_slug:
        return
    executed = [dict(item) for item in metadata.get("executed_skill_handoffs", []) if isinstance(item, dict)]
    executed = [
        item for item in executed if str(item.get("skill_slug") or item.get("skill") or "").strip() != skill_slug
    ]
    executed.append(
        {
            "skill": str(handoff.get("skill") or skill_slug),
            "skill_slug": skill_slug,
            "source": str(handoff.get("source") or ""),
            "execution_tool": "spawn_subagent",
            "tool_call_id": tool_call_id,
            "result": str(result),
        }
    )
    metadata["executed_skill_handoffs"] = executed
    pending = [
        dict(item)
        for item in metadata.get("pending_skill_handoffs", [])
        if isinstance(item, dict) and str(item.get("skill_slug") or item.get("skill") or "").strip() != skill_slug
    ]
    if pending:
        metadata["pending_skill_handoffs"] = pending
    else:
        metadata.pop("pending_skill_handoffs", None)
