from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.runtime.session import SessionContext


class _FakeClient:
    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def stream(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("No fake response prepared")
        outcome = self._responses.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome
        return outcome

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
async def test_kernel_verifies_frozen_prefix_and_refreshes_dynamic_retrieval():
    from app.kernel.contracts import InvocationRequest, RuntimeConfig
    from app.kernel.engine import AgentKernel, KernelDependencies

    build_calls = {"count": 0}
    retrieval_calls: list[str] = []
    session_ctx = SessionContext(session_id="s-1", source="chat")
    tenant_id = uuid4()

    async def build_system_prompt(request, tenant_id, memory_context, current_user_name):
        del request, tenant_id, memory_context, current_user_name
        build_calls["count"] += 1
        return "FROZEN"

    async def resolve_retrieval_context(request, tenant_id):
        del request, tenant_id
        value = f"RETRIEVAL_{len(retrieval_calls) + 1}"
        retrieval_calls.append(value)
        return value

    fake_client = _FakeClient(
        [
            SimpleNamespace(content="first", tool_calls=[], reasoning_content=None, usage={"total_tokens": 3}),
            SimpleNamespace(content="second", tool_calls=[], reasoning_content=None, usage={"total_tokens": 3}),
        ]
    )

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda _agent_id: RuntimeConfig(tenant_id=tenant_id, max_tool_rounds=3),
            resolve_current_user_name=lambda _user_id: "Rocky",
            build_system_prompt=build_system_prompt,
            resolve_memory_context=lambda *_args, **_kwargs: "SNAPSHOT_BLOCK",
            resolve_retrieval_context=resolve_retrieval_context,
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

    request = InvocationRequest(
        model=_make_model(),
        messages=[{"role": "user", "content": "hello"}],
        agent_name="Agent",
        role_description="desc",
        agent_id=uuid4(),
        user_id=uuid4(),
        session_context=session_ctx,
    )

    result1 = await kernel.handle(request)
    result2 = await kernel.handle(request)

    assert result1.content == "first"
    assert result2.content == "second"
    # Re-rendering is the dependency verification pass. The provider still sees
    # byte-identical frozen content and can reuse its prompt cache.
    assert build_calls["count"] == 2
    assert session_ctx.prompt_prefix == "FROZEN"
    assert session_ctx.prompt_fingerprint

    first_system = fake_client.calls[0]["messages"][0].content
    second_system = fake_client.calls[1]["messages"][0].content
    first_dynamic = fake_client.calls[0]["messages"][-1].content
    second_dynamic = fake_client.calls[1]["messages"][-1].content

    assert "SNAPSHOT_BLOCK" not in first_system
    assert "RETRIEVAL_1" not in first_system
    assert "SNAPSHOT_BLOCK" not in second_system
    assert "SNAPSHOT_BLOCK" in first_dynamic
    assert "## Your Memory System" in first_dynamic
    assert second_dynamic.count("SNAPSHOT_BLOCK") == 1
    assert "RETRIEVAL_1" in first_dynamic
    assert "RETRIEVAL_2" in second_dynamic


@pytest.mark.asyncio
async def test_kernel_revalidates_rendered_frozen_context_without_external_signature():
    from app.kernel.contracts import InvocationRequest, RuntimeConfig
    from app.kernel.engine import AgentKernel, KernelDependencies

    tenant_id = uuid4()
    state = {"company": "Company policy v1"}
    build_calls = []
    session_ctx = SessionContext(session_id="s-live-context", source="chat")

    async def build_system_prompt(*_args, **_kwargs):
        build_calls.append(state["company"])
        return f"FROZEN::{state['company']}"

    fake_client = _FakeClient(
        [
            SimpleNamespace(content="first", tool_calls=[], reasoning_content=None, usage={"total_tokens": 3}),
            SimpleNamespace(content="second", tool_calls=[], reasoning_content=None, usage={"total_tokens": 3}),
        ]
    )
    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda _agent_id: RuntimeConfig(tenant_id=tenant_id, max_tool_rounds=3),
            resolve_current_user_name=lambda _user_id: "Rocky",
            build_system_prompt=build_system_prompt,
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
    request = InvocationRequest(
        model=_make_model(),
        messages=[{"role": "user", "content": "hello"}],
        agent_name="Agent",
        role_description="desc",
        agent_id=uuid4(),
        user_id=uuid4(),
        session_context=session_ctx,
    )

    await kernel.handle(request)
    state["company"] = "Company policy v2"
    await kernel.handle(request)

    assert build_calls == ["Company policy v1", "Company policy v2"]
    assert "Company policy v1" in fake_client.calls[0]["messages"][0].content
    assert "Company policy v2" in fake_client.calls[1]["messages"][0].content
    manifest = session_ctx.metadata["frozen_context_dependency_manifest"]
    assert manifest["root_hash"]
    assert manifest["sections"]
    assert all(section["content_hash"] for section in manifest["sections"])
    prompt_manifest = session_ctx.metadata["runtime_assembly_state"]["prompt_assembly_manifest"]
    assert prompt_manifest["frozen_context_dependency_manifest"]["root_hash"] == manifest["root_hash"]


@pytest.mark.asyncio
async def test_kernel_never_falls_back_to_stale_prefix_when_context_rebuild_fails():
    from app.kernel.contracts import InvocationRequest, RuntimeConfig
    from app.kernel.engine import AgentKernel, KernelDependencies

    tenant_id = uuid4()
    build_count = 0

    async def build_system_prompt(*_args, **_kwargs):
        nonlocal build_count
        build_count += 1
        if build_count == 2:
            raise RuntimeError("governed context unavailable")
        return "FROZEN::complete"

    fake_client = _FakeClient(
        [
            SimpleNamespace(content="first", tool_calls=[], reasoning_content=None, usage={"total_tokens": 3}),
            SimpleNamespace(content="stale", tool_calls=[], reasoning_content=None, usage={"total_tokens": 3}),
        ]
    )
    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda _agent_id: RuntimeConfig(tenant_id=tenant_id, max_tool_rounds=3),
            resolve_current_user_name=lambda _user_id: "Rocky",
            build_system_prompt=build_system_prompt,
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
    request = InvocationRequest(
        model=_make_model(),
        messages=[{"role": "user", "content": "hello"}],
        agent_name="Agent",
        role_description="desc",
        agent_id=uuid4(),
        user_id=uuid4(),
        session_context=SessionContext(session_id="s-no-stale", source="chat"),
    )

    await kernel.handle(request)
    with pytest.raises(RuntimeError, match="governed context unavailable"):
        await kernel.handle(request)

    assert build_count == 2
    assert len(fake_client.calls) == 1


@pytest.mark.asyncio
async def test_kernel_routes_runtime_metadata_outside_knowledge_section():
    from app.kernel.contracts import InvocationRequest, RuntimeConfig
    from app.kernel.engine import AgentKernel, KernelDependencies

    tenant_id = uuid4()
    session_ctx = SessionContext(session_id="s-runtime", source="chat")
    fake_client = _FakeClient(
        [SimpleNamespace(content="done", tool_calls=[], reasoning_content=None, usage={"total_tokens": 3})]
    )

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda _agent_id: RuntimeConfig(tenant_id=tenant_id, max_tool_rounds=3),
            resolve_current_user_name=lambda _user_id: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "FROZEN",
            resolve_memory_context=lambda *_args, **_kwargs: "MEMORY_CONTEXT",
            resolve_runtime_metadata_context=lambda *_args, **_kwargs: "## Runtime Metadata\nACTIVE_TRIGGER",
            resolve_retrieval_context=lambda *_args, **_kwargs: "EXTERNAL_KNOWLEDGE",
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

    await kernel.handle(
        InvocationRequest(
            model=_make_model(),
            messages=[{"role": "user", "content": "hello"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
            session_context=session_ctx,
        )
    )

    dynamic_prompt = fake_client.calls[0]["messages"][-1].content
    assert "## Runtime Metadata\nACTIVE_TRIGGER" in dynamic_prompt
    assert "## Knowledge" in dynamic_prompt
    assert dynamic_prompt.index("## Runtime Metadata") < dynamic_prompt.index("## Knowledge")


@pytest.mark.asyncio
async def test_kernel_rebuilds_frozen_prefix_when_prompt_cache_key_changes():
    from app.kernel.contracts import InvocationRequest, RuntimeConfig
    from app.kernel.engine import AgentKernel, KernelDependencies

    build_calls: list[str] = []
    session_ctx = SessionContext(session_id="s-shared", source="chat")
    tenant_id = uuid4()

    async def build_system_prompt(request, tenant_id, memory_context, current_user_name):
        del tenant_id, memory_context, current_user_name
        build_calls.append(f"{request.agent_id}:{request.invocation_scope or 'conversation'}")
        return f"FROZEN::{request.agent_name}::{request.invocation_scope or 'conversation'}"

    fake_client = _FakeClient(
        [
            SimpleNamespace(content="first", tool_calls=[], reasoning_content=None, usage={"total_tokens": 3}),
            SimpleNamespace(content="second", tool_calls=[], reasoning_content=None, usage={"total_tokens": 3}),
        ]
    )

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda _agent_id: RuntimeConfig(tenant_id=tenant_id, max_tool_rounds=3),
            resolve_current_user_name=lambda _user_id: "Rocky",
            build_system_prompt=build_system_prompt,
            resolve_memory_context=lambda *_args, **_kwargs: "",
            resolve_retrieval_context=lambda *_args, **_kwargs: "",
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

    first_agent_id = uuid4()
    second_agent_id = uuid4()

    await kernel.handle(
        InvocationRequest(
            model=_make_model(),
            messages=[{"role": "user", "content": "hello"}],
            agent_name="FirstAgent",
            role_description="desc",
            agent_id=first_agent_id,
            user_id=uuid4(),
            session_context=session_ctx,
        )
    )
    await kernel.handle(
        InvocationRequest(
            model=_make_model(),
            messages=[{"role": "user", "content": "coordinate"}],
            agent_name="SecondAgent",
            role_description="desc",
            agent_id=second_agent_id,
            user_id=uuid4(),
            session_context=session_ctx,
            invocation_scope="coordinator",
        )
    )

    assert len(build_calls) == 2
    assert f"{first_agent_id}:conversation" in build_calls
    assert f"{second_agent_id}:coordinator" in build_calls

    first_prompt = fake_client.calls[0]["messages"][0].content
    second_prompt = fake_client.calls[1]["messages"][0].content
    assert "FROZEN::FirstAgent::conversation" in first_prompt
    assert "FROZEN::SecondAgent::coordinator" in second_prompt
    assert "FROZEN::FirstAgent::conversation" not in second_prompt


@pytest.mark.asyncio
async def test_tool_expansion_rebuild_preserves_dynamic_memory_and_effective_suffix():
    from app.kernel.contracts import InvocationRequest, RuntimeConfig
    from app.kernel.engine import AgentKernel, KernelDependencies, ToolExpansionResult

    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {"id": "call_1", "function": {"name": "tool_search", "arguments": '{"query":"web_search"}'}}
                ],
                reasoning_content=None,
                usage={"total_tokens": 3},
            ),
            SimpleNamespace(content="done", tool_calls=[], reasoning_content=None, usage={"total_tokens": 3}),
        ]
    )

    async def resolve_tool_expansion(_request, _tool_name, _args):
        return ToolExpansionResult(
            tools=[
                {"type": "function", "function": {"name": "delegate_to_agent", "description": "", "parameters": {}}},
                {"type": "function", "function": {"name": "web_search", "description": "", "parameters": {}}},
            ],
            active_tool_groups=[{"name": "web_pack", "summary": "web research tools", "tools": ["web_search"]}],
        )

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda _agent_id: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=3),
            resolve_current_user_name=lambda _user_id: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "FROZEN",
            resolve_memory_context=lambda *_args, **_kwargs: "SNAPSHOT_BLOCK",
            resolve_retrieval_context=lambda *_args, **_kwargs: "RETRIEVAL_BLOCK",
            get_tools=lambda *_args, **_kwargs: [
                {"type": "function", "function": {"name": "tool_search", "description": "", "parameters": {}}},
            ],
            resolve_tool_expansion=resolve_tool_expansion,
            maybe_compress_messages=lambda messages, **_kwargs: messages,
            create_client=lambda _model: fake_client,
            execute_tool=lambda *_args, **_kwargs: "discovered",
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
            messages=[{"role": "user", "content": "coordinate this research"}],
            agent_name="Coordinator",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
            session_context=SessionContext(session_id="s-2", source="chat"),
            invocation_scope="coordinator",
        )
    )

    assert result.content == "done"
    assert [tool["function"]["name"] for tool in fake_client.calls[1]["tools"]] == [
        "delegate_to_agent",
        "web_search",
    ]

    expanded_system = fake_client.calls[1]["messages"][0].content
    expanded_dynamic = fake_client.calls[1]["messages"][-1].content
    assert "SNAPSHOT_BLOCK" not in expanded_system
    assert "RETRIEVAL_BLOCK" not in expanded_system
    assert "SNAPSHOT_BLOCK" in expanded_dynamic
    assert "RETRIEVAL_BLOCK" in expanded_dynamic
    assert "web_pack" in expanded_dynamic
    assert "coordinator mode" in expanded_dynamic.lower()


