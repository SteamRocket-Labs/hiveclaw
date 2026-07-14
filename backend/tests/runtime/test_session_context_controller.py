from __future__ import annotations

import pytest


def _msg(role: str, content: str, **extra):
    from app.services.llm_client import LLMMessage

    return LLMMessage(role=role, content=content, **extra)


def test_cc_style_autocompact_threshold_uses_fixed_buffer() -> None:
    from app.runtime.ccplus_contracts import ContextPolicyV1
    from app.runtime.session_context_controller import calculate_runtime_token_status

    status = calculate_runtime_token_status(
        active_context_tokens=212_000,
        policy=ContextPolicyV1(model_window=256_000),
        cumulative_run_tokens=900_000,
    )

    assert status.full_context_window_limit == 256_000
    assert status.auto_compact_scope_limit == 223_000
    assert status.tokens_until_compaction == 11_000
    assert status.cumulative_run_tokens == 900_000
    assert status.token_limit_reached is False


def test_cumulative_run_tokens_do_not_trigger_context_limit() -> None:
    from app.runtime.ccplus_contracts import ContextPolicyV1
    from app.runtime.session_context_controller import calculate_runtime_token_status

    status = calculate_runtime_token_status(
        active_context_tokens=20_000,
        policy=ContextPolicyV1(model_window=256_000),
        cumulative_run_tokens=1_200_000,
    )

    assert status.should_autocompact is False
    assert status.token_limit_reached is False
    assert status.full_context_window_limit_reached is False


def test_tool_result_budget_pass_compacts_only_recoverable_tool_results() -> None:
    from app.runtime.session_context_controller import apply_tool_result_budget

    digest = "a" * 64
    recoverable = (
        "C" * 80
        + "\n\n[Full output saved to workspace/tool_results/c.txt — 8000 chars; "
        + f"sha256={digest}; char_range=0-8000; reason: threshold. "
        + 'Use read_file("workspace/tool_results/c.txt") to retrieve.]'
    )
    messages = [
        _msg("user", "search"),
        _msg(
            "assistant",
            "",
            tool_calls=[
                {"id": "a", "function": {"name": "run_command"}},
                {"id": "b", "function": {"name": "read_file"}},
                {"id": "c", "function": {"name": "web_fetch", "arguments": '{"url":"https://example.com/a"}'}},
            ],
        ),
        _msg("tool", "A" * 80, tool_call_id="a"),
        _msg("tool", "B" * 80, tool_call_id="b"),
        _msg("tool", recoverable, tool_call_id="c"),
    ]

    result = apply_tool_result_budget(
        messages,
        aggregate_char_budget=120,
        inline_char_limit=70,
        exempt_tool_names={"read_file"},
    )

    assert result.changed is True
    assert result.trimmed_count == 1
    assert result.messages[2].content == "A" * 80
    assert result.messages[3].content == "B" * 80
    assert result.messages[4].content.startswith("[Tool result compacted before next model request:")
    assert "workspace/tool_results/c.txt" in result.messages[4].content
    assert digest in result.messages[4].content
    assert result.after_chars < result.before_chars
    assert [item["tool_call_id"] for item in result.trimmed_context_effects] == ["c"]
    assert result.trimmed_context_effects[0]["tool_name"] == "web_fetch"
    assert result.trimmed_context_effects[0]["reload_pointer"] == {
        "kind": "workspace_artifact",
        "path": "workspace/tool_results/c.txt",
        "sha256": digest,
        "char_range": "0-8000",
    }
    assert result.trimmed_context_effects[0]["result_kind"] == "evidence"
    assert result.trimmed_context_effects[0]["context_effect"] == "external_reference"
    assert result.trimmed_context_effects[0]["source_refs"] == ["url:https://example.com/a"]


