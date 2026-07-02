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

    async def resolve(self, *, agent_id, user_id, session_id=None):
        self.calls.append((agent_id, user_id, session_id))
        return self.context


class _FakeGovernanceResolver:
    def __init__(self, governance_context, governance_dependencies):
        self.governance_context = governance_context
        self.governance_dependencies = governance_dependencies
        self.context_calls = []
        self.deps_calls = 0

    async def build_context(self, *, runtime_context, tool_name, arguments, delegation_token=None):
        self.context_calls.append((runtime_context, tool_name, arguments, delegation_token))
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
        arguments={"path": "workspace/notes.md", "content": "x"},
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
        {"path": "workspace/notes.md", "content": "x"},
        agent_id=context.agent_id,
        user_id=context.user_id,
        delegation_token="delegation-token-1",
    )

    assert result == "OK"
    assert runtime_resolver.calls == [(context.agent_id, context.user_id, None)]
    assert governance_resolver.deps_calls == 1
    assert governance_resolver.context_calls[0][3] == "delegation-token-1"
    assert ensured == [True]
    assert registry.calls[0].tool_name == "write_file"
    assert logged and logged[0][0][0] == context.agent_id
    states = [record["lifecycle_state"] for record in context.tool_lifecycle_records]
    assert states == ["created", "validated", "governed", "preflight", "executing", "completed"]
    tool_call_ids = {record["tool_call_id"] for record in context.tool_lifecycle_records}
    assert len(tool_call_ids) == 1
    assert context.tool_execution_frames[0]["status"] == "executing"
    assert context.tool_execution_frames[-1]["status"] == "completed"
    assert context.tool_execution_frames[-1]["tool_call_id"] in tool_call_ids
    assert logged[0][1]["detail"]["tool_call_lifecycle"]["tool_call_id"] in tool_call_ids
    assert logged[0][1]["detail"]["tool_execution_frame"]["status"] == "completed"


@pytest.mark.asyncio
async def test_tool_runtime_service_exports_truth_evidence_to_trace_metadata_sink():
    from app.runtime.ccplus_contracts import TruthEvidencePackV1
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id=str(uuid4()),
        workspace=Path("/tmp/ws"),
    )
    governance_context = ToolGovernanceContext(
        agent_id=context.agent_id,
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        tool_name="write_file",
        arguments={"path": "workspace/notes.md", "content": "x"},
    )
    evidence = TruthEvidencePackV1(
        evidence_id="truth://policy/write-file",
        query="write_file workspace policy",
        source_refs=("knowledge://policy/workspace.md",),
        confidence=0.91,
    )

    class FakeTruthSearch:
        async def search(self, *_args, **_kwargs):
            return (evidence,)

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(governance_context, SimpleNamespace()),
        registry=_FakeRegistry("OK"),
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        truth_search_service=FakeTruthSearch(),
    )
    trace_metadata_sink: dict[str, object] = {}

    result = await service.execute(
        "write_file",
        {"path": "workspace/notes.md", "content": "x"},
        agent_id=context.agent_id,
        user_id=context.user_id,
        trace_metadata_sink=trace_metadata_sink,
    )

    assert result == "OK"
    assert trace_metadata_sink["evidence_refs"] == ["truth://policy/write-file"]
    assert trace_metadata_sink["truth_evidence"] == [
        {
            "evidence_id": "truth://policy/write-file",
            "query": "write_file workspace policy",
            "source_refs": ["knowledge://policy/workspace.md"],
            "provider": "knowledge_core",
            "confidence": 0.91,
        }
    ]


@pytest.mark.asyncio
async def test_tool_runtime_service_threads_origin_channel_to_runtime_resolver():
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
        arguments={"path": "workspace/notes.md", "content": "x"},
    )

    class RuntimeResolverWithOriginChannel:
        def __init__(self):
            self.calls = []

        async def resolve(self, *, agent_id, user_id, origin_channel=None):
            self.calls.append((agent_id, user_id, origin_channel))
            context.origin_channel = origin_channel
            return context

    runtime_resolver = RuntimeResolverWithOriginChannel()
    service = ToolRuntimeService(
        runtime_resolver=runtime_resolver,
        governance_resolver=_FakeGovernanceResolver(governance_context, SimpleNamespace()),
        registry=_FakeRegistry("OK"),
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
    )

    result = await service.execute(
        "write_file",
        {"path": "workspace/notes.md", "content": "x"},
        agent_id=context.agent_id,
        user_id=context.user_id,
        origin_channel="feishu",
    )

    assert result == "OK"
    assert runtime_resolver.calls == [(context.agent_id, context.user_id, "feishu")]


