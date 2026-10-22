"""Durable, per-part external-channel delivery for terminal run results."""

from __future__ import annotations

import contextlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
import uuid
from typing import Any, Literal

from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.config import get_settings
from app.database import async_session, enter_rls_bypass, tenant_scoped_session
from app.models.audit import AuditLog
from app.models.channel_config import ChannelConfig
from app.models.channel_delivery_outbox import ChannelDeliveryOutbox
from app.models.chat_artifact import ChatArtifact
from app.models.chat_session import ChatSession
from app.models.external_principal import ExternalPrincipal
from app.services.channel_delivery_service import ChannelDeliveryService, DeliveryResult
from app.services.knowledge_provenance import (
    apply_inherited_knowledge_provenance,
    knowledge_content_sensitivity,
    load_transcript_knowledge_provenance,
)

DeliveryKind = Literal["terminal_result", "interactive_prompt"]
TextSender = Callable[..., Awaitable[DeliveryResult]]
FileSender = Callable[..., Awaitable[DeliveryResult]]
_DELIVERY_NAMESPACE = uuid.UUID("36827e94-29df-4a93-8ed8-88bf921ff571")


class ChannelDeliveryPermanentError(RuntimeError):
    """The immutable target cannot be delivered without an operator change."""


class ChannelDeliveryRetryableError(RuntimeError):
    """The provider explicitly reported a safe-to-retry failure."""


class ChannelDeliveryAmbiguousError(RuntimeError):
    """The provider may have accepted a part but no receipt was committed."""


def _uuid(value: uuid.UUID | str | None, *, field: str, optional: bool = False) -> uuid.UUID | None:
    if value is None and optional:
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError, AttributeError) as exc:
        raise ValueError(f"{field} must be a UUID") from exc


def _target_snapshot(target: dict[str, Any]) -> tuple[dict[str, Any], str, str]:
    normalized = ChannelDeliveryService.normalize_reply_target(target)
    if not normalized:
        raise ValueError("channel delivery target is required")
    channel = str(normalized["channel"]).strip().lower()
    encoded = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return normalized, channel, hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ChannelDeliveryIntent:
    tenant_id: uuid.UUID | str
    runtime_task_id: uuid.UUID | str
    agent_id: uuid.UUID | str
    session_id: uuid.UUID | str
    delivery_target: dict[str, Any]
    text: str
    terminal_status: str
    user_id: uuid.UUID | str | None = None
    external_principal_id: uuid.UUID | str | None = None
    channel_config_id: uuid.UUID | str | None = None
    artifact_ids: list[uuid.UUID | str] | None = None
    delivery_kind: DeliveryKind = "terminal_result"
    metadata: dict[str, Any] | None = None
    prompt_item_id: uuid.UUID | str | None = None


@dataclass(frozen=True, slots=True)
class ClaimedChannelDelivery:
    id: uuid.UUID
    tenant_id: uuid.UUID
    runtime_task_id: uuid.UUID
    agent_id: uuid.UUID
    session_id: uuid.UUID
    user_id: uuid.UUID | None
    external_principal_id: uuid.UUID | None
    channel_config_id: uuid.UUID | None
    channel: str
    delivery_target: dict[str, Any]
    text: str
    artifact_ids: tuple[uuid.UUID, ...]
    delivery_receipts: dict[str, Any]
    terminal_status: str
    delivery_kind: DeliveryKind
    metadata: dict[str, Any]
    attempt_count: int


def channel_delivery_id(intent: ChannelDeliveryIntent) -> uuid.UUID:
    _target, _channel, target_hash = _target_snapshot(intent.delivery_target)
    identity = "|".join(
        (
            str(_uuid(intent.tenant_id, field="tenant_id")),
            str(_uuid(intent.runtime_task_id, field="runtime_task_id")),
            str(intent.delivery_kind),
            target_hash,
        )
    )
    if intent.prompt_item_id is not None:
        identity += "|" + str(_uuid(intent.prompt_item_id, field="prompt_item_id"))
    return uuid.uuid5(_DELIVERY_NAMESPACE, identity)


