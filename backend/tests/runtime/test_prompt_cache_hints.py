"""Tests for provider-specific prompt cache hints."""

from app.services.llm_client import LLMMessage, apply_prompt_cache_hints


def test_anthropic_provider_injects_cache_hints():
    messages = [
        LLMMessage(role="system", content="You are a helpful agent."),
        LLMMessage(role="user", content="Hello"),
        LLMMessage(role="assistant", content="Hi there!"),
        LLMMessage(role="user", content="What can you do?"),
    ]
    result = apply_prompt_cache_hints(messages, "anthropic")

    # System message should have cache_control
    sys_msg = result[0]
    assert isinstance(sys_msg.content, list)
    assert sys_msg.content[0]["cache_control"] == {"type": "ephemeral"}
    assert sys_msg.content[0]["text"] == "You are a helpful agent."

    # Last 3 non-system messages should have cache_control
    for msg in result[1:]:
        assert isinstance(msg.content, list)
        assert msg.content[0]["cache_control"] == {"type": "ephemeral"}


def test_claude_provider_also_injects_hints():
    messages = [
        LLMMessage(role="system", content="System prompt"),
        LLMMessage(role="user", content="Hi"),
    ]
    result = apply_prompt_cache_hints(messages, "claude-3-opus")

    sys_msg = result[0]
    assert isinstance(sys_msg.content, list)
    assert sys_msg.content[0]["cache_control"]["type"] == "ephemeral"


def test_non_anthropic_provider_unchanged():
    messages = [
        LLMMessage(role="system", content="System prompt"),
        LLMMessage(role="user", content="Hello"),
    ]
    result = apply_prompt_cache_hints(messages, "openai")

    # Messages should be unchanged
    assert result[0].content == "System prompt"
    assert isinstance(result[0].content, str)
    assert result[1].content == "Hello"
    assert isinstance(result[1].content, str)


def test_empty_messages_no_error():
    result = apply_prompt_cache_hints([], "anthropic")
    assert result == []


def test_originals_not_mutated():
    original = LLMMessage(role="system", content="Original")
    messages = [original]
    result = apply_prompt_cache_hints(messages, "anthropic")

    # Original should be untouched
    assert isinstance(original.content, str)
    assert original.content == "Original"
    # Result should be different
    assert isinstance(result[0].content, list)
