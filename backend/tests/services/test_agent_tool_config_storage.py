from __future__ import annotations

import json

import pytest


def test_agent_tool_config_envelope_hides_arbitrary_mcp_config_and_round_trips() -> None:
    from app.services.agent_tool_config_storage import (
        AGENT_TOOL_CONFIG_ENVELOPE_KEY,
        AGENT_TOOL_CONFIG_PREFIX,
        decrypt_agent_tool_config,
        encrypt_agent_tool_config,
    )
    from app.services.secrets_provider import FernetSecretsProvider

    provider = FernetSecretsProvider("agent-tool-config-primary-key-000001")
    config = {
        "smithery_namespace": "tenant-namespace",
        "nested": {
            "githubPersonalAccessToken": "github-secret-token",
            "region": "cn",
        },
    }

    stored = encrypt_agent_tool_config(config, provider=provider)

    assert set(stored) == {AGENT_TOOL_CONFIG_ENVELOPE_KEY}
    assert stored[AGENT_TOOL_CONFIG_ENVELOPE_KEY].startswith(AGENT_TOOL_CONFIG_PREFIX)
    assert "github-secret-token" not in json.dumps(stored)
    assert "tenant-namespace" not in json.dumps(stored)
    assert decrypt_agent_tool_config(stored, provider=provider) == config


def test_agent_tool_config_encryption_refuses_noop_provider() -> None:
    from app.services.agent_tool_config_storage import (
        AgentToolConfigStorageError,
        encrypt_agent_tool_config,
    )
    from app.services.secrets_provider import NoopSecretsProvider

    with pytest.raises(AgentToolConfigStorageError, match="SECRETS_MASTER_KEY"):
        encrypt_agent_tool_config(
            {"api_key": "must-never-hit-disk"},
            provider=NoopSecretsProvider(),
        )


def test_agent_tool_config_decryption_fails_closed_with_wrong_key() -> None:
    from app.services.agent_tool_config_storage import (
        AgentToolConfigDecryptionError,
        decrypt_agent_tool_config,
        encrypt_agent_tool_config,
    )
    from app.services.secrets_provider import FernetSecretsProvider

    stored = encrypt_agent_tool_config(
        {"api_key": "credential"},
        provider=FernetSecretsProvider("agent-tool-config-first-key-0000001"),
    )

    with pytest.raises(AgentToolConfigDecryptionError, match="cannot be decrypted"):
        decrypt_agent_tool_config(
            stored,
            provider=FernetSecretsProvider("agent-tool-config-wrong-key-0000001"),
        )


def test_agent_tool_config_current_key_envelope_is_authenticated_before_reuse() -> None:
    from app.services.agent_tool_config_storage import (
        AGENT_TOOL_CONFIG_ENVELOPE_KEY,
        AgentToolConfigDecryptionError,
        encrypt_agent_tool_config,
    )
    from app.services.secrets_provider import FernetSecretsProvider

    provider = FernetSecretsProvider("agent-tool-config-auth-key-00000001")
    stored = encrypt_agent_tool_config({"api_key": "credential"}, provider=provider)
    envelope = stored[AGENT_TOOL_CONFIG_ENVELOPE_KEY]
    stored[AGENT_TOOL_CONFIG_ENVELOPE_KEY] = envelope[:-1] + ("A" if envelope[-1] != "A" else "B")

    with pytest.raises(AgentToolConfigDecryptionError, match="cannot be decrypted"):
        encrypt_agent_tool_config(stored, provider=provider)


