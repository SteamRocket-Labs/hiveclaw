"""Hindsight memory backend — read-side accelerator over T3 markdown.

Design invariants:
- T3 markdown is the source of truth; Hindsight is a derived index.
- Every backend instance is scoped to a single tenant_id (constructor-required).
- bank_id = f"hive-t{tenant_id.hex}-a{agent_id.hex}" — tenant + agent double-encoded.
- store() is a no-op; heartbeat batches T3 MD into Hindsight asynchronously.
- All REMOTE errors degrade to empty/None — never raise. (Construction
  still raises ValueError on invalid URL — fail-fast at startup, not at query.)

Phase A deployment: single Hindsight instance + global API key, logical isolation
via bank_id. Upgrade to Phase B (per-tenant key + PG schema) when upstream
vectorize-io/hindsight PR #855 merges.

Backend resolution priority (see app/memory/backend.py): tenant.memory_backend
(nullable, per-tenant override) > env MEMORY_BACKEND > "md".

Operator runbook:
- Opt a tenant in:  PATCH /api/admin/companies/{id}/memory-backend
                    body: {"memory_backend": "hindsight"}
- Opt out:          same endpoint, {"memory_backend": null} (→ env fallback)
                    or {"memory_backend": "md"} (hard pin)
- Rebuild bank:     python -m app.admin.rebuild_hindsight --tenant-id <uuid>
                    [--agent-id <uuid>] — clears cursor, re-uploads T3 bullets
- Metrics:          GET /api/admin/metrics/memory → latency/error/sync snapshot
- Shutdown:         app lifespan calls aclose_all_backends() to drain httpx clients
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

import httpx

from app.memory.backend import ScoredMemory
from app.memory.metrics import (
    RecallTimer,
    record_sync_failure,
)

logger = logging.getLogger(__name__)

_CATEGORY_TAG_PREFIX = "category:"
_HINDSIGHT_API_PREFIX = "/v1/default/banks"


def _budget_for(limit: int) -> str:
    """Map our `limit` to Hindsight's `budget` (low/mid/high)."""
    if limit <= 3:
        return "low"
    if limit <= 10:
        return "mid"
    return "high"


def _parse_iso(value: str) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        logger.debug("[Hindsight] invalid ISO timestamp %r: %s", value, exc)
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _timestamp_in_range(
    timestamp: str,
    date_from: datetime | None,
    date_to: datetime | None,
) -> bool:
    if not (date_from or date_to):
        return True
    parsed = _parse_iso(timestamp)
    if parsed is None:
        # Unknown timestamp — keep (don't silently drop matches)
        return True
    if date_from and parsed < date_from:
        return False
    if date_to and parsed > date_to:
        return False
    return True


def _extract_category(tags: list[str] | None) -> str | None:
    for tag in tags or []:
        if tag.startswith(_CATEGORY_TAG_PREFIX):
            return tag[len(_CATEGORY_TAG_PREFIX):]
    return None


