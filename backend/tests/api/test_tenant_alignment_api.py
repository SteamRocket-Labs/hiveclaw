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
        if "SET LOCAL app.current_tenant_id" in str(_stmt):
            return _ScalarResult(None)
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
async def test_enterprise_info_update_scopes_to_current_org_tenant(monkeypatch):
    import app.api.enterprise as enterprise_api

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
        tenant_id=None,
        current_user=SimpleNamespace(id=uuid4(), role="org_admin", tenant_id=target_tenant_id),
        db=_FakeDB([]),
    )

    assert captured["update_tenant_id"] == target_tenant_id
    assert captured["sync_tenant_id"] == target_tenant_id


@pytest.mark.asyncio
async def test_enterprise_info_list_scopes_to_current_org_tenant():
    import app.api.enterprise as enterprise_api

    target_tenant_id = uuid4()
    other_tenant_id = uuid4()
    db = _FakeDB(
        [
            _ListResult(
                [
                    SimpleNamespace(
                        id=uuid4(),
                        tenant_id=other_tenant_id,
                        info_type="company_profile",
                        content={"name": "Other"},
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
                ]
            )
        ]
    )

    result = await enterprise_api.list_enterprise_info(
        tenant_id=None,
        current_user=SimpleNamespace(role="org_admin", tenant_id=target_tenant_id),
        db=db,
    )

    assert len(result) == 1
    assert result[0].content["name"] == "Target"


@pytest.mark.asyncio
async def test_platform_admin_cannot_read_enterprise_business_body_by_default():
    import app.api.enterprise as enterprise_api

    with pytest.raises(HTTPException) as exc_info:
        await enterprise_api.list_enterprise_info(
            tenant_id=str(uuid4()),
            current_user=SimpleNamespace(role="platform_admin", tenant_id=uuid4()),
            db=_FakeDB([]),
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_platform_admin_cannot_update_enterprise_business_body_by_default():
    import app.api.enterprise as enterprise_api

    with pytest.raises(HTTPException) as exc_info:
        await enterprise_api.update_enterprise_info(
            info_type="company_profile",
            data=enterprise_api.EnterpriseInfoUpdate(content={"name": "Hidden"}, visible_roles=[]),
            tenant_id=str(uuid4()),
            current_user=SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=uuid4()),
            db=_FakeDB([]),
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_platform_admin_cannot_read_selected_tenant_feishu_org_sync_setting():
    import app.api.enterprise as enterprise_api

    own_tenant_id = uuid4()
    target_tenant_id = uuid4()
    db = _FakeDB([])

    with pytest.raises(HTTPException) as exc_info:
        await enterprise_api.get_system_setting(
            key="feishu_org_sync",
            tenant_id=str(target_tenant_id),
            current_user=SimpleNamespace(role="platform_admin", tenant_id=own_tenant_id),
            db=db,
        )

    assert exc_info.value.status_code == 403
    assert db._results == []


@pytest.mark.asyncio
async def test_agent_permission_default_read_is_tenant_scoped_and_side_effect_free():
    import app.api.enterprise as enterprise_api

    tenant_id = uuid4()
    setting = SimpleNamespace(
        tenant_id=tenant_id,
        key="agent_permission_default",
        value={"mode": "auto"},
        updated_at=datetime.now(timezone.utc),
    )
    db = _FakeDB([_ScalarResult(setting)])

    result = await enterprise_api.get_system_setting(
        key="agent_permission_default",
        tenant_id=None,
        current_user=SimpleNamespace(role="org_admin", tenant_id=tenant_id),
        db=db,
    )

    assert result["value"] == {"mode": "auto"}
    assert db.committed is False


@pytest.mark.asyncio
async def test_agent_permission_default_rejects_break_glass_as_tenant_default():
    import app.api.enterprise as enterprise_api

    tenant_id = uuid4()
    db = _FakeDB([])

    with pytest.raises(HTTPException) as exc_info:
        await enterprise_api.update_system_setting(
            key="agent_permission_default",
            data=enterprise_api.SettingUpdate(value={"mode": "bypassPermissions"}),
            tenant_id=None,
            current_user=SimpleNamespace(role="org_admin", tenant_id=tenant_id),
            db=db,
        )

    assert exc_info.value.status_code == 422
    assert db.committed is False


@pytest.mark.asyncio
async def test_platform_admin_cannot_read_company_intro_setting_by_default():
    import app.api.enterprise as enterprise_api

    with pytest.raises(HTTPException) as exc_info:
        await enterprise_api.get_system_setting(
            key=f"company_intro_{uuid4()}",
            tenant_id=str(uuid4()),
            current_user=SimpleNamespace(role="platform_admin", tenant_id=uuid4()),
            db=_FakeDB([]),
        )

    assert exc_info.value.status_code == 403


@pytest.mark.asyncio
async def test_platform_admin_cannot_update_company_intro_setting_by_default():
    import app.api.enterprise as enterprise_api

    db = _FakeDB([])
    with pytest.raises(HTTPException) as exc_info:
        await enterprise_api.update_system_setting(
            key="company_intro",
            data=enterprise_api.SettingUpdate(value={"content": "Hidden"}),
            tenant_id=str(uuid4()),
            current_user=SimpleNamespace(role="platform_admin", tenant_id=uuid4()),
            db=db,
        )

    assert exc_info.value.status_code == 403
    assert db.committed is False


def test_platform_admin_business_body_routes_fail_closed_before_db():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import app.api.enterprise as enterprise_api

    db = _FakeDB([])
    user = SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=uuid4())

    async def override_user():
        return user

    async def override_db():
        yield db

    app = FastAPI()
    app.include_router(enterprise_api.router)
    app.dependency_overrides[enterprise_api.get_current_user] = override_user
    app.dependency_overrides[enterprise_api.get_current_admin] = override_user
    app.dependency_overrides[enterprise_api.get_db] = override_db

    with TestClient(app) as client:
        info_response = client.get("/enterprise/info")
        intro_response = client.get(f"/enterprise/system-settings/company_intro_{uuid4()}")

    assert info_response.status_code == 403
    assert intro_response.status_code == 403
    assert db._results == []


@pytest.mark.asyncio
async def test_unknown_system_setting_key_is_denied_before_selected_tenant_lookup():
    import app.api.enterprise as enterprise_api

    own_tenant_id = uuid4()
    target_tenant_id = uuid4()
    db = _FakeDB([])

    with pytest.raises(HTTPException) as exc_info:
        await enterprise_api.get_system_setting(
            key="behavior_eval_runtime",
            tenant_id=str(target_tenant_id),
            current_user=SimpleNamespace(role="platform_admin", tenant_id=own_tenant_id),
            db=db,
        )

    assert exc_info.value.status_code == 403
    assert db._results == []


@pytest.mark.asyncio
async def test_platform_admin_cannot_trigger_selected_tenant_org_sync():
    import app.api.enterprise as enterprise_api

    own_tenant_id = uuid4()
    target_tenant_id = uuid4()
    db = _FakeDB([])

    with pytest.raises(HTTPException) as exc_info:
        await enterprise_api.trigger_org_sync(
            tenant_id=str(target_tenant_id),
            current_user=SimpleNamespace(role="platform_admin", tenant_id=own_tenant_id),
            db=db,
        )

    assert exc_info.value.status_code == 403
    assert db._results == []


@pytest.mark.asyncio
async def test_enterprise_org_departments_default_to_current_tenant():
    import app.api.enterprise as enterprise_api

    current_tenant_id = uuid4()
    db = _FakeDB(
        [
            _ListResult(
                [
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
                ]
            )
        ]
    )

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
    db = _FakeDB(
        [
            _ListResult(
                [
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
                ]
            )
        ]
    )

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
    db = _FakeDB(
        [
            _ListResult(
                [
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
                ]
            ),
            _ScalarResult(0),
        ]
    )

    result = await organization_api.get_department_tree(
        tenant_id=str(target_tenant_id),
        current_user=SimpleNamespace(role="platform_admin", tenant_id=own_tenant_id),
        db=db,
    )

    assert len(result) == 1
    assert result[0].name == "Target Root"
