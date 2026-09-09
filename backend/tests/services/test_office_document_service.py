from __future__ import annotations

import json
from pathlib import Path

import pytest


class _PreviewAdapter:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def version(self):
        return "1.0.88"

    def run_view(self, path, *, mode, page=None, cwd=None):
        self.calls.append({"path": Path(path), "mode": mode, "page": page, "cwd": Path(cwd)})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        if callable(response):
            return response(Path(path), mode)
        return response


class _BatchAdapter:
    """Records the real OfficeCLI batch invocation instead of pretending flags."""

    def __init__(self, response=None):
        self.response = response or {"success": True, "data": {"applied": 1}}
        self.calls = []

    def run_batch(self, path, *, input_file, stop_on_error=True, cwd=None):
        self.calls.append(
            {
                "path": Path(path),
                "input_file": Path(input_file),
                "stop_on_error": stop_on_error,
                "cwd": Path(cwd),
                "commands": json.loads(Path(input_file).read_text(encoding="utf-8")),
            }
        )
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


def test_office_document_service_rejects_path_escape(tmp_path):
    from app.services.office_document_service import OfficeDocumentPathError, OfficeDocumentService

    service = OfficeDocumentService(tmp_path)

    with pytest.raises(OfficeDocumentPathError):
        service.resolve_document_path("../outside.docx")

    with pytest.raises(OfficeDocumentPathError):
        service.resolve_document_path("/tmp/outside.docx")


def test_xlsx_creation_uses_native_template_and_atomic_publication(tmp_path):
    from app.services.office_document_service import OfficeDocumentService
    from app.services.officecli_adapter import OfficeCLIAdapter
    from types import SimpleNamespace

    calls = []

    def runner(argv, **kwargs):
        calls.append(argv)
        temporary = Path(argv[2])
        assert temporary.parent == tmp_path
        assert temporary.name != "book.xlsx"
        assert not (tmp_path / "book.xlsx").exists()
        temporary.write_bytes(b"native-template")
        return SimpleNamespace(returncode=0, stdout='{"success":true}', stderr="")

    service = OfficeDocumentService(tmp_path, adapter=OfficeCLIAdapter(binary="test-officecli", runner=runner))
    service.create_document("book.xlsx", kind="xlsx")

    assert len(calls) == 1
    assert calls[0][0:2] == ["test-officecli", "create"]
    assert calls[0][3:] == ["--force", "--json"]
    assert (tmp_path / "book.xlsx").read_bytes() == b"native-template"


def test_office_document_service_atomic_save_creates_revision_and_manifest(tmp_path):
    from app.services.office_document_service import OfficeDocumentService

    service = OfficeDocumentService(tmp_path)
    target = tmp_path / "workspace" / "demo.docx"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old-version")

    result = service.atomic_save_bytes("workspace/demo.docx", b"new-version", reason="callback-save")

    assert target.read_bytes() == b"new-version"
    assert result["path"] == "workspace/demo.docx"
    assert result["version"] == 1

    manifest_path = service.manifest_path("workspace/demo.docx")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["path"] == "workspace/demo.docx"
    assert manifest["current_version"] == 1
    assert manifest["revisions"][0]["reason"] == "callback-save"

    revision_path = manifest_path.parent / "revisions" / manifest["revisions"][0]["file"]
    assert revision_path.read_bytes() == b"old-version"


def test_office_document_service_removes_legacy_editor_metadata_without_blocking_writes(tmp_path):
    from app.services.office_document_service import OfficeDocumentService

    service = OfficeDocumentService(tmp_path)
    target = tmp_path / "workspace" / "demo.docx"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"old-version")
    manifest_path = service.manifest_path("workspace/demo.docx")
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "path": "workspace/demo.docx",
                "kind": "docx",
                "current_version": 0,
                "active_editor_session": {"session_id": "legacy-session", "user_id": "legacy-user"},
                "revisions": [],
                "operations": [],
            }
        ),
        encoding="utf-8",
    )

    service.atomic_save_bytes("workspace/demo.docx", b"new-version", reason="agent-apply")

    assert target.read_bytes() == b"new-version"
    saved_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert "active_editor_session" not in saved_manifest


