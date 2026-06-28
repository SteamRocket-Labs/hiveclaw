"""Governance-facing truth search evidence service."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from app.runtime.ccplus_contracts import TruthEvidencePackV1
from app.services import viking_client
from app.services.connector_acl import filter_connector_results_for_prompt


def _optional_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _source_ref(item: dict[str, Any]) -> str:
    source = _optional_str(item.get("source") or item.get("path") or item.get("uri") or item.get("id"))
    return f"knowledge://{source}" if source else "knowledge://unknown"


def _citation(item: dict[str, Any]) -> str:
    return _optional_str(item.get("source") or item.get("path") or item.get("title") or item.get("id")) or "unknown"


def _digest_payload(query: str, source_refs: tuple[str, ...], results: list[dict[str, Any]]) -> str:
    payload = {
        "query": query,
        "source_refs": source_refs,
        "results": [
            {
                "id": item.get("id"),
                "source": item.get("source") or item.get("path"),
                "content": (item.get("content") or item.get("text") or "")[:2000],
            }
            for item in results
        ],
    }
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")).hexdigest()


class TruthSearchService:
    """Return source-bound evidence packs for governance and answer citation."""

    async def search(
        self,
        query: str,
        *,
        tenant_id: uuid.UUID | str | None = None,
        agent_id: uuid.UUID | str | None = None,
        current_user_id: uuid.UUID | str | None = None,
        limit: int = 3,
    ) -> list[TruthEvidencePackV1]:
        clean_query = str(query or "").strip()
        tenant = _optional_str(tenant_id)
        agent = _optional_str(agent_id)
        user = _optional_str(current_user_id)
        if not clean_query or not tenant or (not agent and not user):
            return []
        if not viking_client.is_configured():
            return []

        try:
            raw_results = await viking_client.find(
                clean_query,
                tenant_id=tenant,
                agent_id=agent,
                user_id=user,
                limit=limit,
            )
        except Exception:
            return []

        visible_results = filter_connector_results_for_prompt(
            [item for item in raw_results if isinstance(item, dict)],
            tenant_id=tenant,
            agent_id=agent,
            current_user_id=user,
        )
        if not visible_results:
            return []

        source_refs = tuple(dict.fromkeys(_source_ref(item) for item in visible_results))
        citations = tuple(dict.fromkeys(_citation(item) for item in visible_results))
        digest = _digest_payload(clean_query, source_refs, visible_results)
        return [
            TruthEvidencePackV1(
                evidence_id=f"truth://{digest[:24]}",
                query=clean_query,
                source_refs=source_refs,
                citations=citations,
                acl_scope="tenant",
                digest=digest,
                provider="openviking",
                freshness="runtime",
                confidence=None,
                limitations=(),
                prompt_injection_stripped=False,
                tenant_id=tenant,
                owner_id=user,
                company_id=tenant,
                trace_refs=(f"truth_search:{digest[:24]}",),
            )
        ]
