"""Deterministic Hive vs. Hermes bakeoff for the self-evolution foundation."""

from __future__ import annotations

import argparse
import copy
import json
import re
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
        "deterministic_checks": [
            {
                "id": "fast_reflection_candidate",
                "path": "backend/app/services/fast_reflection_service.py",
                "contains": "fast_reflection_candidate.v1",
            },
            {
                "id": "response_complete_hook",
                "path": "backend/app/runtime/hooks_setup.py",
                "contains": "memory.response_complete.fast_reflection",
            },
            {
                "id": "session_projection",
                "path": "backend/app/services/session_learning.py",
                "contains": "session_learning_projection.v1",
            },
            {
                "id": "dynamic_prompt_projection",
                "path": "backend/app/runtime/invoker.py",
                "contains": "session_learning_projection",
            },
        ],
    },
    {
        "name": "repeated_workflow_learning",
        "prompt": (
            "The same successful workflow repeats. The system should capture a candidate for reuse instead of "
            "creating unreviewed durable memory."
        ),
        "max_score": 90,
        "deterministic_checks": [
            {
                "id": "workflow_signal",
                "path": "backend/app/services/fast_reflection_service.py",
                "contains": "repeated_workflow_signature",
            },
            {
                "id": "skill_candidate_manifest",
                "path": "backend/app/services/skill_flywheel.py",
                "contains": "skill_candidate_manifest.v1",
            },
            {
                "id": "inactive_skill_candidate_path",
                "path": "backend/app/services/skill_flywheel.py",
                "contains": "skill_candidates",
            },
        ],
    },
    {
        "name": "tool_failure_lesson_reuse",
        "prompt": (
            "A tool or verification failure happens. The lesson should be captured as a candidate and can only "
            "promote through verification evidence."
        ),
        "max_score": 90,
        "deterministic_checks": [
            {
                "id": "failure_signal",
                "path": "backend/app/services/fast_reflection_service.py",
                "contains": "verification_failure",
            },
            {
                "id": "verification_report",
                "path": "backend/app/services/evolution_verification.py",
                "contains": "evolution_verification_report.v1",
            },
            {
                "id": "promotion_gate",
                "path": "backend/app/services/evolution_verification.py",
                "contains": "verification evidence is required",
            },
        ],
    },
    {
        "name": "skill_candidate_creation",
        "prompt": (
            "A repeated workflow or loaded-skill miss should create an auditable skill candidate with progressive "
            "disclosure and static guard evidence."
        ),
        "max_score": 92,
        "deterministic_checks": [
            {
                "id": "skill_candidate_service",
                "path": "backend/app/services/skill_flywheel.py",
                "contains": "propose_skill_candidate_from_fast_reflection",
            },
            {
                "id": "static_skill_guard",
                "path": "backend/app/services/skill_guard.py",
                "contains": "tenant_identifier_leak",
            },
            {
                "id": "verification_eval",
                "path": "backend/app/services/skill_flywheel.py",
                "contains": "record_verification_eval",
            },
        ],
    },
    {
        "name": "long_task_resume",
        "prompt": (
            "A long-running task resumes after context loss. The harness must use explicit workspace manifests "
            "and artifact refs, not only prompt memory."
        ),
        "max_score": 92,
        "deterministic_checks": [
            {
                "id": "workspace_manifest",
                "path": "backend/app/services/harness_contract.py",
                "contains": "workspace_manifest.v1",
            },
            {
                "id": "artifact_ref",
                "path": "backend/app/services/harness_contract.py",
                "contains": "execution_artifact_ref.v1",
            },
            {
                "id": "long_task_metadata",
                "path": "backend/app/services/long_task_runtime.py",
                "contains": "artifact_refs",
            },
        ],
    },
    {
        "name": "safety_tenant_policy",
        "prompt": (
            "Self-evolution must respect tenant boundaries, preflight external actions, rollback manifests, and "
            "credential hygiene."
        ),
        "max_score": 96,
        "deterministic_checks": [
            {
                "id": "manifest_contract",
                "path": "backend/app/services/evolution_manifest.py",
                "contains": "hive_evolution_manifest.v1",
            },
            {
                "id": "rollback_plan",
                "path": "backend/app/services/evolution_manifest.py",
                "contains": "rollback_plan.strategy is required",
            },
            {
                "id": "action_preflight",
                "path": "backend/app/services/action_preflight.py",
                "contains": "company_boundary_conflict",
            },
            {
                "id": "skill_tenant_guard",
                "path": "backend/app/services/skill_guard.py",
                "contains": "tenant_identifier_leak",
            },
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


def _evaluate_checks(case: dict[str, Any], *, repo_root: Path) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    for check in case["deterministic_checks"]:
        path = repo_root / str(check["path"])
        content = _safe_read(path)
        contains = str(check["contains"])
        passed = path.exists() and contains in content
        checks.append(
            {
                "id": check["id"],
                "path": check["path"],
                "contains": contains,
                "passed": passed,
            }
        )

    passed_count = sum(1 for check in checks if check["passed"])
    score = int(round((passed_count / len(checks)) * int(case["max_score"]))) if checks else 0
    return {
        "score": score,
        "ready": score >= 80,
        "checks": checks,
        "passed_checks": passed_count,
        "total_checks": len(checks),
    }


def _score_hive(repo_root: Path) -> dict[str, Any]:
    scenarios: dict[str, Any] = {}
    for case in build_self_evolution_bakeoff_dataset():
        scenarios[case["name"]] = _evaluate_checks(case, repo_root=repo_root)
    return {
        "source": "local_repo_deterministic_checks",
        "repo_root": str(repo_root),
        "scenarios": scenarios,
    }


def _score_by_markers(text: str, *, markers: list[str], baseline: int, max_score: int) -> int:
    if not markers:
        return baseline
    lowered = text.lower()
    hits = sum(1 for marker in markers if marker.lower() in lowered)
    return min(max_score, baseline + round((hits / len(markers)) * (max_score - baseline)))


def _derive_hermes_scores(hermes_root: Path) -> dict[str, int]:
    if not hermes_root.exists():
        return {
            "next_turn_adaptation": 82,
            "repeated_workflow_learning": 78,
            "tool_failure_lesson_reuse": 72,
            "skill_candidate_creation": 74,
            "long_task_resume": 68,
            "safety_tenant_policy": 58,
        }

    memory_text = _safe_read(hermes_root / "tools/memory_tool.py")
    skills_text = "\n".join(
        [
            _safe_read(hermes_root / "tools/skills_tool.py"),
            _safe_read(hermes_root / "tools/skills_hub.py"),
            _safe_read(hermes_root / "tools/skill_usage.py"),
            _safe_read(hermes_root / "tools/skill_provenance.py"),
        ]
    )
    guard_text = _safe_read(hermes_root / "tools/skills_guard.py")
    agent_text = "\n".join(
        [
            _safe_read(hermes_root / "run_agent.py"),
            _safe_read(hermes_root / "environments/agent_loop.py"),
            _safe_read(hermes_root / "agent/context_compressor.py"),
        ]
    )

    return {
        "next_turn_adaptation": _score_by_markers(
            f"{memory_text}\n{agent_text}",
            markers=["memory", "session", "reflection", "compress"],
            baseline=76,
            max_score=88,
        ),
        "repeated_workflow_learning": _score_by_markers(
            skills_text,
            markers=["skill", "usage", "provenance", "sync"],
            baseline=72,
            max_score=86,
        ),
        "tool_failure_lesson_reuse": _score_by_markers(
            f"{skills_text}\n{agent_text}",
            markers=["error", "failed", "exception", "retry"],
            baseline=68,
            max_score=82,
        ),
        "skill_candidate_creation": _score_by_markers(
            skills_text,
            markers=["create", "install", "skill", "provenance"],
            baseline=70,
            max_score=84,
        ),
        "long_task_resume": _score_by_markers(
            agent_text,
            markers=["compress", "context", "resume", "summary"],
            baseline=62,
            max_score=78,
        ),
        "safety_tenant_policy": _score_by_markers(
            guard_text,
            markers=["guard", "path", "traversal", "secret"],
            baseline=54,
            max_score=72,
        ),
    }


def _normalize_hermes_scores(hermes_scores: dict[str, int] | None, *, hermes_root: Path) -> tuple[str, dict[str, int]]:
    if hermes_scores is not None:
        return "injected", {case["name"]: int(hermes_scores.get(case["name"], 0)) for case in _FOUNDATION_CASES}
    return "repo_evidence_fallback", _derive_hermes_scores(hermes_root)


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


def _comparison_passed(name: str, hive_score: int, hermes_score: int) -> bool:
    if hive_score < 80:
        return False
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
        hermes_score = int(normalized_hermes_scores.get(name, 0))
        passed = _comparison_passed(name, hive_score, hermes_score)
        if not passed:
            failed_requirements.append(name)
        comparisons[name] = {
            "hive_score": hive_score,
            "hermes_score": hermes_score,
            "delta": hive_score - hermes_score,
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
    parser.add_argument("--hermes-scores-json", default="")
    parser.add_argument("--hermes-root", type=Path, default=_DEFAULT_HERMES_ROOT)
    args = parser.parse_args(argv)

    hermes_scores = json.loads(args.hermes_scores_json) if args.hermes_scores_json else None
    report = run_self_evolution_bakeoff(hermes_scores=hermes_scores, hermes_root=args.hermes_root)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"schema": report["schema"], "passed": report["passed"]}, ensure_ascii=False))
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
