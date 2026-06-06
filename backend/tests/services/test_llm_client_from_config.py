"""create_llm_client_from_config — the explicit contract for config-dict expansion.

Root cause (2026-06-05 production incident): _model_config() carries a
max_input_tokens window hint with a comment saying "consumers must pop it
first" — an implicit contract. Four consumers didn't pop (extract_agent,
auto_dream, session_recall, memory_curation) and every summary-model LLM
call in those pipelines died with TypeError from 8024e137 (6-04) onward,
silently absorbed by fail-soft handlers. The factory makes the contract
explicit: any config dict is filtered to the real client signature.
"""

from __future__ import annotations

import pytest

from app.services.llm_client import create_llm_client, create_llm_client_from_config


def test_from_config_filters_window_hint():
    config = {
        "provider": "openai",
        "model": "gpt-x",
        "api_key": "k",
        "base_url": None,
        "max_input_tokens": 128000,  # the hint that killed four pipelines
    }
    client = create_llm_client_from_config(config)
    assert client is not None
    assert client.model == "gpt-x"


def test_from_config_filters_unknown_future_keys():
    config = {
        "provider": "openai",
        "model": "gpt-x",
        "api_key": "k",
        "some_future_hint": True,
        "label": "Main",
    }
    client = create_llm_client_from_config(config)
    assert client.model == "gpt-x"


def test_from_config_matches_direct_call():
    direct = create_llm_client(provider="openai", model="gpt-x", api_key="k")
    via_config = create_llm_client_from_config({"provider": "openai", "model": "gpt-x", "api_key": "k"})
    assert type(direct) is type(via_config)
    assert direct.model == via_config.model


def test_from_config_still_requires_provider():
    with pytest.raises((TypeError, ValueError)):
        create_llm_client_from_config({"model": "gpt-x", "api_key": "k"})


@pytest.mark.asyncio
async def test_memory_curation_caller_survives_window_hint(monkeypatch):
    """Regression pin for the production incident: the curation LLM caller must
    not explode on a summary-model config that carries max_input_tokens."""

    import app.services.memory_curation as cur_mod

    async def fake_summary_config(tenant_id):
        return {"provider": "openai", "model": "gpt-x", "api_key": "k", "max_input_tokens": 128000}

    import app.services.memory_service as mem_service

    monkeypatch.setattr(mem_service, "_get_summary_model_config", fake_summary_config)

    captured: dict = {}

    class _FakeClient:
        model = "gpt-x"

        async def stream(self, **kwargs):
            from types import SimpleNamespace

            captured["called"] = True
            return SimpleNamespace(content='{"action": "hold", "reason": "ok"}')

        async def close(self):
            return None

    monkeypatch.setattr("app.services.llm_client.create_llm_client", lambda **kw: _FakeClient())

    import uuid

    caller = await cur_mod._build_llm_caller(uuid.uuid4())
    assert caller is not None
    result = await caller("system", "user")
    assert captured.get("called") is True
    assert "hold" in result