def _intent_values(intent: ChannelDeliveryIntent) -> dict[str, Any]:
    target, channel, target_hash = _target_snapshot(intent.delivery_target)
    text_content = str(intent.text or "").strip()
    terminal_status = str(intent.terminal_status or "").strip().lower()
    if not text_content:
        raise ValueError("channel delivery text is required")
    if not terminal_status:
        raise ValueError("terminal_status is required")
    if intent.delivery_kind not in {"terminal_result", "interactive_prompt"}:
        raise ValueError(f"unsupported delivery_kind: {intent.delivery_kind}")
    if intent.prompt_item_id is not None and intent.delivery_kind != "interactive_prompt":
        raise ValueError("prompt_item_id requires an interactive prompt")
    artifact_ids = [str(_uuid(value, field="artifact_id")) for value in (intent.artifact_ids or [])]
    return {
        "id": channel_delivery_id(intent),
        "tenant_id": _uuid(intent.tenant_id, field="tenant_id"),
        "runtime_task_id": _uuid(intent.runtime_task_id, field="runtime_task_id"),
        "agent_id": _uuid(intent.agent_id, field="agent_id"),
        "session_id": _uuid(intent.session_id, field="session_id"),
        "user_id": _uuid(intent.user_id, field="user_id", optional=True),
        "external_principal_id": _uuid(
            intent.external_principal_id,
            field="external_principal_id",
            optional=True,
        ),
        "channel_config_id": _uuid(intent.channel_config_id, field="channel_config_id", optional=True),
        "channel": channel,
        "target_hash": target_hash,
        "delivery_kind": intent.delivery_kind,
        "prompt_key": str(_uuid(intent.prompt_item_id, field="prompt_item_id")) if intent.prompt_item_id else "",
        "terminal_status": terminal_status,
        "delivery_target_json": target,
        "text_content": text_content,
        "artifact_ids_json": artifact_ids,
        "delivery_receipts_json": {},
        "metadata_json": dict(intent.metadata or {}),
        "status": "pending",
        "attempt_count": 0,
        "available_at": datetime.now(UTC),
    }


async def enqueue_channel_delivery(db: AsyncSession, intent: ChannelDeliveryIntent) -> uuid.UUID:
    """Atomically persist an immutable terminal delivery intent.

    Repeated finalization is a no-op. A caller may not mutate a previously
    committed terminal payload by replaying the same run and target.
    """

    provenance = await load_transcript_knowledge_provenance(
        db,
        tenant_id=_uuid(intent.tenant_id, field="tenant_id"),
        agent_id=_uuid(intent.agent_id, field="agent_id"),
        session_id=_uuid(intent.session_id, field="session_id"),
        run_id=_uuid(intent.runtime_task_id, field="runtime_task_id"),
    )
    if provenance is not None:
        intent = replace(
            intent,
            metadata=apply_inherited_knowledge_provenance(intent.metadata, provenance),
        )
    values = _intent_values(intent)
    stmt = (
        insert(ChannelDeliveryOutbox)
        .values(**values)
        .on_conflict_do_nothing(constraint="uq_channel_delivery_outbox_runtime_target")
    )
    await db.execute(stmt)
    return values["id"]


async def enqueue_terminal_delivery_for_task(
    db: AsyncSession,
    *,
    task: Any,
    content: str,
    terminal_status: str,
    artifact_parts: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
    agent_id: uuid.UUID | str | None = None,
    session_id: uuid.UUID | str | None = None,
    user_id: uuid.UUID | str | None = None,
    external_principal_id: uuid.UUID | str | None = None,
    delivery_kind: DeliveryKind = "terminal_result",
    prompt_item_id: uuid.UUID | str | None = None,
) -> uuid.UUID | None:
    """Project a canonical result or prompt into an immutable external delivery intent.

    ``ChatSession.delivery_target_json`` is the transport authority.  Runtime
    source labels are intentionally not used as a gate because legacy sessions
    may still say ``web`` even though they have a valid external target.
    """

    session_id = _uuid(session_id or getattr(task, "parent_session_id", None), field="parent_session_id", optional=True)
    agent_id = _uuid(agent_id or getattr(task, "parent_agent_id", None), field="parent_agent_id", optional=True)
    tenant_id = _uuid(getattr(task, "tenant_id", None), field="tenant_id", optional=True)
    if session_id is None or agent_id is None or tenant_id is None:
        return None
    session = (
        await db.execute(
            select(ChatSession).where(
                ChatSession.id == session_id,
                ChatSession.agent_id == agent_id,
                ChatSession.tenant_id == tenant_id,
            )
        )
    ).scalar_one_or_none()
    target = dict(getattr(session, "delivery_target_json", None) or {}) if session is not None else {}
    normalized_target = ChannelDeliveryService.normalize_reply_target(target)
    if not normalized_target or str(normalized_target.get("channel") or "").lower() == "web":
        return None

    text_content = str(content or "")
    if not text_content.strip():
        return None
    channel = str(normalized_target["channel"])
    channel_config = await ChannelDeliveryService._load_target_config(db, agent_id, channel, normalized_target)
    artifact_ids = [
        _uuid(part.get("artifact_id"), field="artifact_id")
        for part in (artifact_parts or [])
        if isinstance(part, dict) and part.get("artifact_id")
    ]
    if not artifact_ids and delivery_kind == "terminal_result":
        artifact_ids = list(
            (
                await db.execute(
                    select(ChatArtifact.id).where(
                        ChatArtifact.tenant_id == tenant_id,
                        ChatArtifact.agent_id == agent_id,
                        ChatArtifact.session_id == session_id,
                        ChatArtifact.runtime_task_id == task.id,
                    )
                )
            )
            .scalars()
            .all()
        )
    task_metadata = dict(getattr(task, "metadata_json", None) or {})
    resolved_external_principal_id = None if getattr(session, "audience_kind", None) == "group" else (
        external_principal_id
        or task_metadata.get("external_principal_id")
        or getattr(session, "external_principal_id", None)
        or normalized_target.get("external_principal_id")
    )
    resolved_user_id = user_id or getattr(task, "root_user_id", None) or getattr(session, "user_id", None)
    delivery_metadata = dict(metadata or {})
    delivery_metadata.setdefault("source", "canonical_terminal_outcome")
    return await enqueue_channel_delivery(
        db,
        ChannelDeliveryIntent(
            tenant_id=tenant_id,
            runtime_task_id=task.id,
            agent_id=agent_id,
            session_id=session_id,
            user_id=resolved_user_id,
            external_principal_id=resolved_external_principal_id,
            channel_config_id=getattr(channel_config, "id", None),
            delivery_target=normalized_target,
            text=text_content,
            artifact_ids=[value for value in artifact_ids if value is not None],
            terminal_status=terminal_status,
            delivery_kind=delivery_kind,
            prompt_item_id=prompt_item_id,
            metadata=delivery_metadata,
        ),
    )