def test_office_document_service_apply_uses_real_batch_input_contract_in_place(tmp_path):
    from app.services.office_document_service import OfficeDocumentService

    target = tmp_path / "workspace" / "demo.docx"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"docx-source")
    adapter = _BatchAdapter()
    service = OfficeDocumentService(tmp_path, adapter=adapter)

    operations = [{"command": "set", "path": "/body/p[1]", "props": {"text": "Title"}}]
    payload = service.run_apply("workspace/demo.docx", operations=operations)

    assert payload == {"success": True, "data": {"applied": 1}}
    assert len(adapter.calls) == 1
    call = adapter.calls[0]
    assert call["path"] == target
    assert call["cwd"] == tmp_path
    assert call["stop_on_error"] is True
    # The --input file must be the bare JSON array of command objects, not a wrapper.
    assert call["commands"] == operations
    assert call["input_file"].parent == service.manifest_path("workspace/demo.docx").parent / "operations"

    manifest = json.loads(service.manifest_path("workspace/demo.docx").read_text(encoding="utf-8"))
    assert manifest["operations"][0]["operation_count"] == 1
    assert manifest["operations"][0]["output_path"] is None


def test_office_document_service_apply_with_output_path_preserves_source_document(tmp_path):
    from app.services.office_document_service import OfficeDocumentService

    source = tmp_path / "workspace" / "demo.docx"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"docx-source")
    adapter = _BatchAdapter()
    service = OfficeDocumentService(tmp_path, adapter=adapter)

    def apply_side_effect(path, *, input_file, stop_on_error=True, cwd=None):
        adapter.calls.append(
            {"path": Path(path), "input_file": Path(input_file), "stop_on_error": stop_on_error, "cwd": Path(cwd)}
        )
        path.write_bytes(b"docx-updated")
        return {"success": True, "data": {"applied": 1}}

    adapter.run_batch = apply_side_effect
    payload = service.run_apply(
        "workspace/demo.docx",
        operations=[{"command": "add", "parent": "/body", "type": "paragraph", "props": {"text": "New"}}],
        output_path="workspace/out/demo-v2.docx",
    )

    assert payload == {"success": True, "data": {"applied": 1}}
    assert source.read_bytes() == b"docx-source"
    output = tmp_path / "workspace" / "out" / "demo-v2.docx"
    assert output.read_bytes() == b"docx-updated"
    # The batch must have run on a temporary copy inside the output directory, not the source.
    batched_paths = [call["path"] for call in adapter.calls]
    assert all(path != source for path in batched_paths)
    assert {path.suffix for path in batched_paths} == {".docx"}


def test_office_document_service_apply_failure_leaves_source_and_output_untouched(tmp_path):
    from app.services.office_document_service import OfficeDocumentService
    from app.services.officecli_adapter import OfficeCLIExecutionError

    source = tmp_path / "workspace" / "demo.docx"
    source.parent.mkdir(parents=True)
    source.write_bytes(b"docx-source")
    output = tmp_path / "workspace" / "out" / "demo-v2.docx"

    def failing_run_batch(path, *, input_file, stop_on_error=True, cwd=None):
        path.write_bytes(b"partially-applied")
        raise OfficeCLIExecutionError(
            command="batch",
            returncode=2,
            stderr="stop-on-error: command 1 failed",
        )

    adapter = _BatchAdapter()
    adapter.run_batch = failing_run_batch
    service = OfficeDocumentService(tmp_path, adapter=adapter)

    with pytest.raises(OfficeCLIExecutionError):
        service.run_apply(
            "workspace/demo.docx",
            operations=[{"command": "set", "path": "/", "props": {}}],
            output_path="workspace/out/demo-v2.docx",
        )

    assert source.read_bytes() == b"docx-source"
    assert not output.exists()
    # No temporary copy is left behind in the output directory.
    assert list((tmp_path / "workspace" / "out").iterdir()) == []


def test_office_document_service_apply_rejects_non_batch_schema_operations_before_cli(tmp_path):
    from app.services.office_document_service import OfficeDocumentService

    target = tmp_path / "workspace" / "demo.docx"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"docx-source")
    adapter = _BatchAdapter()
    service = OfficeDocumentService(tmp_path, adapter=adapter)

    for invalid in ([], [{"op": "replace_text"}], [{"command": " "}], ["not-a-dict"], [42]):
        with pytest.raises(ValueError):
            service.run_apply("workspace/demo.docx", operations=invalid)

    assert adapter.calls == []
    assert (
        service.manifest_path("workspace/demo.docx").exists() is False
        or json.loads(service.manifest_path("workspace/demo.docx").read_text(encoding="utf-8"))["operations"] == []
    )


