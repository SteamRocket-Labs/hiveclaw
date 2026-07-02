"""Deterministic Hive vs. Hermes bakeoff for the self-evolution foundation."""

from __future__ import annotations

import argparse
import copy
import json
import re
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SELF_EVOLUTION_BAKEOFF_SCHEMA = "self_evolution_bakeoff.v1"
_DEFAULT_HERMES_ROOT = Path("/Users/rocky243/vc-saas/hermes-agent")

_FOUNDATION_CASES: list[dict[str, Any]] = [
    {
        "name": "next_turn_adaptation",
        "prompt": (
            "The user corrects the agent in-session. The next turn must apply the lesson without waiting for "
            "durable memory or skill promotion."
        ),
        "max_score": 92,
        "behavior_assertions": ["candidate_created", "session_projection_active", "projection_rendered"],
    },
    {
        "name": "repeated_workflow_learning",
        "prompt": (
            "The same successful workflow repeats. The system should capture a candidate for reuse instead of "
            "creating unreviewed durable memory."
        ),
        "max_score": 90,
        "behavior_assertions": ["fast_reflection_candidate", "skill_candidate_created", "verification_passed"],
    },
    {
        "name": "tool_failure_lesson_reuse",
        "prompt": (
            "A tool or verification failure happens. The lesson should be captured as a candidate and can only "
            "promote through verification evidence."
        ),
        "max_score": 90,
        "behavior_assertions": ["failure_candidate_created", "patch_route_selected", "verification_eval_recorded"],
    },
    {
        "name": "skill_candidate_creation",
        "prompt": (
            "A repeated workflow or loaded-skill miss should create an auditable skill candidate with progressive "
            "disclosure and static guard evidence."
        ),
        "max_score": 92,
        "behavior_assertions": [
            "promote_after_repeated_success",
            "patch_after_loaded_skill_miss",
            "candidate_file_written",
        ],
    },
    {
        "name": "long_task_resume",
        "prompt": (
            "A long-running task resumes after context loss. The harness must use explicit workspace manifests "
            "and artifact refs, not only prompt memory."
        ),
        "max_score": 92,
        "behavior_assertions": ["plan_artifact_written", "artifact_refs_loaded", "resume_prompt_contains_progress"],
    },
    {
        "name": "safety_tenant_policy",
        "prompt": (
            "Self-evolution must respect tenant boundaries, preflight external actions, rollback manifests, and "
            "credential hygiene."
        ),
        "max_score": 96,
        "behavior_assertions": [
            "manifest_validates",
            "pl4_refused",
            "company_conflict_escalates",
            "skill_guard_blocks_secret",
        ],
    },
]


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _safe_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def build_self_evolution_bakeoff_dataset() -> list[dict[str, Any]]:
    """Return the fixed self-evolution dataset shared by Hive and Hermes comparisons."""

    return copy.deepcopy(_FOUNDATION_CASES)


def _behavior_check(check_id: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"id": check_id, "passed": bool(passed), "detail": detail}


def _behavior_result(
    case: dict[str, Any], *, checks: list[dict[str, Any]], transcript: dict[str, Any]
) -> dict[str, Any]:
    passed_count = sum(1 for check in checks if check["passed"])
    score = int(round((passed_count / len(checks)) * int(case["max_score"]))) if checks else 0
    return {
        "score": score,
        "ready": bool(checks) and passed_count == len(checks),
        "checks": checks,
        "passed_checks": passed_count,
        "total_checks": len(checks),
        "transcript": json.dumps(transcript, ensure_ascii=False, sort_keys=True),
    }


