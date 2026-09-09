from __future__ import annotations

import os
import shutil

import pytest


def test_native_xlsx_template_passes_installed_validator(tmp_path) -> None:
    from xml.etree import ElementTree
    from zipfile import ZipFile

    from app.services.office_document_service import OfficeDocumentService
    from app.services.officecli_adapter import OfficeCLIAdapter

    binary = os.environ.get("OFFICECLI_BIN") or shutil.which("officecli")
    if not binary:
        pytest.skip("OfficeCLI binary is unavailable locally")
    adapter = OfficeCLIAdapter(binary=binary)
    service = OfficeDocumentService(tmp_path, adapter=adapter)
    service.create_document("native.xlsx", kind="xlsx")
    service.run_view("native.xlsx", mode="text")
    service.run_apply(
        "native.xlsx",
        operations=[{"command": "set", "path": "/Sheet1/B6", "props": {"value": "=SUM(17,12)"}}],
    )
    # Read the immediate disk delivery, not the CLI resident's in-memory view.
    with ZipFile(tmp_path / "native.xlsx") as document:
        sheet = ElementTree.fromstring(document.read("xl/worksheets/sheet1.xml"))
    cell = sheet.find('.//{*}c[@r="B6"]')
    assert cell is not None
    assert cell.findtext("{*}f") == "SUM(17,12)"
    assert cell.findtext("{*}v") == "29"
    assert adapter.run("validate", tmp_path / "native.xlsx")["success"] is True


def test_release_officecli_binary_renders_docx_xlsx_and_pptx_html_and_text() -> None:
    from app.scripts.verify_officecli_binary_contract import verify_officecli_binary_contract

    binary = os.environ.get("OFFICECLI_BIN") or shutil.which("officecli")
    if not binary:
        pytest.skip(
            "OfficeCLI binary is unavailable locally; Railway production runs the same verifier before retirement"
        )

    report = verify_officecli_binary_contract(binary=binary)

    assert report["status"] == "ok"
    assert report["version"]
    assert set(report["formats"]) == {"docx", "xlsx", "pptx"}
    for result in report["formats"].values():
        assert result["html"] is True
        assert result["text"] is True
        assert result["service_preview_mode"] == "html"
        assert result["csp"] is True
