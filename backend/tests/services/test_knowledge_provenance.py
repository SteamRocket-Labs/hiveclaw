from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

from app.memory.t0.ledger import append_t0_session_event, replay_t0_session_events
from app.services.knowledge_provenance import (
    build_knowledge_provenance,
    enrich_knowledge_event_metadata,
    load_transcript_knowledge_provenance,
    merge_knowledge_provenance,
)


def _authority(*, action: str = "search") -> dict[str, object]:
    return {
        "schema": "hive.personal_knowledge_permission_decision.v1",
        "allowed": True,
        "action": action,
        "owner_user_id": str(uuid4()),
        "authority_source": "owner_direct_interactive",
        "sensitivity_ceiling": "PL3_sensitive",
        "deny_reason_code": None,
        "document_id": None,
        "document_sensitivity": None,
        "grant_id": None,
        "expires_at": None,
        "credential_reference_only": False,
        "retryable": False,
        "principal": {
            "principal_type": "human_browser",
            "requester_user_id": str(uuid4()),
            "session_id": str(uuid4()),
            "purpose": "interactive_user_request",
        },
    }


def test_search_result_builds_complete_typed_provenance_without_scanning_body() -> None:
    document_public = str(uuid4())
    document_sensitive = str(uuid4())
    segment_public = str(uuid4())
    segment_sensitive = str(uuid4())
    payload = {
        "status": "ok",
        "authority": _authority(),
        "results": [
            {
                "result_kind": "knowledge_segment",
                "document_id": document_public,
                "segment_id": segment_public,
                "source_ref": f"kb://person/alice/documents/{document_public}#segment={segment_public}",
                "snippet": "The words secret and salary are benign test text.",
                "sensitivity": "PL1_public",
            },
            {
                "result_kind": "knowledge_segment",
                "document_id": document_sensitive,
                "segment_id": segment_sensitive,
                "source_ref": f"kb://person/alice/documents/{document_sensitive}#segment={segment_sensitive}",
                "snippet": "A sentence with no security keywords at all.",
                "sensitivity": "PL3_sensitive",
            },
        ],
        "warnings": [],
    }

    provenance = build_knowledge_provenance("search_personal_kb", payload)

    assert provenance is not None
    assert provenance["schema"] == "hive.knowledge_provenance.v1"
    assert provenance["scope"] == "personal"
    assert provenance["tool_name"] == "search_personal_kb"
    assert provenance["max_sensitivity"] == "PL3_sensitive"
    assert provenance["semantic_memory_eligible"] is False
    assert provenance["authority"] == payload["authority"]
    assert provenance["coverage"] == {"result_count": 2, "source_count": 2, "complete": True}
    assert [source["sensitivity"] for source in provenance["sources"]] == ["PL1_public", "PL3_sensitive"]


def test_read_result_applies_document_sensitivity_to_every_segment() -> None:
    document_id = str(uuid4())
    first_segment = str(uuid4())
    second_segment = str(uuid4())
    payload = {
        "status": "ok",
        "document_id": document_id,
        "source_ref": f"kb://person/alice/documents/{document_id}",
        "sensitivity": "PL2_pii",
        "authority": _authority(action="read"),
        "segments": [
            {"segment_id": first_segment, "content": "first"},
            {"segment_id": second_segment, "content": "second"},
        ],
    }

    provenance = build_knowledge_provenance("read_personal_kb", json.dumps(payload))

    assert provenance is not None
    assert provenance["max_sensitivity"] == "PL2_pii"
    assert provenance["semantic_memory_eligible"] is True
    assert provenance["coverage"] == {"result_count": 2, "source_count": 2, "complete": True}
    assert {source["segment_id"] for source in provenance["sources"]} == {first_segment, second_segment}
    assert {source["sensitivity"] for source in provenance["sources"]} == {"PL2_pii"}


def test_invalid_typed_sensitivity_fails_closed_but_preserves_recovery_evidence() -> None:
    document_id = str(uuid4())
    payload = {
        "status": "ok",
        "authority": _authority(),
        "results": [
            {
                "document_id": document_id,
                "segment_id": str(uuid4()),
                "source_ref": f"kb://person/alice/documents/{document_id}",
                "snippet": "ordinary content",
                "sensitivity": "not-a-real-level",
            }
        ],
    }

    provenance = build_knowledge_provenance("search_personal_kb", payload)

    assert provenance is not None
    assert provenance["max_sensitivity"] == "PL4_credential"
    assert provenance["semantic_memory_eligible"] is False
    assert provenance["status"] == "held_invalid_sensitivity"
    assert provenance["warnings"] == ["invalid_sensitivity:not-a-real-level"]
    assert provenance["sources"][0]["declared_sensitivity"] == "not-a-real-level"


