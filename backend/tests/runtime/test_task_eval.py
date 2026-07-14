from __future__ import annotations

from app.runtime.prompt_eval import PromptEvalInputs


def test_evaluate_task_readiness_reports_core_scenarios_ready() -> None:
    from app.runtime.task_eval import evaluate_task_readiness

    report = evaluate_task_readiness()

    assert report["summary"]["failing_scenarios"] == 0
    assert report["summary"]["ready_scenarios"] >= 5
    assert report["benchmark_summary"]["failing_cases"] == 0
    assert report["benchmark_summary"]["passed_cases"] >= 8
    assert report["scenarios"]["research"]["ready"] is True
    assert report["scenarios"]["research"]["task_profile"] == "model_owned"
    assert report["scenarios"]["coding_review"]["ready"] is True
    assert report["scenarios"]["coding_review"]["task_profile"] == "model_owned"
    assert report["scenarios"]["operations"]["ready"] is True
    assert report["scenarios"]["operations"]["task_profile"] == "model_owned"
    assert report["scenarios"]["session_recall"]["ready"] is True
    assert report["scenarios"]["session_recall"]["task_profile"] == "model_owned"
    assert report["scenarios"]["long_context_after_compaction"]["ready"] is True
    assert report["scenarios"]["long_context_after_compaction"]["task_profile"] == "model_owned"
    assert report["scenarios"]["self_evolution"]["ready"] is True
    assert report["scenarios"]["self_evolution"]["task_profile"] == "model_owned"
    assert report["scenarios"]["delegation_worker"]["ready"] is True
    assert report["scenarios"]["delegation_profile_matrix"]["ready"] is True
    assert report["scenarios"]["research"]["benchmark_ready"] is True
    assert report["scenarios"]["session_recall"]["benchmark_ready"] is True
    assert report["scenarios"]["self_evolution"]["benchmark_ready"] is True
    assert report["scenarios"]["research"]["score"] == 100
    assert report["scenarios"]["session_recall"]["benchmark_case_summary"]["failed_cases"] == 0
    assert report["scenarios"]["long_context_after_compaction"]["benchmark_case_summary"]["failed_cases"] == 0
    assert report["scenarios"]["self_evolution"]["assembled_prompt"].count("patch") >= 1


def test_evaluate_task_readiness_detects_missing_tools_and_prompt_failures() -> None:
    from app.runtime.task_eval import TaskEvalInputs, evaluate_task_readiness

    report = evaluate_task_readiness(
        TaskEvalInputs(
            tool_names={
                "read_file",
                "write_file",
                "edit_file",
                "glob_search",
                "grep_search",
                "web_fetch",
            },
            prompt_inputs=PromptEvalInputs(
                tools_section="## Using Your Tools\n(load only)\n",
                heartbeat_template="# Heartbeat\n(no save_skill guidance)\n",
            ),
        )
    )

    assert report["summary"]["failing_scenarios"] >= 2
    assert report["scenarios"]["research"]["ready"] is False
    assert "web_search" in report["scenarios"]["research"]["missing_tools"]
    assert report["scenarios"]["self_evolution"]["ready"] is False
    assert "save_skill" in report["scenarios"]["self_evolution"]["missing_tools"]
    assert "skill_evolution_guidance" in report["scenarios"]["self_evolution"]["failed_prompt_checks"]
    assert "skill_patch_instead_of_duplicate_guidance" in report["scenarios"]["self_evolution"]["failed_prompt_checks"]


def test_evaluate_task_readiness_distinguishes_initial_tools_from_skill_reachable_tools() -> None:
    from app.runtime.task_eval import TaskEvalInputs, evaluate_task_readiness

    report = evaluate_task_readiness(
        TaskEvalInputs(
            tool_names={
                "read_file",
                "write_file",
                "edit_file",
                "glob_search",
                "grep_search",
                "load_skill",
                "search_memory",
                "save_memory",
                "set_trigger",
                "list_triggers",
                "web_fetch",
                "check_async_task",
                "delegate_to_agent",
                "list_async_tasks",
                "get_current_time",
                "save_skill",
            },
            skill_tool_names={"web_search", "web_fetch"},
        )
    )

    research = report["scenarios"]["research"]
    assert research["ready"] is True
    assert research["missing_tools"] == []
    assert research["initial_missing_tools"] == []
    assert research["skill_missing_tools"] == []


