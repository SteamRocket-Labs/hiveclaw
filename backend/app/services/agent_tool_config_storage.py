"""Versioned encrypted-at-rest storage for per-agent tool configuration.

``AgentTool.config`` may contain arbitrary third-party MCP configuration.  A
field allowlist is therefore not sufficient for storage protection: providers
are free to name credentials however they like.  The complete JSON document is
wrapped in one authenticated envelope before it reaches the database and is
restored transparently by the model type on reads.
"""

from __future__ import annotations

from copy import deepcopy
import json
import re
from typing import Any

from cryptography.fernet import InvalidToken
from sqlalchemy import JSON, text
from sqlalchemy.engine import Dialect
from sqlalchemy.types import TypeDecorator

from app.services.secrets_provider import SecretsProvider, get_secrets_provider


AGENT_TOOL_CONFIG_ENVELOPE_KEY = "__hive_agent_tool_config_v1__"
AGENT_TOOL_CONFIG_PREFIX = "hive:agent-tool-config:v1:"


class AgentToolConfigDecryptionError(RuntimeError):
    """An encrypted AgentTool config cannot be authenticated or decoded."""


class AgentToolConfigStorageError(RuntimeError):
    """An AgentTool config cannot be stored with the configured keyring."""


def _provider(provider: SecretsProvider | None) -> SecretsProvider:
    return provider or get_secrets_provider()


def _split_envelope(value: str) -> tuple[str, str]:
    if not isinstance(value, str) or not value.startswith(AGENT_TOOL_CONFIG_PREFIX):
        raise AgentToolConfigDecryptionError("Malformed AgentTool config envelope")
    payload = value[len(AGENT_TOOL_CONFIG_PREFIX) :]
    key_id, separator, token = payload.partition(":")
    if not separator or not key_id or not token:
        raise AgentToolConfigDecryptionError("Malformed AgentTool config envelope")
    return key_id, token


def _envelope_value(value: dict[str, Any] | None) -> str | None:
    if not isinstance(value, dict) or set(value) != {AGENT_TOOL_CONFIG_ENVELOPE_KEY}:
        return None
    envelope = value.get(AGENT_TOOL_CONFIG_ENVELOPE_KEY)
    if not isinstance(envelope, str):
        raise AgentToolConfigDecryptionError("Malformed AgentTool config envelope")
    return envelope


def agent_tool_config_key_id(value: dict[str, Any] | None) -> str | None:
    """Return the envelope key id, or ``None`` for legacy plaintext/empty rows."""

    envelope = _envelope_value(value)
    if envelope is None:
        return None
    return _split_envelope(envelope)[0]


def decrypt_agent_tool_config(
    value: dict[str, Any] | None,
    *,
    provider: SecretsProvider | None = None,
) -> dict[str, Any] | None:
    """Decrypt one config document; legacy plaintext remains readable pre-migration."""

    if value is None:
        return None
    if not isinstance(value, dict):
        raise AgentToolConfigDecryptionError("AgentTool config must be a JSON object")
    envelope = _envelope_value(value)
    if envelope is None:
        return deepcopy(value)
    _stored_key_id, token = _split_envelope(envelope)
    try:
        plaintext = _provider(provider).decrypt_strict(token)
        decoded = json.loads(plaintext)
    except (InvalidToken, ValueError, UnicodeError, json.JSONDecodeError) as exc:
        raise AgentToolConfigDecryptionError("AgentTool config cannot be decrypted by the configured keyring") from exc
    if not isinstance(decoded, dict):
        raise AgentToolConfigDecryptionError("Decrypted AgentTool config is not a JSON object")
    return decoded


def encrypt_agent_tool_config(
    value: dict[str, Any] | None,
    *,
    provider: SecretsProvider | None = None,
) -> dict[str, Any] | None:
    """Encrypt or rotate one complete AgentTool config document."""

    if value is None:
        return None
    if not isinstance(value, dict):
        raise AgentToolConfigStorageError("AgentTool config must be a JSON object")
    if not value:
        return {}

    selected = _provider(provider)
    if selected.key_id == "development-noop":
        raise AgentToolConfigStorageError(
            "AgentTool config requires an encrypted secrets provider; configure SECRETS_MASTER_KEY"
        )

    envelope = _envelope_value(value)
    if envelope is not None:
        stored_key_id, _token = _split_envelope(envelope)
        if stored_key_id == selected.key_id:
            # Authentication is part of the storage contract.  A syntactically
            # valid but corrupted envelope must block migration/write-back
            # instead of being mistaken for a healthy current-key row.
            decrypt_agent_tool_config(value, provider=selected)
            return deepcopy(value)
        value = decrypt_agent_tool_config(value, provider=selected) or {}

    try:
        plaintext = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except (TypeError, ValueError) as exc:
        raise AgentToolConfigStorageError("AgentTool config is not JSON serializable") from exc
    token = selected.encrypt(plaintext)
    return {AGENT_TOOL_CONFIG_ENVELOPE_KEY: (f"{AGENT_TOOL_CONFIG_PREFIX}{selected.key_id}:{token}")}