@pytest.mark.asyncio
async def test_coordinator_and_delegation_suffixes_have_independent_budgets(monkeypatch):
    from app.kernel.contracts import InvocationRequest, RuntimeConfig
    from app.kernel.engine import AgentKernel, KernelDependencies
    from app.runtime import coordinator

    tenant_id = uuid4()
    delegation_suffix = "DELEGATION_SUFFIX_START\n" + ("delegation body\n" * 700)
    coordinator_suffix = "COORDINATOR_SUFFIX_START\n" + ("coordinator body\n" * 700)
    monkeypatch.setattr(coordinator, "get_coordinator_prompt", lambda **_kwargs: coordinator_suffix)

    fake_client = _FakeClient(
        [SimpleNamespace(content="done", tool_calls=[], reasoning_content=None, usage={"total_tokens": 3})]
    )

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda _agent_id: RuntimeConfig(tenant_id=tenant_id, max_tool_rounds=3),
            resolve_current_user_name=lambda _user_id: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "FROZEN",
            resolve_memory_context=lambda *_args, **_kwargs: "",
            resolve_retrieval_context=lambda *_args, **_kwargs: "",
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

    await kernel.handle(
        InvocationRequest(
            model=_make_model(),
            messages=[{"role": "user", "content": "coordinate this work"}],
            agent_name="Coordinator",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
            session_context=SessionContext(session_id="s-suffix-budget", source="agent"),
            invocation_scope="coordinator",
            system_prompt_suffix=delegation_suffix,
        )
    )

    system_prompt = fake_client.calls[0]["messages"][0].content
    dynamic_prompt = fake_client.calls[0]["messages"][-1].content
    assert "DELEGATION_SUFFIX_START" not in system_prompt
    assert "COORDINATOR_SUFFIX_START" not in system_prompt
    assert "DELEGATION_SUFFIX_START" in dynamic_prompt
    assert "COORDINATOR_SUFFIX_START" in dynamic_prompt


