"""X5b: run20's args-absent native rows retain typed failures and safe targets."""

import json

import pytest

from app.services.runtime_tool_timeout_sources import _timeout_text
from app.tools.result_envelope import render_tool_error
from tests.services.test_member_tool_summary_x3c import _native_row, _operator_payload, _user_payload


@pytest.mark.parametrize("tool,kind", [("search_memory", "memory_search"), ("load_memory", "memory_read")])
@pytest.mark.parametrize("legacy_format", [False, True])
def test_memory_timeout_survives_real_member_projection(tool, kind, legacy_format):
    # Run20 step4 rows 3/4: failed invocation, native timeout envelope, no args.
    # Reconstruct the raw runtime envelope: tool-events.json holds only its hash
    # and the already-redacted member payload, not the original result bytes.
    content = _timeout_text(tool, 30, legacy_format=legacy_format, retryable=False)
    row = _native_row(tool, content, outcome="failed")
    before = _operator_payload(row)
    payload = _user_payload(row)
    assert payload["member_summary"] == {"kind": kind, "state": "not_done", "error_class": "timeout"}
    assert payload["content"] == payload["result"] == ""
    assert "<tool_error>" not in json.dumps(payload)
    assert _operator_payload(row) == before
    assert before["content"] == content


@pytest.mark.parametrize("tool,kind", [("search_memory", "memory_search"), ("load_memory", "memory_read")])
def test_memory_refusal_keeps_its_non_timeout_class(tool, kind):
    content = render_tool_error(tool_name=tool, error_class="auth_or_permission", message="timeout is data here",
                                provider="runtime", retryable=False)
    payload = _user_payload(_native_row(tool, content, outcome="denied"))
    assert payload["member_summary"] == {"kind": kind, "state": "not_done", "error_class": "auth_or_permission"}


def test_memory_non_envelope_json_does_not_export_error_class():
    # X5b r2: a result body's arbitrary fields are not an error envelope.
    content = json.dumps({"status": "invalid_arguments", "error_class": "secret text", "message": "x"})
    payload = _user_payload(_native_row("search_memory", content, outcome="success"))
    assert payload["member_summary"] == {"kind": "memory_search", "state": "not_done"}
    assert payload["content"] == payload["result"] == ""
    assert "secret text" not in json.dumps(payload)


def test_memory_empty_result_is_not_a_timeout():
    payload = _user_payload(_native_row("search_memory", json.dumps({"status": "empty", "items": []})))
    assert payload["member_summary"] == {"kind": "memory_search", "state": "done", "found": False}
    prose = _user_payload(_native_row("load_memory", "A remembered note about timeout."))
    assert prose["member_summary"] == {"kind": "memory_read", "state": "done"}


@pytest.mark.parametrize("tool", [
    "web_fetch", "advanced_web_fetch", "anysearch_extract", "exa_fetch", "tavily_extract",
    "firecrawl_fetch", "xcrawl_scrape",
])
@pytest.mark.parametrize("outcome,host", [("success", "example.com"), ("failed", "no-such-host-e2e.invalid")])
def test_all_registered_web_fetch_channels_keep_the_host_without_args(tool, outcome, host):
    # Like run20's native results, the typed target is stored separately from
    # the model-facing page bytes; no URL or page body may reach the member.
    row = _native_row(tool, "PRIVATE_PAGE_BODY", outcome=outcome, member_target={"kind": "web_fetch", "host": host})
    payload = _user_payload(row)
    assert payload["member_summary"] == {
        "kind": "web_fetch", "state": "done" if outcome == "success" else "not_done", "host": host,
    }
    assert "PRIVATE_PAGE_BODY" not in json.dumps(payload)
    assert "member_target" not in payload
