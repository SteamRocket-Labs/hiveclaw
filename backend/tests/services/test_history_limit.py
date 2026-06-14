"""Tests for compute_history_limit — model-aware history loading."""

from __future__ import annotations


def test_compute_history_limit_anthropic_1m():
    """Anthropic 1M context → dynamic budget clamps to the 800 maximum."""
    from app.services.memory_service import compute_history_limit
    limit = compute_history_limit("anthropic", "claude-sonnet-4-20250514")
    # 1M window (ProviderSpec default) - reserves = 784500 / 300 = 2615 → clamped to 800
    assert limit == 800


def test_compute_history_limit_openai_272k():
    """OpenAI 272k context → dynamic budget after reserves."""
    from app.services.memory_service import compute_history_limit
    limit = compute_history_limit("openai", "gpt-4o")
    # 272000 - 54400(prompt=20%) - 1500(tools) - 8000(gen) - 6000(memory) = 202100 / 300 = 673
    assert limit == 673


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
    limit_default = compute_history_limit("openai", "gpt-4o")  # 272k default
    # Override below the provider default proves the override is honored: a
    # smaller window yields a smaller budget than the 272k default.
    limit_override = compute_history_limit("openai", "gpt-4o", max_input_tokens_override=100_000)
    assert limit_override < limit_default


def test_compute_history_limit_unknown_provider_uses_128k_fallback():
    """Unknown provider should fallback to 128k context."""
    from app.services.memory_service import compute_history_limit
    limit = compute_history_limit("some_unknown_provider", "some-model")
    # 128000 - 25600(20%) - 1500(tools) - 8000(gen) - 6000(memory) = 86900 / 300 = 289
    assert limit == 289


def test_compute_history_limit_with_real_prompt_tokens():
    """When real system_prompt_tokens provided, budget is more accurate."""
    from app.services.memory_service import compute_history_limit
    # Pin the window to 128k so neither path clamps to the 800 max, keeping the
    # "real tokens are more accurate than the 20% estimate" contrast observable.
    # Real prompt: 128000 - 8000(prompt) - 3000(tools) - 8000(gen) - 6000(memory) = 103000 / 300 = 343
    limit = compute_history_limit(
        "openai", "gpt-4o",
        max_input_tokens_override=128_000,
        system_prompt_tokens=8000,
        tool_definitions_tokens=3000,
    )
    assert limit == 343
