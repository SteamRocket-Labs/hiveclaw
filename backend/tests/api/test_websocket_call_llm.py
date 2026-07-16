from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.runtime.session import SessionContext


def test_history_rehydration_maps_stored_thinking_to_reasoning_content():
    from app.api.websocket import _conversation_from_history_messages

    entries = _conversation_from_history_messages(
        [
            SimpleNamespace(
                role="assistant",
                content="之前的回答",
                thinking="hidden reasoning",
                thinking_signature="sig-history",
            ),
        ]
    )

    assert entries == [
        {
            "role": "assistant",
            "content": "之前的回答",
            "reasoning_content": "hidden reasoning",
            "reasoning_signature": "sig-history",
        }
    ]


def test_history_rehydration_preserves_platform_like_assistant_bytes_without_inference():
    from app.api.websocket import _conversation_from_history_messages

    entries = _conversation_from_history_messages(
        [
            SimpleNamespace(role="user", content="请继续", thinking=None),
            SimpleNamespace(
                role="assistant", content="[LLM Error] AI 模型额度已耗尽，请联系管理员检查模型额度。", thinking=None
            ),
            SimpleNamespace(role="assistant", content="之前真实完成的结果", thinking=None),
        ]
    )

    assert entries == [
        {"role": "user", "content": "请继续"},
        {
            "role": "assistant",
            "content": "[LLM Error] AI 模型额度已耗尽，请联系管理员检查模型额度。",
        },
        {"role": "assistant", "content": "之前真实完成的结果"},
    ]


def test_websocket_idle_timeout_default_allows_long_wait(monkeypatch):
    from app.api.websocket import _get_ws_idle_timeout_seconds

    monkeypatch.delenv("WS_IDLE_TIMEOUT_SECONDS", raising=False)

    assert _get_ws_idle_timeout_seconds() >= 3600


@pytest.mark.asyncio
async def test_emit_ws_session_lifecycle_hook_api_role_publishes_runtime_control(monkeypatch):
    import app.api.websocket as websocket_api

    published = []
    emitted = []

    async def fake_publish(**kwargs):
        published.append(kwargs)

    async def fake_emit_hook(event, **kwargs):
        emitted.append((event, kwargs))

    monkeypatch.setattr(websocket_api, "_process_role", lambda: "api")
    monkeypatch.setattr("app.services.runtime_control_bus.publish_session_lifecycle_hook", fake_publish)
    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)

    await websocket_api._emit_ws_session_lifecycle_hook(
        "session_idle",
        agent_id=uuid4(),
        session_id="session-1",
        messages=[{"role": "user", "content": "hi"}],
        source="websocket",
        metadata={"idle_seconds": 180},
    )

    assert published
    assert published[0]["event"] == "session_idle"
    assert "messages" not in published[0]
    assert published[0]["metadata"]["message_count"] == 1
    assert emitted == []


@pytest.mark.asyncio
async def test_websocket_ping_control_message_replies_with_pong():
    from app.api.websocket import _handle_websocket_control_message

    sent = []

    class FakeWebSocket:
        async def send_json(self, payload):
            sent.append(payload)

    handled = await _handle_websocket_control_message(FakeWebSocket(), {"type": "ping"})

    assert handled is True
    assert sent == [{"type": "pong"}]


@pytest.mark.asyncio
async def test_websocket_idle_timeout_defers_when_session_run_is_active(monkeypatch):
    import app.api.websocket as websocket_api

    agent_id = uuid4()
    session_id = uuid4()
    captured = {}

    class FakeSessionContext:
        async def __aenter__(self):
            return "db"

        async def __aexit__(self, exc_type, exc, tb):
            return False

    async def fake_get_active_web_chat_run(**kwargs):
        captured.update(kwargs)
        return {"run_id": "run-1", "status": "running"}

    async def fake_resolve_tenant_for_agent(_agent_id, **_kwargs):
        return uuid4()

    monkeypatch.setattr(websocket_api, "tenant_scoped_session", lambda *a, **k: FakeSessionContext())
    monkeypatch.setattr(websocket_api, "resolve_tenant_for_agent", fake_resolve_tenant_for_agent)
    monkeypatch.setattr(websocket_api, "get_active_web_chat_run", fake_get_active_web_chat_run)

    assert await websocket_api._has_active_web_chat_run(agent_id, session_id) is True
    assert captured == {
        "db": "db",
        "agent_id": agent_id,
        "session_id": session_id,
    }


