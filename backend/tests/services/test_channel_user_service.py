from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest
from unittest.mock import AsyncMock, Mock


class _RowsResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def all(self):
        return list(self._rows)


class _ExecuteDB:
    def __init__(self, results):
        self._results = list(results)

    async def execute(self, _stmt):
        if not self._results:
            raise AssertionError("Unexpected execute() call")
        return self._results.pop(0)


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


@pytest.mark.asyncio
async def test_resolve_feishu_user_prefers_provider_user_id(monkeypatch):
    from app.services.channel_user_service import ChannelUserService

    service = ChannelUserService()
    tenant_id = uuid4()
    expected_user = SimpleNamespace(id=uuid4(), email="a@test.com")
    calls: list[str] = []

    async def by_user_id(db, tenant_id, provider_user_id):
        calls.append(f"user_id:{provider_user_id}")
        return expected_user

    async def by_open_id(db, tenant_id, provider_open_id):
        calls.append(f"open_id:{provider_open_id}")
        return None

    async def by_email(db, tenant_id, email):
        calls.append(f"email:{email}")
        return None

    monkeypatch.setattr(service, "_find_by_provider_user_id", by_user_id)
    monkeypatch.setattr(service, "_find_by_open_id", by_open_id)
    monkeypatch.setattr(service, "_find_by_email", by_email)

    user = await service.resolve_feishu_user(
        db=object(),
        tenant_id=tenant_id,
        provider_user_id="ouser_123",
        provider_open_id="ou_open_456",
        email="fallback@test.com",
    )

    assert user is expected_user
    assert calls == ["user_id:ouser_123"]


@pytest.mark.asyncio
async def test_resolve_feishu_delivery_target_by_name_prefers_canonical_user_id_over_session_open_id(monkeypatch):
    from app.services.channel_user_service import ChannelUserService

    service = ChannelUserService()
    tenant_id = uuid4()
    session_user = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        feishu_user_id="u_staff_123",
        feishu_open_id="ou_app_scoped",
    )
    fake_db = _ExecuteDB(
        [
            _RowsResult(
                [
                    SimpleNamespace(
                        external_conv_id="feishu_p2p_ou_app_scoped",
                        user=session_user,
                    )
                ]
            )
        ]
    )
    get_target = AsyncMock(return_value=("u_staff_123", "user_id"))
    monkeypatch.setattr(service, "get_feishu_delivery_target", get_target)

    result = await service.resolve_feishu_delivery_target_by_name(
        fake_db,
        agent_id=uuid4(),
        tenant_id=tenant_id,
        member_name="王天怡",
    )

    assert result == ("u_staff_123", "user_id")
    get_target.assert_awaited_once_with(fake_db, user=session_user)


@pytest.mark.asyncio
async def test_resolve_feishu_delivery_target_by_name_falls_back_to_session_open_id_when_no_user_id(monkeypatch):
    from app.services.channel_user_service import ChannelUserService

    service = ChannelUserService()
    tenant_id = uuid4()
    session_user = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        feishu_user_id=None,
        feishu_open_id="ou_app_scoped",
    )
    fake_db = _ExecuteDB(
        [
            _RowsResult(
                [
                    SimpleNamespace(
                        external_conv_id="feishu_p2p_ou_app_scoped",
                        user=session_user,
                    )
                ]
            )
        ]
    )
    get_target = AsyncMock(return_value=("ou_app_scoped", "open_id"))
    monkeypatch.setattr(service, "get_feishu_delivery_target", get_target)

    result = await service.resolve_feishu_delivery_target_by_name(
        fake_db,
        agent_id=uuid4(),
        tenant_id=tenant_id,
        member_name="王天怡",
    )

    assert result == ("ou_app_scoped", "open_id")


