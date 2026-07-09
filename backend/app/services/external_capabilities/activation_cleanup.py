from __future__ import annotations

from pathlib import Path
import re
import shutil
from typing import Any

# Activation component names live in a single safe path segment. Mirror the
# activation-side character set (`_SAFE_NAME_RE`) so a legitimate skill/subagent
# name always passes while `../`, absolute paths, and separators are rejected.
_SAFE_COMPONENT_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+$")


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
            status = _remove_component_within(workspace / "skills", name)
        elif component_type == "subagent":
            status = _remove_component_within(workspace / "subagents", name, suffix=".md")
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


def _remove_component_within(base: Path, name: str, *, suffix: str = "") -> str:
    """Remove an activation artifact by name, refusing any path escape.

    The name must be a single safe segment; the fully-resolved target must sit
    directly under ``base`` (this also defeats symlink redirection), otherwise
    nothing is deleted and an ``invalid_*`` status is returned.
    """

    if name in {".", ".."} or not _SAFE_COMPONENT_NAME_RE.fullmatch(name):
        return "invalid_component_name"
    base_resolved = base.resolve()
    target = (base_resolved / f"{name}{suffix}").resolve()
    if target.parent != base_resolved:
        return "invalid_component_path"
    return _remove_activation_path(target)


def _optional_string(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
