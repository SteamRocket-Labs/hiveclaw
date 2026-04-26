from app.services.llm_client import get_max_tokens


def test_get_max_tokens_clamps_oversized_override_for_custom_provider():
    assert get_max_tokens("custom", model="qwen3.6-plus", max_output_tokens=999999) == 65536


def test_get_max_tokens_preserves_valid_override():
    assert get_max_tokens("custom", model="qwen3.6-plus", max_output_tokens=32000) == 32000
