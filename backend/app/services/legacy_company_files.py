"""Read-only quarantine/export for the retired shared-file surface."""

from __future__ import annotations

import hashlib
import json
from contextlib import suppress
from dataclasses import dataclass
import errno
import os
from pathlib import Path
import stat as stat_module
import tempfile
from typing import BinaryIO
import zipfile

import anyio


COMPANY_CONTEXT_FILENAMES = frozenset({"company_profile.md", "org_structure.md"})
_IGNORED_CONTROL_FILENAMES = frozenset({".gitkeep"})
_READ_CHUNK_SIZE = 1024 * 1024
_CHANGED_ERRNOS = frozenset(
    value
    for value in (
        errno.ENOENT,
        getattr(errno, "ESTALE", None),
        getattr(errno, "ELOOP", None),
    )
    if value is not None
)


def company_context_path_allowed(relative_path: str, *, directory: bool = False) -> bool:
    """Allow only the two generated company-context files to the Agent runtime."""

    normalized = str(relative_path or "").replace("\\", "/").strip().strip("/")
    if directory:
        return normalized in {"", "enterprise_info"}
    if normalized.startswith("enterprise_info/"):
        normalized = normalized.removeprefix("enterprise_info/")
    return normalized in COMPANY_CONTEXT_FILENAMES


@dataclass(frozen=True, slots=True)
class LegacyCompanyFile:
    relative_path: str
    absolute_path: Path
    size_bytes: int
    sha256: str
    device: int | None = None
    inode: int | None = None
    mtime_ns: int | None = None


@dataclass(frozen=True, slots=True)
class LegacyCompanyFilesSnapshot:
    files: tuple[LegacyCompanyFile, ...]
    excluded_symlink_count: int = 0

    @property
    def file_count(self) -> int:
        return len(self.files)

    @property
    def total_bytes(self) -> int:
        return sum(item.size_bytes for item in self.files)


@dataclass(frozen=True, slots=True)
class LegacyCompanyFilesExport:
    stream: BinaryIO
    size_bytes: int
    filename: str
    snapshot: LegacyCompanyFilesSnapshot


class LegacyCompanyFilesChangedError(RuntimeError):
    """The filesystem changed between evidence scan and archive materialization."""


class LegacyCompanyFilesUnavailableError(RuntimeError):
    """The retired-file quarantine cannot currently be read safely."""


def _check_worker_cancelled() -> None:
    """Cooperate with cancellation when called through ``anyio.to_thread``."""

    try:
        anyio.from_thread.check_cancelled()
    except RuntimeError:
        # Direct unit/service callers are not inside an AnyIO worker thread.
        return


def _raise_mapped_file_error(exc: OSError) -> None:
    if isinstance(exc, FileNotFoundError) or exc.errno in _CHANGED_ERRNOS:
        raise LegacyCompanyFilesChangedError("Retired shared files changed while they were being read") from exc
    raise LegacyCompanyFilesUnavailableError("Retired shared files are temporarily unavailable") from exc


def _open_regular_file(path: Path) -> tuple[BinaryIO, os.stat_result]:
    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        _raise_mapped_file_error(exc)
        raise AssertionError("unreachable")

    try:
        source_stat = os.fstat(descriptor)
        if not stat_module.S_ISREG(source_stat.st_mode):
            raise LegacyCompanyFilesChangedError("Retired shared files changed while they were being read")
        return os.fdopen(descriptor, "rb", closefd=True), source_stat
    except BaseException:
        os.close(descriptor)
        raise


def _stat_identity(source_stat: os.stat_result) -> tuple[int, int, int, int]:
    return (
        source_stat.st_dev,
        source_stat.st_ino,
        source_stat.st_size,
        source_stat.st_mtime_ns,
    )


def _snapshot_identity(item: LegacyCompanyFile) -> tuple[int, int, int, int] | None:
    if item.device is None or item.inode is None or item.mtime_ns is None:
        return None
    return (item.device, item.inode, item.size_bytes, item.mtime_ns)


def _read_fingerprint(path: Path) -> tuple[int, str, os.stat_result]:
    hasher = hashlib.sha256()
    size_bytes = 0
    try:
        source, before = _open_regular_file(path)
        with source:
            while chunk := source.read(_READ_CHUNK_SIZE):
                _check_worker_cancelled()
                size_bytes += len(chunk)
                hasher.update(chunk)
            after = os.fstat(source.fileno())
    except (LegacyCompanyFilesChangedError, LegacyCompanyFilesUnavailableError):
        raise
    except OSError as exc:
        _raise_mapped_file_error(exc)
        raise AssertionError("unreachable")

    if _stat_identity(before) != _stat_identity(after) or size_bytes != before.st_size:
        raise LegacyCompanyFilesChangedError("Retired shared files changed while they were being read")
    return size_bytes, hasher.hexdigest(), before


