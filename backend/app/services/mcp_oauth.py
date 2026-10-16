"""Standard OAuth2 (authorization-code + PKCE) for MCP servers — Step 7.

Hive previously had no standard OAuth2 for MCP (only Smithery-mediated auth_url
hand-offs). This module is the provider-neutral core: PKCE pair generation, the
authorization-URL builder, the token exchange/refresh calls, and encrypted
token-set storage.

Governance (守 mcp_authz):
* Tokens are stored **server-side, encrypted** via the shared
  ``SECRETS_MASTER_KEY`` secrets provider — never returned to an agent, never
  read from agent-supplied tool config (that path is rejected by
  ``assert_no_mcp_token_passthrough``). The agent's tool call carries no token;
  the runtime resolves the server's stored bearer at execution time.
* ``auth_status`` lifecycle on ``MCPServer``: ``none`` → (start: a pending PKCE
  challenge is stored) → ``configured`` (callback exchanged a token) →
  ``expired`` (refresh failed / token past expiry with no refresh) / ``error``.

Honesty boundary: callback/RLS, replay, encrypted migration and runtime bearer
consumption are checked locally with PostgreSQL and synthetic provider transport.
They are not live-verified against a real OAuth MCP server. An MCP server with a
pending/expired OAuth status is **fail-closed** (calls return "authorization
required", never an unauthenticated request).
"""

from __future__ import annotations

import base64
import hashlib
import json
import math
import secrets
import time
from dataclasses import dataclass, field
from urllib.parse import urlencode

import httpx

# auth_status enum values on MCPServer (mirror models/mcp_server.py).
AUTH_NONE = "none"
AUTH_CONFIGURED = "configured"
AUTH_EXPIRED = "expired"
AUTH_ERROR = "error"

_ENCRYPTED_SECRET_PREFIX = "enc:v1:"  # shared convention with tool_config_service
# Refresh a token slightly before it actually expires to avoid edge-of-expiry calls.
_EXPIRY_SKEW_SECONDS = 60


# ── PKCE + authorization URL (pure) ───────────────────────────────────────────


def generate_pkce_pair() -> tuple[str, str]:
    """Return ``(code_verifier, code_challenge)`` for PKCE S256 (RFC 7636)."""
    verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return verifier, challenge


def generate_state() -> str:
    """CSRF state token for the authorization request."""
    return secrets.token_urlsafe(32)


def build_authorization_url(
    *,
    authorization_endpoint: str,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
    scope: str | None = None,
) -> str:
    """Build the authorization-code + PKCE authorization URL."""
    params = {
        "response_type": "code",
        "client_id": client_id,
        "redirect_uri": redirect_uri,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
    }
    if scope:
        params["scope"] = scope
    sep = "&" if "?" in authorization_endpoint else "?"
    return f"{authorization_endpoint}{sep}{urlencode(params)}"


# ── Token set ─────────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OAuthTokenSet:
    access_token: str
    token_type: str = "Bearer"
    refresh_token: str | None = None
    expires_at: float | None = None  # epoch seconds
    scope: str | None = None

    def is_expired(self, now: float) -> bool:
        if self.expires_at is None:
            return False
        return now >= (self.expires_at - _EXPIRY_SKEW_SECONDS)

    def to_dict(self) -> dict:
        return {
            "access_token": self.access_token,
            "token_type": self.token_type,
            "refresh_token": self.refresh_token,
            "expires_at": self.expires_at,
            "scope": self.scope,
        }

    @classmethod
    def from_token_response(cls, data: dict, *, now: float) -> "OAuthTokenSet":
        access_token = data.get("access_token")
        token_type = data.get("token_type", "Bearer")
        if (
            not isinstance(access_token, str)
            or not access_token
            or not isinstance(token_type, str)
            or token_type.lower() != "bearer"
        ):
            raise OAuthError("OAuth token response is unusable")
        if data.get("refresh_token") is not None and not isinstance(data["refresh_token"], str):
            raise OAuthError("OAuth refresh token response is unusable")
        expires_in = data.get("expires_in")
        expires_at = None
        if expires_in is not None:
            try:
                seconds = float(expires_in)
                if isinstance(expires_in, bool) or not math.isfinite(seconds) or seconds < 0:
                    raise ValueError
                expires_at = now + seconds
            except (TypeError, ValueError, OverflowError) as exc:
                raise OAuthError("OAuth token expiry response is unusable") from exc
        return cls(
            access_token=access_token,
            token_type=token_type,
            refresh_token=data.get("refresh_token"),
            expires_at=expires_at,
            scope=data.get("scope"),
        )


