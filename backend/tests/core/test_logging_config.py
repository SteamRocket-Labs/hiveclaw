import logging

from app.core.logging_config import intercept_standard_logging


def test_intercept_standard_logging_raises_noisy_success_loggers_to_warning() -> None:
    logging.getLogger("httpx").setLevel(logging.INFO)
    logging.getLogger("httpcore").setLevel(logging.INFO)
    logging.getLogger("uvicorn.access").setLevel(logging.INFO)
    logging.getLogger("uvicorn.error").setLevel(logging.INFO)

    intercept_standard_logging()

    assert logging.getLogger("httpx").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("httpcore").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("uvicorn.access").getEffectiveLevel() == logging.WARNING
    assert logging.getLogger("uvicorn.error").getEffectiveLevel() == logging.INFO
