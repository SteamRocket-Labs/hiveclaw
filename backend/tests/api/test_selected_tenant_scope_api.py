from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest
from pydantic import ValidationError


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


class _FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.added = []
        self.deleted = []
        self.committed = False
        self.statements = []
        self.sync_session = SimpleNamespace(info={})

    async def execute(self, _stmt):
        self.statements.append(_stmt)
        sql = getattr(_stmt, "text", None) or str(_stmt)
        if sql.lstrip().upper().startswith("SET LOCAL"):
            return _ScalarResult(None)
        return self._results.pop(0)

    def add(self, value):
        self.added.append(value)

    async def delete(self, value):
        self.deleted.append(value)

    async def commit(self):
        self.committed = True

    async def rollback(self):
        return None

    async def refresh(self, _value):
        return None

    async def flush(self):
        for value in self.added:
            if getattr(value, "id", None) is None:
                value.id = uuid4()


def _first_business_statement(db: _FakeDB):
    for stmt in db.statements:
        sql = getattr(stmt, "text", None) or str(stmt)
        if not sql.lstrip().upper().startswith("SET LOCAL"):
            return stmt
    raise AssertionError("No business statement executed")


@pytest.mark.asyncio
async def test_platform_admin_list_users_pins_selected_tenant():
    import app.api.users as users_api

    own_tenant_id = uuid4()
    target_tenant_id = uuid4()
    target_user = SimpleNamespace(
        id=uuid4(),
        tenant_id=target_tenant_id,
        username="target-user",
        email="target@example.com",
        display_name="Target User",
        role="member",
        is_active=True,
        quota_tokens_per_day=None,
        quota_tokens_per_month=None,
        tokens_used_today=0,
        tokens_used_month=0,
        tokens_used_total=0,
        feishu_open_id=None,
        created_at=datetime.now(timezone.utc),
    )
    db = _FakeDB([_ListResult([target_user]), _ScalarResult(3)])

    result = await users_api.list_users(
        tenant_id=str(target_tenant_id),
        current_user=SimpleNamespace(role="platform_admin", tenant_id=own_tenant_id),
        db=db,
    )

    assert result[0].id == target_user.id
    assert result[0].agents_count == 3
    assert any(f"SET LOCAL app.current_tenant_id = '{target_tenant_id}'" in str(stmt) for stmt in db.statements)


@pytest.mark.asyncio
async def test_platform_admin_can_update_selected_tenant_quotas():
    import app.api.enterprise as enterprise_api

    own_tenant_id = uuid4()
    target_tenant_id = uuid4()
    target_tenant = SimpleNamespace(
        id=target_tenant_id,
        default_tokens_per_day=None,
        default_tokens_per_month=None,
        min_heartbeat_interval_minutes=120,
        default_max_triggers=20,
        min_poll_interval_floor=5,
        max_webhook_rate_ceiling=5,
    )
    db = _FakeDB([_ScalarResult(target_tenant)])

    result = await enterprise_api.update_tenant_quotas(
        data=enterprise_api.TenantQuotaUpdate(default_tokens_per_day=100000),
        tenant_id=str(target_tenant_id),
        current_user=SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=own_tenant_id),
        db=db,
    )

    assert result["message"] == "Tenant quotas updated"
    assert target_tenant.default_tokens_per_day == 100000
    assert db.committed is True


@pytest.mark.asyncio
async def test_platform_admin_can_update_other_tenant_user_quota():
    from app import database
    import app.api.users as users_api

    own_tenant_id = uuid4()
    target_tenant_id = uuid4()
    user = SimpleNamespace(
        id=uuid4(),
        tenant_id=target_tenant_id,
        username="alice",
        email="alice@example.com",
        display_name="Alice",
        role="member",
        is_active=True,
        quota_tokens_per_day=None,
        quota_tokens_per_month=None,
        tokens_used_today=0,
        tokens_used_month=0,
        tokens_used_total=0,
    )
    db = _FakeDB([_ScalarResult(user), _ScalarResult(1)])

    result = await users_api.update_user_quota(
        user_id=user.id,
        data=users_api.UserQuotaUpdate(quota_tokens_per_day=50000),
        current_user=SimpleNamespace(role="platform_admin", tenant_id=own_tenant_id),
        db=db,
    )

    assert result.quota_tokens_per_day == 50000
    assert user.quota_tokens_per_day == 50000
    assert db.sync_session.info[database._RLS_TENANT_INFO_KEY] == str(target_tenant_id)
    assert any(f"SET LOCAL app.current_tenant_id = '{target_tenant_id}'" in str(stmt) for stmt in db.statements)
    assert db.committed is True


