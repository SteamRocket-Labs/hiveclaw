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
) -> uuid.UUID | None:
    """Project canonical terminal truth into one immutable external delivery intent.

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
    channel_config = (
        await db.execute(
            select(ChannelConfig).where(
                ChannelConfig.agent_id == agent_id,
                ChannelConfig.tenant_id == tenant_id,
                ChannelConfig.channel_type == channel,
            )
        )
    ).scalar_one_or_none()
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
    resolved_external_principal_id = (
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
        if isinstance(text_receipt, dict) and text_receipt.get("state") == "sending":
            return True
        artifacts = receipts.get("artifacts") if isinstance(receipts, dict) else None
        return isinstance(artifacts, dict) and any(
            isinstance(value, dict) and value.get("state") == "sending" for value in artifacts.values()
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
    ) -> list[tuple[uuid.UUID, Path]]:
        async with tenant_scoped_session(
            item.tenant_id,
            session_factory=self._session_factory,
            require_tenant=True,
            source="channel_delivery_outbox_authority",
        ) as db:
            config = None
            if item.channel_config_id is not None:
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
                if item.channel_config_id is not None and principal.channel_config_id != item.channel_config_id:
                    raise ChannelDeliveryPermanentError("external channel target installation changed")

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
            resolved: list[tuple[uuid.UUID, Path]] = []
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
                resolved.append((artifact_id, absolute))
            return resolved

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
            "state": "delivered" if result.ok else "failed",
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
        if result.retryable:
            raise ChannelDeliveryRetryableError(error)
        raise ChannelDeliveryPermanentError(error)

    async def _deliver(self, item: ClaimedChannelDelivery, *, worker_id: str) -> dict[str, Any]:
        if self._has_ambiguous_part(item):
            raise ChannelDeliveryAmbiguousError("a provider call has no committed receipt; automatic replay is unsafe")
        artifact_paths = await self._load_delivery_authority_and_artifacts(item)
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
                    "text": item.text,
                    "content_sensitivity": knowledge_content_sensitivity(item.metadata),
                    **common,
                },
            )
            for artifact_id, file_path in artifact_paths:
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
