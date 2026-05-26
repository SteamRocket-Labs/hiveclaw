"""Skill tools — load skill instructions, search packs and skills."""

from __future__ import annotations

import uuid
from pathlib import Path

from app.tools.decorator import ToolMeta, tool


# -- load_skill ---------------------------------------------------------------

@tool(ToolMeta(
    name="load_skill",
    description=(
        "Load the full instructions for a named skill from the skills/ directory.\n\n"
        "Usage:\n"
        "- Use this when the current task clearly matches a known skill in the catalog.\n"
        "- Load one relevant skill at a time, then follow its instructions.\n"
        "- Do NOT load a skill speculatively if the task does not clearly match it.\n"
        "- Do NOT use `run_command` to inspect platform or channel credential env vars from a skill; "
        "use dedicated tools and platform channel config instead.\n"
        "- If no skill matches, use your builtin tools directly instead of guessing."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Skill name or skill path, e.g. 'web research', 'data-analysis', or 'skills/web-research/SKILL.md'",
            }
        },
        "required": ["name"],
    },
    category="skills",
    display_name="Load Skill",
    icon="\U0001f9e0",
    adapter="workspace_args",
))
def load_skill(workspace: Path, arguments: dict, tenant_id: str | None = None) -> str:
    from app.services.agent_tool_domains.workspace import _load_skill
    return _load_skill(workspace, arguments.get("name", ""))


# -- save_skill ---------------------------------------------------------------

@tool(ToolMeta(
    name="save_skill",
    description=(
        "Save a reusable workflow as a skill inside the workspace skills/ directory.\n\n"
        "Usage:\n"
        "- Only use this after a workflow has succeeded repeatedly and the steps, decision rules, and verification pattern are stable.\n"
        "- Save durable, reusable instructions that will help future runs of a similar task.\n"
        "- Do NOT save one-off notes, transient state, or raw transcripts as skills. Durable user corrections belong in `save_memory` when the Memory Guide qualifies them; operational notes and evidence belong in workspace files.\n"
        "- Include declared tools/packs only when they are truly part of the repeatable workflow.\n"
        "- Use `overwrite: true` only when intentionally updating an existing skill."
    ),
    parameters={
        "type": "object",
        "properties": {
            "name": {
                "type": "string",
                "description": "Display name for the skill, e.g. 'Deployment Review' or 'Market Research'.",
            },
            "description": {
                "type": "string",
                "description": "Short one-line description explaining when to use the skill.",
            },
            "instructions": {
                "type": "string",
                "description": "The reusable workflow instructions. Include steps, decision rules, verification, and output expectations.",
            },
            "tools": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional declared tools used by this skill, e.g. ['web_search', 'web_fetch'].",
            },
            "packs": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Optional declared capability packs used by this skill, e.g. ['web_pack'].",
            },
            "folder_name": {
                "type": "string",
                "description": "Optional explicit folder slug/path segment under skills/. Defaults to a normalized version of the name.",
            },
            "overwrite": {
                "type": "boolean",
                "description": "When true, update an existing skill with the same name/path instead of failing.",
            },
        },
        "required": ["name", "description", "instructions"],
    },
    category="skills",
    display_name="Save Skill",
    icon="\U0001f4da",
    governance="sensitive",
    adapter="agent_workspace_args",
))
async def save_skill(agent_id: uuid.UUID, workspace: Path, arguments: dict) -> str:
    from app.core.execution_context import get_tool_tenant_id
    from app.services.agent_tool_domains.workspace import _save_skill, _workspace_error, check_declared_packs_authorized
    from app.services.skill_guard import scan_skill_files

    declared_packs = tuple(arguments.get("packs", []) or ())
    tenant_id = get_tool_tenant_id()
    ok, reason = await check_declared_packs_authorized(
        tenant_id=tenant_id,
        agent_id=agent_id,
        declared_packs=declared_packs,
    )
    if not ok:
        return _workspace_error(
            "save_skill",
            "unauthorized_pack",
            reason,
            actionable_hint="Remove the unauthorized pack from `packs`, or request capability access before re-saving.",
        )

    guard_report = scan_skill_files(
        [
            {
                "path": "SKILL.md",
                "content": str(arguments.get("instructions", "") or ""),
            }
        ],
        source="save_skill",
    )
    if not guard_report.allowed:
        categories = ", ".join(finding.category for finding in guard_report.blocking_findings)
        return _workspace_error(
            "save_skill",
            "skill_guard_blocked",
            f"SkillGuard blocked this skill before activation: {categories}",
            actionable_hint="Remove embedded secrets, remote shell installers, path escapes, or destructive commands.",
        )

    return _save_skill(
        workspace,
        name=arguments.get("name", ""),
        description=arguments.get("description", ""),
        instructions=arguments.get("instructions", ""),
        declared_tools=tuple(arguments.get("tools", []) or ()),
        declared_packs=declared_packs,
        folder_name=arguments.get("folder_name"),
        overwrite=bool(arguments.get("overwrite", False)),
    )


