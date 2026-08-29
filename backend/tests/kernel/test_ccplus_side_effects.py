"""CCPlus V1 — D-08 (ToolContentEnvelope side-effect channel) + D-04 (resume
original tool_call_id) LIVE kernel wiring.

These tests drive ``AgentKernel.handle`` end-to-end with a fake LLM client and a
fake ``execute_tool`` that returns a ``ToolContentEnvelope`` carrying the
side-effect channel. They assert the REAL loop consumes the channel:

* ``new_messages`` are appended to the live conversation the next round sees.
* ``terminal_signal`` ends the turn after the current round (no extra model call).
* ``done_payload`` carries the original streamed ``tool_call_id`` at top level.

Revert-sensitivity: each assertion fails if the corresponding wiring in
``engine.py`` is reverted (see inline notes).
"""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.kernel import AgentKernel, InvocationRequest, KernelDependencies, RuntimeConfig
from app.tools.result_envelope import ToolContentEnvelope


def _base_deps(*, fake_client, execute_tool) -> KernelDependencies:
    return KernelDependencies(
        resolve_runtime_config=lambda *_args, **_kwargs: RuntimeConfig(
            tenant_id=uuid4(),
            max_tool_rounds=4,
            quota_message=None,
        ),
        resolve_current_user_name=lambda *_args, **_kwargs: "Rocky",
        build_system_prompt=lambda *_args, **_kwargs: "PROMPT",
        resolve_memory_context=lambda *_args, **_kwargs: "",
        get_tools=lambda *_args, **_kwargs: [
            {
                "type": "function",
                "function": {"name": "read_file", "description": "", "parameters": {"type": "object"}},
            },
            {
                "type": "function",
                "function": {"name": "list_files", "description": "", "parameters": {"type": "object"}},
            },
        ],
        maybe_compress_messages=lambda messages, **_kwargs: messages,
        create_client=lambda _model: fake_client,
        execute_tool=execute_tool,
        persist_memory=lambda **_kwargs: None,
        record_token_usage=lambda *_args, **_kwargs: None,
        get_max_tokens=lambda _provider, _model, override=None: override or 2048,
        extract_usage_tokens=lambda usage: usage.get("total_tokens"),
        estimate_tokens_from_chars=lambda chars: chars // 4,
    )


class _FakeClient:
    def __init__(self, responses: list[SimpleNamespace]) -> None:
        self._responses = list(responses)
        self.calls: list[dict] = []

    async def stream(self, **kwargs):
        self.calls.append(kwargs)
        if not self._responses:
            raise AssertionError("No fake response prepared")
        outcome = self._responses.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    async def close(self) -> None:
        return None


def _tool_call_response(tool_name: str, call_id: str, args: str = "{}") -> SimpleNamespace:
    return SimpleNamespace(
        content="",
        tool_calls=[{"id": call_id, "function": {"name": tool_name, "arguments": args}}],
        reasoning_content=None,
        usage={"total_tokens": 5},
    )


def _final_response(content: str) -> SimpleNamespace:
    return SimpleNamespace(
        content=content,
        tool_calls=[],
        reasoning_content=None,
        usage={"total_tokens": 7},
        finish_reason="stop",
    )


