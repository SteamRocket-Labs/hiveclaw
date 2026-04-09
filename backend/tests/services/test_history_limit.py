"""Tests for compute_history_limit — model-aware history loading."""

from __future__ import annotations


def test_compute_history_limit_anthropic_200k():
    """Anthropic 200k context → dynamic budget after reserves."""
    from app.services.memory_service import compute_history_limit
    limit = compute_history_limit("anthropic", "claude-sonnet-4-20250514")
    # 200000 - 40000(prompt=20%) - 1500(tools) - 8000(gen) - 6000(memory) = 144500 / 300 = 481
    assert limit == 481


def test_compute_history_limit_openai_128k():
    """OpenAI 128k context → dynamic budget after reserves."""
    from app.services.memory_service import compute_history_limit
    limit = compute_history_limit("openai", "gpt-4o")
    # 128000 - 25600(prompt=20%) - 1500(tools) - 8000(gen) - 6000(memory) = 86900 / 300 = 289
    assert limit == 289


def test_compute_history_limit_small_model():
    """Small model with 8k context → should clamp to minimum 20."""
    from app.services.memory_service import compute_history_limit
    # prompt_reserve = max(3000, 8000*0.20) = 3000 (since 1600 < 3000)
    # Wait: max(3000, 8000*0.20) = max(3000, 1600) = 3000
    # 8000 - 3000 - 1500 - 8000 - 6000 = -10500 < 0 → fallback 8000/4 = 2000 / 300 = 6 → clamped to 20
    limit = compute_history_limit("openai", "gpt-4o", max_input_tokens_override=8000)
    assert limit == 20


def test_compute_history_limit_huge_model():
    """Huge context (1M) → should clamp to maximum 800."""
    from app.services.memory_service import compute_history_limit
    limit = compute_history_limit("anthropic", "claude-opus-4-20250514", max_input_tokens_override=1_000_000)
    # 1M - 200000(20%) - 1500 - 8000 - 6000 = 784500 / 300 = 2615 → clamped to 800
    assert limit == 800


def test_compute_history_limit_override_takes_precedence():
    """max_input_tokens_override should override provider default."""
    from app.services.memory_service import compute_history_limit
    limit_default = compute_history_limit("deepseek", "deepseek-chat")  # 64k default
    limit_override = compute_history_limit("deepseek", "deepseek-chat", max_input_tokens_override=200_000)
    assert limit_override > limit_default


def test_compute_history_limit_unknown_provider_uses_128k_fallback():
    """Unknown provider should fallback to 128k context."""
    from app.services.memory_service import compute_history_limit
    limit = compute_history_limit("some_unknown_provider", "some-model")
    # 128000 - 25600(20%) - 1500(tools) - 8000(gen) - 6000(memory) = 86900 / 300 = 289
    assert limit == 289


def test_compute_history_limit_with_real_prompt_tokens():
    """When real system_prompt_tokens provided, budget is more accurate."""
    from app.services.memory_service import compute_history_limit
    # Real prompt: 128000 - 8000(prompt) - 3000(tools) - 8000(gen) - 6000(memory) = 103000 / 300 = 343
    limit = compute_history_limit(
        "openai", "gpt-4o",
        system_prompt_tokens=8000,
        tool_definitions_tokens=3000,
    )
    assert limit == 343
