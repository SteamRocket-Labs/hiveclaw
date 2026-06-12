import logging
import json
from io import StringIO

from app.core.logging_config import intercept_standard_logging


def test_log_record_enrichment_uses_stable_process_trace_without_context() -> None:
    from app.core.logging_config import clear_trace_id, enrich_log_record

    clear_trace_id()
    first = {"extra": {}}
    second = {"extra": {}}

    assert enrich_log_record(first) is True
    assert enrich_log_record(second) is True
    assert first["extra"]["trace_id"] == second["extra"]["trace_id"]
    assert first["extra"]["trace_id"].startswith("process-")


def test_configure_logging_defaults_to_json(monkeypatch) -> None:
    from loguru import logger

    from app.core.logging_config import clear_trace_id, configure_logging

    monkeypatch.delenv("HIVE_LOG_FORMAT", raising=False)
    sink = StringIO()
    clear_trace_id()
    configure_logging(sink=sink, enqueue=False)

    logger.info("structured log probe")

    payload = json.loads(sink.getvalue().strip())
    assert payload["record"]["message"] == "structured log probe"
    assert payload["record"]["extra"]["trace_id"].startswith("process-")


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


def test_sanitize_standard_log_message_redacts_lark_ws_sensitive_query_params() -> None:
    from app.core.logging_config import sanitize_standard_log_message

    raw = (
        "connected to wss://msg-frontier.feishu.cn/ws/v2?"
        "device_id=device-1&access_key=secret-access&ticket=secret-ticket&service_id=33554678"
    )

    sanitized = sanitize_standard_log_message(raw)

    assert "access_key=secret-access" not in sanitized
    assert "ticket=secret-ticket" not in sanitized
    assert "access_key=<redacted>" in sanitized
    assert "ticket=<redacted>" in sanitized
    assert "device_id=device-1" in sanitized
    assert "service_id=33554678" in sanitized
