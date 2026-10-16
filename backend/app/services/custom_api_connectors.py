"""Governed custom API connector support.

Custom API connectors are tenant-owned Tool rows with type ``custom_api``. The
LLM sees only the action schema. Credentials are stored through TenantToolConfig
and injected server-side at execution time.
"""

from __future__ import annotations

import base64
import ipaddress
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urljoin, urlparse

from fastapi import HTTPException
from sqlalchemy import select

from app.database import tenant_scoped_session
from app.models.agent import Agent
from app.models.tool import AgentTool, Tool
from app.services.tenant_resolver import resolve_tenant_for_agent
from app.services.tool_config_service import resolve_tool_config
from app.tools.result_envelope import classify_http_status, render_tool_error

CUSTOM_API_TOOL_PREFIX = "custom_api__"
_CREDENTIAL_ARGUMENT_KEYS = {
    "api_key",
    "apikey",
    "access_token",
    "auth_token",
    "authorization",
    "bearer_token",
    "client_secret",
    "password",
    "secret",
    "token",
    "approved_networks",
    "allowed_networks",
    "network_grant",
}
_SECRET_CONFIG_KEYS = ("api_key", "bearer_token", "basic_username", "basic_password")
_TEMPLATE_RE = re.compile(r"{([A-Za-z_][A-Za-z0-9_]*)}")


class CustomApiConnectorError(ValueError):
    """Raised when a custom API connector is invalid or cannot run safely."""


@dataclass(slots=True, frozen=True)
class PreparedCustomApiRequest:
    method: str
    url: str
    headers: dict[str, str]
    params: dict[str, Any]
    json_body: Any | None
    timeout_seconds: float
    audit: dict[str, Any]
    allowed_networks: tuple[str, ...] = ()
    secret_values: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class CustomApiToolPayload:
    tool_name: str
    display_name: str
    description: str
    parameters_schema: dict[str, Any]
    tool_config: dict[str, Any]
    config_schema: dict[str, Any]
    secret_config: dict[str, str]


def _slugify(value: str, *, fallback: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "_", (value or "").lower()).strip("_")
    slug = re.sub(r"_+", "_", slug)
    return slug or fallback


def build_custom_api_tool_name(connector_name: str, action_name: str) -> str:
    connector = _slugify(connector_name, fallback="connector")
    action = _slugify(action_name, fallback="action")
    return f"{CUSTOM_API_TOOL_PREFIX}{connector}__{action}"[:100].rstrip("_")


def _secret_field_for_auth(auth_scheme: str) -> list[dict[str, Any]]:
    if auth_scheme == "none":
        return []
    if auth_scheme == "bearer":
        return [{"key": "bearer_token", "label": "Bearer Token", "type": "password"}]
    if auth_scheme == "basic":
        return [
            {"key": "basic_username", "label": "Basic Username", "type": "password"},
            {"key": "basic_password", "label": "Basic Password", "type": "password"},
        ]
    return [{"key": "api_key", "label": "API Key", "type": "password"}]


def _secret_config_for_auth(auth_scheme: str, secret_value: str | None) -> dict[str, str]:
    secret = (secret_value or "").strip()
    if not secret:
        return {}
    if auth_scheme == "bearer":
        return {"bearer_token": secret}
    if auth_scheme == "basic":
        username, _, password = secret.partition(":")
        return {"basic_username": username, "basic_password": password}
    if auth_scheme == "api_key":
        return {"api_key": secret}
    return {}


def build_custom_api_tool_payload(
    *,
    connector_name: str,
    action_name: str,
    description: str,
    base_url: str,
    method: str,
    path: str,
    auth_scheme: str = "none",
    auth_location: str = "header",
    auth_name: str | None = None,
    parameters_schema: dict[str, Any] | None = None,
    secret_value: str | None = None,
    headers: dict[str, Any] | None = None,
    query: dict[str, Any] | None = None,
    body_template: Any | None = None,
    timeout_seconds: float = 30.0,
) -> CustomApiToolPayload:
    scheme = (auth_scheme or "none").strip().lower()
    if scheme not in {"none", "api_key", "bearer", "basic"}:
        raise CustomApiConnectorError("auth_scheme must be one of: none, api_key, bearer, basic")
    tool_name = build_custom_api_tool_name(connector_name, action_name)
    tool_config = {
        "base_url": base_url.strip(),
        "auth": {
            "scheme": scheme,
            "in": (auth_location or "header").strip().lower(),
            "name": auth_name or ("Authorization" if scheme == "bearer" else "X-API-Key"),
        },
        "action": {
            "method": (method or "GET").strip().upper(),
            "path": path.strip() or "/",
            "headers": dict(headers or {}),
            "query": dict(query or {}),
            "body": body_template,
            "timeout_seconds": timeout_seconds,
        },
        "custom_api_version": 1,
    }
    return CustomApiToolPayload(
        tool_name=tool_name,
        display_name=f"{connector_name}: {action_name}",
        description=description,
        parameters_schema=parameters_schema or {"type": "object", "properties": {}},
        tool_config=tool_config,
        config_schema={"fields": _secret_field_for_auth(scheme)},
        secret_config=_secret_config_for_auth(scheme, secret_value),
    )


