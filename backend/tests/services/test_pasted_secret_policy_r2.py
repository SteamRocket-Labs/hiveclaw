"""Precision and field-local replacement at the existing ingress API."""
from uuid import uuid4

import pytest

from app.services.credential_boundary_loader import redact_runtime_ingress_payload
from tests.services.pasted_secret_corpus import negative_cases, positive_cases, review_negative_cases


def strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            yield key
            yield from strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from strings(item)


def assert_no_fragments(value, secret):
    for item in strings(value):
        assert secret not in item if len(secret) < 6 else not any(secret[i:i + 6] in item for i in range(len(secret) - 5)), item


@pytest.mark.parametrize("kind,content,secret", list(positive_cases()), ids=lambda x: str(x)[:60])
async def test_each_positive_is_fully_hidden(kind, content, secret):
    payload = {field: content for field in ("content", "display_content", "title", "name", "objective", "prompt", "instruction")}
    payload["parts"] = [{"type": "text", "text": content}]
    result = await redact_runtime_ingress_payload(object(), tenant_id=uuid4(), agent_id=uuid4(), source_channel="web", payload=payload)
    assert_no_fragments(result.value, secret)
    assert result.value["extra_metadata"]["pasted_credential_notice"] is True, kind
    assert "我把一段密钥" not in str(result.value)


@pytest.mark.parametrize("kind,content", list(negative_cases()) + list(review_negative_cases()))
async def test_each_negative_is_byte_identical(kind, content):
    payload = {"content": content, "display_content": content, "parts": [{"type": "text", "text": content}], "extra_metadata": {"permission_mode": "plan"}}
    result = await redact_runtime_ingress_payload(object(), tenant_id=uuid4(), agent_id=uuid4(), source_channel="web", payload=payload)
    assert result.value == payload, kind


async def test_pasted_match_only_replaces_its_span_in_authored_fields():
    value = "Ab1Cd2Ef3Gh4"
    payload = {"content": f"password={value}\nAn ordinary ID: {value}", "id": value, "extra_metadata": {"permission_mode": value}}
    result = await redact_runtime_ingress_payload(object(), tenant_id=uuid4(), agent_id=uuid4(), source_channel="web", payload=payload)
    assert result.value["content"] == f"password=[REDACTED_SECRET]\nAn ordinary ID: {value}"
    assert result.value["id"] == result.value["extra_metadata"]["permission_mode"] == value


async def test_quoted_multi_word_passphrase_is_a_known_limit():
    # A quoted sentence has the same syntax; preserve it until the vault slice.
    payload = {"content": 'password="correct horse battery staple"'}
    result = await redact_runtime_ingress_payload(object(), tenant_id=uuid4(), agent_id=uuid4(), source_channel="web", payload=payload)
    assert result.value == payload
