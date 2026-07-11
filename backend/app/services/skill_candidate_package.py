"""Shared staging helpers for inactive Skill Candidate Packages."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.skill_evolution_registry import (
    AUTHORING_CONTRACT,
    default_evolvable_for_origin,
    default_origin_for_candidate,
    normalize_skill_origin,
)
from app.services.agent_asset_transaction import AgentAssetTransaction, AssetTransactionCorruptionError


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def render_skill_pitch(
    *,
    skill_name: str,
    reason: str,
    source_refs: list[str],
    target_path: str,
    package_type: str,
) -> str:
    refs = "\n".join(f"- {ref}" for ref in source_refs) or "- source refs unavailable"
    return (
        "# Skill Pitch\n\n"
        f"- skill_name: {skill_name}\n"
        f"- package_type: {package_type}\n"
        f"- target_path: {target_path}\n"
        f"- reason: {reason or 'Repeated evidence supports this candidate.'}\n\n"
        "## Source Refs\n"
        f"{refs}\n"
    )


def render_eval_plan(
    *,
    declared_tools: list[str] | tuple[str, ...],
    declared_packs: list[str] | tuple[str, ...],
    package_type: str,
) -> str:
    has_tools = bool(declared_tools or declared_packs)
    tier = "tool-governance" if has_tools else "prompt-only"
    checks = [
        "- parse LLM-authored SKILL.md.draft frontmatter when present",
        "- treat candidate_signal.md as evidence only, never as an activated skill draft",
        "- run static skill guard",
        "- verify declared tools and packs are allowed",
    ]
    if has_tools:
        checks.append("- run artifact smoke test for tool-governed instructions")
    return (
        "# Skill Eval Plan\n\n"
        f"- package_type: {package_type}\n"
        f"- eval_tier: {tier}\n"
        f"- declared_tools: {', '.join(declared_tools) if declared_tools else 'none'}\n"
        f"- declared_packs: {', '.join(declared_packs) if declared_packs else 'none'}\n\n"
        "## Required Checks\n" + "\n".join(checks) + "\n"
    )


def render_failure_cases(*, package_type: str, declared_tools: list[str] | tuple[str, ...]) -> str:
    cases = [
        "- The draft is too broad and should remain T3 capability memory.",
        "- The draft overlaps an existing skill and should become a patch instead.",
        "- The draft contains session-specific, tenant-specific, or sensitive details.",
    ]
    if declared_tools:
        cases.append("- The draft implies a tool permission that is not declared or not allowed.")
    return "# Failure Cases\n\n" + f"- package_type: {package_type}\n" + "\n".join(cases) + "\n"


def write_skill_candidate_package(
    *,
    workspace: Path,
    candidate_id: str,
    rendered_markdown: str,
    skill_name: str,
    package_type: str,
    target_path: str,
    source_refs: list[str],
    reason: str,
    declared_tools: list[str] | tuple[str, ...],
    declared_packs: list[str] | tuple[str, ...],
    status: str = "candidate",
    extra_metadata: dict[str, Any] | None = None,
    draft_filename: str = "SKILL.md.draft",
    skill_origin: str | None = None,
    evolvable: bool | None = None,
    transaction: AgentAssetTransaction | None = None,
) -> dict[str, Any]:
    """Stage an inactive candidate package. Active skills are never written here."""

    if transaction is None:
        with AgentAssetTransaction(
            workspace,
            operation="skill_candidate_package_write",
            idempotency_key=f"skill-candidate:{candidate_id}",
            evidence_refs=tuple(source_refs),
        ) as own_transaction:
            if own_transaction.is_replay:
                manifest_path = workspace / "evolution" / "skill_candidates" / candidate_id / "manifest.json"
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
                expected_sha = hashlib.sha256(rendered_markdown.encode("utf-8")).hexdigest()
                if (
                    manifest.get("draft_sha256") != expected_sha
                    or manifest.get("target_path") != target_path
                    or manifest.get("skill_name") != skill_name
                ):
                    raise AssetTransactionCorruptionError(
                        f"Skill candidate idempotency key reused with different input: {candidate_id}"
                    )
                return manifest
            manifest = write_skill_candidate_package(
                workspace=workspace,
                candidate_id=candidate_id,
                rendered_markdown=rendered_markdown,
                skill_name=skill_name,
                package_type=package_type,
                target_path=target_path,
                source_refs=source_refs,
                reason=reason,
                declared_tools=declared_tools,
                declared_packs=declared_packs,
                status=status,
                extra_metadata=extra_metadata,
                draft_filename=draft_filename,
                skill_origin=skill_origin,
                evolvable=evolvable,
                transaction=own_transaction,
            )
            own_transaction.commit()
            return manifest

    if draft_filename not in {"SKILL.md.draft", "candidate_signal.md"}:
        raise ValueError("draft_filename must be SKILL.md.draft or candidate_signal.md")
    resolved_origin = normalize_skill_origin(
        skill_origin or default_origin_for_candidate(package_type=package_type, draft_filename=draft_filename)
    )
    origin_evolvable = default_evolvable_for_origin(resolved_origin)
    resolved_evolvable = origin_evolvable if evolvable is None else bool(evolvable) and origin_evolvable
    package_dir = workspace / "evolution" / "skill_candidates" / candidate_id
    skill_pitch = render_skill_pitch(
        skill_name=skill_name,
        reason=reason,
        source_refs=source_refs,
        target_path=target_path,
        package_type=package_type,
    )
    eval_plan = render_eval_plan(
        declared_tools=declared_tools,
        declared_packs=declared_packs,
        package_type=package_type,
    )
    failure_cases = render_failure_cases(package_type=package_type, declared_tools=declared_tools)

    _stage_candidate_text(transaction, workspace, package_dir / "skill_pitch.md", skill_pitch)
    _stage_candidate_text(transaction, workspace, package_dir / draft_filename, rendered_markdown)
    _stage_candidate_text(transaction, workspace, package_dir / "eval_plan.md", eval_plan)
    _stage_candidate_text(transaction, workspace, package_dir / "failure_cases.md", failure_cases)

    draft_path = f"evolution/skill_candidates/{candidate_id}/{draft_filename}"
    manifest = {
        "schema": "skill_candidate_package.v1",
        "candidate_id": candidate_id,
        "skill_name": skill_name,
        "package_type": package_type,
        "status": status,
        "target_path": target_path,
        "skill_origin": resolved_origin,
        "evolvable": resolved_evolvable,
        "authoring_contract": AUTHORING_CONTRACT,
        "draft_path": draft_path if draft_filename == "SKILL.md.draft" else None,
        "candidate_signal_path": draft_path if draft_filename == "candidate_signal.md" else None,
        "pitch_path": f"evolution/skill_candidates/{candidate_id}/skill_pitch.md",
        "eval_plan_path": f"evolution/skill_candidates/{candidate_id}/eval_plan.md",
        "failure_cases_path": f"evolution/skill_candidates/{candidate_id}/failure_cases.md",
        "manifest_path": f"evolution/skill_candidates/{candidate_id}/manifest.json",
        "source_refs": source_refs,
        "declared_tools": list(declared_tools),
        "declared_packs": list(declared_packs),
        "created_at": _now_iso(),
        "draft_sha256": hashlib.sha256(rendered_markdown.encode("utf-8")).hexdigest(),
        "metadata": extra_metadata or {},
    }
    _stage_candidate_text(
        transaction,
        workspace,
        package_dir / "manifest.json",
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return manifest


def write_skill_referee_review(
    *,
    workspace: Path,
    candidate_id: str,
    review_markdown: str,
    review_payload: dict[str, Any],
    transaction: AgentAssetTransaction | None = None,
) -> dict[str, Any] | None:
    """Attach the independent Skill Referee review to an inactive package."""

    package_dir = workspace / "evolution" / "skill_candidates" / candidate_id
    manifest_path = package_dir / "manifest.json"
    if transaction is None:
        with AgentAssetTransaction(workspace, operation="skill_candidate_referee_review") as own_transaction:
            result = write_skill_referee_review(
                workspace=workspace,
                candidate_id=candidate_id,
                review_markdown=review_markdown,
                review_payload=review_payload,
                transaction=own_transaction,
            )
            if own_transaction.has_changes:
                own_transaction.commit()
            return result
    manifest_text = transaction.read_text(manifest_path.relative_to(workspace).as_posix())
    if manifest_text is None:
        return None
    try:
        manifest = json.loads(manifest_text)
    except json.JSONDecodeError:
        return None

    rendered = review_markdown.rstrip() + "\n"
    review_path = package_dir / "referee_review.md"
    _stage_candidate_text(transaction, workspace, review_path, rendered)

    relative_review_path = f"evolution/skill_candidates/{candidate_id}/referee_review.md"
    manifest["referee_review_path"] = relative_review_path
    manifest["referee_review_sha256"] = hashlib.sha256(rendered.encode("utf-8")).hexdigest()
    manifest["updated_at"] = _now_iso()
    manifest.setdefault("metadata", {})["referee_review"] = review_payload
    _stage_candidate_text(
        transaction,
        workspace,
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return manifest


def update_skill_candidate_package_status(
    *,
    workspace: Path,
    candidate_id: str,
    status: str,
    reason: str | None = None,
    extra_metadata: dict[str, Any] | None = None,
    transaction: AgentAssetTransaction | None = None,
) -> dict[str, Any] | None:
    manifest_path = workspace / "evolution" / "skill_candidates" / candidate_id / "manifest.json"
    if transaction is None:
        with AgentAssetTransaction(workspace, operation="skill_candidate_status_update") as own_transaction:
            result = update_skill_candidate_package_status(
                workspace=workspace,
                candidate_id=candidate_id,
                status=status,
                reason=reason,
                extra_metadata=extra_metadata,
                transaction=own_transaction,
            )
            if own_transaction.has_changes:
                own_transaction.commit()
            return result
    manifest_text = transaction.read_text(manifest_path.relative_to(workspace).as_posix())
    if manifest_text is None:
        return None
    try:
        manifest = json.loads(manifest_text)
    except json.JSONDecodeError:
        return None
    manifest["status"] = status
    manifest["updated_at"] = _now_iso()
    if reason:
        manifest["status_reason"] = reason
    if extra_metadata:
        manifest.setdefault("metadata", {}).update(extra_metadata)
    _stage_candidate_text(
        transaction,
        workspace,
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
    )
    return manifest


def _stage_candidate_text(
    transaction: AgentAssetTransaction,
    workspace: Path,
    path: Path,
    content: str,
) -> None:
    transaction.stage_text(path.relative_to(workspace).as_posix(), content)