@pytest.mark.asyncio
async def test_tool_runtime_service_blocks_disabled_l2_pack_at_call_time(monkeypatch):
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id=str(uuid4()),
        workspace=Path("/tmp/ws"),
    )
    governance_context = ToolGovernanceContext(
        agent_id=context.agent_id,
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        tool_name="exa_search",
        arguments={"query": "openai sdk"},
    )
    governance_resolver = _FakeGovernanceResolver(governance_context, SimpleNamespace())
    registry = _FakeRegistry("SHOULD_NOT_RUN")

    async def fake_run_governance(_context, _deps, *, event_callback=None):
        return None

    monkeypatch.setattr("app.tools.service.is_l2_tool", lambda tool_name: tool_name == "exa_search", raising=False)
    monkeypatch.setattr("app.tools.service.policy_pack_names_for_tool", lambda tool_name: ("web_pack",), raising=False)
    monkeypatch.setattr("app.tools.service.is_pack_enabled", lambda policies, pack_name: bool(policies.get(pack_name)), raising=False)

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=governance_resolver,
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=fake_run_governance,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
        pack_policy_loader=lambda runtime_context: {"web_pack": False},
    )

    result = await service.execute(
        "exa_search",
        {"query": "openai sdk"},
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert "<tool_error>" in result
    assert "extension_disabled" in result
    assert "web_pack" in result
    assert registry.calls == []
    assert governance_resolver.context_calls == []


@pytest.mark.asyncio
async def test_tool_runtime_service_blocks_disabled_l2_pack_in_execute_with_context(monkeypatch):
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id=str(uuid4()),
        workspace=Path("/tmp/ws"),
    )
    registry = _FakeRegistry("SHOULD_NOT_RUN")

    monkeypatch.setattr("app.tools.service.is_l2_tool", lambda tool_name: tool_name == "exa_search", raising=False)
    monkeypatch.setattr("app.tools.service.policy_pack_names_for_tool", lambda tool_name: ("web_pack",), raising=False)
    monkeypatch.setattr("app.tools.service.is_pack_enabled", lambda policies, pack_name: bool(policies.get(pack_name)), raising=False)

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(SimpleNamespace(), SimpleNamespace()),
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
        pack_policy_loader=lambda runtime_context: {"web_pack": False},
    )

    result = await service.execute_with_context(
        "exa_search",
        {"query": "openai sdk"},
        context,
    )

    assert "<tool_error>" in result
    assert "extension_disabled" in result
    assert "web_pack" in result
    assert registry.calls == []


@pytest.mark.asyncio
async def test_tool_runtime_service_does_not_l2_block_core_command_wrappers():
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id=str(uuid4()),
        workspace=Path("/tmp/ws"),
    )
    governance_context = ToolGovernanceContext(
        agent_id=context.agent_id,
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        tool_name="task_list",
        arguments={},
    )
    governance_resolver = _FakeGovernanceResolver(governance_context, SimpleNamespace())
    registry = _FakeRegistry("OK")

    async def fake_run_governance(_context, _deps, *, event_callback=None):
        return None

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=governance_resolver,
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=fake_run_governance,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
        pack_policy_loader=lambda runtime_context: {"command_pack": False},
    )

    result = await service.execute(
        "task_list",
        {},
        agent_id=context.agent_id,
        user_id=context.user_id,
        emit_runtime_hooks=False,
    )

    assert result == "OK"
    assert registry.calls
    assert governance_resolver.context_calls


@pytest.mark.asyncio
async def test_tool_runtime_service_emits_hooks_and_revalidates_modified_args():
    from app.runtime.hooks import HookEvent, HookResult, hook_registry
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
        session_id="session-1",
    )
    governance_context = ToolGovernanceContext(
        agent_id=context.agent_id,
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        tool_name="write_file",
        arguments={"path": "workspace/notes.md", "content": "x"},
    )
    governance_resolver = _FakeGovernanceResolver(governance_context, SimpleNamespace())
    registry = _FakeRegistry("OK")
    hook_events: list[tuple[str, dict | None]] = []

    def pre_hook(ctx):
        hook_events.append((ctx.event.value, dict(ctx.tool_args or {})))
        return HookResult(modified_args={"path": "workspace/safe.md", "content": ctx.tool_args["content"]})

    def post_hook(ctx):
        hook_events.append((ctx.event.value, dict(ctx.tool_args or {})))
        return None

    async def fake_run_governance(_context, _deps, *, event_callback=None):
        return None

    hook_registry.clear()
    hook_registry.register(HookEvent.PRE_TOOL_USE, pre_hook)
    hook_registry.register(HookEvent.POST_TOOL_USE, post_hook)
    try:
        service = ToolRuntimeService(
            runtime_resolver=_FakeRuntimeResolver(context),
            governance_resolver=governance_resolver,
            registry=registry,
            ensure_registry=lambda: None,
            governance_runner=fake_run_governance,
            fallback_executor=lambda *_args, **_kwargs: "fallback",
            direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
            activity_logger=None,
        )

        result = await service.execute(
            "write_file",
            {"path": "workspace/notes.md", "content": "x"},
            agent_id=context.agent_id,
            user_id=context.user_id,
        )
    finally:
        hook_registry.clear()

    assert result == "OK"
    assert governance_resolver.context_calls[0][2]["path"] == "workspace/safe.md"
    assert registry.calls[0].arguments["path"] == "workspace/safe.md"
    assert hook_events == [
        ("pre_tool_use", {"path": "workspace/notes.md", "content": "x"}),
        ("post_tool_use", {"path": "workspace/safe.md", "content": "x"}),
    ]