@pytest.mark.asyncio
async def test_prompt_too_long_retry_preserves_dynamic_context_blocks(tmp_path):
    from app.kernel.contracts import InvocationRequest, RuntimeConfig
    from app.kernel.engine import AgentKernel, KernelDependencies
    from app.services.llm_utils import LLMError

    fake_client = _FakeClient(
        [
            LLMError("HTTP 400: context_length_exceeded - maximum context length exceeded"),
            SimpleNamespace(content="done", tool_calls=[], reasoning_content=None, usage={"total_tokens": 3}),
        ]
    )

    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda _agent_id: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=3),
            resolve_current_user_name=lambda _user_id: "Rocky",
            build_system_prompt=lambda *_args, **_kwargs: "FROZEN",
            resolve_memory_context=lambda *_args, **_kwargs: "SNAPSHOT_BLOCK",
            resolve_retrieval_context=lambda *_args, **_kwargs: "RETRIEVAL_BLOCK",
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
            messages=[
                {"role": "user", "content": "m1"},
                {"role": "assistant", "content": "m2"},
                {"role": "user", "content": "m3"},
                {"role": "assistant", "content": "m4"},
                {"role": "user", "content": "m5"},
            ],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
            session_context=SessionContext(session_id="s-ptl", source="chat", channel="web"),
            eviction_dir=tmp_path,
        )
    )

    assert result.content == "done"
    retry_system = fake_client.calls[1]["messages"][0].content
    retry_dynamic = fake_client.calls[1]["messages"][-1].content
    assert "SNAPSHOT_BLOCK" not in retry_system
    assert "RETRIEVAL_BLOCK" not in retry_system
    assert "SNAPSHOT_BLOCK" in retry_dynamic
    assert "RETRIEVAL_BLOCK" in retry_dynamic
    assert "Rocky" in retry_dynamic


