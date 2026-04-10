"""Provider-first model config, auth sessions, and model discovery."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from urllib.parse import urlencode
import uuid

import httpx
from cachetools import TTLCache
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings
from app.models.llm import LLMModel
from app.models.llm_provider_config import LLMProviderConfig
from app.services.llm_utils import get_provider_manifest, get_provider_spec, normalize_provider
from app.services.secrets_provider import get_secrets_provider

settings = get_settings()

_AUTH_SESSION_TTL_SECONDS = 600
_AUTH_SESSION_PREFIX = "llm_provider_auth:"
_auth_session_fallback: TTLCache[str, dict[str, Any]] = TTLCache(maxsize=10_000, ttl=_AUTH_SESSION_TTL_SECONDS)


class ProviderAdapter(Protocol):
    async def start_auth(self, **kwargs) -> dict[str, Any]: ...
    async def complete_auth(self, **kwargs) -> dict[str, Any]: ...
    async def poll_auth(self, **kwargs) -> dict[str, Any] | None: ...
    async def disconnect(self, **kwargs) -> bool: ...
    async def validate(self, **kwargs) -> dict[str, Any]: ...
    async def list_models(self, **kwargs) -> list[str]: ...


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.isoformat()


def _mask_secret(value: str | None) -> str:
    if not value:
        return ""
    return f"****{value[-4:]}" if len(value) > 4 else "****"


def _encrypt_secret(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return get_secrets_provider().encrypt(value)
    except Exception:
        return value


def _decrypt_secret(value: str | None) -> str | None:
    if not value:
        return None
    try:
        return get_secrets_provider().decrypt(value)
    except Exception:
        return value


async def _store_auth_session(session: dict[str, Any]) -> None:
    key = f"{_AUTH_SESSION_PREFIX}{session['provider']}:{session['session_id']}"
    try:
        from app.core.events import get_redis

        redis_client = await get_redis()
        await redis_client.set(key, json.dumps(session), ex=_AUTH_SESSION_TTL_SECONDS)
    except Exception:
        _auth_session_fallback[key] = session


async def _load_auth_session(provider: str, session_id: str) -> dict[str, Any] | None:
    key = f"{_AUTH_SESSION_PREFIX}{provider}:{session_id}"
    try:
        from app.core.events import get_redis

        redis_client = await get_redis()
        data = await redis_client.get(key)
        if isinstance(data, str):
            return json.loads(data)
    except Exception:
        pass
    return _auth_session_fallback.get(key)


class APIKeyProviderAdapter:
    async def start_auth(self, **kwargs) -> dict[str, Any]:
        raise HTTPException(status_code=400, detail="This provider only supports API keys")

    async def complete_auth(self, **kwargs) -> dict[str, Any]:
        raise HTTPException(status_code=400, detail="This provider does not support callback auth")

    async def poll_auth(self, **kwargs) -> dict[str, Any] | None:
        return None

    async def disconnect(self, **kwargs) -> bool:
        return True

    async def validate(self, **kwargs) -> dict[str, Any]:
        models = await self.list_models(**kwargs)
        return {"success": True, "models": models, "metadata": {}}

    async def list_models(self, **kwargs) -> list[str]:
        provider = kwargs["provider"]
        spec = get_provider_spec(provider)
        known_models = list(kwargs.get("known_models") or (spec.known_models if spec else []))
        base_url = kwargs.get("base_url") or (spec.default_base_url if spec else None)
        token = kwargs.get("api_key") or kwargs.get("access_token")
        if known_models:
            return known_models
        if not base_url or not token:
            return []
        if provider == "ollama":
            return await _list_ollama_models(base_url)
        return await _list_openai_compatible_models(base_url, token)


class OpenAILoginBridgeAdapter(APIKeyProviderAdapter):
    async def start_auth(self, **kwargs) -> dict[str, Any]:
        bridge_url = getattr(settings, "OPENAI_LOGIN_BRIDGE_URL", "")
        if not bridge_url:
            raise HTTPException(status_code=503, detail="OPENAI_LOGIN_BRIDGE_URL is not configured")
        session_id = kwargs["session"]["session_id"]
        callback_url = kwargs["callback_url"]
        query = urlencode({"session_id": session_id, "callback_url": callback_url})
        return {
            "connect_url": f"{bridge_url.rstrip('/')}?{query}",
            "provider_payload": {"bridge": True},
        }

    async def complete_auth(self, **kwargs) -> dict[str, Any]:
        query_params = kwargs["query_params"]
        token = query_params.get("token") or query_params.get("access_token")
        if not token:
            raise HTTPException(status_code=400, detail="OpenAI login bridge callback missing token")
        models = query_params.get("models") or "gpt-5.4"
        if isinstance(models, str):
            model_list = [item.strip() for item in models.split(",") if item.strip()]
        else:
            model_list = ["gpt-5.4"]
        return {
            "auth_mode": "login_bridge",
            "access_token": token,
            "refresh_token": query_params.get("refresh_token"),
            "oauth_subject": query_params.get("subject"),
            "oauth_email": query_params.get("email"),
            "models": model_list or ["gpt-5.4"],
            "provider_payload": {"bridge": True},
        }


class MiniMaxOAuthAdapter(APIKeyProviderAdapter):
    async def start_auth(self, **kwargs) -> dict[str, Any]:
        mode = kwargs["mode"]
        session = kwargs["session"]
        if mode == "oauth_web":
            authorize_url = getattr(settings, "MINIMAX_OAUTH_AUTHORIZE_URL", "")
            client_id = getattr(settings, "MINIMAX_OAUTH_CLIENT_ID", "")
            if not authorize_url or not client_id:
                raise HTTPException(status_code=503, detail="MiniMax OAuth web login is not configured")
            params = {
                "response_type": "code",
                "client_id": client_id,
                "redirect_uri": kwargs["callback_url"],
                "scope": getattr(settings, "MINIMAX_OAUTH_SCOPES", "openid profile"),
                "state": session["session_id"],
            }
            return {"connect_url": f"{authorize_url}?{urlencode(params)}"}

        if mode == "oauth_device":
            device_url = getattr(settings, "MINIMAX_OAUTH_DEVICE_URL", "")
            client_id = getattr(settings, "MINIMAX_OAUTH_CLIENT_ID", "")
            if not device_url or not client_id:
                raise HTTPException(status_code=503, detail="MiniMax device login is not configured")
            async with httpx.AsyncClient(timeout=20.0) as client:
                response = await client.post(
                    device_url,
                    data={
                        "client_id": client_id,
                        "scope": getattr(settings, "MINIMAX_OAUTH_SCOPES", "openid profile"),
                    },
                )
                response.raise_for_status()
                payload = response.json()
            return {
                "qr_url": payload.get("verification_uri_complete") or payload.get("qr_url"),
                "user_code": payload.get("user_code"),
                "provider_payload": {
                    "device_code": payload.get("device_code"),
                    "interval": payload.get("interval", 5),
                },
            }

        raise HTTPException(status_code=400, detail=f"Unsupported auth mode: {mode}")

    async def complete_auth(self, **kwargs) -> dict[str, Any]:
        query_params = kwargs["query_params"]
        code = query_params.get("code")
        if not code:
            raise HTTPException(status_code=400, detail="MiniMax callback missing code")
        token_url = getattr(settings, "MINIMAX_OAUTH_TOKEN_URL", "")
        client_id = getattr(settings, "MINIMAX_OAUTH_CLIENT_ID", "")
        client_secret = getattr(settings, "MINIMAX_OAUTH_CLIENT_SECRET", "")
        if not token_url or not client_id or not client_secret:
            raise HTTPException(status_code=503, detail="MiniMax OAuth token exchange is not configured")
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                token_url,
                data={
                    "grant_type": "authorization_code",
                    "code": code,
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "redirect_uri": kwargs["callback_url"],
                },
            )
            response.raise_for_status()
            payload = response.json()
        return {
            "auth_mode": "oauth_web",
            "access_token": payload.get("access_token"),
            "refresh_token": payload.get("refresh_token"),
            "oauth_subject": payload.get("sub"),
            "oauth_email": payload.get("email"),
            "models": payload.get("models") or [],
            "provider_payload": payload,
        }

    async def poll_auth(self, **kwargs) -> dict[str, Any] | None:
        session = kwargs["session"]
        provider_payload = dict(session.get("provider_payload") or {})
        device_code = provider_payload.get("device_code")
        token_url = getattr(settings, "MINIMAX_OAUTH_TOKEN_URL", "")
        client_id = getattr(settings, "MINIMAX_OAUTH_CLIENT_ID", "")
        if not device_code or not token_url or not client_id:
            return None
        async with httpx.AsyncClient(timeout=20.0) as client:
            response = await client.post(
                token_url,
                data={
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                    "device_code": device_code,
                    "client_id": client_id,
                },
            )
        if response.status_code >= 400:
            payload = response.json()
            if payload.get("error") in {"authorization_pending", "slow_down"}:
                return None
            raise HTTPException(status_code=400, detail=payload.get("error_description") or payload.get("error") or "MiniMax device login failed")
        payload = response.json()
        return {
            "auth_mode": "oauth_device",
            "access_token": payload.get("access_token"),
            "refresh_token": payload.get("refresh_token"),
            "oauth_subject": payload.get("sub"),
            "oauth_email": payload.get("email"),
            "models": payload.get("models") or [],
            "provider_payload": payload,
        }


async def _list_openai_compatible_models(base_url: str, token: str) -> list[str]:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/chat/completions"):
        normalized = normalized[: -len("/chat/completions")]
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(
            f"{normalized}/models",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
    models = []
    for item in payload.get("data", []):
        model_id = item.get("id")
        if isinstance(model_id, str) and model_id:
            models.append(model_id)
    return models


async def _list_ollama_models(base_url: str) -> list[str]:
    normalized = base_url.rstrip("/")
    if normalized.endswith("/v1"):
        normalized = normalized[: -len("/v1")]
    async with httpx.AsyncClient(timeout=20.0) as client:
        response = await client.get(f"{normalized}/api/tags")
        response.raise_for_status()
        payload = response.json()
    return [item["name"] for item in payload.get("models", []) if isinstance(item.get("name"), str)]


@dataclass
class LLMProviderConfigService:
    """Provider config CRUD, auth session orchestration, and model sync."""

    def get_adapter(self, provider: str) -> ProviderAdapter:
        provider = normalize_provider(provider)
        if provider == "openai":
            return OpenAILoginBridgeAdapter()
        if provider == "minimax":
            return MiniMaxOAuthAdapter()
        return APIKeyProviderAdapter()

    async def list_provider_configs(self, db: AsyncSession, tenant_id) -> list[dict[str, Any]]:
        config_result = await db.execute(
            select(LLMProviderConfig).where(LLMProviderConfig.tenant_id == tenant_id)
        )
        model_result = await db.execute(
            select(LLMModel).where(LLMModel.tenant_id == tenant_id).order_by(LLMModel.created_at.desc())
        )
        configs = {item.provider: item for item in config_result.scalars().all()}
        models = model_result.scalars().all()
        models_by_provider: dict[str, list[Any]] = {}
        for model in models:
            models_by_provider.setdefault(normalize_provider(model.provider), []).append(model)

        items: list[dict[str, Any]] = []
        for manifest_item in get_provider_manifest():
            provider = normalize_provider(manifest_item["provider"])
            config = configs.get(provider)
            provider_models = models_by_provider.get(provider, [])
            source = "saved" if config else "derived" if provider_models else "empty"
            items.append(self._serialize_provider_config(manifest_item, config, provider_models, source))
        return items

    async def get_provider_config(self, db: AsyncSession, tenant_id, provider: str) -> dict[str, Any]:
        provider = normalize_provider(provider)
        items = await self.list_provider_configs(db, tenant_id)
        for item in items:
            if item["provider"] == provider:
                return item
        raise HTTPException(status_code=404, detail="Provider not found")

    async def upsert_provider_config(self, db: AsyncSession, tenant_id, provider: str, payload: dict[str, Any]) -> dict[str, Any]:
        provider = normalize_provider(provider)
        spec = get_provider_spec(provider)
        if not spec:
            raise HTTPException(status_code=404, detail="Unsupported provider")
        result = await db.execute(
            select(LLMProviderConfig).where(
                LLMProviderConfig.tenant_id == tenant_id,
                LLMProviderConfig.provider == provider,
            )
        )
        config = result.scalar_one_or_none()
        if config is None:
            config = LLMProviderConfig(tenant_id=tenant_id, provider=provider)
            db.add(config)
        auth_mode = payload.get("auth_mode") or config.auth_mode or "api_key"
        if auth_mode not in spec.supported_auth_modes:
            raise HTTPException(status_code=422, detail=f"Unsupported auth mode for provider: {auth_mode}")

        config.auth_mode = auth_mode
        config.base_url = payload.get("base_url") or config.base_url or spec.default_base_url
        config.enabled = bool(payload.get("enabled", True))
        if payload.get("api_key"):
            config.api_key_encrypted = _encrypt_secret(payload["api_key"])
        if payload.get("access_token"):
            config.access_token_encrypted = _encrypt_secret(payload["access_token"])
        if payload.get("refresh_token"):
            config.refresh_token_encrypted = _encrypt_secret(payload["refresh_token"])
        if payload.get("oauth_subject") is not None:
            config.oauth_subject = payload.get("oauth_subject")
        if payload.get("oauth_email") is not None:
            config.oauth_email = payload.get("oauth_email")
        if payload.get("metadata_json"):
            merged = dict(config.metadata_json or {})
            merged.update(payload["metadata_json"])
            config.metadata_json = merged

        await db.flush()
        await db.commit()
        return self._serialize_provider_config(
            next(item for item in get_provider_manifest() if item["provider"] == provider),
            config,
            [],
            "saved",
        )

    async def verify_provider(self, db: AsyncSession, tenant_id, provider: str, payload: dict[str, Any]) -> dict[str, Any]:
        provider = normalize_provider(provider)
        existing = await self._load_saved_config(db, tenant_id, provider)
        adapter = self.get_adapter(provider)
        inputs = self._resolve_provider_inputs(provider, payload, existing)
        try:
            result = await adapter.validate(provider=provider, **inputs)
            models = result.get("models") or inputs["known_models"]
            return {
                "success": True,
                "models": models,
                "metadata": result.get("metadata", {}),
            }
        except HTTPException:
            raise
        except Exception as exc:
            return {
                "success": False,
                "models": inputs["known_models"],
                "error": str(exc),
                "metadata": {},
            }

    async def refresh_provider_models(self, db: AsyncSession, tenant_id, provider: str) -> dict[str, Any]:
        provider = normalize_provider(provider)
        config = await self._load_saved_config(db, tenant_id, provider)
        if config is None:
            raise HTTPException(status_code=404, detail="Provider config not found")
        spec = get_provider_spec(provider)
        adapter = self.get_adapter(provider)
        source_models = await adapter.list_models(
            provider=provider,
            auth_mode=config.auth_mode,
            api_key=_decrypt_secret(config.api_key_encrypted),
            access_token=_decrypt_secret(config.access_token_encrypted),
            base_url=config.base_url or (spec.default_base_url if spec else None),
            known_models=list((config.metadata_json or {}).get("discovered_models") or (spec.known_models if spec else [])),
        )
        if not source_models:
            source_models = list((config.metadata_json or {}).get("discovered_models") or (spec.known_models if spec else []))

        result = await db.execute(
            select(LLMModel).where(
                LLMModel.tenant_id == tenant_id,
                LLMModel.provider == provider,
            )
        )
        existing_models = list(result.scalars().all())
        by_exact_key = {(item.model, item.provider_config_id): item for item in existing_models}
        by_legacy_key = {(item.model, item.provider_config_id is None): item for item in existing_models}

        mirrored_secret = config.access_token_encrypted or config.api_key_encrypted or ""
        now = _utcnow()
        created = 0
        updated = 0
        discovered_set = set(source_models)

        for model_id in source_models:
            match = by_exact_key.get((model_id, config.id)) or by_legacy_key.get((model_id, True))
            if match:
                if match.provider_config_id != config.id or not match.discovered_from_provider or not match.enabled:
                    updated += 1
                match.provider_config_id = config.id
                match.discovered_from_provider = True
                match.enabled = True
                match.base_url = config.base_url
                if mirrored_secret:
                    match.api_key_encrypted = mirrored_secret
                continue

            db.add(
                LLMModel(
                    tenant_id=tenant_id,
                    provider=provider,
                    model=model_id,
                    api_key_encrypted=mirrored_secret,
                    base_url=config.base_url,
                    label=model_id,
                    max_tokens_per_day=None,
                    enabled=True,
                    supports_vision=False,
                    max_output_tokens=spec.default_max_tokens if spec else None,
                    max_input_tokens=spec.max_input_tokens if spec else None,
                    temperature=None,
                    provider_config_id=config.id,
                    discovered_from_provider=True,
                )
            )
            created += 1

        disabled = 0
        for item in existing_models:
            if item.provider_config_id == config.id and item.model not in discovered_set and item.enabled:
                item.enabled = False
                disabled += 1

        config.last_refreshed_at = now
        metadata = dict(config.metadata_json or {})
        metadata["discovered_models"] = list(source_models)
        config.metadata_json = metadata
        await db.flush()
        await db.commit()
        return {"created": created, "updated": updated, "disabled": disabled, "models": list(source_models)}

    async def start_auth(
        self,
        *,
        provider: str,
        tenant_id,
        user_id,
        mode: str,
        callback_url: str,
    ) -> dict[str, Any]:
        provider = normalize_provider(provider)
        spec = get_provider_spec(provider)
        if not spec:
            raise HTTPException(status_code=404, detail="Unsupported provider")
        if mode not in spec.supported_auth_modes:
            raise HTTPException(status_code=422, detail=f"Unsupported auth mode for provider: {mode}")
        session = {
            "session_id": secrets.token_urlsafe(18),
            "provider": provider,
            "tenant_id": str(tenant_id),
            "user_id": str(user_id),
            "mode": mode,
            "callback_url": callback_url,
            "connect_url": None,
            "qr_url": None,
            "user_code": None,
            "expires_at": _to_iso(_utcnow() + timedelta(seconds=_AUTH_SESSION_TTL_SECONDS)),
            "status": "pending",
            "provider_payload": {},
            "error": None,
        }
        adapter = self.get_adapter(provider)
        payload = await adapter.start_auth(provider=provider, mode=mode, session=session, callback_url=callback_url)
        session.update({
            "connect_url": payload.get("connect_url"),
            "qr_url": payload.get("qr_url"),
            "user_code": payload.get("user_code"),
            "provider_payload": payload.get("provider_payload", {}),
        })
        await _store_auth_session(session)
        return dict(session)

    async def get_auth_status(self, provider: str, session_id: str, db: AsyncSession | None = None) -> dict[str, Any]:
        provider = normalize_provider(provider)
        session = await _load_auth_session(provider, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Auth session not found")

        expires_at = datetime.fromisoformat(session["expires_at"])
        if expires_at <= _utcnow():
            session["status"] = "expired"
            await _store_auth_session(session)
            return session

        if session["status"] == "pending":
            adapter = self.get_adapter(provider)
            poll_auth = getattr(adapter, "poll_auth", None)
            result = await poll_auth(provider=provider, session=session) if callable(poll_auth) else None
            if result:
                await self._complete_auth_session(db, session, result)

        return session

    async def complete_auth_callback(
        self,
        db: AsyncSession,
        *,
        provider: str,
        session_id: str,
        query_params: dict[str, Any],
    ) -> dict[str, Any]:
        provider = normalize_provider(provider)
        session = await _load_auth_session(provider, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Auth session not found")
        result = await self.get_adapter(provider).complete_auth(
            provider=provider,
            session=session,
            query_params=query_params,
            callback_url=session["callback_url"],
        )
        await self._complete_auth_session(db, session, result)
        return session

    async def disconnect_provider_auth(self, db: AsyncSession, tenant_id, provider: str) -> dict[str, Any]:
        provider = normalize_provider(provider)
        config = await self._load_saved_config(db, tenant_id, provider)
        if config is None:
            return {"ok": False}
        ok = await self.get_adapter(provider).disconnect(provider=provider, config=config)
        if ok:
            config.access_token_encrypted = None
            config.refresh_token_encrypted = None
            config.oauth_subject = None
            config.oauth_email = None
            if config.auth_mode != "api_key":
                config.auth_mode = "api_key"
            await db.commit()
        return {"ok": ok}

    async def _load_saved_config(self, db: AsyncSession, tenant_id, provider: str) -> LLMProviderConfig | None:
        result = await db.execute(
            select(LLMProviderConfig).where(
                LLMProviderConfig.tenant_id == tenant_id,
                LLMProviderConfig.provider == provider,
            )
        )
        return result.scalar_one_or_none()

    def _resolve_provider_inputs(self, provider: str, payload: dict[str, Any], config: LLMProviderConfig | None) -> dict[str, Any]:
        spec = get_provider_spec(provider)
        metadata = dict((config.metadata_json if config else {}) or {})
        return {
            "auth_mode": payload.get("auth_mode") or (config.auth_mode if config else "api_key"),
            "api_key": payload.get("api_key") or _decrypt_secret(config.api_key_encrypted if config else None),
            "access_token": payload.get("access_token") or _decrypt_secret(config.access_token_encrypted if config else None),
            "base_url": payload.get("base_url") or (config.base_url if config else None) or (spec.default_base_url if spec else None),
            "known_models": list(metadata.get("discovered_models") or (spec.known_models if spec else [])),
        }

    def _serialize_provider_config(
        self,
        manifest_item: dict[str, Any],
        config: LLMProviderConfig | None,
        models: list[Any],
        source: str,
    ) -> dict[str, Any]:
        spec = get_provider_spec(manifest_item["provider"])
        latest_model = models[0] if models else None
        api_key_masked = ""
        base_url = manifest_item.get("default_base_url")
        auth_mode = "api_key"
        connected = False
        oauth_subject = None
        oauth_email = None
        enabled = True
        metadata_json = {}
        last_verified_at = None
        last_refreshed_at = None

        if config:
            api_key_masked = _mask_secret(
                config.access_token_encrypted or config.api_key_encrypted or config.refresh_token_encrypted
            )
            base_url = config.base_url or base_url
            auth_mode = config.auth_mode
            connected = bool(config.api_key_encrypted or config.access_token_encrypted)
            oauth_subject = config.oauth_subject
            oauth_email = config.oauth_email
            enabled = config.enabled
            metadata_json = dict(config.metadata_json or {})
            last_verified_at = _to_iso(config.last_verified_at)
            last_refreshed_at = _to_iso(config.last_refreshed_at)
        elif latest_model:
            api_key_masked = _mask_secret(latest_model.api_key_encrypted)
            base_url = latest_model.base_url or base_url
            connected = bool(latest_model.api_key_encrypted)

        return {
            "provider": manifest_item["provider"],
            "display_name": manifest_item["display_name"],
            "protocol": manifest_item["protocol"],
            "default_base_url": manifest_item.get("default_base_url"),
            "supports_tool_choice": manifest_item["supports_tool_choice"],
            "default_max_tokens": manifest_item["default_max_tokens"],
            "supported_auth_modes": manifest_item.get("supported_auth_modes", ["api_key"]),
            "supports_model_discovery": manifest_item.get("supports_model_discovery", True),
            "managed_login_label": manifest_item.get("managed_login_label"),
            "known_models": manifest_item.get("known_models", []),
            "source": source,
            "auth_mode": auth_mode,
            "enabled": enabled,
            "base_url": base_url,
            "api_key_masked": api_key_masked,
            "oauth_subject": oauth_subject,
            "oauth_email": oauth_email,
            "connected": connected,
            "model_count": len(models),
            "models": [self._serialize_model(model) for model in models],
            "has_auth_credentials": connected,
            "last_verified_at": last_verified_at,
            "last_refreshed_at": last_refreshed_at,
            "metadata_json": metadata_json,
            "max_input_tokens": spec.max_input_tokens if spec else manifest_item.get("max_input_tokens"),
        }

    def _serialize_model(self, model: Any) -> dict[str, Any]:
        return {
            "id": str(model.id),
            "provider": model.provider,
            "model": model.model,
            "label": model.label,
            "enabled": bool(model.enabled),
            "supports_vision": bool(getattr(model, "supports_vision", False)),
            "base_url": getattr(model, "base_url", None),
            "api_key_masked": _mask_secret(getattr(model, "api_key_encrypted", None)),
            "max_output_tokens": getattr(model, "max_output_tokens", None),
            "max_input_tokens": getattr(model, "max_input_tokens", None),
            "temperature": getattr(model, "temperature", None),
            "provider_config_id": str(getattr(model, "provider_config_id", "")) if getattr(model, "provider_config_id", None) else None,
            "discovered_from_provider": bool(getattr(model, "discovered_from_provider", False)),
            "created_at": _to_iso(getattr(model, "created_at", None)),
        }

    async def _complete_auth_session(self, db: AsyncSession | None, session: dict[str, Any], auth_result: dict[str, Any]) -> None:
        session["status"] = "completed"
        session["provider_payload"] = auth_result.get("provider_payload", {})
        session["auth_result"] = auth_result
        if db is not None:
            tenant_id = uuid.UUID(session["tenant_id"])
            provider = session["provider"]
            payload = {
                "auth_mode": auth_result.get("auth_mode", session["mode"]),
                "access_token": auth_result.get("access_token"),
                "refresh_token": auth_result.get("refresh_token"),
                "oauth_subject": auth_result.get("oauth_subject"),
                "oauth_email": auth_result.get("oauth_email"),
                "base_url": auth_result.get("base_url"),
                "enabled": True,
                "metadata_json": {"discovered_models": auth_result.get("models") or []},
            }
            await self.upsert_provider_config(db, tenant_id, provider, payload)
        await _store_auth_session(session)


llm_provider_service = LLMProviderConfigService()
