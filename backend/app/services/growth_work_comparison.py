"""Read recorded work conditions and outcomes without scoring or rerunning work."""

from fastapi import HTTPException
from sqlalchemy import String, cast, func, or_, select, tuple_

from app.core.permissions import is_scoped_business_admin
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.runtime_budget import RuntimeBudgetRun
from app.models.runtime_task import RuntimeTask
from app.models.session_feedback import SessionFeedbackEvent
from app.models.session_v2 import SessionModelResult, SessionRunOutcome
from app.services.content_input_receipts import ContentInputReceiptError, _authorize_session_input_source
from app.services.learned_skill_sources import model_source, validate_model_source
from app.services.session_model_inputs import _hash, _model_dependencies
from app.services.session_terminal_outcome import _allowed_source_blocks
from app.services.token_tracker import extract_usage_tokens

_LEARNING_KINDS = {"explicit_memory", "team_memory", "learned_skill_file", "agent_resident_memory"}
_DERIVED_KINDS = {"session_tool_invocation", "session_model_result", "session_compaction"}


def _distinct(values):
    return [value for _key, value in sorted({_hash(value): value for value in values}.items())]


def _envelope(results, budget):
    snapshots = [r.model_request_snapshot_json for r in results]
    wires = [s.get("wire_request") for s in snapshots]
    valid_wires = all(isinstance(w, dict) for w in wires)
    models = [{"provider": s.get("provider"), "model": s.get("model")} for s in snapshots]
    reasoning = [w.get("reasoning") for w in wires] if valid_wires else []
    tools = [w.get("tools") for w in wires] if valid_wires else []
    inputs, evidence, learning = {}, [], []
    bound_ids = {item_id for snapshot in snapshots for item_id in snapshot.get("bound_input_ids", [])}
    direct_commands = set()
    derived_evidence = False
    for result in results:
        snapshot = result.model_request_snapshot_json
        catalog = snapshot["input_bindings"]
        for message in catalog.get("messages", []):
            ref = str(message.get("source_ref") or "")
            if ref.startswith("session-input:") and ref.split(":", 1)[1] in bound_ids:
                inputs.setdefault(ref, message.get("message_hash"))
                direct_commands.add(str(message.get("command_ref") or "").removeprefix("session-command:"))
    for result in results:
        dependencies, _complete = _model_dependencies(result)
        for dependency in dependencies:
            if dependency["source_kind"] == "session_command" and dependency["source_id"] in direct_commands:
                continue
            evidence.append(dependency)
            derived_evidence |= dependency["source_kind"] in _DERIVED_KINDS
            if dependency["source_kind"] in _LEARNING_KINDS:
                learning.append(dependency)
    output_limits = [w.get("max_tokens") for w in wires] if valid_wires else []
    runtime_snapshot = budget.policy_snapshot if budget is not None else None
    # A reference is sufficient for authorization, but is not an equality proof
    # for independently executed tool/model graphs. Do not compare their UUIDs.
    return {
        "model": _distinct(models) if all(m["model"] and m["provider"] for m in models) else None,
        "reasoning": _distinct(reasoning) if reasoning and all(isinstance(r, dict) and r for r in reasoning) else None,
        "business_input": list(inputs.values()) if inputs and all(inputs.values()) else None,
        "authorized_evidence": _distinct(evidence) if not derived_evidence else None,
        "tools": _distinct(tools) if tools and all(isinstance(t, list) for t in tools) else None,
        "budget": {"runtime": runtime_snapshot, "output_limits": _distinct(output_limits)}
            if runtime_snapshot and output_limits and all(isinstance(n, int) and n > 0 for n in output_limits) else None,
        "learning_sources": _distinct(learning),
    }


