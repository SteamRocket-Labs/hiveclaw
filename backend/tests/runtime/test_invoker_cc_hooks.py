from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.fixture(autouse=True)
def _inject_invocation_quota(monkeypatch):
    async def allow_quota(*_args, **_kwargs):
        return None

    monkeypatch.setattr("app.runtime.invoker.check_user_token_quota", allow_quota, raising=False)


@pytest.mark.asyncio
async def test_invoker_emits_setup_before_user_prompt_submit_and_session_start(monkeypatch) -> None:
    from app.runtime.hooks import HookEvent, hook_registry
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent
    from app.runtime.session import SessionContext

    calls: list[tuple[str, str]] = []

    async def allow_quota(_user_id):
        return None

    class _FakeKernel:
        async def handle(self, request):
            calls.append(("kernel", request.messages[-1]["content"]))
            return SimpleNamespace(content="ok", tokens_used=0, final_tools=None, parts=[])

    hook_registry.clear()
    hook_registry.register(HookEvent.SETUP, lambda ctx: calls.append((ctx.event.value, ctx.session_id or "")))
    hook_registry.register(HookEvent.USER_PROMPT_SUBMIT, lambda ctx: calls.append((ctx.event.value, ctx.prompt or "")))
    hook_registry.register(HookEvent.SESSION_START, lambda ctx: calls.append((ctx.event.value, ctx.session_id or "")))
    hook_registry.register(HookEvent.SESSION_END, lambda ctx: calls.append((ctx.event.value, ctx.session_id or "")))
    hook_registry.register(HookEvent.TURN_STOP, lambda ctx: calls.append((ctx.event.value, ctx.session_id or "")))
    monkeypatch.setattr("app.runtime.invoker.get_agent_kernel", lambda: _FakeKernel())
    monkeypatch.setattr("app.runtime.invoker.check_user_token_quota", allow_quota, raising=False)

    try:
        result = await invoke_agent(
            AgentInvocationRequest(
                model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None),
                messages=[{"role": "user", "content": "hello"}],
                agent_name="Agent",
                role_description="desc",
                agent_id=uuid4(),
                user_id=uuid4(),
                memory_session_id="session-1",
                session_context=SessionContext(source="web", channel="chat", session_id="session-1"),
            )
        )
    finally:
        hook_registry.clear()

    assert result.content == "ok"
    assert calls == [
        ("setup", "session-1"),
        ("user_prompt_submit", "hello"),
        ("session_start", "session-1"),
        ("kernel", "hello"),
        ("session_end", "session-1"),
        ("turn_stop", "session-1"),
    ]


@pytest.mark.asyncio
async def test_invoker_injects_user_prompt_submit_additional_context(monkeypatch) -> None:
    from app.runtime.hooks import HookEvent, HookResult, hook_registry
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent
    from app.runtime.session import SessionContext

    seen_suffixes: list[str] = []

    async def allow_quota(_user_id):
        return None

    class _FakeKernel:
        async def handle(self, request):
            seen_suffixes.append(request.system_prompt_suffix)
            return SimpleNamespace(content="ok", tokens_used=0, final_tools=None, parts=[])

    async def prompt_hook(_ctx):
        return HookResult(additional_contexts=["Policy hint from hook."])

    hook_registry.clear()
    hook_registry.register(HookEvent.USER_PROMPT_SUBMIT, prompt_hook)
    monkeypatch.setattr("app.runtime.invoker.get_agent_kernel", lambda: _FakeKernel())
    monkeypatch.setattr("app.runtime.invoker.check_user_token_quota", allow_quota, raising=False)

    try:
        result = await invoke_agent(
            AgentInvocationRequest(
                model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None),
                messages=[{"role": "user", "content": "hello"}],
                agent_name="Agent",
                role_description="desc",
                agent_id=uuid4(),
                user_id=uuid4(),
                memory_session_id="session-1",
                session_context=SessionContext(source="web", channel="chat", session_id="session-1"),
            )
        )
    finally:
        hook_registry.clear()

    assert result.content == "ok"
    assert len(seen_suffixes) == 1
    assert "## Hook Additional Context" in seen_suffixes[0]
    assert "Policy hint from hook." in seen_suffixes[0]


