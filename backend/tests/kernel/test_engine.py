from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest


def _loaded_recovery_result(payload: dict):
    from app.runtime.recovery_manifest import manifest_from_payload
    from app.runtime.recovery_manifest_store import RecoveryAuthorityFrame

    session_id = str(payload.get("session_id") or "")
    authority = RecoveryAuthorityFrame(
        tenant_id="tenant-test",
        agent_id="agent-test",
        requester_user_id="user-test",
        session_id=session_id,
        root_session_id=session_id,
        root_runtime_task_id="root-task-test",
        principal_type="delegated_user",
        principal_id="user-test",
        principal_snapshot_hash="principal-test",
        policy_snapshot_hash="policy-test",
        config_snapshot_hash="config-test",
        base_transcript_sequence=1,
    )
    manifest = manifest_from_payload(payload)
    manifest_ref = f"recovery-manifest://{authority.digest}/{'a' * 64}"
    return SimpleNamespace(
        status="loaded",
        loaded=True,
        authority=authority,
        manifest=manifest,
        manifest_ref=manifest_ref,
        envelope_sha256="a" * 64,
        render_restoration_text=lambda *, budget_chars=20_000: manifest.to_restoration_text(
            budget_chars=budget_chars,
            manifest_ref=manifest_ref,
            manifest_sha256="a" * 64,
            manifest_reader_tool="read_context_resource",
        ),
        status_payload=lambda: {
            "schema": "hive.recovery_manifest_status.v1",
            "status": "loaded",
            "reason": None,
            "retryable": False,
        },
    )


def _bind_verified_recovery_result(request, payload: dict) -> None:
    result = _loaded_recovery_result(payload)
    request.recovery_authority = result.authority
    request.recovery_manifest_result = result
    request.session_context.metadata["recovery_manifest_authority_hash"] = result.authority.digest


def _record_governed_tool_success(trace_metadata_sink) -> None:
    """Give tool test doubles the same settlement facts as the live pipeline."""
    assert isinstance(trace_metadata_sink, dict)
    trace_metadata_sink.setdefault("tool_decision", {"outcome": "allow"})
    trace_metadata_sink.setdefault("tool_execution_frame", {"status": "completed"})


class _FakeClient:
    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def stream(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("No fake response prepared")
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def close(self) -> None:
        return None


def test_llm_message_dict_round_trip_preserves_reasoning_signature() -> None:
    from app.kernel.engine import _dicts_to_llm_messages, _llm_messages_to_dicts
    from app.services.llm_client import LLMMessage

    messages = [
        LLMMessage(
            role="assistant",
            content="tool decision",
            reasoning_content="signed thinking",
            reasoning_signature="sig-round-trip",
        )
    ]

    as_dicts = _llm_messages_to_dicts(messages)
    restored = _dicts_to_llm_messages(as_dicts)

    assert as_dicts[0]["reasoning_signature"] == "sig-round-trip"
    assert restored[0].reasoning_signature == "sig-round-trip"


@pytest.mark.asyncio
async def test_empty_provider_response_is_typed_failure_without_platform_assistant_prose() -> None:
    from app.kernel import AgentKernel, InvocationRequest, KernelDependencies, RuntimeConfig
    from app.kernel.contracts import TerminalReason

    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None)
    client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[],
                reasoning_content=None,
                reasoning_signature=None,
                finish_reason="stop",
                usage={"total_tokens": 3},
            )
        ]
    )
    failures: list[dict] = []
    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_a, **_k: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=1),
            resolve_current_user_name=lambda *_a, **_k: "Rocky",
            build_system_prompt=lambda *_a, **_k: "PROMPT",
            resolve_memory_context=lambda *_a, **_k: "",
            get_tools=lambda *_a, **_k: [],
            maybe_compress_messages=lambda messages, **_k: messages,
            create_client=lambda _model: client,
            execute_tool=lambda *_a, **_k: "",
            persist_memory=lambda **_k: None,
            record_token_usage=lambda *_a, **_k: None,
            get_max_tokens=lambda *_a, **_k: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens") if usage else None,
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "answer"}],
            agent_name="Agent",
            role_description="Answers",
            agent_id=uuid4(),
            user_id=uuid4(),
            model_request_prepare=lambda **_payload: "provider-request:round-1",
            model_request_fail=lambda **payload: failures.append(payload),
        )
    )

    assert result.content == ""
    assert result.parts == []
    assert result.terminal_reason is TerminalReason.PROVIDER_ERROR
    assert failures == [
        {
            "round_index": 1,
            "provider_request_id": "provider-request:round-1",
            "error_class": "provider_empty_response",
            "delivery_state": "response_received",
            "retry_safe": True,
        }
    ]


def test_model_authored_error_prefix_remains_memory_eligible() -> None:
    from app.kernel import InvocationRequest
    from app.kernel.engine import _build_persisted_memory_messages

    request = InvocationRequest(
        model=SimpleNamespace(),
        messages=[{"role": "user", "content": "Quote the old error label."}],
        agent_name="Agent",
        role_description="Quotes text",
        memory_messages=[{"role": "user", "content": "Quote the old error label."}],
    )
    authored = "[LLM Error] is a quoted historical UI label, not this result's machine status."

    persisted = _build_persisted_memory_messages(request, authored)

    assert persisted[-1] == {"role": "assistant", "content": authored}


def test_permissions_context_exposes_plan_mode_plan_file_as_writable_root() -> None:
    from app.kernel import InvocationRequest, RuntimeConfig
    from app.kernel.engine import _build_permissions_context
    from app.runtime.session import SessionContext

    request = InvocationRequest(
        model=SimpleNamespace(),
        messages=[],
        agent_name="agent",
        role_description="role",
        session_context=SessionContext(
            session_id="session-1",
            metadata={
                "permission_profile": {"mode": "default"},
                "plan_mode": {
                    "active": True,
                    "plan_file_path": "workspace/plans/session-1.plan.md",
                },
            },
        ),
    )

    prompt = _build_permissions_context(request, RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=3))

    assert "writable_roots:" in prompt
    assert "- workspace/plans/session-1.plan.md" in prompt


def test_permissions_context_consumes_skill_execution_plans() -> None:
    from app.kernel import InvocationRequest, RuntimeConfig
    from app.kernel.engine import _build_permissions_context
    from app.runtime.session import SessionContext

    session = SessionContext(
        session_id="session-skill",
        metadata={
            "skill_execution_plans": [
                {
                    "skill": "Research",
                    "skill_slug": "research",
                    "source": "skills/research/SKILL.md",
                    "execution_mode": "fork",
                    "execution_tool": "spawn_subagent",
                    "permission_profile": {"mode": "auto", "allowed_tools": ["web_search", "read_file"]},
                    "tool_arguments": {
                        "prompt": "Use the loaded skill `Research`.",
                        "skill": "Research",
                        "permission_profile": {"mode": "auto", "allowed_tools": ["web_search", "read_file"]},
                    },
                }
            ]
        },
    )
    request = InvocationRequest(
        model=SimpleNamespace(),
        messages=[],
        agent_name="agent",
        role_description="role",
        session_context=session,
    )

    prompt = _build_permissions_context(request, RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=3))

    assert "- web_search" in prompt
    assert "- read_file" in prompt
    assert "Pending Skill Execution Handoffs" in prompt
    assert "spawn_subagent" in prompt
    assert "Research" in prompt
    assert session.metadata["permission_profile"]["allowed_tools"] == ["web_search", "read_file"]


def test_split_concatenated_json_returns_single_object_when_valid():
    """T1-4: a single valid JSON object passes through untouched."""
    from app.kernel.engine import _split_concatenated_json

    assert _split_concatenated_json('{"query":"a"}') == ['{"query":"a"}']
    assert _split_concatenated_json('  {"query":"a"}  ') == ['{"query":"a"}']


@pytest.mark.asyncio
async def test_kernel_continues_streaming_output_after_output_cap() -> None:
    from app.kernel import AgentKernel, InvocationRequest, KernelDependencies, RuntimeConfig

    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None)
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="part one ",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 10},
                finish_reason="length",
            ),
            SimpleNamespace(
                content="part two",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 6},
                finish_reason="stop",
            ),
        ]
    )

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_args, **_kwargs: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=3,
                quota_message=None,
            ),
            resolve_current_user_name=lambda *_args, **_kwargs: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [],
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: "",
            persist_memory=lambda **_kwargs: None,
            record_token_usage=lambda *_args, **_kwargs: None,
            get_max_tokens=lambda _provider, _model, override=None: override or 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    prepared_requests: list[dict] = []
    committed_results: list[dict] = []

    async def prepare_request(**payload):
        prepared_requests.append(payload)
        return f"provider-request-{int(payload.get('continuation_index') or 0)}"

    async def commit_result(**payload):
        committed_results.append(payload)
        return {"result_id": str(uuid4()), "provider_request_id": payload["provider_request_id"]}

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "write a long report"}],
            agent_name="Writer",
            role_description="Writes reports",
            agent_id=uuid4(),
            user_id=uuid4(),
            model_request_prepare=prepare_request,
            model_response_commit=commit_result,
        )
    )

    assert result.content == "part one part two"
    assert result.tokens_used == 16
    # Continuation escalates to the provider's per-provider output ceiling
    # (openai = 131072), not the old flat 65536.
    assert [call["max_tokens"] for call in fake_client.calls] == [2048, 131072]
    continuation_messages = fake_client.calls[1]["messages"]
    assert any(message.role == "assistant" and message.content == "part one " for message in continuation_messages)
    assert "Continue the previous answer" in continuation_messages[-1].content
    assert [payload["continuation_index"] for payload in prepared_requests] == [0, 1]
    assert [payload["provider_request_id"] for payload in committed_results] == [
        "provider-request-1",
        "provider-request-0",
    ]
    assert committed_results[0]["logical_round_complete"] is False
    assert committed_results[1]["logical_round_complete"] is True
    assert committed_results[1]["response"]["content"] == "part one part two"
    assert [entry["continuation_index"] for entry in committed_results[1]["response"]["provider_call_ledger"]] == [
        0,
        1,
    ]
    assert prepared_requests[0]["wire_request"]["max_tokens"] == 2048
    assert prepared_requests[1]["wire_request"]["max_tokens"] == 131072
    assert result.model_result_receipt is not None
    assert result.model_result_receipt["provider_request_id"] == "provider-request-0"


@pytest.mark.asyncio
async def test_kernel_converts_stream_retry_tombstone_to_runtime_event() -> None:
    from app.kernel import AgentKernel, InvocationRequest, KernelDependencies, RuntimeConfig
    from app.services.llm_client import STREAM_RETRY_TOMBSTONE

    class _TombstoneClient:
        async def stream(self, **kwargs):
            await kwargs["on_chunk"]("partial ")
            await kwargs["on_chunk"](STREAM_RETRY_TOMBSTONE)
            await kwargs["on_chunk"]("final")
            return SimpleNamespace(
                content="final",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 5},
                finish_reason="stop",
            )

        async def close(self) -> None:
            return None

    chunks: list[str] = []
    runtime_events: list[dict] = []
    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None)
    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_args, **_kwargs: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=3,
                quota_message=None,
            ),
            resolve_current_user_name=lambda *_args, **_kwargs: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [],
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: _TombstoneClient(),
            execute_tool=lambda *_args, **_kwargs: "",
            persist_memory=lambda **_kwargs: None,
            record_token_usage=lambda *_args, **_kwargs: None,
            get_max_tokens=lambda _provider, _model, override=None: override or 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "hello"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
            on_chunk=chunks.append,
            on_event=runtime_events.append,
        )
    )

    assert result.content == "final"
    assert chunks == ["partial ", "final"]
    assert {"type": "stream_retry_tombstone"} in runtime_events


@pytest.mark.asyncio
async def test_kernel_records_prompt_cache_metrics_from_response_usage() -> None:
    from app.kernel import AgentKernel, InvocationRequest, KernelDependencies, RuntimeConfig
    from app.memory.metrics import reset_all, snapshot

    reset_all()
    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None)
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="cached answer",
                tool_calls=[],
                reasoning_content=None,
                usage={"prompt_tokens": 1000, "prompt_tokens_details": {"cached_tokens": 750}},
                finish_reason="stop",
            ),
        ]
    )
    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_args, **_kwargs: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=3,
                quota_message=None,
            ),
            resolve_current_user_name=lambda *_args, **_kwargs: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [],
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: "",
            persist_memory=lambda **_kwargs: None,
            record_token_usage=lambda *_args, **_kwargs: None,
            get_max_tokens=lambda _provider, _model, override=None: override or 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens") or usage.get("prompt_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "hello"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    snap = snapshot()
    assert result.content == "cached answer"
    assert snap["prompt_cache_observations_total"]["openai:hit"] == 1
    assert snap["prompt_cache_read_tokens_total"]["openai"] == 750
    assert snap["prompt_cache_hit_rate"]["openai"] == 0.75


@pytest.mark.asyncio
async def test_response_complete_hook_failure_is_counted_without_failing_invocation(monkeypatch) -> None:
    from app.kernel import AgentKernel, InvocationRequest, KernelDependencies, RuntimeConfig
    from app.memory.metrics import reset_all, snapshot

    reset_all()

    async def raising_emit_hook(*_args, **_kwargs):
        raise RuntimeError("hook bus down")

    monkeypatch.setattr("app.runtime.hooks.emit_hook", raising_emit_hook)

    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None)
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="done",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 3},
                finish_reason="stop",
            )
        ]
    )

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_args, **_kwargs: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=3,
                quota_message=None,
            ),
            resolve_current_user_name=lambda *_args, **_kwargs: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [],
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: "",
            persist_memory=lambda **_kwargs: None,
            record_token_usage=lambda *_args, **_kwargs: None,
            get_max_tokens=lambda _provider, _model, override=None: override or 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "hello"}],
            agent_name="Agent",
            role_description="Answers",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content == "done"
    for _ in range(10):
        if "response_complete:kernel:RuntimeError" in snapshot()["hook_failure_total"]:
            break
        await asyncio.sleep(0.01)
    assert snapshot()["hook_failure_total"]["response_complete:kernel:RuntimeError"] == 1


@pytest.mark.asyncio
async def test_kernel_drains_mid_run_user_messages_between_tool_rounds() -> None:
    from app.kernel import AgentKernel, InvocationRequest, KernelDependencies, RuntimeConfig

    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None)
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[{"id": "call_1", "function": {"name": "list_files", "arguments": "{}"}}],
                reasoning_content=None,
                usage={"total_tokens": 5},
            ),
            SimpleNamespace(
                content="noted",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 7},
            ),
        ]
    )
    drain_calls = 0

    async def drain_mid_run_messages():
        nonlocal drain_calls
        drain_calls += 1
        if drain_calls == 2:
            return [{"role": "user", "content": "Actually focus on the security boundary."}]
        return []

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_args, **_kwargs: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=3,
                quota_message=None,
            ),
            resolve_current_user_name=lambda *_args, **_kwargs: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [
                {
                    "type": "function",
                    "function": {"name": "list_files", "description": "", "parameters": {"type": "object"}},
                }
            ],
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: "files",
            persist_memory=lambda **_kwargs: None,
            record_token_usage=lambda *_args, **_kwargs: None,
            get_max_tokens=lambda _provider, _model, override=None: override or 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "Inspect the project"}],
            agent_name="Engineer",
            role_description="Investigates repositories",
            agent_id=uuid4(),
            user_id=uuid4(),
            mid_run_message_drain=drain_mid_run_messages,
        )
    )

    assert result.content == "noted"
    second_round_messages = fake_client.calls[1]["messages"]
    assert any(
        message.role == "user" and message.content == "Actually focus on the security boundary."
        for message in second_round_messages
    )


@pytest.mark.asyncio
async def test_kernel_binds_round_inputs_and_commits_exact_provider_request_receipt() -> None:
    from app.kernel import AgentKernel, InvocationRequest, KernelDependencies, RuntimeConfig

    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None)
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="done",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 7},
            )
        ]
    )
    lifecycle: list[tuple[str, object]] = []

    async def bind_round_inputs(round_index: int):
        lifecycle.append(("bind", round_index))
        return [
            {
                "role": "user",
                "content": "Use the newly supplied production evidence.",
                "session_input_id": "input-1",
            }
        ]

    async def prepare_model_request(**payload):
        lifecycle.append(("prepare", payload))
        return "provider-request:round-1"

    async def commit_model_response(**payload):
        lifecycle.append(("commit", payload))

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_args, **_kwargs: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=1,
                quota_message=None,
            ),
            resolve_current_user_name=lambda *_args, **_kwargs: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [],
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: "",
            persist_memory=lambda **_kwargs: None,
            record_token_usage=lambda *_args, **_kwargs: None,
            get_max_tokens=lambda _provider, _model, override=None: override or 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "Inspect the project"}],
            agent_name="Engineer",
            role_description="Investigates repositories",
            agent_id=uuid4(),
            user_id=uuid4(),
            round_input_bind=bind_round_inputs,
            model_request_prepare=prepare_model_request,
            model_response_commit=commit_model_response,
        )
    )

    assert result.content == "done"
    assert [item[0] for item in lifecycle] == ["bind", "prepare", "commit"]
    sent_messages = fake_client.calls[0]["messages"]
    assert any(
        message.role == "user" and message.content == "Use the newly supplied production evidence."
        for message in sent_messages
    )
    prepare_payload = lifecycle[1][1]
    assert prepare_payload["round_index"] == 1
    assert prepare_payload["messages"] == sent_messages
    assert prepare_payload["provider"] == "openai"
    assert prepare_payload["model"] == "gpt-4.1"
    assert prepare_payload["provider_idempotency_supported"] is False
    assert prepare_payload["provider_idempotency_key_applied"] is False
    commit_payload = lifecycle[2][1]
    assert commit_payload["round_index"] == 1
    assert commit_payload["provider_request_id"] == "provider-request:round-1"


