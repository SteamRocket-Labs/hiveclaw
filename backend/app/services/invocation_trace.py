"""File-backed invocation tracing for agent runtime spans."""

from __future__ import annotations

import json
import logging
import time
import uuid
from contextvars import ContextVar, Token
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import get_settings

_invocation_id: ContextVar[str | None] = ContextVar("hive_invocation_id", default=None)
logger = logging.getLogger(__name__)


def current_invocation_id() -> str | None:
    return _invocation_id.get()


def set_invocation_id(invocation_id: str) -> Token[str | None]:
    return _invocation_id.set(invocation_id)


def reset_invocation_id(token: Token[str | None]) -> None:
    _invocation_id.reset(token)


def new_invocation_id() -> str:
    return uuid.uuid4().hex


def monotonic_ms() -> float:
    return time.perf_counter() * 1000.0


def _coerce_uuid(value: Any) -> uuid.UUID | None:
    if value is None or value == "":
        return None
    if isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _string_refs(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _truth_evidence_payloads(value: Any) -> list[dict[str, Any]]:
    raw = value
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except Exception:
            return []
    if isinstance(raw, dict):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [dict(item) for item in raw if isinstance(item, dict)]


def _extract_truth_evidence_fields(metadata: dict[str, Any]) -> tuple[list[str], list[dict[str, Any]]]:
    refs: list[str] = []
    evidence: list[dict[str, Any]] = []

    def consume(source: dict[str, Any]) -> None:
        refs.extend(_string_refs(source.get("evidence_refs")))
        refs.extend(_string_refs(source.get("truth_evidence_refs")))
        payloads = _truth_evidence_payloads(source.get("truth_evidence"))
        payloads.extend(_truth_evidence_payloads(source.get("truth_evidence_json")))
        for payload in payloads:
            evidence.append(payload)
            refs.extend(_string_refs(payload.get("evidence_id")))

    if isinstance(metadata, dict):
        consume(metadata)
        preflight = metadata.get("preflight")
        if isinstance(preflight, dict):
            consume(preflight)

    deduped_refs = list(dict.fromkeys(refs))
    deduped_evidence: list[dict[str, Any]] = []
    seen_evidence: set[str] = set()
    for payload in evidence:
        key = json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str)
        if key in seen_evidence:
            continue
        seen_evidence.add(key)
        deduped_evidence.append(payload)
    return deduped_refs, deduped_evidence


def _span_execution_contract(metadata: dict[str, Any]) -> dict[str, Any]:
    preflight = metadata.get("preflight") if isinstance(metadata.get("preflight"), dict) else {}
    side_effect_refs = _string_refs(metadata.get("side_effect_refs"))
    side_effect_refs.extend(_string_refs(preflight.get("side_effect_refs")))
    claim_version = metadata.get("claim_version")
    fence = None
    if claim_version is None:
        try:
            from app.services.runtime_task_fence import current_runtime_task_fence

            fence = current_runtime_task_fence()
            claim_version = fence.claim_version if fence is not None else None
        except ImportError:
            fence = None
    try:
        normalized_claim_version = int(claim_version) if claim_version is not None else None
    except (TypeError, ValueError):
        normalized_claim_version = None
    return {
        "decision_id": str(metadata.get("decision_id") or preflight.get("decision_id") or "")[:128] or None,
        "input_hash": str(metadata.get("input_hash") or preflight.get("input_hash") or "")[:64] or None,
        "claim_version": normalized_claim_version,
        "idempotency_key": str(
            metadata.get("idempotency_key")
            or preflight.get("idempotency_key")
            or (f"runtime-task:{fence.task_id}:claim:{fence.claim_version}" if fence is not None else "")
        )[:200]
        or None,
        "side_effect_refs": list(dict.fromkeys(side_effect_refs)),
    }


def _span_id(span_type: str, metadata: dict[str, Any]) -> str:
    explicit = str(metadata.get("span_id") or "").strip()
    if explicit:
        return explicit[:80]
    if span_type == "invocation":
        return "invocation"
    return f"{span_type}-{uuid.uuid4().hex[:16]}"


