"""Continuation controls for long-lived Agent-to-Agent sessions.

The transcript event is durable evidence. Active runs consume a canonical
Session V2 input at the next provider boundary; inactive open sessions start a
new durable turn; terminal sessions reject the continuation and record it.
"""

from __future__ import annotations

from html import escape
import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.user import User
from app.services.chat_message_parts import build_session_native_event
from app.services.chat_transcript import append_session_event
from app.services.runtime_root_ledger import RuntimeRootIntentSpec
from app.services.web_chat_runtime import (
    A2A_CONTINUATION_TASK_TYPE,
    WEB_CHAT_TURN_TASK_TYPE,
    _find_active_run,
    _submit_active_session_input,
    start_web_chat_run,
)

_TERMINAL_SESSION_STATES = {"completed", "failed", "killed", "closed", "skipped", "cancelled"}
_TASK_RUNTIME_ACTION_STATUS_MAP = {
    "completed": "runtime_action_completed",
    "done": "runtime_action_completed",
    "failed": "runtime_action_failed",
    "error": "runtime_action_failed",
    "killed": "runtime_action_failed",
    "cancelled": "runtime_action_failed",
    "blocked": "runtime_action_blocked",
    "needs_reconciliation": "runtime_action_blocked",
    "suspended": "runtime_action_blocked",
}


def _text(value: Any) -> str:
    return str(value or "").strip()


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if value is None:
        return None
    if isinstance(value, uuid.UUID):
        return value
    text = _text(value)
    if not text:
        return None
    try:
        return uuid.UUID(text)
    except ValueError:
        return None


def _session_metadata(session: ChatSession) -> dict[str, Any]:
    return dict(getattr(session, "transcript_metadata_json", None) or {})


def _session_state(session: ChatSession) -> str:
    metadata = _session_metadata(session)
    return _text(metadata.get("session_state") or metadata.get("state") or "open").lower() or "open"


# Structured parent-route keys reserved by admission: they are stamped from
# durable session fields and caller-supplied extra_metadata can never reroute
# them (Codex REQUEST_CHANGES on DAY1-A2A-CONT-RETURN-001: metadata must not
# select completion-return routing).
_RESERVED_PARENT_ROUTE_KEYS = frozenset({"parent_session_id", "parent_agent_id"})


def is_a2a_delegation_child_session(session: ChatSession) -> bool:
    """Structured A2A delegation child-session binding.

    Only durable session fields decide this — never natural-language content.
    The orchestrator stamps ``session_kind="delegation_run"`` and
    ``runtime_source="delegation"`` when it creates the peer session.
    """

    return (
        _text(getattr(session, "session_kind", None)) == "delegation_run"
        or _text(getattr(session, "runtime_source", None)) == "delegation"
    )


def _runtime_task_type_for_session(session: ChatSession, explicit: str | None = None) -> str:
    if _text(explicit):
        return _text(explicit)
    if _text(getattr(session, "session_kind", None)) == "team_member":
        return "team_member"
    if _text(getattr(session, "runtime_source", None)) == "team_member":
        return "team_member"
    if is_a2a_delegation_child_session(session):
        # A follow-up successor on an A2A delegation child session must remain
        # completion-outbox eligible so its terminal completion durably wakes
        # the parent.  Degrading it to web_chat_turn silently dropped the
        # completion return (DAY1-A2A-CONT-RETURN-001).
        return A2A_CONTINUATION_TASK_TYPE
    return WEB_CHAT_TURN_TASK_TYPE


def _is_subagent_session(session: ChatSession) -> bool:
    return (
        _text(getattr(session, "session_kind", None)) == "subagent"
        or _text(getattr(session, "runtime_source", None)) == "subagent"
        or _text(getattr(session, "source_channel", None)) == "subagent"
    )