async def list_work_samples(db, *, agent, user, before_run_id=None, limit=20):
    """Page authorized work metadata; model-authored titles never enter the list."""
    recorded_at = func.coalesce(RuntimeTask.completed_at, RuntimeTask.created_at)
    base = select(SessionRunOutcome, ChatSession, RuntimeTask).join(ChatSession,
        ChatSession.id == SessionRunOutcome.session_id).join(RuntimeTask,
        RuntimeTask.id == SessionRunOutcome.run_id).where(
        SessionRunOutcome.tenant_id == agent.tenant_id, ChatSession.tenant_id == agent.tenant_id,
        RuntimeTask.tenant_id == agent.tenant_id, RuntimeTask.parent_agent_id == agent.id,
        ChatSession.agent_id == agent.id, SessionRunOutcome.state == "terminal_committed",
        ChatSession.listed_surface == "chat")
    if not is_scoped_business_admin(user, resource_tenant_id=agent.tenant_id):
        base = base.where(or_(ChatSession.user_id == user.id, ChatSession.audience_kind == "group"))
    if before_run_id is not None:
        cursor = (await db.execute(base.where(SessionRunOutcome.run_id == before_run_id))).first()
        if cursor is None:
            raise ContentInputReceiptError("growth_work_cursor_unavailable", "Select recent work again.", 404)
        await _authorize_session_input_source(db, agent.tenant_id, cursor[1], user=user, runtime_request=None)
        base = base.where(tuple_(recorded_at, SessionRunOutcome.id) < (
            cursor[2].completed_at or cursor[2].created_at, cursor[0].id))
    items, last_key = [], None
    while len(items) < limit:
        statement = base.where(tuple_(recorded_at, SessionRunOutcome.id) < last_key) if last_key else base
        rows = (await db.execute(statement.order_by(recorded_at.desc(), SessionRunOutcome.id.desc()).limit(limit))).all()
        if not rows:
            break
        for outcome, session, task in rows:
            last_key = (task.completed_at or task.created_at, outcome.id)
            try:
                await _authorize_session_input_source(db, agent.tenant_id, session, user=user, runtime_request=None)
            except ContentInputReceiptError as exc:
                if exc.status_code in {403, 404}:
                    continue
                raise
            items.append({"run_id": str(outcome.run_id), "session_id": str(session.id),
                "source_channel": session.source_channel,
                "completed_at": task.completed_at.isoformat() if task.completed_at else None})
            if len(items) == limit:
                break
        if len(rows) < limit:
            break
    return {"items": items, "next_cursor": items[-1]["run_id"] if len(items) == limit else None,
        "order": "recorded_at_desc"}


