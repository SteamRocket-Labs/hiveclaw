"""Scenario-level runtime readiness and benchmark evaluation."""

from __future__ import annotations

import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.runtime.context_budget import compute_context_budget, infer_task_profile
from app.runtime.prompt_eval import PromptEvalInputs, evaluate_runtime_prompt_contracts
from app.runtime.prompt_builder import build_dynamic_prompt_suffix
from app.services.agent_tools import CORE_TOOL_NAMES
from app.skills.parser import SkillParser
from app.tools.packs import pack_for_name


@dataclass(slots=True, frozen=True)
class TaskEvalInputs:
    prompt_inputs: PromptEvalInputs | None = None
    tool_names: set[str] | None = None
    skill_tool_names: set[str] | None = None
    delegation_profiles: dict[str, dict[str, Any]] | None = None
    scenario_prompts: dict[str, str] | None = None


@dataclass(slots=True, frozen=True)
class _BenchmarkCase:
    query: str
    expected_task_profile: str | None = None
    prompt_expectations: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class _ScenarioSpec:
    query: str
    required_tools: tuple[str, ...]
    required_skill_tools: tuple[str, ...] = ()
    prompt_checks: tuple[str, ...] = ()
    scenario_checks: tuple[tuple[str, str], ...] = ()
    expected_task_profile: str | None = None
    benchmark_expectations: tuple[str, ...] = ()
    benchmark_cases: tuple[_BenchmarkCase, ...] = ()