def _mark_terminal_subagent_resumed(session: ChatSession, terminal_state: str) -> None:
    metadata = _session_metadata(session)
    metadata["session_state"] = "open"
    metadata["terminal_session_resume"] = True
    metadata["resumed_from_terminal_state"] = terminal_state
    session.transcript_metadata_json = metadata


def _task_notification_runtime_action_kind(task_type: str, source: str) -> str:
    text = f"{task_type} {source}".lower()
    if "workflow" in text:
        return "workflow"
    if "agent_team" in text or "team_member" in text:
        return "agent_team"
    if "subagent" in text:
        return "subagent"
    if "a2a" in text or "delegation" in text:
        return "a2a_delegation"
    return _text(task_type) or "background_task"


def _task_notification_has_dedicated_projection(task_type: str, source: str) -> bool:
    text = f"{task_type} {source}".lower()
    # A2A delegation completion is projected by the A2A orchestrator before it
    # enters this mailbox path. Skipping it here prevents duplicate completion
    # chips while preserving workflow/subagent/background wake feedback.
    return "a2a" in text or "a2a_delegation" in text


def _build_task_notification_runtime_action_payload(
    *,
    task_id: str,
    task_type: str,
    status: str,
    summary: str,
    child_session_id: str | uuid.UUID | None,
    child_agent_name: str | None,
    source: str,
    metadata: dict[str, Any] | None,
) -> dict[str, Any] | None:
    if _task_notification_has_dedicated_projection(task_type, source):
        return None
    clean_task_id = _text(task_id)
    action_kind = _task_notification_runtime_action_kind(task_type, source)
    event_type = _TASK_RUNTIME_ACTION_STATUS_MAP.get(_text(status).lower(), "runtime_action_progress")
    payload: dict[str, Any] = {
        "type": event_type,
        "status": _text(status) or "info",
        "message": _text(summary)
        or _task_notification_display_content(
            child_agent_name=child_agent_name,
            status=status,
            summary=summary,
        ),
        "action_kind": action_kind,
        "notification_source": _text(source) or "task_notification",
        "runtime_task_id": clean_task_id or None,
        "task_id": clean_task_id or None,
        "task_type": _text(task_type) or None,
        "child_session_id": _text(child_session_id) or None,
        "target_agent_name": _text(child_agent_name) or None,
    }
    if action_kind == "workflow":
        payload["workflow_run_id"] = _text((metadata or {}).get("workflow_run_id")) or clean_task_id or None
    return payload


async def _append_mailbox_event(
    *,
    db: AsyncSession,
    agent: Agent,
    user: User,
    session: ChatSession,
    event_type: str,
    message: str,
    parent_session_id: str | uuid.UUID | None,
    metadata: dict[str, Any],
    role: str | None = "user",
    run_id: Any = None,
    parts: list[dict[str, Any]] | None = None,
    materialize_chat_message: bool = True,
    source: str = "agent_session_mailbox",
) -> Any:
    return await append_session_event(
        db=db,
        agent_id=agent.id,
        tenant_id=getattr(session, "tenant_id", None) or getattr(agent, "tenant_id", None),
        session_id=session.id,
        actor_type="agent",
        event_type=event_type,
        content=message,
        role=role,
        user_id=getattr(session, "user_id", None) or getattr(user, "id", None),
        run_id=run_id,
        runtime_task_id=run_id,
        root_session_id=getattr(session, "root_session_id", None) or _uuid_or_none(parent_session_id),
        parent_session_id=getattr(session, "parent_session_id", None) or _uuid_or_none(parent_session_id),
        parts=parts,
        metadata=metadata,
        visibility_scope=getattr(session, "visibility_scope", None) or "team",
        listed_surface=getattr(session, "listed_surface", None) or "parent",
        materialize_chat_message=materialize_chat_message,
        source=source,
    )


def _task_notification_xml_field(tag: str, value: Any) -> str:
    text = _text(value)
    if not text:
        return ""
    return f"<{tag}>{escape(text, quote=False)}</{tag}>"


