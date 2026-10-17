"""Real empty receipt lookup for tests whose executor/backend is a test double."""

import uuid

import pytest

from tests.integration.conftest import (  # noqa: F401
    app_user_engine,
    app_user_sessionmaker,
    migrated_pg_url,
    pg_container,
)


@pytest.fixture
def unreceipted_runtime_db(monkeypatch, app_user_sessionmaker):  # noqa: F811
    monkeypatch.setattr("app.database.async_session", app_user_sessionmaker)
    return str(uuid.uuid4())
