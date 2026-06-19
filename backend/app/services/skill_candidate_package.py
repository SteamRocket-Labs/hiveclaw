"""Shared staging helpers for inactive Skill Candidate Packages."""

from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


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
        "## Required Checks\n"
        + "\n".join(checks)
        + "\n"
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
) -> dict[str, Any]:
    """Stage an inactive candidate package. Active skills are never written here."""

    if draft_filename not in {"SKILL.md.draft", "candidate_signal.md"}:
        raise ValueError("draft_filename must be SKILL.md.draft or candidate_signal.md")
    package_dir = workspace / "evolution" / "skill_candidates" / candidate_id
    package_dir.mkdir(parents=True, exist_ok=True)
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

    (package_dir / "skill_pitch.md").write_text(skill_pitch, encoding="utf-8")
    (package_dir / draft_filename).write_text(rendered_markdown, encoding="utf-8")
    (package_dir / "eval_plan.md").write_text(eval_plan, encoding="utf-8")
    (package_dir / "failure_cases.md").write_text(failure_cases, encoding="utf-8")

    draft_path = f"evolution/skill_candidates/{candidate_id}/{draft_filename}"
    manifest = {
        "schema": "skill_candidate_package.v1",
        "candidate_id": candidate_id,
        "skill_name": skill_name,
        "package_type": package_type,
        "status": status,
        "target_path": target_path,
        "draft_path": draft_path if draft_filename == "SKILL.md.draft" else None,
        "candidate_signal_path": draft_path if draft_filename == "candidate_signal.md" else None,
        "pitch_path": f"evolution/skill_candidates/{candidate_id}/skill_pitch.md",
        "eval_plan_path": f"evolution/skill_candidates/{candidate_id}/eval_plan.md",
        "failure_cases_path": f"evolution/skill_candidates/{candidate_id}/failure_cases.md",
        "source_refs": source_refs,
        "declared_tools": list(declared_tools),
        "declared_packs": list(declared_packs),
        "created_at": _now_iso(),
        "draft_sha256": hashlib.sha256(rendered_markdown.encode("utf-8")).hexdigest(),
        "metadata": extra_metadata or {},
    }
    (package_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return manifest


def update_skill_candidate_package_status(
    *,
    workspace: Path,
    candidate_id: str,
    status: str,
    reason: str | None = None,
    extra_metadata: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    manifest_path = workspace / "evolution" / "skill_candidates" / candidate_id / "manifest.json"
    if not manifest_path.exists():
        return None
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None
    manifest["status"] = status
    manifest["updated_at"] = _now_iso()
    if reason:
        manifest["status_reason"] = reason
    if extra_metadata:
        manifest.setdefault("metadata", {}).update(extra_metadata)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return manifest
