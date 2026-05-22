from __future__ import annotations

from app.memory.md_store import parse_entry_line, parse_entry_record


def test_parse_entry_record_preserves_metadata_tokens() -> None:
    record = parse_entry_record(
        "- [2026-05-22][sensitivity=PL3_sensitive][status=active][entry_id=abc123] "
        "Q3 salary planning requires owner-only handling"
    )

    assert record.content == "Q3 salary planning requires owner-only handling"
    assert record.timestamp == "2026-05-22"
    assert record.metadata == {
        "sensitivity": "PL3_sensitive",
        "status": "active",
        "entry_id": "abc123",
    }


def test_parse_entry_line_keeps_backward_compatible_tuple() -> None:
    content, timestamp = parse_entry_line(
        "- [2026-05-22][sensitivity=PL3_sensitive] Q3 salary planning requires owner-only handling"
    )

    assert content == "Q3 salary planning requires owner-only handling"
    assert timestamp == "2026-05-22"
