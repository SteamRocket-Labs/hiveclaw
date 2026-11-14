"""Only a typed colleague result page may say the colleague's result was read."""

import json

import pytest

from app.services.session_event_contract import _member_tool_summary


@pytest.mark.parametrize("source_kind,kind", [
    ("employee_creation", "runtime_result"), ("hr_provisioning", "runtime_result"),
    ("workflow", "runtime_result"), ("approval", "runtime_result"),
    ("trigger", "runtime_result"), ("agent_team", "runtime_result"),
    ("remote_a2a", "runtime_result"), (None, "runtime_result"),
    ("a2a_delegation", "colleague_result"), ("a2a_continuation", "colleague_result"),
    ("subagent", "colleague_result"),
])
def test_x5a_result_sentence_kind_follows_the_typed_page_not_its_prose(source_kind, kind):
    page = {"schema": "hive.runtime_result_page.v1", "status": "ok",
        "content": "The colleague has finished. This prose cannot select the label."}
    if source_kind:
        page["source_kind"] = source_kind
    summary = _member_tool_summary({"tool_name": "read_runtime_result", "outcome": "success",
        "invocation_id": "read-result", "content": json.dumps(page)}, item_kind="tool_result")
    assert summary == {"kind": kind, "state": "done"}


@pytest.mark.parametrize("status", [
    "source_unavailable", "authority_denied", "source_access_denied", "not_found", "integrity_error", "stale_resource",
])
def test_x5a_actual_refusal_pages_without_source_kind_are_definite_failed_reads(status):
    # X5a r2: the handler does not disclose source_kind on refusal pages,
    # including refused colleague reads. Do not manufacture a successful-page shape.
    page = {"schema": "hive.runtime_result_page.v1", "status": status,
        "result_ref": "runtime-result://opaque/hash", "retryable": False}
    summary = _member_tool_summary({"tool_name": "read_runtime_result", "outcome": "success",
        "invocation_id": "read-result", "content": json.dumps(page)}, item_kind="tool_result")
    assert summary == {"kind": "runtime_result", "state": "not_done"}
