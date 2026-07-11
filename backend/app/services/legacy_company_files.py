"""Read-only quarantine/export for the retired file-tree Company KB surface."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
import tempfile
from typing import BinaryIO
import zipfile


COMPANY_CONTEXT_FILENAMES = frozenset({"company_profile.md", "org_structure.md"})
_IGNORED_CONTROL_FILENAMES = frozenset({".gitkeep"})


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


def _sha256(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def scan_legacy_company_files(company_dir: Path) -> LegacyCompanyFilesSnapshot:
    """Return non-canonical files without following filesystem links."""

    root = Path(company_dir)
    if not root.exists() or not root.is_dir():
        return LegacyCompanyFilesSnapshot(files=())

    resolved_root = root.resolve()
    files: list[LegacyCompanyFile] = []
    excluded_symlinks = 0
    for candidate in sorted(root.rglob("*"), key=lambda item: item.as_posix()):
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
        content = candidate.read_bytes()
        files.append(
            LegacyCompanyFile(
                relative_path=relative,
                absolute_path=candidate,
                size_bytes=len(content),
                sha256=_sha256(content),
            )
        )
    return LegacyCompanyFilesSnapshot(files=tuple(files), excluded_symlink_count=excluded_symlinks)


def _write_zip_entry(bundle: zipfile.ZipFile, name: str, content: bytes) -> None:
    info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
    info.compress_type = zipfile.ZIP_DEFLATED
    info.external_attr = 0o100600 << 16
    bundle.writestr(info, content)


def build_legacy_company_files_export(
    company_dir: Path,
    *,
    tenant_id: str,
) -> LegacyCompanyFilesExport:
    """Create a deterministic evidence archive without mutating source files."""

    snapshot = scan_legacy_company_files(company_dir)
    file_payloads: list[tuple[LegacyCompanyFile, bytes]] = []
    for item in snapshot.files:
        try:
            content = item.absolute_path.read_bytes()
        except OSError as exc:
            raise LegacyCompanyFilesChangedError(
                f"legacy file disappeared during export: {item.relative_path}"
            ) from exc
        if len(content) != item.size_bytes or _sha256(content) != item.sha256:
            raise LegacyCompanyFilesChangedError(f"legacy file changed during export: {item.relative_path}")
        file_payloads.append((item, content))

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
    output = tempfile.SpooledTemporaryFile(max_size=8 * 1024 * 1024, mode="w+b")
    try:
        with zipfile.ZipFile(output, "w") as bundle:
            for item, content in file_payloads:
                _write_zip_entry(bundle, f"legacy_company_files/{item.relative_path}", content)
            _write_zip_entry(
                bundle,
                "manifest.json",
                (json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8"),
            )
        size_bytes = output.tell()
        output.seek(0)
    except Exception:
        output.close()
        raise
    return LegacyCompanyFilesExport(
        stream=output,
        size_bytes=size_bytes,
        filename=f"hive-legacy-company-files-{tenant_id}.zip",
        snapshot=snapshot,
    )
