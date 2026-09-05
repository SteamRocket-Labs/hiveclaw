from __future__ import annotations

from pathlib import Path


def test_output_directory_is_unique_for_two_runs_in_the_same_second(monkeypatch, tmp_path: Path) -> None:
    import app.evals.run as eval_run

    fixed = eval_run.datetime(2026, 8, 31, 1, 2, 3, tzinfo=eval_run.timezone.utc)

    class FrozenDateTime:
        @staticmethod
        def now(_timezone):
            return fixed

    monkeypatch.setattr(eval_run, "datetime", FrozenDateTime)

    first = eval_run._resolve_output_dir(
        suite="core_v1", target="hive", mode="bakeoff", ablation="full", output_root=tmp_path
    )
    second = eval_run._resolve_output_dir(
        suite="core_v1", target="hive", mode="bakeoff", ablation="full", output_root=tmp_path
    )

    assert first != second


def test_run_eval_suite_writes_json_markdown_and_scenario_artifacts(tmp_path: Path) -> None:
    from app.evals.run import run_eval_suite

    report = run_eval_suite(
        suite="core_v1",
        target="hive",
        mode="internal",
        ablation="full",
        output_root=tmp_path,
    )

    output_dir = Path(report["output_dir"])
    assert report["suite"] == "core_v1"
    assert report["target"] == "hive"
    assert report["mode"] == "internal"
    assert report["summary"]["scenario_count"] == 8
    assert report["analysis"]["strengths"]
    assert "gaps" in report["analysis"]
    assert (output_dir / "report.json").exists()
    assert (output_dir / "report.md").exists()
    assert (output_dir / "scenarios" / "coding.json").exists()
    assert (output_dir / "scenarios" / "long_context_after_compaction.json").exists()
    report_md = (output_dir / "report.md").read_text(encoding="utf-8")
    assert "Strengths" in report_md
    assert "Gaps" in report_md


def test_eval_main_accepts_bakeoff_targets_and_ablation_variants(tmp_path: Path) -> None:
    import app.evals.run as eval_run

    def fake_bakeoff(target: str, output_dir: Path):
        return {
            "kind": "bakeoff",
            "transport": "live_cli",
            "repo_root": f"/tmp/{target}",
            "auth_status": "ok",
            "fallback_used": False,
            "benchmark_complete": True,
            "artifact_paths": [str(output_dir / "runtime" / "coding" / "stdout.txt")],
            "runtime": {"status": "completed", "executable": "claude"},
            "scenarios": {
                "coding": {
                    "ready": True,
                    "score": 100,
                    "transcript": "live runtime",
                    "rubric": "coding assistant maturity",
                    "score_breakdown": {"transport": "live_cli"},
                }
            },
        }

    original = eval_run.run_runtime_bakeoff
    eval_run.run_runtime_bakeoff = fake_bakeoff
    try:
        exit_code = eval_run.main(
            ["--suite", "core_v1", "--target", "claude_code", "--mode", "bakeoff"],
            output_root=tmp_path,
        )
    finally:
        eval_run.run_runtime_bakeoff = original

    assert exit_code == 0


def test_run_eval_suite_reports_unavailable_runtime_without_fake_scores(monkeypatch, tmp_path: Path) -> None:
    """Spec §2.4: no repo_evidence fake-score fallback. An unavailable CLI
    yields an honest empty-scenario report — never synthesized scores."""
    import app.evals.run as eval_run

    monkeypatch.setattr(
        eval_run,
        "run_runtime_bakeoff",
        lambda target, output_dir: {
            "kind": "bakeoff",
            "transport": "runtime_unavailable",
            "auth_status": "auth_required",
            "benchmark_complete": False,
            "artifact_paths": ["/tmp/claude-code/runtime/auth.txt"],
            "runtime": {"status": "auth_required", "executable": "claude"},
            "scenarios": {},
        },
    )

    report = eval_run.run_eval_suite(
        suite="core_v1",
        target="claude_code",
        mode="bakeoff",
        ablation="full",
        output_root=tmp_path,
    )

    report_md = (Path(report["output_dir"]) / "report.md").read_text(encoding="utf-8")
    assert "Runtime Status: auth_required" in report_md
    assert "Benchmark Complete: no" in report_md
    assert "Auth Status: auth_required" in report_md
    assert "repo_evidence" not in report_md
    assert report["scenarios"] == {}


def test_bakeoff_runtime_has_no_fake_score_fallback() -> None:
    """The fake-score machinery must be physically gone, not just unused."""
    import inspect

    import app.evals.bakeoff_runtime as bakeoff_runtime

    source = inspect.getsource(bakeoff_runtime)
    assert "repo_evidence" not in source
    assert "_fallback_report" not in source


