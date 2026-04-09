from __future__ import annotations

from datetime import datetime, timezone
from uuid import uuid4

from app.api.channel_secrets import resolve_secret_field, resolve_secret_value
from app.schemas.schemas import ChannelConfigOut


def test_resolve_secret_helpers_preserve_existing_values_for_masked_or_missing_inputs():
    assert resolve_secret_value("****1234", "real-secret") == "real-secret"
    assert resolve_secret_value(None, "real-secret", preserve_missing=True) == "real-secret"
    assert resolve_secret_field({}, "bot_token", "real-secret") == "real-secret"
    assert resolve_secret_field({"bot_token": " new-secret "}, "bot_token", "real-secret") == "new-secret"


def test_channel_config_safe_masks_nested_wecom_bot_secret_and_verification_token():
    config = ChannelConfigOut(
        id=uuid4(),
        agent_id=uuid4(),
        channel_type="wecom",
        app_id="corp-id",
        app_secret="corp-secret-1234",
        encrypt_key="encoding-key-5678",
        verification_token="verification-token-9012",
        is_configured=True,
        is_connected=False,
        extra_config={
            "bot_id": "aibot-id",
            "bot_secret": "wecom-bot-secret-9999",
        },
        created_at=datetime.now(timezone.utc),
    )

    safe = config.to_safe()

    assert safe.app_secret == "****1234"
    assert safe.encrypt_key == "****5678"
    assert safe.verification_token == "****9012"
    assert safe.extra_config["bot_secret"] == "****9999"