# ── Encrypted storage (round-trips through the shared secrets provider) ────────


def _secrets_provider():
    """The shared provider; absence must never permit a new plaintext write."""
    try:
        from app.services.secrets_provider import get_secrets_provider

        return get_secrets_provider()
    except RuntimeError:
        return None


def encrypt_token_set(token: OAuthTokenSet) -> str:
    """Serialize + encrypt a token set for storage in ``MCPServer.config_json``."""
    return encrypt_value(json.dumps(token.to_dict(), ensure_ascii=False))


def decrypt_token_set(blob: str | None) -> OAuthTokenSet | None:
    """Inverse of :func:`encrypt_token_set`. Returns None on missing/garbage."""
    if not blob or not isinstance(blob, str):
        return None
    raw = decrypt_value(blob)
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("access_token"), str) or not data["access_token"]:
        return None
    expiry = data.get("expires_at")
    if expiry is not None and (not isinstance(expiry, (float, int)) or not math.isfinite(expiry)):
        return None
    if data.get("refresh_token") is not None and not isinstance(data["refresh_token"], str):
        return None
    return OAuthTokenSet(
        access_token=str(data["access_token"]),
        token_type=str(data.get("token_type", "Bearer")) or "Bearer",
        refresh_token=data.get("refresh_token"),
        expires_at=data.get("expires_at"),
        scope=data.get("scope"),
    )


def encrypt_value(value: str) -> str:
    """Encrypt a single secret string (PKCE verifier, client_secret) for storage."""
    if not value:
        return value
    provider = _secrets_provider()
    if provider is None or provider.key_id == "development-noop":
        raise OAuthError("OAuth encrypted storage is unavailable", outcome="unavailable")
    ciphertext = provider.encrypt(value)
    if not ciphertext or ciphertext == value:
        raise OAuthError("OAuth encrypted storage is unavailable", outcome="unavailable")
    return _ENCRYPTED_SECRET_PREFIX + ciphertext


def decrypt_value(value: str | None) -> str | None:
    if not value or not isinstance(value, str):
        return value
    if not value.startswith(_ENCRYPTED_SECRET_PREFIX):
        return value
    provider = _secrets_provider()
    if provider is None or provider.key_id == "development-noop":
        return None  # tagged but no provider → cannot recover, fail-closed
    from cryptography.fernet import InvalidToken

    try:
        return provider.decrypt_strict(value[len(_ENCRYPTED_SECRET_PREFIX) :])
    except (InvalidToken, ValueError, UnicodeError):
        return None