# ── P1-1a: cache key purity (no user_name / context_window pollution) ──────


def _base_request(*, user_id, model=None):
    """Build a minimal InvocationRequest for cache-key tests."""
    from app.kernel.contracts import InvocationRequest

    return InvocationRequest(
        model=model or _make_model(),
        messages=[{"role": "user", "content": "hi"}],
        agent_name="StableAgent",
        role_description="desc",
        agent_id=uuid4(),
        user_id=user_id,
        session_context=SessionContext(session_id="s-cache", source="chat"),
    )


def test_cache_key_ignores_current_user_name():
    """Two different users hitting the same agent must produce the SAME key —
    user identity belongs in the dynamic suffix, not the frozen prefix."""
    from app.kernel.contracts import RuntimeConfig
    from app.kernel.engine import _build_frozen_prompt_cache_key

    cfg = RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=10)
    req = _base_request(user_id=uuid4())

    key_alice = _build_frozen_prompt_cache_key(req, cfg, current_user_name="Alice", rendered_prefix="FROZEN")
    key_bob = _build_frozen_prompt_cache_key(req, cfg, current_user_name="Bob", rendered_prefix="FROZEN")
    key_none = _build_frozen_prompt_cache_key(req, cfg, current_user_name=None, rendered_prefix="FROZEN")

    assert key_alice == key_bob == key_none, "current_user_name must not affect frozen-prefix cache key (P1-1a)"


