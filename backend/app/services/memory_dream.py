"""Memory Dream: CC/Codex-style T2 -> T3 consolidation staging.

This service is intentionally separate from ``auto_dream``. ``auto_dream`` is
the Soul Dream / identity-promotion lane; this module owns the project-memory
consolidation lane: reviewed T2 packages -> dream workspace diff -> T3 batch.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.memory.t3_consolidation import T3ConsolidationBatchResult, build_t3_consolidation_batch


RAW_T2_INPUTS_FILENAME = "raw_t2_inputs.md"
PHASE2_DIFF_FILENAME = "phase2_workspace_diff.md"
BASELINE_FILENAME = "baseline.json"
ROLLOUT_SUMMARIES_DIRNAME = "rollout_summaries"


@dataclass(frozen=True, slots=True)
class MemoryDreamWorkspaceResult:
    status: str
    agent_id: uuid.UUID
    workspace_dir: Path
    diff_path: Path
    selected_package_dirs: tuple[Path, ...]
    input_hash: str
    issues: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MemoryDreamRunResult:
    status: str
    workspace_result: MemoryDreamWorkspaceResult
    t3_batch_result: T3ConsolidationBatchResult | None = None
    issues: tuple[str, ...] = ()


def _data_root(data_root: Path | str | None = None) -> Path:
    return Path(data_root) if data_root is not None else Path(get_settings().AGENT_DATA_DIR)


def _memory_dir(root: Path, agent_id: uuid.UUID) -> Path:
    mem_dir = root / str(agent_id) / "memory"
    mem_dir.mkdir(parents=True, exist_ok=True)
    return mem_dir


def _workspace_dir(root: Path, agent_id: uuid.UUID) -> Path:
    workspace = _memory_dir(root, agent_id) / ".dream_workspace"
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / ROLLOUT_SUMMARIES_DIRNAME).mkdir(parents=True, exist_ok=True)
    return workspace


def _load_json(path: Path) -> dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _review_allows_t3(package_dir: Path) -> bool:
    review = (package_dir / "review.md").read_text(encoding="utf-8", errors="replace")
    return "<decision>approved</decision>" in review and "<allowed_next>t3_intake</allowed_next>" in review


def _discover_reviewed_t2_packages(root: Path, agent_id: uuid.UUID, *, max_packages: int) -> tuple[Path, ...]:
    sessions_dir = root / str(agent_id) / "memory" / "sessions"
    if not sessions_dir.exists():
        return ()
    package_dirs: list[Path] = []
    for manifest_path in sorted(sessions_dir.glob("*/segments/*/manifest.json")):
        package_dir = manifest_path.parent
        manifest = _load_json(manifest_path)
        if manifest.get("package_status") != "reviewed":
            continue
        if not all((package_dir / name).exists() for name in ("summary.md", "labels.md", "review.md")):
            continue
        if not _review_allows_t3(package_dir):
            continue
        package_dirs.append(package_dir)
    return tuple(package_dirs[:max_packages])


def _package_sort_key(package_dir: Path) -> tuple[str, str, str]:
    manifest = _load_json(package_dir / "manifest.json")
    return (
        str(manifest.get("session_id") or ""),
        str((manifest.get("source_range") or {}).get("start_sequence") or ""),
        str(manifest.get("package_id") or package_dir.name),
    )


def _render_rollout_summary(package_dir: Path) -> tuple[str, str]:
    manifest = _load_json(package_dir / "manifest.json")
    package_id = str(manifest.get("package_id") or package_dir.name)
    source_refs = [str(ref) for ref in manifest.get("source_refs") or [] if str(ref).strip()]
    summary = (package_dir / "summary.md").read_text(encoding="utf-8", errors="replace").strip()
    labels = (package_dir / "labels.md").read_text(encoding="utf-8", errors="replace").strip()
    review = (package_dir / "review.md").read_text(encoding="utf-8", errors="replace").strip()
    body = "\n".join(
        [
            f"# T2 Rollout Summary: {package_id}",
            "",
            f"- package_dir: {package_dir.as_posix()}",
            f"- source_refs: {', '.join(source_refs) if source_refs else '-'}",
            "",
            "## Summary",
            summary,
            "",
            "## Labels",
            labels,
            "",
            "## Review",
            review,
            "",
        ]
    )
    return package_id, body


def _render_raw_t2_inputs(package_dirs: tuple[Path, ...]) -> str:
    if not package_dirs:
        return "# Raw T2 Inputs\n\n(no reviewed T2 packages selected)\n"
    sections = ["# Raw T2 Inputs", ""]
    for package_dir in sorted(package_dirs, key=_package_sort_key):
        package_id, body = _render_rollout_summary(package_dir)
        sections.extend([f"<!-- package_id: {package_id} -->", body.rstrip(), ""])
    return "\n".join(sections).rstrip() + "\n"


def _hash_workspace_inputs(raw_inputs: str, rollout_summaries: dict[str, str]) -> str:
    digest = hashlib.sha256()
    digest.update(raw_inputs.encode("utf-8"))
    for name in sorted(rollout_summaries):
        digest.update(name.encode("utf-8"))
        digest.update(b"\0")
        digest.update(rollout_summaries[name].encode("utf-8"))
        digest.update(b"\0")
    return digest.hexdigest()


def _read_baseline(workspace: Path) -> dict[str, Any]:
    return _load_json(workspace / BASELINE_FILENAME)


def _write_diff(
    *,
    workspace: Path,
    input_hash: str,
    baseline_hash: str,
    raw_inputs: str,
    rollout_summaries: dict[str, str],
) -> Path:
    diff_path = workspace / PHASE2_DIFF_FILENAME
    lines = [
        "# Memory Dream Workspace Diff",
        "",
        f"- baseline_input_hash: {baseline_hash or '<none>'}",
        f"- current_input_hash: {input_hash}",
        "",
        "## Changed Inputs",
        "",
        f"- {RAW_T2_INPUTS_FILENAME}",
        *[f"- {ROLLOUT_SUMMARIES_DIRNAME}/{name}.md" for name in sorted(rollout_summaries)],
        "",
        "## Current Raw T2 Inputs",
        "",
        raw_inputs.strip(),
        "",
    ]
    diff_path.write_text("\n".join(lines).rstrip() + "\n", encoding="utf-8")
    return diff_path


def prepare_memory_dream_workspace(
    *,
    agent_id: uuid.UUID,
    data_root: Path | str | None = None,
    max_packages: int = 8,
) -> MemoryDreamWorkspaceResult:
    root = _data_root(data_root)
    workspace = _workspace_dir(root, agent_id)
    diff_path = workspace / PHASE2_DIFF_FILENAME
    selected_packages = _discover_reviewed_t2_packages(root, agent_id, max_packages=max_packages)
    if not selected_packages:
        diff_path.unlink(missing_ok=True)
        return MemoryDreamWorkspaceResult(
            status="no_inputs",
            agent_id=agent_id,
            workspace_dir=workspace,
            diff_path=diff_path,
            selected_package_dirs=(),
            input_hash="",
            issues=("no reviewed T2 packages selected",),
        )

    rollout_dir = workspace / ROLLOUT_SUMMARIES_DIRNAME
    if rollout_dir.exists():
        shutil.rmtree(rollout_dir)
    rollout_dir.mkdir(parents=True, exist_ok=True)

    rollout_summaries: dict[str, str] = {}
    for package_dir in selected_packages:
        package_id, body = _render_rollout_summary(package_dir)
        rollout_summaries[package_id] = body
        (rollout_dir / f"{package_id}.md").write_text(body, encoding="utf-8")

    raw_inputs = _render_raw_t2_inputs(selected_packages)
    (workspace / RAW_T2_INPUTS_FILENAME).write_text(raw_inputs, encoding="utf-8")
    input_hash = _hash_workspace_inputs(raw_inputs, rollout_summaries)
    baseline_hash = str(_read_baseline(workspace).get("input_hash") or "")
    if input_hash == baseline_hash:
        diff_path.unlink(missing_ok=True)
        return MemoryDreamWorkspaceResult(
            status="no_changes",
            agent_id=agent_id,
            workspace_dir=workspace,
            diff_path=diff_path,
            selected_package_dirs=selected_packages,
            input_hash=input_hash,
        )

    _write_diff(
        workspace=workspace,
        input_hash=input_hash,
        baseline_hash=baseline_hash,
        raw_inputs=raw_inputs,
        rollout_summaries=rollout_summaries,
    )
    return MemoryDreamWorkspaceResult(
        status="changed",
        agent_id=agent_id,
        workspace_dir=workspace,
        diff_path=diff_path,
        selected_package_dirs=selected_packages,
        input_hash=input_hash,
    )


def finalize_memory_dream_workspace(result: MemoryDreamWorkspaceResult) -> Path:
    baseline_path = result.workspace_dir / BASELINE_FILENAME
    baseline_path.write_text(
        json.dumps(
            {
                "schema_version": "memory_dream.baseline.v1",
                "agent_id": str(result.agent_id),
                "input_hash": result.input_hash,
                "selected_package_dirs": [path.as_posix() for path in result.selected_package_dirs],
            },
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n",
        encoding="utf-8",
    )
    result.diff_path.unlink(missing_ok=True)
    return baseline_path


def run_memory_dream(
    *,
    agent_id: uuid.UUID,
    data_root: Path | str | None = None,
    max_packages: int = 8,
) -> MemoryDreamRunResult:
    root = _data_root(data_root)
    workspace_result = prepare_memory_dream_workspace(agent_id=agent_id, data_root=root, max_packages=max_packages)
    if workspace_result.status != "changed":
        return MemoryDreamRunResult(
            status=workspace_result.status,
            workspace_result=workspace_result,
            issues=workspace_result.issues,
        )
    batch = build_t3_consolidation_batch(
        agent_id=agent_id,
        data_root=root,
        package_dirs=workspace_result.selected_package_dirs,
    )
    status = "staged" if batch.status == "staged" else "held"
    return MemoryDreamRunResult(
        status=status,
        workspace_result=workspace_result,
        t3_batch_result=batch,
        issues=batch.issues,
    )
