"""Materialize approved plugin file bodies into an agent workspace and resolve
plugin variables.

This is the C3 layer of the CC plugin-marketplace alignment. It complements —
does not replace — ``activation.py``'s skill/subagent projection: activation
projects individual components, while this module (1) lands the plugin's file
body under a stable, version-scoped ``workspace/plugins/<name>`` directory (the
``${CLAUDE_PLUGIN_ROOT}``) and (2) substitutes ``${CLAUDE_PLUGIN_ROOT}`` /
``${CLAUDE_PLUGIN_DATA}`` / ``${user_config.KEY}`` in hook commands, MCP server
config, and command/agent/skill content.

FreeCode baseline — utils/plugins/pluginOptionsStorage.ts:326-400:
  * ``substitutePluginVariables`` replaces ``${CLAUDE_PLUGIN_ROOT}`` (version
    install dir) and ``${CLAUDE_PLUGIN_DATA}`` (persistent state dir) using the
    function form of ``.replace`` so ``$``-sequences in paths aren't treated as
    backreferences.
  * ``substituteUserConfigVariables`` throws on a missing key (used for MCP/LSP
    config and hook commands, where a miss is an authoring bug).
  * ``substituteUserConfigInContent`` masks sensitive keys and leaves unknown
    keys literal (used for skill/agent prose that reaches the model prompt).

Consumers: ``activation.py`` calls ``materialize_plugin_root`` once per snapshot
activation and ``resolve_component_variables`` per component before projecting
hooks/MCP/command/agent/skill into the workspace.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import copy
from pathlib import Path, PurePosixPath
import re
from typing import Any

_PLUGIN_ROOT_RE = re.compile(r"\$\{CLAUDE_PLUGIN_ROOT\}")
_PLUGIN_DATA_RE = re.compile(r"\$\{CLAUDE_PLUGIN_DATA\}")
_USER_CONFIG_RE = re.compile(r"\$\{user_config\.([^}]+)\}")
_SAFE_NAME_RE = re.compile(r"[^A-Za-z0-9_.-]+")
_MAX_PLUGIN_FILES = 200
_MAX_PLUGIN_BYTES = 2_000_000


def substitute_plugin_variables(value: str, *, plugin_root: str, plugin_data: str | None = None) -> str:
    """Replace ``${CLAUDE_PLUGIN_ROOT}`` / ``${CLAUDE_PLUGIN_DATA}`` literals.

    Uses function-form replacement so ``$``-sequences in the substituted path
    are never interpreted as regex backreferences. ``${CLAUDE_PLUGIN_DATA}`` is
    left literal when ``plugin_data`` is not supplied.
    """

    out = _PLUGIN_ROOT_RE.sub(lambda _match: plugin_root, value)
    if plugin_data is not None:
        out = _PLUGIN_DATA_RE.sub(lambda _match: plugin_data, out)
    return out


def substitute_user_config_variables(value: str, user_config: Mapping[str, Any]) -> str:
    """Replace ``${user_config.KEY}`` with saved option values (config form).

    Raises ``ValueError`` on a missing key, mirroring CC's throwing behavior for
    MCP/LSP config and hook commands (a miss means a plugin references a key it
    never declared).
    """

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        if key not in user_config or user_config[key] is None:
            raise ValueError(f"Missing required user configuration value: {key}")
        return str(user_config[key])

    return _USER_CONFIG_RE.sub(_replace, value)


def substitute_user_config_in_content(
    content: str,
    options: Mapping[str, Any],
    schema: Mapping[str, Any],
) -> str:
    """Content-safe ``${user_config.KEY}`` substitution for prose reaching the
    model prompt: sensitive keys mask to a placeholder, unknown keys stay
    literal (FreeCode pluginOptionsStorage.ts:385-400)."""

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        field = schema.get(key)
        if isinstance(field, Mapping) and field.get("sensitive") is True:
            return f"[sensitive option '{key}' not available in plugin content]"
        value = options.get(key)
        if value is None:
            return match.group(0)
        return str(value)

    return _USER_CONFIG_RE.sub(_replace, content)


def plugin_root_path(workspace: Path, plugin_name: str) -> Path:
    """Resolve the stable ``${CLAUDE_PLUGIN_ROOT}`` directory for a plugin.

    The plugin name is sanitized to a single path segment so it can never escape
    ``workspace/plugins`` even when the manifest name contains separators or
    ``..`` sequences.
    """

    return (workspace / "plugins" / _safe_plugin_dir_name(plugin_name)).resolve()


def materialize_plugin_root(
    *,
    workspace: Path,
    plugin_name: str,
    files: Sequence[Mapping[str, Any]],
) -> Path:
    """Write a plugin's file body under ``workspace/plugins/<name>`` and return
    the absolute plugin root. Member paths that escape the root are skipped;
    file-count and total-byte ceilings bound the write."""

    root = plugin_root_path(workspace, plugin_name)
    root.mkdir(parents=True, exist_ok=True)
    written = 0
    total_bytes = 0
    for item in files:
        if written >= _MAX_PLUGIN_FILES:
            break
        safe = _safe_relative_path(str(item.get("path") or ""))
        if safe is None:
            continue
        content = str(item.get("content") or "")
        total_bytes += len(content.encode("utf-8"))
        if total_bytes > _MAX_PLUGIN_BYTES:
            break
        target = (root / safe).resolve()
        if not _is_within(target, root):
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        written += 1
    return root


def build_plugin_root_files(components: Sequence[Mapping[str, Any]]) -> list[dict[str, str]]:
    """Reconstruct a plugin's file tree from its snapshot components so the
    materialized ``${CLAUDE_PLUGIN_ROOT}`` directory holds the plugin's own
    bundled files (skill dirs, command/agent markdown) — the raw body that
    hook/MCP/command references point at, distinct from the active skill/subagent
    projection that ``activation.py`` still owns."""

    files: list[dict[str, str]] = []
    for component in components:
        if not isinstance(component, Mapping):
            continue
        component_type = component.get("component_type")
        source_path = _optional_str(component.get("source_path"))
        metadata_raw = component.get("metadata")
        metadata: Mapping[str, Any] = metadata_raw if isinstance(metadata_raw, Mapping) else {}
        if component_type == "skill" and isinstance(metadata.get("files"), list):
            skill_dir = _posix_parent(source_path) if source_path else f"skills/{_component_local(component)}"
            for entry in metadata["files"]:
                if isinstance(entry, Mapping) and entry.get("path"):
                    rel = f"{skill_dir}/{entry['path']}".strip("/") if skill_dir else str(entry["path"])
                    files.append({"path": rel, "content": str(entry.get("content") or "")})
        elif component_type == "slash_command" and metadata.get("content") is not None:
            path = source_path or f"commands/{_component_local(component)}.md"
            files.append({"path": path, "content": str(metadata["content"])})
        elif component_type == "subagent" and metadata.get("definition") is not None:
            path = source_path or f"agents/{_component_local(component)}.md"
            files.append({"path": path, "content": str(metadata["definition"])})
    return files


def resolve_component_variables(
    component: Mapping[str, Any],
    *,
    plugin_root: str,
    user_config: Mapping[str, Any] | None = None,
    user_config_schema: Mapping[str, Any] | None = None,
    plugin_data: str | None = None,
) -> dict[str, Any]:
    """Return a copy of ``component`` with plugin/user_config variables resolved.

    Config-bearing fields (hook spec, MCP config) use the throwing config form so
    a missing declared key surfaces; content fields (command/agent/skill body)
    use the content-safe form so secrets never reach the model prompt. The input
    component is never mutated.
    """

    user_config = user_config or {}
    user_config_schema = user_config_schema or {}
    resolved = copy.deepcopy(dict(component))
    projection = resolved.get("runtime_projection")
    projection = projection if isinstance(projection, dict) else {}
    metadata = resolved.get("metadata")
    metadata = metadata if isinstance(metadata, dict) else {}
    component_type = resolved.get("component_type")

    def config_transform(text: str) -> str:
        # Activation-lenient: unknown user_config keys stay literal so a
        # partially-configured plugin never crashes activation. The throwing,
        # CC-faithful form remains available as substitute_user_config_variables.
        text = substitute_plugin_variables(text, plugin_root=plugin_root, plugin_data=plugin_data)

        def _sub(match: re.Match[str]) -> str:
            value = user_config.get(match.group(1))
            return str(value) if value is not None else match.group(0)

        return _USER_CONFIG_RE.sub(_sub, text)

    def content_transform(text: str) -> str:
        text = substitute_plugin_variables(text, plugin_root=plugin_root, plugin_data=plugin_data)
        return substitute_user_config_in_content(text, user_config, user_config_schema)

    if component_type == "hook" and "spec" in projection:
        projection["spec"] = _walk_strings(projection["spec"], config_transform)
    elif component_type == "mcp_server" and "config" in projection:
        projection["config"] = _walk_strings(projection["config"], config_transform)
    elif component_type == "slash_command" and "content" in metadata:
        metadata["content"] = content_transform(str(metadata["content"]))
    elif component_type == "subagent" and "definition" in metadata:
        metadata["definition"] = content_transform(str(metadata["definition"]))
    elif component_type == "skill" and isinstance(metadata.get("files"), list):
        metadata["files"] = [
            {**file, "content": content_transform(str(file.get("content") or ""))} if isinstance(file, dict) else file
            for file in metadata["files"]
        ]

    if projection:
        resolved["runtime_projection"] = projection
    if metadata:
        resolved["metadata"] = metadata
    return resolved


def _walk_strings(obj: Any, transform: Any) -> Any:
    if isinstance(obj, str):
        return transform(obj)
    if isinstance(obj, list):
        return [_walk_strings(item, transform) for item in obj]
    if isinstance(obj, dict):
        return {key: _walk_strings(value, transform) for key, value in obj.items()}
    return obj


def _optional_str(value: Any) -> str:
    return str(value).strip() if value is not None else ""


def _posix_parent(path: str) -> str:
    parent = str(PurePosixPath(path).parent)
    return "" if parent in {".", "/"} else parent.strip("/")


def _component_local(component: Mapping[str, Any]) -> str:
    local = _optional_str(component.get("local_name"))
    if local:
        return local.replace(":", "-")
    qualified = _optional_str(component.get("qualified_name"))
    return qualified.split(":")[-1] if qualified else "component"


def _safe_plugin_dir_name(plugin_name: str) -> str:
    cleaned = _SAFE_NAME_RE.sub("-", str(plugin_name).strip())
    while ".." in cleaned:
        cleaned = cleaned.replace("..", "-")
    cleaned = cleaned.strip("-.")
    return cleaned or "plugin"


def _safe_relative_path(raw_path: str) -> str | None:
    if not raw_path:
        return None
    path = PurePosixPath(raw_path.replace("\\", "/"))
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _is_within(target: Path, root: Path) -> bool:
    try:
        target.relative_to(root)
    except ValueError:
        return False
    return True
