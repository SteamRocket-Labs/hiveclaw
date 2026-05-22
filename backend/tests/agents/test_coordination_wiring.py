"""Tests for the CoordinationGateway production wiring helper."""

from __future__ import annotations

import uuid
from unittest.mock import patch

import pytest

from app.agents.coordination_gateway import (
    CoordinationGateway,
    InProcessCoordinationGateway,
)
from app.agents.coordination_repository import CoordinationRepository
from app.agents.coordination_wiring import gateway_from_session, pick_gateway


class _FakeSession:
    """Inert placeholder; pick_gateway only inspects identity, never calls methods."""


def _settings_with_backend(backend: str):
    class _Settings:
        COORDINATION_BACKEND = backend

    return _Settings()


class TestPickGateway:
    def test_default_returns_in_process_gateway(self) -> None:
        with patch("app.agents.coordination_wiring.get_settings", return_value=_settings_with_backend("memory")):
            gw = pick_gateway()
        assert isinstance(gw, InProcessCoordinationGateway)

    def test_postgres_with_session_returns_repository(self) -> None:
        session = _FakeSession()
        tenant_id = uuid.uuid4()
        with patch("app.agents.coordination_wiring.get_settings", return_value=_settings_with_backend("postgres")):
            gw = pick_gateway(session=session, tenant_id=tenant_id)
        assert isinstance(gw, CoordinationRepository)

    def test_postgres_without_session_falls_back_and_warns(self, caplog) -> None:
        with patch("app.agents.coordination_wiring.get_settings", return_value=_settings_with_backend("postgres")):
            with caplog.at_level("WARNING"):
                gw = pick_gateway()
        assert isinstance(gw, InProcessCoordinationGateway)
        assert any("falling back" in record.message for record in caplog.records)

    def test_postgres_repository_satisfies_protocol(self) -> None:
        session = _FakeSession()
        tenant_id = uuid.uuid4()
        with patch("app.agents.coordination_wiring.get_settings", return_value=_settings_with_backend("postgres")):
            gw = pick_gateway(session=session, tenant_id=tenant_id)
        assert isinstance(gw, CoordinationGateway)


class TestAsyncFactory:
    @pytest.mark.asyncio
    async def test_gateway_from_session_returns_repository_when_postgres(self) -> None:
        session = _FakeSession()
        tenant_id = uuid.uuid4()
        with patch("app.agents.coordination_wiring.get_settings", return_value=_settings_with_backend("postgres")):
            gw = await gateway_from_session(session, tenant_id)
        assert isinstance(gw, CoordinationRepository)