def test_cache_key_is_disabled_without_verified_rendered_prefix():
    from app.kernel.contracts import RuntimeConfig
    from app.kernel.engine import _build_frozen_prompt_cache_key

    cfg = RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=10)
    req = _base_request(user_id=uuid4())

    assert _build_frozen_prompt_cache_key(req, cfg, current_user_name="Rocky") is None


def test_cache_key_ignores_context_window_tokens():
    """Same agent + same provider/model name but different max_input_tokens
    (e.g. fallback model swap) must hit the SAME cache key."""
    from app.kernel.contracts import RuntimeConfig
    from app.kernel.engine import _build_frozen_prompt_cache_key

    cfg = RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=10)
    user_id = uuid4()

    model_small = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="k",
        base_url=None,
        max_output_tokens=None,
        max_input_tokens=8192,
    )
    model_large = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="k",
        base_url=None,
        max_output_tokens=None,
        max_input_tokens=128_000,
    )

    # Build two requests differing only in max_input_tokens.
    req_small = _base_request(user_id=user_id, model=model_small)
    req_large = _base_request(user_id=user_id, model=model_large)
    # Force same agent_id so the only varying field is context window.
    req_large.agent_id = req_small.agent_id
    req_large.session_context = req_small.session_context

    key_small = _build_frozen_prompt_cache_key(req_small, cfg, current_user_name="x", rendered_prefix="FROZEN")
    key_large = _build_frozen_prompt_cache_key(req_large, cfg, current_user_name="x", rendered_prefix="FROZEN")
    assert key_small == key_large, "context_window_tokens must not affect frozen-prefix cache key (P1-1a)"


def test_cache_key_uses_rendered_content_instead_of_model_metadata():
    """A model swap matters only if it changes the bytes actually rendered."""
    from app.kernel.contracts import RuntimeConfig
    from app.kernel.engine import _build_frozen_prompt_cache_key

    cfg = RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=10)
    user_id = uuid4()

    req_a = _base_request(
        user_id=user_id,
        model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None, max_output_tokens=None),
    )
    req_b = _base_request(
        user_id=user_id,
        model=SimpleNamespace(
            provider="anthropic", model="claude-sonnet-4.6", api_key="k", base_url=None, max_output_tokens=None
        ),
    )
    req_b.agent_id = req_a.agent_id
    req_b.session_context = req_a.session_context

    key_a = _build_frozen_prompt_cache_key(req_a, cfg, current_user_name="x", rendered_prefix="FROZEN")
    key_b = _build_frozen_prompt_cache_key(req_b, cfg, current_user_name="x", rendered_prefix="FROZEN")
    assert key_a == key_b

    changed = _build_frozen_prompt_cache_key(req_b, cfg, current_user_name="x", rendered_prefix="FROZEN v2")
    assert changed != key_b