def _reject_credential_arguments(arguments: dict[str, Any]) -> None:
    for key in arguments:
        normalized = re.sub(r"[^a-z0-9]+", "_", str(key).lower()).strip("_")
        if normalized in _CREDENTIAL_ARGUMENT_KEYS:
            raise CustomApiConnectorError(
                "custom API credentials must be configured by an admin, not passed as tool arguments"
            )


def _render_template(value: Any, arguments: dict[str, Any], *, path_context: bool = False) -> Any:
    if isinstance(value, str):
        match = _TEMPLATE_RE.fullmatch(value)
        if match:
            return arguments.get(match.group(1), "")

        def _replace(part: re.Match[str]) -> str:
            raw = arguments.get(part.group(1), "")
            rendered = str(raw)
            return quote(rendered, safe="") if path_context else rendered

        return _TEMPLATE_RE.sub(_replace, value)
    if isinstance(value, dict):
        return {str(k): _render_template(v, arguments) for k, v in value.items()}
    if isinstance(value, list):
        return [_render_template(item, arguments) for item in value]
    return value


def canonicalize_network_grant(networks: list[str]) -> list[str]:
    if len(networks) > 32:
        raise CustomApiConnectorError(
            "At most 32 approved network ranges are allowed. Remove a range and save the grant again."
        )
    canonical: list[str] = []
    for raw in networks:
        text = str(raw or "").strip()
        if not text:
            continue
        try:
            rendered = str(ipaddress.ip_network(text, strict=False))
        except ValueError as exc:
            raise CustomApiConnectorError(
                f"Network range {text!r} is not a valid CIDR. Use a range such as 10.0.0.0/8 and save again."
            ) from exc
        if rendered not in canonical:
            canonical.append(rendered)
    return canonical


def set_connector_network_grant(tool: Any, networks: list[str]) -> list[str]:
    """Replace one connector's admin-approved internal ranges. An empty list clears them."""

    canonical = canonicalize_network_grant(networks)
    config = dict(tool.config or {})
    if canonical:
        config["approved_networks"] = canonical
    else:
        config.pop("approved_networks", None)
    tool.config = config
    return canonical


def merge_preserved_network_grant(new_config: dict[str, Any], previous_config: dict[str, Any] | None) -> dict[str, Any]:
    """Keep the admin grant across ordinary connector edits, and ignore a grant in the edit payload."""

    previous = list((previous_config or {}).get("approved_networks") or [])
    merged = dict(new_config or {})
    merged.pop("approved_networks", None)
    if previous:
        merged["approved_networks"] = list(previous)
    return merged


