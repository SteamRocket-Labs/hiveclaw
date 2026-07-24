from __future__ import annotations

import pytest
from types import SimpleNamespace
from uuid import uuid4


def test_exact_secret_boundary_ignores_secret_shaped_examples_without_a_binding() -> None:
    from app.services.exact_secret_boundary import ExactSecretBoundary

    boundary = ExactSecretBoundary.empty()
    text = "Fixture: api_key=sk-example-abcdefghijklmnopqrstuvwxyz"

    assert boundary.match_payload({"document": {"body": text}}) == ()
    assert boundary.redact_text(text).text == text


def test_exact_secret_boundary_matches_nested_active_binding_and_preserves_other_bytes() -> None:
    from app.services.exact_secret_boundary import ExactSecretBoundary

    active_secret = "sk-live-tenant-secret-0123456789"
    boundary = ExactSecretBoundary.from_pairs((("tool-config://tenant-1/search/api_key", active_secret),))
    payload = {
        "document": {
            "body": f"prefix::{active_secret}::suffix",
            "fixture": "api_key=sk-example-abcdefghijklmnopqrstuvwxyz",
        }
    }

    assert boundary.match_payload(payload) == ("tool-config://tenant-1/search/api_key",)
    decision = boundary.redact_text(payload["document"]["body"])
    assert decision.text == "prefix::[REDACTED_SECRET]::suffix"
    assert decision.matched_refs == ("tool-config://tenant-1/search/api_key",)
    assert decision.redacted_count == 1


def test_short_exact_binding_does_not_match_inside_unrelated_words() -> None:
    from app.services.exact_secret_boundary import ExactSecretBoundary

    boundary = ExactSecretBoundary.from_pairs((("local-model://api_key", "k"),))

    assert boundary.match_payload({"result": "ok"}) == ()
    assert boundary.redact_text("value=k").text == "value=[REDACTED_SECRET]"


@pytest.mark.asyncio
async def test_stream_redactor_catches_secret_split_across_chunks_without_rewriting_other_text() -> None:
    from app.services.exact_secret_boundary import ExactSecretBoundary, ExactSecretStreamRedactor

    active_secret = "sk-live-stream-secret-0123456789"
    emitted: list[str] = []
    boundary = ExactSecretBoundary.from_pairs((("llm-model://model-1/api_key", active_secret),))
    redactor = ExactSecretStreamRedactor(boundary, emitted.append)

    await redactor.feed(f"alpha::{active_secret[:11]}")
    await redactor.feed(f"{active_secret[11:]}::omega")
    await redactor.finish()

    assert "".join(emitted) == "alpha::[REDACTED_SECRET]::omega"
    assert redactor.redacted_count == 1
    assert redactor.matched_refs == ("llm-model://model-1/api_key",)


def test_payload_redaction_covers_secret_bearing_keys_and_returns_evidence() -> None:
    from app.services.exact_secret_boundary import ExactSecretBoundary

    active_secret = "sk-live-payload-secret-0123456789"
    boundary = ExactSecretBoundary.from_pairs((("tool-config://tenant-1/search/api_key", active_secret),))

    redaction = boundary.redact_payload_with_evidence(
        {
            f"header::{active_secret}": {
                "value": f"prefix::{active_secret}::suffix",
            }
        }
    )

    assert redaction.value == {
        "header::[REDACTED_SECRET]": {
            "value": "prefix::[REDACTED_SECRET]::suffix",
        }
    }
    assert redaction.redacted_count == 2
    assert redaction.matched_refs == ("tool-config://tenant-1/search/api_key",)


def test_exact_secret_boundary_matches_secret_split_across_binary_chunks() -> None:
    from app.services.exact_secret_boundary import ExactSecretBoundary

    secret = "file-secret-0123456789"
    boundary = ExactSecretBoundary.from_pairs((("secret://file", secret),))

    assert boundary.match_binary_chunks(
        [
            b"prefix::file-secret-",
            b"0123456789::suffix",
        ]
    ) == ("secret://file",)


def test_reply_target_boundary_uses_only_typed_channel_secret_fields() -> None:
    from app.services.exact_secret_boundary import boundary_from_reply_target

    boundary = boundary_from_reply_target(
        {
            "channel": "discord",
            "interaction_token": "interaction-secret-0123456789",
            "channel_id": "public-channel-id",
            "description": "api_key=sk-example-not-authority",
        }
    )

    assert boundary.match_payload("interaction-secret-0123456789") == ("channel-target://discord/interaction_token",)
    assert boundary.match_payload("public-channel-id") == ()
    assert boundary.match_payload("api_key=sk-example-not-authority") == ()


@pytest.mark.asyncio
async def test_credential_loader_builds_boundary_from_tenant_tool_password_field() -> None:
    from app.services.credential_boundary_loader import load_exact_secret_boundary

    class _Scalars:
        def __init__(self, values):
            self._values = values

        def all(self):
            return list(self._values)

    class _Result:
        def __init__(self, values):
            self._values = values

        def scalars(self):
            return _Scalars(self._values)

    class _DB:
        def __init__(self, responses):
            self._responses = list(responses)

        async def execute(self, _statement):
            return _Result(self._responses.pop(0))

    tenant_id = uuid4()
    tool_id = uuid4()
    active_secret = "exa-live-tenant-secret-0123456789"
    tool = SimpleNamespace(
        id=tool_id,
        config={"api_key": active_secret, "search_engine": "exa"},
        config_schema={
            "fields": [
                {"key": "api_key", "type": "password"},
                {"key": "search_engine", "type": "select"},
            ]
        },
    )
    db = _DB(
        [
            [],  # enabled tenant LLM models
            [],  # agent channel configs
            [],  # tenant channel configs
            [tool],
            [],  # tenant tool overrides
            [],  # MCP servers
        ]
    )

    boundary = await load_exact_secret_boundary(
        db,
        tenant_id=tenant_id,
        agent_id=None,
    )

    refs = boundary.match_payload({"nested": {"value": active_secret}})
    assert refs == (f"tool-config://{tenant_id}/tenant/{tool_id}/api_key",)
    assert boundary.match_payload({"nested": {"value": "api_key=sk-example-abcdefghijklmnopqrstuvwxyz"}}) == ()
