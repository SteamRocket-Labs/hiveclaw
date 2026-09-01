from __future__ import annotations

from pathlib import Path


def test_harness_ci_runs_pytest_prompt_eval_and_hard_gates() -> None:
    workflow = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "harness-ci.yml"

    assert workflow.exists()
    source = workflow.read_text(encoding="utf-8")
    assert "pytest" in source
    # retrieval_eval retired at the C7 two-plane cutover; wiki retrieval is
    # covered by tests/memory suites, so CI must not invoke the dead module.
    assert "python -m app.memory.retrieval_eval" not in source
    assert "python -m app.runtime.prompt_eval" in source
    # self_evolution_bakeoff demoted to a plain integration test (spec §2.3):
    # its behavior assertions run inside `pytest tests/evals` now.
    assert "python -m app.evals.self_evolution_bakeoff" not in source
    assert "python -m app.evals.run --suite core_v1 --target hive --mode internal" in source
    assert "python -m app.evals.adversarial_suite" in source


def test_harness_ci_lints_only_changed_python_paths() -> None:
    workflow = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "harness-ci.yml"
    source = workflow.read_text(encoding="utf-8")
    backend_job = source.split("  backend-harness:", 1)[1].split("  atomic-user-journeys:", 1)[0]

    assert "fetch-depth: 0" in backend_job
    assert "git -C .. diff --name-only -z --diff-filter=ACMR" in backend_job
    assert 'ruff check "${python_files[@]}"' in backend_job
    assert 'ruff format --check "${python_files[@]}"' in backend_job
    assert "ruff check app tests" not in backend_job
    assert '"ruff==0.15.12"' in (workflow.parents[2] / "backend" / "pyproject.toml").read_text(encoding="utf-8")


def test_harness_ci_uses_the_locked_backend_environment_and_linux_sandbox() -> None:
    workflow = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "harness-ci.yml"
    source = workflow.read_text(encoding="utf-8")
    backend_job = source.split("  backend-harness:", 1)[1].split("  atomic-user-journeys:", 1)[0]
    atomic_job = source.split("  atomic-user-journeys:", 1)[1]

    assert "uv sync --frozen --extra dev" in backend_job
    assert "uv sync --frozen --extra dev" in atomic_job
    assert "backend/uv.lock" in backend_job
    assert "backend/uv.lock" in atomic_job
    assert "timeout-minutes: 60" in backend_job
    assert "sudo apt-get install --yes apparmor-profiles bubblewrap" in backend_job
    assert "/usr/share/apparmor/extra-profiles/bwrap-userns-restrict" in backend_job
    assert "sudo apparmor_parser -r /etc/apparmor.d/bwrap-userns-restrict" in backend_job
    assert "probe_os_sandbox_capability" in backend_job
    assert "assert probe.available, probe.reason" in backend_job
    assert "kernel.apparmor_restrict_unprivileged_userns=0" not in backend_job


def test_harness_ci_has_no_nightly_behavior_eval_or_eval_environment_secrets() -> None:
    workflow = Path(__file__).resolve().parents[3] / ".github" / "workflows" / "harness-ci.yml"
    source = workflow.read_text(encoding="utf-8")

    assert "workflow_dispatch:" in source
    assert "schedule:" not in source
    assert "github.event_name == 'schedule'" not in source
    assert "railway ssh" not in source
    assert "RAILWAY_TOKEN" not in source
    assert "HIVE_EVAL" not in source
    assert "curl -fsSL https://railway.app/install.sh" not in source
    assert "ssh-keygen -t ed25519" not in source
    assert "/api/eval-ci/behavior" not in source
    assert "python -m app.evals.ci_gate" not in source
    assert "python -m app.evals.behavior_eval_evidence" not in source
