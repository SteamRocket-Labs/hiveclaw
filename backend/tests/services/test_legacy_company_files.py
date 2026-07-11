from __future__ import annotations

import io
import json
import zipfile


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
