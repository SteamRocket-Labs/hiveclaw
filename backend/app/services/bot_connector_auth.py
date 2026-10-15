"""Inbound Bot Framework Connector authentication.

This checks the Bearer JWT on a public Teams activity before any configuration
or inbox effect. It is connector-service authentication: not user OIDC, and not
the bot's outbound client-credentials token.
"""

from __future__ import annotations

import base64
import json
import time
from datetime import datetime, timezone
from typing import Any, Protocol
from urllib.parse import urlsplit

import httpx
from authlib.jose import JsonWebToken
from authlib.jose.errors import JoseError

BOT_CONNECTOR_ISSUER = "https://api.botframework.com"
BOT_CONNECTOR_JWKS_HOST = "login.botframework.com"
OPENID_METADATA_URL = "https://login.botframework.com/v1/.well-known/openidconfiguration"
CLOCK_SKEW_SECONDS = 300
# Connector OpenID keys must be refreshed at least every 24 hours.
# https://learn.microsoft.com/en-us/azure/bot-service/rest-api/bot-framework-rest-connector-authentication?view=azure-bot-service-4.0
_KEY_REFRESH_SECONDS = 24 * 60 * 60
_JWT = JsonWebToken(["RS256"])


class BotConnectorAuthError(Exception):
    def __init__(self, code: str, *, retryable: bool = False) -> None:
        super().__init__(code)
        self.code = code
        self.retryable = retryable


class ConnectorKeySource(Protocol):
    async def get_key(self, *, kid: str | None, x5t: str | None) -> dict[str, Any]: ...


class StaticKeySource:
    """Test and fixture source. It does not discover or rotate keys."""

    def __init__(self, keys: list[dict[str, Any]]) -> None:
        self._keys = keys

    async def get_key(self, *, kid: str | None, x5t: str | None) -> dict[str, Any]:
        key = _match_key(self._keys, kid, x5t)
        if key is None:
            raise BotConnectorAuthError("bot_connector_unauthorized")
        return key


def _match_key(keys: list[dict[str, Any]], kid: str | None, x5t: str | None) -> dict[str, Any] | None:
    for key in keys:
        if kid and key.get("kid") == kid:
            return key
        if x5t and key.get("x5t") == x5t:
            return key
    return None


def _unauthorized() -> BotConnectorAuthError:
    return BotConnectorAuthError("bot_connector_unauthorized")


def _decode_part(segment: str) -> dict[str, Any]:
    padded = segment + "=" * (-len(segment) % 4)
    try:
        value = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except Exception as exc:
        raise _unauthorized() from exc
    if not isinstance(value, dict):
        raise _unauthorized()
    return value


def _service_url(value: object) -> str:
    text = str(value or "").strip()
    if text.endswith("/"):
        text = text[:-1]
    return text


def _endorsed(key: dict[str, Any], channel_id: object) -> bool:
    channel = str(channel_id or "").strip().lower()
    endorsements = key.get("endorsements")
    if not channel or not isinstance(endorsements, list) or not endorsements:
        return False
    return any(str(item).strip().lower() == channel for item in endorsements)


def _audience_matches(audience: object, app_id: str) -> bool:
    values = audience if isinstance(audience, list) else [audience]
    return app_id in {str(item) for item in values}


def _within_window(claims: dict[str, Any], now: datetime) -> bool:
    nbf = claims.get("nbf")
    exp = claims.get("exp")
    if isinstance(nbf, bool) or isinstance(exp, bool):
        return False
    if not isinstance(nbf, (int, float)) or not isinstance(exp, (int, float)):
        return False
    current = int(now.timestamp())
    return current + CLOCK_SKEW_SECONDS >= int(nbf) and current - CLOCK_SKEW_SECONDS < int(exp)


def _activity_service_url(claims: dict[str, Any], activity: dict[str, Any]) -> bool:
    claimed = claims.get("serviceurl", claims.get("serviceUrl"))
    if (
        "serviceurl" in claims
        and "serviceUrl" in claims
        and _service_url(claims["serviceurl"]) != _service_url(claims["serviceUrl"])
    ):
        return False
    return bool(_service_url(claimed)) and _service_url(claimed) == _service_url(activity.get("serviceUrl"))