@pytest.mark.asyncio
async def test_kernel_never_retries_an_ambiguous_provider_send_on_a_fallback_model() -> None:
    from app.kernel import AgentKernel, InvocationRequest, KernelDependencies, RuntimeConfig
    from app.kernel.contracts import ProviderRequestNeedsReconciliation
    from app.services.llm_client import LLMError

    primary_model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None)
    fallback_model = SimpleNamespace(provider="anthropic", model="claude-sonnet", api_key="key", base_url=None)
    primary_client = _FakeClient([LLMError("Connection failed after retries: request timed out")])
    fallback_client = _FakeClient(
        [
            SimpleNamespace(
                content="must not be sent",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 1},
            )
        ]
    )
    failed_requests: list[dict] = []

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_args, **_kwargs: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=1,
                quota_message=None,
            ),
            resolve_current_user_name=lambda *_args, **_kwargs: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [],
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda model: primary_client if model is primary_model else fallback_client,
            execute_tool=lambda *_args, **_kwargs: "",
            persist_memory=lambda **_kwargs: None,
            record_token_usage=lambda *_args, **_kwargs: None,
            get_max_tokens=lambda _provider, _model, override=None: override or 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens") if usage else None,
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    with pytest.raises(ProviderRequestNeedsReconciliation):
        await kernel.handle(
            InvocationRequest(
                model=primary_model,
                fallback_model=fallback_model,
                messages=[{"role": "user", "content": "Inspect the project"}],
                agent_name="Engineer",
                role_description="Investigates repositories",
                agent_id=uuid4(),
                user_id=uuid4(),
                model_request_prepare=lambda **_payload: "provider-request:round-1",
                model_request_fail=lambda **payload: failed_requests.append(payload),
            )
        )

    assert len(primary_client.calls) == 1
    assert fallback_client.calls == []
    assert failed_requests == [
        {
            "round_index": 1,
            "provider_request_id": "provider-request:round-1",
            "error_class": "timeout",
            "delivery_state": "unknown",
            "retry_safe": False,
        }
    ]


@pytest.mark.asyncio
async def test_kernel_classifies_typed_http_402_as_rejected_quota_without_replay() -> None:
    """DAY1-PROVIDER-402-CLASSIFICATION-001: a typed HTTP 402 rejection is an
    authoritative delivery_state=rejected.  The kernel must NOT raise
    ProviderRequestNeedsReconciliation, must record exact
    model_request_fail evidence (quota_exhausted/rejected/retry_safe),
    must not invoke any fallback model (quota_exhausted requires a user
    decision), and must return the existing user-facing quota/balance
    error result — never an invented assistant success."""

    from app.kernel import AgentKernel, InvocationRequest, KernelDependencies, RuntimeConfig
    from app.services.llm_client import LLMError

    primary_model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None)
    fallback_model = SimpleNamespace(provider="anthropic", model="claude-sonnet", api_key="key", base_url=None)
    primary_client = _FakeClient(
        [
            LLMError(
                'HTTP 402: {"error":{"message":"Insufficient Balance","type":"invalid_request_error"}}',
                delivery_state="rejected",
                http_status=402,
            )
        ]
    )
    fallback_client = _FakeClient(
        [SimpleNamespace(content="must not run", tool_calls=[], reasoning_content=None, usage={})]
    )
    failures: list[dict] = []
    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_a, **_k: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=1),
            resolve_current_user_name=lambda *_a, **_k: "Rocky",
            build_system_prompt=lambda *_a, **_k: "PROMPT",
            resolve_memory_context=lambda *_a, **_k: "",
            get_tools=lambda *_a, **_k: [],
            maybe_compress_messages=lambda messages, **_k: messages,
            create_client=lambda model: primary_client if model is primary_model else fallback_client,
            execute_tool=lambda *_a, **_k: "",
            persist_memory=lambda **_k: None,
            record_token_usage=lambda *_a, **_k: None,
            get_max_tokens=lambda *_a, **_k: 2048,
            extract_usage_tokens=lambda usage: None,
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=primary_model,
            fallback_model=fallback_model,
            messages=[{"role": "user", "content": "hello"}],
            agent_name="Agent",
            role_description="test",
            agent_id=uuid4(),
            user_id=uuid4(),
            model_request_prepare=lambda **_payload: "provider-request:round-1",
            model_request_fail=lambda **payload: failures.append(payload),
        )
    )

    assert len(primary_client.calls) == 1
    assert fallback_client.calls == []
    assert failures == [
        {
            "round_index": 1,
            "provider_request_id": "provider-request:round-1",
            "error_class": "quota_exhausted",
            "delivery_state": "rejected",
            "retry_safe": True,
        }
    ]
    # The existing user-facing quota/balance error path — no invented success.
    assert "额度或余额不足" in str(result.content)
    assert "must not run" not in str(result.content)


@pytest.mark.asyncio
async def test_kernel_binds_402_quota_policy_to_typed_status_with_opaque_body() -> None:
    """DAY1-PROVIDER-402-CLASSIFICATION-001 correction: an authoritative
    typed HTTP 402 with an OPAQUE body must still yield the exact
    quota_exhausted lifecycle — rejected/retry_safe evidence, zero
    fallback-model calls (the quota hard outcome is owned by
    exc.http_status == 402, never by body text)."""

    from app.kernel import AgentKernel, InvocationRequest, KernelDependencies, RuntimeConfig
    from app.services.llm_client import LLMError

    primary_model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None)
    fallback_model = SimpleNamespace(provider="anthropic", model="claude-sonnet", api_key="key", base_url=None)
    primary_client = _FakeClient([LLMError("opaque provider rejection", delivery_state="rejected", http_status=402)])
    fallback_client = _FakeClient(
        [SimpleNamespace(content="must not run", tool_calls=[], reasoning_content=None, usage={})]
    )
    failures: list[dict] = []
    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_a, **_k: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=1),
            resolve_current_user_name=lambda *_a, **_k: "Rocky",
            build_system_prompt=lambda *_a, **_k: "PROMPT",
            resolve_memory_context=lambda *_a, **_k: "",
            get_tools=lambda *_a, **_k: [],
            maybe_compress_messages=lambda messages, **_k: messages,
            create_client=lambda model: primary_client if model is primary_model else fallback_client,
            execute_tool=lambda *_a, **_k: "",
            persist_memory=lambda **_k: None,
            record_token_usage=lambda *_a, **_k: None,
            get_max_tokens=lambda *_a, **_k: 2048,
            extract_usage_tokens=lambda usage: None,
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=primary_model,
            fallback_model=fallback_model,
            messages=[{"role": "user", "content": "hello"}],
            agent_name="Agent",
            role_description="test",
            agent_id=uuid4(),
            user_id=uuid4(),
            model_request_prepare=lambda **_payload: "provider-request:round-1",
            model_request_fail=lambda **payload: failures.append(payload),
        )
    )

    assert len(primary_client.calls) == 1
    assert fallback_client.calls == []
    assert failures == [
        {
            "round_index": 1,
            "provider_request_id": "provider-request:round-1",
            "error_class": "quota_exhausted",
            "delivery_state": "rejected",
            "retry_safe": True,
        }
    ]
    # The existing user-facing quota/balance error path — no invented success.
    assert "额度或余额不足" in str(result.content)
    assert "must not run" not in str(result.content)


@pytest.mark.asyncio
async def test_text_only_http_402_without_typed_rejection_stays_needs_reconciliation() -> None:
    """Safety invariant (same defect class): 402-looking TEXT on an LLMError
    whose typed delivery_state is unknown must still reconcile — the hard
    invariant is the authoritative typed HTTP status, never the text."""

    from app.kernel import AgentKernel, InvocationRequest, KernelDependencies, RuntimeConfig
    from app.kernel.contracts import ProviderRequestNeedsReconciliation
    from app.services.llm_client import LLMError

    primary_model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None)
    fallback_model = SimpleNamespace(provider="anthropic", model="claude-sonnet", api_key="key", base_url=None)
    primary_client = _FakeClient([LLMError('HTTP 402: {"error":{"message":"Insufficient Balance"}}')])
    fallback_client = _FakeClient(
        [SimpleNamespace(content="must not run", tool_calls=[], reasoning_content=None, usage={})]
    )
    failures: list[dict] = []
    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_a, **_k: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=1),
            resolve_current_user_name=lambda *_a, **_k: "Rocky",
            build_system_prompt=lambda *_a, **_k: "PROMPT",
            resolve_memory_context=lambda *_a, **_k: "",
            get_tools=lambda *_a, **_k: [],
            maybe_compress_messages=lambda messages, **_k: messages,
            create_client=lambda model: primary_client if model is primary_model else fallback_client,
            execute_tool=lambda *_a, **_k: "",
            persist_memory=lambda **_k: None,
            record_token_usage=lambda *_a, **_k: None,
            get_max_tokens=lambda *_a, **_k: 2048,
            extract_usage_tokens=lambda usage: None,
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    with pytest.raises(ProviderRequestNeedsReconciliation):
        await kernel.handle(
            InvocationRequest(
                model=primary_model,
                fallback_model=fallback_model,
                messages=[{"role": "user", "content": "hello"}],
                agent_name="Agent",
                role_description="test",
                agent_id=uuid4(),
                user_id=uuid4(),
                model_request_prepare=lambda **_payload: "provider-request:text-only-402",
                model_request_fail=lambda **payload: failures.append(payload),
            )
        )

    assert fallback_client.calls == []
    assert failures[0]["delivery_state"] == "unknown"
    assert failures[0]["retry_safe"] is False


@pytest.mark.asyncio
async def test_provider_error_text_cannot_authorize_replay_without_typed_rejection() -> None:
    from app.kernel import AgentKernel, InvocationRequest, KernelDependencies, RuntimeConfig
    from app.kernel.contracts import ProviderRequestNeedsReconciliation
    from app.services.llm_client import LLMError

    primary_model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None)
    fallback_model = SimpleNamespace(provider="anthropic", model="claude-sonnet", api_key="key", base_url=None)
    primary_client = _FakeClient([LLMError("HTTP 400: benign text says prompt too long and retry me")])
    fallback_client = _FakeClient(
        [SimpleNamespace(content="must not run", tool_calls=[], reasoning_content=None, usage={})]
    )
    failures: list[dict] = []
    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_a, **_k: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=1),
            resolve_current_user_name=lambda *_a, **_k: "Rocky",
            build_system_prompt=lambda *_a, **_k: "PROMPT",
            resolve_memory_context=lambda *_a, **_k: "",
            get_tools=lambda *_a, **_k: [],
            maybe_compress_messages=lambda messages, **_k: messages,
            create_client=lambda model: primary_client if model is primary_model else fallback_client,
            execute_tool=lambda *_a, **_k: "",
            persist_memory=lambda **_k: None,
            record_token_usage=lambda *_a, **_k: None,
            get_max_tokens=lambda *_a, **_k: 2048,
            extract_usage_tokens=lambda usage: None,
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    with pytest.raises(ProviderRequestNeedsReconciliation):
        await kernel.handle(
            InvocationRequest(
                model=primary_model,
                fallback_model=fallback_model,
                messages=[{"role": "user", "content": "hello"}],
                agent_name="Agent",
                role_description="test",
                agent_id=uuid4(),
                user_id=uuid4(),
                model_request_prepare=lambda **_payload: "provider-request:text-only",
                model_request_fail=lambda **payload: failures.append(payload),
            )
        )

    assert fallback_client.calls == []
    assert failures[0]["delivery_state"] == "unknown"
    assert failures[0]["retry_safe"] is False


def test_mid_run_drain_prefers_structured_llm_content_over_display_text() -> None:
    from app.kernel.engine import _mid_run_items_to_user_messages

    messages = _mid_run_items_to_user_messages(
        [
            {
                "content": "[file:bank.pdf]\nSummarize it.",
                "display_content": "[file:bank.pdf]\nSummarize it.",
                "llm_content": "[File: bank.pdf]\nFull extracted text\n\nQuestion: Summarize it.",
                "attachments": [{"name": "bank.pdf", "path": "workspace/bank.pdf"}],
            }
        ]
    )

    assert len(messages) == 1
    assert messages[0].role == "user"
    assert messages[0].content == "[File: bank.pdf]\nFull extracted text\n\nQuestion: Summarize it."


def test_mid_run_drain_preserves_system_runtime_notifications() -> None:
    from app.kernel.engine import _mid_run_items_to_user_messages

    messages = _mid_run_items_to_user_messages(
        [
            {
                "role": "system",
                "content": "Runtime task notification: the delegated worker completed.",
                "display_content": "Worker completed.",
            }
        ]
    )

    assert len(messages) == 1
    assert messages[0].role == "system"
    assert messages[0].content == "Runtime task notification: the delegated worker completed."


def test_split_concatenated_json_splits_double_object():
    """T1-4: DeepSeek-V4 style {"a":1}{"b":2} concatenation is split into separate payloads."""
    from app.kernel.engine import _split_concatenated_json

    result = _split_concatenated_json('{"query":"a"}{"query":"b"}')
    assert result == ['{"query":"a"}', '{"query":"b"}']


def test_split_concatenated_json_splits_with_whitespace_between():
    """T1-4: whitespace between concatenated payloads is tolerated."""
    from app.kernel.engine import _split_concatenated_json

    result = _split_concatenated_json('{"query":"a"} {"query":"b"}\n{"query":"c"}')
    assert result == ['{"query":"a"}', '{"query":"b"}', '{"query":"c"}']


def test_split_concatenated_json_keeps_string_braces_intact():
    """T1-4: a closing brace inside a string must not be treated as object boundary."""
    from app.kernel.engine import _split_concatenated_json

    payload = '{"query":"contains } brace"}'
    assert _split_concatenated_json(payload) == [payload]


def test_split_concatenated_json_falls_back_when_partial():
    """T1-4: when the buffer is not cleanly split-able, return it as-is so the caller can
    report a parse error rather than swallowing an unknown shape."""
    from app.kernel.engine import _split_concatenated_json

    assert _split_concatenated_json("garbage") == ["garbage"]
    assert _split_concatenated_json('{"a":1}garbage{"b":2}') == ['{"a":1}garbage{"b":2}']


def test_expand_concatenated_tool_calls_splits_each_payload():
    """T1-4: concatenated tool_call.arguments expand into separate tool_calls with
    distinct ids so every payload gets executed and every tool message can reference
    a real tool_call id in the assistant history."""
    from app.kernel.engine import _expand_concatenated_tool_calls

    expanded = _expand_concatenated_tool_calls(
        [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "web_search", "arguments": '{"query":"a"}{"query":"b"}'},
            }
        ]
    )

    assert [tc["id"] for tc in expanded] == ["call_1-split1", "call_1-split2"]
    assert [tc["function"]["arguments"] for tc in expanded] == ['{"query":"a"}', '{"query":"b"}']
    assert all(tc["type"] == "function" for tc in expanded)
    assert all(tc["function"]["name"] == "web_search" for tc in expanded)


def test_expand_concatenated_tool_calls_passes_through_clean_payloads():
    """T1-4: a single valid payload is not duplicated or rewritten."""
    from app.kernel.engine import _expand_concatenated_tool_calls

    original = [
        {
            "id": "call_2",
            "type": "function",
            "function": {"name": "load_skill", "arguments": '{"slug":"office-productivity"}'},
        }
    ]
    expanded = _expand_concatenated_tool_calls(original)
    assert expanded == original


def test_humanize_llm_error_reports_quota_instead_of_auth_for_403_quota():
    from app.kernel.engine import _humanize_llm_error
    from app.services.llm_client import LLMError

    message = _humanize_llm_error(
        LLMError('HTTP 403: {"error":{"message":"Your token-plan quota has been exhausted."}}')
    )

    assert message == "[LLM Error] AI 模型额度已耗尽，请联系管理员检查模型额度或切换模型。"


def test_humanize_llm_error_reports_model_not_found_separately():
    from app.kernel.engine import _humanize_llm_error
    from app.services.llm_client import LLMError

    message = _humanize_llm_error(LLMError('HTTP 404: {"error":{"message":"Model Not Exist"}}'))

    assert message == "[LLM Error] AI 模型名称不存在或未对当前账号开放，请联系管理员检查模型配置。"


def test_humanize_llm_error_reports_provider_bad_request_separately():
    from app.kernel.engine import _humanize_llm_error
    from app.services.llm_client import LLMError

    message = _humanize_llm_error(LLMError('HTTP 400: {"error":{"message":"invalid model parameter"}}'))

    assert message == "[LLM Error] AI 模型供应商拒绝了当前请求，请检查模型名称、参数或消息格式。"


def test_build_restoration_context_prefers_newest_recent_files(tmp_path, monkeypatch):
    from app.kernel.engine import _build_restoration_context
    from app.runtime.session import SessionContext

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id)
    workspace.mkdir(parents=True)

    files = []
    for name in ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"]:
        path = tmp_path / name
        path.write_text(f"content for {name}", encoding="utf-8")
        files.append(str(path))

    session = SessionContext()
    session.recent_files = files

    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    restored = _build_restoration_context(agent_id, session_context=session)

    e_idx = restored.index("### Recent File: e.txt")
    d_idx = restored.index("### Recent File: d.txt")
    c_idx = restored.index("### Recent File: c.txt")
    assert e_idx < d_idx < c_idx


def test_build_restoration_context_resolves_relative_recent_file_paths(tmp_path, monkeypatch):
    from app.kernel.engine import _build_restoration_context
    from app.runtime.session import SessionContext

    agent_id = uuid4()
    agent_root = tmp_path / str(agent_id)
    workspace_dir = agent_root / "workspace"
    workspace_dir.mkdir(parents=True)
    (workspace_dir / "report.md").write_text("relative path content", encoding="utf-8")

    session = SessionContext()
    session.track_file_read("workspace/report.md")

    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    restored = _build_restoration_context(agent_id, session_context=session)

    assert "### Recent File: workspace/report.md" in restored
    assert "relative path content" in restored