def test_evaluate_task_readiness_detects_delegation_profile_regressions() -> None:
    from app.runtime.task_eval import TaskEvalInputs, evaluate_task_readiness

    report = evaluate_task_readiness(
        TaskEvalInputs(
            delegation_profiles={
                "review_readonly": {
                    "core_tools_only": True,
                    "allowed_tools": ("read_file",),
                },
            }
        )
    )

    assert report["scenarios"]["delegation_profile_matrix"]["ready"] is False
    failures = report["scenarios"]["delegation_profile_matrix"]["runtime_failure_details"]
    names = {failure["name"] for failure in failures}
    assert "missing_profile:research_readonly" in names
    assert "review_profile_boundaries" in names


def test_evaluate_task_readiness_detects_assembled_prompt_benchmark_failures() -> None:
    from app.runtime.task_eval import TaskEvalInputs, evaluate_task_readiness

    report = evaluate_task_readiness(
        TaskEvalInputs(
            scenario_prompts={
                "session_recall": "## Task Playbook\n- keep it generic\n",
                "self_evolution": "## Task Playbook\n- keep it generic\n",
            }
        )
    )

    assert report["scenarios"]["session_recall"]["ready"] is False
    assert report["scenarios"]["session_recall"]["benchmark_ready"] is False
    recall_failures = report["scenarios"]["session_recall"]["benchmark_failure_details"]
    assert any(failure["name"] == "assembled_prompt_contract" for failure in recall_failures)
    assert any("search_memory" in failure["detail"] for failure in recall_failures)

    assert report["scenarios"]["long_context_after_compaction"]["ready"] is False
    assert report["scenarios"]["long_context_after_compaction"]["benchmark_ready"] is False

    assert report["scenarios"]["self_evolution"]["ready"] is False
    assert report["scenarios"]["self_evolution"]["benchmark_ready"] is False
    evolution_failures = report["scenarios"]["self_evolution"]["benchmark_failure_details"]
    assert any("save_skill" in failure["detail"] for failure in evolution_failures)


def test_evaluate_task_readiness_detects_case_level_benchmark_failures() -> None:
    from app.runtime.task_eval import TaskEvalInputs, evaluate_task_readiness

    report = evaluate_task_readiness(
        TaskEvalInputs(
            scenario_prompts={
                "session_recall": "## Task Playbook\n- use search_memory first\n- prefer session transcript evidence\n",
                "self_evolution": (
                    "## Task Playbook\n"
                    "- Use save_skill for repeatedly successful capability capsules only.\n"
                    "- Never promote one-off transcript notes.\n"
                ),
            },
        )
    )

    recall_cases = report["scenarios"]["session_recall"]["benchmark_cases"]
    assert any(case["query"].startswith("summarize our earlier decision") for case in recall_cases)
    assert any(case["ready"] is False for case in recall_cases)
    assert report["benchmark_summary"]["failing_cases"] >= 1


def test_task_eval_main_reports_gate_failure(capsys) -> None:
    from app.runtime.task_eval import TaskEvalInputs, main

    exit_code = main(
        ["--fail-on=research,self_evolution"],
        inputs=TaskEvalInputs(tool_names={"read_file"}),
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Scenario readiness summary" in captured.out
    assert "research" in captured.out
    assert "self_evolution" in captured.out
    assert "Remediation:" in captured.out


def test_task_eval_main_reports_benchmark_gate_failure(capsys) -> None:
    from app.runtime.task_eval import TaskEvalInputs, main

    exit_code = main(
        ["--fail-on-benchmark=session_recall"],
        inputs=TaskEvalInputs(
            scenario_prompts={"session_recall": "## Task Playbook\n- keep it generic\n"},
        ),
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "Benchmark cases failed" in captured.out
    assert "session_recall" in captured.out


def test_render_task_readiness_report_includes_failure_details() -> None:
    from app.runtime.task_eval import TaskEvalInputs, evaluate_task_readiness, render_task_readiness_report

    report = evaluate_task_readiness(
        TaskEvalInputs(
            tool_names={"read_file"},
            prompt_inputs=PromptEvalInputs(
                tools_section="## Using Your Tools\n(load only)\n",
                memory_section="## Your Memory System\nsearch_memory only\n",
                heartbeat_template="# Heartbeat\n(no save_skill guidance)\n",
            ),
        )
    )

    rendered = render_task_readiness_report(report, gated_scenarios=("session_recall", "self_evolution"))

    assert "skill_evolution_guidance" in rendered
    assert "memory_usage_guidance" in rendered
    assert "[medium]" in rendered or "[high]" in rendered
    assert "Remediation:" in rendered
    assert "Benchmark cases failed" in rendered


def test_task_eval_main_reports_success(capsys) -> None:
    from app.runtime.task_eval import main

    exit_code = main([])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Scenario readiness summary" in captured.out
    assert "All gated scenarios passed." in captured.out