async def _fetch_json(url: str) -> dict[str, Any]:
    parsed = urlsplit(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != BOT_CONNECTOR_JWKS_HOST:
        raise _unauthorized()
    try:
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as client:
            response = await client.get(url)
            response.raise_for_status()
            body = response.json()
    except BotConnectorAuthError:
        raise
    except Exception as exc:
        raise BotConnectorAuthError("bot_connector_key_discovery_unavailable", retryable=True) from exc
    if not isinstance(body, dict):
        raise BotConnectorAuthError("bot_connector_key_discovery_unavailable", retryable=True)
    return body


class OpenIdKeySource:
    """Pinned Connector metadata and JWKS.

    Known keys are refreshed at least every 24 hours. An unknown kid still
    triggers one extra fetch. A refresh that cannot be obtained does not keep
    serving the expired key.
    """

    def __init__(self, fetcher=None) -> None:
        self._fetcher = fetcher or _fetch_json
        self._keys: list[dict[str, Any]] | None = None
        self._loaded_at: float | None = None

    def _fresh(self) -> bool:
        return (
            bool(self._keys)
            and self._loaded_at is not None
            and time.monotonic() - self._loaded_at < _KEY_REFRESH_SECONDS
        )

    async def _load(self, *, refresh: bool) -> list[dict[str, Any]]:
        if self._keys is not None and not refresh and self._fresh():
            return self._keys
        try:
            document = await self._fetcher(OPENID_METADATA_URL)
            jwks_uri = str(document.get("jwks_uri") or "")
            host = (urlsplit(jwks_uri).hostname or "").lower()
            algorithms = document.get("id_token_signing_alg_values_supported") or []
            if (
                document.get("issuer") != BOT_CONNECTOR_ISSUER
                or host != BOT_CONNECTOR_JWKS_HOST
                or "RS256" not in algorithms
            ):
                raise _unauthorized()
            jwks = await self._fetcher(jwks_uri)
            keys = jwks.get("keys")
            if not isinstance(keys, list):
                raise _unauthorized()
        except BotConnectorAuthError:
            raise
        except Exception as exc:
            raise BotConnectorAuthError("bot_connector_key_discovery_unavailable", retryable=True) from exc
        self._keys = [key for key in keys if isinstance(key, dict)]
        self._loaded_at = time.monotonic()
        return self._keys

    async def get_key(self, *, kid: str | None, x5t: str | None) -> dict[str, Any]:
        had_keys = bool(self._keys)
        stale = not self._fresh()
        keys = await self._load(refresh=stale)
        key = _match_key(keys, kid, x5t)
        # A cold document that never contained the kid gets one refresh.
        # A document that just replaced a known key does not: the absence is
        # the revocation, and a second failed fetch must not hide it.
        if key is None and not (stale and had_keys):
            key = _match_key(await self._load(refresh=True), kid, x5t)
        if key is None:
            raise _unauthorized()
        return key


_key_source: OpenIdKeySource | None = None


def get_connector_key_source() -> OpenIdKeySource:
    global _key_source
    if _key_source is None:
        _key_source = OpenIdKeySource()
    return _key_source


async def authenticate_connector_activity(
    *,
    authorization: str | None,
    activity: dict[str, Any],
    app_id: str,
    key_source: ConnectorKeySource,
    now: datetime | None = None,
) -> None:
    """Raise BotConnectorAuthError when the activity is not a valid Connector JWT."""

    if not app_id or not authorization or not authorization.lower().startswith("bearer "):
        raise _unauthorized()
    token = authorization.split(" ", 1)[1].strip()
    if token.count(".") != 2:
        raise _unauthorized()
    header = _decode_part(token.split(".", 1)[0])
    if header.get("alg") != "RS256" or not (header.get("kid") or header.get("x5t")):
        raise _unauthorized()
    key = await key_source.get_key(kid=header.get("kid"), x5t=header.get("x5t"))
    verification_key = {name: value for name, value in key.items() if name != "endorsements"}
    try:
        claims = dict(_JWT.decode(token, verification_key))
    except (JoseError, ValueError, TypeError) as exc:
        raise _unauthorized() from exc
    if claims.get("iss") != BOT_CONNECTOR_ISSUER or not _audience_matches(claims.get("aud"), app_id):
        raise _unauthorized()
    if not _within_window(claims, now or datetime.now(timezone.utc)):
        raise _unauthorized()
    if not _activity_service_url(claims, activity) or not _endorsed(key, activity.get("channelId")):
        raise _unauthorized()
