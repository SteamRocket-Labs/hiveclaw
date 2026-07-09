from __future__ import annotations

from collections.abc import Iterable, Iterator
import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from app.services.external_capabilities.types import ExternalCapabilityComponent, NormalizedExternalPluginBundle

_MANIFEST_PATH = Path(".claude-plugin/plugin.json")
_IGNORED_PLUGIN_AGENT_FIELDS = ("hooks", "mcpServers", "permissionMode")
_UNSUPPORTED_MANIFEST_COMPONENTS = ("lspServers", "outputStyles")
# CC plugin.json metadata fields (schemas.ts:274-320) captured verbatim for the
# governance surface. `name`/`version`/`description` are already promoted to
# first-class bundle fields, so only the remaining descriptive metadata lands
# in bundle.manifest_metadata.
_MANIFEST_METADATA_FIELDS = ("author", "homepage", "license", "keywords", "repository")


def load_cc_plugin_bundle(plugin_root: Path, *, source_uri: str) -> NormalizedExternalPluginBundle:
    """Normalize a Claude Code plugin directory into Hive extension components.

    The adapter mirrors CC's component discovery and namespacing rules
    (FreeCode utils/plugins/schemas.ts + loadPlugin*.ts), while intentionally
    refusing to let per-agent frontmatter mutate runtime authority. Manifest
    component declarations SUPPLEMENT the standard directories ("in addition to"
    semantics, schemas.ts:429-499) rather than replacing them.
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

    bundle.manifest_metadata.update(_manifest_metadata(manifest))
    bundle.credential_requirements.extend(_credential_requirements(manifest))
    bundle.unsupported_components.extend(_unsupported_components(manifest))

    # Standard directory components first; manifest-declared components merge in
    # (deduplicated by qualified name so a manifest entry never shadows a
    # directory component of the same name).
    bundle.components.extend(_load_standard_commands(root, plugin_name, bundle.admission_notes))
    bundle.components.extend(_load_standard_skills(root, plugin_name, bundle.admission_notes))
    bundle.components.extend(_load_standard_agents(root, plugin_name, bundle.admission_notes))
    bundle.components.extend(_load_standard_hooks(root, plugin_name, bundle.admission_notes))
    _merge_components(bundle.components, _load_manifest_commands(root, plugin_name, manifest, bundle.admission_notes))
    _merge_components(bundle.components, _load_manifest_agents(root, plugin_name, manifest, bundle.admission_notes))
    _merge_components(bundle.components, _load_manifest_skills(root, plugin_name, manifest, bundle.admission_notes))
    _merge_components(bundle.components, _load_manifest_hooks(root, plugin_name, manifest, bundle.admission_notes))
    bundle.components.extend(_load_mcp_servers(root, plugin_name, manifest, bundle.admission_notes))
    return bundle


def _merge_components(
    existing: list[ExternalCapabilityComponent],
    additions: Iterable[ExternalCapabilityComponent],
) -> None:
    seen = {component.qualified_name for component in existing}
    for component in additions:
        if component.qualified_name in seen:
            continue
        existing.append(component)
        seen.add(component.qualified_name)


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
    """Load `commands` declared in plugin.json (schemas.ts:429-452).

    Three forms: a single relative path, an array of paths, or an object mapping
    of command name -> metadata carrying either an inline `content` or a `source`
    path (CommandMetadataSchema, schemas.ts:385-416). These supplement the
    commands/ directory rather than replacing it.
    """
    commands = manifest.get("commands")
    if commands is None:
        return []
    components: list[ExternalCapabilityComponent] = []
    for spec in _iter_manifest_command_specs(commands):
        component = _manifest_command_component(root, plugin_name, spec, admission_notes)
        if component is not None:
            components.append(component)
    return components


def _iter_manifest_command_specs(commands: Any) -> Iterator[dict[str, Any]]:
    if isinstance(commands, str):
        yield {"name": None, "source": commands, "content": None, "meta": {}}
    elif isinstance(commands, list):
        for item in commands:
            if isinstance(item, str):
                yield {"name": None, "source": item, "content": None, "meta": {}}
    elif isinstance(commands, dict):
        for name, meta in commands.items():
            meta = meta if isinstance(meta, dict) else {}
            yield {
                "name": str(name),
                "source": _optional_string(meta.get("source")),
                "content": meta.get("content"),
                "meta": meta,
            }


def _manifest_command_component(
    root: Path,
    plugin_name: str,
    spec: dict[str, Any],
    admission_notes: list[dict[str, Any]],
) -> ExternalCapabilityComponent | None:
    inline_content = spec.get("content")
    meta = spec.get("meta") or {}
    if inline_content is not None:
        content = str(inline_content)
        local_name = spec.get("name") or "command"
        frontmatter, body = _split_frontmatter(content)
        return ExternalCapabilityComponent(
            component_type="slash_command",
            local_name=local_name,
            qualified_name=f"{plugin_name}:{local_name}",
            source_path="",
            content_sha256=hashlib.sha256(content.encode("utf-8")).hexdigest(),
            runtime_projection=_command_projection(meta, frontmatter, body),
            metadata={"content": content},
        )
    source = spec.get("source")
    if not source:
        return None
    component_path = _safe_join(root, source)
    if component_path is None:
        admission_notes.append({"code": "component_path_escape", "component_type": "commands", "path": source})
        return None
    if not (component_path.is_file() and component_path.suffix.lower() == ".md"):
        return None
    content = component_path.read_text(encoding="utf-8", errors="replace")
    frontmatter, body = _split_frontmatter(content)
    local_name = spec.get("name") or component_path.stem
    return ExternalCapabilityComponent(
        component_type="slash_command",
        local_name=local_name,
        qualified_name=f"{plugin_name}:{local_name}",
        source_path=_relative_path(root, component_path),
        content_sha256=_sha256_file(component_path),
        runtime_projection=_command_projection(meta, frontmatter, body),
        metadata={"content": content},
    )


def _command_projection(meta: dict[str, Any], frontmatter: dict[str, Any], body: str) -> dict[str, Any]:
    projection: dict[str, Any] = {
        "description": _string(meta.get("description"))
        or _string(frontmatter.get("description"))
        or _first_body_line(body),
        "allowed_tools": _string_list(meta.get("allowedTools"))
        or _string_list(frontmatter.get("allowed-tools") or frontmatter.get("allowedTools")),
    }
    argument_hint = _string(meta.get("argumentHint")) or _string(frontmatter.get("argument-hint"))
    if argument_hint:
        projection["argument_hint"] = argument_hint
    model = _string(meta.get("model")) or _string(frontmatter.get("model"))
    if model:
        projection["model"] = model
    return projection


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
        components.append(_skill_component(root, plugin_name, skill_file, skills_root=skills_root))
    return components


def _load_manifest_skills(
    root: Path,
    plugin_name: str,
    manifest: dict[str, Any],
    admission_notes: list[dict[str, Any]],
) -> list[ExternalCapabilityComponent]:
    """Load `skills` declared in plugin.json (schemas.ts:484-499) — a relative
    skill directory or an array of them, in addition to the skills/ directory."""
    components: list[ExternalCapabilityComponent] = []
    for path in _manifest_path_list(manifest.get("skills")):
        skill_dir = _safe_join(root, path)
        if skill_dir is None:
            admission_notes.append({"code": "component_path_escape", "component_type": "skills", "path": path})
            continue
        skill_file = skill_dir / "SKILL.md"
        if not skill_file.is_file() or not _is_safe_component_file(root, skill_file, "skills", admission_notes):
            continue
        components.append(_skill_component(root, plugin_name, skill_file))
    return components


def _skill_component(
    root: Path,
    plugin_name: str,
    skill_file: Path,
    *,
    skills_root: Path | None = None,
) -> ExternalCapabilityComponent:
    frontmatter, body = _split_frontmatter(skill_file.read_text(encoding="utf-8", errors="replace"))
    if skills_root is not None:
        relative_dir = skill_file.parent.relative_to(skills_root)
        base_name = ":".join(relative_dir.parts) if relative_dir.parts else skill_file.parent.name
    else:
        base_name = skill_file.parent.name
    local_name = _string(frontmatter.get("name")) or base_name
    return ExternalCapabilityComponent(
        component_type="skill",
        local_name=local_name,
        qualified_name=f"{plugin_name}:{local_name}",
        source_path=_relative_path(root, skill_file),
        content_sha256=_sha256_file(skill_file),
        runtime_projection={
            "description": _string(frontmatter.get("description")) or _first_body_line(body),
            "folder_name": local_name.replace(":", "-"),
        },
        metadata={"files": _text_files_for_directory(root=skill_file.parent, relative_root=skill_file.parent)},
    )


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
        namespace_parts = agent_file.parent.relative_to(agents_root).parts
        components.append(
            _agent_component(root, plugin_name, agent_file, admission_notes, namespace_parts=namespace_parts)
        )
    return components


def _load_manifest_agents(
    root: Path,
    plugin_name: str,
    manifest: dict[str, Any],
    admission_notes: list[dict[str, Any]],
) -> list[ExternalCapabilityComponent]:
    """Load `agents` declared in plugin.json (schemas.ts:460-476) — a relative
    markdown path or an array of them, in addition to the agents/ directory."""
    components: list[ExternalCapabilityComponent] = []
    for path in _manifest_path_list(manifest.get("agents")):
        agent_file = _safe_join(root, path)
        if agent_file is None:
            admission_notes.append({"code": "component_path_escape", "component_type": "agents", "path": path})
            continue
        if not (agent_file.is_file() and agent_file.suffix.lower() == ".md"):
            continue
        components.append(_agent_component(root, plugin_name, agent_file, admission_notes))
    return components


def _agent_component(
    root: Path,
    plugin_name: str,
    agent_file: Path,
    admission_notes: list[dict[str, Any]],
    *,
    namespace_parts: tuple[str, ...] = (),
) -> ExternalCapabilityComponent:
    definition = agent_file.read_text(encoding="utf-8", errors="replace")
    frontmatter, body = _split_frontmatter(definition)
    base_name = _string(frontmatter.get("name")) or agent_file.stem
    local_name = ":".join((*namespace_parts, base_name)) if namespace_parts else base_name
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
    return ExternalCapabilityComponent(
        component_type="subagent",
        local_name=local_name,
        qualified_name=qualified_name,
        source_path=_relative_path(root, agent_file),
        content_sha256=_sha256_file(agent_file),
        runtime_projection=_agent_runtime_projection(frontmatter, body),
        metadata={"definition": definition},
        ignored_fields=ignored_fields,
    )


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
    return _hook_components_from_map(plugin_name, root, hooks_file, hooks)


def _load_manifest_hooks(
    root: Path,
    plugin_name: str,
    manifest: dict[str, Any],
    admission_notes: list[dict[str, Any]],
) -> list[ExternalCapabilityComponent]:
    """Load `hooks` declared in plugin.json (schemas.ts:348-373).

    Three forms: a relative path to a hooks JSON file, an inline hooks map
    (event -> specs), or an array mixing the two. These supplement
    hooks/hooks.json.
    """
    components: list[ExternalCapabilityComponent] = []
    for hooks_map, source_file in _iter_manifest_hook_maps(root, manifest.get("hooks"), admission_notes):
        components.extend(_hook_components_from_map(plugin_name, root, source_file, hooks_map))
    return components


def _iter_manifest_hook_maps(
    root: Path,
    value: Any,
    admission_notes: list[dict[str, Any]],
) -> Iterator[tuple[dict[str, Any], Path]]:
    if value is None:
        return
    items = value if isinstance(value, list) else [value]
    for item in items:
        if isinstance(item, str):
            yield from _hook_map_from_path(root, item, admission_notes)
        elif isinstance(item, dict):
            # Inline HooksSchema is an event -> specs map (no `hooks` wrapper).
            yield (item, root / _MANIFEST_PATH)


def _hook_map_from_path(
    root: Path,
    path: str,
    admission_notes: list[dict[str, Any]],
) -> Iterator[tuple[dict[str, Any], Path]]:
    hooks_path = _safe_join(root, path)
    if hooks_path is None:
        admission_notes.append({"code": "component_path_escape", "component_type": "hooks", "path": path})
        return
    if not hooks_path.is_file():
        return
    payload = _read_json_object(hooks_path)
    hooks = payload.get("hooks") if isinstance(payload.get("hooks"), dict) else payload
    if isinstance(hooks, dict):
        yield (hooks, hooks_path)


def _hook_components_from_map(
    plugin_name: str,
    root: Path,
    source_file: Path,
    hooks: dict[str, Any],
) -> list[ExternalCapabilityComponent]:
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
                    source_path=_relative_path(root, source_file) if source_file.exists() else str(source_file),
                    content_sha256=_sha256_file(source_file) if source_file.exists() else "",
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
    """Load MCP servers from .mcp.json plus the manifest `mcpServers` field.

    The manifest field (schemas.ts:543-572) is a union of an inline dict, a
    relative path to an MCP JSON file, an MCPB/.dxt bundle path (unsupported —
    recorded as an admission note), or an array mixing these. Server names
    already provided by .mcp.json win over manifest duplicates.
    """
    components: list[ExternalCapabilityComponent] = []
    seen: set[str] = set()
    mcp_file = root / ".mcp.json"
    if mcp_file.exists() and _is_safe_component_file(root, mcp_file, "mcpServers", admission_notes):
        payload = _read_json_object(mcp_file)
        for component in _mcp_components(plugin_name, root, mcp_file, payload.get("mcpServers")):
            components.append(component)
            seen.add(component.local_name)
    for servers, source_file in _iter_manifest_mcp_maps(root, manifest.get("mcpServers"), admission_notes):
        for component in _mcp_components(plugin_name, root, source_file, servers):
            if component.local_name in seen:
                continue
            components.append(component)
            seen.add(component.local_name)
    return components


def _iter_manifest_mcp_maps(
    root: Path,
    value: Any,
    admission_notes: list[dict[str, Any]],
) -> Iterator[tuple[dict[str, Any], Path]]:
    if value is None:
        return
    items = value if isinstance(value, list) else [value]
    for item in items:
        if isinstance(item, str):
            if item.endswith((".mcpb", ".dxt")) or item.startswith(("http://", "https://")):
                admission_notes.append(
                    {"code": "unsupported_mcpb_bundle", "component_type": "mcpServers", "path": item}
                )
                continue
            mcp_path = _safe_join(root, item)
            if mcp_path is None:
                admission_notes.append({"code": "component_path_escape", "component_type": "mcpServers", "path": item})
                continue
            if not mcp_path.is_file():
                continue
            payload = _read_json_object(mcp_path)
            servers = payload.get("mcpServers") if isinstance(payload.get("mcpServers"), dict) else payload
            if isinstance(servers, dict):
                yield (servers, mcp_path)
        elif isinstance(item, dict):
            yield (item, root / _MANIFEST_PATH)


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
) -> ExternalCapabilityComponent:
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
        metadata={"content": file_path.read_text(encoding="utf-8", errors="replace")},
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


def _manifest_path_list(value: Any) -> list[str]:
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        return [item for item in value if isinstance(item, str)]
    return []


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


def _manifest_metadata(manifest: dict[str, Any]) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    for key in _MANIFEST_METADATA_FIELDS:
        value = manifest.get(key)
        if value is not None:
            metadata[key] = value
    dependencies = _normalize_dependencies(manifest.get("dependencies"))
    if dependencies:
        metadata["dependencies"] = dependencies
    return metadata


def _normalize_dependencies(value: Any) -> list[str]:
    """Normalize plugin.json `dependencies` to plain "name" / "name@marketplace"
    strings (schemas.ts:1367-1391): trailing @^version is dropped, object refs
    collapse to name[@marketplace]."""
    if not isinstance(value, list):
        return []
    normalized: list[str] = []
    for item in value:
        if isinstance(item, str):
            text = item.strip()
            marker = text.find("@^")
            normalized.append(text[:marker] if marker != -1 else text)
        elif isinstance(item, dict):
            name = _string(item.get("name"))
            if not name:
                continue
            marketplace = _string(item.get("marketplace"))
            normalized.append(f"{name}@{marketplace}" if marketplace else name)
    return normalized


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


def _text_files_for_directory(*, root: Path, relative_root: Path) -> list[dict[str, str]]:
    base = root.resolve()
    relative_base = relative_root.resolve()
    files: list[dict[str, str]] = []
    for file_path in sorted(root.rglob("*")):
        if not file_path.is_file():
            continue
        resolved = file_path.resolve()
        if not resolved.is_relative_to(base):
            continue
        files.append(
            {
                "path": str(resolved.relative_to(relative_base)),
                "content": file_path.read_text(encoding="utf-8", errors="replace"),
            }
        )
    return files


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


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _first_body_line(body: str) -> str:
    for line in body.splitlines():
        stripped = line.strip()
        if stripped and not stripped.startswith("#"):
            return stripped[:1024]
    return ""
