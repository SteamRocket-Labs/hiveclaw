"""R7a: validate actual provider payloads after the shared request preparation."""

import copy
import json
from types import SimpleNamespace as NS
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from app.kernel.contracts import InvocationRequest, TerminalReason
from app.kernel.engine import AgentKernel, _dicts_to_llm_messages
from app.services.llm_client import AnthropicClient, LLMError, LLMMessage, LLMResponse, OpenAICompatibleClient
from app.services.session_model_inputs import _hash, message_snapshot
from app.tools.result_envelope import ToolContentEnvelope
from tests.integration.test_a4c_one_item_withheld import _bindings, _FakeDB
from tests.integration.test_a4c_r2 import patch_validators
from tests.kernel.test_parallel_tool_batch import _make_deps, _make_model
from tests.services.test_web_chat_run_orchestrator import _failure_finalize_harness


def assert_tool_groups(payload, provider):
    """Every call gets exactly one adjacent reply; no intervening role is legal."""
    pending = set()
    for message in payload["messages"]:
        if provider == "openai":
            calls = [call["id"] for call in message.get("tool_calls") or []]
            replies = [message["tool_call_id"]] if message["role"] == "tool" else []
        else:
            blocks = message.get("content")
            blocks = blocks if isinstance(blocks, list) else []
            calls = [block["id"] for block in blocks if block["type"] == "tool_use"]
            replies = [block["tool_use_id"] for block in blocks if block["type"] == "tool_result"]
            if pending:
                assert message["role"] == "user" and len(replies) == len(blocks), message
        if pending:
            assert replies, f"Interrupted tool group {pending}: {message}"
        for reply in replies:
            assert reply in pending, f"Unmatched or duplicate result {reply}: {pending}"
            pending.remove(reply)
        if calls:
            assert message["role"] == "assistant"
            assert not pending and len(set(calls)) == len(calls)
            pending = set(calls)
    assert not pending, f"Missing results: {pending}"


def _builder(provider):
    if provider == "openai":
        return OpenAICompatibleClient(api_key="local", provider="minimax", model="MiniMax-M3.1-flash")
    return AnthropicClient(api_key="local", model="local")


def _calls(names, prefix="call_"):
    return [{"id": f"{prefix}{uuid4() if prefix == 'call-' else index}", "type": "function", "function": {
        "name": name, "arguments": json.dumps({"index": index})}}
        for index, name in enumerate(names)]


@pytest.mark.parametrize("provider", ["openai", "anthropic"])
@pytest.mark.parametrize("pattern,names,failed,withdrawn", [
    ("five_parallel_failed", ["report_progress", "list_files", "read_file", "read_file", "search_memory"], {2, 3}, {2, 3}),
    ("parallel_failed_control", ["report_progress", "list_files", "read_file", "read_file", "search_memory"], {2, 3}, set()),
    ("text_reasoning_success", ["search_memory", "grep_search", "list_files"], set(), {0}),
    ("text_reasoning_control", ["search_memory", "grep_search", "list_files"], set(), set()),
    ("sequential_restart", ["edit_file", "edit_file", "write_file"], {0, 1}, {0, 1}),
    ("mixed_segments", ["reconcile_memory_write", "execute_code", "grep_search"], {0}, {0}),
    ("delegated_child", ["glob_search", "read_file", "list_files"], {1, 2}, {1}),
])
async def test_kernel_request_preserves_tool_groups_after_withholding(monkeypatch, provider, pattern, names, failed, withdrawn):
    from app.services import session_model_round as rounds

    calls = _calls(names, "call-" if pattern == "delegated_child" else "call_")
    blocked_ids = {calls[index]["id"] for index in withdrawn}
    source = {"source_kind": "explicit_memory", "source_id": "withdrawn", "source_version": "v1"}
    patch_validators(monkeypatch, [source])
    monkeypatch.setattr("app.services.content_input_receipts._validate_input_dependencies", AsyncMock())
    monkeypatch.setattr(rounds, "_bound_rows", AsyncMock(return_value=[]))
    monkeypatch.setattr(rounds, "append_session_events", AsyncMock())
    monkeypatch.setattr("app.services.session_model_inputs.capture_context_message_origins", AsyncMock())
    prepared_bindings = []

    async def bind(_db, *, messages, **kwargs):
        bindings = _bindings(len(messages), {index: [source] for index, message in enumerate(messages)
            if message.role == "tool" and message.tool_call_id in blocked_ids})
        for entry, message in zip(bindings["messages"], messages):
            entry["message_hash"] = _hash(message_snapshot(message))
        prepared_bindings.append(bindings)
        return bindings

    monkeypatch.setattr("app.services.session_model_inputs.build_model_input_bindings", bind)
    db = _FakeDB()
    tenant, agent, session, run = (uuid4() for _ in range(4))

    async def prepare(**kwargs):
        return await rounds.prepare_model_request(db, tenant_id=tenant, agent_id=agent,
            session_id=session, run_id=run, turn_id="r7a", **kwargs)

    class Client:
        payloads = []
        snapshots = []

        async def stream(self, *, messages, **kwargs):
            self.snapshots.append(copy.deepcopy(messages))
            self.payloads.append(_builder(provider)._build_payload(messages, None, 0.7, 2048))
            assert_tool_groups(self.payloads[-1], provider)
            if len(self.payloads) == 1:
                return LLMResponse(content="I will inspect these sources.", reasoning_content="Private reasoning",
                    reasoning_signature="local-signature", tool_calls=calls, usage={"total_tokens": 10})
            return LLMResponse(content="done", usage={"total_tokens": 5})

        async def close(self):
            pass

    async def execute(name, args, request, emit_event):
        if args["index"] in failed:
            raise RuntimeError("local tool failure")
        return ToolContentEnvelope(text="successful observation",
            new_messages=({"role": "system", "content": "Side-effect notice"},))

    client = Client()
    result = await AgentKernel(_make_deps(create_client=lambda _: client, execute_tool=execute)).handle(
        InvocationRequest(model=_make_model(), messages=[{"role": "user", "content": pattern}],
            agent_name="Agent", role_description="", agent_id=agent, user_id=uuid4(),
            model_request_prepare=prepare))
    # Validate again outside the kernel's exception-to-failure boundary.
    for payload in client.payloads:
        assert_tool_groups(payload, provider)
    assert result.content == "done"
    assert len(client.payloads) == 2
    final = client.snapshots[-1]
    assert {message.tool_call_id for message in final if message.role == "tool"} == {
        call["id"] for call in calls} - blocked_ids
    if pattern == "mixed_segments":
        # The existing unsafe-tool barrier aborts later siblings after the
        # first failure; all replies must still be present and contiguous.
        assert not any(message.content == "Side-effect notice" for message in final)
        assert all("aborted" in message.content.lower() for message in final if message.role == "tool")
    else:
        assert any(message.content == "Side-effect notice" for message in final)
    if blocked_ids:
        assert "Private reasoning" not in json.dumps(client.payloads[-1])
    for entry in prepared_bindings[-1]["messages"]:
        if entry["status"] != "withheld":
            assert entry["message_hash"] == _hash(message_snapshot(final[entry["message_index"]]))


