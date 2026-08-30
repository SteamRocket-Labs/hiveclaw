"""Canonical provider-history reconstruction for durable Session V2.

Cloud conversation truth is split across ordered ``ChatTranscriptEvent`` rows
and immutable, round-committed provider result seals. This read model joins
those facts without manufacturing semantic text, silently falling back to an
unanchored compatibility table, or pre-trimming authorized evidence. Physical
context-window pressure remains owned by the kernel's model-led compaction
boundary.
"""

from __future__ import annotations

import copy
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit import ChatMessage
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.session_v2 import SessionModelResult, SessionToolInvocation, SessionTurnInput
from app.services.session_user_checkpoint import (
    event_lifecycle,
    event_role,
    is_human_input_checkpoint,
    user_checkpoint_content,
)


_BRANCH_PREFIX_SOURCE = "conversation_branch_prefix"


class SessionSemanticHistoryUnavailable(RuntimeError):
    """Typed fail-closed state raised before any provider request starts."""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        run_id: uuid.UUID,
        tenant_id: uuid.UUID,
        agent_id: uuid.UUID,
        session_id: uuid.UUID,
        retryable: bool,
        evidence_refs: Iterable[str] = (),
    ) -> None:
        super().__init__(message)
        self.code = str(code)
        self.run_id = run_id
        self.tenant_id = tenant_id
        self.agent_id = agent_id
        self.session_id = session_id
        self.retryable = bool(retryable)
        self.evidence_refs = tuple(str(ref) for ref in evidence_refs)

    def receipt(self) -> dict[str, Any]:
        return {
            "status": "unavailable",
            "error_code": self.code,
            "retryable": self.retryable,
            "evidence_refs": list(self.evidence_refs),
        }


@dataclass(frozen=True, slots=True)
class SessionSemanticMessage:
    """Provider-compatible entry with canonical projection coordinates."""

    id: str
    role: str
    content: str | None
    created_at: datetime
    sequence_start: int
    sequence_end: int
    group_id: str
    source_event_ids: tuple[str, ...]
    thinking: str | None = None
    thinking_signature: str | None = None
    tool_calls: list[dict[str, Any]] | None = None
    tool_call_id: str | None = None

    @property
    def sequence(self) -> int:
        return self.sequence_start

    def get(self, key: str, default: Any = None) -> Any:
        return getattr(self, key, default)


@dataclass(frozen=True, slots=True)
class SessionSemanticHistory:
    messages: list[SessionSemanticMessage]
    receipt: dict[str, Any]


@dataclass(frozen=True, slots=True)
class _EventView:
    target: ChatTranscriptEvent
    source: ChatTranscriptEvent

    @property
    def sequence(self) -> int:
        return int(self.target.sequence)

    @property
    def created_at(self) -> datetime:
        value = self.target.created_at
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value

    @property
    def target_event_id(self) -> str:
        return str(self.target.id)

    @property
    def is_branch_copy(self) -> bool:
        return self.target.id != self.source.id


def _uuid_or_none(value: Any) -> uuid.UUID | None:
    if value in {None, ""}:
        return None
    try:
        return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _event_payload(event: ChatTranscriptEvent) -> dict[str, Any]:
    metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
    payload = metadata.get("v2_payload")
    return dict(payload) if isinstance(payload, dict) else dict(metadata)


def _source_scope_run_id(event: ChatTranscriptEvent) -> uuid.UUID | None:
    if event.run_id is not None:
        return event.run_id
    scope = event.scope_json if isinstance(event.scope_json, dict) else {}
    return _uuid_or_none(scope.get("run_id"))


def _source_input_id(event: ChatTranscriptEvent) -> uuid.UUID | None:
    return event.input_id or _uuid_or_none(_event_payload(event).get("input_id")) or event.item_id


def _branch_source_id(event: ChatTranscriptEvent) -> uuid.UUID | None:
    metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
    if metadata.get("projection_source") != _BRANCH_PREFIX_SOURCE:
        return None
    return _uuid_or_none(metadata.get("copied_from_event_id"))


def _unavailable(
    *,
    code: str,
    message: str,
    run_id: uuid.UUID,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    retryable: bool = True,
    evidence_refs: Iterable[str] = (),
) -> SessionSemanticHistoryUnavailable:
    return SessionSemanticHistoryUnavailable(
        code=code,
        message=message,
        run_id=run_id,
        tenant_id=tenant_id,
        agent_id=agent_id,
        session_id=session_id,
        retryable=retryable,
        evidence_refs=evidence_refs,
    )


