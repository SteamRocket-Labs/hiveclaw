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