def test_build_restoration_context_restores_five_recent_files(tmp_path, monkeypatch):
    """P2-1 (docs/compaction-cc-alignment.md): restore the working set after compaction —
    up to 5 recent files (CC restores ≤5), not 3."""
    from app.kernel.engine import _build_restoration_context
    from app.runtime.session import SessionContext

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id)
    workspace.mkdir(parents=True)

    files = []
    for name in ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"]:
        path = tmp_path / name
        path.write_text(f"content for {name}", encoding="utf-8")
        files.append(str(path))

    session = SessionContext()
    session.recent_files = files

    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    restored = _build_restoration_context(agent_id, session_context=session)

    for name in ["a.txt", "b.txt", "c.txt", "d.txt", "e.txt"]:
        assert f"### Recent File: {name}" in restored


def test_build_restoration_context_file_budget_uses_per_file_cap(tmp_path, monkeypatch):
    """Per-file restore budget is the full per-file cap (8K chars), not cap//2."""
    from app.kernel.engine import _POST_COMPACT_PER_FILE_CAP, _build_restoration_context
    from app.runtime.session import SessionContext

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id)
    workspace.mkdir(parents=True)

    big_file = tmp_path / "big.txt"
    marker_tail = "TAIL-MARKER-AT-6000"
    big_file.write_text("z" * 5980 + marker_tail, encoding="utf-8")  # 5999 chars < 8K cap

    session = SessionContext()
    session.recent_files = [str(big_file)]

    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    restored = _build_restoration_context(agent_id, session_context=session)

    assert len(marker_tail) < _POST_COMPACT_PER_FILE_CAP  # sanity
    assert marker_tail in restored  # old cap//2 (4K) would have cut this off


def test_build_restoration_context_long_soul_has_hash_pinned_recovery_ref(tmp_path, monkeypatch):
    import hashlib

    from app.kernel.engine import _build_restoration_context
    from app.runtime.session import SessionContext

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id)
    workspace.mkdir(parents=True)
    soul = "# Soul\n" + ("identity evidence\n" * 1_000) + "DECISIVE_SOUL_TAIL"
    (workspace / "soul.md").write_text(soul, encoding="utf-8")
    session = SessionContext(
        metadata={"context_budget": SimpleNamespace(restore_budget_chars=20_000, restore_per_file_cap_chars=2_000)}
    )

    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    restored = _build_restoration_context(agent_id, session_context=session)

    assert "resource_ref=soul.md" in restored
    assert f"sha256={hashlib.sha256(soul.encode('utf-8')).hexdigest()}" in restored
    assert f"char_range=0-{len(soul)}" in restored
    assert "omitted_range=" in restored


def test_build_restoration_context_injects_persisted_recovery_manifest(tmp_path, monkeypatch):
    from app.kernel.engine import _build_restoration_context
    from app.runtime.session import SessionContext

    agent_id = uuid4()
    manifest_path = tmp_path / str(agent_id) / "runtime_artifacts" / "recovery_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "session_id": "session-recover",
                "recent_reads": ["workspace/source.md"],
                "recent_writes": ["workspace/result.md"],
                "pending_items": ["continue D8 recovery"],
                "permission_profile": {"mode": "default", "allowed_tools": ["write_file"]},
                "pending_tool_frames": [{"tool_name": "write_file", "status": "pending"}],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    session = SessionContext(session_id="session-recover")

    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    recovery_result = _loaded_recovery_result(json.loads(manifest_path.read_text(encoding="utf-8")))
    restored = _build_restoration_context(
        agent_id,
        session_context=session,
        recovery_result=recovery_result,
    )

    assert "### Recovery Manifest" in restored
    assert "continue D8 recovery" in restored
    assert "Pending Tool Frames" in restored
    assert "Permission Profile" in restored


def test_recovery_snapshot_materialization_failure_is_typed_and_hides_internal_refs() -> None:
    from app.kernel.engine import _build_runtime_attachment_sections
    from app.runtime.session import SessionContext

    session = SessionContext(session_id="session-recovery-unavailable")
    authority = SimpleNamespace(session_id=session.session_id)

    def fail_render(*, budget_chars=20_000):
        _ = budget_chars
        raise OSError("PRIVATE_INTERNAL_SNAPSHOT_PATH")

    recovery_result = SimpleNamespace(
        loaded=True,
        authority=authority,
        manifest_ref="recovery-manifest://" + "a" * 64 + "/" + "b" * 64,
        render_restoration_text=fail_render,
    )

    sections = _build_runtime_attachment_sections(
        uuid4(),
        session,
        recovery_result,
    )
    rendered = "\n".join(sections)

    assert "### Recovery State" in rendered
    assert '"status":"unavailable"' in rendered
    assert '"reason":"resource_snapshot_unavailable"' in rendered
    assert "PRIVATE_INTERNAL_SNAPSHOT_PATH" not in rendered
    assert recovery_result.manifest_ref not in rendered


def test_post_compaction_recovery_snapshot_failure_is_typed_and_hides_internal_refs(tmp_path, monkeypatch) -> None:
    from app.kernel.engine import _build_restoration_context
    from app.runtime.session import SessionContext

    session = SessionContext(session_id="session-post-compact-unavailable")
    authority = SimpleNamespace(session_id=session.session_id)

    def fail_render(*, budget_chars=20_000):
        _ = budget_chars
        raise OSError("PRIVATE_INTERNAL_POST_COMPACT_PATH")

    recovery_result = SimpleNamespace(
        loaded=True,
        authority=authority,
        manifest_ref="recovery-manifest://" + "c" * 64 + "/" + "d" * 64,
        render_restoration_text=fail_render,
    )
    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    rendered = _build_restoration_context(
        uuid4(),
        session_context=session,
        recovery_result=recovery_result,
    )

    assert "### Recovery State" in rendered
    assert '"status":"unavailable"' in rendered
    assert '"reason":"resource_snapshot_unavailable"' in rendered
    assert "PRIVATE_INTERNAL_POST_COMPACT_PATH" not in rendered
    assert recovery_result.manifest_ref not in rendered


def test_recovery_checkpoint_hold_revokes_stale_loaded_result(monkeypatch) -> None:
    from app.kernel.engine import _persist_recovery_manifest_checkpoint
    from app.runtime.recovery_manifest_store import RecoveryManifestPersistResult
    from app.runtime.session import SessionContext

    stale_result = SimpleNamespace(status="loaded", loaded=True)
    request = SimpleNamespace(
        session_context=SessionContext(session_id="session-checkpoint-hold"),
        recovery_authority=SimpleNamespace(session_id="session-checkpoint-hold"),
        recovery_manifest_result=stale_result,
    )
    monkeypatch.setattr(
        "app.runtime.recovery_manifest_store.persist_recovery_manifest",
        lambda *_args, **_kwargs: RecoveryManifestPersistResult(
            status="held",
            reason="policy_snapshot_changed_before_persist",
        ),
    )

    _persist_recovery_manifest_checkpoint(request)

    assert request.recovery_manifest_result is not stale_result
    assert request.recovery_manifest_result.status == "held"
    assert request.recovery_manifest_result.loaded is False
    assert request.recovery_manifest_result.reason == "policy_snapshot_changed_before_persist"


def test_recovery_checkpoint_exception_revokes_stale_loaded_result(monkeypatch) -> None:
    from app.kernel.engine import _persist_recovery_manifest_checkpoint
    from app.runtime.session import SessionContext

    stale_result = SimpleNamespace(status="loaded", loaded=True)
    request = SimpleNamespace(
        session_context=SessionContext(session_id="session-checkpoint-failure"),
        recovery_authority=SimpleNamespace(session_id="session-checkpoint-failure"),
        recovery_manifest_result=stale_result,
    )

    def fail_persist(*_args, **_kwargs):
        raise OSError("PRIVATE_CHECKPOINT_PATH")

    monkeypatch.setattr(
        "app.runtime.recovery_manifest_store.persist_recovery_manifest",
        fail_persist,
    )

    _persist_recovery_manifest_checkpoint(request)

    assert request.recovery_manifest_result is not stale_result
    assert request.recovery_manifest_result.status == "unavailable"
    assert request.recovery_manifest_result.loaded is False
    assert request.recovery_manifest_result.reason == "checkpoint_persist_unavailable"
    assert "PRIVATE_CHECKPOINT_PATH" not in json.dumps(request.session_context.metadata)


def test_recovery_pending_tool_frame_without_call_id_is_cleared() -> None:
    from app.kernel import InvocationRequest
    from app.kernel.engine import _clear_pending_tool_frame_for_recovery, _record_pending_tool_frame_for_recovery
    from app.runtime.session import SessionContext

    session = SessionContext(
        session_id="session-1",
        channel="web",
        metadata={"runtime_task_id": "runtime-1", "turn_id": "turn-1"},
    )
    request = InvocationRequest(
        model=SimpleNamespace(),
        messages=[],
        agent_name="Agent",
        role_description="Role",
        agent_id=uuid4(),
        session_context=session,
    )

    _record_pending_tool_frame_for_recovery(
        request,
        tool_name="write_file",
        tool_args={"path": "workspace/a.md", "content": "a"},
        tool_call_id=None,
    )

    assert session.metadata["pending_tool_frame"]["tool_call_id"] == ""
    _clear_pending_tool_frame_for_recovery(request, tool_call_id=None)
    assert "pending_tool_frame" not in session.metadata
    assert "pending_tool_frames" not in session.metadata


def test_runtime_attachment_sections_include_persisted_recovery_manifest(tmp_path: Path, monkeypatch) -> None:
    from app.kernel.engine import _build_runtime_attachment_sections
    from app.runtime.session import SessionContext

    agent_id = uuid4()
    manifest_path = tmp_path / str(agent_id) / "runtime_artifacts" / "recovery_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "session_id": "session-recover",
                "pending_tool_frames": [
                    {
                        "tool_call_id": "call-running",
                        "tool_name": "write_file",
                        "status": "running",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    recovery_result = _loaded_recovery_result(json.loads(manifest_path.read_text(encoding="utf-8")))
    sections = _build_runtime_attachment_sections(
        agent_id,
        SessionContext(session_id="session-recover"),
        recovery_result,
    )

    joined = "\n\n".join(sections)
    assert "### Recovery Manifest" in joined
    assert "call-running" in joined


def test_runtime_attachment_sections_reject_another_sessions_recovery_manifest(
    tmp_path: Path,
    monkeypatch,
) -> None:
    from app.kernel.engine import _build_runtime_attachment_sections
    from app.runtime.session import SessionContext

    agent_id = uuid4()
    manifest_path = tmp_path / str(agent_id) / "runtime_artifacts" / "recovery_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "session_id": "session-a",
                "current_turn_writes": ["workspace/private-session-a.md"],
                "permission_profile": {"mode": "full_access"},
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    session = SessionContext(session_id="session-b")

    recovery_result = _loaded_recovery_result(json.loads(manifest_path.read_text(encoding="utf-8")))
    sections = _build_runtime_attachment_sections(agent_id, session, recovery_result)

    joined = "\n\n".join(sections)
    assert "private-session-a.md" not in joined
    assert "full_access" not in joined
    assert "verified.json" not in joined
    assert "runtime_session_mismatch" in joined
    assert session.current_turn_writes == []
    assert "permission_profile" not in session.metadata


def test_runtime_attachment_sections_use_context_restore_budget(tmp_path: Path, monkeypatch) -> None:
    from app.kernel.engine import _build_runtime_attachment_sections
    from app.runtime.session import SessionContext

    agent_id = uuid4()
    manifest_path = tmp_path / str(agent_id) / "runtime_artifacts" / "recovery_manifest.json"
    manifest_path.parent.mkdir(parents=True)
    manifest_path.write_text(
        json.dumps(
            {
                "session_id": "session-recover",
                "recent_reads": ["x" * 24_000 + " SENTINEL_RESTORE_BUDGET"],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
    session = SessionContext(session_id="session-recover")
    session.metadata["context_budget"] = SimpleNamespace(restore_budget_chars=60_000)

    recovery_result = _loaded_recovery_result(json.loads(manifest_path.read_text(encoding="utf-8")))
    sections = _build_runtime_attachment_sections(agent_id, session, recovery_result)

    assert "SENTINEL_RESTORE_BUDGET" in "\n\n".join(sections)


@pytest.mark.asyncio
async def test_compress_messages_with_lifecycle_hooks_emits_pre_and_post(monkeypatch):
    from app.kernel.engine import _compress_messages_with_lifecycle_hooks
    from app.runtime.hooks import HookEvent

    hook_calls = []

    async def fake_emit_hook(event, **kwargs):
        hook_calls.append((event, kwargs))

    summary_tail = "COMPACTION_SUMMARY_DECISIVE_TAIL"
    full_summary = "s" * 3500 + summary_tail

    async def fake_compressor(messages, **_kwargs):
        return [{"role": "system", "content": full_summary}, messages[-1]]

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)

    messages = [
        {"role": "user", "content": "first"},
        {"role": "assistant", "content": "second"},
    ]
    compressed = await _compress_messages_with_lifecycle_hooks(
        fake_compressor,
        messages,
        agent_id="agent-1",
        session_id="session-1",
        trigger="initial",
        metadata={"phase": "initial_context_compaction"},
    )

    assert compressed[0]["content"] == full_summary
    assert hook_calls[0][0] == HookEvent.PRE_COMPACTION
    assert hook_calls[0][1]["messages"] == messages
    assert hook_calls[0][1]["metadata"]["trigger"] == "initial"
    assert hook_calls[0][1]["metadata"]["phase"] == "initial_context_compaction"
    assert hook_calls[1][0] == HookEvent.POST_COMPACTION
    assert hook_calls[1][1]["metadata"]["trigger"] == "initial"
    assert hook_calls[1][1]["metadata"]["summary"] == full_summary
    assert hook_calls[1][1]["metadata"]["before_msgs"] == 2
    assert hook_calls[1][1]["metadata"]["after_msgs"] == 2


@pytest.mark.asyncio
async def test_mechanical_compaction_lifecycle_hooks_emit_pre_and_post(monkeypatch):
    from app.kernel.engine import _apply_mechanical_compaction_with_lifecycle_hooks
    from app.runtime.hooks import HookEvent

    hook_calls = []

    async def fake_emit_hook(event, **kwargs):
        hook_calls.append((event, kwargs))

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)

    messages = [
        {"role": "user", "content": "old"},
        {"role": "assistant", "content": "old result"},
        {"role": "user", "content": "latest"},
    ]

    compacted = await _apply_mechanical_compaction_with_lifecycle_hooks(
        messages,
        compact=lambda items: items[1:],
        agent_id="agent-1",
        session_id="session-1",
        trigger="prompt_too_long",
        metadata={"phase": "prompt_too_long_round_group_fallback"},
    )

    assert compacted == messages[1:]
    assert hook_calls[0][0] == HookEvent.PRE_COMPACTION
    assert hook_calls[0][1]["messages"] == messages
    assert hook_calls[0][1]["metadata"]["trigger"] == "prompt_too_long"
    assert hook_calls[0][1]["metadata"]["strategy"] == "mechanical"
    assert hook_calls[1][0] == HookEvent.POST_COMPACTION
    assert hook_calls[1][1]["metadata"]["before_msgs"] == 3
    assert hook_calls[1][1]["metadata"]["after_msgs"] == 2


def test_runtime_attachment_sections_report_tool_refresh_and_external_file_changes(tmp_path, monkeypatch):
    from app.kernel.engine import (
        _build_runtime_attachment_sections,
        _snapshot_session_file,
    )
    from app.runtime.session import SessionContext

    agent_id = uuid4()
    agent_root = tmp_path / str(agent_id)
    workspace_dir = agent_root / "workspace"
    workspace_dir.mkdir(parents=True)
    report = workspace_dir / "report.md"
    report.write_text("old content", encoding="utf-8")

    monkeypatch.setattr(
        "app.config.get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    session = SessionContext()
    session.track_discovered_tools(["web_search"])
    session.track_file_read("workspace/report.md", snapshot=_snapshot_session_file(agent_id, "workspace/report.md"))
    report.write_text("new content with different size", encoding="utf-8")

    text = "\n\n".join(_build_runtime_attachment_sections(agent_id, session))

    assert "Runtime Tool Refresh" in text
    assert "web_search" in text
    assert "Runtime File Change Notice" in text
    assert 'read_file("workspace/report.md")' in text


def test_build_persisted_memory_messages_includes_runtime_events():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import _build_persisted_memory_messages
    from app.runtime.session import SessionContext

    session = SessionContext()
    session.track_tool_outcome("web_search", "Found deployment guide and rollback notes")
    session.track_file_write("workspace/deploy-plan.md")
    session.track_external_ref("https://docs.example.com/deploy")
    session.track_pending_item("Verify rollback checklist before production deploy")

    request = InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "继续部署任务"}],
        agent_name="Ops Agent",
        role_description="deployment helper",
        session_context=session,
    )

    persisted = _build_persisted_memory_messages(request, "部署步骤已整理")
    contents = [msg.get("content", "") for msg in persisted if isinstance(msg.get("content"), str)]

    assert any("Runtime event: tool outcome web_search" in content for content in contents)
    assert any("Runtime event: wrote file workspace/deploy-plan.md" in content for content in contents)
    assert any("Runtime event: external reference https://docs.example.com/deploy" in content for content in contents)
    assert any(
        "Runtime event: pending work Verify rollback checklist before production deploy" in content
        for content in contents
    )


def test_build_persisted_memory_messages_preserves_every_pending_item():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import _build_persisted_memory_messages
    from app.runtime.session import SessionContext

    pending_items = [f"pending-item-{index}" for index in range(9)]
    session = SessionContext(pending_items=pending_items)
    request = InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "继续全部任务"}],
        agent_name="Ops Agent",
        role_description="deployment helper",
        session_context=session,
    )

    persisted = _build_persisted_memory_messages(request, "已继续")
    contents = [msg.get("content", "") for msg in persisted if isinstance(msg.get("content"), str)]

    for item in pending_items:
        assert f"Runtime event: pending work {item}" in contents


def test_should_expand_tools_does_not_expand_fs_read_skill_file():
    from app.kernel.engine import _should_expand_tools

    assert _should_expand_tools("fs_read", {"mode": "text", "path": "skills/research/SKILL.md"}) is False


