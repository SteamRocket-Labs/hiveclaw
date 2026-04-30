"""Provider-specific reasoning/thinking request adaptation.

The public model setting is provider-neutral, but each vendor exposes reasoning
controls with different parameter names and constraints. This module is the
single translation layer between persisted LLMModel fields and provider payload
overrides.
"""

from __future__ import annotations

from typing import Any


_OPENAI_EFFORTS = {"none", "minimal", "low", "medium", "high", "xhigh"}
_ANTHROPIC_EFFORTS = {"low", "medium", "high"}
_DEEPSEEK_EFFORTS = {"high", "max"}


def _attr(model_config: Any, name: str, default: Any = None) -> Any:
    if isinstance(model_config, dict):
        return model_config.get(name, default)
    return getattr(model_config, name, default)


def _provider(model_config: Any) -> str:
    return str(_attr(model_config, "provider", "") or "").strip().lower()


def _model_name(model_config: Any) -> str:
    return str(_attr(model_config, "model", "") or "").strip().lower()


def _reasoning_mode(model_config: Any) -> str:
    return str(_attr(model_config, "reasoning_mode", None) or "provider_default").strip().lower()


def _is_enabled(model_config: Any) -> bool:
    return _reasoning_mode(model_config) in {"enabled", "on", "auto", "adaptive"}


def _is_disabled(model_config: Any) -> bool:
    return _reasoning_mode(model_config) in {"disabled", "off", "none"}


def _positive_int(value: Any) -> int | None:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _clean_effort(value: Any, allowed: set[str]) -> str | None:
    effort = str(value or "").strip().lower()
    return effort if effort in allowed else None


def build_reasoning_kwargs(model_config: Any, *, tools_enabled: bool) -> dict[str, Any]:
    """Build provider payload overrides from a model's reasoning settings.

    Unknown/provider-default settings return an empty dict. The returned keys are
    deliberately provider-native because each client already owns final payload
    construction.
    """
    provider = _provider(model_config)
    model_name = _model_name(model_config)
    mode = _reasoning_mode(model_config)
    budget = _positive_int(_attr(model_config, "reasoning_budget_tokens", None))
    preserve = _attr(model_config, "preserve_reasoning", None)
    effort = str(_attr(model_config, "reasoning_effort", "") or "").strip().lower()
    kwargs: dict[str, Any] = {}

    if provider in {"openai-response", "openai_responses", "openairesponses"}:
        if _is_disabled(model_config):
            kwargs["reasoning"] = {"effort": "minimal"}
        elif _is_enabled(model_config):
            kwargs["reasoning"] = {"effort": _clean_effort(effort, _OPENAI_EFFORTS) or "medium"}
        verbosity = str(_attr(model_config, "text_verbosity", "") or "").strip().lower()
        if verbosity in {"low", "medium", "high"}:
            kwargs["text"] = {"verbosity": verbosity}

    elif provider == "openai":
        if _is_disabled(model_config):
            kwargs["reasoning_effort"] = "minimal"
        elif _is_enabled(model_config):
            kwargs["reasoning_effort"] = _clean_effort(effort, _OPENAI_EFFORTS) or "medium"

    elif provider == "anthropic":
        if mode == "adaptive":
            thinking: dict[str, Any] = {"type": "adaptive"}
            clean_effort = _clean_effort(effort, _ANTHROPIC_EFFORTS)
            if clean_effort:
                thinking["effort"] = clean_effort
            kwargs["thinking"] = thinking
        elif _is_enabled(model_config):
            thinking = {"type": "enabled"}
            if budget:
                thinking["budget_tokens"] = budget
            kwargs["thinking"] = thinking

    elif provider == "qwen":
        if _is_disabled(model_config):
            kwargs["enable_thinking"] = False
        elif _is_enabled(model_config):
            kwargs["enable_thinking"] = True
            if budget:
                kwargs["thinking_budget"] = budget
            if preserve is not None:
                kwargs["preserve_thinking"] = bool(preserve)

    elif provider in {"kimi", "moonshot"}:
        if _is_disabled(model_config):
            kwargs["thinking"] = {"type": "disabled"}
        elif _is_enabled(model_config) or "thinking" in model_name:
            kwargs["thinking"] = {"type": "enabled"}

    elif provider in {"zhipu", "glm", "zai", "z.ai"}:
        if _is_disabled(model_config):
            kwargs["thinking"] = {"type": "disabled"}
        elif _is_enabled(model_config):
            kwargs["thinking"] = {"type": "enabled"}
        if preserve is not None:
            kwargs["clear_thinking"] = not bool(preserve)

    elif provider == "minimax":
        # MiniMax exposes reasoning separation/preservation, not a documented
        # effort dial. Keep the model's native thinking behavior intact.
        if preserve:
            kwargs["reasoning_split"] = True

    elif provider == "deepseek":
        # DeepSeek V4 thinking mode supports high/max reasoning effort. When
        # thinking is enabled/default, sampling controls such as temperature are
        # ignored by the provider, so omit temperature instead of showing a fake
        # active dial.
        if _is_disabled(model_config):
            kwargs["thinking"] = {"type": "disabled"}
        else:
            if _is_enabled(model_config):
                kwargs["thinking"] = {"type": "enabled"}
            clean_effort = _clean_effort(effort, _DEEPSEEK_EFFORTS)
            if clean_effort:
                kwargs["reasoning_effort"] = clean_effort
            kwargs["_omit_temperature"] = True

    provider_options = _attr(model_config, "provider_options", None)
    if isinstance(provider_options, dict):
        kwargs.update(provider_options)

    return kwargs


def resolve_temperature(model_config: Any, default: float = 0.7) -> float:
    """Resolve model temperature while preserving explicit zero."""
    configured = _attr(model_config, "temperature", None)
    if configured is None or configured == "":
        return default
    try:
        return float(configured)
    except (TypeError, ValueError):
        return default
