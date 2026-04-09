"""Tests for kernel batch executor — parallel execution of read-only tools."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.kernel.engine import _can_parallelize_batch
from app.tools.registry import PARALLEL_SAFE_TOOL_NAMES


class _FakeClient:
    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def stream(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("No fake response prepared")
        return self._responses.pop(0)

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


def _make_deps(*, execute_tool=None, **overrides):
    from app.kernel.engine import KernelDependencies, RuntimeConfig

    defaults = {
        "resolve_runtime_config": lambda _id: RuntimeConfig(
            tenant_id=uuid4(), max_tool_rounds=5, quota_message=None
        ),
        "resolve_current_user_name": lambda *_a, **_kw: "Rocky",
        "build_system_prompt": lambda *_a, **_kw: "PROMPT",
        "resolve_memory_context": lambda *_a, **_kw: "",
        "get_tools": lambda *_a, **_kw: [
            {"type": "function", "function": {"name": "read_file", "description": "", "parameters": {"type": "object"}}}
        ],
        "maybe_compress_messages": lambda messages, **kw: messages,
        "execute_tool": execute_tool or (lambda *_a, **_kw: "OK"),
        "persist_memory": lambda **kw: None,
        "record_token_usage": lambda *a, **kw: None,
        "get_max_tokens": lambda *a, **kw: 2048,
        "extract_usage_tokens": lambda usage: usage.get("total_tokens") if usage else None,
        "estimate_tokens_from_chars": lambda chars: chars // 4,
    }
    defaults.update(overrides)
    return KernelDependencies(**defaults)


# ---------------------------------------------------------------------------
# Unit tests for _can_parallelize_batch
# ---------------------------------------------------------------------------


class TestCanParallelizeBatch:
    def test_all_read_only_returns_true(self):
        tool_calls = [
            {"function": {"name": "read_file"}},
            {"function": {"name": "glob_search"}},
        ]
        assert _can_parallelize_batch(tool_calls) is True

    def test_mixed_batch_returns_false(self):
        tool_calls = [
            {"function": {"name": "read_file"}},
            {"function": {"name": "write_file"}},
        ]
        assert _can_parallelize_batch(tool_calls) is False

    def test_single_read_only_returns_true(self):
        tool_calls = [{"function": {"name": "list_files"}}]
        assert _can_parallelize_batch(tool_calls) is True

    def test_all_parallel_safe_tools_accepted(self):
        for name in PARALLEL_SAFE_TOOL_NAMES:
            assert _can_parallelize_batch([{"function": {"name": name}}]) is True

    def test_unknown_tool_returns_false(self):
        tool_calls = [{"function": {"name": "unknown_tool"}}]
        assert _can_parallelize_batch(tool_calls) is False


# ---------------------------------------------------------------------------
# Integration tests for kernel parallel dispatch
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parallel_batch_executes_read_only_tools():
    """Two read_file calls in same round should execute via gather."""
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel

    execution_log: list[str] = []

    async def execute_tool(tool_name, args, request, emit_event):
        execution_log.append(f"exec:{tool_name}:{args.get('path', '')}")
        return f"content_of_{args.get('path', '')}"

    fake_client = _FakeClient([
        SimpleNamespace(
            content="",
            tool_calls=[
                {
                    "id": "call_1",
                    "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'},
                },
                {
                    "id": "call_2",
                    "function": {"name": "read_file", "arguments": '{"path":"b.txt"}'},
                },
            ],
            reasoning_content=None,
            usage={"total_tokens": 10},
        ),
        SimpleNamespace(
            content="done",
            tool_calls=[],
            reasoning_content=None,
            usage={"total_tokens": 5},
        ),
    ])

    kernel = AgentKernel(_make_deps(
        execute_tool=execute_tool,
        create_client=lambda _m: fake_client,
    ))

    result = await kernel.handle(
        InvocationRequest(
            model=_make_model(),
            messages=[{"role": "user", "content": "read two files"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content == "done"
    assert len(execution_log) == 2
    assert "exec:read_file:a.txt" in execution_log
    assert "exec:read_file:b.txt" in execution_log
    # Verify both tool results appear in parts
    tool_parts = [p for p in result.parts if p.get("type") == "tool_call"]
    assert len(tool_parts) == 2
    assert tool_parts[0]["result"] == "content_of_a.txt"
    assert tool_parts[1]["result"] == "content_of_b.txt"


@pytest.mark.asyncio
async def test_parallel_batch_preserves_result_order():
    """Results should be in original tool_call order regardless of completion order."""
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel

    async def execute_tool(tool_name, args, request, emit_event):
        path = args.get("path", "")
        # Second call completes faster by sleeping less
        if path == "slow.txt":
            await asyncio.sleep(0.05)
        return f"result_{path}"

    fake_client = _FakeClient([
        SimpleNamespace(
            content="",
            tool_calls=[
                {
                    "id": "call_slow",
                    "function": {"name": "read_file", "arguments": '{"path":"slow.txt"}'},
                },
                {
                    "id": "call_fast",
                    "function": {"name": "read_file", "arguments": '{"path":"fast.txt"}'},
                },
            ],
            reasoning_content=None,
            usage={"total_tokens": 10},
        ),
        SimpleNamespace(
            content="ordered",
            tool_calls=[],
            reasoning_content=None,
            usage={"total_tokens": 5},
        ),
    ])

    kernel = AgentKernel(_make_deps(
        execute_tool=execute_tool,
        create_client=lambda _m: fake_client,
    ))

    result = await kernel.handle(
        InvocationRequest(
            model=_make_model(),
            messages=[{"role": "user", "content": "read files"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content == "ordered"
    tool_parts = [p for p in result.parts if p.get("type") == "tool_call"]
    # Order must match original tool_calls order: slow first, fast second
    assert tool_parts[0]["result"] == "result_slow.txt"
    assert tool_parts[1]["result"] == "result_fast.txt"

    # Verify api_messages also have correct order of tool results
    tool_messages = [m for m in fake_client.calls[1]["messages"] if getattr(m, "role", None) == "tool"]
    assert tool_messages[0].tool_call_id == "call_slow"
    assert tool_messages[1].tool_call_id == "call_fast"


@pytest.mark.asyncio
async def test_mixed_batch_falls_back_to_sequential():
    """If batch contains write_file, all tools run sequentially."""
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel

    execution_order: list[str] = []

    async def execute_tool(tool_name, args, request, emit_event):
        execution_order.append(tool_name)
        return "ok"

    fake_client = _FakeClient([
        SimpleNamespace(
            content="",
            tool_calls=[
                {
                    "id": "call_1",
                    "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'},
                },
                {
                    "id": "call_2",
                    "function": {"name": "write_file", "arguments": '{"path":"b.txt","content":"x"}'},
                },
            ],
            reasoning_content=None,
            usage={"total_tokens": 10},
        ),
        SimpleNamespace(
            content="done",
            tool_calls=[],
            reasoning_content=None,
            usage={"total_tokens": 5},
        ),
    ])

    kernel = AgentKernel(_make_deps(
        execute_tool=execute_tool,
        create_client=lambda _m: fake_client,
    ))

    result = await kernel.handle(
        InvocationRequest(
            model=_make_model(),
            messages=[{"role": "user", "content": "read and write"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content == "done"
    # Sequential: read_file executed first, then write_file
    assert execution_order == ["read_file", "write_file"]


@pytest.mark.asyncio
async def test_single_tool_call_stays_sequential():
    """Single tool call doesn't trigger parallel path."""
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel

    execution_log: list[str] = []

    async def execute_tool(tool_name, args, request, emit_event):
        execution_log.append(tool_name)
        return "single_result"

    fake_client = _FakeClient([
        SimpleNamespace(
            content="",
            tool_calls=[
                {
                    "id": "call_1",
                    "function": {"name": "read_file", "arguments": '{"path":"only.txt"}'},
                },
            ],
            reasoning_content=None,
            usage={"total_tokens": 10},
        ),
        SimpleNamespace(
            content="done",
            tool_calls=[],
            reasoning_content=None,
            usage={"total_tokens": 5},
        ),
    ])

    kernel = AgentKernel(_make_deps(
        execute_tool=execute_tool,
        create_client=lambda _m: fake_client,
    ))

    result = await kernel.handle(
        InvocationRequest(
            model=_make_model(),
            messages=[{"role": "user", "content": "read one file"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content == "done"
    assert execution_log == ["read_file"]
    tool_parts = [p for p in result.parts if p.get("type") == "tool_call"]
    assert len(tool_parts) == 1
    assert tool_parts[0]["result"] == "single_result"


@pytest.mark.asyncio
async def test_parallel_batch_emits_events_in_order():
    """Running events emitted first (all), then done events in order."""
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel

    tool_call_events: list[dict] = []

    async def on_tool_call(payload):
        tool_call_events.append({"name": payload["name"], "status": payload["status"]})

    async def execute_tool(tool_name, args, request, emit_event):
        return f"r_{args.get('path', '')}"

    fake_client = _FakeClient([
        SimpleNamespace(
            content="",
            tool_calls=[
                {
                    "id": "call_1",
                    "function": {"name": "read_file", "arguments": '{"path":"x.txt"}'},
                },
                {
                    "id": "call_2",
                    "function": {"name": "glob_search", "arguments": '{"path":"*.py"}'},
                },
                {
                    "id": "call_3",
                    "function": {"name": "list_files", "arguments": '{"path":"."}'},
                },
            ],
            reasoning_content=None,
            usage={"total_tokens": 10},
        ),
        SimpleNamespace(
            content="done",
            tool_calls=[],
            reasoning_content=None,
            usage={"total_tokens": 5},
        ),
    ])

    kernel = AgentKernel(_make_deps(
        execute_tool=execute_tool,
        create_client=lambda _m: fake_client,
    ))

    await kernel.handle(
        InvocationRequest(
            model=_make_model(),
            messages=[{"role": "user", "content": "read files"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
            on_tool_call=on_tool_call,
        )
    )

    # All "running" events emitted first, then all "done" events
    assert tool_call_events == [
        {"name": "read_file", "status": "running"},
        {"name": "glob_search", "status": "running"},
        {"name": "list_files", "status": "running"},
        {"name": "read_file", "status": "done"},
        {"name": "glob_search", "status": "done"},
        {"name": "list_files", "status": "done"},
    ]


@pytest.mark.asyncio
async def test_parallel_batch_respects_semaphore_limit():
    """Semaphore limits concurrent executions to _PARALLEL_SEMAPHORE_LIMIT."""
    from app.kernel.engine import _PARALLEL_SEMAPHORE_LIMIT

    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel

    peak_concurrent = 0
    current_concurrent = 0
    lock = asyncio.Lock()

    async def execute_tool(tool_name, args, request, emit_event):
        nonlocal peak_concurrent, current_concurrent
        async with lock:
            current_concurrent += 1
            if current_concurrent > peak_concurrent:
                peak_concurrent = current_concurrent
        await asyncio.sleep(0.01)
        async with lock:
            current_concurrent -= 1
        return "ok"

    # Create 6 parallel-safe tool calls (exceeds semaphore limit of 4)
    tool_calls = [
        {
            "id": f"call_{i}",
            "function": {"name": "read_file", "arguments": f'{{"path":"file_{i}.txt"}}'},
        }
        for i in range(6)
    ]

    fake_client = _FakeClient([
        SimpleNamespace(
            content="",
            tool_calls=tool_calls,
            reasoning_content=None,
            usage={"total_tokens": 10},
        ),
        SimpleNamespace(
            content="done",
            tool_calls=[],
            reasoning_content=None,
            usage={"total_tokens": 5},
        ),
    ])

    kernel = AgentKernel(_make_deps(
        execute_tool=execute_tool,
        create_client=lambda _m: fake_client,
    ))

    result = await kernel.handle(
        InvocationRequest(
            model=_make_model(),
            messages=[{"role": "user", "content": "read many files"}],
            agent_name="Agent",
            role_description="desc",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content == "done"
    assert peak_concurrent <= _PARALLEL_SEMAPHORE_LIMIT


@pytest.mark.asyncio
async def test_parallel_batch_applies_pre_tool_hook_modifications():
    """Parallel batches must respect pre-tool arg rewrites just like sequential path."""
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import AgentKernel
    from app.runtime.hooks import HookEvent, HookResult, hook_registry

    seen_paths: list[str] = []

    def rewrite_args(ctx):
        return HookResult(block=False, modified_args={"path": f"safe::{ctx.tool_args['path']}"})

    async def execute_tool(tool_name, args, request, emit_event):
        del tool_name, request, emit_event
        seen_paths.append(args["path"])
        return args["path"]

    hook_registry.clear()
    hook_registry.register(HookEvent.PRE_TOOL_USE, rewrite_args)

    fake_client = _FakeClient([
        SimpleNamespace(
            content="",
            tool_calls=[
                {
                    "id": "call_1",
                    "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'},
                },
                {
                    "id": "call_2",
                    "function": {"name": "read_file", "arguments": '{"path":"b.txt"}'},
                },
            ],
            reasoning_content=None,
            usage={"total_tokens": 10},
        ),
        SimpleNamespace(
            content="done",
            tool_calls=[],
            reasoning_content=None,
            usage={"total_tokens": 5},
        ),
    ])

    try:
        kernel = AgentKernel(_make_deps(
            execute_tool=execute_tool,
            create_client=lambda _m: fake_client,
        ))

        result = await kernel.handle(
            InvocationRequest(
                model=_make_model(),
                messages=[{"role": "user", "content": "read two files"}],
                agent_name="Agent",
                role_description="desc",
                agent_id=uuid4(),
                user_id=uuid4(),
            )
        )
    finally:
        hook_registry.clear()

    assert result.content == "done"
    assert seen_paths == ["safe::a.txt", "safe::b.txt"]