def _task_notification_artifacts_xml(artifacts: list[dict[str, Any]] | None) -> str:
    if not artifacts:
        return ""
    rows: list[str] = []
    for artifact in artifacts:
        path = _text(artifact.get("path"))
        if not path:
            continue
        fields = [
            _task_notification_xml_field("artifact-id", artifact.get("artifact_id") or artifact.get("id")),
            _task_notification_xml_field("artifact-path", path),
            _task_notification_xml_field("artifact-name", artifact.get("name")),
            _task_notification_xml_field("source-agent-id", artifact.get("source_agent_id")),
            _task_notification_xml_field("owner-agent-id", artifact.get("owner_agent_id")),
            _task_notification_xml_field("download-agent-id", artifact.get("download_agent_id")),
        ]
        rows.append("<artifact>\n" + "\n".join(field for field in fields if field) + "\n</artifact>")
    if not rows:
        return ""
    return "<artifacts>\n" + "\n".join(rows) + "\n</artifacts>"


def build_task_notification_message(
    *,
    task_id: str,
    task_type: str,
    status: str,
    summary: str,
    child_session_id: str | uuid.UUID | None = None,
    child_agent_name: str | None = None,
    source: str = "task_notification",
    artifacts: list[dict[str, Any]] | None = None,
) -> str:
    """Build the CC-compatible task notification envelope consumed by parent turns."""

    fields = [
        _task_notification_xml_field("task-id", task_id),
        _task_notification_xml_field("task-type", task_type),
        _task_notification_xml_field("source", source),
        _task_notification_xml_field("child-agent", child_agent_name),
        _task_notification_xml_field("child-session-id", child_session_id),
        _task_notification_xml_field("status", status),
        _task_notification_xml_field("summary", summary),
        _task_notification_xml_field("result", summary),
        _task_notification_artifacts_xml(artifacts),
    ]
    body = "\n".join(field for field in fields if field)
    return f"<task-notification>\n{body}\n</task-notification>"


def _task_notification_display_content(*, child_agent_name: str | None, status: str, summary: str) -> str:
    actor = _text(child_agent_name) or "Background task"
    status_text = _text(status) or "completed"
    clean_summary = _text(summary)
    if len(clean_summary) > 240:
        clean_summary = clean_summary[:237].rstrip() + "..."
    return f"{actor} {status_text}: {clean_summary}" if clean_summary else f"{actor} {status_text}"


def build_task_notification_runtime_context(
    *,
    task_id: str,
    task_type: str,
    status: str,
    summary: str,
    child_session_id: str | uuid.UUID | None = None,
    child_agent_name: str | None = None,
    source: str = "task_notification",
    artifacts: list[dict[str, Any]] | None = None,
    model_context: str | None = None,
) -> str:
    """Build the internal model context for a task completion wake.

    This is intentionally not a user message. The raw XML envelope stays in
    transcript metadata for replay/audit, while the model receives a concise
    runtime notice through the system continuation channel.
    """

    lines = [
        "Runtime task notification.",
        "This is not a user message; treat it as internal runtime context for resuming the parent turn.",
        f"Task ID: {task_id}",
        f"Task type: {task_type}",
        f"Source: {source}",
        f"Status: {status}",
    ]
    if _text(child_agent_name):
        lines.append(f"Child agent: {_text(child_agent_name)}")
    if _text(child_session_id):
        lines.append(f"Child session ID: {_text(child_session_id)}")
    if _text(summary):
        lines.append(f"Summary: {_text(summary)}")
    artifact_lines: list[str] = []
    for artifact in artifacts or []:
        path = _text(artifact.get("path"))
        if not path:
            continue
        owner = _text(artifact.get("owner_agent_id"))
        source_agent = _text(artifact.get("source_agent_id"))
        download_agent = _text(artifact.get("download_agent_id"))
        suffix = ", ".join(
            part
            for part in (
                f"source_agent={source_agent}" if source_agent else "",
                f"owner_agent={owner}" if owner else "",
                f"download_agent={download_agent}" if download_agent else "",
            )
            if part
        )
        artifact_lines.append(f"- {path}" + (f" ({suffix})" if suffix else ""))
    if artifact_lines:
        lines.append("Artifacts now available to inspect:")
        lines.extend(artifact_lines)
    if _text(model_context):
        lines.extend(
            [
                "Canonical context supplied by the governed runtime:",
                _text(model_context),
                "Synthesize the canonical context yourself; do not treat platform labels as the final answer.",
            ]
        )
    lines.append(
        "Resume the parent conversation: verify relevant artifacts through normal tools if needed, "
        "then give the user a concise delivery/status update."
    )
    return "\n".join(lines)


