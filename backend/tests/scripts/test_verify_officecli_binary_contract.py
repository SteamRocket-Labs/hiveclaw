from __future__ import annotations

from pathlib import Path

import pytest


def test_contract_result_requires_html_text_csp_and_html_service_mode() -> None:
    from app.scripts.verify_officecli_binary_contract import _contract_format_ok

    valid = {
        "html": True,
        "text": True,
        "service_preview_mode": "html",
        "csp": True,
        "output_bytes": 128,
    }

    assert _contract_format_ok(valid) is True
    for field in ("html", "text", "csp"):
        invalid = {**valid, field: False}
        assert _contract_format_ok(invalid) is False
    assert _contract_format_ok({**valid, "service_preview_mode": "text_fallback"}) is False


def test_preview_csp_detection_matches_html_escaped_meta_value() -> None:
    from app.scripts.verify_officecli_binary_contract import _preview_contains_csp
    from app.services.office_document_service import OfficeDocumentService

    preview = OfficeDocumentService._harden_html("<html><head></head><body>content</body></html>")

    assert _preview_contains_csp(preview) is True
    assert _preview_contains_csp("<html><head></head><body>content</body></html>") is False


def test_verifier_never_reports_ok_when_any_format_contract_is_invalid(monkeypatch) -> None:
    from app.scripts import verify_officecli_binary_contract as verifier

    class FakeAdapter:
        def __init__(self, **_kwargs):
            pass

        def version(self):
            return "contract-test"

        def run_view(self, path, *, mode, cwd=None, page=None):
            del cwd, page
            office_format = Path(path).suffix.removeprefix(".")
            if mode == "html":
                return {"success": True, "data": "<html><head></head><body>preview</body></html>"}
            if office_format == "docx":
                return {
                    "success": True,
                    "data": {"elements": [{"text": "docx"}], "totalElements": 1},
                }
            if office_format == "pptx":
                return {
                    "success": True,
                    "data": {"slides": [{"index": 0, "texts": ["pptx"]}], "totalSlides": 1},
                }
            return {"success": True, "data": {"sheets": "invalid"}}

    monkeypatch.setattr(verifier, "OfficeCLIAdapter", FakeAdapter)

    with pytest.raises(RuntimeError, match="xlsx"):
        verifier.verify_officecli_binary_contract(binary="fake-officecli")