@pytest.mark.asyncio
async def test_get_selected_tenant_memory_config_defaults_models_to_default_model():
    import app.api.memory as memory_api

    own_tenant_id = uuid4()
    target_tenant_id = uuid4()
    default_model_id = uuid4()
    db = _FakeDB([
        _ScalarResult(None),
        _ScalarResult({"model_id": str(default_model_id)}),
        _ScalarResult(default_model_id),
    ])

    result = await memory_api.get_memory_config(
        tenant_id=str(target_tenant_id),
        current_user=SimpleNamespace(role="platform_admin", tenant_id=own_tenant_id),
        db=db,
    )

    assert result["summary_model_id"] == str(default_model_id)
    assert result["rerank_model_id"] == str(default_model_id)
    assert result["compress_threshold"] == 70
    assert result["keep_recent"] == 10


@pytest.mark.asyncio
async def test_platform_admin_can_update_selected_tenant_memory_config():
    import app.api.memory as memory_api

    own_tenant_id = uuid4()
    target_tenant_id = uuid4()
    # Two DB calls: 1) lookup existing TenantSetting, 2) validate summary_model_id
    db = _FakeDB([_ScalarResult(None), _ScalarResult(uuid4())])

    result = await memory_api.update_memory_config(
        data=memory_api.MemoryConfigUpdate(summary_model_id="model-1", keep_recent=20),
        tenant_id=str(target_tenant_id),
        current_user=SimpleNamespace(role="platform_admin", tenant_id=own_tenant_id),
        db=db,
    )

    assert result["summary_model_id"] == "model-1"
    assert result["keep_recent"] == 20
    assert db.added[0].tenant_id == target_tenant_id
    assert db.committed is True


@pytest.mark.asyncio
async def test_platform_admin_can_update_selected_tenant_memory_config_with_rerank_model():
    import app.api.memory as memory_api

    own_tenant_id = uuid4()
    target_tenant_id = uuid4()
    # Three DB calls: 1) lookup existing TenantSetting, 2) validate summary_model_id, 3) validate rerank_model_id
    db = _FakeDB([_ScalarResult(None), _ScalarResult(uuid4()), _ScalarResult(uuid4())])

    result = await memory_api.update_memory_config(
        data=memory_api.MemoryConfigUpdate(
            summary_model_id="summary-model-1",
            rerank_model_id="rerank-model-1",
            keep_recent=20,
        ),
        tenant_id=str(target_tenant_id),
        current_user=SimpleNamespace(role="platform_admin", tenant_id=own_tenant_id),
        db=db,
    )

    assert result["summary_model_id"] == "summary-model-1"
    assert result["rerank_model_id"] == "rerank-model-1"
    assert result["keep_recent"] == 20
    assert db.added[0].tenant_id == target_tenant_id
    assert db.committed is True


@pytest.mark.asyncio
async def test_platform_admin_can_get_selected_tenant_oidc_config():
    import app.api.enterprise as enterprise_api

    own_tenant_id = uuid4()
    target_tenant_id = uuid4()
    setting = SimpleNamespace(value={
        "issuer_url": "https://issuer.example.com",
        "client_id": "client-id",
        "client_secret": "secret",
        "scopes": "openid profile email",
        "auto_provision": True,
        "display_name": "SSO",
    })
    db = _FakeDB([_ScalarResult(setting)])

    result = await enterprise_api.get_oidc_config(
        tenant_id=str(target_tenant_id),
        current_user=SimpleNamespace(role="platform_admin", tenant_id=own_tenant_id),
        db=db,
    )

    assert result["configured"] is True
    assert result["client_id"] == "client-id"


@pytest.mark.asyncio
async def test_platform_admin_can_create_invitation_codes_for_selected_tenant():
    import app.api.enterprise as enterprise_api

    own_tenant_id = uuid4()
    target_tenant_id = uuid4()
    db = _FakeDB([])

    result = await enterprise_api.create_invitation_codes(
        data=enterprise_api.InvitationCodeCreate(count=2, max_uses=3),
        tenant_id=str(target_tenant_id),
        current_user=SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=own_tenant_id),
        db=db,
    )

    assert result["created"] == 2
    assert len(db.added) == 2
    assert {code.tenant_id for code in db.added} == {target_tenant_id}
    assert any(f"SET LOCAL app.current_tenant_id = '{target_tenant_id}'" in str(stmt) for stmt in db.statements)
    assert db.committed is True


