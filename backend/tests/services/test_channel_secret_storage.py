from __future__ import annotations

import uuid
from pathlib import Path

import pytest


BOT_CHANNEL_TYPES = {
    "feishu",
    "telegram",
    "discord",
    "dingtalk",
    "microsoft_teams",
    "slack",
    "wecom",
}


def test_setup_generates_channel_encryption_key_without_printing_it() -> None:
    setup = (Path(__file__).resolve().parents[3] / "setup.sh").read_text(encoding="utf-8")

    assert "secrets.token_hex(32)" in setup
    assert "Generated SECRETS_MASTER_KEY" in setup
    assert 'echo "$SECRETS_MASTER_KEY"' not in setup


def test_channel_secret_envelope_round_trips_without_plaintext() -> None:
    from app.services.channel_secret_storage import (
        CHANNEL_SECRET_PREFIX,
        decrypt_channel_secret,
        encrypt_channel_secret,
    )
    from app.services.secrets_provider import FernetSecretsProvider

    provider = FernetSecretsProvider("primary-channel-key-000000000000")
    stored = encrypt_channel_secret("tenant-bot-secret", provider=provider)

    assert stored.startswith(CHANNEL_SECRET_PREFIX)
    assert "tenant-bot-secret" not in stored
    assert decrypt_channel_secret(stored, provider=provider) == "tenant-bot-secret"


def test_channel_secret_wrong_key_fails_closed() -> None:
    from app.services.channel_secret_storage import (
        ChannelSecretDecryptionError,
        decrypt_channel_secret,
        encrypt_channel_secret,
    )
    from app.services.secrets_provider import FernetSecretsProvider

    stored = encrypt_channel_secret(
        "never-return-ciphertext-as-plaintext",
        provider=FernetSecretsProvider("first-channel-key-0000000000000"),
    )

    with pytest.raises(ChannelSecretDecryptionError):
        decrypt_channel_secret(stored, provider=FernetSecretsProvider("wrong-channel-key-0000000000000"))


def test_channel_secret_refuses_noop_provider_even_in_development() -> None:
    from app.services.channel_secret_storage import ChannelSecretStorageError, encrypt_channel_secret
    from app.services.secrets_provider import NoopSecretsProvider

    with pytest.raises(ChannelSecretStorageError, match="encrypted secrets provider"):
        encrypt_channel_secret("must-not-hit-disk-in-plaintext", provider=NoopSecretsProvider())


def test_channel_secret_rotation_reads_previous_and_rewrites_current() -> None:
    from app.services.channel_secret_storage import (
        channel_secret_key_id,
        decrypt_channel_secret,
        encrypt_channel_secret,
        rotate_channel_secret,
    )
    from app.services.secrets_provider import FernetSecretsProvider

    old_key = "old-channel-key-00000000000000000"
    new_key = "new-channel-key-00000000000000000"
    old_provider = FernetSecretsProvider(old_key)
    rotating_provider = FernetSecretsProvider(new_key, previous_master_keys=[old_key])
    stored = encrypt_channel_secret("rotatable-secret", provider=old_provider)

    assert decrypt_channel_secret(stored, provider=rotating_provider) == "rotatable-secret"
    assert channel_secret_key_id(stored) == old_provider.key_id

    rotated = rotate_channel_secret(stored, provider=rotating_provider)
    assert rotated != stored
    assert channel_secret_key_id(rotated) == rotating_provider.key_id
    assert decrypt_channel_secret(rotated, provider=rotating_provider) == "rotatable-secret"
    assert rotate_channel_secret(rotated, provider=rotating_provider) == rotated


def test_channel_extra_config_encrypts_only_secret_keys_recursively() -> None:
    from app.services.channel_secret_storage import (
        decrypt_channel_extra_config,
        encrypt_channel_extra_config,
    )
    from app.services.secrets_provider import FernetSecretsProvider

    provider = FernetSecretsProvider("json-channel-key-000000000000000")
    original = {
        "bot_id": "bot-visible",
        "bot_secret": "bot-hidden",
        "nested": {"client_secret": "client-hidden", "region": "cn"},
    }

    stored = encrypt_channel_extra_config(original, provider=provider)
    assert stored["bot_id"] == "bot-visible"
    assert stored["nested"]["region"] == "cn"
    assert "bot-hidden" not in str(stored)
    assert "client-hidden" not in str(stored)
    assert decrypt_channel_extra_config(stored, provider=provider) == original


def test_both_channel_models_use_transparent_encrypted_types() -> None:
    from app.models.channel_config import ChannelConfig
    from app.models.tenant_channel_config import TenantChannelConfig
    from app.services.channel_secret_storage import EncryptedChannelJSON, EncryptedChannelSecret

    for model in (ChannelConfig, TenantChannelConfig):
        for field in ("app_secret", "encrypt_key", "verification_token"):
            assert isinstance(model.__table__.c[field].type, EncryptedChannelSecret)
        assert isinstance(model.__table__.c.extra_config.type, EncryptedChannelJSON)


@pytest.mark.asyncio
async def test_tenant_offboarding_scrubs_agent_and_tenant_channel_secrets() -> None:
    from app.services.channel_secret_storage import scrub_tenant_channel_secrets

    tenant_id = uuid.uuid4()

    class Config:
        def __init__(self, channel_type: str) -> None:
            self.channel_type = channel_type
            self.app_secret = "app-secret"
            self.encrypt_key = "encrypt-key"
            self.verification_token = "verification-token"
            self.extra_config = {"bot_secret": "extra-secret", "region": "cn"}

    agent_configs = [Config(channel_type) for channel_type in sorted(BOT_CHANNEL_TYPES)]
    tenant_configs = [Config("feishu")]

    class Scalars:
        def __init__(self, rows):
            self._rows = rows

        def all(self):
            return self._rows

    class Result:
        def __init__(self, rows):
            self._rows = rows

        def scalars(self):
            return Scalars(self._rows)

    class DB:
        def __init__(self):
            self.results = iter((Result(agent_configs), Result(tenant_configs)))

        async def execute(self, _statement):
            return next(self.results)

    report = await scrub_tenant_channel_secrets(DB(), tenant_id)

    assert report == {"channel_configs": 7, "tenant_channel_configs": 1}
    for config in [*agent_configs, *tenant_configs]:
        assert config.app_secret is None
        assert config.encrypt_key is None
        assert config.verification_token is None
        assert config.extra_config == {"region": "cn"}