class HindsightBackend:
    """Tenant-scoped Hindsight client implementing MemoryBackend Protocol."""

    def __init__(
        self,
        *,
        tenant_id: uuid.UUID,
        url: str,
        api_key: str = "",
        timeout: float = 10.0,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not url:
            raise ValueError("HindsightBackend requires a non-empty url")
        self._tenant_id = tenant_id
        self._url = url.rstrip("/")
        self._api_key = api_key
        self._client = client or httpx.AsyncClient(
            base_url=self._url,
            timeout=httpx.Timeout(timeout, connect=3.0),
            headers={"Authorization": f"Bearer {api_key}"} if api_key else {},
        )
        self._owns_client = client is None

    def _bank_id(self, agent_id: uuid.UUID) -> str:
        return f"hive-t{self._tenant_id.hex}-a{agent_id.hex}"

    # -- MemoryBackend Protocol -----------------------------------------

    async def search(
        self,
        agent_id: uuid.UUID,
        query: str,
        *,
        limit: int = 10,
        date_from: str | None = None,
        date_to: str | None = None,
        category: str | None = None,
    ) -> list[ScoredMemory]:
        """Recall memories from Hindsight. Returns [] on any failure."""
        bank_id = self._bank_id(agent_id)
        body: dict[str, Any] = {
            "query": query,
            "budget": _budget_for(limit),
        }
        if category:
            body["tags"] = [f"{_CATEGORY_TAG_PREFIX}{category}"]
            body["tags_match"] = "any"

        async with RecallTimer("hindsight", self._tenant_id.hex) as timer:
            try:
                resp = await self._client.post(
                    f"{_HINDSIGHT_API_PREFIX}/{bank_id}/memories/recall",
                    json=body,
                )
                resp.raise_for_status()
                payload = resp.json()
            except Exception as exc:
                logger.warning(
                    "[Hindsight] recall failed (tenant=%s agent=%s): %s",
                    self._tenant_id, agent_id, exc,
                )
                timer.error_reason = type(exc).__name__
                return []

            df = _parse_iso(date_from) if date_from else None
            dt = _parse_iso(date_to) if date_to else None

            results: list[ScoredMemory] = []
            for index, hit in enumerate(payload.get("results", [])):
                timestamp = hit.get("timestamp", "") or ""
                if not _timestamp_in_range(timestamp, df, dt):
                    continue
                tags = hit.get("tags") or []
                hit_category = _extract_category(tags) or hit.get("type", "general")
                results.append(
                    ScoredMemory(
                        content=hit.get("text", ""),
                        category=hit_category,
                        # Strictly positive monotonic synthetic score (Hindsight
                        # doesn't return per-result scores through this API).
                        # 1/(n+1) preserves ordering for arbitrary result counts.
                        score=1.0 / (index + 1),
                        timestamp=timestamp,
                        metadata={
                            "hindsight_id": hit.get("id"),
                            "type": hit.get("type"),
                            "context": hit.get("context"),
                            "chunk_id": hit.get("chunk_id"),
                            "bank_id": bank_id,
                        },
                    )
                )
                if len(results) >= limit:
                    break
            timer.observed_results = len(results)
            return results

    async def store(
        self,
        agent_id: uuid.UUID,
        content: str,
        category: str,
        *,
        timestamp: str = "",
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """No-op in Phase 1. Heartbeat batches T3 MD into Hindsight via retain_batch()."""
        del agent_id, content, category, timestamp, metadata
        return None

    async def reflect(
        self,
        agent_id: uuid.UUID,
        topic: str,
    ) -> str | None:
        """Ask Hindsight to generate a consolidated reflection. None on failure."""
        bank_id = self._bank_id(agent_id)
        try:
            resp = await self._client.post(
                f"{_HINDSIGHT_API_PREFIX}/{bank_id}/reflect",
                json={"query": topic, "budget": "low"},
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:
            logger.warning(
                "[Hindsight] reflect failed (tenant=%s agent=%s): %s",
                self._tenant_id, agent_id, exc,
            )
            return None
        text = (payload.get("text") or "").strip()
        return text or None

    # -- Heartbeat-only helpers ----------------------------------------

    async def retain_batch(
        self,
        agent_id: uuid.UUID,
        items: list[dict[str, Any]],
    ) -> bool:
        """Batch-upload T3 MD entries. Called from heartbeat only, not hot path.

        items: [{"content": str, "timestamp": ISO-8601, "category": str,
                 "document_id": str (sha256 hash for idempotency)}]
        Returns True on success, False on failure (failures logged).
        """
        if not items:
            return True
        bank_id = self._bank_id(agent_id)
        payload_items = [
            {
                "content": item["content"],
                "timestamp": item.get("timestamp", ""),
                "document_id": item.get("document_id", ""),
                "tags": [f"{_CATEGORY_TAG_PREFIX}{item.get('category', 'general')}"],
            }
            for item in items
        ]
        try:
            resp = await self._client.post(
                f"{_HINDSIGHT_API_PREFIX}/{bank_id}/memories",
                json={"items": payload_items, "async": True},
            )
            resp.raise_for_status()
            return True
        except Exception as exc:
            logger.warning(
                "[Hindsight] retain_batch failed (tenant=%s agent=%s, n=%d): %s",
                self._tenant_id, agent_id, len(items), exc,
            )
            record_sync_failure(self._tenant_id.hex)
            return False

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