_SCENARIO_SPECS: dict[str, _ScenarioSpec] = {
    "research": _ScenarioSpec(
        query="latest market research on agent frameworks and primary sources",
        required_tools=("load_skill", "web_fetch", "search_memory"),
        required_skill_tools=("web_search",),
        prompt_checks=("system_trust_boundaries", "knowledge_trust_framing"),
        scenario_checks=(("research", "research_sources"), ("research", "research_dates")),
        expected_task_profile="research",
        benchmark_expectations=("## Task Playbook", "primary sources", "Likely Capability Packs", "web_pack"),
        benchmark_cases=(
            _BenchmarkCase(
                query="find the latest official release notes and cite primary sources",
                expected_task_profile="research",
                prompt_expectations=("primary sources", "absolute dates"),
            ),
            _BenchmarkCase(
                query="compare current agent frameworks and separate confirmed facts from inference",
                expected_task_profile="research",
                prompt_expectations=("confirmed facts", "inference"),
            ),
        ),
    ),
    "coding_review": _ScenarioSpec(
        query="please review this implementation and verify regressions",
        required_tools=("read_file", "edit_file", "glob_search", "grep_search"),
        prompt_checks=("task_playbook_review_overlay",),
        scenario_checks=(("coding_review", "coding_verify"), ("coding_review", "review_findings_first")),
        expected_task_profile="coding",
        benchmark_expectations=("## Task Playbook", "Verification / Review Overlay", "Findings first"),
        benchmark_cases=(
            _BenchmarkCase(
                query="review this patch and lead with the most severe regressions",
                expected_task_profile="coding",
                prompt_expectations=("Findings first", "Verification / Review Overlay"),
            ),
        ),
    ),
    "operations": _ScenarioSpec(
        query="investigate the trigger incident and current queue backlog",
        required_tools=("get_current_time", "list_triggers", "set_trigger", "check_async_task"),
        scenario_checks=(("operations", "ops_verify_live_state"), ("operations", "ops_blast_radius")),
        expected_task_profile="operations",
        benchmark_expectations=("## Task Playbook", "Verify live state before acting", "Minimize blast radius"),
        benchmark_cases=(
            _BenchmarkCase(
                query="investigate the queue incident and confirm rollback options before acting",
                expected_task_profile="operations",
                prompt_expectations=("Verify live state before acting", "rollback"),
            ),
        ),
    ),
    "delegation_worker": _ScenarioSpec(
        query="delegate this research task to another agent and follow up later",
        required_tools=("delegate_to_agent", "check_async_task", "list_async_tasks"),
        scenario_checks=(("delegation_worker", "worker_safe_tools"), ("delegation_worker", "worker_return_format")),
        benchmark_cases=(
            _BenchmarkCase(
                query="delegate a bounded research task and require a completed/evidence/blockers summary",
                prompt_expectations=("Return format", "Completed:", "Evidence:", "Blockers:"),
            ),
        ),
    ),
    "delegation_profile_matrix": _ScenarioSpec(
        query="delegate a read-only review or research task to another agent",
        required_tools=("delegate_to_agent", "check_async_task", "list_async_tasks"),
        scenario_checks=(("delegation_worker", "worker_safe_tools"), ("delegation_worker", "worker_return_format")),
    ),
    "session_recall": _ScenarioSpec(
        query="what did we decide last time about the md-first memory system",
        required_tools=("search_memory", "save_memory"),
        prompt_checks=("memory_usage_guidance",),
        scenario_checks=(
            ("memory_recall", "recall_uses_search_memory"),
            ("memory_recall", "recall_prefers_session_evidence"),
        ),
        expected_task_profile="memory_recall",
        benchmark_expectations=("## Task Playbook", "search_memory first", "session transcript evidence"),
        benchmark_cases=(
            _BenchmarkCase(
                query="what did we decide last time about the md-first memory system",
                expected_task_profile="memory_recall",
                prompt_expectations=("search_memory first", "session transcript evidence"),
            ),
            _BenchmarkCase(
                query="summarize our earlier decision on the md-first memory system with the smallest accurate recap",
                expected_task_profile="memory_recall",
                prompt_expectations=("search_memory first", "smallest accurate recap"),
            ),
        ),
    ),
    "long_context_after_compaction": _ScenarioSpec(
        query="what did we decide earlier after compaction about the md-first memory system",
        required_tools=("search_memory", "save_memory"),
        prompt_checks=("memory_usage_guidance",),
        scenario_checks=(
            ("memory_recall", "recall_uses_search_memory"),
            ("memory_recall", "recall_prefers_session_evidence"),
        ),
        expected_task_profile="memory_recall",
        benchmark_expectations=("## Task Playbook", "search_memory first", "session transcript evidence", "smallest accurate recap"),
        benchmark_cases=(
            _BenchmarkCase(
                query="what did we decide earlier after compaction about the md-first memory system",
                expected_task_profile="memory_recall",
                prompt_expectations=("search_memory first", "session transcript evidence", "smallest accurate recap"),
            ),
        ),
    ),
    "self_evolution": _ScenarioSpec(
        query="save this repeated successful workflow as a reusable skill",
        required_tools=("save_skill", "load_skill", "save_memory"),
        prompt_checks=(
            "skill_evolution_guidance",
            "skill_patch_instead_of_duplicate_guidance",
            "heartbeat_skill_curation_consistency",
        ),
        scenario_checks=(
            ("self_evolution", "self_evolution_requires_repeated_success"),
            ("self_evolution", "self_evolution_blocks_one_off_transcripts"),
        ),
        expected_task_profile="self_evolution",
        benchmark_expectations=(
            "## Task Playbook",
            "save_skill",
            "repeatedly successful",
            "one-off transcript",
            "patch the existing skill",
        ),
        benchmark_cases=(
            _BenchmarkCase(
                query="save this repeated successful workflow as a reusable skill",
                expected_task_profile="self_evolution",
                prompt_expectations=("save_skill", "repeatedly successful"),
            ),
            _BenchmarkCase(
                query="turn this stable operating procedure into a reusable skill and capture the why",
                expected_task_profile="self_evolution",
                prompt_expectations=("save_skill", "Capture the why", "one-off transcript"),
            ),
        ),
    ),
}


def _runtime_tool_names(inputs: TaskEvalInputs | None) -> set[str]:
    if inputs and inputs.tool_names is not None:
        return set(inputs.tool_names)
    return set(CORE_TOOL_NAMES)