@pytest.mark.asyncio
async def test_execute_tool_with_hooks_tracks_filesystem_facade_events(monkeypatch):
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import _execute_tool_with_hooks
    from app.runtime.session import SessionContext

    async def fake_emit_hook(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)

    session = SessionContext(session_id="s1")
    session.prompt_prefix = "cached-prefix"
    session.metadata["prompt_cache_key"] = "old-key"
    request = InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "inspect files"}],
        agent_name="Agent",
        role_description="role",
        session_context=session,
        memory_session_id="s1",
    )

    async def fake_execute_tool(_tool_name, _tool_args, _request, _emit_event):
        return "ok"

    async def emit_event(_event):
        return None

    await _execute_tool_with_hooks(
        execute_tool=fake_execute_tool,
        request=request,
        tool_name="fs_read",
        tool_args={"mode": "text", "path": "workspace/notes.md"},
        emit_event=emit_event,
    )
    await _execute_tool_with_hooks(
        execute_tool=fake_execute_tool,
        request=request,
        tool_name="fs_write",
        tool_args={"mode": "edit", "path": "soul.md"},
        emit_event=emit_event,
    )

    assert "workspace/notes.md" in session.recent_files
    assert "soul.md" in session.recent_writes
    assert session.prompt_prefix is None
    assert "prompt_cache_key" not in session.metadata
    assert session.metadata["prompt_cache_invalidated_reason"] == "fs_write:soul.md"
    cache_decisions = session.metadata["runtime_assembly_state"]["cache_decision_ledger"]
    assert "cache_decision_ledger" not in session.metadata
    assert cache_decisions[-1]["cache_surface"] == "prompt_prefix"
    assert cache_decisions[-1]["decision"] == "invalidated"
    assert cache_decisions[-1]["invalidation_reason"] == "fs_write:soul.md"


@pytest.mark.asyncio
async def test_execute_tool_with_hooks_tracks_office_created_document_as_session_artifact(monkeypatch):
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import _execute_tool_with_hooks
    from app.runtime.session import SessionContext

    async def fake_emit_hook(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)

    session = SessionContext(session_id="s-office-create")
    request = InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "create a doc"}],
        agent_name="Agent",
        role_description="role",
        session_context=session,
        memory_session_id="s-office-create",
    )

    async def fake_execute_tool(_tool_name, _tool_args, _request, _emit_event):
        return '{"ok": true, "path": "workspace/proposal.docx"}'

    async def emit_event(_event):
        return None

    await _execute_tool_with_hooks(
        execute_tool=fake_execute_tool,
        request=request,
        tool_name="office_document_create",
        tool_args={"path": "workspace/proposal.docx", "kind": "docx"},
        emit_event=emit_event,
    )

    assert session.recent_writes == ["workspace/proposal.docx"]
    assert session.recent_tool_outcomes[-1] == {
        "tool": "office_document_create",
        "summary": "Wrote workspace/proposal.docx",
    }


@pytest.mark.asyncio
async def test_execute_tool_with_hooks_tracks_office_apply_output_as_session_artifact(monkeypatch):
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import _execute_tool_with_hooks
    from app.runtime.session import SessionContext

    async def fake_emit_hook(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)

    session = SessionContext(session_id="s-office-apply")
    request = InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "update a doc"}],
        agent_name="Agent",
        role_description="role",
        session_context=session,
        memory_session_id="s-office-apply",
    )

    async def fake_execute_tool(_tool_name, _tool_args, _request, _emit_event):
        return '{"ok": true, "result": {"path": "workspace/proposal-v2.docx"}}'

    async def emit_event(_event):
        return None

    await _execute_tool_with_hooks(
        execute_tool=fake_execute_tool,
        request=request,
        tool_name="office_document_apply",
        tool_args={
            "path": "workspace/proposal.docx",
            "operations": [{"op": "replace_text", "text": "new"}],
            "output_path": "workspace/proposal-v2.docx",
        },
        emit_event=emit_event,
    )

    assert session.recent_writes == ["workspace/proposal-v2.docx"]
    assert session.recent_tool_outcomes[-1] == {
        "tool": "office_document_apply",
        "summary": "Wrote workspace/proposal-v2.docx",
    }


@pytest.mark.asyncio
async def test_execute_tool_with_hooks_tracks_structured_code_execution_artifacts(monkeypatch):
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import _execute_tool_with_hooks
    from app.runtime.session import SessionContext
    from app.tools.result_envelope import ToolContentEnvelope

    async def fake_emit_hook(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)
    session = SessionContext(session_id="session-code-artifacts")
    request = InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "build workbook"}],
        agent_name="Agent",
        role_description="role",
        session_context=session,
        memory_session_id="session-code-artifacts",
        agent_id=uuid4(),
    )

    async def fake_execute_tool(
        _tool_name,
        _tool_args,
        _request,
        _emit_event,
        *,
        trace_metadata_sink=None,
    ):
        _record_governed_tool_success(trace_metadata_sink)
        return ToolContentEnvelope(
            text="Workbook created",
            artifacts=(
                {"path": "workspace/report.xlsx", "source": "run_command", "action": "created"},
                {"path": "workspace/chart.png", "source": "run_command", "action": "created"},
            ),
        )

    async def emit_event(_event):
        return None

    result, _args, executed = await _execute_tool_with_hooks(
        execute_tool=fake_execute_tool,
        request=request,
        tool_name="run_command",
        tool_args={"command": "python build.py"},
        emit_event=emit_event,
    )

    assert executed is True
    assert result == "Workbook created"
    assert session.current_turn_writes == ["workspace/report.xlsx", "workspace/chart.png"]


@pytest.mark.asyncio
async def test_execute_tool_with_hooks_consumes_lock_captured_write_state(monkeypatch):
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import _execute_tool_with_hooks
    from app.runtime.session import SessionContext

    async def fake_emit_hook(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)
    session = SessionContext(session_id="session-exact-write-state")
    request = InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "write report"}],
        agent_name="Agent",
        role_description="role",
        session_context=session,
        memory_session_id="session-exact-write-state",
        agent_id=uuid4(),
    )
    exact_state = {
        "path": "workspace/report.md",
        "exists": True,
        "sha256": "e" * 64,
        "size": 11,
    }

    async def fake_execute_tool(
        _tool_name,
        _tool_args,
        _request,
        _emit_event,
        *,
        trace_metadata_sink=None,
    ):
        trace_metadata_sink["workspace_mutation_evidence_captured"] = True
        trace_metadata_sink["workspace_mutation_states"] = {"workspace/report.md": exact_state}
        trace_metadata_sink["workspace_mutation_state_errors"] = {}
        trace_metadata_sink["workspace_mutation_lineage"] = [
            {
                "path": "workspace/report.md",
                "before_state": {
                    "path": "workspace/report.md",
                    "exists": False,
                    "sha256": None,
                    "size": 0,
                },
                "after_state": exact_state,
            }
        ]
        return "OK"

    async def emit_event(_event):
        return None

    await _execute_tool_with_hooks(
        execute_tool=fake_execute_tool,
        request=request,
        tool_name="write_file",
        tool_args={"path": "workspace/report.md", "content": "owned state"},
        emit_event=emit_event,
    )

    assert session.current_turn_writes == ["workspace/report.md"]
    assert session.current_turn_write_snapshots["workspace/report.md"] == exact_state
    assert session.current_turn_write_lineage[0]["after_state"] == exact_state


@pytest.mark.asyncio
async def test_execute_tool_with_hooks_does_not_claim_failed_workspace_write(monkeypatch):
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import _execute_tool_with_hooks
    from app.runtime.session import SessionContext

    async def fake_emit_hook(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)
    session = SessionContext(session_id="session-failed-write")
    request = InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "write report"}],
        agent_name="Agent",
        role_description="role",
        session_context=session,
        memory_session_id="session-failed-write",
        agent_id=uuid4(),
    )

    async def fake_execute_tool(
        _tool_name,
        _tool_args,
        _request,
        _emit_event,
        *,
        trace_metadata_sink=None,
    ):
        trace_metadata_sink["workspace_mutation_evidence_captured"] = True
        trace_metadata_sink["workspace_mutation_states"] = {}
        trace_metadata_sink["workspace_mutation_state_errors"] = {}
        trace_metadata_sink["workspace_mutation_lineage"] = []
        return '❌ denied\n<tool_error>{"error_class":"denied"}</tool_error>'

    async def emit_event(_event):
        return None

    await _execute_tool_with_hooks(
        execute_tool=fake_execute_tool,
        request=request,
        tool_name="write_file",
        tool_args={"path": "workspace/report.md", "content": "not written"},
        emit_event=emit_event,
    )

    assert session.current_turn_writes == []
    assert session.current_turn_write_snapshots == {}


@pytest.mark.asyncio
async def test_hook_emitter_consumes_post_tool_output_rewrite(monkeypatch):
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import _execute_tool_with_hooks
    from app.runtime.hooks import HookEvent, HookResult
    from app.runtime.session import SessionContext

    async def fake_emit_hook(event, **_kwargs):
        if event == HookEvent.POST_TOOL_USE:
            return HookResult(output_rewrite="[redacted by hook]")
        return None

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)

    request = InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "call tool"}],
        agent_name="Agent",
        role_description="role",
        session_context=SessionContext(session_id="s-rewrite"),
        memory_session_id="s-rewrite",
    )

    async def fake_execute_tool(
        _tool_name,
        _tool_args,
        _request,
        _emit_event,
        *,
        trace_metadata_sink=None,
    ):
        _record_governed_tool_success(trace_metadata_sink)
        return "raw secret output"

    async def emit_event(_event):
        return None

    result, effective_args, executed = await _execute_tool_with_hooks(
        execute_tool=fake_execute_tool,
        request=request,
        tool_name="mcp__server__secret_tool",
        tool_args={"id": "secret"},
        emit_event=emit_event,
    )

    assert result == "[redacted by hook]"
    assert effective_args == {"id": "secret"}
    assert executed is True


@pytest.mark.asyncio
async def test_post_tool_hook_and_recovery_tracking_receive_full_semantic_evidence(monkeypatch):
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import _execute_tool_with_hooks
    from app.runtime.hooks import HookEvent
    from app.runtime.session import SessionContext

    captured: dict = {}

    async def fake_emit_hook(event, **kwargs):
        if event == HookEvent.POST_TOOL_USE:
            captured.update(kwargs)
        return None

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)
    tail = "DECISIVE_TOOL_RESULT_TAIL"
    oldest = "OLDEST_AUTHORIZED_MESSAGE"
    raw_result = ("tool evidence " * 300) + tail
    session = SessionContext(session_id="s-full-hook")
    request = InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": oldest}]
        + [{"role": "assistant", "content": f"message-{index}"} for index in range(14)],
        agent_name="Agent",
        role_description="role",
        session_context=session,
        memory_session_id="s-full-hook",
    )

    async def fake_execute_tool(
        _tool_name,
        _tool_args,
        _request,
        _emit_event,
        *,
        trace_metadata_sink=None,
    ):
        _record_governed_tool_success(trace_metadata_sink)
        return raw_result

    async def emit_event(_event):
        return None

    result, _effective_args, executed = await _execute_tool_with_hooks(
        execute_tool=fake_execute_tool,
        request=request,
        tool_name="execute_code",
        tool_args={"code": "print('ok')"},
        emit_event=emit_event,
    )

    assert executed is True
    assert result == raw_result
    assert tail in captured["tool_result"]
    assert captured["messages"][0]["content"] == oldest
    assert tail in session.recent_tool_outcomes[-1]["summary"]


@pytest.mark.asyncio
async def test_tool_failure_preserves_full_error_for_model_hook_and_span(monkeypatch):
    from app.kernel.contracts import InvocationRequest, RuntimeConfig
    from app.kernel.engine import _execute_tool_with_hooks
    from app.runtime.hooks import HookEvent
    from app.runtime.session import SessionContext

    captured_hook: dict = {}
    captured_spans: list[dict] = []
    error_tail = "TOOL_ERROR_DECISIVE_TAIL"
    full_error = "e" * 900 + error_tail

    async def fake_emit_hook(event, **kwargs):
        if event == HookEvent.POST_TOOL_FAILURE:
            captured_hook.update(kwargs)
        return None

    async def failing_tool(*_args, **_kwargs):
        raise RuntimeError(full_error)

    async def record_span(**kwargs):
        captured_spans.append(kwargs)

    async def emit_event(_event):
        return None

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)
    request = InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "call tool"}],
        agent_name="Agent",
        role_description="role",
        agent_id=uuid4(),
        session_context=SessionContext(session_id="s-tool-error"),
        memory_session_id="s-tool-error",
    )

    result, _effective_args, executed = await _execute_tool_with_hooks(
        execute_tool=failing_tool,
        request=request,
        runtime_config=RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=3),
        tool_name="read_file",
        tool_args={"path": "missing"},
        emit_event=emit_event,
        record_span=record_span,
    )

    assert executed is False
    assert error_tail in result
    assert error_tail in captured_hook["error"]
    assert error_tail in captured_spans[-1]["metadata"]["error"]


@pytest.mark.asyncio
async def test_execute_tool_with_hooks_records_lifecycle_records_in_tool_span():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import _execute_tool_with_hooks
    from app.runtime.hooks import HookEvent, HookResult, hook_registry
    from app.runtime.session import SessionContext

    hook_registry.clear()

    def rewrite(_ctx):
        return HookResult(modified_args={"query": "github trending"})

    hook_registry.register(HookEvent.PRE_TOOL_USE, rewrite, key="skill:session-1:research:pre_tool_use")
    spans = []

    request = InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "call tool"}],
        agent_name="Agent",
        role_description="role",
        session_context=SessionContext(session_id="session-1"),
        memory_session_id="session-1",
    )

    async def fake_execute_tool(
        _tool_name,
        tool_args,
        _request,
        _emit_event,
        *,
        trace_metadata_sink=None,
    ):
        assert tool_args == {"query": "github trending"}
        _record_governed_tool_success(trace_metadata_sink)
        return "search result"

    async def emit_event(_event):
        return None

    async def record_span(**kwargs):
        spans.append(kwargs)
        return {"id": "span-1"}

    try:
        result, effective_args, executed = await _execute_tool_with_hooks(
            execute_tool=fake_execute_tool,
            request=request,
            tool_name="web_search",
            tool_args={"query": "github"},
            emit_event=emit_event,
            record_span=record_span,
        )
    finally:
        hook_registry.clear()

    assert result == "search result"
    assert effective_args == {"query": "github trending"}
    assert executed is True
    lifecycle_records = spans[-1]["metadata"]["hook_lifecycle_records"]
    assert lifecycle_records[0]["event"] == "pre_tool_use"
    assert lifecycle_records[0]["decision"] == "rewrite_args"
    assert lifecycle_records[0]["source"] == "skill:session-1:research:pre_tool_use"


@pytest.mark.asyncio
async def test_execute_tool_with_hooks_executes_pending_skill_fork_handoff(monkeypatch):
    from app.kernel.contracts import InvocationRequest, RuntimeConfig
    from app.kernel.engine import _execute_tool_with_hooks
    from app.runtime.session import SessionContext

    async def fake_emit_hook(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)

    session = SessionContext(
        session_id="session-skill-fork",
        metadata={
            "pending_skill_handoffs": [
                {
                    "skill": "Research",
                    "skill_slug": "research",
                    "source": "skills/research/SKILL.md",
                    "execution_tool": "spawn_subagent",
                    "tool_arguments": {
                        "prompt": "Use the loaded skill `Research`.",
                        "description": "Skill fork worker for Research",
                        "skill": "Research",
                        "skill_source": "skills/research/SKILL.md",
                        "permission_profile": {
                            "mode": "auto",
                            "allowed_tools": ["web_search", "read_file"],
                        },
                    },
                    "permission_profile": {
                        "mode": "auto",
                        "allowed_tools": ["web_search", "read_file"],
                    },
                }
            ]
        },
    )
    request = InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "load research"}],
        agent_name="Agent",
        role_description="role",
        session_context=session,
        memory_session_id="session-skill-fork",
    )
    calls: list[dict[str, object]] = []

    async def fake_execute_tool(
        _tool_name,
        _tool_args,
        _request,
        _emit_event,
        *,
        tool_call_id=None,
        trace_metadata_sink=None,
    ):
        _record_governed_tool_success(trace_metadata_sink)
        calls.append({"tool_name": _tool_name, "args": dict(_tool_args), "tool_call_id": tool_call_id})
        if _tool_name == "load_skill":
            return "Loaded Research"
        if _tool_name == "spawn_subagent":
            return '{"ok": true, "child_session_id": "child-1"}'
        raise AssertionError(f"unexpected tool {_tool_name}")

    async def emit_event(_event):
        return None

    result, effective_args, executed = await _execute_tool_with_hooks(
        execute_tool=fake_execute_tool,
        request=request,
        runtime_config=RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=3),
        tool_name="load_skill",
        tool_args={"name": "Research"},
        tool_call_id="call-load-skill",
        emit_event=emit_event,
    )

    assert executed is True
    assert effective_args == {"name": "Research"}
    assert [call["tool_name"] for call in calls] == ["load_skill", "spawn_subagent"]
    assert calls[1]["tool_call_id"] == "call-load-skill:skill:research"
    assert calls[1]["args"]["permission_profile"]["allowed_tools"] == ["web_search", "read_file"]
    assert calls[1]["args"]["run_in_background"] is True
    assert calls[1]["args"]["skill_source"] == "skills/research/SKILL.md"
    assert "Skill fork worker `Research` executed through `spawn_subagent`." in result
    assert "child-1" in result
    assert "pending_skill_handoffs" not in session.metadata
    assert session.metadata["executed_skill_handoffs"][0]["skill_slug"] == "research"


