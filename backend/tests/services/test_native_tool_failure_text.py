"""Changing member copy must preserve immutable native no-body observations."""

import json

import pytest

from app.services.content_input_receipts import _native_read_denial_matches
from app.services.session_tool_runtime import _sha256


@pytest.mark.parametrize("producer", ["read_file", "read_document"])
@pytest.mark.parametrize("kind", ["missing", "denied", "control", "source"])
def test_historical_file_refusal_bytes_keep_their_exact_scope(producer, kind):
    from app.services.content_input_receipts import _platform_control_read_unavailable, _workspace_read_denial
    from app.tools.result_envelope import render_tool_error

    path = "workspace/report.md"
    error_class, provider, status, retryable, hint, extra = "auth_or_permission", "workspace", None, False, None, {}
    if kind == "missing":
        observation = {"kind": "filesystem_read_not_found", "path": path}
        error_class, message = "not_found", f"File not found: {path}"
        if producer == "read_file":
            hint = "Check the path or use glob_search/list_files to discover the correct file first."
    elif kind == "denied":
        observation = {"kind": "filesystem_read_denied", "path": path, "boundary": "path"}
        message = "Access denied for this path."
    elif kind == "control":
        path = "memory/control/t2_job_sweep.json"
        observation = {"kind": "platform_control_read_unavailable", "path": path}
        error_class, provider, status = "provider_unavailable", "content_input_receipt", 409
        message = "This platform control file has no independently bound body source."
        hint = "Use read_ledger for canonical work state and memory tools for memory evidence. Reconcile the original control source before reading its body; keep using other verified results."
        extra = {"reason_code": "platform_content_unverified", "outcome": "unavailable"}
    else:
        observation = {"kind": "workspace_source_read_denied", "path": path,
            "http_status": 403, "reason_code": "content_source_access_denied"}
        provider, status = "content_input_receipt", 403
        message = "This file's source receipt cannot authorize model consumption."
        hint = "Restore the scoped source grant or regenerate from current authorized inputs."
        extra = {"reason_code": "content_source_access_denied", "outcome": "denied"}

    old_payload = {"ok": False, "tool_name": producer, "error_class": error_class, "message": message,
        "provider": provider, "http_status": status, "retryable": retryable}
    if hint:
        old_payload["actionable_hint"] = hint
    old_payload.update(extra)
    parts = [f"❌ {message}"]
    if hint:
        parts.append(f"Hint: {hint}")
    parts.append(f"<tool_error>{json.dumps(old_payload, ensure_ascii=False)}</tool_error>")
    if kind == "control":
        current = _platform_control_read_unavailable(producer)
    elif kind == "source":
        current = _workspace_read_denial(producer, 403, "content_source_access_denied")
    else:
        current = render_tool_error(tool_name=producer, error_class=error_class, message=message,
            provider=provider, http_status=status, retryable=retryable, actionable_hint=hint)
    for tool, args in [(producer, {"path": path}), ("fs_read", {"path": path,
        "mode": "text" if producer == "read_file" else "document"})]:
        if kind == "control":
            current = _platform_control_read_unavailable(tool)
        elif kind == "source":
            current = _workspace_read_denial(tool, 403, "content_source_access_denied")
        wire_payload = {**old_payload, "tool_name": tool if kind in {"control", "source"} else producer}
        wire_parts = [*parts[:-1], f"<tool_error>{json.dumps(wire_payload, ensure_ascii=False)}</tool_error>"]
        for content in ("\n\n".join(wire_parts), current):
            assert _native_read_denial_matches(tool, args, observation, content_hash=_sha256(content))
            assert not _native_read_denial_matches(tool, args, observation, content_hash=_sha256(content + "tampered"))
            assert not _native_read_denial_matches(tool, {**args, "path": "workspace/other.md"}, observation,
                content_hash=_sha256(content))
