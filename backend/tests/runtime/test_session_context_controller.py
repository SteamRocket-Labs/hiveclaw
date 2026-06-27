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


def test_tool_result_budget_pass_compacts_oldest_non_exempt_tool_results() -> None:
    from app.runtime.session_context_controller import apply_tool_result_budget

    messages = [
        _msg("user", "search"),
        _msg(
            "assistant",
            "",
            tool_calls=[
                {"id": "a", "function": {"name": "run_command"}},
                {"id": "b", "function": {"name": "read_file"}},
                {"id": "c", "function": {"name": "run_command"}},
            ],
        ),
        _msg("tool", "A" * 80, tool_call_id="a"),
        _msg("tool", "B" * 80, tool_call_id="b"),
        _msg("tool", "C" * 80, tool_call_id="c"),
    ]

    result = apply_tool_result_budget(
        messages,
        aggregate_char_budget=120,
        inline_char_limit=70,
        exempt_tool_names={"read_file"},
    )

    assert result.changed is True
    assert result.trimmed_count == 2
    assert result.messages[2].content.startswith("[Tool result compacted before next model request:")
    assert result.messages[3].content == "B" * 80
    assert result.messages[4].content.startswith("[Tool result compacted before next model request:")
    assert result.after_chars < result.before_chars


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
    ]