@pytest.mark.asyncio
async def test_load_skill_frontmatter_fork_executes_in_same_tool_call(tmp_path: Path, monkeypatch):
    from app.kernel.contracts import InvocationRequest, RuntimeConfig
    from app.kernel.engine import _execute_tool_with_hooks
    from app.runtime.session import SessionContext

    workspace = tmp_path / "agent"
    skill_dir = workspace / "skills" / "research"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "\n".join(
            [
                "---",
                "name: Research",
                "description: Research with a scoped worker.",
                "allowed-tools:",
                "  - web_search",
                "  - read_file",
                "metadata:",
                "  hive:",
                "    context: fork",
                "    agent: researcher",
                "---",
                "# Research",
                "Use a worker when the task needs isolated exploration.",
            ]
        ),
        encoding="utf-8",
    )

    async def fake_emit_hook(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)
    monkeypatch.setattr("app.kernel.engine._agent_workspace_root", lambda _agent_id: workspace)

    session = SessionContext(session_id="session-skill-fork", metadata={})
    request = InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "load research"}],
        agent_name="Agent",
        role_description="role",
        agent_id=uuid4(),
        session_context=session,
        memory_session_id="session-skill-fork",
    )
    calls: list[dict[str, object]] = []

    async def fake_execute_tool(
        _tool_name,
        _tool_args,
        _request,
        _emit_event,
        *,
        tool_call_id=None,
        trace_metadata_sink=None,
    ):
        _record_governed_tool_success(trace_metadata_sink)
        calls.append({"tool_name": _tool_name, "args": dict(_tool_args), "tool_call_id": tool_call_id})
        if _tool_name == "load_skill":
            return "Loaded Research"
        if _tool_name == "spawn_subagent":
            return '{"ok": true, "child_session_id": "child-research"}'
        raise AssertionError(f"unexpected tool {_tool_name}")

    async def emit_event(_event):
        return None

    result, _effective_args, executed = await _execute_tool_with_hooks(
        execute_tool=fake_execute_tool,
        request=request,
        runtime_config=RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=3),
        tool_name="load_skill",
        tool_args={"name": "research"},
        tool_call_id="call-load-skill",
        emit_event=emit_event,
    )

    assert executed is True
    assert [call["tool_name"] for call in calls] == ["load_skill", "spawn_subagent"]
    assert calls[1]["tool_call_id"] == "call-load-skill:skill:research"
    assert calls[1]["args"]["permission_profile"]["allowed_tools"] == ["web_search", "read_file"]
    assert calls[1]["args"]["run_in_background"] is True
    assert "child-research" in result
    assert "pending_skill_handoffs" not in session.metadata
    assert session.metadata["executed_skill_handoffs"][0]["skill_slug"] == "research"


@pytest.mark.asyncio
async def test_recovered_pending_tool_frame_replays_read_only_tool_through_governed_runtime(monkeypatch):
    from app.kernel.contracts import InvocationRequest, RuntimeConfig
    from app.kernel.engine import _execute_recovered_pending_tool_frames
    from app.runtime.session import SessionContext

    async def fake_emit_hook(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)

    session = SessionContext(
        session_id="session-recover",
        metadata={
            "recovered_pending_tool_frames": [
                {
                    "tool_call_id": "call-read",
                    "tool_name": "read_file",
                    "arguments": {"path": "workspace/report.md"},
                    "status": "running",
                }
            ]
        },
    )
    request = InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "continue"}],
        agent_name="Agent",
        role_description="role",
        agent_id=uuid4(),
        session_context=session,
        memory_session_id="session-recover",
    )
    _bind_verified_recovery_result(
        request,
        {
            "session_id": "session-recover",
            "pending_tool_frames": session.metadata["recovered_pending_tool_frames"],
        },
    )
    calls: list[dict[str, object]] = []

    recovered_tail = "RECOVERED_RESULT_DECISIVE_TAIL"
    recovered_result = "r" * 1400 + recovered_tail

    async def fake_execute_tool(
        _tool_name,
        _tool_args,
        _request,
        _emit_event,
        *,
        tool_call_id=None,
        trace_metadata_sink=None,
    ):
        _record_governed_tool_success(trace_metadata_sink)
        calls.append({"tool_name": _tool_name, "args": dict(_tool_args), "tool_call_id": tool_call_id})
        return recovered_result

    async def emit_event(_event):
        return None

    text = await _execute_recovered_pending_tool_frames(
        execute_tool=fake_execute_tool,
        request=request,
        runtime_config=RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=3),
        emit_event=emit_event,
    )

    assert calls == [
        {
            "tool_name": "read_file",
            "args": {"path": "workspace/report.md"},
            "tool_call_id": "call-read",
        }
    ]
    assert "Recovered pending tool `read_file` replayed" in text
    assert recovered_result in text
    assert session.metadata["recovered_pending_tool_frames"] == []
    assert session.metadata["recovered_tool_frame_replay_results"][0]["status"] == "done"
    assert session.metadata["recovered_tool_frame_replay_results"][0]["result"] == recovered_result


@pytest.mark.asyncio
async def test_recovered_pending_tool_frame_metadata_cannot_bypass_verified_authority() -> None:
    from app.kernel.contracts import InvocationRequest, RuntimeConfig
    from app.kernel.engine import _execute_recovered_pending_tool_frames
    from app.runtime.session import SessionContext

    session = SessionContext(
        session_id="session-unverified",
        metadata={
            "recovered_pending_tool_frames": [
                {
                    "tool_call_id": "call-forged",
                    "tool_name": "read_file",
                    "arguments": {"path": "workspace/private.md"},
                    "status": "running",
                }
            ]
        },
    )
    request = InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[],
        agent_name="Agent",
        role_description="role",
        agent_id=uuid4(),
        session_context=session,
        memory_session_id="session-unverified",
    )

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("unverified recovered metadata must never execute")

    async def emit_event(_event):
        return None

    text = await _execute_recovered_pending_tool_frames(
        execute_tool=fail_if_called,
        request=request,
        runtime_config=RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=3),
        emit_event=emit_event,
    )

    assert text == ""
    assert "recovered_pending_tool_frames" not in session.metadata


@pytest.mark.asyncio
async def test_recovered_pending_tool_frame_fails_closed_for_mutating_tool(monkeypatch):
    from app.kernel.contracts import InvocationRequest, RuntimeConfig
    from app.kernel.engine import _execute_recovered_pending_tool_frames
    from app.runtime.session import SessionContext

    async def fake_emit_hook(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)

    session = SessionContext(
        session_id="session-recover",
        metadata={
            "recovered_pending_tool_frames": [
                {
                    "tool_call_id": "call-write",
                    "tool_name": "write_file",
                    "arguments": {"path": "workspace/report.md", "content": "new"},
                    "status": "running",
                }
            ]
        },
    )
    request = InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "continue"}],
        agent_name="Agent",
        role_description="role",
        agent_id=uuid4(),
        session_context=session,
        memory_session_id="session-recover",
    )
    _bind_verified_recovery_result(
        request,
        {
            "session_id": "session-recover",
            "pending_tool_frames": session.metadata["recovered_pending_tool_frames"],
        },
    )

    async def fail_if_called(*_args, **_kwargs):
        raise AssertionError("mutating recovered frame must not auto-replay")

    async def emit_event(_event):
        return None

    text = await _execute_recovered_pending_tool_frames(
        execute_tool=fail_if_called,
        request=request,
        runtime_config=RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=3),
        emit_event=emit_event,
    )

    assert "requires reconciliation" in text
    assert session.metadata["recovered_pending_tool_frames"] == []
    reconciliation = session.metadata["recovered_tool_frame_reconciliation"][0]
    assert reconciliation["tool_name"] == "write_file"
    assert reconciliation["status"] == "needs_reconciliation"
    assert reconciliation["reason"] == "recovered_tool_frame_not_replay_safe"


def test_skill_handoff_execution_preserves_full_result_in_recovery_metadata() -> None:
    from app.services.skill_execution_adapter import record_skill_handoff_execution

    tail = "HANDOFF_RESULT_DECISIVE_TAIL"
    result = "h" * 2500 + tail
    metadata = {"pending_skill_handoffs": [{"skill": "Research", "skill_slug": "research"}]}

    record_skill_handoff_execution(
        metadata,
        {"skill": "Research", "skill_slug": "research", "source": "skills/research/SKILL.md"},
        tool_call_id="call-1",
        result=result,
    )

    assert metadata["executed_skill_handoffs"][0]["result"] == result


@pytest.mark.asyncio
async def test_recovery_manifest_reloads_recovered_mcp_deferred_tool_schema(tmp_path: Path, monkeypatch):
    from app.kernel import AgentKernel, InvocationRequest, KernelDependencies, RuntimeConfig
    from app.kernel.engine import ToolExpansionResult
    from app.runtime.recovery_manifest_store import (
        persist_recovery_manifest,
        resolve_recovery_authority,
    )
    from app.runtime.session import SessionContext

    agent_id = uuid4()
    workspace = tmp_path / str(agent_id)
    monkeypatch.setattr("app.kernel.engine._agent_workspace_root", lambda _agent_id: workspace)
    monkeypatch.setattr("app.runtime.recovery_manifest_store._data_root", lambda _data_root: tmp_path)

    core_tool = {
        "type": "function",
        "function": {"name": "tool_search", "description": "", "parameters": {"type": "object"}},
    }
    mcp_tool = {
        "type": "function",
        "function": {"name": "mcp__docs__search", "description": "", "parameters": {"type": "object"}},
    }
    expansion_queries: list[str] = []
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="done",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 5},
                finish_reason="stop",
            )
        ]
    )

    async def resolve_tool_expansion(request, tool_name, args):
        expansion_queries.append(str(args.get("query") or ""))
        assert tool_name == "tool_search"
        if args.get("query") == "select:mcp__docs__search":
            request.session_context.track_discovered_tools(["mcp__docs__search"])
            return ToolExpansionResult(
                tools=[core_tool, mcp_tool],
                active_tool_groups=[
                    {
                        "name": "mcp_runtime",
                        "summary": "Recovered MCP runtime tools",
                        "tools": ["mcp__docs__search"],
                    }
                ],
            )
        return None

    tenant_id = uuid4()
    runtime_config = RuntimeConfig(
        tenant_id=tenant_id,
        max_tool_rounds=3,
        quota_message=None,
    )
    user_id = uuid4()
    session = SessionContext(
        session_id="session-mcp-recover",
        metadata={"runtime_task_id": "runtime-mcp-recover"},
    )
    request = InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="test", base_url=None),
        messages=[{"role": "user", "content": "continue"}],
        agent_name="Agent",
        role_description="role",
        agent_id=agent_id,
        user_id=user_id,
        session_context=session,
        memory_session_id="session-mcp-recover",
        core_tools_only=True,
        expand_tools=True,
    )
    authority = resolve_recovery_authority(request, runtime_config).frame
    assert authority is not None
    persisted_session = SessionContext(
        session_id="session-mcp-recover",
        active_tool_groups=[{"name": "mcp_runtime", "summary": "", "tools": []}],
        metadata={"mcp_assignments": [{"server": "docs", "tools": ["search"]}]},
    )
    persisted_session.track_discovered_tools(["mcp__docs__search"])
    assert persist_recovery_manifest(authority, persisted_session, data_root=tmp_path).status == "written"

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_args, **_kwargs: runtime_config,
            resolve_current_user_name=lambda *_args, **_kwargs: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [core_tool],
            resolve_tool_expansion=resolve_tool_expansion,
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: "",
            persist_memory=lambda **_kwargs: None,
            record_token_usage=lambda *_args, **_kwargs: None,
            get_max_tokens=lambda _provider, _model, override=None: override or 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )
    result = await kernel.handle(request)

    assert result.content == "done"
    assert "select:mcp__docs__search" in expansion_queries
    sent_tool_names = [tool["function"]["name"] for tool in fake_client.calls[0]["tools"]]
    assert sent_tool_names == ["tool_search", "mcp__docs__search"]
    assert session.metadata["mcp_server_refs"] == ["docs"]
    assert session.metadata["recovered_from_manifest"] is True


@pytest.mark.asyncio
async def test_execute_tool_with_hooks_writes_trace_metadata_sink_to_span(monkeypatch):
    from app.kernel.contracts import InvocationRequest, RuntimeConfig
    from app.kernel.engine import _execute_tool_with_hooks
    from app.runtime.session import SessionContext

    async def fake_emit_hook(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)
    request = InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "send update"}],
        agent_name="Agent",
        role_description="role",
        agent_id=uuid4(),
        session_context=SessionContext(session_id="session-truth"),
        memory_session_id="session-truth",
    )
    spans: list[dict] = []

    async def fake_execute_tool(
        _tool_name,
        _tool_args,
        _request,
        _emit_event,
        *,
        tool_call_id=None,
        trace_metadata_sink=None,
    ):
        assert tool_call_id == "call-send"
        assert isinstance(trace_metadata_sink, dict)
        _record_governed_tool_success(trace_metadata_sink)
        trace_metadata_sink["effective_arguments"] = {
            "to": "user@example.com",
            "body": "hi",
            "_requester_user_id": "runtime-user",
        }
        trace_metadata_sink["evidence_refs"] = ["truth://policy/email-confirmation"]
        trace_metadata_sink["truth_evidence"] = [{"evidence_id": "truth://policy/email-confirmation"}]
        return "sent"

    async def record_span(**kwargs):
        spans.append(kwargs)

    async def emit_event(_event):
        return None

    side_effects: dict = {}
    result, _effective_args, executed = await _execute_tool_with_hooks(
        execute_tool=fake_execute_tool,
        request=request,
        runtime_config=RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=3),
        tool_name="send_email",
        tool_args={"to": "user@example.com", "body": "hi"},
        tool_call_id="call-send",
        emit_event=emit_event,
        record_span=record_span,
        side_effect_sink=side_effects,
    )

    assert result == "sent"
    assert executed is True
    assert spans[-1]["metadata"]["evidence_refs"] == ["truth://policy/email-confirmation"]
    assert spans[-1]["metadata"]["truth_evidence"] == [{"evidence_id": "truth://policy/email-confirmation"}]
    assert side_effects["tool_execution_evidence"]["effective_arguments"] == {
        "to": "user@example.com",
        "body": "hi",
        "_requester_user_id": "runtime-user",
    }


@pytest.mark.asyncio
async def test_execute_tool_with_hooks_records_tool_result_ledger(monkeypatch):
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import _execute_tool_with_hooks
    from app.runtime.session import SessionContext

    async def fake_emit_hook(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)
    session = SessionContext(session_id="session-ledger")
    session.metadata["turn_id"] = "turn-tool-success"
    session.metadata["intent_id"] = "intent-tool-success"
    session.metadata["runtime_assembly_state"] = {
        "available_deferred_tool_candidates": [
            {
                "name": "web_search",
                "candidate_ref": {"candidate_id": "tool_schema:web_search:v1/test", "kind": "tool_schema"},
            }
        ]
    }
    request = InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "search"}],
        agent_name="Agent",
        role_description="role",
        agent_id=uuid4(),
        session_context=session,
        memory_session_id="session-ledger",
    )
    spans: list[dict] = []

    async def fake_execute_tool(_tool_name, _tool_args, _request, _emit_event, *, trace_metadata_sink=None):
        _record_governed_tool_success(trace_metadata_sink)
        trace_metadata_sink["evidence_refs"] = ["truth://search/result"]
        return "result text"

    async def record_span(**kwargs):
        spans.append(kwargs)

    async def emit_event(_event):
        return None

    result, _effective_args, executed = await _execute_tool_with_hooks(
        execute_tool=fake_execute_tool,
        request=request,
        tool_name="web_search",
        tool_args={"query": "memory systems"},
        emit_event=emit_event,
        record_span=record_span,
    )

    assert result == "result text"
    assert executed is True
    ledger_entry = spans[-1]["metadata"]["tool_result_ledger_entry"]
    assert ledger_entry["result_kind"] == "evidence"
    assert ledger_entry["context_effect"] == "external_reference"
    assert ledger_entry["source_refs"] == ["truth://search/result", "query:memory systems"]
    assert session.metadata["runtime_assembly_state"]["tool_result_ledger"][-1] == ledger_entry
    activation_event = ledger_entry["followup_activation_events"][0]
    assert activation_event["event_type"] == "tool_success"
    assert activation_event["turn_id"] == "turn-tool-success"
    assert activation_event["intent_id"] == "intent-tool-success"
    assert activation_event["candidate_ref"]["candidate_id"] == "tool_schema:web_search:v1/test"
    assert activation_event["feedback"]["signal"] == "tool_success"
    assert activation_event["feedback"]["credit"] > 0
    assert session.metadata["runtime_assembly_state"]["activation_events"][-1] == activation_event
    assert "activation_events" not in session.metadata


@pytest.mark.asyncio
async def test_execute_tool_with_hooks_records_runtime_failure_policy_on_error(monkeypatch):
    from app.kernel.contracts import InvocationRequest, RuntimeConfig
    from app.kernel.engine import _execute_tool_with_hooks
    from app.runtime.session import SessionContext

    async def fake_emit_hook(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)
    session = SessionContext(session_id="session-failure-policy")
    session.metadata["turn_id"] = "turn-tool-failure"
    session.metadata["intent_id"] = "intent-tool-failure"
    request = InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "send"}],
        agent_name="Agent",
        role_description="role",
        agent_id=uuid4(),
        session_context=session,
        memory_session_id="session-failure-policy",
    )
    spans: list[dict] = []

    async def fake_execute_tool(*_args, **_kwargs):
        raise RuntimeError("network timeout")

    async def record_span(**kwargs):
        spans.append(kwargs)

    async def emit_event(_event):
        return None

    result, _effective_args, executed = await _execute_tool_with_hooks(
        execute_tool=fake_execute_tool,
        request=request,
        runtime_config=RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=3),
        tool_name="send_email",
        tool_args={"to": "user@example.com"},
        emit_event=emit_event,
        record_span=record_span,
    )

    assert executed is False
    assert result.startswith("[Tool execution error] RuntimeError: network timeout")
    policy = spans[-1]["metadata"]["runtime_failure_policy"]
    assert policy["failure_kind"] == "tool_failure"
    assert policy["safe_to_continue"] is True
    ledger_policy = session.metadata["runtime_assembly_state"]["tool_result_ledger"][-1]["side_effects"][
        "runtime_failure_policy"
    ]
    assert ledger_policy == policy
    activation_event = session.metadata["runtime_assembly_state"]["tool_result_ledger"][-1][
        "followup_activation_events"
    ][0]
    assert activation_event["event_type"] == "tool_failure"
    assert activation_event["turn_id"] == "turn-tool-failure"
    assert activation_event["candidate_ref"]["candidate_id"].startswith("tool_schema:send_email:")
    assert activation_event["feedback"]["signal"] == "tool_failure"
    assert activation_event["feedback"]["credit"] < 0
    assert session.metadata["runtime_assembly_state"]["activation_events"][-1] == activation_event
    assert "tool_result_ledger" not in session.metadata
    assert "activation_events" not in session.metadata


