from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
from pathlib import Path
import tracemalloc
import zipfile

import pytest


def test_legacy_company_file_scan_excludes_canonical_context_and_symlinks(tmp_path) -> None:
    from app.services.legacy_company_files import scan_legacy_company_files

    (tmp_path / "company_profile.md").write_text("# Company\n", encoding="utf-8")
    (tmp_path / "org_structure.md").write_text("# Org\n", encoding="utf-8")
    (tmp_path / "knowledge_base").mkdir()
    (tmp_path / "knowledge_base" / "policy.md").write_text("legacy policy\n", encoding="utf-8")
    (tmp_path / "old-upload.pdf").write_bytes(b"legacy-pdf")
    outside = tmp_path.parent / "outside-secret.txt"
    outside.write_text("secret\n", encoding="utf-8")
    (tmp_path / "unsafe-link").symlink_to(outside)

    snapshot = scan_legacy_company_files(tmp_path)

    assert [item.relative_path for item in snapshot.files] == [
        "knowledge_base/policy.md",
        "old-upload.pdf",
    ]
    assert snapshot.file_count == 2
    assert snapshot.total_bytes == len(b"legacy policy\n") + len(b"legacy-pdf")
    assert snapshot.excluded_symlink_count == 1


def test_legacy_company_file_scan_rejects_a_symlinked_quarantine_root(tmp_path) -> None:
    from app.services import legacy_company_files

    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "host-secret.txt").write_text("secret\n", encoding="utf-8")
    quarantine_root = tmp_path / "enterprise_info_tenant"
    quarantine_root.symlink_to(outside, target_is_directory=True)

    with pytest.raises(legacy_company_files.LegacyCompanyFilesChangedError):
        legacy_company_files.scan_legacy_company_files(quarantine_root)


def test_legacy_company_file_scan_does_not_treat_root_io_failure_as_empty(monkeypatch, tmp_path) -> None:
    from app.services import legacy_company_files

    quarantine_root = tmp_path / "enterprise_info_tenant"
    quarantine_root.mkdir()
    original_lstat = Path.lstat

    def unavailable_lstat(self: Path, *_args, **_kwargs):
        if self == quarantine_root:
            raise PermissionError("private quarantine root")
        return original_lstat(self, *_args, **_kwargs)

    monkeypatch.setattr(Path, "lstat", unavailable_lstat)

    with pytest.raises(legacy_company_files.LegacyCompanyFilesUnavailableError) as exc:
        legacy_company_files.scan_legacy_company_files(quarantine_root)

    assert "private quarantine root" not in str(exc.value)


def test_legacy_company_file_export_is_read_only_and_contains_evidence_manifest(tmp_path) -> None:
    from app.services.legacy_company_files import build_legacy_company_files_export

    (tmp_path / "company_profile.md").write_text("# Company\n", encoding="utf-8")
    (tmp_path / "knowledge_base").mkdir()
    original = tmp_path / "knowledge_base" / "policy.md"
    original.write_text("legacy policy\n", encoding="utf-8")

    archive = build_legacy_company_files_export(tmp_path, tenant_id="tenant-1")
    content = archive.stream.read()
    archive.stream.close()

    assert original.read_text(encoding="utf-8") == "legacy policy\n"
    assert archive.size_bytes == len(content)
    with zipfile.ZipFile(io.BytesIO(content), "r") as bundle:
        assert bundle.namelist() == [
            "legacy_company_files/knowledge_base/policy.md",
            "manifest.json",
        ]
        assert bundle.read("legacy_company_files/knowledge_base/policy.md") == b"legacy policy\n"
        manifest = json.loads(bundle.read("manifest.json"))

    assert manifest["schema"] == "hive.legacy_company_files_export.v1"
    assert manifest["tenant_id"] == "tenant-1"
    assert manifest["retired_surface"] == "/enterprise/knowledge-base"
    assert manifest["files"][0]["relative_path"] == "knowledge_base/policy.md"
    assert manifest["files"][0]["sha256"]


