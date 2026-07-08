from __future__ import annotations

from pathlib import Path
import shutil
from typing import Any


def deactivate_activation_components(components: Any, *, workspace: Path) -> list[dict[str, Any]]:
    if not isinstance(components, list):
        return []
    deactivated: list[dict[str, Any]] = []
    for component in components:
        if not isinstance(component, dict):
            continue
        component_type = str(component.get("component_type") or "unknown")
        name = _optional_string(component.get("name")) or _optional_string(component.get("qualified_name")) or "unknown"
        if component_type == "skill":
            status = _remove_activation_path(workspace / "skills" / name)
        elif component_type == "subagent":
            status = _remove_activation_path(workspace / "subagents" / f"{name}.md")
        elif component_type == "mcp_server":
            status = "manual_revoke_required"
        else:
            status = "unsupported_deactivation_component"
        deactivated.append({"component_type": component_type, "name": name, "status": status})
    return deactivated


def _remove_activation_path(path: Path) -> str:
    if not path.exists():
        return "already_absent"
    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink()
    return "removed"


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
