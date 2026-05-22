from __future__ import annotations


def test_prepare_memory_write_rejects_pl4_credentials() -> None:
    from app.memory.write_gate import prepare_memory_write

    decision = prepare_memory_write(
        "Owner Alice shared api_key=sk-1234567890abcdefghijklmnop for setup.",
        category="reference",
        evidence_refs=["t0:behavior/chat-1.md#L4"],
    )

    assert decision.rejected is True
    assert decision.sensitivity == "PL4_credential"
    assert "sk-1234567890abcdefghijklmnop" not in decision.content
    assert decision.metadata["sensitivity"] == "PL4_credential"
    assert decision.metadata["status"] == "rejected"


def test_prepare_memory_write_masks_pii_and_adds_lifecycle_metadata() -> None:
    from app.memory.write_gate import prepare_memory_write

    decision = prepare_memory_write(
        "Owner Alice email is alice@example.com for vendor escalation.",
        category="user",
        evidence_refs=["t0:behavior/chat-2.md#L8"],
    )

    assert decision.rejected is False
    assert decision.content == "Owner Alice email is <Email_1> for vendor escalation."
    assert decision.metadata["sensitivity"] == "PL2_pii"
    assert decision.metadata["status"] == "active"
    assert decision.metadata["version"] == "1"
    assert decision.metadata["access_count"] == "0"
    assert decision.metadata["last_accessed"] == "never"
    assert decision.metadata["evidence_refs"] == "t0:behavior/chat-2.md#L8"
    assert decision.metadata["entry_id"]