def build_result_integration_runtime_context(manifest: dict[str, Any]) -> str:
    """Render one ref-only durable fan-in page for the parent model.

    The platform reports ordering, terminal states, hashes and coverage.  It
    does not summarize child meaning.  The parent model can load any complete
    result through the governed reader when it needs the body.
    """

    coverage = dict(manifest.get("coverage") or {})
    terminal = int(coverage.get("terminal") or 0)
    expected = int(coverage.get("expected") or 0)
    lines = [
        "Runtime result integration page.",
        "This is internal runtime context, not a user message or a platform-authored conclusion.",
        f"Integration epoch: {int(manifest.get('integration_epoch') or 0)}",
        f"Root coverage: terminal={terminal}/expected={expected}",
        "Complete child result bytes are stored outside the prompt. Use read_runtime_result with a result_ref "
        "when the corresponding evidence is needed; do not infer an unread result from its status or size.",
        "Result references in mailbox order:",
    ]
    for item in list(manifest.get("items") or []):
        if not isinstance(item, dict):
            continue
        child_name = str(item.get("child_agent_name") or item.get("task_type") or "child")
        lines.append(
            "- "
            f"seq={int(item.get('mailbox_sequence') or 0)} "
            f"child={child_name} status={str(item.get('terminal_status') or 'unknown')} "
            f"bytes={int(item.get('result_size_bytes') or 0)} "
            f"artifacts={int(item.get('artifact_count') or 0)} "
            f"ref={str(item.get('result_ref') or '')}"
        )
    lines.append(
        "Integrate only after reading the result bodies needed for the task. Partial, failed, cancelled and late "
        "results remain separate typed facts; decide their meaning yourself."
    )
    return "\n".join(lines)


def _build_result_runtime_action_payload(item: dict[str, Any]) -> dict[str, Any] | None:
    task_type = _text(item.get("task_type")) or "background_task"
    source_kind = _text(item.get("source_kind")) or "runtime_result"
    if _task_notification_has_dedicated_projection(task_type, source_kind):
        return None
    status = _text(item.get("terminal_status")).lower() or "completed"
    source_run_id = _text(item.get("source_run_id"))
    child_name = _text(item.get("child_agent_name")) or task_type
    size_bytes = max(0, int(item.get("result_size_bytes") or 0))
    payload: dict[str, Any] = {
        "type": _TASK_RUNTIME_ACTION_STATUS_MAP.get(status, "runtime_action_progress"),
        "status": status,
        "message": f"{child_name} {status}: durable result ready ({size_bytes} bytes).",
        "action_kind": _task_notification_runtime_action_kind(task_type, source_kind),
        "notification_source": source_kind,
        "runtime_task_id": source_run_id or None,
        "task_id": source_run_id or None,
        "task_type": task_type,
        "child_session_id": _text(item.get("child_session_id")) or None,
        "target_agent_name": _text(item.get("child_agent_name")) or None,
        "result_ref": _text(item.get("result_ref")),
        "result_sha256": _text(item.get("result_sha256")),
        "result_size_bytes": size_bytes,
        "artifact_count": max(0, int(item.get("artifact_count") or 0)),
        "mailbox_sequence": int(item.get("mailbox_sequence") or 0),
    }
    if payload["action_kind"] == "workflow":
        payload["workflow_run_id"] = source_run_id or None
    return payload


