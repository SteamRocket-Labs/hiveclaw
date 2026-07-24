"""C2 — DB pool parameters must come from settings, and the pool must be observable."""

from __future__ import annotations


def test_pool_settings_defaults_and_env_override(monkeypatch) -> None:
    from app.config import Settings

    defaults = Settings(_env_file=None)
    assert defaults.DB_POOL_SIZE == 20
    assert defaults.DB_MAX_OVERFLOW == 10
    assert defaults.DB_POOL_TIMEOUT == 30
    assert defaults.DB_STATEMENT_TIMEOUT_MS == 30_000
    assert defaults.DB_IDLE_IN_TRANSACTION_TIMEOUT_MS == 300_000
    assert defaults.DB_TEMP_FILE_LIMIT_KB == 131_072
    assert defaults.DB_LOG_TEMP_FILES_KB == 65_536

    monkeypatch.setenv("DB_POOL_SIZE", "7")
    monkeypatch.setenv("DB_MAX_OVERFLOW", "3")
    monkeypatch.setenv("DB_POOL_TIMEOUT", "5")
    monkeypatch.setenv("DB_STATEMENT_TIMEOUT_MS", "12000")
    monkeypatch.setenv("DB_IDLE_IN_TRANSACTION_TIMEOUT_MS", "90000")
    monkeypatch.setenv("DB_TEMP_FILE_LIMIT_KB", "65536")
    monkeypatch.setenv("DB_LOG_TEMP_FILES_KB", "32768")
    overridden = Settings(_env_file=None)
    assert overridden.DB_POOL_SIZE == 7
    assert overridden.DB_MAX_OVERFLOW == 3
    assert overridden.DB_POOL_TIMEOUT == 5
    assert overridden.DB_STATEMENT_TIMEOUT_MS == 12_000
    assert overridden.DB_IDLE_IN_TRANSACTION_TIMEOUT_MS == 90_000
    assert overridden.DB_TEMP_FILE_LIMIT_KB == 65_536
    assert overridden.DB_LOG_TEMP_FILES_KB == 32_768


def test_engine_pool_kwargs_reads_settings() -> None:
    from app.config import get_settings
    from app.database import _engine_pool_kwargs

    settings = get_settings()
    assert _engine_pool_kwargs(settings) == {
        "pool_size": settings.DB_POOL_SIZE,
        "max_overflow": settings.DB_MAX_OVERFLOW,
        "pool_timeout": settings.DB_POOL_TIMEOUT,
        "pool_pre_ping": True,
    }


def test_asyncpg_connections_receive_database_resource_guards() -> None:
    from app.config import Settings
    from app.database import _engine_connect_args

    settings = Settings(
        _env_file=None,
        HIVE_PROCESS_ROLE="api",
        DB_STATEMENT_TIMEOUT_MS=12_000,
        DB_IDLE_IN_TRANSACTION_TIMEOUT_MS=90_000,
        DB_TEMP_FILE_LIMIT_KB=65_536,
        DB_LOG_TEMP_FILES_KB=32_768,
    )

    assert _engine_connect_args(settings) == {
        "server_settings": {
            "application_name": "hive-api",
            "statement_timeout": "12000",
            "idle_in_transaction_session_timeout": "90000",
        }
    }


def test_rls_role_bootstrap_owns_privileged_temp_file_guards(monkeypatch) -> None:
    from app.config import get_settings
    from app.scripts import grant_rls_app_role

    monkeypatch.setenv("DB_STATEMENT_TIMEOUT_MS", "12000")
    monkeypatch.setenv("DB_IDLE_IN_TRANSACTION_TIMEOUT_MS", "90000")
    monkeypatch.setenv("DB_TEMP_FILE_LIMIT_KB", "65536")
    monkeypatch.setenv("DB_LOG_TEMP_FILES_KB", "32768")
    get_settings.cache_clear()
    try:
        assert grant_rls_app_role._role_setting_values() == {
            "statement_timeout": "12000ms",
            "idle_in_transaction_session_timeout": "90000ms",
            "temp_file_limit": "65536kB",
            "log_temp_files": "32768kB",
        }
        assert [name for name, _format_sql in grant_rls_app_role._ROLE_SETTING_FORMATS] == [
            "statement_timeout",
            "idle_in_transaction_session_timeout",
            "temp_file_limit",
            "log_temp_files",
        ]
    finally:
        get_settings.cache_clear()


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
    assert snap["query_guards"] == {
        "statement_timeout_ms": 30_000,
        "idle_in_transaction_timeout_ms": 300_000,
        "temp_file_limit_kb": 131_072,
        "log_temp_files_kb": 65_536,
    }


def test_runtime_task_web_chat_recovery_budget_has_a_bounded_default() -> None:
    from app.config import Settings

    settings = Settings(_env_file=None)

    assert settings.RUNTIME_TASK_WEB_CHAT_MAX_EXECUTION_ATTEMPTS == 3
