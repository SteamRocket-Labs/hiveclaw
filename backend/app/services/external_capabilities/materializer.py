from __future__ import annotations

import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import urlparse

import httpx

MAX_REMOTE_PACKAGE_BYTES = 512_000
MAX_REMOTE_PACKAGE_FILES = 100
MAX_REMOTE_DIRECTORY_DEPTH = 3
DEFAULT_ALLOWED_REMOTE_HOSTS = {
    "github.com",
    "raw.githubusercontent.com",
    "api.github.com",
    "skills.sh",
    "www.skills.sh",
}

JsonFetcher = Callable[[str, Mapping[str, str]], Awaitable[Any]]
TextFetcher = Callable[[str, Mapping[str, str]], Awaitable[str]]


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
    blocking_notes: Sequence[Mapping[str, Any]] | None = None,
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
    blocking_notes = [*_normalize_blocking_notes(blocking_notes or ()), *path_notes, *command_notes]
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


async def materialize_remote_source(
    *,
    source_uri: str,
    source_format: str,
    package_name: str,
    token: str = "",
    install_commands: Sequence[str] | None = None,
    quarantine_root: Path | None = None,
    allowed_hosts: set[str] | None = None,
    fetch_json: JsonFetcher | None = None,
    fetch_text: TextFetcher | None = None,
    max_total_bytes: int = MAX_REMOTE_PACKAGE_BYTES,
    max_files: int = MAX_REMOTE_PACKAGE_FILES,
) -> MaterializedExternalPackage:
    """Fetch a remote external source into the same quarantine-only materialization boundary.

    This function may perform network fetches from explicit allowlisted hosts,
    but it never runs package install commands and never inherits host secrets.
    Command-shaped sources are represented as blocked review artifacts.
    """

    command = source_uri.strip()
    if _looks_like_install_command(command):
        return materialize_file_bundle(
            source_format=source_format,
            source_uri=source_uri,
            package_name=package_name,
            files=[],
            install_commands=[command],
            quarantine_root=quarantine_root,
        )

    parsed = urlparse(source_uri)
    hosts = allowed_hosts or DEFAULT_ALLOWED_REMOTE_HOSTS
    if parsed.scheme not in {"http", "https"} or parsed.netloc.lower() not in hosts:
        return materialize_file_bundle(
            source_format=source_format,
            source_uri=source_uri,
            package_name=package_name,
            files=[],
            blocking_notes=[
                {
                    "code": "remote_host_not_allowed",
                    "host": parsed.netloc.lower(),
                    "scheme": parsed.scheme,
                }
            ],
            quarantine_root=quarantine_root,
        )

    json_fetcher = fetch_json or _default_fetch_json
    text_fetcher = fetch_text or _default_fetch_text
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    try:
        remote = await _fetch_remote_files(
            source_uri=source_uri,
            headers=headers,
            fetch_json=json_fetcher,
            fetch_text=text_fetcher,
            max_total_bytes=max_total_bytes,
            max_files=max_files,
        )
    except _RemoteMaterializationBlocked as blocked:
        return materialize_file_bundle(
            source_format=source_format,
            source_uri=source_uri,
            package_name=package_name,
            files=[],
            blocking_notes=[blocked.note],
            quarantine_root=quarantine_root,
        )
    except Exception as exc:  # noqa: BLE001 - remote intake must fail closed into review evidence
        return materialize_file_bundle(
            source_format=source_format,
            source_uri=source_uri,
            package_name=package_name,
            files=[],
            blocking_notes=[{"code": "remote_fetch_failed", "message": str(exc)}],
            quarantine_root=quarantine_root,
        )

    package = materialize_file_bundle(
        source_format=source_format,
        source_uri=source_uri,
        package_name=package_name,
        files=remote.files,
        resolved_ref=remote.resolved_ref,
        install_commands=install_commands,
        quarantine_root=quarantine_root,
    )
    package.report["remote_fetch"] = remote.report
    if quarantine_root is not None:
        _write_report(quarantine_root / package.artifact_sha256[:24] / "materialization_report.json", package.report)
    return package


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


