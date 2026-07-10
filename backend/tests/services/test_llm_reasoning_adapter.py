from __future__ import annotations

from types import SimpleNamespace

from app.services.llm_client import (
    LLMMessage,
    OpenAICompatibleClient,
    OpenAIResponsesClient,
    create_llm_client,
    get_provider_manifest,
)
from app.services.llm_reasoning import build_reasoning_kwargs


def _model(**overrides):
    base = {
        "provider": "openai-response",
        "model": "gpt-5.1",
        "reasoning_mode": "enabled",
        "reasoning_effort": "high",
        "reasoning_budget_tokens": None,
        "reasoning_display": None,
        "preserve_reasoning": None,
        "text_verbosity": None,
        "provider_options": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_openai_responses_maps_reasoning_effort_and_verbosity():
    kwargs = build_reasoning_kwargs(
        _model(text_verbosity="low"),
        tools_enabled=True,
    )

    assert kwargs == {
        "reasoning": {"effort": "high"},
        "text": {"verbosity": "low"},
    }


def test_openai_disabled_reasoning_maps_to_minimal_effort():
    kwargs = build_reasoning_kwargs(
        _model(reasoning_mode="disabled", reasoning_effort=None),
        tools_enabled=True,
    )

    assert kwargs == {"reasoning": {"effort": "minimal"}}


def test_anthropic_maps_manual_thinking_budget():
    kwargs = build_reasoning_kwargs(
        _model(
            provider="anthropic",
            model="claude-sonnet-4-5",
            reasoning_effort=None,
            reasoning_budget_tokens=4096,
        ),
        tools_enabled=True,
    )

    assert kwargs == {"thinking": {"type": "enabled", "budget_tokens": 4096}}


def test_anthropic_maps_adaptive_thinking():
    kwargs = build_reasoning_kwargs(
        _model(
            provider="anthropic",
            model="claude-opus-4-7",
            reasoning_mode="adaptive",
            reasoning_effort="high",
        ),
        tools_enabled=True,
    )

    assert kwargs == {"thinking": {"type": "adaptive", "effort": "high"}}


def test_qwen_maps_enable_thinking_budget_and_preservation():
    kwargs = build_reasoning_kwargs(
        _model(
            provider="qwen",
            model="qwen3-max",
            reasoning_budget_tokens=8192,
            preserve_reasoning=True,
        ),
        tools_enabled=True,
    )

    assert kwargs == {
        "enable_thinking": True,
        "thinking_budget": 8192,
        "preserve_thinking": True,
    }


def test_qwen_disabled_maps_enable_thinking_false():
    kwargs = build_reasoning_kwargs(
        _model(provider="qwen", model="qwen3-max", reasoning_mode="disabled", reasoning_effort=None),
        tools_enabled=True,
    )

    assert kwargs == {"enable_thinking": False}


def test_kimi_maps_thinking_type():
    kwargs = build_reasoning_kwargs(
        _model(provider="kimi", model="kimi-k2.5", reasoning_mode="disabled", reasoning_effort=None),
        tools_enabled=True,
    )

    assert kwargs == {"thinking": {"type": "disabled"}}


def test_glm_maps_thinking_type_and_reasoning_preservation():
    kwargs = build_reasoning_kwargs(
        _model(provider="zhipu", model="glm-4.7", preserve_reasoning=True),
        tools_enabled=True,
    )

    assert kwargs == {"thinking": {"type": "enabled"}, "clear_thinking": False}


def test_minimax_maps_reasoning_split_without_fake_effort():
    kwargs = build_reasoning_kwargs(
        _model(provider="minimax", model="MiniMax-M2.7", reasoning_effort="high", preserve_reasoning=True),
        tools_enabled=True,
    )

    assert kwargs == {"reasoning_split": True}


def test_deepseek_maps_official_thinking_controls_and_omits_temperature():
    kwargs = build_reasoning_kwargs(
        _model(provider="deepseek", model="deepseek-v4-pro", reasoning_mode="enabled", reasoning_effort="max"),
        tools_enabled=False,
    )

    assert kwargs == {"thinking": {"type": "enabled"}, "reasoning_effort": "max", "_omit_temperature": True}


def test_deepseek_disabled_thinking_keeps_sampling_controls_available():
    kwargs = build_reasoning_kwargs(
        _model(provider="deepseek", model="deepseek-v4-flash", reasoning_mode="disabled", reasoning_effort="max"),
        tools_enabled=True,
    )

    assert kwargs == {"thinking": {"type": "disabled"}}


def test_deepseek_payload_preserves_reasoning_content_for_thinking_turns_and_omits_temperature():
    client = OpenAICompatibleClient(api_key="test", model="deepseek-v4-pro", base_url="https://api.deepseek.com")
    payload = client._build_payload(
        [
            LLMMessage(role="assistant", content="answer", reasoning_content="private chain"),
            LLMMessage(role="user", content="next"),
        ],
        tools=None,
        temperature=0.7,
        max_tokens=512,
        _omit_temperature=True,
    )

    assert payload["messages"][0]["reasoning_content"] == "private chain"
    assert "temperature" not in payload


def test_openai_gpt55_routes_to_responses_and_omits_unsupported_temperature():
    client = create_llm_client(provider="openai", api_key="test", model="gpt-5.5")

    kwargs = build_reasoning_kwargs(
        _model(provider="openai", model="gpt-5.5", reasoning_mode="provider_default", reasoning_effort=None),
        tools_enabled=True,
    )
    payload = client._build_payload(
        [LLMMessage(role="user", content="Say ok.")],
        tools=None,
        temperature=0.7,
        max_tokens=16,
        **kwargs,
    )

    routed_client = getattr(client, "_inner", client)
    assert isinstance(routed_client, OpenAIResponsesClient)
    assert payload["model"] == "gpt-5.5"
    assert "input" in payload
    assert "temperature" not in payload


def test_provider_manifest_exposes_reasoning_capabilities_and_recommended_models():
    manifest = {entry["provider"]: entry for entry in get_provider_manifest()}

    assert manifest["openai-response"]["reasoning_strategy"] == "openai_responses"
    assert "high" in manifest["openai-response"]["supported_reasoning_efforts"]
    assert manifest["anthropic"]["reasoning_strategy"] == "anthropic_thinking"
    assert manifest["qwen"]["reasoning_strategy"] == "qwen_thinking"
    assert manifest["minimax"]["reasoning_strategy"] == "minimax_reasoning_split"
    assert manifest["kimi"]["reasoning_strategy"] == "kimi_thinking"
    assert manifest["zhipu"]["reasoning_strategy"] == "glm_thinking"
    assert manifest["deepseek"]["default_base_url"] == "https://api.deepseek.com"
    assert manifest["deepseek"]["reasoning_strategy"] == "deepseek_thinking"
    assert manifest["deepseek"]["supported_reasoning_modes"] == ["provider_default", "enabled", "disabled"]
    assert manifest["deepseek"]["supported_reasoning_efforts"] == ["high", "max"]
    assert manifest["deepseek"]["supports_tools_with_reasoning"] is True
    assert [item["model"] for item in manifest["deepseek"]["recommended_models"]] == [
        "deepseek-v4-flash",
        "deepseek-v4-pro",
    ]
    assert manifest["qwen"]["recommended_models"]
