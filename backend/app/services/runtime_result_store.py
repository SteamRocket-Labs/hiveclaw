"""Lossless durable result encoding and ref-only parent integration contracts.

The model owns the meaning of a child result.  This module only owns exact
bytes, hashes, ordering, pagination and recovery references.  Integration
pages therefore carry no summary or model-authored body: a parent can fetch
the complete authorized payload through ``read_runtime_result``.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import Any, Iterable, Mapping
import uuid


RUNTIME_RESULT_SCHEMA = "hive.runtime_result.v1"
RUNTIME_RESULT_INTEGRATION_SCHEMA = "hive.runtime_result_integration_page.v1"
_RUNTIME_RESULT_ID_NAMESPACE = uuid.UUID("6e24bb74-d601-5a6e-b7db-010f5565e28c")


@dataclass(frozen=True, slots=True)
class EncodedRuntimeResult:
    payload_bytes: bytes
    sha256: str
    size_bytes: int


def _canonical_json_bytes(payload: Mapping[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def encode_runtime_result_payload(
    *,
    summary: str,
    artifacts: list[dict[str, Any]] | None = None,
    metadata: dict[str, Any] | None = None,
) -> EncodedRuntimeResult:
    """Encode the complete result without truncation or semantic rewriting."""

    payload_bytes = _canonical_json_bytes(
        {
            "schema": RUNTIME_RESULT_SCHEMA,
            "summary": str(summary),
            "artifacts": list(artifacts or []),
            "metadata": dict(metadata or {}),
        }
    )
    return EncodedRuntimeResult(
        payload_bytes=payload_bytes,
        sha256=hashlib.sha256(payload_bytes).hexdigest(),
        size_bytes=len(payload_bytes),
    )


def decode_runtime_result_payload(payload_bytes: bytes) -> dict[str, Any]:
    payload = json.loads(payload_bytes.decode("utf-8"))
    if not isinstance(payload, dict) or payload.get("schema") != RUNTIME_RESULT_SCHEMA:
        raise ValueError("unsupported runtime result payload schema")
    return payload


def runtime_result_ref(*, result_id: uuid.UUID, sha256: str) -> str:
    digest = str(sha256 or "").strip().lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("sha256 must be a lowercase SHA-256 hex digest")
    return f"runtime-result://{result_id}/{digest}"


def runtime_result_object_id(
    *,
    tenant_id: uuid.UUID,
    source_kind: str,
    source_run_id: str,
    sha256: str,
) -> uuid.UUID:
    """Derive the object identity from the same fields as its unique key."""

    digest = str(sha256 or "").strip().lower()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        raise ValueError("sha256 must be a lowercase SHA-256 hex digest")
    normalized_kind = str(source_kind or "").strip().lower()
    normalized_run_id = str(source_run_id or "").strip()
    if not normalized_kind or not normalized_run_id:
        raise ValueError("source_kind and source_run_id are required")
    return uuid.uuid5(
        _RUNTIME_RESULT_ID_NAMESPACE,
        f"{tenant_id}:{normalized_kind}:{normalized_run_id}:{digest}",
    )


@dataclass(frozen=True, slots=True)
class RuntimeResultDescriptor:
    outbox_id: uuid.UUID
    mailbox_sequence: int
    source_kind: str
    source_run_id: str
    task_type: str
    terminal_status: str
    child_session_id: uuid.UUID | None
    child_agent_name: str | None
    result_ref: str
    result_sha256: str
    result_size_bytes: int
    artifact_count: int = 0

    def to_manifest_item(self) -> dict[str, Any]:
        return {
            "outbox_id": str(self.outbox_id),
            "mailbox_sequence": int(self.mailbox_sequence),
            "source_kind": self.source_kind,
            "source_run_id": self.source_run_id,
            "task_type": self.task_type,
            "terminal_status": self.terminal_status,
            "child_session_id": str(self.child_session_id) if self.child_session_id else None,
            "child_agent_name": self.child_agent_name,
            "result_ref": self.result_ref,
            "result_sha256": self.result_sha256,
            "result_size_bytes": int(self.result_size_bytes),
            "artifact_count": int(self.artifact_count),
        }


@dataclass(frozen=True, slots=True)
class RuntimeResultIntegrationPage:
    integration_epoch: int
    root_runtime_task_id: uuid.UUID | None
    items: tuple[RuntimeResultDescriptor, ...]
    coverage: Mapping[str, int | bool]
    complete_page: bool

    def to_manifest(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "schema": RUNTIME_RESULT_INTEGRATION_SCHEMA,
            "integration_epoch": int(self.integration_epoch),
            "root_runtime_task_id": (str(self.root_runtime_task_id) if self.root_runtime_task_id is not None else None),
            "mailbox_sequence_start": self.items[0].mailbox_sequence if self.items else None,
            "mailbox_sequence_end": self.items[-1].mailbox_sequence if self.items else None,
            "item_count": len(self.items),
            "complete_page": bool(self.complete_page),
            "coverage": dict(self.coverage),
            "items": [item.to_manifest_item() for item in self.items],
        }
        payload_bytes = _canonical_json_bytes(payload)
        return {
            **payload,
            "manifest_sha256": hashlib.sha256(payload_bytes).hexdigest(),
        }


def build_runtime_result_integration_pages(
    descriptors: Iterable[RuntimeResultDescriptor],
    *,
    page_item_limit: int,
    starting_epoch: int,
    root_runtime_task_id: uuid.UUID | None,
    coverage: Mapping[str, int | bool],
) -> tuple[RuntimeResultIntegrationPage, ...]:
    """Partition ordered refs without dropping, ranking or interpreting results.

    ``page_item_limit`` is an explicit transport resource ceiling.  It affects
    only how many ref descriptors are delivered per wake; every descriptor is
    retained in order and the complete bytes remain addressable by hash.
    """

    if isinstance(page_item_limit, bool) or int(page_item_limit) < 1:
        raise ValueError("page_item_limit must be a positive integer")
    if isinstance(starting_epoch, bool) or int(starting_epoch) < 1:
        raise ValueError("starting_epoch must be a positive integer")
    ordered = sorted(descriptors, key=lambda item: (item.mailbox_sequence, str(item.outbox_id)))
    if len({item.outbox_id for item in ordered}) != len(ordered):
        raise ValueError("runtime result descriptors contain duplicate outbox identities")
    if len({item.mailbox_sequence for item in ordered}) != len(ordered):
        raise ValueError("runtime result descriptors contain duplicate mailbox sequences")

    pages: list[RuntimeResultIntegrationPage] = []
    limit = int(page_item_limit)
    for page_index, offset in enumerate(range(0, len(ordered), limit)):
        items = tuple(ordered[offset : offset + limit])
        pages.append(
            RuntimeResultIntegrationPage(
                integration_epoch=int(starting_epoch) + page_index,
                root_runtime_task_id=root_runtime_task_id,
                items=items,
                coverage=dict(coverage),
                complete_page=len(items) <= limit,
            )
        )
    return tuple(pages)
