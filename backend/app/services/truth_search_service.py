"""Governance-facing truth search evidence service."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from app.runtime.ccplus_contracts import TruthEvidencePackV1
from app.services import viking_client
from app.services.connector_acl import filter_connector_results_for_prompt

_DEFAULT_PROMPT_CHAR_BUDGET = 1500
_PROMPT_INJECTION_PATTERNS = (
    re.compile(r"\b(ignore|disregard|forget)\s+(all\s+)?(previous|prior|above)\s+instructions\b", re.IGNORECASE),
    re.compile(r"\b(system|developer)\s+prompt\b.*\b(ignore|override|replace|reveal|leak)\b", re.IGNORECASE),
    re.compile(r"\b(reveal|leak|print|show)\s+(the\s+)?(system|developer)\s+prompt\b", re.IGNORECASE),
)


def _optional_str(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _source_ref(item: dict[str, Any]) -> str:
    source = _optional_str(item.get("source") or item.get("path") or item.get("uri") or item.get("id"))
    return f"knowledge://{source}" if source else "knowledge://unknown"


def _citation(item: dict[str, Any]) -> str:
    return _optional_str(item.get("source") or item.get("path") or item.get("title") or item.get("id")) or "unknown"


def _snippet(item: dict[str, Any], *, max_chars: int) -> str:
    content = str(item.get("content") or item.get("text") or "").strip()
    if not content:
        return ""
    if len(content) <= max_chars:
        return content
    return content[: max(0, max_chars - 3)].rstrip() + "..."


def _strip_prompt_injection(text: str) -> tuple[str, bool]:
    stripped = False
    kept_lines: list[str] = []
    for line in str(text or "").splitlines():
        if any(pattern.search(line) for pattern in _PROMPT_INJECTION_PATTERNS):
            stripped = True
            continue
        kept_lines.append(line)
    return "\n".join(kept_lines).strip(), stripped


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
    return hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()


def _provider_failure_pack(
    query: str,
    *,
    tenant: str,
    agent: str | None,
    user: str | None,
    limitation: str,
) -> TruthEvidencePackV1:
    digest = hashlib.sha256(
        json.dumps(
            {
                "query": query,
                "tenant": tenant,
                "agent": agent,
                "user": user,
                "limitation": limitation,
            },
            ensure_ascii=False,
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    short_digest = digest[:24]
    return TruthEvidencePackV1(
        evidence_id=f"truth://provider-error/{short_digest}",
        query=query,
        source_refs=(),
        citations=(),
        snippets=(),
        acl_scope="tenant",
        digest=digest,
        provider="openviking",
        freshness="runtime",
        confidence=0.0,
        limitations=(limitation,),
        prompt_injection_stripped=False,
        tenant_id=tenant,
        owner_id=user,
        company_id=tenant,
        trace_refs=(f"truth_search_provider_error:{short_digest}",),
    )


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
        source_collector: list[dict[str, Any]] | None = None,
        max_chars: int | None = None,
    ) -> list[TruthEvidencePackV1]:
        clean_query = str(query or "").strip()
        tenant = _optional_str(tenant_id)
        agent = _optional_str(agent_id)
        user = _optional_str(current_user_id)
        if not clean_query or not tenant or (not agent and not user):
            return []
        if not viking_client.is_configured():
            return [
                _provider_failure_pack(
                    clean_query,
                    tenant=tenant,
                    agent=agent,
                    user=user,
                    limitation="provider_unconfigured",
                )
            ]

        try:
            raw_results = await viking_client.find(
                clean_query,
                tenant_id=tenant,
                agent_id=agent,
                user_id=user,
                limit=limit,
            )
        except Exception as exc:
            return [
                _provider_failure_pack(
                    clean_query,
                    tenant=tenant,
                    agent=agent,
                    user=user,
                    limitation=f"provider_error:{exc.__class__.__name__}",
                )
            ]

        visible_results = filter_connector_results_for_prompt(
            [item for item in raw_results if isinstance(item, dict)],
            tenant_id=tenant,
            agent_id=agent,
            current_user_id=user,
        )
        if not visible_results:
            return []
        if source_collector is not None:
            source_collector.extend(dict(item) for item in visible_results)

        source_refs = tuple(dict.fromkeys(_source_ref(item) for item in visible_results))
        citations = tuple(dict.fromkeys(_citation(item) for item in visible_results))
        char_budget = max_chars if isinstance(max_chars, int) and max_chars > 0 else _DEFAULT_PROMPT_CHAR_BUDGET
        per_item_budget = max(200, char_budget // max(len(visible_results), 1))
        snippet_items: list[str] = []
        prompt_injection_stripped = False
        for item in visible_results:
            snippet, stripped = _strip_prompt_injection(_snippet(item, max_chars=per_item_budget))
            prompt_injection_stripped = prompt_injection_stripped or stripped
            if snippet:
                snippet_items.append(snippet)
        snippets = tuple(snippet_items)
        digest = _digest_payload(clean_query, source_refs, visible_results)
        limitations = ("prompt_injection_stripped",) if prompt_injection_stripped else ()
        return [
            TruthEvidencePackV1(
                evidence_id=f"truth://{digest[:24]}",
                query=clean_query,
                source_refs=source_refs,
                citations=citations,
                snippets=snippets,
                acl_scope="tenant",
                digest=digest,
                provider="openviking",
                freshness="runtime",
                confidence=None,
                limitations=limitations,
                prompt_injection_stripped=prompt_injection_stripped,
                tenant_id=tenant,
                owner_id=user,
                company_id=tenant,
                trace_refs=(f"truth_search:{digest[:24]}",),
            )
        ]

    def render_prompt_context(
        self,
        evidence_packs: list[TruthEvidencePackV1] | tuple[TruthEvidencePackV1, ...],
        *,
        max_chars: int | None = None,
    ) -> str:
        """Render source-bound evidence for model context without changing authority.

        The returned block is evidence only. It names citations/source refs so the
        model can cite or challenge it, but does not grant permissions or inject
        hidden instructions.
        """
        if not evidence_packs:
            return ""
        char_budget = max_chars if isinstance(max_chars, int) and max_chars > 0 else _DEFAULT_PROMPT_CHAR_BUDGET
        parts: list[str] = []
        used = 0
        for pack in evidence_packs:
            citation = pack.citations[0] if pack.citations else (pack.source_refs[0] if pack.source_refs else "unknown")
            for snippet in pack.snippets:
                if not snippet.strip():
                    continue
                prefix = f"**[{citation}]**: "
                remaining = char_budget - used - len(prefix)
                if remaining <= 0:
                    break
                text = snippet if len(snippet) <= remaining else snippet[: max(0, remaining - 3)].rstrip() + "..."
                parts.append(prefix + text)
                used += len(prefix) + len(text)
            if used >= char_budget:
                break
        if not parts:
            return ""
        return "## Relevant Company Knowledge\n\n" + "\n\n".join(parts)
