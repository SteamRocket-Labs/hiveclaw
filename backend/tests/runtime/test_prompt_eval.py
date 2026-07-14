from __future__ import annotations

import pytest


def test_evaluate_runtime_prompt_contracts_has_no_failures() -> None:
    from app.runtime.prompt_eval import evaluate_runtime_prompt_contracts

    report = evaluate_runtime_prompt_contracts()

    assert report["failed"] == []
    assert "system_trust_boundaries" in report["passed"]
    assert "memory_usage_guidance" in report["passed"]
    assert "always_on_required_tools_are_core" in report["passed"]
    assert "direct_core_tool_before_skill_guidance" in report["passed"]
    assert "output_scale_matches_user_request" in report["passed"]
    assert "web_lookup_requires_tool_search_discovery" in report["passed"]
    assert "tools_section_names_mcp_discovery" in report["passed"]
    assert "skill_evolution_guidance" in report["passed"]
    assert "skill_patch_instead_of_duplicate_guidance" in report["passed"]
    assert "heartbeat_skill_curation_consistency" in report["passed"]
    assert "task_playbook_review_overlay" in report["passed"]
    assert "heartbeat_model_owned_evidence_policy" in report["passed"]
    assert report["scenarios"]["research"]["failed"] == []
    assert report["scenarios"]["coding_review"]["failed"] == []
    assert report["scenarios"]["operations"]["failed"] == []
    assert report["scenarios"]["memory_recall"]["failed"] == []
    assert report["scenarios"]["self_evolution"]["failed"] == []
    assert report["scenarios"]["delegation_worker"]["failed"] == []
    assert report["checks"]["system_trust_boundaries"]["detail"]
    assert report["checks"]["system_trust_boundaries"]["severity"] == "critical"
    assert report["checks"]["system_trust_boundaries"]["remediation"]
    assert "default" in report["heartbeat_templates"]
    assert "hr_agent_template" in report["heartbeat_templates"]
    assert report["heartbeat_templates"]["hr_agent_template"]["failed"] == []
    assert report["scenarios"]["research"]["checks"]["research_sources"]["detail"]
    assert report["summary"]["critical_failures"] == 0
    assert report["summary"]["high_failures"] == 0


def test_evaluate_runtime_prompt_contracts_reports_failure_details() -> None:
    from app.runtime.prompt_eval import PromptEvalInputs, evaluate_runtime_prompt_contracts

    report = evaluate_runtime_prompt_contracts(
        PromptEvalInputs(
            system_section="## System\n(no trust boundaries)\n",
            executing_actions_section="## Core Directives\n(make reports for everything)\n",
            memory_section="## Your Memory System\n(save only)\n",
            tools_section="## Using Your Tools\n(load only)\n",
            review_playbook="## Task Playbook\n(no overlay)\n",
            memory_recall_playbook="## Task Playbook\n(generic recall)\n",
            self_evolution_playbook="## Task Playbook\n(generic skill note)\n",
            heartbeat_template="# Heartbeat\n(no weights)\n",
            knowledge_section="raw knowledge\n",
            delegation_worker_prompt="worker mode\n",
        )
    )

    assert "system_trust_boundaries" in report["failed"]
    assert "skill_evolution_guidance" in report["failed"]
    assert "direct_core_tool_before_skill_guidance" in report["failed"]
    assert "output_scale_matches_user_request" in report["failed"]
    assert "skill_patch_instead_of_duplicate_guidance" in report["failed"]
    assert "heartbeat_skill_curation_consistency" in report["failed"]
    assert "heartbeat_model_owned_evidence_policy" in report["failed"]
    assert report["checks"]["system_trust_boundaries"]["passed"] is False
    assert report["checks"]["system_trust_boundaries"]["severity"] == "critical"
    assert report["checks"]["system_trust_boundaries"]["remediation"]
    assert "Trust Boundaries" in report["checks"]["system_trust_boundaries"]["detail"]
    assert report["scenarios"]["delegation_worker"]["failed"] != []
    assert report["scenarios"]["memory_recall"]["failed"] != []
    assert report["scenarios"]["self_evolution"]["failed"] != []
    assert report["scenarios"]["delegation_worker"]["checks"]["worker_return_format_not_forced"]["passed"] is False
    assert report["summary"]["critical_failures"] >= 1


def test_assert_runtime_prompt_contracts_raises_for_high_severity_failures() -> None:
    from app.runtime.prompt_eval import PromptEvalInputs, assert_runtime_prompt_contracts

    with pytest.raises(RuntimeError, match="system_trust_boundaries"):
        assert_runtime_prompt_contracts(
            PromptEvalInputs(system_section="## System\n(no trust boundaries)\n"),
        )


def test_prompt_eval_main_reports_gate_failure(capsys) -> None:
    from app.runtime.prompt_eval import PromptEvalInputs, main

    exit_code = main(
        ["--fail-severity=critical"],
        inputs=PromptEvalInputs(system_section="## System\n(no trust boundaries)\n"),
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert "critical_failures=1" in captured.out
    assert "system_trust_boundaries" in captured.out


def test_prompt_eval_main_reports_success(capsys) -> None:
    from app.runtime.prompt_eval import main

    exit_code = main([])

    captured = capsys.readouterr()
    assert exit_code == 0
    assert "Prompt contract gate passed" in captured.out