def encrypt_legacy_oauth_config(config: dict, provider) -> dict:
    """Encrypt known legacy fields without hiding the nonsecret JSON control state."""
    oauth = config.get("oauth")
    if not oauth:
        return config
    if not isinstance(oauth, dict):
        raise ValueError("Malformed MCP OAuth configuration blocks secret migration")
    oauth = dict(oauth)
    pending = oauth.get("pending")
    if pending is not None and not isinstance(pending, dict):
        raise ValueError("Malformed MCP OAuth attempt blocks secret migration")
    containers = [(oauth, ("token", "client_secret"))]
    if pending:
        pending = dict(pending)
        oauth["pending"] = pending
        containers.append((pending, ("code_verifier", "client_secret")))
    for container, keys in containers:
        for key in keys:
            value = container.get(key)
            if value in (None, ""):
                continue
            if not isinstance(value, str):
                raise ValueError("Malformed MCP OAuth secret blocks migration")
            if value.startswith(_ENCRYPTED_SECRET_PREFIX):
                provider.decrypt_strict(value[len(_ENCRYPTED_SECRET_PREFIX) :])
                continue
            encrypted = provider.encrypt(value)
            if encrypted == value or not encrypted:
                raise ValueError("MCP OAuth migration requires encrypted storage")
            container[key] = _ENCRYPTED_SECRET_PREFIX + encrypted
    return {**config, "oauth": oauth}


# ── Token endpoint calls (network) ────────────────────────────────────────────


@dataclass(frozen=True)
class OAuthError(Exception):
    message: str
    status_code: int = 0
    details: dict = field(default_factory=dict)
    outcome: str = "unknown"

    def __str__(self) -> str:
        return self.message


class OAuthFlowError(ValueError):
    def __init__(self, code: str, outcome: str, status_code: int = 400):
        self.status_code = status_code
        self.detail = {
            "code": code,
            "outcome": outcome,
            "recovery": "Check authorization status or start a new authorization.",
        }
        super().__init__(code)


async def exchange_code_for_token(
    *,
    token_endpoint: str,
    client_id: str,
    code: str,
    redirect_uri: str,
    code_verifier: str,
    client_secret: str | None = None,
    now: float | None = None,
) -> OAuthTokenSet:
    """Exchange an authorization code for an access token (PKCE)."""
    payload = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": redirect_uri,
        "client_id": client_id,
        "code_verifier": code_verifier,
    }
    if client_secret:
        payload["client_secret"] = client_secret
    return await _post_token(token_endpoint, payload, now=now)


async def refresh_access_token(
    *,
    token_endpoint: str,
    client_id: str,
    refresh_token: str,
    client_secret: str | None = None,
    now: float | None = None,
) -> OAuthTokenSet:
    """Refresh an access token using a refresh token."""
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": refresh_token,
        "client_id": client_id,
    }
    if client_secret:
        payload["client_secret"] = client_secret
    token = await _post_token(token_endpoint, payload, now=now)
    # Some providers omit refresh_token on refresh — keep the prior one.
    if token.refresh_token is None:
        token = OAuthTokenSet(
            access_token=token.access_token,
            token_type=token.token_type,
            refresh_token=refresh_token,
            expires_at=token.expires_at,
            scope=token.scope,
        )
    return token


async def _post_token(token_endpoint: str, payload: dict, *, now: float | None) -> OAuthTokenSet:
    stamp = time.time() if now is None else now
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                token_endpoint,
                data=payload,
                headers={"Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded"},
            )
    except httpx.HTTPError as exc:
        raise OAuthError(message="OAuth token request outcome is unknown") from exc
    if resp.status_code >= 400:
        try:
            error = resp.json().get("error")
        except (TypeError, ValueError, AttributeError):
            error = None
        rejected = resp.status_code in (400, 401) and error in {
            "invalid_grant",
            "invalid_client",
            "unauthorized_client",
            "invalid_request",
            "unsupported_grant_type",
            "invalid_scope",
        }
        raise OAuthError(
            message=f"OAuth token endpoint returned {resp.status_code}",
            status_code=resp.status_code,
            outcome="failed" if rejected else "unknown",
        )
    try:
        data = resp.json()
    except (TypeError, ValueError) as exc:
        raise OAuthError(message="OAuth token endpoint returned non-JSON body") from exc
    if not isinstance(data, dict) or not data.get("access_token"):
        raise OAuthError(message="OAuth token response missing access_token")
    return OAuthTokenSet.from_token_response(data, now=stamp)
