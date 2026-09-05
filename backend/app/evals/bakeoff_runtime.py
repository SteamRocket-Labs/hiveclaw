"""Live bakeoff adapters for external agent runtimes."""

from __future__ import annotations

import json
import ast
import base64
import hashlib
import ipaddress
import marshal
import os
import platform
import re
import shlex
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import tarfile
import tomllib
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from pathlib import PurePosixPath
from shutil import which as _which
from time import monotonic, sleep, time
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from app.services.exact_secret_boundary import ExactSecretBoundary
from app.services.subprocess_env import build_agent_subprocess_env


_SCENARIOS = (
    "coding",
    "review",
    "research",
    "operations",
    "delegation",
    "memory_recall",
    "self_evolution",
    "long_context_after_compaction",
)
_RUNTIME_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "answer": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "files_created": {"type": "array", "items": {"type": "string"}},
        "used_parallelism": {"type": "boolean"},
        "notes": {"type": "string"},
    },
    "required": ["status", "answer", "evidence", "files_created", "used_parallelism", "notes"],
    "additionalProperties": False,
}

J4_ENVELOPE_SCHEMA = "hive.j4.same_envelope.v1"
J4_RECEIPT_SCHEMA = "hive.j4.runtime_receipt.v1"
J4_MODEL = "gpt-5.4"
J4_REASONING_EFFORT = "low"
J4_STATUSES = {
    "completed",
    "auth_required",
    "authority_denied",
    "cli_unavailable",
    "model_unavailable",
    "resource_unavailable",
    "sandbox_unavailable",
    "timeout",
    "cancelled",
    "needs_reconciliation",
    "failed",
    "invalid_output",
    "attestation_failed",
}
FREECODE_BUILD_MANIFEST_SCHEMA = "hive.j4.freecode_build_manifest.v2"
FREECODE_BINARY_ENV = "HIVE_J4_FREECODE_BINARY"
FREECODE_CODEX_PROVIDER_CONTRACT = "freecode.codex-fetch-adapter.CLAUDE_CODE_USE_OPENAI.v1"
FREECODE_J4_HOOK = Path(__file__).with_name("freecode_j4_hook.py")
HERMES_J4_LAUNCHER = Path(__file__).with_name("hermes_j4_launcher.py")
HERMES_J4_SOURCE_ROOT_ENV = "HIVE_J4_HERMES_SOURCE_ROOT"
HERMES_J4_STATE_DB_ENV = "HIVE_J4_HERMES_STATE_DB"
HERMES_J4_SITE_PACKAGES_ENV = "HIVE_J4_HERMES_SITE_PACKAGES"
HERMES_J4_WORKSPACE_ROOT_ENV = "HIVE_J4_WORKSPACE_ROOT"
HERMES_J4_SOURCE_SCOPE = "all_worktree_bytes_except_git_and_validated_runtime_roots"
_HERMES_J4_AMBIENT_ENV_DENYLIST = (
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
    "REQUESTS_CA_BUNDLE",
    "NODE_EXTRA_CA_CERTS",
    "NPM_CONFIG_REGISTRY",
    "PIP_INDEX_URL",
    "PIP_EXTRA_INDEX_URL",
)
_J4_RUNTIME_ORDER = ("hive", "freecode", "hermes")
_J4_ALLOWED_TOOLS = {
    "hive": ("read_file", "write_file", "edit_file", "glob_search", "grep_search"),
    "freecode": ("Read", "Write", "Edit", "Glob", "Grep"),
    "hermes": ("read_file", "write_file", "patch", "search_files"),
}
_J4_OUTPUT_FILES = {
    "coding": ("calculator.py",),
    "review": ("review.md",),
    "research": ("research_summary.md",),
    "operations": ("ops_summary.md",),
    "delegation": ("delegation_plan.md",),
    "memory_recall": ("memory_answer.md",),
    "self_evolution": ("self_evolution.md",),
    "long_context_after_compaction": ("long_context_answer.md",),
}
_J4_CRITERIA = {
    "coding": ("coding.ast_add", "coding.execution_assertions"),
    "review": ("review.priority", "review.file_ref", "review.issue"),
    "research": ("research.winner", "research.date", "research.source_refs"),
    "operations": ("operations.root_cause", "operations.staging_verification"),
    "delegation": ("delegation.coverage", "delegation.mode", "delegation.fallback"),
    "memory_recall": ("memory.exact_bytes",),
    "self_evolution": ("self_evolution.repeated_evidence", "self_evolution.skill_decision"),
    "long_context_after_compaction": ("long_context.exact_bytes",),
}


@dataclass(frozen=True, slots=True)
class J4RuntimeConfig:
    """Explicit authority and resource inputs for one manual P08-J4 run."""

    hive_base_url: str | None = None
    hive_bearer: str | None = field(default=None, repr=False)
    hive_agent_id: str | None = None
    hive_revision: str | None = None
    hive_binary_sha256: str | None = None
    freecode_build_manifest: str | None = None
    freecode_build_manifest_sha256: str | None = None
    hermes_python: str | None = None
    hermes_python_sha256: str | None = None
    hermes_python_environment_sha256: str | None = None
    hermes_source_root: str | None = None
    hermes_source_revision: str | None = None
    hermes_source_sha256: str | None = None
    hermes_freeze_root: str | None = None
    hermes_auth_store: str | None = None
    hermes_auth_store_sha256: str | None = None
    external_profile_authorized: bool = False
    require_same_credential_domain: bool = False
    max_tool_rounds: int = 6
    wall_clock_seconds: int = 120
    cancel_fence_seconds: int = 10
    max_budget_usd: float = 2.0
    max_files: int = 64
    max_bytes: int = 1_000_000
    http_client: Any | None = field(default=None, repr=False, compare=False)


@dataclass(frozen=True, slots=True)
class PreparedJ4Runtimes:
    """Frozen, locally attested runtime inputs shared by every J4 scenario."""

    freecode_manifest: dict[str, Any]
    freecode_input_manifest: dict[str, Any]
    freecode_manifest_sha256: str
    freecode_build_receipt: dict[str, Any]
    freecode_build_receipt_sha256: str
    freecode_hook: Path
    freecode_hook_sha256: str
    freecode_hook_python: Path
    freecode_hook_python_sha256: str
    freecode_hook_python_environment_sha256: str
    hermes_binary: dict[str, Any]
    hermes_python: Path
    hermes_venv_python: Path
    hermes_base_python_root: Path
    hermes_launcher: Path
    hermes_source_root: Path
    hermes_source_paths: frozenset[str] = field(repr=False, compare=False)
    hermes_site_packages: Path
    hermes_auth_projection: dict[str, Any] = field(repr=False, compare=False)
    hermes_auth_profile: dict[str, Any] = field(repr=False, compare=False)
    hermes_auth_source: Path = field(repr=False, compare=False)
    hermes_auth_source_sha256: str = field(repr=False, compare=False)
    hermes_auth_projection_sha256: str = field(repr=False, compare=False)
    hermes_auth_run_nonce: bytes = field(repr=False, compare=False)
    cleanup_root: Path | None = field(default=None, repr=False, compare=False)
    cleanup_handle: Any | None = field(default=None, repr=False, compare=False)


@dataclass(slots=True)
class ProcessRunResult:
    command: list[str]
    cwd: str
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    sandbox: dict[str, Any] | None = None


def _freecode_binary() -> Path | None:
    """Resolve the legacy standalone bakeoff CLI; formal J4 never uses this path."""

    configured = str(os.environ.get(FREECODE_BINARY_ENV) or "").strip()
    discovered = configured or _which("freecode")
    return Path(discovered).expanduser().resolve() if discovered else None


@dataclass(slots=True)
class ScenarioWorkspace:
    name: str
    workspace_dir: Path
    prompt: str
    rubric: str


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    max_turns: int
    timeout_seconds: int
    preflight_timeout_seconds: int = 20


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_sha256_file(path: Path) -> str:
    try:
        return _sha256_file(path)
    except OSError:
        return ""


