"""Tests for the MCP OAuth2 core (Step 7).

Covers the verifiable security logic: PKCE S256, authorization-URL build, token
set + expiry, encrypted storage round-trip, and the token exchange/refresh with a
mocked transport. The interactive end-to-end flow against a live OAuth MCP server
is not exercised here (no such server in CI) — see mcp_oauth module docstring.
"""

from __future__ import annotations

import base64
import hashlib

import pytest

from app.services import mcp_oauth
from app.services.mcp_oauth import (
    AUTH_CONFIGURED,
    OAuthError,
    OAuthTokenSet,
    build_authorization_url,
    decrypt_token_set,
    decrypt_value,
    encrypt_token_set,
    encrypt_value,
    exchange_code_for_token,
    generate_pkce_pair,
    generate_state,
    refresh_access_token,
)


def test_pkce_pair_is_valid_s256():
    verifier, challenge = generate_pkce_pair()
    assert 43 <= len(verifier) <= 128
    expected = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    assert challenge == expected
    assert "=" not in challenge  # base64url, no padding


def test_pkce_pairs_are_unique():
    assert generate_pkce_pair()[0] != generate_pkce_pair()[0]
    assert generate_state() != generate_state()


def test_build_authorization_url():
    url = build_authorization_url(
        authorization_endpoint="https://auth.example.com/authorize",
        client_id="cid",
        redirect_uri="https://hive/cb",
        state="st8",
        code_challenge="chal",
        scope="read write",
    )
    assert url.startswith("https://auth.example.com/authorize?")
    assert "response_type=code" in url
    assert "code_challenge=chal" in url
    assert "code_challenge_method=S256" in url
    assert "client_id=cid" in url
    assert "state=st8" in url
    assert "scope=read+write" in url


def test_build_authorization_url_appends_to_existing_query():
    url = build_authorization_url(
        authorization_endpoint="https://auth.example.com/authorize?foo=1",
        client_id="cid",
        redirect_uri="https://hive/cb",
        state="s",
        code_challenge="c",
    )
    assert "?foo=1&" in url


def test_token_set_expiry():
    assert OAuthTokenSet(access_token="t", expires_at=None).is_expired(1_000_000) is False
    # expires_at minus 60s skew
    tok = OAuthTokenSet(access_token="t", expires_at=1000.0)
    assert tok.is_expired(900) is False
    assert tok.is_expired(950) is True  # within 60s skew window
    assert tok.is_expired(2000) is True


def test_token_set_from_response():
    tok = OAuthTokenSet.from_token_response(
        {"access_token": "a", "refresh_token": "r", "expires_in": 3600, "token_type": "Bearer", "scope": "x"},
        now=1000.0,
    )
    assert tok.access_token == "a"
    assert tok.refresh_token == "r"
    assert tok.expires_at == 4600.0
    assert tok.scope == "x"


def test_encrypt_decrypt_token_set_round_trip():
    tok = OAuthTokenSet(access_token="secret-at", refresh_token="secret-rt", expires_at=123.0, scope="s")
    blob = encrypt_token_set(tok)
    restored = decrypt_token_set(blob)
    assert restored is not None
    assert restored.access_token == "secret-at"
    assert restored.refresh_token == "secret-rt"
    assert restored.expires_at == 123.0


def test_decrypt_token_set_handles_garbage():
    assert decrypt_token_set(None) is None
    assert decrypt_token_set("") is None
    assert decrypt_token_set("not-json") is None
    assert decrypt_token_set("{}") is None  # no access_token


def test_encrypt_decrypt_value_round_trip():
    assert decrypt_value(encrypt_value("verifier-123")) == "verifier-123"
    assert encrypt_value("") == ""
    assert decrypt_value(None) is None


class _FakeResponse:
    def __init__(self, status_code: int, payload: dict | None = None, text: str = ""):
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self):
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class _FakeAsyncClient:
    def __init__(self, response: _FakeResponse):
        self._response = response
        self.last_post: dict | None = None

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, data=None, headers=None):
        self.last_post = {"url": url, "data": data}
        return self._response


@pytest.mark.asyncio
async def test_exchange_code_for_token(monkeypatch):
    fake = _FakeAsyncClient(
        _FakeResponse(200, {"access_token": "at", "refresh_token": "rt", "expires_in": 3600, "token_type": "Bearer"})
    )
    monkeypatch.setattr(mcp_oauth.httpx, "AsyncClient", lambda *a, **k: fake)
    tok = await exchange_code_for_token(
        token_endpoint="https://t",
        client_id="cid",
        code="thecode",
        redirect_uri="https://cb",
        code_verifier="ver",
        now=1000.0,
    )
    assert tok.access_token == "at"
    assert tok.expires_at == 4600.0
    assert fake.last_post["data"]["grant_type"] == "authorization_code"
    assert fake.last_post["data"]["code_verifier"] == "ver"


@pytest.mark.asyncio
async def test_exchange_raises_on_http_error(monkeypatch):
    fake = _FakeAsyncClient(_FakeResponse(400, None, text="bad request"))
    monkeypatch.setattr(mcp_oauth.httpx, "AsyncClient", lambda *a, **k: fake)
    with pytest.raises(OAuthError):
        await exchange_code_for_token(
            token_endpoint="https://t", client_id="c", code="x", redirect_uri="https://cb", code_verifier="v"
        )


@pytest.mark.asyncio
async def test_refresh_keeps_prior_refresh_token_when_omitted(monkeypatch):
    fake = _FakeAsyncClient(_FakeResponse(200, {"access_token": "new-at", "expires_in": 3600}))
    monkeypatch.setattr(mcp_oauth.httpx, "AsyncClient", lambda *a, **k: fake)
    tok = await refresh_access_token(
        token_endpoint="https://t", client_id="c", refresh_token="orig-rt", now=1000.0
    )
    assert tok.access_token == "new-at"
    assert tok.refresh_token == "orig-rt"  # provider omitted it → prior kept
    assert AUTH_CONFIGURED == "configured"
