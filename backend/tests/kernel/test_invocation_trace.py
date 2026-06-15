from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.runtime.session import SessionContext


class _FakeClient:
    def __init__(self, responses):
        self.responses = list(responses)

    async def stream(self, **_kwargs):
        if not self.responses:
            raise AssertionError("No fake response prepared")
        return self.responses.pop(0)

    async def close(self) -> None:
        return None


def _make_model():
    return SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )


@pytest.mark.asyncio
async def test_kernel_records_invocation_and_generation_spans(monkeypatch, tmp_path):
    from app.kernel.contracts import InvocationRequest, RuntimeConfig
    from app.kernel.engine import AgentKernel, KernelDependencies
    from app.services import invocation_trace

    monkeypatch.setattr(invocation_trace, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    agent_id = uuid4()
    session_ctx = SessionContext(session_id="session-trace", source="web")
    fake_client = _FakeClient(
        [SimpleNamespace(content="done", tool_calls=[], reasoning_content=None, usage={"total_tokens": 7})]
    )
    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda _agent_id: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=3),
            resolve_current_user_name=lambda _user_id: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "FROZEN",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [],
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: "OK",
            persist_memory=lambda **_kwargs: None,
            record_token_usage=lambda *_args, **_kwargs: None,
            get_max_tokens=lambda *_args, **_kwargs: 1024,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=_make_model(),
            messages=[{"role": "user", "content": "hello"}],
            agent_name="Trace Agent",
            role_description="desc",
            agent_id=agent_id,
            user_id=uuid4(),
            session_context=session_ctx,
        )
    )

    assert result.content == "done"
    trace_id = session_ctx.metadata["trace_id"]
    span_path = tmp_path / str(agent_id) / "traces" / "invocation_spans.jsonl"
    records = [json.loads(line) for line in span_path.read_text(encoding="utf-8").splitlines()]

    assert {record["span_type"] for record in records} == {"generation", "invocation"}
    assert {record["invocation_id"] for record in records} == {trace_id}
    generation = next(record for record in records if record["span_type"] == "generation")
    assert generation["metadata"]["provider"] == "openai"
    assert generation["metadata"]["model"] == "gpt-4.1"
    assert generation["metadata"]["usage"]["total_tokens"] == 7


@pytest.mark.asyncio
async def test_kernel_records_tool_span(monkeypatch, tmp_path):
    from app.kernel.contracts import InvocationRequest, RuntimeConfig
    from app.kernel.engine import AgentKernel, KernelDependencies
    from app.services import invocation_trace

    monkeypatch.setattr(invocation_trace, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    agent_id = uuid4()
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[{"id": "call_1", "function": {"name": "read_file", "arguments": "{}"}}],
                reasoning_content=None,
                usage={"total_tokens": 3},
            ),
            SimpleNamespace(content="done", tool_calls=[], reasoning_content=None, usage={"total_tokens": 4}),
        ]
    )
    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda _agent_id: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=3),
            resolve_current_user_name=lambda _user_id: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "FROZEN",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [
                {"type": "function", "function": {"name": "read_file", "description": "", "parameters": {}}},
            ],
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: "file content",
            persist_memory=lambda **_kwargs: None,
            record_token_usage=lambda *_args, **_kwargs: None,
            get_max_tokens=lambda *_args, **_kwargs: 1024,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=_make_model(),
            messages=[{"role": "user", "content": "read"}],
            agent_name="Trace Agent",
            role_description="desc",
            agent_id=agent_id,
            user_id=uuid4(),
            session_context=SessionContext(session_id="session-tool-trace", source="web"),
        )
    )

    assert result.content == "done"
    span_path = tmp_path / str(agent_id) / "traces" / "invocation_spans.jsonl"
    records = [json.loads(line) for line in span_path.read_text(encoding="utf-8").splitlines()]
    tool = next(record for record in records if record["span_type"] == "tool")
    assert tool["name"] == "read_file"
    assert tool["metadata"]["status"] == "ok"
    assert tool["metadata"]["result_chars"] == len("file content")