def build_result_integration_message(manifest: dict[str, Any]) -> str:
    """Build a replayable ref-only envelope without copying result bodies."""

    item_rows: list[str] = []
    for item in list(manifest.get("items") or []):
        if not isinstance(item, dict):
            continue
        item_rows.append(
            "<result-ref>"
            + _task_notification_xml_field("mailbox-sequence", item.get("mailbox_sequence"))
            + _task_notification_xml_field("task-type", item.get("task_type"))
            + _task_notification_xml_field("status", item.get("terminal_status"))
            + _task_notification_xml_field("sha256", item.get("result_sha256"))
            + _task_notification_xml_field("bytes", item.get("result_size_bytes"))
            + _task_notification_xml_field("ref", item.get("result_ref"))
            + "</result-ref>"
        )
    return (
        "<runtime-result-integration>\n"
        + _task_notification_xml_field("integration-epoch", manifest.get("integration_epoch"))
        + _task_notification_xml_field("root-runtime-task-id", manifest.get("root_runtime_task_id"))
        + "\n"
        + "\n".join(item_rows)
        + "\n</runtime-result-integration>"
    )


async def continue_parent_session_with_result_page(
    *,
    db: AsyncSession,
    agent: Agent,
    user: User,
    session: ChatSession,
    integration_page_id: uuid.UUID,
    manifest: dict[str, Any],
    inherited_budget_run_id: uuid.UUID | None = None,
    resume_parent: bool = True,
) -> dict[str, Any]:
    """Project one durable result page and wake the parent at most once."""

    items = [item for item in list(manifest.get("items") or []) if isinstance(item, dict)]
    coverage = dict(manifest.get("coverage") or {})
    display_content = (
        f"{len(items)} runtime result(s) ready in integration epoch "
        f"{int(manifest.get('integration_epoch') or 0)} "
        f"(terminal={int(coverage.get('terminal') or 0)}/expected={int(coverage.get('expected') or 0)})."
    )
    envelope = build_result_integration_message(manifest)
    runtime_context = build_result_integration_runtime_context(manifest)
    metadata = {
        "runtime_result_integration_page": True,
        "integration_page_id": str(integration_page_id),
        "integration_epoch": int(manifest.get("integration_epoch") or 0),
        "manifest_sha256": str(manifest.get("manifest_sha256") or ""),
        "root_runtime_task_id": manifest.get("root_runtime_task_id"),
        "coverage": coverage,
        "result_manifest": manifest,
        "result_refs": [str(item.get("result_ref") or "") for item in items],
        "mailbox_sequence_start": manifest.get("mailbox_sequence_start"),
        "mailbox_sequence_end": manifest.get("mailbox_sequence_end"),
        "item_count": len(items),
        "task_notification": True,
        "task_type": "runtime_result_integration",
        "status": "ready",
        "message": display_content,
        "display_content": display_content,
        "causation_id": str(integration_page_id),
        **({"budget_run_id": str(inherited_budget_run_id)} if inherited_budget_run_id is not None else {}),
    }
    for item in items:
        runtime_action_payload = _build_result_runtime_action_payload(item)
        if runtime_action_payload is None:
            continue
        runtime_event = build_session_native_event(runtime_action_payload)
        await _append_mailbox_event(
            db=db,
            agent=agent,
            user=user,
            session=session,
            event_type=str(runtime_action_payload["type"]),
            message=str(runtime_action_payload["message"]),
            parent_session_id=getattr(session, "parent_session_id", None) or getattr(session, "id", None),
            metadata={
                **runtime_action_payload,
                "runtime_action_from_result_integration": True,
                "integration_page_id": str(integration_page_id),
                "integration_epoch": int(manifest.get("integration_epoch") or 0),
                "causation_id": _text(item.get("outbox_id")) or str(integration_page_id),
            },
            role="system",
            parts=[runtime_event["part"]] if isinstance(runtime_event.get("part"), dict) else None,
            materialize_chat_message=False,
            source="runtime_result_integration",
        )
    if not resume_parent:
        await _append_mailbox_event(
            db=db,
            agent=agent,
            user=user,
            session=session,
            event_type="agent_task_notification",
            message=display_content,
            parent_session_id=getattr(session, "parent_session_id", None) or getattr(session, "id", None),
            metadata={**metadata, "consumer": "session_projection"},
            role="system",
            materialize_chat_message=False,
            source="runtime_result_integration",
        )
        await db.commit()
        return {
            "ok": True,
            "status": "projected",
            "consumer": "session_projection",
            "integration_page_id": str(integration_page_id),
        }
    return await continue_agent_session_from_mailbox(
        db=db,
        agent=agent,
        user=user,
        session=session,
        message=envelope,
        parent_session_id=getattr(session, "parent_session_id", None) or getattr(session, "id", None),
        display_content=display_content,
        event_type="agent_task_notification",
        mailbox_kind="runtime_result_integration",
        materialize_chat_message=False,
        source_channel="runtime_result_integration",
        extra_metadata=metadata,
        event_content=display_content,
        llm_content=runtime_context,
        mailbox_role="system",
        model_message_role="system",
        budget_admission_status_override=("approved" if inherited_budget_run_id is not None else None),
    )


