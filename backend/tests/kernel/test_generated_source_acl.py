from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.runtime.session import SessionContext


class _StreamingClient:
    def __init__(self, responses):
        self.responses = list(responses)

    async def stream(self, **kwargs):
        if not self.responses:
            raise AssertionError("No fake response prepared")
        response = self.responses.pop(0)
        content = getattr(response, "content", "") or ""
        if content and kwargs.get("on_chunk"):
            await kwargs["on_chunk"](content)
        return response

    async def close(self) -> None:
        return None


def _model():
    return SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="test-key",
        base_url=None,
        max_output_tokens=None,
    )


def _kernel(*, tenant_id, client, tools=None, execute_tool=None, spans=None):
    from app.kernel import AgentKernel, KernelDependencies, RuntimeConfig

    async def record_invocation_span(**kwargs):
        if spans is not None:
            spans.append(kwargs)

    return AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda _agent_id: RuntimeConfig(tenant_id=tenant_id, max_tool_rounds=4),
            resolve_current_user_name=lambda _user_id: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "SYSTEM",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            get_tools=lambda *_args, **_kwargs: tools or [],
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: client,
            execute_tool=execute_tool or (lambda *_args, **_kwargs: ""),
            persist_memory=lambda **_kwargs: None,
            record_token_usage=lambda *_args, **_kwargs: None,
            record_invocation_span=record_invocation_span,
            get_max_tokens=lambda *_args, **_kwargs: 1024,
            extract_usage_tokens=lambda usage: usage.get("total_tokens") if usage else None,
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )


@pytest.mark.asyncio
async def test_kernel_blocks_forbidden_connector_source_before_streaming() -> None:
    from app.kernel.contracts import InvocationRequest
    from app.services.connector_acl import CONNECTOR_SOURCE_ITEMS_METADATA_KEY

    tenant_id = uuid4()
    user_id = uuid4()
    agent_id = uuid4()
    hidden_source = "feishu://doc/hidden"
    chunks: list[str] = []
    spans: list[dict] = []
    session = SessionContext(
        session_id="acl-session",
        source="web",
        metadata={
            CONNECTOR_SOURCE_ITEMS_METADATA_KEY: [
                {
                    "source": hidden_source,
                    "acl": {"tenant_ids": [str(tenant_id)], "user_ids": [str(uuid4())]},
                }
            ]
        },
    )
    client = _StreamingClient(
        [
            SimpleNamespace(
                content=f"Leaked citation: {hidden_source}",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 5},
                finish_reason="stop",
            )
        ]
    )

    result = await _kernel(tenant_id=tenant_id, client=client, spans=spans).handle(
        InvocationRequest(
            model=_model(),
            messages=[{"role": "user", "content": "summarize"}],
            agent_name="ACL Agent",
            role_description="desc",
            agent_id=agent_id,
            user_id=user_id,
            session_context=session,
            on_chunk=lambda chunk: chunks.append(chunk),
        )
    )

    assert hidden_source not in result.content
    assert result.content.startswith("[Permission Check]")
    assert chunks == []
    assert session.metadata["generated_source_permission_checks"][-1]["allowed"] is False
    authz = session.metadata["generated_source_permission_checks"][-1]["authorization_decision_entry"]
    assert authz["schema"] == "hive.ccplus.authorization_decision.v1"
    assert authz["policy"] == "generated_source_acl"
    assert authz["resource"] == hidden_source
    assert authz["result"] == "blocked"
    assert authz["reason"] == "forbidden_connector_source"
    permission_span = next(span for span in spans if span["name"] == "generated_source_permission_check")
    assert permission_span["metadata"]["status"] == "blocked"
    assert permission_span["metadata"]["forbidden_source_count"] == 1
    assert permission_span["metadata"]["authorization_decision_entry"]["resource"] == hidden_source


@pytest.mark.asyncio
async def test_kernel_registers_tool_result_connector_sources_for_final_check() -> None:
    from app.kernel.contracts import InvocationRequest
    from app.services.connector_acl import CONNECTOR_SOURCE_ITEMS_METADATA_KEY

    tenant_id = uuid4()
    user_id = uuid4()
    agent_id = uuid4()
    hidden_source = "feishu://doc/from-tool"
    session = SessionContext(session_id="tool-acl-session", source="web")
    client = _StreamingClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "function": {"name": "feishu_doc_read", "arguments": '{"doc_id":"from-tool"}'},
                    }
                ],
                reasoning_content=None,
                usage={"total_tokens": 3},
                finish_reason="tool_calls",
            ),
            SimpleNamespace(
                content=f"Answer cites {hidden_source}",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 4},
                finish_reason="stop",
            ),
        ]
    )

    async def execute_tool(*_args, **_kwargs):
        return {
            "source": hidden_source,
            "content": "hidden doc",
            "acl": {"tenant_ids": [str(tenant_id)], "user_ids": [str(uuid4())]},
        }

    result = await _kernel(
        tenant_id=tenant_id,
        client=client,
        tools=[{"type": "function", "function": {"name": "feishu_doc_read", "description": "", "parameters": {}}}],
        execute_tool=execute_tool,
    ).handle(
        InvocationRequest(
            model=_model(),
            messages=[{"role": "user", "content": "read feishu"}],
            agent_name="ACL Agent",
            role_description="desc",
            agent_id=agent_id,
            user_id=user_id,
            session_context=session,
        )
    )

    assert session.metadata[CONNECTOR_SOURCE_ITEMS_METADATA_KEY][0]["source"] == hidden_source
    assert hidden_source not in result.content
    assert result.content.startswith("[Permission Check]")


@pytest.mark.asyncio
async def test_kernel_registers_governed_tool_argument_sources_even_without_result_metadata() -> None:
    from app.kernel.contracts import InvocationRequest
    from app.services.connector_acl import CONNECTOR_SOURCE_ITEMS_METADATA_KEY

    tenant_id = uuid4()
    user_id = uuid4()
    agent_id = uuid4()
    hidden_source = "feishu://doc/arg-only"
    session = SessionContext(session_id="tool-arg-acl-session", source="web")
    client = _StreamingClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {
                        "id": "call_1",
                        "function": {"name": "feishu_doc_read", "arguments": '{"doc_id":"arg-only"}'},
                    }
                ],
                reasoning_content=None,
                usage={"total_tokens": 3},
                finish_reason="tool_calls",
            ),
            SimpleNamespace(
                content=f"Answer cites {hidden_source}",
                tool_calls=[],
                reasoning_content=None,
                usage={"total_tokens": 4},
                finish_reason="stop",
            ),
        ]
    )

    async def execute_tool(*_args, **_kwargs):
        return "plain document text without source metadata"

    result = await _kernel(
        tenant_id=tenant_id,
        client=client,
        tools=[{"type": "function", "function": {"name": "feishu_doc_read", "description": "", "parameters": {}}}],
        execute_tool=execute_tool,
    ).handle(
        InvocationRequest(
            model=_model(),
            messages=[{"role": "user", "content": "read feishu"}],
            agent_name="ACL Agent",
            role_description="desc",
            agent_id=agent_id,
            user_id=user_id,
            session_context=session,
        )
    )

    assert session.metadata[CONNECTOR_SOURCE_ITEMS_METADATA_KEY][0]["source"] == hidden_source
    assert session.metadata[CONNECTOR_SOURCE_ITEMS_METADATA_KEY][0]["acl"]["deny_by_default"] is True
    assert hidden_source not in result.content
    assert result.content.startswith("[Permission Check]")