async def _load_event_views(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
) -> tuple[list[_EventView], dict[str, Any]]:
    target_events = list(
        (
            await db.execute(
                select(ChatTranscriptEvent)
                .where(
                    ChatTranscriptEvent.tenant_id == tenant_id,
                    ChatTranscriptEvent.agent_id == agent_id,
                    ChatTranscriptEvent.session_id == session_id,
                    ChatTranscriptEvent.listed_surface == "chat",
                )
                .order_by(ChatTranscriptEvent.sequence.asc())
            )
        ).scalars()
    )
    branch_targets: list[tuple[ChatTranscriptEvent, uuid.UUID]] = []
    for event in target_events:
        metadata = event.metadata_json if isinstance(event.metadata_json, dict) else {}
        if metadata.get("projection_source") != _BRANCH_PREFIX_SOURCE:
            continue
        source_id = _branch_source_id(event)
        if source_id is None:
            raise _unavailable(
                code="branch_prefix_lineage_invalid",
                message="A copied branch prefix event has no valid canonical source reference.",
                run_id=run_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                evidence_refs=(f"chat_transcript_event:{event.id}",),
            )
        branch_targets.append((event, source_id))

    source_by_id: dict[uuid.UUID, ChatTranscriptEvent] = {}
    if branch_targets:
        requested_ids = {source_id for _target, source_id in branch_targets}
        source_rows = list(
            (
                await db.execute(
                    select(ChatTranscriptEvent).where(
                        ChatTranscriptEvent.id.in_(requested_ids),
                        ChatTranscriptEvent.tenant_id == tenant_id,
                        ChatTranscriptEvent.agent_id == agent_id,
                    )
                )
            ).scalars()
        )
        source_by_id = {event.id: event for event in source_rows}
        if set(source_by_id) != requested_ids:
            missing = sorted(str(value) for value in requested_ids - set(source_by_id))
            raise _unavailable(
                code="branch_prefix_source_unavailable",
                message="Canonical source events for the copied branch prefix are unavailable.",
                run_id=run_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                evidence_refs=tuple(f"chat_transcript_event:{value}" for value in missing),
            )

    views: list[_EventView] = []
    for target in target_events:
        source_id = _branch_source_id(target)
        if source_id is None:
            views.append(_EventView(target=target, source=target))
            continue
        source = source_by_id[source_id]
        metadata = target.metadata_json if isinstance(target.metadata_json, dict) else {}
        claimed_source_session_id = _uuid_or_none(metadata.get("source_session_id"))
        if claimed_source_session_id is None or claimed_source_session_id != source.session_id:
            raise _unavailable(
                code="branch_prefix_authority_mismatch",
                message="Copied branch prefix lineage does not match its claimed source Session.",
                run_id=run_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                retryable=False,
                evidence_refs=(f"chat_transcript_event:{target.id}", f"chat_transcript_event:{source.id}"),
            )
        views.append(_EventView(target=target, source=source))

    source_session_ids = sorted({str(view.source.session_id) for view in views if view.is_branch_copy})
    branch_receipt: dict[str, Any] = {"status": "not_applicable"}
    if branch_targets:
        branch_receipt = {
            "status": "resolved",
            "copied_event_count": len(branch_targets),
            "source_session_id": source_session_ids[0] if len(source_session_ids) == 1 else None,
            "source_session_ids": source_session_ids,
        }
    return views, branch_receipt


async def _current_run_input_ids(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    session_id: uuid.UUID,
    run_id: uuid.UUID,
) -> set[uuid.UUID]:
    return set(
        (
            await db.execute(
                select(SessionTurnInput.id).where(
                    SessionTurnInput.tenant_id == tenant_id,
                    SessionTurnInput.session_id == session_id,
                    SessionTurnInput.target_run_id == run_id,
                )
            )
        ).scalars()
    )


