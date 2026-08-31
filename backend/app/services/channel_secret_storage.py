"""Versioned encrypted-at-rest storage for channel credentials."""

from __future__ import annotations

from copy import deepcopy
import json
from typing import Any
import uuid

from cryptography.fernet import InvalidToken
from sqlalchemy import JSON, String, Text, select, text
from sqlalchemy.dialects.postgresql import JSON as PostgreSQLJSON
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
DELIVERY_TARGET_SECRET_FIELDS = {
    "dingtalk": frozenset({"session_webhook"}),
    "discord": frozenset({"interaction_token"}),
    "wechat_personal": frozenset({"context_token"}),
}
CHANNEL_INGRESS_PROVIDER_FIELD = "_channel_ingress_provider"
CHANNEL_INGRESS_SECRET_PATHS = {
    "dingtalk": (("body", "session_webhook"),),
    "discord": (("body", "token"),),
    "feishu": (("body", "token"),),
    "slack": (("body", "token"),),
    "wechat_personal": (("body", "delivery_target", "context_token"),),
}
DELIVERY_TARGET_SECRET_TABLES = (
    ("chat_sessions.delivery_target_json", "chat_sessions", "delivery_target_json", "JSONB"),
    (
        "channel_delivery_outbox.delivery_target_json",
        "channel_delivery_outbox",
        "delivery_target_json",
        "JSONB",
    ),
    (
        "budget_transition_outbox.delivery_target_json",
        "budget_transition_outbox",
        "delivery_target_json",
        "JSONB",
    ),
    (
        "agent_schedules.delivery_target_json",
        "agent_schedules",
        "delivery_target_json",
        "JSONB",
    ),
    ("agent_triggers.reply_context", "agent_triggers", "reply_context", "JSONB"),
    ("agent_triggers.config", "agent_triggers", "config", "JSONB"),
    ("runtime_tasks.metadata_json", "runtime_tasks", "metadata_json", "JSON"),
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


def _transform_delivery_target(
    value: dict[str, Any] | None,
    *,
    encrypt: bool,
    provider: SecretsProvider,
) -> dict[str, Any] | None:
    if value is None:
        return None

    def transform(item: Any) -> Any:
        if isinstance(item, dict):
            transformed = {key: transform(child) for key, child in item.items()}
            channel = str(transformed.get("channel") or "").strip().casefold()
            if channel == "teams":
                channel = "microsoft_teams"
            for field_name in DELIVERY_TARGET_SECRET_FIELDS.get(channel, ()):
                field_value = transformed.get(field_name)
                if not isinstance(field_value, str) or not field_value:
                    continue
                transformed[field_name] = (
                    encrypt_channel_secret(field_value, provider=provider)
                    if encrypt
                    else decrypt_channel_secret(field_value, provider=provider)
                )
            return transformed
        if isinstance(item, list):
            return [transform(child) for child in item]
        return item

    return transform(deepcopy(value))


def encrypt_delivery_target(
    value: dict[str, Any] | None,
    *,
    provider: SecretsProvider | None = None,
) -> dict[str, Any] | None:
    """Encrypt typed ephemeral transport credentials before JSON persistence."""

    return _transform_delivery_target(
        value,
        encrypt=True,
        provider=_provider(provider),
    )


def decrypt_delivery_target(
    value: dict[str, Any] | None,
    *,
    provider: SecretsProvider | None = None,
) -> dict[str, Any] | None:
    """Decrypt typed transport credentials only at the authorized runtime seam."""

    return _transform_delivery_target(
        value,
        encrypt=False,
        provider=_provider(provider),
    )


def _transform_channel_ingress_payload(
    value: dict[str, Any] | None,
    *,
    encrypt: bool,
    provider: SecretsProvider,
) -> dict[str, Any] | None:
    if value is None:
        return None
    transformed = deepcopy(value)
    channel = str(transformed.get(CHANNEL_INGRESS_PROVIDER_FIELD) or "").strip().casefold()
    for path in CHANNEL_INGRESS_SECRET_PATHS.get(channel, ()):
        parent: Any = transformed
        for segment in path[:-1]:
            if not isinstance(parent, dict):
                parent = None
                break
            parent = parent.get(segment)
        if not isinstance(parent, dict):
            continue
        field_name = path[-1]
        field_value = parent.get(field_name)
        if not isinstance(field_value, str) or not field_value:
            continue
        parent[field_name] = (
            encrypt_channel_secret(field_value, provider=provider)
            if encrypt
            else decrypt_channel_secret(field_value, provider=provider)
        )
    return transformed


def encrypt_channel_ingress_payload(
    value: dict[str, Any] | None,
    *,
    provider: SecretsProvider | None = None,
) -> dict[str, Any] | None:
    """Encrypt provider-contract transport secrets in one inbox payload."""

    return _transform_channel_ingress_payload(
        value,
        encrypt=True,
        provider=_provider(provider),
    )


def decrypt_channel_ingress_payload(
    value: dict[str, Any] | None,
    *,
    provider: SecretsProvider | None = None,
) -> dict[str, Any] | None:
    """Decrypt typed inbox transport secrets at the dispatch boundary."""

    return _transform_channel_ingress_payload(
        value,
        encrypt=False,
        provider=_provider(provider),
    )


def scrub_channel_ingress_payload(value: dict[str, Any] | None) -> dict[str, Any]:
    """Remove typed inbox transport secrets while preserving replay evidence."""

    scrubbed = deepcopy(value or {})
    channel = str(scrubbed.get(CHANNEL_INGRESS_PROVIDER_FIELD) or "").strip().casefold()
    for path in CHANNEL_INGRESS_SECRET_PATHS.get(channel, ()):
        parent: Any = scrubbed
        for segment in path[:-1]:
            if not isinstance(parent, dict):
                parent = None
                break
            parent = parent.get(segment)
        if isinstance(parent, dict):
            parent.pop(path[-1], None)
    return scrubbed


def scrub_delivery_target(value: dict[str, Any] | None) -> dict[str, Any]:
    """Remove typed transport credentials while preserving public addressing."""

    def scrub(item: Any) -> Any:
        if isinstance(item, dict):
            scrubbed = {key: scrub(child) for key, child in item.items()}
            channel = str(scrubbed.get("channel") or "").strip().casefold()
            if channel == "teams":
                channel = "microsoft_teams"
            for field_name in DELIVERY_TARGET_SECRET_FIELDS.get(channel, ()):
                scrubbed.pop(field_name, None)
            return scrubbed
        if isinstance(item, list):
            return [scrub(child) for child in item]
        return item

    return scrub(deepcopy(value or {}))


def redact_delivery_target(value: dict[str, Any] | None) -> dict[str, Any]:
    """Mask typed transport credentials for API and audit projections."""

    def redact(item: Any) -> Any:
        if isinstance(item, dict):
            redacted = {key: redact(child) for key, child in item.items()}
            channel = str(redacted.get("channel") or "").strip().casefold()
            if channel == "teams":
                channel = "microsoft_teams"
            for field_name in DELIVERY_TARGET_SECRET_FIELDS.get(channel, ()):
                if redacted.get(field_name):
                    redacted[field_name] = "****"
            return redacted
        if isinstance(item, list):
            return [redact(child) for child in item]
        return item

    return redact(deepcopy(value or {}))


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


def _delivery_target_from_db(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return dict(value)
    if isinstance(value, str):
        parsed = json.loads(value)
        return dict(parsed) if isinstance(parsed, dict) else {}
    return {}


def _delivery_target_secret_values(target: Any):
    if isinstance(target, dict):
        channel = str(target.get("channel") or "").strip().casefold()
        if channel == "teams":
            channel = "microsoft_teams"
        for field_name in DELIVERY_TARGET_SECRET_FIELDS.get(channel, ()):
            value = target.get(field_name)
            if isinstance(value, str) and value:
                yield value
        for child in target.values():
            yield from _delivery_target_secret_values(child)
    elif isinstance(target, list):
        for child in target:
            yield from _delivery_target_secret_values(child)


def inspect_delivery_target_secret_rows(
    bind,
    *,
    current_key_id: str | None = None,
) -> dict[str, Any]:
    """Return count-only evidence for persisted channel reply-target secrets."""

    from sqlalchemy import inspect as sqlalchemy_inspect

    tables: dict[str, dict[str, int]] = {}
    totals = {
        "rows": 0,
        "secret_values": 0,
        "plaintext": 0,
        "encrypted": 0,
        "non_current": 0,
    }
    inspector = sqlalchemy_inspect(bind)
    for report_key, table_name, column_name, _pg_cast in DELIVERY_TARGET_SECRET_TABLES:
        report = {
            "rows": 0,
            "secret_values": 0,
            "plaintext": 0,
            "encrypted": 0,
            "non_current": 0,
        }
        if inspector.has_table(table_name):
            rows = bind.execute(
                text(
                    f"SELECT id, {column_name} AS delivery_target FROM {table_name}"  # noqa: S608
                )
            ).mappings()
            for row in rows:
                report["rows"] += 1
                target = _delivery_target_from_db(row["delivery_target"])
                for value in _delivery_target_secret_values(target):
                    report["secret_values"] += 1
                    key_id = channel_secret_key_id(value)
                    if key_id is None:
                        report["plaintext"] += 1
                    else:
                        report["encrypted"] += 1
                        if current_key_id is not None and key_id != current_key_id:
                            report["non_current"] += 1
        tables[report_key] = report
        for key in totals:
            totals[key] += report[key]
    return {
        "schema": "hive.delivery_target_secret_inventory.v1",
        "tables": tables,
        "totals": totals,
    }


def migrate_delivery_target_secret_rows(
    bind,
    *,
    provider: SecretsProvider,
    apply: bool,
) -> dict[str, Any]:
    """Dry-run or encrypt/rotate every typed delivery-target credential."""

    from sqlalchemy import inspect as sqlalchemy_inspect

    before = inspect_delivery_target_secret_rows(
        bind,
        current_key_id=provider.key_id,
    )
    if not apply:
        return {**before, "mode": "dry_run", "rewritten_rows": 0}

    rewritten_rows = 0
    inspector = sqlalchemy_inspect(bind)
    for _report_key, table_name, column_name, pg_cast in DELIVERY_TARGET_SECRET_TABLES:
        if not inspector.has_table(table_name):
            continue
        rows = bind.execute(
            text(
                f"SELECT id, {column_name} AS delivery_target FROM {table_name}"  # noqa: S608
            )
        ).mappings()
        for row in rows:
            current = _delivery_target_from_db(row["delivery_target"])
            encrypted = encrypt_delivery_target(current, provider=provider) or {}
            if encrypted == current:
                continue
            bind.execute(
                text(
                    f"UPDATE {table_name} SET {column_name} = "  # noqa: S608
                    + (
                        f"CAST(:delivery_target AS {pg_cast}) WHERE id = :id"
                        if bind.dialect.name == "postgresql"
                        else ":delivery_target WHERE id = :id"
                    )
                ),
                {
                    "delivery_target": json.dumps(encrypted),
                    "id": row["id"],
                },
            )
            rewritten_rows += 1

    after = inspect_delivery_target_secret_rows(
        bind,
        current_key_id=provider.key_id,
    )
    if after["totals"]["plaintext"] or after["totals"]["non_current"]:
        raise RuntimeError("delivery-target credential migration verification failed")
    return {
        **after,
        "mode": "apply",
        "rewritten_rows": rewritten_rows,
        "before": before["totals"],
    }


def _channel_ingress_payload_from_db(value: Any, *, provider: str) -> dict[str, Any]:
    payload = _delivery_target_from_db(value)
    payload[CHANNEL_INGRESS_PROVIDER_FIELD] = str(provider or "").strip().casefold()
    return payload


def _channel_ingress_secret_values(payload: dict[str, Any]):
    channel = str(payload.get(CHANNEL_INGRESS_PROVIDER_FIELD) or "").strip().casefold()
    for path in CHANNEL_INGRESS_SECRET_PATHS.get(channel, ()):
        value: Any = payload
        for segment in path:
            if not isinstance(value, dict):
                value = None
                break
            value = value.get(segment)
        if isinstance(value, str) and value:
            yield value


def inspect_channel_ingress_secret_rows(
    bind,
    *,
    current_key_id: str | None = None,
) -> dict[str, Any]:
    """Return count-only evidence for typed inbox transport credentials."""

    from sqlalchemy import inspect as sqlalchemy_inspect

    report = {
        "rows": 0,
        "secret_values": 0,
        "plaintext": 0,
        "encrypted": 0,
        "non_current": 0,
    }
    if sqlalchemy_inspect(bind).has_table("channel_ingress_events"):
        rows = bind.execute(text("SELECT id, provider, payload_json FROM channel_ingress_events")).mappings()
        for row in rows:
            report["rows"] += 1
            payload = _channel_ingress_payload_from_db(
                row["payload_json"],
                provider=row["provider"],
            )
            for value in _channel_ingress_secret_values(payload):
                report["secret_values"] += 1
                key_id = channel_secret_key_id(value)
                if key_id is None:
                    report["plaintext"] += 1
                else:
                    report["encrypted"] += 1
                    if current_key_id is not None and key_id != current_key_id:
                        report["non_current"] += 1
    return {
        "schema": "hive.channel_ingress_secret_inventory.v1",
        "tables": {"channel_ingress_events.payload_json": report},
        "totals": dict(report),
    }


def migrate_channel_ingress_secret_rows(
    bind,
    *,
    provider: SecretsProvider,
    apply: bool,
) -> dict[str, Any]:
    """Dry-run or encrypt/rotate every typed inbox transport credential."""

    from sqlalchemy import inspect as sqlalchemy_inspect

    before = inspect_channel_ingress_secret_rows(
        bind,
        current_key_id=provider.key_id,
    )
    if not apply:
        return {**before, "mode": "dry_run", "rewritten_rows": 0}

    rewritten_rows = 0
    if sqlalchemy_inspect(bind).has_table("channel_ingress_events"):
        rows = bind.execute(text("SELECT id, provider, payload_json FROM channel_ingress_events")).mappings()
        for row in rows:
            current = _channel_ingress_payload_from_db(
                row["payload_json"],
                provider=row["provider"],
            )
            encrypted = (
                encrypt_channel_ingress_payload(
                    current,
                    provider=provider,
                )
                or {}
            )
            if encrypted == current:
                continue
            bind.execute(
                text(
                    "UPDATE channel_ingress_events SET payload_json = "
                    + (
                        "CAST(:payload AS JSONB) WHERE id = :id"
                        if bind.dialect.name == "postgresql"
                        else ":payload WHERE id = :id"
                    )
                ),
                {"payload": json.dumps(encrypted), "id": row["id"]},
            )
            rewritten_rows += 1

    after = inspect_channel_ingress_secret_rows(
        bind,
        current_key_id=provider.key_id,
    )
    if after["totals"]["plaintext"] or after["totals"]["non_current"]:
        raise RuntimeError("channel-ingress credential migration verification failed")
    return {
        **after,
        "mode": "apply",
        "rewritten_rows": rewritten_rows,
        "before": before["totals"],
    }


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


class EncryptedDeliveryTargetJSON(TypeDecorator[dict[str, Any]]):
    """JSON/JSONB type that protects typed ephemeral transport credentials."""

    impl = JSON
    comparator_factory = PostgreSQLJSON.Comparator
    astext_type = Text()
    cache_ok = True

    def __init__(self, *, postgres_jsonb: bool = False) -> None:
        super().__init__()
        self.postgres_jsonb = postgres_jsonb

    def load_dialect_impl(self, dialect: Dialect):
        if dialect.name == "postgresql" and self.postgres_jsonb:
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())

    def process_bind_param(
        self,
        value: dict[str, Any] | None,
        dialect: Dialect,
    ) -> dict[str, Any] | None:
        del dialect
        return encrypt_delivery_target(value)

    def process_result_value(
        self,
        value: dict[str, Any] | None,
        dialect: Dialect,
    ) -> dict[str, Any] | None:
        del dialect
        decrypted = decrypt_delivery_target(value)
        if decrypted is None:
            return None
        # PostgreSQL's legacy JSON type may return a stored ``\u0000`` escape
        # as U+0000. Keep the global lossless text contract at this typed read
        # boundary as well as at the DBAPI write boundary.
        from app.database import repair_postgres_nul

        repaired, _replacement_count = repair_postgres_nul(decrypted)
        return repaired

    def copy(self, **kw):
        del kw
        return EncryptedDeliveryTargetJSON(postgres_jsonb=self.postgres_jsonb)


class EncryptedChannelIngressPayloadJSON(TypeDecorator[dict[str, Any]]):
    """JSON/JSONB type for provider-contract inbox transport secrets."""

    impl = JSON
    cache_ok = True

    def __init__(self, *, postgres_jsonb: bool = False) -> None:
        super().__init__()
        self.postgres_jsonb = postgres_jsonb

    def load_dialect_impl(self, dialect: Dialect):
        if dialect.name == "postgresql" and self.postgres_jsonb:
            return dialect.type_descriptor(JSONB())
        return dialect.type_descriptor(JSON())

    def process_bind_param(
        self,
        value: dict[str, Any] | None,
        dialect: Dialect,
    ) -> dict[str, Any] | None:
        del dialect
        return encrypt_channel_ingress_payload(value)

    def process_result_value(
        self,
        value: dict[str, Any] | None,
        dialect: Dialect,
    ) -> dict[str, Any] | None:
        del dialect
        return decrypt_channel_ingress_payload(value)

    def copy(self, **kw):
        del kw
        return EncryptedChannelIngressPayloadJSON(postgres_jsonb=self.postgres_jsonb)


async def scrub_tenant_channel_secrets(db, tenant_id: uuid.UUID) -> dict[str, int]:
    """Remove all channel credentials when a tenant is deactivated."""
    from app.models.budget_transition_outbox import BudgetTransitionOutbox
    from app.models.channel_config import ChannelConfig
    from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
    from app.models.channel_ingress_event import ChannelIngressEvent
    from app.models.chat_session import ChatSession
    from app.models.schedule import AgentSchedule
    from app.models.tenant_channel_config import TenantChannelConfig
    from app.models.trigger import AgentTrigger
    from app.models.runtime_task import RuntimeTask

    channel_result = await db.execute(select(ChannelConfig).where(ChannelConfig.tenant_id == tenant_id))
    tenant_result = await db.execute(select(TenantChannelConfig).where(TenantChannelConfig.tenant_id == tenant_id))
    session_result = await db.execute(select(ChatSession).where(ChatSession.tenant_id == tenant_id))
    delivery_result = await db.execute(
        select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.tenant_id == tenant_id)
    )
    budget_delivery_result = await db.execute(
        select(BudgetTransitionOutbox).where(BudgetTransitionOutbox.tenant_id == tenant_id)
    )
    schedule_result = await db.execute(select(AgentSchedule).where(AgentSchedule.tenant_id == tenant_id))
    trigger_result = await db.execute(
        select(AgentTrigger).where(AgentTrigger.tenant_id == tenant_id).order_by(AgentTrigger.id).with_for_update()
    )
    runtime_task_result = await db.execute(select(RuntimeTask).where(RuntimeTask.tenant_id == tenant_id))
    channel_ingress_result = await db.execute(
        select(ChannelIngressEvent).where(ChannelIngressEvent.tenant_id == tenant_id)
    )
    channel_configs = channel_result.scalars().all()
    tenant_configs = tenant_result.scalars().all()
    chat_sessions = session_result.scalars().all()
    channel_deliveries = delivery_result.scalars().all()
    budget_deliveries = budget_delivery_result.scalars().all()
    schedules = schedule_result.scalars().all()
    triggers = trigger_result.scalars().all()
    runtime_tasks = runtime_task_result.scalars().all()
    channel_ingress_events = channel_ingress_result.scalars().all()
    for config in [*channel_configs, *tenant_configs]:
        config.app_secret = None
        config.encrypt_key = None
        config.verification_token = None
        config.extra_config = scrub_channel_extra_config(config.extra_config)
    for row in [*chat_sessions, *channel_deliveries, *budget_deliveries, *schedules]:
        row.delivery_target_json = scrub_delivery_target(
            row.delivery_target_json,
        )
    for trigger in triggers:
        trigger.reply_context = scrub_delivery_target(trigger.reply_context)
        trigger.config = scrub_delivery_target(trigger.config)
    for task in runtime_tasks:
        task.metadata_json = scrub_delivery_target(task.metadata_json)
    for event in channel_ingress_events:
        event.payload_json = scrub_channel_ingress_payload(event.payload_json)
    return {
        "channel_configs": len(channel_configs),
        "tenant_channel_configs": len(tenant_configs),
        "chat_session_targets": len(chat_sessions),
        "channel_delivery_targets": len(channel_deliveries),
        "budget_delivery_targets": len(budget_deliveries),
        "schedule_targets": len(schedules),
        "trigger_targets": len(triggers),
        "runtime_task_targets": len(runtime_tasks),
        "channel_ingress_payloads": len(channel_ingress_events),
    }