@pytest.mark.asyncio
async def test_platform_admin_create_agent_pins_selected_tenant(monkeypatch):
    import app.api.agents as agents_api
    from app.schemas.schemas import AgentCreate

    own_tenant_id = uuid4()
    target_tenant_id = uuid4()
    target_tenant = SimpleNamespace(
        id=target_tenant_id,
        default_max_triggers=20,
        min_poll_interval_floor=5,
        max_webhook_rate_ceiling=5,
    )
    db = _FakeDB([_ScalarResult(target_tenant), _ListResult([])])

    async def noop_async(*_args, **_kwargs):
        return None

    class _FakeAgentManager:
        def _agent_dir(self, _agent_id):
            from pathlib import Path

            return Path("/tmp")

        async def initialize_agent_files(self, *_args, **_kwargs):
            return None

        async def start_container(self, *_args, **_kwargs):
            return None

    monkeypatch.setattr(agents_api, "ensure_agent_identity", noop_async)
    monkeypatch.setattr(agents_api, "_agent_out", lambda agent: SimpleNamespace(name=agent.name))
    monkeypatch.setattr("app.services.tool_seeder.assign_default_tools_to_agent", noop_async)
    monkeypatch.setattr("app.services.agent_manager.agent_manager", _FakeAgentManager())
    monkeypatch.setattr("app.core.policy.write_audit_event", noop_async)

    result = await agents_api.create_agent(
        data=AgentCreate(name="投研助手", tenant_id=target_tenant_id),
        current_user=SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=own_tenant_id),
        db=db,
    )

    assert result.name == "投研助手"
    assert db.added[0].tenant_id == target_tenant_id
    assert any(f"SET LOCAL app.current_tenant_id = '{target_tenant_id}'" in str(stmt) for stmt in db.statements)


@pytest.mark.asyncio
async def test_platform_admin_can_test_selected_tenant_llm_model(monkeypatch):
    import app.api.enterprise as enterprise_api

    own_tenant_id = uuid4()
    target_tenant_id = uuid4()
    target_model_id = uuid4()
    db = _FakeDB([_ScalarResult(SimpleNamespace(api_key="target-secret"))])

    class _FakeClient:
        async def complete(self, messages, max_tokens, **kwargs):
            assert messages[0].content == "Say 'ok' and nothing else."
            assert max_tokens == 16
            assert "temperature" in kwargs
            return SimpleNamespace(content="ok")

    def fake_create_llm_client(provider, model, api_key, base_url):
        assert provider == "openai"
        assert model == "gpt-4o-mini"
        assert api_key == "target-secret"
        assert base_url is None
        return _FakeClient()

    async def fake_write_audit_event(*args, **kwargs):
        return None

    monkeypatch.setattr("app.services.llm_client.create_llm_client", fake_create_llm_client)
    monkeypatch.setattr("app.core.policy.write_audit_event", fake_write_audit_event)

    result = await enterprise_api.test_llm_model(
        data=enterprise_api.LLMTestRequest(
            provider="openai",
            model="gpt-4o-mini",
            model_id=str(target_model_id),
        ),
        tenant_id=str(target_tenant_id),
        current_user=SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=own_tenant_id),
        db=db,
    )

    params = _first_business_statement(db).compile().params
    assert target_tenant_id in params.values()
    assert own_tenant_id not in params.values()
    assert result["success"] is True
    assert result["reply"] == "ok"


@pytest.mark.asyncio
async def test_llm_test_applies_gpt55_responses_request_options(monkeypatch):
    import app.api.enterprise as enterprise_api

    captured = {}

    class _FakeClient:
        async def complete(self, messages, max_tokens, **kwargs):
            captured["messages"] = messages
            captured["max_tokens"] = max_tokens
            captured["kwargs"] = kwargs
            return SimpleNamespace(content="ok")

    def fake_create_llm_client(provider, model, api_key, base_url):
        assert provider == "openai"
        assert model == "gpt-5.5"
        assert api_key == "sk-test"
        assert base_url is None
        return _FakeClient()

    monkeypatch.setattr("app.services.llm_client.create_llm_client", fake_create_llm_client)

    result = await enterprise_api.test_llm_model(
        data=enterprise_api.LLMTestRequest(
            provider="openai",
            model="gpt-5.5",
            api_key="sk-test",
            temperature=0.7,
            reasoning_mode="enabled",
            reasoning_effort="high",
            text_verbosity="low",
        ),
        current_user=SimpleNamespace(id=uuid4(), role="admin", tenant_id=uuid4()),
        db=_FakeDB([]),
    )

    assert result["success"] is True
    assert captured["max_tokens"] == 1024
    assert captured["kwargs"] == {
        "temperature": 0.7,
        "reasoning": {"effort": "high"},
        "text": {"verbosity": "low"},
        "_omit_temperature": True,
    }


