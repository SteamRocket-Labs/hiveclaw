from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value

    def scalar(self):
        return self._value


class _ListResult:
    def __init__(self, values):
        self._values = values

    def scalars(self):
        return SimpleNamespace(all=lambda: self._values)

    def all(self):
        return self._values


class _FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.added = []
        self.committed = False
        self.flushed = False

    async def execute(self, _stmt):
        if not self._results:
            raise AssertionError("Unexpected execute() call")
        return self._results.pop(0)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushed = True

    async def commit(self):
        self.committed = True


@pytest.mark.asyncio
async def test_enterprise_info_update_scopes_to_selected_tenant(monkeypatch):
    import app.api.enterprise as enterprise_api

    own_tenant_id = uuid4()
    target_tenant_id = uuid4()
    captured: dict[str, object] = {}

    async def fake_update(db, tenant_id, info_type, content, visible_roles, updated_by):
        captured["update_tenant_id"] = tenant_id
        return SimpleNamespace(
            id=uuid4(),
            info_type=info_type,
            content=content,
            version=1,
            visible_roles=visible_roles,
            updated_at=datetime.now(timezone.utc),
        )

    async def fake_sync(db, tenant_id=None):
        captured["sync_tenant_id"] = tenant_id
        return 1

    monkeypatch.setattr(enterprise_api.enterprise_sync_service, "update_enterprise_info", fake_update)
    monkeypatch.setattr(enterprise_api.enterprise_sync_service, "sync_to_all_agents", fake_sync)

    await enterprise_api.update_enterprise_info(
        info_type="company_profile",
        data=enterprise_api.EnterpriseInfoUpdate(content={"name": "Target Co"}, visible_roles=[]),
        tenant_id=str(target_tenant_id),
        current_user=SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=own_tenant_id),
        db=_FakeDB([]),
    )

    assert captured["update_tenant_id"] == target_tenant_id
    assert captured["sync_tenant_id"] == target_tenant_id


@pytest.mark.asyncio
async def test_enterprise_info_list_scopes_to_selected_tenant():
    import app.api.enterprise as enterprise_api

    own_tenant_id = uuid4()
    target_tenant_id = uuid4()
    db = _FakeDB([
        _ListResult([
            SimpleNamespace(
                id=uuid4(),
                tenant_id=own_tenant_id,
                info_type="company_profile",
                content={"name": "Own"},
                version=1,
                visible_roles=[],
                updated_at=datetime.now(timezone.utc),
            ),
            SimpleNamespace(
                id=uuid4(),
                tenant_id=target_tenant_id,
                info_type="company_profile",
                content={"name": "Target"},
                version=2,
                visible_roles=[],
                updated_at=datetime.now(timezone.utc),
            ),
        ])
    ])

    result = await enterprise_api.list_enterprise_info(
        tenant_id=str(target_tenant_id),
        current_user=SimpleNamespace(role="platform_admin", tenant_id=own_tenant_id),
        db=db,
    )

    assert len(result) == 1
    assert result[0].content["name"] == "Target"


@pytest.mark.asyncio
async def test_feishu_org_sync_setting_scopes_to_selected_tenant():
    import app.api.enterprise as enterprise_api

    own_tenant_id = uuid4()
    target_tenant_id = uuid4()
    db = _FakeDB([
        _ScalarResult(SimpleNamespace(
            tenant_id=target_tenant_id,
            key="feishu_org_sync",
            value={"app_id": "tenant-b-app"},
            updated_at=datetime.now(timezone.utc),
        ))
    ])

    result = await enterprise_api.get_system_setting(
        key="feishu_org_sync",
        tenant_id=str(target_tenant_id),
        current_user=SimpleNamespace(role="platform_admin", tenant_id=own_tenant_id),
        db=db,
    )

    assert result["value"]["app_id"] == "tenant-b-app"


