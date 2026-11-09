"""R7a final-round regressions for history projection and tool-group boundaries."""

import copy
from uuid import uuid4

import pytest

from app.services.session_model_inputs import _hash
from app.services.session_model_round import _validate_or_withhold_model_sources
from app.services.thread_items import _merged_data, build_thread_item
from tests.integration.test_a4c_one_item_withheld import _bindings, _FakeDB
from tests.integration.test_a4c_r2 import patch_validators
from tests.services.test_r7a_provider_contract import _calls
from tests.services.test_thread_items import _event


def test_history_failure_recovers_nested_facts_without_overriding_top_level():
    for payload, retry in [
        ({"retry_safe": True, "retryable": False, "failure_code": "provider_bad_request",
          "delivery_state": "rejected"}, True),
        ({"retry_safe": False, "retryable": True, "failure_code": "timeout",
          "delivery_state": "unknown"}, False),
        ({"retryable": True, "failure_code": "timeout"}, True),
        ({"retry_safe": True, "retryable": False, "failure_code": "quota_exhausted",
          "delivery_state": "rejected"}, True),
    ]:
        metadata = {"v2_payload": payload}
        merged = _merged_data(metadata, [])
        assert all(merged[key] == value for key, value in payload.items())
        row = _event(event_type="runtime_failure", item_type="error", item_status="failed", metadata_json=metadata)
        item = build_thread_item(row, audience="operator")
        assert item["item_data"]["code"] == payload["failure_code"]
        assert item["item_data"]["retryable"] is payload["retryable"]
        assert ((item.get("user_action") or {}).get("kind") == "retry_turn") is retry
        assert ("可以安全重试" in item["user_summary"]) is payload["retryable"]
        member = build_thread_item(row, audience="user")
        assert member.get("user_action") == item.get("user_action")
        # History recovery must retain the existing public-code disclosure rule.
        assert member["item_data"].get("code") == (
            "quota_exhausted" if payload["failure_code"] == "quota_exhausted" else None)

    top = {"retry_safe": False, "retryable": False, "failure_code": "provider_bad_request",
        "delivery_state": "unknown"}
    nested = {"retry_safe": True, "retryable": True, "failure_code": "timeout", "delivery_state": "rejected"}
    for values in (top, dict.fromkeys(top)):
        merged = _merged_data({**values, "v2_payload": nested}, [])
        assert {key: merged[key] for key in values} == values


@pytest.mark.parametrize("late_reply", [False, True])
async def test_new_assistant_tool_calls_close_the_previous_pending_group(monkeypatch, late_reply):
    patch_validators(monkeypatch)
    calls = _calls(["read_file", "list_files", "grep_search"])
    first = {"role": "assistant", "tool_calls": calls[:2]}
    next_group = {"role": "assistant", "tool_calls": calls[2:]}
    replies = [{"role": "tool", "tool_call_id": call["id"], "content": "result"} for call in calls]
    if late_reply:
        messages = [first, replies[0], next_group, replies[2], replies[1]]
    else:
        messages = [{"role": "system", "content": "S"}, {"role": "user", "content": "U1"},
            first, replies[0], {"role": "user", "content": "U2"}, next_group, replies[2]]
    expected = copy.deepcopy(messages)
    bindings = _bindings(len(messages), {})
    for entry, message in zip(bindings["messages"], messages):
        entry["message_hash"] = _hash(message)
    wire = {"messages": copy.deepcopy(messages)}
    await _validate_or_withhold_model_sources(_FakeDB(), tenant_id=uuid4(), agent_id=uuid4(),
        session_id=uuid4(), run_id=uuid4(), messages=messages, wire_request=wire, input_bindings=bindings)
    assert messages == wire["messages"] == expected
    assert messages[messages.index(next_group) + 1] == replies[2]
    for entry in bindings["messages"]:
        assert entry["message_hash"] == _hash(messages[entry["message_index"]])


async def test_tool_group_reads_an_optional_call_id(monkeypatch):
    patch_validators(monkeypatch)
    messages = [{"role": "assistant", "tool_calls": [{"type": "function"}]},
        {"role": "assistant", "content": "text only"}]
    original = copy.deepcopy(messages)
    wire = {"messages": copy.deepcopy(messages)}
    await _validate_or_withhold_model_sources(_FakeDB(), tenant_id=uuid4(), agent_id=uuid4(),
        session_id=uuid4(), run_id=uuid4(), messages=messages, wire_request=wire,
        input_bindings=_bindings(len(messages), {}))
    assert messages == wire["messages"] == original