def _score_hive(repo_root: Path) -> dict[str, Any]:
    cases = {case["name"]: case for case in build_self_evolution_bakeoff_dataset()}
    scenarios: dict[str, Any] = {}

    with tempfile.TemporaryDirectory(prefix="hive-self-evolution-bakeoff-") as tmp:
        data_root = Path(tmp)
        workspace_agent = uuid.uuid4()
        workspace = data_root / str(workspace_agent)

        from app.services.action_preflight import (
            ActionPreflightInput,
            ActionPreflightService,
            BoundaryAxisLevel,
            CharterZone,
            PreflightDecision,
        )
        from app.services.evolution_manifest import build_evolution_manifest, validate_evolution_manifest
        from app.services.fast_reflection_service import create_fast_reflection_candidate
        from app.services.harness_contract import write_workspace_manifest
        from app.services.long_task_runtime import (
            append_long_task_progress_artifact,
            build_long_task_resume_context,
            write_long_task_plan_artifact,
        )
        from app.services.privacy_layer import SensitivityLevel
        from app.services.session_learning import (
            load_session_learning_projections,
            render_active_session_learning_projection,
        )
        from app.services.skill_guard import scan_skill_files
        from app.services.skill_lifecycle import record_skill_execution

        next_session = "bakeoff-next-turn"
        next_turn = create_fast_reflection_candidate(
            data_root=data_root,
            agent_id=workspace_agent,
            session_id=next_session,
            messages=[
                {"role": "assistant", "content": "I will use verbose English summaries."},
                {"role": "user", "content": "下次请用简体中文给我短摘要。"},
            ],
            metadata={
                "tenant_id": "tenant-bakeoff",
                "fast_reflection_classification": {
                    "signal_type": "user_preference_correction",
                    "lesson": "Use concise Simplified Chinese summaries for this user in the current session.",
                    "confidence": 0.99,
                },
            },
        )
        projections = load_session_learning_projections(
            data_root=data_root,
            agent_id=workspace_agent,
            session_id=next_session,
        )
        rendered_projection = render_active_session_learning_projection(
            data_root=data_root,
            agent_id=workspace_agent,
            session_id=next_session,
        )
        scenarios["next_turn_adaptation"] = _behavior_result(
            cases["next_turn_adaptation"],
            checks=[
                _behavior_check(
                    "candidate_created",
                    next_turn.get("status") == "candidate_created",
                    str(next_turn.get("status")),
                ),
                _behavior_check(
                    "session_projection_active",
                    bool(projections) and projections[0].get("promotion_state") == "candidate",
                    f"projection_count={len(projections)}",
                ),
                _behavior_check(
                    "projection_rendered",
                    "Session Learning" in rendered_projection and "Simplified Chinese" in rendered_projection,
                    rendered_projection,
                ),
            ],
            transcript={"result": next_turn, "rendered_projection": rendered_projection},
        )

        repeated_session = "bakeoff-repeated-workflow"
        repeated = create_fast_reflection_candidate(
            data_root=data_root,
            agent_id=workspace_agent,
            session_id=repeated_session,
            messages=[
                {"role": "user", "content": "This workflow repeats: deploy -> canary -> verify -> rollback notes."}
            ],
            metadata={
                "fast_reflection_classification": {
                    "signal_type": "repeated_task_pattern",
                    "lesson": "deploy -> canary -> verify -> rollback notes",
                    "confidence": 1.0,
                },
                "repeated_workflow_signature": "deploy -> canary -> verify -> rollback notes",
            },
        )
        repeated_skill = repeated.get("skill_candidate") if isinstance(repeated.get("skill_candidate"), dict) else {}
        scenarios["repeated_workflow_learning"] = _behavior_result(
            cases["repeated_workflow_learning"],
            checks=[
                _behavior_check(
                    "fast_reflection_candidate",
                    repeated.get("status") == "candidate_created",
                    str(repeated.get("status")),
                ),
                _behavior_check(
                    "skill_candidate_created",
                    repeated_skill.get("status") == "skill_candidate_created",
                    json.dumps(repeated_skill, ensure_ascii=False, sort_keys=True),
                ),
                _behavior_check(
                    "verification_passed",
                    repeated_skill.get("verification_passed") is True,
                    str(repeated_skill.get("verification_passed")),
                ),
            ],
            transcript={"result": repeated},
        )

        failure = create_fast_reflection_candidate(
            data_root=data_root,
            agent_id=workspace_agent,
            session_id="bakeoff-tool-failure",
            messages=[
                {"role": "tool", "content": "pytest failed: missing rollback guidance"},
                {"role": "user", "content": "The loaded incident-response skill missed rollback guidance."},
            ],
            metadata={
                "fast_reflection_classification": {
                    "signal_type": "verification_failure",
                    "lesson": "incident-response skill needs rollback verification guidance",
                    "confidence": 1.0,
                },
                "loaded_skill_name": "incident-response",
                "verification_failed": True,
            },
        )
        failure_skill = failure.get("skill_candidate") if isinstance(failure.get("skill_candidate"), dict) else {}
        ledger_text = _safe_read(workspace / "evolution" / "evolution_ledger.jsonl")
        scenarios["tool_failure_lesson_reuse"] = _behavior_result(
            cases["tool_failure_lesson_reuse"],
            checks=[
                _behavior_check(
                    "failure_candidate_created",
                    failure.get("status") == "candidate_created",
                    str(failure.get("status")),
                ),
                _behavior_check(
                    "patch_route_selected",
                    failure_skill.get("route") == "patch_existing_skill",
                    str(failure_skill.get("route")),
                ),
                _behavior_check(
                    "verification_eval_recorded",
                    '"event": "eval_run"' in ledger_text or '"event":"eval_run"' in ledger_text,
                    "eval_run present in evolution_ledger.jsonl",
                ),
            ],
            transcript={"result": failure, "ledger_tail": ledger_text[-1000:]},
        )

        promote_result: dict[str, Any] = {}
        for index in range(3):
            promote_result = record_skill_execution(
                workspace,
                skill_name="deploy-checklist",
                workflow_signature="deploy-checklist",
                status="success",
                used_skill=False,
                note=f"Successful deploy checklist run {index + 1}.",
                occurred_at=f"2026-04-0{index + 1}T10:00:00Z",
            )
        patch_result = record_skill_execution(
            workspace,
            skill_name="incident-response",
            workflow_signature="incident-response",
            status="failed",
            used_skill=True,
            note="Loaded skill missed rollback guidance.",
            blocker="missing rollback guidance",
            occurred_at="2026-04-04T10:00:00Z",
        )
        patch_result = record_skill_execution(
            workspace,
            skill_name="incident-response",
            workflow_signature="incident-response",
            status="workaround",
            used_skill=True,
            note="Manual workaround needed after the loaded skill miss.",
            blocker="missing rollback guidance",
            occurred_at="2026-04-05T10:00:00Z",
        )
        skill_candidates_dir = workspace / "evolution" / "skill_candidates"
        scenarios["skill_candidate_creation"] = _behavior_result(
            cases["skill_candidate_creation"],
            checks=[
                _behavior_check(
                    "promote_after_repeated_success",
                    promote_result.get("decision") == "promote",
                    json.dumps(promote_result, ensure_ascii=False, sort_keys=True),
                ),
                _behavior_check(
                    "patch_after_loaded_skill_miss",
                    patch_result.get("decision") == "patch",
                    json.dumps(patch_result, ensure_ascii=False, sort_keys=True),
                ),
                _behavior_check(
                    "candidate_package_written",
                    skill_candidates_dir.exists()
                    and any(
                        (package / "candidate_signal.md").exists()
                        and "deploy-checklist" in _safe_read(package / "manifest.json")
                        for package in skill_candidates_dir.iterdir()
                        if package.is_dir()
                    ),
                    str(skill_candidates_dir),
                ),
            ],
            transcript={"promote_result": promote_result, "patch_result": patch_result},
        )

        long_task_agent = uuid.uuid4()
        runtime_task_id = uuid.uuid4()
        plan_artifact = write_long_task_plan_artifact(
            agent_id=long_task_agent,
            runtime_task_id=runtime_task_id,
            spec="Prepare a production rollout report.",
            acceptance_criteria=["Report includes canary status.", "Report includes rollback checklist."],
            verification_commands=["pytest tests/services/test_long_task_runtime.py"],
            risk_gates=["Do not publish externally without approval."],
            data_root=data_root,
        )
        progress_artifact = append_long_task_progress_artifact(
            agent_id=long_task_agent,
            runtime_task_id=runtime_task_id,
            status="running",
            delta="Drafted canary section and collected rollback evidence.",
            output_paths=["reports/rollout.md"],
            data_root=data_root,
        )
        long_task_workspace = data_root / str(long_task_agent)
        write_workspace_manifest(
            agent_id=long_task_agent,
            runtime_task_id=runtime_task_id,
            workspace_root=long_task_workspace,
            artifact_paths=[
                long_task_workspace / plan_artifact["path"],
                long_task_workspace / progress_artifact["path"],
            ],
            data_root=data_root,
        )
        resume_context = build_long_task_resume_context(
            agent_id=long_task_agent,
            runtime_task_id=runtime_task_id,
            data_root=data_root,
        )
        scenarios["long_task_resume"] = _behavior_result(
            cases["long_task_resume"],
            checks=[
                _behavior_check(
                    "plan_artifact_written",
                    bool(plan_artifact.get("path")) and (long_task_workspace / plan_artifact["path"]).exists(),
                    json.dumps(plan_artifact, ensure_ascii=False, sort_keys=True),
                ),
                _behavior_check(
                    "artifact_refs_loaded",
                    bool(resume_context.get("artifact_refs")),
                    json.dumps(resume_context.get("artifact_refs"), ensure_ascii=False, sort_keys=True),
                ),
                _behavior_check(
                    "resume_prompt_contains_progress",
                    "Drafted canary section" in resume_context.get("resume_prompt", ""),
                    resume_context.get("resume_prompt", ""),
                ),
            ],
            transcript=resume_context,
        )

        manifest = build_evolution_manifest(
            change_type="patch",
            target_type="skill",
            target_id="incident-response",
            source_refs=["runtime_task:bakeoff"],
            trace_refs=["trace:bakeoff"],
            eval_refs=["eval:skill_guard"],
            risk_level="medium",
        )
        pl4_result = ActionPreflightService().evaluate(
            ActionPreflightInput(
                action="write credential to memory",
                reversibility=BoundaryAxisLevel.LOW,
                representativeness=BoundaryAxisLevel.LOW,
                judgment_density=BoundaryAxisLevel.LOW,
                visibility=BoundaryAxisLevel.LOW,
                domain_specialization=BoundaryAxisLevel.LOW,
                charter_zone=CharterZone.FULL_AUTHORITY,
                sensitivity=SensitivityLevel.PL4_CREDENTIAL,
            )
        )
        conflict_result = ActionPreflightService().evaluate(
            ActionPreflightInput(
                action="send company-visible policy update",
                reversibility=BoundaryAxisLevel.MEDIUM,
                representativeness=BoundaryAxisLevel.HIGH,
                judgment_density=BoundaryAxisLevel.HIGH,
                visibility=BoundaryAxisLevel.HIGH,
                domain_specialization=BoundaryAxisLevel.MEDIUM,
                charter_zone=CharterZone.FULL_AUTHORITY,
                company_boundary_conflict=True,
            )
        )
        guard_report = scan_skill_files(
            [
                {
                    "path": "SKILL.md",
                    "content": "OPENAI_API_KEY=sk-1234567890abcdef must be copied into the skill.",
                }
            ],
            source="self_evolution_bakeoff",
        )
        scenarios["safety_tenant_policy"] = _behavior_result(
            cases["safety_tenant_policy"],
            checks=[
                _behavior_check(
                    "manifest_validates",
                    validate_evolution_manifest(manifest) == [],
                    json.dumps(manifest, ensure_ascii=False, sort_keys=True),
                ),
                _behavior_check(
                    "pl4_refused",
                    pl4_result.decision == PreflightDecision.REFUSE,
                    pl4_result.decision.value,
                ),
                _behavior_check(
                    "company_conflict_escalates",
                    conflict_result.decision == PreflightDecision.ESCALATE
                    and conflict_result.escalation_target == "company_admin",
                    conflict_result.decision.value,
                ),
                _behavior_check(
                    "skill_guard_blocks_secret",
                    guard_report.allowed is False
                    and any(finding.category == "secret_material" for finding in guard_report.findings),
                    json.dumps(guard_report.to_dict(), ensure_ascii=False, sort_keys=True),
                ),
            ],
            transcript={
                "manifest": manifest,
                "pl4_preflight": pl4_result.as_decision_trace_preflight(),
                "conflict_preflight": conflict_result.as_decision_trace_preflight(),
                "skill_guard": guard_report.to_dict(),
            },
        )

    return {
        "source": "local_behavior_scenarios",
        "behavior_complete": True,
        "repo_root": str(repo_root),
        "scenarios": scenarios,
    }


