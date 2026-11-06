"""Move uploaded knowledge and deliver one notice in the offboarding transaction."""

import json

from sqlalchemy import String, cast, func, select, update

from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.knowledge import (
    KnowledgeAssertion, KnowledgeDocument, KnowledgeEntity, KnowledgeIndexJob, KnowledgeLink, KnowledgeSegment,
)
from app.models.notification import Notification
from app.models.runtime_task import RuntimeTask
from app.models.task import Task
from app.models.trigger import AgentTrigger


async def transfer_knowledge_and_notify(db, *, target_user, successor, agents, revocations, handover_audits):
    documents = list((await db.execute(select(KnowledgeDocument).where(
        KnowledgeDocument.tenant_id == target_user.tenant_id, KnowledgeDocument.scope_type == "person",
        KnowledgeDocument.scope_id == target_user.id,
    ))).scalars().all())
    for model in (KnowledgeDocument, KnowledgeSegment, KnowledgeEntity, KnowledgeAssertion, KnowledgeLink, KnowledgeIndexJob):
        values = {"scope_id": successor.id}
        if model is KnowledgeDocument:
            values["owner_user_id"] = successor.id
        await db.execute(update(model).where(model.tenant_id == target_user.tenant_id,
            model.scope_type == "person", model.scope_id == target_user.id).values(**values))

    agent_ids = [agent.id for agent in agents]
    private_chat = select(ChatSession.id).where(
        ChatSession.tenant_id == target_user.tenant_id, ChatSession.session_kind == "human_chat",
        cast(ChatSession.id, String) == func.coalesce(RuntimeTask.child_session_id, RuntimeTask.parent_session_id),
    ).exists()
    runs = list((await db.execute(select(RuntimeTask).where(
        RuntimeTask.id.in_([signal.task_id for signal in revocations.runtime_task_signals]),
        RuntimeTask.tenant_id == target_user.tenant_id, RuntimeTask.parent_agent_id.in_(agent_ids),
        ~private_chat,
    ))).scalars().all())
    tasks = list((await db.execute(select(Task).where(
        Task.tenant_id == target_user.tenant_id, Task.agent_id.in_(agent_ids),
        func.coalesce(Task.responsible_user_id, Task.created_by) == target_user.id,
        Task.status.notin_(("done", "cancelled")),
    ))).scalars().all())
    for task in tasks:
        task.responsible_user_id = successor.id
    tasks_by_run = {task.active_runtime_task_id: task for task in tasks}
    work = []
    for run in runs:
        task = tasks_by_run.get(run.id)
        interruption = (run.metadata_json or {}).get("outcome", {}).get("summary")
        known = list(dict.fromkeys(value for value in (
            getattr(task, "last_result", None), run.result_summary if run.result_summary != interruption else None,
        ) if value))
        events = (await db.execute(select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.tenant_id == target_user.tenant_id, ChatTranscriptEvent.agent_id == run.parent_agent_id,
            ChatTranscriptEvent.run_id == run.id,
            ChatTranscriptEvent.event_type.in_(("tool_result", "tool_result.completed")),
        ).order_by(ChatTranscriptEvent.sequence))).scalars().all()
        for event in events:
            if event.schema_version == 2:
                result = event.content
            else:
                try:
                    result = json.loads(event.content or "{}").get("result")
                except (ValueError, AttributeError):
                    result = event.content
            if result and result not in known:
                known.append(result)
        work.append({"id": str(run.id), "agent_id": str(run.parent_agent_id),
            "task_id": str(task.id) if task else None, "title": task.title if task else run.task_type,
            "known_results": known, "side_effect_state": (run.metadata_json or {}).get("side_effect_state"),
            "requires_confirmation": (run.metadata_json or {}).get("side_effect_state") == "unknown"})
    work.extend({"id": str(task.id), "agent_id": str(task.agent_id), "task_id": str(task.id),
        "title": task.title, "known_results": [task.last_result] if task.last_result else [],
        "requires_confirmation": False} for task in tasks if task.active_runtime_task_id not in {run.id for run in runs})
    trigger_ids = [value for audit in handover_audits for value in audit.details.get("quarantined_trigger_ids", [])]
    triggers = (await db.execute(select(AgentTrigger).where(
        AgentTrigger.tenant_id == target_user.tenant_id, AgentTrigger.agent_id.in_(agent_ids),
        AgentTrigger.id.in_(trigger_ids),
    ))).scalars().all()
    payload = {"agents": [{"id": str(agent.id), "name": agent.name} for agent in agents],
        "knowledge": [{"id": str(doc.id), "title": doc.title} for doc in documents], "work": work,
        "triggers": [{"id": str(trigger.id), "agent_id": str(trigger.agent_id), "name": trigger.name}
            for trigger in triggers]}
    db.add(Notification(user_id=successor.id, type="offboarding_handover", title="工作交接",
        body=json.dumps(payload, ensure_ascii=False), ref_id=target_user.id))
