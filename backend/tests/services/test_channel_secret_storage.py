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


def test_delivery_target_encrypts_only_typed_transport_secret_fields() -> None:
    from app.services.channel_secret_storage import (
        decrypt_delivery_target,
        encrypt_delivery_target,
        redact_delivery_target,
    )
    from app.services.secrets_provider import FernetSecretsProvider

    provider = FernetSecretsProvider("delivery-target-key-00000000000000")
    original = {
        "channel": "discord",
        "channel_id": "public-channel",
        "interaction_token": "interaction-secret-0123456789",
        "description": "api_key=sk-example-not-authority",
    }

    stored = encrypt_delivery_target(original, provider=provider)

    assert stored["channel_id"] == "public-channel"
    assert stored["description"] == "api_key=sk-example-not-authority"
    assert "interaction-secret-0123456789" not in str(stored)
    assert decrypt_delivery_target(stored, provider=provider) == original
    assert redact_delivery_target(original) == {
        "channel": "discord",
        "channel_id": "public-channel",
        "interaction_token": "****",
        "description": "api_key=sk-example-not-authority",
    }


def test_channel_ingress_payload_encrypts_only_provider_contract_secrets() -> None:
    from app.services.channel_secret_storage import (
        decrypt_channel_ingress_payload,
        encrypt_channel_ingress_payload,
    )
    from app.services.secrets_provider import FernetSecretsProvider

    provider = FernetSecretsProvider("channel-ingress-key-00000000000000")
    original = {
        "_channel_ingress_provider": "discord",
        "body": {
            "token": "interaction-secret-0123456789",
            "data": {
                "options": [
                    {
                        "name": "message",
                        "value": "Explain api_key=sk-example-not-authority exactly.",
                    }
                ]
            },
        },
    }

    stored = encrypt_channel_ingress_payload(original, provider=provider)

    assert stored["body"]["data"] == original["body"]["data"]
    assert "interaction-secret-0123456789" not in str(stored)
    assert decrypt_channel_ingress_payload(stored, provider=provider) == original


def test_both_channel_models_use_transparent_encrypted_types() -> None:
    from app.models.channel_config import ChannelConfig
    from app.models.tenant_channel_config import TenantChannelConfig
    from app.services.channel_secret_storage import EncryptedChannelJSON, EncryptedChannelSecret

    for model in (ChannelConfig, TenantChannelConfig):
        for field in ("app_secret", "encrypt_key", "verification_token"):
            assert isinstance(model.__table__.c[field].type, EncryptedChannelSecret)
        assert isinstance(model.__table__.c.extra_config.type, EncryptedChannelJSON)


def test_delivery_target_models_use_transparent_encrypted_type() -> None:
    from app.models.budget_transition_outbox import BudgetTransitionOutbox
    from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
    from app.models.chat_session import ChatSession
    from app.models.schedule import AgentSchedule
    from app.models.trigger import AgentTrigger
    from app.models.runtime_task import RuntimeTask
    from app.services.channel_secret_storage import EncryptedDeliveryTargetJSON

    for column in (
        ChatSession.__table__.c.delivery_target_json,
        ChannelDeliveryOutbox.__table__.c.delivery_target_json,
        BudgetTransitionOutbox.__table__.c.delivery_target_json,
        AgentSchedule.__table__.c.delivery_target_json,
        AgentTrigger.__table__.c.reply_context,
        AgentTrigger.__table__.c.config,
        RuntimeTask.__table__.c.metadata_json,
    ):
        assert isinstance(column.type, EncryptedDeliveryTargetJSON)


def test_channel_ingress_model_uses_transparent_encrypted_payload_type() -> None:
    from app.models.channel_ingress_event import ChannelIngressEvent
    from app.services.channel_secret_storage import EncryptedChannelIngressPayloadJSON

    assert isinstance(
        ChannelIngressEvent.__table__.c.payload_json.type,
        EncryptedChannelIngressPayloadJSON,
    )


def test_delivery_target_backfill_reports_and_encrypts_legacy_plaintext() -> None:
    import json

    from sqlalchemy import create_engine, text

    from app.services.channel_secret_storage import (
        inspect_delivery_target_secret_rows,
        migrate_delivery_target_secret_rows,
    )
    from app.services.secrets_provider import FernetSecretsProvider

    engine = create_engine("sqlite://")
    provider = FernetSecretsProvider("delivery-backfill-key-0000000000000")
    plaintext = "legacy-interaction-secret-0123456789"
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE chat_sessions (id TEXT PRIMARY KEY, delivery_target_json JSON)"))
        connection.execute(
            text("CREATE TABLE channel_delivery_outbox (id TEXT PRIMARY KEY, delivery_target_json JSON)")
        )
        payload = json.dumps(
            {
                "channel": "discord",
                "interaction_token": plaintext,
                "channel_id": "public-channel",
            }
        )
        connection.execute(
            text("INSERT INTO chat_sessions (id, delivery_target_json) VALUES ('session-1', :payload)"),
            {"payload": payload},
        )
        connection.execute(
            text("INSERT INTO channel_delivery_outbox (id, delivery_target_json) VALUES ('outbox-1', :payload)"),
            {"payload": payload},
        )

        before = inspect_delivery_target_secret_rows(
            connection,
            current_key_id=provider.key_id,
        )
        report = migrate_delivery_target_secret_rows(
            connection,
            provider=provider,
            apply=True,
        )
        raw = [
            row[0]
            for row in connection.execute(
                text(
                    "SELECT delivery_target_json FROM chat_sessions "
                    "UNION ALL SELECT delivery_target_json "
                    "FROM channel_delivery_outbox"
                )
            )
        ]

    assert before["totals"]["plaintext"] == 2
    assert report["rewritten_rows"] == 2
    assert report["totals"]["plaintext"] == 0
    assert report["totals"]["encrypted"] == 2
    assert all(plaintext not in str(value) for value in raw)


