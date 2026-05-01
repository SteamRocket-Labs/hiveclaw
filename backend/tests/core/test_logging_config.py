import logging

from app.core.logging_config import intercept_standard_logging


def test_intercept_standard_logging_raises_noisy_http_client_log_levels() -> None:
    logging.getLogger("httpx").setLevel(logging.INFO)
    logging.getLogger("httpcore").setLevel(logging.INFO)

    intercept_standard_logging()

    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING
