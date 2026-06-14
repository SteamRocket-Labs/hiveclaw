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

Honesty boundary: the interactive end-to-end flow is unit-verified (PKCE, URL
build, exchange/refresh with mocked transport, encrypt/decrypt round-trip). It is
not live-verified against a real OAuth MCP server — and an MCP server with a
pending/expired OAuth status is **fail-closed** (calls return "authorization
required", never an unauthenticated request).
"""

from __future__ import annotations

import base64
import hashlib
import json
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
        expires_in = data.get("expires_in")
        expires_at = (now + float(expires_in)) if isinstance(expires_in, (int, float)) else None
        return cls(
            access_token=str(data.get("access_token", "")),
            token_type=str(data.get("token_type", "Bearer")) or "Bearer",
            refresh_token=data.get("refresh_token"),
            expires_at=expires_at,
            scope=data.get("scope"),
        )


# ── Encrypted storage (round-trips through the shared secrets provider) ────────


def _secrets_provider():
    """The shared secrets provider, or None when none is configured (dev / no
    master key / not yet initialized) — in which case storage is plaintext, the
    same zero-migration no-op behavior as ``tool_config_service``."""
    try:
        from app.services.secrets_provider import get_secrets_provider

        return get_secrets_provider()
    except RuntimeError:
        return None


def encrypt_token_set(token: OAuthTokenSet) -> str:
    """Serialize + encrypt a token set for storage in ``MCPServer.config_json``."""
    plaintext = json.dumps(token.to_dict(), ensure_ascii=False)
    provider = _secrets_provider()
    if provider is None:
        return plaintext
    ciphertext = provider.encrypt(plaintext)
    if ciphertext == plaintext:  # no-op provider (dev / no master key)
        return plaintext
    return _ENCRYPTED_SECRET_PREFIX + ciphertext


def decrypt_token_set(blob: str | None) -> OAuthTokenSet | None:
    """Inverse of :func:`encrypt_token_set`. Returns None on missing/garbage."""
    if not blob or not isinstance(blob, str):
        return None
    raw = blob
    if blob.startswith(_ENCRYPTED_SECRET_PREFIX):
        provider = _secrets_provider()
        if provider is None:
            return None  # tagged ciphertext but no provider → unusable, fail-closed
        raw = provider.decrypt(blob[len(_ENCRYPTED_SECRET_PREFIX) :])
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("access_token"):
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
    if provider is None:
        return value
    ciphertext = provider.encrypt(value)
    if ciphertext == value:  # no-op provider
        return value
    return _ENCRYPTED_SECRET_PREFIX + ciphertext


def decrypt_value(value: str | None) -> str | None:
    if not value or not isinstance(value, str):
        return value
    if not value.startswith(_ENCRYPTED_SECRET_PREFIX):
        return value
    provider = _secrets_provider()
    if provider is None:
        return None  # tagged but no provider → cannot recover, fail-closed
    return provider.decrypt(value[len(_ENCRYPTED_SECRET_PREFIX) :])


# ── Token endpoint calls (network) ────────────────────────────────────────────


@dataclass(frozen=True)
class OAuthError(Exception):
    message: str
    status_code: int = 0
    details: dict = field(default_factory=dict)

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.message


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
        raise OAuthError(message=f"OAuth token request failed: {str(exc)[:200]}") from exc
    if resp.status_code >= 400:
        raise OAuthError(
            message=f"OAuth token endpoint returned {resp.status_code}",
            status_code=resp.status_code,
            details={"body": resp.text[:500]},
        )
    try:
        data = resp.json()
    except (TypeError, ValueError) as exc:
        raise OAuthError(message="OAuth token endpoint returned non-JSON body") from exc
    if not isinstance(data, dict) or not data.get("access_token"):
        raise OAuthError(message="OAuth token response missing access_token", details={"body": str(data)[:300]})
    return OAuthTokenSet.from_token_response(data, now=stamp)
