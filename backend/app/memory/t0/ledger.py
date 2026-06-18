"""Append-only T0 session ledger.

This is Hive's raw conversation substrate. It follows the same ground rule as
Claude Code transcripts and Codex rollouts: accepted session events are appended
to a per-session ledger first, then higher layers may build summaries or indexes
from it. T0 is not a summary store and does not rewrite historical events.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import uuid
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as xml_escape
from xml.sax.saxutils import quoteattr

from app.config import get_settings
from app.memory.form_lint import lint_memory_form
from app.services.privacy_layer import PrivacyLayer, PrivacyStore


SCHEMA_VERSION = "t0.session-ledger.v1"
SOURCE_FILENAME = "source.md"
INDEX_FILENAME = "index.json"
_EVENT_BLOCK_RE = re.compile(r"<t0_event\b.*?</t0_event>", re.DOTALL)


@dataclass(frozen=True, slots=True)
class T0AppendResult:
    path: Path
    segment_id: str
    event_id: str
    sequence: int


@dataclass(frozen=True, slots=True)
class T0SealResult:
    path: Path
    segment_id: str
    sequence: int


@dataclass(frozen=True, slots=True)
class LegacyImportResult:
    path: Path
    segment_id: str
    sequence: int
    imported: bool


@dataclass(frozen=True, slots=True)
class T0SessionEvent:
    event_id: str
    sequence: int
    event_type: str
    role: str | None
    content: str
    created_at: str
    message_id: str | None
    actor_id: str | None
    runtime_task_id: str | None
    source: str
    sensitivity: str
    metadata: dict[str, Any]
    path: Path
    segment_id: str


def append_t0_session_event(
    *,
    agent_id: uuid.UUID | str,
    session_id: uuid.UUID | str,
    event_type: str,
    role: str | None = None,
    content: Any = "",
    message_id: uuid.UUID | str | None = None,
    actor_id: uuid.UUID | str | None = None,
    tenant_id: uuid.UUID | str | None = None,
    runtime_task_id: uuid.UUID | str | None = None,
    source: str = "runtime",
    metadata: dict[str, Any] | None = None,
    created_at: datetime | None = None,
    data_root: Path | str | None = None,
) -> T0AppendResult:
    """Append one raw session event to the active T0 segment."""

    now = _utc_now(created_at)
    session_dir = _session_dir(data_root, agent_id, session_id)
    index = _load_or_create_index(session_dir, agent_id=agent_id, session_id=session_id, now=now)
    segment = _ensure_open_segment(index, now=now)
    sequence = int(index.get("next_sequence") or 1)
    path = session_dir / segment["path"]
    sanitized_content, sensitivity, form_warnings = _sanitize_t0_content(content)
    event_id = _new_event_id()
    event_metadata = _clean_metadata(metadata)
    if form_warnings:
        event_metadata["form_warnings"] = form_warnings
    _append_event_block(
        path,
        event_id=event_id,
        sequence=sequence,
        event_type=event_type,
        role=role,
        content=sanitized_content,
        created_at=now,
        message_id=message_id,
        actor_id=actor_id,
        tenant_id=tenant_id,
        runtime_task_id=runtime_task_id,
        source=source,
        sensitivity=sensitivity,
        metadata=event_metadata,
    )
    index["next_sequence"] = sequence + 1
    index["updated_at"] = _iso(now)
    _write_index(session_dir, index)
    return T0AppendResult(path=path, segment_id=str(segment["segment_id"]), event_id=event_id, sequence=sequence)


def seal_t0_session_segment(
    *,
    agent_id: uuid.UUID | str,
    session_id: uuid.UUID | str,
    reason: str,
    metadata: dict[str, Any] | None = None,
    data_root: Path | str | None = None,
    created_at: datetime | None = None,
) -> T0SealResult | None:
    """Seal the active segment by appending a boundary event.

    Sealing creates a resume boundary; it does not end or summarize the DB
    ChatSession. The next append will create a new segment under the same
    session ledger.
    """

    now = _utc_now(created_at)
    session_dir = _session_dir(data_root, agent_id, session_id)
    index = _load_or_create_index(session_dir, agent_id=agent_id, session_id=session_id, now=now)
    active_segment_id = index.get("active_segment_id")
    if not active_segment_id:
        return None
    segment = _segment_by_id(index, str(active_segment_id))
    if segment is None or segment.get("state") != "open":
        index["active_segment_id"] = None
        _write_index(session_dir, index)
        return None

    sequence = int(index.get("next_sequence") or 1)
    path = session_dir / segment["path"]
    event_id = _new_event_id()
    event_metadata = {"reason": reason, **_clean_metadata(metadata)}
    _append_event_block(
        path,
        event_id=event_id,
        sequence=sequence,
        event_type="segment_boundary",
        role="system",
        content=reason,
        created_at=now,
        message_id=None,
        actor_id=None,
        tenant_id=None,
        runtime_task_id=None,
        source="t0_ledger",
        sensitivity="PL1_public",
        metadata=event_metadata,
    )
    segment["state"] = "sealed"
    segment["sealed_at"] = _iso(now)
    segment["seal_reason"] = reason
    index["active_segment_id"] = None
    index["next_sequence"] = sequence + 1
    index["updated_at"] = _iso(now)
    _write_index(session_dir, index)
    return T0SealResult(path=path, segment_id=str(segment["segment_id"]), sequence=sequence)


def import_legacy_t0_file(
    *,
    agent_id: uuid.UUID | str,
    session_id: uuid.UUID | str,
    legacy_path: Path | str,
    data_root: Path | str | None = None,
    created_at: datetime | None = None,
) -> LegacyImportResult:
    """Import one legacy logs/YYYY-MM-DD/* file into a sealed session segment.

    The source file is never modified or deleted. Idempotency is keyed by the
    source path plus content digest, so repeated repair runs cannot duplicate
    history in the new ledger.
    """

    now = _utc_now(created_at)
    source_path = Path(legacy_path)
    raw = source_path.read_text(encoding="utf-8", errors="replace")
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    legacy_key = hashlib.sha256(f"{source_path.resolve()}\0{digest}".encode("utf-8")).hexdigest()
    session_dir = _session_dir(data_root, agent_id, session_id)
    index = _load_or_create_index(session_dir, agent_id=agent_id, session_id=session_id, now=now)
    legacy_imports = dict(index.get("legacy_imports") or {})
    if legacy_key in legacy_imports:
        record = legacy_imports[legacy_key]
        return LegacyImportResult(
            path=session_dir / str(record["path"]),
            segment_id=str(record["segment_id"]),
            sequence=int(record["sequence"]),
            imported=False,
        )

    segment_id = f"legacy-{digest[:12]}"
    segment_path = Path("segments") / segment_id / SOURCE_FILENAME
    segment = _segment_by_id(index, segment_id)
    if segment is None:
        segment = {
            "segment_id": segment_id,
            "path": segment_path.as_posix(),
            "state": "sealed",
            "created_at": _iso(now),
            "sealed_at": _iso(now),
            "seal_reason": "legacy_import",
            "origin": "legacy_t0_log",
        }
        index.setdefault("segments", []).append(segment)
        _ensure_segment_file(
            session_dir / segment_path, agent_id=agent_id, session_id=session_id, segment_id=segment_id, now=now
        )

    sequence = int(index.get("next_sequence") or 1)
    path = session_dir / segment_path
    event_id = _new_event_id()
    _append_event_block(
        path,
        event_id=event_id,
        sequence=sequence,
        event_type="legacy_import",
        role="system",
        content=raw,
        created_at=now,
        message_id=None,
        actor_id=None,
        tenant_id=None,
        runtime_task_id=None,
        source="legacy_t0_logger",
        sensitivity="PL1_public",
        metadata={
            "legacy_path": source_path.as_posix(),
            "legacy_sha256": digest,
        },
    )
    legacy_imports[legacy_key] = {
        "segment_id": segment_id,
        "path": segment_path.as_posix(),
        "sequence": sequence,
        "legacy_path": source_path.as_posix(),
        "sha256": digest,
        "imported_at": _iso(now),
    }
    index["legacy_imports"] = legacy_imports
    index["next_sequence"] = sequence + 1
    index["updated_at"] = _iso(now)
    _write_index(session_dir, index)
    return LegacyImportResult(path=path, segment_id=segment_id, sequence=sequence, imported=True)


def replay_t0_session_events(
    *,
    agent_id: uuid.UUID | str,
    session_id: uuid.UUID | str,
    data_root: Path | str | None = None,
) -> list[T0SessionEvent]:
    """Read a T0 session ledger back into ordered raw events."""

    session_dir = _session_dir(data_root, agent_id, session_id)
    index = _read_index(session_dir)
    segment_records = list(index.get("segments") or []) if index else _discover_segments(session_dir)
    events: list[T0SessionEvent] = []
    for segment in segment_records:
        segment_id = str(segment.get("segment_id") or "")
        rel_path = segment.get("path") or (Path("segments") / segment_id / SOURCE_FILENAME).as_posix()
        path = session_dir / str(rel_path)
        if not path.exists():
            continue
        events.extend(_parse_events_from_source(path=path, segment_id=segment_id))
    return sorted(events, key=lambda event: event.sequence)


def _session_dir(data_root: Path | str | None, agent_id: uuid.UUID | str, session_id: uuid.UUID | str) -> Path:
    return _data_root(data_root) / str(agent_id) / "memory" / "t0" / "sessions" / str(session_id)


def _data_root(data_root: Path | str | None) -> Path:
    return Path(data_root) if data_root is not None else Path(get_settings().AGENT_DATA_DIR)


def _load_or_create_index(
    session_dir: Path,
    *,
    agent_id: uuid.UUID | str,
    session_id: uuid.UUID | str,
    now: datetime,
) -> dict[str, Any]:
    existing = _read_index(session_dir)
    if existing:
        return existing
    session_dir.mkdir(parents=True, exist_ok=True)
    index = {
        "schema_version": SCHEMA_VERSION,
        "agent_id": str(agent_id),
        "session_id": str(session_id),
        "created_at": _iso(now),
        "updated_at": _iso(now),
        "active_segment_id": None,
        "next_sequence": 1,
        "segments": [],
        "legacy_imports": {},
    }
    _write_index(session_dir, index)
    return index


def _read_index(session_dir: Path) -> dict[str, Any] | None:
    path = session_dir / INDEX_FILENAME
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_index(session_dir: Path, index: dict[str, Any]) -> None:
    session_dir.mkdir(parents=True, exist_ok=True)
    path = session_dir / INDEX_FILENAME
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(index, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def _ensure_open_segment(index: dict[str, Any], *, now: datetime) -> dict[str, Any]:
    active_segment_id = index.get("active_segment_id")
    if active_segment_id:
        segment = _segment_by_id(index, str(active_segment_id))
        if segment is not None and segment.get("state") == "open":
            return segment

    segment_id = f"seg-{now.strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex[:8]}"
    segment = {
        "segment_id": segment_id,
        "path": (Path("segments") / segment_id / SOURCE_FILENAME).as_posix(),
        "state": "open",
        "created_at": _iso(now),
        "sealed_at": None,
        "origin": "session",
    }
    index.setdefault("segments", []).append(segment)
    index["active_segment_id"] = segment_id
    return segment


def _segment_by_id(index: dict[str, Any], segment_id: str) -> dict[str, Any] | None:
    for segment in index.get("segments") or []:
        if str(segment.get("segment_id")) == segment_id:
            return segment
    return None


def _append_event_block(
    path: Path,
    *,
    event_id: str,
    sequence: int,
    event_type: str,
    role: str | None,
    content: str,
    created_at: datetime,
    message_id: uuid.UUID | str | None,
    actor_id: uuid.UUID | str | None,
    tenant_id: uuid.UUID | str | None,
    runtime_task_id: uuid.UUID | str | None,
    source: str,
    sensitivity: str,
    metadata: dict[str, Any],
) -> None:
    segment_id = path.parent.name
    session_id = path.parents[2].name
    agent_id = path.parents[5].name
    _ensure_segment_file(path, agent_id=agent_id, session_id=session_id, segment_id=segment_id, now=created_at)
    block = _render_event_block(
        event_id=event_id,
        sequence=sequence,
        event_type=event_type,
        role=role,
        content=content,
        created_at=created_at,
        message_id=message_id,
        actor_id=actor_id,
        tenant_id=tenant_id,
        runtime_task_id=runtime_task_id,
        source=source,
        sensitivity=sensitivity,
        metadata=metadata,
    )
    with path.open("a", encoding="utf-8") as fh:
        fh.write(block)
        fh.flush()
        os.fsync(fh.fileno())


def _ensure_segment_file(
    path: Path,
    *,
    agent_id: uuid.UUID | str,
    session_id: uuid.UUID | str,
    segment_id: str,
    now: datetime,
) -> None:
    if path.exists():
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    header = (
        "# T0 Session Ledger\n\n"
        f"schema_version: {SCHEMA_VERSION}\n"
        f"agent_id: {agent_id}\n"
        f"session_id: {session_id}\n"
        f"segment_id: {segment_id}\n"
        f"created_at: {_iso(now)}\n\n"
    )
    path.write_text(header, encoding="utf-8")


def _render_event_block(
    *,
    event_id: str,
    sequence: int,
    event_type: str,
    role: str | None,
    content: str,
    created_at: datetime,
    message_id: uuid.UUID | str | None,
    actor_id: uuid.UUID | str | None,
    tenant_id: uuid.UUID | str | None,
    runtime_task_id: uuid.UUID | str | None,
    source: str,
    sensitivity: str,
    metadata: dict[str, Any],
) -> str:
    attrs = {
        "id": event_id,
        "seq": str(sequence),
        "event_type": event_type,
        "role": role or "",
        "created_at": _iso(created_at),
        "message_id": _id_value(message_id),
        "actor_id": _id_value(actor_id),
        "tenant_id": _id_value(tenant_id),
        "runtime_task_id": _id_value(runtime_task_id),
        "source": source,
        "sensitivity": sensitivity,
    }
    rendered_attrs = " ".join(f"{key}={quoteattr(value)}" for key, value in attrs.items())
    metadata_json = json.dumps(metadata, ensure_ascii=False, sort_keys=True)
    return (
        f"<t0_event {rendered_attrs}>\n"
        f"  <content>{xml_escape(content)}</content>\n"
        f"  <metadata>{xml_escape(metadata_json)}</metadata>\n"
        "</t0_event>\n\n"
    )


def _parse_events_from_source(*, path: Path, segment_id: str) -> list[T0SessionEvent]:
    text = path.read_text(encoding="utf-8", errors="replace")
    parsed: list[T0SessionEvent] = []
    for match in _EVENT_BLOCK_RE.finditer(text):
        try:
            node = ET.fromstring(match.group(0))
        except ET.ParseError:
            continue
        attrs = dict(node.attrib)
        raw_metadata = node.findtext("metadata") or "{}"
        try:
            metadata = json.loads(raw_metadata)
        except json.JSONDecodeError:
            metadata = {"raw_metadata": raw_metadata}
        if not isinstance(metadata, dict):
            metadata = {"value": metadata}
        parsed.append(
            T0SessionEvent(
                event_id=attrs.get("id", ""),
                sequence=_safe_int(attrs.get("seq")),
                event_type=attrs.get("event_type", ""),
                role=attrs.get("role") or None,
                content=node.findtext("content") or "",
                created_at=attrs.get("created_at", ""),
                message_id=attrs.get("message_id") or None,
                actor_id=attrs.get("actor_id") or None,
                runtime_task_id=attrs.get("runtime_task_id") or None,
                source=attrs.get("source", ""),
                sensitivity=attrs.get("sensitivity", ""),
                metadata=metadata,
                path=path,
                segment_id=segment_id,
            )
        )
    return parsed


def _discover_segments(session_dir: Path) -> list[dict[str, Any]]:
    segments_dir = session_dir / "segments"
    if not segments_dir.exists():
        return []
    return [
        {
            "segment_id": segment_dir.name,
            "path": (Path("segments") / segment_dir.name / SOURCE_FILENAME).as_posix(),
        }
        for segment_dir in sorted(segments_dir.iterdir())
        if (segment_dir / SOURCE_FILENAME).exists()
    ]


def _sanitize_t0_content(content: Any) -> tuple[str, str, list[str]]:
    if isinstance(content, str):
        raw = content
    else:
        raw = json.dumps(content, ensure_ascii=False, sort_keys=True)
    decision = PrivacyLayer(PrivacyStore()).classify_and_mask(raw)
    lint = lint_memory_form(decision.sanitized_text[:4000])
    form_warnings = sorted({violation.code for violation in lint.violations if violation.code != "empty"})
    return decision.sanitized_text, decision.sensitivity.value, form_warnings


def _clean_metadata(metadata: dict[str, Any] | None) -> dict[str, Any]:
    cleaned: dict[str, Any] = {}
    for key, value in (metadata or {}).items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool, list, dict)):
            cleaned[str(key)] = value
        else:
            cleaned[str(key)] = str(value)
    return cleaned


def _utc_now(value: datetime | None = None) -> datetime:
    if value is None:
        return datetime.now(timezone.utc)
    return value.astimezone(timezone.utc) if value.tzinfo else value.replace(tzinfo=timezone.utc)


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _safe_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _new_event_id() -> str:
    return f"evt_{uuid.uuid4().hex}"


def _id_value(value: uuid.UUID | str | None) -> str:
    if value is None:
        return ""
    if isinstance(value, uuid.UUID):
        return value.hex
    return str(value)
