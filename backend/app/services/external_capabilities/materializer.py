from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence


@dataclass(frozen=True)
class MaterializedExternalPackage:
    source_format: str
    source_uri: str
    package_name: str
    resolved_ref: str
    artifact_sha256: str
    files: list[dict[str, str]]
    status: str
    report: dict[str, Any]
    blocking_notes: list[dict[str, Any]] = field(default_factory=list)


def materialize_file_bundle(
    *,
    source_format: str,
    source_uri: str,
    package_name: str,
    files: Sequence[Mapping[str, Any]],
    resolved_ref: str | None = None,
    install_commands: Sequence[str] | None = None,
    quarantine_root: Path | None = None,
) -> MaterializedExternalPackage:
    """Materialize already-fetched external files into a quarantined package report.

    This server-side materializer deliberately does not execute install-time
    commands. Command execution belongs in a future isolated worker with denied
    host secrets and explicit network policy; until then command-bearing sources
    remain blocked review artifacts.
    """

    normalized_files, path_notes = _normalize_safe_files(files)
    command_notes = _install_command_notes(install_commands or ())
    blocking_notes = [*path_notes, *command_notes]
    artifact_sha256 = _files_sha256(normalized_files)
    effective_resolved_ref = resolved_ref or f"sha256:{artifact_sha256}"
    status = "blocked" if blocking_notes else "quarantined"
    quarantine_report = _write_quarantine(
        quarantine_root=quarantine_root,
        artifact_sha256=artifact_sha256,
        files=normalized_files,
    )
    report = {
        "schema": "hive.external_capability.materialization.v1",
        "status": status,
        "source_format": source_format,
        "source_uri": source_uri,
        "package_name": package_name,
        "resolved_ref": effective_resolved_ref,
        "artifact_sha256": artifact_sha256,
        "file_count": len(normalized_files),
        "sandbox": {
            "provider": "quarantine_only",
            "network": "deny",
            "host_home_mounted": False,
            "inherited_host_secrets": False,
        },
        "install_time_commands_executed": [],
        "blocking_notes": blocking_notes,
        "quarantine": quarantine_report,
    }
    if quarantine_root is not None:
        _write_report(quarantine_root / artifact_sha256[:24] / "materialization_report.json", report)
    return MaterializedExternalPackage(
        source_format=source_format,
        source_uri=source_uri,
        package_name=package_name,
        resolved_ref=effective_resolved_ref,
        artifact_sha256=artifact_sha256,
        files=normalized_files,
        status=status,
        report=report,
        blocking_notes=blocking_notes,
    )


def _normalize_safe_files(files: Sequence[Mapping[str, Any]]) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    normalized: list[dict[str, str]] = []
    notes: list[dict[str, Any]] = []
    for item in files:
        raw_path = str(item.get("path") or "").strip()
        safe_path = _safe_relative_path(raw_path)
        if safe_path is None:
            notes.append({"code": "materialized_path_escape", "path": raw_path})
            continue
        normalized.append({"path": safe_path, "content": str(item.get("content") or "")})
    return normalized, notes


def _safe_relative_path(raw_path: str) -> str | None:
    if not raw_path:
        return None
    path = PurePosixPath(raw_path.replace("\\", "/"))
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        return None
    return path.as_posix()


def _install_command_notes(commands: Iterable[str]) -> list[dict[str, Any]]:
    return [
        {
            "code": "install_time_commands_require_isolated_worker",
            "command": str(command),
        }
        for command in commands
        if str(command).strip()
    ]


def _files_sha256(files: Sequence[Mapping[str, str]]) -> str:
    payload = [{"path": item["path"], "content": item["content"]} for item in sorted(files, key=lambda item: item["path"])]
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def _write_quarantine(
    *,
    quarantine_root: Path | None,
    artifact_sha256: str,
    files: Sequence[Mapping[str, str]],
) -> dict[str, Any]:
    key = artifact_sha256[:24]
    report = {"key": key, "file_count": len(files), "written": False}
    if quarantine_root is None:
        return report
    package_root = quarantine_root / key
    package_root.mkdir(parents=True, exist_ok=True)
    for item in files:
        target = package_root / item["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(item["content"], encoding="utf-8")
    report["written"] = True
    report["report_file"] = "materialization_report.json"
    return report


def _write_report(path: Path, report: dict[str, Any]) -> None:
    path.write_text(json.dumps(report, ensure_ascii=False, sort_keys=True, indent=2), encoding="utf-8")