@pytest.mark.asyncio
async def test_execute_tool_with_hooks_records_runtime_failure_policy_on_hook_block(monkeypatch):
    from app.kernel.contracts import InvocationRequest, RuntimeConfig
    from app.kernel.engine import _execute_tool_with_hooks
    from app.runtime.hooks import HookResult
    from app.runtime.session import SessionContext

    async def fake_emit_hook(*_args, **_kwargs):
        return HookResult(block=True, reason="external send requires confirmation")

    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)
    session = SessionContext(session_id="session-hook-failure-policy")
    request = InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "send"}],
        agent_name="Agent",
        role_description="role",
        agent_id=uuid4(),
        session_context=session,
        memory_session_id="session-hook-failure-policy",
    )
    spans: list[dict] = []

    async def fail_execute_tool(*_args, **_kwargs):
        raise AssertionError("blocked tools must not execute")

    async def record_span(**kwargs):
        spans.append(kwargs)

    async def emit_event(_event):
        return None

    result, _effective_args, executed = await _execute_tool_with_hooks(
        execute_tool=fail_execute_tool,
        request=request,
        runtime_config=RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=3),
        tool_name="send_email",
        tool_args={"to": "user@example.com"},
        emit_event=emit_event,
        record_span=record_span,
    )

    assert executed is False
    assert result == "Blocked by hook: external send requires confirmation"
    policy = spans[-1]["metadata"]["runtime_failure_policy"]
    assert policy["failure_kind"] == "hook_block"
    assert policy["requires_user"] is True
    assert policy["safe_to_continue"] is False
    assert (
        session.metadata["runtime_assembly_state"]["tool_result_ledger"][-1]["side_effects"]["runtime_failure_policy"]
        == policy
    )


@pytest.mark.asyncio
async def test_agent_kernel_handles_tool_round_and_collects_parts():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

    agent_id = uuid4()
    user_id = uuid4()
    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )

    tool_load_calls: list[bool] = []
    persisted_payloads: list[dict] = []

    async def resolve_runtime_config(_agent_id):
        return RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=5, quota_message=None)

    async def resolve_current_user_name(_user_id):
        return "Rocky"

    async def build_system_prompt(request, tenant_id, memory_context, current_user_name):
        assert tenant_id is not None
        assert current_user_name == "Rocky"
        assert memory_context == ""
        return f"PROMPT::{request.agent_name}"

    async def resolve_memory_context(request, tenant_id):
        assert tenant_id is not None
        return ""

    async def get_tools(_agent_id, core_only):
        tool_load_calls.append(core_only)
        name = "core_tool" if core_only else "expanded_tool"
        return [{"type": "function", "function": {"name": name, "description": "", "parameters": {"type": "object"}}}]

    async def maybe_compress_messages(messages, **kwargs):
        return messages

    async def execute_tool(tool_name, args, request, emit_event):
        del request, emit_event
        assert tool_name == "read_file"
        assert args == {"path": "skills/web-research/SKILL.md"}
        return "SKILL_CONTENT"

    async def persist_memory(**kwargs):
        persisted_payloads.append(kwargs)

    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "function": {"name": "read_file", "arguments": '{"path":"skills/web-research/SKILL.md"}'},
                    }
                ],
                reasoning_content="reasoning",
                usage={"total_tokens": 10},
            ),
            SimpleNamespace(
                content="final answer",
                tool_calls=[],
                reasoning_content="final reasoning",
                usage={"total_tokens": 8},
            ),
        ]
    )

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=resolve_runtime_config,
            resolve_current_user_name=resolve_current_user_name,
            build_system_prompt=build_system_prompt,
            resolve_memory_context=resolve_memory_context,
            get_tools=get_tools,
            maybe_compress_messages=maybe_compress_messages,
            create_client=lambda model: fake_client,
            execute_tool=execute_tool,
            persist_memory=persist_memory,
            record_token_usage=lambda *args, **kwargs: None,
            get_max_tokens=lambda *args, **kwargs: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "帮我做调研"}],
            agent_name="Researcher",
            role_description="Research agent",
            agent_id=agent_id,
            user_id=user_id,
        )
    )

    assert result.content == "final answer"
    assert tool_load_calls == [True]
    assert fake_client.calls[0]["tools"][0]["function"]["name"] == "core_tool"
    assert fake_client.calls[1]["tools"][0]["function"]["name"] == "core_tool"
    assert result.parts == [
        {
            "type": "tool_call",
            "name": "read_file",
            "args": {"path": "skills/web-research/SKILL.md"},
            "status": "done",
            "result": "SKILL_CONTENT",
            "reasoning": "reasoning",
        },
        {"type": "reasoning", "text": "final reasoning"},
        {"type": "text", "text": "final answer"},
    ]
    assert persisted_payloads


@pytest.mark.asyncio
async def test_kernel_round_trips_reasoning_signature_after_tool_call() -> None:
    from app.kernel import AgentKernel, InvocationRequest, KernelDependencies, RuntimeConfig

    model = SimpleNamespace(provider="anthropic", model="claude-sonnet-4", api_key="key", base_url=None)
    agent_id = uuid4()
    user_id = uuid4()
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "function": {"name": "read_file", "arguments": '{"path":"notes.md"}'},
                    }
                ],
                reasoning_content="tool thinking",
                reasoning_signature="sig-tool-turn",
                usage={"total_tokens": 10},
            ),
            SimpleNamespace(
                content="done",
                tool_calls=[],
                reasoning_content="final thinking",
                reasoning_signature="sig-final",
                usage={"total_tokens": 5},
                finish_reason="stop",
            ),
        ]
    )

    async def execute_tool(tool_name, args, request, emit_event):
        del request, emit_event
        assert tool_name == "read_file"
        assert args == {"path": "notes.md"}
        return "file contents"

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_args, **_kwargs: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=3),
            resolve_current_user_name=lambda *_args, **_kwargs: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "",
                        "parameters": {"type": "object"},
                    },
                }
            ],
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=execute_tool,
            persist_memory=lambda **_kwargs: None,
            record_token_usage=lambda *_args, **_kwargs: None,
            get_max_tokens=lambda _provider, _model, override=None: override or 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "read notes"}],
            agent_name="Researcher",
            role_description="Research agent",
            agent_id=agent_id,
            user_id=user_id,
        )
    )

    assert result.content == "done"
    assert result.reasoning_signature == "sig-final"
    second_round_messages = fake_client.calls[1]["messages"]
    assistant_tool_turn = next(message for message in second_round_messages if message.role == "assistant")
    assert assistant_tool_turn.reasoning_content == "tool thinking"
    assert assistant_tool_turn.reasoning_signature == "sig-tool-turn"


@pytest.mark.asyncio
async def test_plan_mode_tool_intercept_notice_follows_tool_result_message() -> None:
    from app.kernel import AgentKernel, InvocationRequest, KernelDependencies, RuntimeConfig
    from app.runtime.session import SessionContext

    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None)
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_delegate",
                        "function": {
                            "name": "delegate_to_agent",
                            "arguments": json.dumps(
                                {
                                    "agent_name": "飞书知识库助手",
                                    "message": "去飞书知识库搜索飞翼艇报告并总结",
                                },
                                ensure_ascii=False,
                            ),
                        },
                    }
                ],
                reasoning_content=None,
                usage={"total_tokens": 9},
            ),
            SimpleNamespace(
                content="plan submitted",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 7},
                finish_reason="stop",
            ),
        ]
    )

    requires_confirmation = json.dumps(
        {
            "ok": False,
            "status": "requires_confirmation",
            "requires_confirmation": True,
            "summary": "Confirm before delegating.",
        },
        ensure_ascii=False,
    )

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_args, **_kwargs: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=3,
                quota_message=None,
            ),
            resolve_current_user_name=lambda *_args, **_kwargs: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [],
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: requires_confirmation,
            persist_memory=lambda **_kwargs: None,
            record_token_usage=lambda *_args, **_kwargs: None,
            get_max_tokens=lambda _provider, _model, override=None: override or 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "使用飞书知识库助手找飞翼艇报告"}],
            agent_name="Leslie的智能助手",
            role_description="Coordinator",
            agent_id=uuid4(),
            user_id=uuid4(),
            session_context=SessionContext(
                session_id="wechat-session",
                source="wechat_personal",
                channel="wechat_personal",
            ),
        )
    )

    assert result.content == "plan submitted"
    second_round_messages = fake_client.calls[1]["messages"]
    assistant_index = next(
        i for i, message in enumerate(second_round_messages) if message.role == "assistant" and message.tool_calls
    )
    assert second_round_messages[assistant_index + 1].role == "tool"
    assert second_round_messages[assistant_index + 1].tool_call_id == "call_delegate"
    assert not any(
        message.role == "system" and "[Plan Mode Activated]" in (message.content or "")
        for message in second_round_messages
    )


@pytest.mark.asyncio
async def test_agent_kernel_stops_after_blocking_ask_user_question_result():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )
    clarification_result = (
        '{"status":"awaiting_user_clarification","blocking":true,'
        '"questions":[{"question":"Pick one?","options":[{"label":"A"},{"label":"B"}]}]}'
    )
    tool_events: list[dict] = []
    persisted_payloads: list[dict] = []
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_clarify",
                        "function": {
                            "name": "ask_user_question",
                            "arguments": (
                                '{"questions":[{"question":"Pick one?","options":[{"label":"A"},{"label":"B"}]}]}'
                            ),
                        },
                    }
                ],
                reasoning_content=None,
                usage={"total_tokens": 4},
            ),
            SimpleNamespace(
                content="this second model turn must not run",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 4},
            ),
        ]
    )

    async def execute_tool(tool_name, args, request, emit_event):
        del args, request, emit_event
        assert tool_name == "ask_user_question"
        return clarification_result

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda _agent_id: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=5),
            resolve_current_user_name=lambda _user_id: "Rocky",
            build_system_prompt=lambda request, tenant_id, memory_context, current_user_name: "PROMPT",
            resolve_memory_context=lambda request, tenant_id: "",
            get_tools=lambda _agent_id, _core_only: [
                {
                    "type": "function",
                    "function": {
                        "name": "ask_user_question",
                        "description": "",
                        "parameters": {"type": "object"},
                    },
                }
            ],
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=execute_tool,
            persist_memory=lambda **kwargs: persisted_payloads.append(kwargs),
            record_token_usage=lambda *_args, **_kwargs: None,
            get_max_tokens=lambda *_args, **_kwargs: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens") if usage else None,
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "ask me before choosing"}],
            agent_name="Planner",
            role_description="Planning agent",
            agent_id=uuid4(),
            user_id=uuid4(),
            on_tool_call=lambda payload: tool_events.append(payload),
        )
    )

    assert len(fake_client.calls) == 1
    assert result.content == ""
    assert "this second model turn must not run" not in [part.get("text") for part in result.parts]
    assert tool_events[-1]["name"] == "ask_user_question"
    assert tool_events[-1]["status"] == "done"
    assert tool_events[-1]["result"] == clarification_result
    assert persisted_payloads


@pytest.mark.asyncio
async def test_agent_kernel_propagates_terminal_tool_card_signal_from_callback():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

    class _TerminalToolCardSignal(Exception):
        pass

    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_read",
                        "function": {"name": "read_file", "arguments": '{"path":"workspace/a.md"}'},
                    }
                ],
                reasoning_content=None,
                usage={"total_tokens": 4},
            ),
            SimpleNamespace(
                content="callback signal was swallowed",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 4},
            ),
        ]
    )

    async def execute_tool(tool_name, args, request, emit_event):
        del args, request, emit_event
        assert tool_name == "read_file"
        return "file content"

    async def on_tool_call(payload):
        if payload.get("status") == "done":
            raise _TerminalToolCardSignal("terminal card finalized")

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda _agent_id: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=5),
            resolve_current_user_name=lambda _user_id: "Rocky",
            build_system_prompt=lambda request, tenant_id, memory_context, current_user_name: "PROMPT",
            resolve_memory_context=lambda request, tenant_id: "",
            get_tools=lambda _agent_id, _core_only: [
                {
                    "type": "function",
                    "function": {
                        "name": "read_file",
                        "description": "",
                        "parameters": {"type": "object"},
                    },
                }
            ],
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=execute_tool,
            persist_memory=lambda **_kwargs: None,
            record_token_usage=lambda *_args, **_kwargs: None,
            get_max_tokens=lambda *_args, **_kwargs: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens") if usage else None,
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    with pytest.raises(_TerminalToolCardSignal):
        await kernel.handle(
            InvocationRequest(
                model=model,
                messages=[{"role": "user", "content": "read a file"}],
                agent_name="Reader",
                role_description="Read files",
                agent_id=uuid4(),
                user_id=uuid4(),
                on_tool_call=on_tool_call,
            )
        )

    assert len(fake_client.calls) == 1


@pytest.mark.asyncio
async def test_agent_kernel_splits_concatenated_tool_arguments_into_separate_calls():
    """Tier 1-4: concatenated DeepSeek-V4 style args `{"a":1}{"b":2}` must be split into
    two executable tool_calls (rather than dropped to `{}`). Both calls execute and the
    assistant history records each split call with a unique id and a valid arguments JSON.
    """
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )

    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_bad",
                        "function": {
                            "name": "web_fetch",
                            "arguments": '{"url":"https://example.com/a"}{"url":"https://example.com/b"}',
                        },
                    }
                ],
                reasoning_content=None,
                usage={"total_tokens": 2},
            ),
            SimpleNamespace(
                content="recovered",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 2},
            ),
        ]
    )

    executed_args: list[dict] = []

    def _capture_execute(name: str, arguments: dict, *_args, **_kwargs):
        executed_args.append(arguments)
        return f"fetched-{arguments.get('url', 'unknown')}"

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_args, **_kwargs: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=3,
                quota_message=None,
            ),
            resolve_current_user_name=lambda *_args, **_kwargs: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [
                {
                    "type": "function",
                    "function": {"name": "web_fetch", "description": "", "parameters": {"type": "object"}},
                }
            ],
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=_capture_execute,
            persist_memory=lambda **_kwargs: None,
            record_token_usage=lambda *_args, **_kwargs: None,
            get_max_tokens=lambda *_args, **_kwargs: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "fetch both pages"}],
            agent_name="Researcher",
            role_description="Research agent",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content == "recovered"
    assert len(fake_client.calls) == 2
    retry_messages = fake_client.calls[1]["messages"]
    assistant_with_bad_call = next(
        message for message in retry_messages if message.role == "assistant" and message.tool_calls
    )

    # Tier 1-4: the concatenated payload becomes two tool_calls, each with valid JSON
    assert len(assistant_with_bad_call.tool_calls) == 2
    first, second = assistant_with_bad_call.tool_calls
    assert first["function"]["arguments"] == '{"url":"https://example.com/a"}'
    assert second["function"]["arguments"] == '{"url":"https://example.com/b"}'
    assert first["type"] == "function"
    assert second["type"] == "function"
    assert first["id"] != second["id"], "Split tool_calls must carry distinct ids"

    # Both payloads were executed instead of being collapsed
    assert {a.get("url") for a in executed_args} == {
        "https://example.com/a",
        "https://example.com/b",
    }


@pytest.mark.asyncio
async def test_runtime_invoker_delegates_to_agent_kernel(monkeypatch):
    from app.kernel.contracts import InvocationResult
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent

    captured = {}

    class _FakeKernel:
        async def handle(self, request):
            captured["request"] = request
            return InvocationResult(
                content="kernel-result",
                tokens_used=7,
                final_tools=[{"type": "function", "function": {"name": "x"}}],
                parts=[{"type": "text", "text": "kernel-result"}],
            )

    monkeypatch.setattr("app.runtime.invoker.get_agent_kernel", lambda: _FakeKernel())

    async def allow_quota(_user_id):
        return None

    monkeypatch.setattr("app.runtime.invoker.check_user_token_quota", allow_quota)

    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="key",
        base_url=None,
        max_output_tokens=None,
    )

    result = await invoke_agent(
        AgentInvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "hello"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert captured["request"].agent_name == "Agent"
    assert captured["request"].messages == [{"role": "user", "content": "hello"}]
    assert result.content == "kernel-result"
    assert result.tokens_used == 7
    assert result.final_tools == [{"type": "function", "function": {"name": "x"}}]
    assert result.parts == [{"type": "text", "text": "kernel-result"}]


@pytest.mark.asyncio
async def test_agent_kernel_emits_ptl_full_compress_before_round_group_retry_event():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig
    from app.services.llm_client import LLMError

    runtime_events: list[dict] = []
    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
        max_input_tokens=128000,
        supports_vision=False,
    )
    fake_client = _FakeClient(
        [
            LLMError("HTTP 400: context_length_exceeded - maximum context length exceeded"),
            SimpleNamespace(
                content="recovered after ptl retry",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 6},
            ),
        ]
    )

    compress_calls: list[list[dict]] = []

    async def maybe_compress_messages(messages, **kwargs):
        compress_calls.append(messages)
        if len(compress_calls) == 1:
            return messages
        return [
            {"role": "system", "content": "compressed summary"},
            messages[-1],
        ]

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda _agent_id: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=5),
            resolve_current_user_name=lambda _user_id: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            resolve_retrieval_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [],
            maybe_compress_messages=maybe_compress_messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: "unused",
            persist_memory=lambda **kwargs: None,
            record_token_usage=lambda *args, **kwargs: None,
            get_max_tokens=lambda provider, model, override=None: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens") if usage else None,
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[
                {"role": "user", "content": "older question"},
                {"role": "assistant", "content": "older answer"},
                {"role": "user", "content": "follow-up question"},
                {"role": "assistant", "content": "follow-up answer"},
                {"role": "user", "content": "latest ask"},
            ],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
            on_event=runtime_events.append,
        )
    )

    assert result.content == "recovered after ptl retry"
    assert len(compress_calls) >= 2
    assert len(fake_client.calls) == 2
    assert len(fake_client.calls[1]["messages"]) < len(fake_client.calls[0]["messages"])
    session_compact_events = [event for event in runtime_events if event.get("type") == "session_compact"]
    assert session_compact_events[0] == {
        "type": "session_compact",
        "summary": "Prompt too long; compressed conversation before retry.",
        "original_message_count": 6,
        "kept_message_count": 3,
        "reason": "prompt_too_long_retry",
        "strategy": "full_compress",
        "attempt": 1,
    }


