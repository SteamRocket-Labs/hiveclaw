"""Skill candidate flywheel fed by fast reflection signals."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.services.evolution_ledger import record_evolution_candidate
from app.services.evolution_verification import record_verification_eval, run_evolution_verification
from app.services.skill_candidate_package import write_skill_candidate_package
from app.services.skill_guard import scan_skill_files
from app.services.skill_lifecycle import record_skill_lifecycle_event, update_skill_candidate_record


def _slug(value: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9_-]+", "-", value.strip().lower()).strip("-")
    return normalized[:80] or "skill-candidate"


def _render_candidate_skill(
    *,
    skill_name: str,
    route: str,
    lesson: str,
    signal_type: str,
    skill_decision: dict[str, Any],
) -> str:
    return "\n".join(
        [
            "---",
            f"name: {skill_name}",
            "description: Candidate skill draft from fast reflection; review before activation.",
            "tools: []",
            "---",
            "",
            f"# {skill_name}",
            "",
            "## Candidate Lesson",
            lesson.strip() or "Review the linked fast reflection candidate before writing durable instructions.",
            "",
            "## Promotion Notes",
            f"- route: {route}",
            f"- signal_type: {signal_type}",
            "- status: candidate only; do not activate until verification and human review pass.",
            "",
            "## Model Decision Evidence",
            "```json",
            json.dumps(skill_decision, ensure_ascii=False, indent=2, sort_keys=True),
            "```",
            "",
        ]
    )


def propose_skill_candidate_from_fast_reflection(
    *,
    workspace: Path,
    fast_candidate: dict[str, Any],
) -> dict[str, Any]:
    metadata = dict(fast_candidate.get("metadata") or {})
    if metadata.get("schema") != "fast_reflection_candidate.v1":
        return {"status": "skipped", "reason": "not_fast_reflection_candidate"}

    signal_type = str(metadata.get("signal_type") or "")
    lesson = str(metadata.get("lesson") or "").strip()
    decision = metadata.get("learning_brain_decision")
    if not isinstance(decision, dict) or str(decision.get("container") or "") != "skill_candidate":
        return {"status": "skipped", "reason": "model_did_not_nominate_skill"}
    if str(decision.get("promotion_intent") or "") != "candidate":
        return {"status": "skipped", "reason": "model_did_not_nominate_skill"}
    skill_decision = decision.get("skill_decision")
    if not isinstance(skill_decision, dict):
        return {"status": "held", "reason": "incomplete_model_skill_decision"}
    action = str(skill_decision.get("action") or "").strip()
    if action == "new":
        skill_name = str(skill_decision.get("candidate_name") or "").strip()
        route = "new_class_skill"
    elif action == "patch":
        skill_name = str(skill_decision.get("target_skill") or "").strip()
        route = "patch_existing_skill"
    else:
        return {"status": "held", "reason": "incomplete_model_skill_decision"}
    if not skill_name:
        return {"status": "held", "reason": "incomplete_model_skill_decision"}

    draft = _render_candidate_skill(
        skill_name=skill_name,
        route=route,
        lesson=lesson,
        signal_type=signal_type,
        skill_decision=skill_decision,
    )
    guard = scan_skill_files([{"path": "candidate_signal.md", "content": draft}], source="fast_reflection")
    if not guard.allowed:
        record_skill_lifecycle_event(
            workspace,
            skill_name=skill_name,
            status="blocked",
            note="Fast reflection skill candidate blocked by static skill guard.",
        )
        return {"status": "blocked", "reason": "skill_guard_failed", "guard": guard.to_dict()}

    source_attempt_ids = [str(metadata.get("session_id") or fast_candidate.get("candidate_id") or "fast-reflection")]
    candidate = record_evolution_candidate(
        workspace,
        target_type="skill_candidate",
        target_id=skill_name,
        diff=draft,
        source_attempt_ids=source_attempt_ids,
        baseline_version="skill-candidate@draft",
        metadata={
            "schema": "skill_candidate_manifest.v1",
            "fast_reflection_candidate_id": fast_candidate.get("candidate_id"),
            "route": route,
            "signal_type": signal_type,
            "model_skill_decision": skill_decision,
            "guard": guard.to_dict(),
            "progressive_disclosure": {
                "kind": "candidate_summary",
                "summary": lesson,
                "declared_tools": [],
                "support_files": ["candidate_signal.md"],
            },
        },
    )

    manifest = write_skill_candidate_package(
        workspace=workspace,
        candidate_id=candidate["candidate_id"],
        rendered_markdown=draft,
        skill_name=skill_name,
        package_type=route,
        target_path=f"skills/{_slug(skill_name)}/SKILL.md"
        if route == "new_class_skill"
        else f"skills/{_slug(skill_name)}/SKILL.md",
        source_refs=source_attempt_ids,
        reason=lesson or f"Created from fast reflection route={route}",
        declared_tools=[],
        declared_packs=[],
        status="candidate",
        extra_metadata={"fast_reflection_candidate_id": fast_candidate.get("candidate_id"), "signal_type": signal_type},
        draft_filename="candidate_signal.md",
    )

    verification_report = run_evolution_verification(
        workspace=workspace,
        candidate=candidate,
        graders=[
            {
                "type": "skill_guard",
                "path": manifest["candidate_signal_path"],
            }
        ],
    )
    record_verification_eval(workspace, candidate=candidate, verification_report=verification_report)
    update_skill_candidate_record(
        workspace,
        workflow_signature=str(fast_candidate.get("candidate_id") or skill_name),
        skill_name=skill_name,
        last_status="candidate",
        last_note=f"Created via fast reflection route={route}",
    )
    record_skill_lifecycle_event(
        workspace,
        skill_name=skill_name,
        status="candidate",
        note=f"Created skill candidate from fast reflection ({route}).",
    )

    return {
        "status": "skill_candidate_created",
        "candidate_id": candidate["candidate_id"],
        "skill_name": skill_name,
        "route": route,
        "guard": guard.to_dict(),
        "verification_passed": verification_report["passed"],
    }
