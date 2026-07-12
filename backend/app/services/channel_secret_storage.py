"""Versioned encrypted-at-rest storage for channel credentials."""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any
import uuid

from cryptography.fernet import InvalidToken
from sqlalchemy import JSON, String, select, text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from app.services.secrets_provider import SecretsProvider, get_secrets_provider


CHANNEL_SECRET_PREFIX = "hive:channel-secret:v1:"
CHANNEL_EXTRA_SECRET_KEYS = frozenset(
    {
        "api_key",
        "app_secret",
        "bot_secret",
        "bot_token",
        "client_secret",
        "ilink_bot_token",
        "signing_secret",
    }
)
CHANNEL_SECRET_TABLES = (
    ("channel_configs", "json"),
    ("tenant_channel_configs", "jsonb"),
)


class ChannelSecretDecryptionError(RuntimeError):
    """A versioned channel credential cannot be decrypted by the configured keyring."""


class ChannelSecretStorageError(RuntimeError):
    """A channel credential cannot be stored without real at-rest encryption."""


def _provider(provider: SecretsProvider | None) -> SecretsProvider:
    return provider or get_secrets_provider()


def _split_envelope(value: str) -> tuple[str, str]:
    payload = value[len(CHANNEL_SECRET_PREFIX) :]
    key_id, separator, token = payload.partition(":")
    if not separator or not key_id or not token:
        raise ChannelSecretDecryptionError("Malformed channel secret envelope")
    return key_id, token


def channel_secret_key_id(value: str | None) -> str | None:
    if not value or not value.startswith(CHANNEL_SECRET_PREFIX):
        return None
    return _split_envelope(value)[0]


def encrypt_channel_secret(value: str | None, *, provider: SecretsProvider | None = None) -> str | None:
    if not value:
        return value
    selected = _provider(provider)
    if selected.key_id == "development-noop":
        raise ChannelSecretStorageError(
            "Channel credentials require an encrypted secrets provider; configure SECRETS_MASTER_KEY"
        )
    if value.startswith(CHANNEL_SECRET_PREFIX):
        return rotate_channel_secret(value, provider=selected)
    return f"{CHANNEL_SECRET_PREFIX}{selected.key_id}:{selected.encrypt(value)}"


def decrypt_channel_secret(value: str | None, *, provider: SecretsProvider | None = None) -> str | None:
    if not value or not value.startswith(CHANNEL_SECRET_PREFIX):
        return value
    _stored_key_id, token = _split_envelope(value)
    try:
        return _provider(provider).decrypt_strict(token)
    except (InvalidToken, ValueError, UnicodeError) as exc:
        raise ChannelSecretDecryptionError("Channel secret cannot be decrypted by the configured keyring") from exc


def rotate_channel_secret(value: str | None, *, provider: SecretsProvider | None = None) -> str | None:
    if not value:
        return value
    selected = _provider(provider)
    if not value.startswith(CHANNEL_SECRET_PREFIX):
        return encrypt_channel_secret(value, provider=selected)
    if channel_secret_key_id(value) == selected.key_id:
        return value
    plaintext = decrypt_channel_secret(value, provider=selected)
    return encrypt_channel_secret(plaintext, provider=selected)


def _transform_extra(value: Any, *, encrypt: bool, provider: SecretsProvider) -> Any:
    if isinstance(value, dict):
        transformed: dict[str, Any] = {}
        for key, item in value.items():
            if key.casefold() in CHANNEL_EXTRA_SECRET_KEYS and isinstance(item, str):
                transformed[key] = (
                    encrypt_channel_secret(item, provider=provider)
                    if encrypt
                    else decrypt_channel_secret(item, provider=provider)
                )
            else:
                transformed[key] = _transform_extra(item, encrypt=encrypt, provider=provider)
        return transformed
    if isinstance(value, list):
        return [_transform_extra(item, encrypt=encrypt, provider=provider) for item in value]
    return value