@pytest.mark.asyncio
async def test_kernel_records_code_execution_evidence_from_tool_envelope(monkeypatch, tmp_path):
    from app.kernel.contracts import InvocationRequest, RuntimeConfig
    from app.kernel.engine import AgentKernel, KernelDependencies
    from app.services import invocation_trace
    from app.tools.result_envelope import ToolContentEnvelope

    monkeypatch.setattr(invocation_trace, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    agent_id = uuid4()
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[{"id": "call_1", "function": {"name": "execute_code", "arguments": "{}"}}],
                reasoning_content=None,
                usage={"total_tokens": 3},
            ),
            SimpleNamespace(content="done", tool_calls=[], reasoning_content=None, usage={"total_tokens": 4}),
        ]
    )
    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda _agent_id: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=3),
            resolve_current_user_name=lambda _user_id: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "FROZEN",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [
                {"type": "function", "function": {"name": "execute_code", "description": "", "parameters": {}}},
            ],
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: ToolContentEnvelope(
                text="ok",
                metadata={
                    "code_execution_evidence": {
                        "provider": "vercel_sandbox",
                        "isolation": "vercel_microvm",
                        "network_policy": "deny-all",
                    }
                },
            ),
            persist_memory=lambda **_kwargs: None,
            record_token_usage=lambda *_args, **_kwargs: None,
            get_max_tokens=lambda *_args, **_kwargs: 1024,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    result = await kernel.handle(
        InvocationRequest(
            model=_make_model(),
            messages=[{"role": "user", "content": "run"}],
            agent_name="Trace Agent",
            role_description="desc",
            agent_id=agent_id,
            user_id=uuid4(),
            session_context=SessionContext(session_id="session-code-trace", source="web"),
        )
    )

    assert result.content == "done"
    span_path = tmp_path / str(agent_id) / "traces" / "invocation_spans.jsonl"
    records = [json.loads(line) for line in span_path.read_text(encoding="utf-8").splitlines()]
    tool = next(record for record in records if record["span_type"] == "tool")
    assert tool["metadata"]["code_execution_evidence"]["provider"] == "vercel_sandbox"
    assert tool["metadata"]["code_execution_evidence"]["network_policy"] == "deny-all"


@pytest.mark.asyncio
async def test_kernel_persists_invocation_spans_with_runtime_join_keys(monkeypatch, tmp_path):
    from app.kernel.contracts import InvocationRequest, RuntimeConfig
    from app.kernel.engine import AgentKernel, KernelDependencies
    from app.services import invocation_trace

    monkeypatch.setattr(invocation_trace, "get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

    agent_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    runtime_task_id = uuid4()
    request_id = uuid4()
    captured: list[dict] = []
    fake_client = _FakeClient(
        [SimpleNamespace(content="done", tool_calls=[], reasoning_content=None, usage={"total_tokens": 11})]
    )

    async def record_invocation_span(**kwargs):
        captured.append(kwargs)

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda _agent_id: RuntimeConfig(tenant_id=tenant_id, max_tool_rounds=3),
            resolve_current_user_name=lambda _user_id: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "FROZEN",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: [],
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: "OK",
            persist_memory=lambda **_kwargs: None,
            record_token_usage=lambda *_args, **_kwargs: None,
            record_invocation_span=record_invocation_span,
            get_max_tokens=lambda *_args, **_kwargs: 1024,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )

    session_ctx = SessionContext(
        session_id="session-runtime",
        source="web",
        metadata={
            "runtime_task_id": str(runtime_task_id),
            "request_id": str(request_id),
            "parent_trace_id": "trace-parent",
        },
    )

    result = await kernel.handle(
        InvocationRequest(
            model=_make_model(),
            messages=[{"role": "user", "content": "hello"}],
            agent_name="Trace Agent",
            role_description="desc",
            agent_id=agent_id,
            user_id=user_id,
            session_context=session_ctx,
        )
    )

    assert result.content == "done"
    assert {row["span_type"] for row in captured} == {"generation", "invocation"}
    generation = next(row for row in captured if row["span_type"] == "generation")
    invocation = next(row for row in captured if row["span_type"] == "invocation")
    assert generation["tenant_id"] == tenant_id
    assert generation["agent_id"] == agent_id
    assert generation["user_id"] == user_id
    assert generation["runtime_task_id"] == runtime_task_id
    assert generation["request_id"] == request_id
    assert generation["parent_trace_id"] == "trace-parent"
    assert generation["parent_span_id"].startswith("invocation-")
    assert generation["usage"]["total_tokens"] == 11
    assert invocation["span_id"] == generation["parent_span_id"]
    assert invocation["runtime_task_id"] == runtime_task_id
