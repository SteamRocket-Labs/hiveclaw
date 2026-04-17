"""Integration tests for HindsightBackend against a real Hindsight server.

These tests require a running Hindsight server. Start one with:

    docker run --rm -p 8888:8888 -p 9999:9999 \\
      -e HINDSIGHT_API_LLM_API_KEY=$OPENAI_API_KEY \\
      -v $HOME/.hindsight-docker:/home/hindsight/.pg0 \\
      ghcr.io/vectorize-io/hindsight:latest

Then export the URL and run only integration tests:

    export HINDSIGHT_URL=http://localhost:8888
    export HINDSIGHT_API_KEY=<your-key-if-any>
    pytest tests/memory/test_hindsight_backend_integration.py -v

Without HINDSIGHT_URL these tests are skipped silently.
"""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest

from app.memory.backends.hindsight import HindsightBackend

HINDSIGHT_URL = os.environ.get("HINDSIGHT_URL", "").strip()
HINDSIGHT_API_KEY = os.environ.get("HINDSIGHT_API_KEY", "").strip()

pytestmark = pytest.mark.skipif(
    not HINDSIGHT_URL,
    reason="HINDSIGHT_URL not set — skipping real Hindsight integration tests",
)


def _new_tenant() -> uuid.UUID:
    return uuid.uuid4()


def _new_agent() -> uuid.UUID:
    return uuid.uuid4()


async def _seed(backend: HindsightBackend, agent_id: uuid.UUID, count: int = 3) -> None:
    items = [
        {
            "content": f"Alice works at Company-{i}. She prefers Python over Java.",
            "category": "knowledge",
            "timestamp": f"2026-04-{15 + i:02d}T10:00:00Z",
            "document_id": f"doc-{agent_id.hex[:8]}-{i}",
        }
        for i in range(count)
    ]
    await backend.retain_batch(agent_id, items)
    # Hindsight processes retains async; give it a moment
    await asyncio.sleep(2.0)


@pytest.mark.asyncio
async def test_end_to_end_retain_and_recall() -> None:
    tenant = _new_tenant()
    agent = _new_agent()
    backend = HindsightBackend(
        tenant_id=tenant,
        url=HINDSIGHT_URL,
        api_key=HINDSIGHT_API_KEY,
        timeout=30.0,
    )
    try:
        await _seed(backend, agent)
        results = await backend.search(agent, "What does Alice do?", limit=5)
        assert len(results) > 0, "expected at least one recall hit"
        assert any("Alice" in r.content for r in results)
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_agent_isolation_within_tenant() -> None:
    """Two agents in the same tenant must not see each other's memories."""
    tenant = _new_tenant()
    agent_a = _new_agent()
    agent_b = _new_agent()

    backend = HindsightBackend(
        tenant_id=tenant, url=HINDSIGHT_URL, api_key=HINDSIGHT_API_KEY, timeout=30.0,
    )
    try:
        # Only seed agent_a
        await _seed(backend, agent_a)
        # agent_b has no memories — recall should return empty
        results_b = await backend.search(agent_b, "Alice", limit=5)
        assert len(results_b) == 0, (
            f"agent_b leaked {len(results_b)} hits from agent_a's bank"
        )
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_tenant_isolation_via_bank_naming() -> None:
    """Two tenants with the same agent_id must have distinct banks."""
    agent = _new_agent()  # same agent UUID across tenants
    tenant_a = _new_tenant()
    tenant_b = _new_tenant()

    backend_a = HindsightBackend(
        tenant_id=tenant_a, url=HINDSIGHT_URL, api_key=HINDSIGHT_API_KEY, timeout=30.0,
    )
    backend_b = HindsightBackend(
        tenant_id=tenant_b, url=HINDSIGHT_URL, api_key=HINDSIGHT_API_KEY, timeout=30.0,
    )
    try:
        # Tenant A seeds memories for this agent
        await _seed(backend_a, agent)
        # Tenant B queries the same agent — should see nothing
        results_b = await backend_b.search(agent, "Alice", limit=5)
        assert len(results_b) == 0, (
            f"tenant_b leaked {len(results_b)} hits from tenant_a's bank — "
            f"bank_id isolation failed"
        )
    finally:
        await backend_a.close()
        await backend_b.close()


@pytest.mark.asyncio
async def test_degradation_on_unreachable_server() -> None:
    """Pointing at a dead URL must return [] / None, not raise."""
    backend = HindsightBackend(
        tenant_id=_new_tenant(),
        url="http://127.0.0.1:1",  # connection refused
        api_key="",
        timeout=2.0,
    )
    try:
        assert await backend.search(_new_agent(), "q") == []
        assert await backend.reflect(_new_agent(), "q") is None
        assert await backend.retain_batch(
            _new_agent(), [{"content": "x", "category": "c"}]
        ) is False
    finally:
        await backend.close()


@pytest.mark.asyncio
async def test_category_filter_roundtrip() -> None:
    """retain_batch tags with category:X → search category=X returns only those."""
    tenant = _new_tenant()
    agent = _new_agent()
    backend = HindsightBackend(
        tenant_id=tenant, url=HINDSIGHT_URL, api_key=HINDSIGHT_API_KEY, timeout=30.0,
    )
    try:
        await backend.retain_batch(agent, [
            {"content": "Blue is nice.", "category": "feedback",
             "timestamp": "2026-04-01T00:00:00Z", "document_id": "doc-fb-1"},
            {"content": "Python ships with asyncio.", "category": "knowledge",
             "timestamp": "2026-04-02T00:00:00Z", "document_id": "doc-kn-1"},
        ])
        await asyncio.sleep(3.0)

        feedback_hits = await backend.search(agent, "nice", category="feedback", limit=10)
        knowledge_hits = await backend.search(agent, "Python", category="knowledge", limit=10)

        assert any("Blue" in h.content for h in feedback_hits)
        assert all(h.category == "feedback" for h in feedback_hits if h.category)
        assert any("Python" in h.content for h in knowledge_hits)
    finally:
        await backend.close()