@pytest.mark.asyncio
async def test_resolve_feishu_delivery_target_by_name_prefers_provider_backed_user_target_for_tenant_user(monkeypatch):
    from app.services.channel_user_service import ChannelUserService

    service = ChannelUserService()
    tenant_id = uuid4()
    tenant_user = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        display_name="王天怡",
        feishu_user_id=None,
        feishu_open_id="ou_app_scoped",
    )
    fake_db = _ExecuteDB(
        [
            _RowsResult([]),             # existing session lookup
            _ScalarResult(None),         # org member by relationship
            _ScalarResult(None),         # tenant org member
            _ScalarResult(tenant_user),  # tenant user
        ]
    )
    get_target = AsyncMock(return_value=("u_provider_123", "user_id"))
    monkeypatch.setattr(service, "get_feishu_delivery_target", get_target)

    result = await service.resolve_feishu_delivery_target_by_name(
        fake_db,
        agent_id=uuid4(),
        tenant_id=tenant_id,
        member_name="王天怡",
    )

    assert result == ("u_provider_123", "user_id")
    get_target.assert_awaited_once_with(fake_db, user=tenant_user)


@pytest.mark.asyncio
async def test_resolve_feishu_delivery_target_by_name_canonicalizes_relationship_member_open_id(monkeypatch):
    from app.services.channel_user_service import ChannelUserService

    service = ChannelUserService()
    tenant_id = uuid4()
    relationship_member = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        name="王天怡",
        external_id=None,
        feishu_user_id=None,
        open_id="ou_provider_only",
        feishu_open_id=None,
    )
    canonical_user = SimpleNamespace(id=uuid4(), tenant_id=tenant_id)
    fake_db = _ExecuteDB(
        [
            _RowsResult([]),                     # existing session lookup
            _ScalarResult(relationship_member),  # relationship org member
        ]
    )
    resolve_user = AsyncMock(return_value=canonical_user)
    get_target = AsyncMock(return_value=("u_provider_123", "user_id"))
    monkeypatch.setattr(service, "resolve_feishu_user", resolve_user)
    monkeypatch.setattr(service, "get_feishu_delivery_target", get_target)

    result = await service.resolve_feishu_delivery_target_by_name(
        fake_db,
        agent_id=uuid4(),
        tenant_id=tenant_id,
        member_name="王天怡",
    )

    assert result == ("u_provider_123", "user_id")
    resolve_user.assert_awaited_once_with(
        fake_db,
        tenant_id=tenant_id,
        provider_open_id="ou_provider_only",
    )
    get_target.assert_awaited_once_with(fake_db, user=canonical_user)


@pytest.mark.asyncio
async def test_resolve_feishu_delivery_target_by_name_canonicalizes_tenant_org_member_open_id(monkeypatch):
    from app.services.channel_user_service import ChannelUserService

    service = ChannelUserService()
    tenant_id = uuid4()
    org_member = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        name="王天怡",
        external_id=None,
        feishu_user_id=None,
        open_id="ou_provider_only",
        feishu_open_id=None,
    )
    canonical_user = SimpleNamespace(id=uuid4(), tenant_id=tenant_id)
    fake_db = _ExecuteDB(
        [
            _RowsResult([]),         # existing session lookup
            _ScalarResult(None),     # relationship org member
            _ScalarResult(org_member),  # tenant org member
        ]
    )
    resolve_user = AsyncMock(return_value=canonical_user)
    get_target = AsyncMock(return_value=("u_provider_456", "user_id"))
    monkeypatch.setattr(service, "resolve_feishu_user", resolve_user)
    monkeypatch.setattr(service, "get_feishu_delivery_target", get_target)

    result = await service.resolve_feishu_delivery_target_by_name(
        fake_db,
        agent_id=uuid4(),
        tenant_id=tenant_id,
        member_name="王天怡",
    )

    assert result == ("u_provider_456", "user_id")
    resolve_user.assert_awaited_once_with(
        fake_db,
        tenant_id=tenant_id,
        provider_open_id="ou_provider_only",
    )
    get_target.assert_awaited_once_with(fake_db, user=canonical_user)


