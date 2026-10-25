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


@pytest.fixture
def mocked_workspace_read_runtime(monkeypatch):
    """Isolate activity/fence units from the separately tested native source port."""
    from app.services import content_input_receipts

    tenant_id = str(uuid.uuid4())
    read_scope = object()

    async def authorize(request):
        assert request.context.tenant_id == tenant_id
        assert request.tool_name == "read_file"
        assert request.arguments["path"] == "workspace/notes.md"
        return read_scope

    async def settle(*, request, read, tool_call_id, result):
        assert request.context.tenant_id == tenant_id
        assert read is read_scope
        assert tool_call_id
        # This is a unit dependency double, never a native receipt or evidence
        # that a real file is authorized for model consumption.
        return None, {"status": "unit_test_double"}

    monkeypatch.setattr(content_input_receipts, "authorize_workspace_receipt_read", authorize)
    monkeypatch.setattr(content_input_receipts, "settle_workspace_receipt_read", settle)
    return tenant_id