@pytest.mark.asyncio
async def test_agent_kernel_emits_runtime_fallback_event_after_prompt_too_long():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig
    from app.services.llm_client import LLMError

    runtime_events: list[dict] = []
    primary_model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="primary",
        base_url=None,
        max_output_tokens=None,
        max_input_tokens=128000,
        supports_vision=False,
    )
    fallback_model = SimpleNamespace(
        provider="anthropic",
        model="claude-sonnet",
        api_key="fallback",
        base_url=None,
        max_output_tokens=None,
        max_input_tokens=200000,
        supports_vision=False,
    )
    primary_client = _FakeClient(
        [
            LLMError("HTTP 400: context_length_exceeded - maximum context length exceeded"),
        ]
    )
    fallback_client = _FakeClient(
        [
            SimpleNamespace(
                content="fallback recovered",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 9},
            ),
        ]
    )

    def create_client(model):
        if model is primary_model:
            return primary_client
        if model is fallback_model:
            return fallback_client
        raise AssertionError(f"Unexpected model {model}")

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda _agent_id: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=2),
            resolve_current_user_name=lambda _user_id: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            resolve_retrieval_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [],
            maybe_compress_messages=lambda messages, **kwargs: messages,
            create_client=create_client,
            execute_tool=lambda *_args, **_kwargs: "unused",
            persist_memory=lambda **kwargs: None,
            record_token_usage=lambda *args, **kwargs: None,
            get_max_tokens=lambda provider, model, override=None: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens") if usage else None,
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=primary_model,
            fallback_model=fallback_model,
            messages=[{"role": "user", "content": "hello"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
            on_event=runtime_events.append,
        )
    )

    assert result.content == "fallback recovered"
    # T-G1 also emits reminder_injected observability events (max_tool_rounds=2
    # puts round 0 on the final-pressure threshold), so filter by type.
    fallback_events = [e for e in runtime_events if e["type"] == "runtime_fallback"]
    assert len(fallback_events) == 1
    assert fallback_events[0]["reason"] == "prompt_too_long"
    assert fallback_events[0]["from_model"] == "gpt-4.1"
    assert fallback_events[0]["to_model"] == "claude-sonnet"
    assert fallback_events[0]["provider"] == "anthropic"
    assert fallback_events[0]["part"]["event_type"] == "runtime_fallback"


@pytest.mark.asyncio
async def test_agent_kernel_keeps_core_tools_when_load_skill_has_no_declared_expansion():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

    tool_load_calls: list[bool] = []
    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )

    async def resolve_runtime_config(_agent_id):
        return RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=4, quota_message=None)

    async def get_tools(_agent_id, core_only):
        tool_load_calls.append(core_only)
        name = "core_tool" if core_only else "expanded_tool"
        return [{"type": "function", "function": {"name": name, "description": "", "parameters": {"type": "object"}}}]

    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "function": {"name": "load_skill", "arguments": '{"name":"web research"}'},
                    }
                ],
                reasoning_content=None,
                usage={"total_tokens": 2},
            ),
            SimpleNamespace(
                content="done",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 2},
            ),
        ]
    )

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=resolve_runtime_config,
            resolve_current_user_name=lambda *_args, **_kwargs: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=get_tools,
            maybe_compress_messages=lambda messages, **kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: "SKILL",
            persist_memory=lambda **kwargs: None,
            record_token_usage=lambda *args, **kwargs: None,
            get_max_tokens=lambda *args, **kwargs: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "load a skill"}],
            agent_name="Researcher",
            role_description="Research agent",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content == "done"
    assert tool_load_calls == [True]
    assert fake_client.calls[0]["tools"][0]["function"]["name"] == "core_tool"
    assert fake_client.calls[1]["tools"][0]["function"]["name"] == "core_tool"


@pytest.mark.asyncio
async def test_agent_kernel_does_not_expand_tools_after_load_skill():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig, ToolExpansionResult

    tool_load_calls: list[bool] = []
    expansion_calls: list[tuple[str, dict[str, str]]] = []
    emitted_events: list[dict] = []
    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )

    async def resolve_runtime_config(_agent_id):
        return RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=4, quota_message=None)

    async def get_tools(_agent_id, core_only):
        tool_load_calls.append(core_only)
        name = "core_tool" if core_only else "expanded_tool"
        return [{"type": "function", "function": {"name": name, "description": "", "parameters": {"type": "object"}}}]

    async def resolve_tool_expansion(_request, tool_name, args):
        expansion_calls.append((tool_name, args))
        return ToolExpansionResult(
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "web_search",
                        "description": "",
                        "parameters": {"type": "object"},
                    },
                }
            ],
            active_tool_groups=[
                {
                    "name": "web_pack",
                    "summary": "网页搜索与抓取能力",
                    "tools": ["web_search"],
                    "source": "system",
                }
            ],
            event_payload={
                "type": "tool_group_activation",
                "packs": [
                    {
                        "name": "web_pack",
                        "summary": "网页搜索与抓取能力",
                        "tools": ["web_search"],
                        "source": "system",
                    }
                ],
                "tool_groups": [
                    {
                        "name": "web_pack",
                        "summary": "网页搜索与抓取能力",
                        "tools": ["web_search"],
                        "source": "system",
                    }
                ],
                "message": "Activated web_pack",
                "status": "info",
            },
        )

    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "function": {"name": "load_skill", "arguments": '{"name":"web research"}'},
                    }
                ],
                reasoning_content=None,
                usage={"total_tokens": 2},
            ),
            SimpleNamespace(
                content="done",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 2},
            ),
        ]
    )

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=resolve_runtime_config,
            resolve_current_user_name=lambda *_args, **_kwargs: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=get_tools,
            resolve_tool_expansion=resolve_tool_expansion,
            maybe_compress_messages=lambda messages, **kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: "SKILL",
            persist_memory=lambda **kwargs: None,
            record_token_usage=lambda *args, **kwargs: None,
            get_max_tokens=lambda *args, **kwargs: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "load a skill"}],
            agent_name="Researcher",
            role_description="Research agent",
            agent_id=uuid4(),
            user_id=uuid4(),
            on_event=lambda event: emitted_events.append(event),
        )
    )

    assert result.content == "done"
    assert tool_load_calls == [True]
    assert expansion_calls == []
    assert fake_client.calls[0]["tools"][0]["function"]["name"] == "core_tool"
    assert fake_client.calls[1]["tools"][0]["function"]["name"] == "core_tool"
    assert not any(event.get("type") == "tool_group_activation" for event in emitted_events)


@pytest.mark.asyncio
async def test_agent_kernel_does_not_auto_expand_without_skill_or_mcp_trigger():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

    tool_load_calls: list[bool] = []
    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )

    async def resolve_runtime_config(_agent_id):
        return RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=5, quota_message=None)

    async def get_tools(_agent_id, core_only):
        tool_load_calls.append(core_only)
        name = "core_tool" if core_only else "expanded_tool"
        return [{"type": "function", "function": {"name": name, "description": "", "parameters": {"type": "object"}}}]

    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "function": {"name": "list_files", "arguments": '{"path":"skills"}'},
                    }
                ],
                reasoning_content=None,
                usage={"total_tokens": 2},
            ),
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_2",
                        "function": {"name": "list_files", "arguments": '{"path":"workspace"}'},
                    }
                ],
                reasoning_content=None,
                usage={"total_tokens": 2},
            ),
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_3",
                        "function": {"name": "list_files", "arguments": '{"path":"memory"}'},
                    }
                ],
                reasoning_content=None,
                usage={"total_tokens": 2},
            ),
            SimpleNamespace(
                content="done",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 2},
            ),
        ]
    )

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=resolve_runtime_config,
            resolve_current_user_name=lambda *_args, **_kwargs: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=get_tools,
            maybe_compress_messages=lambda messages, **kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: "OK",
            persist_memory=lambda **kwargs: None,
            record_token_usage=lambda *args, **kwargs: None,
            get_max_tokens=lambda *args, **kwargs: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "keep using the default toolkit"}],
            agent_name="Researcher",
            role_description="Research agent",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content == "done"
    assert tool_load_calls == [True]
    assert fake_client.calls[0]["tools"][0]["function"]["name"] == "core_tool"
    assert fake_client.calls[1]["tools"][0]["function"]["name"] == "core_tool"
    assert fake_client.calls[2]["tools"][0]["function"]["name"] == "core_tool"
    assert fake_client.calls[3]["tools"][0]["function"]["name"] == "core_tool"


@pytest.mark.asyncio
async def test_midloop_compaction_triggers_after_interval():
    """Mid-loop compaction fires every _MIDLOOP_COMPACT_CHECK_INTERVAL rounds
    and compresses when maybe_compress_messages returns fewer messages."""
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )

    compress_calls: list[int] = []
    compaction_events: list[dict] = []

    async def maybe_compress_messages(messages, **kwargs):
        compress_calls.append(len(messages))
        # Simulate compression: if >4 messages, summarise the old ones into 1
        if len(messages) > 4:
            on_compaction = kwargs.get("on_compaction")
            if on_compaction:
                result = on_compaction(
                    {
                        "summary": "compressed",
                        "original_message_count": len(messages),
                        "kept_message_count": 3,
                    }
                )
                if result is not None:
                    await result
            summary = {"role": "system", "content": "[Summary of previous conversation]"}
            return [summary] + messages[-2:]
        return messages

    # 4 tool-call rounds then a final text response
    responses = []
    for i in range(4):
        responses.append(
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": f"call_{i}",
                        "function": {"name": "list_files", "arguments": f'{{"path":"dir{i}"}}'},
                    }
                ],
                reasoning_content=None,
                usage={"total_tokens": 5},
            )
        )
    responses.append(
        SimpleNamespace(
            content="all done",
            tool_calls=[],
            reasoning_content=None,
            usage={"total_tokens": 3},
        )
    )

    fake_client = _FakeClient(responses)

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_a, **_kw: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=10,
                quota_message=None,
            ),
            resolve_current_user_name=lambda *_a, **_kw: "Rocky",
            build_system_prompt=lambda *_a, **_kw: "SYSTEM",
            resolve_memory_context=lambda *_a, **_kw: "",
            get_tools=lambda *_a, **_kw: [
                {
                    "type": "function",
                    "function": {"name": "list_files", "description": "", "parameters": {"type": "object"}},
                }
            ],
            maybe_compress_messages=maybe_compress_messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_a, **_kw: "files: a.txt, b.txt",
            persist_memory=lambda **kw: None,
            record_token_usage=lambda *a, **kw: None,
            get_max_tokens=lambda *a, **kw: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "list everything"}],
            agent_name="Agent",
            role_description="test",
            agent_id=uuid4(),
            user_id=uuid4(),
            on_event=lambda ev: compaction_events.append(ev) if ev.get("type") == "session_compact" else None,
        )
    )

    assert result.content == "all done"

    # Pre-loop compression is call 1 (1 user message → no compression needed)
    # Mid-loop compression fires at round 3 (interval=3)
    # At that point api_messages[1:] has: user + 3×(assistant+tool) = 7 messages → compressed
    assert len(compress_calls) >= 2, f"Expected at least 2 compress calls, got {compress_calls}"

    # The mid-loop call should have received >4 messages and compressed
    midloop_call_msg_count = compress_calls[1]
    assert midloop_call_msg_count > 4, f"Mid-loop should see >4 messages, got {midloop_call_msg_count}"

    # Compaction event should have been emitted
    assert len(compaction_events) >= 1, "Expected at least one session_compact event"
    assert compaction_events[0]["type"] == "session_compact"


def test_maybe_evict_tool_result_truncates_large_output():
    from app.kernel.engine import _maybe_evict_tool_result

    # Small result — returned unchanged
    small = "hello world"
    assert _maybe_evict_tool_result("run_code", "call_1", small) == small

    # Exempt tool — never evicted even if large
    large = "x" * 60000
    assert _maybe_evict_tool_result("read_file", "call_2", large) == large
    assert _maybe_evict_tool_result("list_files", "call_3", large) == large
    assert _maybe_evict_tool_result("web_search", "call_exempt", large) == large

    # Non-exempt large result without durable storage returns a typed,
    # retryable persistence failure; it never invents a recovery pointer.
    evicted = _maybe_evict_tool_result("run_code", "call_4", large)
    assert len(evicted) > len(large)
    assert "tool_result_persistence_failed" in evicted
    assert '"retryable": true' in evicted
    assert "Full output saved" not in evicted
    assert "complete output remains inline" in evicted
    assert "call_4" in evicted
    assert evicted.startswith(large)


def test_ptl_round_group_fallback_persists_full_dropped_messages_and_hash(tmp_path):
    import hashlib
    import json

    from app.kernel.engine import LLMMessage, _prepare_ptl_round_group_fallback

    messages = [
        LLMMessage(role="user", content="old-user"),
        LLMMessage(role="assistant", content="old-answer"),
        LLMMessage(role="user", content="middle-user"),
        LLMMessage(role="assistant", content="middle-answer"),
        LLMMessage(role="user", content="latest-user"),
        LLMMessage(role="assistant", content="latest-answer"),
    ]

    kept, receipt = _prepare_ptl_round_group_fallback(
        messages,
        drop_ratio=0.34,
        artifact_dir=tmp_path,
        session_id="session-1",
        attempt=2,
    )

    assert kept[0].content == "middle-user"
    artifact_path = tmp_path / receipt["artifact_filename"]
    payload = artifact_path.read_text(encoding="utf-8")
    assert "old-user" in payload and "old-answer" in payload
    assert receipt["sha256"] == hashlib.sha256(payload.encode("utf-8")).hexdigest()
    assert receipt["dropped_group_range"] == "0:1"
    assert receipt["recoverable"] is True
    assert json.loads(payload)["messages"][0]["content"] == "old-user"


def test_forced_tool_result_eviction_always_returns_a_recoverable_pointer(tmp_path):
    from app.kernel.engine import _maybe_evict_tool_result

    raw = "short-but-semantic\n" * 180

    inline = _maybe_evict_tool_result(
        "read_file",
        "call_force_short",
        raw,
        eviction_dir=tmp_path,
        force=True,
        reason="round aggregate budget",
    )

    assert inline != raw
    assert "workspace/tool_results/call_force_short.txt" in inline
    assert f"char_range=0-{len(raw)}" in inline
    assert (tmp_path / "call_force_short.txt").read_text(encoding="utf-8") == raw


def test_maybe_evict_never_emits_pointer_when_artifact_write_fails(tmp_path):
    from app.kernel.engine import _maybe_evict_tool_result

    invalid_dir = tmp_path / "not-a-directory"
    invalid_dir.write_text("occupied", encoding="utf-8")
    large = "evidence\n" * 10_000

    result = _maybe_evict_tool_result("run_code", "call_failed_write", large, eviction_dir=invalid_dir)

    assert "tool_result_persistence_failed" in result
    assert '"retryable": true' in result
    assert "workspace/tool_results" not in result
    assert "Full output saved" not in result


def test_maybe_evict_writes_file_when_eviction_dir_provided(tmp_path):
    from app.kernel.engine import _maybe_evict_tool_result

    large = "RESULT_DATA\n" * 5000  # ~60000 chars
    eviction_dir = tmp_path / "tool_results"

    evicted = _maybe_evict_tool_result("run_code", "call_99", large, eviction_dir=eviction_dir)

    # File should exist with full content
    written_file = eviction_dir / "call_99.txt"
    assert written_file.exists()
    assert written_file.read_text(encoding="utf-8") == large

    # Inline content should have file reference
    assert "workspace/tool_results/call_99.txt" in evicted
    assert "read_file" in evicted
    assert len(evicted) < len(large)


def test_force_evict_writes_exempt_tool_result_for_round_aggregate_overflow(tmp_path):
    from app.kernel.engine import _maybe_evict_tool_result

    large = "READ_FILE_RESULT\n" * 5000
    eviction_dir = tmp_path / "tool_results"

    evicted = _maybe_evict_tool_result(
        "read_file",
        "call_force",
        large,
        eviction_dir=eviction_dir,
        force=True,
        reason="round aggregate budget",
    )

    written_file = eviction_dir / "call_force.txt"
    assert written_file.exists()
    assert written_file.read_text(encoding="utf-8") == large
    assert "workspace/tool_results/call_force.txt" in evicted
    assert "round aggregate budget" in evicted
    assert len(evicted) < len(large)