@pytest.mark.asyncio
async def test_resolve_feishu_user_falls_back_open_id_then_email(monkeypatch):
    from app.services.channel_user_service import ChannelUserService

    service = ChannelUserService()
    tenant_id = uuid4()
    expected_user = SimpleNamespace(id=uuid4(), email="fallback@test.com")
    calls: list[str] = []

    async def by_user_id(db, tenant_id, provider_user_id):
        calls.append(f"user_id:{provider_user_id}")
        return None

    async def by_open_id(db, tenant_id, provider_open_id):
        calls.append(f"open_id:{provider_open_id}")
        return None

    async def by_email(db, tenant_id, email):
        calls.append(f"email:{email}")
        return expected_user

    monkeypatch.setattr(service, "_find_by_provider_user_id", by_user_id)
    monkeypatch.setattr(service, "_find_by_open_id", by_open_id)
    monkeypatch.setattr(service, "_find_by_email", by_email)

    user = await service.resolve_feishu_user(
        db=object(),
        tenant_id=tenant_id,
        provider_user_id="ouser_123",
        provider_open_id="ou_open_456",
        email="fallback@test.com",
    )

    assert user is expected_user
    assert calls == [
        "user_id:ouser_123",
        "open_id:ou_open_456",
        "email:fallback@test.com",
    ]


@pytest.mark.asyncio
async def test_resolve_or_create_feishu_user_upserts_external_identity_for_existing_user(monkeypatch):
    import app.services.channel_user_service as channel_user_module
    from app.services.channel_user_service import ChannelUserService

    service = ChannelUserService()
    tenant_id = uuid4()
    existing_user = SimpleNamespace(id=uuid4(), email="existing@test.com")
    provider = SimpleNamespace(id=uuid4())
    profile = {
        "user_id": "ouser_123",
        "open_id": "ou_open_456",
        "email": "existing@test.com",
        "name": "Existing User",
    }

    fake_db = SimpleNamespace(flush=AsyncMock())

    monkeypatch.setattr(service, "resolve_feishu_user", AsyncMock(return_value=existing_user))
    monkeypatch.setattr(channel_user_module.feishu_auth_provider, "_ensure_provider", AsyncMock(return_value=provider))
    upsert_mock = AsyncMock()
    monkeypatch.setattr(channel_user_module.feishu_auth_provider, "_upsert_external_identity", upsert_mock)
    write_through_mock = Mock()
    hydrate_mock = Mock()
    monkeypatch.setattr(channel_user_module.feishu_auth_provider, "_write_through_user_fields", write_through_mock)
    monkeypatch.setattr(channel_user_module.feishu_auth_provider, "_hydrate_user_profile", hydrate_mock)

    user = await service.resolve_or_create_feishu_user(
        fake_db,
        tenant_id=tenant_id,
        profile=profile,
    )

    assert user is existing_user
    upsert_mock.assert_awaited_once_with(fake_db, provider, existing_user, profile)
    write_through_mock.assert_called_once_with(existing_user, profile)
    hydrate_mock.assert_called_once_with(existing_user, profile)
    fake_db.flush.assert_awaited_once()


@pytest.mark.asyncio
async def test_resolve_or_create_feishu_user_creates_when_missing(monkeypatch):
    import app.services.channel_user_service as channel_user_module
    from app.services.channel_user_service import ChannelUserService

    service = ChannelUserService()
    tenant_id = uuid4()
    provider = SimpleNamespace(id=uuid4())
    created_user = SimpleNamespace(id=uuid4(), email="created@test.com")
    profile = {
        "user_id": "ouser_123",
        "open_id": "ou_open_456",
        "email": "created@test.com",
        "name": "Created User",
    }

    fake_db = SimpleNamespace(flush=AsyncMock())

    monkeypatch.setattr(service, "resolve_feishu_user", AsyncMock(return_value=None))
    monkeypatch.setattr(channel_user_module.feishu_auth_provider, "_ensure_provider", AsyncMock(return_value=provider))
    create_mock = AsyncMock(return_value=created_user)
    monkeypatch.setattr(channel_user_module.feishu_auth_provider, "_find_or_create_user", create_mock)

    user = await service.resolve_or_create_feishu_user(
        fake_db,
        tenant_id=tenant_id,
        profile=profile,
    )

    assert user is created_user
    create_mock.assert_awaited_once_with(fake_db, provider, profile, tenant_id)