def _validate_base_url(base_url: str) -> str:
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise CustomApiConnectorError("base_url must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise CustomApiConnectorError("base_url must not contain userinfo credentials")
    return base_url.rstrip("/")


def _redact_headers(headers: dict[str, str], secret_values: set[str]) -> dict[str, str]:
    redacted: dict[str, str] = {}
    for key, value in headers.items():
        redacted[key] = "[secret]" if value in secret_values or key.lower() == "authorization" else value
    return redacted


def prepare_custom_api_request(
    *,
    tool_name: str,
    tool_config: dict[str, Any],
    secret_config: dict[str, str],
    arguments: dict[str, Any],
) -> PreparedCustomApiRequest:
    _reject_credential_arguments(arguments)
    base_url = _validate_base_url(str(tool_config.get("base_url") or ""))
    action = tool_config.get("action") if isinstance(tool_config.get("action"), dict) else {}
    auth = tool_config.get("auth") if isinstance(tool_config.get("auth"), dict) else {}
    method = str(action.get("method") or "GET").upper()
    if method not in {"GET", "POST", "PUT", "PATCH", "DELETE"}:
        raise CustomApiConnectorError("custom API method must be GET, POST, PUT, PATCH, or DELETE")
    path = str(_render_template(str(action.get("path") or "/"), arguments, path_context=True))
    url = urljoin(base_url + "/", path.lstrip("/"))
    headers = {str(k): str(_render_template(v, arguments)) for k, v in dict(action.get("headers") or {}).items()}
    params = {str(k): _render_template(v, arguments) for k, v in dict(action.get("query") or {}).items()}
    json_body = _render_template(action.get("body"), arguments) if action.get("body") is not None else None
    scheme = str(auth.get("scheme") or "none").lower()
    location = str(auth.get("in") or "header").lower()
    auth_name = str(auth.get("name") or "X-API-Key")
    secret_values = {v for v in secret_config.values() if isinstance(v, str) and v}
    if scheme == "api_key":
        api_key = secret_config.get("api_key")
        if not api_key:
            raise CustomApiConnectorError("custom API api_key is not configured")
        if location == "query":
            params[auth_name] = api_key
        else:
            headers[auth_name] = api_key
    elif scheme == "bearer":
        token = secret_config.get("bearer_token")
        if not token:
            raise CustomApiConnectorError("custom API bearer_token is not configured")
        headers["Authorization"] = f"Bearer {token}"
        secret_values.add(f"Bearer {token}")
    elif scheme == "basic":
        username = secret_config.get("basic_username")
        password = secret_config.get("basic_password")
        if not username or not password:
            raise CustomApiConnectorError("custom API basic credentials are not configured")
        encoded = base64.b64encode(f"{username}:{password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {encoded}"
        secret_values.add(f"Basic {encoded}")
    elif scheme != "none":
        raise CustomApiConnectorError("Unsupported custom API auth scheme")
    timeout_seconds = float(action.get("timeout_seconds") or 30.0)
    timeout_seconds = min(max(timeout_seconds, 1.0), 120.0)
    audit_params = {k: ("[secret]" if v in secret_values else v) for k, v in params.items()}
    networks = tool_config.get("approved_networks") if isinstance(tool_config.get("approved_networks"), list) else []
    return PreparedCustomApiRequest(
        method=method,
        url=url,
        headers=headers,
        params=params,
        json_body=json_body,
        timeout_seconds=timeout_seconds,
        audit={
            "tool": tool_name,
            "method": method,
            "url": url,
            "headers": _redact_headers(headers, secret_values),
            "params": audit_params,
            "has_json_body": json_body is not None,
        },
        allowed_networks=tuple(str(item) for item in networks),
        secret_values=tuple(secret_values),
    )


def _governed_custom_api_error(tool_name: str, exc: Exception) -> str:
    from app.services.governed_egress import GovernedEgressError

    if isinstance(exc, GovernedEgressError):
        dns_unavailable = exc.code == "network_target_denied" and exc.reason == "DNS resolution failed closed"
        if exc.code == "network_timeout" or dns_unavailable:
            error_class = "custom_api_unavailable"
            retryable = True
            hint = "Check the connector URL, allowlist, network route, and request template."
        elif exc.code in {"network_target_denied", "network_redirect_denied"}:
            error_class = "custom_api_target_denied"
            retryable = False
            hint = "Ask an administrator to approve this connector's network range."
        else:
            error_class = "custom_api_request_error"
            retryable = False
            hint = "Check the connector URL, allowlist, network route, and request template."
        return render_tool_error(
            tool_name=tool_name,
            error_class=error_class,
            message=f"{tool_name} blocked: {exc.reason}",
            provider="custom_api",
            retryable=retryable,
            actionable_hint=hint,
        )
    return render_tool_error(
        tool_name=tool_name,
        error_class="custom_api_request_error",
        message=f"{tool_name} request failed: {exc}",
        provider="custom_api",
        retryable=True,
        actionable_hint="Check the connector URL, allowlist, network route, and request template.",
    )


async def execute_prepared_custom_api_request(tool_name: str, prepared: PreparedCustomApiRequest) -> str:
    from app.services.governed_egress import EgressLimits, GovernedEgressError, request_governed_http

    try:
        response = await request_governed_http(
            prepared.method,
            prepared.url,
            headers=prepared.headers,
            params=prepared.params,
            json_body=prepared.json_body,
            allowed_networks=prepared.allowed_networks,
            secret_values=prepared.secret_values,
            limits=EgressLimits(total_timeout_seconds=prepared.timeout_seconds),
        )
    except GovernedEgressError as exc:
        return _governed_custom_api_error(tool_name, exc)
    except Exception as exc:
        return _governed_custom_api_error(tool_name, exc)
    body = response.text
    if response.status_code >= 400:
        error_class, retryable = classify_http_status(response.status_code)
        return render_tool_error(
            tool_name=tool_name,
            error_class=error_class,
            message=f"{tool_name} failed with HTTP {response.status_code}: {body}",
            provider="custom_api",
            http_status=response.status_code,
            retryable=retryable,
            actionable_hint="Check connector credentials, endpoint path, and action arguments.",
        )
    content_type = response.headers.get("content-type", "")
    if "application/json" in content_type:
        try:
            body = json.dumps(response.json(), ensure_ascii=False, indent=2)
        except Exception:
            body = response.text
    return f"Custom API `{tool_name}` HTTP {response.status_code}\n\n{body}"


async def execute_custom_api_tool(tool_name: str, arguments: dict, *, agent_id: uuid.UUID | None, execution_context=None) -> str:
    if agent_id is None:
        return f"Unknown tool: {tool_name}"
    try:
        tenant_id = await resolve_tenant_for_agent(agent_id)
        async with tenant_scoped_session(tenant_id) as db:
            result = await db.execute(
                select(Tool).where(Tool.name == tool_name, Tool.type == "custom_api", Tool.tenant_id == tenant_id)
            )
            tool = result.scalar_one_or_none()
            if not tool or not bool(getattr(tool, "enabled", True)):
                return f"Unknown tool: {tool_name}"
            agent_result = await db.execute(select(Agent).where(Agent.id == agent_id))
            agent = agent_result.scalar_one_or_none()
            if not agent or str(agent.tenant_id) != str(tenant_id):
                return f"Unknown tool: {tool_name}"
            at_result = await db.execute(
                select(AgentTool).where(AgentTool.agent_id == agent_id, AgentTool.tool_id == tool.id)
            )
            assignment = at_result.scalar_one_or_none()
            if assignment is not None and not bool(assignment.enabled):
                return f"❌ Custom API tool {tool_name} denied by this agent's tool assignment"
            if assignment is None and not bool(getattr(tool, "is_default", False)):
                return f"Unknown tool: {tool_name}"
            if execution_context is None:
                runtime_config = await resolve_tool_config(tool_name, tenant_id, agent_id=agent_id, db=db)
            else:
                from app.services.custom_api_accounts import resolve_account_config

                runtime_config = await resolve_account_config(db, tool=tool, agent_id=agent_id, context=execution_context)
        secret_config = {
            key: str(runtime_config.get(key) or "") for key in _SECRET_CONFIG_KEYS if runtime_config.get(key)
        }
        prepared = prepare_custom_api_request(
            tool_name=tool_name,
            tool_config=tool.config or {},
            secret_config=secret_config,
            arguments=arguments,
        )
        if execution_context is not None:
            from app.services.exact_secret_boundary import ExactSecretBoundary

            execution_context.exact_secret_boundary = ExactSecretBoundary.combine(execution_context.exact_secret_boundary,
                ExactSecretBoundary.from_pairs((f"custom-api-account://{tool.id}", value) for value in prepared.secret_values))
        return await execute_prepared_custom_api_request(tool_name, prepared)
    except HTTPException as exc:
        error_class = "custom_api_account_denied" if exc.status_code == 403 else "custom_api_account_unavailable" if exc.status_code == 404 else "custom_api_account_review_required"
        return render_tool_error(tool_name=tool_name, error_class=error_class, message=str(exc.detail),
            provider="custom_api", retryable=False, actionable_hint="Review the account and authorization in the original work or employee profile.")
    except CustomApiConnectorError as exc:
        return render_tool_error(
            tool_name=tool_name,
            error_class="custom_api_connector_error",
            message=str(exc),
            provider="custom_api",
            retryable=False,
            actionable_hint="Update the connector in Company Admin > Tools > Custom APIs.",
        )
