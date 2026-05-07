import logging

from app.core.logging_config import intercept_standard_logging


def test_intercept_standard_logging_raises_noisy_success_loggers_to_warning() -> None:
    logging.getLogger("httpx").setLevel(logging.INFO)
    logging.getLogger("httpcore").setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    logging.getLogger("websockets.server").setLevel(logging.INFO)
    logging.getLogger("websockets.legacy.server").setLevel(logging.INFO)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)

    intercept_standard_logging()

    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("uvicorn.access").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("websockets.server").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("websockets.legacy.server").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("uvicorn.error").getEffectiveLevel() == logging.INFO


def test_sanitize_standard_log_message_strips_websocket_query_string() -> None:
    from app.core.logging_config import sanitize_standard_log_message

    raw = (
        '91.103.122.193:0 - "WebSocket '
        '/ws/chat/f5aefa50-91ff-4282-b4df-bb7baaa76e0b?token=secret-token&session_id=session-123" [accepted]'
    )

    sanitized = sanitize_standard_log_message(raw)

    assert sanitized == '91.103.122.193:0 - "WebSocket /ws/chat/f5aefa50-91ff-4282-b4df-bb7baaa76e0b" [accepted]'
    assert "secret-token" not in sanitized
    assert "session_id=" not in sanitized