# -- tool_search --------------------------------------------------------------

@tool(ToolMeta(
    name="tool_search",
    description=(
        "Search for delayed capability packs and skills that can be activated on demand.\n\n"
        "Usage:\n"
        "- Use this when you suspect a missing capability but do not yet know the exact skill or pack name.\n"
        "- This only returns summaries and does not auto-load tools.\n"
        "- Prefer builtin tools, loaded skills, and direct web/file workflows before reaching for admin-only MCP extensions.\n"
        "- Do NOT use this as a general way to browse admin-only MCP extensions during ordinary task execution.\n"
        "- After reading the summaries, call `load_skill` or the matching activation tool explicitly."
    ),
    parameters={
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "Optional query like 'feishu', 'web research', or 'email'",
            },
        },
    },
    category="skills",
    display_name="Tool Search",
    icon="\U0001f50d",
    read_only=True,
    parallel_safe=False,
    adapter="workspace_args",
))
def tool_search(workspace: Path, arguments: dict, tenant_id: str | None = None) -> str:
    from app.services.agent_tool_domains.workspace import _tool_search
    return _tool_search(workspace, arguments.get("query", ""))


# -- pin_skill ----------------------------------------------------------------

@tool(ToolMeta(
    name="pin_skill",
    description=(
        "Pin or unpin one of your own skills to control auto-archival.\n\n"
        "Pinned skills are never auto-archived by the skill curator, even after "
        "long disuse; unpin to let an unused skill age out naturally.\n"
        "Use this when the skill-evolution digest warns that a still-useful "
        "skill is nearing auto-archival, or to retire a skill you no longer need."
    ),
    parameters={
        "type": "object",
        "properties": {
            "skill": {
                "type": "string",
                "description": "Skill slug — the folder name under skills/ (e.g. 'deploy-checklist').",
            },
            "pinned": {
                "type": "boolean",
                "description": "true to pin (protect from archival), false to unpin. Defaults to true.",
            },
        },
        "required": ["skill"],
    },
    category="skills",
    display_name="Pin Skill",
    icon="\U0001f4cc",
    read_only=False,
    parallel_safe=False,
    governance="sensitive",
    adapter="workspace_args",
))
def pin_skill(workspace: Path, arguments: dict, tenant_id: str | None = None) -> str:
    from app.services.skill_curator import set_skill_pinned

    slug = str(arguments.get("skill", "")).strip()
    if not slug:
        return "❌ pin_skill: 'skill' (the skill slug) is required."

    pinned_arg = arguments.get("pinned", True)
    if isinstance(pinned_arg, str):
        pinned = pinned_arg.strip().lower() not in {"0", "false", "no", "off"}
    else:
        pinned = bool(pinned_arg)

    set_skill_pinned(workspace, slug, pinned)
    if pinned:
        return f"📌 Pinned skill '{slug}'. The curator will never auto-archive it while pinned."
    return f"Unpinned skill '{slug}'. It can now age out and be auto-archived if it stays unused."
