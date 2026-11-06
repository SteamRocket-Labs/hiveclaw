"""Move company work and deliver one notice in the offboarding transaction."""

import json

from sqlalchemy import String, UniqueConstraint, delete, func, or_, select, update

from app.database import Base
from app.models.agent import Agent
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.knowledge import (
    KnowledgeAssertion, KnowledgeDocument, KnowledgeEntity, KnowledgeIndexJob, KnowledgeLink, KnowledgeSegment,
    PersonalKnowledgeProposal,
)
from app.models.notification import Notification
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionToolInvocation
from app.models.task import Task
from app.models.trigger import AgentTrigger
from app.models.user import User
from app.services.personal_knowledge_service import _merge_source_refs, _merge_unique_strings
from app.services.privacy_layer import max_sensitivity


async def _move_knowledge(db, target_user, successor):
    models = (KnowledgeDocument, KnowledgeSegment, KnowledgeEntity, KnowledgeAssertion, KnowledgeLink, KnowledgeIndexJob)
    rows, replacements, duplicates, documents = {}, {}, {}, []
    provenance = {model.__tablename__.removeprefix("knowledge_"): [] for model in models}
    losing_documents, deleted_segments, segment_counts = {}, {}, {}

    def repoint(value):
        if isinstance(value, dict):
            if value.get("segment_id") in deleted_segments:
                return {**{key: repoint(item) for key, item in value.items()
                    if key not in {"segment_id", "seg_hash", "segment_hash", "position"}},
                    "document_id": str(deleted_segments[value["segment_id"]])}
            return {key: repoint(item) for key, item in value.items()}
        if isinstance(value, list):
            return [repoint(item) for item in value]
        return replacements.get(value, value)

    for model in models:
        rows[model] = list((await db.scalars(select(model).where(model.tenant_id == target_user.tenant_id,
            model.scope_type == "person", model.scope_id.in_([target_user.id, successor.id]))
            .order_by(model.scope_id == target_user.id, model.id).with_for_update())).all())
        keys = [[col.name for col in constraint.columns if col.name not in {"tenant_id", "scope_type", "scope_id"}]
            for constraint in model.__table__.constraints if isinstance(constraint, UniqueConstraint)]
        kept = []
        for row in rows[model]:
            if model is KnowledgeLink:
                for end in ("from", "to"):
                    if getattr(row, f"{end}_id") in deleted_segments:
                        setattr(row, f"{end}_kind", "document")
            match = next((other for other in kept if any(all(repoint(getattr(row, key)) == repoint(getattr(other, key))
                for key in fields) for fields in keys)), None)
            losing_segment = model is KnowledgeSegment and row.document_id in losing_documents
            replacement_id = losing_documents[row.document_id] if losing_segment else match.id if match else None
            if replacement_id is None:
                kept.append(row)
            else:
                paths = ("queued_markdown_path", "queued_source_path", "processed_canonical_md_path")
                entry = {col.name: str(value) if value is not None and (col.name == "id" or col.name.endswith("_id")) else value
                    for col in model.__table__.columns
                    if (col.name == "id" or col.name.endswith(("_id", "_hash", "_sha256", "_path"))
                        or col.name == "status") for value in [getattr(row, col.name)]}
                entry["created_at"] = row.created_at.isoformat()
                if model is KnowledgeDocument:
                    entry.update(sensitivity=row.sensitivity, agent_searchable=row.agent_searchable,
                        doc_metadata_json={key: value for key, value in row.doc_metadata_json.items() if key in paths})
                    leaver_wins = match.status not in ("ready", "degraded", "archived") and row.status in ("ready", "degraded")
                    losing_documents[match.id if leaver_wins else row.id] = match.id
                    if leaver_wins:
                        for field in ("status", "canonical_md_path", "canonical_md_sha256", "artifact_hash"):
                            setattr(match, field, getattr(row, field))
                    match.sensitivity = max_sensitivity(match.sensitivity, row.sensitivity).value
                    match.agent_searchable = match.agent_searchable and row.agent_searchable
                elif model is KnowledgeSegment:
                    deleted_segments[row.id] = deleted_segments[str(row.id)] = replacement_id
                    segment_counts[row.document_id] = segment_counts.get(row.document_id, 0) + 1
                elif model in (KnowledgeEntity, KnowledgeAssertion, KnowledgeLink):
                    match.source_refs_json = _merge_source_refs(repoint(match.source_refs_json), repoint(row.source_refs_json))
                    match.confidence = max(match.confidence, row.confidence)
                    if model is KnowledgeEntity:
                        match.aliases_json = _merge_unique_strings(match.aliases_json, row.aliases_json)
                        match.description = match.description or row.description
                    elif model is KnowledgeAssertion:
                        match.subject_entity_id = match.subject_entity_id or repoint(row.subject_entity_id)
                        match.object_entity_id = match.object_entity_id or repoint(row.object_entity_id)
                elif model is KnowledgeIndexJob:
                    entry.update(stage=row.stage, attempt_count=row.attempt_count, job_metadata_json={key: value
                            for key, value in row.job_metadata_json.items() if key in (*paths, "queued_at", "retried_at",
                                "failed_at", "finished_at", "cancelled_at", "claimed_at", "claimed_token",
                                "created_by_user_id", "processed_result_job_id", "processed_document_id")})
                if model is not KnowledgeSegment:
                    provenance[model.__tablename__.removeprefix("knowledge_")].append(entry)
                replacements[row.id], replacements[str(row.id)] = replacement_id, str(replacement_id)
                duplicates.setdefault(model, []).append(row.id)
            if model is KnowledgeDocument and row.scope_id == target_user.id:
                documents.append({"id": str(match.id if match else row.id), "title": match.title if match else row.title,
                    **({"merged": True} if match else {})})

    provenance["segments"] = [{"document_id": str(document_id), "dropped_segment_count": count}
        for document_id, count in segment_counts.items()]
    duplicate_ids = [value for ids in duplicates.values() for value in ids]
    # Edges have polymorphic endpoints; remove duplicate edges before repointing them.
    await db.execute(delete(KnowledgeLink).where(KnowledgeLink.id.in_(duplicates.get(KnowledgeLink, []))))
    await db.execute(delete(KnowledgeSegment).where(KnowledgeSegment.id.in_(duplicates.get(KnowledgeSegment, []))))
    for model, ids in duplicates.items():
        for old_id in ids:
            for table in Base.metadata.tables.values():
                references = [fk.parent for fk in table.foreign_keys if fk.column.table is model.__table__]
                if table is KnowledgeLink.__table__:
                    references += [table.c.from_id, table.c.to_id]
                for column in references:
                    values = {column.name: replacements[old_id]}
                    if model is KnowledgeSegment and table is KnowledgeLink.__table__:
                        values[column.name.removesuffix("_id") + "_kind"] = "document"
                    if table.name == "knowledge_grants":
                        values["resource_id"] = replacements[old_id]
                    await db.execute(update(table).where(table.c.tenant_id == target_user.tenant_id,
                        column == old_id, table.c.id.notin_(duplicate_ids)).values(**values))
    for table in Base.metadata.tables.values():
        if "source_refs_json" in table.c and duplicate_ids:
            references = (await db.execute(select(table.c.id, table.c.source_refs_json).where(
                table.c.tenant_id == target_user.tenant_id, table.c.id.notin_(duplicate_ids),
                or_(*(table.c.source_refs_json.cast(String).contains(str(value)) for value in duplicate_ids))))).all()
            for row_id, refs in references:
                await db.execute(update(table).where(table.c.id == row_id).values(source_refs_json=repoint(refs)))
    for model in reversed(models):
        await db.execute(delete(model).where(model.id.in_(duplicates.get(model, []))))
    for model, records in rows.items():
        for row in records:
            if row.id in duplicate_ids:
                continue
            for col in model.__table__.columns:
                setattr(row, col.name, repoint(getattr(row, col.name)))
            row.scope_id = successor.id
            if model is KnowledgeDocument:
                row.owner_user_id = successor.id
    await db.execute(update(PersonalKnowledgeProposal).where(PersonalKnowledgeProposal.tenant_id == target_user.tenant_id,
        PersonalKnowledgeProposal.owner_user_id == target_user.id, PersonalKnowledgeProposal.status == "pending")
        .values(owner_user_id=successor.id))
    return documents, provenance


