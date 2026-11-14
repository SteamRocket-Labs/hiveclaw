"""Member permission projections preserve authored data and distinct event fields."""

import json

import pytest


def test_permission_projection_preserves_authored_json_and_argument_fields():
    from app.services.session_event_contract import serialize_session_event
    from tests.services.test_session_event_contract import _event

    authored = {"content": json.dumps({"permission_request": {"permission_mode": "example"}})}
    assert serialize_session_event(_event(payload=authored), audience="user")["payload"] == authored
    arguments = {"permission_request": {"permission_profile": "a proposed document field"}}
    payload = {"permission_request": {"permission_request_id": "request", "arguments": arguments}}
    assert serialize_session_event(
        _event(item_kind="tool_permission", lifecycle="waiting", payload=payload), audience="user",
    )["payload"] == payload


def test_permission_projection_keeps_distinct_sibling_values():
    from app.services.session_event_contract import member_permission_payload

    payload = {"created_at": "event-time", "status": "session_permission_required", "type": "permission",
        "capability": "internal-policy", "permission_request": {"permission_request_id": "request",
            "created_at": "request-time", "status": "waiting", "type": "tool_permission", "capability": "internal-policy"}}
    projected = member_permission_payload(payload)
    assert projected["created_at"] == "event-time"
    assert projected["status"] == "session_permission_required"
    assert projected["type"] == "permission"
    assert "capability" not in projected
    assert projected["permission_request"] == {"permission_request_id": "request"}


def test_permission_projection_drops_internal_siblings_by_name():
    from app.services.session_event_contract import member_permission_payload

    internal = {"capability", "permission_mode", "decision_reason", "pending_tool_frame",
                "round_state", "permission_profile"}
    payload = {key: "sibling-value" for key in internal}
    payload["permission_request"] = {"permission_request_id": "request",
                                     **{key: "nested-value" for key in internal}}
    projected = member_permission_payload(payload)
    assert not internal.intersection(projected)


def test_permission_projection_keeps_equal_non_internal_siblings():
    from app.services.session_event_contract import member_permission_payload

    siblings = {"status": "session_permission_required", "created_at": "same-time", "type": "permission"}
    projected = member_permission_payload({**siblings, "permission_request": siblings})
    assert {key: projected[key] for key in siblings} == siblings


@pytest.mark.parametrize("status", [None, "session_permission_required", "permission_resolved", "permission_denied"])
def test_permission_summary_follows_status(status):
    from app.services.session_event_contract import member_permission_payload

    event = {"type": "permission", "message": "Internal hook decision", "text": "Internal hook decision",
             "permission_request": {"permission_request_id": "request", "tool_name": "delete_file"}}
    if status is not None:
        event["status"] = status
    payload = {"payload": event, "metadata": event, "parts": [event], "content": json.dumps(event)}
    projected = member_permission_payload(payload)
    expected = "需要你的确认：Delete file" if status in {None, "session_permission_required"} else "已记录你的决定。"
    for carrier in [projected["payload"], projected["metadata"], projected["parts"][0], json.loads(projected["content"])]:
        assert carrier["message"] == carrier["text"] == expected


def test_permission_projection_preserves_unrelated_matching_text():
    from app.services.session_event_contract import member_permission_payload

    projected = member_permission_payload({"content": "hello", "parts": [
        {"type": "text", "text": "hello"},
        {"type": "event", "text": "hello", "permission_request": {
            "permission_request_id": "request", "tool_name": "delete_file", "capability": "internal-policy"}},
    ]})
    assert projected["content"] == projected["parts"][0]["text"] == "hello"


@pytest.mark.parametrize("carrier", ["toolResult", "part", "content-result"])
def test_permission_result_strings_are_projected(carrier):
    from app.services.session_event_contract import member_permission_payload

    receipt = json.dumps({"message": "INTERNAL_POLICY_REASON", "permission_request": {
        "permission_request_id": "request", "tool_name": "delete_file", "capability": "internal-policy",
        "decision_reason": "INTERNAL_POLICY_REASON", "pending_tool_frame": {"permission_profile": "internal"}}})
    payload = {"toolResult": receipt} if carrier == "toolResult" else (
        {"parts": [{"type": "tool_call", "result": receipt}]} if carrier == "part" else
        {"content": json.dumps({"name": "delete_file", "result": receipt})})
    projected = member_permission_payload(payload)
    result = projected["toolResult"] if carrier == "toolResult" else (
        projected["parts"][0]["result"] if carrier == "part" else json.loads(projected["content"])["result"])
    assert json.loads(result) == {"message": "需要你的确认：Delete file", "permission_request": {
        "permission_request_id": "request", "tool_name": "delete_file"}}


@pytest.mark.parametrize("result", ["plain result\n", ' { "ok": true, "result": "normal" } ', '[1, 2]',
                                  '{"permission_request": "a document field"}', "invalid {json"])
def test_ordinary_result_strings_and_arguments_are_unchanged(result):
    from app.services.session_event_contract import member_permission_payload

    arguments = {"permission_request": {"capability": "authored document field"}, "result": result}
    payload = {"toolResult": result, "arguments": arguments,
               "parts": [{"type": "tool_call", "arguments": arguments, "result": result}],
               "content": json.dumps({"name": "tool", "args": arguments, "result": result}, indent=2)}
    assert member_permission_payload(payload) == payload
