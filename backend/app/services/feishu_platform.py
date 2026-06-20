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