@pytest.mark.asyncio
async def test_call_llm_delegates_to_runtime_invoker(monkeypatch):
    from app.api.websocket import call_llm

    captured = {}
    cancel_event = asyncio.Event()
    fallback_model = SimpleNamespace(
        provider="anthropic",
        model="claude-sonnet",
        api_key="fallback",
        base_url=None,
        max_output_tokens=None,
    )

    async def fake_invoke_agent(request):
        captured["request"] = request
        return SimpleNamespace(content="runtime-result")

    monkeypatch.setattr("app.api.websocket.invoke_agent", fake_invoke_agent)

    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="key",
        base_url=None,
        max_output_tokens=None,
    )

    result = await call_llm(
        model=model,
        messages=[{"role": "user", "content": "hello"}],
        agent_name="Agent",
        role_description="desc",
        agent_id=uuid4(),
        user_id=uuid4(),
        supports_vision=True,
        session_id="session-1",
        memory_messages=[{"role": "user", "content": "hello"}],
        memory_context="MEM",
        cancel_event=cancel_event,
        fallback_model=fallback_model,
    )

    assert result == "runtime-result"
    assert captured["request"].model is model
    assert captured["request"].fallback_model is fallback_model
    assert captured["request"].cancel_event is cancel_event
    assert captured["request"].supports_vision is True
    assert captured["request"].memory_session_id == "session-1"
    assert captured["request"].memory_messages == [{"role": "user", "content": "hello"}]
    assert captured["request"].memory_context == ""
    assert captured["request"].session_context is not None
    assert captured["request"].session_context.session_id == "session-1"
    assert captured["request"].session_context.source == "web"
    assert captured["request"].session_context.channel == "web"
    assert captured["request"].emit_turn_stop is False


@pytest.mark.asyncio
async def test_call_llm_strips_upstream_system_messages_and_passes_execution_identity(monkeypatch):
    from app.api.websocket import call_llm
    from app.kernel.contracts import ExecutionIdentityRef

    captured = {}

    async def fake_invoke_agent(request):
        captured["request"] = request
        return SimpleNamespace(content="runtime-result")

    monkeypatch.setattr("app.api.websocket.invoke_agent", fake_invoke_agent)

    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="key",
        base_url=None,
        max_output_tokens=None,
    )
    execution_identity = ExecutionIdentityRef(
        identity_type="delegated_user",
        identity_id=uuid4(),
        label="Rocky via web",
    )

    result = await call_llm(
        model=model,
        messages=[
            {"role": "system", "content": "legacy system prompt"},
            {"role": "user", "content": "hello"},
        ],
        agent_name="Agent",
        role_description="desc",
        agent_id=uuid4(),
        user_id=uuid4(),
        session_id="session-2",
        memory_messages=[
            {"role": "system", "content": "legacy system prompt"},
            {"role": "user", "content": "hello"},
        ],
        execution_identity=execution_identity,
    )

    assert result == "runtime-result"
    assert captured["request"].messages == [{"role": "user", "content": "hello"}]
    assert captured["request"].memory_messages == [{"role": "user", "content": "hello"}]
    assert captured["request"].execution_identity is execution_identity


@pytest.mark.asyncio
async def test_call_llm_reuses_provided_session_context(monkeypatch):
    from app.api.websocket import call_llm

    captured = {}
    session_context = SessionContext(session_id="session-reused", source="websocket", channel="web")
    session_context.prompt_prefix = "CACHED_PREFIX"
    session_context.active_skills.append("Skill A")

    async def fake_invoke_agent(request):
        captured["request"] = request
        return SimpleNamespace(content="runtime-result")

    monkeypatch.setattr("app.api.websocket.invoke_agent", fake_invoke_agent)

    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="key",
        base_url=None,
        max_output_tokens=None,
    )

    result = await call_llm(
        model=model,
        messages=[{"role": "user", "content": "hello"}],
        agent_name="Agent",
        role_description="desc",
        agent_id=uuid4(),
        user_id=uuid4(),
        session_id="session-reused",
        session_context=session_context,
    )

    assert result == "runtime-result"
    assert captured["request"].session_context is session_context
    assert captured["request"].session_context.prompt_prefix == "CACHED_PREFIX"
    assert captured["request"].session_context.active_skills == ["Skill A"]


