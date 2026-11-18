"""X9b: distinguish the native no-body colleague notice in the member view."""
import copy
import json
from uuid import uuid4

import pytest

from app.services.runtime_display_status import delegation_failure_result_page
from tests.services.test_member_tool_summary import _native_row, _operator_payload, _user_payload


@pytest.mark.parametrize("status", ["failed", "killed"])
def test_colleague_end_page_without_body_uses_existing_found_flag(status):
    page = delegation_failure_result_page(task_id=str(uuid4()), status=status,
        metadata={"error_code": "delegation_timeout"})
    assert page is not None
    row = _native_row("read_runtime_result", json.dumps(page))
    before = copy.deepcopy(row.metadata_json)
    operator = _operator_payload(row)
    member = _user_payload(row)
    assert member["member_summary"] == {"kind": "runtime_result", "state": "done", "found": False}
    assert member["result"] == member["content"] == ""
    assert _operator_payload(row) == operator
    assert row.metadata_json == before


def test_running_colleague_notice_does_not_claim_the_colleague_ended():
    page = delegation_failure_result_page(task_id=str(uuid4()), status="failed",
        metadata={"error_code": "identical_running_task_read"})
    assert page is not None
    member = _user_payload(_native_row("read_runtime_result", json.dumps(page)))
    assert member["member_summary"].get("found") is not False