def _claimed(row: ChannelDeliveryOutbox) -> ClaimedChannelDelivery:
    return ClaimedChannelDelivery(
        id=row.id,
        tenant_id=row.tenant_id,
        runtime_task_id=row.runtime_task_id,
        agent_id=row.agent_id,
        session_id=row.session_id,
        user_id=row.user_id,
        external_principal_id=row.external_principal_id,
        channel_config_id=row.channel_config_id,
        channel=row.channel,
        delivery_target=dict(row.delivery_target_json or {}),
        text=row.text_content,
        artifact_ids=tuple(uuid.UUID(str(value)) for value in (row.artifact_ids_json or [])),
        delivery_receipts=dict(row.delivery_receipts_json or {}),
        terminal_status=row.terminal_status,
        delivery_kind=row.delivery_kind,  # type: ignore[arg-type]
        metadata=dict(row.metadata_json or {}),
        attempt_count=int(row.attempt_count or 0),
    )


async def _default_text_sender(**kwargs: Any) -> DeliveryResult:
    idempotency_key = str(kwargs.pop("idempotency_key"))
    extra_detail = dict(kwargs.pop("extra_detail", {}) or {})
    extra_detail["idempotency_key"] = idempotency_key
    return await ChannelDeliveryService.send_text(extra_detail=extra_detail, **kwargs)


async def _default_file_sender(**kwargs: Any) -> DeliveryResult:
    idempotency_key = str(kwargs.pop("idempotency_key"))
    kwargs.pop("artifact_id", None)
    extra_detail = dict(kwargs.pop("extra_detail", {}) or {})
    extra_detail["idempotency_key"] = idempotency_key
    return await ChannelDeliveryService.send_file(extra_detail=extra_detail, **kwargs)


