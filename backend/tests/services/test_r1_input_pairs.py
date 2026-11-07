"""Withholding one round must not remove older rounds that reuse a tool ID."""

import copy
import json

import pytest

from app.services.llm_client import LLMMessage
from app.services.session_model_inputs import _hash, message_snapshot
from app.services.session_model_round import _withhold_model_source


@pytest.mark.parametrize("blocked_role", ["assistant", "tool"])
def test_withholding_pairs_with_the_originating_round_when_call_ids_repeat(blocked_role):
    messages = []
    for index in range(6):
        messages.extend([
            LLMMessage(role="user", content=f"My request {index}"),
            LLMMessage(role="assistant", content=f"My plan {index}", tool_calls=[{
                "id": "reused-call", "type": "function", "function": {
                    "name": "web_search", "arguments": json.dumps({"query": f"query {index}"}),
                },
            }]),
            LLMMessage(role="tool", tool_call_id="reused-call",
                content="FOREIGN_REPLY_BYTES" if index == 5 else f"My result {index}"),
        ])
    wire = {"messages": [message_snapshot(message) for message in messages]}
    original = copy.deepcopy(wire["messages"])
    source = {"source_kind": "session_command", "source_id": "foreign-source", "source_version": "revoked"}
    blocked = len(messages) - (2 if blocked_role == "assistant" else 1)
    bindings = {"dependencies": [source], "messages": [
        {"message_index": i, "message_hash": _hash(value), "status": "bound",
            "code": "canonical_origin_matched", "dependency_status": "resolved",
            "dependencies": [source] if i == blocked else []}
        for i, value in enumerate(original)
    ]}

    _withhold_model_source(messages, wire, bindings, source, "content_source_access_denied")

    assert wire["messages"][:-2] == original[:-2]
    assert all(entry["status"] == "bound" for entry in bindings["messages"][:-2])
    assert all(entry["status"] == "withheld" for entry in bindings["messages"][-2:])
    assert "FOREIGN_REPLY_BYTES" not in json.dumps(wire)
