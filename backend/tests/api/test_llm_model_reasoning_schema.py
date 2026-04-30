from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.schemas.schemas import LLMModelCreate, LLMModelOut, LLMModelUpdate


def test_llm_model_create_accepts_reasoning_settings():
    data = LLMModelCreate(
        provider="qwen",
        model="qwen3-max",
        api_key="sk-test",
        label="Qwen Reasoning",
        temperature=0.6,
        reasoning_mode="enabled",
        reasoning_budget_tokens=8192,
        preserve_reasoning=True,
        provider_options={"preserve_thinking": True},
    )

    assert data.temperature == 0.6
    assert data.reasoning_mode == "enabled"
    assert data.reasoning_budget_tokens == 8192
    assert data.preserve_reasoning is True
    assert data.provider_options == {"preserve_thinking": True}


def test_llm_model_update_accepts_reasoning_settings():
    data = LLMModelUpdate(
        reasoning_mode="adaptive",
        reasoning_effort="high",
        text_verbosity="low",
        temperature=1.0,
    )

    assert data.reasoning_mode == "adaptive"
    assert data.reasoning_effort == "high"
    assert data.text_verbosity == "low"
    assert data.temperature == 1.0


def test_llm_model_out_exposes_reasoning_settings():
    out = LLMModelOut(
        id=uuid4(),
        provider="openai-response",
        model="gpt-5.1",
        label="GPT-5.1",
        enabled=True,
        supports_vision=True,
        created_at=datetime.now(timezone.utc),
        temperature=0.7,
        reasoning_mode="enabled",
        reasoning_effort="high",
        reasoning_budget_tokens=None,
        reasoning_display="summarized",
        preserve_reasoning=True,
        text_verbosity="medium",
        provider_options={"foo": "bar"},
    )

    assert out.reasoning_mode == "enabled"
    assert out.reasoning_effort == "high"
    assert out.reasoning_display == "summarized"
    assert out.preserve_reasoning is True
    assert out.text_verbosity == "medium"
    assert out.provider_options == {"foo": "bar"}
