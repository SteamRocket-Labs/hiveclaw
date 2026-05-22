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
from app.agents.coordination_wiring import gateway_from_session, gateway_scope, pick_gateway


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


class TestGatewayScope:
    @pytest.mark.asyncio
    async def test_explicit_gateway_short_circuits(self) -> None:
        explicit = InProcessCoordinationGateway()
        async with gateway_scope(explicit) as gw:
            assert gw is explicit

    @pytest.mark.asyncio
    async def test_memory_backend_yields_in_process_gateway(self) -> None:
        with patch("app.agents.coordination_wiring.get_settings", return_value=_settings_with_backend("memory")):
            async with gateway_scope(tenant_id=uuid.uuid4()) as gw:
                assert isinstance(gw, InProcessCoordinationGateway)

    @pytest.mark.asyncio
    async def test_postgres_without_tenant_falls_back(self) -> None:
        with patch("app.agents.coordination_wiring.get_settings", return_value=_settings_with_backend("postgres")):
            async with gateway_scope() as gw:
                assert isinstance(gw, InProcessCoordinationGateway)

    @pytest.mark.asyncio
    async def test_postgres_with_invalid_tenant_string_falls_back(self) -> None:
        with patch("app.agents.coordination_wiring.get_settings", return_value=_settings_with_backend("postgres")):
            async with gateway_scope(tenant_id="not-a-uuid") as gw:
                assert isinstance(gw, InProcessCoordinationGateway)

    @pytest.mark.asyncio
    async def test_postgres_with_tenant_opens_session_and_yields_repository(self) -> None:
        commits: list[bool] = []

        class _FakeSessionCM:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def commit(self):
                commits.append(True)

            async def rollback(self):  # pragma: no cover
                commits.append(False)

        tenant_id = uuid.uuid4()
        with patch("app.agents.coordination_wiring.get_settings", return_value=_settings_with_backend("postgres")):
            with patch("app.agents.coordination_wiring.async_session", return_value=_FakeSessionCM()):
                async with gateway_scope(tenant_id=tenant_id) as gw:
                    assert isinstance(gw, CoordinationRepository)
        assert commits == [True]

    @pytest.mark.asyncio
    async def test_postgres_with_string_tenant_uuid_opens_session(self) -> None:
        tenant_id = uuid.uuid4()

        class _FakeSessionCM:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                return False

            async def commit(self):
                pass

            async def rollback(self):  # pragma: no cover
                pass

        with patch("app.agents.coordination_wiring.get_settings", return_value=_settings_with_backend("postgres")):
            with patch("app.agents.coordination_wiring.async_session", return_value=_FakeSessionCM()):
                async with gateway_scope(tenant_id=str(tenant_id)) as gw:
                    assert isinstance(gw, CoordinationRepository)
