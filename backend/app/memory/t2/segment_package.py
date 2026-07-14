"""Canonical Segment Package builder for T0 -> T2.

This module owns the file-level pipeline:
T0 source range -> source_bundle.json -> summary.md/labels.md/review.md
candidate -> Platform Gate hard checks -> atomic Segment Package commit.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import logging
import os
import re
import shutil
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as _xml_escape_raw

from app.config import get_settings
from app.memory.t0.ledger import replay_t0_session_events
from app.memory.t2.prompts import (
    EPISODE_GATE_REVIEW_PROMPT,
    EPISODE_GATE_REVIEW_PROMPT_VERSION,
    EPISODE_STITCHER_PROMPT,
    EPISODE_STITCHER_PROMPT_VERSION,
    LABELS_PROMPT_VERSION,
    LEARNING_BRAIN_LABELS_PROMPT,
    MEMORY_GATE_REVIEW_PROMPT,
    REVIEW_PROMPT_VERSION,
    SUMMARY_AGENT_PROMPT,
    SUMMARY_PROMPT_VERSION,
)


logger = logging.getLogger(__name__)

SUMMARY_FILENAME = "summary.md"
LABELS_FILENAME = "labels.md"
REVIEW_FILENAME = "review.md"
MANIFEST_FILENAME = "manifest.json"
SOURCE_BUNDLE_FILENAME = "source_bundle.json"
SYNTHESIS_FILENAME = "synthesis.md"
EPISODE_BUNDLE_FILENAME = "episode_bundle.json"
_REVIEW_RUBRIC_SCORE_NAMES = frozenset(
    {"summary_fidelity", "source_ref_coverage", "label_alignment", "safety_scope", "package_closure"}
)
_EPISODE_REVIEW_RUBRIC_SCORE_NAMES = frozenset(
    {"continuity_fidelity", "source_ref_coverage", "correction_quality", "closure_quality", "safety_scope"}
)
_ALLOWED_REVIEW_NEXT = {"t3_intake", "episode_stitching", "short_term_carryover", "archive_recall_only", "none"}
_ACTIVATION_KEYS_SCHEMA_VERSION = "t2.activation_keys.20260705"
_ALLOWED_ACTIVATION_TASK_INTENTS = {
    "architecture_design",
    "coding",
    "research",
    "operations",
    "memory_recall",
    "self_evolution",
    "general",
}
_ALLOWED_ACTIVATION_ENTITY_TYPES = {"doc", "file", "person", "org", "concept", "tool", "skill"}
_ALLOWED_ACTIVATION_TEMPORAL_KINDS = {"explicit", "relative", "continuation"}
_ALLOWED_ACTIVATION_DECISION_STATUSES = {"accepted", "rejected", "superseded", "open"}
_ALLOWED_ACTIVATION_RELATION_RELS = {"depends_on", "blocks", "related_to", "contradicts"}
_ALLOWED_ACTIVATION_RISK_FLAGS = {
    "architecture_drift",
    "privacy_sensitive",
    "cross_tenant",
    "security_relevant",
    "production_impact",
    "policy_conflict",
    "evidence_gap",
}
_LINEAGE_METADATA_KEYS = (
    "root_session_id",
    "parent_session_id",
    "branch_session_id",
    "branch_mode",
    "source_session_id",
    "anchor_event_id",
    "anchor_sequence",
    "visible_prefix_end",
    "regenerate_from_event_id",
    "regenerate_prompt_source_event_id",
    "edit_from_event_id",
    "rollback_strategy",
    "command",
)
_LINEAGE_RISK_BRANCH_MODES = {"fork", "edit", "insert_before", "insert_after", "regenerate", "rewind", "rollback"}
_COMPACTION_LINEAGE_KEYS = {
    "compaction_id",
    "compaction_checkpoint",
    "replacement_history",
    "compaction_replacement_history",
    "compacted_from",
}

SourceBundleAgent = Callable[..., str | Awaitable[str]]


@dataclass(frozen=True, slots=True)
class T2SegmentPackageResult:
    status: str
    job_id: str
    package_dir: Path
    staging_dir: Path
    issues: tuple[str, ...] = ()
    episode_result: T2EpisodeStitchPackageResult | None = None


@dataclass(frozen=True, slots=True)
class T2SegmentPackageJobResult:
    status: str
    job_id: str
    package_dir: Path
    staging_dir: Path
    issues: tuple[str, ...] = ()
    package_result: T2SegmentPackageResult | None = None


@dataclass(frozen=True, slots=True)
class T2EpisodeStitchPackageResult:
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
    memory_gate: SourceBundleAgent | None,
    data_root: Path | str | None = None,
    package_id: str | None = None,
    job_id: str | None = None,
    session_lineage: dict[str, Any] | None = None,
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
        session_lineage=session_lineage,
    )
    _write_json(staging_dir / SOURCE_BUNDLE_FILENAME, source_bundle)

    summary_md = await _call_agent(summary_agent, source_bundle)
    (staging_dir / "summary.candidate.md").write_text(summary_md, encoding="utf-8")
    labels_md = await _call_agent(learning_brain, source_bundle, summary_md)
    (staging_dir / "labels.candidate.md").write_text(labels_md, encoding="utf-8")
    if memory_gate is None:
        issues = ["independent Memory Gate required: all T2 reviews must be LLM-authored"]
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
    review_md = await _call_agent(memory_gate, source_bundle, summary_md, labels_md)
    review_mode = "independent_gate"
    (staging_dir / "review.candidate.md").write_text(review_md, encoding="utf-8")

    issues = _validate_candidate(
        source_bundle=source_bundle, summary_md=summary_md, labels_md=labels_md, review_md=review_md
    )
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
        review_mode=review_mode,
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
    session_lineage: dict[str, Any] | None = None,
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
            session_lineage=session_lineage,
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
        result = await build_t2_segment_package(
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
            session_lineage=session_lineage,
        )
        if result.status == "committed" and _package_requests_episode_stitching(result.package_dir):
            episode_result = await build_t2_episode_stitch_package_with_llm(
                agent_id=agent_id,
                tenant_id=tenant_id,
                session_id=session_id,
                trigger_package_id=resolved_package_id,
                data_root=root,
            )
            return replace(result, episode_result=episode_result)
        return result
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
            session_lineage=session_lineage,
        )


async def run_t2_segment_package_job(
    *,
    agent_id: uuid.UUID | str,
    tenant_id: uuid.UUID | str | None,
    session_id: uuid.UUID | str,
    t0_segment_id: str,
    data_root: Path | str | None = None,
    package_id: str | None = None,
    job_id: str | None = None,
    session_lineage: dict[str, Any] | None = None,
) -> T2SegmentPackageJobResult:
    """Durably record and run a T0->T2 package job.

    The job manifest is the crash/restart boundary: queued/running/terminal
    state is written before and after the LLM-owned package builder. The
    builder may still return a held package when model config is unavailable;
    that held result is a valid terminal state, not a silent failure.
    """

    root = _data_root(data_root)
    resolved_package_id = package_id or _stable_t2_package_id(session_id=session_id, t0_segment_id=t0_segment_id)
    resolved_job_id = job_id or _stable_t2_job_id(session_id=session_id, t0_segment_id=t0_segment_id)
    staging_dir = _staging_dir(root, agent_id, resolved_job_id)
    staging_dir.mkdir(parents=True, exist_ok=True)
    package_dir = _package_dir(root, agent_id, session_id, t0_segment_id)
    manifest_path = staging_dir / "job_manifest.json"
    base_manifest = {
        "schema_version": "t2.segment-package-job.v1",
        "job_id": resolved_job_id,
        "package_id": resolved_package_id,
        "agent_id": str(agent_id),
        "tenant_id": str(tenant_id) if tenant_id else None,
        "session_id": str(session_id),
        "t0_segment_id": str(t0_segment_id),
        "package_dir": _relative_agent_path(root, agent_id, package_dir),
        "staging_dir": _relative_agent_path(root, agent_id, staging_dir),
        "created_at": _now(),
    }
    carried = _carry_over_job_fields(manifest_path)
    if not manifest_path.exists():
        _write_json(manifest_path, {**base_manifest, "status": "queued", "updated_at": _now(), "issues": []})
    _write_json(manifest_path, {**base_manifest, **carried, "status": "running", "updated_at": _now(), "issues": []})
    try:
        result = await build_t2_segment_package_with_llm(
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            t0_segment_id=t0_segment_id,
            data_root=root,
            package_id=resolved_package_id,
            job_id=resolved_job_id,
            session_lineage=session_lineage,
        )
        terminal = "committed" if result.status == "committed" else "held"
        _write_json(
            manifest_path,
            {
                **base_manifest,
                **carried,
                "status": terminal,
                "package_status": result.status,
                "issues": list(result.issues),
                "package_manifest_path": _relative_agent_path(root, agent_id, result.package_dir / MANIFEST_FILENAME),
                "updated_at": _now(),
            },
        )
        return T2SegmentPackageJobResult(
            status=terminal,
            job_id=resolved_job_id,
            package_dir=result.package_dir,
            staging_dir=staging_dir,
            issues=result.issues,
            package_result=result,
        )
    except Exception as exc:  # noqa: BLE001
        issue = f"T0->T2 job failed before package builder terminal state: {type(exc).__name__}: {exc}"
        _write_json(
            manifest_path,
            {
                **base_manifest,
                **carried,
                "status": "failed",
                "issues": [issue],
                "updated_at": _now(),
            },
        )
        return T2SegmentPackageJobResult(
            status="failed",
            job_id=resolved_job_id,
            package_dir=package_dir,
            staging_dir=staging_dir,
            issues=(issue,),
        )


async def build_t2_episode_stitch_package(
    *,
    agent_id: uuid.UUID | str,
    tenant_id: uuid.UUID | str | None,
    session_id: uuid.UUID | str,
    trigger_package_id: str,
    stitcher_agent: SourceBundleAgent,
    memory_gate: SourceBundleAgent,
    data_root: Path | str | None = None,
    episode_id: str | None = None,
    job_id: str | None = None,
    source_package_ids: list[str] | None = None,
) -> T2EpisodeStitchPackageResult:
    root = _data_root(data_root)
    resolved_episode_id = episode_id or f"episode-{uuid.uuid4().hex}"
    resolved_job_id = job_id or f"episode-job-{uuid.uuid4().hex}"
    staging_dir = _episode_staging_dir(root, agent_id, resolved_job_id)
    staging_dir.mkdir(parents=True, exist_ok=True)

    episode_bundle = _build_episode_bundle(
        root=root,
        agent_id=agent_id,
        tenant_id=tenant_id,
        session_id=session_id,
        trigger_package_id=trigger_package_id,
        episode_id=resolved_episode_id,
        source_package_ids=source_package_ids,
    )
    _write_json(staging_dir / EPISODE_BUNDLE_FILENAME, episode_bundle)

    synthesis_md = await _call_agent(stitcher_agent, episode_bundle)
    (staging_dir / "synthesis.candidate.md").write_text(synthesis_md, encoding="utf-8")
    review_md = await _call_agent(memory_gate, episode_bundle, synthesis_md)
    (staging_dir / "review.candidate.md").write_text(review_md, encoding="utf-8")

    issues = _validate_episode_candidate(episode_bundle=episode_bundle, synthesis_md=synthesis_md, review_md=review_md)
    if issues:
        _write_json(
            staging_dir / "platform_gate_report.json",
            {
                "schema_version": "t2.episode-platform-gate-report.v1",
                "status": "held",
                "issues": issues,
                "created_at": _now(),
            },
        )
        return T2EpisodeStitchPackageResult(
            status="held",
            job_id=resolved_job_id,
            package_dir=_episode_dir(root, agent_id, session_id, resolved_episode_id),
            staging_dir=staging_dir,
            issues=tuple(issues),
        )

    package_dir = _episode_dir(root, agent_id, session_id, resolved_episode_id)
    files = {
        SYNTHESIS_FILENAME: synthesis_md,
        REVIEW_FILENAME: review_md,
    }
    manifest = _build_episode_manifest(
        episode_bundle=episode_bundle,
        files=files,
        job_id=resolved_job_id,
    )
    files[MANIFEST_FILENAME] = json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    _commit_atomically(package_dir, files)
    _write_json(
        staging_dir / "platform_gate_report.json",
        {
            "schema_version": "t2.episode-platform-gate-report.v1",
            "status": "committed",
            "issues": [],
            "package_dir": _relative_agent_path(root, agent_id, package_dir),
            "created_at": _now(),
        },
    )
    return T2EpisodeStitchPackageResult(
        status="committed",
        job_id=resolved_job_id,
        package_dir=package_dir,
        staging_dir=staging_dir,
    )


async def build_t2_episode_stitch_package_with_llm(
    *,
    agent_id: uuid.UUID | str,
    tenant_id: uuid.UUID | str | None,
    session_id: uuid.UUID | str,
    trigger_package_id: str,
    data_root: Path | str | None = None,
    episode_id: str | None = None,
    job_id: str | None = None,
    source_package_ids: list[str] | None = None,
) -> T2EpisodeStitchPackageResult:
    root = _data_root(data_root)
    resolved_episode_id = episode_id or f"episode-{uuid.uuid4().hex}"
    resolved_job_id = job_id or f"episode-job-{uuid.uuid4().hex}"

    try:
        from app.services.memory_service import _get_summary_model_config

        model_config = await _get_summary_model_config(uuid.UUID(str(tenant_id))) if tenant_id else None
    except Exception:
        model_config = None

    if not model_config:
        return _hold_episode_without_llm(
            root=root,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            trigger_package_id=trigger_package_id,
            episode_id=resolved_episode_id,
            job_id=resolved_job_id,
            source_package_ids=source_package_ids,
            reason="no summary model config for T2 episode stitching",
        )

    async def stitcher(episode_bundle: dict[str, Any]) -> str:
        return await _run_t2_llm_agent(
            model_config=model_config,
            prompt=EPISODE_STITCHER_PROMPT,
            payload=episode_bundle,
            phase="episode_stitcher",
            agent_id=agent_id,
            tenant_id=tenant_id,
        )

    async def gate(episode_bundle: dict[str, Any], synthesis_md: str) -> str:
        return await _run_t2_llm_agent(
            model_config=model_config,
            prompt=EPISODE_GATE_REVIEW_PROMPT,
            payload={"episode_bundle": episode_bundle, "synthesis_md": synthesis_md},
            phase="episode_gate",
            agent_id=agent_id,
            tenant_id=tenant_id,
        )

    try:
        return await build_t2_episode_stitch_package(
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            trigger_package_id=trigger_package_id,
            stitcher_agent=stitcher,
            memory_gate=gate,
            data_root=root,
            episode_id=resolved_episode_id,
            job_id=resolved_job_id,
            source_package_ids=source_package_ids,
        )
    except Exception as exc:  # noqa: BLE001
        return _hold_episode_without_llm(
            root=root,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            trigger_package_id=trigger_package_id,
            episode_id=resolved_episode_id,
            job_id=resolved_job_id,
            source_package_ids=source_package_ids,
            reason=f"T2 episode stitching failed: {type(exc).__name__}: {exc}",
        )


_FAILURE_MODE_HEADING_RE = re.compile(r"^###\s+(?P<title>.+?)(?:\s+—\s+(?P<status>\S+))?\s*$")
_FAILURE_MODE_ANCHOR_RE = re.compile(r"<!--\s*id:\s*(?P<id>[^\s>]+)\s*-->")


def _known_failure_modes(*, root: Path, agent_id: uuid.UUID | str) -> list[dict[str, str]]:
    """Known self.md failure modes (id/title/status) for the labels agent.

    工序 1 emits ``<failure_signal ref outcome>`` recurrence/avoidance rows —
    the labels LLM needs full visibility of the ids it may reference (L1).
    Absence of self.md or the section reads as an empty list, never an error.
    """
    path = Path(root) / str(agent_id) / "memory" / "self" / "self.md"
    if not path.exists():
        return []
    try:
        content = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        logger.warning("known_failure_modes read failed for %s: %s", agent_id, exc)
        return []
    modes: list[dict[str, str]] = []
    in_section = False
    current: dict[str, str] | None = None
    for line in content.splitlines():
        if line.startswith("## "):
            in_section = line.strip() == "## 失败模式"
            current = None
            continue
        if not in_section:
            continue
        heading = _FAILURE_MODE_HEADING_RE.match(line)
        if heading:
            current = {
                "id": "",
                "title": heading.group("title").strip(),
                "status": (heading.group("status") or "").strip(),
            }
            continue
        if current is None:
            continue
        anchor = _FAILURE_MODE_ANCHOR_RE.search(line)
        if anchor and not current["id"]:
            current["id"] = anchor.group("id")
            modes.append(current)
            continue
        status_match = re.match(r"^-\s*状态:\s*(?P<status>\S+)", line.strip())
        if status_match and current in modes:
            current["status"] = status_match.group("status")
    return [mode for mode in modes if mode["id"]]


def _build_source_bundle(
    *,
    root: Path,
    agent_id: uuid.UUID | str,
    tenant_id: uuid.UUID | str | None,
    session_id: uuid.UUID | str,
    t0_segment_id: str,
    package_id: str,
    session_lineage: dict[str, Any] | None = None,
) -> dict[str, Any]:
    segment_events = [
        event
        for event in replay_t0_session_events(agent_id=agent_id, session_id=session_id, data_root=root)
        if event.segment_id == t0_segment_id
    ]
    events = [event for event in segment_events if _is_t2_semantic_source_event(event)]
    excluded_events = [event for event in segment_events if not _is_t2_semantic_source_event(event)]
    if not events:
        raise ValueError(f"no semantic T0 events for session={session_id} segment={t0_segment_id}")
    if not any(_is_semantic_content_event(event) for event in events):
        raise ValueError(f"no semantic T0 events for session={session_id} segment={t0_segment_id}")
    source_path = events[0].path
    source_ref = _source_ref(
        root=root, agent_id=agent_id, session_id=session_id, segment_id=t0_segment_id, path=source_path, events=events
    )
    work_ledger = _work_ledger_payload(root=root, agent_id=agent_id, session_id=session_id)
    known_failure_modes = _known_failure_modes(root=root, agent_id=agent_id)
    lineage = _build_lineage_payload(session_id=session_id, session_lineage=session_lineage, events=segment_events)
    excluded_refs = _excluded_source_refs(session_id=session_id, events=excluded_events)
    context_refs = _context_refs_from_lineage_and_events(lineage=lineage, events=segment_events)
    visible_source_view = _visible_source_view(
        session_id=session_id,
        t0_segment_id=t0_segment_id,
        semantic_events=events,
        excluded_refs=excluded_refs,
        lineage=lineage,
    )
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
        "context_refs": context_refs,
        "excluded_refs": excluded_refs,
        "lineage": lineage,
        "visible_source_view": visible_source_view,
        "t0_events": [_event_payload(event) for event in events],
        "message_refs": [event.message_id for event in events if event.message_id],
        "span_refs": [],
        "artifact_refs": work_ledger["source_refs"],
        "work_ledger": work_ledger,
        "known_failure_modes": known_failure_modes,
        "previous_checkpoint_refs": [],
        "principal_context": {},
        "created_at": _now(),
    }


def _is_t2_semantic_source_event(event: Any) -> bool:
    metadata = event.metadata or {}
    if _is_false_metadata_flag(metadata.get("semantic_memory_eligible")):
        return False
    if _is_true_metadata_flag(metadata.get("projection_only")):
        return False
    return True


def _is_semantic_content_event(event: Any) -> bool:
    if getattr(event, "event_type", None) == "segment_boundary":
        return False
    return bool(str(getattr(event, "content", "") or "").strip())


def _is_false_metadata_flag(value: Any) -> bool:
    if value is False:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"false", "0", "no"}
    return False


def _is_true_metadata_flag(value: Any) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes"}
    return False


def _build_lineage_payload(
    *,
    session_id: uuid.UUID | str,
    session_lineage: dict[str, Any] | None,
    events: list[Any],
) -> dict[str, Any]:
    lineage: dict[str, Any] = {
        "schema_version": "t2.session-lineage.v1",
        "session_id": str(session_id),
    }
    provided = session_lineage if isinstance(session_lineage, dict) else {}
    for key in _LINEAGE_METADATA_KEYS:
        value = provided.get(key)
        if value is None or value == "":
            value = _first_event_metadata_value(events, key)
        if value is None or value == "":
            continue
        lineage[key] = _json_safe_lineage_value(value)
    lineage.setdefault("root_session_id", str(session_id))
    lineage.setdefault("branch_session_id", str(session_id))
    risk_flags = _lineage_risk_flags(lineage=lineage, events=events)
    lineage["risk_flags"] = risk_flags
    return lineage


def _first_event_metadata_value(events: list[Any], key: str) -> Any:
    for event in events:
        metadata = event.metadata if isinstance(getattr(event, "metadata", None), dict) else {}
        value = metadata.get(key)
        if value is not None and value != "":
            return value
    return None


def _json_safe_lineage_value(value: Any) -> Any:
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    if isinstance(value, (list, tuple)):
        return [_json_safe_lineage_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_safe_lineage_value(item) for key, item in value.items()}
    return str(value)


def _lineage_risk_flags(*, lineage: dict[str, Any], events: list[Any]) -> list[str]:
    flags: list[str] = []
    branch_mode = str(lineage.get("branch_mode") or "").strip().lower()
    if branch_mode in _LINEAGE_RISK_BRANCH_MODES:
        flags.append(f"branch_mode={branch_mode}")
    source_session_id = str(lineage.get("source_session_id") or "").strip()
    session_id = str(lineage.get("session_id") or "").strip()
    if source_session_id and source_session_id != session_id:
        flags.append(f"source_session_id={source_session_id}")
    command = str(lineage.get("command") or "").strip().lower()
    if command in {"rollback", "rewind", "branch"}:
        flags.append(f"command={command}")
    if str(lineage.get("rollback_strategy") or "").strip():
        flags.append("rollback_strategy")
    if any(_event_has_any_metadata_key(event, _COMPACTION_LINEAGE_KEYS) for event in events):
        flags.append("compaction_replacement")
    return _dedupe(flags)


def _event_has_any_metadata_key(event: Any, keys: set[str]) -> bool:
    metadata = event.metadata if isinstance(getattr(event, "metadata", None), dict) else {}
    return any(key in metadata and metadata.get(key) not in (None, "", [], {}) for key in keys)


def _excluded_source_refs(*, session_id: uuid.UUID | str, events: list[Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    for event in events:
        refs.append(
            {
                "uri": f"t0://session/{session_id}/segment/{event.segment_id}#seq={event.sequence}",
                "event_id": event.event_id,
                "sequence": event.sequence,
                "reason": _t2_exclusion_reason(event),
            }
        )
    return refs


def _t2_exclusion_reason(event: Any) -> str:
    metadata = event.metadata if isinstance(getattr(event, "metadata", None), dict) else {}
    if _is_false_metadata_flag(metadata.get("semantic_memory_eligible")):
        return "semantic_memory_eligible=false"
    if _is_true_metadata_flag(metadata.get("projection_only")):
        return "projection_only=true"
    return "non_semantic"


def _context_refs_from_lineage_and_events(*, lineage: dict[str, Any], events: list[Any]) -> list[dict[str, Any]]:
    refs: list[dict[str, Any]] = []
    source_session_id = str(lineage.get("source_session_id") or "").strip()
    anchor_event_id = str(lineage.get("anchor_event_id") or "").strip()
    if source_session_id and anchor_event_id:
        refs.append(
            {
                "kind": "branch_anchor",
                "session_id": source_session_id,
                "event_id": anchor_event_id,
                "sequence": lineage.get("anchor_sequence") or lineage.get("visible_prefix_end"),
            }
        )
    for event in events:
        metadata = event.metadata if isinstance(getattr(event, "metadata", None), dict) else {}
        for key in ("semantic_source_refs", "context_refs", "previous_checkpoint_refs"):
            refs.extend(_coerce_context_refs(metadata.get(key), default_kind=key))
    return _dedupe_dicts(refs)


def _coerce_context_refs(value: Any, *, default_kind: str) -> list[dict[str, Any]]:
    if value in (None, "", [], {}):
        return []
    if isinstance(value, dict):
        item = dict(value)
        item.setdefault("kind", default_kind)
        return [item]
    if isinstance(value, str):
        return [{"kind": default_kind, "uri": value}]
    if isinstance(value, (list, tuple)):
        refs: list[dict[str, Any]] = []
        for item in value:
            refs.extend(_coerce_context_refs(item, default_kind=default_kind))
        return refs
    return [{"kind": default_kind, "value": str(value)}]


def _visible_source_view(
    *,
    session_id: uuid.UUID | str,
    t0_segment_id: str,
    semantic_events: list[Any],
    excluded_refs: list[dict[str, Any]],
    lineage: dict[str, Any],
) -> dict[str, Any]:
    return {
        "schema_version": "t2.visible-source-view.v1",
        "session_id": str(session_id),
        "t0_segment_id": t0_segment_id,
        "semantic_sequences": [event.sequence for event in semantic_events],
        "semantic_event_ids": [event.event_id for event in semantic_events],
        "excluded_refs": excluded_refs,
        "lineage_risk_flags": list(lineage.get("risk_flags") or []),
    }


def _dedupe_dicts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for item in items:
        key = json.dumps(item, ensure_ascii=False, sort_keys=True)
        if key in seen:
            continue
        seen.add(key)
        result.append(item)
    return result


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


def _build_episode_bundle(
    *,
    root: Path,
    agent_id: uuid.UUID | str,
    tenant_id: uuid.UUID | str | None,
    session_id: uuid.UUID | str,
    trigger_package_id: str,
    episode_id: str,
    source_package_ids: list[str] | None,
) -> dict[str, Any]:
    packages_by_id = _load_t2_packages(root=root, agent_id=agent_id, session_id=session_id)
    if trigger_package_id not in packages_by_id:
        raise ValueError(f"trigger T2 package not found: {trigger_package_id}")
    if _package_allowed_next(packages_by_id[trigger_package_id]["review_md"]) != "episode_stitching":
        raise ValueError("episode stitching requires trigger review.allowed_next=episode_stitching")
    lineage_warnings: list[str] = []
    if source_package_ids is None:
        default_source_package_ids, lineage_warnings = _default_episode_source_package_ids(
            packages_by_id=packages_by_id, trigger_package_id=trigger_package_id
        )
        candidate_source_package_ids = default_source_package_ids
    else:
        trigger_package = packages_by_id[trigger_package_id]
        candidate_source_package_ids = list(source_package_ids)
        for package_id in candidate_source_package_ids:
            package = packages_by_id.get(package_id)
            if (
                package is not None
                and package_id != trigger_package_id
                and not _packages_lineage_compatible(trigger_package, package)
            ):
                lineage_warnings.append(f"incompatible_explicit_source_package={package_id}")
    selected_ids = _dedupe([trigger_package_id, *candidate_source_package_ids])
    source_packages = []
    for package_id in selected_ids:
        package = packages_by_id.get(package_id)
        if package is None:
            raise ValueError(f"T2 source package not found: {package_id}")
        source_packages.append(package)

    t0_refs = _dedupe([ref for package in source_packages for ref in package["source_refs"]])
    adjacent = [package for package_id, package in packages_by_id.items() if package_id not in set(selected_ids)]
    return {
        "schema_version": "t2.episode_bundle.v1",
        "episode_id": episode_id,
        "episode_job_id": f"episode-job-{episode_id}",
        "agent_id": str(agent_id),
        "tenant_id": str(tenant_id) if tenant_id else None,
        "session_id": str(session_id),
        "trigger_package_id": trigger_package_id,
        "trigger_reason": "review.allowed_next=episode_stitching",
        "source_packages": source_packages,
        "adjacent_packages": adjacent,
        "open_carryover_packages": [
            package
            for package in packages_by_id.values()
            if package["package_status"] in {"open", "rolling_checkpoint"} and package["package_id"] not in selected_ids
        ],
        "lineage": packages_by_id[trigger_package_id].get("lineage") or {},
        "lineage_warnings": lineage_warnings,
        "t0_source_refs": t0_refs,
        "t0_source_ranges": [package.get("source_range") for package in source_packages if package.get("source_range")],
        "principal_context": {},
        "created_at": _now(),
    }


def _load_t2_packages(
    *, root: Path, agent_id: uuid.UUID | str, session_id: uuid.UUID | str
) -> dict[str, dict[str, Any]]:
    packages: dict[str, dict[str, Any]] = {}
    for session_segments_dir in _segment_package_roots(root, agent_id, session_id):
        if not session_segments_dir.exists():
            continue
        for package_dir in sorted(path for path in session_segments_dir.iterdir() if path.is_dir()):
            manifest_path = package_dir / MANIFEST_FILENAME
            if not manifest_path.exists():
                continue
            try:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except Exception:
                continue
            package_id = str(manifest.get("package_id") or "").strip()
            if not package_id or package_id in packages:
                continue
            summary = (package_dir / SUMMARY_FILENAME).read_text(encoding="utf-8", errors="replace")
            labels = (package_dir / LABELS_FILENAME).read_text(encoding="utf-8", errors="replace")
            review = (package_dir / REVIEW_FILENAME).read_text(encoding="utf-8", errors="replace")
            packages[package_id] = {
                "package_id": package_id,
                "path": _relative_agent_path(root, agent_id, package_dir),
                "t0_segment_id": str(manifest.get("t0_segment_id") or package_dir.name),
                "source_refs": [str(ref) for ref in manifest.get("source_refs") or [] if str(ref).strip()],
                "source_range": manifest.get("source_range"),
                "lineage": manifest.get("lineage") or {},
                "visible_source_view": manifest.get("visible_source_view") or {},
                "context_refs": manifest.get("context_refs") or [],
                "excluded_refs": manifest.get("excluded_refs") or [],
                "package_status": str(manifest.get("package_status") or ""),
                "summary_md": summary,
                "labels_md": labels,
                "review_md": review,
            }
    return packages


def _default_episode_source_package_ids(
    *, packages_by_id: dict[str, dict[str, Any]], trigger_package_id: str
) -> tuple[list[str], list[str]]:
    package_ids = sorted(packages_by_id, key=lambda package_id: _package_sort_key(packages_by_id[package_id]))
    if trigger_package_id not in packages_by_id:
        return [], []
    trigger_index = package_ids.index(trigger_package_id)
    trigger_package = packages_by_id[trigger_package_id]
    continuity_state = _package_continuity_state(packages_by_id[trigger_package_id].get("labels_md") or "")
    offsets = {
        "needs_previous": (-1,),
        "needs_next": (1,),
        "same_episode_candidate": (-1, 1),
    }.get(continuity_state, (-1, 1))
    adjacent_ids: list[str] = []
    incompatible_candidates: list[str] = []
    for offset in offsets:
        neighbor_index = trigger_index + offset
        if neighbor_index < 0 or neighbor_index >= len(package_ids):
            continue
        neighbor_id = package_ids[neighbor_index]
        neighbor = packages_by_id[neighbor_id]
        if _packages_lineage_compatible(trigger_package, neighbor):
            adjacent_ids.append(neighbor_id)
        else:
            incompatible_candidates.append(neighbor_id)
    warnings: list[str] = []
    if not adjacent_ids and incompatible_candidates:
        warnings.append("missing_compatible_adjacent_source")
    return adjacent_ids, warnings


def _packages_lineage_compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return _package_lineage_key(left) == _package_lineage_key(right)


def _package_lineage_key(package: dict[str, Any]) -> tuple[str, str, str, str, str]:
    lineage = package.get("lineage") if isinstance(package.get("lineage"), dict) else {}
    session_id = str(lineage.get("session_id") or "")
    return (
        str(lineage.get("root_session_id") or session_id),
        str(lineage.get("branch_session_id") or session_id),
        str(lineage.get("source_session_id") or ""),
        str(lineage.get("branch_mode") or ""),
        str(lineage.get("anchor_event_id") or ""),
    )


def _package_sort_key(package: dict[str, Any]) -> tuple[int, str]:
    source_range = package.get("source_range") if isinstance(package.get("source_range"), dict) else {}
    try:
        start_sequence = int(source_range.get("start_sequence"))
    except (TypeError, ValueError):
        start_sequence = 1_000_000_000
    return start_sequence, str(package.get("path") or package.get("package_id") or "")


def _package_continuity_state(labels_md: str) -> str:
    issues: list[str] = []
    labels = _extract_single_xml(labels_md, "t2_labels", issues)
    if labels is None:
        return ""
    return _continuity_state(labels)


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
    session_lineage: dict[str, Any] | None = None,
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
            session_lineage=session_lineage,
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


def _hold_episode_without_llm(
    *,
    root: Path,
    agent_id: uuid.UUID | str,
    tenant_id: uuid.UUID | str | None,
    session_id: uuid.UUID | str,
    trigger_package_id: str,
    episode_id: str,
    job_id: str,
    source_package_ids: list[str] | None,
    reason: str,
) -> T2EpisodeStitchPackageResult:
    staging_dir = _episode_staging_dir(root, agent_id, job_id)
    staging_dir.mkdir(parents=True, exist_ok=True)
    try:
        episode_bundle = _build_episode_bundle(
            root=root,
            agent_id=agent_id,
            tenant_id=tenant_id,
            session_id=session_id,
            trigger_package_id=trigger_package_id,
            episode_id=episode_id,
            source_package_ids=source_package_ids,
        )
        _write_json(staging_dir / EPISODE_BUNDLE_FILENAME, episode_bundle)
    except Exception as exc:  # noqa: BLE001
        reason = f"{reason}; episode bundle unavailable: {type(exc).__name__}: {exc}"
    _write_json(
        staging_dir / "platform_gate_report.json",
        {
            "schema_version": "t2.episode-platform-gate-report.v1",
            "status": "held",
            "issues": [reason],
            "created_at": _now(),
        },
    )
    return T2EpisodeStitchPackageResult(
        status="held",
        job_id=job_id,
        package_dir=_episode_dir(root, agent_id, session_id, episode_id),
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
    from app.services.llm_client import (
        LLMMessage,
        create_llm_client_from_config,
        get_max_tokens,
        with_llm_usage_context,
    )

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
            max_tokens=get_max_tokens(
                str(model_config.get("provider") or ""),
                str(model_config.get("model") or ""),
                model_config.get("max_output_tokens"),
            ),
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
    truth_path = getattr(events[0], "truth_path", None) or path
    truth_path = Path(truth_path)
    return {
        "uri": f"t0://session/{session_id}/segment/{segment_id}#seq={start_seq}..{end_seq}",
        "path": _relative_agent_path(root, agent_id, path),
        "sha256": _sha256(path.read_text(encoding="utf-8", errors="replace")),
        "truth_path": _relative_agent_path(root, agent_id, truth_path),
        "truth_sha256": _sha256(truth_path.read_text(encoding="utf-8", errors="replace"))
        if truth_path.exists()
        else "",
        "projection_path": _relative_agent_path(root, agent_id, path),
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
        "truth_path": str(getattr(event, "truth_path", "") or ""),
        "byte_offset": getattr(event, "byte_offset", None),
        "byte_length": getattr(event, "byte_length", None),
        "event_hash": getattr(event, "event_hash", None),
        "prev_event_hash": getattr(event, "prev_event_hash", None),
        "record_schema_version": getattr(event, "record_schema_version", None),
    }


def _validate_candidate(*, source_bundle: dict[str, Any], summary_md: str, labels_md: str, review_md: str) -> list[str]:
    issues: list[str] = []
    summary = _extract_single_xml(summary_md, "t2_summary", issues)
    labels = _extract_single_xml(labels_md, "t2_labels", issues)
    review = _extract_single_xml(review_md, "t2_review", issues)
    expected_uris = {
        str(ref.get("uri")) for ref in source_bundle.get("source_refs") or [] if str(ref.get("uri") or "").strip()
    }
    if not expected_uris:
        issues.append("source_refs missing from source_bundle")
    for name, node in ((SUMMARY_FILENAME, summary), (LABELS_FILENAME, labels), (REVIEW_FILENAME, review)):
        if node is None:
            continue
        found = {
            str(item.attrib.get("uri"))
            for item in node.findall(".//source_ref")
            if str(item.attrib.get("uri") or "").strip()
        }
        if not found:
            issues.append(f"{name} source_refs missing")
        elif not found.intersection(expected_uris):
            issues.append(f"{name} source_refs do not match source_bundle")
    _validate_continuity_fields(summary=summary, labels=labels, review=review, issues=issues)
    _validate_activation_keys(labels=labels, issues=issues)
    if review is not None and (review.findtext("decision") or "").strip() != "approved":
        issues.append("review decision is not approved")
    _validate_review_rubric(summary=summary, labels=labels, review=review, issues=issues)
    return issues


def _validate_activation_keys(*, labels: ET.Element | None, issues: list[str]) -> None:
    if labels is None:
        return
    nodes = labels.findall("activation_keys")
    if not nodes:
        return
    if len(nodes) > 1:
        issues.append("labels.md activation_keys must appear at most once")
    node = nodes[0]
    if (node.attrib.get("schema_version") or "").strip() != _ACTIVATION_KEYS_SCHEMA_VERSION:
        issues.append("labels.md activation_keys schema_version must be t2.activation_keys.20260705")
    for task_intent in node.findall("task_intent"):
        if _xml_text(task_intent) and _xml_text(task_intent) not in _ALLOWED_ACTIVATION_TASK_INTENTS:
            issues.append("labels.md activation_keys task_intent must be controlled enum")
    for entity in node.findall("entity"):
        if _xml_text(entity) and (entity.attrib.get("type") or "").strip() not in _ALLOWED_ACTIVATION_ENTITY_TYPES:
            issues.append("labels.md activation_keys entity type must be controlled enum")
    for temporal_hint in node.findall("temporal_hint"):
        if (
            _xml_text(temporal_hint)
            and (temporal_hint.attrib.get("kind") or "").strip() not in _ALLOWED_ACTIVATION_TEMPORAL_KINDS
        ):
            issues.append("labels.md activation_keys temporal_hint kind must be controlled enum")
    for decision in node.findall("decision"):
        if (
            _xml_text(decision)
            and (decision.attrib.get("status") or "").strip() not in _ALLOWED_ACTIVATION_DECISION_STATUSES
        ):
            issues.append("labels.md activation_keys decision status must be controlled enum")
    for relation_seed in node.findall("relation_seed"):
        if (
            _xml_text(relation_seed)
            and (relation_seed.attrib.get("rel") or "").strip() not in _ALLOWED_ACTIVATION_RELATION_RELS
        ):
            issues.append("labels.md activation_keys relation_seed rel must be controlled enum")
    for risk_flag in node.findall("risk_flag"):
        if _xml_text(risk_flag) and _xml_text(risk_flag) not in _ALLOWED_ACTIVATION_RISK_FLAGS:
            issues.append("labels.md activation_keys risk_flag must be controlled enum")


def _xml_text(node: ET.Element) -> str:
    return str(node.text or "").strip()


def _validate_continuity_fields(
    *,
    summary: ET.Element | None,
    labels: ET.Element | None,
    review: ET.Element | None,
    issues: list[str],
) -> None:
    if summary is not None:
        segment_state = _segment_state(summary)
        if segment_state not in {"complete", "continuation", "interrupted", "low_signal", "administrative"}:
            issues.append(
                "summary.md segment_state must be complete|continuation|interrupted|low_signal|administrative"
            )
        if summary.find("continuity") is None:
            issues.append("summary.md continuity block missing")
    if labels is not None:
        continuity_state = _continuity_state(labels)
        if continuity_state not in {
            "standalone",
            "same_episode_candidate",
            "needs_previous",
            "needs_next",
            "low_signal",
            "admin_only",
        }:
            issues.append("labels.md continuity_state must be controlled enum")
    if review is not None:
        allowed_next = (review.findtext("allowed_next") or "").strip()
        if allowed_next not in _ALLOWED_REVIEW_NEXT:
            issues.append("review.md allowed_next must be controlled enum")
        if allowed_next == "t3_intake":
            if summary is not None and _segment_state(summary) != "complete":
                issues.append("segment_state must be complete for t3_intake")
            if labels is not None and _continuity_state(labels) != "standalone":
                issues.append("continuity_state must be standalone for t3_intake")
        if allowed_next == "episode_stitching" and labels is not None:
            if _continuity_state(labels) not in {"same_episode_candidate", "needs_previous", "needs_next"}:
                issues.append(
                    "episode_stitching requires continuity_state same_episode_candidate|needs_previous|needs_next"
                )


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
        if name not in _REVIEW_RUBRIC_SCORE_NAMES:
            issues.append(f"review_rubric has unknown score {name or '<missing>'}")
            continue
        value = _parse_unit_score(score_node.attrib.get("value"))
        if value is None:
            issues.append(f"review_rubric score {name} must be between 0.00 and 1.00")
            continue
        scores[name] = value

    missing = sorted(_REVIEW_RUBRIC_SCORE_NAMES - set(scores))
    if missing:
        issues.append(f"review_rubric missing scores: {', '.join(missing)}")

    review_score = _parse_unit_score(rubric.findtext("review_score"))
    if review_score is None:
        issues.append("review_rubric review_score must be between 0.00 and 1.00")

    decision = (review.findtext("decision") or "").strip()
    allowed_next = (review.findtext("allowed_next") or "").strip()
    if decision == "approved" and allowed_next == "t3_intake":
        package_status = _package_status(summary=summary, labels=labels)
        if package_status == "rolling_checkpoint":
            issues.append("rolling_checkpoint package cannot be approved for t3_intake")


def _validate_episode_candidate(*, episode_bundle: dict[str, Any], synthesis_md: str, review_md: str) -> list[str]:
    issues: list[str] = []
    synthesis = _extract_single_xml(synthesis_md, "episode_synthesis", issues)
    review = _extract_single_xml(review_md, "episode_review", issues)
    expected_refs = {str(ref) for ref in episode_bundle.get("t0_source_refs") or [] if str(ref).strip()}
    expected_package_ids = {
        str(package.get("package_id"))
        for package in episode_bundle.get("source_packages") or []
        if str(package.get("package_id") or "").strip()
    }
    if not expected_refs:
        issues.append("episode_bundle t0_source_refs missing")
    if not expected_package_ids:
        issues.append("episode_bundle source_packages missing")

    if synthesis is not None:
        found_refs = {
            str(item.attrib.get("uri"))
            for item in synthesis.findall(".//source_ref")
            if str(item.attrib.get("uri") or "").strip()
        }
        if not found_refs:
            issues.append("synthesis.md source_refs missing")
        elif expected_refs and not found_refs.intersection(expected_refs):
            issues.append("synthesis.md source_refs do not match episode_bundle")
        found_package_ids = {
            str(item.attrib.get("package_id"))
            for item in synthesis.findall(".//package_ref")
            if str(item.attrib.get("package_id") or "").strip()
        }
        if not found_package_ids:
            issues.append("synthesis.md source package refs missing")
        elif expected_package_ids and not found_package_ids.intersection(expected_package_ids):
            issues.append("synthesis.md source package refs do not match episode_bundle")

    if review is not None and (review.findtext("decision") or "").strip() != "approved":
        issues.append("episode review decision is not approved")
    if review is not None and (review.findtext("allowed_next") or "").strip() == "t3_intake":
        lineage_warnings = [
            str(item).strip() for item in episode_bundle.get("lineage_warnings") or [] if str(item).strip()
        ]
        if "missing_compatible_adjacent_source" in lineage_warnings:
            issues.append("missing compatible adjacent source package for episode stitching")
        incompatible = [item for item in lineage_warnings if item.startswith("incompatible_explicit_source_package=")]
        if incompatible:
            issues.append("incompatible explicit source package lineage for episode stitching")
    _validate_episode_review_rubric(synthesis=synthesis, review=review, issues=issues)
    return issues


def _validate_episode_review_rubric(
    *,
    synthesis: ET.Element | None,
    review: ET.Element | None,
    issues: list[str],
) -> None:
    if review is None:
        return
    rubric = review.find("episode_review_rubric")
    if rubric is None:
        issues.append("episode_review_rubric missing from review.md")
        return
    if (rubric.attrib.get("schema_version") or "").strip() != "t2.episode_review_rubric.v1":
        issues.append("episode_review_rubric schema_version must be t2.episode_review_rubric.v1")

    scores: dict[str, float] = {}
    for score_node in rubric.findall("score"):
        name = (score_node.attrib.get("name") or "").strip()
        if name not in _EPISODE_REVIEW_RUBRIC_SCORE_NAMES:
            issues.append(f"episode_review_rubric has unknown score {name or '<missing>'}")
            continue
        value = _parse_unit_score(score_node.attrib.get("value"))
        if value is None:
            issues.append(f"episode_review_rubric score {name} must be between 0.00 and 1.00")
            continue
        scores[name] = value

    missing = sorted(_EPISODE_REVIEW_RUBRIC_SCORE_NAMES - set(scores))
    if missing:
        issues.append(f"episode_review_rubric missing scores: {', '.join(missing)}")

    review_score = _parse_unit_score(rubric.findtext("review_score"))
    if review_score is None:
        issues.append("episode_review_rubric review_score must be between 0.00 and 1.00")

    decision = (review.findtext("decision") or "").strip()
    allowed_next = (review.findtext("allowed_next") or "").strip()
    if allowed_next not in {"t3_intake", "short_term_carryover", "archive_recall_only", "none"}:
        issues.append("episode review allowed_next must be controlled enum")
    if decision == "approved" and allowed_next == "t3_intake":
        if synthesis is not None and (synthesis.attrib.get("status") or "").strip() != "closed":
            issues.append("episode_synthesis status must be closed for t3_intake")


def _parse_unit_score(raw: str | None) -> float | None:
    try:
        value = float(str(raw or "").strip())
    except ValueError:
        return None
    if value < 0.0 or value > 1.0:
        return None
    return value


def _xml_escape(value: str) -> str:
    return _xml_escape_raw(str(value), {'"': "&quot;"})


def _package_status(*, summary: ET.Element | None, labels: ET.Element | None) -> str:
    label_status = (labels.findtext(".//package_status") if labels is not None else "") or ""
    if label_status.strip():
        return label_status.strip()
    return ((summary.attrib.get("status") if summary is not None else "") or "").strip()


def _package_requests_episode_stitching(package_dir: Path) -> bool:
    review_path = package_dir / REVIEW_FILENAME
    if not review_path.exists():
        return False
    issues: list[str] = []
    review = _extract_single_xml(review_path.read_text(encoding="utf-8", errors="replace"), "t2_review", issues)
    return bool(review is not None and (review.findtext("allowed_next") or "").strip() == "episode_stitching")


def _package_allowed_next(review_md: str) -> str:
    issues: list[str] = []
    review = _extract_single_xml(review_md, "t2_review", issues)
    if review is None:
        return ""
    return (review.findtext("allowed_next") or "").strip()


def _segment_state(summary: ET.Element) -> str:
    node = summary.find("segment_state")
    if node is None:
        return ""
    return (node.attrib.get("value") or node.text or "").strip()


def _continuity_state(labels: ET.Element) -> str:
    return (labels.findtext(".//continuity_state") or "").strip()


def _extract_single_xml(markdown: str, tag: str, issues: list[str]) -> ET.Element | None:
    text = markdown or ""
    open_pattern = re.compile(rf"<{re.escape(tag)}(?=[\s>/])")
    starts = list(open_pattern.finditer(text))
    end = text.rfind(f"</{tag}>")
    if not starts or end < 0:
        issues.append(f"{tag} block missing")
        return None
    start = starts[0].start()
    end += len(f"</{tag}>")
    if len(starts) > 1:
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
    review_mode: str = "independent_gate",
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
        "context_refs": source_bundle.get("context_refs") or [],
        "excluded_refs": source_bundle.get("excluded_refs") or [],
        "activation_keys_preview": _activation_keys_preview_from_labels(files.get(LABELS_FILENAME, "")),
        "lineage": source_bundle.get("lineage") or {},
        "visible_source_view": source_bundle.get("visible_source_view") or {},
        "package_status": package_status,
        "review_mode": review_mode,
        "prompts": {
            "summary_prompt_version": SUMMARY_PROMPT_VERSION,
            "labels_prompt_version": LABELS_PROMPT_VERSION,
            "review_prompt_version": REVIEW_PROMPT_VERSION,
        },
        "files": {filename: {"sha256": _sha256(content)} for filename, content in files.items()},
        "created_at": _now(),
        "write_audit": {
            "writer": "Platform Gate",
            "commit_mode": "atomic_segment_package",
        },
        "rollback_refs": [],
    }


def _activation_keys_preview_from_labels(labels_md: str) -> dict[str, Any]:
    preview: dict[str, Any] = {
        "schema_version": "t2.activation_keys.preview.v1",
        "present": False,
        "task_intents": [],
        "scenarios": [],
        "entities": [],
        "temporal_hints": [],
        "decisions": [],
        "open_loops": [],
        "relation_seeds": [],
        "risk_flags": [],
    }
    issues: list[str] = []
    labels = _extract_single_xml(labels_md, "t2_labels", issues)
    if labels is None:
        if issues:
            preview["parse_issues"] = issues
        return preview
    node = labels.find("activation_keys")
    if node is None:
        return preview
    preview["present"] = True
    preview["source_schema_version"] = (node.attrib.get("schema_version") or "").strip()
    preview["task_intents"] = _activation_child_texts(node, "task_intent")
    preview["scenarios"] = _activation_child_texts(node, "scenario")
    preview["entities"] = [
        {"type": (item.attrib.get("type") or "").strip(), "value": _xml_text(item)}
        for item in node.findall("entity")
        if _xml_text(item)
    ]
    preview["temporal_hints"] = [
        {"kind": (item.attrib.get("kind") or "").strip(), "value": _xml_text(item)}
        for item in node.findall("temporal_hint")
        if _xml_text(item)
    ]
    preview["decisions"] = [
        {"status": (item.attrib.get("status") or "").strip(), "value": _xml_text(item)}
        for item in node.findall("decision")
        if _xml_text(item)
    ]
    preview["open_loops"] = _activation_child_texts(node, "open_loop")
    preview["relation_seeds"] = [
        {"rel": (item.attrib.get("rel") or "").strip(), "value": _xml_text(item)}
        for item in node.findall("relation_seed")
        if _xml_text(item)
    ]
    preview["risk_flags"] = _activation_child_texts(node, "risk_flag")
    return preview


def _activation_child_texts(node: ET.Element, tag: str) -> list[str]:
    return [text for item in node.findall(tag) if (text := _xml_text(item))]


def _build_episode_manifest(
    *,
    episode_bundle: dict[str, Any],
    files: dict[str, str],
    job_id: str,
) -> dict[str, Any]:
    return {
        "schema_version": "t2.episode-stitch.manifest.v1",
        "episode_id": episode_bundle["episode_id"],
        "job_id": job_id,
        "agent_id": episode_bundle["agent_id"],
        "tenant_id": episode_bundle.get("tenant_id"),
        "session_id": episode_bundle["session_id"],
        "trigger_package_id": episode_bundle["trigger_package_id"],
        "source_packages": [
            str(package["package_id"])
            for package in episode_bundle.get("source_packages") or []
            if str(package.get("package_id") or "").strip()
        ],
        "source_refs": [str(ref) for ref in episode_bundle.get("t0_source_refs") or []],
        "lineage": episode_bundle.get("lineage") or {},
        "lineage_warnings": episode_bundle.get("lineage_warnings") or [],
        "package_status": "reviewed",
        "prompts": {
            "stitcher_prompt_version": EPISODE_STITCHER_PROMPT_VERSION,
            "review_prompt_version": EPISODE_GATE_REVIEW_PROMPT_VERSION,
        },
        "files": {filename: {"sha256": _sha256(content)} for filename, content in files.items()},
        "created_at": _now(),
        "write_audit": {
            "writer": "Platform Gate",
            "commit_mode": "atomic_episode_stitch_package",
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


def _t2_sessions_dir(root: Path, agent_id: uuid.UUID | str) -> Path:
    return root / str(agent_id) / "memory" / "t2" / "sessions"


def _legacy_t2_sessions_dir(root: Path, agent_id: uuid.UUID | str) -> Path:
    return root / str(agent_id) / "memory" / "sessions"


def _segment_package_roots(root: Path, agent_id: uuid.UUID | str, session_id: uuid.UUID | str) -> tuple[Path, Path]:
    return (
        _t2_sessions_dir(root, agent_id) / str(session_id) / "segments",
        _legacy_t2_sessions_dir(root, agent_id) / str(session_id) / "segments",
    )


def _package_dir(root: Path, agent_id: uuid.UUID | str, session_id: uuid.UUID | str, t0_segment_id: str) -> Path:
    return _t2_sessions_dir(root, agent_id) / str(session_id) / "segments" / t0_segment_id


def _episode_dir(root: Path, agent_id: uuid.UUID | str, session_id: uuid.UUID | str, episode_id: str) -> Path:
    return _t2_sessions_dir(root, agent_id) / str(session_id) / "episodes" / episode_id


def _staging_dir(root: Path, agent_id: uuid.UUID | str, job_id: str) -> Path:
    return root / str(agent_id) / "memory" / ".staging" / "t2_jobs" / job_id


def _episode_staging_dir(root: Path, agent_id: uuid.UUID | str, job_id: str) -> Path:
    return root / str(agent_id) / "memory" / ".staging" / "t2_episode_jobs" / job_id


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


_JOB_CARRY_OVER_KEYS = ("retry_count", "last_retry_at", "retry_exhausted_alerted_at", "recovered_from")


def _carry_over_job_fields(manifest_path: Path) -> dict[str, Any]:
    """Preserve sweep bookkeeping fields across job re-runs (C9-1)."""
    if not manifest_path.exists():
        return {}
    try:
        existing = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, ValueError, TypeError):
        return {}
    if not isinstance(existing, dict):
        return {}
    return {key: existing[key] for key in _JOB_CARRY_OVER_KEYS if key in existing}


def _stable_t2_package_id(*, session_id: uuid.UUID | str, t0_segment_id: str) -> str:
    digest = hashlib.sha256(f"{session_id}\0{t0_segment_id}".encode("utf-8")).hexdigest()[:16]
    return f"t2pkg-{digest}"


def _stable_t2_job_id(*, session_id: uuid.UUID | str, t0_segment_id: str) -> str:
    digest = hashlib.sha256(f"{session_id}\0{t0_segment_id}".encode("utf-8")).hexdigest()[:16]
    return f"t2job-{digest}"


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