def test_channel_ingress_backfill_reports_and_encrypts_legacy_transport_secrets() -> None:
    import json

    from sqlalchemy import create_engine, text

    from app.services.channel_secret_storage import (
        inspect_channel_ingress_secret_rows,
        migrate_channel_ingress_secret_rows,
    )
    from app.services.secrets_provider import FernetSecretsProvider

    engine = create_engine("sqlite://")
    provider = FernetSecretsProvider("ingress-backfill-key-00000000000000")
    plaintext = "legacy-discord-token-0123456789"
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE channel_ingress_events "
                "(id TEXT PRIMARY KEY, provider TEXT NOT NULL, payload_json JSON NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO channel_ingress_events (id, provider, payload_json) "
                "VALUES ('event-1', 'discord', :payload)"
            ),
            {
                "payload": json.dumps(
                    {
                        "body": {
                            "token": plaintext,
                            "data": {
                                "options": [
                                    {
                                        "name": "message",
                                        "value": "api_key=sk-example-not-authority",
                                    }
                                ]
                            },
                        }
                    }
                )
            },
        )

        before = inspect_channel_ingress_secret_rows(
            connection,
            current_key_id=provider.key_id,
        )
        report = migrate_channel_ingress_secret_rows(
            connection,
            provider=provider,
            apply=True,
        )
        raw = connection.execute(
            text("SELECT payload_json FROM channel_ingress_events WHERE id = 'event-1'")
        ).scalar_one()

    assert before["totals"]["plaintext"] == 1
    assert report["rewritten_rows"] == 1
    assert report["totals"]["plaintext"] == 0
    assert report["totals"]["encrypted"] == 1
    assert plaintext not in str(raw)
    assert "api_key=sk-example-not-authority" in str(raw)


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
    chat_session = type(
        "Session",
        (),
        {
            "delivery_target_json": {
                "channel": "discord",
                "channel_id": "public-channel",
                "interaction_token": "interaction-secret",
            }
        },
    )()
    outbox_item = type(
        "Outbox",
        (),
        {
            "delivery_target_json": {
                "channel": "dingtalk",
                "conversation_id": "public-conversation",
                "session_webhook": "webhook-secret",
            }
        },
    )()
    budget_item = type(
        "BudgetOutbox",
        (),
        {
            "delivery_target_json": {
                "channel": "wechat_personal",
                "to_user_id": "public-user",
                "context_token": "context-secret",
            }
        },
    )()
    schedule = type(
        "Schedule",
        (),
        {
            "delivery_target_json": {
                "channel": "discord",
                "channel_id": "scheduled-channel",
                "interaction_token": "scheduled-secret",
            }
        },
    )()
    trigger = type(
        "Trigger",
        (),
        {
            "reply_context": {
                "channel": "dingtalk",
                "conversation_id": "trigger-conversation",
                "session_webhook": "trigger-secret",
            },
            "config": {
                "delivery_target_json": {
                    "channel": "discord",
                    "channel_id": "nested-channel",
                    "interaction_token": "nested-secret",
                }
            },
        },
    )()
    runtime_task = type(
        "RuntimeTask",
        (),
        {
            "metadata_json": {
                "delivery_target_json": {
                    "channel": "wechat_personal",
                    "to_user_id": "runtime-user",
                    "context_token": "runtime-secret",
                }
            }
        },
    )()
    ingress_event = type(
        "IngressEvent",
        (),
        {
            "payload_json": {
                "_channel_ingress_provider": "discord",
                "body": {
                    "token": "ingress-token",
                    "data": {"value": "public-event"},
                },
            }
        },
    )()

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
            self.results = iter(
                (
                    Result(agent_configs),
                    Result(tenant_configs),
                    Result([chat_session]),
                    Result([outbox_item]),
                    Result([budget_item]),
                    Result([schedule]),
                    Result([trigger]),
                    Result([runtime_task]),
                    Result([ingress_event]),
                )
            )

        async def execute(self, _statement):
            return next(self.results)

    report = await scrub_tenant_channel_secrets(DB(), tenant_id)

    assert report == {
        "channel_configs": 7,
        "tenant_channel_configs": 1,
        "chat_session_targets": 1,
        "channel_delivery_targets": 1,
        "budget_delivery_targets": 1,
        "schedule_targets": 1,
        "trigger_targets": 1,
        "runtime_task_targets": 1,
        "channel_ingress_payloads": 1,
    }
    for config in [*agent_configs, *tenant_configs]:
        assert config.app_secret is None
        assert config.encrypt_key is None
        assert config.verification_token is None
        assert config.extra_config == {"region": "cn"}
    assert chat_session.delivery_target_json == {
        "channel": "discord",
        "channel_id": "public-channel",
    }
    assert outbox_item.delivery_target_json == {
        "channel": "dingtalk",
        "conversation_id": "public-conversation",
    }
    assert budget_item.delivery_target_json == {
        "channel": "wechat_personal",
        "to_user_id": "public-user",
    }
    assert schedule.delivery_target_json == {
        "channel": "discord",
        "channel_id": "scheduled-channel",
    }
    assert trigger.reply_context == {
        "channel": "dingtalk",
        "conversation_id": "trigger-conversation",
    }
    assert trigger.config == {
        "delivery_target_json": {
            "channel": "discord",
            "channel_id": "nested-channel",
        }
    }
    assert runtime_task.metadata_json == {
        "delivery_target_json": {
            "channel": "wechat_personal",
            "to_user_id": "runtime-user",
        }
    }
    assert ingress_event.payload_json == {
        "_channel_ingress_provider": "discord",
        "body": {
            "data": {"value": "public-event"},
        },
    }