def _user_messages(
    views: list[_EventView],
    *,
    current_run_id: uuid.UUID,
    current_input_ids: set[uuid.UUID],
) -> list[SessionSemanticMessage]:
    latest_by_input: dict[uuid.UUID, _EventView] = {}
    for view in views:
        source = view.source
        if not is_human_input_checkpoint(source):
            continue
        input_id = _source_input_id(source)
        if input_id is None or input_id in current_input_ids:
            continue
        if _source_scope_run_id(source) == current_run_id:
            continue
        current = latest_by_input.get(input_id)
        if current is None:
            latest_by_input[input_id] = view
            continue
        current_rank = (1 if event_lifecycle(current.source) == "revised" else 0, current.sequence)
        candidate_rank = (1 if event_lifecycle(source) == "revised" else 0, view.sequence)
        if candidate_rank >= current_rank:
            latest_by_input[input_id] = view

    messages: list[SessionSemanticMessage] = []
    for input_id, view in latest_by_input.items():
        content = user_checkpoint_content(view.source)
        if not content:
            continue
        messages.append(
            SessionSemanticMessage(
                id=f"session-input:{input_id}",
                role="user",
                content=content,
                created_at=view.created_at,
                sequence_start=view.sequence,
                sequence_end=view.sequence,
                group_id=f"input:{input_id}",
                source_event_ids=(view.target_event_id,),
            )
        )
    return messages


