from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable

import yaml

from app.services.external_capabilities.types import ExternalCapabilityComponent, NormalizedExternalPluginBundle

_MANIFEST_PATH = Path(".claude-plugin/plugin.json")
_IGNORED_PLUGIN_AGENT_FIELDS = ("hooks", "mcpServers", "permissionMode")
_UNSUPPORTED_MANIFEST_COMPONENTS = ("lspServers", "outputStyles")


def load_cc_plugin_bundle(plugin_root: Path, *, source_uri: str) -> NormalizedExternalPluginBundle:
    """Normalize a Claude Code plugin directory into Hive extension components.

    The adapter mirrors CC's component discovery and namespacing rules, while
    intentionally refusing to let per-agent frontmatter mutate runtime authority.
    """
    root = plugin_root.resolve()
    manifest_path = root / _MANIFEST_PATH
    manifest = _read_json_object(manifest_path) if manifest_path.exists() else {}
    plugin_name = _string(manifest.get("name")) or root.name
    bundle = NormalizedExternalPluginBundle(
        source_format="cc_plugin",
        source_uri=source_uri,
        plugin_name=plugin_name,
        version=_string(manifest.get("version")) or None,
        description=_string(manifest.get("description")) or None,
        manifest_sha256=_sha256_file(manifest_path) if manifest_path.exists() else None,
    )

    bundle.credential_requirements.extend(_credential_requirements(manifest))
    bundle.unsupported_components.extend(_unsupported_components(manifest))

    bundle.components.extend(_load_standard_commands(root, plugin_name, bundle.admission_notes))
    bundle.components.extend(_load_manifest_commands(root, plugin_name, manifest, bundle.admission_notes))
    bundle.components.extend(_load_standard_skills(root, plugin_name, bundle.admission_notes))
    bundle.components.extend(_load_standard_agents(root, plugin_name, bundle.admission_notes))
    bundle.components.extend(_load_standard_hooks(root, plugin_name, bundle.admission_notes))
    bundle.components.extend(_load_mcp_servers(root, plugin_name, manifest, bundle.admission_notes))
    return bundle


def _load_standard_commands(
    root: Path,
    plugin_name: str,
    admission_notes: list[dict[str, Any]],
) -> list[ExternalCapabilityComponent]:
    commands_root = root / "commands"
    if not commands_root.is_dir():
        return []
    return [
        _markdown_component(
            root=root,
            base_dir=commands_root,
            file_path=file_path,
            plugin_name=plugin_name,
            component_type="slash_command",
            admission_notes=admission_notes,
        )
        for file_path in sorted(commands_root.rglob("*.md"))
        if _is_safe_component_file(root, file_path, "commands", admission_notes)
    ]


def _load_manifest_commands(
    root: Path,
    plugin_name: str,
    manifest: dict[str, Any],
    admission_notes: list[dict[str, Any]],
) -> list[ExternalCapabilityComponent]:
    commands = manifest.get("commands")
    if commands is None or (root / "commands").is_dir():
        return []
    components: list[ExternalCapabilityComponent] = []
    for item in _manifest_path_values(commands):
        component_path = _safe_join(root, item)
        if component_path is None:
            admission_notes.append({"code": "component_path_escape", "component_type": "commands", "path": item})
            continue
        if component_path.is_file() and component_path.suffix.lower() == ".md":
            components.append(
                _markdown_component(
                    root=root,
                    base_dir=component_path.parent,
                    file_path=component_path,
                    plugin_name=plugin_name,
                    component_type="slash_command",
                    admission_notes=admission_notes,
                )
            )
    return components


def _load_standard_skills(
    root: Path,
    plugin_name: str,
    admission_notes: list[dict[str, Any]],
) -> list[ExternalCapabilityComponent]:
    skills_root = root / "skills"
    if not skills_root.is_dir():
        return []
    components: list[ExternalCapabilityComponent] = []
    for skill_file in sorted(skills_root.rglob("SKILL.md")):
        if not _is_safe_component_file(root, skill_file, "skills", admission_notes):
            continue
        relative_dir = skill_file.parent.relative_to(skills_root)
        local_name = ":".join(relative_dir.parts) if relative_dir.parts else skill_file.parent.name
        frontmatter, body = _split_frontmatter(skill_file.read_text(encoding="utf-8", errors="replace"))
        declared_name = _string(frontmatter.get("name"))
        local_name = declared_name or local_name
        qualified_name = f"{plugin_name}:{local_name}"
        components.append(
            ExternalCapabilityComponent(
                component_type="skill",
                local_name=local_name,
                qualified_name=qualified_name,
                source_path=_relative_path(root, skill_file),
                content_sha256=_sha256_file(skill_file),
                runtime_projection={
                    "description": _string(frontmatter.get("description")) or _first_body_line(body),
                },
            )
        )
    return components


