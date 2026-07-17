"""LLM provider error classification and user-facing policy."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal


LLMErrorKind = Literal[
    "quota_exhausted",
    "rate_limited",
    "auth_or_permission",
    "model_not_found",
    "provider_bad_request",
    "timeout",
    "prompt_too_long",
    "connection",
    "unknown",
]


@dataclass(frozen=True, slots=True)
class LLMErrorClassification:
    kind: LLMErrorKind
    user_message: str
    requires_user_decision: bool = False


def classify_llm_error(exc: Exception) -> LLMErrorClassification:
    """Classify model-provider failures without hiding account/config state."""
    msg = str(exc)
    lower = msg.lower()

    if (
        "insufficient balance" in lower
        or "insufficient_balance" in lower
        or "balance is insufficient" in lower
        or "credit balance" in lower
        or "余额不足" in msg
    ):
        return LLMErrorClassification(
            kind="quota_exhausted",
            user_message="[LLM Error] AI 模型额度或余额不足，请联系管理员检查账户余额、模型额度或切换模型。",
            requires_user_decision=True,
        )

    if (
        "quota" in lower
        or "insufficient_quota" in lower
        or "quotaexhausted" in lower
        or "allocated quota exceeded" in lower
        or "token-plan quota has been exhausted" in lower
    ):
        return LLMErrorClassification(
            kind="quota_exhausted",
            user_message="[LLM Error] AI 模型额度已耗尽，请联系管理员检查模型额度或切换模型。",
            requires_user_decision=True,
        )

    if "529" in msg or "overloaded" in lower:
        return LLMErrorClassification(
            kind="rate_limited",
            user_message="[LLM Error] AI 模型服务方暂时过载，已重试仍失败，请稍后重试。",
            requires_user_decision=False,
        )

    if (
        "429" in msg
        or "too many requests" in lower
        or "rate_limit" in lower
        or "rate limit" in lower
        or "速率限制" in msg
    ):
        return LLMErrorClassification(
            kind="rate_limited",
            user_message="[LLM Error] AI 模型服务方已限流，请稍后重试，或由用户选择切换模型。",
            requires_user_decision=True,
        )

    if (
        "context_length" in lower
        or "maximum context length" in lower
        or "prompt is too long" in lower
        or "too many tokens" in lower
        or "input too long" in lower
        or "request too large" in lower
    ):
        return LLMErrorClassification(
            kind="prompt_too_long",
            user_message="[LLM Error] 对话内容超出模型上下文长度限制，请开启新会话或清理历史消息。",
        )

    if (
        "model not exist" in lower
        or "model_not_found" in lower
        or "model not found" in lower
        or "model does not exist" in lower
        or ("the model" in lower and "does not exist" in lower)
        or "404" in msg
    ):
        return LLMErrorClassification(
            kind="model_not_found",
            user_message="[LLM Error] AI 模型名称不存在或未对当前账号开放，请联系管理员检查模型配置。",
            requires_user_decision=True,
        )

    if (
        "401" in msg
        or "403" in msg
        or "authentication" in lower
        or "unauthorized" in lower
        or "invalid api key" in lower
        or "permission denied" in lower
    ):
        return LLMErrorClassification(
            kind="auth_or_permission",
            user_message="[LLM Error] AI 模型 API 认证失败，请联系管理员检查模型配置。",
            requires_user_decision=True,
        )

    if "400" in msg or "bad request" in lower or "invalid_request" in lower:
        return LLMErrorClassification(
            kind="provider_bad_request",
            user_message="[LLM Error] AI 模型供应商拒绝了当前请求，请检查模型名称、参数或消息格式。",
            requires_user_decision=True,
        )

    if "timeout" in lower or "timed out" in lower:
        return LLMErrorClassification(
            kind="timeout",
            user_message="[LLM Error] AI 模型服务方响应超时，请稍后重试。",
        )

    if "connection" in lower or "connect" in lower:
        return LLMErrorClassification(
            kind="connection",
            user_message="[LLM Error] 连接 AI 模型服务方失败，请稍后重试。",
        )

    return LLMErrorClassification(
        kind="unknown",
        user_message=f"[LLM Error] AI 模型调用异常，请稍后重试。({msg})",
    )


def should_surface_without_model_fallback(exc: Exception) -> bool:
    """Return True when changing models would hide provider/account truth."""
    return classify_llm_error(exc).requires_user_decision


def is_llm_error_message(content: object) -> bool:
    """Detect persisted assistant rows that represent failed model/runtime calls."""
    if not isinstance(content, str):
        return False
    stripped = content.lstrip()
    return stripped.startswith("[LLM Error]") or stripped.startswith("[Runtime Limit]")
