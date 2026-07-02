from __future__ import annotations

from pathlib import Path


RETIRED_PATHS = (
    "backend/app/api/eval_ci.py",
    "backend/app/services/eval_ci_service.py",
    "backend/app/services/tenant_behavior_eval_publisher.py",
    "backend/app/evals/hive_live_runner.py",
    "backend/app/evals/baseline.py",
    "backend/app/evals/baselines",
    "backend/app/evals/update_behavior_baseline.py",
    "backend/app/evals/behavior_eval_evidence.py",
    "backend/app/evals/ci_gate.py",
    "backend/app/evals/cost_budget.py",
    "backend/app/evals/hermes_baseline.py",
    "frontend/src/pages/workspace/WorkspaceEvalCiSection.tsx",
    "frontend/src/pages/workspace/WorkspaceEvalCiSection.test.tsx",
)

RETIRED_STRINGS = (
    "HIVE_EVAL_",
    "BEHAVIOR_EVAL_AUTO_PUBLISH_ENABLED",
    "BEHAVIOR_EVAL_REPORT_MAX_AGE_HOURS",
    "app.api.eval_ci",
    "app.services.eval_ci_service",
    "tenant_behavior_eval_publisher",
    "hive_live_runner",
    "behavior_eval_passed",
    "decide_behavior_gated_promotion",
    "/eval-ci/",
)


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def test_retired_eval_ci_files_are_absent() -> None:
    root = _repo_root()

    for relative in RETIRED_PATHS:
        assert not (root / relative).exists(), relative


def test_product_runtime_sources_do_not_reference_retired_eval_ci_surface() -> None:
    root = _repo_root()
    sources = (
        root / "backend/app",
        root / "frontend/src",
        root / ".github/workflows/harness-ci.yml",
    )
    findings: list[str] = []

    for source in sources:
        files = [source] if source.is_file() else list(source.rglob("*"))
        for path in files:
            if not path.is_file() or path.suffix not in {".py", ".ts", ".tsx", ".yml", ".yaml"}:
                continue
            text = path.read_text(encoding="utf-8")
            for needle in RETIRED_STRINGS:
                if needle in text:
                    findings.append(f"{path.relative_to(root)} contains {needle}")

    assert findings == []