_CREDENTIAL_KEY_EXACT = frozenset(
    {
        "apikey",
        "apitoken",
        "authcode",
        "authtoken",
        "bearertoken",
        "bottoken",
        "clientsecret",
        "credential",
        "credentials",
        "password",
        "privatekey",
        "refreshtoken",
        "secret",
        "signingsecret",
        "token",
    }
)
_CREDENTIAL_KEY_SUFFIXES = (
    "accesstoken",
    "apikey",
    "apikeys",
    "apitoken",
    "authcode",
    "authtoken",
    "bearertoken",
    "bottoken",
    "clientsecret",
    "credential",
    "credentials",
    "password",
    "privatekey",
    "refreshtoken",
    "secret",
    "signingsecret",
)


def is_agent_tool_credential_key(key: object) -> bool:
    """Classify structural credential keys at the API disclosure boundary.

    This only inspects machine field names.  It never derives semantic truth
    from natural-language values and therefore stays inside the exact
    credential-visibility hard invariant.
    """

    normalized = re.sub(r"[^a-z0-9]", "", str(key).casefold())
    return normalized in _CREDENTIAL_KEY_EXACT or normalized.endswith(_CREDENTIAL_KEY_SUFFIXES)


def inspect_agent_tool_config_rows(bind, *, current_key_id: str | None = None) -> dict[str, Any]:
    """Return count-only storage evidence without exposing config contents."""

    report = {
        "rows": 0,
        "non_empty": 0,
        "plaintext": 0,
        "encrypted": 0,
        "non_current": 0,
        "malformed": 0,
    }
    rows = bind.execute(text("SELECT id, config FROM agent_tools")).mappings()
    for row in rows:
        report["rows"] += 1
        config = row["config"] or {}
        if not config:
            continue
        report["non_empty"] += 1
        try:
            key_id = agent_tool_config_key_id(config)
        except AgentToolConfigDecryptionError:
            report["malformed"] += 1
            continue
        if key_id is None:
            report["plaintext"] += 1
            continue
        report["encrypted"] += 1
        if current_key_id is not None and key_id != current_key_id:
            report["non_current"] += 1
    return {"schema": "hive.agent_tool_config_inventory.v1", "totals": report}


def migrate_agent_tool_config_rows(
    bind,
    *,
    provider: SecretsProvider,
    apply: bool,
) -> dict[str, Any]:
    """Dry-run or encrypt/rotate every non-empty AgentTool config in place."""

    if provider.key_id == "development-noop":
        raise AgentToolConfigStorageError("AgentTool config migration requires SECRETS_MASTER_KEY")
    before = inspect_agent_tool_config_rows(bind, current_key_id=provider.key_id)
    if before["totals"]["malformed"]:
        raise AgentToolConfigDecryptionError("Malformed AgentTool config envelopes require operator recovery")
    if not apply:
        return {**before, "mode": "dry_run", "rewritten_rows": 0}

    rewritten_rows = 0
    rows = list(bind.execute(text("SELECT id, config FROM agent_tools")).mappings())
    for row in rows:
        config = row["config"] or {}
        if not config:
            continue
        encrypted = encrypt_agent_tool_config(config, provider=provider)
        if encrypted == config:
            continue
        bind.execute(
            text("UPDATE agent_tools SET config = CAST(:config AS json) WHERE id = :id"),
            {"config": json.dumps(encrypted), "id": row["id"]},
        )
        rewritten_rows += 1

    after = inspect_agent_tool_config_rows(bind, current_key_id=provider.key_id)
    totals = after["totals"]
    if totals["plaintext"] or totals["non_current"] or totals["malformed"]:
        raise RuntimeError("AgentTool config migration verification failed")
    return {
        **after,
        "mode": "apply",
        "rewritten_rows": rewritten_rows,
        "before": before["totals"],
    }


class EncryptedAgentToolConfig(TypeDecorator[dict[str, Any]]):
    """SQLAlchemy JSON type that encrypts complete AgentTool config documents."""

    impl = JSON
    cache_ok = True

    def load_dialect_impl(self, dialect: Dialect):
        return dialect.type_descriptor(JSON())

    def process_bind_param(
        self,
        value: dict[str, Any] | None,
        dialect: Dialect,
    ) -> dict[str, Any] | None:
        del dialect
        return encrypt_agent_tool_config(value)

    def process_result_value(
        self,
        value: dict[str, Any] | None,
        dialect: Dialect,
    ) -> dict[str, Any] | None:
        del dialect
        return decrypt_agent_tool_config(value)

    def copy(self, **kw):
        del kw
        return EncryptedAgentToolConfig()
