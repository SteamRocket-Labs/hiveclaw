from __future__ import annotations

from pathlib import Path


def test_run_eval_suite_writes_json_markdown_and_scenario_artifacts(tmp_path: Path) -> None:
    from app.evals.run import run_eval_suite

    report = run_eval_suite(
        suite="core_v1",
        target="clawith",
        mode="internal",
        ablation="full",
        output_root=tmp_path,
    )

    output_dir = Path(report["output_dir"])
    assert report["suite"] == "core_v1"
    assert report["target"] == "clawith"
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


def test_run_eval_suite_writes_runtime_status_and_fallback_to_markdown(monkeypatch, tmp_path: Path) -> None:
    import app.evals.run as eval_run

    monkeypatch.setattr(
        eval_run,
        "run_runtime_bakeoff",
        lambda target, output_dir: {
            "kind": "bakeoff",
            "transport": "repo_evidence_only",
            "repo_root": "/tmp/claude-code",
            "auth_status": "auth_required",
            "fallback_used": True,
            "benchmark_complete": False,
            "artifact_paths": ["/tmp/claude-code/runtime/auth.txt"],
            "runtime": {"status": "auth_required", "executable": "claude"},
            "fallback": {"transport": "repo_evidence"},
            "scenarios": {
                "coding": {
                    "ready": True,
                    "score": 95,
                    "transcript": "repo evidence",
                    "rubric": "coding assistant maturity",
                    "score_breakdown": {"repo_found": True},
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
    assert "Runtime Status: auth_required" in report_md
    assert "Fallback Transport: repo_evidence" in report_md
    assert "Benchmark Complete: no" in report_md
    assert "Auth Status: auth_required" in report_md


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
    assert "coding: selected=gpt-4.1-mini fallback=gpt-4.1 reason=simple_turn_cheap_model source=agent_config" in report_md
    assert "research: fallback prompt_too_long gpt-4.1 -> claude-sonnet" in report_md


def test_run_eval_suite_supports_continuity_and_skill_internal_suites(tmp_path: Path) -> None:
    from app.evals.run import run_eval_suite

    continuity_report = run_eval_suite(
        suite="continuity_v1",
        target="clawith",
        mode="internal",
        ablation="full",
        output_root=tmp_path,
    )
    skill_report = run_eval_suite(
        suite="skill_v1",
        target="clawith",
        mode="internal",
        ablation="full",
        output_root=tmp_path,
    )

    assert continuity_report["suite"] == "continuity_v1"
    assert continuity_report["summary"]["scenario_count"] == 4
    assert skill_report["suite"] == "skill_v1"
    assert skill_report["summary"]["scenario_count"] == 4
    assert skill_report["summary"]["pass_rate"] == 100.0
