"""R7a review rulings: preserve provider facts and complete tool groups."""

import copy
from uuid import uuid4

import pytest

from app.services.llm_client import LLMError
from app.services.llm_error_policy import classify_llm_error, should_surface_without_model_fallback
from tests.integration.test_a4c_one_item_withheld import _bindings, _FakeDB
from tests.integration.test_a4c_r2 import patch_validators
from tests.services.test_r7a_provider_contract import _builder, _calls, assert_tool_groups


@pytest.mark.parametrize("status,body,kind,decision,retryable", [
    (429, "quota exhausted", "quota_exhausted", True, False),
    (429, "token-plan quota has been exhausted", "quota_exhausted", True, False),
    (400, "prompt is too long: maximum context length exceeded", "prompt_too_long", False, False),
    (413, "context_length_exceeded", "prompt_too_long", False, False),
    (400, "quota exhausted; timeout; rate limit", "provider_bad_request", True, False),
    (404, "opaque provider rejection", "unknown", False, False),
    (429, "opaque provider rejection", "rate_limited", True, True),
    (529, "opaque provider rejection", "rate_limited", False, True),
    (500, "opaque provider failure", "unknown", False, True),
    (503, "overloaded", "rate_limited", False, True),
    (500, "insufficient balance", "quota_exhausted", True, False),
    (422, "invalid_request", "provider_bad_request", True, False),
    (408, "opaque provider failure", "timeout", False, True),
    (None, "request timed out", "timeout", False, True),
    (None, "connection failed", "connection", False, True),
])
def test_provider_body_and_typed_status_preserve_base_policy(status, body, kind, decision, retryable):
    error = LLMError(body, http_status=status, delivery_state="rejected")
    classification = classify_llm_error(error)
    assert classification.kind == kind
    assert classification.retryable is retryable
    assert classification.requires_user_decision is decision
    assert should_surface_without_model_fallback(error) is decision
    if kind == "unknown":
        assert classification.user_message.endswith(f"({body})")


@pytest.mark.parametrize("status", [400, 401, 403, 404, 413, 422])
@pytest.mark.parametrize("body", ["overloaded", "rate limit", "timeout", "connection failed"])
def test_body_cannot_make_a_deterministic_typed_rejection_retryable(status, body):
    assert classify_llm_error(LLMError(body, http_status=status, delivery_state="rejected")).retryable is False


@pytest.mark.parametrize("delivery", ["rejected", "unknown"])
@pytest.mark.parametrize("body", ["opaque provider error", "HTTP 503: opaque provider error"])
def test_untyped_unknown_uses_delivery_safety_without_guessing_http_status(delivery, body):
    classification = classify_llm_error(LLMError(body, delivery_state=delivery))
    assert classification.kind == "unknown"
    assert classification.user_message.endswith(f"({body})")
    assert classification.retryable is (delivery == "rejected")


@pytest.mark.parametrize("delivery", ["rejected", "unknown"])
async def test_rejected_400_keeps_retry_button_without_retry_copy_or_parent_retry(monkeypatch, delivery):
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel
    from app.services import web_chat_run_orchestrator as orchestrator
    from app.services.runtime_result_store import encode_runtime_result_payload, decode_runtime_result_payload
    from app.services.thread_items import build_thread_item
    from tests.kernel.test_parallel_tool_batch import _make_deps, _make_model
    from tests.services.test_thread_items import _event
    from tests.services.test_web_chat_run_orchestrator import _failure_finalize_harness

    class Client:
        async def stream(self, **kwargs):
            raise LLMError("opaque provider rejection", http_status=400, delivery_state=delivery)

        async def close(self):
            pass

    result = await AgentKernel(_make_deps(create_client=lambda _: Client())).handle(
        InvocationRequest(model=_make_model(), messages=[{"role": "user", "content": "Do work"}],
            agent_name="Child", role_description=""))
    calls = []
    await orchestrator._finalize_assistant_response(_failure_finalize_harness(calls), result,
        result.content, None, "failed", {"terminal_reason": "provider_error"})
    failure = calls[0]["failure"]
    assert failure["retryable"] is False
    item = build_thread_item(_event(event_type="runtime_failure", item_type="error", item_status="failed",
        metadata_json=failure), audience="user")
    assert item["user_summary"] == "任务遇到问题，已停止继续执行。"
    assert "可以安全重试" not in item["user_summary"]
    assert (item.get("user_action") or {}).get("kind") == ("retry_turn" if delivery == "rejected" else None)
    assert failure["retry_safe"] is (delivery == "rejected")
    parent = decode_runtime_result_payload(encode_runtime_result_payload(
        summary=calls[0]["result_summary"], metadata=calls[0]["metadata_json"], artifacts=[]).payload_bytes)
    assert parent["metadata"]["retryable"] is False


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("role", ["system", "user", "assistant"])
@pytest.mark.parametrize("after_first_reply", [False, True])
async def test_shared_preparation_defers_every_non_tool_row(monkeypatch, provider, role, after_first_reply):
    from app.kernel.engine import _dicts_to_llm_messages
    from app.services.session_model_inputs import _hash
    from app.services.session_model_round import _validate_or_withhold_model_sources

    patch_validators(monkeypatch)
    calls = _calls(["read_file", "list_files"])
    assistant = {"role": "assistant", "content": "Inspecting", "tool_calls": calls}
    replies = [{"role": "tool", "tool_call_id": call["id"], "content": "result"} for call in calls]
    notes = [{"role": role, "content": "first note"}, {"role": "system", "content": "second note"}]
    messages = [assistant, *replies[:int(after_first_reply)], *notes, *replies[int(after_first_reply):]]
    bindings = _bindings(len(messages), {})
    for entry, message in zip(bindings["messages"], messages):
        entry["message_hash"] = _hash(message)
    wire = {"messages": copy.deepcopy(messages)}
    await _validate_or_withhold_model_sources(_FakeDB(), tenant_id=uuid4(), agent_id=uuid4(),
        session_id=uuid4(), run_id=uuid4(), messages=messages, wire_request=wire, input_bindings=bindings)
    assert messages == wire["messages"] == [assistant, *replies, *notes]
    assert_tool_groups(_builder(provider)._build_payload(_dicts_to_llm_messages(messages), None, 0.7, 2048), provider)
    for entry in bindings["messages"]:
        assert entry["message_hash"] == _hash(messages[entry["message_index"]])


@pytest.mark.parametrize("extra_wire_row", [False, True])
async def test_tool_group_permutation_skips_mismatched_message_lists(monkeypatch, extra_wire_row):
    from app.services.session_model_round import _validate_or_withhold_model_sources

    patch_validators(monkeypatch)
    call = _calls(["read_file"])[0]
    messages = [{"role": "assistant", "tool_calls": [call]}, {"role": "system", "content": "note"},
        {"role": "tool", "tool_call_id": call["id"], "content": "result"}]
    wire = {"messages": copy.deepcopy(messages)}
    (wire["messages"] if extra_wire_row else messages).append({"role": "user", "content": "extra"})
    original = copy.deepcopy((messages, wire))
    await _validate_or_withhold_model_sources(_FakeDB(), tenant_id=uuid4(), agent_id=uuid4(),
        session_id=uuid4(), run_id=uuid4(), messages=messages, wire_request=wire,
        input_bindings=_bindings(len(wire["messages"]), {}))
    assert (messages, wire) == original