def test_run_eval_suite_writes_live_bakeoff_markdown_without_fallback(monkeypatch, tmp_path: Path) -> None:
    import app.evals.run as eval_run

    monkeypatch.setattr(
        eval_run,
        "run_runtime_bakeoff",
        lambda target, output_dir: {
            "kind": "bakeoff",
            "transport": "live_cli",
            "repo_root": "/tmp/claude-code",
            "auth_status": "ok",
            "fallback_used": False,
            "benchmark_complete": True,
            "artifact_paths": ["/tmp/claude-code/runtime/coding/stdout.txt"],
            "runtime": {"status": "completed", "executable": "claude"},
            "fallback": None,
            "scenarios": {
                "coding": {
                    "ready": True,
                    "score": 100,
                    "transcript": "live runtime",
                    "rubric": "coding assistant maturity",
                    "score_breakdown": {"transport": "live_cli"},
                }
            },
        },
    )

    report = eval_run.run_eval_suite(
        suite="core_v1",
        target="claude_code",
        mode="bakeoff",
        ablation="full",
        output_root=tmp_path,
    )

    report_md = (Path(report["output_dir"]) / "report.md").read_text(encoding="utf-8")
    assert "Runtime Status: completed" in report_md
    assert "Benchmark Complete: yes" in report_md
    assert "Fallback Transport:" not in report_md


def test_run_eval_suite_writes_partial_live_bakeoff_markdown(monkeypatch, tmp_path: Path) -> None:
    import app.evals.run as eval_run

    monkeypatch.setattr(
        eval_run,
        "run_runtime_bakeoff",
        lambda target, output_dir: {
            "kind": "bakeoff",
            "transport": "live_cli_partial",
            "repo_root": "/tmp/hermes-agent",
            "auth_status": "ok",
            "fallback_used": False,
            "benchmark_complete": False,
            "artifact_paths": ["/tmp/hermes-agent/runtime/coding/stdout.txt"],
            "runtime": {"status": "partial", "executable": "hermes"},
            "incomplete_scenarios": [{"scenario": "coding", "reason": "timeout_partial"}],
            "fallback": None,
            "scenarios": {
                "coding": {
                    "ready": True,
                    "score": 80,
                    "transcript": "partial runtime",
                    "rubric": "coding assistant maturity",
                    "score_breakdown": {"transport": "live_cli", "reason": "timeout_partial", "timeout": True},
                }
            },
        },
    )

    report = eval_run.run_eval_suite(
        suite="core_v1",
        target="hermes_agent",
        mode="bakeoff",
        ablation="full",
        output_root=tmp_path,
    )

    report_md = (Path(report["output_dir"]) / "report.md").read_text(encoding="utf-8")
    assert "Runtime Status: partial" in report_md
    assert "Benchmark Complete: no" in report_md
    assert "Incomplete Scenarios: coding(timeout_partial)" in report_md
    assert "Fallback Transport:" not in report_md


def test_run_eval_suite_writes_route_observations_to_markdown(monkeypatch, tmp_path: Path) -> None:
    import app.evals.run as eval_run

    monkeypatch.setattr(
        eval_run,
        "run_runtime_bakeoff",
        lambda target, output_dir: {
            "kind": "bakeoff",
            "transport": "live_cli",
            "repo_root": "/tmp/claude-code",
            "auth_status": "ok",
            "fallback_used": False,
            "benchmark_complete": True,
            "artifact_paths": ["/tmp/claude-code/runtime/coding/stdout.txt"],
            "runtime": {"status": "completed", "executable": "claude"},
            "route_observations": [
                {
                    "scenario": "coding",
                    "selected_model": "gpt-4.1-mini",
                    "fallback_model": "gpt-4.1",
                    "reason": "simple_turn_cheap_model",
                    "config_source": "agent_config",
                },
                {
                    "scenario": "research",
                    "fallback_reason": "prompt_too_long",
                    "from_model": "gpt-4.1",
                    "to_model": "claude-sonnet",
                },
            ],
            "fallback": None,
            "scenarios": {
                "coding": {
                    "ready": True,
                    "score": 100,
                    "transcript": "live runtime",
                    "rubric": "coding assistant maturity",
                    "score_breakdown": {"transport": "live_cli"},
                }
            },
        },
    )

    report = eval_run.run_eval_suite(
        suite="core_v1",
        target="claude_code",
        mode="bakeoff",
        ablation="full",
        output_root=tmp_path,
    )

    report_md = (Path(report["output_dir"]) / "report.md").read_text(encoding="utf-8")
    assert "Runtime Routing" in report_md
    assert (
        "coding: selected=gpt-4.1-mini fallback=gpt-4.1 reason=simple_turn_cheap_model source=agent_config" in report_md
    )
    assert "research: fallback prompt_too_long gpt-4.1 -> claude-sonnet" in report_md