class ChannelDeliveryOutboxService:
    """Claim, deliver, retry, reconcile, and manually resend channel output."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        lease_seconds: int = 60,
        retry_base_seconds: int = 2,
        max_attempts: int = 8,
        text_sender: TextSender | None = None,
        file_sender: FileSender | None = None,
    ) -> None:
        self._session_factory = session_factory or async_session
        self._lease_seconds = max(1, int(lease_seconds))
        self._retry_base_seconds = max(0, int(retry_base_seconds))
        self._max_attempts = max(1, int(max_attempts))
        self._text_sender = text_sender or _default_text_sender
        self._file_sender = file_sender or _default_file_sender

    @contextlib.asynccontextmanager
    async def _worker_session(self, operation: str):
        async with self._session_factory() as db:
            async with enter_rls_bypass(db, reason=f"channel_delivery_outbox.{operation}") as bypass_db:
                yield bypass_db

    async def claim_batch(
        self,
        *,
        worker_id: str,
        now: datetime | None = None,
        limit: int = 20,
    ) -> list[ClaimedChannelDelivery]:
        current = now or datetime.now(UTC)
        expired = current - timedelta(seconds=self._lease_seconds)
        async with self._worker_session("claim") as db:
            rows = list(
                (
                    await db.execute(
                        select(ChannelDeliveryOutbox)
                        .where(
                            or_(
                                and_(
                                    ChannelDeliveryOutbox.status == "pending",
                                    ChannelDeliveryOutbox.available_at <= current,
                                ),
                                and_(
                                    ChannelDeliveryOutbox.status == "processing",
                                    ChannelDeliveryOutbox.locked_at <= expired,
                                ),
                            )
                        )
                        .order_by(ChannelDeliveryOutbox.available_at, ChannelDeliveryOutbox.created_at)
                        .limit(max(1, int(limit)))
                        .with_for_update(skip_locked=True)
                    )
                )
                .scalars()
                .all()
            )
            for row in rows:
                row.status = "processing"
                row.locked_by = str(worker_id)
                row.locked_at = current
                row.attempt_count = int(row.attempt_count or 0) + 1
            await db.commit()
            return [_claimed(row) for row in rows]

    @staticmethod
    def _has_ambiguous_part(item: ClaimedChannelDelivery) -> bool:
        receipts = item.delivery_receipts
        text_receipt = receipts.get("text") if isinstance(receipts, dict) else None
        if isinstance(text_receipt, dict) and text_receipt.get("state") in {"sending", "unknown"}:
            return True
        artifacts = receipts.get("artifacts") if isinstance(receipts, dict) else None
        return isinstance(artifacts, dict) and any(
            isinstance(value, dict) and value.get("state") in {"sending", "unknown"} for value in artifacts.values()
        )

    async def _update_part_receipt(
        self,
        *,
        item: ClaimedChannelDelivery,
        worker_id: str,
        part_key: str,
        receipt: dict[str, Any],
    ) -> None:
        async with self._worker_session("part_receipt") as db:
            row = (
                await db.execute(
                    select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.id == item.id).with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.status != "processing" or row.locked_by != worker_id:
                raise ChannelDeliveryAmbiguousError("delivery lease was lost while committing provider receipt")
            receipts = dict(row.delivery_receipts_json or {})
            if part_key == "text":
                receipts["text"] = receipt
            else:
                artifacts = dict(receipts.get("artifacts") or {})
                artifacts[part_key] = receipt
                receipts["artifacts"] = artifacts
            row.delivery_receipts_json = receipts
            await db.commit()

    async def _load_delivery_authority_and_artifacts(
        self,
        item: ClaimedChannelDelivery,
    ) -> list[tuple[uuid.UUID, Path, str]]:
        async with tenant_scoped_session(
            item.tenant_id,
            session_factory=self._session_factory,
            require_tenant=True,
            source="channel_delivery_outbox_authority",
        ) as db:
            config = None
            tenant_installation = bool(item.delivery_target.get("tenant_channel_config_id"))
            if tenant_installation:
                from app.services.channel_installation import load_tenant_channel_for_target

                config = await load_tenant_channel_for_target(db, agent_id=item.agent_id, channel=item.channel,
                    target=item.delivery_target, expected_config_id=item.channel_config_id)
                if config is None:
                    raise ChannelDeliveryPermanentError("enterprise channel installation or identity changed")
            elif item.channel_config_id is not None:
                config = (
                    await db.execute(
                        select(ChannelConfig).where(
                            ChannelConfig.id == item.channel_config_id,
                            ChannelConfig.agent_id == item.agent_id,
                            ChannelConfig.tenant_id == item.tenant_id,
                            ChannelConfig.channel_type == item.channel,
                        )
                    )
                ).scalar_one_or_none()
                if config is None:
                    raise ChannelDeliveryPermanentError("channel target configuration was revoked or replaced")
            # is_configured is the durable send authority; is_connected is a
            # transient connectivity/identity observation and normal channel
            # setup leaves it false, so it must not dead-letter delivery.
            if config is not None and not config.is_configured:
                raise ChannelDeliveryPermanentError("channel target configuration is no longer active")
            if item.external_principal_id is not None:
                principal = (
                    await db.execute(
                        select(ExternalPrincipal).where(
                            ExternalPrincipal.id == item.external_principal_id,
                            ExternalPrincipal.tenant_id == item.tenant_id,
                        )
                    )
                ).scalar_one_or_none()
                if principal is None or principal.status != "active":
                    raise ChannelDeliveryPermanentError("external channel target was revoked")
                config_id = principal.tenant_channel_config_id if tenant_installation else principal.channel_config_id
                if item.channel_config_id is not None and config_id != item.channel_config_id:
                    raise ChannelDeliveryPermanentError("external channel target installation changed")
                if (item.metadata.get("source") == "employee_first_task_question"
                    and principal.linked_user_id != item.user_id):
                    raise ChannelDeliveryPermanentError("first-task requester channel binding changed")

            session = await db.scalar(select(ChatSession).where(ChatSession.id == item.session_id,
                ChatSession.tenant_id == item.tenant_id, ChatSession.agent_id == item.agent_id))
            if (session is not None and session.audience_kind != "group" and item.external_principal_id is not None
                and item.user_id is not None and (session.user_id != item.user_id or principal.linked_user_id != item.user_id)):
                raise ChannelDeliveryPermanentError("private channel requester binding changed")
            from app.services.remote_a2a_channel import validate_delivery

            await validate_delivery(db, item=item)
            if session is not None and session.audience_kind == "group":
                from app.services.shared_group_authority import validate_shared_group_delivery

                await validate_shared_group_delivery(db, item=item, session=session)
            if not item.artifact_ids:
                return []
            artifacts = list(
                (
                    await db.execute(
                        select(ChatArtifact).where(
                            ChatArtifact.id.in_(item.artifact_ids),
                            ChatArtifact.tenant_id == item.tenant_id,
                            ChatArtifact.agent_id == item.agent_id,
                            ChatArtifact.session_id == item.session_id,
                            ChatArtifact.runtime_task_id == item.runtime_task_id,
                        )
                    )
                )
                .scalars()
                .all()
            )
            by_id = {artifact.id: artifact for artifact in artifacts}
            resolved: list[tuple[uuid.UUID, Path, str]] = []
            workspace_root = (Path(get_settings().AGENT_DATA_DIR) / str(item.agent_id)).resolve()
            for artifact_id in item.artifact_ids:
                artifact = by_id.get(artifact_id)
                if artifact is None:
                    raise ChannelDeliveryPermanentError(f"artifact {artifact_id} no longer resolves")
                storage = str((artifact.snapshot_json or {}).get("snapshot_storage_path") or "").strip()
                storage_path = Path(storage)
                if not storage or storage_path.is_absolute() or ".." in storage_path.parts:
                    raise ChannelDeliveryPermanentError(f"artifact {artifact_id} has an unsafe snapshot path")
                absolute = (workspace_root / storage_path).resolve()
                try:
                    absolute.relative_to(workspace_root)
                except ValueError as exc:
                    raise ChannelDeliveryPermanentError(
                        f"artifact {artifact_id} snapshot escaped its workspace"
                    ) from exc
                if not absolute.is_file():
                    raise ChannelDeliveryPermanentError(f"artifact {artifact_id} snapshot is unavailable")
                resolved.append((artifact_id, absolute, Path(artifact.path).name))
            return resolved

    async def _private_delivery_reader(self, db: AsyncSession, item: ClaimedChannelDelivery, session: ChatSession):
        from fastapi import HTTPException
        from app.services.external_principal_service import load_external_runtime_actor
        from app.services.runtime_user_identity import load_scoped_runtime_user

        user = await load_scoped_runtime_user(db, item.user_id, tenant_id=item.tenant_id) if item.user_id else None
        if item.user_id is not None and (user is None or session.user_id != item.user_id):
            raise HTTPException(403, detail={"code": "artifact_delivery_reader_unavailable"})
        if item.external_principal_id is not None:
            actor = await load_external_runtime_actor(db, tenant_id=item.tenant_id,
                principal_id=item.external_principal_id, expected_user_id=item.user_id)
            if item.user_id is not None and (not actor.authority_bound or actor.id != item.user_id):
                raise HTTPException(403, detail={"code": "artifact_delivery_binding_changed"})
        return user

    async def _authorize_native_result_text(self, db: AsyncSession, item: ClaimedChannelDelivery) -> None:
        from app.core.source_read_purpose import source_recipient_read
        from app.models.session_v2 import SessionModelResult, SessionRunOutcome
        from app.services.content_input_receipts import ContentInputReceiptError, _validate_input_dependencies
        from app.services.session_model_inputs import _model_dependencies

        if item.metadata.get("source") not in {"session_terminal_outcome", "session_terminal_outcome_recovery"}:
            return
        session = await db.get(ChatSession, item.session_id, populate_existing=True)
        if session is None or session.tenant_id != item.tenant_id or session.agent_id != item.agent_id:
            raise ContentInputReceiptError("native_result_source_unavailable", "The original work is unavailable.", 503)
        if session.audience_kind == "group":
            return  # The current group recipient/source closure has its own release gate.
        try:
            outcome_id = uuid.UUID(str(item.metadata.get("outcome_id")))
        except (ValueError, TypeError, AttributeError) as exc:
            raise ContentInputReceiptError("native_result_source_unavailable", "The result reference is unavailable.", 503) from exc
        row = (await db.execute(select(SessionRunOutcome, SessionModelResult).join(SessionModelResult,
            SessionModelResult.id == SessionRunOutcome.terminal_result_id).where(
                SessionRunOutcome.id == outcome_id, SessionRunOutcome.tenant_id == item.tenant_id,
                SessionRunOutcome.session_id == item.session_id, SessionRunOutcome.run_id == item.runtime_task_id,
                SessionRunOutcome.state == "terminal_committed", SessionModelResult.tenant_id == item.tenant_id,
                SessionModelResult.session_id == item.session_id, SessionModelResult.run_id == item.runtime_task_id,
                SessionModelResult.state == "round_committed").execution_options(populate_existing=True))).first()
        if row is None:
            raise ContentInputReceiptError("native_result_source_unavailable", "The committed result is unavailable.", 503)
        _outcome, result = row
        if (item.text != str((result.seal_json or {}).get("semantic_content") or "").strip()
            or (item.metadata.get("terminal_result_id") is not None
                and str(result.id) != str(item.metadata["terminal_result_id"]))):
            raise ContentInputReceiptError("native_result_source_changed", "The result no longer matches its delivery.", 403)
        dependencies, complete = _model_dependencies(result)
        if not complete:
            raise ContentInputReceiptError("native_result_sources_unavailable", "The result lacks its complete source receipt.", 503)
        user = await self._private_delivery_reader(db, item, session)
        if user is None:
            await _validate_input_dependencies(db, item.tenant_id, dependencies, user=None)
        else:
            with source_recipient_read(session_id=session.id, user_id=user.id):
                await _validate_input_dependencies(db, item.tenant_id, dependencies, user=user)

    async def _authorize_artifact_part(self, db: AsyncSession, item: ClaimedChannelDelivery,
        artifact_id: uuid.UUID) -> ChatArtifact:
        from fastapi import HTTPException
        from app.core.resource_authority import authorize_resource_action
        from app.core.source_read_purpose import source_recipient_read
        from app.services.content_input_receipts import validate_artifact_content_receipt

        artifact = await db.scalar(select(ChatArtifact).where(ChatArtifact.id == artifact_id,
            ChatArtifact.tenant_id == item.tenant_id, ChatArtifact.agent_id == item.agent_id,
            ChatArtifact.session_id == item.session_id, ChatArtifact.runtime_task_id == item.runtime_task_id)
            .execution_options(populate_existing=True))
        session = await db.get(ChatSession, item.session_id, populate_existing=True)
        if (artifact is None or session is None or session.tenant_id != item.tenant_id
            or session.agent_id != item.agent_id or artifact_id not in item.artifact_ids):
            raise HTTPException(404, detail={"code": "artifact_delivery_authority_unavailable"})
        if session.audience_kind == "group":
            from app.services.shared_group_authority import load_current_group_recipient_users

            _config, recipients = await load_current_group_recipient_users(db, session=session)
            for user in recipients:
                with source_recipient_read(session_id=session.id, user_id=user.id):
                    await validate_artifact_content_receipt(db, item.tenant_id, user, artifact)
        else:
            user = await self._private_delivery_reader(db, item, session)
            if user is not None:
                with source_recipient_read(session_id=session.id, user_id=user.id):
                    await authorize_resource_action(db, user, agent_id=item.agent_id, resource_kind="chat_artifact",
                        resource_id=artifact.id, action="deliver", owner_user_id=artifact.owner_user_id,
                        root_session_id=artifact.root_session_id or artifact.session_id,
                        authority_state=artifact.authority_state)
            else:
                # An unbound recipient cannot borrow a later linked User's grants.
                # Only a verified source closure with no private reader requirement can pass.
                await validate_artifact_content_receipt(db, item.tenant_id, None, artifact)
        return artifact

    async def _send_part(
        self,
        *,
        item: ClaimedChannelDelivery,
        worker_id: str,
        part_key: str,
        sender: Callable[..., Awaitable[DeliveryResult]],
        sender_kwargs: dict[str, Any],
    ) -> None:
        current = (
            item.delivery_receipts.get("text")
            if part_key == "text"
            else ((item.delivery_receipts.get("artifacts") or {}).get(part_key))
        )
        if isinstance(current, dict) and current.get("state") == "delivered":
            return
        idempotency_key = f"{item.id}:{part_key}"
        with contextlib.ExitStack() as captured_files:
            if part_key == "text":
                from fastapi import HTTPException
                from app.services.content_input_receipts import ContentInputReceiptError

                try:
                    await self._authorize_native_result_text(sender_kwargs["db"], item)
                except (HTTPException, ContentInputReceiptError) as exc:
                    detail = getattr(exc, "detail", None)
                    code = getattr(exc, "code", None) or (detail.get("code") if isinstance(detail, dict) else None)
                    error_type = (ChannelDeliveryRetryableError if exc.status_code >= 500 else ChannelDeliveryPermanentError)
                    raise error_type(f"native_result_source_unavailable:{code or 'source_access_denied'}") from exc
                except Exception as exc:
                    raise ChannelDeliveryRetryableError(f"native_result_authority_unavailable:{type(exc).__name__}") from exc
                from app.services.remote_a2a_channel import validate_delivery

                await validate_delivery(sender_kwargs["db"], item=item)
            else:
                from fastapi import HTTPException
                from app.services.chat_artifact_delivery import captured_chat_artifact_file
                from app.services.content_input_receipts import ContentInputReceiptError
                from app.services.workspace_resource_authority import WorkspaceAuthorityError

                try:
                    artifact = await self._authorize_artifact_part(sender_kwargs["db"], item, uuid.UUID(part_key))
                    root = Path(get_settings().AGENT_DATA_DIR) / str(item.agent_id)
                    captured, _source = captured_files.enter_context(captured_chat_artifact_file(artifact, root))
                    if captured is None:
                        raise ChannelDeliveryPermanentError("artifact_snapshot_unavailable")
                    sender_kwargs = {**sender_kwargs, "file_path": captured}
                except (HTTPException, ContentInputReceiptError, WorkspaceAuthorityError, PermissionError, LookupError) as exc:
                    detail = getattr(exc, "detail", None)
                    code = getattr(exc, "code", None) or (detail.get("code") if isinstance(detail, dict) else None)
                    message = f"artifact_source_unavailable:{code or 'artifact_access_denied'}"
                    error_type = (ChannelDeliveryRetryableError if getattr(exc, "status_code", 403) >= 500
                        else ChannelDeliveryPermanentError)
                    raise error_type(message) from exc
                except (ChannelDeliveryPermanentError, ChannelDeliveryRetryableError):
                    raise
                except Exception as exc:
                    # No provider call has started; this read failure is safe to retry.
                    raise ChannelDeliveryRetryableError(f"artifact_authority_unavailable:{type(exc).__name__}") from exc

            await self._update_part_receipt(
                item=item,
                worker_id=worker_id,
                part_key=part_key,
                receipt={
                    "state": "sending",
                    "idempotency_key": idempotency_key,
                    "started_at": datetime.now(UTC).isoformat(),
                },
            )
            result = await sender(idempotency_key=idempotency_key, **sender_kwargs)
            receipt = {
                "state": "delivered" if result.ok else "unknown" if result.status == "unknown" else "failed",
                "idempotency_key": idempotency_key,
                "provider_status": result.status,
                "message": result.message,
                "retryable": bool(result.retryable),
                "detail": dict(result.detail or {}),
                "recorded_at": datetime.now(UTC).isoformat(),
            }
            await self._update_part_receipt(
                item=item,
                worker_id=worker_id,
                part_key=part_key,
                receipt=receipt,
            )
            if result.ok:
                return
            error = f"{item.channel} {part_key} delivery failed: {result.message}"
            if result.status == "unknown":
                raise ChannelDeliveryAmbiguousError(error)
            if result.retryable:
                raise ChannelDeliveryRetryableError(error)
            raise ChannelDeliveryPermanentError(error)

    async def _deliver(self, item: ClaimedChannelDelivery, *, worker_id: str) -> dict[str, Any]:
        if self._has_ambiguous_part(item):
            raise ChannelDeliveryAmbiguousError("a provider call has no committed receipt; automatic replay is unsafe")
        artifact_paths = await self._load_delivery_authority_and_artifacts(item)
        text = item.text
        smartbot = item.channel == 'wecom' and item.delivery_target.get('wecom_transport') == 'smart_bot'
        if smartbot:
            from urllib.parse import urlsplit

            base = (get_settings().PUBLIC_BASE_URL or get_settings().BASE_URL).rstrip('/')
            parsed = urlsplit(base)
            if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
                raise ChannelDeliveryPermanentError('Smart Bot requires a configured HTTPS Web work URL')
            text += f'\n\n[在原工作中查看、答复或编辑成果]({base}/agents/{item.agent_id}/sessions/{item.session_id})'
        common = {
            "agent_id": item.agent_id,
            "reply_target": item.delivery_target,
            "delivery_mode": "terminal_outbox",
            "extra_detail": {
                "channel_delivery_outbox_id": str(item.id),
                "runtime_task_id": str(item.runtime_task_id),
                "knowledge_provenance": item.metadata.get("knowledge_provenance"),
            },
        }
        async with tenant_scoped_session(
            item.tenant_id,
            session_factory=self._session_factory,
            require_tenant=True,
            source="channel_delivery_outbox_provider_call",
        ) as db:
            await self._send_part(
                item=item,
                worker_id=worker_id,
                part_key="text",
                sender=self._text_sender,
                sender_kwargs={
                    "db": db,
                    "tenant_id": item.tenant_id,
                    "text": text,
                    "content_sensitivity": knowledge_content_sensitivity(item.metadata),
                    **common,
                },
            )
            if smartbot:
                return {'status': 'delivered', 'channel': item.channel, 'artifact_delivery': 'authenticated_work_link',
                    'artifact_ids': [str(value) for value in item.artifact_ids]}
            for artifact_id, file_path, file_name in artifact_paths:
                await self._send_part(
                    item=item,
                    worker_id=worker_id,
                    part_key=str(artifact_id),
                    sender=self._file_sender,
                    sender_kwargs={
                        "db": db,
                        "tenant_id": item.tenant_id,
                        "artifact_id": artifact_id,
                        "file_path": file_path,
                        "file_name": file_name,
                        "message": "",
                        **common,
                    },
                )
        return {
            "status": "delivered",
            "channel": item.channel,
            "text_idempotency_key": f"{item.id}:text",
            "artifact_count": len(artifact_paths),
        }

    async def _mark_delivered(self, *, item_id: uuid.UUID, worker_id: str, receipt: dict[str, Any]) -> bool:
        async with self._worker_session("ack") as db:
            row = (
                await db.execute(
                    select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.id == item_id).with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.status != "processing" or row.locked_by != worker_id:
                return False
            row.status = "delivered"
            row.delivered_at = datetime.now(UTC)
            row.last_error = None
            row.locked_by = None
            row.locked_at = None
            metadata = dict(row.metadata_json or {})
            metadata["terminal_delivery_receipt"] = receipt
            row.metadata_json = metadata
            await db.commit()
            return True

    async def _mark_failure(
        self,
        *,
        item: ClaimedChannelDelivery,
        worker_id: str,
        error: Exception,
    ) -> str:
        async with self._worker_session("failure") as db:
            row = (
                await db.execute(
                    select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.id == item.id).with_for_update()
                )
            ).scalar_one_or_none()
            if row is None or row.status != "processing" or row.locked_by != worker_id:
                return "stale"
            row.last_error = f"{type(error).__name__}: {error}"
            row.locked_by = None
            row.locked_at = None
            if isinstance(error, ChannelDeliveryAmbiguousError):
                row.status = "needs_reconciliation"
                outcome = "needs_reconciliation"
            elif isinstance(error, ChannelDeliveryPermanentError) or int(row.attempt_count or 0) >= self._max_attempts:
                row.status = "dead_letter"
                outcome = "dead_letter"
            else:
                delay = min(300, self._retry_base_seconds * (2 ** max(0, int(row.attempt_count or 1) - 1)))
                row.status = "pending"
                row.available_at = datetime.now(UTC) + timedelta(seconds=delay)
                outcome = "retry"
            await db.commit()
            return outcome

    async def drain_once(self, *, worker_id: str, limit: int = 20) -> dict[str, int]:
        claimed = await self.claim_batch(worker_id=worker_id, limit=limit)
        counts = {
            "claimed": len(claimed),
            "delivered": 0,
            "retried": 0,
            "dead_lettered": 0,
            "needs_reconciliation": 0,
        }
        for item in claimed:
            try:
                receipt = await self._deliver(item, worker_id=worker_id)
                if await self._mark_delivered(item_id=item.id, worker_id=worker_id, receipt=receipt):
                    counts["delivered"] += 1
            except Exception as exc:  # classified below; unknown provider errors are ambiguous.
                if not isinstance(
                    exc,
                    (ChannelDeliveryRetryableError, ChannelDeliveryPermanentError, ChannelDeliveryAmbiguousError),
                ):
                    exc = ChannelDeliveryAmbiguousError(f"{type(exc).__name__}: {exc}")
                outcome = await self._mark_failure(item=item, worker_id=worker_id, error=exc)
                if outcome == "retry":
                    counts["retried"] += 1
                elif outcome == "dead_letter":
                    counts["dead_lettered"] += 1
                elif outcome == "needs_reconciliation":
                    counts["needs_reconciliation"] += 1
        return counts

    async def request_manual_resend(
        self,
        *,
        tenant_id: uuid.UUID,
        outbox_id: uuid.UUID,
        actor_user_id: uuid.UUID,
        reason: str,
    ) -> None:
        normalized_reason = str(reason or "").strip()
        if not normalized_reason:
            raise ValueError("manual resend reason is required")
        async with tenant_scoped_session(
            tenant_id,
            session_factory=self._session_factory,
            require_tenant=True,
            source="channel_delivery_outbox_manual_resend",
        ) as db:
            row = (
                await db.execute(
                    select(ChannelDeliveryOutbox).where(ChannelDeliveryOutbox.id == outbox_id).with_for_update()
                )
            ).scalar_one_or_none()
            if row is None:
                raise LookupError("channel delivery outbox item not found")
            if row.status not in {"dead_letter", "needs_reconciliation"}:
                raise ValueError("only dead-letter or reconciliation deliveries can be resent")
            event = {
                "actor_user_id": str(actor_user_id),
                "reason": normalized_reason,
                "previous_status": row.status,
                "previous_error": row.last_error,
                "requested_at": datetime.now(UTC).isoformat(),
            }
            metadata = dict(row.metadata_json or {})
            metadata["manual_resends"] = [*list(metadata.get("manual_resends") or []), event]
            row.metadata_json = metadata
            row.delivery_receipts_json = {}
            row.status = "pending"
            row.attempt_count = 0
            row.available_at = datetime.now(UTC)
            row.locked_by = None
            row.locked_at = None
            row.last_error = None
            db.add(
                AuditLog(
                    tenant_id=tenant_id,
                    user_id=actor_user_id,
                    agent_id=row.agent_id,
                    action="channel_delivery_manual_resend",
                    details={"outbox_id": str(row.id), **event},
                )
            )
            await db.commit()


__all__ = [
    "ChannelDeliveryIntent",
    "ChannelDeliveryOutboxService",
    "ClaimedChannelDelivery",
    "channel_delivery_id",
    "enqueue_channel_delivery",
]
