from app.kernel.engine import _turn_token_budget_message
from app.services.llm_error_policy import is_llm_error_message


def test_turn_token_budget_message_is_runtime_failure() -> None:
    message = _turn_token_budget_message(tokens_used=1001, token_budget=1000)

    assert message.startswith("[Runtime Limit]")
    assert is_llm_error_message(message)
    assert "higher turn budget" not in message