@pytest.mark.parametrize("status,kind,retryable", [(400, "provider_bad_request", False),
    (429, "rate_limited", True), (500, "unknown", True), (503, "unknown", True), (408, "timeout", True),
    (401, "auth_or_permission", False), (403, "auth_or_permission", False),
    (404, "unknown", False), (402, "quota_exhausted", False), (529, "rate_limited", True)])
def test_typed_status_ignores_bare_numbers_in_provider_body(status, kind, retryable):
    from app.services.llm_error_policy import classify_llm_error

    # Round 2 preserves semantic body matches; only bare status numbers are
    # ignored when a typed status exists. An opaque 404 is not a missing model.
    classification = classify_llm_error(LLMError("opaque failure references 400 401 403 404 429 529",
        delivery_state="rejected", http_status=status))
    assert classification.kind == kind
    assert classification.retryable is retryable


@pytest.mark.parametrize("delivery", ["rejected", "unknown"])
async def test_provider_400_failure_reaches_terminal_and_parent_without_retry(monkeypatch, delivery):
    from app.services import web_chat_run_orchestrator as orchestrator
    from app.services.runtime_result_store import encode_runtime_result_payload, decode_runtime_result_payload

    class Client:
        async def stream(self, **kwargs):
            raise LLMError("opaque provider rejection", http_status=400, delivery_state=delivery)

        async def close(self):
            pass

    result = await AgentKernel(_make_deps(create_client=lambda _: Client())).handle(
        InvocationRequest(model=_make_model(), messages=[{"role": "user", "content": "Remember this"}],
            agent_name="Child", role_description=""))
    assert result.terminal_reason is TerminalReason.PROVIDER_ERROR
    calls = []
    await orchestrator._finalize_assistant_response(_failure_finalize_harness(calls), result,
        result.content, None, "failed", {"terminal_reason": "provider_error"})
    failure = calls[0]["failure"]
    assert failure["failure_code"] == "provider_bad_request"
    assert failure["delivery_state"] == delivery
    assert failure["retryable"] is False
    # Completion outbox encodes the child's terminal metadata for the parent's
    # read_runtime_result. These facts must survive the finalizer boundary.
    parent = decode_runtime_result_payload(encode_runtime_result_payload(
        summary=calls[0]["result_summary"], metadata=calls[0]["metadata_json"], artifacts=[]).payload_bytes)
    assert parent["metadata"]["error_code"] == "provider_bad_request"
    assert parent["metadata"]["retryable"] is False


def test_strict_validator_rejects_interrupted_missing_and_unmatched_results():
    calls = _calls(["read_file", "list_files"])
    valid = [{"role": "assistant", "tool_calls": calls},
        {"role": "tool", "tool_call_id": "call_0"}, {"role": "tool", "tool_call_id": "call_1"}]
    for messages in (valid[:2], [valid[0], {"role": "user"}, *valid[1:]],
                     [valid[0], valid[1], valid[1]], [{"role": "tool", "tool_call_id": "other"}]):
        with pytest.raises(AssertionError):
            assert_tool_groups({"messages": messages}, "openai")
