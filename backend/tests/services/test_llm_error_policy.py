from app.kernel.engine import _turn_token_budget_message
from app.services.llm_error_policy import classify_llm_error, is_llm_error_message


def test_turn_token_budget_message_is_runtime_failure() -> None:
    message = _turn_token_budget_message(tokens_used=1001, token_budget=1000)

    # The persisted limit text is now Chinese; failure recognition is unchanged.
    assert message == "这次用完了用量预算，没有完成。"
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


def test_minimax_token_plan_quota_keeps_body_classification_and_delivery_facts() -> None:
    from app.kernel.engine import _humanize_llm_error
    from app.services.llm_client import LLMError
    from app.services.llm_error_policy import should_surface_without_model_fallback

    message = (
        'HTTP 429: {"type":"error","error":{"type":"rate_limit_error",'
        '"message":"已达到 Token Plan 用量上限：请升级 Token Plan 套餐或购买积分补充用量。 (2056)",'
        '"http_code":"429"}}'
    )
    for delivery_state in ("rejected", "unknown"):
        error = LLMError(message, delivery_state=delivery_state, http_status=429)

        classification = classify_llm_error(error)

        # Round 2 restores the base Token Plan outcome: a typed rate-limit
        # status must retain the body's account-quota explanation.
        assert classification.kind == "quota_exhausted"
        assert classification.retryable is False
        assert classification.requires_user_decision is True
        assert should_surface_without_model_fallback(error) is True
        assert "额度已耗尽" in _humanize_llm_error(error)
        assert error.delivery_state == delivery_state
        assert error.http_status == 429
    assert classify_llm_error(RuntimeError(message)).kind == "quota_exhausted"


def test_both_limit_messages_are_excluded_from_compaction_assistant_speech() -> None:
    from app.kernel.engine import _tool_round_limit_message
    from app.services.conversation_summarizer import _build_summary_input

    messages = [
        _turn_token_budget_message(tokens_used=1001, token_budget=1000),
        _tool_round_limit_message(200),
    ]
    for message in messages:
        assert is_llm_error_message(message)
    text, dropped = _build_summary_input([
        {"role": "assistant", "content": message} for message in messages
    ] + [{"role": "assistant", "content": "The analysis is ready."}], provider="openai")
    assert all(message not in text for message in messages)
    assert "assistant: The analysis is ready." in text
    assert dropped == 0