def encrypt_channel_extra_config(
    value: dict[str, Any] | None,
    *,
    provider: SecretsProvider | None = None,
) -> dict[str, Any] | None:
    if value is None:
        return None
    return _transform_extra(deepcopy(value), encrypt=True, provider=_provider(provider))


def decrypt_channel_extra_config(
    value: dict[str, Any] | None,
    *,
    provider: SecretsProvider | None = None,
) -> dict[str, Any] | None:
    if value is None:
        return None
    return _transform_extra(deepcopy(value), encrypt=False, provider=_provider(provider))


def scrub_channel_extra_config(value: dict[str, Any] | None) -> dict[str, Any]:
    def scrub(item: Any) -> Any:
        if isinstance(item, dict):
            return {key: scrub(child) for key, child in item.items() if key.casefold() not in CHANNEL_EXTRA_SECRET_KEYS}
        if isinstance(item, list):
            return [scrub(child) for child in item]
        return item

    return scrub(deepcopy(value or {}))


def redact_channel_extra_config(
    value: dict[str, Any] | None,
    *,
    reveal_suffix: bool = False,
) -> dict[str, Any]:
    """Mask nested credential values for API/audit projections."""

    def redact(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                key: (
                    f"****{child[-4:]}"
                    if reveal_suffix and key.casefold() in CHANNEL_EXTRA_SECRET_KEYS and len(str(child)) > 4
                    else "****"
                    if key.casefold() in CHANNEL_EXTRA_SECRET_KEYS and child
                    else redact(child)
                )
                for key, child in item.items()
            }
        if isinstance(item, list):
            return [redact(child) for child in item]
        return item

    return redact(deepcopy(value or {}))


def _iter_extra_secret_values(value: Any):
    if isinstance(value, dict):
        for key, item in value.items():
            if key.casefold() in CHANNEL_EXTRA_SECRET_KEYS and isinstance(item, str) and item:
                yield item
            else:
                yield from _iter_extra_secret_values(item)
    elif isinstance(value, list):
        for item in value:
            yield from _iter_extra_secret_values(item)


def inspect_channel_secret_rows(bind, *, current_key_id: str | None = None) -> dict[str, Any]:
    """Return count-only storage evidence; never include credential values."""
    tables: dict[str, dict[str, int]] = {}
    totals = {"rows": 0, "secret_values": 0, "plaintext": 0, "encrypted": 0, "non_current": 0}
    for table_name, _json_type in CHANNEL_SECRET_TABLES:
        rows = bind.execute(
            text(
                f"SELECT id, app_secret, encrypt_key, verification_token, extra_config FROM {table_name}"  # noqa: S608
            )
        ).mappings()
        report = {"rows": 0, "secret_values": 0, "plaintext": 0, "encrypted": 0, "non_current": 0}
        for row in rows:
            report["rows"] += 1
            values = [row[field] for field in ("app_secret", "encrypt_key", "verification_token") if row[field]]
            values.extend(_iter_extra_secret_values(row["extra_config"] or {}))
            for value in values:
                report["secret_values"] += 1
                key_id = channel_secret_key_id(value)
                if key_id is None:
                    report["plaintext"] += 1
                else:
                    report["encrypted"] += 1
                    if current_key_id is not None and key_id != current_key_id:
                        report["non_current"] += 1
        tables[table_name] = report
        for key in totals:
            totals[key] += report[key]
    return {"schema": "hive.channel_secret_inventory.v1", "tables": tables, "totals": totals}


