"""Real-PG fixtures for deep-research tests that need them (§9 P14) —
re-exported from tests/integration. Opt-in per test (no autouse): the
existing pure/monkeypatch deep-research tests are unaffected."""

from __future__ import annotations

from tests.integration.conftest import (  # noqa: F401  (re-exported fixtures)
    APP_USER,
    APP_USER_PASSWORD,
    _async_url,
    app_user_engine,
    app_user_sessionmaker,
    migrated_pg_url,
    owner_engine,
    owner_sessionmaker,
    pg_container,
)
