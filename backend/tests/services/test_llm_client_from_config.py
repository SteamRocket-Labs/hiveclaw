"""create_llm_client_from_config — the explicit contract for config-dict expansion.

Root cause (2026-06-05 production incident): _model_config() carries a
max_input_tokens window hint with a comment saying "consumers must pop it
first" — an implicit contract. Several consumers didn't pop it and summary-model LLM
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