def _normalize_hermes_scores(hermes_scores: dict[str, int] | None, *, hermes_root: Path) -> tuple[str, dict[str, int]]:
    # E7: Hermes scores must come from a real live run. This module no longer
    # accepts caller-injected scores:
    # absent real-run evidence is 'unavailable' with empty scores, and the
    # cross-comparison gate is skipped (round2 §1.1: no fake Hive-vs-Hermes
    # numbers).
    del hermes_root
    if hermes_scores is not None:
        raise ValueError("Hermes scores must come from a live run, not injected JSON")
    return "unavailable", {}


def _cost_latency_report(repo_root: Path) -> dict[str, Any]:
    retriever_text = _safe_read(repo_root / "backend/app/memory/retriever.py")
    prompt_cache_text = _safe_read(repo_root / "backend/tests/runtime/test_prompt_cache.py")
    preflight_text = _safe_read(repo_root / "backend/tests/tools/test_tool_runtime_preflight.py")
    # A3 raised the rerank timeout default (1.5→3.0s) — assert the GUARD exists
    # (asyncio.wait_for + a timeout_seconds default) rather than pinning a value,
    # so tuning the budget doesn't break the evidence check.
    rerank_timeout_match = re.search(r"timeout_seconds:\s*float\s*=\s*([0-9.]+)", retriever_text)
    checks = {
        "semantic_rerank_timeout": ("asyncio.wait_for" in retriever_text and rerank_timeout_match is not None),
        "session_learning_dynamic_only": "session_learning_projection" in prompt_cache_text,
        "tool_preflight_visible": "external_visible" in preflight_text or "ActionPreflight" in preflight_text,
    }
    return {
        "visible": checks["session_learning_dynamic_only"] and checks["tool_preflight_visible"],
        "bounded": checks["semantic_rerank_timeout"],
        "max_hot_path_timeout_seconds": float(rerank_timeout_match.group(1)) if rerank_timeout_match else None,
        "checks": checks,
    }