@pytest.mark.asyncio
async def test_tool_runtime_service_blocks_hook_modified_args_that_violate_schema():
    from app.runtime.hooks import HookEvent, HookResult, hook_registry
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
        session_id="session-1",
    )
    governance_context = ToolGovernanceContext(
        agent_id=context.agent_id,
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        tool_name="write_file",
        arguments={"path": "workspace/notes.md", "content": "x"},
    )
    governance_resolver = _FakeGovernanceResolver(governance_context, SimpleNamespace())
    registry = _FakeRegistry("SHOULD_NOT_RUN")

    def pre_hook(_ctx):
        return HookResult(modified_args={"path": "workspace/safe.md"})

    hook_registry.clear()
    hook_registry.register(HookEvent.PRE_TOOL_USE, pre_hook)
    try:
        service = ToolRuntimeService(
            runtime_resolver=_FakeRuntimeResolver(context),
            governance_resolver=governance_resolver,
            registry=registry,
            ensure_registry=lambda: None,
            governance_runner=lambda *_args, **_kwargs: None,
            fallback_executor=lambda *_args, **_kwargs: "fallback",
            direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
            activity_logger=None,
        )

        result = await service.execute(
            "write_file",
            {"path": "workspace/notes.md", "content": "x"},
            agent_id=context.agent_id,
            user_id=context.user_id,
        )
    finally:
        hook_registry.clear()

    assert "<tool_error>" in result
    assert "invalid_tool_arguments" in result
    assert "content" in result
    assert governance_resolver.context_calls == []
    assert registry.calls == []


@pytest.mark.asyncio
async def test_tool_runtime_service_threads_session_permission_context_into_delegation():
    from app.runtime.ccplus_contracts import PermissionMode, PermissionProfileV1
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
        session_id="parent-session-1",
        permission_profile=PermissionProfileV1(
            mode=PermissionMode.BYPASS_PERMISSIONS,
            allowed_tools=("web_search", "feishu_doc_read"),
        ),
    )
    governance_context = ToolGovernanceContext(
        agent_id=context.agent_id,
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        tool_name="delegate_to_agent",
        arguments={"agent_name": "Researcher", "message": "go"},
    )
    governance_resolver = _FakeGovernanceResolver(governance_context, SimpleNamespace())
    registry = _FakeRegistry("TASK")

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=governance_resolver,
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
    )

    result = await service.execute(
        "delegate_to_agent",
        {"agent_name": "Researcher", "message": "go"},
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert result == "TASK"
    governance_arguments = governance_resolver.context_calls[0][2]
    executed_arguments = registry.calls[0].arguments
    for arguments in (governance_arguments, executed_arguments):
        assert arguments["parent_session_id"] == "parent-session-1"
        assert arguments["_permission_profile"]["mode"] == "bypassPermissions"
        assert arguments["_permission_profile"]["allowed_tools"] == [
            "web_search",
            "feishu_doc_read",
        ]


@pytest.mark.asyncio
async def test_tool_runtime_service_threads_session_permission_context_into_agent_message():
    from app.runtime.ccplus_contracts import PermissionMode, PermissionProfileV1
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
        session_id="parent-session-1",
        permission_profile=PermissionProfileV1(
            mode=PermissionMode.BYPASS_PERMISSIONS,
            allowed_tools=("web_search", "feishu_doc_read"),
        ),
    )
    governance_context = ToolGovernanceContext(
        agent_id=context.agent_id,
        user_id=context.user_id,
        tenant_id=context.tenant_id,
        tool_name="send_message_to_agent",
        arguments={"agent_name": "Knowledge", "message": "查一下飞书知识库"},
    )
    governance_resolver = _FakeGovernanceResolver(governance_context, SimpleNamespace())
    registry = _FakeRegistry("MESSAGE")

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=governance_resolver,
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
    )

    result = await service.execute(
        "send_message_to_agent",
        {"agent_name": "Knowledge", "message": "查一下飞书知识库"},
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert result == "MESSAGE"
    governance_arguments = governance_resolver.context_calls[0][2]
    executed_arguments = registry.calls[0].arguments
    for arguments in (governance_arguments, executed_arguments):
        assert arguments["parent_session_id"] == "parent-session-1"
        assert arguments["_permission_profile"]["mode"] == "bypassPermissions"
        assert arguments["_permission_profile"]["allowed_tools"] == [
            "web_search",
            "feishu_doc_read",
        ]


@pytest.mark.asyncio
async def test_tool_runtime_service_logs_readonly_tool_calls():
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
        tool_name="read_file",
        arguments={"path": "workspace/notes.md"},
    )
    logged = []

    async def fake_log_activity(*args, **kwargs):
        logged.append((args, kwargs))

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(governance_context, SimpleNamespace()),
        registry=_FakeRegistry("file contents"),
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=fake_log_activity,
    )

    result = await service.execute(
        "read_file",
        {"path": "workspace/notes.md"},
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert result == "file contents"
    assert logged
    assert logged[0][0][1] == "tool_call"
    assert logged[0][1]["tenant_id"] == context.tenant_id
    assert logged[0][1]["detail"]["tool"] == "read_file"


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
        {"language": "python", "code": "print(1)"},
        agent_id=context.agent_id,
    )

    assert result == "DIRECT"
    assert runtime_resolver.calls == [(context.agent_id, context.agent_id, None)]
    assert captured["tool_name"] == "execute_code"
    assert captured["context"] == context
    assert [record["lifecycle_state"] for record in context.tool_lifecycle_records] == [
        "created",
        "validated",
        "executing",
        "completed",
    ]
    assert context.tool_execution_frames[-1]["status"] == "completed"