def _runtime_skill_tool_names(inputs: TaskEvalInputs | None) -> set[str]:
    if inputs and inputs.skill_tool_names is not None:
        return set(inputs.skill_tool_names)

    parser = SkillParser()
    backend_root = Path(__file__).resolve().parents[2]
    skill_roots = [
        backend_root / "app" / "templates" / "system_skills",
        backend_root / "app" / "templates" / "skills",
        *sorted(backend_root.glob("*_agent_template/skills")),
    ]

    declared_tools: set[str] = set()
    for skill_root in skill_roots:
        if not skill_root.exists():
            continue
        for skill_path in sorted(skill_root.rglob("SKILL.md")):
            try:
                parsed = parser.parse_file(
                    skill_path,
                    relative_path=skill_path.relative_to(backend_root).as_posix(),
                    default_name=skill_path.parent.name,
                )
            except Exception:
                continue
            declared_tools.update(parsed.metadata.declared_tools)
            for pack_name in parsed.metadata.declared_packs:
                pack = pack_for_name(pack_name)
                if pack:
                    declared_tools.update(pack.tools)
    return declared_tools


def _runtime_delegation_profiles(inputs: TaskEvalInputs | None) -> dict[str, dict[str, Any]]:
    if inputs and inputs.delegation_profiles is not None:
        return inputs.delegation_profiles

    from app.agents.orchestrator import _DELEGATION_TOOL_PROFILES

    return {
        name: {
            "core_tools_only": profile.core_tools_only,
            "allowed_tools": tuple(profile.allowed_tools),
            "excluded_tools": tuple(profile.excluded_tools),
            "tool_policy": profile.tool_policy,
            "memory_policy": profile.memory_policy,
        }
        for name, profile in _DELEGATION_TOOL_PROFILES.items()
    }


def _default_scenario_prompt(name: str, query: str) -> str:
    budget = compute_context_budget(context_window_tokens=128000, query=query, active_pack_count=0)
    retrieval_context = "Source: benchmark evidence\n- Retrieved supporting context"
    memory_snapshot = ""
    if name in {"session_recall", "self_evolution"}:
        memory_snapshot = "T3 snapshot:\n- prior decision or stable workflow"
    if name == "long_context_after_compaction":
        memory_snapshot = "T3 snapshot:\n- earlier decision survived compaction\n- use transcript evidence and continuity artifacts"
    prompt = build_dynamic_prompt_suffix(
        active_packs=[],
        retrieval_context=retrieval_context if name == "research" else "",
        budget_profile=budget,
        latest_user_query=query,
        memory_snapshot=memory_snapshot,
        user_name="Benchmark",
        channel="benchmark",
        agent_name="Benchmark Agent",
    )
    if name in {"delegation_worker", "delegation_profile_matrix"}:
        from app.agents.orchestrator import _DELEGATED_WORKER_PROMPT_SUFFIX

        prompt = f"{prompt}\n\n{_DELEGATED_WORKER_PROMPT_SUFFIX}".strip()
    return prompt


def _runtime_scenario_prompts(inputs: TaskEvalInputs | None) -> dict[str, str]:
    prompts = {
        name: _default_scenario_prompt(name, spec.query)
        for name, spec in _SCENARIO_SPECS.items()
    }
    if inputs and inputs.scenario_prompts is not None:
        prompts.update(inputs.scenario_prompts)
        if (
            "session_recall" in inputs.scenario_prompts
            and "long_context_after_compaction" not in inputs.scenario_prompts
        ):
            prompts["long_context_after_compaction"] = inputs.scenario_prompts["session_recall"]
    return prompts


def _scenario_prompt_for_case(
    scenario_name: str,
    case: _BenchmarkCase,
    scenario_prompts: dict[str, str],
    *,
    use_override: bool = True,
) -> str:
    if use_override and scenario_name in scenario_prompts:
        return scenario_prompts[scenario_name]
    return _default_scenario_prompt(scenario_name, case.query)


def _top_level_check_failed(report: dict[str, Any], check_name: str) -> bool:
    return not bool(report["checks"].get(check_name, {}).get("passed", False))


def _scenario_check_failed(report: dict[str, Any], scenario_name: str, check_name: str) -> bool:
    return not bool(report["scenarios"].get(scenario_name, {}).get("checks", {}).get(check_name, {}).get("passed", False))


def _top_level_check_entry(report: dict[str, Any], check_name: str) -> dict[str, Any] | None:
    return report["checks"].get(check_name)


def _scenario_check_entry(report: dict[str, Any], scenario_name: str, check_name: str) -> dict[str, Any] | None:
    return report["scenarios"].get(scenario_name, {}).get("checks", {}).get(check_name)


