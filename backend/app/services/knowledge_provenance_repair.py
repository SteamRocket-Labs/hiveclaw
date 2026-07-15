"""Append-only repair for legacy Knowledge transcript provenance.

The repair reads only exact historical ``tool_result`` envelopes. It never
copies Knowledge bodies into the repair event and never mutates the original
transcript event. T0/T2 consumers use the typed correction receipt to exclude
legacy PL3/PL4 source events from future semantic distillation.
"""

from __future__ import annotations

import json
import uuid
from collections import Counter
from collections.abc import Mapping
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.chat_transcript_event import ChatTranscriptEvent
from app.services.chat_transcript import append_session_event
from app.services.knowledge_provenance import (
    KNOWLEDGE_TOOL_NAMES,
    KNOWLEDGE_PROVENANCE_KEY,
    enrich_knowledge_event_metadata,
    knowledge_content_sensitivity,
)
from app.services.privacy_layer import SensitivityLevel, canonicalize_sensitivity, sensitivity_rank


REPAIR_VERSION = "knowledge_provenance_backfill.v1"


def classify_legacy_knowledge_event(event: Any) -> dict[str, Any] | None:
    """Return a body-free repair receipt for one exact legacy tool result."""

    if str(getattr(event, "event_type", "") or "") != "tool_result":
        return None
    metadata = getattr(event, "metadata_json", None)
    original_metadata = dict(metadata) if isinstance(metadata, Mapping) else {}
    if isinstance(original_metadata.get(KNOWLEDGE_PROVENANCE_KEY), Mapping):
        return None
    enriched = enrich_knowledge_event_metadata(
        event_type="tool_result",
        content=getattr(event, "content", None),
        metadata=original_metadata,
    )
    provenance = enriched.get(KNOWLEDGE_PROVENANCE_KEY)
    if not isinstance(provenance, Mapping):
        return None
    sensitivity = knowledge_content_sensitivity(enriched)
    if sensitivity is None:
        return None
    semantic_memory_eligible = sensitivity_rank(canonicalize_sensitivity(sensitivity)) < sensitivity_rank(
        SensitivityLevel.PL3_SENSITIVE
    )
    return {
        "schema": "hive.knowledge_provenance_repair.v1",
        "repair_version": REPAIR_VERSION,
        "target_transcript_event_id": str(getattr(event, "id", "") or ""),
        "target_transcript_sequence": getattr(event, "sequence", None),
        "knowledge_provenance": dict(provenance),
        "content_sensitivity": sensitivity,
        "semantic_memory_eligible": semantic_memory_eligible,
        "projection_only": True,
    }


async def repair_legacy_knowledge_provenance(
    db: AsyncSession,
    *,
    apply: bool = False,
    tenant_id: uuid.UUID | None = None,
    agent_id: uuid.UUID | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """Classify or append idempotent repair receipts across transcript truth."""

    existing_query = select(ChatTranscriptEvent).where(
        ChatTranscriptEvent.event_type == "knowledge_provenance_repair"
    )
    candidate_query = select(ChatTranscriptEvent).where(
        ChatTranscriptEvent.event_type == "tool_result",
        ChatTranscriptEvent.metadata_json["tool_name"].as_string().in_(tuple(sorted(KNOWLEDGE_TOOL_NAMES))),
    )
    if tenant_id is not None:
        existing_query = existing_query.where(ChatTranscriptEvent.tenant_id == tenant_id)
        candidate_query = candidate_query.where(ChatTranscriptEvent.tenant_id == tenant_id)
    if agent_id is not None:
        existing_query = existing_query.where(ChatTranscriptEvent.agent_id == agent_id)
        candidate_query = candidate_query.where(ChatTranscriptEvent.agent_id == agent_id)
    candidate_query = candidate_query.order_by(
        ChatTranscriptEvent.created_at.asc(),
        ChatTranscriptEvent.id.asc(),
    )
    if limit is not None:
        candidate_query = candidate_query.limit(max(0, int(limit)))

    existing_rows = list((await db.execute(existing_query)).scalars().all())
    repaired_target_ids = {
        str((row.metadata_json or {}).get("target_transcript_event_id") or "").strip()
        for row in existing_rows
        if isinstance(getattr(row, "metadata_json", None), Mapping)
    }
    repaired_target_ids.discard("")
    candidates = list((await db.execute(candidate_query)).scalars().all())

    tenant_counts: Counter[str] = Counter()
    report: dict[str, Any] = {
        "mode": "apply" if apply else "dry_run",
        "repair_version": REPAIR_VERSION,
        "tool_results_scanned": len(candidates),
        "already_labeled": 0,
        "knowledge_results": 0,
        "sensitive_results": 0,
        "already_repaired": 0,
        "repair_events_appended": 0,
        "affected_sessions": 0,
    }
    affected_sessions: set[str] = set()

    for event in candidates:
        metadata = getattr(event, "metadata_json", None)
        if isinstance(metadata, Mapping) and isinstance(metadata.get(KNOWLEDGE_PROVENANCE_KEY), Mapping):
            report["already_labeled"] += 1
            continue
        repair = classify_legacy_knowledge_event(event)
        if repair is None:
            continue
        report["knowledge_results"] += 1
        tenant_counts[str(event.tenant_id)] += 1
        affected_sessions.add(str(event.session_id))
        sensitivity = canonicalize_sensitivity(repair["content_sensitivity"])
        if sensitivity_rank(sensitivity) >= sensitivity_rank(SensitivityLevel.PL3_SENSITIVE):
            report["sensitive_results"] += 1
        target_id = repair["target_transcript_event_id"]
        if target_id in repaired_target_ids:
            report["already_repaired"] += 1
            continue
        if not apply:
            continue

        event_payload = {
            "schema": repair["schema"],
            "repair_version": REPAIR_VERSION,
            "target_transcript_event_id": target_id,
            "content_sensitivity": repair["content_sensitivity"],
        }
        await append_session_event(
            db=db,
            agent_id=event.agent_id,
            tenant_id=event.tenant_id,
            session_id=event.session_id,
            run_id=event.run_id,
            actor_type="system",
            event_type="knowledge_provenance_repair",
            role="system",
            t0_role="system",
            parent_event_id=event.id,
            content=json.dumps(event_payload, ensure_ascii=False, sort_keys=True),
            source="knowledge_provenance_repair",
            materialize_chat_message=False,
            metadata={
                **repair,
                "source": "knowledge_provenance_repair",
                "idempotency_key": f"{REPAIR_VERSION}:{target_id}",
                "target_event_ref": f"transcript://event/{target_id}",
            },
        )
        repaired_target_ids.add(target_id)
        report["repair_events_appended"] += 1

    if apply and report["repair_events_appended"] and hasattr(db, "flush"):
        await db.flush()
    report["affected_sessions"] = len(affected_sessions)
    report["knowledge_results_by_tenant"] = dict(sorted(tenant_counts.items()))
    return report