def _load_standard_agents(
    root: Path,
    plugin_name: str,
    admission_notes: list[dict[str, Any]],
) -> list[ExternalCapabilityComponent]:
    agents_root = root / "agents"
    if not agents_root.is_dir():
        return []
    components: list[ExternalCapabilityComponent] = []
    for agent_file in sorted(agents_root.rglob("*.md")):
        if not _is_safe_component_file(root, agent_file, "agents", admission_notes):
            continue
        frontmatter, body = _split_frontmatter(agent_file.read_text(encoding="utf-8", errors="replace"))
        local_name = _string(frontmatter.get("name")) or agent_file.stem
        namespace = agent_file.parent.relative_to(agents_root)
        if namespace.parts:
            local_name = ":".join((*namespace.parts, local_name))
        qualified_name = f"{plugin_name}:{local_name}"
        ignored_fields = tuple(field for field in _IGNORED_PLUGIN_AGENT_FIELDS if field in frontmatter)
        for field in ignored_fields:
            admission_notes.append(
                {
                    "code": "ignored_plugin_agent_escalation_field",
                    "component": qualified_name,
                    "field": field,
                }
            )
        components.append(
            ExternalCapabilityComponent(
                component_type="subagent",
                local_name=local_name,
                qualified_name=qualified_name,
                source_path=_relative_path(root, agent_file),
                content_sha256=_sha256_file(agent_file),
                runtime_projection=_agent_runtime_projection(frontmatter, body),
                ignored_fields=ignored_fields,
            )
        )
    return components


def _load_standard_hooks(
    root: Path,
    plugin_name: str,
    admission_notes: list[dict[str, Any]],
) -> list[ExternalCapabilityComponent]:
    hooks_file = root / "hooks" / "hooks.json"
    if not hooks_file.exists():
        return []
    if not _is_safe_component_file(root, hooks_file, "hooks", admission_notes):
        return []
    payload = _read_json_object(hooks_file)
    hooks = payload.get("hooks")
    if not isinstance(hooks, dict):
        return []
    components: list[ExternalCapabilityComponent] = []
    for event_name, event_specs in hooks.items():
        if not isinstance(event_specs, list):
            continue
        for index, spec in enumerate(event_specs):
            matcher = _string(spec.get("matcher")) if isinstance(spec, dict) else ""
            local_name = f"hook:{event_name}:{matcher or '*'}:{index}"
            components.append(
                ExternalCapabilityComponent(
                    component_type="hook",
                    local_name=local_name,
                    qualified_name=f"{plugin_name}:{local_name}",
                    source_path=_relative_path(root, hooks_file),
                    content_sha256=_sha256_file(hooks_file),
                    runtime_projection={
                        "event": event_name,
                        "matcher": matcher,
                        "spec": spec,
                    },
                )
            )
    return components


def _load_mcp_servers(
    root: Path,
    plugin_name: str,
    manifest: dict[str, Any],
    admission_notes: list[dict[str, Any]],
) -> list[ExternalCapabilityComponent]:
    components: list[ExternalCapabilityComponent] = []
    mcp_file = root / ".mcp.json"
    if mcp_file.exists() and _is_safe_component_file(root, mcp_file, "mcpServers", admission_notes):
        payload = _read_json_object(mcp_file)
        components.extend(_mcp_components(plugin_name, root, mcp_file, payload.get("mcpServers")))
    components.extend(_mcp_components(plugin_name, root, root / _MANIFEST_PATH, manifest.get("mcpServers")))
    return components


def _mcp_components(
    plugin_name: str,
    root: Path,
    source_file: Path,
    servers: Any,
) -> list[ExternalCapabilityComponent]:
    if not isinstance(servers, dict):
        return []
    return [
        ExternalCapabilityComponent(
            component_type="mcp_server",
            local_name=server_name,
            qualified_name=f"{plugin_name}:mcp:{server_name}",
            source_path=_relative_path(root, source_file),
            content_sha256=_sha256_file(source_file) if source_file.exists() else "",
            runtime_projection={"server_name": server_name, "config": config},
        )
        for server_name, config in servers.items()
    ]