def _span_status(metadata: dict[str, Any]) -> str:
    return str(metadata.get("status") or "ok")[:30]


def _record_span_metric(*, span_type: str, status: str, duration_ms: float, metadata: dict[str, Any]) -> None:
    try:
        from app.memory.metrics import record_invocation_span_metric

        record_invocation_span_metric(
            span_type=span_type,
            status=status,
            duration_ms=duration_ms,
            usage=metadata.get("usage") if isinstance(metadata.get("usage"), dict) else None,
            provider=str(metadata.get("provider") or "unknown"),
            model=str(metadata.get("model") or "unknown"),
            source=str(metadata.get("source") or "unknown"),
        )
    except Exception as exc:  # noqa: BLE001 - metrics must not break runtime tracing
        logger.debug("[InvocationTrace] failed to record metric: %s", exc)


def append_invocation_span(
    *,
    agent_id: uuid.UUID | None,
    span_type: str,
    name: str,
    started_at_ms: float,
    ended_at_ms: float | None = None,
    invocation_id: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    if agent_id is None:
        return None
    trace_id = invocation_id or current_invocation_id() or new_invocation_id()
    end_ms = ended_at_ms if ended_at_ms is not None else monotonic_ms()
    metadata = metadata or {}
    span_id = _span_id(span_type, metadata)
    status = _span_status(metadata)
    duration_ms = max(0.0, round(end_ms - started_at_ms, 3))
    evidence_refs, truth_evidence_json = _extract_truth_evidence_fields(metadata)
    execution_contract = _span_execution_contract(metadata)
    payload = {
        "schema": "invocation_span.v1",
        "invocation_id": trace_id,
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": metadata.get("parent_span_id"),
        "parent_trace_id": metadata.get("parent_trace_id"),
        "agent_id": str(agent_id),
        "span_type": span_type,
        "name": name,
        "status": status,
        "started_at": datetime.now(UTC).isoformat(),
        "duration_ms": duration_ms,
        "evidence_refs": evidence_refs,
        "truth_evidence": truth_evidence_json,
        **execution_contract,
        "metadata": metadata,
    }
    _record_span_metric(span_type=span_type, status=status, duration_ms=duration_ms, metadata=metadata)
    trace_dir = Path(get_settings().AGENT_DATA_DIR) / str(agent_id) / "runtime_artifacts" / "traces"
    trace_dir.mkdir(parents=True, exist_ok=True)
    with (trace_dir / "invocation_spans.jsonl").open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
    return payload


async def record_invocation_span(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    trace_id: str,
    span_id: str,
    parent_span_id: str | None,
    parent_trace_id: str | None,
    span_type: str,
    name: str,
    status: str,
    duration_ms: float,
    agent_id: uuid.UUID | None,
    user_id: uuid.UUID | None,
    runtime_task_id: uuid.UUID | None,
    session_id: str | None,
    request_id: uuid.UUID | None,
    execution_identity_type: str | None = None,
    execution_identity_id: uuid.UUID | str | None = None,
    execution_identity_label: str | None = None,
    metadata: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
    error: str | None = None,
    started_at: datetime | None = None,
    ended_at: datetime | None = None,
):
    """Persist one invocation span into the tenant-scoped query surface."""
    from app.models.invocation_span import InvocationSpan

    clean_metadata = dict(metadata or {})
    identity_metadata = clean_metadata.get("execution_identity")
    if isinstance(identity_metadata, dict):
        execution_identity_type = (
            execution_identity_type or identity_metadata.get("identity_type") or identity_metadata.get("type")
        )
        execution_identity_id = (
            execution_identity_id or identity_metadata.get("identity_id") or identity_metadata.get("id")
        )
        execution_identity_label = execution_identity_label or identity_metadata.get("label")
    evidence_refs, truth_evidence_json = _extract_truth_evidence_fields(clean_metadata)
    execution_contract = _span_execution_contract(clean_metadata)
    row = InvocationSpan(
        id=uuid.uuid4(),
        tenant_id=tenant_id,
        agent_id=agent_id,
        user_id=user_id,
        execution_identity_type=str(execution_identity_type)[:20] if execution_identity_type else None,
        execution_identity_id=_coerce_uuid(execution_identity_id),
        execution_identity_label=str(execution_identity_label)[:200] if execution_identity_label else None,
        runtime_task_id=runtime_task_id,
        session_id=str(session_id) if session_id else None,
        request_id=request_id,
        decision_id=execution_contract["decision_id"],
        input_hash=execution_contract["input_hash"],
        claim_version=execution_contract["claim_version"],
        idempotency_key=execution_contract["idempotency_key"],
        side_effect_refs=execution_contract["side_effect_refs"],
        trace_id=str(trace_id),
        span_id=str(span_id)[:80],
        parent_span_id=str(parent_span_id)[:80] if parent_span_id else None,
        parent_trace_id=str(parent_trace_id)[:255] if parent_trace_id else None,
        span_type=str(span_type)[:50],
        name=str(name)[:200],
        status=str(status or "ok")[:30],
        started_at=started_at or datetime.now(UTC),
        ended_at=ended_at,
        duration_ms=max(0.0, float(duration_ms or 0.0)),
        usage=dict(usage) if isinstance(usage, dict) else None,
        evidence_refs=evidence_refs,
        truth_evidence_json=truth_evidence_json,
        metadata_json=clean_metadata,
        error=str(error)[:4000] if error else None,
    )
    db.add(row)
    await db.flush()
    if agent_id is not None:
        from app.services.ai_assets import record_asset_usage

        await record_asset_usage(
            db,
            tenant_id=tenant_id,
            asset_type="agent",
            native_key=f"agent:{agent_id}",
            evidence={
                "kind": "invocation_span",
                "trace_id": str(trace_id),
                "span_id": str(span_id),
                "idempotency_key": f"span:{trace_id}:{span_id}",
                "runtime_task_id": str(runtime_task_id) if runtime_task_id else None,
                "session_id": str(session_id) if session_id else None,
                "status": str(status),
            },
        )
    return row


async def persist_invocation_span(
    *,
    tenant_id: uuid.UUID | str | None,
    trace_id: str,
    span_id: str,
    parent_span_id: str | None,
    parent_trace_id: str | None,
    span_type: str,
    name: str,
    status: str,
    duration_ms: float,
    agent_id: uuid.UUID | str | None,
    user_id: uuid.UUID | str | None,
    runtime_task_id: uuid.UUID | str | None,
    session_id: str | None,
    request_id: uuid.UUID | str | None,
    execution_identity_type: str | None = None,
    execution_identity_id: uuid.UUID | str | None = None,
    execution_identity_label: str | None = None,
    metadata: dict[str, Any] | None = None,
    usage: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    """Best-effort production writer used by the kernel.

    Failures are logged but never block the LLM/tool loop. Tenant resolution is
    deliberately fail-closed: no tenant_id means no DB span because a NULL tenant
    would be globally visible under the bootstrap RLS policy.
    """
    coerced_tenant_id = _coerce_uuid(tenant_id)
    if coerced_tenant_id is None:
        logger.warning("[InvocationTrace] skip DB span without tenant_id: trace=%s span=%s", trace_id, span_id)
        return
    try:
        from app.database import tenant_scoped_session

        async with tenant_scoped_session(coerced_tenant_id) as db:
            await record_invocation_span(
                db,
                tenant_id=coerced_tenant_id,
                trace_id=trace_id,
                span_id=span_id,
                parent_span_id=parent_span_id,
                parent_trace_id=parent_trace_id,
                span_type=span_type,
                name=name,
                status=status,
                duration_ms=duration_ms,
                agent_id=_coerce_uuid(agent_id),
                user_id=_coerce_uuid(user_id),
                runtime_task_id=_coerce_uuid(runtime_task_id),
                session_id=session_id,
                request_id=_coerce_uuid(request_id),
                execution_identity_type=execution_identity_type,
                execution_identity_id=execution_identity_id,
                execution_identity_label=execution_identity_label,
                metadata=metadata,
                usage=usage,
                error=error,
            )
            await db.commit()
    except Exception as exc:  # noqa: BLE001 - tracing must not break runtime
        logger.warning("[InvocationTrace] failed to persist span trace=%s span=%s: %s", trace_id, span_id, exc)


def _span_to_dict(row: Any) -> dict[str, Any]:
    return {
        "id": str(row.id),
        "tenant_id": str(row.tenant_id),
        "agent_id": str(row.agent_id) if row.agent_id else None,
        "user_id": str(row.user_id) if row.user_id else None,
        "execution_identity": (
            {
                "type": row.execution_identity_type,
                "id": str(row.execution_identity_id) if row.execution_identity_id else None,
                "label": row.execution_identity_label,
            }
            if row.execution_identity_type or row.execution_identity_id or row.execution_identity_label
            else None
        ),
        "runtime_task_id": str(row.runtime_task_id) if row.runtime_task_id else None,
        "session_id": row.session_id,
        "request_id": str(row.request_id) if row.request_id else None,
        "decision_id": row.decision_id,
        "input_hash": row.input_hash,
        "claim_version": row.claim_version,
        "idempotency_key": row.idempotency_key,
        "side_effect_refs": row.side_effect_refs or [],
        "trace_id": row.trace_id,
        "span_id": row.span_id,
        "parent_span_id": row.parent_span_id,
        "parent_trace_id": row.parent_trace_id,
        "span_type": row.span_type,
        "name": row.name,
        "status": row.status,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "ended_at": row.ended_at.isoformat() if row.ended_at else None,
        "duration_ms": row.duration_ms,
        "usage": row.usage or {},
        "evidence_refs": row.evidence_refs or [],
        "truth_evidence": row.truth_evidence_json or [],
        "metadata": row.metadata_json or {},
        "error": row.error,
        "children": [],
    }


async def get_invocation_trace_tree(
    db: AsyncSession,
    *,
    tenant_id: uuid.UUID,
    trace_id: str,
    max_spans: int = 1000,
) -> dict[str, Any]:
    """Return a trace plus cross-invocation children as a nested tree."""
    from app.models.invocation_span import InvocationSpan

    trace_ids = {str(trace_id)}
    rows_by_id: dict[uuid.UUID, Any] = {}
    while True:
        rows = (
            (
                await db.execute(
                    select(InvocationSpan)
                    .where(
                        InvocationSpan.tenant_id == tenant_id,
                        or_(
                            InvocationSpan.trace_id.in_(trace_ids),
                            InvocationSpan.parent_trace_id.in_(trace_ids),
                        ),
                    )
                    .order_by(InvocationSpan.started_at.asc(), InvocationSpan.created_at.asc(), InvocationSpan.id.asc())
                    .limit(max_spans)
                )
            )
            .scalars()
            .all()
        )
        before = len(rows_by_id)
        for row in rows:
            rows_by_id[row.id] = row
            trace_ids.add(row.trace_id)
        if len(rows_by_id) == before or len(rows_by_id) >= max_spans:
            break

    flat = [_span_to_dict(row) for row in sorted(rows_by_id.values(), key=lambda r: (r.started_at, r.created_at, r.id))]
    nodes = {(row["trace_id"], row["span_id"]): row for row in flat}
    roots: list[dict[str, Any]] = []
    for node in flat:
        parent_span_id = node.get("parent_span_id")
        if not parent_span_id:
            roots.append(node)
            continue
        parent_trace_id = node.get("parent_trace_id") or node["trace_id"]
        parent = nodes.get((parent_trace_id, parent_span_id))
        if parent is None:
            roots.append(node)
            continue
        parent["children"].append(node)

    return {
        "tenant_id": str(tenant_id),
        "trace_id": str(trace_id),
        "span_count": len(flat),
        "spans": flat,
        "tree": roots,
    }
