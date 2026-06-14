"""Provider registry config + MiniMax thinking-split wiring.

Covers two things:

1. ProviderSpec.max_input_tokens / default_max_tokens carry the officially
   confirmed window/output values per provider.
2. The OpenAI-compatible client injects ``reasoning_split: True`` into the
   request payload for the MiniMax provider only — wiring the previously
   metadata-only ``minimax_reasoning_split`` strategy into real requests so
   M3 thinking is separated into ``reasoning_content`` instead of inlined.
"""

from __future__ import annotations

from app.services.llm_client import (
    LLMMessage,
    OpenAICompatibleClient,
    create_llm_client,
    get_provider_spec,
)


# ---------------------------------------------------------------------------
# ProviderSpec config values (officially confirmed numbers)
# ---------------------------------------------------------------------------


def test_max_input_tokens_official_windows():
    assert get_provider_spec("minimax").max_input_tokens == 1000000
    assert get_provider_spec("anthropic").max_input_tokens == 1000000
    assert get_provider_spec("openai").max_input_tokens == 272000
    assert get_provider_spec("openai-response").max_input_tokens == 272000
    assert get_provider_spec("qwen").max_input_tokens == 1000000
    assert get_provider_spec("kimi").max_input_tokens == 262144
    assert get_provider_spec("zhipu").max_input_tokens == 200000


def test_max_input_tokens_unchanged_providers():
    from app.services.llm_client import ProviderSpec

    assert get_provider_spec("deepseek").max_input_tokens == 1000000
    assert get_provider_spec("gemini").max_input_tokens == 1048576
    # ProviderSpec class default window held at 256000.
    assert ProviderSpec.max_input_tokens == 256000
    # Providers carrying their own explicit window are untouched by this change.
    assert get_provider_spec("openrouter").max_input_tokens == 128000
    assert get_provider_spec("azure").max_input_tokens == 128000


def test_default_max_tokens_output_budgets():
    # Class default raised from 8192 -> 32768.
    from app.services.llm_client import ProviderSpec

    assert ProviderSpec.default_max_tokens == 32768

    assert get_provider_spec("deepseek").default_max_tokens == 65536
    assert get_provider_spec("anthropic").default_max_tokens == 32768
    assert get_provider_spec("zhipu").default_max_tokens == 32768
    assert get_provider_spec("qwen").default_max_tokens == 32768
    assert get_provider_spec("kimi").default_max_tokens == 32768


def test_conservative_output_budgets_held():
    # MiniMax M3 max output not officially confirmed -> stay at 16384.
    assert get_provider_spec("minimax").default_max_tokens == 16384
    # OpenAI held at 16384.
    assert get_provider_spec("openai").default_max_tokens == 16384
    assert get_provider_spec("openai-response").default_max_tokens == 16384


# ---------------------------------------------------------------------------
# Per-provider output hard ceilings (replaces the flat 65536 cap)
# ---------------------------------------------------------------------------


def test_provider_spec_max_output_tokens_class_default():
    # Class default ceiling raised from the implicit 65536 to 131072 so a
    # provider without an explicit value is no longer artificially capped.
    from app.services.llm_client import ProviderSpec

    assert ProviderSpec.max_output_tokens == 131072


def test_per_provider_output_ceilings_configured():
    # DeepSeek V4 officially emits up to 384K output tokens.
    assert get_provider_spec("deepseek").max_output_tokens == 384000
    # Claude 128K-class output.
    assert get_provider_spec("anthropic").max_output_tokens == 131072
    assert get_provider_spec("openai").max_output_tokens == 131072
    assert get_provider_spec("openai-response").max_output_tokens == 131072


def test_unconfigured_providers_use_class_default_ceiling():
    # Providers without an explicit value fall back to the class default 131072.
    for provider in ("minimax", "qwen", "kimi", "zhipu", "gemini", "azure", "openrouter"):
        assert get_provider_spec(provider).max_output_tokens == 131072


def test_absolute_output_ceiling_constant():
    # A global absolute ceiling guards against runaway per-provider configs.
    from app.services.llm_client import ABSOLUTE_MAX_OUTPUT_TOKENS

    assert ABSOLUTE_MAX_OUTPUT_TOKENS == 524288
    # No configured per-provider ceiling may exceed the absolute cap.
    from app.services.llm_client import PROVIDER_REGISTRY

    for spec in PROVIDER_REGISTRY.values():
        assert spec.max_output_tokens <= ABSOLUTE_MAX_OUTPUT_TOKENS


def test_minimax_m27_per_model_window_preserved():
    spec = get_provider_spec("minimax")
    assert spec.model_max_tokens.get("MiniMax-M2.7") == 204800


def test_recommended_models_flagships():
    minimax = get_provider_spec("minimax").recommended_models
    assert minimax[0] == {
        "model": "MiniMax-M3",
        "label": "MiniMax M3",
        "supports_reasoning": True,
    }

    anthropic = get_provider_spec("anthropic").recommended_models
    assert anthropic[0] == {
        "model": "claude-opus-4-8",
        "label": "Claude Opus 4.8",
        "supports_reasoning": True,
        "reasoning_mode": "adaptive",
    }


# ---------------------------------------------------------------------------
# MiniMax thinking-split wiring
# ---------------------------------------------------------------------------


def _minimax_client() -> OpenAICompatibleClient:
    client = create_llm_client(provider="minimax", api_key="test", model="MiniMax-M3")
    return getattr(client, "_inner", client)


def test_minimax_payload_includes_reasoning_split():
    client = _minimax_client()
    payload = client._build_payload(
        [LLMMessage(role="user", content="hi")],
        tools=None,
        temperature=0.7,
        max_tokens=512,
    )
    assert payload["reasoning_split"] is True


def test_minimax_reasoning_split_present_for_streaming_payload():
    client = _minimax_client()
    payload = client._build_payload(
        [LLMMessage(role="user", content="hi")],
        tools=None,
        temperature=0.7,
        max_tokens=512,
        stream=True,
    )
    assert payload["reasoning_split"] is True


def test_non_minimax_provider_has_no_reasoning_split():
    for provider, model in (
        ("openai", "gpt-5.4"),
        ("deepseek", "deepseek-v4-pro"),
        ("qwen", "qwen3-max"),
        ("kimi", "kimi-k2.5"),
        ("zhipu", "glm-4.7"),
    ):
        wrapped = create_llm_client(provider=provider, api_key="test", model=model)
        client = getattr(wrapped, "_inner", wrapped)
        payload = client._build_payload(
            [LLMMessage(role="user", content="hi")],
            tools=None,
            temperature=0.7,
            max_tokens=512,
        )
        assert "reasoning_split" not in payload, provider


def test_minimax_reasoning_split_does_not_override_explicit_kwarg():
    # When build_reasoning_kwargs already supplies reasoning_split (e.g. the
    # preserve_reasoning path), the client must not clobber the caller's value.
    client = _minimax_client()
    payload = client._build_payload(
        [LLMMessage(role="user", content="hi")],
        tools=None,
        temperature=0.7,
        max_tokens=512,
        reasoning_split=True,
    )
    assert payload["reasoning_split"] is True


def test_directly_constructed_client_without_provider_omits_reasoning_split():
    # A bare OpenAICompatibleClient (no provider hint) must not inject the
    # MiniMax-specific field — only minimax-provider clients do.
    client = OpenAICompatibleClient(api_key="test", model="gpt-test", base_url="https://x/v1")
    payload = client._build_payload(
        [LLMMessage(role="user", content="hi")],
        tools=None,
        temperature=0.7,
        max_tokens=512,
    )
    assert "reasoning_split" not in payload
