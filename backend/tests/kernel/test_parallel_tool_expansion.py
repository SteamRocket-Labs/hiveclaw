"""Regression: deferred-tool expansion must work in a mixed/parallel tool batch.

Production evidence (Session 990fc4f9 run 988d58ba): a round containing
track_todo calls plus a successful ``tool_search(select:team_create)`` ran
through the segmented parallel branch, which appended tool results without
applying tool expansion — later provider requests never received the loaded
schema and the capability stayed unreachable for the rest of the run.

These tests drive the real AgentKernel/provider boundary with a fake client:
round 1 returns a mixed batch (independent safe tool + loaders + a failing
loader); the next actual provider request must expose the authorized schemas
from the successful loaders only.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.kernel import AgentKernel, InvocationRequest, KernelDependencies, RuntimeConfig
from app.kernel.engine import ToolExpansionResult
from app.runtime.session import SessionContext


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


def _tool(name: str) -> dict:
    return {"type": "function", "function": {"name": name, "description": "", "parameters": {"type": "object"}}}


def _model() -> SimpleNamespace:
    return SimpleNamespace(provider="openai", model="gpt-4.1", api_key="test", base_url=None, max_output_tokens=None)


def _response(content: str, tool_calls: list[dict] | None = None) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        tool_calls=tool_calls or [],
        reasoning_content=None,
        reasoning_signature=None,
        finish_reason="stop" if not tool_calls else "tool_calls",
        usage={"total_tokens": 5},
    )


def _record_governed_tool_success(trace_metadata_sink) -> None:
    """Give tool test doubles the same settlement facts as the live pipeline."""
    assert isinstance(trace_metadata_sink, dict)
    trace_metadata_sink.setdefault("tool_decision", {"outcome": "allow"})
    trace_metadata_sink.setdefault("tool_execution_frame", {"status": "completed"})


async def _run_mixed_batch_turn(execute_tool, resolve_tool_expansion, responses):
    session = SessionContext(session_id="session-parallel-expansion", metadata={})
    request = InvocationRequest(
        model=_model(),
        messages=[{"role": "user", "content": "find and use the deferred capability"}],
        agent_name="Agent",
        role_description="role",
        agent_id=uuid4(),
        user_id=uuid4(),
        session_context=session,
        memory_session_id="session-parallel-expansion",
        core_tools_only=True,
        expand_tools=True,
    )
    fake_client = _FakeClient(responses)
    kernel = AgentKernel(
        KernelDependencies(
            resolve_runtime_config=lambda *_a, **_k: RuntimeConfig(tenant_id=uuid4(), max_tool_rounds=6),
            resolve_current_user_name=lambda *_a, **_k: "Example Owner",
            build_system_prompt=lambda *_a, **_k: "PROMPT",
            resolve_memory_context=lambda *_a, **_k: "",
            get_tools=lambda *_a, **_k: [_tool("read_file"), _tool("tool_search")],
            resolve_tool_expansion=resolve_tool_expansion,
            maybe_compress_messages=lambda messages, **_k: messages,
            create_client=lambda _m: fake_client,
            execute_tool=execute_tool,
            persist_memory=lambda **_k: None,
            record_token_usage=lambda *_a, **_k: None,
            get_max_tokens=lambda _provider, _model, override=None: override or 2048,
            extract_usage_tokens=lambda usage: usage.get("total_tokens"),
            estimate_tokens_from_chars=lambda chars: chars // 4,
        )
    )
    result = await kernel.handle(request)
    return result, fake_client, session


@pytest.mark.asyncio
@pytest.mark.parametrize("blocked_loader", ["failed", "denied"])
async def test_successful_tool_search_in_parallel_batch_expands_tools_for_next_request(blocked_loader):
    """Mixed batch: safe tool + two disjoint loaders + one failing loader.

    The two successful loaders must make their authorized schemas available to
    the very next provider request; the failing loader must not expand.
    """
    expansion_queries: list[str] = []
    discovered: set[str] = set()

    async def execute_tool(tool_name, args, request, emit_event, trace_metadata_sink=None):
        if tool_name == "tool_search" and args.get("query") == "select:tool_gamma":
            if blocked_loader == "failed":
                raise RuntimeError("gamma loader failed")
            trace_metadata_sink["tool_decision"] = {"outcome": "deny"}
            trace_metadata_sink["tool_execution_frame"] = {"status": "denied"}
            return json.dumps({"status": "denied", "code": "resource_permission_required"})
        if trace_metadata_sink is not None:
            _record_governed_tool_success(trace_metadata_sink)
        return json.dumps({"status": "ok", "tool": tool_name})

    async def resolve_tool_expansion(request, tool_name, args):
        expansion_queries.append(str(args.get("query") or ""))
        assert tool_name == "tool_search"
        loaded_by_query = {
            "select:tool_alpha": ("group_alpha", "tool_alpha"),
            "select:tool_beta": ("group_beta", "tool_beta"),
        }
        entry = loaded_by_query.get(str(args.get("query") or ""))
        if entry is None:
            return None
        group_name, tool = entry
        request.session_context.track_discovered_tools([tool])
        discovered.add(tool)
        tools = [_tool("read_file"), _tool("tool_search")] + [_tool(t) for t in sorted(discovered)]
        return ToolExpansionResult(
            tools=tools,
            active_tool_groups=[
                {"name": group_name, "summary": f"{group_name} tools", "tools": [tool]},
            ],
        )

    result, fake_client, session = await _run_mixed_batch_turn(
        execute_tool,
        resolve_tool_expansion,
        [
            _response(
                "",
                [
                    {"id": "call_1", "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}},
                    {"id": "call_2", "function": {"name": "tool_search", "arguments": '{"query":"select:tool_alpha"}'}},
                    {"id": "call_3", "function": {"name": "tool_search", "arguments": '{"query":"select:tool_beta"}'}},
                    {"id": "call_4", "function": {"name": "tool_search", "arguments": '{"query":"select:tool_gamma"}'}},
                ],
            ),
            # Round 2: the model can now actually call the newly loaded schema.
            _response("", [{"id": "call_5", "function": {"name": "tool_alpha", "arguments": "{}"}}]),
            _response("done with the deferred capability"),
        ],
    )

    assert result.content == "done with the deferred capability"

    # Round 1 was dispatched through the segmented parallel branch: a mixed
    # batch with at least one concurrency-safe tool.
    round1_tool_names = [t["function"]["name"] for t in fake_client.calls[0]["tools"]]
    assert "read_file" in round1_tool_names and "tool_search" in round1_tool_names
    assert "tool_alpha" not in round1_tool_names and "tool_beta" not in round1_tool_names

    # The failing loader must never reach expansion resolution.
    assert "select:tool_gamma" not in expansion_queries
    assert set(expansion_queries) == {"select:tool_alpha", "select:tool_beta"}

    # The very next actual provider request carries both disjoint loaders'
    # authorized schemas (and preserves the base tools), but not the failed one.
    round2_tool_names = [t["function"]["name"] for t in fake_client.calls[1]["tools"]]
    assert "tool_alpha" in round2_tool_names
    assert "tool_beta" in round2_tool_names
    assert "tool_gamma" not in round2_tool_names
    assert "read_file" in round2_tool_names and "tool_search" in round2_tool_names

    # Session context keeps both discovered groups and tools (discoverability).
    group_names = [g.get("name") for g in session.active_tool_groups]
    assert "group_alpha" in group_names and "group_beta" in group_names
    assert set(session.discovered_tools or []) == {"tool_alpha", "tool_beta"}


@pytest.mark.asyncio
async def test_sequential_tool_search_expansion_still_works():
    """Single-loader batch runs the sequential path; expansion must be unchanged."""
    expansion_queries: list[str] = []

    async def execute_tool(tool_name, args, request, emit_event, trace_metadata_sink=None):
        if trace_metadata_sink is not None:
            _record_governed_tool_success(trace_metadata_sink)
        return json.dumps({"status": "ok", "tool": tool_name})

    async def resolve_tool_expansion(request, tool_name, args):
        expansion_queries.append(str(args.get("query") or ""))
        if str(args.get("query")) != "select:tool_alpha":
            return None
        request.session_context.track_discovered_tools(["tool_alpha"])
        return ToolExpansionResult(
            tools=[_tool("read_file"), _tool("tool_search"), _tool("tool_alpha")],
            active_tool_groups=[{"name": "group_alpha", "summary": "alpha tools", "tools": ["tool_alpha"]}],
        )

    result, fake_client, session = await _run_mixed_batch_turn(
        execute_tool,
        resolve_tool_expansion,
        [
            _response(
                "",
                [{"id": "call_1", "function": {"name": "tool_search", "arguments": '{"query":"select:tool_alpha"}'}}],
            ),
            _response("done"),
        ],
    )

    assert result.content == "done"
    assert expansion_queries == ["select:tool_alpha"]
    round2_tool_names = [t["function"]["name"] for t in fake_client.calls[1]["tools"]]
    assert "tool_alpha" in round2_tool_names
    assert [g.get("name") for g in session.active_tool_groups] == ["group_alpha"]
