from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ScalarsResult:
    def __init__(self, rows):
        self._rows = list(rows)

    def scalars(self):
        return self

    def all(self):
        return list(self._rows)


class _DB:
    def __init__(self, results):
        self._results = list(results)

    async def execute(self, _stmt):
        if not self._results:
            raise AssertionError("Unexpected execute() call")
        return self._results.pop(0)


class _AsyncSessionContext:
    def __init__(self, db):
        self._db = db

    async def __aenter__(self):
        return self._db

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _AsyncSessionFactory:
    def __init__(self, dbs):
        self._dbs = list(dbs)

    def __call__(self):
        if not self._dbs:
            raise AssertionError("Unexpected async_session() call")
        return _AsyncSessionContext(self._dbs.pop(0))


@pytest.mark.asyncio
async def test_feishu_user_search_prefers_provider_backed_org_member_ids(monkeypatch):
    from app.services.agent_tool_domains import feishu_users

    agent_id = uuid4()
    tenant_id = uuid4()
    org_member = SimpleNamespace(
        tenant_id=tenant_id,
        name="王天怡",
        en_name="Tianyi Wang",
        external_id="u_provider_123",
        feishu_user_id=None,
        open_id="ou_provider_only",
        feishu_open_id=None,
        email="ty@example.com",
        department_path="产品 / AI",
    )

    async def _fake_get_token(_agent_id):
        return {"app_id": "cli_agent_app"}

    monkeypatch.setattr(feishu_users, "_get_feishu_token", _fake_get_token)
    monkeypatch.setattr(feishu_users, "load_feishu_contacts_cache", lambda _agent_id: {"users": []})
    monkeypatch.setattr(
        "app.database.async_session",
        _AsyncSessionFactory(
            [
                _DB([_ScalarResult(tenant_id)]),
                _DB([_ScalarsResult([org_member])]),
            ]
        ),
    )

    result = await feishu_users._feishu_user_search(agent_id, {"name": "王天怡"})

    assert "user_id: `u_provider_123`" in result
    assert "open_id: `ou_provider_only`" in result
    assert "ty@example.com" in result


@pytest.mark.asyncio
async def test_feishu_user_search_uses_canonical_user_delivery_target(monkeypatch):
    from app.services.agent_tool_domains import feishu_users

    agent_id = uuid4()
    tenant_id = uuid4()
    user = SimpleNamespace(
        id=uuid4(),
        tenant_id=tenant_id,
        display_name="王天怡",
        feishu_user_id=None,
        feishu_open_id="ou_legacy_only",
        email="ty@example.com",
    )

    async def _fake_get_token(_agent_id):
        return {"app_id": "cli_agent_app"}

    async def _fake_get_delivery_target(*_args, **_kwargs):
        return ("u_provider_999", "user_id")

    monkeypatch.setattr(feishu_users, "_get_feishu_token", _fake_get_token)
    monkeypatch.setattr(feishu_users, "load_feishu_contacts_cache", lambda _agent_id: {"users": []})
    monkeypatch.setattr(
        "app.services.channel_user_service.channel_user_service.get_feishu_delivery_target",
        _fake_get_delivery_target,
    )
    monkeypatch.setattr(
        "app.database.async_session",
        _AsyncSessionFactory(
            [
                _DB([_ScalarResult(tenant_id)]),
                _DB([_ScalarsResult([])]),
                _DB([_ScalarsResult([user])]),
            ]
        ),
    )

    result = await feishu_users._feishu_user_search(agent_id, {"name": "王天怡"})

    assert "user_id: `u_provider_999`" in result
    assert "open_id: `ou_legacy_only`" not in result
    assert "ty@example.com" in result
