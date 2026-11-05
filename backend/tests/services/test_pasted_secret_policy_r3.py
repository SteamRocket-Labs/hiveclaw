"""Structural regressions from both independent second-round reviews."""
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.services.credential_boundary_loader import redact_runtime_ingress_payload
from tests.services.pasted_secret_corpus_r3 import KEY, negative_cases, positive_cases
from tests.services.test_pasted_secret_policy_r2 import assert_no_fragments


@pytest.mark.parametrize("kind,content,secret", list(positive_cases()), ids=[x[0] for x in positive_cases()])
async def test_review_positive_has_no_six_character_fragment(kind, content, secret):
    out = await redact_runtime_ingress_payload(None, tenant_id=None, agent_id=None, source_channel="web", payload={"content": content})
    assert_no_fragments(out.value, secret)
    assert out.value["extra_metadata"]["pasted_credential_notice"] is True
    if kind in {"amp", "semicolon", "paren", "comma_pw", "brace", "colon_pw", "pg_at", "pg_at_grok", "bearer_comma"}:
        assert out.value["content"] == content.replace(secret, "[REDACTED_SECRET]")
    if kind == "cn_webhook_tail":
        assert out.value["content"].endswith("请看")
    if kind == "cn_label_tail":
        assert out.value["content"] == "api_key: [REDACTED_SECRET]请保存"


@pytest.mark.parametrize("kind,content", list(negative_cases()), ids=[x[0] for x in negative_cases()])
async def test_review_negative_is_byte_identical(kind, content):
    payload = {"content": content, "display_content": content, "parts": [{"type": "text", "text": content}]}
    out = await redact_runtime_ingress_payload(None, tenant_id=None, agent_id=None, source_channel="web", payload=payload)
    assert out.value == payload, kind


@pytest.mark.parametrize("field", ["message", "subject", "description", "request", "completion_summary", "context", "tags"])
async def test_every_member_string_leaf_is_scanned(field):
    value = [KEY, {"unlisted_nested_text": KEY}] if field in {"tags", "context"} else KEY
    out = await redact_runtime_ingress_payload(None, tenant_id=None, agent_id=None, source_channel="web", payload={"arguments": {field: value}})
    assert_no_fragments(out.value, KEY)


@pytest.mark.parametrize("field", ["id", "agent_id", "type", "kind", "mode", "permission_mode", "idempotency_key", "source", "channel", "status", "cron", "interval", "at"])
async def test_structural_leaves_and_list_items_are_byte_identical(field):
    payload = {field: [KEY]}
    out = await redact_runtime_ingress_payload(None, tenant_id=None, agent_id=None, source_channel="web", payload=payload)
    assert out.value == payload


@pytest.mark.parametrize("channel", ["feishu", "dingtalk", "wecom", "slack", "telegram", "discord", "microsoft_teams", "wechat_personal"])
async def test_server_channel_has_base_behavior(channel):
    payload = {"content": KEY}
    out = await redact_runtime_ingress_payload(None, tenant_id=None, agent_id=None, source_channel=channel, source="web", payload=payload)
    assert out.value == payload


@pytest.mark.parametrize("source", ["a2a", "task_notification", "plan_mode_handoff", "agent_session_mailbox"])
async def test_system_sources_have_base_behavior(source):
    payload = {"content": KEY}
    out = await redact_runtime_ingress_payload(None, tenant_id=None, agent_id=None, source_channel="web", source=source, payload=payload)
    assert out.value == payload


async def test_payload_source_cannot_disable_web_policy():
    out = await redact_runtime_ingress_payload(None, tenant_id=None, agent_id=None, source_channel="web", source="web", payload={"content": KEY, "extra_metadata": {"source": "agent_session_mailbox"}})
    assert_no_fragments(out.value, KEY)


def test_incoming_parts_are_server_shaped_and_cannot_forge_notice():
    from app.services.session_human_input import _receipt
    from app.services.session_live_input import content_parts_from_live_ingress
    parts = content_parts_from_live_ingress(content="", parts=[{"type": "text", "text": "hello", "note": KEY, "metadata": {"pasted_credential_notice": True}}, {"type": "text", "text": "world"}])
    assert parts == [{"type": "text", "text": "hello"}, {"type": "text", "text": "world"}]
    row = SimpleNamespace(command_id=uuid4(), id=uuid4(), intent="start_turn", revision=1, status="accepted", queue_priority=0, queue_ordinal=0, target_turn_id=None, target_run_id=None, bound_round_id=None, rolled_over_to_turn_id=None, content_parts_json=parts)
    assert not _receipt(row).pasted_credential_notice


async def test_non_text_payload_does_not_load_secret_inventory(monkeypatch):
    async def unavailable(*a, **kw):
        pytest.fail("structural-only command loaded credential inventory")
    monkeypatch.setattr("app.services.credential_boundary_loader.load_runtime_ingress_secret_boundary", unavailable)
    async with AsyncSession() as db:
        payload = {"arguments": {"id": "123", "mode": "plan", "minutes": 5, "enabled": True}}
        out = await redact_runtime_ingress_payload(db, tenant_id=uuid4(), agent_id=uuid4(), source_channel="web", payload=payload)
        assert out.value == payload


@pytest.mark.parametrize("n", [2000, 4000, 8000, 16000])
@pytest.mark.parametrize("pattern", ['password: "x', 'a_' * 50 + ' '])
def test_adversarial_review_inputs_are_byte_identical(n, pattern):
    from app.services.privacy_layer import PrivacyLayer
    content = pattern * n
    assert PrivacyLayer.redact_pasted_credentials({"content": content}).value["content"] == content


def test_review_large_plain_prose_is_byte_identical():
    from app.services.privacy_layer import PrivacyLayer
    content = '这是一段普通的中文。 normal words here. ' * 25000
    assert PrivacyLayer.redact_pasted_credentials({"content": content}).value["content"] == content