@pytest.mark.asyncio
async def test_tool_runtime_service_execute_approved_logs_approval_metadata():
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    agent_id = uuid4()
    approved_by = uuid4()
    approval_id = uuid4()
    context = ToolExecutionContext(
        agent_id=agent_id,
        user_id=approved_by,
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
    )
    logged = []

    async def fake_log_activity(*args, **kwargs):
        logged.append((args, kwargs))

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(SimpleNamespace(), SimpleNamespace()),
        registry=_FakeRegistry("APPROVED"),
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=fake_log_activity,
    )

    result = await service.execute_approved(
        "write_file",
        {"path": "workspace/notes.md", "content": "done"},
        agent_id=agent_id,
        approved_by_user_id=approved_by,
        approval_id=approval_id,
    )

    assert result == "APPROVED"
    assert service.runtime_resolver.calls == [(agent_id, approved_by, None)]
    assert logged[0][0][1] == "tool_call_approved"
    assert logged[0][1]["detail"]["approved"] is True
    assert logged[0][1]["detail"]["approved_by_user_id"] == str(approved_by)
    assert logged[0][1]["detail"]["approval_id"] == str(approval_id)
    assert [record["lifecycle_state"] for record in context.tool_lifecycle_records] == [
        "created",
        "validated",
        "executing",
        "completed",
    ]
    assert logged[0][1]["detail"]["tool_call_lifecycle"]["lifecycle_state"] == "completed"
    assert logged[0][1]["detail"]["tool_execution_frame"]["status"] == "completed"


@pytest.mark.asyncio
async def test_tool_runtime_service_execute_approved_logs_readonly_tools():
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    agent_id = uuid4()
    approved_by = uuid4()
    context = ToolExecutionContext(
        agent_id=agent_id,
        user_id=approved_by,
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
    )
    logged = []

    async def fake_log_activity(*args, **kwargs):
        logged.append((args, kwargs))

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(SimpleNamespace(), SimpleNamespace()),
        registry=_FakeRegistry("read result"),
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=fake_log_activity,
    )

    result = await service.execute_approved(
        "read_file",
        {"path": "workspace/notes.md"},
        agent_id=agent_id,
        approved_by_user_id=approved_by,
    )

    assert result == "read result"
    assert logged
    assert logged[0][0][1] == "tool_call_approved"
    assert logged[0][1]["tenant_id"] == context.tenant_id
    assert logged[0][1]["detail"]["tool"] == "read_file"


def _extract_tool_error_payload(result: str) -> dict:
    marker = "<tool_error>"
    end_marker = "</tool_error>"
    start = result.index(marker) + len(marker)
    end = result.index(end_marker)
    return json.loads(result[start:end])