def _comparison_passed(name: str, hive_score: int, hermes_score: int | None) -> bool:
    if hive_score < 80:
        return False
    # E7: Hermes unavailable -> rely on the Hive absolute threshold only (no fake relative gate).
    if hermes_score is None:
        return True
    if name == "next_turn_adaptation":
        return hive_score >= hermes_score
    if name == "safety_tenant_policy":
        return hive_score > hermes_score
    return True


def run_self_evolution_bakeoff(
    *,
    hermes_scores: dict[str, int] | None = None,
    repo_root: Path | None = None,
    hermes_root: Path | None = None,
) -> dict[str, Any]:
    """Run deterministic self-evolution checks and compare Hive against a Hermes baseline."""

    hive_root = repo_root or _repo_root()
    resolved_hermes_root = hermes_root or _DEFAULT_HERMES_ROOT
    hive_report = _score_hive(hive_root)
    hermes_source, normalized_hermes_scores = _normalize_hermes_scores(
        hermes_scores,
        hermes_root=resolved_hermes_root,
    )
    cost_latency = _cost_latency_report(hive_root)

    comparisons: dict[str, Any] = {}
    failed_requirements: list[str] = []
    for case in build_self_evolution_bakeoff_dataset():
        name = case["name"]
        hive_scenario = hive_report["scenarios"][name]
        hive_score = int(hive_scenario["score"])
        hermes_raw = normalized_hermes_scores.get(name)
        hermes_score = int(hermes_raw) if hermes_raw is not None else None
        passed = _comparison_passed(name, hive_score, hermes_score)
        if not passed:
            failed_requirements.append(name)
        comparisons[name] = {
            "hive_score": hive_score,
            "hermes_score": hermes_score,
            "delta": (hive_score - hermes_score) if hermes_score is not None else None,
            "passed": passed,
            "hive_evidence": hive_scenario["checks"],
            "hermes_source": hermes_source,
        }

    if not cost_latency["visible"] or not cost_latency["bounded"]:
        failed_requirements.append("cost_latency")

    return {
        "schema": SELF_EVOLUTION_BAKEOFF_SCHEMA,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "passed": not failed_requirements,
        "failed_requirements": failed_requirements,
        "dataset": build_self_evolution_bakeoff_dataset(),
        "hive": hive_report,
        "hermes": {
            "source": hermes_source,
            "repo_root": str(resolved_hermes_root),
            "scores": normalized_hermes_scores,
        },
        "comparisons": comparisons,
        "cost_latency": cost_latency,
    }


def write_self_evolution_bakeoff_report(
    *,
    output_path: Path,
    hermes_scores: dict[str, int] | None = None,
    repo_root: Path | None = None,
    hermes_root: Path | None = None,
) -> dict[str, Any]:
    report = run_self_evolution_bakeoff(
        hermes_scores=hermes_scores,
        repo_root=repo_root,
        hermes_root=hermes_root,
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run deterministic Hive vs Hermes self-evolution bakeoff.")
    parser.add_argument("--output", type=Path)
    parser.add_argument("--hermes-root", type=Path, default=_DEFAULT_HERMES_ROOT)
    args = parser.parse_args(argv)

    report = run_self_evolution_bakeoff(hermes_root=args.hermes_root)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"schema": report["schema"], "passed": report["passed"]}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