def test_non_knowledge_tool_and_empty_denied_result_do_not_taint_content() -> None:
    assert build_knowledge_provenance("read_file", {"sensitivity": "PL4_credential"}) is None
    assert (
        build_knowledge_provenance(
            "search_personal_kb",
            {"status": "denied", "results": [], "authority": _authority(), "warnings": ["denied"]},
        )
        is None
    )


def test_event_enrichment_reads_only_the_typed_tool_envelope() -> None:
    document_id = str(uuid4())
    segment_id = str(uuid4())
    result = {
        "status": "ok",
        "authority": _authority(),
        "results": [
            {
                "document_id": document_id,
                "segment_id": segment_id,
                "source_ref": f"kb://person/alice/documents/{document_id}#segment={segment_id}",
                "snippet": "No magic words are required.",
                "sensitivity": "PL3_sensitive",
            }
        ],
    }
    content = json.dumps(
        {
            "name": "search_personal_kb",
            "status": "done",
            "result": json.dumps(result),
        }
    )

    metadata = enrich_knowledge_event_metadata(
        event_type="tool_result",
        content=content,
        metadata={"source": "subagent", "semantic_memory_eligible": True},
    )

    assert metadata["content_sensitivity"] == "PL3_sensitive"
    assert metadata["semantic_memory_eligible"] is False
    assert metadata["knowledge_provenance"]["tool_name"] == "search_personal_kb"
    assert metadata["knowledge_provenance"]["coverage"]["complete"] is True

    benign = enrich_knowledge_event_metadata(
        event_type="assistant_message",
        content="This prose says sensitivity=PL4_credential but is not a typed tool envelope.",
        metadata={},
    )
    assert "knowledge_provenance" not in benign
    assert "content_sensitivity" not in benign


def test_event_enrichment_propagates_exact_nested_subagent_provenance() -> None:
    nested = {
        "schema": "hive.knowledge_provenance_aggregate.v1",
        "scope": ["personal"],
        "tool_names": ["read_personal_kb"],
        "max_sensitivity": "PL3_sensitive",
        "semantic_memory_eligible": False,
        "source_event_refs": ["t0://child/tool-result"],
        "coverage": {"source_event_count": 1, "complete": True},
        "event_manifest_sha256": "abc",
    }
    content = json.dumps(
        {
            "name": "spawn_subagent",
            "status": "done",
            "result": json.dumps({"ok": True, "content": "digest", "knowledge_provenance": nested}),
        }
    )

    metadata = enrich_knowledge_event_metadata(
        event_type="tool_result",
        content=content,
        metadata={"tool_name": "spawn_subagent"},
    )

    assert metadata["knowledge_provenance"] == nested
    assert metadata["content_sensitivity"] == "PL3_sensitive"
    assert metadata["semantic_memory_eligible"] is False


def test_merge_provenance_uses_lossless_event_refs_and_max_sensitivity() -> None:
    public = build_knowledge_provenance(
        "search_personal_kb",
        {
            "status": "ok",
            "authority": _authority(),
            "results": [
                {
                    "document_id": str(uuid4()),
                    "segment_id": str(uuid4()),
                    "source_ref": "kb://person/alice/public",
                    "sensitivity": "PL1_public",
                }
            ],
        },
    )
    sensitive = build_knowledge_provenance(
        "read_personal_kb",
        {
            "status": "ok",
            "document_id": str(uuid4()),
            "source_ref": "kb://person/alice/sensitive",
            "sensitivity": "PL3_sensitive",
            "authority": _authority(action="read"),
            "segments": [{"segment_id": str(uuid4()), "content": "body"}],
        },
    )
    assert public is not None and sensitive is not None

    merged = merge_knowledge_provenance(
        [
            ("transcript://event/public", public),
            ("transcript://event/sensitive", sensitive),
        ]
    )

    assert merged is not None
    assert merged["schema"] == "hive.knowledge_provenance_aggregate.v1"
    assert merged["max_sensitivity"] == "PL3_sensitive"
    assert merged["semantic_memory_eligible"] is False
    assert merged["source_event_refs"] == [
        "transcript://event/public",
        "transcript://event/sensitive",
    ]
    assert merged["coverage"] == {"source_event_count": 2, "complete": True}