@pytest.mark.asyncio
async def test_tool_runtime_service_preflight_asks_before_external_visible_tool():
    from app.runtime.ccplus_contracts import TruthEvidencePackV1
    from app.services.decision_trace import DecisionTraceStore
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
        session_id="session-1",
    )
    registry = _FakeRegistry("SHOULD_NOT_RUN")
    traces = DecisionTraceStore()

    class _FakeTruthSearch:
        async def search(self, *_args, **_kwargs):
            return [
                TruthEvidencePackV1(
                    evidence_id="truth://policy/email-confirmation",
                    query="send external message via send_feishu_message",
                    source_refs=("knowledge://policy/email",),
                    citations=("policy/email",),
                    tenant_id="tenant-1",
                    trace_refs=("trace://truth/email-confirmation",),
                )
            ]

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(SimpleNamespace(), SimpleNamespace()),
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
        decision_trace_store=traces,
        truth_search_service=_FakeTruthSearch(),
    )

    result = await service.execute(
        "send_feishu_message",
        {"message": "Send external vendor reply about pricing"},
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert result.startswith("[Preflight:ask]")
    assert "send_feishu_message" in result
    assert "checkpoint=" in result
    assert "decision=decision/" in result
    assert registry.calls == []
    decisions = traces.decisions()
    assert len(decisions) == 1
    assert decisions[0].chosen == "ask"
    assert decisions[0].tenant_id == "tenant-1"
    assert decisions[0].agent_id == str(context.agent_id)
    assert decisions[0].user_id == str(context.user_id)
    assert decisions[0].session_id == "session-1"
    assert decisions[0].tool_name == "send_feishu_message"
    assert decisions[0].preflight["decision"] == "ask"
    assert decisions[0].preflight["checkpoint_id"]
    assert decisions[0].preflight["evidence_refs"] == "truth://policy/email-confirmation"


@pytest.mark.asyncio
async def test_tool_runtime_service_allows_delegated_user_feishu_message():
    from app.core.execution_context import ExecutionIdentity
    from app.runtime.ccplus_contracts import TruthEvidencePackV1
    from app.services.decision_trace import DecisionTraceStore
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
        execution_identity=ExecutionIdentity(
            identity_type="delegated_user",
            identity_id=uuid4(),
            label="Rocky via web",
        ),
    )
    registry = _FakeRegistry("SENT")
    traces = DecisionTraceStore()

    class _SuccessfulTruthSearch:
        async def search(self, *_args, **_kwargs):
            return [
                TruthEvidencePackV1(
                    evidence_id="truth://policy/delegated-user-send",
                    query="send_feishu_message",
                    source_refs=("knowledge://policy/delegated-user-send",),
                    citations=("policy/delegated-user-send",),
                    tenant_id="tenant-1",
                    trace_refs=("trace://truth/delegated-user-send",),
                )
            ]

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(SimpleNamespace(), SimpleNamespace()),
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
        decision_trace_store=traces,
        truth_search_service=_SuccessfulTruthSearch(),
    )

    result = await service.execute(
        "send_feishu_message",
        {
            "open_id": "ou_example",
            "message": "明天上午 9:00-9:30，常春藤办公室，讨论 Agent 新需求，有空吗？",
        },
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert result == "SENT"
    assert len(registry.calls) == 1
    assert registry.calls[0].tool_name == "send_feishu_message"
    assert traces.decisions() == []


@pytest.mark.asyncio
async def test_tool_runtime_service_preflight_refuses_credential_arguments():
    from app.services.decision_trace import DecisionTraceStore
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    context = ToolExecutionContext(
        agent_id=uuid4(),
        user_id=uuid4(),
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
    )
    registry = _FakeRegistry("SHOULD_NOT_RUN")
    traces = DecisionTraceStore()

    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(SimpleNamespace(), SimpleNamespace()),
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
        decision_trace_store=traces,
    )

    result = await service.execute(
        "write_file",
        {"path": "secrets.txt", "content": "api_key=sk-1234567890abcdefghijklmnop"},
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert result.startswith("[Preflight:refuse]")
    assert "pl4_zero_retention" in result
    assert registry.calls == []
    decisions = traces.decisions()
    assert len(decisions) == 1
    assert decisions[0].chosen == "refuse"
    assert decisions[0].sensitivity == "PL4_credential"


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
@pytest.mark.parametrize(
    ("tool_name", "expected_min_timeout"),
    [
        ("spawn_subagent", 180.0),
        ("start_workflow", 180.0),
    ],
)
async def test_tool_runtime_service_long_running_tools_have_explicit_timeout(
    monkeypatch, tool_name, expected_min_timeout
):
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
    seen = {}
    arguments = {"task": "long running"}
    if tool_name == "start_workflow":
        arguments = {
            "definition": {
                "name": "read-probe",
                "args_schema": {},
                "steps": [
                    {
                        "id": "scan",
                        "type": "agent_step",
                        "leaf": {"name": "scanner", "type": "explorer"},
                        "task": "Scan the workspace",
                    }
                ],
            },
            "args": {},
        }

    async def fake_execute_with_context(self, *_args, **_kwargs):
        return "ok"

    async def fake_wait_for(awaitable, *, timeout):
        seen["timeout"] = timeout
        return await awaitable

    monkeypatch.setattr(ToolRuntimeService, "execute_with_context", fake_execute_with_context)
    monkeypatch.setattr("app.tools.service.asyncio.wait_for", fake_wait_for)

    result = await service.execute(
        tool_name,
        arguments,
        agent_id=context.agent_id,
        user_id=context.user_id,
    )

    assert result == "ok"
    assert seen["timeout"] >= expected_min_timeout


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
            {"url": "https://example.com"},
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


@pytest.mark.asyncio
async def test_interactive_plan_mode_blocks_non_readonly_tools_before_gate():
    from app.services.plan_mode_runtime_context import reset_interactive_plan_mode, set_interactive_plan_mode
    from app.tools.service import ToolRuntimeService

    class _Resolver:
        async def resolve(self, **_kwargs):  # pragma: no cover - must not be reached
            raise AssertionError("runtime context should not be resolved for blocked tools")

    class _GovernanceResolver:
        async def build_context(self, **_kwargs):  # pragma: no cover - must not be reached
            raise AssertionError("governance should not run for blocked tools")

        def build_dependencies(self):  # pragma: no cover - must not be reached
            raise AssertionError("governance dependencies should not be built")

    service = ToolRuntimeService(
        runtime_resolver=_Resolver(),
        governance_resolver=_GovernanceResolver(),
        registry=_FakeRegistry("should not run"),
        ensure_registry=lambda: None,
        governance_runner=lambda *_args, **_kwargs: None,
        fallback_executor=lambda *_args, **_kwargs: "should not run",
        direct_fallback_executor=lambda *_args, **_kwargs: "should not run",
        plan_mode_gate=SimpleNamespace(check=lambda *_args, **_kwargs: None),
        plan_mode_session_factory=lambda: None,
        plan_mode_service=SimpleNamespace(),
    )

    token = set_interactive_plan_mode({"original_request": "plan first"})
    try:
        result = await service.execute("write_file", {"path": "x", "content": "y"}, agent_id=uuid4(), user_id=uuid4())
    finally:
        reset_interactive_plan_mode(token)

    assert "plan_mode_readonly_violation" in result
    assert "exit_plan_mode" in result


@pytest.mark.asyncio
async def test_interactive_plan_mode_allows_write_only_to_exact_plan_file():
    # Phase 4B: the gate reads plan_file_path off the ContextVar mirror and lets
    # writes hit only that exact file — other paths and delete stay blocked.
    from app.services.plan_mode_runtime_context import reset_interactive_plan_mode, set_interactive_plan_mode
    from app.tools.service import ToolRuntimeService

    pf = "workspace/plans/s1.plan.md"
    token = set_interactive_plan_mode({"original_request": "plan", "plan_file_path": pf})
    try:
        # Exact plan-file write → allowed (gate returns None).
        assert ToolRuntimeService._interactive_plan_mode_readonly_block("write_file", {"path": pf}) is None
        # Any other workspace path → blocked.
        other = ToolRuntimeService._interactive_plan_mode_readonly_block("write_file", {"path": "soul.md"})
        assert other is not None and "plan_mode_readonly_violation" in other
        # Delete on the plan file → blocked (iron law ③).
        deleted = ToolRuntimeService._interactive_plan_mode_readonly_block("fs_write", {"path": pf, "mode": "delete"})
        assert deleted is not None and "plan_mode_readonly_violation" in deleted
    finally:
        reset_interactive_plan_mode(token)


def test_interactive_plan_mode_allows_only_narrow_readonly_subagent_lane():
    from app.services.plan_mode_runtime_context import reset_interactive_plan_mode, set_interactive_plan_mode
    from app.tools.service import ToolRuntimeService

    token = set_interactive_plan_mode({"original_request": "plan"})
    try:
        assert (
            ToolRuntimeService._interactive_plan_mode_readonly_block(
                "spawn_subagent",
                {"task": "inspect relevant files", "type": "explorer"},
            )
            is None
        )
        assert ToolRuntimeService._interactive_plan_mode_readonly_block("preview_workflow", {"definition": {}}) is None
        assert ToolRuntimeService._interactive_plan_mode_readonly_block("propose_dynamic_workflow", {"goal": "audit"}) is None
        assert ToolRuntimeService._interactive_plan_mode_readonly_block("check_subagent", {}) is None
        start = ToolRuntimeService._interactive_plan_mode_readonly_block("start_workflow", {"definition": {}})
        assert start is not None and "plan_mode_readonly_violation" in start

        worker = ToolRuntimeService._interactive_plan_mode_readonly_block(
            "spawn_subagent",
            {"task": "make the change", "type": "worker"},
        )
        assert worker is not None and "plan_mode_readonly_violation" in worker

        background = ToolRuntimeService._interactive_plan_mode_readonly_block(
            "spawn_subagent",
            {"task": "inspect later", "run_in_background": True},
        )
        assert background is not None and "plan_mode_readonly_violation" in background
    finally:
        reset_interactive_plan_mode(token)


# ── Confirmation gate: tool intercept never auto-enters Plan Mode ──


def test_redact_args_drops_handshake_keys_and_masks_secrets():
    from app.tools.service import _redact_args

    out = _redact_args(
        {
            "cron": "0 9 * * *",
            "api_key": "sk-xxx",
            "confirmed_plan_id": "p1",
            "webhook_token": "t",
        }
    )
    assert out == {"cron": "0 9 * * *", "api_key": "[redacted]", "webhook_token": "[redacted]"}


def test_confirmation_required_payload_strips_legacy_plan_activation_signal():
    from app.tools.service import _confirmation_required_payload

    payload = {
        "status": "needs_plan",
        "activate_interactive_plan": True,
        "interactive_plan_seed": {"action_kind": "create_enabled_trigger"},
        "summary": "needs a plan",
    }
    out = _confirmation_required_payload(payload)

    assert out["status"] == "requires_confirmation"
    assert out["requires_confirmation"] is True
    assert "activate_interactive_plan" not in out
    assert "interactive_plan_seed" not in out
    assert payload["status"] == "needs_plan"


@pytest.mark.asyncio
async def test_live_intercept_returns_confirmation_required_without_plan_mode_activation():
    """A live source whose gated tool is blocked returns a confirmation block.

    It must not attach a Plan Mode activation signal; Plan Mode entry is explicit
    user/UI state only.
    """
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    agent_id = uuid4()
    user_id = uuid4()
    context = ToolExecutionContext(
        agent_id=agent_id,
        user_id=user_id,
        tenant_id="tenant-1",
        workspace=Path("/tmp/ws"),
    )
    governance_context = ToolGovernanceContext(
        agent_id=agent_id,
        user_id=user_id,
        tenant_id="tenant-1",
        tool_name="set_trigger",
        arguments={"name": "Daily brief"},
    )

    class _PlanGate:
        async def check(self, *_args, **_kwargs):
            return SimpleNamespace(
                needs_plan=True,
                needs_plan_payload={
                    "status": "needs_plan",
                    "summary": "Scheduled autonomous work needs a plan.",
                    "next_action": "enter_plan_mode",
                },
            )

    class _SessionFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return False

    class _FailingPlanService:
        def __init__(self):
            self.calls = 0

        async def ensure_awaiting_plan(self, **_kwargs):
            self.calls += 1
            raise AssertionError("live interactive intercept should not materialise an RPC plan")

    async def fake_run_governance(_context, _deps, *, event_callback=None):
        return None

    plan_service = _FailingPlanService()
    registry = _FakeRegistry("SHOULD_NOT_RUN")
    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(governance_context, SimpleNamespace()),
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=fake_run_governance,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
        plan_mode_gate=_PlanGate(),
        plan_mode_session_factory=_SessionFactory(),
        plan_mode_service=plan_service,
    )

    result = await service.execute(
        "set_trigger",
        {"name": "Daily brief", "type": "cron", "config": {"expr": "0 9 * * *"}},
        agent_id=agent_id,
        user_id=user_id,
        plan_mode_interactive_available=True,
    )
    payload = json.loads(result)

    assert payload["status"] == "requires_confirmation"
    assert payload["requires_confirmation"] is True
    assert "activate_interactive_plan" not in payload
    assert "interactive_plan_seed" not in payload
    assert "plan_id" not in payload
    assert plan_service.calls == 0
    assert registry.calls == []