@pytest.mark.asyncio
async def test_prepare_session_context_records_tool_result_budget_runtime_decision() -> None:
    from app.runtime.ccplus_contracts import ContextPolicyV1
    from app.runtime.session_context_controller import prepare_session_context_for_request

    decisions: list[dict] = []

    async def fake_compress(messages, **_kwargs):
        raise AssertionError("tool-result budget pass should not force semantic compression")

    digest = "b" * 64
    recoverable = (
        "A" * 120
        + "\n\n[Full output saved to workspace/tool_results/call-1.txt — 1200 chars; "
        + f"sha256={digest}; char_range=0-1200; reason: threshold. "
        + 'Use read_file("workspace/tool_results/call-1.txt") to retrieve.]'
    )

    await prepare_session_context_for_request(
        messages=[
            _msg("user", "inspect"),
            _msg("assistant", "", tool_calls=[{"id": "call-1", "function": {"name": "run_command"}}]),
            _msg("tool", recoverable, tool_call_id="call-1"),
        ],
        policy=ContextPolicyV1(model_window=256_000, round_tool_result_budget=60, tool_result_inline_limit=50),
        estimate_tokens=lambda _msgs: 100,
        compress_messages=fake_compress,
        on_decision=decisions.append,
    )

    budget_event = decisions[0]
    runtime_decision = budget_event["runtime_decision_entry"]
    assert budget_event["event_type"] == "tool_result_budget_pass"
    assert runtime_decision["kind"] == "compaction"
    assert runtime_decision["trigger"] == "tool_result_budget"
    assert runtime_decision["status"] == "completed"
    assert runtime_decision["next_action"] == "recalculate_context_window"
    agent_cycle = runtime_decision["agent_cycle_decision_entry"]
    assert agent_cycle["schema"] == "hive.ccplus.agent_cycle_decision.v1"
    assert agent_cycle["trigger"] == "tool_result_budget"
    assert agent_cycle["judge"] == "compaction_controller"
    assert agent_cycle["decision"] == "completed"
    assert agent_cycle["outcome"] == "completed"
    assert agent_cycle["model_interaction"] == "runtime_control"
    assert agent_cycle["permission_result"] == "unchanged"
    assert agent_cycle["budget_result"] == "tool_result_trimmed"
    assert runtime_decision["details"]["tool_result_trimmed"] is True
    assert runtime_decision["details"]["trimmed_tool_call_ids"] == ["call-1"]
    assert runtime_decision["details"]["trimmed_context_effects"][0]["tool_call_id"] == "call-1"
    assert budget_event["trimmed_context_effects"][0]["reload_pointer"]["path"] == ("workspace/tool_results/call-1.txt")


@pytest.mark.asyncio
async def test_prepare_session_context_emits_skipped_reason_when_below_threshold() -> None:
    from app.runtime.ccplus_contracts import ContextPolicyV1
    from app.runtime.session_context_controller import prepare_session_context_for_request

    decisions: list[dict] = []

    async def fake_compress(messages, **_kwargs):
        raise AssertionError("compression should not run below threshold")

    result = await prepare_session_context_for_request(
        messages=[_msg("user", "hello")],
        policy=ContextPolicyV1(model_window=256_000),
        estimate_tokens=lambda msgs: 50,
        compress_messages=fake_compress,
        cumulative_run_tokens=1_000_000,
        on_decision=decisions.append,
    )

    assert result.token_status.active_context_tokens == 50
    assert result.changed is False
    assert [item["event_type"] for item in decisions] == ["context_window_status", "compaction_skipped"]
    assert decisions[-1]["reason"] == "below_autocompact_threshold"
    assert decisions[-1]["cumulative_run_tokens"] == 1_000_000
    runtime_decision = decisions[-1]["runtime_decision_entry"]
    assert runtime_decision["kind"] == "compaction"
    assert runtime_decision["status"] == "skipped"
    assert runtime_decision["next_action"] == "continue"
    assert runtime_decision["details"]["threshold"] == result.token_status.auto_compact_scope_limit


@pytest.mark.asyncio
async def test_prepare_session_context_compresses_when_cc_threshold_reached() -> None:
    from app.runtime.ccplus_contracts import ContextPolicyV1
    from app.runtime.session_context_controller import prepare_session_context_for_request

    decisions: list[dict] = []
    messages = [_msg("user", "old"), _msg("assistant", "older"), _msg("user", "latest")]

    async def fake_compress(messages_arg, **kwargs):
        assert kwargs["compress_threshold"] == 1.0
        on_compaction = kwargs.get("on_compaction")
        if on_compaction:
            maybe = on_compaction(
                {
                    "summary": "summary",
                    "original_message_count": len(messages_arg),
                    "kept_message_count": 2,
                }
            )
            if maybe is not None:
                await maybe
        return [{"role": "system", "content": "summary"}, {"role": "user", "content": "latest"}]

    result = await prepare_session_context_for_request(
        messages=messages,
        policy=ContextPolicyV1(model_window=256_000),
        estimate_tokens=lambda _msgs: 224_000,
        compress_messages=fake_compress,
        cumulative_run_tokens=0,
        on_decision=decisions.append,
        compress_kwargs={"model_provider": "openai", "model_name": "gpt-test", "tenant_id": None},
    )

    assert result.changed is True
    assert len(result.messages) == 2
    assert [item["event_type"] for item in decisions] == [
        "context_window_status",
        "compaction_started",
        "compaction_completed",
        "compaction_lifecycle",
    ]
    lifecycle = decisions[-1]["compaction_lifecycle"]
    assert lifecycle["trigger"] == "request_preflight"
    assert lifecycle["before_message_count"] == 3
    assert lifecycle["after_message_count"] == 2
    runtime_decisions = [item["runtime_decision_entry"] for item in decisions if item.get("runtime_decision_entry")]
    assert runtime_decisions[-1]["kind"] == "compaction"
    assert runtime_decisions[-1]["status"] == "completed"
    assert runtime_decisions[-1]["next_action"] == "continue"
    assert runtime_decisions[-1]["details"]["before_tokens"] == 224_000
    assert runtime_decisions[-1]["details"]["after_tokens"] <= 224_000