async def continue_agent_session_from_mailbox(
    *,
    db: AsyncSession,
    agent: Agent,
    user: User,
    session: ChatSession,
    message: str,
    parent_session_id: str | uuid.UUID | None = None,
    interrupt_requested: bool = False,
    runtime_task_type: str | None = None,
    display_content: str = "",
    event_type: str = "agent_session_message",
    mailbox_kind: str = "followup",
    materialize_chat_message: bool = True,
    source_channel: str = "agent_session_mailbox",
    extra_metadata: dict[str, Any] | None = None,
    parts: list[dict[str, Any]] | None = None,
    event_content: str | None = None,
    llm_content: str | None = None,
    mailbox_role: str | None = "user",
    model_message_role: str = "user",
    run_id: uuid.UUID | None = None,
    root_item_intent: RuntimeRootIntentSpec | None = None,
    budget_admission_status_override: str | None = None,
) -> dict[str, Any]:
    """Append and consume a follow-up message for an Agent-Agent session."""

    clean_message = _text(message)
    if not clean_message:
        return {"ok": False, "status": "rejected", "reason": "empty_message"}
    clean_event_content = _text(event_content) or clean_message
    clean_llm_content = _text(llm_content) or clean_message
    model_role = _text(model_message_role).lower() or "user"
    if model_role not in {"user", "system"}:
        model_role = "user"

    state = _session_state(session)
    metadata_base = {
        **_session_metadata(session),
        **(extra_metadata or {}),
        "mailbox_kind": mailbox_kind,
        "mailbox_role": mailbox_role,
        "runtime_mailbox_role": model_role,
        "parent_session_id": _text(parent_session_id or getattr(session, "parent_session_id", None)) or None,
        "interrupt_requested": bool(interrupt_requested),
        "session_state": state,
    }
    if state in _TERMINAL_SESSION_STATES:
        if _is_subagent_session(session):
            metadata_base = {
                **metadata_base,
                "terminal_session_resume": True,
                "resumed_from_terminal_state": state,
            }
            _mark_terminal_subagent_resumed(session, state)
        else:
            await _append_mailbox_event(
                db=db,
                agent=agent,
                user=user,
                session=session,
                event_type="agent_session_message_rejected",
                message=clean_event_content,
                parent_session_id=parent_session_id,
                role="system",
                metadata={
                    **metadata_base,
                    "reason": "terminal_agent_session",
                    "resumable": False,
                    "redirect": "spawn_new_session",
                },
                materialize_chat_message=False,
                source=source_channel,
            )
            await db.commit()
            return {
                "ok": False,
                "status": "rejected",
                "reason": "terminal_agent_session",
                "child_session_id": str(session.id),
                "session_state": state,
                "resumable": False,
                "redirect": "spawn_new_session",
            }

    active_run = await _find_active_run(db=db, agent_id=agent.id, session_id=session.id)
    mailbox_event = await _append_mailbox_event(
        db=db,
        agent=agent,
        user=user,
        session=session,
        event_type=event_type,
        message=clean_event_content,
        parent_session_id=parent_session_id,
        run_id=getattr(active_run, "id", None) if active_run is not None else None,
        metadata={
            **metadata_base,
            "consumer": "session_v2_round_input" if active_run is not None else "continuation_turn",
        },
        role=mailbox_role,
        parts=parts,
        materialize_chat_message=materialize_chat_message,
        source=source_channel,
    )

    if active_run is not None:
        mailbox_event_id = getattr(mailbox_event, "event_id", None) or getattr(mailbox_event, "id", None)
        if mailbox_event_id is None:
            raise RuntimeError("agent session event is missing a stable identity")
        payload = await _submit_active_session_input(
            db=db,
            active_run=active_run,
            agent=agent,
            user=user,
            session=session,
            content=clean_llm_content,
            display_content=display_content,
            source_channel=source_channel,
            message_already_in_t0=True,
            role=model_role,
            idempotency_key=f"agent-session-event:{mailbox_event_id}",
        )
        return {
            **payload,
            "ok": True,
            "status": "queued",
            "consumer": "session_v2_round_input",
            "child_session_id": str(session.id),
        }

    run = await start_web_chat_run(
        db=db,
        agent=agent,
        user=user,
        session=session,
        content=clean_llm_content,
        display_content=display_content,
        append_user_message=False,
        runtime_task_type=_runtime_task_type_for_session(session, runtime_task_type),
        budget_interactive=False,
        run_id=run_id,
        root_item_intent=root_item_intent,
        budget_admission_status_override=budget_admission_status_override,
        extra_metadata={
            "source": source_channel,
            "agent_session_message": True,
            "runtime_mailbox_role": model_role,
            "latest_user_prompt_overrides_history": model_role == "user",
            **{
                key: metadata_base[key]
                for key in ("terminal_session_resume", "resumed_from_terminal_state")
                if key in metadata_base
            },
            # Caller-supplied metadata may add context but can never reroute
            # the reserved structured parent binding.
            **{key: value for key, value in (extra_metadata or {}).items() if key not in _RESERVED_PARENT_ROUTE_KEYS},
            # Reserved structured parent route, stamped from durable session
            # fields. It always wins over caller-supplied extra_metadata; an
            # absent durable binding (None) never clobbers a caller value.
            **{
                key: value
                for key, value in {
                    "parent_session_id": _text(parent_session_id or getattr(session, "parent_session_id", None))
                    or None,
                    "parent_agent_id": _text(getattr(session, "peer_agent_id", None)) or None,
                }.items()
                if value is not None or key not in (extra_metadata or {})
            },
        },
    )
    return {
        **run,
        "ok": True,
        "status": (
            "waiting_budget_approval" if str(run.get("status") or "") == "waiting_budget_approval" else "started"
        ),
        "consumer": "continuation_turn",
        "child_session_id": str(session.id),
    }


