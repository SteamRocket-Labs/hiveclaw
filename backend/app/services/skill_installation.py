"""Unified installer for active skill packages.

This service is for explicit/imported skill assets, not self-evolution
promotion. Self-evolution still stages candidates first and commits through the
Skill Distiller gate. All active skill writes outside that lane should pass
through this installer so path checks and SkillGuard are not reimplemented in
each caller.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from app.services.skill_guard import scan_skill_files
from app.services.agent_asset_transaction import AgentAssetTransaction
from app.services.skill_lifecycle import record_skill_lifecycle_event


def _safe_folder_name(folder_name: str) -> str:
    normalized = (folder_name or "").strip()
    if (
        not normalized
        or normalized in {".", ".."}
        or "/" in normalized
        or "\\" in normalized
        or normalized.startswith(".")
        or "\x00" in normalized
    ):
        raise ValueError("Invalid skill folder_name")
    return normalized


def _normalize_files(files: list[dict[str, Any]] | tuple[dict[str, Any], ...]) -> list[dict[str, str]]:
    normalized: list[dict[str, str]] = []
    for item in files:
        path = str(item.get("path") or "").strip()
        content = item.get("content")
        normalized.append({"path": path, "content": "" if content is None else str(content)})
    if not normalized:
        raise ValueError("Skill package has no files")
    if not any(item["path"] in {"SKILL.md", "skill.md"} for item in normalized):
        raise ValueError("Skill package must contain SKILL.md")
    return normalized


def collect_skill_package_files(root: Path) -> list[dict[str, str]]:
    """Read a skill package directory into SkillGuard/install file records."""

    files: list[dict[str, str]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        rel = path.relative_to(root).as_posix()
        files.append({"path": rel, "content": path.read_text(encoding="utf-8", errors="replace")})
    return files


def install_active_skill_package(
    *,
    workspace: Path,
    folder_name: str,
    files: list[dict[str, Any]] | tuple[dict[str, Any], ...],
    source: str,
    overwrite: bool = False,
    transaction: AgentAssetTransaction | None = None,
) -> dict[str, Any]:
    """Install an active skill package after static guard verification."""

    safe_folder = _safe_folder_name(folder_name)
    normalized_files = _normalize_files(files)
    guard_report = scan_skill_files(normalized_files, source=source)
    if not guard_report.allowed:
        categories = ", ".join(finding.category for finding in guard_report.blocking_findings)
        raise ValueError(f"SkillGuard blocked active skill package before installation: {categories}")
    if guard_report.requires_review:
        categories = ", ".join(finding.category for finding in guard_report.review_findings)
        raise ValueError(f"SkillGuard quarantined active skill package for semantic/admin review: {categories}")

    skill_dir = (workspace / "skills" / safe_folder).resolve()
    skills_root = (workspace / "skills").resolve()
    try:
        skill_dir.relative_to(skills_root)
    except ValueError as exc:
        raise ValueError("Skill target escapes skills/") from exc
    if skill_dir.exists() and not overwrite:
        raise ValueError(f"Skill already exists at skills/{safe_folder}/SKILL.md")

    if transaction is None:
        with AgentAssetTransaction(
            workspace,
            operation="active_skill_package_install",
            evidence_refs=(f"skill-source:{source}",),
        ) as own_transaction:
            result = install_active_skill_package(
                workspace=workspace,
                folder_name=folder_name,
                files=files,
                source=source,
                overwrite=overwrite,
                transaction=own_transaction,
            )
            if own_transaction.has_changes:
                own_transaction.commit()
            return result

    written: list[str] = []
    for item in normalized_files:
        file_path = (skill_dir / item["path"]).resolve()
        try:
            file_path.relative_to(skill_dir)
        except ValueError as exc:
            raise ValueError(f"Skill file escapes package boundary: {item['path']}") from exc
        relative_path = file_path.relative_to(workspace).as_posix()
        if transaction.read_text(relative_path) == item["content"]:
            continue
        transaction.stage_text(relative_path, item["content"])
        written.append(item["path"])

    if written:
        try:
            record_skill_lifecycle_event(
                workspace,
                skill_name=safe_folder,
                status="installed",
                note=f"Installed active skill package from {source}; files={len(written)}",
                transaction=transaction,
            )
        except Exception:
            # Lifecycle telemetry must not break a verified explicit install.
            pass

    return {
        "status": "installed" if written else "unchanged",
        "folder_name": safe_folder,
        "files_written": len(written),
        "files": written,
        "skill_guard": guard_report.to_dict(),
    }