async def transfer_knowledge_and_notify(db, *, target_user, successor, agents, handover_audits):
    with db.no_autoflush:
        documents, provenance = await _move_knowledge(db, target_user, successor)
    agent_ids = [agent.id for agent in agents]
    tasks = (await db.execute(select(Task, User.id).join(Agent, Agent.id == Task.agent_id)
        .outerjoin(User, (User.id == func.coalesce(Agent.owner_user_id, Agent.creator_id)) & User.is_active
            & (User.tenant_id == target_user.tenant_id)).where(
        Task.tenant_id == target_user.tenant_id, Task.status.notin_(("done", "cancelled")),
        Task.created_by == target_user.id))).all()
    work, originals = [], {}
    for task, bot_owner in tasks:
        owner = successor.id if task.agent_id in agent_ids else bot_owner
        originals[str(task.id)] = str(task.created_by)
        if owner:
            task.created_by = owner
            if task.responsible_user_id in (None, target_user.id):
                task.responsible_user_id = owner
        if owner != successor.id and owner is not None:
            continue
        run = await db.scalar(select(RuntimeTask).where(RuntimeTask.id == task.active_runtime_task_id,
            RuntimeTask.task_type == "business_task")) if task.active_runtime_task_id else None
        steps = (await db.execute(select(SessionToolInvocation.tool_name, ChatTranscriptEvent.lifecycle,
            ChatTranscriptEvent.created_at).join(ChatTranscriptEvent,
                ChatTranscriptEvent.invocation_id == SessionToolInvocation.id).where(
            SessionToolInvocation.tenant_id == target_user.tenant_id, SessionToolInvocation.run_id == run.id,
            ChatTranscriptEvent.item_kind == "tool_call").distinct(SessionToolInvocation.id)
            .order_by(SessionToolInvocation.id, ChatTranscriptEvent.sequence.desc()))).all() if run else []
        effect = (run.metadata_json or {}).get("side_effect_state") if run else "not_started"
        work.append({"id": str(run.id if run else task.id), "agent_id": str(task.agent_id), "task_id": str(task.id),
            "title": task.title, "side_effect_state": effect, "requires_confirmation": effect == "unknown",
            "known_results": [{"tool_name": name, "state": "started" if state == "progress" else state,
                "time": time.isoformat()} for name, state, time in sorted(steps, key=lambda step: step[2])],
            **({"needs_admin": True} if owner is None else {})})
    for audit in handover_audits:
        audit.details["task_original_creators"] = {str(task.id): originals[str(task.id)] for task, _owner in tasks
            if task.agent_id == audit.agent_id}
    trigger_ids = [value for audit in handover_audits for value in audit.details.get("quarantined_trigger_ids", [])]
    triggers = (await db.scalars(select(AgentTrigger).where(AgentTrigger.tenant_id == target_user.tenant_id,
        AgentTrigger.agent_id.in_(agent_ids), AgentTrigger.id.in_(trigger_ids)))).all()
    if agents or documents or work or triggers:
        payload = {"agents": [{"id": str(agent.id), "name": agent.name} for agent in agents], "knowledge": documents,
            "work": work, "triggers": [{"id": str(trigger.id), "agent_id": str(trigger.agent_id), "name": trigger.name}
                for trigger in triggers]}
        db.add(Notification(user_id=successor.id, type="offboarding_handover", title="工作交接",
            body=json.dumps(payload, ensure_ascii=False), ref_id=target_user.id))
    return {"task_original_creators": originals, "merged_knowledge_provenance": provenance}
