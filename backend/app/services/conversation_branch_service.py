from __future__ import annotations

import asyncio
import copy
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Literal

from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.agent import Agent
from app.models.chat_session import ChatSession
from app.models.chat_transcript_event import ChatTranscriptEvent
from app.models.user import User
from app.services.chat_transcript import append_session_event
from app.services.session_user_checkpoint import (
    event_role as shared_event_role,
    is_human_input_checkpoint,
    is_human_input_row,
    user_checkpoint_content,
    user_checkpoint_events,
)
from app.services.session_workspace_snapshot import (
    clone_workspace_snapshot_for_session,
    delete_workspace_snapshot,
)

ConversationBranchMode = Literal[
    "fork",
    "branch",
    "edit",
    "insert_before",
    "insert_after",
    "reply",
    "regenerate",
    "side_question",
]

_CONTENT_REQUIRED_MODES = {"edit", "insert_before", "insert_after", "reply", "side_question"}
_VALID_MODES = {*_CONTENT_REQUIRED_MODES, "fork", "branch", "regenerate"}
_LEGACY_BRANCH_TITLE_SUFFIX_RE = re.compile(
    r"\s+\((?:branch|fork|edit|insert|reply|regenerate|btw|clear)\)\s*$",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class BranchRunRequest:
    content: str
    display_content: str
    file_name: str = ""
    append_user_message: bool = True
    attachments: list[dict[str, Any]] | None = None
    parts: list[dict[str, Any]] | None = None
    extra_metadata: dict[str, Any] | None = None


@dataclass(frozen=True, slots=True)
class ConversationBranchResult:
    session: ChatSession
    branch: dict[str, Any]
    run_request: BranchRunRequest | None


def _uuid(value: uuid.UUID | str) -> uuid.UUID:
    return value if isinstance(value, uuid.UUID) else uuid.UUID(str(value))


def _event_role(event: ChatTranscriptEvent) -> str | None:
    shared_role = shared_event_role(event)
    if shared_role:
        return shared_role
    event_type = getattr(event, "event_type", None)
    if event_type == "assistant_message":
        return "assistant"
    if event_type in {"tool_call", "tool_result"}:
        return "tool_call"
    return None


def _event_t0_role(event: ChatTranscriptEvent, role: str | None) -> str | None:
    metadata = getattr(event, "metadata_json", None) or {}
    t0_role = metadata.get("t0_role")
    return t0_role if isinstance(t0_role, str) and t0_role else role


def _prefix_includes_anchor(mode: str, *, anchor: ChatTranscriptEvent | None = None) -> bool:
    if anchor is not None and _event_role(anchor) == "user" and mode in {"fork", "branch"}:
        return False
    return mode in {"fork", "branch", "insert_after", "reply", "side_question"}


def _draft_content_for_anchor(mode: str, anchor: ChatTranscriptEvent) -> str:
    if _event_role(anchor) == "user" and mode in {"fork", "branch"}:
        if is_human_input_checkpoint(anchor):
            return user_checkpoint_content(anchor)
        return (getattr(anchor, "content", None) or "").strip()
    return ""


def _branch_title(source_session: ChatSession, mode: str, title: str | None) -> str:
    if title and title.strip():
        return title.strip()[:200]
    source_title = (getattr(source_session, "title", None) or "Conversation").strip()
    source_metadata = getattr(source_session, "transcript_metadata_json", None)
    source_is_branch = bool(
        getattr(source_session, "parent_session_id", None)
        or (isinstance(source_metadata, dict) and source_metadata.get("branch_mode"))
    )
    if source_is_branch:
        while _LEGACY_BRANCH_TITLE_SUFFIX_RE.search(source_title):
            source_title = _LEGACY_BRANCH_TITLE_SUFFIX_RE.sub("", source_title).strip()
    return (source_title or "Conversation")[:200]


def _session_contract_overrides(source_session: ChatSession, mode: str) -> dict[str, str]:
    if mode == "side_question":
        return {
            "session_kind": "side_question",
            "runtime_source": "side_question",
            "listed_surface": "sidechain",
        }
    return {
        "session_kind": getattr(source_session, "session_kind", None) or "human_chat",
        "runtime_source": getattr(source_session, "runtime_source", None) or "web_chat",
        "listed_surface": getattr(source_session, "listed_surface", None) or "chat",
    }


def _branch_metadata(
    *,
    source_session: ChatSession,
    mode: str,
    root_session_id: uuid.UUID,
    anchor: ChatTranscriptEvent,
    user: User,
    prefix_events: list[ChatTranscriptEvent],
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "branch_mode": mode,
        "source_session_id": str(source_session.id),
        "root_session_id": str(root_session_id),
        "anchor_event_id": str(anchor.id),
        "anchor_sequence": anchor.sequence,
        "created_by_user_id": str(getattr(user, "id", "")),
    }
    if mode == "side_question":
        metadata.update(
            {
                "side_session": True,
                "side_session_kind": "btw",
                "tool_policy": "disabled_by_default",
                "max_turns": 1,
            }
        )
    source_metadata = getattr(source_session, "transcript_metadata_json", None)
    source_snapshots = source_metadata.get("workspace_snapshots") if isinstance(source_metadata, dict) else None
    if isinstance(source_snapshots, dict):
        prefix_event_ids = {str(getattr(event, "id", "")) for event in prefix_events}
        inherited_snapshots = {
            event_id: copy.deepcopy(snapshot)
            for event_id, snapshot in source_snapshots.items()
            if str(event_id) in prefix_event_ids and isinstance(snapshot, dict)
        }
        if inherited_snapshots:
            metadata["workspace_snapshots"] = inherited_snapshots
    return metadata


async def _load_anchor_event(
    *,
    db: AsyncSession,
    source_session: ChatSession,
    agent_id: uuid.UUID,
    anchor_event_id: uuid.UUID,
) -> ChatTranscriptEvent:
    result = await db.execute(
        select(ChatTranscriptEvent).where(
            ChatTranscriptEvent.id == anchor_event_id,
            ChatTranscriptEvent.session_id == source_session.id,
            ChatTranscriptEvent.agent_id == agent_id,
        )
    )
    event = result.scalar_one_or_none()
    if event is None:
        raise HTTPException(status_code=404, detail="Anchor transcript event not found")
    return event


async def _load_prefix_events(
    *,
    db: AsyncSession,
    source_session_id: uuid.UUID,
    anchor: ChatTranscriptEvent,
    include_anchor: bool,
) -> list[ChatTranscriptEvent]:
    predicate = (
        ChatTranscriptEvent.sequence <= anchor.sequence
        if include_anchor
        else ChatTranscriptEvent.sequence < anchor.sequence
    )
    result = await db.execute(
        select(ChatTranscriptEvent)
        .where(
            ChatTranscriptEvent.session_id == source_session_id,
            predicate,
            ChatTranscriptEvent.listed_surface == "chat",
        )
        .order_by(ChatTranscriptEvent.sequence.asc())
    )
    return list(result.scalars().all())


async def _copy_prefix_events(
    *,
    db: AsyncSession,
    agent: Agent,
    user: User,
    branch_session: ChatSession,
    source_session: ChatSession,
    mode: str,
    events: list[ChatTranscriptEvent],
    anchor: ChatTranscriptEvent | None = None,
    include_anchor: bool = False,
) -> tuple[list[str], dict[str, str]]:
    copied_event_ids: list[str] = []
    copied_event_map: dict[str, str] = {}
    previous_new_event_id: uuid.UUID | None = None
    authoritative_user_ids = {str(event.id) for event in user_checkpoint_events(events)}
    anchor_item_id = (
        str(getattr(anchor, "item_id", "") or "") if anchor is not None and is_human_input_checkpoint(anchor) else None
    )
    for event in events:
        if is_human_input_row(event):
            event_item_id = str(getattr(event, "item_id", "") or "")
            # The anchored HumanInput item carries its exact content as the
            # draft when the prefix excludes it; superseded accepted bytes and
            # state facts never enter the branch prefix — only the item's
            # latest content lifecycle does. An explicit include-anchor
            # override (fork side threads) opts the anchored item back in.
            if anchor_item_id and not include_anchor and event_item_id == anchor_item_id:
                continue
            if str(event.id) not in authoritative_user_ids:
                continue
        role = _event_role(event)
        content = (
            user_checkpoint_content(event)
            if is_human_input_checkpoint(event)
            else (getattr(event, "content", None) or "")
        )
        result = await append_session_event(
            db=db,
            agent_id=agent.id,
            tenant_id=getattr(agent, "tenant_id", getattr(source_session, "tenant_id", None)),
            session_id=branch_session.id,
            actor_type=getattr(event, "actor_type", None) or "system",
            event_type=getattr(event, "event_type", None) or "event",
            content=content,
            role=role,
            t0_role=_event_t0_role(event, role),
            user_id=getattr(user, "id", None),
            run_id=None,
            parent_event_id=previous_new_event_id,
            root_session_id=branch_session.root_session_id,
            parent_session_id=source_session.id,
            parts=getattr(event, "parts_json", None) or None,
            metadata={
                **(getattr(event, "metadata_json", None) or {}),
                "source": "conversation_branch",
                "branch_mode": mode,
                "projection_only": True,
                "projection_source": "conversation_branch_prefix",
                "semantic_memory_eligible": False,
                "source_session_id": str(source_session.id),
                "copied_from_event_id": str(event.id),
                "copied_from_sequence": getattr(event, "sequence", None),
            },
            visibility_scope=getattr(event, "visibility_scope", None) or "direct_user",
            listed_surface=getattr(event, "listed_surface", None) or "chat",
            source="conversation_branch",
            bridge_to_t0=False,
        )
        previous_new_event_id = result.event_id
        source_event_id = str(event.id)
        copied_event_ids.append(source_event_id)
        copied_event_map[source_event_id] = str(result.event_id)
    return copied_event_ids, copied_event_map


async def _remap_branch_workspace_snapshots(
    branch_session: ChatSession,
    *,
    agent_id: uuid.UUID,
    copied_event_map: dict[str, str],
) -> None:
    metadata = dict(getattr(branch_session, "transcript_metadata_json", None) or {})
    source_snapshots = metadata.get("workspace_snapshots")
    if not isinstance(source_snapshots, dict):
        return
    remapped: dict[str, dict[str, Any]] = {}
    try:
        for source_event_id, snapshot in source_snapshots.items():
            new_event_id = copied_event_map.get(str(source_event_id))
            if not new_event_id or not isinstance(snapshot, dict):
                continue
            cloned = await asyncio.to_thread(
                clone_workspace_snapshot_for_session,
                agent_id=agent_id,
                source_snapshot=snapshot,
                target_session_id=branch_session.id,
                target_checkpoint_event_id=new_event_id,
            )
            remapped[new_event_id] = cloned
    except BaseException:
        for cloned in remapped.values():
            await asyncio.to_thread(
                delete_workspace_snapshot,
                agent_id=agent_id,
                snapshot=cloned,
            )
        raise
    if remapped:
        metadata["workspace_snapshots"] = remapped
    else:
        metadata.pop("workspace_snapshots", None)
    branch_session.transcript_metadata_json = metadata


def _last_user_event(events: list[ChatTranscriptEvent]) -> ChatTranscriptEvent | None:
    for event in reversed(events):
        if _event_role(event) == "user":
            return event
    return None


def _validate_branch_request(*, mode: str, anchor: ChatTranscriptEvent, content: str) -> None:
    if mode == "rewind":
        raise HTTPException(
            status_code=400,
            detail=(
                "Use the rewind session command to move the current session projection, "
                "or branch mode to create a new child session."
            ),
        )
    if mode not in _VALID_MODES:
        raise HTTPException(status_code=400, detail=f"Unsupported branch mode: {mode}")
    if mode in _CONTENT_REQUIRED_MODES and not content.strip():
        raise HTTPException(status_code=400, detail=f"content is required for {mode}")
    role = _event_role(anchor)
    if mode == "edit" and role != "user":
        raise HTTPException(status_code=400, detail="edit requires a user message anchor")
    if mode == "regenerate" and role != "assistant":
        raise HTTPException(status_code=400, detail="regenerate requires an assistant message anchor")


async def create_conversation_branch(
    *,
    db: AsyncSession,
    agent: Agent,
    user: User,
    source_session: ChatSession,
    mode: ConversationBranchMode | str,
    anchor_event_id: uuid.UUID | str,
    content: str = "",
    display_content: str = "",
    file_name: str = "",
    title: str | None = None,
    attachments: list[dict[str, Any]] | None = None,
    parts: list[dict[str, Any]] | None = None,
    branch_session_id: uuid.UUID | str | None = None,
    include_anchor_override: bool | None = None,
) -> ConversationBranchResult:
    mode_text = str(mode)
    anchor_uuid = _uuid(anchor_event_id)
    anchor = await _load_anchor_event(
        db=db,
        source_session=source_session,
        agent_id=agent.id,
        anchor_event_id=anchor_uuid,
    )
    _validate_branch_request(mode=mode_text, anchor=anchor, content=content)

    deterministic_branch_id = _uuid(branch_session_id) if branch_session_id is not None else None
    if deterministic_branch_id is not None:
        existing_branch = await db.get(ChatSession, deterministic_branch_id)
        if existing_branch is not None:
            metadata = dict(getattr(existing_branch, "transcript_metadata_json", None) or {})
            if (
                existing_branch.parent_session_id != source_session.id
                or metadata.get("branch_mode") != mode_text
                or metadata.get("anchor_event_id") != str(anchor.id)
            ):
                raise HTTPException(status_code=409, detail="Branch request id is already bound to another branch")
            return ConversationBranchResult(
                session=existing_branch,
                branch={
                    "mode": mode_text,
                    "source_session_id": str(source_session.id),
                    "root_session_id": str(existing_branch.root_session_id or source_session.id),
                    "session_id": str(existing_branch.id),
                    "anchor_event_id": str(anchor.id),
                    "draft_content": "",
                    "copied_event_ids": list(metadata.get("copied_event_ids") or []),
                    "replayed": True,
                },
                run_request=None,
            )

    include_anchor = (
        include_anchor_override
        if include_anchor_override is not None
        else _prefix_includes_anchor(mode_text, anchor=anchor)
    )
    prefix_events = await _load_prefix_events(
        db=db,
        source_session_id=source_session.id,
        anchor=anchor,
        include_anchor=include_anchor,
    )
    root_session_id = getattr(source_session, "root_session_id", None) or source_session.id
    now = datetime.now(timezone.utc)
    contract_overrides = _session_contract_overrides(source_session, mode_text)
    branch_session_id = deterministic_branch_id or uuid.uuid4()
    tenant_id = getattr(agent, "tenant_id", getattr(source_session, "tenant_id", None))
    target_workspace_uri = f"session://{branch_session_id}/workspace"
    from app.runtime.hooks import HookEvent, emit_hook

    create_hook = await emit_hook(
        HookEvent.WORKTREE_CREATE,
        evidence_mode="independent",
        agent_id=agent.id,
        session_id=str(source_session.id),
        source="conversation_branch_service",
        metadata={
            "tenant_id": str(tenant_id) if tenant_id else None,
            "user_id": str(getattr(user, "id", "") or "") or None,
            "cloud_workspace_kind": "conversation_branch",
            "branch_mode": mode_text,
            "source_session_id": str(source_session.id),
            "target_session_id": str(branch_session_id),
            "anchor_event_id": str(anchor.id),
            "source_workspace_uri": f"session://{source_session.id}/workspace",
            "target_workspace_uri": target_workspace_uri,
        },
    )
    if create_hook and create_hook.block:
        raise HTTPException(
            status_code=409, detail=create_hook.reason or "Conversation branch creation blocked by hook"
        )
    if create_hook and create_hook.worktree_path and create_hook.worktree_path != target_workspace_uri:
        raise HTTPException(
            status_code=409,
            detail="WorktreeCreate hook cannot redirect a governed cloud conversation workspace",
        )
    branch_session = ChatSession(
        id=branch_session_id,
        agent_id=agent.id,
        tenant_id=tenant_id,
        user_id=getattr(user, "id", getattr(source_session, "user_id", None)),
        title=_branch_title(source_session, mode_text, title),
        source_channel=getattr(source_session, "source_channel", None) or "web",
        participant_id=getattr(source_session, "participant_id", None),
        peer_agent_id=getattr(source_session, "peer_agent_id", None),
        delivery_target_json=getattr(source_session, "delivery_target_json", None),
        session_kind=contract_overrides["session_kind"],
        actor_type=getattr(source_session, "actor_type", None) or "user",
        runtime_source=contract_overrides["runtime_source"],
        visibility_scope=getattr(source_session, "visibility_scope", None) or "direct_user",
        listed_surface=contract_overrides["listed_surface"],
        parent_session_id=source_session.id,
        root_session_id=root_session_id,
        transcript_metadata_json=_branch_metadata(
            source_session=source_session,
            mode=mode_text,
            root_session_id=root_session_id,
            anchor=anchor,
            user=user,
            prefix_events=prefix_events,
        ),
        created_at=now,
        last_message_at=now
        if mode_text not in {"fork", "branch"}
        else getattr(source_session, "last_message_at", None),
    )
    db.add(branch_session)
    if hasattr(db, "flush"):
        await db.flush()

    copied_event_ids, copied_event_map = await _copy_prefix_events(
        db=db,
        agent=agent,
        user=user,
        branch_session=branch_session,
        source_session=source_session,
        mode=mode_text,
        events=prefix_events,
        anchor=anchor,
        include_anchor=include_anchor,
    )
    await _remap_branch_workspace_snapshots(
        branch_session,
        agent_id=agent.id,
        copied_event_map=copied_event_map,
    )
    branch_metadata = dict(branch_session.transcript_metadata_json or {})
    branch_metadata["copied_event_ids"] = list(copied_event_ids)
    branch_session.transcript_metadata_json = branch_metadata
    workspace_change_metadata = {
        "tenant_id": str(tenant_id) if tenant_id else None,
        "user_id": str(getattr(user, "id", "") or "") or None,
        "cloud_workspace_kind": "conversation_branch",
        "branch_mode": mode_text,
        "source_session_id": str(source_session.id),
        "target_session_id": str(branch_session.id),
        "old_cwd": f"session://{source_session.id}/workspace",
        "new_cwd": f"session://{branch_session.id}/workspace",
    }
    await emit_hook(
        HookEvent.CWD_CHANGED,
        evidence_db=db,
        agent_id=agent.id,
        session_id=str(branch_session.id),
        source="conversation_branch_service",
        metadata=dict(workspace_change_metadata),
    )
    await emit_hook(
        HookEvent.WORKSPACE_CONTEXT_CHANGED,
        evidence_db=db,
        agent_id=agent.id,
        session_id=str(branch_session.id),
        source="conversation_branch_service",
        metadata={
            **workspace_change_metadata,
            "copied_event_count": len(copied_event_ids),
            "workspace_snapshot_count": len(
                (getattr(branch_session, "transcript_metadata_json", None) or {}).get("workspace_snapshots") or {}
            ),
        },
    )

    run_request: BranchRunRequest | None = None
    if mode_text in _CONTENT_REQUIRED_MODES:
        visible_content = display_content if display_content else content
        extra_metadata = {
            "branch_mode": mode_text,
            "source_session_id": str(source_session.id),
            "branch_session_id": str(branch_session.id),
            "anchor_event_id": str(anchor.id),
        }
        if mode_text == "side_question":
            extra_metadata.update(
                {
                    "side_session": True,
                    "side_session_kind": "btw",
                    "tool_policy": "disabled_by_default",
                    "disable_tools": True,
                    "max_turns": 1,
                }
            )
        run_request = BranchRunRequest(
            content=content,
            display_content=visible_content,
            file_name=file_name,
            append_user_message=True,
            attachments=attachments or [],
            parts=parts or [],
            extra_metadata=extra_metadata,
        )
    elif mode_text == "regenerate":
        last_user = _last_user_event(prefix_events)
        if last_user is None or not user_checkpoint_content(last_user).strip():
            raise HTTPException(status_code=400, detail="regenerate requires a prior user message")
        prompt = user_checkpoint_content(last_user)
        run_request = BranchRunRequest(
            content=prompt,
            display_content=prompt,
            append_user_message=False,
            extra_metadata={
                "branch_mode": mode_text,
                "source_session_id": str(source_session.id),
                "branch_session_id": str(branch_session.id),
                "anchor_event_id": str(anchor.id),
                "regenerate_from_event_id": str(anchor.id),
                "regenerate_prompt_source_event_id": str(last_user.id),
                "regenerate_prompt": prompt,
                "semantic_source_refs": [
                    {
                        "session_id": str(source_session.id),
                        "event_id": str(last_user.id),
                        "role": "user",
                        "kind": "regenerate_prompt",
                    }
                ],
            },
        )

    draft_content = _draft_content_for_anchor(mode_text, anchor)

    return ConversationBranchResult(
        session=branch_session,
        branch={
            "mode": mode_text,
            "source_session_id": str(source_session.id),
            "root_session_id": str(root_session_id),
            "session_id": str(branch_session.id),
            "anchor_event_id": str(anchor.id),
            "draft_content": draft_content,
            "copied_event_ids": copied_event_ids,
        },
        run_request=run_request,
    )
