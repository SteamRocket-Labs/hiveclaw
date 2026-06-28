"""Session-scoped hook registration for loaded Skill frontmatter."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.runtime.hooks import HookContext, HookEvent, HookRegistry, HookResult, hook_registry
from app.runtime.session import SessionContext
from app.services.skill_execution_adapter import skill_execution_plan_payload
from app.skills.loader import WorkspaceSkillLoader
from app.skills.registry import SkillRegistry
from app.skills.types import ParsedSkill


def _normalize_slug(value: str) -> str:
    return value.strip().lower().replace("_", "-").replace(" ", "-")


def _resolve_skill(workspace: Path, skill_name: str) -> ParsedSkill | None:
    loader = WorkspaceSkillLoader()
    registry = SkillRegistry()
    registry.register_many(loader.load_from_workspace(workspace))
    try:
        return registry.resolve(skill_name)
    except KeyError:
        return None


def _skill_hook_context(skill_name: str, event: HookEvent) -> str:
    return (
        f"Loaded skill `{skill_name}` declares a `{event.value}` hook. "
        "Use its loaded instructions when this event applies."
    )


def _build_skill_hook_handler(skill_name: str, event: HookEvent):
    context = _skill_hook_context(skill_name, event)

    async def _handler(_ctx: HookContext) -> HookResult:
        return HookResult(additional_contexts=[context])

    return _handler


def _append_session_registrations(session_context: SessionContext, registrations: list[dict[str, Any]]) -> None:
    existing = session_context.metadata.get("skill_hook_registrations")
    if not isinstance(existing, list):
        existing = []
    by_key = {str(item.get("key")): dict(item) for item in existing if isinstance(item, dict)}
    for registration in registrations:
        by_key[str(registration["key"])] = dict(registration)
    session_context.metadata["skill_hook_registrations"] = list(by_key.values())


def _append_session_execution_plan(session_context: SessionContext, parsed: ParsedSkill) -> None:
    existing = session_context.metadata.get("skill_execution_plans")
    if not isinstance(existing, list):
        existing = []
    plan = skill_execution_plan_payload(parsed)
    by_key = {str(item.get("skill_slug") or item.get("skill")): dict(item) for item in existing if isinstance(item, dict)}
    by_key[str(plan["skill_slug"])] = plan
    session_context.metadata["skill_execution_plans"] = list(by_key.values())


def register_loaded_skill_hooks(
    workspace: Path,
    skill_name: str,
    *,
    session_context: SessionContext,
    registry: HookRegistry | None = None,
    agent_id: Any | None = None,
) -> list[dict[str, Any]]:
    """Register hooks declared in a loaded Skill's frontmatter.

    The registration is intentionally session-scoped. Frontmatter declares that
    the loaded skill wants a lifecycle event, but it does not execute scripts or
    bypass tool governance. Executable skill components must be called through
    explicit governed tools such as `run_skill_tool`.
    """
    session_id = str(session_context.session_id or "").strip()
    if not session_id:
        return []

    parsed = _resolve_skill(workspace, skill_name)
    if parsed is None:
        return []
    _append_session_execution_plan(session_context, parsed)
    if not parsed.metadata.hooks:
        return []

    target_registry = registry or hook_registry
    slug = _normalize_slug(parsed.metadata.name or skill_name)
    registrations: list[dict[str, Any]] = []
    for raw_event in parsed.metadata.hooks:
        try:
            event = HookEvent(str(raw_event))
        except ValueError:
            continue

        key = f"skill:{session_id}:{slug}:{event.value}"
        target_registry.register_spec(
            event,
            _build_skill_hook_handler(parsed.metadata.name, event),
            {"session_ids": [session_id]},
            key=key,
            handler_name="skill_frontmatter_hook",
        )
        registrations.append(
            {
                "key": key,
                "event": event.value,
                "skill": parsed.metadata.name,
                "skill_slug": slug,
                "session_id": session_id,
                "agent_id": str(agent_id) if agent_id is not None else None,
                "source": parsed.relative_path,
            }
        )

    if registrations:
        _append_session_registrations(session_context, registrations)
    return registrations
