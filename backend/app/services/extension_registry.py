"""CCPlus extension registry projection.

This is a read-model projection over existing extension surfaces. It does not
install, enable, or delete tenant extensions; durable control-plane storage can
back this shape without changing the runtime contract.
"""

from __future__ import annotations

from typing import Any, Iterable

from app.runtime.ccplus_contracts import ExtensionDescriptorV1, ExtensionRegistryV1
from app.services.command_registry import CommandDefinition
from app.skills.types import ParsedSkill


def _strings(value: Any) -> tuple[str, ...]:
    if value is None:
        return ()
    if isinstance(value, str):
        return (value,) if value.strip() else ()
    if isinstance(value, Iterable):
        return tuple(str(item).strip() for item in value if str(item).strip())
    text = str(value).strip()
    return (text,) if text else ()


def _skill_descriptor(skill: ParsedSkill) -> ExtensionDescriptorV1:
    effects: list[str] = ["progressive_disclosure"]
    if skill.metadata.hidden:
        effects.append("hidden_from_model_catalog")
    if skill.metadata.disable_model_invocation:
        effects.append("disabled_for_model_invocation")
    if not skill.metadata.user_invocable:
        effects.append("disabled_for_user_invocation")
    if skill.metadata.agent:
        effects.append(f"agent:{skill.metadata.agent}")
    effects.extend(f"hook:{hook}" for hook in skill.metadata.hooks)
    if skill.metadata.context:
        effects.append("context_frontmatter")
    if skill.metadata.when_to_use:
        effects.append("when_to_use_frontmatter")
    return ExtensionDescriptorV1(
        id=f"skill:{skill.metadata.name}",
        type="skill",
        source=skill.relative_path,
        trust_level="system" if skill.metadata.is_system else "tenant",
        owner_scope="agent",
        enabled_scope="agent",
        exposed_tools=tuple(skill.metadata.declared_tools),
        deferred_tools=tuple(skill.metadata.allowed_tools),
        hook_events=tuple(skill.metadata.hooks),
        permission_requirements=tuple(skill.metadata.allowed_tools),
        install_review={
            "user_invocable": skill.metadata.user_invocable,
            "model_invocable": not skill.metadata.disable_model_invocation and not skill.metadata.hidden,
            "hidden": skill.metadata.hidden,
        },
        runtime_effects=tuple(effects),
        audit_refs=(f"skill://{skill.relative_path}",),
    )


def _hook_descriptor(item: dict[str, Any]) -> ExtensionDescriptorV1:
    event = str(item.get("event") or "").strip()
    consumer = str(item.get("runtime_consumer") or "").strip()
    lifecycle = str(item.get("lifecycle_state") or "unknown").strip()
    effects = tuple(effect for effect in (lifecycle, f"consumer:{consumer}" if consumer else "") if effect)
    return ExtensionDescriptorV1(
        id=f"hook:{event}",
        type="hook_pack",
        source=consumer or "runtime/hooks.py",
        trust_level="system",
        owner_scope="platform",
        enabled_scope="agent",
        hook_events=(event,) if event else (),
        runtime_effects=effects,
        audit_refs=_strings(item.get("audit_refs")),
    )


def _command_descriptor(command: CommandDefinition) -> ExtensionDescriptorV1:
    return ExtensionDescriptorV1(
        id=f"command:{command.name}",
        type="command",
        source=command.source,
        trust_level="system" if command.source == "builtin" else "tenant",
        owner_scope="platform" if command.source == "builtin" else "tenant",
        enabled_scope="session",
        permission_requirements=(command.permission_mode,),
        runtime_effects=(
            f"category:{command.category}",
            f"mode:{command.execution_mode}",
            f"bridge_safe:{str(command.bridge_safe).lower()}",
            f"remote_safe:{str(command.remote_safe).lower()}",
            f"visible_to_model:{str(command.visible_to_model).lower()}",
            f"visible_to_user:{str(command.visible_to_user).lower()}",
        ),
        audit_refs=(command.handler_ref,),
    )


def _mcp_descriptor(server: dict[str, Any]) -> ExtensionDescriptorV1:
    server_id = str(server.get("id") or server.get("name") or "").strip()
    prompts = _strings(server.get("prompts"))
    resources = _strings(server.get("resources"))
    effects = [f"mcp_prompt:{prompt}->command" for prompt in prompts]
    effects.extend(f"mcp_resource:{resource}->skill" for resource in resources if resource.startswith("skill://"))
    return ExtensionDescriptorV1(
        id=f"mcp_server:{server_id}",
        type="mcp_server",
        source=str(server.get("name") or server_id),
        trust_level=str(server.get("trust_level") or "tenant"),
        owner_scope=str(server.get("owner_scope") or "tenant"),
        enabled_scope=str(server.get("enabled_scope") or "agent"),
        exposed_tools=_strings(server.get("tools")),
        deferred_tools=resources,
        runtime_effects=tuple(effects),
        audit_refs=_strings(server.get("audit_refs")),
    )


def build_extension_registry_projection(
    *,
    skills: Iterable[ParsedSkill] = (),
    hook_catalog: Iterable[dict[str, Any]] = (),
    commands: Iterable[CommandDefinition] = (),
    mcp_servers: Iterable[dict[str, Any]] = (),
    workflow_packs: Iterable[dict[str, Any]] = (),
    tool_packs: Iterable[dict[str, Any]] = (),
    plugins: Iterable[dict[str, Any]] = (),
) -> ExtensionRegistryV1:
    extensions: list[ExtensionDescriptorV1] = []
    extensions.extend(_skill_descriptor(skill) for skill in skills)
    extensions.extend(_hook_descriptor(item) for item in hook_catalog)
    extensions.extend(_command_descriptor(command) for command in commands)
    extensions.extend(_mcp_descriptor(server) for server in mcp_servers)

    for raw_type, records in (
        ("workflow_pack", workflow_packs),
        ("tool_pack", tool_packs),
        ("plugin", plugins),
    ):
        for record in records:
            extension_id = str(record.get("id") or record.get("name") or "").strip()
            if not extension_id:
                continue
            extensions.append(
                ExtensionDescriptorV1(
                    id=f"{raw_type}:{extension_id}",
                    type=raw_type,
                    source=str(record.get("source") or record.get("name") or extension_id),
                    trust_level=str(record.get("trust_level") or "tenant"),
                    owner_scope=str(record.get("owner_scope") or "tenant"),
                    enabled_scope=str(record.get("enabled_scope") or "agent"),
                    exposed_tools=_strings(record.get("tools")),
                    deferred_tools=_strings(record.get("deferred_tools")),
                    hook_events=_strings(record.get("hook_events")),
                    permission_requirements=_strings(record.get("permission_requirements")),
                    install_review=dict(record.get("install_review") or {}),
                    runtime_effects=_strings(record.get("runtime_effects")),
                    audit_refs=_strings(record.get("audit_refs")),
                )
            )

    return ExtensionRegistryV1(extensions=tuple(sorted(extensions, key=lambda item: item.id)))