async def _committed_round_messages(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    current_run_id: uuid.UUID,
    views: list[_EventView],
) -> tuple[list[SessionSemanticMessage], list[dict[str, Any]], int]:
    committed_views: dict[uuid.UUID, _EventView] = {}
    tool_result_views: dict[uuid.UUID, _EventView] = {}
    for view in views:
        source = view.source
        if _source_scope_run_id(source) == current_run_id:
            continue
        if (
            source.schema_version == 2
            and source.item_kind == "result_commit"
            and source.lifecycle == "round_committed"
            and source.result_id is not None
        ):
            committed_views[source.result_id] = view
        if (
            source.schema_version == 2
            and source.item_kind == "tool_result"
            and source.lifecycle == "completed"
            and source.invocation_id is not None
        ):
            tool_result_views[source.invocation_id] = view
    if not committed_views:
        return [], [], 0

    result_rows = list(
        (
            await db.execute(
                select(SessionModelResult).where(
                    SessionModelResult.id.in_(set(committed_views)),
                    SessionModelResult.tenant_id == tenant_id,
                    SessionModelResult.state == "round_committed",
                )
            )
        ).scalars()
    )
    results_by_id = {row.id: row for row in result_rows}
    if set(results_by_id) != set(committed_views):
        missing = sorted(str(value) for value in set(committed_views) - set(results_by_id))
        raise _unavailable(
            code="committed_model_seal_unavailable",
            message="A canonical round commit has no matching immutable provider result seal.",
            run_id=current_run_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            evidence_refs=tuple(f"session_model_result:{value}" for value in missing),
        )
    for result_id, result in results_by_id.items():
        source_event = committed_views[result_id].source
        if result.session_id != source_event.session_id or result.run_id != _source_scope_run_id(source_event):
            raise _unavailable(
                code="committed_model_seal_authority_mismatch",
                message="A committed provider result is not bound to its canonical Session and Run.",
                run_id=current_run_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                retryable=False,
                evidence_refs=(
                    f"session_model_result:{result.id}",
                    f"chat_transcript_event:{source_event.id}",
                ),
            )

    logical_results = [row for row in result_rows if int((row.seal_json or {}).get("continuation_index") or 0) == 0]
    provider_request_ids = {row.provider_request_id for row in logical_results}
    invocation_rows = (
        list(
            (
                await db.execute(
                    select(SessionToolInvocation).where(
                        SessionToolInvocation.tenant_id == tenant_id,
                        SessionToolInvocation.provider_request_id.in_(provider_request_ids),
                    )
                )
            ).scalars()
        )
        if provider_request_ids
        else []
    )
    invocations_by_request: dict[str, dict[str, SessionToolInvocation]] = {}
    results_by_request = {row.provider_request_id: row for row in logical_results}
    for invocation in invocation_rows:
        expected_result = results_by_request[invocation.provider_request_id]
        if invocation.session_id != expected_result.session_id or invocation.run_id != expected_result.run_id:
            raise _unavailable(
                code="tool_invocation_authority_mismatch",
                message="A tool invocation is not bound to its committed provider round.",
                run_id=current_run_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                retryable=False,
                evidence_refs=(
                    f"session_model_result:{expected_result.id}",
                    f"session_tool_invocation:{invocation.id}",
                ),
            )
        invocations_by_request.setdefault(invocation.provider_request_id, {})[invocation.provider_tool_use_id] = (
            invocation
        )

    messages: list[SessionSemanticMessage] = []
    held: list[dict[str, Any]] = []
    for result in logical_results:
        committed_view = committed_views[result.id]
        seal = result.seal_json if isinstance(result.seal_json, dict) else None
        response = seal.get("response") if seal is not None else None
        if not isinstance(response, dict):
            raise _unavailable(
                code="committed_provider_response_unavailable",
                message="A committed model result does not contain its immutable provider response.",
                run_id=current_run_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                evidence_refs=(
                    f"session_model_result:{result.id}",
                    f"chat_transcript_event:{committed_view.target.id}",
                ),
            )
        raw_tool_calls = response.get("tool_calls")
        tool_calls = copy.deepcopy(raw_tool_calls) if isinstance(raw_tool_calls, list) else []
        invocation_map = invocations_by_request.get(result.provider_request_id, {})
        completed_pairs: list[tuple[dict[str, Any], SessionToolInvocation, _EventView]] = []
        missing_tool_use_ids: list[str] = []
        for tool_call in tool_calls:
            tool_use_id = str(tool_call.get("id") or "") if isinstance(tool_call, dict) else ""
            invocation = invocation_map.get(tool_use_id)
            result_view = tool_result_views.get(invocation.id) if invocation is not None else None
            if (
                not tool_use_id
                or invocation is None
                or invocation.effect_state != "effect_committed"
                or invocation.result_event_id is None
                or result_view is None
                or result_view.source.id != invocation.result_event_id
            ):
                missing_tool_use_ids.append(tool_use_id or "<missing-provider-tool-use-id>")
                continue
            completed_pairs.append((tool_call, invocation, result_view))
        if missing_tool_use_ids:
            held.append(
                {
                    "kind": "unsettled_tool_round",
                    "model_result_id": str(result.id),
                    "provider_request_id": result.provider_request_id,
                    "missing_provider_tool_use_ids": missing_tool_use_ids,
                    "evidence_refs": [
                        f"session_model_result:{result.id}",
                        f"chat_transcript_event:{committed_view.target.id}",
                    ],
                }
            )
            continue

        end_sequence = max([committed_view.sequence, *(pair[2].sequence for pair in completed_pairs)])
        source_event_ids = (
            committed_view.target_event_id,
            *(pair[2].target_event_id for pair in completed_pairs),
        )
        content = response.get("content")
        if content is not None and not isinstance(content, str):
            raise _unavailable(
                code="committed_provider_response_invalid",
                message="A committed provider response has a non-string content contract.",
                run_id=current_run_id,
                tenant_id=tenant_id,
                agent_id=agent_id,
                session_id=session_id,
                retryable=False,
                evidence_refs=(f"session_model_result:{result.id}",),
            )
        messages.append(
            SessionSemanticMessage(
                id=f"session-model-result:{result.id}",
                role="assistant",
                content=content,
                created_at=committed_view.created_at,
                sequence_start=committed_view.sequence,
                sequence_end=end_sequence,
                group_id=f"round:{result.id}",
                source_event_ids=tuple(source_event_ids),
                thinking=response.get("reasoning_content")
                if isinstance(response.get("reasoning_content"), str)
                else None,
                thinking_signature=response.get("reasoning_signature")
                if isinstance(response.get("reasoning_signature"), str)
                else None,
                tool_calls=tool_calls or None,
            )
        )
        for tool_call, invocation, result_view in completed_pairs:
            payload = _event_payload(result_view.source)
            tool_content = payload.get("content")
            if not isinstance(tool_content, str):
                tool_content = ""
            messages.append(
                SessionSemanticMessage(
                    id=f"session-tool-result:{invocation.id}",
                    role="tool",
                    content=tool_content,
                    created_at=result_view.created_at,
                    sequence_start=committed_view.sequence,
                    sequence_end=end_sequence,
                    group_id=f"round:{result.id}",
                    source_event_ids=tuple(source_event_ids),
                    tool_call_id=str(tool_call["id"]),
                )
            )
    return messages, held, len(result_rows) - len(logical_results)


