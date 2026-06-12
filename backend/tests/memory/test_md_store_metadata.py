from __future__ import annotations

from uuid import uuid4

from app.memory.md_store import append_t3_entry, build_t3_entry_manifest, parse_entry_line, parse_entry_record
from app.memory.lifecycle_store import bump_access_telemetry


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


def test_t3_manifest_derives_activation_aliases_from_sidecar(tmp_path) -> None:
    agent_id = uuid4()
    append_t3_entry(
        tmp_path,
        agent_id,
        category="knowledge",
        content="Owner prefers concise Railway incident summaries",
        metadata={
            "entry_id": "entry-activation-1",
            "conf": "0.91",
        },
    )
    for _ in range(4):
        bump_access_telemetry(tmp_path, agent_id, entry_id="entry-activation-1")

    entry = build_t3_entry_manifest(tmp_path, agent_id)[0]

    assert entry.metadata["confidence"] == "0.91"
    assert float(entry.metadata["retention_score"]) > 0
