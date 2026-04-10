from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from uuid import uuid4

import pytest


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
        self.committed = False
        self.flushed = False
        self.refreshed = []

    async def execute(self, _stmt):
        if not self._results:
            raise AssertionError("No fake DB result prepared")
        return self._results.pop(0)

    def add(self, value):
        self.added.append(value)

    async def flush(self):
        self.flushed = True

    async def commit(self):
        self.committed = True

    async def refresh(self, value):
        self.refreshed.append(value)


def _provider_config(**overrides):
    now = datetime.now(timezone.utc)
    base = dict(
        id=uuid4(),
        tenant_id=uuid4(),
        provider="openai",
        auth_mode="api_key",
        api_key_encrypted="provider-secret-1234",
        access_token_encrypted=None,
        refresh_token_encrypted=None,
        base_url="https://api.openai.com/v1",
        oauth_subject=None,
        oauth_email=None,
        enabled=True,
        metadata_json={},
        last_verified_at=now,
        last_refreshed_at=now,
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


def _model(**overrides):
    now = datetime.now(timezone.utc)
    base = dict(
        id=uuid4(),
        tenant_id=uuid4(),
        provider="openai",
        model="gpt-5.4",
        api_key_encrypted="model-secret-4321",
        base_url="https://api.openai.com/v1",
        label="GPT-5.4",
        max_tokens_per_day=None,
        enabled=True,
        supports_vision=False,
        max_output_tokens=8192,
        max_input_tokens=128000,
        temperature=0.8,
        provider_config_id=None,
        discovered_from_provider=False,
        created_at=now,
        updated_at=now,
    )
    base.update(overrides)
    return SimpleNamespace(**base)


@pytest.mark.asyncio
async def test_list_provider_configs_includes_saved_derived_and_empty_states():
    from app.services.llm_provider_service import LLMProviderConfigService

    tenant_id = uuid4()
    saved = _provider_config(tenant_id=tenant_id, provider="openai")
    derived_model = _model(
        tenant_id=tenant_id,
        provider="minimax",
        model="MiniMax-M1",
        label="MiniMax M1",
        api_key_encrypted="derived-minimax-9999",
        base_url="https://api.minimaxi.com/v1",
    )
    db = _FakeDB([
        _ListResult([saved]),
        _ListResult([derived_model]),
    ])

    service = LLMProviderConfigService()
    configs = await service.list_provider_configs(db, tenant_id)

    openai = next(item for item in configs if item["provider"] == "openai")
    minimax = next(item for item in configs if item["provider"] == "minimax")
    qwen = next(item for item in configs if item["provider"] == "qwen")

    assert openai["source"] == "saved"
    assert openai["api_key_masked"].endswith("1234")
    assert openai["model_count"] == 0

    assert minimax["source"] == "derived"
    assert minimax["api_key_masked"].endswith("9999")
    assert minimax["model_count"] == 1
    assert minimax["base_url"] == "https://api.minimaxi.com/v1"

    assert qwen["source"] == "empty"
    assert qwen["model_count"] == 0


@pytest.mark.asyncio
async def test_verify_provider_uses_adapter_and_returns_models(monkeypatch):
    from app.services.llm_provider_service import LLMProviderConfigService

    service = LLMProviderConfigService()

    class _FakeAdapter:
        async def validate(self, **kwargs):
            assert kwargs["provider"] == "openai"
            assert kwargs["auth_mode"] == "api_key"
            assert kwargs["api_key"] == "sk-test"
            return {
                "success": True,
                "models": ["gpt-5.4", "gpt-4.1"],
                "metadata": {"validated": True},
            }

    monkeypatch.setattr(service, "get_adapter", lambda _provider: _FakeAdapter())

    db = _FakeDB([
        _ScalarResult(None),
        _ListResult([]),
    ])
    result = await service.verify_provider(
        db,
        tenant_id=uuid4(),
        provider="openai",
        payload={"auth_mode": "api_key", "api_key": "sk-test", "base_url": "https://api.openai.com/v1"},
    )

    assert result["success"] is True
    assert result["models"] == ["gpt-5.4", "gpt-4.1"]
    assert result["metadata"]["validated"] is True


@pytest.mark.asyncio
async def test_refresh_provider_models_upserts_without_overwriting_manual_params(monkeypatch):
    from app.services.llm_provider_service import LLMProviderConfigService

    tenant_id = uuid4()
    provider_config = _provider_config(tenant_id=tenant_id, provider="openai")
    existing_manual = _model(
        tenant_id=tenant_id,
        provider="openai",
        model="gpt-5.4",
        label="Custom GPT Label",
        max_output_tokens=2048,
        provider_config_id=None,
        discovered_from_provider=False,
    )
    stale_managed = _model(
        tenant_id=tenant_id,
        provider="openai",
        model="gpt-4.1",
        label="Old Managed Label",
        provider_config_id=provider_config.id,
        discovered_from_provider=True,
    )
    db = _FakeDB([
        _ScalarResult(provider_config),
        _ListResult([existing_manual, stale_managed]),
    ])

    service = LLMProviderConfigService()

    class _FakeAdapter:
        async def list_models(self, **kwargs):
            assert kwargs["provider"] == "openai"
            return ["gpt-5.4"]

    monkeypatch.setattr(service, "get_adapter", lambda _provider: _FakeAdapter())

    result = await service.refresh_provider_models(db, tenant_id, "openai")

    assert result["created"] == 0
    assert result["updated"] == 1
    assert result["disabled"] == 1
    assert existing_manual.label == "Custom GPT Label"
    assert existing_manual.max_output_tokens == 2048
    assert existing_manual.provider_config_id == provider_config.id
    assert existing_manual.discovered_from_provider is True
    assert stale_managed.enabled is False
