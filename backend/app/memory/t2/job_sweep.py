"""T2 package-job sweep: crash recovery + bounded retry (C9-1, spec §6.2.1).

Held/failed T2 jobs are valid terminal states written by
``run_t2_segment_package_job``; without a sweep they are never picked up and
the underlying experience silently never enters memory. This module owns the
pickup loop:

- ``sweep_stale_t2_jobs`` — synchronous, zero-LLM crash recovery: stale
  queued/running manifests (process died mid-job) are normalized to ``held``.
  Startup entry point; safe to run for every agent on boot.
- ``sweep_t2_jobs`` — full sweep on the heartbeat cadence: stale recovery,
  then bounded retry of held/failed jobs through the canonical
  ``run_t2_segment_package_job`` (stable job_id, idempotent re-run). Jobs
  exhausting ``max_retries`` raise exactly one ``t2_job_retry_exhausted``
  audit alert and stay visible in the control report until resolved.

Retry bookkeeping (``retry_count`` / ``last_retry_at`` /
``retry_exhausted_alerted_at`` / ``recovered_from``) lives in the job
manifest itself; the job runner carries these fields across re-runs.
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

JOB_MANIFEST_FILENAME = "job_manifest.json"
SWEEP_REPORT_SCHEMA = "t2_job_sweep.v1"
DEFAULT_MAX_RETRIES = 3
DEFAULT_STALE_AFTER_SECONDS = 3600.0

_INFLIGHT_STATUSES = {"queued", "running"}
_RETRYABLE_STATUSES = {"held", "failed"}


@dataclass(frozen=True, slots=True)
class T2JobSweepReport:
    agent_id: str
    scanned: int = 0
    recovered_stale: tuple[str, ...] = ()
    retried: tuple[str, ...] = ()
    committed: tuple[str, ...] = ()
    still_held: tuple[str, ...] = ()
    exhausted: tuple[str, ...] = ()
    alerted: tuple[str, ...] = ()
    deferred: tuple[str, ...] = ()


def sweep_stale_t2_jobs(
    *,
    agent_id: uuid.UUID | str,
    data_root: Path | str,
    now: datetime | None = None,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    max_retries: int = DEFAULT_MAX_RETRIES,
) -> T2JobSweepReport:
    """Zero-LLM crash recovery: normalize stale in-flight jobs to held.

    Held/failed jobs are observed (``still_held``/``exhausted``) but never
    retried here — LLM retries belong to the heartbeat cadence, not startup.
    """

    root = Path(data_root)
    current = _now_utc(now)
    scanned = 0
    recovered: list[str] = []
    still_held: list[str] = []
    exhausted: list[str] = []
    for manifest_path, manifest in _iter_job_manifests(root, agent_id):
        scanned += 1
        job_id = str(manifest.get("job_id") or manifest_path.parent.name)
        status = str(manifest.get("status") or "").strip().lower()
        if status in _INFLIGHT_STATUSES and _is_stale(manifest, now=current, stale_after_seconds=stale_after_seconds):
            _recover_stale_manifest(manifest_path, manifest, from_status=status, now=current)
            recovered.append(job_id)
            still_held.append(job_id)
            continue
        if status in _RETRYABLE_STATUSES:
            if _retry_count(manifest) >= max_retries:
                exhausted.append(job_id)
            else:
                still_held.append(job_id)
    report = T2JobSweepReport(
        agent_id=str(agent_id),
        scanned=scanned,
        recovered_stale=tuple(recovered),
        still_held=tuple(still_held),
        exhausted=tuple(exhausted),
    )
    _write_control_report(root, agent_id, report, now=current)
    return report


async def sweep_t2_jobs(
    *,
    agent_id: uuid.UUID | str,
    data_root: Path | str,
    tenant_id: uuid.UUID | str | None = None,
    now: datetime | None = None,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
    max_retries: int = DEFAULT_MAX_RETRIES,
    max_jobs_per_sweep: int | None = None,
) -> T2JobSweepReport:
    """Full sweep: stale recovery + bounded retry of held/failed jobs."""

    if max_jobs_per_sweep is not None and max_jobs_per_sweep <= 0:
        raise ValueError("max_jobs_per_sweep must be positive or None")
    root = Path(data_root)
    current = _now_utc(now)
    manifests = _iter_job_manifests(root, agent_id)
    authoritative_tenant_id = _parse_uuid(tenant_id)
    if authoritative_tenant_id is None and any(not manifest.get("tenant_id") for _, manifest in manifests):
        try:
            from app.services.tenant_resolver import resolve_tenant_for_agent

            authoritative_tenant_id = await resolve_tenant_for_agent(agent_id)
        except Exception as exc:  # noqa: BLE001 - unresolved jobs remain held with their evidence intact
            logger.warning("T2 job tenant resolution failed for agent %s: %s", agent_id, exc)
    scanned = 0
    retry_attempts = 0
    recovered: list[str] = []
    retried: list[str] = []
    committed: list[str] = []
    still_held: list[str] = []
    exhausted: list[str] = []
    alerted: list[str] = []
    deferred: list[str] = []
    for manifest_path, manifest in manifests:
        scanned += 1
        job_id = str(manifest.get("job_id") or manifest_path.parent.name)
        status = str(manifest.get("status") or "").strip().lower()
        if status in _INFLIGHT_STATUSES and _is_stale(manifest, now=current, stale_after_seconds=stale_after_seconds):
            manifest = _recover_stale_manifest(manifest_path, manifest, from_status=status, now=current)
            recovered.append(job_id)
            status = "held"
        if status not in _RETRYABLE_STATUSES:
            continue
        manifest = _repair_tenant_authority(
            manifest_path,
            manifest,
            authoritative_tenant_id=authoritative_tenant_id,
            now=current,
        )
        if _retry_count(manifest) >= max_retries:
            exhausted.append(job_id)
            if not str(manifest.get("retry_exhausted_alerted_at") or "").strip():
                await _emit_retry_exhausted_alert(manifest_path, manifest, now=current)
                alerted.append(job_id)
            continue
        if max_jobs_per_sweep is not None and retry_attempts >= max_jobs_per_sweep:
            deferred.append(job_id)
            continue
        retry_attempts += 1
        result_status = await _retry_job(manifest_path, manifest, root=root, now=current)
        retried.append(job_id)
        if result_status == "committed":
            committed.append(job_id)
        else:
            still_held.append(job_id)
    report = T2JobSweepReport(
        agent_id=str(agent_id),
        scanned=scanned,
        recovered_stale=tuple(recovered),
        retried=tuple(retried),
        committed=tuple(committed),
        still_held=tuple(still_held),
        exhausted=tuple(exhausted),
        alerted=tuple(alerted),
        deferred=tuple(deferred),
    )
    _write_control_report(root, agent_id, report, now=current)
    return report


def _repair_tenant_authority(
    manifest_path: Path,
    manifest: dict[str, Any],
    *,
    authoritative_tenant_id: uuid.UUID | None,
    now: datetime,
) -> dict[str, Any]:
    """Repair jobs created without correct Agent tenant authority while preserving failed-attempt history."""

    manifest_tenant_id = _parse_uuid(manifest.get("tenant_id"))
    if authoritative_tenant_id is None or manifest_tenant_id == authoritative_tenant_id:
        return manifest
    previous_retry_count = _retry_count(manifest)
    previous_tenant_value = manifest.get("tenant_id")
    manifest["tenant_id"] = str(authoritative_tenant_id)
    manifest["tenant_authority_repaired_at"] = now.isoformat()
    if previous_tenant_value:
        manifest["tenant_authority_previous_value"] = str(previous_tenant_value)
    if previous_retry_count:
        manifest["authority_repaired_from_retry_count"] = previous_retry_count
        manifest["retry_count"] = 0
        manifest.pop("retry_exhausted_alerted_at", None)
    manifest["updated_at"] = now.isoformat()
    _write_json(manifest_path, manifest)
    return manifest


def sweep_all_agents_stale_t2_jobs(
    *,
    data_root: Path | str,
    now: datetime | None = None,
    stale_after_seconds: float = DEFAULT_STALE_AFTER_SECONDS,
) -> list[T2JobSweepReport]:
    """Startup crash recovery across every agent workspace under data_root."""

    root = Path(data_root)
    if not root.exists():
        return []
    reports: list[T2JobSweepReport] = []
    for agent_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        if not (_jobs_dir(root, agent_dir.name)).exists():
            continue
        reports.append(
            sweep_stale_t2_jobs(
                agent_id=agent_dir.name,
                data_root=root,
                now=now,
                stale_after_seconds=stale_after_seconds,
            )
        )
    return reports


async def _retry_job(manifest_path: Path, manifest: dict[str, Any], *, root: Path, now: datetime) -> str:
    """Re-run one held/failed job through the canonical runner. Returns terminal status."""

    from app.memory.t2.segment_package import run_t2_segment_package_job

    job_id = str(manifest.get("job_id") or manifest_path.parent.name)
    manifest["retry_count"] = _retry_count(manifest) + 1
    manifest["last_retry_at"] = now.isoformat()
    manifest["updated_at"] = now.isoformat()
    _write_json(manifest_path, manifest)
    try:
        result = await run_t2_segment_package_job(
            data_root=root,
            agent_id=str(manifest.get("agent_id") or ""),
            tenant_id=manifest.get("tenant_id"),
            session_id=str(manifest.get("session_id") or ""),
            t0_segment_id=str(manifest.get("t0_segment_id") or ""),
            package_id=manifest.get("package_id"),
            job_id=job_id,
        )
        return result.status
    except Exception as exc:  # noqa: BLE001 - a broken retry must not kill the sweep loop
        logger.warning("T2 job sweep retry crashed for job %s: %s: %s", job_id, type(exc).__name__, exc)
        return "failed"


async def _emit_retry_exhausted_alert(manifest_path: Path, manifest: dict[str, Any], *, now: datetime) -> None:
    job_id = str(manifest.get("job_id") or manifest_path.parent.name)
    payload = {
        "job_id": job_id,
        "session_id": str(manifest.get("session_id") or ""),
        "t0_segment_id": str(manifest.get("t0_segment_id") or ""),
        "retry_count": _retry_count(manifest),
        "status": str(manifest.get("status") or ""),
        "issues": [str(issue) for issue in (manifest.get("issues") or [])],
    }
    agent_id = _parse_uuid(manifest.get("agent_id"))
    try:
        from app.services.audit_logger import write_audit_log

        await write_audit_log("t2_job_retry_exhausted", payload, agent_id=agent_id)
    except Exception as exc:  # noqa: BLE001 - alerting must not break the sweep; the ledger still records it
        logger.warning("T2 job retry-exhausted audit alert failed for job %s: %s", job_id, exc)
    manifest["retry_exhausted_alerted_at"] = now.isoformat()
    manifest["updated_at"] = now.isoformat()
    _write_json(manifest_path, manifest)


def _recover_stale_manifest(
    manifest_path: Path, manifest: dict[str, Any], *, from_status: str, now: datetime
) -> dict[str, Any]:
    issues = [str(issue) for issue in (manifest.get("issues") or [])]
    issues.append(f"T2 job crash-recovered: stale {from_status} manifest normalized to held")
    manifest["status"] = "held"
    manifest["recovered_from"] = from_status
    manifest["issues"] = issues
    manifest["updated_at"] = now.isoformat()
    _write_json(manifest_path, manifest)
    return manifest


def _iter_job_manifests(root: Path, agent_id: uuid.UUID | str) -> list[tuple[Path, dict[str, Any]]]:
    jobs_dir = _jobs_dir(root, agent_id)
    if not jobs_dir.exists():
        return []
    manifests: list[tuple[Path, dict[str, Any]]] = []
    for job_dir in sorted(path for path in jobs_dir.iterdir() if path.is_dir()):
        manifest_path = job_dir / JOB_MANIFEST_FILENAME
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            logger.warning("T2 job sweep skipped unreadable manifest %s: %s", manifest_path, exc)
            continue
        if not isinstance(manifest, dict):
            logger.warning("T2 job sweep skipped non-object manifest %s", manifest_path)
            continue
        manifests.append((manifest_path, manifest))
    return manifests


def _is_stale(manifest: dict[str, Any], *, now: datetime, stale_after_seconds: float) -> bool:
    raw = str(manifest.get("updated_at") or manifest.get("created_at") or "").strip()
    if not raw:
        return True
    try:
        stamp = datetime.fromisoformat(raw)
    except ValueError:
        return True
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return (now - stamp).total_seconds() > stale_after_seconds


def _retry_count(manifest: dict[str, Any]) -> int:
    try:
        return max(0, int(manifest.get("retry_count") or 0))
    except (TypeError, ValueError):
        return 0


def _write_control_report(root: Path, agent_id: uuid.UUID | str, report: T2JobSweepReport, *, now: datetime) -> None:
    payload = {
        "schema": SWEEP_REPORT_SCHEMA,
        "agent_id": report.agent_id,
        "generated_at": now.isoformat(),
        "scanned": report.scanned,
        "recovered_stale": list(report.recovered_stale),
        "retried": list(report.retried),
        "committed": list(report.committed),
        "still_held": list(report.still_held),
        "exhausted": list(report.exhausted),
        "alerted": list(report.alerted),
        "deferred": list(report.deferred),
    }
    _write_json(root / str(agent_id) / "memory" / "control" / "t2_job_sweep.json", payload)


def _jobs_dir(root: Path, agent_id: uuid.UUID | str) -> Path:
    return root / str(agent_id) / "memory" / ".staging" / "t2_jobs"


def _parse_uuid(value: Any) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (TypeError, ValueError):
        return None


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _now_utc(now: datetime | None) -> datetime:
    value = now or datetime.now(UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)
