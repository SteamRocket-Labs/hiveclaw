from __future__ import annotations

import asyncio
from pathlib import Path
import json
from types import SimpleNamespace
from uuid import uuid4

import pytest


class _FakeRuntimeResolver:
    def __init__(self, context):
        self.context = context
        self.calls = []

    async def resolve(self, *, agent_id, user_id):
        self.calls.append((agent_id, user_id))
        return self.context


class _FakeGovernanceResolver:
    def __init__(self, governance_context, governance_dependencies):
        self.governance_context = governance_context
        self.governance_dependencies = governance_dependencies
        self.context_calls = []
        self.deps_calls = 0

    async def build_context(self, *, runtime_context, tool_name, arguments):
        self.context_calls.append((runtime_context, tool_name, arguments))
        return self.governance_context

    def build_dependencies(self):
        self.deps_calls += 1
        return self.governance_dependencies


class _FakeRegistry:
    def __init__(self, result):
        self.result = result
        self.calls = []

    async def try_execute(self, request):
        self.calls.append(request)
        return self.result


@pytest.mark.asyncio
async def test_tool_runtime_service_executes_through_registry_and_logs():
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
    )
    governance_context = ToolGovernanceContext(
        agent_id=context.agent_id,
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        tool_name="write_file",
        arguments={"path": "focus.md", "content": "x"},
    )
    runtime_resolver = _FakeRuntimeResolver(context)
    governance_resolver = _FakeGovernanceResolver(governance_context, SimpleNamespace())
    registry = _FakeRegistry("OK")
    logged = []
    ensured = []

    async def fake_run_governance(_context, _deps, *, event_callback=None):
        return None

    async def fake_log_activity(*args, **kwargs):
        logged.append((args, kwargs))

    service = ToolRuntimeService(
        runtime_resolver=runtime_resolver,
        governance_resolver=governance_resolver,
        registry=registry,
        ensure_registry=lambda: ensured.append(True),
        governance_runner=fake_run_governance,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=fake_log_activity,
    )

    result = await service.execute(
        "write_file",
        {"path": "focus.md", "content": "x"},
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert result == "OK"
    assert runtime_resolver.calls == [(context.agent_id, context.user_id)]
    assert governance_resolver.deps_calls == 1
    assert ensured == [True]
    assert registry.calls[0].tool_name == "write_file"
    assert logged and logged[0][0][0] == context.agent_id


@pytest.mark.asyncio
async def test_tool_runtime_service_returns_governance_block_without_registry_call():
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
    )
    runtime_resolver = _FakeRuntimeResolver(context)
    governance_resolver = _FakeGovernanceResolver(
        ToolGovernanceContext(
            agent_id=context.agent_id,
            user_id=context.user_id,
            tenant_id=context.tenant_id,
            tool_name="send_feishu_message",
            arguments={"message": "hi"},
        ),
        SimpleNamespace(),
    )
    registry = _FakeRegistry("SHOULD_NOT_RUN")

    async def fake_run_governance(_context, _deps, *, event_callback=None):
        return "BLOCKED"

    service = ToolRuntimeService(
        runtime_resolver=runtime_resolver,
        governance_resolver=governance_resolver,
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=fake_run_governance,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
    )

    result = await service.execute(
        "send_feishu_message",
        {"message": "hi"},
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert result == "BLOCKED"
    assert registry.calls == []


@pytest.mark.asyncio
async def test_tool_runtime_service_execute_direct_uses_direct_fallback():
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
    )
    runtime_resolver = _FakeRuntimeResolver(context)
    registry = _FakeRegistry(None)
    captured = {}

    async def fake_direct_fallback(tool_name, arguments, runtime_context):
        captured["tool_name"] = tool_name
        captured["arguments"] = arguments
        captured["context"] = runtime_context
        return "DIRECT"

    service = ToolRuntimeService(
        runtime_resolver=runtime_resolver,
        governance_resolver=_FakeGovernanceResolver(SimpleNamespace(), SimpleNamespace()),
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=fake_direct_fallback,
        activity_logger=None,
    )

    result = await service.execute_direct(
        "execute_code",
        {"code": "print(1)"},
        agent_id=context.agent_id,
    )

    assert result == "DIRECT"
    assert runtime_resolver.calls == [(context.agent_id, context.agent_id)]
    assert captured["tool_name"] == "execute_code"
    assert captured["context"] == context


def _extract_tool_error_payload(result: str) -> dict:
    marker = "<tool_error>"
    end_marker = "</tool_error>"
    start = result.index(marker) + len(marker)
    end = result.index(end_marker)
    return json.loads(result[start:end])


@pytest.mark.asyncio
async def test_tool_runtime_service_timeout_returns_structured_error():
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
    )

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(SimpleNamespace(), SimpleNamespace()),
        registry=_FakeRegistry(None),
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
    )

    async def slow_execute(self, *_args, **_kwargs):
        raise asyncio.TimeoutError

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ToolRuntimeService, "execute_with_context", slow_execute)

    try:
        result = await service.execute(
            "web_search",
            {"query": "quota issue"},
            agent_id=context.agent_id,
            user_id=context.user_id,
        )
    finally:
        monkeypatch.undo()

    payload = _extract_tool_error_payload(result)
    assert payload["tool_name"] == "web_search"
    assert payload["error_class"] == "timeout"
    assert payload["retryable"] is True


@pytest.mark.asyncio
async def test_tool_runtime_service_exception_returns_structured_error():
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
    )

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(SimpleNamespace(), SimpleNamespace()),
        registry=_FakeRegistry(None),
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
    )

    async def broken_execute(self, *_args, **_kwargs):
        raise ValueError("invalid upstream payload")

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ToolRuntimeService, "execute_with_context", broken_execute)

    try:
        result = await service.execute(
            "firecrawl_fetch",
            {"query": "test"},
            agent_id=context.agent_id,
            user_id=context.user_id,
        )
    finally:
        monkeypatch.undo()

    payload = _extract_tool_error_payload(result)
    assert payload["tool_name"] == "firecrawl_fetch"
    assert payload["error_class"] == "tool_execution_error"
    assert payload["retryable"] is False
    assert payload["provider"] == "runtime"


@pytest.mark.asyncio
async def test_tool_runtime_service_logs_structured_tool_errors():
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
    )
    logged = []

    async def fake_log_activity(*args, **kwargs):
        logged.append((args, kwargs))

    async def fake_fallback(*_args, **_kwargs):
        return (
            "❌ sample failure\n\n"
            '<tool_error>{"ok": false, "tool_name": "web_search", "error_class": "quota_or_billing", '
            '"message": "quota hit", "provider": "exa", "http_status": 402, "retryable": false}</tool_error>'
        )

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(SimpleNamespace(), SimpleNamespace()),
        registry=_FakeRegistry(None),
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=fake_fallback,
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=fake_log_activity,
    )

    result = await service.execute(
        "web_search",
        {"query": "quota issue"},
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert "<tool_error>" in result
    assert any(args[1] == "error" for args, _kwargs in logged)
