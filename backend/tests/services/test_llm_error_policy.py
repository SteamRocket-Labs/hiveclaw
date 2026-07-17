from app.kernel.engine import _turn_token_budget_message
from app.services.llm_error_policy import classify_llm_error, is_llm_error_message


def test_turn_token_budget_message_is_runtime_failure() -> None:
    message = _turn_token_budget_message(tokens_used=1001, token_budget=1000)

    assert message.startswith("[Runtime Limit]")
    assert is_llm_error_message(message)
    assert "higher turn budget" not in message


def test_insufficient_balance_is_classified_as_quota_not_bad_request() -> None:
    classification = classify_llm_error(
        RuntimeError(
            'HTTP 402 {"message":"Insufficient Balance","type":"invalid_request_error"}'
        )
    )

    assert classification.kind == "quota_exhausted"
    assert classification.requires_user_decision is True
    assert "余额" in classification.user_message
