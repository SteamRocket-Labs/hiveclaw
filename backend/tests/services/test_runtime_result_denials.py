"""Native no-data feedback stays bound without certifying its denied target."""
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.services.runtime_result_sources import runtime_result_denial_bound
from app.services.session_model_inputs import _hash


def _denial(status="source_access_denied", reason="content_source_access_denied", retryable=False):
    ref = f"runtime-result://{uuid4()}/{'b' * 64}"
    body = {"schema": "hive.runtime_result_page.v1", "status": status, "reason_code": reason,
        "result_ref": ref, "retryable": retryable, "recovery_action": "reconcile_result_sources"}
    content = json.dumps(body)
    receipt = f"tool-frame:{_hash(content)}"
    invocation = SimpleNamespace(tool_name="read_runtime_result", effect_state="effect_committed",
        receipt_ref=receipt, effective_arguments_json={"result_ref": ref}, provider_arguments_json={})
    payload = {"outcome": "success", "content_hash": _hash(content), "content_or_error_ref": receipt}
    return invocation, payload, content


@pytest.mark.parametrize(("status", "reason", "retryable"), [
    ("source_access_denied", "content_source_access_denied", False),
    ("source_unavailable", "runtime_result_sources_unbound", False),
    ("source_unavailable", "content_source_unavailable", True),
])
def test_native_source_refusal_keeps_only_its_settled_feedback(status, reason, retryable):
    invocation, payload, content = _denial(status, reason, retryable)
    assert runtime_result_denial_bound(invocation=invocation, payload=payload, content=content)


@pytest.mark.parametrize("mode", ["extra_content", "wrong_tool", "not_settled", "unsupported_status",
    "denial_retry", "wrong_target", "receipt_rewritten", "event_rehashed"])
def test_native_source_refusal_cannot_certify_content_or_a_different_frame(mode):
    invocation, payload, content = _denial()
    body = json.loads(content)
    if mode == "wrong_tool":
        invocation.tool_name = "read_file"
    elif mode == "not_settled":
        invocation.effect_state = "prepared_not_started"
    elif mode == "receipt_rewritten":
        invocation.receipt_ref = "tool-frame:" + "a" * 64
    else:
        if mode == "extra_content":
            body["content"] = "DENIED_TARGET_BYTES"
        elif mode == "unsupported_status":
            body["status"] = "ok"
        elif mode == "denial_retry":
            body["retryable"] = True
        elif mode == "wrong_target":
            body["result_ref"] = f"runtime-result://{uuid4()}/{'c' * 64}"
        content = json.dumps(body, indent=2)
        payload["content_hash"] = _hash(content)
        payload["content_or_error_ref"] = f"tool-frame:{payload['content_hash']}"
        if mode != "event_rehashed":
            invocation.receipt_ref = payload["content_or_error_ref"]
    assert not runtime_result_denial_bound(invocation=invocation, payload=payload, content=content)
