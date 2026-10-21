"""Recover scheduled input provenance without granting authority from metadata."""

import uuid

from fastapi import HTTPException
from sqlalchemy import select

from app.models.runtime_budget import RuntimeBudgetRun
from app.models.runtime_task import RuntimeTask
from app.models.session_v2 import SessionCommand, SessionTurnInput
from app.models.trigger import AgentTrigger
from app.models.user import User


def _unavailable():
    return HTTPException(409, detail={"code": "trigger_input_origin_unavailable"})


async def authorize_trigger_origin(db, *, wrapper, trigger_ids):
    """Recheck the original requester and fire generation at an execution boundary."""
    from app.services.trigger_resource_authority import (
        authorize_trigger_action, trigger_execution_block_reason, trigger_owner_user_id, trigger_fire_authority_block_reason,
    )

    user = await db.get(User, wrapper.root_user_id, populate_existing=True)
    rows = list(await db.scalars(select(AgentTrigger).where(AgentTrigger.tenant_id == wrapper.tenant_id,
        AgentTrigger.agent_id == wrapper.parent_agent_id, AgentTrigger.id.in_(trigger_ids))))
    if user is None or not user.is_active or len(rows) != len(trigger_ids):
        raise _unavailable()
    for trigger in rows:
        generation_block = trigger_fire_authority_block_reason(trigger, wrapper.metadata_json)
        if generation_block:
            raise HTTPException(403 if generation_block == "trigger_authority_revoked" else 409,
                detail={"code": generation_block})
        if trigger_owner_user_id(trigger) != user.id or trigger_execution_block_reason(trigger):
            raise _unavailable()
        await authorize_trigger_action(db, user, agent_id=wrapper.parent_agent_id, trigger=trigger, action="execute")
        config = trigger.config or {}
        if config.get("plan_id") and isinstance(config.get("plan_authorization"), dict):
            from app.services.plan_authorization_lease import (
                PlanAuthorizationLeaseError, verify_consumed_plan_authorization_lease,
            )

            try:
                await verify_consumed_plan_authorization_lease(db=db, tenant_id=wrapper.tenant_id,
                    agent_id=wrapper.parent_agent_id, plan_id=config["plan_id"], evidence=config["plan_authorization"])
            except PlanAuthorizationLeaseError as exc:
                raise HTTPException(403, detail={"code": "trigger_plan_authority_revoked"}) from exc
    return rows


async def load_trigger_input_origin(db, *, tenant_id, agent_id, session, metadata, for_effect=True):
    source = metadata.get("source")
    if source not in {"loop_same_session", "trigger_reflection"}:
        return None
    if session.tenant_id != tenant_id or session.agent_id != agent_id:
        raise _unavailable()
    from app.services.session_model_inputs import _hash
    from app.services.trigger_daemon import _trigger_child_run_id

    try:
        wrapper_id = uuid.UUID(str(metadata["trigger_runtime_task_id"]))
        input_id = uuid.UUID(str(metadata["session_v2_input_id"]))
        command_id = uuid.UUID(str(metadata["session_v2_command_id"]))
        wrapper = await db.get(RuntimeTask, wrapper_id, populate_existing=True)
        row = await db.get(SessionTurnInput, input_id, populate_existing=True)
        command = await db.get(SessionCommand, command_id, populate_existing=True)
        if (wrapper is None or wrapper.tenant_id != tenant_id or wrapper.parent_agent_id != agent_id
            or wrapper.task_type != "trigger" or wrapper.status != "completed"
            or wrapper.root_user_id is None or wrapper.root_user_id != session.user_id
            or wrapper.metadata_json.get("delivery") != ("reflection" if source == "trigger_reflection" else "same_session")
            or (source == "trigger_reflection" and (session.session_kind != "trigger_run"
                or session.runtime_source != "trigger" or session.runtime_task_id != wrapper.id))
            or wrapper.metadata_json.get("source_session_id") != str(session.id)
            or input_id != _trigger_child_run_id(wrapper.id, effect="same_session", discriminator=str(session.id))
            or row is None or row.tenant_id != tenant_id or row.session_id != session.id
            or row.command_id != command_id or row.intent != "queue_next_turn"
            or command is None or command.tenant_id != tenant_id
            or command.session_id != session.id or command.principal_type != "user"
            or command.principal_id != wrapper.root_user_id or command.namespace != "human_input"
            or command.command_kind != "queue_next_turn"
            or command.request_hash != _hash({"command_kind": command.command_kind, "request": command.request_json})
            or command.target_hash != _hash(command.target_json)):
            raise ValueError("scheduled_input_binding_mismatch")
        original = command.target_json["runtime_metadata"]
        trigger_ids = [uuid.UUID(str(value)) for value in original["trigger_ids"]]
        if (original.get("source") != source
            or uuid.UUID(str(original.get("trigger_runtime_task_id"))) != wrapper.id
            or not trigger_ids or len(set(trigger_ids)) != len(trigger_ids)
            or not set(map(str, trigger_ids)).issubset(set(wrapper.metadata_json.get("trigger_ids") or []))
            or any((wrapper.metadata_json.get("trigger_settlement_overrides") or {}).get(str(key)) != "success" for key in trigger_ids)):
            raise ValueError("scheduled_input_origin_mismatch")
        budget = await db.get(RuntimeBudgetRun, wrapper.budget_run_id, populate_existing=True) if wrapper.budget_run_id else None
        root_id = wrapper.root_runtime_task_id or wrapper.id
        if (budget is None or budget.tenant_id != tenant_id or budget.root_runtime_task_id != root_id
            or budget.root_agent_id != agent_id or budget.root_user_id not in {None, wrapper.root_user_id}):
            raise ValueError("scheduled_input_budget_mismatch")
        if for_effect:
            await authorize_trigger_origin(db, wrapper=wrapper, trigger_ids=trigger_ids)
        return {"schema": "hive.trigger_input_origin.v1", "wrapper_id": str(wrapper.id),
            **({"authority_generations": wrapper.metadata_json["trigger_authority_generations"]}
                if "trigger_authority_generations" in wrapper.metadata_json else {}),
            **({"source": source} if source == "trigger_reflection" else {}), "input_id": str(row.id), "input_revision": row.revision, "input_sha256": _hash(row.content_parts_json),
            "command_id": str(command.id), "request_hash": command.request_hash, "target_hash": command.target_hash,
            "trigger_ids": list(map(str, trigger_ids)), "requester_user_id": str(wrapper.root_user_id),
            "session_id": str(session.id), "budget_run_id": str(budget.id), "root_runtime_task_id": str(root_id)}
    except (ValueError, KeyError, TypeError, AttributeError) as exc:
        raise _unavailable() from exc
