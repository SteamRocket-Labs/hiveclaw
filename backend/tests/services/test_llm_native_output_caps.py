"""Native model ceilings are separate from default output allocations."""

from types import SimpleNamespace

import pytest

from app.kernel.engine import _resolve_continuation_max_tokens
from app.services.llm_client import OpenAIResponsesClient, get_max_tokens, get_provider_output_ceiling


@pytest.mark.parametrize("provider", ["openai", "openai-response", "openai_response"])
@pytest.mark.parametrize("model", ["gpt-5.5", "gpt-5.5-2026-04-23", "gpt-5.4", "gpt-5.4-2026-03-05"])
def test_known_native_output_ceiling_preserves_the_full_model_limit(provider, model):
    assert get_provider_output_ceiling(provider, model) == 128_000
    assert get_max_tokens(provider, model, max_output_tokens=131_072) == 128_000
    assert get_max_tokens(provider, model, max_output_tokens=128_000) == 128_000
    assert get_max_tokens(provider, model) == 16_384
    assert _resolve_continuation_max_tokens(SimpleNamespace(provider=provider, model=model, max_output_tokens=8192)) == 128_000


@pytest.mark.parametrize("provider,model,expected", [
    ("openai", "gpt-5.5-future", 131_072),
    ("custom", "gpt-5.5", 131_072),
    ("deepseek", "deepseek-v4-pro", 384_000),
    ("anthropic", "claude-opus-4-8", 131_072),
])
def test_unknown_models_and_other_providers_keep_their_existing_ceiling(provider, model, expected):
    assert get_provider_output_ceiling(provider, model) == expected
    assert _resolve_continuation_max_tokens(SimpleNamespace(provider=provider, model=model)) == expected


def test_responses_output_cap_is_a_reported_partial_generation():
    client = OpenAIResponsesClient(api_key="synthetic-key", model="gpt-5.5-2026-04-23")
    data = {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"},
        "output_text": "Preserve this partial answer", "usage": {"input_tokens": 100, "output_tokens": 8192}}
    assert client._extract_api_error(data) is None
    response = client._parse_response_data(data)
    assert response.content == "Preserve this partial answer" and response.finish_reason == "length"
    assert response.usage == data["usage"]


@pytest.mark.parametrize("data", [
    {"status": "incomplete", "incomplete_details": {"reason": "content_filter"}},
    {"status": "failed", "incomplete_details": {"reason": "max_output_tokens"}},
    {"status": "cancelled"},
    {"status": "incomplete", "incomplete_details": {"reason": "max_output_tokens"}, "error": {"message": "Provider error"}},
])
def test_other_responses_failures_are_not_reinterpreted_as_output_caps(data):
    client = OpenAIResponsesClient(api_key="synthetic-key", model="gpt-5.5-2026-04-23")
    assert client._extract_api_error(data)