def _tree_identity(
    root: Path,
    *,
    allowed_external_python_root: Path | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Hash every filesystem entry below ``root`` without following symlinks."""

    resolved = root.expanduser().resolve()
    identity: dict[str, Any] = {
        "root": str(resolved),
        "sha256": "",
        "entry_count": 0,
        "total_bytes": 0,
    }
    if not resolved.is_dir():
        return identity, ["tree_unavailable"]
    entries: list[dict[str, Any]] = []
    errors: list[str] = []
    for current_root, directory_names, file_names in os.walk(resolved, followlinks=False):
        current = Path(current_root)
        symlink_directories = sorted(name for name in directory_names if (current / name).is_symlink())
        directory_names[:] = sorted(name for name in directory_names if name not in symlink_directories)
        relative_root = current.relative_to(resolved)
        for name in [*directory_names, *symlink_directories, *sorted(file_names)]:
            path = current / name
            relative = (relative_root / name).as_posix() if relative_root != Path(".") else name
            try:
                details = path.lstat()
                mode = f"{stat.S_IMODE(details.st_mode):04o}"
                resolved_target_sha256: str | None = None
                if stat.S_ISDIR(details.st_mode):
                    entries.append({"path": relative, "kind": "directory", "mode": mode})
                    continue
                if stat.S_ISLNK(details.st_mode):
                    target = os.readlink(path)
                    try:
                        path.resolve(strict=True).relative_to(resolved)
                    except (OSError, ValueError):
                        allowed_root = (
                            allowed_external_python_root.resolve(strict=True)
                            if allowed_external_python_root is not None
                            else None
                        )
                        python_entry = bool(
                            re.fullmatch(
                                r"(?:bin|Scripts)/python(?:\d+(?:\.\d+)*)?(?:\.exe)?",
                                relative,
                                flags=re.IGNORECASE,
                            )
                        )
                        try:
                            resolved_target = path.resolve(strict=True)
                            if allowed_root is None or not python_entry:
                                raise ValueError
                            resolved_target.relative_to(allowed_root)
                            if not resolved_target.is_file():
                                raise ValueError
                            resolved_target_sha256 = _sha256_file(resolved_target)
                        except (OSError, ValueError):
                            errors.append(f"tree_external_symlink:{relative}")
                    payload = target.encode("utf-8")
                    kind = "symlink"
                    size = len(payload)
                    digest = _sha256_bytes(payload)
                elif stat.S_ISREG(details.st_mode):
                    kind = "file"
                    size = details.st_size
                    digest = _sha256_file(path)
                else:
                    errors.append(f"tree_unsupported_entry:{relative}")
                    continue
            except OSError:
                errors.append(f"tree_unreadable:{relative}")
                continue
            entry = {
                "path": relative,
                "kind": kind,
                "mode": mode,
                "size": size,
                "sha256": digest,
            }
            if resolved_target_sha256 is not None:
                entry["resolved_target_sha256"] = resolved_target_sha256
            entries.append(entry)
    identity["entry_count"] = len(entries)
    identity["total_bytes"] = sum(int(entry.get("size") or 0) for entry in entries)
    errors = sorted(set(errors))
    if not errors:
        identity["sha256"] = _sha256_json(entries)
    return identity, errors


def _trusted_command(command: list[str], *, cwd: Path) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(  # noqa: S603
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=20,
            env=build_agent_subprocess_env(home=Path.home()),
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None


def _load_freecode_build_manifest(config: J4RuntimeConfig) -> tuple[dict[str, Any], list[str]]:
    """Validate owner-pinned FreeCode provenance without executing the artifact."""

    configured_path = str(config.freecode_build_manifest or "").strip()
    expected_manifest_sha = str(config.freecode_build_manifest_sha256 or "").strip().lower()
    if not configured_path or not re.fullmatch(r"[0-9a-f]{64}", expected_manifest_sha):
        return {}, ["freecode_build_manifest_attestation_required"]
    path = Path(configured_path).expanduser()
    try:
        path_stat = path.lstat()
        if not stat.S_ISREG(path_stat.st_mode) or path.is_symlink():
            return {}, ["freecode_build_manifest_unsupported_entry"]
        raw = path.read_bytes()
    except OSError:
        return {}, ["freecode_build_manifest_unavailable"]
    if _sha256_bytes(raw) != expected_manifest_sha:
        return {}, ["freecode_build_manifest_sha256_mismatch"]
    try:
        manifest = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}, ["freecode_build_manifest_invalid"]
    if not isinstance(manifest, dict) or manifest.get("schema") != FREECODE_BUILD_MANIFEST_SCHEMA:
        return {}, ["freecode_build_manifest_invalid"]

    source = manifest.get("source") if isinstance(manifest.get("source"), dict) else {}
    build = manifest.get("build") if isinstance(manifest.get("build"), dict) else {}
    errors: list[str] = []
    source_root = Path(str(source.get("root") or "")).expanduser()
    revision = str(source.get("revision") or "").strip().lower()
    tree = str(source.get("tree") or "").strip().lower()
    if not source_root.is_absolute() or not source_root.is_dir():
        errors.append("freecode_source_unavailable")
    else:
        head = _trusted_command(["git", "-C", str(source_root), "rev-parse", "HEAD"], cwd=source_root)
        observed_tree = _trusted_command(["git", "-C", str(source_root), "rev-parse", "HEAD^{tree}"], cwd=source_root)
        status_result = _trusted_command(
            ["git", "-C", str(source_root), "status", "--porcelain=v1", "-z", "--untracked-files=no"],
            cwd=source_root,
        )
        if head is None or head.returncode != 0 or head.stdout.strip().lower() != revision:
            errors.append("freecode_source_revision_mismatch")
        if observed_tree is None or observed_tree.returncode != 0 or observed_tree.stdout.strip().lower() != tree:
            errors.append("freecode_source_tree_mismatch")
        if status_result is None or status_result.returncode != 0 or status_result.stdout:
            errors.append("freecode_source_dirty")
        observed_source_sha = _sha256_json({"revision": revision, "tree": tree})
        if observed_source_sha != str(source.get("sha256") or "").strip().lower():
            errors.append("freecode_source_sha256_mismatch")
        for relative, field_name in (
            ("bun.lock", "lock_sha256"),
            ("package.json", "package_sha256"),
            ("scripts/build.ts", "build_script_sha256"),
        ):
            candidate = source_root / relative
            if not candidate.is_file() or _safe_sha256_file(candidate) != str(source.get(field_name) or "").lower():
                errors.append(f"freecode_source_{field_name}_mismatch")
        try:
            package = json.loads((source_root / "package.json").read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            package = {}
        if not isinstance(package, dict) or not str(package.get("version") or "").strip():
            errors.append("freecode_source_version_unavailable")
        tracked_result = _trusted_command(
            ["git", "-C", str(source_root), "ls-files", "-z"],
            cwd=source_root,
        )
        tracked = (
            {value for value in tracked_result.stdout.split("\0") if value}
            if tracked_result is not None and tracked_result.returncode == 0
            else set()
        )
        content_sha256, content_errors = _source_manifest_digest(source_root, tracked)
        if not tracked or content_errors or content_sha256 != str(source.get("content_sha256") or "").strip().lower():
            errors.append("freecode_source_content_sha256_mismatch")

    bun_path = Path(str(build.get("bun_path") or "")).expanduser()
    try:
        bun_stat = bun_path.lstat()
    except OSError:
        bun_stat = None
    bun_identity_valid = not (
        not bun_path.is_absolute()
        or bun_stat is None
        or not stat.S_ISREG(bun_stat.st_mode)
        or bun_path.is_symlink()
        or _safe_sha256_file(bun_path) != str(build.get("bun_sha256") or "").lower()
    )
    if not bun_identity_valid:
        errors.append("freecode_build_bun_identity_mismatch")
    argv = build.get("argv")
    expected_argv = [str(bun_path), "run", "./scripts/build.ts"]
    if argv != expected_argv:
        errors.append("freecode_build_argv_invalid")
    if not str(build.get("bun_version") or "").strip():
        errors.append("freecode_build_bun_version_required")
    elif bun_identity_valid:
        observed_bun = _trusted_command([str(bun_path), "--version"], cwd=source_root)
        if (
            observed_bun is None
            or observed_bun.returncode != 0
            or observed_bun.stdout.strip() != str(build["bun_version"]).strip()
        ):
            errors.append("freecode_build_bun_version_mismatch")
    if str(build.get("os") or "").strip().lower() != platform.system().lower():
        errors.append("freecode_build_os_mismatch")
    if str(build.get("arch") or "").strip().lower() != platform.machine().lower():
        errors.append("freecode_build_arch_mismatch")
    if build.get("attestation") != "harness-fresh-build-request":
        errors.append("freecode_build_request_attestation_required")
    dependencies_root = Path(str(build.get("dependencies_root") or "")).expanduser()
    if not dependencies_root.is_absolute() or dependencies_root != source_root / "node_modules":
        errors.append("freecode_build_dependencies_root_invalid")
    else:
        dependencies_identity, dependencies_errors = _tree_identity(dependencies_root)
        if dependencies_errors:
            errors.append("freecode_build_dependencies_unavailable")
        elif (
            dependencies_identity.get("sha256") != str(build.get("dependencies_sha256") or "").strip().lower()
            or dependencies_identity.get("entry_count") != build.get("dependencies_entry_count")
            or dependencies_identity.get("total_bytes") != build.get("dependencies_total_bytes")
        ):
            errors.append("freecode_build_dependencies_identity_mismatch")
    return manifest, sorted(set(errors))


def _jwt_expiry(value: str) -> int | None:
    parts = value.split(".")
    if len(parts) != 3:
        return None
    try:
        payload = json.loads(base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)).decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return None
    expires_at = payload.get("exp") if isinstance(payload, dict) else None
    return expires_at if isinstance(expires_at, int) and not isinstance(expires_at, bool) else None


def _hermes_auth_projection(pool_entry: dict[str, Any]) -> dict[str, Any]:
    """Expose one non-refreshable Codex credential and no unrelated profile material."""

    selected = {
        "id": "p08-j4-selected",
        "auth_type": "oauth",
        "priority": 0,
        "source": "manual:p08-j4",
        "access_token": pool_entry["access_token"],
    }
    return {
        "version": 1,
        "providers": {},
        "credential_pool": {"openai-codex": [selected]},
        "active_provider": "openai-codex",
    }


def _hermes_auth_secret_boundary(projection: dict[str, Any]) -> ExactSecretBoundary:
    pool = projection.get("credential_pool")
    entries = pool.get("openai-codex") if isinstance(pool, dict) else None
    entry = entries[0] if isinstance(entries, list) and len(entries) == 1 else {}
    return ExactSecretBoundary.from_pairs(
        (f"hermes_auth.{name}", str(entry.get(name) or "")) for name in ("access_token", "refresh_token")
    )


def _read_private_regular_file(path: Path) -> bytes | None:
    """Read a bounded current-user private regular file without following its leaf."""

    descriptor: int | None = None
    try:
        if path.resolve(strict=True) != path:
            return None
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        details = os.fstat(descriptor)
        if (
            not stat.S_ISREG(details.st_mode)
            or (hasattr(os, "getuid") and details.st_uid != os.getuid())
            or stat.S_IMODE(details.st_mode) & (stat.S_IRWXG | stat.S_IRWXO)
        ):
            return None
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > 16 * 1024 * 1024:
                return None
            chunks.append(chunk)
        return b"".join(chunks)
    except OSError:
        return None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _private_regular_file_sha256(path: Path) -> str | None:
    payload = _read_private_regular_file(path)
    return _sha256_bytes(payload) if payload is not None else None


def _load_hermes_auth_profile(
    config: J4RuntimeConfig,
) -> tuple[dict[str, Any], dict[str, Any], Path | None, list[str]]:
    configured = str(config.hermes_auth_store or "").strip()
    expected_sha256 = str(config.hermes_auth_store_sha256 or "").strip().lower()
    if not configured or not re.fullmatch(r"[0-9a-f]{64}", expected_sha256):
        return {}, {}, None, ["hermes_auth_store_attestation_required"]
    path = Path(os.path.abspath(Path(configured).expanduser()))
    descriptor: int | None = None
    try:
        resolved = path.resolve(strict=True)
        if resolved != path:
            return {}, {}, None, ["hermes_auth_store_symlink_boundary"]
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        details = os.fstat(descriptor)
        if not stat.S_ISREG(details.st_mode):
            return {}, {}, None, ["hermes_auth_store_unsupported_entry"]
        if hasattr(os, "getuid") and details.st_uid != os.getuid():
            return {}, {}, None, ["hermes_auth_store_owner_mismatch"]
        if stat.S_IMODE(details.st_mode) & (stat.S_IRWXG | stat.S_IRWXO):
            return {}, {}, None, ["hermes_auth_store_permissions_too_broad"]
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 1024 * 1024)
            if not chunk:
                break
            total += len(chunk)
            if total > 16 * 1024 * 1024:
                return {}, {}, None, ["hermes_auth_store_too_large"]
            chunks.append(chunk)
        raw = b"".join(chunks)
    except OSError:
        return {}, {}, None, ["hermes_auth_store_unavailable"]
    finally:
        if descriptor is not None:
            os.close(descriptor)
    if _sha256_bytes(raw) != expected_sha256:
        return {}, {}, None, ["hermes_auth_store_sha256_mismatch"]
    try:
        auth_store = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return {}, {}, None, ["hermes_auth_store_invalid"]
    providers = auth_store.get("providers") if isinstance(auth_store, dict) else None
    provider = providers.get("openai-codex") if isinstance(providers, dict) else None
    credential_pool = auth_store.get("credential_pool") if isinstance(auth_store, dict) else None
    pool = credential_pool.get("openai-codex") if isinstance(credential_pool, dict) else None
    selection_exact = isinstance(pool, list) and len(pool) == 1 and isinstance(pool[0], dict)
    selected = pool[0] if selection_exact else {}
    access_token = selected.get("access_token")
    expires_at = _jwt_expiry(access_token) if isinstance(access_token, str) else None
    required_lifetime = config.wall_clock_seconds * len(_SCENARIOS) * len(_J4_RUNTIME_ORDER) + 300
    errors: list[str] = []
    if not isinstance(auth_store, dict) or auth_store.get("active_provider") != "openai-codex":
        errors.append("hermes_auth_store_active_provider_mismatch")
    if not isinstance(access_token, str) or not access_token.strip() or expires_at is None:
        errors.append("hermes_codex_access_token_unavailable")
    elif expires_at <= int(time()) + required_lifetime:
        errors.append("hermes_codex_access_token_window_insufficient")
    if not selection_exact:
        errors.append("hermes_codex_credential_selection_ambiguous")
    endpoint_values = [provider.get("base_url") if isinstance(provider, dict) else None, selected.get("base_url")]
    if any(
        str(value).rstrip("/") != "https://chatgpt.com/backend-api/codex"
        for value in endpoint_values
        if value not in (None, "")
    ):
        errors.append("hermes_codex_endpoint_mismatch")
    if selected.get("last_status") not in (None, "", "ok"):
        errors.append("hermes_codex_credential_not_usable")
    if selected.get("auth_type") != "oauth":
        errors.append("hermes_codex_auth_type_mismatch")
    if errors:
        return {}, {}, None, sorted(set(errors))
    projection = _hermes_auth_projection(selected)
    return (
        {
            "provider": "openai-codex",
            "source_attested": True,
            "expires_at": expires_at,
            "refresh_capable": False,
            "credential_count": 1,
        },
        projection,
        path,
        [],
    )


def _safe_relative_path(value: str) -> str:
    path = PurePosixPath(str(value).replace("\\", "/"))
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"Unsafe workspace path: {value!r}")
    return path.as_posix()


def _manifest(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    manifest: list[dict[str, Any]] = []
    errors: list[str] = []
    if not root.exists():
        return manifest, ["workspace_missing"]
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            errors.append(f"symlink:{path.relative_to(root).as_posix()}")
            continue
        if not path.is_file():
            continue
        relative = _safe_relative_path(path.relative_to(root).as_posix())
        payload = path.read_bytes()
        manifest.append(
            {
                "path": relative,
                "sha256": _sha256_bytes(payload),
                "size": len(payload),
                "mode": f"{stat.S_IMODE(path.stat().st_mode):04o}",
            }
        )
    paths = [entry["path"] for entry in manifest]
    if len(paths) != len(set(paths)):
        errors.append("duplicate_path")
    return manifest, errors


def _workspace_diff(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[dict[str, Any]]:
    before_by_path = {entry["path"]: entry for entry in before}
    after_by_path = {entry["path"]: entry for entry in after}
    changes: list[dict[str, Any]] = []
    for path in sorted(set(before_by_path) | set(after_by_path)):
        previous = before_by_path.get(path)
        current = after_by_path.get(path)
        if previous == current:
            continue
        changes.append(
            {
                "path": path,
                "change": "created" if previous is None else "deleted" if current is None else "modified",
                "before_sha256": previous.get("sha256") if previous else None,
                "after_sha256": current.get("sha256") if current else None,
            }
        )
    return changes


def _clone_seed(
    seed_root: Path,
    workspace_root: Path,
) -> tuple[list[dict[str, Any]], list[str], tuple[int, int] | None]:
    """Clone the seed and return the identity of the workspace actually created.

    The (st_dev, st_ino) identity lets every later evidence or scoring
    consumer prove it is reading the directory this clone created, not a
    same-path replacement; None means the identity could not be observed.
    """

    workspace_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(seed_root, workspace_root, copy_function=shutil.copy2)
    try:
        details = workspace_root.lstat()
    except OSError:
        return _manifest(workspace_root) + (None,)
    return (*_manifest(workspace_root), (details.st_dev, details.st_ino))


def _scorer_source_sha256() -> str:
    return _sha256_file(Path(__file__).resolve())


def _scorer_runtime_identity() -> dict[str, Any]:
    """Bind the loaded scorer implementation to the source frozen for this run."""

    loaded_functions = {
        name: _sha256_bytes(marshal.dumps(function.__code__))
        for name, function in (
            ("_safe_read", _safe_read),
            ("_eval_integer_expression", _eval_integer_expression),
            ("_external_score", _external_score),
        )
    }
    return {
        "source_sha256": _scorer_source_sha256(),
        "loaded_code_sha256": _sha256_json(loaded_functions),
        "loaded_functions": loaded_functions,
    }


def _freeze_scorer_source(
    output_dir: Path,
    identity: dict[str, Any],
) -> tuple[Path | None, list[str]]:
    source = Path(__file__).resolve()
    destination = output_dir / "j4_scoring" / "scorer" / "bakeoff_runtime.py"
    claim_error = _claim_owned_output_subtree(output_dir, ("j4_scoring", "scorer"))
    if claim_error == "unsupported":
        return None, ["scorer_boundary_unsupported"]
    if claim_error is not None:
        return None, ["scorer_boundary_unavailable"]
    try:
        if destination.exists():
            if destination.is_symlink() or not destination.is_file():
                return None, ["scorer_snapshot_unsupported_entry"]
        else:
            shutil.copy2(source, destination)
    except (OSError, shutil.Error):
        return None, ["scorer_snapshot_failed"]
    if _safe_sha256_file(destination) != identity.get("source_sha256"):
        return None, ["scorer_snapshot_mismatch"]
    return destination, []


def _scorer_runtime_stable(identity: dict[str, Any], frozen_source: Path) -> bool:
    return (
        _scorer_runtime_identity() == identity
        and not frozen_source.is_symlink()
        and _safe_sha256_file(frozen_source) == identity.get("source_sha256")
    )


def _runtime_workspace_path(output_dir: Path, runtime: str, envelope_id: str) -> Path:
    return output_dir / "j4_runtime" / runtime / envelope_id / "workspace"


def _claim_owned_output_subtree(output_dir: Path, levels: tuple[str, ...]) -> str | None:
    """Claim every level of an output subtree below the trusted output root.

    Each pre-existing level must be a real directory (lstat; a symlink or any
    non-directory entry is refused) and missing levels are created with
    mode 0o700. Returns None when the whole chain is owned, "unsupported"
    when a level is a symlink or non-directory, and "unavailable" when a
    filesystem error blocks the claim.
    """

    parent = output_dir
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        return "unavailable"
    for name in levels:
        candidate = parent / name
        try:
            candidate.mkdir(mode=0o700)
        except FileExistsError:
            try:
                details = candidate.lstat()
            except OSError:
                return "unavailable"
            if candidate.is_symlink() or not stat.S_ISDIR(details.st_mode):
                return "unsupported"
        except OSError:
            return "unavailable"
        parent = candidate
    return None


def _claim_j4_attempt_root(
    output_dir: Path,
    runtime: str,
    envelope_id: str,
) -> tuple[Path, tuple[int, int] | None, str | None]:
    """Claim an exclusive attempt root beneath the owner-controlled output_dir.

    Boundary contract, stated honestly: the lstat checks are conflict
    detection against an owner mistake or a stale prior run inside the
    trusted owner-controlled output root, not a sandbox against a hostile
    same-UID writer — such a writer can swap a level between these checks
    and the mkdir/lstat below, and closing that window would require
    fd-relative mkdir/O_NOFOLLOW traversal that is deliberately not built.
    Cleanup identity-checks only the roots this attempt directly created
    (the attempt root and its runtime/state children); any other content
    found beneath an owned root is removed recursively by path with the
    standard-library symlink-safe rmtree, and a pre-existing or replaced
    root is preserved and reported as a typed reconciliation instead of
    being deleted or trusted.
    """

    fallback_root = output_dir / "j4_runtime" / runtime / envelope_id
    parent_error = _claim_owned_output_subtree(output_dir, ("j4_runtime", runtime))
    if parent_error == "unsupported":
        return fallback_root, None, "attempt_parent_unsupported"
    if parent_error is not None:
        return fallback_root, None, "attempt_parent_unavailable"
    attempt_root = output_dir / "j4_runtime" / runtime / envelope_id
    try:
        attempt_root.mkdir(mode=0o700)
        details = attempt_root.lstat()
    except FileExistsError:
        return attempt_root, None, "attempt_root_conflict"
    except OSError:
        return attempt_root, None, "attempt_root_unavailable"
    if attempt_root.is_symlink() or not stat.S_ISDIR(details.st_mode):
        return attempt_root, None, "attempt_root_unsupported"
    return attempt_root, (details.st_dev, details.st_ino), None


def _j4_attempt_root_owned(attempt_root: Path, identity: tuple[int, int]) -> bool:
    try:
        details = attempt_root.lstat()
    except OSError:
        return False
    return (
        not attempt_root.is_symlink() and stat.S_ISDIR(details.st_mode) and (details.st_dev, details.st_ino) == identity
    )


def _remove_owned_j4_root(root: Path, identity: tuple[int, int]) -> str:
    """Delete an attempt child root only while it is still exactly the directory
    this attempt created. A replaced or unidentifiable path is preserved and
    reported as "ambiguous"; an owned path that survives rmtree is "failed"."""

    if not _j4_attempt_root_owned(root, identity):
        return "ambiguous"
    shutil.rmtree(root, ignore_errors=True)
    if root.exists() or root.is_symlink():
        return "failed"
    return "removed"


def _hermes_state_root(workspace_root: Path, envelope_id: str) -> Path:
    """Return the state root owned only by this envelope's Hermes attempt."""

    attempt_root = workspace_root.parent
    if attempt_root.name != envelope_id:
        raise ValueError("Hermes state root must be anchored to the exact envelope attempt")
    return attempt_root / "state"


def _initialize_hermes_attempt_home(
    state_root: Path,
    prepared: PreparedJ4Runtimes,
) -> tuple[dict[str, str], list[str]]:
    directories = {
        "HERMES_HOME": state_root / "hermes-home",
        "HOME": state_root / "os-home",
        "CODEX_HOME": state_root / "codex-home",
        "HERMES_MANAGED_DIR": state_root / "managed",
        "TMPDIR": state_root / "tmp",
    }
    try:
        for directory in directories.values():
            directory.mkdir(mode=0o700)
        projection = prepared.hermes_auth_projection
        encoded = (_canonical_json(projection) + "\n").encode("utf-8")
        auth_path = directories["HERMES_HOME"] / "auth.json"
        descriptor = os.open(
            auth_path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=False) as handle:
                handle.write(encoded)
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            os.close(descriptor)
    except OSError:
        return {}, ["hermes_attempt_home_initialization_failed"]
    if _sha256_json(projection) != prepared.hermes_auth_projection_sha256:
        return {}, ["hermes_attempt_auth_projection_mismatch"]
    temporary = str(directories["TMPDIR"].resolve())
    return (
        {
            **{name: str(path.resolve()) for name, path in directories.items()},
            "TMP": temporary,
            "TEMP": temporary,
            "HERMES_SAFE_MODE": "1",
            "HERMES_IGNORE_USER_CONFIG": "1",
            "HERMES_IGNORE_RULES": "1",
            "HERMES_BUNDLED_SKILLS": str((state_root / "nonexistent-bundled-skills").resolve()),
            "PYTHONNOUSERSITE": "1",
        },
        [],
    )


def _hermes_auth_credential_invariant(payload: Any) -> dict[str, Any] | None:
    if not isinstance(payload, dict):
        return None
    if set(payload) != {"version", "providers", "credential_pool", "active_provider"}:
        return None
    providers = payload.get("providers")
    pool = payload.get("credential_pool")
    if not isinstance(pool, dict) or set(pool) != {"openai-codex"}:
        return None
    entries = pool.get("openai-codex") if isinstance(pool, dict) else None
    if not isinstance(providers, dict) or providers or not isinstance(entries, list) or len(entries) != 1:
        return None
    entry = entries[0]
    if not isinstance(entry, dict):
        return None
    invariant_fields = {
        "id",
        "auth_type",
        "priority",
        "source",
        "access_token",
        "base_url",
        "expires_at",
        "expires_at_ms",
    }
    mutable_fields = {
        "label",
        "last_status",
        "last_status_at",
        "last_error_code",
        "last_error_reason",
        "last_error_message",
        "last_error_reset_at",
        "request_count",
        "failure_reason",
    }
    if set(entry) - invariant_fields - mutable_fields or "refresh_token" in entry:
        return None
    if entry.get("label") not in (None, "manual:p08-j4"):
        return None
    return {
        "version": payload.get("version"),
        "active_provider": payload.get("active_provider"),
        "providers": providers,
        "credential": {name: entry.get(name) for name in sorted(invariant_fields)},
    }


def _hermes_attempt_state_attestation(
    state_root: Path,
    prepared: PreparedJ4Runtimes,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate attempt-local credential invariants and return a secret-safe manifest."""

    errors: list[str] = []
    auth_path = state_root / "hermes-home" / "auth.json"
    raw = _read_private_regular_file(auth_path)
    try:
        observed_auth = json.loads(raw) if raw is not None else None
    except (UnicodeDecodeError, json.JSONDecodeError):
        observed_auth = None
    if _hermes_auth_credential_invariant(observed_auth) != _hermes_auth_credential_invariant(
        prepared.hermes_auth_projection
    ):
        errors.append("hermes_attempt_auth_credential_drift")

    manifest, manifest_errors = _manifest(state_root)
    errors.extend(f"state_manifest:{error}" for error in manifest_errors)
    allowed_directories = {"hermes-home", "os-home", "codex-home", "managed", "tmp"}
    for path in sorted(state_root.rglob("*")):
        if path.is_symlink() or not path.is_dir():
            continue
        relative = path.relative_to(state_root).as_posix()
        if (
            relative not in allowed_directories
            and relative != "hermes-home/logs"
            and not relative.startswith("hermes-home/logs/")
        ):
            errors.append(f"hermes_attempt_state_unexpected_directory:{relative}")
    allowed_exact = {"state.db", "hermes-home/auth.json", "hermes-home/auth.lock"}
    for entry in manifest:
        relative = str(entry["path"])
        if relative not in allowed_exact and not relative.startswith("hermes-home/logs/"):
            errors.append(f"hermes_attempt_state_unexpected_path:{relative}")
        if relative == "hermes-home/auth.json":
            entry["sha256"] = prepared.hermes_auth_profile["projection_run_sha256"]
            entry["secret_redacted"] = True
    return manifest, sorted(set(errors))


def _cleanup_hermes_attempt_state(state_root: Path) -> list[str]:
    shutil.rmtree(state_root, ignore_errors=True)
    # lstat-based remainder check: a replacement dangling symlink makes
    # Path.exists() False while the attempt path is still occupied.
    remains = state_root.exists() or state_root.is_symlink()
    return ["hermes_attempt_state_cleanup_failed"] if remains else []


def _build_same_envelope(
    scenario: ScenarioWorkspace,
    *,
    config: J4RuntimeConfig,
    scorer_identity: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], str]:
    base_manifest, errors = _manifest(scenario.workspace_dir)
    if errors:
        raise ValueError(f"Invalid seed workspace: {', '.join(errors)}")
    scorer = scorer_identity or _scorer_runtime_identity()
    scorer_sha256 = str(scorer["source_sha256"])
    identity_seed = {
        "schema": J4_ENVELOPE_SCHEMA,
        "scenario": scenario.name,
        "seed_sha256": _sha256_json(base_manifest),
        "scorer_sha256": scorer_sha256,
    }
    envelope_id = f"p08-j4-{scenario.name}-{_sha256_json(identity_seed)[:16]}"
    scenario.prompt = (
        f"The runtime-provided workspace root is logically named workspace/{envelope_id}.\n"
        "Treat the current workspace root as that directory and read TASK.md there.\n"
        "Treat that directory as the only writable evaluation workspace.\n"
        f"{scenario.prompt}"
    )
    (scenario.workspace_dir / "prompt.txt").write_text(scenario.prompt, encoding="utf-8")
    seed_manifest, errors = _manifest(scenario.workspace_dir)
    if errors:
        raise ValueError(f"Invalid seed workspace: {', '.join(errors)}")
    seed_sha256 = _sha256_json(seed_manifest)
    envelope = {
        "schema": J4_ENVELOPE_SCHEMA,
        "envelope_id": envelope_id,
        "task": {
            "suite": "P08-J4",
            "scenario": scenario.name,
            "prompt_sha256": _sha256_bytes(scenario.prompt.encode("utf-8")),
            "output_schema_sha256": _sha256_json(_RUNTIME_JSON_SCHEMA),
            "rubric_id": f"p08-j4.{scenario.name}.v1",
        },
        "workspace": {
            "logical_root": "workspace",
            "seed_manifest": seed_manifest,
            "seed_sha256": seed_sha256,
            "max_files": config.max_files,
            "max_bytes": config.max_bytes,
            "clone_policy": "immutable_seed_per_runtime",
        },
        "model": {
            "vendor": "openai",
            "model": J4_MODEL,
            "equivalence": "exact_vendor_model_id",
            "reasoning_effort": J4_REASONING_EFFORT,
            "fallback_allowed": False,
            "allowed_provider_routes": {
                "hive": ["openai-response"],
                "freecode": ["chatgpt-codex"],
                "hermes": ["openai-codex"],
            },
        },
        "resources": {
            "max_tool_rounds": config.max_tool_rounds,
            "wall_clock_seconds": config.wall_clock_seconds,
            "hard_common": ["max_tool_rounds", "wall_clock_seconds", "reasoning_effort"],
            "runtime_extra_guards": {"freecode": {"max_budget_usd": config.max_budget_usd}},
        },
        "authority": {
            "readable_scope": "evaluation_workspace_only",
            "writable_scope": "evaluation_workspace_only",
            "allowed_tools": {runtime: list(_J4_ALLOWED_TOOLS[runtime]) for runtime in _J4_RUNTIME_ORDER},
            "network_tools_allowed": False,
            "proof_required": True,
        },
        "scorer": {
            "id": "hive.p08-j4.external",
            "version": "1",
            "source_sha256": scorer_sha256,
            "loaded_code_sha256": scorer["loaded_code_sha256"],
            "criteria_ids": list(_J4_CRITERIA[scenario.name]),
        },
    }
    return envelope, _sha256_json(envelope)


def _ensure_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _runtime_profile(target: str) -> RuntimeProfile:
    if target == "claude_code":
        return RuntimeProfile(max_turns=4, timeout_seconds=60, preflight_timeout_seconds=20)
    if target == "freecode":
        return RuntimeProfile(max_turns=6, timeout_seconds=120, preflight_timeout_seconds=20)
    if target == "hermes_agent":
        return RuntimeProfile(max_turns=6, timeout_seconds=90, preflight_timeout_seconds=20)
    raise ValueError(f"Unsupported bakeoff target: {target}")


def build_runtime_command(target: str, *, prompt: str, workspace_dir: Path, max_turns: int = 4) -> list[str]:
    if target == "claude_code":
        return [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(_RUNTIME_JSON_SCHEMA, separators=(",", ":")),
            "--model",
            "sonnet",
            "--effort",
            "low",
            "--max-turns",
            str(max_turns),
            "--permission-mode",
            "bypassPermissions",
            "--dangerously-skip-permissions",
            "--add-dir",
            str(workspace_dir),
            prompt,
        ]
    if target == "freecode":
        freecode_binary = _freecode_binary()
        return [
            str(freecode_binary or "freecode"),
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            _canonical_json(_RUNTIME_JSON_SCHEMA),
            "--model",
            J4_MODEL,
            "--effort",
            J4_REASONING_EFFORT,
            "--max-turns",
            str(max_turns),
            "--max-budget-usd",
            "2.0",
            "--tools",
            "Read,Write,Edit,Glob,Grep",
            "--permission-mode",
            "bypassPermissions",
            "--dangerously-skip-permissions",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--no-chrome",
            prompt,
        ]
    if target == "hermes_agent":
        return [
            "hermes",
            "chat",
            "-q",
            prompt,
            "-Q",
            "--max-turns",
            str(max_turns),
            "--yolo",
        ]
    raise ValueError(f"Unsupported bakeoff target: {target}")


def _load_json_object(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise ValueError("Runtime output must be exactly one JSON object.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Runtime output must be a JSON object.")
    return payload


def extract_runtime_payload(target: str, raw_output: str) -> dict[str, Any]:
    outer = _load_json_object(raw_output)
    if target in {"claude_code", "freecode"} and isinstance(outer.get("structured_output"), dict):
        return outer["structured_output"]
    if target in {"claude_code", "freecode"} and isinstance(outer.get("result"), str):
        nested = outer["result"].strip()
        if nested.startswith("{"):
            return _load_json_object(nested)
    return outer


def _run_process(
    command: list[str],
    cwd: Path,
    timeout_seconds: int,
    *,
    env_overrides: dict[str, str] | None = None,
    env_remove: tuple[str, ...] = (),
    require_workspace_sandbox: bool = False,
    additional_writable_roots: tuple[Path, ...] = (),
    network_access: bool = True,
) -> ProcessRunResult:
    started = monotonic()
    env = build_agent_subprocess_env(home=Path.home())
    if command and command[0] == "claude":
        # Claude OAuth/keychain auth is unavailable in simple mode, so avoid
        # inheriting or forcing CLAUDE_CODE_SIMPLE during live bakeoffs.
        env.pop("CLAUDE_CODE_SIMPLE", None)
    if env_overrides:
        env.update(env_overrides)
    for name in env_remove:
        env.pop(name, None)
    sandbox: dict[str, Any] | None = None
    cleanup_paths: list[Path] = []
    actual_command = command
    if require_workspace_sandbox:
        if additional_writable_roots:
            actual_command, cleanup_paths, sandbox = _build_workspace_sandbox_command(
                command,
                cwd,
                env,
                additional_writable_roots=additional_writable_roots,
                network_access=network_access,
            )
        elif not network_access:
            actual_command, cleanup_paths, sandbox = _build_workspace_sandbox_command(
                command,
                cwd,
                env,
                network_access=network_access,
            )
        else:
            actual_command, cleanup_paths, sandbox = _build_workspace_sandbox_command(command, cwd, env)
        if actual_command is None:
            return ProcessRunResult(
                command=command,
                cwd=str(cwd),
                returncode=126,
                stdout="",
                stderr=str((sandbox or {}).get("reason") or "Workspace sandbox unavailable"),
                duration_ms=int((monotonic() - started) * 1000),
                sandbox=sandbox,
            )
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen(  # noqa: S603
            actual_command,
            cwd=str(cwd),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
            start_new_session=os.name == "posix",
        )
        try:
            stdout, stderr = process.communicate(timeout=timeout_seconds)
            returncode = process.returncode
        except subprocess.TimeoutExpired as exc:
            partial_stdout = _ensure_text(exc.stdout)
            partial_stderr = _ensure_text(exc.stderr)
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGTERM)
                else:
                    process.terminate()
            except (OSError, ProcessLookupError):
                pass
            try:
                stdout, stderr = process.communicate(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except (OSError, ProcessLookupError):
                    pass
                stdout, stderr = process.communicate()
            stdout = _ensure_text(stdout) or partial_stdout
            stderr = (_ensure_text(stderr) or partial_stderr) + (
                f"\nTimed out after {timeout_seconds} seconds; process group terminated."
            )
            returncode = 124
    except OSError as exc:
        returncode = 127
        stdout = ""
        stderr = str(exc)
    finally:
        for path in cleanup_paths:
            path.unlink(missing_ok=True)
    return ProcessRunResult(
        command=command,
        cwd=str(cwd),
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration_ms=int((monotonic() - started) * 1000),
        sandbox=sandbox,
    )


def _build_workspace_sandbox_command(
    command: list[str],
    cwd: Path,
    env: dict[str, str],
    *,
    additional_writable_roots: tuple[Path, ...] = (),
    network_access: bool = True,
) -> tuple[list[str] | None, list[Path], dict[str, Any]]:
    """Build a real workspace-write OS sandbox or fail closed.

    Provider transport still needs network access, while the model-visible tool
    surface is separately restricted to file tools by each adapter.
    """

    from app.runtime.ccplus_contracts import SandboxProfile
    from app.services.subprocess_sandbox import (
        SandboxBuildSpec,
        build_sandboxed_agent_command,
        probe_os_sandbox_capability,
    )

    probe = probe_os_sandbox_capability()
    if not probe.available:
        return None, [], {"status": "unavailable", "provider": probe.provider, "reason": probe.reason}
    work_dir = cwd.resolve()
    attempt_root = work_dir.parent
    resolved_additional: list[Path] = []
    for root in additional_writable_roots:
        resolved = root.expanduser().resolve()
        try:
            resolved.relative_to(attempt_root)
        except ValueError:
            return (
                None,
                [],
                {
                    "status": "unavailable",
                    "provider": probe.provider,
                    "reason": "Additional writable root escapes the J4 attempt directory",
                },
            )
        if resolved == attempt_root or not resolved.is_dir():
            return (
                None,
                [],
                {
                    "status": "unavailable",
                    "provider": probe.provider,
                    "reason": "Additional writable root must be an existing attempt subdirectory",
                },
            )
        resolved_additional.append(resolved)
    writable_roots = tuple(str(path) for path in (work_dir, *resolved_additional)) if resolved_additional else ()
    built = build_sandboxed_agent_command(
        command,
        work_dir=work_dir,
        env=env,
        spec=SandboxBuildSpec(
            profile=SandboxProfile.WORKSPACE_WRITE,
            network_access=network_access,
            writable_roots=writable_roots,
        ),
    )
    if built.command is None or built.command == command:
        return (
            None,
            list(built.cleanup_paths),
            {
                "status": "unavailable",
                "provider": probe.provider,
                "reason": built.error or "OS sandbox did not wrap the command",
            },
        )
    return (
        list(built.command),
        list(built.cleanup_paths),
        {
            "status": "enforced",
            "provider": probe.provider,
            "reason": probe.reason,
            "writable_roots": [str(work_dir), *[str(path) for path in resolved_additional]],
        },
    )


def _write_files(workspace_dir: Path, files: dict[str, str]) -> None:
    for relative_path, content in files.items():
        target = workspace_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _scenario_workspace(base_dir: Path, scenario_name: str) -> ScenarioWorkspace:
    workspace_dir = base_dir / scenario_name
    workspace_dir.mkdir(parents=True, exist_ok=False)

    common_prompt = (
        "You are running inside a temporary evaluation workspace.\n"
        "Read TASK.md and complete the task using only local workspace files.\n"
        "Do not use the network.\n"
        "When finished, respond with exactly one JSON object matching this shape:\n"
        '{"status":"success|partial|failed","answer":"short answer","evidence":["..."],"files_created":["..."],"used_parallelism":false,"notes":"short note"}\n'
        "Do not wrap the JSON in markdown fences."
    )

    if scenario_name == "coding":
        _write_files(
            workspace_dir,
            {
                "TASK.md": "Fix calculator.py so add(a, b) returns the correct sum.",
                "calculator.py": "def add(a, b):\n    return a - b\n",
            },
        )
        return ScenarioWorkspace(
            scenario_name, workspace_dir, common_prompt, "coding task readiness and required tool surface"
        )
    if scenario_name == "review":
        _write_files(
            workspace_dir,
            {
                "TASK.md": "Review review_target.py. Create review.md with findings first. Include at least one [P1] or [P2] finding and a file reference.",
                "review_target.py": "def delete_items(items, limit):\n    for index in range(limit + 1):\n        items.pop(index)\n    return items\n",
            },
        )
        return ScenarioWorkspace(scenario_name, workspace_dir, common_prompt, "review workflow maturity")
    if scenario_name == "research":
        _write_files(
            workspace_dir,
            {
                "TASK.md": (
                    "Write research_summary.md answering which supplier ships first. Include exact lines "
                    "`Winner: <supplier>`, `Ship date: <YYYY-MM-DD>`, and "
                    "`Sources: source_alpha.md, source_beta.md`."
                ),
                "source_alpha.md": "Supplier Atlas ships the EU rollout on 2026-05-01.",
                "source_beta.md": "Supplier Beacon ships the US rollout on 2026-05-03.",
            },
        )
        return ScenarioWorkspace(scenario_name, workspace_dir, common_prompt, "research workflow maturity")
    if scenario_name == "operations":
        _write_files(
            workspace_dir,
            {
                "TASK.md": (
                    "Write ops_summary.md with the exact root-cause line `Root cause: DATABASE_URL missing` "
                    "and a `Staging verification:` line describing how to verify startup before restart."
                ),
                "service.log": "[fatal] DATABASE_URL missing at startup\n[info] service aborted before migrations\n",
                "runbook.md": "Safe remediation: set DATABASE_URL, verify with staging, then restart the service.",
            },
        )
        return ScenarioWorkspace(scenario_name, workspace_dir, common_prompt, "operations workflow maturity")
    if scenario_name == "delegation":
        _write_files(
            workspace_dir,
            {
                "TASK.md": (
                    "Create delegation_plan.md with exact lines `Tasks: task-a, task-b`, either "
                    "`Mode: parallel` or `Mode: serial`, and `Fallback: serial if delegation unavailable`."
                ),
                "task-a.md": "Audit release notes for customer-facing regressions.",
                "task-b.md": "Verify deployment checklist coverage for rollback and monitoring.",
            },
        )
        return ScenarioWorkspace(scenario_name, workspace_dir, common_prompt, "delegation surface")
    if scenario_name == "memory_recall":
        _write_files(
            workspace_dir,
            {
                "TASK.md": "Read the briefing files and write memory_answer.md containing only the secret phrase.",
                "briefing.md": "The launch codename is cedar-lantern.\nKeep it exact.",
                "appendix.md": "Remember: the secret phrase is two words separated by a hyphen.",
            },
        )
        return ScenarioWorkspace(scenario_name, workspace_dir, common_prompt, "memory/continuity surface")
    if scenario_name == "self_evolution":
        _write_files(
            workspace_dir,
            {
                "TASK.md": (
                    "Write self_evolution.md with exact line `Evidence count: 6` and a `Decision:` line "
                    "choosing either `update existing skill` or `create new skill`."
                ),
                "task_history.md": (
                    "Run 1: successful deploy checklist refresh.\n"
                    "Run 2: successful deploy checklist refresh.\n"
                    "Run 3: successful deploy checklist refresh.\n"
                    "Run 4: successful deploy checklist refresh.\n"
                    "Run 5: successful deploy checklist refresh.\n"
                    "Run 6: successful deploy checklist refresh.\n"
                ),
            },
        )
        return ScenarioWorkspace(scenario_name, workspace_dir, common_prompt, "skill routing/evolution")
    if scenario_name == "long_context_after_compaction":
        filler = "release-check telemetry budget rollback observers\n" * 200
        _write_files(
            workspace_dir,
            {
                "TASK.md": "Read the workspace and write long_context_answer.md containing only the hidden token.",
                "filler_a.md": filler,
                "filler_b.md": filler,
                "filler_c.md": filler,
                "filler_d.md": filler,
                "needle.md": "Hidden token: delta-saffron-42",
            },
        )
        return ScenarioWorkspace(scenario_name, workspace_dir, common_prompt, "compaction strategy")
    raise ValueError(f"Unsupported runtime scenario: {scenario_name}")


def _safe_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _score_runtime_scenario(
    scenario: ScenarioWorkspace, payload: dict[str, Any], run_result: ProcessRunResult
) -> dict[str, Any]:
    workspace_dir = scenario.workspace_dir
    score = 0
    breakdown: dict[str, Any] = {
        "transport": "live_cli",
        "duration_ms": run_result.duration_ms,
        "files_created": payload.get("files_created", []),
        "used_parallelism": payload.get("used_parallelism"),
        "payload_status": payload.get("status"),
        "evidence_count": len(payload.get("evidence", [])) if isinstance(payload.get("evidence"), list) else 0,
    }

    if scenario.name == "coding":
        content = _safe_read(workspace_dir / "calculator.py")
        score += 80 if "return a + b" in content else 0
        score += 20 if str(payload.get("status", "")).lower() == "success" else 0
    elif scenario.name == "review":
        content = _safe_read(workspace_dir / "review.md")
        score += 40 if content else 0
        score += 40 if "[P1]" in content or "[P2]" in content else 0
        score += 20 if "review_target.py" in content else 0
    elif scenario.name == "research":
        content = _safe_read(workspace_dir / "research_summary.md")
        lowered = content.lower()
        score += 40 if content else 0
        score += 30 if "atlas" in lowered else 0
        score += 30 if "source_alpha.md" in lowered and "source_beta.md" in lowered else 0
    elif scenario.name == "operations":
        content = _safe_read(workspace_dir / "ops_summary.md").lower()
        score += 40 if content else 0
        score += 30 if "database_url" in content else 0
        score += 30 if "staging" in content or "verify" in content else 0
    elif scenario.name == "delegation":
        content = _safe_read(workspace_dir / "delegation_plan.md").lower()
        score += 40 if content else 0
        score += 30 if "task-a" in content and "task-b" in content else 0
        score += 30 if "parallel" in content or "delegate" in content or "fallback" in content else 0
    elif scenario.name == "memory_recall":
        answer = (_safe_read(workspace_dir / "memory_answer.md") or str(payload.get("answer", ""))).lower()
        score += 100 if "cedar-lantern" in answer else 0
    elif scenario.name == "self_evolution":
        content = _safe_read(workspace_dir / "self_evolution.md").lower()
        score += 40 if content else 0
        score += 30 if "skill" in content else 0
        score += (
            30 if "repeat" in content or "repeated" in content or "promote" in content or "update" in content else 0
        )
    elif scenario.name == "long_context_after_compaction":
        answer = (_safe_read(workspace_dir / "long_context_answer.md") or str(payload.get("answer", ""))).lower()
        score += 100 if "delta-saffron-42" in answer else 0

    return {
        "ready": score >= 80,
        "score": score,
        "transcript": _ensure_text(run_result.stdout).strip()[:4000],
        "rubric": scenario.rubric,
        "score_breakdown": breakdown,
    }


def _score_timeout_runtime_scenario(
    target: str, scenario: ScenarioWorkspace, result: ProcessRunResult
) -> dict[str, Any]:
    try:
        payload = extract_runtime_payload(target, _ensure_text(result.stdout))
    except ValueError:
        payload = {}

    scored = _score_runtime_scenario(scenario, payload, result)
    if int(scored["score"]) <= 0:
        return _failed_runtime_scenario(scenario, result, reason="timeout")

    scored["transcript"] = (_ensure_text(result.stdout) or _ensure_text(result.stderr)).strip()[:4000]
    scored["score_breakdown"].update(
        {
            "timeout": True,
            "reason": "timeout_partial",
            "returncode": result.returncode,
            "partial_scoring": "artifact_and_payload" if payload else "artifact_only",
        }
    )
    return scored


def _failed_runtime_scenario(scenario: ScenarioWorkspace, result: ProcessRunResult, *, reason: str) -> dict[str, Any]:
    return {
        "ready": False,
        "score": 0,
        "transcript": (_ensure_text(result.stdout) or _ensure_text(result.stderr)).strip()[:4000],
        "rubric": scenario.rubric,
        "score_breakdown": {
            "transport": "live_cli",
            "duration_ms": result.duration_ms,
            "reason": reason,
            "returncode": result.returncode,
        },
    }


def _write_runtime_artifacts(
    output_dir: Path, scenario: ScenarioWorkspace, prompt: str, result: ProcessRunResult
) -> None:
    runtime_dir = output_dir / "runtime" / scenario.name
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    (runtime_dir / "stdout.txt").write_text(_ensure_text(result.stdout), encoding="utf-8")
    (runtime_dir / "stderr.txt").write_text(_ensure_text(result.stderr), encoding="utf-8")


def _write_preflight_artifacts(output_dir: Path, result: ProcessRunResult) -> list[str]:
    runtime_dir = output_dir / "runtime_preflight"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = runtime_dir / "stdout.txt"
    stderr_path = runtime_dir / "stderr.txt"
    stdout_path.write_text(_ensure_text(result.stdout), encoding="utf-8")
    stderr_path.write_text(_ensure_text(result.stderr), encoding="utf-8")
    return [str(stdout_path), str(stderr_path)]


def _runtime_failure_status(target: str, result: ProcessRunResult) -> str:
    haystack = f"{_ensure_text(result.stdout)}\n{_ensure_text(result.stderr)}".lower()
    if target in {"claude_code", "freecode"} and "not logged in" in haystack:
        return "auth_required"
    if "timed out" in haystack:
        return "timeout"
    if result.returncode != 0:
        return "command_failed"
    return "unknown_error"


def _preflight_runtime(target: str, output_dir: Path, profile: RuntimeProfile) -> ProcessRunResult | None:
    if target != "claude_code":
        return None
    preflight_dir = output_dir / "runtime_preflight"
    preflight_dir.mkdir(parents=True, exist_ok=True)
    command = [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--max-turns",
        "1",
        "--permission-mode",
        "bypassPermissions",
        "--dangerously-skip-permissions",
        'Return exactly {"status":"success","answer":"ok","evidence":[],"files_created":[],"used_parallelism":false,"notes":"preflight"}',
    ]
    return _run_process(command, preflight_dir, timeout_seconds=profile.preflight_timeout_seconds)


def _unavailable_report(
    *,
    executable: str,
    status: str,
    preflight: ProcessRunResult | None = None,
    artifact_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Honest no-run report (spec §2.4): when the target CLI cannot run there
    are NO scenario scores — synthesized repo-evidence scoring is retired."""
    runtime_payload: dict[str, Any] = {"status": status, "executable": executable}
    if preflight is not None:
        runtime_payload.update(
            {
                "command": preflight.command,
                "stdout": preflight.stdout,
                "stderr": preflight.stderr,
            }
        )
    return {
        "kind": "bakeoff",
        "transport": "runtime_unavailable",
        "runtime": runtime_payload,
        "auth_status": status,
        "benchmark_complete": False,
        "artifact_paths": artifact_paths or [],
        "scenarios": {},
    }


def _collect_runtime_artifact_paths(output_dir: Path) -> list[str]:
    artifacts: list[str] = []
    for root_name in ("runtime_preflight", "runtime"):
        root = output_dir / root_name
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file():
                artifacts.append(str(path))
    return artifacts


def _collect_incomplete_scenarios(scenario_reports: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    incomplete_reasons = {"timeout", "timeout_partial", "command_failed", "invalid_json", "unknown_error"}
    incomplete: list[dict[str, str]] = []
    for name, details in scenario_reports.items():
        reason = str(details.get("score_breakdown", {}).get("reason") or "").strip()
        if reason in incomplete_reasons:
            incomplete.append({"scenario": name, "reason": reason})
    return incomplete


def run_runtime_bakeoff(
    target: str,
    *,
    output_dir: Path,
    external_profile_authorized: bool = False,
) -> dict[str, Any]:
    executable = (
        "claude"
        if target == "claude_code"
        else str(_freecode_binary() or "freecode")
        if target == "freecode"
        else "hermes"
        if target == "hermes_agent"
        else None
    )
    if executable is None:
        raise ValueError(f"Unsupported bakeoff target: {target}")
    if target == "freecode" and not external_profile_authorized:
        return _unavailable_report(executable=executable, status="authority_denied")
    profile = _runtime_profile(target)

    if not _which(executable):
        return _unavailable_report(executable=executable, status="cli_unavailable")

    preflight = _preflight_runtime(target, output_dir, profile)
    preflight_artifacts: list[str] = []
    if preflight is not None:
        preflight_artifacts = _write_preflight_artifacts(output_dir, preflight)
        preflight_haystack = f"{preflight.stdout}\n{preflight.stderr}".lower()
        if preflight.returncode != 0 or "not logged in" in preflight_haystack:
            return _unavailable_report(
                executable=executable,
                status=_runtime_failure_status(target, preflight),
                preflight=preflight,
                artifact_paths=preflight_artifacts,
            )

    scenario_reports: dict[str, dict[str, Any]] = {}
    runtime_root = output_dir / "runtime_workspaces"
    runtime_root.mkdir(parents=True, exist_ok=True)
    for scenario_name in _SCENARIOS:
        scenario = _scenario_workspace(runtime_root, scenario_name)
        prompt = scenario.prompt
        command = build_runtime_command(
            target,
            prompt=prompt,
            workspace_dir=scenario.workspace_dir,
            max_turns=profile.max_turns,
        )
        if target == "freecode":
            result = _run_process(
                command,
                scenario.workspace_dir,
                timeout_seconds=profile.timeout_seconds,
                env_overrides={"CLAUDE_CODE_USE_OPENAI": "1"},
            )
        else:
            result = _run_process(command, scenario.workspace_dir, timeout_seconds=profile.timeout_seconds)
        _write_runtime_artifacts(output_dir, scenario, prompt, result)
        if result.returncode == 124:
            scenario_reports[scenario_name] = _score_timeout_runtime_scenario(target, scenario, result)
            continue
        if result.returncode != 0 and not result.stdout.strip():
            scenario_reports[scenario_name] = _failed_runtime_scenario(
                scenario,
                result,
                reason=_runtime_failure_status(target, result),
            )
            continue
        if target in {"claude_code", "freecode"}:
            try:
                outer = _load_json_object(result.stdout)
            except ValueError:
                scenario_reports[scenario_name] = _failed_runtime_scenario(
                    scenario,
                    result,
                    reason="invalid_json",
                )
                continue
            if outer.get("is_error"):
                scenario_reports[scenario_name] = _failed_runtime_scenario(
                    scenario,
                    result,
                    reason=_runtime_failure_status(target, result),
                )
                continue
        try:
            payload = extract_runtime_payload(target, result.stdout)
        except ValueError:
            scenario_reports[scenario_name] = _failed_runtime_scenario(
                scenario,
                result,
                reason="invalid_json",
            )
            continue
        scenario_reports[scenario_name] = _score_runtime_scenario(scenario, payload, result)

    # Spec §2.4: failed scenarios stay failed — no synthesized repo-evidence
    # scores backfill a live run. Incomplete coverage is reported, not papered.
    incomplete_scenarios = _collect_incomplete_scenarios(scenario_reports)
    transport = "live_cli_partial" if incomplete_scenarios else "live_cli"
    benchmark_complete = not incomplete_scenarios
    runtime_status = "partial" if incomplete_scenarios else "completed"

    return {
        "kind": "bakeoff",
        "transport": transport,
        "runtime": {"status": runtime_status, "executable": executable},
        "auth_status": "ok",
        "benchmark_complete": benchmark_complete,
        "incomplete_scenarios": incomplete_scenarios,
        "artifact_paths": _collect_runtime_artifact_paths(output_dir),
        "scenarios": scenario_reports,
    }


def _validate_runtime_payload(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["payload_not_object"]
    required = set(_RUNTIME_JSON_SCHEMA["required"])
    errors: list[str] = []
    if set(payload) != required:
        errors.append("payload_keys")
    if payload.get("status") not in {"success", "partial", "failed"}:
        errors.append("status")
    for key in ("answer", "notes"):
        if not isinstance(payload.get(key), str):
            errors.append(key)
    for key in ("evidence", "files_created"):
        value = payload.get(key)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            errors.append(key)
    if not isinstance(payload.get("used_parallelism"), bool):
        errors.append("used_parallelism")
    return sorted(set(errors))


def _normalize_model_id(value: Any) -> str:
    model = str(value or "").strip().lower()
    for prefix in ("openai/", "openai:"):
        if model.startswith(prefix):
            model = model[len(prefix) :]
    return model


def _usage_total(value: Any, names: set[str]) -> int:
    total = 0
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower().replace("_", "") in names and isinstance(item, (int, float)):
                total += int(item)
            elif isinstance(item, (dict, list)):
                total += _usage_total(item, names)
    elif isinstance(value, list):
        total += sum(_usage_total(item, names) for item in value)
    return total


def _artifact(path: Path, content: str) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = content.encode("utf-8")
    path.write_bytes(payload)
    return {"path": str(path.resolve()), "bytes": len(payload), "sha256": _sha256_bytes(payload)}


def _write_j4_artifacts(
    output_dir: Path,
    *,
    runtime: str,
    envelope_id: str,
    stdout: str,
    stderr: str,
    transcript: str = "",
) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """Write attempt artifacts only into a claimed, owned output subtree.

    A pre-planted symlink or non-directory anywhere in the j4_artifacts
    chain is refused with a typed error and no write, so artifacts can
    never be redirected into a foreign tree or publish foreign paths.
    """

    root = output_dir / "j4_artifacts" / runtime / str(envelope_id)
    claim_error = _claim_owned_output_subtree(output_dir, ("j4_artifacts", runtime))
    if claim_error == "unsupported":
        return {}, ["j4_artifacts_boundary_unsupported"]
    if claim_error is not None:
        return {}, ["j4_artifacts_boundary_unavailable"]
    try:
        root.mkdir(mode=0o700)
    except FileExistsError:
        # The per-envelope artifact leaf is exclusive: a pre-existing entry
        # is a conflict to reconcile, never a destination to merge into.
        return {}, ["j4_artifacts_boundary_conflict"]
    except OSError:
        return {}, ["j4_artifacts_boundary_unavailable"]
    artifacts = {
        "stdout": _artifact(root / "stdout.txt", stdout),
        "stderr": _artifact(root / "stderr.txt", stderr),
        "transcript": _artifact(root / "transcript.txt", transcript),
    }
    return artifacts, []


def _command_output(command: list[str], *, cwd: Path) -> str:
    try:
        result = subprocess.run(  # noqa: S603
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=10,
            env=build_agent_subprocess_env(home=Path.home()),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (result.stdout or result.stderr or "").strip() if result.returncode == 0 else ""


def _binary_identity(runtime: str, binary: Path | None) -> dict[str, Any]:
    if binary is None:
        return {"path": "", "version": "", "sha256": "", "revision": ""}
    resolved = binary.expanduser().resolve()
    version = _command_output([str(resolved), "--version"], cwd=resolved.parent) if resolved.is_file() else ""
    revision = ""
    if runtime == "freecode" and resolved.is_file():
        source = resolved.parent
        revision = _command_output(["git", "-C", str(source), "rev-parse", "HEAD"], cwd=source)
    return {
        "path": str(resolved),
        "version": version,
        "sha256": _sha256_file(resolved) if resolved.is_file() else "",
        "revision": revision,
    }


def _copy_attested_executable(source: Path, destination: Path, expected_sha256: str, expected_size: int) -> list[str]:
    """Copy one executable from an O_NOFOLLOW fd so path replacement cannot change executed bytes."""

    errors: list[str] = []
    source_fd: int | None = None
    destination_fd: int | None = None
    digest = hashlib.sha256()
    copied = 0
    try:
        source_fd = os.open(source, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        source_stat = os.fstat(source_fd)
        if not stat.S_ISREG(source_stat.st_mode) or not source_stat.st_mode & stat.S_IXUSR:
            return ["freecode_artifact_unsupported_entry"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination_fd = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o500)
        while True:
            chunk = os.read(source_fd, 1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
            copied += len(chunk)
            view = memoryview(chunk)
            while view:
                written = os.write(destination_fd, view)
                view = view[written:]
        os.fsync(destination_fd)
    except OSError:
        errors.append("freecode_artifact_freeze_failed")
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)
    if copied != expected_size:
        errors.append("freecode_artifact_size_mismatch")
    if digest.hexdigest() != expected_sha256:
        errors.append("freecode_artifact_sha256_mismatch")
    if errors:
        destination.unlink(missing_ok=True)
    return sorted(set(errors))


def _fresh_build_freecode(
    manifest: dict[str, Any],
    *,
    cleanup_root: Path,
) -> tuple[dict[str, Any], dict[str, Any], list[str]]:
    """Build the only executable J4 will use from a pinned Git tree and frozen dependencies."""

    source = manifest["source"]
    build = manifest["build"]
    source_root = Path(source["root"])
    dependencies_root = Path(build["dependencies_root"])
    bun_path = Path(build["bun_path"])
    frozen_source = cleanup_root / "freecode-source"
    frozen_dependencies = frozen_source / "node_modules"
    toolchain_root = cleanup_root / "freecode-toolchain"
    frozen_bun = toolchain_root / "bun"
    build_state = cleanup_root / "freecode-build-state"
    archive_path = cleanup_root / "freecode-source.tar"
    errors: list[str] = []

    git_path = Path(_which("git") or "")
    tracked_result = _trusted_command(
        ["git", "-C", str(source_root), "ls-tree", "-r", "--name-only", "-z", source["revision"]],
        cwd=source_root,
    )
    tracked = (
        {value for value in tracked_result.stdout.split("\0") if value}
        if tracked_result is not None and tracked_result.returncode == 0
        else set()
    )
    if not git_path.is_absolute() or not git_path.is_file() or not tracked:
        return {}, {}, ["freecode_fresh_build_source_inventory_unavailable"]
    archive_result = _run_process(
        [
            str(git_path),
            "-C",
            str(source_root),
            "archive",
            "--format=tar",
            f"--output={archive_path}",
            source["revision"],
        ],
        source_root,
        timeout_seconds=30,
        require_workspace_sandbox=True,
        additional_writable_roots=(cleanup_root,),
        network_access=False,
    )
    if archive_result.returncode != 0 or not archive_path.is_file():
        return {}, {}, ["freecode_fresh_build_source_snapshot_failed"]
    try:
        frozen_source.mkdir(mode=0o700)
        with tarfile.open(archive_path, "r") as archive:
            archive.extractall(frozen_source, filter="data")
        archive_path.unlink()
        shutil.copytree(
            dependencies_root,
            frozen_dependencies,
            symlinks=True,
            copy_function=shutil.copy2,
        )
        toolchain_root.mkdir(mode=0o700)
        shutil.copy2(bun_path, frozen_bun, follow_symlinks=False)
        frozen_bun.chmod(0o500)
        build_state.mkdir(mode=0o700)
        for name in ("home", "tmp"):
            (build_state / name).mkdir(mode=0o700)
    except (OSError, shutil.Error, tarfile.TarError):
        return {}, {}, ["freecode_fresh_build_input_freeze_failed"]

    source_pre_sha, source_pre_errors = _source_manifest_digest(frozen_source, tracked)
    dependencies_pre, dependencies_pre_errors = _tree_identity(frozen_dependencies)
    try:
        package = json.loads((frozen_source / "package.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        package = {}
    expected_version = str(package.get("version") or "").strip() if isinstance(package, dict) else ""
    if (
        source_pre_errors
        or source_pre_sha != source["content_sha256"]
        or dependencies_pre_errors
        or dependencies_pre.get("sha256") != build["dependencies_sha256"]
        or _safe_sha256_file(frozen_bun) != build["bun_sha256"]
        or not expected_version
    ):
        return {}, {}, ["freecode_fresh_build_frozen_input_mismatch"]

    output_path = frozen_source / "cli"
    if output_path.exists():
        return {}, {}, ["freecode_fresh_build_output_not_fresh"]
    build_env = {
        "HOME": str(build_state / "home"),
        "TMPDIR": str(build_state / "tmp"),
        "TMP": str(build_state / "tmp"),
        "TEMP": str(build_state / "tmp"),
        "PATH": f"{toolchain_root}:/usr/bin:/bin",
    }
    command = [str(frozen_bun), "run", "./scripts/build.ts"]
    result = _run_process(
        command,
        frozen_source,
        timeout_seconds=600,
        env_overrides=build_env,
        env_remove=_HERMES_J4_AMBIENT_ENV_DENYLIST,
        require_workspace_sandbox=True,
        additional_writable_roots=(build_state,),
        network_access=False,
    )
    source_post_sha, source_post_errors = _source_manifest_digest(frozen_source, tracked)
    dependencies_post, dependencies_post_errors = _tree_identity(frozen_dependencies)
    try:
        output_details = output_path.lstat()
    except OSError:
        output_details = None
    if (
        result.returncode != 0
        or not isinstance(result.sandbox, dict)
        or result.sandbox.get("status") != "enforced"
        or output_details is None
        or not stat.S_ISREG(output_details.st_mode)
        or output_path.is_symlink()
        or not output_details.st_mode & stat.S_IXUSR
        or source_post_errors
        or source_post_sha != source_pre_sha
        or dependencies_post_errors
        or dependencies_post != dependencies_pre
        or _safe_sha256_file(frozen_bun) != build["bun_sha256"]
    ):
        return {}, {}, ["freecode_fresh_build_failed_or_inputs_drifted"]

    version_result = _run_process(
        [str(output_path), "--version"],
        frozen_source,
        timeout_seconds=20,
        env_overrides=build_env,
        env_remove=_HERMES_J4_AMBIENT_ENV_DENYLIST,
        require_workspace_sandbox=True,
        additional_writable_roots=(build_state,),
        network_access=False,
    )
    version = (version_result.stdout or version_result.stderr or "").strip()
    if (
        version_result.returncode != 0
        or not isinstance(version_result.sandbox, dict)
        or version_result.sandbox.get("status") != "enforced"
        or version != expected_version
    ):
        return {}, {}, ["freecode_fresh_build_version_mismatch"]

    effective_artifact = {
        "path": str(output_path),
        "version": version,
        "size": output_details.st_size,
        "sha256": _sha256_file(output_path),
    }
    receipt = {
        "schema": "hive.j4.freecode_fresh_build_receipt.v1",
        "source_revision": source["revision"],
        "source_tree": source["tree"],
        "source_content_sha256": source_pre_sha,
        "source_package_version": expected_version,
        "dependencies_sha256": dependencies_pre["sha256"],
        "dependencies_entry_count": dependencies_pre["entry_count"],
        "bun_sha256": build["bun_sha256"],
        "bun_version": build["bun_version"],
        "git_sha256": _sha256_file(git_path),
        "argv": command,
        "network_access": False,
        "sandbox": dict(result.sandbox),
        "artifact": dict(effective_artifact),
        "inputs_stable": True,
    }
    effective_manifest = {
        **manifest,
        "artifact": effective_artifact,
        "build": {**build, "fresh_receipt_sha256": _sha256_json(receipt)},
    }
    return effective_manifest, receipt, errors


def _freecode_runtime_sha256(prepared: PreparedJ4Runtimes) -> str:
    artifact = prepared.freecode_manifest["artifact"]
    source = prepared.freecode_manifest["source"]
    return _sha256_json(
        {
            "schema": "hive.j4.freecode_frozen_runtime_identity.v1",
            "build_manifest_sha256": prepared.freecode_manifest_sha256,
            "fresh_build_receipt_sha256": prepared.freecode_build_receipt_sha256,
            "artifact_sha256": artifact["sha256"],
            "artifact_version": artifact["version"],
            "source_revision": source["revision"],
            "source_sha256": source["sha256"],
            "hook_sha256": prepared.freecode_hook_sha256,
            "hook_python_sha256": prepared.freecode_hook_python_sha256,
            "hook_python_environment_sha256": prepared.freecode_hook_python_environment_sha256,
        }
    )


def _freeze_freecode_runtime(
    prepared: PreparedJ4Runtimes,
    *,
    workspace_root: Path,
) -> tuple[dict[str, Any], Path, Path, list[str]]:
    artifact = prepared.freecode_manifest["artifact"]
    source = prepared.freecode_manifest["source"]
    attempt_root = workspace_root.parent
    runtime_root = attempt_root / "runtime"
    state_root = attempt_root / "state"
    frozen_binary = runtime_root / "freecode"
    frozen_hook = runtime_root / "freecode_j4_hook.py"
    errors = _copy_attested_executable(
        Path(artifact["path"]),
        frozen_binary,
        str(artifact["sha256"]).lower(),
        int(artifact["size"]),
    )
    try:
        shutil.copy2(prepared.freecode_hook, frozen_hook)
        frozen_hook.chmod(0o400)
    except OSError:
        errors.append("freecode_authority_guard_freeze_failed")
    hook_sha = _sha256_file(frozen_hook) if frozen_hook.is_file() else ""
    if hook_sha != prepared.freecode_hook_sha256:
        errors.append("freecode_authority_guard_sha256_mismatch")
    version = ""
    if not errors:
        home = state_root / "home"
        temporary = state_root / "tmp"
        home.mkdir(mode=0o700)
        temporary.mkdir(mode=0o700)
        version_result = _run_process(
            [str(frozen_binary), "--version"],
            workspace_root,
            timeout_seconds=20,
            env_overrides={"HOME": str(home), "TMPDIR": str(temporary), "TMP": str(temporary), "TEMP": str(temporary)},
            require_workspace_sandbox=True,
            additional_writable_roots=(state_root,),
            network_access=False,
        )
        version = (version_result.stdout or version_result.stderr or "").strip()
        if version_result.returncode != 0 or version != str(artifact["version"]).strip():
            errors.append("freecode_artifact_version_mismatch")
        if not isinstance(version_result.sandbox, dict) or version_result.sandbox.get("status") != "enforced":
            errors.append("freecode_version_sandbox_unavailable")
    runtime_sha = _freecode_runtime_sha256(prepared)
    identity = {
        "path": str(frozen_binary),
        "version": version,
        "sha256": str(artifact["sha256"]).lower(),
        "revision": source["revision"],
        "runtime_sha256": runtime_sha,
        "components": {
            "build_manifest": {
                "sha256": prepared.freecode_manifest_sha256,
                "schema": prepared.freecode_manifest["schema"],
            },
            "fresh_build_receipt": dict(prepared.freecode_build_receipt),
            "source": dict(source),
            "authority_guard": {
                "path": str(frozen_hook),
                "sha256": hook_sha,
                "python_path": str(prepared.freecode_hook_python),
                "python_sha256": prepared.freecode_hook_python_sha256,
                "python_environment_sha256": prepared.freecode_hook_python_environment_sha256,
            },
        },
    }
    return identity, frozen_binary, frozen_hook, sorted(set(errors))


def _git_source_identity(
    source_root: Path,
    *,
    excluded_runtime_roots: tuple[Path, ...] = (),
) -> tuple[dict[str, Any], list[str]]:
    """Attest every source-worktree byte except Git metadata and a validated runtime root."""

    resolved = source_root.expanduser().resolve()
    identity: dict[str, Any] = {
        "root": str(resolved),
        "sha256": "",
        "revision": "",
        "clean": False,
        "lock_sha256": "",
        "excluded_runtime_roots": [],
        "scope": HERMES_J4_SOURCE_SCOPE,
    }
    if not resolved.is_dir():
        return identity, ["hermes_source_unavailable"]

    revision = _command_output(["git", "-C", str(resolved), "rev-parse", "HEAD"], cwd=resolved)
    top_level = _command_output(["git", "-C", str(resolved), "rev-parse", "--show-toplevel"], cwd=resolved)
    tracked_output = _command_output(["git", "-C", str(resolved), "ls-files", "-z"], cwd=resolved)
    changed_output = _command_output(
        ["git", "-C", str(resolved), "diff", "--name-only", "-z", "HEAD"],
        cwd=resolved,
    )
    untracked_output = _command_output(
        ["git", "-C", str(resolved), "ls-files", "--others", "--exclude-standard", "-z"],
        cwd=resolved,
    )
    identity["revision"] = revision
    errors: list[str] = []
    if not revision or not top_level or Path(top_level).resolve() != resolved:
        errors.append("hermes_source_git_root")
    tracked_all = {value for value in tracked_output.split("\0") if value}
    excluded: list[Path] = []
    for root in excluded_runtime_roots:
        candidate = root.expanduser().resolve()
        try:
            candidate.relative_to(resolved)
        except ValueError:
            continue
        excluded.append(candidate)
    identity["excluded_runtime_roots"] = [str(path) for path in sorted(excluded)]
    excluded_relative = [root.relative_to(resolved).as_posix() for root in excluded]

    def in_scope(relative: str) -> bool:
        return not any(relative == root or relative.startswith(f"{root}/") for root in excluded_relative)

    tracked = {path for path in tracked_all if in_scope(path)}
    changed = {path for path in changed_output.split("\0") if path and in_scope(path)}
    untracked = {path for path in untracked_output.split("\0") if path and in_scope(path)}
    if changed or untracked:
        errors.append("hermes_source_dirty")
    for root in excluded:
        relative_root = root.relative_to(resolved).as_posix()
        if any(path == relative_root or path.startswith(f"{relative_root}/") for path in tracked_all):
            errors.append("hermes_runtime_root_overlaps_tracked_source")
        ignored = _command_output(
            ["git", "-C", str(resolved), "check-ignore", "--no-index", "--", relative_root],
            cwd=resolved,
        )
        if not root.is_dir() or relative_root not in {line.rstrip("/") for line in ignored.splitlines()}:
            errors.append("hermes_runtime_root_not_ignored")
    present: set[str] = set()
    for current_root, directory_names, file_names in os.walk(resolved, followlinks=False):
        current = Path(current_root)
        relative_root = current.relative_to(resolved)
        if relative_root == Path("."):
            directory_names[:] = [name for name in directory_names if name != ".git"]
            file_names = [name for name in file_names if name != ".git"]
        symlink_directories = [name for name in directory_names if (current / name).is_symlink()]
        directory_names[:] = [
            name
            for name in directory_names
            if name not in symlink_directories and (current / name).resolve() not in excluded
        ]
        for name in [*file_names, *symlink_directories]:
            relative = (relative_root / name).as_posix() if relative_root != Path(".") else name
            if in_scope(relative):
                present.add(relative)
    if present != tracked:
        errors.append("hermes_source_untracked_bytes")

    manifest: list[dict[str, Any]] = []
    for relative in sorted(tracked):
        path = resolved / relative
        try:
            file_stat = path.lstat()
            if stat.S_ISLNK(file_stat.st_mode):
                payload = os.readlink(path).encode("utf-8")
                kind = "symlink"
                errors.append("hermes_source_symlink")
            elif stat.S_ISREG(file_stat.st_mode):
                payload = path.read_bytes()
                kind = "file"
            else:
                errors.append("hermes_source_unsupported_entry")
                continue
        except OSError:
            errors.append("hermes_source_unreadable")
            continue
        manifest.append(
            {
                "path": relative,
                "kind": kind,
                "executable": bool(file_stat.st_mode & stat.S_IXUSR),
                "bytes": len(payload),
                "sha256": _sha256_bytes(payload),
            }
        )
    if not tracked:
        errors.append("hermes_source_empty")
    lock_path = resolved / "uv.lock"
    if "uv.lock" not in tracked or not lock_path.is_file():
        errors.append("hermes_source_lock_unavailable")
    else:
        identity["lock_sha256"] = _sha256_file(lock_path)
    errors = sorted(set(errors))
    if not errors:
        identity["sha256"] = _sha256_json(manifest)
        identity["clean"] = True
    return identity, errors


def _hermes_source_version(source_root: Path) -> str:
    try:
        payload = tomllib.loads((source_root / "pyproject.toml").read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return ""
    project = payload.get("project") if isinstance(payload, dict) else None
    return str(project.get("version") or "").strip() if isinstance(project, dict) else ""


def _empty_hermes_runtime_identity() -> dict[str, Any]:
    return {
        "path": "",
        "version": "",
        "sha256": "",
        "revision": "",
        "runtime_sha256": "",
        "components": {},
    }


def _python_environment_identity(
    python: Path,
    *,
    base_root_override: Path | None = None,
) -> tuple[dict[str, Any], list[str]]:
    """Attest the complete venv and the base CPython tree used by ``-I -S``."""

    configured = python.expanduser()
    configured = Path(os.path.abspath(configured))
    identity: dict[str, Any] = {
        "path": str(configured),
        "kind": "",
        "entry_sha256": "",
        "link_target": None,
        "resolved_path": "",
        "resolved_sha256": "",
        "venv_root": "",
        "pyvenv_sha256": "",
        "tree_sha256": "",
        "tree_entry_count": 0,
        "tree_total_bytes": 0,
        "base_root": "",
        "base_python_path": "",
        "base_python_sha256": "",
        "base_tree_sha256": "",
        "base_tree_entry_count": 0,
        "base_tree_total_bytes": 0,
        "sha256": "",
    }
    errors: list[str] = []
    try:
        entry_stat = configured.lstat()
    except OSError:
        return identity, ["hermes_python_unavailable"]
    if stat.S_ISLNK(entry_stat.st_mode):
        link_target = os.readlink(configured)
        identity["kind"] = "symlink"
        identity["link_target"] = link_target
        identity["entry_sha256"] = _sha256_bytes(link_target.encode("utf-8"))
    elif stat.S_ISREG(entry_stat.st_mode):
        identity["kind"] = "file"
        identity["entry_sha256"] = _sha256_file(configured)
    else:
        errors.append("hermes_python_unsupported_entry")

    try:
        resolved = configured.resolve(strict=True)
    except OSError:
        resolved = configured
        errors.append("hermes_python_unavailable")
    resolved_sha256 = _sha256_file(resolved) if resolved.is_file() else ""
    identity["resolved_path"] = str(resolved)
    identity["resolved_sha256"] = resolved_sha256
    if not resolved_sha256 or not os.access(configured, os.X_OK):
        errors.append("hermes_python_unavailable")

    if configured.parent.name.lower() not in {"bin", "scripts"}:
        errors.append("hermes_python_not_venv")
        venv_root = configured.parent
    else:
        venv_root = configured.parent.parent
    pyvenv = venv_root / "pyvenv.cfg"
    identity["venv_root"] = str(venv_root)
    if not pyvenv.is_file():
        errors.append("hermes_pyvenv_unavailable")
    else:
        identity["pyvenv_sha256"] = _safe_sha256_file(pyvenv)
        if not identity["pyvenv_sha256"]:
            errors.append("hermes_pyvenv_unavailable")

    base_home: Path | None = None
    if pyvenv.is_file():
        try:
            for line in pyvenv.read_text(encoding="utf-8").splitlines():
                key, separator, value = line.partition("=")
                if separator and key.strip().lower() == "home":
                    configured_home = Path(value.strip()).expanduser()
                    if configured_home.is_absolute():
                        base_home = configured_home.resolve(strict=True)
                    break
        except (OSError, UnicodeDecodeError):
            base_home = None
    origin_base_root = (
        base_home.parent if base_home is not None and base_home.name.lower() in {"bin", "scripts"} else base_home
    )
    tree, tree_errors = _tree_identity(
        venv_root,
        allowed_external_python_root=origin_base_root,
    )
    errors.extend(f"hermes_python_environment_{error}" for error in tree_errors)
    identity["tree_sha256"] = tree.get("sha256") or ""
    identity["tree_entry_count"] = tree.get("entry_count") or 0
    identity["tree_total_bytes"] = tree.get("total_bytes") or 0

    if base_home is None or origin_base_root is None or not base_home.is_dir():
        errors.append("hermes_python_base_home_unavailable")
    else:
        try:
            base_python_relative = resolved.relative_to(origin_base_root)
        except ValueError:
            origin_base_python = base_home / configured.name
            try:
                base_python_relative = origin_base_python.relative_to(origin_base_root)
            except ValueError:
                errors.append("hermes_python_base_executable_unavailable")
                base_python_relative = Path("bin") / configured.name
        try:
            base_root = (
                base_root_override.expanduser().resolve(strict=True)
                if base_root_override is not None
                else origin_base_root.resolve(strict=True)
            )
        except OSError:
            errors.append("hermes_python_base_root_unavailable")
            base_root = origin_base_root
        base_python = base_root / base_python_relative
        base_tree, base_tree_errors = _tree_identity(base_root)
        errors.extend(f"hermes_python_base_{error}" for error in base_tree_errors)
        identity["base_root"] = str(base_root)
        identity["base_python_path"] = str(base_python)
        identity["base_python_sha256"] = _safe_sha256_file(base_python)
        identity["base_tree_sha256"] = base_tree.get("sha256") or ""
        identity["base_tree_entry_count"] = base_tree.get("entry_count") or 0
        identity["base_tree_total_bytes"] = base_tree.get("total_bytes") or 0
        if not identity["base_python_sha256"] or not os.access(base_python, os.X_OK):
            errors.append("hermes_python_base_executable_unavailable")

    errors = sorted(set(errors))
    if errors:
        return identity, errors
    identity["sha256"] = _sha256_json(
        {
            "schema": "hive.j4.hermes_python_environment.v1",
            "kind": identity["kind"],
            "entry_sha256": identity["entry_sha256"],
            "link_target": identity["link_target"],
            "resolved_sha256": identity["resolved_sha256"],
            "pyvenv_sha256": identity["pyvenv_sha256"],
            "tree_sha256": identity["tree_sha256"],
            "tree_entry_count": identity["tree_entry_count"],
            "tree_total_bytes": identity["tree_total_bytes"],
            "base_python_sha256": identity["base_python_sha256"],
            "base_tree_sha256": identity["base_tree_sha256"],
            "base_tree_entry_count": identity["base_tree_entry_count"],
            "base_tree_total_bytes": identity["base_tree_total_bytes"],
        }
    )
    return identity, []


def _hermes_runtime_identity(
    config: J4RuntimeConfig,
    *,
    launcher_path: Path = HERMES_J4_LAUNCHER,
) -> tuple[dict[str, Any], list[str]]:
    """Bind the executed interpreter, in-repo launcher, and clean Hermes source."""

    required = (
        config.hermes_python,
        config.hermes_python_sha256,
        config.hermes_python_environment_sha256,
        config.hermes_source_root,
        config.hermes_source_revision,
        config.hermes_source_sha256,
    )
    if not all(str(value or "").strip() for value in required):
        return _empty_hermes_runtime_identity(), ["hermes_runtime_attestation_required"]

    configured_python = Path(str(config.hermes_python)).expanduser()
    python, python_errors = _python_environment_identity(configured_python)
    errors: list[str] = list(python_errors)
    python_path = Path(str(python.get("path") or configured_python))
    source_root = Path(str(config.hermes_source_root)).expanduser().resolve()
    launcher = launcher_path.expanduser().resolve()
    identity = _empty_hermes_runtime_identity()
    python_sha256 = str(python.get("resolved_sha256") or "")
    if python_sha256 != str(config.hermes_python_sha256).strip().lower():
        errors.append("hermes_python_sha256_mismatch")
    if python.get("sha256") != str(config.hermes_python_environment_sha256).strip().lower():
        errors.append("hermes_python_environment_sha256_mismatch")

    venv_root = Path(str(python.get("venv_root") or python_path.parent.parent))
    source, source_errors = _git_source_identity(source_root, excluded_runtime_roots=(venv_root,))
    errors.extend(source_errors)
    if source.get("revision") != str(config.hermes_source_revision).strip():
        errors.append("hermes_source_revision_mismatch")
    if source.get("sha256") != str(config.hermes_source_sha256).strip().lower():
        errors.append("hermes_source_sha256_mismatch")

    launcher_sha256 = _safe_sha256_file(launcher) if launcher.is_file() else ""
    if not launcher_sha256:
        errors.append("hermes_launcher_unavailable")
    version = _hermes_source_version(source_root)
    if not version:
        errors.append("hermes_version_unavailable")

    components = {
        "python": python,
        "launcher": {"path": str(launcher), "sha256": launcher_sha256},
        "source": source,
    }
    identity.update(
        {
            "path": str(python_path),
            "version": f"Hermes Agent v{version}" if version else "",
            "revision": str(source.get("revision") or ""),
            "components": components,
        }
    )
    errors = sorted(set(errors))
    if errors:
        return identity, errors
    runtime_sha256 = _sha256_json(
        {
            "schema": "hive.j4.hermes_runtime_identity.v1",
            "python_environment_sha256": python["sha256"],
            "launcher_sha256": launcher_sha256,
            "source_revision": source["revision"],
            "source_sha256": source["sha256"],
            "source_lock_sha256": source["lock_sha256"],
        }
    )
    identity["sha256"] = runtime_sha256
    identity["runtime_sha256"] = runtime_sha256
    return identity, []


def _source_manifest_digest(root: Path, paths: set[str]) -> tuple[str, list[str]]:
    manifest: list[dict[str, Any]] = []
    errors: list[str] = []
    for relative in sorted(paths):
        path = root / relative
        try:
            details = path.lstat()
            if not stat.S_ISREG(details.st_mode):
                errors.append(f"source_unsupported_entry:{relative}")
                continue
            manifest.append(
                {
                    "path": relative,
                    "kind": "file",
                    "executable": bool(details.st_mode & stat.S_IXUSR),
                    "bytes": details.st_size,
                    "sha256": _sha256_file(path),
                }
            )
        except OSError:
            errors.append(f"source_unreadable:{relative}")
    return (_sha256_json(manifest) if not errors else ""), sorted(set(errors))


def _case_sensitive_directory(root: Path) -> bool:
    probe = root / f".hive-j4-case-{uuid.uuid4().hex}a"
    alternate = probe.with_name(probe.name[:-1] + "A")
    descriptor: int | None = None
    try:
        descriptor = os.open(probe, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        return not alternate.exists()
    except OSError:
        return False
    finally:
        if descriptor is not None:
            os.close(descriptor)
        probe.unlink(missing_ok=True)


def _hermes_freeze_root(
    config: J4RuntimeConfig,
    *,
    source_root: Path,
    venv_root: Path,
    base_python_root: Path,
    required_bytes: int,
) -> tuple[Path | None, list[str]]:
    configured = str(config.hermes_freeze_root or "").strip()
    if not configured:
        return None, ["hermes_freeze_root_required"]
    root = Path(configured).expanduser()
    try:
        details = root.lstat()
        resolved = root.resolve(strict=True)
    except OSError:
        return None, ["hermes_freeze_root_unavailable"]
    if not root.is_absolute() or root.is_symlink() or not stat.S_ISDIR(details.st_mode):
        return None, ["hermes_freeze_root_unavailable"]
    for runtime_root in (source_root.resolve(), venv_root.resolve(), base_python_root.resolve()):
        try:
            resolved.relative_to(runtime_root)
        except ValueError:
            pass
        else:
            return None, ["hermes_freeze_root_overlaps_runtime"]
    if not _case_sensitive_directory(resolved):
        return None, ["hermes_freeze_root_not_case_sensitive"]
    try:
        available = shutil.disk_usage(resolved).free
    except OSError:
        return None, ["hermes_freeze_root_capacity_unavailable"]
    if available < required_bytes:
        return None, ["hermes_freeze_root_capacity_insufficient"]
    return resolved, []


def _prepare_j4_runtimes(
    config: J4RuntimeConfig,
) -> tuple[PreparedJ4Runtimes | None, list[dict[str, Any]]]:
    """Validate owner inputs, then freeze Hermes source and venv before any model call."""

    freecode_manifest, freecode_errors = _load_freecode_build_manifest(config)
    if freecode_errors:
        return None, [
            {
                "code": "runtime_identity_attestation_failed",
                "runtime": "freecode",
                "detail": ",".join(freecode_errors),
            }
        ]
    hook = FREECODE_J4_HOOK.resolve()
    hook_sha256 = _safe_sha256_file(hook)
    if not hook_sha256:
        return None, [
            {
                "code": "runtime_identity_attestation_failed",
                "runtime": "freecode",
                "detail": "freecode_authority_guard_unavailable",
            }
        ]

    hermes_origin, hermes_errors = _hermes_runtime_identity(config)
    if hermes_errors:
        code = (
            "cli_unavailable" if "hermes_python_unavailable" in hermes_errors else "runtime_identity_attestation_failed"
        )
        return None, [{"code": code, "runtime": "hermes", "detail": ",".join(hermes_errors)}]
    (
        hermes_auth_profile,
        hermes_auth_projection,
        hermes_auth_source,
        hermes_auth_errors,
    ) = _load_hermes_auth_profile(config)
    if hermes_auth_errors:
        return None, [
            {
                "code": "runtime_identity_attestation_failed",
                "runtime": "hermes",
                "detail": ",".join(hermes_auth_errors),
            }
        ]
    assert hermes_auth_source is not None
    hermes_auth_projection_sha256 = _sha256_json(hermes_auth_projection)
    hermes_auth_run_nonce = os.urandom(32)
    hermes_auth_profile = {
        **hermes_auth_profile,
        "projection_run_sha256": _sha256_bytes(
            hermes_auth_run_nonce + _canonical_json(hermes_auth_projection).encode("utf-8")
        ),
    }
    components = hermes_origin["components"]
    source_origin = components["source"]
    python_origin = components["python"]
    source_root = Path(source_origin["root"])
    venv_root = Path(python_origin["venv_root"])
    base_python_root = Path(python_origin["base_root"])
    try:
        python_relative = Path(python_origin["path"]).relative_to(venv_root)
        base_python_relative = Path(python_origin["base_python_path"]).relative_to(base_python_root)
    except ValueError:
        return None, [
            {
                "code": "runtime_identity_attestation_failed",
                "runtime": "hermes",
                "detail": "hermes_python_escapes_attested_runtime",
            }
        ]
    tracked_result = _trusted_command(["git", "-C", str(source_root), "ls-files", "-z"], cwd=source_root)
    if tracked_result is None or tracked_result.returncode != 0:
        return None, [
            {
                "code": "runtime_identity_attestation_failed",
                "runtime": "hermes",
                "detail": "hermes_source_inventory_unavailable",
            }
        ]
    tracked = {value for value in tracked_result.stdout.split("\0") if value}
    excluded_roots = [Path(value) for value in source_origin.get("excluded_runtime_roots", [])]
    excluded_relative: list[str] = []
    for excluded in excluded_roots:
        try:
            excluded_relative.append(excluded.relative_to(source_root).as_posix())
        except ValueError:
            continue
    tracked = {
        relative
        for relative in tracked
        if not any(relative == root or relative.startswith(f"{root}/") for root in excluded_relative)
    }
    if ".env" in tracked:
        return None, [
            {
                "code": "runtime_identity_attestation_failed",
                "runtime": "hermes",
                "detail": "hermes_source_project_env_forbidden",
            }
        ]
    observed_source_sha, source_manifest_errors = _source_manifest_digest(source_root, tracked)
    if source_manifest_errors or observed_source_sha != source_origin.get("sha256"):
        return None, [
            {
                "code": "runtime_identity_attestation_failed",
                "runtime": "hermes",
                "detail": "hermes_source_manifest_mismatch",
            }
        ]

    try:
        source_bytes = sum((source_root / relative).lstat().st_size for relative in tracked)
    except OSError:
        return None, [
            {
                "code": "runtime_identity_attestation_failed",
                "runtime": "hermes",
                "detail": "hermes_source_capacity_inventory_unavailable",
            }
        ]
    payload_bytes = (
        source_bytes
        + int(python_origin.get("tree_total_bytes") or 0)
        + int(python_origin.get("base_tree_total_bytes") or 0)
        + int(freecode_manifest["build"].get("dependencies_total_bytes") or 0)
        + 512 * 1024 * 1024
    )
    required_bytes = int(payload_bytes * 1.25) + 128 * 1024 * 1024
    freeze_root, freeze_root_errors = _hermes_freeze_root(
        config,
        source_root=source_root,
        venv_root=venv_root,
        base_python_root=base_python_root,
        required_bytes=required_bytes,
    )
    if freeze_root_errors:
        return None, [
            {
                "code": "resource_unavailable",
                "runtime": "hermes",
                "detail": ",".join(freeze_root_errors),
                "required_bytes": required_bytes,
            }
        ]
    assert freeze_root is not None

    cleanup_root: Path | None = None
    cleanup_handle: Any | None = None
    try:
        cleanup_handle = tempfile.TemporaryDirectory(prefix=".hive-j4-frozen-", dir=freeze_root)
        cleanup_root = Path(cleanup_handle.name)
        frozen_source = cleanup_root / "source"
        frozen_venv = cleanup_root / "venv"
        frozen_base_python = cleanup_root / "base-python"
        frozen_launcher = cleanup_root / "hermes_j4_launcher.py"
        frozen_source.mkdir()
        for relative in sorted(tracked):
            source = source_root / relative
            destination = frozen_source / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, destination, follow_symlinks=False)
        shutil.copytree(venv_root, frozen_venv, symlinks=True, copy_function=shutil.copy2)
        shutil.copytree(base_python_root, frozen_base_python, symlinks=True, copy_function=shutil.copy2)
        shutil.copy2(HERMES_J4_LAUNCHER, frozen_launcher)

        freecode_input_manifest = freecode_manifest
        freecode_manifest, freecode_build_receipt, freecode_build_errors = _fresh_build_freecode(
            freecode_input_manifest,
            cleanup_root=cleanup_root,
        )
        if freecode_build_errors:
            raise ValueError(",".join(freecode_build_errors))
        freecode_build_receipt_sha256 = _sha256_json(freecode_build_receipt)

        frozen_source_sha, frozen_source_errors = _source_manifest_digest(frozen_source, tracked)
        frozen_venv_python = frozen_venv / python_relative
        frozen_python_identity, frozen_python_errors = _python_environment_identity(
            frozen_venv_python,
            base_root_override=frozen_base_python,
        )
        frozen_python = frozen_base_python / base_python_relative
        site_packages = sorted(path for path in (frozen_venv / "lib").glob("python*/site-packages") if path.is_dir())
        if frozen_source_errors or frozen_source_sha != source_origin.get("sha256"):
            hermes_errors.append("hermes_frozen_source_mismatch")
        if frozen_python_errors or frozen_python_identity.get("sha256") != python_origin.get("sha256"):
            hermes_errors.append("hermes_frozen_python_environment_mismatch")
        if len(site_packages) != 1:
            hermes_errors.append("hermes_frozen_site_packages_unavailable")
        launcher_sha = _sha256_file(frozen_launcher)
        if launcher_sha != components["launcher"]["sha256"]:
            hermes_errors.append("hermes_frozen_launcher_mismatch")
        if hermes_errors:
            raise ValueError(",".join(sorted(set(hermes_errors))))

        frozen_source_identity = {
            **source_origin,
            "root": str(frozen_source),
            "excluded_runtime_roots": [],
            "origin_root": str(source_root),
            "frozen": True,
        }
        frozen_components = {
            "python": frozen_python_identity,
            "launcher": {"path": str(frozen_launcher), "sha256": launcher_sha},
            "source": frozen_source_identity,
            "auth_profile": hermes_auth_profile,
            "origin_runtime_sha256": hermes_origin["runtime_sha256"],
        }
        runtime_sha = _sha256_json(
            {
                "schema": "hive.j4.hermes_frozen_runtime_identity.v1",
                "python_environment_sha256": frozen_python_identity["sha256"],
                "launcher_sha256": launcher_sha,
                "source_revision": frozen_source_identity["revision"],
                "source_sha256": frozen_source_identity["sha256"],
                "source_lock_sha256": frozen_source_identity["lock_sha256"],
                "auth_profile_sha256": hermes_auth_profile["projection_run_sha256"],
            }
        )
        hermes_binary = {
            "path": str(frozen_python),
            "version": hermes_origin["version"],
            "sha256": runtime_sha,
            "revision": hermes_origin["revision"],
            "runtime_sha256": runtime_sha,
            "components": frozen_components,
        }
        prepared = PreparedJ4Runtimes(
            freecode_manifest=freecode_manifest,
            freecode_input_manifest=freecode_input_manifest,
            freecode_manifest_sha256=str(config.freecode_build_manifest_sha256).strip().lower(),
            freecode_build_receipt=freecode_build_receipt,
            freecode_build_receipt_sha256=freecode_build_receipt_sha256,
            freecode_hook=hook,
            freecode_hook_sha256=hook_sha256,
            freecode_hook_python=frozen_python,
            freecode_hook_python_sha256=_safe_sha256_file(frozen_python),
            freecode_hook_python_environment_sha256=str(frozen_python_identity["base_tree_sha256"]),
            hermes_binary=hermes_binary,
            hermes_python=frozen_python,
            hermes_venv_python=frozen_venv_python,
            hermes_base_python_root=frozen_base_python,
            hermes_launcher=frozen_launcher,
            hermes_source_root=frozen_source,
            hermes_source_paths=frozenset(tracked),
            hermes_site_packages=site_packages[0],
            hermes_auth_projection=hermes_auth_projection,
            hermes_auth_profile=hermes_auth_profile,
            hermes_auth_source=hermes_auth_source,
            hermes_auth_source_sha256=str(config.hermes_auth_store_sha256).strip().lower(),
            hermes_auth_projection_sha256=hermes_auth_projection_sha256,
            hermes_auth_run_nonce=hermes_auth_run_nonce,
            cleanup_root=cleanup_root,
            cleanup_handle=cleanup_handle,
        )
    except (OSError, shutil.Error, ValueError) as exc:
        if cleanup_handle is not None:
            cleanup_handle.cleanup()
        elif cleanup_root is not None:
            shutil.rmtree(cleanup_root, ignore_errors=True)
        detail = str(exc)
        return None, [
            {
                "code": "runtime_freeze_failed",
                "runtime": "freecode" if "freecode_" in detail else "hermes",
                "detail": detail,
            }
        ]

    smoke_errors = _hermes_launcher_smoke(prepared.hermes_binary, site_packages=prepared.hermes_site_packages)
    if not smoke_errors and not _hermes_frozen_runtime_stable(prepared):
        smoke_errors.append("hermes_runtime_drift_after_smoke")
    if smoke_errors:
        _cleanup_prepared(prepared)
        return None, [{"code": "runtime_smoke_failed", "runtime": "hermes", "detail": ",".join(smoke_errors)}]
    return prepared, []


def _cleanup_prepared(prepared: PreparedJ4Runtimes) -> None:
    cleanup = getattr(prepared.cleanup_handle, "cleanup", None)
    if callable(cleanup):
        cleanup()
    elif prepared.cleanup_root is not None:
        shutil.rmtree(prepared.cleanup_root, ignore_errors=True)


def _prepared_runtime_blockers(
    prepared: PreparedJ4Runtimes,
    config: J4RuntimeConfig,
) -> list[dict[str, Any]]:
    """Re-hash mutable runtime inputs immediately around every adapter invocation."""

    blockers: list[dict[str, Any]] = []
    if not _freecode_prepared_runtime_stable(prepared, config):
        blockers.append({"code": "runtime_identity_drift", "runtime": "freecode"})
    if not _hermes_frozen_runtime_stable(prepared):
        blockers.append({"code": "runtime_identity_drift", "runtime": "hermes"})
    return blockers


def _freecode_prepared_runtime_stable(prepared: PreparedJ4Runtimes, config: J4RuntimeConfig) -> bool:
    del config
    artifact = prepared.freecode_manifest["artifact"]
    artifact_path = Path(artifact["path"])
    try:
        artifact_details = artifact_path.lstat()
    except OSError:
        artifact_details = None
    hook_python_environment, hook_python_environment_errors = _tree_identity(prepared.hermes_base_python_root)
    return bool(
        artifact_details is not None
        and stat.S_ISREG(artifact_details.st_mode)
        and not artifact_path.is_symlink()
        and artifact_details.st_size == artifact["size"]
        and _safe_sha256_file(artifact_path) == artifact["sha256"]
        and _sha256_json(prepared.freecode_build_receipt) == prepared.freecode_build_receipt_sha256
        and _safe_sha256_file(prepared.freecode_hook) == prepared.freecode_hook_sha256
        and _safe_sha256_file(prepared.freecode_hook_python) == prepared.freecode_hook_python_sha256
        and not hook_python_environment_errors
        and hook_python_environment.get("sha256") == prepared.freecode_hook_python_environment_sha256
    )


def _hermes_frozen_runtime_stable(prepared: PreparedJ4Runtimes) -> bool:
    source = prepared.hermes_binary["components"]["source"]
    source_sha, source_errors = _source_manifest_digest(
        prepared.hermes_source_root,
        set(prepared.hermes_source_paths),
    )
    python_identity, python_errors = _python_environment_identity(
        prepared.hermes_venv_python,
        base_root_override=prepared.hermes_base_python_root,
    )
    auth_profile = prepared.hermes_binary["components"].get("auth_profile", {})
    observed_auth_projection_sha = _sha256_json(prepared.hermes_auth_projection)
    observed_auth_run_sha = _sha256_bytes(
        prepared.hermes_auth_run_nonce + _canonical_json(prepared.hermes_auth_projection).encode("utf-8")
    )
    return not (
        source_errors
        or source_sha != source.get("sha256")
        or python_errors
        or python_identity.get("sha256") != prepared.hermes_binary["components"]["python"].get("sha256")
        or _safe_sha256_file(prepared.hermes_launcher) != prepared.hermes_binary["components"]["launcher"].get("sha256")
        or auth_profile != prepared.hermes_auth_profile
        or observed_auth_projection_sha != prepared.hermes_auth_projection_sha256
        or observed_auth_run_sha != auth_profile.get("projection_run_sha256")
        or _private_regular_file_sha256(prepared.hermes_auth_source) != prepared.hermes_auth_source_sha256
    )


def _hermes_launcher_smoke(
    identity: dict[str, Any],
    *,
    site_packages: Path | None = None,
) -> list[str]:
    """Prove the attested launcher can load Hermes without a provider-capable command."""

    components = identity.get("components") if isinstance(identity.get("components"), dict) else {}
    launcher = components.get("launcher") if isinstance(components.get("launcher"), dict) else {}
    source = components.get("source") if isinstance(components.get("source"), dict) else {}
    python_path = str(identity.get("path") or "")
    launcher_path = str(launcher.get("path") or "")
    source_root = str(source.get("root") or "")
    expected_version = str(identity.get("version") or "")
    if not all((python_path, launcher_path, source_root, expected_version)):
        return ["hermes_launcher_identity_incomplete"]
    if site_packages is None:
        python_component = components.get("python") if isinstance(components.get("python"), dict) else {}
        venv_root = Path(str(python_component.get("venv_root") or ""))
        candidates = sorted(path for path in (venv_root / "lib").glob("python*/site-packages") if path.is_dir())
        if len(candidates) != 1:
            return ["hermes_launcher_site_packages_unavailable"]
        site_packages = candidates[0]

    with tempfile.TemporaryDirectory(prefix="hive-j4-hermes-preflight-") as temporary:
        attempt_root = Path(temporary) / "attempt"
        workspace_root = attempt_root / "workspace"
        state_root = attempt_root / "state"
        isolated_directories = {
            "HERMES_HOME": state_root / "hermes-home",
            "HOME": state_root / "os-home",
            "CODEX_HOME": state_root / "codex-home",
            "HERMES_MANAGED_DIR": state_root / "managed",
            "TMPDIR": state_root / "tmp",
        }
        for directory in (workspace_root, state_root, *isolated_directories.values()):
            directory.mkdir(parents=True, exist_ok=True)
        source_version = expected_version.removeprefix("Hermes Agent v")
        result = _run_process(
            [
                python_path,
                "-I",
                "-S",
                launcher_path,
                "attest-runtime",
                "--expected-version",
                source_version,
            ],
            workspace_root,
            timeout_seconds=20,
            env_overrides={
                HERMES_J4_SOURCE_ROOT_ENV: source_root,
                HERMES_J4_STATE_DB_ENV: str(state_root / "state.db"),
                HERMES_J4_SITE_PACKAGES_ENV: str(site_packages),
                HERMES_J4_WORKSPACE_ROOT_ENV: str(workspace_root),
                "HERMES_WRITE_SAFE_ROOT": str(workspace_root),
                "PYTHONDONTWRITEBYTECODE": "1",
                **{name: str(path) for name, path in isolated_directories.items()},
                "TMP": str(isolated_directories["TMPDIR"]),
                "TEMP": str(isolated_directories["TMPDIR"]),
                "HERMES_SAFE_MODE": "1",
                "HERMES_IGNORE_USER_CONFIG": "1",
                "HERMES_IGNORE_RULES": "1",
                "HERMES_BUNDLED_SKILLS": str(state_root / "nonexistent-bundled-skills"),
                "PYTHONNOUSERSITE": "1",
            },
            env_remove=_HERMES_J4_AMBIENT_ENV_DENYLIST,
            require_workspace_sandbox=True,
            additional_writable_roots=(state_root,),
            network_access=False,
        )
    if result.sandbox.get("status") != "enforced":
        return ["hermes_launcher_sandbox_unavailable"]
    if result.returncode != 0:
        return ["hermes_launcher_runtime_attestation_failed"]
    try:
        attestation = _load_json_object(_ensure_text(result.stdout))
    except ValueError:
        return ["hermes_launcher_runtime_attestation_invalid"]
    if (
        attestation.get("ok") is not True
        or attestation.get("executable_match") is not True
        or attestation.get("prefix_match") is not True
        or attestation.get("stdlib_origins_match") is not True
        or attestation.get("hermes_origins_match") is not True
        or attestation.get("unexpected_loaded_origin_count") != 0
        or attestation.get("source_version") != source_version
        or attestation.get("errors") != []
    ):
        return ["hermes_launcher_runtime_unattested"]
    return []


def _unobserved_workspace_receipt(envelope: dict[str, Any], *, reason: str) -> dict[str, Any]:
    """Represent a workspace this attempt never observed as unknown evidence.

    A denied, unclaimed or replaced workspace root must not be traversed, read
    or hashed; the receipt records genuinely unobserved evidence instead of a
    fake empty observation, and never erases a valid owned observation.
    """

    limits = envelope["workspace"]
    return {
        "logical_root": limits["logical_root"],
        "local_path": None,
        "root_identity": None,
        "before_manifest": None,
        "before_sha256": None,
        "after_manifest": None,
        "after_sha256": None,
        "diff": None,
        "boundary_ok": None,
        "boundary_errors": [f"workspace_unobserved:{reason}"],
        "file_count": None,
        "total_bytes": None,
    }


def _workspace_receipt(
    workspace_root: Path,
    before: list[dict[str, Any]],
    *,
    envelope: dict[str, Any],
    declared_paths: list[str] | None = None,
    workspace_identity: tuple[int, int] | None = None,
) -> dict[str, Any]:
    if workspace_root.is_symlink():
        # The workspace root itself was replaced: any traversal would read and
        # hash foreign state, so the evidence is unobserved, not empty.
        return _unobserved_workspace_receipt(envelope, reason="workspace_root_symlink")
    if workspace_identity is None:
        # The clone never observed the root's identity, so ownership cannot
        # be verified at all: an unobserved identity must stay unknown
        # evidence instead of being upgraded by a traversal-time stat.
        return _unobserved_workspace_receipt(envelope, reason="workspace_identity_unobserved")
    if not _j4_attempt_root_owned(workspace_root, workspace_identity):
        # The path no longer resolves to the directory this attempt cloned:
        # an ancestor swap or a same-path replacement points elsewhere, so
        # the evidence is unobserved instead of trusted foreign bytes.
        return _unobserved_workspace_receipt(envelope, reason="workspace_root_replaced")
    after, errors = _manifest(workspace_root)
    unsafe_declared: list[str] = []
    declared_seen: set[str] = set()
    duplicate_declared: list[str] = []
    for path in declared_paths or []:
        try:
            safe = _safe_relative_path(path)
        except ValueError:
            unsafe_declared.append(path)
            continue
        if safe in declared_seen:
            duplicate_declared.append(safe)
        declared_seen.add(safe)
    limits = envelope["workspace"]
    total_bytes = sum(int(entry["size"]) for entry in after)
    boundary_ok = (
        not errors
        and not unsafe_declared
        and not duplicate_declared
        and len(after) <= int(limits["max_files"])
        and total_bytes <= int(limits["max_bytes"])
    )
    # Publish exactly the identity verified BEFORE the manifest traversal.
    # A replacement directory created during the walk must not lend its fresh
    # stat to this receipt: downstream scoring compares this tuple against
    # the live source, so only the pre-verified identity is trusted.
    root_identity: dict[str, int] = {"st_dev": workspace_identity[0], "st_ino": workspace_identity[1]}
    return {
        "logical_root": limits["logical_root"],
        "local_path": str(workspace_root.resolve()),
        "root_identity": root_identity,
        "before_manifest": before,
        "before_sha256": _sha256_json(before),
        "after_manifest": after,
        "after_sha256": _sha256_json(after),
        "diff": _workspace_diff(before, after),
        "boundary_ok": boundary_ok,
        "boundary_errors": [
            *errors,
            *[f"unsafe_declared_path:{path}" for path in unsafe_declared],
            *[f"duplicate_declared_path:{path}" for path in duplicate_declared],
        ],
        "file_count": len(after),
        "total_bytes": total_bytes,
    }


def _create_scoring_snapshot(
    *,
    output_dir: Path,
    runtime: str,
    envelope: dict[str, Any],
    receipt: dict[str, Any],
) -> tuple[Path | None, str, list[str]]:
    workspace = receipt.get("workspace") if isinstance(receipt.get("workspace"), dict) else {}
    # Score only the workspace the receipt verified by exact identity: a
    # replaced root (or a receipt that never observed one) must not be read,
    # hashed, or copied into the scoring tree, even if its content matches.
    observed_identity = workspace.get("root_identity") if isinstance(workspace.get("root_identity"), dict) else None
    source = _runtime_workspace_path(output_dir, runtime, envelope["envelope_id"])
    if observed_identity is None:
        return None, "", ["scoring_workspace_unverified"]
    try:
        source_details = source.lstat()
    except OSError:
        return None, "", ["scoring_source_unavailable"]
    if (
        source.is_symlink()
        or not stat.S_ISDIR(source_details.st_mode)
        or (source_details.st_dev, source_details.st_ino)
        != (observed_identity.get("st_dev"), observed_identity.get("st_ino"))
    ):
        return None, "", ["scoring_source_replaced"]
    observed, errors = _manifest(source)
    if errors or observed != workspace.get("after_manifest") or _sha256_json(observed) != workspace.get("after_sha256"):
        return None, "", ["scoring_source_drift"]
    envelope_root = output_dir / "j4_scoring" / runtime / str(envelope["envelope_id"])
    destination = envelope_root / "workspace"
    if destination.exists() or destination.is_symlink():
        return None, "", ["scoring_snapshot_exists"]
    claim_error = _claim_owned_output_subtree(output_dir, ("j4_scoring", runtime))
    if claim_error == "unsupported":
        return None, "", ["scoring_destination_unsupported"]
    if claim_error is not None:
        return None, "", ["scoring_destination_unavailable"]
    try:
        # The per-envelope scoring leaf is exclusive, exactly like the
        # attempt root: a pre-existing entry is a typed conflict, never a
        # foreign destination to copy scoring input into.
        envelope_root.mkdir(mode=0o700)
        destination.mkdir(mode=0o700)
    except FileExistsError:
        return None, "", ["scoring_snapshot_exists"]
    except OSError:
        return None, "", ["scoring_destination_unavailable"]
    try:
        shutil.copytree(source, destination, copy_function=shutil.copy2, dirs_exist_ok=True)
        snapshot, snapshot_errors = _manifest(destination)
    except (OSError, shutil.Error):
        shutil.rmtree(destination, ignore_errors=True)
        return None, "", ["scoring_snapshot_failed"]
    if snapshot_errors or snapshot != observed:
        shutil.rmtree(destination, ignore_errors=True)
        return None, "", ["scoring_snapshot_mismatch"]
    return destination, _sha256_json(snapshot), []


def _base_receipt(
    *,
    runtime: str,
    binary: dict[str, Any],
    envelope: dict[str, Any],
    envelope_sha256: str,
    status: str,
    argv: list[str],
    duration_ms: int,
    exit_code: int | None,
    workspace: dict[str, Any],
    artifacts: dict[str, Any],
    parsed_payload: dict[str, Any] | None = None,
    schema_errors: list[str] | None = None,
    turns: int | None = None,
    tokens: int | None = None,
    observed_cost: float | None = None,
    effective_model: str | None = None,
    effective_provider: str | None = None,
    fallback_observed: bool | None = None,
    attestation_source: str | None = None,
    effective_resources: dict[str, Any] | None = None,
    resource_sources: dict[str, str] | None = None,
    authority: dict[str, Any] | None = None,
    route_attestation: dict[str, Any] | None = None,
    execution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in J4_STATUSES:
        raise ValueError(f"Unsupported J4 status: {status}")
    return {
        "schema": J4_RECEIPT_SCHEMA,
        "runtime": runtime,
        "binary": binary,
        "envelope_sha256": envelope_sha256,
        "seed_sha256": envelope["workspace"]["seed_sha256"],
        "scorer_sha256": envelope["scorer"]["source_sha256"],
        "scorer_loaded_code_sha256": envelope["scorer"]["loaded_code_sha256"],
        "status": status,
        "argv": list(argv),
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "turns": turns,
        "tokens": tokens,
        "observed_cost": observed_cost,
        "requested_model": J4_MODEL,
        "effective_model": effective_model,
        "requested_provider": envelope["model"]["allowed_provider_routes"][runtime][0],
        "effective_provider": effective_provider,
        "fallback_observed": fallback_observed,
        "attestation_source": attestation_source,
        "resources": {
            "requested": {
                "max_tool_rounds": envelope["resources"]["max_tool_rounds"],
                "wall_clock_seconds": envelope["resources"]["wall_clock_seconds"],
                "reasoning_effort": envelope["model"]["reasoning_effort"],
            },
            "effective": effective_resources,
            "sources": resource_sources or {},
        },
        "authority": authority,
        "route_attestation": route_attestation
        or {
            "call_count": 0,
            "routes": [],
            "source": None,
        },
        "execution": execution or {},
        "workspace": workspace,
        "artifacts": artifacts,
        "parsed": {
            "payload": parsed_payload,
            "schema_valid": parsed_payload is not None and not (schema_errors or []),
            "schema_errors": schema_errors or [],
        },
        "score": None,
    }


def _expected_resources(envelope: dict[str, Any]) -> dict[str, Any]:
    return {
        "max_tool_rounds": envelope["resources"]["max_tool_rounds"],
        "wall_clock_seconds": envelope["resources"]["wall_clock_seconds"],
        "reasoning_effort": envelope["model"]["reasoning_effort"],
    }


def _argv_value(command: list[str], flag: str) -> str | None:
    positions = [index for index, value in enumerate(command) if value == flag]
    if len(positions) != 1:
        return None
    index = positions[0]
    return command[index + 1] if index + 1 < len(command) else None


def _cli_resource_attestation(
    runtime: str,
    command: list[str],
    envelope: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, str]]:
    expected = _expected_resources(envelope)
    max_turns = _argv_value(command, "--max-turns")
    reasoning_flag = "--effort" if runtime == "freecode" else "--reasoning"
    reasoning = _argv_value(command, reasoning_flag)
    run_budget = (
        str(envelope["resources"]["wall_clock_seconds"])
        if runtime == "freecode"
        else _argv_value(command, "--run-budget")
    )
    effective = {
        "max_tool_rounds": int(max_turns) if max_turns and max_turns.isdigit() else None,
        "wall_clock_seconds": int(run_budget) if run_budget and run_budget.isdigit() else None,
        "reasoning_effort": reasoning,
    }
    sources = {
        "max_tool_rounds": f"{runtime}.argv.--max-turns",
        "wall_clock_seconds": (
            "adapter.subprocess_timeout"
            if runtime == "freecode"
            else "hermes.argv.--run-budget+adapter.subprocess_timeout"
        ),
        "reasoning_effort": f"{runtime}.argv.{reasoning_flag}",
    }
    return (effective if effective == expected else None), sources


def _cli_authority_attestation(
    runtime: str,
    command: list[str],
    envelope: dict[str, Any],
    sandbox: dict[str, Any] | None,
    *,
    workspace_root: Path,
    invocation_env: dict[str, str],
    runtime_identity: dict[str, Any],
) -> dict[str, Any] | None:
    expected_tools = list(envelope["authority"]["allowed_tools"][runtime])
    if not isinstance(sandbox, dict) or sandbox.get("status") != "enforced":
        return None
    if runtime == "freecode":
        configured = (_argv_value(command, "--tools") or "").split(",")
        tools_source = "freecode.argv.--tools"
        components = runtime_identity.get("components") if isinstance(runtime_identity.get("components"), dict) else {}
        guard = components.get("authority_guard") if isinstance(components.get("authority_guard"), dict) else {}
        try:
            settings = json.loads(_argv_value(command, "--settings") or "")
            hooks = settings["hooks"]["PreToolUse"]
            hook_command = hooks[0]["hooks"][0]["command"]
        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
            hook_command = ""
        command_scope_ok = (
            configured == expected_tools
            and _argv_value(command, "--permission-mode") == "dontAsk"
            and "--dangerously-skip-permissions" not in command
            and str(guard.get("path") or "") in hook_command
            and str(guard.get("python_path") or "") in hook_command
            and invocation_env.get("HIVE_J4_WORKSPACE_ROOT") == str(workspace_root.resolve())
            and invocation_env.get("HIVE_J4_FREECODE_HOOK_LOG")
        )
        guard_source = "freecode.PreToolUse.workspace_guard"
    else:
        configured = list(_J4_ALLOWED_TOOLS["hermes"]) if _argv_value(command, "-t") == "file" else []
        tools_source = "hermes.argv.-t=file"
        state_root = _hermes_state_root(workspace_root, str(envelope["envelope_id"])).resolve()
        expected_attempt_env = {
            "HERMES_HOME": str(state_root / "hermes-home"),
            "HOME": str(state_root / "os-home"),
            "CODEX_HOME": str(state_root / "codex-home"),
            "HERMES_MANAGED_DIR": str(state_root / "managed"),
            "TMPDIR": str(state_root / "tmp"),
            "TMP": str(state_root / "tmp"),
            "TEMP": str(state_root / "tmp"),
            "HERMES_SAFE_MODE": "1",
            "HERMES_IGNORE_USER_CONFIG": "1",
            "HERMES_IGNORE_RULES": "1",
            "HERMES_BUNDLED_SKILLS": str(state_root / "nonexistent-bundled-skills"),
            "PYTHONNOUSERSITE": "1",
        }
        command_scope_ok = (
            "--safe-mode" in command
            and "--yolo" not in command
            and invocation_env.get(HERMES_J4_WORKSPACE_ROOT_ENV) == str(workspace_root.resolve())
            and invocation_env.get("HERMES_WRITE_SAFE_ROOT") == str(workspace_root.resolve())
            and all(invocation_env.get(name) == value for name, value in expected_attempt_env.items())
            and not invocation_env.get("HERMES_CODEX_BASE_URL")
        )
        guard_source = "hermes.launcher.workspace_guard+attempt_local_environment"
    if configured != expected_tools or not command_scope_ok:
        return None
    requested = {
        "allowed_tools": expected_tools,
        "readable_scope": envelope["authority"]["readable_scope"],
        "writable_scope": envelope["authority"]["writable_scope"],
    }
    return {
        "requested": requested,
        "effective": dict(requested),
        "sources": {
            "allowed_tools": tools_source,
            "readable_scope": guard_source,
            "writable_scope": f"os_sandbox:{sandbox.get('provider') or 'unknown'}",
        },
        "sandbox": dict(sandbox),
        "runtime_private_writable_roots": [
            path for path in sandbox.get("writable_roots", []) if path != str(workspace_root.resolve())
        ],
    }


def _hive_session_authority(
    session_payload: Any,
    *,
    remote_root: str,
    envelope: dict[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(session_payload, dict):
        return None
    profile = session_payload.get("permission_profile")
    if not isinstance(profile, dict):
        return None
    expected_tools = list(envelope["authority"]["allowed_tools"]["hive"])
    mode = profile.get("mode")
    allowed_tools = profile.get("allowed_tools")
    writable_roots = profile.get("writable_roots")
    readable_roots = profile.get("readable_roots")
    capability_snapshot = profile.get("capability_policy_snapshot")
    if not isinstance(allowed_tools, list) or not all(isinstance(value, str) for value in allowed_tools):
        return None
    if not isinstance(writable_roots, list) or not all(isinstance(value, str) for value in writable_roots):
        return None
    if (
        mode != "bypassPermissions"
        or len(allowed_tools) != len(expected_tools)
        or set(allowed_tools) != set(expected_tools)
        or writable_roots != [remote_root]
        or readable_roots != [remote_root]
        or not isinstance(capability_snapshot, dict)
        or capability_snapshot.get("session_exact_scope") is not True
    ):
        return None
    requested = {
        "allowed_tools": expected_tools,
        "readable_scope": envelope["authority"]["readable_scope"],
        "writable_scope": envelope["authority"]["writable_scope"],
    }
    return {
        "requested": requested,
        "effective": dict(requested),
        "sources": {
            "allowed_tools": "hive.session.permission_profile.allowed_tools",
            "readable_scope": "hive.session.permission_profile.readable_roots",
            "writable_scope": "hive.session.permission_profile.writable_roots",
        },
        "sandbox": {
            "status": "enforced",
            "provider": "hive.session.permission_profile",
        },
    }


def _freecode_command(
    *,
    prompt: str,
    envelope: dict[str, Any],
    workspace_root: Path,
    binary: Path,
    hook: Path,
    hook_python: Path,
) -> list[str]:
    guard_command = f"{shlex.quote(str(hook_python))} -I -S {shlex.quote(str(hook))}"
    settings = {
        "hooks": {
            "PreToolUse": [
                {
                    "matcher": "Read|Write|Edit|Glob|Grep",
                    "hooks": [{"type": "command", "command": guard_command, "timeout": 5}],
                }
            ]
        }
    }
    return [
        str(binary),
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        _canonical_json(_RUNTIME_JSON_SCHEMA),
        "--model",
        J4_MODEL,
        "--effort",
        J4_REASONING_EFFORT,
        "--max-turns",
        str(envelope["resources"]["max_tool_rounds"]),
        "--max-budget-usd",
        str(envelope["resources"]["runtime_extra_guards"]["freecode"]["max_budget_usd"]),
        "--tools",
        "Read,Write,Edit,Glob,Grep",
        "--permission-mode",
        "dontAsk",
        "--settings",
        _canonical_json(settings),
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--no-chrome",
        prompt,
    ]


def _hermes_command(
    *,
    workspace_root: Path,
    envelope: dict[str, Any],
    python: Path,
    launcher: Path = HERMES_J4_LAUNCHER,
) -> list[str]:
    return [
        str(Path(os.path.abspath(python.expanduser()))),
        "-I",
        "-S",
        str(launcher.resolve()),
        "chat",
        "--query-file",
        "prompt.txt",
        "--oneshot",
        "-Q",
        "--in",
        str(workspace_root.resolve()),
        "-m",
        J4_MODEL,
        "--provider",
        "openai-codex",
        "--reasoning",
        J4_REASONING_EFFORT,
        "-t",
        "file",
        "--max-turns",
        str(envelope["resources"]["max_tool_rounds"]),
        "--run-budget",
        str(envelope["resources"]["wall_clock_seconds"]),
        "--safe-mode",
        "--source",
        "p08-j4",
    ]


def _freecode_attestation(
    outer: dict[str, Any],
    *,
    invocation_env: dict[str, str] | None = None,
) -> tuple[str | None, str | None, bool | None, int | None, int | None, float | None]:
    usage = outer.get("modelUsage")
    if not isinstance(usage, dict) or not usage:
        return None, None, None, None, None, None
    models = {_normalize_model_id(key) for key in usage}
    effective_model = next(iter(models)) if len(models) == 1 else None
    # FreeCode's Codex adapter records the selected model as a bare key (for
    # example ``gpt-5.4``). The provider route is independently pinned by the
    # adapter's CLAUDE_CODE_USE_OPENAI=1 invocation. Namespaced OpenAI keys are
    # accepted for compatibility, while another explicit namespace is not.
    usage_contract_attested = all(
        not any(separator in str(key).strip().lower() for separator in ("/", ":"))
        or str(key).strip().lower().startswith(("openai/", "openai:"))
        for key in usage
    )
    provider_attested = (
        isinstance(invocation_env, dict)
        and invocation_env.get("CLAUDE_CODE_USE_OPENAI") == "1"
        and usage_contract_attested
    )
    effective_provider = "chatgpt-codex" if provider_attested else None
    fallback_observed = len(models) != 1 or effective_model != J4_MODEL or effective_provider != "chatgpt-codex"
    turns_value = outer.get("num_turns")
    turns = turns_value if isinstance(turns_value, int) and not isinstance(turns_value, bool) else None
    tokens = _usage_total(
        usage,
        {"inputtokens", "outputtokens", "cachecreationinputtokens", "cachereadinputtokens"},
    )
    raw_cost = outer.get("total_cost_usd")
    observed_cost = float(raw_cost) if isinstance(raw_cost, (int, float)) else None
    return effective_model, effective_provider, fallback_observed, turns, tokens, observed_cost


def _cli_failure_status(result: ProcessRunResult, *, runtime: str) -> str:
    if result.returncode == 124:
        return "timeout"
    if result.returncode == 126:
        return "sandbox_unavailable"
    if result.returncode == 127:
        return "cli_unavailable"
    lines = [line.strip().lower() for line in f"{result.stdout}\n{result.stderr}".splitlines() if line.strip()]
    auth_markers = (
        "authentication required",
        "not logged in",
        "no credentials for openai-codex",
        "no valid credentials",
        "oauth authentication required",
    )
    if runtime in {"freecode", "hermes"} and any(marker in line for line in lines for marker in auth_markers):
        return "auth_required"
    return "failed"


def _freecode_hook_log_attestation(text: str) -> tuple[dict[str, Any], list[str]]:
    event_count = 0
    denied_count = 0
    errors: list[str] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        event_count += 1
        try:
            record = _load_json_object(line)
        except ValueError:
            errors.append("hook_log_jsonl")
            continue
        if set(record) != {"allowed", "resolved_relative_path", "tool_name", "tool_use_id_hash"}:
            errors.append("hook_log_schema")
            continue
        allowed = record.get("allowed")
        relative = record.get("resolved_relative_path")
        tool_name = record.get("tool_name")
        call_hash = record.get("tool_use_id_hash")
        relative_valid = relative == "."
        if isinstance(relative, str) and relative not in {".", "<outside>", "<invalid>"}:
            try:
                relative_valid = _safe_relative_path(relative) == relative
            except ValueError:
                relative_valid = False
        if (
            not isinstance(allowed, bool)
            or tool_name not in {*_J4_ALLOWED_TOOLS["freecode"], "<invalid>"}
            or not re.fullmatch(r"[0-9a-f]{64}", str(call_hash or ""))
            or (allowed and (tool_name == "<invalid>" or not relative_valid))
            or (not allowed and relative not in {"<outside>", "<invalid>"})
        ):
            errors.append("hook_log_schema")
        if allowed is False:
            denied_count += 1
    return {
        "event_count": event_count,
        "denied_count": denied_count,
        "sha256": _sha256_bytes(text.encode("utf-8")),
        "valid": not errors,
    }, sorted(set(errors))


def _run_freecode_j4_attempt(
    scenario: ScenarioWorkspace,
    envelope: dict[str, Any],
    envelope_sha256: str,
    *,
    output_dir: Path,
    config: J4RuntimeConfig,
    prepared: PreparedJ4Runtimes | None = None,
) -> dict[str, Any]:
    workspace_root = _runtime_workspace_path(output_dir, "freecode", envelope["envelope_id"])
    before, clone_errors, workspace_identity = _clone_seed(scenario.workspace_dir, workspace_root)
    if prepared is None:
        binary: dict[str, Any] = {
            "path": "",
            "version": "",
            "sha256": "",
            "revision": "",
            "runtime_sha256": "",
            "components": {},
        }
        binary_path = Path("freecode")
        frozen_hook = FREECODE_J4_HOOK
        freeze_errors = ["prepared_runtime_required"]
    else:
        binary, binary_path, frozen_hook, freeze_errors = _freeze_freecode_runtime(
            prepared,
            workspace_root=workspace_root,
        )
        if not _freecode_prepared_runtime_stable(prepared, config):
            freeze_errors.append("runtime_identity_drift")
    hook_python = prepared.freecode_hook_python if prepared is not None else Path(sys.executable)
    command = _freecode_command(
        prompt=scenario.prompt,
        envelope=envelope,
        workspace_root=workspace_root,
        binary=binary_path,
        hook=frozen_hook,
        hook_python=hook_python,
    )
    if clone_errors or freeze_errors:
        artifacts, artifacts_errors = _write_j4_artifacts(
            output_dir,
            runtime="freecode",
            envelope_id=envelope["envelope_id"],
            stdout="",
            stderr="",
        )
        workspace = _workspace_receipt(workspace_root, before, envelope=envelope, workspace_identity=workspace_identity)
        return _base_receipt(
            runtime="freecode",
            binary=binary,
            envelope=envelope,
            envelope_sha256=envelope_sha256,
            status="attestation_failed",
            argv=command,
            duration_ms=0,
            exit_code=None,
            workspace=workspace,
            artifacts=artifacts,
            schema_errors=[*clone_errors, *freeze_errors, *artifacts_errors, "binary_identity"],
        )

    assert prepared is not None
    state_root = workspace_root.parent / "state"
    hook_log = state_root / "hook.jsonl"
    temporary = state_root / "tmp"
    freecode_env = {
        "CLAUDE_CODE_USE_OPENAI": "1",
        "HIVE_J4_WORKSPACE_ROOT": str(workspace_root.resolve()),
        "HIVE_J4_FREECODE_HOOK_LOG": str(hook_log.resolve()),
        "TMPDIR": str(temporary.resolve()),
        "TMP": str(temporary.resolve()),
        "TEMP": str(temporary.resolve()),
    }
    pre_binary_sha = _sha256_file(binary_path)
    pre_hook_sha = _sha256_file(frozen_hook)
    pre_hook_python_sha = _safe_sha256_file(hook_python)
    pre_hook_python_environment, pre_hook_python_environment_errors = _tree_identity(prepared.hermes_base_python_root)
    result = _run_process(
        command,
        workspace_root,
        timeout_seconds=envelope["resources"]["wall_clock_seconds"],
        env_overrides=freecode_env,
        require_workspace_sandbox=True,
        additional_writable_roots=(state_root,),
    )
    post_binary_sha = _sha256_file(binary_path) if binary_path.is_file() else ""
    post_hook_sha = _sha256_file(frozen_hook) if frozen_hook.is_file() else ""
    post_hook_python_sha = _safe_sha256_file(hook_python)
    post_hook_python_environment, post_hook_python_environment_errors = _tree_identity(prepared.hermes_base_python_root)
    runtime_stable = (
        pre_binary_sha == post_binary_sha == binary.get("sha256")
        and pre_hook_sha == post_hook_sha == binary.get("components", {}).get("authority_guard", {}).get("sha256")
        and pre_hook_python_sha
        == post_hook_python_sha
        == binary.get("components", {}).get("authority_guard", {}).get("python_sha256")
        and not pre_hook_python_environment_errors
        and not post_hook_python_environment_errors
        and pre_hook_python_environment.get("sha256")
        == post_hook_python_environment.get("sha256")
        == binary.get("components", {}).get("authority_guard", {}).get("python_environment_sha256")
    )
    effective_resources, resource_sources = _cli_resource_attestation("freecode", command, envelope)
    authority = _cli_authority_attestation(
        "freecode",
        command,
        envelope,
        result.sandbox,
        workspace_root=workspace_root,
        invocation_env=freecode_env,
        runtime_identity=binary,
    )
    try:
        hook_log_text = hook_log.read_text(encoding="utf-8") if hook_log.is_file() else ""
    except (OSError, UnicodeDecodeError):
        hook_log_text = ""
        hook_log_errors = ["hook_log_unreadable"]
    else:
        hook_log_errors = []
    hook_log_attestation, observed_hook_log_errors = _freecode_hook_log_attestation(hook_log_text)
    hook_log_errors.extend(observed_hook_log_errors)
    artifacts, artifacts_errors = _write_j4_artifacts(
        output_dir,
        runtime="freecode",
        envelope_id=envelope["envelope_id"],
        stdout=_ensure_text(result.stdout),
        stderr=_ensure_text(result.stderr),
        transcript=hook_log_text,
    )
    payload: dict[str, Any] | None = None
    schema_errors: list[str] = []
    schema_errors.extend(artifacts_errors)
    schema_errors.extend(hook_log_errors)
    effective_model: str | None = None
    fallback_observed: bool | None = None
    turns: int | None = None
    tokens: int | None = None
    observed_cost: float | None = None
    status = _cli_failure_status(result, runtime="freecode") if result.returncode else "completed"
    if not runtime_stable:
        schema_errors.append("runtime_identity_drift")
        status = "attestation_failed"
    if result.returncode == 0:
        try:
            outer = _load_json_object(_ensure_text(result.stdout))
        except ValueError:
            outer = {}
            schema_errors.append("whole_output_json")
        if outer.get("is_error") is not False or str(outer.get("subtype") or "") != "success":
            schema_errors.append("outer_success")
        payload = outer.get("structured_output") if isinstance(outer.get("structured_output"), dict) else None
        schema_errors.extend(_validate_runtime_payload(payload))
        (
            effective_model,
            effective_provider,
            fallback_observed,
            turns,
            tokens,
            observed_cost,
        ) = _freecode_attestation(outer, invocation_env=freecode_env)
        if (
            effective_model != J4_MODEL
            or effective_provider != "chatgpt-codex"
            or fallback_observed is not False
            or not isinstance(turns, int)
            or turns < 1
            or not isinstance(tokens, int)
            or tokens < 1
        ):
            schema_errors.append("model_usage_attestation")
        if effective_resources is None:
            schema_errors.append("resource_attestation")
        if authority is None:
            schema_errors.append("authority_attestation")
        if schema_errors:
            if "authority_attestation" in schema_errors:
                status = "sandbox_unavailable"
            elif "resource_attestation" in schema_errors:
                status = "resource_unavailable"
            else:
                status = (
                    "attestation_failed"
                    if any(
                        "attestation" in error or "runtime_identity" in error or error.startswith("hook_log")
                        for error in schema_errors
                    )
                    else "invalid_output"
                )
    workspace = _workspace_receipt(
        workspace_root,
        before,
        envelope=envelope,
        declared_paths=payload.get("files_created") if isinstance(payload, dict) else None,
        workspace_identity=workspace_identity,
    )
    if status == "completed" and not workspace["boundary_ok"]:
        status = "sandbox_unavailable"
    receipt = _base_receipt(
        runtime="freecode",
        binary=binary,
        envelope=envelope,
        envelope_sha256=envelope_sha256,
        status=status,
        argv=command,
        duration_ms=result.duration_ms,
        exit_code=result.returncode,
        workspace=workspace,
        artifacts=artifacts,
        parsed_payload=payload,
        schema_errors=schema_errors,
        turns=turns,
        tokens=tokens,
        observed_cost=observed_cost,
        effective_model=effective_model,
        effective_provider=effective_provider,
        fallback_observed=fallback_observed,
        attestation_source=(
            f"freecode.stdout.modelUsage+adapter.env.CLAUDE_CODE_USE_OPENAI=1+{FREECODE_CODEX_PROVIDER_CONTRACT}"
            if effective_model
            else None
        ),
        effective_resources=effective_resources,
        resource_sources=resource_sources,
        authority=authority,
        route_attestation={
            "call_count": 1 if effective_model and isinstance(tokens, int) and tokens > 0 else 0,
            "count_semantics": "minimum_observed",
            "routes": (
                [{"model": effective_model, "provider": effective_provider}]
                if effective_model and effective_provider
                else []
            ),
            "source": (
                f"freecode.stdout.modelUsage+adapter.env.CLAUDE_CODE_USE_OPENAI=1+{FREECODE_CODEX_PROVIDER_CONTRACT}"
            ),
            "provider_binding": {
                "route": "chatgpt-codex",
                "invocation_env": {"name": "CLAUDE_CODE_USE_OPENAI", "value": "1"},
                "source_contract": FREECODE_CODEX_PROVIDER_CONTRACT,
                "attested": effective_provider == "chatgpt-codex",
            },
        },
        execution={
            "chat_spawn_count": 1,
            "argv_sha256": _sha256_json(command),
            "cwd": str(workspace_root.resolve()),
            "workspace_flag": None,
            "runtime_pre_sha256": pre_binary_sha,
            "runtime_post_sha256": post_binary_sha,
            "guard_pre_sha256": pre_hook_sha,
            "guard_post_sha256": post_hook_sha,
            "guard_python_pre_sha256": pre_hook_python_sha,
            "guard_python_post_sha256": post_hook_python_sha,
            "guard_python_environment_pre_sha256": pre_hook_python_environment.get("sha256") or "",
            "guard_python_environment_post_sha256": post_hook_python_environment.get("sha256") or "",
            "hook_log": hook_log_attestation,
        },
    )
    return receipt


def _run_freecode_j4(
    scenario: ScenarioWorkspace,
    envelope: dict[str, Any],
    envelope_sha256: str,
    *,
    output_dir: Path,
    config: J4RuntimeConfig,
    prepared: PreparedJ4Runtimes | None = None,
) -> dict[str, Any]:
    """Run FreeCode and clean only the runtime/state roots owned by this attempt."""

    workspace_root = _runtime_workspace_path(output_dir, "freecode", envelope["envelope_id"])
    attempt_root, attempt_identity, claim_error = _claim_j4_attempt_root(
        output_dir,
        "freecode",
        str(envelope["envelope_id"]),
    )
    runtime_root = attempt_root / "runtime"
    state_root = attempt_root / "state"

    def blocked_receipt(*, status: str, schema_error: str) -> dict[str, Any]:
        binary = {
            "path": "",
            "version": "",
            "sha256": "",
            "revision": "",
            "runtime_sha256": "",
            "components": {},
        }
        command = _freecode_command(
            prompt=scenario.prompt,
            envelope=envelope,
            workspace_root=workspace_root,
            binary=runtime_root / "freecode",
            hook=runtime_root / "freecode_j4_hook.py",
            hook_python=(prepared.freecode_hook_python if prepared is not None else Path(sys.executable)),
        )
        return _base_receipt(
            runtime="freecode",
            binary=binary,
            envelope=envelope,
            envelope_sha256=envelope_sha256,
            status=status,
            argv=command,
            duration_ms=0,
            exit_code=None,
            workspace=_unobserved_workspace_receipt(envelope, reason="unclaimed_attempt_root"),
            artifacts={},
            schema_errors=[schema_error],
            execution={"attempt_roots_owned": False, "state_cleanup_verified": False},
        )

    if claim_error is not None or attempt_identity is None:
        status, schema_error = {
            "attempt_root_conflict": ("needs_reconciliation", "freecode_attempt_state_conflict"),
            "attempt_parent_unsupported": ("needs_reconciliation", "freecode_attempt_boundary_unsupported"),
            "attempt_root_unsupported": ("needs_reconciliation", "freecode_attempt_boundary_unsupported"),
        }.get(str(claim_error), ("resource_unavailable", "freecode_attempt_state_unavailable"))
        return blocked_receipt(status=status, schema_error=schema_error)

    owned_children: list[tuple[Path, tuple[int, int]]] = []
    try:
        for root in (runtime_root, state_root):
            root.mkdir(mode=0o700, exist_ok=False)
            details = root.lstat()
            owned_children.append((root, (details.st_dev, details.st_ino)))
    except OSError as exc:
        # Roll back only children this attempt created and still owns by exact
        # identity; a replaced child is foreign state and must survive.
        setup_cleanup_errors: list[str] = []
        for root, identity in reversed(owned_children):
            outcome = _remove_owned_j4_root(root, identity)
            if outcome == "ambiguous":
                setup_cleanup_errors.append("freecode_attempt_state_cleanup_ambiguous")
            elif outcome == "failed":
                setup_cleanup_errors.append("freecode_attempt_state_cleanup_failed")
        conflict = isinstance(exc, FileExistsError)
        receipt = blocked_receipt(
            status="needs_reconciliation" if conflict else "resource_unavailable",
            schema_error="freecode_attempt_state_conflict" if conflict else "freecode_attempt_state_unavailable",
        )
        execution = receipt["execution"]
        execution["attempt_roots_owned"] = _j4_attempt_root_owned(attempt_root, attempt_identity)
        execution["state_cleanup_verified"] = not setup_cleanup_errors
        if setup_cleanup_errors:
            parsed = receipt["parsed"]
            parsed["schema_errors"] = sorted({*parsed["schema_errors"], *setup_cleanup_errors})
            parsed["schema_valid"] = False
        return receipt

    receipt: dict[str, Any] | None = None
    cleanup_errors: list[str] = []
    owned_at_cleanup = True
    try:
        receipt = _run_freecode_j4_attempt(
            scenario,
            envelope,
            envelope_sha256,
            output_dir=output_dir,
            config=config,
            prepared=prepared,
        )
    finally:
        owned_at_cleanup = _j4_attempt_root_owned(attempt_root, attempt_identity)
        if owned_at_cleanup:
            for root, identity in reversed(owned_children):
                outcome = _remove_owned_j4_root(root, identity)
                if outcome == "ambiguous":
                    cleanup_errors.append("freecode_attempt_state_cleanup_ambiguous")
                elif outcome == "failed":
                    cleanup_errors.append("freecode_attempt_state_cleanup_failed")
        else:
            cleanup_errors.append("freecode_attempt_state_cleanup_ambiguous")
    assert receipt is not None
    execution = receipt.setdefault("execution", {})
    execution["attempt_roots_owned"] = owned_at_cleanup
    execution["state_cleanup_verified"] = not cleanup_errors
    if cleanup_errors:
        receipt["status"] = "needs_reconciliation"
        parsed = receipt.setdefault("parsed", {})
        parsed["schema_errors"] = sorted(set([*(parsed.get("schema_errors") or []), *cleanup_errors]))
        parsed["schema_valid"] = False
    return receipt


def _hermes_session_id(stderr: str) -> str | None:
    matches = re.findall(r"(?mi)^session_id:\s*([a-z0-9][a-z0-9._:-]*)\s*$", stderr)
    return matches[0] if len(matches) == 1 else None


def _parse_jsonl(text: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        payload = _load_json_object(line)
        records.append(payload)
    if not records:
        raise ValueError("Hermes transcript export is empty.")
    return records


def _decoded_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _hermes_attestation(
    records: list[dict[str, Any]],
) -> tuple[str | None, str | None, bool | None, int, int, float | None, bool]:
    routes: set[tuple[str, str]] = set()
    fallback_observed = False
    attribution_complete = True
    seen_call_segments: set[str] = set()
    turns = 0
    tokens = 0
    costs: list[float] = []
    for record in records:
        sessions = record.get("segments") if isinstance(record.get("segments"), list) else [record]
        for session in sessions:
            if not isinstance(session, dict):
                attribution_complete = False
                continue
            model = _normalize_model_id(session.get("model"))
            provider = str(session.get("billing_provider") or "").strip().lower()
            model_config = _decoded_mapping(session.get("model_config"))
            model = model or _normalize_model_id(model_config.get("model"))
            provider = provider or str(model_config.get("provider") or "").strip().lower()
            fallback_observed = fallback_observed or any(
                bool(model_config.get(key))
                for key in ("fallback_model", "fallback_provider", "_fallback_activated", "fallback_activated")
            )
            api_calls = session.get("api_call_count")
            session_tokens = _usage_total(session, {"inputtokens", "outputtokens", "cachetokens"})
            tokens += session_tokens
            cost = session.get("actual_cost_usd")
            if not isinstance(cost, (int, float)):
                cost = session.get("estimated_cost_usd") or session.get("cost_usd")
            if isinstance(cost, (int, float)):
                costs.append(float(cost))
            valid_call_count = isinstance(api_calls, int) and not isinstance(api_calls, bool) and api_calls >= 0
            if valid_call_count and api_calls > 0:
                call_segment_sha256 = _sha256_json(session)
                if call_segment_sha256 in seen_call_segments:
                    attribution_complete = False
                else:
                    seen_call_segments.add(call_segment_sha256)
                    turns += api_calls
                    if model and provider:
                        routes.add((model, provider))
                    else:
                        attribution_complete = False
            elif (
                (not valid_call_count and api_calls is not None)
                or session_tokens > 0
                or (isinstance(cost, (int, float)) and float(cost) > 0)
            ):
                attribution_complete = False
    if len(routes) != 1:
        return None, None, True if routes else None, turns, tokens, sum(costs) if costs else None, False
    model, provider = next(iter(routes))
    fallback_observed = fallback_observed or model != J4_MODEL or provider != "openai-codex"
    return model, provider, fallback_observed, turns, tokens, sum(costs) if costs else None, attribution_complete


def _run_hermes_j4_attempt(
    scenario: ScenarioWorkspace,
    envelope: dict[str, Any],
    envelope_sha256: str,
    *,
    output_dir: Path,
    config: J4RuntimeConfig,
    prepared: PreparedJ4Runtimes | None = None,
) -> dict[str, Any]:
    del config
    workspace_root = _runtime_workspace_path(output_dir, "hermes", envelope["envelope_id"])
    before, clone_errors, workspace_identity = _clone_seed(scenario.workspace_dir, workspace_root)
    binary = prepared.hermes_binary if prepared is not None else _empty_hermes_runtime_identity()
    identity_errors = [] if prepared is not None else ["prepared_runtime_required"]
    python_path = prepared.hermes_python if prepared is not None else Path("hermes-python")
    launcher_path = prepared.hermes_launcher if prepared is not None else HERMES_J4_LAUNCHER
    command = _hermes_command(
        workspace_root=workspace_root,
        envelope=envelope,
        python=python_path,
        launcher=launcher_path,
    )
    if clone_errors or identity_errors:
        artifacts, artifacts_errors = _write_j4_artifacts(
            output_dir,
            runtime="hermes",
            envelope_id=envelope["envelope_id"],
            stdout="",
            stderr="",
        )
        return _base_receipt(
            runtime="hermes",
            binary=binary,
            envelope=envelope,
            envelope_sha256=envelope_sha256,
            status="cli_unavailable" if "hermes_python_unavailable" in identity_errors else "attestation_failed",
            argv=command,
            duration_ms=0,
            exit_code=None,
            workspace=_workspace_receipt(
                workspace_root, before, envelope=envelope, workspace_identity=workspace_identity
            ),
            artifacts=artifacts,
            schema_errors=[*clone_errors, *identity_errors, *artifacts_errors, "binary_identity"],
        )

    assert prepared is not None
    state_root = _hermes_state_root(workspace_root, str(envelope["envelope_id"]))
    # Bind the freshness attestation to a direct observation, not a constant:
    # any filesystem entry named state.db at attempt start (including a
    # symlink) means the state did not start fresh.
    try:
        (state_root / "state.db").lstat()
        pre_state_absent = False
    except FileNotFoundError:
        pre_state_absent = True
    except OSError:
        pre_state_absent = False
    attempt_env, attempt_home_errors = _initialize_hermes_attempt_home(state_root, prepared)
    expires_at = prepared.hermes_auth_profile.get("expires_at")
    if not isinstance(expires_at, int) or expires_at <= int(time()) + envelope["resources"]["wall_clock_seconds"] + 60:
        attempt_home_errors.append("hermes_codex_access_token_window_insufficient")
    if attempt_home_errors:
        artifacts, artifacts_errors = _write_j4_artifacts(
            output_dir,
            runtime="hermes",
            envelope_id=envelope["envelope_id"],
            stdout="",
            stderr="",
        )
        return _base_receipt(
            runtime="hermes",
            binary=binary,
            envelope=envelope,
            envelope_sha256=envelope_sha256,
            status=(
                "auth_required"
                if "hermes_codex_access_token_window_insufficient" in attempt_home_errors
                else "resource_unavailable"
            ),
            argv=command,
            duration_ms=0,
            exit_code=None,
            workspace=_workspace_receipt(
                workspace_root, before, envelope=envelope, workspace_identity=workspace_identity
            ),
            artifacts=artifacts,
            schema_errors=sorted({*attempt_home_errors, *artifacts_errors}),
        )
    hermes_env = {
        HERMES_J4_SOURCE_ROOT_ENV: str(prepared.hermes_source_root),
        HERMES_J4_STATE_DB_ENV: str((state_root / "state.db").resolve()),
        HERMES_J4_SITE_PACKAGES_ENV: str(prepared.hermes_site_packages),
        HERMES_J4_WORKSPACE_ROOT_ENV: str(workspace_root.resolve()),
        "HERMES_WRITE_SAFE_ROOT": str(workspace_root.resolve()),
        "PYTHONDONTWRITEBYTECODE": "1",
        **attempt_env,
    }
    runtime_pre_stable = _hermes_frozen_runtime_stable(prepared)
    result = _run_process(
        command,
        workspace_root,
        timeout_seconds=envelope["resources"]["wall_clock_seconds"],
        env_overrides=hermes_env,
        env_remove=_HERMES_J4_AMBIENT_ENV_DENYLIST,
        require_workspace_sandbox=True,
        additional_writable_roots=(state_root.resolve(),),
    )
    runtime_post_chat_stable = _hermes_frozen_runtime_stable(prepared)
    effective_resources, resource_sources = _cli_resource_attestation("hermes", command, envelope)
    authority = _cli_authority_attestation(
        "hermes",
        command,
        envelope,
        result.sandbox,
        workspace_root=workspace_root,
        invocation_env=hermes_env,
        runtime_identity=binary,
    )
    payload: dict[str, Any] | None = None
    schema_errors: list[str] = []
    effective_model: str | None = None
    effective_provider: str | None = None
    fallback_observed: bool | None = None
    turns: int | None = None
    tokens: int | None = None
    observed_cost: float | None = None
    transcript = ""
    transcript_export_spawn_count = 0
    session_attestation: dict[str, Any] | None = None
    session_id: str | None = None
    status = _cli_failure_status(result, runtime="hermes") if result.returncode else "completed"
    if not pre_state_absent:
        schema_errors.append("state_not_fresh")
    if not runtime_pre_stable or not runtime_post_chat_stable:
        schema_errors.append("runtime_identity_drift")
        status = "attestation_failed"
    if result.returncode == 0:
        try:
            payload = _load_json_object(_ensure_text(result.stdout))
        except ValueError:
            schema_errors.append("whole_output_json")
        schema_errors.extend(_validate_runtime_payload(payload))
        session_id = _hermes_session_id(_ensure_text(result.stderr))
        if session_id is None:
            schema_errors.append("session_id_attestation")
        elif runtime_pre_stable and runtime_post_chat_stable:
            attest = _run_process(
                [
                    str(python_path),
                    "-I",
                    "-S",
                    str(launcher_path),
                    "attest-session",
                    "--session-id",
                    session_id,
                    "--expected-prompt-sha256",
                    envelope["task"]["prompt_sha256"],
                    "--expected-source",
                    "p08-j4",
                ],
                workspace_root,
                timeout_seconds=20,
                env_overrides=hermes_env,
                env_remove=_HERMES_J4_AMBIENT_ENV_DENYLIST,
                require_workspace_sandbox=True,
                additional_writable_roots=(state_root.resolve(),),
                network_access=False,
            )
            try:
                session_attestation = _load_json_object(_ensure_text(attest.stdout))
            except ValueError:
                session_attestation = None
            if (
                attest.returncode != 0
                or not isinstance(session_attestation, dict)
                or session_attestation.get("ok") is not True
                or session_attestation.get("session_id") != session_id
                or session_attestation.get("tip_session_id") != session_id
                or session_attestation.get("source") != "p08-j4"
                or session_attestation.get("all_sources_match") is not True
                or session_attestation.get("prompt_sha256_match") is not True
                or session_attestation.get("session_count") != session_attestation.get("lineage_count")
            ):
                schema_errors.append("session_lineage_attestation")
            lineage_ids = (
                session_attestation.get("lineage_session_ids")
                if isinstance(session_attestation, dict)
                and isinstance(session_attestation.get("lineage_session_ids"), list)
                else []
            )
            records: list[dict[str, Any]] = []
            transcript_parts: list[str] = []
            for lineage_id in lineage_ids:
                export = _run_process(
                    [
                        str(python_path),
                        "-I",
                        "-S",
                        str(launcher_path),
                        "sessions",
                        "export",
                        "-",
                        "--format",
                        "jsonl",
                        "--session-id",
                        str(lineage_id),
                        "--redact",
                    ],
                    workspace_root,
                    timeout_seconds=20,
                    env_overrides=hermes_env,
                    env_remove=_HERMES_J4_AMBIENT_ENV_DENYLIST,
                    require_workspace_sandbox=True,
                    additional_writable_roots=(state_root.resolve(),),
                    network_access=False,
                )
                transcript_export_spawn_count += 1
                exported = _ensure_text(export.stdout)
                transcript_parts.append(exported.rstrip("\n"))
                if export.returncode != 0:
                    schema_errors.append("transcript_export")
                    continue
                try:
                    exported_records = _parse_jsonl(exported)
                except ValueError:
                    schema_errors.append("transcript_jsonl")
                    continue
                if (
                    len(exported_records) != 1
                    or exported_records[0].get("id") != lineage_id
                    or exported_records[0].get("source") != "p08-j4"
                ):
                    schema_errors.append("transcript_invocation_binding")
                    continue
                records.extend(exported_records)
            transcript = "\n".join(part for part in transcript_parts if part)
            if not lineage_ids or [record.get("id") for record in records] != lineage_ids:
                schema_errors.append("transcript_lineage_coverage")
            elif records:
                (
                    effective_model,
                    effective_provider,
                    fallback_observed,
                    turns,
                    tokens,
                    observed_cost,
                    attribution_complete,
                ) = _hermes_attestation(records)
                if (
                    effective_model != J4_MODEL
                    or effective_provider != "openai-codex"
                    or fallback_observed is not False
                    or turns < 1
                    or attribution_complete is not True
                ):
                    schema_errors.append("model_route_attestation")
        if effective_resources is None:
            schema_errors.append("resource_attestation")
        if authority is None:
            schema_errors.append("authority_attestation")
        if schema_errors:
            if "authority_attestation" in schema_errors:
                status = "sandbox_unavailable"
            elif "resource_attestation" in schema_errors:
                status = "resource_unavailable"
            else:
                status = (
                    "attestation_failed"
                    if any(
                        "attestation" in error or "transcript" in error or "runtime_identity" in error
                        for error in schema_errors
                    )
                    else "invalid_output"
                )
    runtime_final_stable = _hermes_frozen_runtime_stable(prepared)
    if not runtime_final_stable:
        schema_errors.append("runtime_identity_drift")
        status = "attestation_failed"
    state_manifest, state_manifest_errors = _hermes_attempt_state_attestation(
        state_root,
        prepared,
    )
    if state_manifest_errors:
        schema_errors.extend(state_manifest_errors)
        status = "attestation_failed"
    secret_boundary = _hermes_auth_secret_boundary(prepared.hermes_auth_projection)
    safe_stdout = secret_boundary.redact_text(_ensure_text(result.stdout)).text
    safe_stderr = secret_boundary.redact_text(_ensure_text(result.stderr)).text
    safe_transcript = secret_boundary.redact_text(transcript).text
    safe_payload = secret_boundary.redact_payload(payload)
    artifacts, artifacts_errors = _write_j4_artifacts(
        output_dir,
        runtime="hermes",
        envelope_id=envelope["envelope_id"],
        stdout=safe_stdout,
        stderr=safe_stderr,
        transcript=safe_transcript,
    )
    schema_errors.extend(artifacts_errors)
    workspace = _workspace_receipt(
        workspace_root,
        before,
        envelope=envelope,
        declared_paths=payload.get("files_created") if isinstance(payload, dict) else None,
        workspace_identity=workspace_identity,
    )
    if status == "completed" and not workspace["boundary_ok"]:
        status = "sandbox_unavailable"
    return _base_receipt(
        runtime="hermes",
        binary=binary,
        envelope=envelope,
        envelope_sha256=envelope_sha256,
        status=status,
        argv=command,
        duration_ms=result.duration_ms,
        exit_code=result.returncode,
        workspace=workspace,
        artifacts=artifacts,
        parsed_payload=safe_payload,
        schema_errors=schema_errors,
        turns=turns,
        tokens=tokens,
        observed_cost=observed_cost,
        effective_model=effective_model,
        effective_provider=effective_provider,
        fallback_observed=fallback_observed,
        attestation_source=("hermes.launcher.attest-session+hermes.sessions.export.jsonl" if effective_model else None),
        effective_resources=effective_resources,
        resource_sources=resource_sources,
        authority=authority,
        route_attestation={
            "call_count": turns if isinstance(turns, int) else 0,
            "count_semantics": "exact",
            "routes": ([{"model": effective_model, "provider": effective_provider}] if effective_model else []),
            "source": "hermes.launcher.attest-session+hermes.sessions.export.jsonl",
        },
        execution={
            "chat_spawn_count": 1,
            "invocation_id": uuid.uuid5(uuid.NAMESPACE_URL, envelope_sha256).hex,
            "argv_sha256": _sha256_json(command),
            "cwd": str(workspace_root.resolve()),
            "workspace_flag": _argv_value(command, "--in"),
            "session_id": session_id,
            "transcript_export_spawn_count": transcript_export_spawn_count,
            "pre_state_db_absent": pre_state_absent,
            "state_manifest_sha256": _sha256_json(state_manifest),
            "runtime_pre_stable": runtime_pre_stable,
            "runtime_post_chat_stable": runtime_post_chat_stable,
            "runtime_final_stable": runtime_final_stable,
            "auth_profile": dict(prepared.hermes_auth_profile),
            "attempt_environment_sha256": _sha256_json(
                {
                    name: hermes_env[name]
                    for name in (
                        "HERMES_HOME",
                        "HOME",
                        "CODEX_HOME",
                        "HERMES_MANAGED_DIR",
                        "TMPDIR",
                        "TMP",
                        "TEMP",
                        "HERMES_SAFE_MODE",
                        "HERMES_IGNORE_USER_CONFIG",
                        "HERMES_IGNORE_RULES",
                        "HERMES_BUNDLED_SKILLS",
                        "PYTHONNOUSERSITE",
                    )
                }
            ),
            "session_attestation": session_attestation,
        },
    )


def _run_hermes_j4(
    scenario: ScenarioWorkspace,
    envelope: dict[str, Any],
    envelope_sha256: str,
    *,
    output_dir: Path,
    config: J4RuntimeConfig,
    prepared: PreparedJ4Runtimes | None = None,
) -> dict[str, Any]:
    """Run Hermes and always remove attempt-local credentials and runtime state."""

    workspace_root = _runtime_workspace_path(output_dir, "hermes", envelope["envelope_id"])
    attempt_root, attempt_identity, claim_error = _claim_j4_attempt_root(
        output_dir,
        "hermes",
        str(envelope["envelope_id"]),
    )
    state_root = _hermes_state_root(workspace_root, str(envelope["envelope_id"]))

    def blocked_receipt(*, status: str, schema_error: str) -> dict[str, Any]:
        binary = prepared.hermes_binary if prepared is not None else _empty_hermes_runtime_identity()
        command = _hermes_command(
            workspace_root=workspace_root,
            envelope=envelope,
            python=prepared.hermes_python if prepared is not None else Path("hermes-python"),
            launcher=prepared.hermes_launcher if prepared is not None else HERMES_J4_LAUNCHER,
        )
        return _base_receipt(
            runtime="hermes",
            binary=binary,
            envelope=envelope,
            envelope_sha256=envelope_sha256,
            status=status,
            argv=command,
            duration_ms=0,
            exit_code=None,
            workspace=_unobserved_workspace_receipt(envelope, reason="unclaimed_attempt_root"),
            artifacts={},
            schema_errors=[schema_error],
            execution={"state_root_owned": False, "state_cleanup_verified": False},
        )

    if claim_error is not None or attempt_identity is None:
        status, schema_error = {
            "attempt_root_conflict": ("needs_reconciliation", "hermes_attempt_state_conflict"),
            "attempt_parent_unsupported": ("needs_reconciliation", "hermes_attempt_boundary_unsupported"),
            "attempt_root_unsupported": ("needs_reconciliation", "hermes_attempt_boundary_unsupported"),
        }.get(str(claim_error), ("resource_unavailable", "hermes_attempt_state_unavailable"))
        return blocked_receipt(status=status, schema_error=schema_error)

    try:
        state_root.mkdir(mode=0o700, exist_ok=False)
        state_details = state_root.lstat()
    except OSError as exc:
        conflict = isinstance(exc, FileExistsError)
        return blocked_receipt(
            status="needs_reconciliation" if conflict else "resource_unavailable",
            schema_error="hermes_attempt_state_conflict" if conflict else "hermes_attempt_state_unavailable",
        )
    state_identity = (state_details.st_dev, state_details.st_ino)
    receipt: dict[str, Any] | None = None
    cleanup_errors: list[str] = []
    owned_at_cleanup = True
    try:
        receipt = _run_hermes_j4_attempt(
            scenario,
            envelope,
            envelope_sha256,
            output_dir=output_dir,
            config=config,
            prepared=prepared,
        )
    finally:
        # Both the enclosing attempt root and the state child must still be
        # exactly what this attempt created; a replaced state root is foreign
        # state and must survive as a typed reconciliation, not be deleted.
        owned_at_cleanup = _j4_attempt_root_owned(attempt_root, attempt_identity)
        if owned_at_cleanup and _j4_attempt_root_owned(state_root, state_identity):
            cleanup_errors = _cleanup_hermes_attempt_state(state_root)
        else:
            cleanup_errors = ["hermes_attempt_state_cleanup_ambiguous"]
    assert receipt is not None
    execution = receipt.setdefault("execution", {})
    execution["state_root_owned"] = owned_at_cleanup
    execution["state_cleanup_verified"] = not cleanup_errors
    if cleanup_errors:
        receipt["status"] = "needs_reconciliation"
        parsed = receipt.setdefault("parsed", {})
        parsed["schema_errors"] = sorted(set([*(parsed.get("schema_errors") or []), *cleanup_errors]))
        parsed["schema_valid"] = False
    return receipt


def _http_error_status(status_code: int) -> str:
    if status_code == 401:
        return "auth_required"
    if status_code == 403:
        return "authority_denied"
    if status_code in {404, 410}:
        return "resource_unavailable"
    return "failed"


def _valid_hive_base_url(value: str) -> bool:
    parsed = urlparse(str(value or "").strip())
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    if parsed.scheme == "https":
        return True
    if parsed.scheme != "http":
        return False
    if parsed.hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


def _response_json(response: Any) -> Any:
    try:
        return response.json()
    except Exception:
        return None


def _hive_server_build_identity(health_payload: Any) -> dict[str, str] | None:
    if not isinstance(health_payload, dict):
        return None
    components = health_payload.get("components")
    identity = components.get("build_identity") if isinstance(components, dict) else None
    if not isinstance(identity, dict):
        return None
    revision = str(identity.get("revision") or "").strip()
    sha256 = str(identity.get("sha256") or "").strip().lower()
    if not revision or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        return None
    return {"revision": revision, "sha256": sha256}


def _hive_request(
    client: Any,
    *,
    base_url: str,
    bearer: str,
    method: str,
    path: str,
    timeout: float,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    headers = {"Authorization": f"Bearer {bearer}", "Accept": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    return client.request(
        method,
        urljoin(base_url.rstrip("/") + "/", path.lstrip("/")),
        headers=headers,
        params=params,
        json=payload,
        timeout=timeout,
    )


def _hive_workspace_is_clean(
    client: Any,
    *,
    base_url: str,
    bearer: str,
    agent_id: str,
    root: str,
    timeout: float,
) -> tuple[bool, str | None]:
    response = _hive_request(
        client,
        base_url=base_url,
        bearer=bearer,
        method="GET",
        path=f"/api/v1/agents/{agent_id}/files/",
        params={"path": root},
        timeout=timeout,
    )
    if response.status_code == 404:
        return True, None
    if response.status_code >= 400:
        return False, _http_error_status(response.status_code)
    entries = _response_json(response)
    if not isinstance(entries, list):
        return False, "invalid_output"
    return (not entries, None if not entries else "sandbox_unavailable")


def _hive_active_run_fence(
    client: Any,
    *,
    base_url: str,
    bearer: str,
    agent_id: str,
    session_id: str,
    timeout: float,
) -> tuple[bool, dict[str, Any]]:
    response = _hive_request(
        client,
        base_url=base_url,
        bearer=bearer,
        method="GET",
        path=f"/api/v1/agents/{agent_id}/sessions/{session_id}/runs/active",
        timeout=timeout,
    )
    if response.status_code >= 400:
        return False, {"status": "unreconciled", "http_status": response.status_code, "active_run_id": None}
    active_payload = _response_json(response)
    if active_payload is None:
        return True, {"status": "settled", "http_status": response.status_code, "active_run_id": None}
    active_run_id = str(active_payload.get("run_id") or "") if isinstance(active_payload, dict) else ""
    return False, {
        "status": "unreconciled",
        "http_status": response.status_code,
        "active_run_id": active_run_id or None,
    }


def _hive_list_workspace(
    client: Any,
    *,
    base_url: str,
    bearer: str,
    agent_id: str,
    root: str,
    timeout: float,
) -> tuple[dict[str, str], str | None]:
    files: dict[str, str] = {}
    pending = [root]
    seen: set[str] = set()
    while pending:
        directory = pending.pop()
        if directory in seen:
            return {}, "duplicate_directory"
        seen.add(directory)
        response = _hive_request(
            client,
            base_url=base_url,
            bearer=bearer,
            method="GET",
            path=f"/api/v1/agents/{agent_id}/files/",
            params={"path": directory},
            timeout=timeout,
        )
        if response.status_code >= 400:
            return {}, _http_error_status(response.status_code)
        entries = _response_json(response)
        if not isinstance(entries, list):
            return {}, "invalid_output"
        for entry in entries:
            if not isinstance(entry, dict):
                return {}, "invalid_output"
            entry_path = str(entry.get("path") or "")
            try:
                relative = PurePosixPath(entry_path).relative_to(PurePosixPath(root)).as_posix()
                _safe_relative_path(relative)
            except (ValueError, TypeError):
                return {}, "sandbox_unavailable"
            if entry.get("is_dir") is True:
                pending.append(entry_path)
                continue
            if relative in files:
                return {}, "sandbox_unavailable"
            content_response = _hive_request(
                client,
                base_url=base_url,
                bearer=bearer,
                method="GET",
                path=f"/api/v1/agents/{agent_id}/files/content",
                params={"path": entry_path},
                timeout=timeout,
            )
            if content_response.status_code >= 400:
                return {}, _http_error_status(content_response.status_code)
            content_payload = _response_json(content_response)
            if not isinstance(content_payload, dict) or not isinstance(content_payload.get("content"), str):
                return {}, "invalid_output"
            files[relative] = content_payload["content"]
    return files, None


def _replace_local_workspace(root: Path, files: dict[str, str]) -> list[str]:
    expected = set(files)
    errors: list[str] = []
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file() and path.relative_to(root).as_posix() not in expected:
            path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    for relative, content in files.items():
        try:
            safe = _safe_relative_path(relative)
        except ValueError:
            errors.append(f"unsafe_remote_path:{relative}")
            continue
        target = root / safe
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return errors


def _event_run_id(event: dict[str, Any]) -> str:
    scope = event.get("scope") if isinstance(event.get("scope"), dict) else {}
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    return str(scope.get("run_id") or event.get("run_id") or metadata.get("runtime_task_id") or "")


def _event_marker(event: dict[str, Any]) -> tuple[str, str]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    kind = str(
        event.get("kind") or event.get("item_kind") or metadata.get("event_type") or event.get("event_type") or ""
    )
    lifecycle = str(event.get("lifecycle") or event.get("item_status") or "")
    return kind, lifecycle


def _hive_transcript_attestation(
    events: list[dict[str, Any]],
    *,
    run_id: str,
) -> dict[str, Any]:
    route: dict[str, Any] | None = None
    final_text: str | None = None
    provider_calls: list[dict[str, Any]] = []
    provider_call_ids: set[str] = set()
    provider_call_event_count = 0
    invalid_provider_call_count = 0
    tokens = 0
    costs: list[float] = []
    terminal_status: str | None = None
    for event in events:
        if _event_run_id(event) != run_id:
            continue
        kind, lifecycle = _event_marker(event)
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        if kind in {"model_route", "session_context.model_route"} or metadata.get("event_type") == "model_route":
            route = {**metadata, **{key: value for key, value in payload.items() if key != "metadata"}}
        if kind in {"provider_call_ledger", "session_context.provider_call_ledger"}:
            provider_call_event_count += 1
            ledger = _decoded_mapping(metadata.get("provider_prompt_ledger") or payload.get("provider_prompt_ledger"))
            cache_metrics = _decoded_mapping(metadata.get("cache_metrics") or payload.get("cache_metrics"))
            provider_call_id = str(ledger.get("provider_call_id") or "").strip()
            provider = str(ledger.get("provider") or "").strip().lower()
            model = _normalize_model_id(ledger.get("model"))
            tool_names = ledger.get("tool_names")
            tool_schema_sha256 = str(ledger.get("tool_schema_sha256") or "")
            tool_count = metadata.get("tool_count", payload.get("tool_count"))
            if (
                provider_call_id
                and provider
                and model
                and isinstance(tool_count, int)
                and not isinstance(tool_count, bool)
                and isinstance(tool_names, list)
                and tool_names
                and all(isinstance(name, str) and name for name in tool_names)
                and len(tool_names) == len(set(tool_names))
                and re.fullmatch(r"[0-9a-f]{64}", tool_schema_sha256)
                and provider_call_id not in provider_call_ids
            ):
                provider_call_ids.add(provider_call_id)
                provider_calls.append(
                    {
                        "provider_call_id": provider_call_id,
                        "provider": provider,
                        "model": model,
                        "tool_count": tool_count,
                        "tool_names": list(tool_names),
                        "tool_schema_sha256": tool_schema_sha256,
                    }
                )
            else:
                invalid_provider_call_count += 1
            tokens += _usage_total(cache_metrics, {"totalinputtokens", "outputtokens"})
            cost = payload.get("cost_usd") or metadata.get("cost_usd")
            if isinstance(cost, (int, float)):
                costs.append(float(cost))
        if kind in {"assistant_final", "assistant_text.completed"} and lifecycle in {"completed", ""}:
            content = payload.get("content")
            if isinstance(content, str):
                final_text = content
        run_lifecycle = lifecycle if event.get("item_kind") == "run" else ""
        if kind.startswith("run."):
            run_lifecycle = kind.split(".", 1)[1]
        if run_lifecycle in {"completed", "failed", "cancelled", "needs_reconciliation"}:
            terminal_status = run_lifecycle
    fallback_observed = bool(route and route.get("fallback_model"))
    return {
        "route": route,
        "final_text": final_text,
        "fallback_observed": fallback_observed,
        "turns": len(provider_calls),
        "tokens": tokens,
        "observed_cost": sum(costs) if costs else None,
        "terminal_status": terminal_status,
        "call_count": len(provider_calls),
        "provider_call_event_count": provider_call_event_count,
        "invalid_provider_call_count": invalid_provider_call_count,
        "provider_calls": provider_calls,
    }


def _hive_cancel_and_fence(
    client: Any,
    *,
    base_url: str,
    bearer: str,
    agent_id: str,
    session_id: str,
    run_id: str,
    attempt_id: str,
    fence_seconds: int,
) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None, dict[str, Any]]:
    cancel_key = f"p08-j4-cancel:{attempt_id}:{run_id}"
    cancel_response = _hive_request(
        client,
        base_url=base_url,
        bearer=bearer,
        method="POST",
        path=f"/api/v1/agents/{agent_id}/sessions/{session_id}/runs/{run_id}/cancel",
        timeout=10,
        extra_headers={"Idempotency-Key": cancel_key},
    )
    cancel_payload = _response_json(cancel_response)
    cancellation = {
        "requested": True,
        "http_status": cancel_response.status_code,
        "idempotency_key_sha256": _sha256_bytes(cancel_key.encode("utf-8")),
        "receipt": (
            {
                key: cancel_payload[key]
                for key in ("command_id", "run_id", "status", "accepted")
                if isinstance(cancel_payload, dict) and key in cancel_payload
            }
        ),
        "fence": "pending",
    }
    valid_receipt = (
        isinstance(cancel_payload, dict)
        and cancel_payload.get("accepted") is True
        and str(cancel_payload.get("run_id") or "") == run_id
        and str(cancel_payload.get("status") or "") in {"accepted", "applying", "applied"}
        and bool(str(cancel_payload.get("command_id") or "").strip())
    )
    if cancel_response.status_code >= 400 or not valid_receipt:
        cancellation["fence"] = "unreconciled"
        return "needs_reconciliation", [], None, cancellation

    deadline = monotonic() + fence_seconds
    latest_events: list[dict[str, Any]] = []
    latest_attestation: dict[str, Any] | None = None
    while monotonic() < deadline:
        transcript_response = _hive_request(
            client,
            base_url=base_url,
            bearer=bearer,
            method="GET",
            path=f"/api/v1/agents/{agent_id}/sessions/{session_id}/transcript",
            params={"schema_version": 2, "limit": 1000},
            timeout=10,
        )
        active_response = _hive_request(
            client,
            base_url=base_url,
            bearer=bearer,
            method="GET",
            path=f"/api/v1/agents/{agent_id}/sessions/{session_id}/runs/active",
            timeout=10,
        )
        events = _response_json(transcript_response)
        if (
            transcript_response.status_code >= 400
            or active_response.status_code >= 400
            or not isinstance(events, list)
            or any(not isinstance(event, dict) for event in events)
        ):
            cancellation["fence"] = "unreconciled"
            return "needs_reconciliation", latest_events, latest_attestation, cancellation
        latest_events = events
        latest_attestation = _hive_transcript_attestation(events, run_id=run_id)
        active_payload = _response_json(active_response)
        terminal_status = latest_attestation["terminal_status"]
        if active_payload is None and terminal_status is not None:
            cancellation["fence"] = "settled"
            if terminal_status in {"cancelled", "failed", "needs_reconciliation"}:
                return terminal_status, latest_events, latest_attestation, cancellation
            return "timeout", latest_events, latest_attestation, cancellation
        sleep(0.25)
    cancellation["fence"] = "unreconciled"
    return "needs_reconciliation", latest_events, latest_attestation, cancellation


def _run_hive_j4(
    scenario: ScenarioWorkspace,
    envelope: dict[str, Any],
    envelope_sha256: str,
    *,
    output_dir: Path,
    config: J4RuntimeConfig,
) -> dict[str, Any]:
    workspace_root = _runtime_workspace_path(output_dir, "hive", envelope["envelope_id"])
    attempt_root, attempt_identity, claim_error = _claim_j4_attempt_root(
        output_dir,
        "hive",
        str(envelope["envelope_id"]),
    )
    attempt_id = uuid.uuid4().hex
    remote_root = f"workspace/p08-j4/{attempt_id}/{envelope['envelope_id']}"
    base_url = str(config.hive_base_url or "").strip()
    binary = {
        "path": base_url,
        "version": "",
        "sha256": "",
        "revision": "",
    }
    argv = [
        "GET /api/v1/auth/me",
        f"GET /api/v1/agents/{config.hive_agent_id or '<agent>'}/files/?path=<attempt-root>",
        f"POST /api/v1/agents/{config.hive_agent_id or '<agent>'}/sessions",
        f"POST /api/v1/agents/{config.hive_agent_id or '<agent>'}/sessions/<session>/runs",
        "GET transcript?schema_version=2",
        "POST run/<run>/cancel + GET runs/active fence on timeout",
    ]
    if claim_error is not None or attempt_identity is None:
        # No seed clone or other attempt effect runs before the attempt root is
        # claimed: a denied root must not be written through or read from.
        status, schema_error = {
            "attempt_root_conflict": ("needs_reconciliation", "hive_attempt_state_conflict"),
            "attempt_parent_unsupported": ("needs_reconciliation", "hive_attempt_boundary_unsupported"),
            "attempt_root_unsupported": ("needs_reconciliation", "hive_attempt_boundary_unsupported"),
        }.get(str(claim_error), ("resource_unavailable", "hive_attempt_state_unavailable"))
        return _base_receipt(
            runtime="hive",
            binary=binary,
            envelope=envelope,
            envelope_sha256=envelope_sha256,
            status=status,
            argv=argv,
            duration_ms=0,
            exit_code=None,
            workspace=_unobserved_workspace_receipt(envelope, reason="unclaimed_attempt_root"),
            artifacts={},
            schema_errors=[schema_error],
            execution={
                "attempt_id": attempt_id,
                "session_id": None,
                "run_id": None,
                "remote_root": remote_root,
                "terminal_status": None,
            },
        )
    before, clone_errors, workspace_identity = _clone_seed(scenario.workspace_dir, workspace_root)
    setup_errors = [*clone_errors]
    if not _valid_hive_base_url(base_url):
        setup_errors.append("hive_base_url")
    if not config.hive_bearer:
        setup_errors.append("hive_bearer")
    if not config.hive_agent_id:
        setup_errors.append("hive_agent_id")
    if setup_errors:
        artifacts, artifacts_errors = _write_j4_artifacts(
            output_dir,
            runtime="hive",
            envelope_id=envelope["envelope_id"],
            stdout="",
            stderr="",
        )
        return _base_receipt(
            runtime="hive",
            binary=binary,
            envelope=envelope,
            envelope_sha256=envelope_sha256,
            status="resource_unavailable",
            argv=argv,
            duration_ms=0,
            exit_code=None,
            workspace=_workspace_receipt(
                workspace_root, before, envelope=envelope, workspace_identity=workspace_identity
            ),
            artifacts=artifacts,
            schema_errors=[*setup_errors, *artifacts_errors],
            execution={
                "attempt_id": attempt_id,
                "session_id": None,
                "run_id": None,
                "remote_root": remote_root,
                "terminal_status": None,
            },
        )

    owns_client = config.http_client is None
    client = config.http_client or httpx.Client()
    started = monotonic()
    transcript_events: list[dict[str, Any]] = []
    payload: dict[str, Any] | None = None
    schema_errors: list[str] = []
    status = "failed"
    exit_code: int | None = None
    effective_model: str | None = None
    effective_provider: str | None = None
    fallback_observed: bool | None = None
    turns: int | None = None
    tokens: int | None = None
    observed_cost: float | None = None
    run_id = ""
    session_id = ""
    terminal_status: str | None = None
    route_attestation: dict[str, Any] = {"call_count": 0, "routes": [], "source": None}
    effective_resources: dict[str, Any] | None = None
    resource_sources: dict[str, str] = {}
    authority: dict[str, Any] | None = None
    execution: dict[str, Any] = {
        "attempt_id": attempt_id,
        "session_id": None,
        "run_id": None,
        "remote_root": remote_root,
        "terminal_status": None,
    }

    def fence_unsettled_run() -> None:
        nonlocal status, transcript_events, turns, tokens, observed_cost, terminal_status
        try:
            status, fenced_events, fenced_attestation, cancellation = _hive_cancel_and_fence(
                client,
                base_url=base_url,
                bearer=config.hive_bearer or "",
                agent_id=config.hive_agent_id or "",
                session_id=session_id,
                run_id=run_id,
                attempt_id=attempt_id,
                fence_seconds=config.cancel_fence_seconds,
            )
        except (httpx.HTTPError, OSError):
            status = "needs_reconciliation"
            fenced_events = []
            fenced_attestation = None
            cancellation = {"requested": True, "fence": "unreconciled", "http_status": None}
        execution["cancel"] = cancellation
        if fenced_events:
            transcript_events = fenced_events
        if fenced_attestation:
            turns = fenced_attestation["turns"]
            tokens = fenced_attestation["tokens"]
            observed_cost = fenced_attestation["observed_cost"]
            terminal_status = fenced_attestation["terminal_status"]
        execution["terminal_status"] = terminal_status

    try:
        health = _hive_request(
            client,
            base_url=base_url,
            bearer=config.hive_bearer or "",
            method="GET",
            path="/api/health",
            timeout=10,
        )
        if health.status_code >= 400:
            status = _http_error_status(health.status_code)
            raise RuntimeError("typed_http_stop")
        health_payload = _response_json(health)
        if isinstance(health_payload, dict):
            binary["version"] = str(health_payload.get("version") or "")
        if not binary["version"]:
            status = "attestation_failed"
            schema_errors.append("hive_version")
            raise RuntimeError("typed_http_stop")
        server_identity = _hive_server_build_identity(health_payload)
        if server_identity is None:
            status = "resource_unavailable"
            schema_errors.append("hive_build_identity_unavailable")
            raise RuntimeError("typed_http_stop")
        binary.update(server_identity)
        if (config.hive_revision and config.hive_revision != binary["revision"]) or (
            config.hive_binary_sha256 and str(config.hive_binary_sha256).lower() != binary["sha256"]
        ):
            status = "attestation_failed"
            schema_errors.append("hive_build_identity_mismatch")
            raise RuntimeError("typed_http_stop")
        auth = _hive_request(
            client,
            base_url=base_url,
            bearer=config.hive_bearer or "",
            method="GET",
            path="/api/v1/auth/me",
            timeout=10,
        )
        if auth.status_code >= 400:
            status = _http_error_status(auth.status_code)
            raise RuntimeError("typed_http_stop")
        agent = _hive_request(
            client,
            base_url=base_url,
            bearer=config.hive_bearer or "",
            method="GET",
            path=f"/api/v1/agents/{config.hive_agent_id}",
            timeout=10,
        )
        if agent.status_code >= 400:
            status = _http_error_status(agent.status_code)
            raise RuntimeError("typed_http_stop")
        agent_payload = _response_json(agent)
        models = _hive_request(
            client,
            base_url=base_url,
            bearer=config.hive_bearer or "",
            method="GET",
            path="/api/v1/enterprise/llm-models",
            timeout=10,
        )
        if models.status_code >= 400:
            status = _http_error_status(models.status_code)
            raise RuntimeError("typed_http_stop")
        model_rows = _response_json(models)
        primary_id = str(agent_payload.get("primary_model_id") or "") if isinstance(agent_payload, dict) else ""
        primary = (
            next(
                (row for row in model_rows if isinstance(row, dict) and str(row.get("id") or "") == primary_id),
                None,
            )
            if isinstance(model_rows, list)
            else None
        )
        if (
            not isinstance(agent_payload, dict)
            or agent_payload.get("fallback_model_id") is not None
            or int(agent_payload.get("max_tool_rounds") or 0) != envelope["resources"]["max_tool_rounds"]
            or not isinstance(primary, dict)
            or primary.get("provider") != "openai-response"
            or _normalize_model_id(primary.get("model")) != J4_MODEL
            or str(primary.get("reasoning_effort") or "").lower() != J4_REASONING_EFFORT
            or primary.get("enabled") is not True
        ):
            status = "model_unavailable"
            raise RuntimeError("typed_http_stop")

        effective_resources = _expected_resources(envelope)
        resource_sources = {
            "max_tool_rounds": "hive.agent.max_tool_rounds",
            "wall_clock_seconds": "j4.adapter.deadline",
            "reasoning_effort": "hive.primary_model.reasoning_effort",
        }
        is_clean, clean_error = _hive_workspace_is_clean(
            client,
            base_url=base_url,
            bearer=config.hive_bearer or "",
            agent_id=config.hive_agent_id or "",
            root=remote_root,
            timeout=10,
        )
        if not is_clean:
            status = clean_error if clean_error in J4_STATUSES else "sandbox_unavailable"
            schema_errors.append(f"attempt_workspace:{clean_error or 'not_clean'}")
            raise RuntimeError("typed_http_stop")

        session = _hive_request(
            client,
            base_url=base_url,
            bearer=config.hive_bearer or "",
            method="POST",
            path=f"/api/v1/agents/{config.hive_agent_id}/sessions",
            payload={
                "title": f"P08-J4 {envelope['envelope_id']} {attempt_id}",
                "permission_mode": "bypassPermissions",
                "allowed_tools": list(_J4_ALLOWED_TOOLS["hive"]),
                "writable_roots": [remote_root],
            },
            timeout=10,
        )
        if session.status_code >= 400:
            status = _http_error_status(session.status_code)
            raise RuntimeError("typed_http_stop")
        session_payload = _response_json(session)
        session_id = str(session_payload.get("id") or "") if isinstance(session_payload, dict) else ""
        execution["session_id"] = session_id or None
        if not session_id:
            status = "invalid_output"
            schema_errors.append("session_id")
            raise RuntimeError("typed_http_stop")
        authority = _hive_session_authority(session_payload, remote_root=remote_root, envelope=envelope)
        if authority is None:
            status = "sandbox_unavailable"
            schema_errors.append("session_authority_attestation")
            raise RuntimeError("typed_http_stop")

        expected_seed_files: dict[str, str] = {}
        for entry in envelope["workspace"]["seed_manifest"]:
            relative = _safe_relative_path(str(entry["path"]))
            content = (scenario.workspace_dir / relative).read_text(encoding="utf-8")
            expected_seed_files[relative] = content
            write = _hive_request(
                client,
                base_url=base_url,
                bearer=config.hive_bearer or "",
                method="PUT",
                path=f"/api/v1/agents/{config.hive_agent_id}/files/content",
                params={"path": f"{remote_root}/{relative}"},
                payload={"content": content},
                timeout=10,
            )
            if write.status_code >= 400:
                status = _http_error_status(write.status_code)
                raise RuntimeError("typed_http_stop")
            readback = _hive_request(
                client,
                base_url=base_url,
                bearer=config.hive_bearer or "",
                method="GET",
                path=f"/api/v1/agents/{config.hive_agent_id}/files/content",
                params={"path": f"{remote_root}/{relative}"},
                timeout=10,
            )
            readback_payload = _response_json(readback)
            if (
                readback.status_code >= 400
                or not isinstance(readback_payload, dict)
                or readback_payload.get("content") != content
            ):
                status = "attestation_failed"
                schema_errors.append(f"seed_readback:{relative}")
                raise RuntimeError("typed_http_stop")
        seeded_files, seed_error = _hive_list_workspace(
            client,
            base_url=base_url,
            bearer=config.hive_bearer or "",
            agent_id=config.hive_agent_id or "",
            root=remote_root,
            timeout=10,
        )
        if seed_error or seeded_files != expected_seed_files:
            status = seed_error if seed_error in J4_STATUSES else "attestation_failed"
            schema_errors.append(f"seed_manifest:{seed_error or 'mismatch'}")
            raise RuntimeError("typed_http_stop")

        run = _hive_request(
            client,
            base_url=base_url,
            bearer=config.hive_bearer or "",
            method="POST",
            path=f"/api/v1/agents/{config.hive_agent_id}/sessions/{session_id}/runs",
            payload={
                "content": scenario.prompt,
                "display_content": scenario.prompt,
                "permission_mode": "bypassPermissions",
                "model_routing_locked": True,
                "idempotency_key": f"p08-j4:{envelope_sha256}:{attempt_id}",
            },
            timeout=10,
        )
        if run.status_code >= 400:
            status = _http_error_status(run.status_code)
            raise RuntimeError("typed_http_stop")
        run_payload = _response_json(run)
        run_id = str(run_payload.get("run_id") or "") if isinstance(run_payload, dict) else ""
        execution["run_id"] = run_id or None
        if not run_id:
            status = "invalid_output"
            schema_errors.append("run_id")
            raise RuntimeError("typed_http_stop")

        deadline = monotonic() + envelope["resources"]["wall_clock_seconds"]
        terminal = False
        final_text: str | None = None
        route: dict[str, Any] | None = None
        transcript_attestation: dict[str, Any] | None = None
        while monotonic() < deadline:
            transcript_response = _hive_request(
                client,
                base_url=base_url,
                bearer=config.hive_bearer or "",
                method="GET",
                path=f"/api/v1/agents/{config.hive_agent_id}/sessions/{session_id}/transcript",
                params={"schema_version": 2, "limit": 1000},
                timeout=10,
            )
            if transcript_response.status_code >= 400:
                status = _http_error_status(transcript_response.status_code)
                raise RuntimeError("typed_http_stop")
            events = _response_json(transcript_response)
            if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
                status = "invalid_output"
                raise RuntimeError("typed_http_stop")
            transcript_events = events
            transcript_attestation = _hive_transcript_attestation(events, run_id=run_id)
            route = transcript_attestation["route"]
            final_text = transcript_attestation["final_text"]
            fallback_observed = transcript_attestation["fallback_observed"]
            turns = transcript_attestation["turns"]
            tokens = transcript_attestation["tokens"]
            observed_cost = transcript_attestation["observed_cost"]
            terminal_status = transcript_attestation["terminal_status"]
            terminal = terminal_status is not None
            if terminal:
                execution["terminal_status"] = terminal_status
                break
            sleep(0.25)
        if not terminal:
            fence_unsettled_run()
            raise RuntimeError("typed_http_stop")
        if terminal_status in {"failed", "cancelled", "needs_reconciliation"}:
            status = terminal_status
            execution["terminal_status"] = terminal_status
            raise RuntimeError("typed_http_stop")
        try:
            active_settled, active_fence = _hive_active_run_fence(
                client,
                base_url=base_url,
                bearer=config.hive_bearer or "",
                agent_id=config.hive_agent_id or "",
                session_id=session_id,
                timeout=10,
            )
        except (httpx.HTTPError, OSError):
            active_settled = False
            active_fence = {"status": "unreconciled", "http_status": None, "active_run_id": None}
        execution["active_fence"] = active_fence
        if not active_settled:
            status = "needs_reconciliation"
            schema_errors.append("active_run_fence")
            raise RuntimeError("typed_http_stop")
        assert transcript_attestation is not None
        provider_calls = transcript_attestation["provider_calls"]
        actual_routes = [
            {"model": call["model"], "provider": call["provider"]} for call in provider_calls if isinstance(call, dict)
        ]
        route_attestation = {
            "call_count": transcript_attestation["call_count"],
            "count_semantics": "exact",
            "routes": actual_routes,
            "source": "hive.transcript.v2.provider_call_ledger",
            "tool_names": list(provider_calls[0].get("tool_names") or ()) if provider_calls else [],
            "tool_schema_sha256": provider_calls[0].get("tool_schema_sha256") if provider_calls else None,
        }
        if actual_routes:
            effective_model = actual_routes[0]["model"]
            effective_provider = actual_routes[0]["provider"]
        if (
            effective_model != J4_MODEL
            or effective_provider != "openai-response"
            or fallback_observed is not False
            or not isinstance(route, dict)
            or route.get("model_routing_locked") is not True
            or _normalize_model_id(route.get("selected_model")) != J4_MODEL
            or str(route.get("selected_provider") or "") != "openai-response"
            or not provider_calls
            or transcript_attestation["invalid_provider_call_count"] > 0
            or transcript_attestation["provider_call_event_count"] != len(provider_calls)
            or any(
                call.get("model") != J4_MODEL
                or call.get("provider") != "openai-response"
                or call.get("tool_count") != len(_J4_ALLOWED_TOOLS["hive"])
                or call.get("tool_names") != list(_J4_ALLOWED_TOOLS["hive"])
                for call in provider_calls
            )
            or len({call.get("tool_schema_sha256") for call in provider_calls}) != 1
        ):
            status = "attestation_failed"
            schema_errors.append("model_route_attestation")
            raise RuntimeError("typed_http_stop")
        try:
            payload = _load_json_object(final_text or "")
        except ValueError:
            schema_errors.append("whole_output_json")
        schema_errors.extend(_validate_runtime_payload(payload))
        if schema_errors:
            status = "invalid_output"
            raise RuntimeError("typed_http_stop")
        remote_files, remote_error = _hive_list_workspace(
            client,
            base_url=base_url,
            bearer=config.hive_bearer or "",
            agent_id=config.hive_agent_id or "",
            root=remote_root,
            timeout=10,
        )
        if remote_error:
            status = "attestation_failed"
            schema_errors.append(f"post_terminal_workspace_evidence:{remote_error}")
            raise RuntimeError("typed_http_stop")
        schema_errors.extend(_replace_local_workspace(workspace_root, remote_files))
        status = "completed" if not schema_errors else "attestation_failed"
        terminal_status = "completed"
        execution["terminal_status"] = terminal_status
        exit_code = 0
    except (httpx.HTTPError, OSError):
        if run_id and terminal_status is None and "cancel" not in execution:
            fence_unsettled_run()
        elif terminal_status == "completed":
            status = "attestation_failed"
            schema_errors.append("post_terminal_workspace_evidence:transport")
            execution["terminal_status"] = terminal_status
        else:
            status = "needs_reconciliation" if run_id and terminal_status is None else "resource_unavailable"
            execution["terminal_status"] = terminal_status
    except (KeyError, TypeError, ValueError):
        status = "invalid_output"
        if run_id and terminal_status is None and "cancel" not in execution:
            fence_unsettled_run()
    except RuntimeError as exc:
        if str(exc) != "typed_http_stop":
            status = "failed"
        if run_id and terminal_status is None and "cancel" not in execution:
            fence_unsettled_run()
    finally:
        if owns_client:
            client.close()

    transcript_text = "\n".join(_canonical_json(event) for event in transcript_events)
    artifacts, artifacts_errors = _write_j4_artifacts(
        output_dir,
        runtime="hive",
        envelope_id=envelope["envelope_id"],
        stdout=_canonical_json(payload) if payload is not None else "",
        stderr="",
        transcript=transcript_text,
    )
    schema_errors.extend(artifacts_errors)
    workspace = _workspace_receipt(
        workspace_root,
        before,
        envelope=envelope,
        declared_paths=payload.get("files_created") if isinstance(payload, dict) else None,
        workspace_identity=workspace_identity,
    )
    if status == "completed" and not workspace["boundary_ok"]:
        status = "sandbox_unavailable"
    return _base_receipt(
        runtime="hive",
        binary=binary,
        envelope=envelope,
        envelope_sha256=envelope_sha256,
        status=status,
        argv=argv,
        duration_ms=int((monotonic() - started) * 1000),
        exit_code=exit_code,
        workspace=workspace,
        artifacts=artifacts,
        parsed_payload=payload,
        schema_errors=schema_errors,
        turns=turns,
        tokens=tokens,
        observed_cost=observed_cost,
        effective_model=effective_model,
        effective_provider=effective_provider,
        fallback_observed=fallback_observed,
        attestation_source="hive.transcript.v2.provider_call_ledger" if effective_model else None,
        effective_resources=effective_resources,
        resource_sources=resource_sources,
        authority=authority,
        route_attestation=route_attestation,
        execution=execution,
    )


def _eval_integer_expression(node: ast.expr, bindings: dict[str, int]) -> int:
    if isinstance(node, ast.Name) and node.id in bindings:
        return bindings[node.id]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _eval_integer_expression(node.left, bindings) + _eval_integer_expression(node.right, bindings)
    raise ValueError("unsupported expression")


def _external_score(scenario_name: str, workspace_root: Path) -> dict[str, Any]:
    checks: dict[str, bool]
    if scenario_name == "coding":
        try:
            tree = ast.parse(_safe_read(workspace_root / "calculator.py"))
        except (SyntaxError, ValueError):
            tree = ast.Module(body=[], type_ignores=[])
        valid_add = False
        add_expression: ast.expr | None = None
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != "add":
                continue
            if [argument.arg for argument in node.args.args] != ["a", "b"] or len(node.body) != 1:
                continue
            statement = node.body[0]
            valid_add = bool(
                isinstance(statement, ast.Return)
                and isinstance(statement.value, ast.BinOp)
                and isinstance(statement.value.op, ast.Add)
                and isinstance(statement.value.left, ast.Name)
                and statement.value.left.id == "a"
                and isinstance(statement.value.right, ast.Name)
                and statement.value.right.id == "b"
            )
            if valid_add:
                add_expression = statement.value
        execution_assertions = bool(
            add_expression is not None
            and all(
                _eval_integer_expression(add_expression, {"a": a, "b": b}) == expected
                for a, b, expected in ((2, 3, 5), (-4, 1, -3), (0, 0, 0))
            )
        )
        checks = {
            "coding.ast_add": valid_add,
            "coding.execution_assertions": execution_assertions,
        }
    elif scenario_name == "review":
        content = _safe_read(workspace_root / "review.md")
        lowered = content.lower()
        finding_lines = [line for line in lowered.splitlines() if line.strip().startswith(("[p1]", "[p2]"))]
        checks = {
            "review.priority": bool(finding_lines),
            "review.file_ref": (workspace_root / "review_target.py").is_file()
            and any("review_target.py" in line for line in finding_lines),
            "review.issue": any(
                token in line for line in finding_lines for token in ("index", "range", "pop", "out of bounds")
            ),
        }
    elif scenario_name == "research":
        lines = {line.strip().lower() for line in _safe_read(workspace_root / "research_summary.md").splitlines()}
        checks = {
            "research.winner": "winner: atlas" in lines,
            "research.date": "ship date: 2026-05-01" in lines,
            "research.source_refs": "sources: source_alpha.md, source_beta.md" in lines,
        }
    elif scenario_name == "operations":
        lines = [line.strip().lower() for line in _safe_read(workspace_root / "ops_summary.md").splitlines()]
        staging_lines = [line for line in lines if line.startswith("staging verification:")]
        checks = {
            "operations.root_cause": "root cause: database_url missing" in lines,
            "operations.staging_verification": any(
                "database_url" in line and "startup" in line for line in staging_lines
            ),
        }
    elif scenario_name == "delegation":
        lines = {line.strip().lower() for line in _safe_read(workspace_root / "delegation_plan.md").splitlines()}
        checks = {
            "delegation.coverage": "tasks: task-a, task-b" in lines,
            "delegation.mode": bool({"mode: parallel", "mode: serial"} & lines),
            "delegation.fallback": "fallback: serial if delegation unavailable" in lines,
        }
    elif scenario_name == "memory_recall":
        checks = {"memory.exact_bytes": _safe_read(workspace_root / "memory_answer.md") == "cedar-lantern"}
    elif scenario_name == "self_evolution":
        lines = {line.strip().lower() for line in _safe_read(workspace_root / "self_evolution.md").splitlines()}
        checks = {
            "self_evolution.repeated_evidence": "evidence count: 6" in lines,
            "self_evolution.skill_decision": bool(
                {"decision: update existing skill", "decision: create new skill"} & lines
            ),
        }
    elif scenario_name == "long_context_after_compaction":
        checks = {
            "long_context.exact_bytes": _safe_read(workspace_root / "long_context_answer.md") == "delta-saffron-42"
        }
    else:
        raise ValueError(f"Unsupported external scoring scenario: {scenario_name}")
    passed = sum(1 for value in checks.values() if value)
    return {
        "score": round((passed / len(checks)) * 100) if checks else 0,
        "ready": bool(checks) and passed == len(checks),
        "criteria": checks,
        "source": "external_workspace_assertions",
    }


def _receipt_blockers(
    receipt: dict[str, Any],
    envelope: dict[str, Any],
    envelope_sha256: str,
    *,
    output_dir: Path,
    config: J4RuntimeConfig,
    prepared: PreparedJ4Runtimes,
    expected_prompt: str,
) -> list[str]:
    runtime = str(receipt.get("runtime") or "")
    blockers: list[str] = []
    expected_workspace = _runtime_workspace_path(output_dir, runtime, envelope["envelope_id"]).resolve()
    if receipt.get("schema") != J4_RECEIPT_SCHEMA:
        blockers.append("receipt_schema")
    if receipt.get("status") != "completed":
        blockers.append(f"status:{receipt.get('status') or 'missing'}")
    if receipt.get("envelope_sha256") != envelope_sha256:
        blockers.append("envelope_sha256")
    if receipt.get("seed_sha256") != envelope["workspace"]["seed_sha256"]:
        blockers.append("seed_sha256")
    if receipt.get("scorer_sha256") != envelope["scorer"]["source_sha256"]:
        blockers.append("scorer_sha256")
    if receipt.get("scorer_loaded_code_sha256") != envelope["scorer"]["loaded_code_sha256"]:
        blockers.append("scorer_loaded_code_sha256")
    if receipt.get("effective_model") != J4_MODEL:
        blockers.append("effective_model")
    allowed_routes = envelope["model"]["allowed_provider_routes"].get(runtime, [])
    if receipt.get("effective_provider") not in allowed_routes:
        blockers.append("effective_provider")
    if receipt.get("fallback_observed") is not False:
        blockers.append("fallback_observed")
    if not receipt.get("attestation_source"):
        blockers.append("attestation_source")
    if receipt.get("parsed", {}).get("schema_valid") is not True:
        blockers.append("parsed_schema")
    if receipt.get("workspace", {}).get("boundary_ok") is not True:
        blockers.append("workspace_boundary")
    if receipt.get("workspace", {}).get("before_sha256") != envelope["workspace"]["seed_sha256"]:
        blockers.append("workspace_seed")
    if receipt.get("workspace", {}).get("local_path") != str(expected_workspace):
        blockers.append("workspace_identity")
    expected_resources = {
        "max_tool_rounds": envelope["resources"]["max_tool_rounds"],
        "wall_clock_seconds": envelope["resources"]["wall_clock_seconds"],
        "reasoning_effort": envelope["model"]["reasoning_effort"],
    }
    requested = receipt.get("resources", {}).get("requested")
    effective = receipt.get("resources", {}).get("effective")
    resource_sources = receipt.get("resources", {}).get("sources")
    if (
        requested != expected_resources
        or effective != expected_resources
        or not isinstance(resource_sources, dict)
        or any(not resource_sources.get(name) for name in expected_resources)
    ):
        blockers.append("hard_common_resources")
    expected_authority = {
        "allowed_tools": envelope["authority"]["allowed_tools"].get(runtime),
        "readable_scope": envelope["authority"]["readable_scope"],
        "writable_scope": envelope["authority"]["writable_scope"],
    }
    authority = receipt.get("authority") if isinstance(receipt.get("authority"), dict) else {}
    authority_sources = authority.get("sources") if isinstance(authority.get("sources"), dict) else {}
    sandbox = authority.get("sandbox") if isinstance(authority.get("sandbox"), dict) else {}
    if (
        authority.get("requested") != expected_authority
        or authority.get("effective") != expected_authority
        or not authority_sources.get("allowed_tools")
        or not authority_sources.get("readable_scope")
        or not authority_sources.get("writable_scope")
        or sandbox.get("status") != "enforced"
    ):
        blockers.append("authority_envelope")
    route_attestation = receipt.get("route_attestation") if isinstance(receipt.get("route_attestation"), dict) else {}
    routes = route_attestation.get("routes") if isinstance(route_attestation.get("routes"), list) else []
    call_count = route_attestation.get("call_count")
    count_semantics = route_attestation.get("count_semantics", "exact")
    turns = receipt.get("turns")
    hive_tool_identity_valid = runtime != "hive" or (
        route_attestation.get("tool_names") == list(envelope["authority"]["allowed_tools"]["hive"])
        and bool(re.fullmatch(r"[0-9a-f]{64}", str(route_attestation.get("tool_schema_sha256") or "")))
    )
    if (
        not isinstance(call_count, int)
        or isinstance(call_count, bool)
        or call_count < 1
        or count_semantics not in {"exact", "minimum_observed"}
        or not isinstance(turns, int)
        or isinstance(turns, bool)
        or turns < 1
        or (count_semantics == "exact" and turns != call_count)
        or (count_semantics == "minimum_observed" and turns < call_count)
        or not route_attestation.get("source")
        or not hive_tool_identity_valid
        or not routes
        or any(
            not isinstance(route, dict) or route.get("model") != J4_MODEL or route.get("provider") not in allowed_routes
            for route in routes
        )
    ):
        blockers.append("provider_call_evidence")
    if runtime == "hive":
        execution = receipt.get("execution") if isinstance(receipt.get("execution"), dict) else {}
        active_fence = execution.get("active_fence") if isinstance(execution.get("active_fence"), dict) else {}
        attempt_id = str(execution.get("attempt_id") or "")
        expected_root_prefix = f"workspace/p08-j4/{attempt_id}/"
        if (
            not attempt_id
            or not execution.get("session_id")
            or not execution.get("run_id")
            or not str(execution.get("remote_root") or "").startswith(expected_root_prefix)
            or execution.get("terminal_status") != "completed"
            or active_fence.get("status") != "settled"
            or not isinstance(active_fence.get("http_status"), int)
            or not 200 <= active_fence["http_status"] < 300
            or active_fence.get("active_run_id") is not None
        ):
            blockers.append("execution_refs")
    binary = receipt.get("binary") if isinstance(receipt.get("binary"), dict) else {}
    if not all(binary.get(key) for key in ("path", "version", "revision")) or not re.fullmatch(
        r"[0-9a-f]{64}", str(binary.get("sha256") or "")
    ):
        blockers.append("binary_identity")
    if runtime == "hive" and (
        binary.get("path") != str(config.hive_base_url or "").strip()
        or binary.get("revision") != config.hive_revision
        or binary.get("sha256") != str(config.hive_binary_sha256 or "").strip().lower()
    ):
        blockers.append("hive_expected_runtime_identity")
    if runtime == "hermes":
        if binary != prepared.hermes_binary:
            blockers.append("hermes_expected_runtime_identity")
        components = binary.get("components") if isinstance(binary.get("components"), dict) else {}
        python_component = components.get("python") if isinstance(components.get("python"), dict) else {}
        launcher_component = components.get("launcher") if isinstance(components.get("launcher"), dict) else {}
        source_component = components.get("source") if isinstance(components.get("source"), dict) else {}
        argv = receipt.get("argv") if isinstance(receipt.get("argv"), list) else []
        expected_argv = _hermes_command(
            workspace_root=expected_workspace,
            envelope=envelope,
            python=prepared.hermes_python,
            launcher=prepared.hermes_launcher,
        )
        expected_excluded_runtime_roots: list[str] = []
        try:
            venv_root = Path(str(python_component.get("venv_root") or "")).resolve()
            source_root = Path(str(source_component.get("root") or "")).resolve()
            venv_root.relative_to(source_root)
            expected_excluded_runtime_roots = [str(venv_root)]
        except (OSError, ValueError):
            pass
        if (
            binary.get("runtime_sha256") != binary.get("sha256")
            or not re.fullmatch(r"[0-9a-f]{64}", str(binary.get("runtime_sha256") or ""))
            or not re.fullmatch(r"[0-9a-f]{64}", str(python_component.get("sha256") or ""))
            or not re.fullmatch(r"[0-9a-f]{64}", str(python_component.get("entry_sha256") or ""))
            or not re.fullmatch(r"[0-9a-f]{64}", str(python_component.get("resolved_sha256") or ""))
            or not re.fullmatch(r"[0-9a-f]{64}", str(python_component.get("pyvenv_sha256") or ""))
            or not re.fullmatch(r"[0-9a-f]{64}", str(python_component.get("tree_sha256") or ""))
            or not re.fullmatch(r"[0-9a-f]{64}", str(python_component.get("base_python_sha256") or ""))
            or not re.fullmatch(r"[0-9a-f]{64}", str(python_component.get("base_tree_sha256") or ""))
            or not isinstance(python_component.get("tree_entry_count"), int)
            or python_component.get("tree_entry_count", 0) < 1
            or not isinstance(python_component.get("base_tree_entry_count"), int)
            or python_component.get("base_tree_entry_count", 0) < 1
            or python_component.get("kind") not in {"file", "symlink"}
            or not re.fullmatch(r"[0-9a-f]{64}", str(launcher_component.get("sha256") or ""))
            or not re.fullmatch(r"[0-9a-f]{64}", str(source_component.get("sha256") or ""))
            or not re.fullmatch(r"[0-9a-f]{64}", str(source_component.get("lock_sha256") or ""))
            or source_component.get("revision") != binary.get("revision")
            or source_component.get("clean") is not True
            or source_component.get("excluded_runtime_roots") != expected_excluded_runtime_roots
            or source_component.get("scope") != HERMES_J4_SOURCE_SCOPE
            or len(argv) < 4
            or argv != expected_argv
            or argv[0] != binary.get("path")
            or argv[0] != python_component.get("base_python_path")
            or argv[1:3] != ["-I", "-S"]
            or argv[3] != launcher_component.get("path")
        ):
            blockers.append("hermes_runtime_identity")
        execution = receipt.get("execution") if isinstance(receipt.get("execution"), dict) else {}
        session_attestation = (
            execution.get("session_attestation") if isinstance(execution.get("session_attestation"), dict) else {}
        )
        expected_state_root = _hermes_state_root(
            expected_workspace,
            str(envelope["envelope_id"]),
        ).resolve()
        expected_attempt_environment_sha256 = _sha256_json(
            {
                "HERMES_HOME": str(expected_state_root / "hermes-home"),
                "HOME": str(expected_state_root / "os-home"),
                "CODEX_HOME": str(expected_state_root / "codex-home"),
                "HERMES_MANAGED_DIR": str(expected_state_root / "managed"),
                "TMPDIR": str(expected_state_root / "tmp"),
                "TMP": str(expected_state_root / "tmp"),
                "TEMP": str(expected_state_root / "tmp"),
                "HERMES_SAFE_MODE": "1",
                "HERMES_IGNORE_USER_CONFIG": "1",
                "HERMES_IGNORE_RULES": "1",
                "HERMES_BUNDLED_SKILLS": str(expected_state_root / "nonexistent-bundled-skills"),
                "PYTHONNOUSERSITE": "1",
            }
        )
        if (
            execution.get("chat_spawn_count") != 1
            or execution.get("argv_sha256") != _sha256_json(expected_argv)
            or execution.get("transcript_export_spawn_count") != session_attestation.get("lineage_count")
            or execution.get("cwd") != receipt.get("workspace", {}).get("local_path")
            or execution.get("workspace_flag") != receipt.get("workspace", {}).get("local_path")
            or execution.get("pre_state_db_absent") is not True
            or execution.get("state_root_owned") is not True
            or execution.get("state_cleanup_verified") is not True
            or execution.get("runtime_pre_stable") is not True
            or execution.get("runtime_post_chat_stable") is not True
            or execution.get("runtime_final_stable") is not True
            or execution.get("auth_profile") != prepared.hermes_auth_profile
            or execution.get("attempt_environment_sha256") != expected_attempt_environment_sha256
            or session_attestation.get("ok") is not True
            or session_attestation.get("session_id") != execution.get("session_id")
        ):
            blockers.append("hermes_invocation_identity")
    if runtime == "freecode":
        components = binary.get("components") if isinstance(binary.get("components"), dict) else {}
        build_manifest = components.get("build_manifest") if isinstance(components.get("build_manifest"), dict) else {}
        fresh_build_receipt = (
            components.get("fresh_build_receipt") if isinstance(components.get("fresh_build_receipt"), dict) else {}
        )
        source_component = components.get("source") if isinstance(components.get("source"), dict) else {}
        guard = components.get("authority_guard") if isinstance(components.get("authority_guard"), dict) else {}
        execution = receipt.get("execution") if isinstance(receipt.get("execution"), dict) else {}
        expected_artifact = prepared.freecode_manifest["artifact"]
        expected_source = prepared.freecode_manifest["source"]
        expected_runtime_root = expected_workspace.parent / "runtime"
        expected_argv = _freecode_command(
            prompt=expected_prompt,
            envelope=envelope,
            workspace_root=expected_workspace,
            binary=expected_runtime_root / "freecode",
            hook=expected_runtime_root / "freecode_j4_hook.py",
            hook_python=prepared.freecode_hook_python,
        )
        argv = receipt.get("argv") if isinstance(receipt.get("argv"), list) else []
        if (
            binary.get("path") != str(expected_runtime_root / "freecode")
            or binary.get("version") != expected_artifact.get("version")
            or binary.get("sha256") != expected_artifact.get("sha256")
            or binary.get("revision") != expected_source.get("revision")
            or binary.get("runtime_sha256") != _freecode_runtime_sha256(prepared)
            or not re.fullmatch(r"[0-9a-f]{64}", str(binary.get("runtime_sha256") or ""))
            or not re.fullmatch(r"[0-9a-f]{64}", str(build_manifest.get("sha256") or ""))
            or build_manifest.get("sha256") != prepared.freecode_manifest_sha256
            or build_manifest.get("schema") != FREECODE_BUILD_MANIFEST_SCHEMA
            or fresh_build_receipt != prepared.freecode_build_receipt
            or _sha256_json(fresh_build_receipt) != prepared.freecode_build_receipt_sha256
            or fresh_build_receipt.get("inputs_stable") is not True
            or fresh_build_receipt.get("network_access") is not False
            or fresh_build_receipt.get("artifact") != expected_artifact
            or source_component != expected_source
            or not re.fullmatch(r"[0-9a-f]{64}", str(guard.get("sha256") or ""))
            or guard.get("path") != str(expected_runtime_root / "freecode_j4_hook.py")
            or guard.get("sha256") != prepared.freecode_hook_sha256
            or guard.get("python_path") != str(prepared.freecode_hook_python)
            or not re.fullmatch(r"[0-9a-f]{64}", str(guard.get("python_sha256") or ""))
            or guard.get("python_sha256") != prepared.freecode_hook_python_sha256
            or not re.fullmatch(r"[0-9a-f]{64}", str(guard.get("python_environment_sha256") or ""))
            or guard.get("python_environment_sha256") != prepared.freecode_hook_python_environment_sha256
            or execution.get("chat_spawn_count") != 1
            or execution.get("attempt_roots_owned") is not True
            or execution.get("state_cleanup_verified") is not True
            or argv != expected_argv
            or execution.get("argv_sha256") != _sha256_json(expected_argv)
            or execution.get("cwd") != receipt.get("workspace", {}).get("local_path")
            or execution.get("runtime_pre_sha256") != binary.get("sha256")
            or execution.get("runtime_post_sha256") != binary.get("sha256")
            or execution.get("guard_pre_sha256") != guard.get("sha256")
            or execution.get("guard_post_sha256") != guard.get("sha256")
            or execution.get("guard_python_pre_sha256") != guard.get("python_sha256")
            or execution.get("guard_python_post_sha256") != guard.get("python_sha256")
            or execution.get("guard_python_environment_pre_sha256") != guard.get("python_environment_sha256")
            or execution.get("guard_python_environment_post_sha256") != guard.get("python_environment_sha256")
            or not isinstance(execution.get("hook_log"), dict)
            or execution["hook_log"].get("valid") is not True
            or execution["hook_log"].get("sha256") != receipt.get("artifacts", {}).get("transcript", {}).get("sha256")
        ):
            blockers.append("freecode_runtime_identity")
    artifacts = receipt.get("artifacts") if isinstance(receipt.get("artifacts"), dict) else {}
    for name in ("stdout", "stderr", "transcript"):
        artifact = artifacts.get(name) if isinstance(artifacts.get(name), dict) else {}
        if not artifact.get("path") or not re.fullmatch(r"[0-9a-f]{64}", str(artifact.get("sha256") or "")):
            blockers.append(f"artifact:{name}")
    return blockers


def _empty_j4_report(*, blockers: list[dict[str, Any]], receipts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "kind": "bakeoff",
        "schema": J4_ENVELOPE_SCHEMA,
        "transport": "runtime_unavailable",
        "runtime": {"status": "blocked"},
        "auth_status": "blocked",
        "benchmark_complete": False,
        "acceptance_ready": False,
        "comparison": {"status": "blocked", "scores": {}, "blockers": blockers},
        "scenario_scores": {},
        "receipts": receipts or [],
        "envelopes": [],
        "artifact_paths": [],
        "scenarios": {},
    }


def _post_run_blocked_report(
    *,
    blockers: list[dict[str, Any]],
    receipts: list[dict[str, Any]],
    envelopes: list[dict[str, Any]],
    scoring_snapshots: dict[tuple[str, str], tuple[Path, str]],
    scorer_artifact: dict[str, Any],
) -> dict[str, Any]:
    statuses = {
        runtime: sorted(
            {str(receipt.get("status") or "missing") for receipt in receipts if receipt.get("runtime") == runtime}
        )
        for runtime in _J4_RUNTIME_ORDER
    }
    auth_required = [runtime for runtime, observed in statuses.items() if "auth_required" in observed]
    return {
        "kind": "bakeoff",
        "schema": J4_ENVELOPE_SCHEMA,
        "transport": "same_envelope_live",
        "runtime": {"status": "completed_with_blockers", "observed_statuses": statuses},
        "auth_status": "auth_required" if auth_required else "ok",
        "auth_required_runtimes": auth_required,
        "benchmark_complete": False,
        "acceptance_ready": False,
        "comparison": {"status": "blocked", "scores": {}, "blockers": blockers},
        "scenario_scores": {},
        "receipts": receipts,
        "envelopes": envelopes,
        "artifact_paths": [
            artifact["path"]
            for receipt in receipts
            for artifact in receipt.get("artifacts", {}).values()
            if isinstance(artifact, dict) and artifact.get("path")
        ],
        "scoring_snapshot_paths": [str(path) for path, _sha256 in scoring_snapshots.values()],
        "scorer_artifact": scorer_artifact,
        "scenarios": {},
    }


def _j4_acceptance_decision(scenario_scores: dict[str, dict[str, Any]]) -> dict[str, Any]:
    comparisons: dict[str, dict[str, bool]] = {}
    all_hard_criteria_ready = bool(scenario_scores)
    hive_not_weaker = bool(scenario_scores)
    for scenario_name, runtime_scores in scenario_scores.items():
        criteria_names = set(_J4_CRITERIA.get(scenario_name, ()))
        exact_coverage = bool(criteria_names) and all(
            set(((runtime_scores.get(runtime) or {}).get("criteria") or {})) == criteria_names
            for runtime in _J4_RUNTIME_ORDER
        )
        all_hard_criteria_ready = (
            all_hard_criteria_ready
            and all(bool((runtime_scores.get(runtime) or {}).get("ready")) for runtime in _J4_RUNTIME_ORDER)
            and exact_coverage
        )
        if not exact_coverage:
            all_hard_criteria_ready = False
            hive_not_weaker = False
        for criterion in sorted(criteria_names):
            observed = {
                runtime: bool(((runtime_scores.get(runtime) or {}).get("criteria") or {}).get(criterion))
                for runtime in _J4_RUNTIME_ORDER
            }
            not_weaker = observed["hive"] or not (observed["freecode"] or observed["hermes"])
            hive_not_weaker = hive_not_weaker and not_weaker
            comparisons[f"{scenario_name}.{criterion}"] = {**observed, "hive_not_weaker": not_weaker}
    return {
        "acceptance_ready": all_hard_criteria_ready and hive_not_weaker,
        "all_hard_criteria_ready": all_hard_criteria_ready,
        "hive_not_weaker": hive_not_weaker,
        "comparisons": comparisons,
    }


def _j4_runtime_preflight(config: J4RuntimeConfig) -> list[dict[str, Any]]:
    """Compatibility wrapper for callers that only need a no-model readiness result."""

    prepared, blockers = _prepare_j4_runtimes(config)
    if prepared is not None:
        _cleanup_prepared(prepared)
    return blockers


def run_same_envelope_bakeoff(
    *,
    output_dir: Path,
    config: J4RuntimeConfig,
) -> dict[str, Any]:
    """Run the manual three-runtime P08-J4 comparison without any model preflight."""

    output_dir = output_dir.expanduser().resolve()
    precondition_blockers: list[dict[str, Any]] = []
    if not config.external_profile_authorized:
        precondition_blockers.append(
            {"code": "external_profile_authority_required", "runtimes": ["freecode", "hermes"]}
        )
    hive_base_url = str(config.hive_base_url or "").strip()
    hive_configured = all(
        (
            _valid_hive_base_url(hive_base_url),
            config.hive_bearer,
            config.hive_agent_id,
        )
    )
    if not hive_configured:
        precondition_blockers.append({"code": "hive_session_authority_required", "runtime": "hive"})
    if not str(config.hive_revision or "").strip() or not re.fullmatch(
        r"[0-9a-f]{64}", str(config.hive_binary_sha256 or "").strip().lower()
    ):
        precondition_blockers.append({"code": "hive_build_identity_required", "runtime": "hive"})
    if (
        config.max_tool_rounds < 1
        or config.wall_clock_seconds < 1
        or config.cancel_fence_seconds < 1
        or config.max_budget_usd <= 0
        or config.max_files < 1
        or config.max_bytes < 1
    ):
        precondition_blockers.append({"code": "resource_configuration_invalid"})
    if config.require_same_credential_domain:
        precondition_blockers.append(
            {
                "code": "model_auth_unavailable",
                "detail": "The three allowed routes use different provider and credential domains.",
            }
        )
    if precondition_blockers:
        return _empty_j4_report(blockers=precondition_blockers)

    prepared, runtime_blockers = _prepare_j4_runtimes(config)
    if runtime_blockers:
        return _empty_j4_report(blockers=runtime_blockers)
    assert prepared is not None

    try:
        return _run_same_envelope_bakeoff_prepared(
            output_dir=output_dir,
            config=config,
            prepared=prepared,
        )
    finally:
        _cleanup_prepared(prepared)


def _run_same_envelope_bakeoff_prepared(
    *,
    output_dir: Path,
    config: J4RuntimeConfig,
    prepared: PreparedJ4Runtimes,
) -> dict[str, Any]:

    scorer_identity = _scorer_runtime_identity()
    scorer_source, scorer_errors = _freeze_scorer_source(output_dir, scorer_identity)
    if scorer_errors:
        return _empty_j4_report(blockers=[{"code": code, "runtime": "external_scorer"} for code in scorer_errors])
    assert scorer_source is not None
    scorer_artifact = {
        "path": str(scorer_source),
        **scorer_identity,
    }

    seed_root = output_dir / "j4_seed"
    try:
        seed_root.mkdir(parents=True, exist_ok=False)
    except FileExistsError:
        return _empty_j4_report(blockers=[{"code": "seed_workspace_conflict"}])
    except OSError:
        return _empty_j4_report(blockers=[{"code": "seed_workspace_unavailable"}])
    receipts: list[dict[str, Any]] = []
    envelopes: list[dict[str, Any]] = []
    envelope_hashes: dict[str, str] = {}
    scenario_prompts: dict[str, str] = {}
    stopped_runtimes: set[str] = set()
    runtime_drift_blockers: list[dict[str, Any]] = []
    scoring_snapshots: dict[tuple[str, str], tuple[Path, str]] = {}
    stop_all = False
    adapters = {
        "hive": _run_hive_j4,
        "freecode": _run_freecode_j4,
        "hermes": _run_hermes_j4,
    }
    for scenario_name in _SCENARIOS:
        drift = _prepared_runtime_blockers(prepared, config)
        if not _scorer_runtime_stable(scorer_identity, scorer_source):
            drift.append({"code": "runtime_identity_drift", "runtime": "external_scorer"})
        if drift:
            runtime_drift_blockers.extend(
                {**item, "scenario": scenario_name, "phase": "scenario_pre"} for item in drift
            )
            break
        scenario = _scenario_workspace(seed_root, scenario_name)
        envelope, envelope_sha256 = _build_same_envelope(
            scenario,
            config=config,
            scorer_identity=scorer_identity,
        )
        envelopes.append({"envelope": envelope, "sha256": envelope_sha256})
        envelope_hashes[scenario_name] = envelope_sha256
        scenario_prompts[scenario_name] = scenario.prompt
        for runtime in _J4_RUNTIME_ORDER:
            if runtime in stopped_runtimes:
                continue
            adapter_kwargs: dict[str, Any] = {"output_dir": output_dir, "config": config}
            if runtime != "hive":
                adapter_kwargs["prepared"] = prepared
            receipt = adapters[runtime](scenario, envelope, envelope_sha256, **adapter_kwargs)
            receipts.append(receipt)
            if receipt.get("status") == "completed":
                snapshot_path, snapshot_sha256, snapshot_errors = _create_scoring_snapshot(
                    output_dir=output_dir,
                    runtime=runtime,
                    envelope=envelope,
                    receipt=receipt,
                )
                if snapshot_errors:
                    runtime_drift_blockers.extend(
                        {
                            "code": error,
                            "runtime": runtime,
                            "scenario": scenario_name,
                            "phase": "post_receipt",
                        }
                        for error in snapshot_errors
                    )
                    stop_all = True
                    break
                assert snapshot_path is not None
                scoring_snapshots[(scenario_name, runtime)] = (snapshot_path, snapshot_sha256)
            if receipt.get("status") == "auth_required":
                stopped_runtimes.add(runtime)
        if stop_all:
            break

    blockers: list[dict[str, Any]] = list(runtime_drift_blockers)
    by_scenario_runtime = {
        (str(envelope["task"]["scenario"]), runtime): next(
            (
                receipt
                for receipt in receipts
                if receipt.get("runtime") == runtime
                and receipt.get("envelope_sha256") == envelope_hashes[str(envelope["task"]["scenario"])]
            ),
            None,
        )
        for envelope_entry in envelopes
        for envelope in [envelope_entry["envelope"]]
        for runtime in _J4_RUNTIME_ORDER
    }
    for envelope_entry in envelopes:
        envelope = envelope_entry["envelope"]
        scenario_name = str(envelope["task"]["scenario"])
        envelope_sha256 = envelope_entry["sha256"]
        common_resources: set[str] = set()
        for runtime in _J4_RUNTIME_ORDER:
            receipt = by_scenario_runtime[(scenario_name, runtime)]
            if receipt is None:
                blockers.append({"scenario": scenario_name, "runtime": runtime, "code": "receipt_missing"})
                continue
            for code in _receipt_blockers(
                receipt,
                envelope,
                envelope_sha256,
                output_dir=output_dir,
                config=config,
                prepared=prepared,
                expected_prompt=scenario_prompts[scenario_name],
            ):
                blockers.append({"scenario": scenario_name, "runtime": runtime, "code": code})
            common_resources.add(_canonical_json(receipt.get("resources", {}).get("effective")))
        if len(common_resources) != 1:
            blockers.append({"scenario": scenario_name, "code": "hard_common_mismatch"})

    for runtime in _J4_RUNTIME_ORDER:
        identities = {
            (
                _sha256_json(
                    {key: receipt.get("binary", {}).get(key) for key in ("path", "version", "sha256", "revision")}
                )
                if runtime == "hive"
                else str(receipt.get("binary", {}).get("runtime_sha256") or "")
            )
            for receipt in receipts
            if receipt.get("runtime") == runtime
        }
        if len(identities) != 1 or not all(re.fullmatch(r"[0-9a-f]{64}", value) for value in identities):
            blockers.append({"runtime": runtime, "code": "cross_scenario_runtime_identity"})

    hive_tool_identities = {
        _canonical_json(
            {
                "tool_names": receipt.get("route_attestation", {}).get("tool_names"),
                "tool_schema_sha256": receipt.get("route_attestation", {}).get("tool_schema_sha256"),
            }
        )
        for receipt in receipts
        if receipt.get("runtime") == "hive"
    }
    if len(hive_tool_identities) != 1:
        blockers.append({"runtime": "hive", "code": "cross_scenario_hive_tool_schema_identity"})

    scorer_identities = {
        _sha256_json(
            {
                "source_sha256": receipt.get("scorer_sha256"),
                "loaded_code_sha256": receipt.get("scorer_loaded_code_sha256"),
            }
        )
        for receipt in receipts
    }
    if scorer_identities != {
        _sha256_json(
            {
                "source_sha256": scorer_identity["source_sha256"],
                "loaded_code_sha256": scorer_identity["loaded_code_sha256"],
            }
        )
    }:
        blockers.append({"runtime": "external_scorer", "code": "cross_scenario_scorer_identity"})

    if blockers:
        report = _post_run_blocked_report(
            blockers=blockers,
            receipts=receipts,
            envelopes=envelopes,
            scoring_snapshots=scoring_snapshots,
            scorer_artifact=scorer_artifact,
        )
        return report

    scenario_scores: dict[str, dict[str, Any]] = {}
    scenarios: dict[str, dict[str, Any]] = {}
    runtime_totals = {runtime: [] for runtime in _J4_RUNTIME_ORDER}
    for envelope_entry in envelopes:
        envelope = envelope_entry["envelope"]
        scenario_name = str(envelope["task"]["scenario"])
        scenario_scores[scenario_name] = {}
        for runtime in _J4_RUNTIME_ORDER:
            receipt = by_scenario_runtime[(scenario_name, runtime)]
            assert receipt is not None
            snapshot_path, expected_snapshot_sha = scoring_snapshots[(scenario_name, runtime)]
            snapshot_before, snapshot_before_errors = _manifest(snapshot_path)
            if (
                snapshot_before_errors
                or _sha256_json(snapshot_before) != expected_snapshot_sha
                or not _scorer_runtime_stable(scorer_identity, scorer_source)
            ):
                blockers.append(
                    {
                        "scenario": scenario_name,
                        "runtime": runtime,
                        "code": (
                            "scoring_snapshot_drift"
                            if snapshot_before_errors or _sha256_json(snapshot_before) != expected_snapshot_sha
                            else "scorer_runtime_drift"
                        ),
                    }
                )
                break
            score = _external_score(scenario_name, snapshot_path)
            snapshot_after, snapshot_after_errors = _manifest(snapshot_path)
            if (
                snapshot_after_errors
                or _sha256_json(snapshot_after) != expected_snapshot_sha
                or not _scorer_runtime_stable(scorer_identity, scorer_source)
            ):
                blockers.append(
                    {
                        "scenario": scenario_name,
                        "runtime": runtime,
                        "code": (
                            "scoring_snapshot_drift"
                            if snapshot_after_errors or _sha256_json(snapshot_after) != expected_snapshot_sha
                            else "scorer_runtime_drift"
                        ),
                    }
                )
                break
            receipt["score"] = score
            scenario_scores[scenario_name][runtime] = score
            runtime_totals[runtime].append(int(score["score"]))
        if blockers:
            break
        scores = [int(details["score"]) for details in scenario_scores[scenario_name].values()]
        scenarios[scenario_name] = {
            "ready": all(details["ready"] for details in scenario_scores[scenario_name].values()),
            "score": round(sum(scores) / len(scores)),
            "transcript": "external_workspace_assertions",
            "rubric": f"P08-J4 external rubric {scenario_name}",
            "score_breakdown": scenario_scores[scenario_name],
        }
    if blockers:
        report = _post_run_blocked_report(
            blockers=blockers,
            receipts=receipts,
            envelopes=envelopes,
            scoring_snapshots=scoring_snapshots,
            scorer_artifact=scorer_artifact,
        )
        return report
    comparison_scores = {
        runtime: round(sum(scores) / len(scores), 2) if scores else 0.0 for runtime, scores in runtime_totals.items()
    }
    acceptance = _j4_acceptance_decision(scenario_scores)
    report = {
        "kind": "bakeoff",
        "schema": J4_ENVELOPE_SCHEMA,
        "transport": "same_envelope_live",
        "runtime": {"status": "completed"},
        "auth_status": "ok",
        "benchmark_complete": True,
        "acceptance_ready": acceptance["acceptance_ready"],
        "comparison": {
            "status": "completed",
            "scores": comparison_scores,
            "blockers": [],
            "acceptance": acceptance,
        },
        "scenario_scores": scenario_scores,
        "receipts": receipts,
        "envelopes": envelopes,
        "artifact_paths": [
            artifact["path"]
            for receipt in receipts
            for artifact in receipt.get("artifacts", {}).values()
            if isinstance(artifact, dict) and artifact.get("path")
        ],
        "scoring_snapshot_paths": [str(path) for path, _sha256 in scoring_snapshots.values()],
        "scorer_artifact": scorer_artifact,
        "scenarios": scenarios,
    }
    return report
