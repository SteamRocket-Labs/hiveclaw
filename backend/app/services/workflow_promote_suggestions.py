"""Neutral review evidence for ephemeral Workflow definitions.

This module groups every observed run without deciding that repetition or
completion means promotion. The agent/model or user may use the complete facts
to author a promotion proposal; the platform never registers one itself.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import or_, select

from app.database import tenant_scoped_session
from app.models.runtime_task import RuntimeTask
from app.models.workflow import WorkflowDefinitionRecord


@dataclass(slots=True)
class PromoteSuggestion:
    definition_hash: str
    name: str
    run_count: int
    sample_run_ids: list[uuid.UUID] = field(default_factory=list)
    quality_evidence: dict = field(default_factory=dict)


_PROMOTABLE_DEFINITION_SOURCES = {"ephemeral", "dynamic_workflow"}


def _mapping(value) -> dict:
    return value if isinstance(value, dict) else {}


def _quality_evidence_for_runs(runs: list[RuntimeTask]) -> dict:
    dynamic_evidence = []
    for run in runs:
        metadata = _mapping(run.metadata_json)
        dynamic = _mapping(metadata.get("dynamic_workflow"))
        evidence = _mapping(dynamic.get("outcome_evidence"))
        if evidence:
            dynamic_evidence.append(evidence)

    status_counts: dict[str, int] = {}
    for run in runs:
        status = str(run.status or "unknown")
        status_counts[status] = status_counts.get(status, 0) + 1
    payload = {
        "model_promotion_review": "not_requested",
        "status_counts": status_counts,
        "runs_with_outcome_evidence": len(dynamic_evidence),
    }
    if dynamic_evidence:
        payload.update(
            {
                "steps_failed": sum(int(evidence.get("steps_failed") or 0) for evidence in dynamic_evidence),
                "leaf_failed": sum(int(evidence.get("leaf_failed") or 0) for evidence in dynamic_evidence),
                "leaf_total": sum(int(evidence.get("leaf_total") or 0) for evidence in dynamic_evidence),
                "success_criteria_count": max(
                    int(evidence.get("success_criteria_count") or 0) for evidence in dynamic_evidence
                ),
            }
        )
    return payload


async def collect_promote_suggestions(
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID | None = None,
    session_factory=None,
    threshold: int | None = None,
) -> list[PromoteSuggestion]:
    """Group all ephemeral runs by definition_hash for neutral review.

    ``threshold`` is accepted for API compatibility but cannot hide evidence.
    ``agent_id`` narrows the evidence to the requested authority scope.
    """
    _ = threshold

    async with tenant_scoped_session(str(tenant_id), session_factory=session_factory) as session:
        criteria = [RuntimeTask.task_type == "workflow"]
        if agent_id is not None:
            criteria.append(RuntimeTask.parent_agent_id == agent_id)
        rows = (await session.execute(select(RuntimeTask).where(*criteria))).scalars().all()
        # Explicit tenant filter (belt-and-braces over RLS, same spirit as the
        # runtime_tasks metadata-mirror filter below): NULL = platform-curated
        # templates, whose names are taken for every tenant.
        registered_names = set(
            (
                await session.execute(
                    select(WorkflowDefinitionRecord.name).where(
                        WorkflowDefinitionRecord.status.in_(("draft", "active")),
                        or_(
                            WorkflowDefinitionRecord.tenant_id == tenant_id,
                            WorkflowDefinitionRecord.tenant_id.is_(None),
                        ),
                    )
                )
            )
            .scalars()
            .all()
        )

    by_hash: dict[str, list[RuntimeTask]] = {}
    for row in rows:
        metadata = row.metadata_json or {}
        if metadata.get("tenant_id") != str(tenant_id):
            continue  # tenant_id column is nullable/backfilled; mirror filters
        if metadata.get("definition_source") not in _PROMOTABLE_DEFINITION_SOURCES:
            continue
        definition_hash = metadata.get("definition_hash")
        if not definition_hash:
            continue
        by_hash.setdefault(definition_hash, []).append(row)

    suggestions: list[PromoteSuggestion] = []
    for definition_hash, runs in by_hash.items():
        name = ((runs[0].metadata_json or {}).get("definition_json") or {}).get("name") or "unnamed-workflow"
        if name in registered_names:
            continue  # already a template — nothing to suggest
        suggestions.append(
            PromoteSuggestion(
                definition_hash=definition_hash,
                name=name,
                run_count=len(runs),
                sample_run_ids=[run.id for run in runs],
                quality_evidence=_quality_evidence_for_runs(runs),
            )
        )
    return suggestions