async def _legacy_messages(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    current_run_id: uuid.UUID,
    views: list[_EventView],
) -> list[SessionSemanticMessage]:
    candidates: list[_EventView] = []
    message_ids: set[uuid.UUID] = set()
    for view in views:
        source = view.source
        if source.schema_version == 2 or _source_scope_run_id(source) == current_run_id:
            continue
        candidates.append(view)
        if source.message_id is not None:
            message_ids.add(source.message_id)
    rows = (
        list(
            (
                await db.execute(
                    select(ChatMessage).where(
                        ChatMessage.id.in_(message_ids),
                        ChatMessage.tenant_id == tenant_id,
                        ChatMessage.agent_id == agent_id,
                    )
                )
            ).scalars()
        )
        if message_ids
        else []
    )
    rows_by_id = {row.id: row for row in rows}
    messages: list[SessionSemanticMessage] = []
    seen_message_ids: set[uuid.UUID] = set()
    for view in candidates:
        source = view.source
        if source.message_id is not None:
            if source.message_id in seen_message_ids:
                continue
            row = rows_by_id.get(source.message_id)
            if row is None or row.role not in {"user", "assistant", "tool_call"}:
                continue
            if row.conversation_id != str(source.session_id):
                raise _unavailable(
                    code="legacy_message_authority_mismatch",
                    message="An anchored legacy message is not bound to its canonical Session.",
                    run_id=current_run_id,
                    tenant_id=tenant_id,
                    agent_id=agent_id,
                    session_id=source.session_id,
                    retryable=False,
                    evidence_refs=(
                        f"chat_transcript_event:{source.id}",
                        f"chat_message:{row.id}",
                    ),
                )
            seen_message_ids.add(source.message_id)
            messages.append(
                SessionSemanticMessage(
                    id=str(row.id),
                    role=row.role,
                    content=row.content,
                    created_at=view.created_at,
                    sequence_start=view.sequence,
                    sequence_end=view.sequence,
                    group_id=f"legacy-message:{row.id}",
                    source_event_ids=(view.target_event_id,),
                    thinking=row.thinking,
                    thinking_signature=row.thinking_signature,
                )
            )
            continue
        role = event_role(source)
        if role not in {"user", "assistant"}:
            continue
        content = user_checkpoint_content(source) if role == "user" else str(source.content or "")
        if not content:
            continue
        messages.append(
            SessionSemanticMessage(
                id=f"legacy-event:{view.target.id}",
                role=role,
                content=content,
                created_at=view.created_at,
                sequence_start=view.sequence,
                sequence_end=view.sequence,
                group_id=f"legacy-event:{view.target.id}",
                source_event_ids=(view.target_event_id,),
            )
        )
    return messages


async def load_session_semantic_history(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    current_run_id: uuid.UUID,
) -> SessionSemanticHistory:
    """Load every authorized committed semantic entry for one provider turn."""

    try:
        views, branch_receipt = await _load_event_views(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            run_id=current_run_id,
        )
        current_input_ids = await _current_run_input_ids(
            db,
            tenant_id=tenant_id,
            session_id=session_id,
            run_id=current_run_id,
        )
        users = _user_messages(
            views,
            current_run_id=current_run_id,
            current_input_ids=current_input_ids,
        )
        rounds, held_items, ignored_continuations = await _committed_round_messages(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            current_run_id=current_run_id,
            views=views,
        )
        legacy = await _legacy_messages(
            db,
            tenant_id=tenant_id,
            agent_id=agent_id,
            current_run_id=current_run_id,
            views=views,
        )
    except SessionSemanticHistoryUnavailable:
        raise
    except Exception as exc:
        raise _unavailable(
            code="canonical_session_history_unavailable",
            message=f"Canonical session history is unavailable ({type(exc).__name__}).",
            run_id=current_run_id,
            tenant_id=tenant_id,
            agent_id=agent_id,
            session_id=session_id,
            evidence_refs=("chat_transcript_events", "session_model_results", "session_tool_invocations"),
        ) from exc

    messages = [*users, *rounds, *legacy]
    messages.sort(
        key=lambda message: (
            message.sequence_start,
            0 if message.role == "user" else (1 if message.role == "assistant" else 2),
            message.id,
        )
    )
    status = "degraded" if held_items else ("complete" if messages else "empty")
    receipt = {
        "schema": "hive.session_semantic_history_receipt.v1",
        "status": status,
        "truth_source": "chat_transcript_events+session_model_results",
        "session_id": str(session_id),
        "current_run_id": str(current_run_id),
        "mechanical_message_limit_applied": False,
        "event_count": len(views),
        "message_count": len(messages),
        "coverage": {
            "user_checkpoints": len(users),
            "committed_provider_messages": sum(1 for message in rounds if message.role == "assistant"),
            "settled_tool_results": sum(1 for message in rounds if message.role == "tool"),
            "anchored_legacy_messages": len(legacy),
            "ignored_physical_continuation_results": ignored_continuations,
        },
        "excluded_current_run_input_ids": sorted(str(value) for value in current_input_ids),
        "held_items": held_items,
        "branch_prefix": branch_receipt,
    }
    return SessionSemanticHistory(messages=messages, receipt=receipt)