@pytest.mark.asyncio
async def test_org_sync_route_uses_selected_tenant(monkeypatch):
    import app.api.enterprise as enterprise_api

    own_tenant_id = uuid4()
    target_tenant_id = uuid4()
    captured: dict[str, object] = {}

    async def fake_full_sync(tenant_id):
        captured["tenant_id"] = tenant_id
        return {"departments": 3}

    async def fake_sync_org_structure(db, tenant_id):
        captured["workspace_sync_tenant_id"] = tenant_id

    monkeypatch.setattr("app.services.org_sync_service.org_sync_service.full_sync", fake_full_sync)
    monkeypatch.setattr("app.services.workspace_sync.sync_org_structure", fake_sync_org_structure)

    result = await enterprise_api.trigger_org_sync(
        tenant_id=str(target_tenant_id),
        current_user=SimpleNamespace(role="platform_admin", tenant_id=own_tenant_id),
    )

    assert result["departments"] == 3
    assert captured["tenant_id"] == target_tenant_id
    assert captured["workspace_sync_tenant_id"] == target_tenant_id


@pytest.mark.asyncio
async def test_enterprise_org_departments_default_to_current_tenant():
    import app.api.enterprise as enterprise_api

    current_tenant_id = uuid4()
    db = _FakeDB([
        _ListResult([
            SimpleNamespace(
                id=uuid4(),
                tenant_id=current_tenant_id,
                feishu_id="dept-a",
                name="Dept A",
                parent_id=None,
                path="Dept A",
                member_count=2,
            ),
            SimpleNamespace(
                id=uuid4(),
                tenant_id=uuid4(),
                feishu_id="dept-b",
                name="Dept B",
                parent_id=None,
                path="Dept B",
                member_count=1,
            ),
        ])
    ])

    result = await enterprise_api.list_org_departments(
        current_user=SimpleNamespace(role="org_admin", tenant_id=current_tenant_id),
        db=db,
    )

    assert len(result) == 1
    assert result[0]["name"] == "Dept A"


@pytest.mark.asyncio
async def test_enterprise_org_members_default_to_current_tenant():
    import app.api.enterprise as enterprise_api

    current_tenant_id = uuid4()
    db = _FakeDB([
        _ListResult([
            SimpleNamespace(
                id=uuid4(),
                tenant_id=current_tenant_id,
                name="Alice",
                email="alice@example.com",
                title="PM",
                department_path="Dept A",
                avatar_url=None,
            ),
            SimpleNamespace(
                id=uuid4(),
                tenant_id=uuid4(),
                name="Bob",
                email="bob@example.com",
                title="HR",
                department_path="Dept B",
                avatar_url=None,
            ),
        ])
    ])

    result = await enterprise_api.list_org_members(
        current_user=SimpleNamespace(role="org_admin", tenant_id=current_tenant_id),
        db=db,
    )

    assert len(result) == 1
    assert result[0]["name"] == "Alice"


@pytest.mark.asyncio
async def test_org_admin_cannot_create_llm_model_for_other_tenant(monkeypatch):
    import app.api.enterprise as enterprise_api

    own_tenant_id = uuid4()
    target_tenant_id = uuid4()

    monkeypatch.setattr(enterprise_api, "get_secrets_provider", lambda: SimpleNamespace(encrypt=lambda value: value))
    monkeypatch.setattr(enterprise_api.LLMModelOut, "model_validate", staticmethod(lambda model: model))

    with pytest.raises(HTTPException, match="Access denied"):
        await enterprise_api.add_llm_model(
            data=enterprise_api.LLMModelCreate(provider="openai", model="gpt-4.1", api_key="secret", label="Target"),
            tenant_id=str(target_tenant_id),
            current_user=SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=own_tenant_id),
            db=_FakeDB([]),
        )


@pytest.mark.asyncio
async def test_legacy_org_department_tree_supports_selected_tenant():
    import app.api.organization as organization_api

    own_tenant_id = uuid4()
    target_tenant_id = uuid4()
    db = _FakeDB([
        _ListResult([
            SimpleNamespace(
                id=uuid4(),
                tenant_id=own_tenant_id,
                name="Own Root",
                parent_id=None,
                manager_id=None,
                sort_order=0,
                created_at=datetime.now(timezone.utc),
            ),
            SimpleNamespace(
                id=uuid4(),
                tenant_id=target_tenant_id,
                name="Target Root",
                parent_id=None,
                manager_id=None,
                sort_order=0,
                created_at=datetime.now(timezone.utc),
            ),
        ]),
        _ScalarResult(0),
    ])

    result = await organization_api.get_department_tree(
        tenant_id=str(target_tenant_id),
        current_user=SimpleNamespace(role="platform_admin", tenant_id=own_tenant_id),
        db=db,
    )

    assert len(result) == 1
    assert result[0].name == "Target Root"
