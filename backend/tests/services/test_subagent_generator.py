"""AI-generated subagent definitions (CC /agents "generate" method, vendor-neutral).

The generator translates a natural-language description into a complete
定义.md (name / description / type / system_prompt) via the platform LLM.
Neutrality is part of the product contract (L3: model equality) — the
generation prompt must not name any AI vendor or model family.
"""

from __future__ import annotations

import json

import pytest

import app.services.subagent_generator as gen_mod
from app.agents.subagent_definition import parse_subagent_definition
from app.services.subagent_generator import (
    GENERATION_SYSTEM_PROMPT,
    SubagentGenerationError,
    generate_subagent_definition,
)

_MODEL_CONFIG = {"provider": "openai", "api_key": "k", "model": "m", "base_url": None}


def _llm_response(content: str) -> dict:
    return {"choices": [{"message": {"content": content}}]}


def _ok_payload(**overrides) -> str:
    payload = {
        "name": "defi-market-scout",
        "description": "Use this agent when you need fresh DeFi market intelligence.",
        "type": "explorer",
        "system_prompt": "You are a DeFi market scout. Investigate and report with sources.",
    }
    payload.update(overrides)
    return json.dumps(payload)


def test_generation_prompt_is_vendor_neutral():
    # L3 model equality: a neutral control plane never names a privileged vendor.
    lowered = GENERATION_SYSTEM_PROMPT.lower()
    for vendor in ("claude", "anthropic", "gpt", "openai", "gemini", "deepseek"):
        assert vendor not in lowered, f"generation prompt must not name vendor {vendor!r}"


def test_generation_prompt_speaks_hive_tooling():
    # The whenToUse examples must reference Hive's spawn surface, not CC's Agent tool.
    assert "spawn_subagent" in GENERATION_SYSTEM_PROMPT
    assert "explorer" in GENERATION_SYSTEM_PROMPT
    assert "worker" in GENERATION_SYSTEM_PROMPT
    assert "critic" in GENERATION_SYSTEM_PROMPT


def test_generation_prompt_keeps_fewshot_examples():
    # CC parity: the two full <example> demonstrations are few-shot format
    # teaching for the description field — owner decision: no simplification.
    assert GENERATION_SYSTEM_PROMPT.count("<example>") == 2
    assert GENERATION_SYSTEM_PROMPT.count("</example>") == 2
    assert "test-runner" in GENERATION_SYSTEM_PROMPT
    assert "greeting-responder" in GENERATION_SYSTEM_PROMPT


@pytest.mark.asyncio
async def test_generate_returns_parseable_definition(monkeypatch):
    captured: dict = {}

    async def fake_chat_complete(**kwargs):
        captured.update(kwargs)
        return _llm_response(_ok_payload())

    monkeypatch.setattr(gen_mod, "chat_complete", fake_chat_complete)

    definition = await generate_subagent_definition(
        "track DeFi market movements for me",
        model_config=_MODEL_CONFIG,
        existing_names=["explorer", "worker", "critic"],
    )

    spec = parse_subagent_definition(definition)
    assert spec.name == "defi-market-scout"
    assert spec.description.startswith("Use this agent when")
    assert spec.type == "explorer"
    assert "DeFi market scout" in spec.system_prompt
    # the user request and the taken names both reach the LLM
    user_message = captured["messages"][-1]["content"]
    assert "track DeFi market movements" in user_message
    assert "explorer" in user_message


@pytest.mark.asyncio
async def test_generate_strips_markdown_fences(monkeypatch):
    async def fake_chat_complete(**kwargs):
        return _llm_response("```json\n" + _ok_payload() + "\n```")

    monkeypatch.setattr(gen_mod, "chat_complete", fake_chat_complete)
    definition = await generate_subagent_definition("x", model_config=_MODEL_CONFIG)
    assert parse_subagent_definition(definition).name == "defi-market-scout"


@pytest.mark.asyncio
async def test_generate_unknown_type_falls_back_to_explorer(monkeypatch):
    # explorer is the narrowest (read-only) baseline — the safe direction.
    async def fake_chat_complete(**kwargs):
        return _llm_response(_ok_payload(type="superagent"))

    monkeypatch.setattr(gen_mod, "chat_complete", fake_chat_complete)
    definition = await generate_subagent_definition("x", model_config=_MODEL_CONFIG)
    assert parse_subagent_definition(definition).type == "explorer"


@pytest.mark.asyncio
async def test_generate_fails_loud_on_bad_json(monkeypatch):
    async def fake_chat_complete(**kwargs):
        return _llm_response("I cannot do that")

    monkeypatch.setattr(gen_mod, "chat_complete", fake_chat_complete)
    with pytest.raises(SubagentGenerationError):
        await generate_subagent_definition("x", model_config=_MODEL_CONFIG)


@pytest.mark.asyncio
async def test_generate_fails_loud_on_invalid_name(monkeypatch):
    async def fake_chat_complete(**kwargs):
        return _llm_response(_ok_payload(name="../escape"))

    monkeypatch.setattr(gen_mod, "chat_complete", fake_chat_complete)
    with pytest.raises(SubagentGenerationError, match="name"):
        await generate_subagent_definition("x", model_config=_MODEL_CONFIG)
