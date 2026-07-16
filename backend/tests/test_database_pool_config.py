"""C2 — DB pool parameters must come from settings, and the pool must be observable."""

from __future__ import annotations


def test_pool_settings_defaults_and_env_override(monkeypatch) -> None:
    from app.config import Settings

    defaults = Settings(_env_file=None)
    assert defaults.DB_POOL_SIZE == 20
    assert defaults.DB_MAX_OVERFLOW == 10
    assert defaults.DB_POOL_TIMEOUT == 30

    monkeypatch.setenv("DB_POOL_SIZE", "7")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "3")
    monkeypatch.setenv("DB_POOL_TIMEOUT", "5")
    overridden = Settings(_env_file=None)
    assert overridden.DB_POOL_SIZE == 7
    assert overridden.DB_MAX_OVERFLOW == 3
    assert overridden.DB_POOL_TIMEOUT == 5


def test_engine_pool_kwargs_reads_settings() -> None:
    from app.config import get_settings
    from app.database import _engine_pool_kwargs

    settings = get_settings()
    assert _engine_pool_kwargs(settings) == {
        "pool_size": settings.DB_POOL_SIZE,
        "max_overflow": settings.DB_MAX_OVERFLOW,
        "pool_timeout": settings.DB_POOL_TIMEOUT,
    }


def test_live_engine_pool_matches_settings() -> None:
    from app.config import get_settings
    from app.database import engine

    assert engine.pool.size() == get_settings().DB_POOL_SIZE


def test_snapshot_db_pool_reports_occupancy_fields() -> None:
    from app.database import snapshot_db_pool

    snap = snapshot_db_pool()
    assert isinstance(snap["size"], int)
    assert isinstance(snap["checked_out"], int)
    assert isinstance(snap["checked_in"], int)
    assert isinstance(snap["overflow"], int)
    assert snap["max_overflow"] >= 0
    assert snap["pool_timeout_seconds"] > 0
    assert snap["capacity"] == snap["size"] + snap["max_overflow"]
    assert snap["saturation_pct"] >= 0.0


def test_runtime_task_web_chat_recovery_budget_has_a_bounded_default() -> None:
    from app.config import Settings

    settings = Settings(_env_file=None)

    assert settings.RUNTIME_TASK_WEB_CHAT_MAX_EXECUTION_ATTEMPTS == 3