def test_agent_tool_config_rotation_rewraps_old_key_without_plaintext() -> None:
    from app.services.agent_tool_config_storage import (
        agent_tool_config_key_id,
        decrypt_agent_tool_config,
        encrypt_agent_tool_config,
    )
    from app.services.secrets_provider import FernetSecretsProvider

    old_key = "agent-tool-config-old-key-00000000001"
    new_key = "agent-tool-config-new-key-00000000001"
    old_provider = FernetSecretsProvider(old_key)
    rotating_provider = FernetSecretsProvider(new_key, previous_master_keys=[old_key])
    original = {"auth_code": "mail-secret", "email_address": "ops@example.test"}
    old_stored = encrypt_agent_tool_config(original, provider=old_provider)

    rotated = encrypt_agent_tool_config(old_stored, provider=rotating_provider)

    assert agent_tool_config_key_id(rotated) == rotating_provider.key_id
    assert rotated != old_stored
    assert "mail-secret" not in json.dumps(rotated)
    assert decrypt_agent_tool_config(rotated, provider=rotating_provider) == original


def test_encrypted_agent_tool_config_type_encrypts_bind_and_decrypts_result() -> None:
    from sqlalchemy.dialects.postgresql import dialect

    from app.services import secrets_provider
    from app.services.agent_tool_config_storage import (
        AGENT_TOOL_CONFIG_ENVELOPE_KEY,
        EncryptedAgentToolConfig,
    )
    from app.services.secrets_provider import FernetSecretsProvider

    previous = secrets_provider._provider
    secrets_provider._provider = FernetSecretsProvider("agent-tool-type-key-00000000000001")
    try:
        column_type = EncryptedAgentToolConfig()
        stored = column_type.process_bind_param({"api_key": "runtime-key"}, dialect())
        assert AGENT_TOOL_CONFIG_ENVELOPE_KEY in stored
        assert "runtime-key" not in json.dumps(stored)
        assert column_type.process_result_value(stored, dialect()) == {"api_key": "runtime-key"}
    finally:
        secrets_provider._provider = previous


def test_agent_tool_secret_mask_and_merge_cover_schema_and_nested_credentials() -> None:
    from app.services.tool_config_service import (
        MASKED_SECRET_SENTINEL,
        mask_agent_tool_config_secrets,
        merge_agent_tool_config_secrets,
    )

    schema = {
        "fields": [
            {"key": "custom_credential", "type": "password"},
            {"key": "token_budget", "type": "number"},
        ]
    }
    stored = {
        "api_key": "direct-key",
        "modelscope_api_token": "modelscope-secret",
        "custom_credential": "schema-secret",
        "nested": {
            "githubPersonalAccessToken": "github-token",
            "clientSecret": "client-secret",
            "region": "cn",
        },
        "token_budget": 4096,
    }

    masked = mask_agent_tool_config_secrets(stored, schema)

    assert masked["api_key"] == MASKED_SECRET_SENTINEL
    assert masked["modelscope_api_token"] == MASKED_SECRET_SENTINEL
    assert masked["custom_credential"] == MASKED_SECRET_SENTINEL
    assert masked["nested"]["githubPersonalAccessToken"] == MASKED_SECRET_SENTINEL
    assert masked["nested"]["clientSecret"] == MASKED_SECRET_SENTINEL
    assert masked["nested"]["region"] == "cn"
    assert masked["token_budget"] == 4096
    assert "direct-key" not in json.dumps(masked)
    assert "github-token" not in json.dumps(masked)

    incoming = {
        "api_key": MASKED_SECRET_SENTINEL,
        "modelscope_api_token": MASKED_SECRET_SENTINEL,
        "custom_credential": MASKED_SECRET_SENTINEL,
        "nested": {
            "githubPersonalAccessToken": MASKED_SECRET_SENTINEL,
            "clientSecret": "replacement-secret",
            "region": "us",
        },
        "token_budget": 8192,
    }
    merged = merge_agent_tool_config_secrets(incoming, stored, schema)

    assert merged["api_key"] == "direct-key"
    assert merged["modelscope_api_token"] == "modelscope-secret"
    assert merged["custom_credential"] == "schema-secret"
    assert merged["nested"]["githubPersonalAccessToken"] == "github-token"
    assert merged["nested"]["clientSecret"] == "replacement-secret"
    assert merged["nested"]["region"] == "us"
    assert merged["token_budget"] == 8192
