from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import yaml

from app.services.external_capabilities.types import ExternalCapabilityComponent, NormalizedExternalPluginBundle

_MANIFEST_PATH = Path(".codex-plugin/plugin.json")


def load_codex_plugin_bundle(plugin_root: Path, *, source_uri: str) -> NormalizedExternalPluginBundle:
    """Normalize a Codex plugin directory into Hive external capability components.

    Codex plugins are treated as a source format, not a second runtime. Skills
    become Hive skill components; app connector declarations are admitted as
    unsupported until a Hive connector binding exists.
    """
    root = plugin_root.resolve()
    manifest_path = root / _MANIFEST_PATH
    manifest = _read_json_object(manifest_path) if manifest_path.exists() else {}
    plugin_name = _string(manifest.get("name")) or root.name
    bundle = NormalizedExternalPluginBundle(
        source_format="codex_plugin",
        source_uri=source_uri,
        plugin_name=plugin_name,
        version=_string(manifest.get("version")) or None,
        description=_string(manifest.get("description")) or None,
        manifest_sha256=_sha256_file(manifest_path) if manifest_path.exists() else None,
    )
    bundle.components.extend(_load_skills(root=root, plugin_name=plugin_name, manifest=manifest))
    bundle.unsupported_components.extend(_unsupported_apps(root=root, manifest=manifest))
    return bundle


def _load_skills(*, root: Path, plugin_name: str, manifest: dict[str, Any]) -> list[ExternalCapabilityComponent]:
    skills_value = manifest.get("skills") or "skills"
    skills_root = _safe_join(root, skills_value)
    if skills_root is None or not skills_root.is_dir():
        return []
    components: list[ExternalCapabilityComponent] = []
    for skill_file in sorted(skills_root.rglob("SKILL.md")):
        if not _is_safe_file(root, skill_file):
            continue
        frontmatter, body = _split_frontmatter(skill_file.read_text(encoding="utf-8", errors="replace"))
        relative_dir = skill_file.parent.relative_to(skills_root)
        local_name = _string(frontmatter.get("name")) or (
            ":".join(relative_dir.parts) if relative_dir.parts else skill_file.parent.name
        )
        components.append(
            ExternalCapabilityComponent(
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
        )
    return components


def _unsupported_apps(*, root: Path, manifest: dict[str, Any]) -> list[dict[str, Any]]:
    apps_value = manifest.get("apps")
    if not apps_value:
        return []
    apps_path = _safe_join(root, apps_value)
    if apps_path is None or not apps_path.is_file():
        return [
            {
                "component_type": "apps",
                "reason": "codex_app_connector_manifest_not_found",
            }
        ]
    payload = _read_json_object(apps_path)
    apps = payload.get("apps")
    app_names = sorted(str(name) for name in apps.keys()) if isinstance(apps, dict) else []
    return [
        {
            "component_type": "apps",
            "reason": "codex_app_connector_requires_hive_connector_binding",
            "apps": app_names,
        }
    ]


def _safe_join(root: Path, value: Any) -> Path | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = (root / value).resolve()
    try:
        candidate.relative_to(root.resolve())
    except ValueError:
        return None
    return candidate


def _is_safe_file(root: Path, file_path: Path) -> bool:
    resolved = file_path.resolve()
    try:
        resolved.relative_to(root.resolve())
    except ValueError:
        return False
    return resolved.is_file()


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


def _relative_path(root: Path, file_path: Path) -> str:
    try:
        return str(file_path.resolve().relative_to(root.resolve()))
    except ValueError:
        return str(file_path)


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


def _read_json_object(path: Path) -> dict[str, Any]:
    loaded = json.loads(path.read_text(encoding="utf-8"))
    return loaded if isinstance(loaded, dict) else {}


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _string(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _first_body_line(body: str) -> str:
    for line in body.splitlines():
        if line.strip():
            return line.strip()
    return ""
