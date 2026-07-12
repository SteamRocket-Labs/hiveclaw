"""Single owner for provisioning the governed Plan Mode Markdown artifact."""

from __future__ import annotations

from pathlib import Path
from uuid import UUID

from app.config import get_settings


def provision_agent_plan_file_slot(
    agent_id: UUID,
    plan_file_path: str,
    *,
    agent_data_dir: str | Path | None = None,
) -> Path:
    """Reserve the exact Plan Mode target without creating an unowned file.

    The relative path is runtime-owned metadata, but this boundary still rejects
    absolute and traversal-bearing paths so every caller gets the same fail-
    closed workspace invariant. Only the parent directory is created: the first
    governed ``write_file`` call must atomically create the file and its
    WorkspaceResourceManifest. Pre-creating an empty file would make the RLS
    authority layer correctly reject it as an existing resource with no owner.
    """

    relative_path = Path(plan_file_path)
    if not plan_file_path.strip() or relative_path.is_absolute() or any(part == ".." for part in relative_path.parts):
        raise ValueError("Plan Mode file path must be relative to the agent workspace")

    data_root = Path(agent_data_dir) if agent_data_dir is not None else Path(get_settings().AGENT_DATA_DIR)
    workspace_root = (data_root / str(agent_id)).resolve()
    absolute_path = (workspace_root / relative_path).resolve()
    try:
        absolute_path.relative_to(workspace_root)
    except ValueError as exc:
        raise ValueError("Plan Mode file path must be relative to the agent workspace") from exc

    absolute_path.parent.mkdir(parents=True, exist_ok=True)
    return absolute_path


__all__ = ["provision_agent_plan_file_slot"]