@pytest.mark.asyncio
async def test_unattended_intercept_returns_confirmation_required_without_plan_mode_activation():
    """An unattended source must also fail closed without creating Plan Mode.

    Background runs cannot self-author their way into Plan Mode when blocked.
    """
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    agent_id = uuid4()
    user_id = uuid4()
    context = ToolExecutionContext(agent_id=agent_id, user_id=user_id, tenant_id="tenant-1", workspace=Path("/tmp/ws"))
    governance_context = ToolGovernanceContext(
        agent_id=agent_id,
        user_id=user_id,
        tenant_id="tenant-1",
        tool_name="set_trigger",
        arguments={"name": "Daily brief"},
    )

    class _PlanGate:
        async def check(self, *_args, **_kwargs):
            return SimpleNamespace(
                needs_plan=True,
                needs_plan_payload={
                    "status": "needs_plan",
                    "summary": "Scheduled autonomous work needs a plan.",
                    "next_action": "enter_plan_mode",
                },
            )

    class _SessionFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return False

    class _FailingPlanService:
        def __init__(self):
            self.calls = 0

        async def ensure_awaiting_plan(self, **_kwargs):
            self.calls += 1
            raise AssertionError("unattended intercept should defer to main-loop Plan Mode, not the RPC planner")

    async def fake_run_governance(_context, _deps, *, event_callback=None):
        return None

    plan_service = _FailingPlanService()
    registry = _FakeRegistry("SHOULD_NOT_RUN")
    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(governance_context, SimpleNamespace()),
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=fake_run_governance,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
        plan_mode_gate=_PlanGate(),
        plan_mode_session_factory=_SessionFactory(),
        plan_mode_service=plan_service,
    )

    result = await service.execute(
        "set_trigger",
        {"name": "Daily brief", "type": "cron", "config": {"expr": "0 9 * * *"}},
        agent_id=agent_id,
        user_id=user_id,
        plan_mode_interactive_available=False,
        plan_mode_unattended_available=True,
    )
    payload = json.loads(result)

    assert payload["status"] == "requires_confirmation"
    assert payload["requires_confirmation"] is True
    assert "activate_interactive_plan" not in payload
    assert "interactive_plan_seed" not in payload
    assert "plan_id" not in payload
    assert plan_service.calls == 0
    assert registry.calls == []


