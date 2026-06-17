from __future__ import annotations


def test_estimate_tokens_from_text_keeps_legacy_ascii_ratio() -> None:
    from app.services.token_tracker import estimate_tokens_from_text

    assert estimate_tokens_from_text("a" * 350) == 100


def test_estimate_tokens_from_text_counts_cjk_near_character_count() -> None:
    from app.services.token_tracker import estimate_tokens_from_chars, estimate_tokens_from_text

    text = "中文预算校准会影响长任务上下文和费用观测" * 20

    assert estimate_tokens_from_text(text) >= int(len(text) * 0.9)
    assert estimate_tokens_from_text(text) > estimate_tokens_from_chars(len(text)) * 3


def test_estimate_tokens_from_text_handles_mixed_cjk_and_ascii() -> None:
    from app.services.token_tracker import estimate_tokens_from_text

    text = ("agent budget " * 20) + ("中文预算" * 20)

    assert estimate_tokens_from_text(text) > int(len(text) / 3.5)
    assert estimate_tokens_from_text(text) < len(text)


def test_extract_usage_tokens_excludes_deepseek_prompt_cache_hits() -> None:
    from app.services.token_tracker import extract_usage_tokens

    assert (
        extract_usage_tokens(
            {
                "total_tokens": 139482,
                "prompt_tokens": 139154,
                "completion_tokens": 328,
                "prompt_cache_hit_tokens": 133120,
                "prompt_cache_miss_tokens": 6034,
            }
        )
        == 6362
    )


def test_extract_usage_tokens_excludes_openai_cached_prompt_tokens() -> None:
    from app.services.token_tracker import extract_usage_tokens

    assert (
        extract_usage_tokens(
            {
                "total_tokens": 1000,
                "prompt_tokens": 900,
                "completion_tokens": 100,
                "prompt_tokens_details": {"cached_tokens": 700},
            }
        )
        == 300
    )


def test_extract_usage_tokens_counts_anthropic_native_usage_without_double_subtracting_cache_read() -> None:
    from app.services.token_tracker import extract_usage_tokens

    assert (
        extract_usage_tokens(
            {
                "input_tokens": 900,
                "output_tokens": 100,
                "cache_read_input_tokens": 700,
                "cache_creation_input_tokens": 50,
            }
        )
        == 1050
    )


def test_extract_usage_tokens_prefers_deepseek_cache_miss_when_available() -> None:
    from app.services.token_tracker import extract_usage_tokens

    assert (
        extract_usage_tokens(
            {
                "total_tokens": 20_000,
                "completion_tokens": 123,
                "prompt_cache_hit_tokens": 18_000,
                "prompt_cache_miss_tokens": 1_877,
            }
        )
        == 2_000
    )