@pytest.mark.asyncio
async def test_call_llm_auto_close_emits_session_close(monkeypatch):
    from app.api.websocket import call_llm
    from app.runtime.hooks import HookEvent

    captured = {"events": []}

    async def fake_invoke_agent(request):
        captured["request"] = request
        return SimpleNamespace(content="runtime-result")

    async def fake_emit_hook(event, **kwargs):
        captured["events"].append((event, kwargs))

    monkeypatch.setattr("app.api.websocket.invoke_agent", fake_invoke_agent)
    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)

    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="key",
        base_url=None,
        max_output_tokens=None,
    )

    result = await call_llm(
        model=model,
        messages=[{"role": "user", "content": "hello"}],
        agent_name="Agent",
        role_description="desc",
        agent_id=uuid4(),
        user_id=uuid4(),
        session_id="session-auto-close",
        memory_messages=[{"role": "user", "content": "hello"}],
        auto_close_session=True,
        session_source="feishu",
        session_channel="feishu",
    )

    assert result == "runtime-result"
    assert len(captured["events"]) == 1
    event, payload = captured["events"][0]
    assert event == HookEvent.TURN_STOP
    assert payload["session_id"] == "session-auto-close"
    assert payload["source"] == "feishu"
    assert payload["metadata"]["reason"] == "invoke_complete"
    assert payload["metadata"]["checkpoint_kind"] == "user_turn_stop"
    assert payload["messages"][-1] == {"role": "assistant", "content": "runtime-result"}


@pytest.mark.asyncio
async def test_call_llm_auto_close_emits_turn_abort_from_typed_terminal_reason(monkeypatch):
    from app.api.websocket import call_llm
    from app.kernel.contracts import TerminalReason
    from app.runtime.hooks import HookEvent

    captured = {"events": []}

    async def fake_invoke_agent(request):
        captured["request"] = request
        return SimpleNamespace(
            content="Provider failure display without a magic prefix",
            terminal_reason=TerminalReason.PROVIDER_ERROR,
        )

    async def fake_emit_hook(event, **kwargs):
        captured["events"].append((event, kwargs))

    monkeypatch.setattr("app.api.websocket.invoke_agent", fake_invoke_agent)
    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)

    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="key",
        base_url=None,
        max_output_tokens=None,
    )

    result = await call_llm(
        model=model,
        messages=[{"role": "user", "content": "hello"}],
        agent_name="Agent",
        role_description="desc",
        agent_id=uuid4(),
        user_id=uuid4(),
        session_id="session-auto-abort",
        memory_messages=[{"role": "user", "content": "hello"}],
        auto_close_session=True,
        session_source="feishu",
        session_channel="feishu",
    )

    assert result == "Provider failure display without a magic prefix"
    assert captured["request"].emit_turn_stop is False
    assert len(captured["events"]) == 1
    event, payload = captured["events"][0]
    assert event == HookEvent.TURN_ABORT
    assert payload["session_id"] == "session-auto-abort"
    assert payload["source"] == "feishu"
    assert payload["metadata"]["reason"] == "invoke_failed"
    assert payload["metadata"]["checkpoint_kind"] == "turn_abort"
    assert payload["metadata"]["semantic_memory_eligible"] is False
    assert payload["messages"] == [{"role": "user", "content": "hello"}]


@pytest.mark.asyncio
async def test_call_llm_auto_close_does_not_infer_abort_from_model_authored_prefix(monkeypatch):
    from app.api.websocket import call_llm
    from app.kernel.contracts import TerminalReason
    from app.runtime.hooks import HookEvent

    captured = {"events": []}

    async def fake_invoke_agent(_request):
        return SimpleNamespace(
            content="[LLM Error] is the literal format being discussed by the model.",
            terminal_reason=TerminalReason.TURN_STOP,
        )

    async def fake_emit_hook(event, **kwargs):
        captured["events"].append((event, kwargs))

    monkeypatch.setattr("app.api.websocket.invoke_agent", fake_invoke_agent)
    monkeypatch.setattr("app.runtime.hooks.emit_hook", fake_emit_hook)
    model = SimpleNamespace(
        provider="openai",
        model="gpt-4.1",
        api_key="key",
        base_url=None,
        max_output_tokens=None,
    )

    result = await call_llm(
        model=model,
        messages=[{"role": "user", "content": "Explain the legacy format"}],
        agent_name="Agent",
        role_description="desc",
        agent_id=uuid4(),
        user_id=uuid4(),
        session_id="session-prefix-is-content",
        memory_messages=[{"role": "user", "content": "Explain the legacy format"}],
        auto_close_session=True,
    )

    assert result == "[LLM Error] is the literal format being discussed by the model."
    event, payload = captured["events"][0]
    assert event == HookEvent.TURN_STOP
    assert payload["messages"][-1]["content"] == result
