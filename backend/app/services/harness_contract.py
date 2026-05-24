"""Workspace manifest and artifact reference contract for long-running harnesses."""

from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass(frozen=True, slots=True)
class ExecutionArtifactRef:
    schema: str
    path: str
    kind: str
    content_hash: str
    char_count: int
    created_at: str
    source: str = "workspace"


@dataclass(frozen=True, slots=True)
class WorkspaceManifest:
    schema: str
    runtime_task_id: str
    workspace_root: str
    boundary: dict[str, Any]
    artifact_refs: list[dict[str, Any]]
    created_at: str


def _agent_root(agent_id: uuid.UUID, *, data_root: str | Path) -> Path:
    return Path(data_root) / str(agent_id)


def _manifest_path(agent_id: uuid.UUID, runtime_task_id: uuid.UUID, *, data_root: str | Path) -> Path:
    return (
        _agent_root(agent_id, data_root=data_root)
        / "runtime_artifacts"
        / "long_tasks"
        / runtime_task_id.hex
        / "workspace_manifest.json"
    )


def _relative_to_agent(agent_id: uuid.UUID, path: Path, *, data_root: str | Path) -> str:
    return path.resolve().relative_to(_agent_root(agent_id, data_root=data_root).resolve()).as_posix()


def build_execution_artifact_ref(
    *,
    agent_id: uuid.UUID,
    path: Path,
    data_root: str | Path,
    kind: str = "file",
    source: str = "workspace",
) -> dict[str, Any]:
    content = path.read_bytes() if path.exists() and path.is_file() else b""
    ref = ExecutionArtifactRef(
        schema="execution_artifact_ref.v1",
        path=_relative_to_agent(agent_id, path, data_root=data_root),
        kind=kind,
        content_hash=hashlib.sha256(content).hexdigest(),
        char_count=len(content.decode("utf-8", errors="replace")),
        created_at=_now_iso(),
        source=source,
    )
    return asdict(ref)


def write_workspace_manifest(
    *,
    agent_id: uuid.UUID,
    runtime_task_id: uuid.UUID,
    workspace_root: Path,
    artifact_paths: list[Path],
    data_root: str | Path,
) -> dict[str, Any]:
    manifest_path = _manifest_path(agent_id, runtime_task_id, data_root=data_root)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    refs = [
        build_execution_artifact_ref(agent_id=agent_id, path=Path(path), data_root=data_root)
        for path in artifact_paths
        if Path(path).exists()
    ]
    manifest = WorkspaceManifest(
        schema="workspace_manifest.v1",
        runtime_task_id=runtime_task_id.hex,
        workspace_root=_relative_to_agent(agent_id, workspace_root, data_root=data_root),
        boundary={
            "generated_code_boundary": "workspace",
            "credential_policy": "do not include tenant/provider credentials in artifacts",
            "artifact_root": _relative_to_agent(agent_id, manifest_path.parent, data_root=data_root),
        },
        artifact_refs=refs,
        created_at=_now_iso(),
    )
    payload = asdict(manifest)
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "schema": "workspace_manifest.v1",
        "path": _relative_to_agent(agent_id, manifest_path, data_root=data_root),
        "created_at": payload["created_at"],
        "artifact_refs": refs,
    }


def _load_manifest(agent_id: uuid.UUID, runtime_task_id: uuid.UUID, *, data_root: str | Path) -> dict[str, Any]:
    path = _manifest_path(agent_id, runtime_task_id, data_root=data_root)
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def build_manifest_resume_context(
    *,
    agent_id: uuid.UUID,
    runtime_task_id: uuid.UUID,
    data_root: str | Path,
) -> dict[str, Any]:
    manifest = _load_manifest(agent_id, runtime_task_id, data_root=data_root)
    artifact_refs = manifest.get("artifact_refs") or []
    lines = [f"Workspace manifest for long task {runtime_task_id.hex}."]
    if manifest.get("workspace_root"):
        lines.append(f"Workspace root: {manifest['workspace_root']}")
    if artifact_refs:
        lines.append("Artifact refs:")
        for ref in artifact_refs:
            lines.append(f"- {ref.get('path')} ({ref.get('kind')}, sha256={ref.get('content_hash')})")
    return {
        "schema": "workspace_manifest_resume_context.v1",
        "runtime_task_id": runtime_task_id.hex,
        "manifest": manifest,
        "artifact_refs": artifact_refs,
        "resume_prompt": "\n".join(lines),
    }