def test_office_preview_renders_hardened_html_and_reuses_hash_cache(tmp_path):
    from app.services.office_document_service import OfficeDocumentService

    target = tmp_path / "workspace" / "demo.docx"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"docx-source")
    adapter = _PreviewAdapter(
        [{"success": True, "data": "<!DOCTYPE html><html><head><title>Demo</title></head><body>Hello</body></html>"}]
    )
    service = OfficeDocumentService(tmp_path, adapter=adapter, preview_max_bytes=1024 * 1024)

    first = service.render_preview("workspace/demo.docx")
    second = service.render_preview("workspace/demo.docx")

    assert first.preview_mode == "html"
    assert first.cache_hit is False
    assert second.cache_hit is True
    assert first.source_sha256 == second.source_sha256
    assert first.renderer_version == "1.0.88"
    assert 'http-equiv="Content-Security-Policy"' in first.html
    assert "default-src &#x27;none&#x27;" in first.html
    assert len(adapter.calls) == 1
    manifest = json.loads(
        (service.manifest_path("workspace/demo.docx").parent / "preview" / "manifest.json").read_text()
    )
    assert manifest["source_sha256"] == first.source_sha256
    assert manifest["preview_mode"] == "html"
    # The old JSON-only adapter cached a fallback for the same binary/source.
    # Changing the integration contract must invalidate that legacy cache.
    manifest.pop("preview_contract_version")
    manifest["preview_mode"] = "text_fallback"
    cache_dir = service.manifest_path("workspace/demo.docx").parent / "preview"
    (cache_dir / "manifest.json").write_text(json.dumps(manifest))
    assert service._load_preview_cache(
        cache_dir, source_sha256=first.source_sha256, renderer_version=first.renderer_version
    ) is None


def test_office_preview_falls_back_to_escaped_text_on_html_renderer_failure(tmp_path):
    from app.services.office_document_service import OfficeDocumentService
    from app.services.officecli_adapter import OfficeCLIExecutionError

    target = tmp_path / "workspace" / "demo.docx"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"docx-source")
    adapter = _PreviewAdapter(
        [
            OfficeCLIExecutionError(command="view", returncode=2, stderr="html renderer unavailable"),
            {
                "success": True,
                "data": {
                    "elements": [
                        {"path": "/body/0", "text": "<script>alert('no')</script>", "type": "paragraph"},
                        {"path": "/body/1", "text": "Visible text", "type": "paragraph"},
                    ],
                    "totalElements": 2,
                },
            },
        ]
    )
    service = OfficeDocumentService(tmp_path, adapter=adapter, preview_max_bytes=1024 * 1024)

    result = service.render_preview("workspace/demo.docx")

    assert result.preview_mode == "text_fallback"
    assert "Text-only preview" in result.html
    assert "&lt;script&gt;" in result.html
    assert "<script>alert" not in result.html
    assert [call["mode"] for call in adapter.calls] == ["html", "text"]


@pytest.mark.parametrize(
    ("office_format", "payload", "expected_fragments"),
    [
        (
            "docx",
            {
                "success": True,
                "data": {
                    "elements": [
                        {"path": "/body/0", "text": "First paragraph", "type": "paragraph"},
                        {"path": "/body/1", "text": "Last paragraph sentinel", "type": "paragraph"},
                    ],
                    "totalElements": 2,
                },
            },
            ("First paragraph", "Last paragraph sentinel"),
        ),
        (
            "xlsx",
            {
                "success": True,
                "data": {
                    "sheets": [
                        {
                            "name": "Summary",
                            "rows": [{"row": 1, "cells": {"A1": "Revenue", "B1": 42}}],
                        },
                        {
                            "name": "Last sheet sentinel",
                            "rows": [{"row": 9, "cells": {"Z9": True}}],
                        },
                    ]
                },
            },
            ("Sheet: Summary", "A1: Revenue", "B1: 42", "Last sheet sentinel", "Z9: true"),
        ),
        (
            "pptx",
            {
                "success": True,
                "data": {
                    "slides": [
                        {"index": 0, "path": "/slides/0", "texts": ["Opening"]},
                        {"index": 7, "path": "/slides/7", "texts": ["Last slide sentinel"]},
                    ],
                    "totalSlides": 2,
                },
            },
            ("Slide 1", "Opening", "Slide 8", "Last slide sentinel"),
        ),
    ],
)
def test_officecli_structured_text_payload_preserves_all_format_content(
    office_format,
    payload,
    expected_fragments,
):
    from app.services.office_document_service import extract_officecli_text_payload

    rendered = extract_officecli_text_payload(payload, office_format=office_format)

    for fragment in expected_fragments:
        assert fragment in rendered