@pytest.mark.asyncio
async def test_large_tool_result_evicted_in_kernel_loop():
    """Kernel evicts large tool results inline during the LLM loop."""
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="k",
        base_url=None,
        max_output_tokens=None,
    )

    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[{"id": "c1", "function": {"name": "run_code", "arguments": '{"code":"test"}'}}],
                reasoning_content=None,
                usage={"total_tokens": 5},
            ),
            SimpleNamespace(
                content="done",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 3},
            ),
        ]
    )

    large_result = "RESULT_LINE\n" * 5000  # ~60000 chars (exceeds 50K eviction threshold)

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_a, **_kw: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=5,
                quota_message=None,
            ),
            resolve_current_user_name=lambda *_a, **_kw: "Rocky",
            build_system_prompt=lambda *_a, **_kw: "SYSTEM",
            resolve_memory_context=lambda *_a, **_kw: "",
            get_tools=lambda *_a, **_kw: [
                {
                    "type": "function",
                    "function": {"name": "run_code", "description": "", "parameters": {"type": "object"}},
                }
            ],
            maybe_compress_messages=lambda messages, **kw: messages,
            create_client=lambda _m: fake_client,
            execute_tool=lambda *_a, **_kw: large_result,
            persist_memory=lambda **kw: None,
            record_token_usage=lambda *a, **kw: None,
            get_max_tokens=lambda *a, **kw: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda c: c // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "search"}],
            agent_name="Agent",
            role_description="test",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content == "done"

    # If durable persistence is unavailable, the model keeps the complete result
    # rather than receiving a semantically truncated substitute.
    second_call_messages = fake_client.calls[1]["messages"]
    tool_msg = [m for m in second_call_messages if m.role == "tool"][0]
    assert large_result in tool_msg.content
    assert "tool_result_persistence_failed" in tool_msg.content


@pytest.mark.asyncio
async def test_empty_tool_result_is_wrapped_with_actionable_message():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[{"id": "c1", "function": {"name": "read_file", "arguments": '{"path":"empty.txt"}'}}],
                reasoning_content=None,
                usage={"total_tokens": 3},
            ),
            SimpleNamespace(content="handled empty", tool_calls=[], reasoning_content=None, usage={"total_tokens": 3}),
        ]
    )
    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_a, **_kw: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=3),
            resolve_current_user_name=lambda *_a, **_kw: "Rocky",
            build_system_prompt=lambda *_a, **_kw: "SYSTEM",
            resolve_memory_context=lambda *_a, **_kw: "",
            get_tools=lambda *_a, **_kw: [
                {"type": "function", "function": {"name": "read_file", "description": "", "parameters": {}}}
            ],
            maybe_compress_messages=lambda messages, **kw: messages,
            create_client=lambda _m: fake_client,
            execute_tool=lambda *_a, **_kw: "",
            persist_memory=lambda **kw: None,
            record_token_usage=lambda *a, **kw: None,
            get_max_tokens=lambda *a, **kw: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda c: c // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "read empty file"}],
            agent_name="Agent",
            role_description="test",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content == "handled empty"
    tool_msg = [m for m in fake_client.calls[1]["messages"] if m.role == "tool"][0]
    assert "[Tool returned empty result]" in tool_msg.content
    tool_part = [p for p in result.parts if p.get("type") == "tool_call"][0]
    assert "[Tool returned empty result]" in tool_part["result"]


@pytest.mark.asyncio
async def test_persist_memory_called_on_max_rounds_exceeded():
    """persist_memory must be called even when max tool rounds exhausted."""
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig

    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="k",
        base_url=None,
        max_output_tokens=None,
    )
    persist_calls: list[dict] = []

    # 3 rounds of tool calls, max_tool_rounds=2 → will exceed
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[{"id": f"c{i}", "function": {"name": "list_files", "arguments": "{}"}}],
                reasoning_content=None,
                usage={"total_tokens": 3},
            )
            for i in range(3)
        ]
    )

    async def persist_memory(**kwargs):
        persist_calls.append(kwargs)

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_a, **_kw: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=2,
                quota_message=None,
            ),
            resolve_current_user_name=lambda *_a, **_kw: "Rocky",
            build_system_prompt=lambda *_a, **_kw: "SYSTEM",
            resolve_memory_context=lambda *_a, **_kw: "",
            get_tools=lambda *_a, **_kw: [
                {
                    "type": "function",
                    "function": {"name": "list_files", "description": "", "parameters": {"type": "object"}},
                }
            ],
            maybe_compress_messages=lambda messages, **kw: messages,
            create_client=lambda _m: fake_client,
            execute_tool=lambda *_a, **_kw: "ok",
            persist_memory=persist_memory,
            record_token_usage=lambda *a, **kw: None,
            get_max_tokens=lambda *a, **kw: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda c: c // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "keep going"}],
            agent_name="Agent",
            role_description="test",
            agent_id=uuid4(),
            user_id=uuid4(),
            memory_session_id="sess-max",
        )
    )

    assert "tool-round limit" in result.content
    assert "continue" in result.content.lower()
    assert len(persist_calls) == 1, f"Expected 1 persist call, got {len(persist_calls)}"
    assert persist_calls[0]["session_id"] == "sess-max"


@pytest.mark.asyncio
async def test_turn_token_budget_stops_before_next_tool_round_with_typed_receipt():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig, TerminalReason

    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[{"id": "c1", "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}}],
                reasoning_content=None,
                usage={"total_tokens": 50},
            ),
            SimpleNamespace(
                content="done after tool", tool_calls=[], reasoning_content=None, usage={"total_tokens": 3}
            ),
        ]
    )
    executed: list[str] = []
    persist_calls: list[dict] = []
    recorded_tokens: list[int] = []
    runtime_events: list[dict] = []

    async def prepare_model_request(**_kwargs):
        return "provider-request-budget-1"

    async def commit_model_response(**kwargs):
        return {
            "provider_request_id": kwargs["provider_request_id"],
            "round_index": kwargs["round_index"],
        }

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_a, **_kw: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=3,
                turn_token_budget=40,
            ),
            resolve_current_user_name=lambda *_a, **_kw: "Rocky",
            build_system_prompt=lambda *_a, **_kw: "SYSTEM",
            resolve_memory_context=lambda *_a, **_kw: "",
            get_tools=lambda *_a, **_kw: [
                {"type": "function", "function": {"name": "read_file", "description": "", "parameters": {}}}
            ],
            maybe_compress_messages=lambda messages, **kw: messages,
            create_client=lambda _m: fake_client,
            execute_tool=lambda tool_name, *_a, **_kw: executed.append(tool_name) or "tool result",
            persist_memory=lambda **kw: persist_calls.append(kw),
            record_token_usage=lambda _agent_id, tokens, **_kw: recorded_tokens.append(tokens),
            get_max_tokens=lambda *a, **kw: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda c: c // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "respect tool followup"}],
            agent_name="Agent",
            role_description="test",
            agent_id=uuid4(),
            user_id=uuid4(),
            memory_session_id="sess-budget",
            on_event=runtime_events.append,
            model_request_prepare=prepare_model_request,
            model_response_commit=commit_model_response,
        )
    )

    assert result.content.startswith("[Runtime Limit]")
    assert result.terminal_reason is TerminalReason.TOOL_BUDGET
    assert result.tokens_used == 50
    assert executed == []
    assert len(fake_client.calls) == 1
    assert recorded_tokens == [50]
    assert result.model_result_receipt == {
        "provider_request_id": "provider-request-budget-1",
        "round_index": 1,
    }
    assert persist_calls[0]["session_id"] == "sess-budget"
    assert any(
        part.get("event_type") == "turn_token_budget_exhausted" for part in result.parts if isinstance(part, dict)
    )
    persisted_text = "\n".join(
        str(message.get("content") or "")
        for message in persist_calls[0].get("messages", [])
        if isinstance(message, dict)
    )
    assert "[Runtime Limit]" in persisted_text
    budget_events = [event for event in runtime_events if event.get("type") == "turn_token_budget_exhausted"]
    assert budget_events == [
        {
            "type": "turn_token_budget_exhausted",
            "status": "blocked",
            "round": 1,
            "tokens_used": 50,
            "token_budget": 40,
            "blocked_tool_call_count": 1,
            "retryable": True,
            "part": {
                "type": "event",
                "event_type": "turn_token_budget_exhausted",
                "title": "Turn budget reached",
                "text": "The turn stopped before the next tool action.",
                "status": "warning",
                "tokens_used": 50,
                "token_budget": 40,
                "retryable": True,
            },
        }
    ]


@pytest.mark.asyncio
async def test_turn_token_budget_counts_recovered_usage_without_double_billing():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig, TerminalReason

    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[{"id": "c2", "function": {"name": "read_file", "arguments": '{"path":"b.txt"}'}}],
                reasoning_content=None,
                usage={"total_tokens": 10},
            )
        ]
    )
    executed: list[str] = []
    recorded_tokens: list[int] = []
    runtime_events: list[dict] = []

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_a, **_kw: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=3,
                turn_token_budget=40,
            ),
            resolve_current_user_name=lambda *_a, **_kw: "Rocky",
            build_system_prompt=lambda *_a, **_kw: "SYSTEM",
            resolve_memory_context=lambda *_a, **_kw: "",
            get_tools=lambda *_a, **_kw: [
                {"type": "function", "function": {"name": "read_file", "description": "", "parameters": {}}}
            ],
            maybe_compress_messages=lambda messages, **kw: messages,
            create_client=lambda _m: fake_client,
            execute_tool=lambda tool_name, *_a, **_kw: executed.append(tool_name) or "tool result",
            persist_memory=lambda **_kw: None,
            record_token_usage=lambda _agent_id, tokens, **_kw: recorded_tokens.append(tokens),
            get_max_tokens=lambda *_a, **_kw: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[
                {"role": "assistant", "content": None, "tool_calls": []},
                {"role": "tool", "tool_call_id": "c1", "content": "approved result"},
            ],
            agent_name="Agent",
            role_description="test",
            agent_id=uuid4(),
            user_id=uuid4(),
            initial_round_index=1,
            initial_turn_tokens_used=35,
            on_event=runtime_events.append,
        )
    )

    assert result.terminal_reason is TerminalReason.TOOL_BUDGET
    assert result.tokens_used == 45
    assert recorded_tokens == [10]
    assert executed == []
    assert len(fake_client.calls) == 1
    budget_event = next(event for event in runtime_events if event.get("type") == "turn_token_budget_exhausted")
    assert budget_event["round"] == 2
    assert budget_event["tokens_used"] == 45
    assert budget_event["token_budget"] == 40


@pytest.mark.asyncio
async def test_turn_token_budget_ignores_cached_prompt_reads_before_tool_round():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig
    from app.services.token_tracker import extract_usage_tokens

    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[{"id": "c1", "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}}],
                reasoning_content=None,
                usage={
                    "total_tokens": 1000,
                    "prompt_tokens": 980,
                    "completion_tokens": 20,
                    "prompt_tokens_details": {"cached_tokens": 960},
                },
            ),
            SimpleNamespace(content="done", tool_calls=[], reasoning_content=None, usage={"total_tokens": 3}),
        ]
    )
    executed: list[str] = []
    persist_calls: list[dict] = []

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_a, **_kw: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=3,
                turn_token_budget=100,
            ),
            resolve_current_user_name=lambda *_a, **_kw: "Rocky",
            build_system_prompt=lambda *_a, **_kw: "SYSTEM",
            resolve_memory_context=lambda *_a, **_kw: "",
            get_tools=lambda *_a, **_kw: [
                {"type": "function", "function": {"name": "read_file", "description": "", "parameters": {}}}
            ],
            maybe_compress_messages=lambda messages, **kw: messages,
            create_client=lambda _m: fake_client,
            execute_tool=lambda tool_name, *_a, **_kw: executed.append(tool_name) or "tool result",
            persist_memory=lambda **kw: persist_calls.append(kw),
            record_token_usage=lambda *a, **kw: None,
            get_max_tokens=lambda *a, **kw: 2048,
            extract_usage_tokens=extract_usage_tokens,
            estimate_tokens_from_chars=lambda c: c // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "respect effective budget"}],
            agent_name="Agent",
            role_description="test",
            agent_id=uuid4(),
            user_id=uuid4(),
            memory_session_id="sess-budget-cache",
        )
    )

    assert result.content == "done"
    assert executed == ["read_file"]
    assert len(persist_calls) == 1
    persisted_text = "\n".join(
        str(message.get("content") or "")
        for message in persist_calls[0].get("messages", [])
        if isinstance(message, dict)
    )
    assert "[Runtime Limit]" not in persisted_text
    assert len(fake_client.calls) == 2


@pytest.mark.asyncio
async def test_turn_token_budget_does_not_replace_zero_cache_miss_usage_with_full_prompt_estimate():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig
    from app.services.token_tracker import extract_usage_tokens

    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[{"id": "c1", "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}}],
                reasoning_content=None,
                usage={
                    "total_tokens": 1000,
                    "prompt_tokens": 1000,
                    "completion_tokens": 0,
                    "prompt_tokens_details": {"cached_tokens": 1000},
                },
            ),
            SimpleNamespace(
                content="done",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 1, "completion_tokens": 1},
            ),
        ]
    )
    executed: list[str] = []

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_a, **_kw: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=3,
                turn_token_budget=1,
            ),
            resolve_current_user_name=lambda *_a, **_kw: "Rocky",
            build_system_prompt=lambda *_a, **_kw: "SYSTEM",
            resolve_memory_context=lambda *_a, **_kw: "",
            get_tools=lambda *_a, **_kw: [
                {"type": "function", "function": {"name": "read_file", "description": "", "parameters": {}}}
            ],
            maybe_compress_messages=lambda messages, **kw: messages,
            create_client=lambda _m: fake_client,
            execute_tool=lambda tool_name, *_a, **_kw: executed.append(tool_name) or "tool result",
            persist_memory=lambda **_kw: None,
            record_token_usage=lambda *_a, **_kw: None,
            get_max_tokens=lambda *_a, **_kw: 2048,
            extract_usage_tokens=extract_usage_tokens,
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "use the cached prompt"}],
            agent_name="Agent",
            role_description="test",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content == "done"
    assert result.tokens_used == 1
    assert executed == ["read_file"]
    assert len(fake_client.calls) == 2


@pytest.mark.asyncio
async def test_turn_token_budget_preserves_a_completed_model_answer_byte_for_byte():
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig, TerminalReason

    answer = "[Runtime Limit] is quoted here as ordinary user-requested documentation."
    model = SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None)
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content=answer,
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 50},
            )
        ]
    )

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_a, **_kw: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=3,
                turn_token_budget=40,
            ),
            resolve_current_user_name=lambda *_a, **_kw: "Rocky",
            build_system_prompt=lambda *_a, **_kw: "SYSTEM",
            resolve_memory_context=lambda *_a, **_kw: "",
            get_tools=lambda *_a, **_kw: [],
            maybe_compress_messages=lambda messages, **kw: messages,
            create_client=lambda _m: fake_client,
            execute_tool=lambda *_a, **_kw: "unused",
            persist_memory=lambda **_kw: None,
            record_token_usage=lambda *_a, **_kw: None,
            get_max_tokens=lambda *_a, **_kw: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "quote the runtime-limit label"}],
            agent_name="Agent",
            role_description="test",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content == answer
    assert result.terminal_reason is TerminalReason.TURN_STOP


@pytest.mark.asyncio
async def test_persist_memory_called_on_llm_error():
    """persist_memory must be called when LLM returns an error (after fallback exhausted)."""
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel, KernelDependencies, RuntimeConfig
    from app.services.llm_client import LLMError

    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="k",
        base_url=None,
        max_output_tokens=None,
    )
    persist_calls: list[dict] = []

    class _ErrorClient:
        async def stream(self, **kwargs):
            raise LLMError("rate limit exceeded")

        async def close(self):
            pass

    async def persist_memory(**kwargs):
        persist_calls.append(kwargs)

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_a, **_kw: RuntimeConfig(
                tenant_id=uuid4(),
                max_tool_rounds=5,
                quota_message=None,
            ),
            resolve_current_user_name=lambda *_a, **_kw: "Rocky",
            build_system_prompt=lambda *_a, **_kw: "SYSTEM",
            resolve_memory_context=lambda *_a, **_kw: "",
            get_tools=lambda *_a, **_kw: [],
            maybe_compress_messages=lambda messages, **kw: messages,
            create_client=lambda _m: _ErrorClient(),
            execute_tool=lambda *_a, **_kw: "ok",
            persist_memory=persist_memory,
            record_token_usage=lambda *a, **kw: None,
            get_max_tokens=lambda *a, **kw: 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens") if usage else None,
            estimate_tokens_from_chars=lambda c: c // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=model,
            messages=[{"role": "user", "content": "hello"}],
            agent_name="Agent",
            role_description="test",
            agent_id=uuid4(),
            user_id=uuid4(),
            memory_session_id="sess-err",
        )
    )

    assert "LLM Error" in result.content
    assert len(persist_calls) == 1
    assert persist_calls[0]["session_id"] == "sess-err"


class TestPromptTooLongDetection:
    """_is_prompt_too_long detects PTL errors from various providers."""

    def test_openai_context_length_exceeded(self) -> None:
        from app.kernel.engine import _is_prompt_too_long

        exc = Exception("HTTP 400: context_length_exceeded - This model's maximum context length is 128000 tokens")
        assert _is_prompt_too_long(exc) is True

    def test_anthropic_too_long(self) -> None:
        from app.kernel.engine import _is_prompt_too_long

        exc = Exception(
            "Anthropic stream error (invalid_request_error): prompt is too long: 210000 tokens > 200000 maximum"
        )
        assert _is_prompt_too_long(exc) is True

    def test_gemini_request_too_large(self) -> None:
        from app.kernel.engine import _is_prompt_too_long

        exc = Exception("HTTP 400: request too large for model")
        assert _is_prompt_too_long(exc) is True

    def test_input_too_long(self) -> None:
        from app.kernel.engine import _is_prompt_too_long

        exc = Exception("input too long for this model")
        assert _is_prompt_too_long(exc) is True

    def test_unrelated_error_not_detected(self) -> None:
        from app.kernel.engine import _is_prompt_too_long

        exc = Exception("HTTP 500: Internal server error")
        assert _is_prompt_too_long(exc) is False

    def test_rate_limit_not_detected(self) -> None:
        from app.kernel.engine import _is_prompt_too_long

        exc = Exception("Rate limited after 3 attempts: 429 Too Many Requests")
        assert _is_prompt_too_long(exc) is False

    def test_connection_error_not_detected(self) -> None:
        from app.kernel.engine import _is_prompt_too_long

        exc = Exception("Connection failed after 3 attempts: timeout")
        assert _is_prompt_too_long(exc) is False
