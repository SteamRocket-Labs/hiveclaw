from __future__ import annotations

import json
from uuid import uuid4

from app.services.knowledge_provenance import (
    build_knowledge_provenance,
    enrich_knowledge_event_metadata,
)
from app.services.web_chat_runtime import _knowledge_tool_replay_projection


def _authority() -> dict[str, object]:
    return {
        "schema": "hive.company_ontology_query_authority.v1",
        "authorized_release_ids": [str(uuid4())],
        "fresh_permission_per_item": True,
        "authorization_before_expansion": True,
    }


def test_ontology_query_provenance_covers_objects_facts_and_links() -> None:
    release_id = str(uuid4())
    object_id = str(uuid4())
    assertion_id = str(uuid4())
    link_id = str(uuid4())
    object_evidence = f"company-evidence://{uuid4()}"
    fact_evidence = f"company-evidence://{uuid4()}"
    link_evidence = f"company-evidence://{uuid4()}"
    payload = {
        "status": "ok",
        "authority": _authority(),
        "objects": [
            {
                "object_id": object_id,
                "release_id": release_id,
                "display_name": "Private project",
                "sensitivity": "PL2_pii",
                "source_refs": [object_evidence],
                "facts": [
                    {
                        "assertion_id": assertion_id,
                        "typed_value": "restricted",
                        "sensitivity": "PL3_sensitive",
                        "source_refs": [fact_evidence],
                    }
                ],
                "links": [
                    {
                        "link_id": link_id,
                        "properties": {"private": "value"},
                        "sensitivity": "PL1_public",
                        "source_refs": [link_evidence],
                    }
                ],
            }
        ],
    }

    provenance = build_knowledge_provenance("query_company_ontology", payload)

    assert provenance is not None
    assert provenance["scope"] == "company"
    assert provenance["tool_name"] == "query_company_ontology"
    assert provenance["max_sensitivity"] == "PL3_sensitive"
    assert provenance["semantic_memory_eligible"] is False
    assert provenance["authority"] == payload["authority"]
    assert provenance["coverage"] == {
        "result_count": 3,
        "source_count": 3,
        "complete": True,
    }
    assert {
        (
            source["result_kind"],
            source.get("object_id"),
            source.get("assertion_id"),
            source.get("link_id"),
            source["source_ref"],
        )
        for source in provenance["sources"]
    } == {
        ("ontology_object", object_id, None, None, object_evidence),
        ("ontology_assertion", object_id, assertion_id, None, fact_evidence),
        ("ontology_link", object_id, None, link_id, link_evidence),
    }


def test_ontology_fact_provenance_fails_closed_without_complete_typed_lineage() -> None:
    assertion_id = str(uuid4())
    payload = {
        "status": "ok",
        "authority": _authority(),
        "fact": {
            "assertion_id": assertion_id,
            "typed_value": "sensitive fact",
            "sensitivity": "not-a-sensitivity",
            "source_refs": [],
            "lineage": {
                "coverage": {"complete": False, "evidence_count": 0},
                "evidence": [],
            },
        },
    }

    provenance = build_knowledge_provenance("explain_company_fact", payload)

    assert provenance is not None
    assert provenance["status"] == "held_invalid_sensitivity"
    assert provenance["max_sensitivity"] == "PL4_credential"
    assert provenance["semantic_memory_eligible"] is False
    assert provenance["coverage"]["complete"] is False
    assert provenance["sources"][0]["assertion_id"] == assertion_id
    assert provenance["warnings"] == ["invalid_sensitivity:not-a-sensitivity"]


def test_ontology_tool_result_enrichment_and_replay_preserve_only_typed_references() -> None:
    object_id = str(uuid4())
    release_id = str(uuid4())
    evidence_ref = f"company-evidence://{uuid4()}"
    result = {
        "status": "ok",
        "authority": _authority(),
        "object": {
            "object_id": object_id,
            "release_id": release_id,
            "display_name": "CONFIDENTIAL-NAME",
            "properties": {"secret": "CONFIDENTIAL-PROPERTY"},
            "sensitivity": "PL3_sensitive",
            "source_refs": [evidence_ref],
            "facts": [],
            "links": [],
        },
        "warnings": [],
    }
    content = json.dumps(
        {
            "name": "get_company_object",
            "status": "done",
            "result": json.dumps(result),
        }
    )

    metadata = enrich_knowledge_event_metadata(
        event_type="tool_result",
        content=content,
        metadata={"tool_name": "get_company_object"},
    )
    projection = _knowledge_tool_replay_projection(
        tool_name="get_company_object",
        args={"object_id": object_id},
        raw_result=result,
    )

    assert metadata["content_sensitivity"] == "PL3_sensitive"
    assert metadata["semantic_memory_eligible"] is False
    assert projection is not None
    replay = json.loads(projection)
    assert replay["scope"] == "company"
    assert replay["result_count"] == 1
    assert replay["references"] == [
        {
            "release_id": release_id,
            "object_id": object_id,
            "source_ref": evidence_ref,
        }
    ]
    assert replay["instruction"] == (
        "Call query_company_ontology/get_company_object/explain_company_fact again if the content is needed."
    )
    assert "CONFIDENTIAL-NAME" not in projection
    assert "CONFIDENTIAL-PROPERTY" not in projection


def test_simulation_and_mutation_results_are_not_treated_as_knowledge_content() -> None:
    payload = {
        "status": "ok",
        "simulation": {"sensitivity": "PL4_credential"},
    }

    assert build_knowledge_provenance("simulate_company_action", payload) is None
    assert build_knowledge_provenance("propose_ontology_change", payload) is None
