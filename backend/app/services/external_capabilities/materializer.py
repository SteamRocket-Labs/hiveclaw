from __future__ import annotations

import asyncio
import base64
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import tarfile
import tempfile
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
DEFAULT_ALLOWED_NPM_REGISTRY_HOSTS = {"registry.npmjs.org"}
GIT_CLONE_TIMEOUT_SECONDS = 60.0
NPM_MAX_COMPRESSED_BYTES = 5_000_000

JsonFetcher = Callable[[str, Mapping[str, str]], Awaitable[Any]]
TextFetcher = Callable[[str, Mapping[str, str]], Awaitable[str]]
BytesFetcher = Callable[[str, Mapping[str, str]], Awaitable[bytes]]

_NPM_SCOPED_NAME = re.compile(r"^@[a-z0-9][a-z0-9\-._]*/[a-z0-9][a-z0-9\-._]*$")
_NPM_REGULAR_NAME = re.compile(r"^[a-z0-9][a-z0-9\-._]*$")


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


def materialize_local_source(
    *,
    source_path: str,
    source_format: str,
    package_name: str,
    source_kind: str = "auto",
    quarantine_root: Path | None = None,
    allowed_roots: Sequence[Path] | None = None,
    max_total_bytes: int = MAX_REMOTE_PACKAGE_BYTES,
    max_files: int = MAX_REMOTE_PACKAGE_FILES,
    max_depth: int = MAX_REMOTE_DIRECTORY_DEPTH,
) -> MaterializedExternalPackage:
    """Materialize a local ``file`` or ``directory`` plugin source into the same
    quarantine-only boundary (FreeCode marketplaceManager.ts:1623/1636).

    Local reads are bounded by ``allowed_roots`` (paths outside are rejected),
    reject symlinks (no following into host files), and honor file-count / total-
    byte / directory-depth ceilings. The output is a staging bundle for the
    import/review flow; nothing is executed and no host secrets are inherited.
    """

    resolved = Path(urlparse(source_path).path if source_path.startswith("file://") else source_path)
    if not _within_allowed_roots(resolved, allowed_roots):
        return _blocked_local_bundle(
            source_format,
            source_path,
            package_name,
            quarantine_root,
            source_kind,
            {"code": "local_source_outside_allowed_roots", "path": str(resolved)},
        )
    if resolved.is_symlink():
        return _blocked_local_bundle(
            source_format,
            source_path,
            package_name,
            quarantine_root,
            source_kind,
            {"code": "materialized_symlink_rejected", "path": str(resolved)},
        )
    if not resolved.exists():
        return _blocked_local_bundle(
            source_format,
            source_path,
            package_name,
            quarantine_root,
            source_kind,
            {"code": "local_source_not_found", "path": str(resolved)},
        )

    effective_kind = source_kind
    if effective_kind == "auto":
        effective_kind = "directory" if resolved.is_dir() else "file"

    if effective_kind == "file":
        content = resolved.read_text(encoding="utf-8", errors="replace")
        files = [{"path": resolved.name, "content": content}]
        blocking_notes: list[dict[str, Any]] = []
    else:
        files, blocking_notes = _read_local_directory(
            resolved, max_files=max_files, max_total_bytes=max_total_bytes, max_depth=max_depth
        )

    package = materialize_file_bundle(
        source_format=source_format,
        source_uri=source_path,
        package_name=package_name,
        files=files,
        resolved_ref=f"local:{effective_kind}",
        blocking_notes=blocking_notes,
        quarantine_root=quarantine_root,
    )
    package.report["source_kind"] = effective_kind
    if quarantine_root is not None:
        _write_report(quarantine_root / package.artifact_sha256[:24] / "materialization_report.json", package.report)
    return package


def _blocked_local_bundle(
    source_format: str,
    source_uri: str,
    package_name: str,
    quarantine_root: Path | None,
    source_kind: str,
    note: dict[str, Any],
) -> MaterializedExternalPackage:
    package = materialize_file_bundle(
        source_format=source_format,
        source_uri=source_uri,
        package_name=package_name,
        files=[],
        blocking_notes=[note],
        quarantine_root=quarantine_root,
    )
    package.report["source_kind"] = source_kind
    return package


def _within_allowed_roots(path: Path, allowed_roots: Sequence[Path] | None) -> bool:
    # Security boundary: no configured roots means no local read is allowed.
    # Failing open here would let any caller that omits allowed_roots read
    # arbitrary server paths into review evidence.
    if not allowed_roots:
        return False
    resolved = path.resolve()
    for root in allowed_roots:
        try:
            resolved.relative_to(Path(root).resolve())
            return True
        except ValueError:
            continue
    return False