async def continue_parent_session_with_task_notification(
    *,
    db: AsyncSession,
    agent: Agent,
    user: User,
    session: ChatSession,
    task_id: str,
    task_type: str,
    status: str,
    summary: str,
    child_session_id: str | uuid.UUID | None = None,
    child_agent_name: str | None = None,
    source: str = "task_notification",
    metadata: dict[str, Any] | None = None,
    artifacts: list[dict[str, Any]] | None = None,
    resume_parent: bool = True,
) -> dict[str, Any]:
    message = build_task_notification_message(
        task_id=task_id,
        task_type=task_type,
        status=status,
        summary=summary,
        child_session_id=child_session_id,
        child_agent_name=child_agent_name,
        source=source,
        artifacts=artifacts,
    )
    display_content = _task_notification_display_content(
        child_agent_name=child_agent_name,
        status=status,
        summary=summary,
    )
    runtime_context = build_task_notification_runtime_context(
        task_id=task_id,
        task_type=task_type,
        status=status,
        summary=summary,
        child_session_id=child_session_id,
        child_agent_name=child_agent_name,
        source=source,
        artifacts=artifacts,
        model_context=_text((metadata or {}).get("model_context")) or None,
    )
    artifact_paths = [str(part.get("path")) for part in artifacts or [] if part.get("path")]
    artifact_ids = [
        str(part.get("artifact_id") or part.get("id"))
        for part in artifacts or []
        if part.get("artifact_id") or part.get("id")
    ]
    task_metadata = {
        "task_notification": True,
        "task_id": task_id,
        "task_type": task_type,
        "status": status,
        "summary": summary,
        "child_session_id": _text(child_session_id) or None,
        "child_agent_name": _text(child_agent_name) or None,
        "source": "task_notification",
        "notification_source": source,
        "message": display_content,
        "display_content": display_content,
        "task_notification_envelope": message,
        "artifacts": artifacts or [],
        "artifact_paths": artifact_paths,
        "artifact_ids": artifact_ids,
        **(metadata or {}),
    }
    runtime_action_payload = _build_task_notification_runtime_action_payload(
        task_id=task_id,
        task_type=task_type,
        status=status,
        summary=summary,
        child_session_id=child_session_id,
        child_agent_name=child_agent_name,
        source=source,
        metadata=metadata,
    )
    if runtime_action_payload is not None:
        runtime_event = build_session_native_event(runtime_action_payload)
        await _append_mailbox_event(
            db=db,
            agent=agent,
            user=user,
            session=session,
            event_type=str(runtime_action_payload["type"]),
            message=str(runtime_action_payload.get("message") or display_content),
            parent_session_id=getattr(session, "parent_session_id", None) or getattr(session, "id", None),
            metadata={
                **task_metadata,
                **runtime_action_payload,
                "runtime_action_from_task_notification": True,
            },
            role="system",
            parts=[runtime_event["part"]] if isinstance(runtime_event.get("part"), dict) else None,
            materialize_chat_message=False,
            source="task_notification",
        )
    if not resume_parent:
        await _append_mailbox_event(
            db=db,
            agent=agent,
            user=user,
            session=session,
            event_type="agent_task_notification",
            message=display_content,
            parent_session_id=getattr(session, "parent_session_id", None) or getattr(session, "id", None),
            metadata={
                **task_metadata,
                "consumer": "session_projection",
            },
            role="system",
            parts=artifacts,
            materialize_chat_message=False,
            source="task_notification",
        )
        await db.commit()
        return {
            "ok": True,
            "status": "projected",
            "consumer": "session_projection",
            "child_session_id": str(session.id),
        }
    return await continue_agent_session_from_mailbox(
        db=db,
        agent=agent,
        user=user,
        session=session,
        message=message,
        parent_session_id=getattr(session, "parent_session_id", None) or getattr(session, "id", None),
        display_content=display_content,
        event_type="agent_task_notification",
        mailbox_kind="task_notification",
        materialize_chat_message=False,
        source_channel="task_notification",
        extra_metadata=task_metadata,
        parts=artifacts,
        event_content=display_content,
        llm_content=runtime_context,
        mailbox_role="system",
        model_message_role="system",
    )
