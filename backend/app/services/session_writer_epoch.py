"""Database-backed writer generation allocation and cutover invariants.

Every new RuntimeTask reads the environment epoch in its creation transaction.
The immutable value is then enforced by PostgreSQL for all later mutations, so
rolling instances cannot become dual writers for one Run.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import UTC, datetime
import hashlib
import logging
import os
from pathlib import Path
import socket
from typing import Any, Iterable, Mapping, Sequence

from sqlalchemy import select

from app.models.session_v2 import SessionWriterEpoch, SessionWriterHeartbeat

logger = logging.getLogger(__name__)

SUPPORTED_SESSION_WRITER_GENERATIONS = (1, 2)

_SESSION_WRITER_SOURCE_PATHS = (
    "VERSION",
    "app/models/chat_transcript_event.py",
    "app/models/session_v2.py",
    "app/services/session_event_contract.py",
    "app/services/session_v2_persistence.py",
    "app/services/session_writer_epoch.py",
    "app/services/web_chat_run_orchestrator.py",
    "app/services/web_chat_runtime.py",
    "app/api/websocket.py",
)


class SessionWriterEpochError(RuntimeError):
    """Base error for an unavailable or invalid writer authority."""


class SessionWriterEpochUnavailable(SessionWriterEpochError):
    pass


class SessionWriterGenerationUnsupported(SessionWriterEpochError):
    pass


class SessionWriterEpochTransitionError(SessionWriterEpochError):
    pass


@dataclass(frozen=True, slots=True)
class SessionWriterEpochSnapshot:
    state: str
    new_run_generation: int
    allowed_existing_generations: tuple[int, ...]
    enforcement_mode: str
    version: int
    release_id: str | None


async def read_session_writer_epoch(db: Any, *, for_update: bool = False) -> SessionWriterEpochSnapshot:
    statement = select(SessionWriterEpoch).where(SessionWriterEpoch.id == "global")
    if for_update:
        statement = statement.with_for_update()
    result = await db.execute(statement)
    row = result.scalar_one_or_none()
    if row is None:
        raise SessionWriterEpochUnavailable("global Session writer epoch row is missing")
    allowed = tuple(sorted({int(value) for value in (row.allowed_existing_generations_json or [])}))
    return SessionWriterEpochSnapshot(
        state=str(row.state),
        new_run_generation=int(row.new_run_generation),
        allowed_existing_generations=allowed,
        enforcement_mode=str(row.enforcement_mode),
        version=int(row.version),
        release_id=str(row.release_id) if row.release_id else None,
    )


async def assign_runtime_task_writer_generation(
    db: Any,
    task: Any,
    *,
    supported_generations: Sequence[int] = (1, 2),
) -> SessionWriterEpochSnapshot:
    """Bind a new Run to the current DB epoch before it is flushed."""

    snapshot = await read_session_writer_epoch(db)
    supported = {int(value) for value in supported_generations}
    generation = snapshot.new_run_generation
    if generation not in supported:
        raise SessionWriterGenerationUnsupported(
            f"this runtime artifact does not support Session writer generation {generation}"
        )
    if generation not in set(snapshot.allowed_existing_generations):
        raise SessionWriterEpochUnavailable(
            f"Session writer epoch generation {generation} is not allowed for new or existing Runs"
        )
    task.writer_generation = generation
    return snapshot


async def upsert_session_writer_heartbeat(
    db: Any,
    *,
    service: str,
    instance_id: str,
    artifact_digest: str,
    supported_generations: Sequence[int] = (1, 2),
    now: datetime | None = None,
) -> SessionWriterHeartbeat:
    """Publish one live artifact capability receipt using a stable instance key."""

    normalized_supported = sorted({int(value) for value in supported_generations})
    if not normalized_supported:
        raise SessionWriterGenerationUnsupported("writer heartbeat must advertise at least one generation")
    statement = (
        select(SessionWriterHeartbeat)
        .where(
            SessionWriterHeartbeat.service == str(service),
            SessionWriterHeartbeat.instance_id == str(instance_id),
        )
        .with_for_update()
    )
    row = (await db.execute(statement)).scalar_one_or_none()
    if row is None:
        row = SessionWriterHeartbeat(
            service=str(service),
            instance_id=str(instance_id),
            artifact_digest=str(artifact_digest),
            supported_generations_json=normalized_supported,
        )
        db.add(row)
    else:
        row.artifact_digest = str(artifact_digest)
        row.supported_generations_json = normalized_supported
    row.last_seen_at = now or datetime.now(UTC)
    await db.flush()
    return row


def _session_writer_source_digest() -> str:
    """Hash the Session writer source shared by every Railway service artifact."""

    backend_root = Path(__file__).resolve().parents[2]
    digest = hashlib.sha256()
    for relative_path in _SESSION_WRITER_SOURCE_PATHS:
        path = backend_root / relative_path
        digest.update(relative_path.encode("utf-8"))
        digest.update(b"\0")
        try:
            digest.update(path.read_bytes())
        except OSError as exc:
            # A missing source file must produce a distinct, deterministic
            # identity, never silently impersonate the complete artifact.
            digest.update(f"unreadable:{type(exc).__name__}".encode("utf-8"))
        digest.update(b"\0")
    return "sha256:" + digest.hexdigest()


def session_writer_artifact_identity() -> tuple[str, str, str]:
    """Return stable service/instance/artifact evidence without secret data."""

    from app.config import get_settings

    settings = get_settings()
    service = str(os.getenv("RAILWAY_SERVICE_NAME") or settings.HIVE_PROCESS_ROLE or "unknown")
    instance_id = str(
        os.getenv("RAILWAY_REPLICA_ID") or os.getenv("HOSTNAME") or f"{socket.gethostname()}:{os.getpid()}"
    )
    supplied_digest = str(os.getenv("HIVE_ARTIFACT_DIGEST") or "").strip()
    git_revision = str(os.getenv("RAILWAY_GIT_COMMIT_SHA") or "").strip()
    if supplied_digest.startswith("sha256:") and len(supplied_digest) == 71:
        digest = supplied_digest
    elif supplied_digest or git_revision:
        release_material = supplied_digest or f"git:{git_revision}"
        digest = "sha256:" + hashlib.sha256(release_material.encode("utf-8")).hexdigest()
    else:
        digest = _session_writer_source_digest()
    return service, instance_id, digest


async def start_session_writer_heartbeat_loop(*, interval_seconds: float | None = None) -> None:
    """Continuously publish live writer support for safe epoch transitions."""

    from app.database import async_session

    raw_interval = interval_seconds
    if raw_interval is None:
        try:
            raw_interval = float(os.getenv("SESSION_WRITER_HEARTBEAT_SECONDS", "15"))
        except ValueError:
            raw_interval = 15.0
    interval = max(1.0, float(raw_interval))
    service, instance_id, artifact_digest = session_writer_artifact_identity()
    while True:
        try:
            async with async_session() as db:
                await upsert_session_writer_heartbeat(
                    db,
                    service=service,
                    instance_id=instance_id,
                    artifact_digest=artifact_digest,
                    supported_generations=SUPPORTED_SESSION_WRITER_GENERATIONS,
                )
                await db.commit()
        except asyncio.CancelledError:
            raise
        except Exception:
            # Compatibility artifacts may briefly run before schema expand.
            # Missing heartbeat prevents cutover; it must not take the serving
            # process down or pretend the instance supports an epoch.
            logger.exception(
                "Session writer heartbeat failed service=%s instance=%s",
                service,
                instance_id,
            )
        await asyncio.sleep(interval)


def validate_writer_epoch_transition(
    *,
    current_state: str,
    target_state: str,
    new_run_generation: int,
    allowed_existing_generations: Iterable[int],
    active_runs_by_generation: Mapping[int, int],
    live_supported_generations: Iterable[Iterable[int]],
) -> None:
    """Pure fail-closed validation used by ops/API transition transactions."""

    allowed = {int(value) for value in allowed_existing_generations}
    new_generation = int(new_run_generation)
    if current_state not in {"legacy_open", "v1_draining", "v2_only"}:
        raise SessionWriterEpochTransitionError(f"unknown current writer epoch state: {current_state}")
    if target_state not in {"legacy_open", "v1_draining", "v2_only"}:
        raise SessionWriterEpochTransitionError(f"unknown target writer epoch state: {target_state}")
    if new_generation not in allowed:
        raise SessionWriterEpochTransitionError("new_run_generation must remain in allowed_existing_generations")
    if current_state == "v2_only" and target_state == "legacy_open":
        raise SessionWriterEpochTransitionError("V2-only rollback cannot reopen the legacy writer authority")

    heartbeat_sets = [{int(value) for value in values} for values in live_supported_generations]
    if not heartbeat_sets:
        raise SessionWriterEpochTransitionError("no live writer heartbeat evidence is available")
    if any(new_generation not in supported for supported in heartbeat_sets):
        raise SessionWriterEpochTransitionError(
            f"a live runtime artifact does not support new Run generation {new_generation}"
        )

    if target_state == "v1_draining":
        if new_generation != 2 or not {1, 2}.issubset(allowed):
            raise SessionWriterEpochTransitionError(
                "v1_draining requires new generation 2 while existing generations 1 and 2 remain allowed"
            )
    if target_state == "v2_only":
        active_generation_one = int(active_runs_by_generation.get(1, 0) or 0)
        if active_generation_one:
            raise SessionWriterEpochTransitionError(
                f"cannot enter v2_only while {active_generation_one} generation 1 active runs remain"
            )
        if new_generation != 2 or allowed != {2}:
            raise SessionWriterEpochTransitionError("v2_only requires new generation 2 and allowed set {2}")