def test_merge_provenance_flattens_nested_subagent_refs_without_losing_parent_receipt() -> None:
    nested = {
        "schema": "hive.knowledge_provenance_aggregate.v1",
        "scope": ["personal"],
        "tool_names": ["read_personal_kb"],
        "max_sensitivity": "PL3_sensitive",
        "semantic_memory_eligible": False,
        "source_event_refs": ["subagent://tool-call/call-kb"],
        "coverage": {"source_event_count": 1, "complete": True},
        "event_manifest_sha256": "nested-hash",
    }

    merged = merge_knowledge_provenance([("transcript://event/spawn-result", nested)])

    assert merged is not None
    assert merged["scope"] == ["personal"]
    assert merged["tool_names"] == ["read_personal_kb"]
    assert merged["source_event_refs"] == [
        "transcript://event/spawn-result",
        "subagent://tool-call/call-kb",
    ]


def test_t0_consumes_typed_sensitivity_and_excludes_sensitive_knowledge_from_t2(tmp_path: Path) -> None:
    agent_id = uuid4()
    tenant_id = uuid4()
    session_id = uuid4()
    document_id = str(uuid4())
    segment_id = str(uuid4())
    result = {
        "status": "ok",
        "authority": _authority(),
        "results": [
            {
                "document_id": document_id,
                "segment_id": segment_id,
                "source_ref": f"kb://person/alice/documents/{document_id}#segment={segment_id}",
                "snippet": "typed sensitive content with no keywords",
                "sensitivity": "PL3_sensitive",
            }
        ],
    }
    content = json.dumps(
        {"name": "search_personal_kb", "status": "done", "result": json.dumps(result)},
        sort_keys=True,
    )

    appended = append_t0_session_event(
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        event_type="tool_result",
        role="tool",
        content=content,
        metadata={"tool_name": "search_personal_kb"},
        data_root=tmp_path,
    )
    events = replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=tmp_path)

    assert len(events) == 1
    assert events[0].event_id == appended.event_id
    assert events[0].sensitivity == "PL3_sensitive"
    assert events[0].metadata["content_sensitivity"] == "PL3_sensitive"
    assert events[0].metadata["semantic_memory_eligible"] is False
    assert events[0].content == content

    from app.memory.t2.segment_package import _build_source_bundle

    try:
        _build_source_bundle(
            root=tmp_path,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            t0_segment_id=appended.segment_id,
            package_id="pkg-sensitive-kb",
        )
    except ValueError as exc:
        assert "no semantic T0 events" in str(exc)
    else:
        raise AssertionError("PL3 Knowledge evidence must not enter the T2 semantic source bundle")


async def test_transcript_aggregate_is_scoped_to_the_exact_run_and_uses_event_refs() -> None:
    run_id = uuid4()
    event_id = uuid4()
    provenance = build_knowledge_provenance(
        "read_personal_kb",
        {
            "status": "ok",
            "document_id": str(uuid4()),
            "source_ref": "kb://person/alice/private",
            "sensitivity": "PL3_sensitive",
            "authority": _authority(action="read"),
            "segments": [{"segment_id": str(uuid4()), "content": "private body"}],
        },
    )
    assert provenance is not None

    class _Scalars:
        def all(self):
            return [
                type(
                    "TranscriptEvent",
                    (),
                    {
                        "id": event_id,
                        "metadata_json": {"knowledge_provenance": provenance},
                    },
                )()
            ]

    class _Result:
        def scalars(self):
            return _Scalars()

    class _DB:
        def __init__(self):
            self.statement = None

        async def execute(self, statement):
            self.statement = statement
            return _Result()

    db = _DB()
    merged = await load_transcript_knowledge_provenance(
        db,
        tenant_id=uuid4(),
        agent_id=uuid4(),
        session_id=uuid4(),
        run_id=run_id,
    )

    assert db.statement is not None
    assert merged is not None
    assert merged["max_sensitivity"] == "PL3_sensitive"
    assert merged["source_event_refs"] == [f"transcript://event/{event_id}"]