def scan_legacy_company_files(company_dir: Path) -> LegacyCompanyFilesSnapshot:
    """Return non-canonical files without following filesystem links."""

    root = Path(company_dir)
    try:
        root_stat = root.lstat()
    except FileNotFoundError:
        return LegacyCompanyFilesSnapshot(files=())
    except OSError as exc:
        _raise_mapped_file_error(exc)
        raise AssertionError("unreachable")
    if stat_module.S_ISLNK(root_stat.st_mode):
        raise LegacyCompanyFilesChangedError("Retired shared-file quarantine root changed")
    if not stat_module.S_ISDIR(root_stat.st_mode):
        raise LegacyCompanyFilesUnavailableError("Retired shared files are temporarily unavailable")

    try:
        resolved_root = root.resolve(strict=True)
    except OSError as exc:
        _raise_mapped_file_error(exc)
        raise AssertionError("unreachable")
    files: list[LegacyCompanyFile] = []
    excluded_symlinks = 0
    try:
        candidates = sorted(root.rglob("*"), key=lambda item: item.as_posix())
        for candidate in candidates:
            _check_worker_cancelled()
            if candidate.is_symlink():
                excluded_symlinks += 1
                continue
            if not candidate.is_file():
                continue
            try:
                relative = candidate.resolve().relative_to(resolved_root).as_posix()
            except ValueError:
                excluded_symlinks += 1
                continue
            if relative in COMPANY_CONTEXT_FILENAMES or candidate.name in _IGNORED_CONTROL_FILENAMES:
                continue
            size_bytes, sha256, source_stat = _read_fingerprint(candidate)
            files.append(
                LegacyCompanyFile(
                    relative_path=relative,
                    absolute_path=candidate,
                    size_bytes=size_bytes,
                    sha256=sha256,
                    device=source_stat.st_dev,
                    inode=source_stat.st_ino,
                    mtime_ns=source_stat.st_mtime_ns,
                )
            )
    except (LegacyCompanyFilesChangedError, LegacyCompanyFilesUnavailableError):
        raise
    except OSError as exc:
        _raise_mapped_file_error(exc)
    try:
        after_root_stat = root.lstat()
    except OSError as exc:
        _raise_mapped_file_error(exc)
        raise AssertionError("unreachable")
    if _stat_identity(root_stat) != _stat_identity(after_root_stat):
        raise LegacyCompanyFilesChangedError("Retired shared-file quarantine root changed")
    return LegacyCompanyFilesSnapshot(files=tuple(files), excluded_symlink_count=excluded_symlinks)


def _write_zip_entry(bundle: zipfile.ZipFile, name: str, content: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100600 << 16
    bundle.writestr(info, content)


def _copy_verified_file_to_zip(bundle: zipfile.ZipFile, item: LegacyCompanyFile) -> None:
    info = zipfile.ZipInfo(
        f"legacy_company_files/{item.relative_path}",
        date_time=(1980, 1, 1, 0, 0, 0),
    )
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100600 << 16
    hasher = hashlib.sha256()
    size_bytes = 0

    try:
        source, before = _open_regular_file(item.absolute_path)
        expected_identity = _snapshot_identity(item)
        if expected_identity is not None and _stat_identity(before) != expected_identity:
            source.close()
            raise LegacyCompanyFilesChangedError("Retired shared files changed while they were being exported")
        with source, bundle.open(info, "w") as target:
            while chunk := source.read(_READ_CHUNK_SIZE):
                _check_worker_cancelled()
                size_bytes += len(chunk)
                hasher.update(chunk)
                target.write(chunk)
            after = os.fstat(source.fileno())
    except (LegacyCompanyFilesChangedError, LegacyCompanyFilesUnavailableError):
        raise
    except OSError as exc:
        _raise_mapped_file_error(exc)
        raise AssertionError("unreachable")

    if (
        _stat_identity(before) != _stat_identity(after)
        or size_bytes != item.size_bytes
        or hasher.hexdigest() != item.sha256
    ):
        raise LegacyCompanyFilesChangedError("Retired shared files changed while they were being exported")


def build_legacy_company_files_export(
    company_dir: Path,
    *,
    tenant_id: str,
) -> LegacyCompanyFilesExport:
    """Create a deterministic evidence archive without mutating source files."""

    _check_worker_cancelled()
    snapshot = scan_legacy_company_files(company_dir)

    manifest = {
        "schema": "hive.legacy_company_files_export.v1",
        "tenant_id": str(tenant_id),
        "retired_surface": "/enterprise/knowledge-base",
        "read_only": True,
        "excluded_symlink_count": snapshot.excluded_symlink_count,
        "files": [
            {
                "relative_path": item.relative_path,
                "size_bytes": item.size_bytes,
                "sha256": item.sha256,
            }
            for item in snapshot.files
        ],
    }
    try:
        output = tempfile.TemporaryFile(mode="w+b")
    except OSError as exc:
        raise LegacyCompanyFilesUnavailableError("Retired shared files are temporarily unavailable") from exc
    try:
        with zipfile.ZipFile(output, "w") as bundle:
            for item in snapshot.files:
                _copy_verified_file_to_zip(bundle, item)
            _write_zip_entry(
                bundle,
                "manifest.json",
                (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            )
        size_bytes = output.tell()
        output.seek(0)
    except BaseException:
        with suppress(Exception):
            output.close()
        raise
    return LegacyCompanyFilesExport(
        stream=output,
        size_bytes=size_bytes,
        filename=f"hive-legacy-company-files-{tenant_id}.zip",
        snapshot=snapshot,
    )