async def _read_work(db, *, agent, user, run_id):
    row = (await db.execute(select(SessionRunOutcome, ChatSession, RuntimeTask).join(ChatSession,
        ChatSession.id == SessionRunOutcome.session_id).join(RuntimeTask,
        RuntimeTask.id == SessionRunOutcome.run_id).where(SessionRunOutcome.tenant_id == agent.tenant_id,
        SessionRunOutcome.run_id == run_id, ChatSession.tenant_id == agent.tenant_id, ChatSession.agent_id == agent.id,
        RuntimeTask.tenant_id == agent.tenant_id, RuntimeTask.parent_agent_id == agent.id,
        RuntimeTask.parent_session_id == cast(SessionRunOutcome.session_id, String)))).first()
    if row is None:
        raise ContentInputReceiptError("growth_work_unavailable", "Select an available completed work item.", 404)
    outcome, session, task = row
    await _authorize_session_input_source(db, agent.tenant_id, session, user=user, runtime_request=None)
    results = list(await db.scalars(select(SessionModelResult).outerjoin(ChatTranscriptEvent,
        ChatTranscriptEvent.id == SessionModelResult.round_committed_event_id).where(
        SessionModelResult.tenant_id == agent.tenant_id, SessionModelResult.session_id == session.id,
        SessionModelResult.run_id == run_id).order_by(ChatTranscriptEvent.sequence.asc().nulls_last(), SessionModelResult.id)))
    if outcome.state != "terminal_committed" or task.status != "completed" or not results:
        raise ContentInputReceiptError("growth_work_uncommitted", "Reconcile this work before comparing it.", 409)
    for result in results:
        if result.state != "round_committed":
            raise ContentInputReceiptError("growth_work_uncommitted", "Reconcile this work before comparing it.", 409)
        await validate_model_source(db, agent.tenant_id, model_source(result), user=user)
    terminal = next((r for r in results if r.id == outcome.terminal_result_id), None)
    event = await db.get(ChatTranscriptEvent, outcome.terminal_event_id)
    if (terminal is None or event is None or event.tenant_id != agent.tenant_id or event.session_id != session.id
        or event.run_id != run_id or event.result_id != terminal.id or event.item_kind != "assistant_final"
        or event.agent_id != agent.id or event.lifecycle != "completed"
        or event.content_hash != (outcome.seal_json or {}).get("result_content_hash")
        or (outcome.seal_json or {}).get("source_blocks") != _allowed_source_blocks(terminal.seal_json)
        or event.content_hash != _hash([b["content_hash"] for b in _allowed_source_blocks(terminal.seal_json)])):
        raise ContentInputReceiptError("growth_work_unverified", "Reconcile the original result before comparing it.", 409)
    budget = await db.get(RuntimeBudgetRun, task.budget_run_id) if task.budget_run_id is not None else None
    if budget is not None and budget.tenant_id != agent.tenant_id:
        budget = None
    counts = {"useful": 0, "misleading": 0}
    feedback = await db.scalars(select(SessionFeedbackEvent).where(SessionFeedbackEvent.tenant_id == agent.tenant_id,
        SessionFeedbackEvent.agent_id == agent.id, SessionFeedbackEvent.session_id == session.id))
    result_ids = {str(r.id) for r in results}
    events = {str(e.id): e for e in await db.scalars(select(ChatTranscriptEvent).where(
        ChatTranscriptEvent.tenant_id == agent.tenant_id, ChatTranscriptEvent.session_id == session.id,
        ChatTranscriptEvent.run_id == run_id, ChatTranscriptEvent.lifecycle == "completed",
        ChatTranscriptEvent.item_kind.in_(("assistant_text", "assistant_final"))))}
    for rating in feedback:
        target = (rating.attribution_json or {}).get("target") or {}
        rated_event = events.get(target.get("event_id"))
        if (target.get("schema") == "hive.session_feedback_target.v1" and target.get("kind") == "session_item"
            and target.get("run_id") == str(run_id) and target.get("model_result_id") in result_ids
            and rated_event is not None and target.get("item_id") == str(rated_event.item_id)
            and target.get("model_result_id") == str(rated_event.result_id)
            and target.get("content_hash") == rated_event.content_hash
            and rating.label in counts):
            counts[rating.label] += 1
    usage = [extract_usage_tokens((r.seal_json or {}).get("usage")) for r in results]
    duration = (task.completed_at - task.started_at).total_seconds() if task.completed_at and task.started_at else None
    return {"run_id": str(run_id), "session_id": str(session.id), "status": task.status,
        "result": terminal.seal_json["semantic_content"], "result_id": str(terminal.id),
        "work_url": f"/agents/{agent.id}/sessions/{session.id}",
        "duration_seconds": duration if duration is not None and duration >= 0 else None,
        "usage": {"effective_tokens": sum(usage) if all(n is not None for n in usage) else None,
            "recorded_rounds": len(results)},
        "cost": {"amount": None, "currency": None, "status": "not_recorded"},
        "human_corrections": None, "feedback": counts,
        "request_hashes": [r.model_request_hash for r in results], "envelope": _envelope(results, budget)}


async def compare_recorded_work(db, *, agent, user, baseline_run_id, candidate_run_id):
    arms = {}
    for name, run in (("baseline", baseline_run_id), ("candidate", candidate_run_id)):
        try:
            arms[name] = await _read_work(db, agent=agent, user=user, run_id=run)
        except (ContentInputReceiptError, HTTPException) as exc:
            # Denial stays local to this read; other growth metrics remain usable.
            return {"schema": "hive.growth_work_comparison.v1", "status": "unavailable",
                "interpretation": "observations_only", "reason_code": getattr(exc, "code", "growth_work_unavailable"),
                "recovery": "select_or_restore_work", "conditions": []}
    conditions = []
    for key in ("model", "reasoning", "business_input", "authorized_evidence", "tools", "budget"):
        left, right = (arms[name]["envelope"][key] for name in ("baseline", "candidate"))
        def observation(value):
            if key == "model" or value is None:
                return value
            return {"sha256": _hash(value), "entries": len(value)}
        conditions.append({"key": key, "status": "unknown" if left is None or right is None
            else "same" if left == right else "different", "baseline": observation(left), "candidate": observation(right)})
    statuses = {c["status"] for c in conditions}
    for arm in arms.values():
        arm["learning_sources"] = arm.pop("envelope")["learning_sources"]
    return {"schema": "hive.growth_work_comparison.v1", **arms, "conditions": conditions,
        "status": "not_comparable" if "different" in statuses else "insufficient_evidence" if "unknown" in statuses
            else "recorded_conditions_match", "interpretation": "observations_only"}
