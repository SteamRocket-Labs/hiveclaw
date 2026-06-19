"""Canonical Segment Package builder for T0 -> T2.

This module owns the file-level pipeline:
T0 source range -> source_bundle.json -> summary.md/labels.md/review.md
candidate -> Platform Gate hard checks -> atomic Segment Package commit.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import os
import shutil
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.config import get_settings
from app.memory.t0.ledger import replay_t0_session_events
from app.memory.t2.prompts import (
    LABELS_PROMPT_VERSION,
    LEARNING_BRAIN_LABELS_PROMPT,
    MEMORY_GATE_REVIEW_PROMPT,
    REVIEW_PROMPT_VERSION,
    SUMMARY_AGENT_PROMPT,
    SUMMARY_PROMPT_VERSION,
)


SUMMARY_FILENAME = "summary.md"
LABELS_FILENAME = "labels.md"
REVIEW_FILENAME = "review.md"
MANIFEST_FILENAME = "manifest.json"
SOURCE_BUNDLE_FILENAME = "source_bundle.json"
_REVIEW_RUBRIC_SCORE_WEIGHTS = {
    "summary_fidelity": 0.35,
    "source_ref_coverage": 0.25,
    "label_alignment": 0.20,
    "safety_scope": 0.10,
    "package_closure": 0.10,
}
_T3_INTAKE_REVIEW_THRESHOLDS = {
    "summary_fidelity": 0.85,
    "source_ref_coverage": 0.85,
    "label_alignment": 0.75,
    "safety_scope": 0.85,
    "package_closure": 0.75,
}

SourceBundleAgent = Callable[..., str | Awaitable[str]]


@dataclass(frozen=True, slots=True)
class T2SegmentPackageResult:
    status: str
    job_id: str
    package_dir: Path
    staging_dir: Path
    issues: tuple[str, ...] = ()


async def build_t2_segment_package(
    *,
    agent_id: uuid.UUID | str,
    tenant_id: uuid.UUID | str | None,
    session_id: uuid.UUID | str,
    t0_segment_id: str,
    summary_agent: SourceBundleAgent,
    learning_brain: SourceBundleAgent,
    memory_gate: SourceBundleAgent,
    data_root: Path | str | None = None,
    package_id: str | None = None,
    job_id: str | None = None,
) -> T2SegmentPackageResult:
    root = _data_root(data_root)
    resolved_package_id = package_id or f"t2pkg-{uuid.uuid4().hex}"
    resolved_job_id = job_id or f"job-{uuid.uuid4().hex}"
    staging_dir = _staging_dir(root, agent_id, resolved_job_id)
    staging_dir.mkdir(parents=True, exist_ok=True)

    source_bundle = _build_source_bundle(
        root=root,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        t0_segment_id=t0_segment_id,
        package_id=resolved_package_id,
    )
    _write_json(staging_dir / SOURCE_BUNDLE_FILENAME, source_bundle)

    summary_md = await _call_agent(summary_agent, source_bundle)
    (staging_dir / "summary.candidate.md").write_text(summary_md, encoding="utf-8")
    labels_md = await _call_agent(learning_brain, source_bundle, summary_md)
    (staging_dir / "labels.candidate.md").write_text(labels_md, encoding="utf-8")
    review_md = await _call_agent(memory_gate, source_bundle, summary_md, labels_md)
    (staging_dir / "review.candidate.md").write_text(review_md, encoding="utf-8")

    issues = _validate_candidate(source_bundle=source_bundle, summary_md=summary_md, labels_md=labels_md, review_md=review_md)
    if issues:
        _write_json(
            staging_dir / "platform_gate_report.json",
            {
                "schema_version": "t2.platform-gate-report.v1",
                "status": "held",
                "issues": issues,
                "created_at": _now(),
            },
        )
        return T2SegmentPackageResult(
            status="held",
            job_id=resolved_job_id,
            package_dir=_package_dir(root, agent_id, session_id, t0_segment_id),
            staging_dir=staging_dir,
            issues=tuple(issues),
        )

    package_dir = _package_dir(root, agent_id, session_id, t0_segment_id)
    files = {
        SUMMARY_FILENAME: summary_md,
        LABELS_FILENAME: labels_md,
        REVIEW_FILENAME: review_md,
    }
    manifest = _build_manifest(
        source_bundle=source_bundle,
        files=files,
        package_status="reviewed",
        job_id=resolved_job_id,
    )
    files[MANIFEST_FILENAME] = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _commit_atomically(package_dir, files)
    _write_json(
        staging_dir / "platform_gate_report.json",
        {
            "schema_version": "t2.platform-gate-report.v1",
            "status": "committed",
            "issues": [],
            "package_dir": _relative_agent_path(root, agent_id, package_dir),
            "created_at": _now(),
        },
    )
    return T2SegmentPackageResult(
        status="committed",
        job_id=resolved_job_id,
        package_dir=package_dir,
        staging_dir=staging_dir,
    )


async def build_t2_segment_package_with_llm(
    *,
    agent_id: uuid.UUID | str,
    tenant_id: uuid.UUID | str | None,
    session_id: uuid.UUID | str,
    t0_segment_id: str,
    data_root: Path | str | None = None,
    package_id: str | None = None,
    job_id: str | None = None,
) -> T2SegmentPackageResult:
    """Build a canonical T2 package using the tenant summary model.

    If no model config is available, the source bundle is retained in staging
    and the job is held. There is intentionally no mechanical summary fallback.
    """

    root = _data_root(data_root)
    resolved_package_id = package_id or f"t2pkg-{uuid.uuid4().hex}"
    resolved_job_id = job_id or f"job-{uuid.uuid4().hex}"

    try:
        from app.services.memory_service import _get_summary_model_config

        model_config = await _get_summary_model_config(uuid.UUID(str(tenant_id))) if tenant_id else None
    except Exception:
        model_config = None

    if not model_config:
        return _hold_without_llm(
            root=root,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            t0_segment_id=t0_segment_id,
            package_id=resolved_package_id,
            job_id=resolved_job_id,
            reason="no summary model config for T0->T2 package build",
        )

    async def summary_agent(source_bundle: dict[str, Any]) -> str:
        return await _run_t2_llm_agent(
            model_config=model_config,
            prompt=SUMMARY_AGENT_PROMPT,
            payload=source_bundle,
            phase="summary",
            agent_id=agent_id,
            tenant_id=tenant_id,
        )

    async def learning_brain(source_bundle: dict[str, Any], summary_md: str) -> str:
        return await _run_t2_llm_agent(
            model_config=model_config,
            prompt=LEARNING_BRAIN_LABELS_PROMPT,
            payload={"source_bundle": source_bundle, "summary_md": summary_md},
            phase="labels",
            agent_id=agent_id,
            tenant_id=tenant_id,
        )

    async def memory_gate(source_bundle: dict[str, Any], summary_md: str, labels_md: str) -> str:
        return await _run_t2_llm_agent(
            model_config=model_config,
            prompt=MEMORY_GATE_REVIEW_PROMPT,
            payload={"source_bundle": source_bundle, "summary_md": summary_md, "labels_md": labels_md},
            phase="review",
            agent_id=agent_id,
            tenant_id=tenant_id,
        )

    try:
        return await build_t2_segment_package(
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            t0_segment_id=t0_segment_id,
            summary_agent=summary_agent,
            learning_brain=learning_brain,
            memory_gate=memory_gate,
            data_root=root,
            package_id=resolved_package_id,
            job_id=resolved_job_id,
        )
    except Exception as exc:  # noqa: BLE001 - failed LLM/package jobs must hold, not corrupt memory
        return _hold_without_llm(
            root=root,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            t0_segment_id=t0_segment_id,
            package_id=resolved_package_id,
            job_id=resolved_job_id,
            reason=f"T0->T2 package build failed: {type(exc).__name__}: {exc}",
        )


def _build_source_bundle(
    *,
    root: Path,
    agent_id: uuid.UUID | str,
    tenant_id: uuid.UUID | str | None,
    session_id: uuid.UUID | str,
    t0_segment_id: str,
    package_id: str,
) -> dict[str, Any]:
    events = [
        event
        for event in replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=root)
        if event.segment_id == t0_segment_id
    ]
    if not events:
        raise ValueError(f"no T0 events for session={session_id} segment={t0_segment_id}")
    source_path = events[0].path
    source_ref = _source_ref(root=root, agent_id=agent_id, session_id=session_id, segment_id=t0_segment_id, path=source_path, events=events)
    work_ledger = _work_ledger_payload(root=root, agent_id=agent_id, session_id=session_id)
    return {
        "schema_version": "t2.source_bundle.v1",
        "package_id": package_id,
        "agent_id": str(agent_id),
        "tenant_id": str(tenant_id) if tenant_id else None,
        "session_id": str(session_id),
        "t0_segment_id": t0_segment_id,
        "source_range": {"start_sequence": events[0].sequence, "end_sequence": events[-1].sequence},
        "distillation_scope": "semantic_candidate",
        "source_kind": "t0_session_ledger",
        "source_refs": [source_ref],
        "t0_events": [_event_payload(event) for event in events],
        "message_refs": [event.message_id for event in events if event.message_id],
        "span_refs": [],
        "artifact_refs": work_ledger["source_refs"],
        "work_ledger": work_ledger,
        "previous_checkpoint_refs": [],
        "principal_context": {},
        "created_at": _now(),
    }


def _work_ledger_payload(*, root: Path, agent_id: uuid.UUID | str, session_id: uuid.UUID | str) -> dict[str, Any]:
    try:
        from app.services.agent_work_ledger import load_agent_work_ledger

        ledger = load_agent_work_ledger(agent_id=uuid.UUID(str(agent_id)), session_id=session_id, data_root=root) or {}
    except Exception:
        ledger = {}

    findings: list[dict[str, Any]] = []
    source_refs: list[str] = []
    for item in ledger.get("findings") or []:
        if not isinstance(item, dict):
            continue
        summary = str(item.get("summary") or "").strip()
        refs = [str(ref).strip() for ref in (item.get("source_refs") or []) if str(ref).strip()]
        if (item.get("trust") or "").strip().lower() != "verified" or not summary or not refs:
            continue
        findings.append(
            {
                "id": str(item.get("id") or f"finding-{len(findings) + 1}"),
                "summary": summary,
                "trust": "verified",
                "source_refs": refs,
            }
        )
        source_refs.extend(refs)

    failures: list[dict[str, Any]] = []
    for item in ledger.get("failures") or []:
        if not isinstance(item, dict) or item.get("resolved"):
            continue
        error = str(item.get("error") or "").strip()
        next_strategy = str(item.get("next_strategy") or "").strip()
        if not error or not next_strategy:
            continue
        failures.append(
            {
                "id": str(item.get("id") or f"failure-{len(failures) + 1}"),
                "error": error,
                "next_strategy": next_strategy,
            }
        )

    return {
        "schema_version": "t2.work_ledger_source.v1",
        "path": f"runtime_artifacts/sessions/{session_id}/work_ledger.json",
        "findings": findings,
        "failures": failures,
        "source_refs": _dedupe(source_refs),
    }


def _hold_without_llm(
    *,
    root: Path,
    agent_id: uuid.UUID | str,
    tenant_id: uuid.UUID | str | None,
    session_id: uuid.UUID | str,
    t0_segment_id: str,
    package_id: str,
    job_id: str,
    reason: str,
) -> T2SegmentPackageResult:
    staging_dir = _staging_dir(root, agent_id, job_id)
    staging_dir.mkdir(parents=True, exist_ok=True)
    try:
        source_bundle = _build_source_bundle(
            root=root,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            t0_segment_id=t0_segment_id,
            package_id=package_id,
        )
        _write_json(staging_dir / SOURCE_BUNDLE_FILENAME, source_bundle)
    except Exception as exc:  # noqa: BLE001
        reason = f"{reason}; source bundle unavailable: {type(exc).__name__}: {exc}"
    _write_json(
        staging_dir / "platform_gate_report.json",
        {
            "schema_version": "t2.platform-gate-report.v1",
            "status": "held",
            "issues": [reason],
            "created_at": _now(),
        },
    )
    return T2SegmentPackageResult(
        status="held",
        job_id=job_id,
        package_dir=_package_dir(root, agent_id, session_id, t0_segment_id),
        staging_dir=staging_dir,
        issues=(reason,),
    )


async def _run_t2_llm_agent(
    *,
    model_config: dict[str, Any],
    prompt: str,
    payload: dict[str, Any],
    phase: str,
    agent_id: uuid.UUID | str,
    tenant_id: uuid.UUID | str | None,
) -> str:
    from app.services.llm_client import LLMMessage, create_llm_client_from_config, with_llm_usage_context

    client = create_llm_client_from_config(
        with_llm_usage_context(
            model_config,
            source=f"t2_{phase}_agent",
            agent_id=agent_id,
            tenant_id=tenant_id,
            metadata={"phase": phase},
        )
    )
    try:
        response = await client.stream(
            messages=[
                LLMMessage(role="system", content=prompt),
                LLMMessage(role="user", content=json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True)),
            ],
            max_tokens=8192,
            temperature=0.2,
        )
        content = response.content or ""
        if not content.strip():
            raise ValueError(f"T2 {phase} agent returned empty content")
        return content
    finally:
        await client.close()


def _source_ref(
    *,
    root: Path,
    agent_id: uuid.UUID | str,
    session_id: uuid.UUID | str,
    segment_id: str,
    path: Path,
    events: list[Any],
) -> dict[str, str]:
    start_seq = events[0].sequence
    end_seq = events[-1].sequence
    return {
        "uri": f"t0://session/{session_id}/segment/{segment_id}#seq={start_seq}..{end_seq}",
        "path": _relative_agent_path(root, agent_id, path),
        "sha256": _sha256(path.read_text(encoding="utf-8", errors="replace")),
    }


def _event_payload(event: Any) -> dict[str, Any]:
    return {
        "event_id": event.event_id,
        "sequence": event.sequence,
        "event_type": event.event_type,
        "role": event.role,
        "content": event.content,
        "created_at": event.created_at,
        "message_id": event.message_id,
        "actor_id": event.actor_id,
        "runtime_task_id": event.runtime_task_id,
        "source": event.source,
        "sensitivity": event.sensitivity,
        "metadata": event.metadata,
        "segment_id": event.segment_id,
    }


def _validate_candidate(*, source_bundle: dict[str, Any], summary_md: str, labels_md: str, review_md: str) -> list[str]:
    issues: list[str] = []
    summary = _extract_single_xml(summary_md, "t2_summary", issues)
    labels = _extract_single_xml(labels_md, "t2_labels", issues)
    review = _extract_single_xml(review_md, "t2_review", issues)
    expected_uris = {str(ref.get("uri")) for ref in source_bundle.get("source_refs") or [] if str(ref.get("uri") or "").strip()}
    if not expected_uris:
        issues.append("source_refs missing from source_bundle")
    for name, node in ((SUMMARY_FILENAME, summary), (LABELS_FILENAME, labels), (REVIEW_FILENAME, review)):
        if node is None:
            continue
        found = {str(item.attrib.get("uri")) for item in node.findall(".//source_ref") if str(item.attrib.get("uri") or "").strip()}
        if not found:
            issues.append(f"{name} source_refs missing")
        elif not found.intersection(expected_uris):
            issues.append(f"{name} source_refs do not match source_bundle")
    if review is not None and (review.findtext("decision") or "").strip() != "approved":
        issues.append("review decision is not approved")
    _validate_review_rubric(summary=summary, labels=labels, review=review, issues=issues)
    return issues


def _validate_review_rubric(
    *,
    summary: ET.Element | None,
    labels: ET.Element | None,
    review: ET.Element | None,
    issues: list[str],
) -> None:
    if review is None:
        return
    rubric = review.find("review_rubric")
    if rubric is None:
        issues.append("review_rubric missing from review.md")
        return
    if (rubric.attrib.get("schema_version") or "").strip() != "t2.review_rubric.v1":
        issues.append("review_rubric schema_version must be t2.review_rubric.v1")

    scores: dict[str, float] = {}
    for score_node in rubric.findall("score"):
        name = (score_node.attrib.get("name") or "").strip()
        if name not in _REVIEW_RUBRIC_SCORE_WEIGHTS:
            issues.append(f"review_rubric has unknown score {name or '<missing>'}")
            continue
        value = _parse_unit_score(score_node.attrib.get("value"))
        if value is None:
            issues.append(f"review_rubric score {name} must be between 0.00 and 1.00")
            continue
        scores[name] = value

    missing = sorted(set(_REVIEW_RUBRIC_SCORE_WEIGHTS) - set(scores))
    if missing:
        issues.append(f"review_rubric missing scores: {', '.join(missing)}")

    review_score = _parse_unit_score(rubric.findtext("review_score"))
    if review_score is None:
        issues.append("review_rubric review_score must be between 0.00 and 1.00")
    elif not missing:
        expected = _round_to_005(sum(scores[name] * weight for name, weight in _REVIEW_RUBRIC_SCORE_WEIGHTS.items()))
        if abs(review_score - expected) > 0.001:
            issues.append(f"review_rubric review_score {review_score:.2f} does not match formula result {expected:.2f}")

    decision = (review.findtext("decision") or "").strip()
    allowed_next = (review.findtext("allowed_next") or "").strip()
    if decision == "approved" and allowed_next == "t3_intake":
        for name, threshold in _T3_INTAKE_REVIEW_THRESHOLDS.items():
            value = scores.get(name)
            if value is not None and value < threshold:
                issues.append(f"review_rubric score {name}={value:.2f} below t3_intake threshold {threshold:.2f}")
        if review_score is not None and review_score < 0.80:
            issues.append(f"review_rubric review_score {review_score:.2f} below t3_intake threshold 0.80")
        package_status = _package_status(summary=summary, labels=labels)
        if package_status == "rolling_checkpoint":
            issues.append("rolling_checkpoint package cannot be approved for t3_intake")


def _parse_unit_score(raw: str | None) -> float | None:
    try:
        value = float(str(raw or "").strip())
    except ValueError:
        return None
    if value < 0.0 or value > 1.0:
        return None
    return value


def _package_status(*, summary: ET.Element | None, labels: ET.Element | None) -> str:
    label_status = (labels.findtext(".//package_status") if labels is not None else "") or ""
    if label_status.strip():
        return label_status.strip()
    return ((summary.attrib.get("status") if summary is not None else "") or "").strip()


def _extract_single_xml(markdown: str, tag: str, issues: list[str]) -> ET.Element | None:
    text = markdown or ""
    start = text.find(f"<{tag}")
    end = text.rfind(f"</{tag}>")
    if start < 0 or end < 0:
        issues.append(f"{tag} block missing")
        return None
    end += len(f"</{tag}>")
    if text.find(f"<{tag}", start + 1) >= 0:
        issues.append(f"{tag} has multiple blocks")
        return None
    try:
        return ET.fromstring(text[start:end])
    except ET.ParseError as exc:
        issues.append(f"{tag} XML parse failed: {exc}")
        return None


def _build_manifest(
    *,
    source_bundle: dict[str, Any],
    files: dict[str, str],
    package_status: str,
    job_id: str,
) -> dict[str, Any]:
    return {
        "schema_version": "t2.segment-package.manifest.v1",
        "package_id": source_bundle["package_id"],
        "job_id": job_id,
        "agent_id": source_bundle["agent_id"],
        "tenant_id": source_bundle.get("tenant_id"),
        "session_id": source_bundle["session_id"],
        "t0_segment_id": source_bundle["t0_segment_id"],
        "source_range": source_bundle["source_range"],
        "source_refs": [str(ref["uri"]) for ref in source_bundle.get("source_refs") or []],
        "package_status": package_status,
        "prompts": {
            "summary_prompt_version": SUMMARY_PROMPT_VERSION,
            "labels_prompt_version": LABELS_PROMPT_VERSION,
            "review_prompt_version": REVIEW_PROMPT_VERSION,
        },
        "files": {
            filename: {"sha256": _sha256(content)}
            for filename, content in files.items()
        },
        "created_at": _now(),
        "write_audit": {
            "writer": "Platform Gate",
            "commit_mode": "atomic_segment_package",
        },
        "rollback_refs": [],
    }


async def _call_agent(agent: SourceBundleAgent, *args: Any) -> str:
    result = agent(*args)
    if inspect.isawaitable(result):
        result = await result
    if not isinstance(result, str) or not result.strip():
        raise ValueError("T2 agent returned empty non-string output")
    return result


def _commit_atomically(package_dir: Path, files: dict[str, str]) -> None:
    package_dir.parent.mkdir(parents=True, exist_ok=True)
    tmp_dir = package_dir.parent / f".{package_dir.name}.tmp-{uuid.uuid4().hex}"
    tmp_dir.mkdir(parents=True)
    try:
        for filename, content in files.items():
            (tmp_dir / filename).write_text(content, encoding="utf-8")
        if package_dir.exists():
            backup_dir = package_dir.parent / f".{package_dir.name}.bak-{uuid.uuid4().hex}"
            os.replace(package_dir, backup_dir)
            os.replace(tmp_dir, package_dir)
            shutil.rmtree(backup_dir, ignore_errors=True)
        else:
            os.replace(tmp_dir, package_dir)
    finally:
        if tmp_dir.exists():
            shutil.rmtree(tmp_dir, ignore_errors=True)


def _data_root(data_root: Path | str | None) -> Path:
    return Path(data_root) if data_root is not None else Path(get_settings().AGENT_DATA_DIR)


def _package_dir(root: Path, agent_id: uuid.UUID | str, session_id: uuid.UUID | str, t0_segment_id: str) -> Path:
    return root / str(agent_id) / "memory" / "sessions" / str(session_id) / "segments" / t0_segment_id


def _staging_dir(root: Path, agent_id: uuid.UUID | str, job_id: str) -> Path:
    return root / str(agent_id) / "memory" / ".staging" / "t2_jobs" / job_id


def _relative_agent_path(root: Path, agent_id: uuid.UUID | str, path: Path) -> str:
    try:
        return path.relative_to(root / str(agent_id)).as_posix()
    except ValueError:
        return path.as_posix()


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value not in seen:
            result.append(value)
            seen.add(value)
    return result


def _round_to_005(value: float) -> float:
    return round(round(max(0.0, min(value, 1.0)) / 0.05) * 0.05, 2)


def _now() -> str:
    return datetime.now(UTC).isoformat()