def _markdown_component(
    *,
    root: Path,
    base_dir: Path,
    file_path: Path,
    plugin_name: str,
    component_type: str,
    admission_notes: list[dict[str, Any]],
) -> ExternalCapabilityComponent:
    del admission_notes
    frontmatter, body = _split_frontmatter(file_path.read_text(encoding="utf-8", errors="replace"))
    local_name = _component_local_name(base_dir, file_path)
    qualified_name = f"{plugin_name}:{local_name}"
    runtime_projection = {
        "description": _string(frontmatter.get("description")) or _first_body_line(body),
        "allowed_tools": _string_list(frontmatter.get("allowed-tools") or frontmatter.get("allowedTools")),
    }
    return ExternalCapabilityComponent(
        component_type=component_type,  # type: ignore[arg-type]
        local_name=local_name,
        qualified_name=qualified_name,
        source_path=_relative_path(root, file_path),
        content_sha256=_sha256_file(file_path),
        runtime_projection=runtime_projection,
    )


def _agent_runtime_projection(frontmatter: dict[str, Any], body: str) -> dict[str, Any]:
    projection: dict[str, Any] = {
        "description": _string(frontmatter.get("description"))
        or _string(frontmatter.get("when-to-use"))
        or _first_body_line(body),
    }
    tools = _string_list(frontmatter.get("tools"))
    if tools:
        projection["tools"] = tools
    skills = _string_list(frontmatter.get("skills"))
    if skills:
        projection["skills"] = skills
    model = _string(frontmatter.get("model"))
    if model:
        projection["model"] = model
    return projection


def _component_local_name(base_dir: Path, file_path: Path) -> str:
    relative = file_path.relative_to(base_dir)
    parts = list(relative.with_suffix("").parts)
    if relative.name.lower() == "skill.md" and len(parts) >= 2:
        parts = parts[:-1]
    return ":".join(parts)


def _manifest_path_values(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        return (value,)
    if isinstance(value, list):
        return tuple(item for item in value if isinstance(item, str))
    if isinstance(value, dict):
        paths: list[str] = []
        for metadata in value.values():
            if isinstance(metadata, dict) and isinstance(metadata.get("source"), str):
                paths.append(metadata["source"])
        return tuple(paths)
    return ()


def _split_frontmatter(content: str) -> tuple[dict[str, Any], str]:
    stripped = content.strip()
    if not stripped.startswith("---\n"):
        return {}, stripped
    remainder = stripped[4:]
    if "\n---\n" not in remainder:
        return {}, stripped
    frontmatter_text, body = remainder.split("\n---\n", 1)
    loaded = yaml.safe_load(frontmatter_text) or {}
    return (loaded if isinstance(loaded, dict) else {}), body.strip()


def _credential_requirements(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    user_config = manifest.get("userConfig")
    if not isinstance(user_config, dict):
        return []
    requirements: list[dict[str, Any]] = []
    for key, spec in user_config.items():
        spec = spec if isinstance(spec, dict) else {}
        requirements.append(
            {
                "key": str(key),
                "sensitive": bool(spec.get("sensitive")),
                "source": "manifest.userConfig",
            }
        )
    return requirements


def _unsupported_components(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        {"component_type": component_type, "reason": "not_supported_by_hive_runtime_yet"}
        for component_type in _UNSUPPORTED_MANIFEST_COMPONENTS
        if component_type in manifest
    ]


def _safe_join(root: Path, relative_path: str) -> Path | None:
    candidate = (root / relative_path).resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        return None
    return candidate


def _is_safe_component_file(
    root: Path,
    file_path: Path,
    component_type: str,
    admission_notes: list[dict[str, Any]],
) -> bool:
    resolved = file_path.resolve()
    try:
        resolved.relative_to(root)
    except ValueError:
        admission_notes.append(
            {
                "code": "component_path_escape",
                "component_type": component_type,
                "path": str(file_path),
            }
        )
        return False
    return resolved.is_file()


def _relative_path(root: Path, file_path: Path) -> str:
    try:
        return str(file_path.resolve().relative_to(root))
    except ValueError:
        return str(file_path)


def _read_json_object(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _string(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _string_list(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        separators = "," if "," in value else None
        items = value.split(separators) if separators else value.split()
        return [item.strip() for item in items if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [_string(item) for item in value if _string(item)]
    return [_string(value)] if _string(value) else []


def _first_body_line(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:1024]
    return ""
