"""Self-evolution service-level behavior tests (eval-system-spec §2.3).

Demoted from ``app/evals/self_evolution_bakeoff.py``: the scenario execution
kept its assertion value — each test drives REAL service functions in a
throwaway workspace and asserts the observable behavior — but the scoring
shell around it (max_score points, Hermes comparison, absolute gate, CLI
entrypoint) retired with the synthetic-score era. Hive-vs-Hermes comparison
now lives exclusively in the manual milestone bakeoff (J4, live CLI runs).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path
from typing import Any

import pytest


@pytest.fixture(scope="module")
def behavior_workspace(tmp_path_factory) -> dict[str, Any]:
    """Run every scenario once against real services in one throwaway root."""
    data_root = tmp_path_factory.mktemp("self-evolution-behaviors")
    agent_id = uuid.uuid4()
    return {"data_root": data_root, "agent_id": agent_id}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def test_next_turn_adaptation_projects_the_lesson(behavior_workspace) -> None:
    """A user correction becomes an active session-learning projection the
    very next turn — no durable memory or skill promotion required."""
    from app.services.fast_reflection_service import create_fast_reflection_candidate
    from app.services.session_learning import (
        load_session_learning_projections,
        render_active_session_learning_projection,
    )

    data_root = behavior_workspace["data_root"]
    agent_id = behavior_workspace["agent_id"]
    session_id = "behavior-next-turn"

    result = create_fast_reflection_candidate(
        data_root=data_root,
        agent_id=agent_id,
        session_id=session_id,
        messages=[
            {"role": "assistant", "content": "I will use verbose English summaries."},
            {"role": "user", "content": "下次请用简体中文给我短摘要。"},
        ],
        metadata={
            "tenant_id": "tenant-behavior",
            "fast_reflection_classification": {
                "signal_type": "user_preference_correction",
                "lesson": "Use concise Simplified Chinese summaries for this user in the current session.",
                "confidence": 0.99,
            },
        },
    )
    projections = load_session_learning_projections(data_root=data_root, agent_id=agent_id, session_id=session_id)
    rendered = render_active_session_learning_projection(data_root=data_root, agent_id=agent_id, session_id=session_id)

    assert result.get("status") == "candidate_created", result
    assert projections and projections[0].get("promotion_state") == "candidate"
    assert "Session Learning" in rendered and "Simplified Chinese" in rendered


def test_repeated_workflow_creates_verified_skill_candidate(behavior_workspace) -> None:
    """A repeating workflow captures a candidate for reuse instead of writing
    unreviewed durable memory; the candidate passes hard verification."""
    from app.services.fast_reflection_service import create_fast_reflection_candidate

    result = create_fast_reflection_candidate(
        data_root=behavior_workspace["data_root"],
        agent_id=behavior_workspace["agent_id"],
        session_id="behavior-repeated-workflow",
        messages=[{"role": "user", "content": "This workflow repeats: deploy -> canary -> verify -> rollback notes."}],
        metadata={
            "fast_reflection_classification": {
                "signal_type": "repeated_task_pattern",
                "lesson": "deploy -> canary -> verify -> rollback notes",
                "confidence": 1.0,
            },
            "repeated_workflow_signature": "deploy -> canary -> verify -> rollback notes",
        },
    )
    skill_candidate = result.get("skill_candidate") if isinstance(result.get("skill_candidate"), dict) else {}

    assert result.get("status") == "candidate_created", result
    assert skill_candidate.get("status") == "skill_candidate_created", skill_candidate
    assert skill_candidate.get("verification_passed") is True


def test_tool_failure_lesson_routes_to_patch_with_eval_evidence(behavior_workspace) -> None:
    """A verification failure on a loaded skill is captured, routed to the
    patch lane, and leaves an eval_run record in the evolution ledger."""
    from app.services.fast_reflection_service import create_fast_reflection_candidate

    data_root = behavior_workspace["data_root"]
    agent_id = behavior_workspace["agent_id"]

    result = create_fast_reflection_candidate(
        data_root=data_root,
        agent_id=agent_id,
        session_id="behavior-tool-failure",
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
    failure_skill = result.get("skill_candidate") if isinstance(result.get("skill_candidate"), dict) else {}
    ledger_text = _read(Path(data_root) / str(agent_id) / "evolution" / "evolution_ledger.jsonl")

    assert result.get("status") == "candidate_created", result
    assert failure_skill.get("route") == "patch_existing_skill", failure_skill
    assert '"event": "eval_run"' in ledger_text or '"event":"eval_run"' in ledger_text


def test_skill_lifecycle_promotes_and_patches_with_candidate_package(behavior_workspace) -> None:
    """Repeated success promotes; a loaded-skill miss patches; an auditable
    candidate package lands on disk."""
    from app.services.skill_lifecycle import record_skill_execution

    workspace = Path(behavior_workspace["data_root"]) / str(behavior_workspace["agent_id"])

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
    record_skill_execution(
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

    assert promote_result.get("decision") == "promote", json.dumps(promote_result, ensure_ascii=False)
    assert patch_result.get("decision") == "patch", json.dumps(patch_result, ensure_ascii=False)
    assert skill_candidates_dir.exists() and any(
        (package / "candidate_signal.md").exists() and "deploy-checklist" in _read(package / "manifest.json")
        for package in skill_candidates_dir.iterdir()
        if package.is_dir()
    )


def test_long_task_resume_uses_manifests_and_artifact_refs(behavior_workspace) -> None:
    """Resume after context loss reads explicit workspace manifests and
    artifact refs — not prompt memory alone."""
    from app.services.harness_contract import write_workspace_manifest
    from app.services.long_task_runtime import (
        append_long_task_progress_artifact,
        build_long_task_resume_context,
        write_long_task_plan_artifact,
    )

    data_root = behavior_workspace["data_root"]
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
    long_task_workspace = Path(data_root) / str(long_task_agent)
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

    assert plan_artifact.get("path") and (long_task_workspace / plan_artifact["path"]).exists()
    assert resume_context.get("artifact_refs")
    assert "Drafted canary section" in resume_context.get("resume_prompt", "")


def test_safety_tenant_policy_gates_hold() -> None:
    """Evolution manifests validate; PL4 credentials refuse; company-boundary
    conflicts escalate to the company admin; skill guard blocks secrets."""
    from app.services.action_preflight import (
        ActionPreflightInput,
        ActionPreflightService,
        BoundaryAxisLevel,
        CharterZone,
        PreflightDecision,
    )
    from app.services.evolution_manifest import build_evolution_manifest, validate_evolution_manifest
    from app.services.privacy_layer import SensitivityLevel
    from app.services.skill_guard import scan_skill_files

    manifest = build_evolution_manifest(
        change_type="patch",
        target_type="skill",
        target_id="incident-response",
        source_refs=["runtime_task:behavior"],
        trace_refs=["trace:behavior"],
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
        source="self_evolution_behaviors",
    )

    assert validate_evolution_manifest(manifest) == []
    assert pl4_result.decision == PreflightDecision.REFUSE
    assert conflict_result.decision == PreflightDecision.ESCALATE
    assert conflict_result.escalation_target == "company_admin"
    assert guard_report.allowed is False
    assert any(finding.category == "secret_material" for finding in guard_report.findings)


def test_learning_surfaces_stay_observable() -> None:
    """Structural guards that survived the scoring shell: session learning
    stays a dynamic-suffix concern (cache-safe) and tool preflight stays
    visible in the runtime tests. (The rerank latency guard retired with the
    zero-LLM read side — its subject no longer exists.)"""
    repo_root = Path(__file__).resolve().parents[3]
    prompt_cache_text = _read(repo_root / "backend/tests/runtime/test_prompt_cache.py")
    preflight_text = _read(repo_root / "backend/tests/tools/test_tool_runtime_preflight.py")

    assert "session_learning_projection" in prompt_cache_text
    assert "external_visible" in preflight_text or "ActionPreflight" in preflight_text
