from app.kernel.engine import _turn_token_budget_message
from app.services.llm_error_policy import classify_llm_error, is_llm_error_message


def test_turn_token_budget_message_is_runtime_failure() -> None:
    message = _turn_token_budget_message(tokens_used=1001, token_budget=1000)

    assert message.startswith("[Runtime Limit]")
    assert is_llm_error_message(message)
    assert "higher turn budget" not in message


def test_insufficient_balance_is_classified_as_quota_not_bad_request() -> None:
    classification = classify_llm_error(
        RuntimeError('HTTP 402 {"message":"Insufficient Balance","type":"invalid_request_error"}')
    )

    assert classification.kind == "quota_exhausted"
    assert classification.requires_user_decision is True
    assert "余额" in classification.user_message


def test_typed_http_status_402_is_quota_without_body_text() -> None:
    """DAY1-PROVIDER-402-CLASSIFICATION-001 correction: the quota hard outcome
    is owned by the authoritative typed ``http_status == 402``, never by
    natural-language body text.  An opaque or empty body must classify
    identically."""

    from app.services.llm_client import LLMError
    from app.services.llm_error_policy import should_surface_without_model_fallback

    opaque = classify_llm_error(LLMError("opaque provider rejection", delivery_state="rejected", http_status=402))
    assert opaque.kind == "quota_exhausted"
    assert opaque.requires_user_decision is True
    assert "余额" in opaque.user_message

    empty = classify_llm_error(LLMError("", delivery_state="rejected", http_status=402))
    assert empty.kind == "quota_exhausted"
    assert empty.requires_user_decision is True

    assert (
        should_surface_without_model_fallback(
            LLMError("opaque provider rejection", delivery_state="rejected", http_status=402)
        )
        is True
    )


def test_non_402_typed_status_does_not_become_quota() -> None:
    """Only exact typed 402 owns the quota outcome: 408/5xx/529/429 typed
    statuses must keep their existing behavior."""

    from app.services.llm_client import LLMError

    for status in (408, 500, 503, 529):
        classification = classify_llm_error(
            LLMError("opaque provider failure", delivery_state="unknown", http_status=status)
        )
        assert classification.kind != "quota_exhausted", status
        assert classification.requires_user_decision is False, status

    # 429 keeps its existing text-driven rate-limit path (unchanged).
    rate_limited = classify_llm_error(
        LLMError("HTTP 429: too many requests", delivery_state="rejected", http_status=429)
    )
    assert rate_limited.kind == "rate_limited"
