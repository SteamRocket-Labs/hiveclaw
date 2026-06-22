from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_invoker_emits_user_prompt_submit_before_session_start(monkeypatch) -> None:
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
    hook_registry.register(HookEvent.USER_PROMPT_SUBMIT, lambda ctx: calls.append((ctx.event.value, ctx.prompt or "")))
    hook_registry.register(HookEvent.SESSION_START, lambda ctx: calls.append((ctx.event.value, ctx.session_id or "")))
    hook_registry.register(HookEvent.SESSION_END, lambda ctx: calls.append((ctx.event.value, ctx.session_id or "")))
    hook_registry.register(HookEvent.SESSION_CLOSE, lambda ctx: calls.append((ctx.event.value, ctx.session_id or "")))
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
        ("user_prompt_submit", "hello"),
        ("session_start", "session-1"),
        ("kernel", "hello"),
        ("session_end", "session-1"),
        ("session_close", "session-1"),
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