@pytest.mark.asyncio
async def test_invoker_setup_can_block_before_model_execution(monkeypatch) -> None:
    from app.runtime.hooks import HookEvent, HookResult, hook_registry
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent
    from app.runtime.session import SessionContext

    kernel_calls = 0

    class _FakeKernel:
        async def handle(self, _request):
            nonlocal kernel_calls
            kernel_calls += 1
            return SimpleNamespace(content="unexpected", tokens_used=0, final_tools=None, parts=[])

    async def block_setup(_ctx):
        return HookResult(block=True, reason="workspace setup denied")

    hook_registry.clear()
    hook_registry.register(HookEvent.SETUP, block_setup)
    monkeypatch.setattr("app.runtime.invoker.get_agent_kernel", lambda: _FakeKernel())
    try:
        result = await invoke_agent(
            AgentInvocationRequest(
                model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None),
                messages=[{"role": "user", "content": "hello"}],
                agent_name="Agent",
                role_description="desc",
                agent_id=uuid4(),
                user_id=uuid4(),
                memory_session_id="session-1",
                session_context=SessionContext(source="web", channel="chat", session_id="session-1"),
            )
        )
    finally:
        hook_registry.clear()

    assert kernel_calls == 0
    assert result.terminal_reason.value == "hook_stopped"
    assert "workspace setup denied" in result.content


@pytest.mark.asyncio
async def test_system_prompt_emits_instructions_loaded_with_context_evidence(monkeypatch) -> None:
    import app.runtime.invoker as invoker
    from app.kernel.contracts import InvocationRequest
    from app.runtime.hooks import HookEvent, hook_registry
    from app.runtime.session import SessionContext

    agent_id = uuid4()
    tenant_id = uuid4()
    outer = invoker.AgentInvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[{"role": "user", "content": "hello"}],
        agent_name="Agent",
        role_description="desc",
        agent_id=agent_id,
        user_id=uuid4(),
        memory_session_id="session-1",
        session_context=SessionContext(
            source="web",
            channel="chat",
            session_id="session-1",
            metadata={"tenant_id": str(tenant_id), "runtime_task_id": str(uuid4())},
        ),
    )
    captured = []

    async def fake_build(*_args, **_kwargs):
        return "# Agent Instructions\nDo the work safely."

    async def capture(ctx):
        captured.append(ctx)

    monkeypatch.setattr(invoker, "_build_system_prompt", fake_build)
    hook_registry.clear()
    hook_registry.register(HookEvent.INSTRUCTIONS_LOADED, capture)
    try:
        kernel = invoker.get_agent_kernel(outer)
        prompt = await kernel._deps.build_system_prompt(
            InvocationRequest(
                model=outer.model,
                messages=outer.messages,
                agent_name=outer.agent_name,
                role_description=outer.role_description,
                agent_id=agent_id,
                user_id=outer.user_id,
                memory_session_id="session-1",
                session_context=outer.session_context,
            ),
            tenant_id,
            "",
            "Owner",
        )
    finally:
        hook_registry.clear()

    assert prompt.startswith("# Agent Instructions")
    assert len(captured) == 1
    assert captured[0].session_id == "session-1"
    assert captured[0].metadata["instruction_uri"] == f"agent://{agent_id}/context/frozen-prefix"
    assert captured[0].metadata["content_sha256"]


@pytest.mark.asyncio
async def test_session_start_consumes_initial_message_context_and_watch_paths(monkeypatch) -> None:
    from app.runtime.hooks import HookEvent, HookResult, hook_registry
    from app.runtime.invoker import AgentInvocationRequest, invoke_agent
    from app.runtime.session import SessionContext

    captured = {}

    class _FakeKernel:
        async def handle(self, request):
            captured["messages"] = list(request.messages)
            captured["suffix"] = request.system_prompt_suffix
            return SimpleNamespace(content="ok", tokens_used=0, final_tools=None, parts=[])

    async def session_start(_ctx):
        return HookResult(
            initial_user_message="Bootstrap the governed workspace.",
            additional_contexts=["Session policy context."],
            watch_paths=["workspace/**/*.md"],
        )

    context = SessionContext(source="web", channel="chat", session_id="session-1")
    hook_registry.clear()
    hook_registry.register(HookEvent.SESSION_START, session_start)
    monkeypatch.setattr("app.runtime.invoker.get_agent_kernel", lambda: _FakeKernel())
    try:
        result = await invoke_agent(
            AgentInvocationRequest(
                model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="k", base_url=None),
                messages=[{"role": "user", "content": "hello"}],
                agent_name="Agent",
                role_description="desc",
                agent_id=uuid4(),
                user_id=uuid4(),
                memory_session_id="session-1",
                session_context=context,
            )
        )
    finally:
        hook_registry.clear()

    assert result.content == "ok"
    assert captured["messages"] == [
        {"role": "user", "content": "Bootstrap the governed workspace.", "source": "session_start_hook"},
        {"role": "user", "content": "hello"},
    ]
    assert "Session policy context." in captured["suffix"]
    assert context.metadata["hook_watch_paths"] == ["workspace/**/*.md"]
