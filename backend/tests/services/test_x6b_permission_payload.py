"""Member permission projections preserve authored data and distinct event fields."""

import json


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