@pytest.mark.asyncio
async def test_tool_new_messages_are_injected_into_next_round_conversation() -> None:
    """D-08: a tool result whose envelope carries ``new_messages`` injects those
    messages into the conversation the NEXT model round sees.

    Revert-sensitive: if the consume block in engine.py is removed, the injected
    message never reaches ``api_messages`` and ``calls[1]["messages"]`` will not
    contain it — the assertion fails.
    """
    injected_marker = "INJECTED-BY-TOOL-9f3a"
    fake_client = _FakeClient(
        [
            _tool_call_response("read_file", "call_inject_1"),
            _final_response("acknowledged"),
        ]
    )

    def execute_tool(tool_name, args, request, emit_event):
        return ToolContentEnvelope(
            text="file contents",
            new_messages=({"role": "user", "content": injected_marker},),
        )

    kernel = AgentKernel(_base_deps(fake_client=fake_client, execute_tool=execute_tool))

    result = await kernel.handle(
        InvocationRequest(
            model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None),
            messages=[{"role": "user", "content": "Read the file"}],
            agent_name="Engineer",
            role_description="Investigates repositories",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content == "acknowledged"
    # The kernel reached a 2nd round (the tool did NOT terminate), and the
    # injected message rode into that round's conversation.
    assert len(fake_client.calls) == 2
    second_round_messages = fake_client.calls[1]["messages"]
    assert any(message.role == "user" and message.content == injected_marker for message in second_round_messages), (
        "tool-injected new_messages must reach the next round's live conversation"
    )


@pytest.mark.asyncio
async def test_tool_new_messages_injected_on_parallel_path() -> None:
    """D-08: the parallel/segmented execution path also consumes ``new_messages``.

    Two parallel-safe ``read_file`` calls force the segmented-parallel branch.
    """
    injected_marker = "PARALLEL-INJECT-7c21"
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {"id": "call_p1", "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}},
                    {"id": "call_p2", "function": {"name": "read_file", "arguments": '{"path":"b.txt"}'}},
                ],
                reasoning_content=None,
                usage={"total_tokens": 5},
            ),
            _final_response("parallel done"),
        ]
    )

    def execute_tool(tool_name, args, request, emit_event):
        if args.get("path") == "b.txt":
            return ToolContentEnvelope(
                text="b contents",
                new_messages=({"role": "user", "content": injected_marker},),
            )
        return "a contents"

    kernel = AgentKernel(_base_deps(fake_client=fake_client, execute_tool=execute_tool))

    result = await kernel.handle(
        InvocationRequest(
            model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None),
            messages=[{"role": "user", "content": "Read both files"}],
            agent_name="Engineer",
            role_description="Investigates repositories",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content == "parallel done"
    assert len(fake_client.calls) == 2
    second_round_messages = fake_client.calls[1]["messages"]
    assert any(message.role == "user" and message.content == injected_marker for message in second_round_messages), (
        "parallel-path tool-injected new_messages must reach the next round"
    )


@pytest.mark.asyncio
async def test_proven_non_progress_is_summarized_by_model_without_platform_tool_removal(monkeypatch) -> None:
    from app.kernel import engine
    from app.kernel.loop_guard import LoopGuard

    monkeypatch.setattr(engine, "LoopGuard", lambda: LoopGuard(repeated_failure_threshold=2))
    fake_client = _FakeClient(
        [
            _tool_call_response("read_file", "call_retry_1", '{"path":"same.txt"}'),
            _tool_call_response("read_file", "call_retry_2", '{"path":"same.txt"}'),
            _tool_call_response("read_file", "call_retry_3", '{"path":"same.txt"}'),
            _final_response("I exhausted the read retry and preserved the evidence; please repair access, then retry."),
        ]
    )

    def execute_tool(_tool_name, _args, _request, _emit_event, *, trace_metadata_sink=None):
        trace_metadata_sink["tool_decision"] = {"outcome": "allow"}
        trace_metadata_sink["tool_execution_frame"] = {"status": "failed"}
        return ToolContentEnvelope(
            text="[Tool execution error] timeout",
            metadata={
                "loop_guard_proof": {
                    "retry_exhausted": True,
                    "progress_token": "storage-state-v1",
                }
            },
        )

    kernel = AgentKernel(_base_deps(fake_client=fake_client, execute_tool=execute_tool))
    result = await kernel.handle(
        InvocationRequest(
            model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None),
            messages=[{"role": "user", "content": "Read the file completely"}],
            agent_name="Engineer",
            role_description="Investigates repositories",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content.startswith("I exhausted the read retry")
    assert len(fake_client.calls) == 4
    assert [tool["function"]["name"] for tool in fake_client.calls[3]["tools"]] == [
        "read_file",
        "list_files",
    ]
    assert any("loop_guard_terminal_evidence" in str(message.content) for message in fake_client.calls[3]["messages"])
    assert not result.content.startswith("[Loop Guard]")


@pytest.mark.asyncio
async def test_tool_new_messages_wait_until_all_same_round_tool_results_are_appended() -> None:
    """D-08: injected messages must not split the provider's tool-result block.

    When one assistant message contains multiple tool calls, every corresponding
    role="tool" result must appear contiguously before any new user/system
    message is injected. Otherwise the next provider call sees an invalid
    assistant/tool history order.
    """
    injected_marker = "ORDERED-AFTER-ALL-TOOLS-3d9a"
    fake_client = _FakeClient(
        [
            SimpleNamespace(
                content="",
                tool_calls=[
                    {"id": "call_first", "function": {"name": "read_file", "arguments": '{"path":"a.txt"}'}},
                    {"id": "call_second", "function": {"name": "read_file", "arguments": '{"path":"b.txt"}'}},
                ],
                reasoning_content=None,
                usage={"total_tokens": 5},
            ),
            _final_response("ordered"),
        ]
    )

    def execute_tool(tool_name, args, request, emit_event):
        if args.get("path") == "a.txt":
            return ToolContentEnvelope(
                text="a contents",
                new_messages=({"role": "user", "content": injected_marker},),
            )
        return "b contents"

    kernel = AgentKernel(_base_deps(fake_client=fake_client, execute_tool=execute_tool))

    result = await kernel.handle(
        InvocationRequest(
            model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None),
            messages=[{"role": "user", "content": "Read both files"}],
            agent_name="Engineer",
            role_description="Investigates repositories",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    assert result.content == "ordered"
    second_round_messages = fake_client.calls[1]["messages"]
    roles_and_content = [
        (message.role, getattr(message, "tool_call_id", None), message.content) for message in second_round_messages
    ]
    first_tool_idx = next(idx for idx, item in enumerate(roles_and_content) if item[1] == "call_first")
    second_tool_idx = next(idx for idx, item in enumerate(roles_and_content) if item[1] == "call_second")
    injected_idx = next(idx for idx, item in enumerate(roles_and_content) if item[2] == injected_marker)

    assert first_tool_idx < second_tool_idx < injected_idx


@pytest.mark.asyncio
async def test_tool_terminal_signal_ends_the_turn() -> None:
    """D-08: a non-empty ``terminal_signal`` ends the turn after the current round.

    Only ONE model response is prepared (the tool-calling round). If the kernel
    looped into another round (i.e. the terminal_signal was NOT consumed), the
    fake client would raise ``AssertionError('No fake response prepared')``.
    Revert-sensitive by construction.
    """
    from app.kernel.contracts import TerminalReason

    fake_client = _FakeClient([_tool_call_response("read_file", "call_term_1")])

    def execute_tool(tool_name, args, request, emit_event):
        return ToolContentEnvelope(text="done reading", terminal_signal="waiting_for_user")

    kernel = AgentKernel(_base_deps(fake_client=fake_client, execute_tool=execute_tool))

    result = await kernel.handle(
        InvocationRequest(
            model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None),
            messages=[{"role": "user", "content": "Read the file then pause"}],
            agent_name="Engineer",
            role_description="Investigates repositories",
            agent_id=uuid4(),
            user_id=uuid4(),
        )
    )

    # The turn ended on the tool round — exactly one model call was made.
    assert len(fake_client.calls) == 1
    assert result.terminal_reason == TerminalReason.TURN_STOP


@pytest.mark.asyncio
async def test_done_payload_carries_original_streamed_tool_call_id() -> None:
    """D-04: the done_payload emitted to ``on_tool_call`` carries the ORIGINAL
    streamed ``tool_call_id`` at the top level so the web resume path can reuse
    it instead of synthesizing ``call_{msg.id}``.

    Revert-sensitive: if the ``"tool_call_id": tc["id"]`` key is removed from the
    done_payload, the captured payload has no top-level ``tool_call_id``.
    """
    original_id = "call_resume_orig_4e1d"
    captured_done: list[dict] = []
    fake_client = _FakeClient(
        [
            _tool_call_response("read_file", original_id),
            _final_response("resumed"),
        ]
    )

    def execute_tool(tool_name, args, request, emit_event):
        return "file body"

    async def on_tool_call(payload: dict) -> None:
        if payload.get("status") == "done":
            captured_done.append(payload)

    kernel = AgentKernel(_base_deps(fake_client=fake_client, execute_tool=execute_tool))

    await kernel.handle(
        InvocationRequest(
            model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None),
            messages=[{"role": "user", "content": "Read the file"}],
            agent_name="Engineer",
            role_description="Investigates repositories",
            agent_id=uuid4(),
            user_id=uuid4(),
            on_tool_call=on_tool_call,
        )
    )

    assert captured_done, "expected a done tool-call payload"
    assert captured_done[0]["tool_call_id"] == original_id


@pytest.mark.asyncio
async def test_tool_envelope_preserves_full_receipt_but_projects_bounded_model_result() -> None:
    """A governed effect may need a full UI/audit receipt without exposing its
    navigation identifiers to the next model round.

    The durable callback receives the byte-exact receipt while the provider sees
    only the explicitly supplied truthful projection.  No final-answer scanner or
    platform-authored replacement is involved.
    """

    raw_receipt = (
        '{"ok":true,"hr_agent_id":"bef8b286-b923-4e29-84c9-022f995ae6b3",'
        '"hr_session_id":"2eb843de-6f8c-52cf-aeda-a17cd08f26da",'
        '"creation_brief_sha256":"724d3ce32853c2dd8ef7e4d396cb6513"}'
    )
    model_projection = (
        '{"ok":true,"status":"hr_handoff_ready",'
        '"message":"The user-facing HR review action is available in the handoff card."}'
    )
    captured_done: list[dict] = []
    fake_client = _FakeClient(
        [
            _tool_call_response("read_file", "call_governed_receipt"),
            _final_response("Use the HR review action shown above."),
        ]
    )

    def execute_tool(tool_name, args, request, emit_event):
        return ToolContentEnvelope(text=raw_receipt, model_visible_text=model_projection)

    async def on_tool_call(payload: dict) -> None:
        if payload.get("status") == "done":
            captured_done.append(payload)

    kernel = AgentKernel(_base_deps(fake_client=fake_client, execute_tool=execute_tool))
    await kernel.handle(
        InvocationRequest(
            model=SimpleNamespace(provider="openai", model="gpt-4.1", api_key="key", base_url=None),
            messages=[{"role": "user", "content": "Start the governed handoff"}],
            agent_name="Engineer",
            role_description="Investigates repositories",
            agent_id=uuid4(),
            user_id=uuid4(),
            on_tool_call=on_tool_call,
        )
    )

    assert len(captured_done) == 1
    assert str(captured_done[0]["result"]) == raw_receipt
    assert captured_done[0]["model_seen_result"] == model_projection
    assert captured_done[0]["content_replacement"]["replacement_applied"] is True
    provider_tool_messages = [message for message in fake_client.calls[1]["messages"] if message.role == "tool"]
    assert [message.content for message in provider_tool_messages] == [model_projection]
    assert "hr_agent_id" not in str(provider_tool_messages[0].content)
    assert "hr_session_id" not in str(provider_tool_messages[0].content)


def test_side_effect_channel_has_no_production_producer() -> None:
    """B-3: the ``new_messages`` / ``terminal_signal`` side-effect channel is an
    explicitly-tracked DEFERRED CONTRACT — the kernel consumes it (the tests
    above prove that), but no production tool *constructs* an envelope with these
    fields. Every live terminal/clarification flow uses the JSON-status marker
    path instead, so the channel stays empty in practice.

    This guard pins that deferral: it scans the live ``app/`` sources for a
    producer (a ``ToolContentEnvelope(...)`` constructed with ``new_messages=`` or
    ``terminal_signal=``). If one ever appears, the channel has been activated and
    the DEFERRED-CONTRACT note on ``ToolContentEnvelope`` must be revisited — so
    this test fails on purpose to force that review, rather than letting a
    seeded-but-unwired field silently flip to live without documentation.
    """
    from pathlib import Path

    app_root = Path(__file__).resolve().parents[2] / "app"
    assert app_root.is_dir(), app_root
    # The definition module declares the fields (annotations, not kwargs); exclude it.
    definition = (app_root / "tools" / "result_envelope.py").resolve()

    producers: list[str] = []
    for path in app_root.rglob("*.py"):
        if path.resolve() == definition:
            continue
        text = path.read_text(encoding="utf-8")
        if "new_messages=" in text or "terminal_signal=" in text:
            producers.append(str(path.relative_to(app_root)))

    assert not producers, (
        "ToolContentEnvelope side-effect channel gained a production producer "
        f"({producers}); revisit the DEFERRED CONTRACT note in result_envelope.py."
    )