def test_cache_key_changes_on_standalone_system_prompt_change():
    from app.kernel.contracts import RuntimeConfig
    from app.kernel.engine import _build_frozen_prompt_cache_key

    cfg = RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=10)
    req_a = _base_request(user_id=uuid4())
    req_b = _base_request(user_id=req_a.user_id)
    req_b.agent_id = req_a.agent_id
    req_b.session_context = req_a.session_context
    req_a.standalone_system_prompt = "You are a read-only reviewer."
    req_b.standalone_system_prompt = "You are a code execution worker."

    key_a = _build_frozen_prompt_cache_key(
        req_a,
        cfg,
        current_user_name="x",
        rendered_prefix=req_a.standalone_system_prompt,
    )
    key_b = _build_frozen_prompt_cache_key(
        req_b,
        cfg,
        current_user_name="x",
        rendered_prefix=req_b.standalone_system_prompt,
    )

    assert key_a != key_b


def test_cache_key_ignores_optional_metadata_signatures_and_tracks_rendered_context():
    from app.kernel.contracts import RuntimeConfig
    from app.kernel.engine import _build_frozen_prompt_cache_key

    cfg = RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=10)
    req = _base_request(user_id=uuid4())

    req.session_context.metadata["frozen_context_signature"] = {
        "company": "company-v1",
        "org": "org-v1",
        "a2a": "a2a-v1",
        "configured_channels": ["feishu"],
    }
    key_v1 = _build_frozen_prompt_cache_key(req, cfg, current_user_name="x", rendered_prefix="COMPANY v1")
    req.session_context.metadata["frozen_context_signature"] = {
        "company": "company-v2",
        "org": "org-v1",
        "a2a": "a2a-v1",
        "configured_channels": ["feishu"],
    }
    same_rendered_key = _build_frozen_prompt_cache_key(req, cfg, current_user_name="x", rendered_prefix="COMPANY v1")
    key_v2 = _build_frozen_prompt_cache_key(req, cfg, current_user_name="x", rendered_prefix="COMPANY v2")

    assert key_v1 == same_rendered_key
    assert key_v1 != key_v2


def test_cache_key_changes_on_subagent_definition_change(tmp_path):
    from app.agents.subagent import SubagentSpec
    from app.agents.subagent_definition import definition_store_for_agent
    from app.kernel.contracts import RuntimeConfig
    from app.kernel.engine import _build_frozen_prompt_cache_key
    from app.runtime.prompt_sections import build_subagent_listing_section

    cfg = RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=10)
    req = _base_request(user_id=uuid4())
    req.session_context.metadata["agent_data_dir"] = str(tmp_path)
    store = definition_store_for_agent(req.agent_id, agent_data_dir=tmp_path)

    store.save(SubagentSpec(name="scout", description="Research scout", system_prompt="Find sources."))
    rendered_v1 = build_subagent_listing_section(agent_id=req.agent_id, agent_data_dir=tmp_path)
    key_v1 = _build_frozen_prompt_cache_key(req, cfg, current_user_name="x", rendered_prefix=rendered_v1)

    store.save(SubagentSpec(name="scout", description="Senior research scout", system_prompt="Find primary sources."))
    rendered_v2 = build_subagent_listing_section(agent_id=req.agent_id, agent_data_dir=tmp_path)
    key_v2 = _build_frozen_prompt_cache_key(req, cfg, current_user_name="x", rendered_prefix=rendered_v2)

    assert key_v1 != key_v2


def test_cache_key_version_bumped_for_sa_09():
    """Sanity check that the version constant reflects the schema change so
    persisted prefixes from older deployments invalidate cleanly on rollout."""
    from app.kernel.engine import _FROZEN_PROMPT_CACHE_VERSION

    assert _FROZEN_PROMPT_CACHE_VERSION == "frozen-v5"