def test_officecli_docx_total_elements_may_include_nodes_nested_in_rendered_top_level_blocks():
    from app.services.office_document_service import extract_officecli_text_payload

    payload = {
        "success": True,
        "data": {
            "elements": [
                {"path": "/body/0", "text": "Heading", "type": "paragraph"},
                {"path": "/body/1", "text": "Paragraph", "type": "paragraph"},
                {"path": "/body/2", "text": "Table cell content", "type": "table"},
            ],
            "totalElements": 4,
        },
    }

    rendered = extract_officecli_text_payload(payload, office_format="docx")

    assert "Heading" in rendered
    assert "Paragraph" in rendered
    assert "Table cell content" in rendered


@pytest.mark.parametrize(
    ("office_format", "payload"),
    [
        ("docx", {"success": True, "data": {"elements": [{"text": 7}], "totalElements": 1}}),
        (
            "docx",
            {
                "success": True,
                "data": {
                    "elements": [{"text": "one"}, {"text": "two"}],
                    "totalElements": 1,
                },
            },
        ),
        ("xlsx", {"success": True, "data": {"sheets": [{"name": "Sheet", "rows": "not-a-list"}]}}),
        (
            "pptx",
            {"success": True, "data": {"slides": [{"index": 0, "texts": ["only one"]}], "totalSlides": 2}},
        ),
    ],
)
def test_officecli_structured_text_payload_rejects_malformed_or_incomplete_contract(office_format, payload):
    from app.services.office_document_service import OfficePreviewMalformedError, extract_officecli_text_payload

    with pytest.raises(OfficePreviewMalformedError):
        extract_officecli_text_payload(payload, office_format=office_format)


def test_office_preview_retries_once_then_fails_when_source_keeps_changing(tmp_path):
    from app.services.office_document_service import OfficeDocumentService, OfficePreviewSourceChangedError

    target = tmp_path / "workspace" / "demo.docx"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"version-0")

    def changing_response(path, _mode):
        path.write_bytes(path.read_bytes() + b"-changed")
        return {"success": True, "data": "<!DOCTYPE html><html><head></head><body>Preview</body></html>"}

    adapter = _PreviewAdapter([changing_response, changing_response])
    service = OfficeDocumentService(tmp_path, adapter=adapter, preview_max_bytes=1024 * 1024)

    with pytest.raises(OfficePreviewSourceChangedError) as exc:
        service.render_preview("workspace/demo.docx")

    assert exc.value.error_code == "office_preview_source_changed"
    assert len(adapter.calls) == 2


def test_office_preview_rejects_oversized_html_without_truncating(tmp_path):
    from app.services.office_document_service import OfficeDocumentService, OfficePreviewTooLargeError

    target = tmp_path / "workspace" / "demo.docx"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"docx-source")
    adapter = _PreviewAdapter(
        [{"success": True, "data": "<!DOCTYPE html><html><head></head><body>oversized</body></html>"}]
    )
    service = OfficeDocumentService(tmp_path, adapter=adapter, preview_max_bytes=32)

    with pytest.raises(OfficePreviewTooLargeError) as exc:
        service.render_preview("workspace/demo.docx")

    assert exc.value.error_code == "office_preview_too_large"


def test_office_preview_returns_typed_unavailable_when_html_and_text_infrastructure_fail(tmp_path):
    from app.services.office_document_service import OfficeDocumentService, OfficePreviewUnavailableError
    from app.services.officecli_adapter import OfficeCLIExecutionError, OfficeCLITimeoutError

    target = tmp_path / "workspace" / "demo.docx"
    target.parent.mkdir(parents=True)
    target.write_bytes(b"docx-source")
    adapter = _PreviewAdapter(
        [
            OfficeCLITimeoutError(command="view", timeout_seconds=3),
            OfficeCLIExecutionError(command="view", returncode=127, stderr="binary is unavailable"),
        ]
    )
    service = OfficeDocumentService(tmp_path, adapter=adapter, preview_max_bytes=1024 * 1024)

    with pytest.raises(OfficePreviewUnavailableError) as exc:
        service.render_preview("workspace/demo.docx")

    assert exc.value.error_code == "office_preview_unavailable"
    assert [call["mode"] for call in adapter.calls] == ["html", "text"]
