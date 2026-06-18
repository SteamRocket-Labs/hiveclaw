from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, results):
        self._results = list(results)
        self.added = []
        self.committed = False
        self.flushed = False
        self.statements = []

    async def execute(self, statement):
        self.statements.append(statement)
        if "SET LOCAL app.current_tenant_id" in str(statement):
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


class _Bypass:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *_exc_info):
        return False


@pytest.mark.asyncio
async def test_eval_ci_runtime_model_endpoint_upserts_model_and_binds_agent(monkeypatch):
    import app.services.eval_ci_service as eval_ci_service
    from app.models.llm import LLMModel
    from app.models.tenant_setting import TenantSetting

    tenant_id = uuid4()
    agent_id = uuid4()
    user_id = uuid4()
    source_model_id = uuid4()
    agent = SimpleNamespace(id=agent_id, tenant_id=tenant_id, primary_model_id=None, fallback_model_id=None)
    user = SimpleNamespace(id=user_id, tenant_id=tenant_id, is_active=True)
    db = _FakeDB([
        _ScalarResult(agent),
        _ScalarResult(user),
        _ScalarResult(None),
    ])

    monkeypatch.setenv("HIVE_EVAL_TENANT_ID", str(tenant_id))
    monkeypatch.setenv("HIVE_EVAL_AGENT_ID", str(agent_id))
    monkeypatch.setenv("HIVE_EVAL_USER_ID", str(user_id))
    monkeypatch.setattr(eval_ci_service, "enter_rls_bypass", lambda *_args, **_kwargs: _Bypass())
    monkeypatch.setattr(
        eval_ci_service,
        "get_secrets_provider",
        lambda: SimpleNamespace(encrypt=lambda value: f"encrypted:{value}"),
    )

    result = await eval_ci_service.configure_production_behavior_eval_model(
        db,
        eval_ci_service.EvalRuntimeModelConfig(
            source_model_id=str(source_model_id),
            source_tenant_id=str(uuid4()),
            provider="openai",
            model="gpt-5.5",
            api_key="sk-prod",
            label="Company Default",
            max_output_tokens=8192,
        ),
    )

    mirrored_model = next(item for item in db.added if isinstance(item, LLMModel))
    mirror_setting = next(item for item in db.added if isinstance(item, TenantSetting))

    assert mirrored_model.tenant_id == tenant_id
    assert mirrored_model.api_key_encrypted == "encrypted:sk-prod"
    assert mirrored_model.enabled is True
    assert agent.primary_model_id == mirrored_model.id
    assert mirror_setting.tenant_id == tenant_id
    assert mirror_setting.key == eval_ci_service.BEHAVIOR_EVAL_RUNTIME_MODEL_MIRROR_SETTING_KEY
    assert mirror_setting.value["source_model_id"] == str(source_model_id)
    assert mirror_setting.value["model_id"] == str(mirrored_model.id)
    assert db.flushed is True
    assert db.committed is True
    assert result["model"]["model_id"] == str(mirrored_model.id)
    assert "api_key" not in result["model"]


@pytest.mark.asyncio
async def test_enterprise_eval_runtime_sync_forwards_decrypted_model_server_side(monkeypatch):
    import app.api.enterprise as enterprise_api

    tenant_id = uuid4()
    model_id = uuid4()
    current_user = SimpleNamespace(id=uuid4(), role="platform_admin", tenant_id=uuid4())
    model = SimpleNamespace(
        id=model_id,
        tenant_id=tenant_id,
        provider="anthropic",
        model="claude-sonnet-4-5",
        base_url=None,
        label="Company Sonnet",
        api_key="sk-decrypted",
        enabled=True,
        supports_vision=True,
        max_output_tokens=12000,
        max_input_tokens=200000,
        temperature=None,
        reasoning_mode="provider_default",
        reasoning_effort=None,
        reasoning_budget_tokens=None,
        reasoning_display=None,
        preserve_reasoning=True,
        text_verbosity=None,
        provider_options={"beta": "on"},
    )
    db = _FakeDB([_ScalarResult(model)])
    captured: dict[str, object] = {}

    class _Response:
        status_code = 200
        text = '{"ok": true}'

        def raise_for_status(self):
            return None

        def json(self):
            return {
                "configured": True,
                "tenant_id": "eval-tenant-internal",
                "user": {
                    "id": "eval-user-internal",
                    "display_name": "Eval Admin",
                },
                "model": {
                    "model_id": "eval-model-id",
                    "provider": "anthropic",
                    "model": "claude-sonnet-4-5",
                    "api_key": "must-not-leak",
                },
                "mirror": {
                    "model_id": "eval-model-id",
                    "source_model_id": str(model_id),
                    "provider": "anthropic",
                    "model": "claude-sonnet-4-5",
                    "label": "Company Sonnet",
                    "synced_at": "2026-06-14T00:00:00+00:00",
                },
            }

    class _Client:
        def __init__(self, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout")

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc_info):
            return False

        async def post(self, url, json, headers):
            captured["url"] = url
            captured["json"] = json
            captured["headers"] = headers
            return _Response()

    monkeypatch.setenv("HIVE_EVAL_API_URL", "https://backend-eval.example")
    monkeypatch.setenv("HIVE_EVAL_CI_TOKEN", "ci-token")
    monkeypatch.setattr(enterprise_api.httpx, "AsyncClient", _Client)

    result = await enterprise_api.sync_eval_ci_runtime_model(
        data=enterprise_api.EvalRuntimeModelSyncRequest(model_id=model_id),
        tenant_id=str(tenant_id),
        current_user=current_user,
        db=db,
    )

    params = next(
        statement.compile().params
        for statement in db.statements
        if "SET LOCAL app.current_tenant_id" not in str(statement)
    )
    assert tenant_id in params.values()
    assert captured["url"] == "https://backend-eval.example/api/eval-ci/runtime/model"
    assert captured["headers"] == {"Authorization": "Bearer ci-token"}
    assert captured["json"]["api_key"] == "sk-decrypted"
    assert captured["json"]["source_model_id"] == str(model_id)
    assert captured["json"]["source_tenant_id"] == str(tenant_id)
    assert captured["json"]["provider_options"] == {"beta": "on"}
    assert result["model"]["model"] == "claude-sonnet-4-5"
    assert result["mirror"]["source_model_id"] == str(model_id)
    assert "tenant_id" not in result
    assert "user" not in result
    assert "model_id" not in result["model"]
    assert "api_key" not in result["model"]


def test_eval_runtime_model_config_accepts_high_output_below_absolute():
    # Mirroring a high-output company model (e.g. DeepSeek 384K) must not be
    # rejected by the eval runtime payload's max_output_tokens bound.
    import app.services.eval_ci_service as eval_ci_service

    cfg = eval_ci_service.EvalRuntimeModelConfig(
        provider="deepseek",
        model="deepseek-v4-pro",
        api_key="sk-prod",
        label="Company DeepSeek",
        max_output_tokens=384000,
    )
    assert cfg.max_output_tokens == 384000


def test_eval_runtime_model_config_rejects_above_absolute_output():
    import pytest as _pytest
    from pydantic import ValidationError

    import app.services.eval_ci_service as eval_ci_service

    with _pytest.raises(ValidationError):
        eval_ci_service.EvalRuntimeModelConfig(
            provider="deepseek",
            model="deepseek-v4-pro",
            api_key="sk-prod",
            label="Company DeepSeek",
            max_output_tokens=eval_ci_service.EVAL_RUNTIME_MAX_OUTPUT_TOKENS + 1,
        )
