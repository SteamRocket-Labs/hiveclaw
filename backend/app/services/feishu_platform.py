"""Feishu/Lark Open Platform region resolution."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

FEISHU_REGION_CN = "feishu_cn"
FEISHU_REGION_LARK_GLOBAL = "lark_global"
FEISHU_REGION_CUSTOM = "custom"

DEFAULT_FEISHU_REGION = FEISHU_REGION_CN

FEISHU_CN_OPEN_API_DOMAIN = "https://open.feishu.cn"
FEISHU_CN_OAUTH_AUTHORIZE_URL = "https://accounts.feishu.cn/open-apis/authen/v1/authorize"

LARK_GLOBAL_OPEN_API_DOMAIN = "https://open.larksuite.com"
LARK_GLOBAL_OAUTH_AUTHORIZE_URL = "https://accounts.larksuite.com/open-apis/authen/v1/authorize"


class FeishuMutationUnknown(RuntimeError):
    """A dispatched mutation lacks a reliable response and must be reconciled."""


def _redact_diagnostic(message, protected_values):
    from app.services.exact_secret_boundary import ExactSecretBoundary

    boundary = ExactSecretBoundary.from_pairs((f"feishu-transport:{i}", value)
        for i, value in enumerate(protected_values) if isinstance(value, str) and value)
    return boundary.redact_text(str(message)).text


def feishu_tool_error(tool_name, *, message, http_status=None, code=None, error_class=None,
    platform=None, protected_values=()):
    """Project an actual protocol failure without interpreting its prose."""
    from app.tools.result_envelope import ToolContentEnvelope, classify_http_status, render_tool_error

    classification, retryable = classify_http_status(http_status) if isinstance(http_status, int) else ("provider_error", False)
    classification = error_class or classification
    next_action = ("configure_connection" if classification == "not_configured" else
        "review_request" if classification in {"invalid_arguments", "invalid_approval_form"} else
        "reconnect" if http_status == 401 else "review_permissions" if http_status == 403 else
        "retry_later" if retryable else "review_provider_error")
    facts = {"schema": "hive.feishu_failure.v1", "tool_name": tool_name, "error_class": classification,
        "http_status": http_status, "provider_code": code if type(code) is int else None,
        "retryable": retryable, "next_action": next_action}
    text = render_tool_error(tool_name=tool_name, error_class=classification,
        message=_redact_diagnostic(message, protected_values),
        provider="feishu_openapi", http_status=http_status, retryable=retryable,
        actionable_hint="Review the current connection and provider response before requesting this action again.",
        extra={"provider_code": facts["provider_code"], "next_action": next_action})
    result = ToolContentEnvelope(text=text, metadata={"feishu_failure": facts})
    if platform is not None and type(http_status) is int:
        from app.services.feishu_read_receipts import attest_feishu_api_failure

        attest_feishu_api_failure(result, platform=platform)
    return result


class FeishuAPIError(RuntimeError):
    """Keep the exact API response facts across the service/office-tool boundary."""

    def __init__(self, message, *, http_status=None, code=None, platform=None, protected_values=()):
        super().__init__(_redact_diagnostic(message, protected_values))
        self.http_status, self.code = http_status, code
        self.platform = platform

    def tool_result(self, tool_name):
        return feishu_tool_error(tool_name, message=str(self), http_status=self.http_status, code=self.code,
            platform=self.platform)


async def post_feishu_mutation(client, url, **kwargs):
    """Never turn an ambiguous mutating transport response into a retryable failure."""
    import httpx

    try:
        response = await client.post(url, **kwargs)
    except httpx.RequestError as exc:
        raise FeishuMutationUnknown("feishu_mutation_outcome_unknown") from exc
    if response.status_code >= 500 or 300 <= response.status_code < 400:
        raise FeishuMutationUnknown("feishu_mutation_outcome_unknown")
    if response.status_code < 300:
        try:
            data = response.json()
        except (ValueError, TypeError) as exc:
            raise FeishuMutationUnknown("feishu_mutation_response_unreadable") from exc
        if not isinstance(data, dict):
            raise FeishuMutationUnknown("feishu_mutation_response_unreadable")
    return response


@dataclass(frozen=True, slots=True)
class FeishuPlatform:
    region: str
    open_api_domain: str
    open_api_base_url: str
    oauth_authorize_url: str

    def open_api_url(self, path: str) -> str:
        clean_path = "/" + path.lstrip("/")
        if clean_path.startswith("/open-apis/"):
            return f"{self.open_api_domain}{clean_path}"
        return f"{self.open_api_base_url}{clean_path}"


def _clean_url(value: Any) -> str:
    return str(value or "").strip().rstrip("/")


def _normalize_region(value: Any) -> str:
    region = str(value or "").strip().lower()
    if region in {"lark", "lark_global", "global", "overseas"}:
        return FEISHU_REGION_LARK_GLOBAL
    if region in {"custom", "custom_domain"}:
        return FEISHU_REGION_CUSTOM
    return FEISHU_REGION_CN


def resolve_feishu_platform(extra_config: Mapping[str, Any] | None = None) -> FeishuPlatform:
    cfg = dict(extra_config or {})
    region = _normalize_region(cfg.get("platform_region"))

    if region == FEISHU_REGION_LARK_GLOBAL:
        return FeishuPlatform(
            region=FEISHU_REGION_LARK_GLOBAL,
            open_api_domain=LARK_GLOBAL_OPEN_API_DOMAIN,
            open_api_base_url=f"{LARK_GLOBAL_OPEN_API_DOMAIN}/open-apis",
            oauth_authorize_url=LARK_GLOBAL_OAUTH_AUTHORIZE_URL,
        )

    if region == FEISHU_REGION_CUSTOM:
        open_api_domain = _clean_url(cfg.get("open_api_domain") or cfg.get("open_api_base_url"))
        oauth_authorize_url = _clean_url(cfg.get("oauth_authorize_url"))
        if not open_api_domain:
            open_api_domain = FEISHU_CN_OPEN_API_DOMAIN
        if open_api_domain.endswith("/open-apis"):
            open_api_domain = open_api_domain[: -len("/open-apis")]
        if not oauth_authorize_url:
            oauth_authorize_url = FEISHU_CN_OAUTH_AUTHORIZE_URL
        return FeishuPlatform(
            region=FEISHU_REGION_CUSTOM,
            open_api_domain=open_api_domain,
            open_api_base_url=f"{open_api_domain}/open-apis",
            oauth_authorize_url=oauth_authorize_url,
        )

    return FeishuPlatform(
        region=FEISHU_REGION_CN,
        open_api_domain=FEISHU_CN_OPEN_API_DOMAIN,
        open_api_base_url=f"{FEISHU_CN_OPEN_API_DOMAIN}/open-apis",
        oauth_authorize_url=FEISHU_CN_OAUTH_AUTHORIZE_URL,
    )
