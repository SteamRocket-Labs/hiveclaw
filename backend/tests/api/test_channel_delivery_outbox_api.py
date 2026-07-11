from types import SimpleNamespace
import uuid

import pytest
from fastapi import HTTPException

from app.api.channel_deliveries import _redacted_delivery_target, _require_delivery_operator


def test_channel_delivery_admin_target_redacts_provider_secrets_and_ids():
    target = {
        "channel": "dingtalk",
        "conversation_id": "cid-secret",
        "session_webhook": "https://oapi.dingtalk.com/robot/send?access_token=secret",
        "interaction_token": "token-secret",
        "context_token": "context-secret",
        "sender_staff_id": "staff-123",
    }
    redacted = _redacted_delivery_target(target)
    assert redacted == {
        "channel": "dingtalk",
        "recipient_hint": "sta…123",
    }
    encoded = str(redacted)
    assert "secret" not in encoded
    assert "conversation_id" not in encoded


@pytest.mark.parametrize("role", ["org_admin", "platform_admin"])
def test_channel_delivery_operator_accepts_company_and_platform_admin(role):
    user = SimpleNamespace(role=role, tenant_id=uuid.uuid4())
    assert _require_delivery_operator(user) is user


def test_channel_delivery_operator_rejects_member():
    with pytest.raises(HTTPException) as exc:
        _require_delivery_operator(SimpleNamespace(role="member", tenant_id=uuid.uuid4()))
    assert exc.value.status_code == 403
