"""Only a typed colleague result page may say the colleague's result was read."""

import json

import pytest

from app.services.session_event_contract import _member_tool_summary


@pytest.mark.parametrize("source_kind,kind", [
    ("employee_creation", "runtime_result"), ("hr_provisioning", "runtime_result"),
    ("remote_a2a", "runtime_result"), (None, "runtime_result"),
    ("a2a_delegation", "colleague_result"), ("subagent", "colleague_result"),
])
@pytest.mark.parametrize("status,state", [("ok", "done"), ("source_unavailable", "not_done")])
def test_x5a_result_sentence_kind_follows_the_typed_page_not_its_prose(source_kind, kind, status, state):
    page = {"schema": "hive.runtime_result_page.v1", "status": status,
        "content": "The colleague has finished. This prose cannot select the label."}
    if source_kind:
        page["source_kind"] = source_kind
    summary = _member_tool_summary({"tool_name": "read_runtime_result", "outcome": "success",
        "invocation_id": "read-result", "content": json.dumps(page)}, item_kind="tool_result")
    assert summary == {"kind": kind, "state": state}