@pytest.mark.asyncio
async def test_non_eligible_source_intercept_returns_confirmation_required_fail_closed():
    """A non-eligible source gets the same static confirmation block.

    The key invariant is source-independent: no tool intercept may enter Plan
    Mode for the user.
    """
    from app.tools.governance import ToolGovernanceContext
    from app.tools.runtime import ToolExecutionContext
    from app.tools.service import ToolRuntimeService

    agent_id = uuid4()
    user_id = uuid4()
    context = ToolExecutionContext(agent_id=agent_id, user_id=user_id, tenant_id="tenant-1", workspace=Path("/tmp/ws"))
    governance_context = ToolGovernanceContext(
        agent_id=agent_id,
        user_id=user_id,
        tenant_id="tenant-1",
        tool_name="set_trigger",
        arguments={"name": "Daily brief"},
    )

    class _PlanGate:
        async def check(self, *_args, **_kwargs):
            return SimpleNamespace(
                needs_plan=True,
                needs_plan_payload={"status": "needs_plan", "summary": "needs plan", "next_action": "enter_plan_mode"},
            )

    class _SessionFactory:
        def __call__(self):
            return self

        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_args):
            return False

    async def fake_run_governance(_context, _deps, *, event_callback=None):
        return None

    registry = _FakeRegistry("SHOULD_NOT_RUN")
    service = ToolRuntimeService(
        runtime_resolver=_FakeRuntimeResolver(context),
        governance_resolver=_FakeGovernanceResolver(governance_context, SimpleNamespace()),
        registry=registry,
        ensure_registry=lambda: None,
        governance_runner=fake_run_governance,
        fallback_executor=lambda *_args, **_kwargs: "fallback",
        direct_fallback_executor=lambda *_args, **_kwargs: "direct-fallback",
        activity_logger=None,
        plan_mode_gate=_PlanGate(),
        plan_mode_session_factory=_SessionFactory(),
    )

    # Neither availability flag set → non-eligible source.
    result = await service.execute(
        "set_trigger",
        {"name": "Daily brief", "type": "cron", "config": {"expr": "0 9 * * *"}},
        agent_id=agent_id,
        user_id=user_id,
        plan_mode_interactive_available=False,
        plan_mode_unattended_available=False,
    )
    payload = json.loads(result)

    assert payload["status"] == "requires_confirmation"  # blocked
    assert payload["requires_confirmation"] is True
    assert "activate_interactive_plan" not in payload  # agent does NOT plan
    assert "plan_id" not in payload  # nothing materialised — no RPC fallback
    assert registry.calls == []  # tool did NOT execute (fail-closed)