def _evaluate_profile_matrix_failures(
    profile_name: str,
    profile: dict[str, Any] | None,
    *,
    required_tools: tuple[str, ...],
    forbidden_tools: tuple[str, ...],
) -> list[dict[str, str]]:
    if profile is None:
        return [{
            "name": f"missing_profile:{profile_name}",
            "severity": "high",
            "detail": f"Delegation profile {profile_name} is missing from the runtime policy matrix.",
            "remediation": "Restore the missing delegation profile and expose it through delegate_to_agent.",
        }]

    failures: list[dict[str, str]] = []
    allowed_tools = tuple(profile.get("allowed_tools") or ())
    core_tools_only = bool(profile.get("core_tools_only", True))
    if core_tools_only or any(tool not in allowed_tools for tool in required_tools) or any(
        tool in allowed_tools for tool in forbidden_tools
    ):
        failures.append(
            {
                "name": f"{profile_name.split('_')[0]}_profile_boundaries",
                "severity": "high",
                "detail": (
                    f"Delegation profile {profile_name} does not enforce the expected read-only boundaries "
                    f"(core_tools_only={core_tools_only}, allowed_tools={allowed_tools})."
                ),
                "remediation": (
                    "Set core_tools_only=False and tighten allowed_tool_names so the profile exposes only the intended read-only surface."
                ),
            }
        )
    return failures


def _evaluate_prompt_benchmark_failures(
    scenario_name: str,
    scenario_prompt: str,
    *,
    expectations: tuple[str, ...],
) -> list[dict[str, str]]:
    missing = [expectation for expectation in expectations if expectation not in scenario_prompt]
    if not missing:
        return []
    return [
        {
            "name": "assembled_prompt_contract",
            "severity": "high",
            "detail": (
                f"Scenario {scenario_name} assembled prompt is missing benchmark expectations: {', '.join(missing)}."
            ),
            "remediation": (
                "Restore the scenario-specific playbook or dynamic prompt assembly so this task shape gets the expected guidance."
            ),
        }
    ]


