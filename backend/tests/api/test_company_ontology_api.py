from __future__ import annotations

import pytest


def test_company_ontology_routes_are_live_under_both_api_prefixes() -> None:
    from app.main import app

    paths = set(app.openapi()["paths"])
    relative_paths = {
        "/knowledge/company/ontology/packages",
        "/knowledge/company/ontology/package-installations",
        "/knowledge/company/ontology/package-installations/{installation_id}",
        "/knowledge/company/ontology/activations",
        "/knowledge/company/ontology/activations/{activation_id}/dry-run",
        "/knowledge/company/ontology/curation-runs",
        "/knowledge/company/ontology/curation-runs/{run_id}",
        "/knowledge/company/ontology/curation-runs/{run_id}/publish",
        "/knowledge/company/ontology/query",
        "/knowledge/company/ontology/types",
        "/knowledge/company/ontology/objects",
        "/knowledge/company/ontology/objects/{object_id}",
        "/knowledge/company/ontology/facts/{assertion_id}/evidence",
        "/knowledge/company/ontology/actions/{action_type_ref}/simulate",
        "/knowledge/company/ontology/releases",
        "/knowledge/company/ontology/releases/{release_id}",
        "/knowledge/company/ontology/releases/{release_id}/retire",
        "/knowledge/company/ontology/releases/{release_id}/restore",
        "/knowledge/company/ontology/capabilities",
    }

    for prefix in ("/api", "/api/v1"):
        assert {f"{prefix}{path}" for path in relative_paths} <= paths


def test_company_ontology_mutation_schemas_never_accept_actor_or_tenant_identity() -> None:
    from app.main import app

    schema = app.openapi()
    mutation_paths = {
        "/api/knowledge/company/ontology/package-installations",
        "/api/knowledge/company/ontology/activations",
        "/api/knowledge/company/ontology/curation-runs/{run_id}/publish",
        "/api/knowledge/company/ontology/releases/{release_id}/retire",
        "/api/knowledge/company/ontology/releases/{release_id}/restore",
    }
    forbidden = {
        "tenant_id",
        "actor_id",
        "accountable_user_id",
        "published_by_user_id",
        "installed_by_user_id",
        "activated_by_user_id",
        "model_execution_receipt",
    }

    for path in mutation_paths:
        operation = schema["paths"][path]["post"]
        request_body = operation.get("requestBody", {})
        rendered = str(request_body)
        assert not any(field in rendered for field in forbidden)


def test_company_ontology_curation_is_agent_tool_only_not_a_browser_payload_api() -> None:
    from app.main import app

    schema = app.openapi()

    for prefix in ("/api", "/api/v1"):
        operations = schema["paths"][f"{prefix}/knowledge/company/ontology/curation-runs"]
        assert "get" in operations
        assert "post" not in operations


@pytest.mark.asyncio
async def test_company_ontology_engine_outage_maps_to_retryable_service_unavailable() -> None:
    from fastapi import HTTPException

    from app.api.knowledge_company import _call
    from app.services.company_ontology_engine import OntologyEngineUnavailable

    async def _unavailable() -> None:
        raise OntologyEngineUnavailable("provider detail must not leak")

    with pytest.raises(HTTPException) as raised:
        await _call(_unavailable())

    assert raised.value.status_code == 503
    assert raised.value.detail == "company_ontology_engine_unavailable"