@pytest.mark.asyncio
async def test_llm_test_rejects_openai_gpt55_pro_alias_before_provider_call(monkeypatch):
    import app.api.enterprise as enterprise_api

    def fail_create_llm_client(*_args, **_kwargs):
        raise AssertionError("invalid OpenAI model aliases must be rejected before provider calls")

    monkeypatch.setattr("app.services.llm_client.create_llm_client", fail_create_llm_client)

    result = await enterprise_api.test_llm_model(
        data=enterprise_api.LLMTestRequest(
            provider="openai",
            model="gpt-5.5-pro",
            api_key="sk-test",
        ),
        current_user=SimpleNamespace(id=uuid4(), role="admin", tenant_id=uuid4()),
        db=_FakeDB([]),
    )

    assert result["success"] is False
    assert "gpt-5.5-pro" in result["error"]
    assert "gpt-5.5" in result["error"]


def test_llm_model_create_rejects_openai_gpt55_pro_alias():
    import app.api.enterprise as enterprise_api

    with pytest.raises(ValidationError, match="gpt-5.5-pro"):
        enterprise_api.LLMModelCreate(
            provider="openai",
            model="gpt-5.5-pro",
            api_key="secret",
            label="GPT-5.5 Pro",
        )


@pytest.mark.asyncio
async def test_platform_admin_can_update_selected_tenant_llm_model(monkeypatch):
    import app.api.enterprise as enterprise_api

    own_tenant_id = uuid4()
    target_tenant_id = uuid4()
    model_id = uuid4()
    model = SimpleNamespace(
        id=model_id,
        provider="openai",
        model="gpt-4o-mini",
        base_url=None,
        label="Target model",
        api_key_encrypted="encrypted-key",
        max_tokens_per_day=1000,
        enabled=True,
        supports_vision=False,
        max_output_tokens=2048,
        max_input_tokens=8192,
        created_at=datetime.now(timezone.utc),
    )
    db = _FakeDB([_ScalarResult(model)])

    async def fake_write_audit_event(*args, **kwargs):
        return None

    monkeypatch.setattr("app.core.policy.write_audit_event", fake_write_audit_event)

    result = await enterprise_api.update_llm_model(
        model_id=model_id,
        tenant_id=str(target_tenant_id),
        data=enterprise_api.LLMModelUpdate(label="Updated target model"),
        current_user=SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=own_tenant_id),
        db=db,
    )

    params = _first_business_statement(db).compile().params
    assert target_tenant_id in params.values()
    assert own_tenant_id not in params.values()
    assert model.label == "Updated target model"
    assert result.label == "Updated target model"
    assert db.committed is True


def test_llm_model_create_accepts_high_max_output_tokens_below_absolute():
    # Schema bound raised to the absolute ceiling (524288) so high-output
    # providers (e.g. DeepSeek 384K) can be configured; runtime clamps per provider.
    import app.api.enterprise as enterprise_api

    created = enterprise_api.LLMModelCreate(
        provider="deepseek",
        model="deepseek-v4-pro",
        api_key="secret",
        label="DeepSeek",
        max_output_tokens=384000,
    )
    assert created.max_output_tokens == 384000


def test_llm_model_create_rejects_above_absolute_max_output_tokens():
    import app.api.enterprise as enterprise_api
    from app.services.llm_client import ABSOLUTE_MAX_OUTPUT_TOKENS

    with pytest.raises(ValidationError):
        enterprise_api.LLMModelCreate(
            provider="custom",
            model="qwen3.6-plus",
            api_key="secret",
            label="Qwen",
            max_output_tokens=ABSOLUTE_MAX_OUTPUT_TOKENS + 1,
        )


def test_llm_model_update_rejects_above_absolute_max_output_tokens():
    import app.api.enterprise as enterprise_api
    from app.services.llm_client import ABSOLUTE_MAX_OUTPUT_TOKENS

    with pytest.raises(ValidationError):
        enterprise_api.LLMModelUpdate(max_output_tokens=ABSOLUTE_MAX_OUTPUT_TOKENS + 1)


class _RowsResult:
    def __init__(self, values):
        self._values = values

    def all(self):
        return self._values


@pytest.mark.asyncio
async def test_platform_admin_can_delete_selected_tenant_llm_model(monkeypatch):
    import app.api.enterprise as enterprise_api

    own_tenant_id = uuid4()
    target_tenant_id = uuid4()
    model_id = uuid4()
    model = SimpleNamespace(
        id=model_id,
        provider="openai",
        model="gpt-4o-mini",
    )
    db = _FakeDB([_ScalarResult(model), _RowsResult([])])

    async def fake_write_audit_event(*args, **kwargs):
        return None

    monkeypatch.setattr("app.core.policy.write_audit_event", fake_write_audit_event)

    await enterprise_api.remove_llm_model(
        model_id=model_id,
        tenant_id=str(target_tenant_id),
        current_user=SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=own_tenant_id),
        db=db,
    )

    params = _first_business_statement(db).compile().params
    assert target_tenant_id in params.values()
    assert own_tenant_id not in params.values()
    assert db.deleted == [model]
    assert db.committed is True
