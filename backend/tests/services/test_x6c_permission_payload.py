"""Typed permission copies never reinterpret an ordinary tool's JSON document."""

import copy
import json
import time

import pytest

from app.services.session_event_contract import member_permission_payload


@pytest.mark.parametrize("carrier", ["toolResult", "result", "content-result"])
@pytest.mark.parametrize("document", [
    {"type": "permission", "message": "Admins may push", "capability": "contents:write", "permission_mode": "strict"},
    {"status": "permission_denied", "message": "Resource not accessible by integration", "capability": "contents:write"},
    {"status": {"code": 403}},
    {"status": ["denied"]},
    {"type": "permission", "status": {"code": 403}, "text": "permission configuration"},
    {"type": "permission", "status": ["denied"], "text": "permission configuration"},
    {"data": [], "metadata": {"status": "permission_denied", "message": "API data"}},
    {"item_kind": "tool_result", "payload": {"outcome": "denied", "content": "permission documentation"}},
    # Mention the receipt key without holding a receipt Mapping (reviewer round 2, S1).
    {"status": "permission_denied", "message": "Repo says no", "permission_request": None},
    {"type": "permission", "text": "Docs: the permission_request field is optional", "capability": "x"},
])
def test_untyped_result_strings_are_byte_identical(carrier, document):
    body = " \n" + json.dumps(document, ensure_ascii=False, indent=2) + "\n "
    payload = ({"content": json.dumps({"name": "ordinary_tool", "result": body}, indent=2)}
               if carrier == "content-result" else {carrier: body})
    before = copy.deepcopy(payload)
    projected = member_permission_payload(payload)
    assert projected == before
    if carrier == "content-result":
        assert json.loads(projected["content"])["result"] == body
    else:
        assert projected[carrier] == body
    assert payload == before


@pytest.mark.parametrize("status", [{"code": 403}, ["denied"]])
def test_typed_permission_with_non_string_status_does_not_raise(status):
    event = {"type": "permission", "status": status, "message": "internal teaching", "capability": "private"}
    assert member_permission_payload(event) == {
        "type": "permission", "status": status, "message": "当前没有权限完成这次操作。"}


@pytest.mark.parametrize("carrier", ["payload", "metadata", "v2_payload", "parts"])
def test_requestless_permission_internal_context_is_private(carrier):
    event = {"type": "permission", "status": "governance_hook_denied", "message": "internal teaching",
             "reason": "PII export; ticket SEC-118", "hook_key": "export-guard",
             "credential_family": "mail", "security_zone": "public"}
    if carrier == "parts":
        event = {**event, "type": "event", "event_type": "permission", "text": event["message"]}
        frame = {carrier: [event]}
    else:
        frame = {carrier: event}
    before = copy.deepcopy(frame)
    projected = member_permission_payload(frame)
    clean = projected[carrier][0] if carrier == "parts" else projected[carrier]
    assert clean["message"] == "当前没有权限完成这次操作。"
    assert not {"reason", "hook_key", "credential_family", "security_zone"} & clean.keys()
    assert frame == before


def test_non_permission_internal_context_fields_keep_their_meaning():
    event = {"type": "child_session", "status": "completed", "message": "Colleague returned",
             "reason": "delegation_completed", "hook_key": "authored-hook",
             "credential_family": "authored-family", "security_zone": "authored-zone"}
    assert member_permission_payload(event) == event


def test_nested_denied_tool_result_reuses_the_clearing_path():
    from tests.services.test_session_event_contract import _event

    envelope = _event(item_kind="tool_result", lifecycle="completed", payload={
        "tool_name": "unmapped_tool", "outcome": "denied", "content": "internal teaching",
        "result": "internal teaching", "model_visible_content": "internal teaching",
        "parts": [{"type": "text", "text": "internal teaching"}],
    })
    before = copy.deepcopy(envelope)
    member = member_permission_payload(envelope)
    payload = member["payload"]
    assert payload["content"] == payload["result"] == payload["model_visible_content"] == "当前没有权限完成这次操作。"
    assert payload["parts"] == []
    assert "member_summary" not in payload
    assert member_permission_payload(member) == member
    assert envelope == before


@pytest.mark.parametrize("carrier", ["messages", "content-result"])
def test_large_untyped_result_skips_json_parsing(monkeypatch, carrier):
    import app.services.session_event_contract as contract

    body = json.dumps({"permissions": {"read": True}, "body": "x" * (775 * 1024)})
    payload = ({"toolResult": body, "parts": [{"type": "tool_call", "result": body}]}
               if carrier == "messages" else {"content": json.dumps({"name": "ordinary_tool", "result": body})})
    loads = contract.json.loads
    calls = []

    def observe(*args, **kwargs):
        calls.append(True)
        return loads(*args, **kwargs)

    monkeypatch.setattr(contract.json, "loads", observe)
    start = time.perf_counter()
    for _ in range(20):
        assert member_permission_payload(payload) == payload
    milliseconds = (time.perf_counter() - start) * 1000 / 20
    print(f"X6c ordinary {carrier}: {milliseconds:.3f} ms/projection, json.loads calls={len(calls)}")
    # An exact parser-call check, not a machine-load-sensitive timing gate.
    assert calls == []