def _read_local_directory(
    root: Path,
    *,
    max_files: int,
    max_total_bytes: int,
    max_depth: int,
    exclude_dirs: set[str] | None = None,
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    root = root.resolve()
    excluded = exclude_dirs or set()
    files: list[dict[str, str]] = []
    notes: list[dict[str, Any]] = []
    total_bytes = 0
    stop = False

    def walk(current: Path, rel_prefix: str, depth: int) -> None:
        nonlocal total_bytes, stop
        if stop or depth > max_depth:
            return
        for entry in sorted(current.iterdir(), key=lambda item: item.name):
            if stop:
                return
            rel = f"{rel_prefix}{entry.name}"
            if entry.is_symlink():
                notes.append({"code": "materialized_symlink_rejected", "path": rel})
                continue
            if entry.is_dir():
                if entry.name in excluded:
                    continue
                walk(entry, f"{rel}/", depth + 1)
            elif entry.is_file():
                if len(files) >= max_files:
                    notes.append({"code": "local_file_count_exceeded", "max_files": max_files})
                    stop = True
                    return
                content = entry.read_text(encoding="utf-8", errors="replace")
                total_bytes += len(content.encode("utf-8"))
                if total_bytes > max_total_bytes:
                    notes.append({"code": "local_total_size_exceeded", "max_total_bytes": max_total_bytes})
                    stop = True
                    return
                files.append({"path": rel, "content": content})

    walk(root, "", 0)
    return files, notes


async def materialize_git_source(
    *,
    git_url: str,
    source_format: str,
    package_name: str,
    ref: str | None = None,
    quarantine_root: Path | None = None,
    allowed_hosts: set[str] | None = None,
    allowed_roots: Sequence[Path] | None = None,
    max_total_bytes: int = MAX_REMOTE_PACKAGE_BYTES,
    max_files: int = MAX_REMOTE_PACKAGE_FILES,
    max_depth: int = MAX_REMOTE_DIRECTORY_DEPTH,
    timeout_seconds: float = GIT_CLONE_TIMEOUT_SECONDS,
) -> MaterializedExternalPackage:
    """Hardened shallow clone of a generic git plugin source (FreeCode
    marketplaceManager.ts:836-897).

    A platform-side deterministic fetch, NOT agent-controlled code execution.
    Hardening: ``--depth 1`` single-branch, git hooks + fsmonitor disabled, the
    ext/dumb-http RCE protocols denied, no host credential helper inherited, a
    wall-clock timeout, ``.git`` stripped from the bundle, symlinks rejected, and
    size/count/depth ceilings. Only ``https`` and ``file`` (allowed-root bounded)
    URL schemes are permitted; ``git://``/``ssh``/``ext::`` are refused.

    Consumer: the C4 plugin import flow (fetch → quarantine →
    ``cc_plugin_adapter.load_cc_plugin_bundle`` → Trust Gate review), parallel to
    ``materialize_remote_source``.
    """

    parsed = urlparse(git_url)
    scheme = parsed.scheme.lower()
    hosts = allowed_hosts or DEFAULT_ALLOWED_REMOTE_HOSTS
    if scheme == "https":
        if parsed.netloc.lower() not in hosts:
            return _blocked_local_bundle(
                source_format,
                git_url,
                package_name,
                quarantine_root,
                "git",
                {"code": "git_host_not_allowed", "host": parsed.netloc.lower()},
            )
    elif scheme == "file":
        if not _within_allowed_roots(Path(parsed.path), allowed_roots):
            return _blocked_local_bundle(
                source_format,
                git_url,
                package_name,
                quarantine_root,
                "git",
                {"code": "git_file_outside_allowed_roots", "path": parsed.path},
            )
    else:
        return _blocked_local_bundle(
            source_format,
            git_url,
            package_name,
            quarantine_root,
            "git",
            {"code": "git_scheme_not_allowed", "scheme": scheme},
        )

    with tempfile.TemporaryDirectory(prefix="hive-git-") as tmp:
        clone_dir = Path(tmp) / "repo"
        hooks_dir = Path(tmp) / "nohooks"
        hooks_dir.mkdir()
        args = [
            "git",
            "-c",
            f"core.hooksPath={hooks_dir}",
            "-c",
            "core.fsmonitor=false",
            "-c",
            "credential.helper=",
            "-c",
            "protocol.ext.allow=never",
        ]
        if scheme == "file":
            args += ["-c", "protocol.file.allow=always"]
        args += ["clone", "--depth", "1", "--no-tags", "--single-branch"]
        if ref:
            args += ["--branch", ref]
        args += ["--", git_url, str(clone_dir)]
        try:
            exit_code = await _run_git_clone(args, timeout_seconds)
        except Exception as exc:  # noqa: BLE001 - clone failures become review evidence
            return _blocked_local_bundle(
                source_format,
                git_url,
                package_name,
                quarantine_root,
                "git",
                {"code": "git_clone_failed", "message": str(exc)},
            )
        if exit_code != 0:
            return _blocked_local_bundle(
                source_format,
                git_url,
                package_name,
                quarantine_root,
                "git",
                {"code": "git_clone_failed", "exit_code": exit_code},
            )
        files, blocking_notes = _read_local_directory(
            clone_dir,
            max_files=max_files,
            max_total_bytes=max_total_bytes,
            max_depth=max_depth,
            exclude_dirs={".git"},
        )

    package = materialize_file_bundle(
        source_format=source_format,
        source_uri=git_url,
        package_name=package_name,
        files=files,
        resolved_ref=f"git:{ref or 'default'}",
        blocking_notes=blocking_notes,
        quarantine_root=quarantine_root,
    )
    package.report["source_kind"] = "git"
    if quarantine_root is not None:
        _write_report(quarantine_root / package.artifact_sha256[:24] / "materialization_report.json", package.report)
    return package


async def materialize_npm_source(
    *,
    package: str,
    source_format: str,
    package_name: str,
    version: str | None = None,
    registry: str = "https://registry.npmjs.org",
    quarantine_root: Path | None = None,
    allowed_registry_hosts: set[str] | None = None,
    fetch_json: JsonFetcher | None = None,
    fetch_bytes: BytesFetcher | None = None,
    max_total_bytes: int = MAX_REMOTE_PACKAGE_BYTES,
    max_files: int = MAX_REMOTE_PACKAGE_FILES,
    max_compressed_bytes: int = NPM_MAX_COMPRESSED_BYTES,
) -> MaterializedExternalPackage:
    """Read-only npm registry tarball fetch + unpack into the quarantine boundary.

    Hive-ahead-of-CC additive delta: CC's npm marketplace source is unimplemented
    (marketplaceManager.ts:1618 ``// TODO``). This NEVER invokes npm or any
    package lifecycle scripts — it resolves the version, downloads the ``.tgz``,
    and unpacks pure files. Hardening: registry host allowlist, compressed and
    decompressed size ceilings, tar member path-traversal rejection, and
    symlink/hardlink rejection.

    Consumer: the C4 plugin import flow (fetch → quarantine →
    ``cc_plugin_adapter.load_cc_plugin_bundle`` → Trust Gate review), parallel to
    ``materialize_remote_source``.
    """

    if not _valid_npm_package_name(package):
        return _blocked_local_bundle(
            source_format,
            f"npm:{package}",
            package_name,
            quarantine_root,
            "npm",
            {"code": "npm_invalid_package_name", "package": package},
        )
    parsed_registry = urlparse(registry)
    hosts = allowed_registry_hosts or DEFAULT_ALLOWED_NPM_REGISTRY_HOSTS
    if parsed_registry.scheme != "https" or parsed_registry.netloc.lower() not in hosts:
        return _blocked_local_bundle(
            source_format,
            f"npm:{package}",
            package_name,
            quarantine_root,
            "npm",
            {"code": "npm_registry_not_allowed", "registry": registry},
        )

    json_fetcher = fetch_json or _default_fetch_json
    bytes_fetcher = fetch_bytes or _default_fetch_bytes
    try:
        metadata = await json_fetcher(f"{registry.rstrip('/')}/{package}", {})
        resolved_version, tarball_url = _resolve_npm_tarball(metadata, version)
        if not tarball_url:
            return _blocked_local_bundle(
                source_format,
                f"npm:{package}",
                package_name,
                quarantine_root,
                "npm",
                {"code": "npm_version_not_found", "requested_version": version},
            )
        if urlparse(tarball_url).netloc.lower() not in hosts:
            return _blocked_local_bundle(
                source_format,
                f"npm:{package}",
                package_name,
                quarantine_root,
                "npm",
                {"code": "npm_tarball_host_not_allowed", "host": urlparse(tarball_url).netloc.lower()},
            )
        data = await bytes_fetcher(tarball_url, {})
    except Exception as exc:  # noqa: BLE001 - fetch failures become review evidence
        return _blocked_local_bundle(
            source_format,
            f"npm:{package}",
            package_name,
            quarantine_root,
            "npm",
            {"code": "npm_fetch_failed", "message": str(exc)},
        )

    if len(data) > max_compressed_bytes:
        return _blocked_local_bundle(
            source_format,
            f"npm:{package}",
            package_name,
            quarantine_root,
            "npm",
            {"code": "npm_tarball_too_large", "compressed_bytes": len(data)},
        )

    files, blocking_notes = _extract_npm_tarball(data, max_total_bytes=max_total_bytes, max_files=max_files)
    package_obj = materialize_file_bundle(
        source_format=source_format,
        source_uri=f"npm:{package}",
        package_name=package_name,
        files=files,
        resolved_ref=f"npm:{package}@{resolved_version}",
        blocking_notes=blocking_notes,
        quarantine_root=quarantine_root,
    )
    package_obj.report["source_kind"] = "npm"
    if quarantine_root is not None:
        _write_report(
            quarantine_root / package_obj.artifact_sha256[:24] / "materialization_report.json", package_obj.report
        )
    return package_obj


async def _run_git_clone(args: list[str], timeout_seconds: float) -> int:
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1"}
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        await asyncio.wait_for(proc.communicate(), timeout=timeout_seconds)
    except (asyncio.TimeoutError, TimeoutError) as exc:
        proc.kill()
        await proc.wait()
        raise TimeoutError("git clone timed out") from exc
    return proc.returncode if proc.returncode is not None else 1


def _resolve_npm_tarball(metadata: Any, version: str | None) -> tuple[str | None, str | None]:
    if not isinstance(metadata, Mapping):
        return None, None
    versions = metadata.get("versions")
    if not isinstance(versions, Mapping):
        return None, None
    resolved = version
    if not resolved:
        dist_tags = metadata.get("dist-tags")
        resolved = dist_tags.get("latest") if isinstance(dist_tags, Mapping) else None
    if not resolved or resolved not in versions:
        return None, None
    version_meta = versions[resolved]
    dist = version_meta.get("dist") if isinstance(version_meta, Mapping) else None
    tarball = dist.get("tarball") if isinstance(dist, Mapping) else None
    return str(resolved), (str(tarball) if tarball else None)


def _extract_npm_tarball(
    data: bytes,
    *,
    max_total_bytes: int,
    max_files: int,
) -> tuple[list[dict[str, str]], list[dict[str, Any]]]:
    files: list[dict[str, str]] = []
    notes: list[dict[str, Any]] = []
    total_bytes = 0
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
            for member in tar:
                if len(files) >= max_files:
                    notes.append({"code": "npm_file_count_exceeded", "max_files": max_files})
                    break
                if member.issym() or member.islnk():
                    notes.append({"code": "materialized_symlink_rejected", "path": member.name})
                    continue
                if not member.isfile():
                    continue
                safe = _safe_relative_path(_strip_npm_prefix(member.name))
                if safe is None:
                    notes.append({"code": "materialized_path_escape", "path": member.name})
                    continue
                if member.size < 0 or total_bytes + member.size > max_total_bytes:
                    notes.append({"code": "npm_total_size_exceeded", "max_total_bytes": max_total_bytes})
                    break
                total_bytes += member.size
                extracted = tar.extractfile(member)
                content = extracted.read().decode("utf-8", errors="replace") if extracted is not None else ""
                files.append({"path": safe, "content": content})
    except (tarfile.TarError, OSError, EOFError) as exc:
        notes.append({"code": "npm_tarball_unreadable", "message": str(exc)})
    return files, notes


def _strip_npm_prefix(name: str) -> str:
    normalized = name.replace("\\", "/").lstrip("/")
    if normalized.startswith("package/"):
        return normalized[len("package/") :]
    return normalized


def _valid_npm_package_name(name: str) -> bool:
    if ".." in name or "//" in name:
        return False
    return bool(_NPM_SCOPED_NAME.match(name) or _NPM_REGULAR_NAME.match(name))


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
            f"https://api.github.com/repos/{github['owner']}/{github['repo']}/contents/{path}?ref={github['branch']}"
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
        raise _RemoteMaterializationBlocked({"code": "remote_total_size_exceeded", "max_total_bytes": max_total_bytes})
    if max_files > MAX_REMOTE_PACKAGE_FILES:
        raise _RemoteMaterializationBlocked(
            {"code": "remote_file_count_exceeded", "max_files": MAX_REMOTE_PACKAGE_FILES}
        )


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


async def _default_fetch_bytes(url: str, headers: Mapping[str, str]) -> bytes:
    async with httpx.AsyncClient(timeout=30, headers=dict(headers)) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.content


def _files_sha256(files: Sequence[Mapping[str, str]]) -> str:
    payload = [
        {"path": item["path"], "content": item["content"]} for item in sorted(files, key=lambda item: item["path"])
    ]
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