def test_legacy_company_file_read_requires_exact_snapshot_and_never_accepts_traversal(tmp_path) -> None:
    from app.services.legacy_company_files import (
        LegacyCompanyFilesChangedError,
        read_legacy_company_file,
    )

    source = tmp_path / "knowledge_base" / "policy.md"
    source.parent.mkdir()
    source.write_text("legacy policy\n", encoding="utf-8")
    expected_hash = hashlib.sha256(source.read_bytes()).hexdigest()

    result = read_legacy_company_file(
        tmp_path,
        relative_path="knowledge_base/policy.md",
        expected_sha256=expected_hash,
    )

    assert result.item.relative_path == "knowledge_base/policy.md"
    assert result.data == b"legacy policy\n"
    with pytest.raises(LegacyCompanyFilesChangedError):
        read_legacy_company_file(
            tmp_path,
            relative_path="../outside-secret.txt",
            expected_sha256=expected_hash,
        )
    source.write_text("changed\n", encoding="utf-8")
    with pytest.raises(LegacyCompanyFilesChangedError):
        read_legacy_company_file(
            tmp_path,
            relative_path="knowledge_base/policy.md",
            expected_sha256=expected_hash,
        )


def test_legacy_company_file_export_uses_bounded_memory_for_large_sources(tmp_path) -> None:
    from app.services.legacy_company_files import build_legacy_company_files_export

    source = tmp_path / "large-retired-file.bin"
    with source.open("wb") as stream:
        stream.truncate(12 * 1024 * 1024)

    archive = None
    tracemalloc.start()
    try:
        archive = build_legacy_company_files_export(tmp_path, tenant_id="tenant-large")
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        if archive is not None:
            archive.stream.close()

    assert peak < 8 * 1024 * 1024


def test_legacy_company_file_export_rejects_same_content_source_replacement(monkeypatch, tmp_path) -> None:
    from app.services import legacy_company_files

    source = tmp_path / "retired.md"
    source.write_text("same content\n", encoding="utf-8")
    snapshot = legacy_company_files.scan_legacy_company_files(tmp_path)
    replacement = tmp_path / "replacement.md"
    replacement.write_text("same content\n", encoding="utf-8")

    def replace_after_scan(_company_dir):
        os.replace(replacement, source)
        return snapshot

    monkeypatch.setattr(legacy_company_files, "scan_legacy_company_files", replace_after_scan)

    with pytest.raises(legacy_company_files.LegacyCompanyFilesChangedError):
        legacy_company_files.build_legacy_company_files_export(tmp_path, tenant_id="tenant-race")


def test_legacy_company_file_scan_classifies_io_failure_without_leaking_path(monkeypatch, tmp_path) -> None:
    from app.services import legacy_company_files

    source = tmp_path / "private.md"
    source.write_text("private\n", encoding="utf-8")

    def denied_path_open(self: Path, *_args, **_kwargs):
        if self == source:
            raise PermissionError("secret host path must not escape")
        return original_path_open(self, *_args, **_kwargs)

    def denied_os_open(path, *_args, **_kwargs):
        if Path(path) == source:
            raise PermissionError("secret host path must not escape")
        return original_os_open(path, *_args, **_kwargs)

    original_path_open = Path.open
    original_os_open = os.open
    monkeypatch.setattr(Path, "open", denied_path_open)
    monkeypatch.setattr(os, "open", denied_os_open)

    with pytest.raises(legacy_company_files.LegacyCompanyFilesUnavailableError) as exc:
        legacy_company_files.scan_legacy_company_files(tmp_path)

    assert "secret host path" not in str(exc.value)


def test_legacy_company_file_export_classifies_temporary_storage_failure(monkeypatch, tmp_path) -> None:
    from app.services import legacy_company_files

    (tmp_path / "retired.md").write_text("retired\n", encoding="utf-8")

    def unavailable_tempfile(*_args, **_kwargs):
        raise OSError("private temporary directory detail")

    monkeypatch.setattr(legacy_company_files.tempfile, "TemporaryFile", unavailable_tempfile)

    with pytest.raises(legacy_company_files.LegacyCompanyFilesUnavailableError) as exc:
        legacy_company_files.build_legacy_company_files_export(tmp_path, tenant_id="tenant-temp")

    assert "private temporary directory detail" not in str(exc.value)


def test_legacy_company_file_export_closes_temporary_archive_when_cancelled(monkeypatch, tmp_path) -> None:
    from app.services import legacy_company_files

    (tmp_path / "retired.md").write_text("retired\n", encoding="utf-8")
    output = io.BytesIO()

    monkeypatch.setattr(legacy_company_files.tempfile, "TemporaryFile", lambda **_kwargs: output)

    def cancel_during_copy(_bundle, _item):
        raise asyncio.CancelledError

    monkeypatch.setattr(legacy_company_files, "_copy_verified_file_to_zip", cancel_during_copy)

    with pytest.raises(asyncio.CancelledError):
        legacy_company_files.build_legacy_company_files_export(tmp_path, tenant_id="tenant-cancel")

    assert output.closed is True