def migrate_channel_secret_rows(bind, *, provider: SecretsProvider, apply: bool) -> dict[str, Any]:
    """Dry-run or encrypt/rotate every persisted channel credential in place."""
    before = inspect_channel_secret_rows(bind, current_key_id=provider.key_id)
    if not apply:
        return {**before, "mode": "dry_run", "rewritten_rows": 0}

    rewritten_rows = 0
    for table_name, json_type in CHANNEL_SECRET_TABLES:
        rows = bind.execute(
            text(
                f"SELECT id, app_secret, encrypt_key, verification_token, extra_config FROM {table_name}"  # noqa: S608
            )
        ).mappings()
        for row in rows:
            values = {
                field: rotate_channel_secret(row[field], provider=provider)
                for field in ("app_secret", "encrypt_key", "verification_token")
            }
            extra_config = encrypt_channel_extra_config(row["extra_config"] or {}, provider=provider)
            changed = any(values[field] != row[field] for field in values) or extra_config != (
                row["extra_config"] or {}
            )
            if not changed:
                continue
            bind.execute(
                text(
                    f"UPDATE {table_name} SET "  # noqa: S608
                    "app_secret = :app_secret, encrypt_key = :encrypt_key, "
                    "verification_token = :verification_token, "
                    f"extra_config = CAST(:extra_config AS {json_type}) WHERE id = :id"
                ),
                {**values, "extra_config": json.dumps(extra_config), "id": row["id"]},
            )
            rewritten_rows += 1

    after = inspect_channel_secret_rows(bind, current_key_id=provider.key_id)
    if after["totals"]["plaintext"] or after["totals"]["non_current"]:
        raise RuntimeError("channel credential migration verification failed")
    return {**after, "mode": "apply", "rewritten_rows": rewritten_rows, "before": before["totals"]}


class EncryptedChannelSecret(TypeDecorator[str]):
    """SQLAlchemy string type that encrypts writes and decrypts reads."""

    impl = String
    cache_ok = True

    def __init__(self, length: int = 1024) -> None:
        super().__init__(length=length)

    def process_bind_param(self, value: str | None, dialect: Dialect) -> str | None:
        del dialect
        return encrypt_channel_secret(value)

    def process_result_value(self, value: str | None, dialect: Dialect) -> str | None:
        del dialect
        return decrypt_channel_secret(value)


class EncryptedChannelJSON(TypeDecorator[dict[str, Any]]):
    """JSON/JSONB type that protects known credential keys recursively."""

    impl = JSON
    cache_ok = True

    def __init__(self, *, postgres_jsonb: bool = False) -> None:
        super().__init__()
        self.postgres_jsonb = postgres_jsonb

    def load_dialect_impl(self, dialect: Dialect):
        if dialect.name == "postgresql" and self.postgres_jsonb:
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())

    def process_bind_param(self, value: dict[str, Any] | None, dialect: Dialect) -> dict[str, Any] | None:
        del dialect
        return encrypt_channel_extra_config(value)

    def process_result_value(self, value: dict[str, Any] | None, dialect: Dialect) -> dict[str, Any] | None:
        del dialect
        return decrypt_channel_extra_config(value)

    def copy(self, **kw):
        del kw
        return EncryptedChannelJSON(postgres_jsonb=self.postgres_jsonb)


async def scrub_tenant_channel_secrets(db, tenant_id: uuid.UUID) -> dict[str, int]:
    """Remove all channel credentials when a tenant is deactivated."""
    from app.models.channel_config import ChannelConfig
    from app.models.tenant_channel_config import TenantChannelConfig

    channel_result = await db.execute(select(ChannelConfig).where(ChannelConfig.tenant_id == tenant_id))
    tenant_result = await db.execute(select(TenantChannelConfig).where(TenantChannelConfig.tenant_id == tenant_id))
    channel_configs = channel_result.scalars().all()
    tenant_configs = tenant_result.scalars().all()
    for config in [*channel_configs, *tenant_configs]:
        config.app_secret = None
        config.encrypt_key = None
        config.verification_token = None
        config.extra_config = scrub_channel_extra_config(config.extra_config)
    return {
        "channel_configs": len(channel_configs),
        "tenant_channel_configs": len(tenant_configs),
    }
