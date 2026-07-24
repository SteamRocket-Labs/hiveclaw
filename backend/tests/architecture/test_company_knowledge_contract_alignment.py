from __future__ import annotations

import inspect

import pytest


@pytest.mark.asyncio
async def test_default_runtime_does_not_prefetch_personal_or_company_knowledge() -> None:
    from app.runtime.invoker import _resolve_retrieval_context

    assert await _resolve_retrieval_context(object(), None) == ""
    source = inspect.getsource(_resolve_retrieval_context)
    assert "never prefetches Personal or Company KB content" in source
    assert 'return ""' in source


def test_company_tools_describe_fresh_authority_and_tool_only_disclosure() -> None:
    from app.tools.handlers.knowledge import (
        explain_company_kb_source,
        propose_company_kb_update,
        read_company_kb,
        search_company_kb,
    )

    search_description = search_company_kb.meta.description
    read_description = read_company_kb.meta.description
    proposal_description = propose_company_kb_update.meta.description
    explain_description = explain_company_kb_source.meta.description

    assert "authenticated execution frame" in search_description
    assert "source ACL" in search_description
    assert "complete-evidence" in search_description
    assert "Denied candidates do not leak" in search_description
    assert "tool-only" in search_description
    assert "never prefetched" in search_description
    assert "re-evaluates current read and cite permissions" in read_description
    assert "not_found_or_denied" in read_description
    assert "never publishes" in proposal_description
    assert "human review" in proposal_description
    assert "never returns artifact paths" in explain_description


def test_company_gateway_never_reads_canonical_artifact_files() -> None:
    import app.services.company_knowledge_gateway as gateway

    source = inspect.getsource(gateway)
    assert ".read_text(" not in source
    assert ".read_bytes(" not in source
    assert "Path(" not in source


def test_company_retrieval_routes_are_live_under_both_api_prefixes() -> None:
    from app.main import app

    paths = set(app.openapi()["paths"])
    suffixes = {
        "/knowledge/company/search",
        "/knowledge/company/documents",
        "/knowledge/company/documents/{document_id}",
        "/knowledge/company/evidence/{evidence_id}",
        "/knowledge/company/capabilities",
    }
    for prefix in ("/api", "/api/v1"):
        assert {f"{prefix}{suffix}" for suffix in suffixes} <= paths