def test_run_eval_suite_supports_continuity_and_skill_internal_suites(tmp_path: Path) -> None:
    from app.evals.run import run_eval_suite

    continuity_report = run_eval_suite(
        suite="continuity_v1",
        target="hive",
        mode="internal",
        ablation="full",
        output_root=tmp_path,
    )
    skill_report = run_eval_suite(
        suite="skill_v1",
        target="hive",
        mode="internal",
        ablation="full",
        output_root=tmp_path,
    )

    assert continuity_report["suite"] == "continuity_v1"
    assert continuity_report["summary"]["scenario_count"] == 4
    assert skill_report["suite"] == "skill_v1"
    assert skill_report["summary"]["scenario_count"] == 4
    assert skill_report["summary"]["pass_rate"] == 100.0


def test_j4_cli_reads_only_the_explicitly_named_bearer_env(monkeypatch, tmp_path: Path) -> None:
    import app.evals.run as eval_run

    captured: list[dict[str, object]] = []

    def fake_run_eval_suite(**kwargs):
        captured.append(kwargs)
        accepted = bool(kwargs["j4_config"].hive_bearer)
        return {
            "suite": kwargs["suite"],
            "target": kwargs["target"],
            "mode": kwargs["mode"],
            "ablation": kwargs["ablation"],
            "summary": {"average_score": 0.0, "pass_rate": 0.0},
            "output_dir": str(tmp_path),
            "benchmark_complete": True,
            "acceptance_ready": accepted,
        }

    monkeypatch.setattr(eval_run, "run_eval_suite", fake_run_eval_suite)
    monkeypatch.setenv("HIVE_BEARER", "implicit-secret")
    monkeypatch.setenv("EXPLICIT_J4_TOKEN", "explicit-secret")

    assert (
        eval_run.main(
            ["--suite", "core_v1", "--target", "hive", "--mode", "bakeoff", "--j4-same-envelope"],
            output_root=tmp_path,
        )
        == 1
    )
    assert captured[-1]["j4_config"].hive_bearer is None

    assert (
        eval_run.main(
            [
                "--suite",
                "core_v1",
                "--target",
                "hive",
                "--mode",
                "bakeoff",
                "--j4-same-envelope",
                "--hive-bearer-env",
                "EXPLICIT_J4_TOKEN",
                "--freecode-build-manifest",
                "/frozen/freecode/build-manifest.json",
                "--freecode-build-manifest-sha256",
                "d" * 64,
                "--hermes-python",
                "/frozen/hermes/python",
                "--hermes-python-sha256",
                "a" * 64,
                "--hermes-python-environment-sha256",
                "c" * 64,
                "--hermes-source-root",
                "/frozen/hermes/source",
                "--hermes-source-revision",
                "frozen-revision",
                "--hermes-source-sha256",
                "b" * 64,
                "--hermes-freeze-root",
                "/frozen/hermes/runtime-copies",
                "--hermes-auth-store",
                "/frozen/hermes/auth-store.json",
                "--hermes-auth-store-sha256",
                "e" * 64,
            ],
            output_root=tmp_path,
        )
        == 0
    )
    assert captured[-1]["j4_config"].hive_bearer == "explicit-secret"
    assert captured[-1]["j4_config"].freecode_build_manifest == "/frozen/freecode/build-manifest.json"
    assert captured[-1]["j4_config"].freecode_build_manifest_sha256 == "d" * 64
    assert captured[-1]["j4_config"].hermes_python == "/frozen/hermes/python"
    assert captured[-1]["j4_config"].hermes_python_sha256 == "a" * 64
    assert captured[-1]["j4_config"].hermes_python_environment_sha256 == "c" * 64
    assert captured[-1]["j4_config"].hermes_source_root == "/frozen/hermes/source"
    assert captured[-1]["j4_config"].hermes_source_revision == "frozen-revision"
    assert captured[-1]["j4_config"].hermes_source_sha256 == "b" * 64
    assert captured[-1]["j4_config"].hermes_freeze_root == "/frozen/hermes/runtime-copies"
    # Synthetic paths only: the credential-store flags must reach the J4 config
    # without this test ever reading an actual credential file.
    assert captured[-1]["j4_config"].hermes_auth_store == "/frozen/hermes/auth-store.json"
    assert captured[-1]["j4_config"].hermes_auth_store_sha256 == "e" * 64
