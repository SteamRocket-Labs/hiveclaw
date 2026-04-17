"""Unit tests for HindsightBackend.

Uses httpx.MockTransport to stub the Hindsight HTTP API — we verify request
construction and response parsing without touching a real server.

Test Double rationale: Hindsight is an external HTTP service at a network
boundary. Per our testing policy (CLAUDE.md), external APIs may be replaced
with test doubles as long as the rationale is explicit. Integration tests
against a real Hindsight container live in test_hindsight_backend_integration.py.
"""

from __future__ import annotations

import json
import uuid

import httpx
import pytest

from app.memory.backend import MemoryBackend, ScoredMemory
from app.memory.backends.hindsight import HindsightBackend


TENANT = uuid.UUID("12345678-1234-5678-1234-567812345678")
AGENT = uuid.UUID("87654321-4321-8765-4321-876543218765")


def _make_backend(handler) -> HindsightBackend:
    transport = httpx.MockTransport(handler)
    client = httpx.AsyncClient(
        base_url="http://mock.local",
        transport=transport,
        headers={"Authorization": "Bearer test-key"},
    )
    return HindsightBackend(
        tenant_id=TENANT,
        url="http://mock.local",
        api_key="test-key",
        client=client,
    )


# ── bank_id naming ─────────────────────────────────────────────


def test_bank_id_encodes_tenant_and_agent() -> None:
    backend = HindsightBackend(tenant_id=TENANT, url="http://x")
    bank_id = backend._bank_id(AGENT)
    assert bank_id == f"hive-t{TENANT.hex}-a{AGENT.hex}"
    assert TENANT.hex in bank_id
    assert AGENT.hex in bank_id


def test_different_tenants_produce_different_bank_ids() -> None:
    backend_a = HindsightBackend(tenant_id=TENANT, url="http://x")
    backend_b = HindsightBackend(tenant_id=uuid.uuid4(), url="http://x")
    assert backend_a._bank_id(AGENT) != backend_b._bank_id(AGENT)


def test_protocol_conformance() -> None:
    backend = HindsightBackend(tenant_id=TENANT, url="http://x")
    assert isinstance(backend, MemoryBackend)


def test_empty_url_rejected() -> None:
    with pytest.raises(ValueError):
        HindsightBackend(tenant_id=TENANT, url="")


# ── search: request construction ───────────────────────────────


@pytest.mark.asyncio
async def test_search_sends_correct_bank_and_query() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        captured["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={"results": []})

    backend = _make_backend(handler)
    await backend.search(AGENT, "test query", limit=5)

    assert f"hive-t{TENANT.hex}-a{AGENT.hex}" in captured["url"]
    assert captured["url"].endswith("/memories/recall")
    assert captured["body"]["query"] == "test query"
    assert captured["body"]["budget"] == "mid"  # limit=5 → mid
    assert captured["auth"] == "Bearer test-key"
    await backend.close()


@pytest.mark.asyncio
async def test_search_maps_limit_to_budget() -> None:
    budgets: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        budgets.append(json.loads(request.content)["budget"])
        return httpx.Response(200, json={"results": []})

    backend = _make_backend(handler)
    await backend.search(AGENT, "q", limit=2)
    await backend.search(AGENT, "q", limit=10)
    await backend.search(AGENT, "q", limit=50)
    assert budgets == ["low", "mid", "high"]
    await backend.close()


@pytest.mark.asyncio
async def test_search_maps_category_to_tag() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"results": []})

    backend = _make_backend(handler)
    await backend.search(AGENT, "q", category="feedback")
    assert captured["body"]["tags"] == ["category:feedback"]
    assert captured["body"]["tags_match"] == "any"
    await backend.close()


# ── search: response parsing ───────────────────────────────────


