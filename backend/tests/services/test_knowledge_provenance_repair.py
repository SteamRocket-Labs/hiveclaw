from __future__ import annotations

import json
from types import SimpleNamespace
from uuid import uuid4

import pytest

from app.memory.t2.segment_package import (
    _is_t2_semantic_source_event,
    _knowledge_provenance_excluded_transcript_event_ids,
)
from app.services.knowledge_provenance_repair import (
    classify_legacy_knowledge_event,
    repair_legacy_knowledge_provenance,
)


def _legacy_event(*, sensitivity: str = "PL3_sensitive") -> SimpleNamespace:
    event_id = uuid4()
    document_id = str(uuid4())
    segment_id = str(uuid4())
    content = json.dumps(
        {
            "name": "search_personal_kb",
            "status": "done",
            "result": json.dumps(
                {
                    "status": "ok",
                    "authority": {"allowed": True, "authority_source": "owner_direct_interactive"},
                    "results": [
                        {
                            "document_id": document_id,
                            "segment_id": segment_id,
                            "source_ref": f"kb://person/alice/documents/{document_id}#segment={segment_id}",
                            "snippet": "legacy typed body",
                            "sensitivity": sensitivity,
                        }
                    ],
                }
            ),
        }
    )
    return SimpleNamespace(
        id=event_id,
        event_type="tool_result",
        content=content,
        metadata_json={"tool_name": "search_personal_kb"},
        tenant_id=uuid4(),
        agent_id=uuid4(),
        session_id=uuid4(),
        run_id=uuid4(),
        message_id=None,
        user_id=None,
        external_principal_id=None,
        participant_id=None,
        sequence=12,
    )


def test_classify_legacy_knowledge_event_uses_exact_tool_envelope() -> None:
    event = _legacy_event()

    repair = classify_legacy_knowledge_event(event)

    assert repair is not None
    assert repair["target_transcript_event_id"] == str(event.id)
    assert repair["knowledge_provenance"]["max_sensitivity"] == "PL3_sensitive"
    assert repair["semantic_memory_eligible"] is False
    assert "legacy typed body" not in json.dumps(repair)

    event.content = "benign prose says search_personal_kb and PL4_credential"
    assert classify_legacy_knowledge_event(event) is None


@pytest.mark.asyncio
async def test_repair_is_append_only_idempotent_and_never_copies_knowledge_body(monkeypatch) -> None:
    event = _legacy_event()
    appended: list[dict] = []

    class _Scalars:
        def __init__(self, rows):
            self.rows = rows

        def all(self):
            return self.rows

    class _Result:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return _Scalars(self.rows)

    class _DB:
        def __init__(self):
            self.results = [_Result([]), _Result([event])]
            self.flush_count = 0

        async def execute(self, _statement):
            return self.results.pop(0)

        async def flush(self):
            self.flush_count += 1

    async def fake_append_session_event(**kwargs):
        appended.append(kwargs)
        return SimpleNamespace(event_id=uuid4())

    monkeypatch.setattr(
        "app.services.knowledge_provenance_repair.append_session_event",
        fake_append_session_event,
    )

    db = _DB()
    report = await repair_legacy_knowledge_provenance(db, apply=True)

    assert report["mode"] == "apply"
    assert report["knowledge_results"] == 1
    assert report["sensitive_results"] == 1
    assert report["repair_events_appended"] == 1
    assert db.flush_count == 1
    assert len(appended) == 1
    written = appended[0]
    assert written["event_type"] == "knowledge_provenance_repair"
    assert written["parent_event_id"] == event.id
    assert written["metadata"]["target_transcript_event_id"] == str(event.id)
    assert written["metadata"]["projection_only"] is True
    assert written["metadata"]["semantic_memory_eligible"] is False
    assert "legacy typed body" not in written["content"]


def test_t2_excludes_legacy_event_when_append_only_sensitive_repair_exists() -> None:
    target_id = str(uuid4())
    legacy = SimpleNamespace(
        event_type="tool_result",
        metadata={"transcript_event_id": target_id},
    )
    repair = SimpleNamespace(
        event_type="knowledge_provenance_repair",
        metadata={
            "target_transcript_event_id": target_id,
            "content_sensitivity": "PL3_sensitive",
            "semantic_memory_eligible": False,
            "projection_only": True,
        },
    )

    excluded = _knowledge_provenance_excluded_transcript_event_ids([legacy, repair])

    assert excluded == {target_id}
    assert _is_t2_semantic_source_event(legacy, excluded_transcript_event_ids=excluded) is False
    assert _is_t2_semantic_source_event(repair, excluded_transcript_event_ids=excluded) is False