def _normalize_blocking_notes(notes: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    return [dict(note) for note in notes if isinstance(note, Mapping)]


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


def _looks_like_install_command(value: str) -> bool:
    lowered = value.strip().lower()
    return lowered.startswith(("npx ", "npm ", "pnpm ", "yarn ", "bunx "))


@dataclass(frozen=True)
class _RemoteFetchResult:
    files: list[dict[str, str]]
    resolved_ref: str
    report: dict[str, Any]


class _RemoteMaterializationBlocked(Exception):
    def __init__(self, note: dict[str, Any]) -> None:
        super().__init__(str(note))
        self.note = note


async def _fetch_remote_files(
    *,
    source_uri: str,
    headers: Mapping[str, str],
    fetch_json: JsonFetcher,
    fetch_text: TextFetcher,
    max_total_bytes: int,
    max_files: int,
) -> _RemoteFetchResult:
    github = _parse_github_source(source_uri)
    if github is not None:
        files = await _fetch_github_files(
            github=github,
            headers=headers,
            fetch_json=fetch_json,
            max_total_bytes=max_total_bytes,
            max_files=max_files,
        )
        return _RemoteFetchResult(
            files=files,
            resolved_ref=f"github:{github['owner']}/{github['repo']}@{github['branch']}:{github['path'] or '.'}",
            report={
                "source_kind": github["kind"],
                "host_allowlist_enforced": True,
                "network": "allowlist",
                "file_count": len(files),
                "max_total_bytes": max_total_bytes,
                "max_files": max_files,
            },
        )

    raw = _parse_raw_github_source(source_uri)
    if raw is not None:
        content = await fetch_text(source_uri, headers)
        _check_size(len(content.encode("utf-8")), max_total_bytes=max_total_bytes, max_files=1)
        return _RemoteFetchResult(
            files=[{"path": raw["path"].split("/")[-1] or "SKILL.md", "content": content}],
            resolved_ref=f"github:{raw['owner']}/{raw['repo']}@{raw['branch']}:{raw['path']}",
            report={
                "source_kind": "github_raw_file",
                "host_allowlist_enforced": True,
                "network": "allowlist",
                "file_count": 1,
                "max_total_bytes": max_total_bytes,
                "max_files": max_files,
            },
        )

    raise _RemoteMaterializationBlocked({"code": "remote_source_format_unsupported", "source_uri": source_uri})


def _parse_github_source(source_uri: str) -> dict[str, str] | None:
    parsed = urlparse(source_uri)
    if parsed.netloc.lower() != "github.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 2:
        return None
    owner, repo = parts[0], parts[1].removesuffix(".git")
    if len(parts) >= 5 and parts[2] in {"tree", "blob"}:
        return {
            "kind": "github_tree" if parts[2] == "tree" else "github_file",
            "owner": owner,
            "repo": repo,
            "branch": parts[3],
            "path": "/".join(parts[4:]),
        }
    if len(parts) == 2:
        return {"kind": "github_tree", "owner": owner, "repo": repo, "branch": "main", "path": ""}
    return None


def _parse_raw_github_source(source_uri: str) -> dict[str, str] | None:
    parsed = urlparse(source_uri)
    if parsed.netloc.lower() != "raw.githubusercontent.com":
        return None
    parts = [part for part in parsed.path.split("/") if part]
    if len(parts) < 4:
        return None
    return {"owner": parts[0], "repo": parts[1], "branch": parts[2], "path": "/".join(parts[3:])}


async def _fetch_github_files(
    *,
    github: Mapping[str, str],
    headers: Mapping[str, str],
    fetch_json: JsonFetcher,
    max_total_bytes: int,
    max_files: int,
) -> list[dict[str, str]]:
    files: list[dict[str, str]] = []
    total_bytes = 0
    root_path = github["path"]

    async def recurse(path: str, rel_prefix: str, depth: int) -> None:
        nonlocal total_bytes
        if depth > MAX_REMOTE_DIRECTORY_DEPTH:
            raise _RemoteMaterializationBlocked({"code": "remote_directory_depth_exceeded", "path": path})
        api_url = (
            f"https://api.github.com/repos/{github['owner']}/{github['repo']}/contents/{path}"
            f"?ref={github['branch']}"
        )
        items = await fetch_json(api_url, headers)
        if isinstance(items, Mapping):
            items = [items]
        if not isinstance(items, list):
            raise _RemoteMaterializationBlocked({"code": "remote_manifest_unreadable", "path": path})
        for item in items:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or "")
            item_path = str(item.get("path") or name)
            rel = f"{rel_prefix}{name}" if rel_prefix else _relative_to_root(item_path, root_path)
            if item.get("type") == "dir":
                await recurse(item_path, f"{rel}/", depth + 1)
                continue
            if item.get("type") != "file":
                continue
            if len(files) >= max_files:
                raise _RemoteMaterializationBlocked({"code": "remote_file_count_exceeded", "max_files": max_files})
            declared_size = int(item.get("size") or 0)
            total_bytes += declared_size
            _check_size(total_bytes, max_total_bytes=max_total_bytes, max_files=len(files) + 1)
            content_payload = await fetch_json(str(item.get("url") or ""), headers)
            content = _decode_github_content(content_payload)
            total_bytes += max(0, len(content.encode("utf-8")) - declared_size)
            _check_size(total_bytes, max_total_bytes=max_total_bytes, max_files=len(files) + 1)
            files.append({"path": rel, "content": content})

    await recurse(root_path, "", 0)
    return files


def _relative_to_root(item_path: str, root_path: str) -> str:
    normalized_item = item_path.strip("/")
    normalized_root = root_path.strip("/")
    if normalized_root and normalized_item.startswith(normalized_root + "/"):
        return normalized_item[len(normalized_root) + 1 :]
    return normalized_item.split("/")[-1]


def _decode_github_content(payload: Any) -> str:
    if not isinstance(payload, Mapping):
        raise _RemoteMaterializationBlocked({"code": "remote_file_unreadable"})
    raw_content = str(payload.get("content") or "")
    try:
        return base64.b64decode(raw_content).decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        raise _RemoteMaterializationBlocked({"code": "remote_file_decode_failed"}) from exc


def _check_size(total_bytes: int, *, max_total_bytes: int, max_files: int) -> None:
    if total_bytes > max_total_bytes:
        raise _RemoteMaterializationBlocked(
            {"code": "remote_total_size_exceeded", "max_total_bytes": max_total_bytes}
        )
    if max_files > MAX_REMOTE_PACKAGE_FILES:
        raise _RemoteMaterializationBlocked({"code": "remote_file_count_exceeded", "max_files": MAX_REMOTE_PACKAGE_FILES})


async def _default_fetch_json(url: str, headers: Mapping[str, str]) -> Any:
    async with httpx.AsyncClient(timeout=30, headers=dict(headers)) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.json()


async def _default_fetch_text(url: str, headers: Mapping[str, str]) -> str:
    async with httpx.AsyncClient(timeout=30, headers=dict(headers)) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text


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