@pytest.mark.asyncio
async def test_search_parses_results_with_descending_scores() -> None:
    response_body = {
        "results": [
            {"id": "m1", "text": "first", "type": "experience", "timestamp": "2026-04-01T00:00:00Z"},
            {"id": "m2", "text": "second", "type": "world", "timestamp": "2026-04-02T00:00:00Z"},
            {"id": "m3", "text": "third", "type": "observation", "timestamp": "2026-04-03T00:00:00Z"},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_body)

    backend = _make_backend(handler)
    results = await backend.search(AGENT, "q", limit=5)

    assert len(results) == 3
    assert [r.content for r in results] == ["first", "second", "third"]
    assert results[0].score > results[1].score > results[2].score
    assert all(isinstance(r, ScoredMemory) for r in results)
    assert results[0].metadata["hindsight_id"] == "m1"
    assert results[0].metadata["type"] == "experience"
    await backend.close()


@pytest.mark.asyncio
async def test_search_extracts_category_from_tag() -> None:
    response_body = {
        "results": [
            {"id": "m1", "text": "x", "tags": ["category:knowledge", "other"], "type": "world"},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_body)

    backend = _make_backend(handler)
    results = await backend.search(AGENT, "q")
    assert results[0].category == "knowledge"
    await backend.close()


@pytest.mark.asyncio
async def test_search_respects_limit() -> None:
    response_body = {
        "results": [{"id": f"m{i}", "text": f"item-{i}"} for i in range(20)]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_body)

    backend = _make_backend(handler)
    results = await backend.search(AGENT, "q", limit=3)
    assert len(results) == 3
    await backend.close()


# ── search: date filtering (client-side post-filter) ───────────


@pytest.mark.asyncio
async def test_search_filters_by_date_from() -> None:
    response_body = {
        "results": [
            {"id": "m1", "text": "old", "timestamp": "2026-03-01T00:00:00Z"},
            {"id": "m2", "text": "new", "timestamp": "2026-04-15T00:00:00Z"},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_body)

    backend = _make_backend(handler)
    results = await backend.search(AGENT, "q", date_from="2026-04-01T00:00:00Z")
    assert len(results) == 1
    assert results[0].content == "new"
    await backend.close()


@pytest.mark.asyncio
async def test_search_filters_by_date_range() -> None:
    response_body = {
        "results": [
            {"id": "m1", "text": "before", "timestamp": "2026-03-01T00:00:00Z"},
            {"id": "m2", "text": "during", "timestamp": "2026-04-05T00:00:00Z"},
            {"id": "m3", "text": "after", "timestamp": "2026-05-01T00:00:00Z"},
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_body)

    backend = _make_backend(handler)
    results = await backend.search(
        AGENT, "q",
        date_from="2026-04-01T00:00:00Z",
        date_to="2026-04-30T00:00:00Z",
    )
    assert len(results) == 1
    assert results[0].content == "during"
    await backend.close()


@pytest.mark.asyncio
async def test_search_keeps_results_without_timestamp() -> None:
    response_body = {
        "results": [
            {"id": "m1", "text": "no-ts"},  # missing timestamp
        ]
    }

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=response_body)

    backend = _make_backend(handler)
    results = await backend.search(AGENT, "q", date_from="2026-04-01T00:00:00Z")
    # Unknown timestamps must not be silently dropped
    assert len(results) == 1
    await backend.close()


# ── search: graceful degradation ───────────────────────────────


@pytest.mark.asyncio
async def test_search_returns_empty_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500, json={"error": "internal"})

    backend = _make_backend(handler)
    results = await backend.search(AGENT, "q")
    assert results == []
    await backend.close()


@pytest.mark.asyncio
async def test_search_returns_empty_on_connection_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    backend = _make_backend(handler)
    results = await backend.search(AGENT, "q")
    assert results == []
    await backend.close()


@pytest.mark.asyncio
async def test_search_returns_empty_on_malformed_response() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b"not-json")

    backend = _make_backend(handler)
    results = await backend.search(AGENT, "q")
    assert results == []
    await backend.close()


# ── reflect ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reflect_returns_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path.endswith("/reflect")
        assert json.loads(request.content)["query"] == "what about Alice?"
        return httpx.Response(200, json={"text": "Alice works at Google."})

    backend = _make_backend(handler)
    result = await backend.reflect(AGENT, "what about Alice?")
    assert result == "Alice works at Google."
    await backend.close()


@pytest.mark.asyncio
async def test_reflect_returns_none_on_empty_text() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"text": "   "})

    backend = _make_backend(handler)
    result = await backend.reflect(AGENT, "q")
    assert result is None
    await backend.close()


@pytest.mark.asyncio
async def test_reflect_returns_none_on_http_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    backend = _make_backend(handler)
    result = await backend.reflect(AGENT, "q")
    assert result is None
    await backend.close()


# ── store / retain_batch ───────────────────────────────────────


@pytest.mark.asyncio
async def test_store_is_noop() -> None:
    """store() never hits the network in Phase 1."""
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        pytest.fail("store() must not make HTTP requests in Phase 1")

    backend = _make_backend(handler)
    # Any of these calls should be silent no-ops
    await backend.store(AGENT, "content", "feedback")
    await backend.store(AGENT, "x", "y", timestamp="2026-04-17T00:00:00Z", metadata={"a": 1})
    await backend.close()


@pytest.mark.asyncio
async def test_retain_batch_sends_items_with_category_tag() -> None:
    captured: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"success": True})

    backend = _make_backend(handler)
    ok = await backend.retain_batch(AGENT, [
        {"content": "fact 1", "category": "knowledge", "timestamp": "2026-04-01T00:00:00Z", "document_id": "abc123"},
        {"content": "fact 2", "category": "feedback", "timestamp": "2026-04-02T00:00:00Z", "document_id": "def456"},
    ])

    assert ok is True
    assert captured["url"].endswith(f"/banks/hive-t{TENANT.hex}-a{AGENT.hex}/memories")
    assert len(captured["body"]["items"]) == 2
    assert captured["body"]["items"][0]["tags"] == ["category:knowledge"]
    assert captured["body"]["items"][0]["document_id"] == "abc123"
    assert captured["body"]["async"] is True
    await backend.close()


@pytest.mark.asyncio
async def test_retain_batch_empty_items_short_circuits() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        del request
        pytest.fail("retain_batch with empty items must not make HTTP requests")

    backend = _make_backend(handler)
    assert await backend.retain_batch(AGENT, []) is True
    await backend.close()


@pytest.mark.asyncio
async def test_retain_batch_returns_false_on_failure() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(500)

    backend = _make_backend(handler)
    ok = await backend.retain_batch(AGENT, [{"content": "x", "category": "c"}])
    assert ok is False
    await backend.close()
