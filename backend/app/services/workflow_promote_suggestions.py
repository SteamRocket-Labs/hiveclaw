"""Promote suggestions (§9 P13, §4/§6.6 seed): repeated ephemeral evidence.

When the same ephemeral definition (same definition_hash) completes
``WORKFLOW_PROMOTE_SUGGESTION_THRESHOLD`` times, that is the evidence the
product path waits for — suggest 保存为模板/自动化. This module only
OBSERVES and proposes (§10 decision 4: the agent/user still walks the
promote-proposal → human-approval path); it never registers anything itself.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import select

from app.config import get_settings
from app.database import tenant_scoped_session
from app.models.runtime_task import RuntimeTask
from app.models.workflow import WorkflowDefinitionRecord


@dataclass(slots=True)
class PromoteSuggestion:
    definition_hash: str
    name: str
    run_count: int
    sample_run_ids: list[uuid.UUID] = field(default_factory=list)


async def collect_promote_suggestions(
    *,
    tenant_id: uuid.UUID,
    session_factory=None,
    threshold: int | None = None,
) -> list[PromoteSuggestion]:
    """Group completed ephemeral runs by definition_hash; suggest promotion
    once a hash crosses the threshold and no registered definition already
    carries that name."""
    limit = threshold if threshold is not None else get_settings().WORKFLOW_PROMOTE_SUGGESTION_THRESHOLD

    async with tenant_scoped_session(str(tenant_id), session_factory=session_factory) as session:
        rows = (
            (
                await session.execute(
                    select(RuntimeTask).where(RuntimeTask.task_type == "workflow", RuntimeTask.status == "completed")
                )
            )
            .scalars()
            .all()
        )
        registered_names = set(
            (
                await session.execute(
                    select(WorkflowDefinitionRecord.name).where(
                        WorkflowDefinitionRecord.status.in_(("draft", "active"))
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
            continue  # runtime_tasks has no tenant column; the mirror filters
        if metadata.get("definition_source") != "ephemeral":
            continue
        definition_hash = metadata.get("definition_hash")
        if not definition_hash:
            continue
        by_hash.setdefault(definition_hash, []).append(row)

    suggestions: list[PromoteSuggestion] = []
    for definition_hash, runs in by_hash.items():
        if len(runs) < limit:
            continue
        name = ((runs[0].metadata_json or {}).get("definition_json") or {}).get("name") or "unnamed-workflow"
        if name in registered_names:
            continue  # already a template — nothing to suggest
        suggestions.append(
            PromoteSuggestion(
                definition_hash=definition_hash,
                name=name,
                run_count=len(runs),
                sample_run_ids=[run.id for run in runs[:5]],
            )
        )
    return suggestions