def _evaluate_benchmark_cases(
    scenario_name: str,
    spec: _ScenarioSpec,
    scenario_prompts: dict[str, str],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    cases: list[dict[str, Any]] = []
    passed = 0

    for case in spec.benchmark_cases:
        scenario_prompt = _scenario_prompt_for_case(scenario_name, case, scenario_prompts)
        inferred_profile = infer_task_profile(case.query).name
        expected_profile = case.expected_task_profile or spec.expected_task_profile
        failures: list[dict[str, str]] = []

        if expected_profile is not None and inferred_profile != expected_profile:
            failures.append(
                {
                    "name": "task_profile_mismatch",
                    "severity": "medium",
                    "detail": (
                        f"Benchmark case routed to profile={inferred_profile}, expected={expected_profile} "
                        f"for query: {case.query}"
                    ),
                    "remediation": "Expand task-profile inference hints so more user phrasings stay on the intended execution path.",
                }
            )

        missing_expectations = [
            expectation
            for expectation in case.prompt_expectations
            if expectation not in scenario_prompt
        ]
        if missing_expectations:
            failures.append(
                {
                    "name": "case_prompt_contract",
                    "severity": "high",
                    "detail": (
                        f"Benchmark case prompt for scenario {scenario_name} is missing: {', '.join(missing_expectations)} "
                        f"(query: {case.query})."
                    ),
                    "remediation": (
                        "Strengthen the scenario-specific playbook so alternate phrasings still receive the required guidance."
                    ),
                }
            )

        ready = not failures
        if ready:
            passed += 1
        cases.append(
            {
                "query": case.query,
                "expected_task_profile": expected_profile,
                "task_profile": inferred_profile,
                "ready": ready,
                "failures": failures,
            }
        )

    total = len(cases)
    return cases, {
        "total_cases": total,
        "passed_cases": passed,
        "failed_cases": max(total - passed, 0),
    }


def evaluate_task_readiness(inputs: TaskEvalInputs | None = None) -> dict[str, Any]:
    prompt_report = evaluate_runtime_prompt_contracts(inputs.prompt_inputs if inputs else None)
    tool_names = _runtime_tool_names(inputs)
    skill_tool_names = _runtime_skill_tool_names(inputs)
    delegation_profiles = _runtime_delegation_profiles(inputs)
    scenario_prompts = _runtime_scenario_prompts(inputs)

    scenarios: dict[str, dict[str, Any]] = {}
    ready_count = 0
    benchmark_total = 0
    benchmark_passed = 0

    for name, spec in _SCENARIO_SPECS.items():
        initial_missing_tools = [tool_name for tool_name in spec.required_tools if tool_name not in tool_names]
        reachable_skill_tools = skill_tool_names if "load_skill" in tool_names else set()
        skill_missing_tools = [
            tool_name for tool_name in spec.required_skill_tools if tool_name not in reachable_skill_tools
        ]
        missing_tools = [*initial_missing_tools, *skill_missing_tools]
        failed_prompt_checks = [check_name for check_name in spec.prompt_checks if _top_level_check_failed(prompt_report, check_name)]
        failed_scenario_checks = [
            f"{scenario_name}.{check_name}"
            for scenario_name, check_name in spec.scenario_checks
            if _scenario_check_failed(prompt_report, scenario_name, check_name)
        ]
        runtime_failure_details: list[dict[str, str]] = []
        if initial_missing_tools:
            runtime_failure_details.append(
                {
                    "name": "missing_initial_tools",
                    "severity": "high",
                    "detail": f"First-round tool surface is missing: {', '.join(initial_missing_tools)}.",
                    "remediation": "Expose the missing tools in the default core surface before gating this scenario as ready.",
                }
            )
        if skill_missing_tools:
            runtime_failure_details.append(
                {
                    "name": "missing_skill_tools",
                    "severity": "high",
                    "detail": f"Skill-reachable tool surface is missing: {', '.join(skill_missing_tools)}.",
                    "remediation": "Declare the missing tools on the relevant skill or pack so load_skill can activate them.",
                }
            )
        prompt_failure_details: list[dict[str, str]] = []
        for check_name in failed_prompt_checks:
            entry = _top_level_check_entry(prompt_report, check_name)
            if entry is None:
                continue
            prompt_failure_details.append(
                {
                    "name": check_name,
                    "severity": entry["severity"],
                    "detail": entry["detail"],
                    "remediation": entry["remediation"],
                }
            )
        for scenario_name, check_name in spec.scenario_checks:
            entry = _scenario_check_entry(prompt_report, scenario_name, check_name)
            if entry is None or entry["passed"]:
                continue
            prompt_failure_details.append(
                {
                    "name": f"{scenario_name}.{check_name}",
                    "severity": entry["severity"],
                    "detail": entry["detail"],
                    "remediation": entry["remediation"],
                }
            )

        inferred_profile = infer_task_profile(spec.query).name
        task_profile_ok = spec.expected_task_profile is None or inferred_profile == spec.expected_task_profile
        if spec.expected_task_profile is not None and not task_profile_ok:
            runtime_failure_details.append(
                {
                    "name": "task_profile_mismatch",
                    "severity": "medium",
                    "detail": (
                        f"Task router inferred profile={inferred_profile}, expected={spec.expected_task_profile} "
                        f"for scenario {name}."
                    ),
                    "remediation": "Adjust task-profile inference keywords or the scenario query so routing matches the intended execution path.",
                }
            )

        extra_runtime_failures: list[dict[str, str]] = []
        extra_runtime_conditions = 0
        if name == "delegation_profile_matrix":
            extra_runtime_conditions = 2
            extra_runtime_failures.extend(
                _evaluate_profile_matrix_failures(
                    "review_readonly",
                    delegation_profiles.get("review_readonly"),
                    required_tools=("read_file", "glob_search", "grep_search", "search_memory"),
                    forbidden_tools=("write_file", "edit_file", "run_command", "execute_code"),
                )
            )
            extra_runtime_failures.extend(
                _evaluate_profile_matrix_failures(
                    "research_readonly",
                    delegation_profiles.get("research_readonly"),
                    required_tools=("read_file", "search_memory", "web_fetch", "web_search"),
                    forbidden_tools=("write_file", "edit_file", "run_command", "execute_code"),
                )
            )
            runtime_failure_details.extend(extra_runtime_failures)

        benchmark_failure_details = _evaluate_prompt_benchmark_failures(
            name,
            scenario_prompts.get(name, ""),
            expectations=spec.benchmark_expectations,
        )
        benchmark_cases, benchmark_case_summary = _evaluate_benchmark_cases(name, spec, scenario_prompts)
        benchmark_total += benchmark_case_summary["total_cases"]
        benchmark_passed += benchmark_case_summary["passed_cases"]
        benchmark_ready = not benchmark_failure_details and benchmark_case_summary["failed_cases"] == 0

        total_conditions = (
            len(spec.required_tools)
            + len(spec.required_skill_tools)
            + len(spec.prompt_checks)
            + len(spec.scenario_checks)
            + (1 if spec.expected_task_profile is not None else 0)
            + extra_runtime_conditions
            + (1 if spec.benchmark_expectations else 0)
            + benchmark_case_summary["total_cases"]
        )
        failed_conditions = (
            len(initial_missing_tools)
            + len(skill_missing_tools)
            + len(failed_prompt_checks)
            + len(failed_scenario_checks)
            + (0 if task_profile_ok else 1)
            + len(extra_runtime_failures)
            + (0 if not benchmark_failure_details else 1)
            + benchmark_case_summary["failed_cases"]
        )
        passed_conditions = max(total_conditions - failed_conditions, 0)
        score = int(round((passed_conditions / total_conditions) * 100)) if total_conditions else 100
        ready = failed_conditions == 0
        if ready:
            ready_count += 1

        scenarios[name] = {
            "ready": ready,
            "score": score,
            "task_profile": inferred_profile,
            "task_profile_ok": task_profile_ok,
            "expected_task_profile": spec.expected_task_profile,
            "benchmark_ready": benchmark_ready,
            "benchmark_failure_details": benchmark_failure_details,
            "benchmark_cases": benchmark_cases,
            "benchmark_case_summary": benchmark_case_summary,
            "scenario_prompt": scenario_prompts.get(name, ""),
            "assembled_prompt": scenario_prompts.get(name, ""),
            "missing_tools": missing_tools,
            "initial_missing_tools": initial_missing_tools,
            "skill_missing_tools": skill_missing_tools,
            "failed_prompt_checks": failed_prompt_checks,
            "failed_scenario_checks": failed_scenario_checks,
            "runtime_failure_details": runtime_failure_details,
            "prompt_failure_details": prompt_failure_details,
            "required_tools": list(spec.required_tools),
            "required_skill_tools": list(spec.required_skill_tools),
        }

    return {
        "scenarios": scenarios,
        "summary": {
            "total_scenarios": len(_SCENARIO_SPECS),
            "ready_scenarios": ready_count,
            "failing_scenarios": len(_SCENARIO_SPECS) - ready_count,
        },
        "benchmark_summary": {
            "total_cases": benchmark_total,
            "passed_cases": benchmark_passed,
            "failing_cases": max(benchmark_total - benchmark_passed, 0),
        },
        "prompt_summary": prompt_report["summary"],
    }


def render_task_readiness_report(
    report: dict[str, Any],
    *,
    gated_scenarios: tuple[str, ...] | None = None,
    benchmark_gated_scenarios: tuple[str, ...] | None = None,
) -> str:
    summary = report["summary"]
    benchmark_summary = report["benchmark_summary"]
    lines = [
        "Scenario readiness summary:",
        (
            f"ready_scenarios={summary['ready_scenarios']} "
            f"failing_scenarios={summary['failing_scenarios']} "
            f"total_scenarios={summary['total_scenarios']}"
        ),
        (
            f"Benchmark summary: passed_cases={benchmark_summary['passed_cases']} "
            f"failing_cases={benchmark_summary['failing_cases']} "
            f"total_cases={benchmark_summary['total_cases']}"
        ),
    ]

    for scenario_name, scenario in report["scenarios"].items():
        status = "READY" if scenario["ready"] else "BLOCKED"
        profile_line = f"profile={scenario['task_profile']}"
        if scenario["expected_task_profile"] is not None:
            profile_line += f" expected={scenario['expected_task_profile']}"
        lines.append(f"- {scenario_name}: {status} score={scenario['score']} {profile_line}")
        if scenario["missing_tools"]:
            lines.append(f"  missing_tools: {', '.join(scenario['missing_tools'])}")
        if scenario.get("initial_missing_tools"):
            lines.append(f"  initial_missing_tools: {', '.join(scenario['initial_missing_tools'])}")
        if scenario.get("skill_missing_tools"):
            lines.append(f"  skill_missing_tools: {', '.join(scenario['skill_missing_tools'])}")
        if scenario["failed_prompt_checks"]:
            lines.append(f"  failed_prompt_checks: {', '.join(scenario['failed_prompt_checks'])}")
        if scenario["failed_scenario_checks"]:
            lines.append(f"  failed_scenario_checks: {', '.join(scenario['failed_scenario_checks'])}")
        for failure in scenario.get("runtime_failure_details", ()):
            lines.append(
                f"  runtime_failure: {failure['name']} [{failure['severity']}] "
                f"{failure['detail']} | Remediation: {failure['remediation']}"
            )
        for failure in scenario.get("benchmark_failure_details", ()):
            lines.append(
                f"  benchmark_failure: {failure['name']} [{failure['severity']}] "
                f"{failure['detail']} | Remediation: {failure['remediation']}"
            )
        benchmark_case_summary = scenario.get("benchmark_case_summary", {})
        if benchmark_case_summary.get("total_cases", 0):
            lines.append(
                "  Benchmark cases failed: "
                f"{benchmark_case_summary['failed_cases']}/{benchmark_case_summary['total_cases']}"
            )
        if benchmark_case_summary.get("failed_cases", 0):
            for case in scenario.get("benchmark_cases", ()):
                if case.get("ready", False):
                    continue
                lines.append(f"    case: {case['query']}")
                for failure in case.get("failures", ()):
                    lines.append(
                        f"      benchmark_case_failure: {failure['name']} [{failure['severity']}] "
                        f"{failure['detail']} | Remediation: {failure['remediation']}"
                    )
        for failure in scenario.get("prompt_failure_details", ()):
            lines.append(
                f"  prompt_failure: {failure['name']} [{failure['severity']}] "
                f"{failure['detail']} | Remediation: {failure['remediation']}"
            )
        if scenario["expected_task_profile"] is not None and not scenario["task_profile_ok"]:
            lines.append("  task_profile_mismatch: inferred task profile does not match the expected scenario route")

    gated = gated_scenarios or tuple(report["scenarios"].keys())
    failing_gated = [scenario_name for scenario_name in gated if not report["scenarios"].get(scenario_name, {}).get("ready", False)]
    if failing_gated:
        lines.append(f"Gated scenarios failed: {', '.join(failing_gated)}")
    else:
        lines.append("All gated scenarios passed.")

    benchmark_gated = benchmark_gated_scenarios or ()
    if benchmark_gated:
        failing_benchmark = [
            scenario_name
            for scenario_name in benchmark_gated
            if not report["scenarios"].get(scenario_name, {}).get("benchmark_ready", False)
        ]
        if failing_benchmark:
            lines.append(f"Gated benchmark scenarios failed: {', '.join(failing_benchmark)}")
        else:
            lines.append("All gated benchmark scenarios passed.")
    return "\n".join(lines)


def main(argv: list[str] | None = None, *, inputs: TaskEvalInputs | None = None) -> int:
    args = list(argv or [])
    gated_scenarios = tuple(_SCENARIO_SPECS.keys())
    benchmark_gated_scenarios: tuple[str, ...] = ()
    for arg in args:
        if arg.startswith("--fail-on="):
            gated_scenarios = tuple(part.strip() for part in arg.split("=", 1)[1].split(",") if part.strip())
        elif arg.startswith("--fail-on-benchmark="):
            benchmark_gated_scenarios = tuple(part.strip() for part in arg.split("=", 1)[1].split(",") if part.strip())

    report = evaluate_task_readiness(inputs)
    text = render_task_readiness_report(
        report,
        gated_scenarios=gated_scenarios,
        benchmark_gated_scenarios=benchmark_gated_scenarios,
    )
    print(text)

    blocked = any(not report["scenarios"].get(name, {}).get("ready", False) for name in gated_scenarios)
    benchmark_blocked = any(
        not report["scenarios"].get(name, {}).get("benchmark_ready", False)
        for name in benchmark_gated_scenarios
    )
    return 1 if blocked or benchmark_blocked else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
