"""Unified evaluation runner for internal and bakeoff comparisons."""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
import uuid

from app.evals.bakeoff_runtime import run_runtime_bakeoff
from app.runtime.prompt_eval import PromptEvalInputs, evaluate_runtime_prompt_contracts
from app.runtime.recovery_manifest import RecoveryManifest, merge_session_memory_into_manifest
from app.runtime.task_eval import TaskEvalInputs, evaluate_task_readiness
from app.services.session_memory import (
    SessionMemoryPayload,
    load_session_memory,
    render_session_memory_excerpt,
    update_session_memory,
    write_compaction_summary,
)
from app.services.skill_lifecycle import record_skill_execution

_VALID_TARGETS = {"clawith", "claude_code", "hermes_agent"}
_VALID_MODES = {"internal", "bakeoff"}
_VALID_ABLATIONS = {"full", "no_memory", "no_skill", "no_compaction"}
_VALID_SUITES = {"core_v1", "continuity_v1", "skill_v1"}
_SKILL_MATCH_STOPWORDS = {"use", "the", "for", "and", "with", "a", "an", "to"}


def _resolve_output_dir(
    *,
    suite: str,
    target: str,
    mode: str,
    ablation: str,
    output_root: str | Path | None,
) -> Path:
    root = Path(output_root) if output_root is not None else (Path.home() / ".hive" / "evals")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = root / suite / target / f"{mode}-{ablation}" / stamp
    path.mkdir(parents=True, exist_ok=True)
    (path / "scenarios").mkdir(exist_ok=True)
    return path


def _ablation_inputs(ablation: str) -> tuple[PromptEvalInputs | None, TaskEvalInputs | None]:
    if ablation == "full":
        return None, None
    if ablation == "no_memory":
        prompt_inputs = PromptEvalInputs(memory_section="## Your Memory System\n(save only)\n")
        task_inputs = TaskEvalInputs(
            prompt_inputs=prompt_inputs,
            scenario_prompts={"session_recall": "## Task Playbook\n- keep it generic\n"},
        )
        return prompt_inputs, task_inputs
    if ablation == "no_skill":
        prompt_inputs = PromptEvalInputs(
            tools_section="## Using Your Tools\n(load only)\n",
            heartbeat_template="# Heartbeat\n(no save_skill guidance)\n",
        )
        task_inputs = TaskEvalInputs(
            prompt_inputs=prompt_inputs,
            tool_names={
                "read_file",
                "write_file",
                "edit_file",
                "glob_search",
                "grep_search",
                "web_search",
                "web_fetch",
                "search_memory",
                "save_memory",
                "delegate_to_agent",
                "check_async_task",
                "list_async_tasks",
                "get_current_time",
                "list_triggers",
                "set_trigger",
            },
        )
        return prompt_inputs, task_inputs
    if ablation == "no_compaction":
        return None, None
    raise ValueError(f"Unsupported ablation: {ablation}")


def _internal_scenario_report(ablation: str) -> dict[str, Any]:
    prompt_inputs, task_inputs = _ablation_inputs(ablation)
    prompt_report = evaluate_runtime_prompt_contracts(prompt_inputs)
    task_report = evaluate_task_readiness(task_inputs)

    coding_ready = bool(task_report["scenarios"]["coding_review"]["ready"])
    review_ready = bool(
        task_report["scenarios"]["coding_review"]["ready"]
        and task_report["scenarios"]["coding_review"]["benchmark_ready"]
    )
    research_ready = bool(task_report["scenarios"]["research"]["ready"])
    operations_ready = bool(task_report["scenarios"]["operations"]["ready"])
    delegation_ready = bool(
        task_report["scenarios"]["delegation_worker"]["ready"]
        and task_report["scenarios"]["delegation_profile_matrix"]["ready"]
    )
    memory_ready = bool(task_report["scenarios"]["session_recall"]["ready"])
    evolution_ready = bool(task_report["scenarios"]["self_evolution"]["ready"])
    long_context_ready = bool(
        task_report["scenarios"]["long_context_after_compaction"]["ready"]
        and ablation != "no_compaction"
    )

    scenarios = {
        "coding": {
            "ready": coding_ready,
            "score": 100 if coding_ready else 0,
            "transcript": "task_eval:coding_review",
            "rubric": "coding task readiness and required tool surface",
            "score_breakdown": {"task_ready": coding_ready},
        },
        "review": {
            "ready": review_ready,
            "score": 100 if review_ready else 0,
            "transcript": "task_eval:coding_review benchmark",
            "rubric": "review findings-first overlay and benchmark compliance",
            "score_breakdown": {"benchmark_ready": review_ready},
        },
        "research": {
            "ready": research_ready,
            "score": task_report["scenarios"]["research"]["score"],
            "transcript": "task_eval:research",
            "rubric": "primary-source research readiness",
            "score_breakdown": {"task_ready": research_ready},
        },
        "operations": {
            "ready": operations_ready,
            "score": task_report["scenarios"]["operations"]["score"],
            "transcript": "task_eval:operations",
            "rubric": "live-state verification and blast-radius control",
            "score_breakdown": {"task_ready": operations_ready},
        },
        "delegation": {
            "ready": delegation_ready,
            "score": 100 if delegation_ready else 0,
            "transcript": "task_eval:delegation_worker + delegation_profile_matrix",
            "rubric": "worker delegation safety and profile boundaries",
            "score_breakdown": {
                "delegation_worker": task_report["scenarios"]["delegation_worker"]["ready"],
                "delegation_profile_matrix": task_report["scenarios"]["delegation_profile_matrix"]["ready"],
            },
        },
        "memory_recall": {
            "ready": memory_ready,
            "score": task_report["scenarios"]["session_recall"]["score"],
            "transcript": "task_eval:session_recall",
            "rubric": "search-memory-first recall with session evidence",
            "score_breakdown": {"task_ready": memory_ready},
        },
        "self_evolution": {
            "ready": evolution_ready,
            "score": task_report["scenarios"]["self_evolution"]["score"],
            "transcript": "task_eval:self_evolution",
            "rubric": "save_skill only for repeated successful workflows",
            "score_breakdown": {"task_ready": evolution_ready},
        },
        "long_context_after_compaction": {
            "ready": long_context_ready,
            "score": task_report["scenarios"]["long_context_after_compaction"]["score"] if ablation != "no_compaction" else 0,
            "transcript": "task_eval:long_context_after_compaction",
            "rubric": "continuity preserved after compaction",
            "score_breakdown": {
                "scenario_ready": task_report["scenarios"]["long_context_after_compaction"]["ready"],
                "compaction_enabled": ablation != "no_compaction",
                "prompt_memory_contract": prompt_report["checks"]["memory_usage_guidance"]["passed"],
            },
        },
    }

    return {
        "kind": "internal",
        "prompt_report": prompt_report,
        "task_report": task_report,
        "scenarios": scenarios,
    }


def _continuity_scenario_report(output_dir: Path) -> dict[str, Any]:
    data_root = output_dir / "_continuity_runtime"
    agent_id = uuid.uuid4()
    session_id = "continuity-suite"
    round_payloads = [
        SessionMemoryPayload(
            session_id=session_id,
            source="pre_compaction",
            session_title="Continuity Suite",
            current_state="Round 1 compaction summary was persisted.",
            task_spec="Preserve continuity across repeated compactions.",
            important_files=["backend/app/runtime/prompt_builder.py", "backend/app/kernel/engine.py"],
            key_results=["Round 1 state persisted before compaction."],
            pending_work=["Prepare round 2 continuity restore."],
            worklog=["Round 1: persisted continuity state before compaction."],
            compaction_count=1,
            last_compaction_at="2026-04-09T01:00:00Z",
        ),
        SessionMemoryPayload(
            session_id=session_id,
            source="pre_compaction",
            session_title="Continuity Suite",
            current_state="Round 2 compaction summary preserved task state.",
            task_spec="Preserve continuity across repeated compactions.",
            important_files=["backend/app/runtime/prompt_builder.py", "backend/app/runtime/recovery_manifest.py"],
            key_results=["Round 2 kept the latest pending work."],
            pending_work=["Prepare round 3 continuity restore."],
            worklog=["Round 2: verified pending work survived compaction."],
            compaction_count=2,
            last_compaction_at="2026-04-09T01:05:00Z",
        ),
        SessionMemoryPayload(
            session_id=session_id,
            source="pre_compaction",
            session_title="Continuity Suite",
            current_state="Round 3 continuity state is ready for restore.",
            task_spec="Preserve continuity across repeated compactions.",
            important_files=["backend/app/services/session_memory.py", "backend/app/runtime/recovery_manifest.py"],
            key_results=["Round 3 captured current state and key files."],
            pending_work=["Restore the session and continue execution."],
            worklog=["Round 3: final continuity checkpoint before restore."],
            compaction_count=3,
            last_compaction_at="2026-04-09T01:10:00Z",
        ),
    ]

    scenarios: dict[str, dict[str, Any]] = {}
    for index, payload in enumerate(round_payloads, start=1):
        update_session_memory(agent_id, payload, data_root=data_root)
        write_compaction_summary(
            agent_id,
            f"Round {index} compacted successfully.",
            original_message_count=20 + index,
            kept_message_count=8 + index,
            data_root=data_root,
        )
        loaded = load_session_memory(agent_id, data_root=data_root)
        excerpt = render_session_memory_excerpt(loaded or SessionMemoryPayload(), budget_chars=800)
        ready = bool(
            loaded
            and loaded.compaction_count == index
            and payload.current_state in excerpt
            and payload.pending_work[0] in excerpt
        )
        scenarios[f"compaction_round_{index}"] = {
            "ready": ready,
            "score": 100 if ready else 0,
            "transcript": excerpt,
            "rubric": f"continuity survives compaction round {index}",
            "score_breakdown": {
                "compaction_count": loaded.compaction_count if loaded else 0,
                "continuity_excerpt_nonempty": bool(excerpt),
            },
        }

    manifest = merge_session_memory_into_manifest(
        RecoveryManifest(session_id=session_id),
        agent_id=agent_id,
        data_root=data_root,
    )
    restore_ready = (
        "Restore the session and continue execution." in manifest.pending_items
        and any("Round 3 captured current state and key files." in item.get("summary", "") for item in manifest.recent_tool_outcomes)
    )
    scenarios["session_restore"] = {
        "ready": restore_ready,
        "score": 100 if restore_ready else 0,
        "transcript": manifest.to_restoration_text(),
        "rubric": "session restore receives the latest continuity state",
        "score_breakdown": {
            "pending_items": len(manifest.pending_items),
            "tool_outcomes": len(manifest.recent_tool_outcomes),
        },
    }
    return {"kind": "internal", "transport": "internal", "scenarios": scenarios}


def _skill_match_confidence(query: str, skill_name: str, summary: str) -> float:
    haystack = f"{skill_name} {summary}".lower()
    query_tokens = {
        token
        for token in query.lower().replace("-", " ").split()
        if token and token not in _SKILL_MATCH_STOPWORDS and len(token) > 2
    }
    if not query_tokens:
        return 0.0
    matched = sum(1 for token in query_tokens if token in haystack)
    return round(matched / len(query_tokens), 2)


def _skill_scenario_report(output_dir: Path) -> dict[str, Any]:
    prompt_report = evaluate_runtime_prompt_contracts()
    workspace = output_dir / "_skill_runtime"
    catalog = [
        {"name": "deploy-checklist", "summary": "canary rollout rollback logs promote globally"},
        {"name": "incident-response", "summary": "rollback guidance alerts remediation runbook"},
    ]
    top_match = max(
        (
            {
                "name": entry["name"],
                "confidence": _skill_match_confidence(
                    "use the deploy checklist for canary rollout and rollback logs",
                    entry["name"],
                    entry["summary"],
                ),
            }
            for entry in catalog
        ),
        key=lambda item: item["confidence"],
    )
    promote_result = record_skill_execution(
        workspace,
        skill_name="deploy-checklist",
        workflow_signature="deploy-checklist",
        status="success",
        used_skill=False,
        note="First stable deploy checklist run.",
        occurred_at="2026-04-01T10:00:00Z",
    )
    promote_result = record_skill_execution(
        workspace,
        skill_name="deploy-checklist",
        workflow_signature="deploy-checklist",
        status="success",
        used_skill=False,
        note="Second stable deploy checklist run.",
        occurred_at="2026-04-05T10:00:00Z",
    )
    patch_result = record_skill_execution(
        workspace,
        skill_name="incident-response",
        workflow_signature="incident-response",
        status="failed",
        used_skill=True,
        note="Loaded skill missed rollback guidance.",
        blocker="missing rollback guidance",
        occurred_at="2026-04-01T10:00:00Z",
    )
    patch_result = record_skill_execution(
        workspace,
        skill_name="incident-response",
        workflow_signature="incident-response",
        status="workaround",
        used_skill=True,
        note="Patched manually after a loaded-skill miss.",
        blocker="missing rollback guidance",
        occurred_at="2026-04-03T10:00:00Z",
    )

    scenarios = {
        "trigger_precision": {
            "ready": top_match["name"] == "deploy-checklist" and top_match["confidence"] >= 0.70,
            "score": 100 if top_match["name"] == "deploy-checklist" and top_match["confidence"] >= 0.70 else 0,
            "transcript": json.dumps(top_match, ensure_ascii=False),
            "rubric": "matching skills should cross the load-before-act confidence threshold",
            "score_breakdown": top_match,
        },
        "load_before_act_compliance": {
            "ready": bool(prompt_report["checks"]["skill_load_before_act_guidance"]["passed"]),
            "score": 100 if prompt_report["checks"]["skill_load_before_act_guidance"]["passed"] else 0,
            "transcript": prompt_report["checks"]["skill_load_before_act_guidance"]["detail"],
            "rubric": "matching skills must be loaded before non-read-only actions",
            "score_breakdown": prompt_report["checks"]["skill_load_before_act_guidance"],
        },
        "save_skill_precision": {
            "ready": promote_result["decision"] == "promote",
            "score": 100 if promote_result["decision"] == "promote" else 0,
            "transcript": json.dumps(promote_result, ensure_ascii=False),
            "rubric": "repeated successful workflows should promote into skill candidates and then skills",
            "score_breakdown": promote_result,
        },
        "stale_skill_patch_recovery": {
            "ready": patch_result["decision"] == "patch",
            "score": 100 if patch_result["decision"] == "patch" else 0,
            "transcript": json.dumps(patch_result, ensure_ascii=False),
            "rubric": "repeated loaded-skill misses should route to patch instead of duplicate creation",
            "score_breakdown": patch_result,
        },
    }
    return {"kind": "internal", "transport": "internal", "scenarios": scenarios}


def _summarize_scores(scenarios: dict[str, dict[str, Any]]) -> dict[str, Any]:
    scores = [int(details["score"]) for details in scenarios.values()]
    ready = sum(1 for details in scenarios.values() if details["ready"])
    return {
        "scenario_count": len(scenarios),
        "ready_scenarios": ready,
        "pass_rate": round((ready / len(scenarios)) * 100, 2) if scenarios else 0.0,
        "average_score": round(sum(scores) / len(scores), 2) if scores else 0.0,
    }


def _build_analysis(scenarios: dict[str, dict[str, Any]]) -> dict[str, Any]:
    ranked = sorted(
        scenarios.items(),
        key=lambda item: (-int(item[1]["score"]), item[0]),
    )
    lowest_ranked = sorted(
        scenarios.items(),
        key=lambda item: (int(item[1]["score"]), item[0]),
    )

    def _analysis_entry(scenario_name: str, details: dict[str, Any]) -> dict[str, Any]:
        return {
            "scenario": scenario_name,
            "score": int(details["score"]),
            "ready": bool(details["ready"]),
            "summary": details["rubric"],
        }

    strengths = [
        _analysis_entry(scenario_name, details)
        for scenario_name, details in ranked
        if details["ready"] and int(details["score"]) >= 90
    ][:4]
    if not strengths:
        strengths = [_analysis_entry(scenario_name, details) for scenario_name, details in ranked[: min(3, len(ranked))]]

    gaps = [
        _analysis_entry(scenario_name, details)
        for scenario_name, details in lowest_ranked
        if (not details["ready"]) or int(details["score"]) < 85
    ][:4]

    top_score = int(ranked[0][1]["score"]) if ranked else 0
    bottom_score = int(lowest_ranked[0][1]["score"]) if lowest_ranked else 0

    return {
        "strengths": strengths,
        "gaps": gaps,
        "highest_scoring_scenarios": [
            scenario_name for scenario_name, details in ranked if int(details["score"]) == top_score
        ],
        "lowest_scoring_scenarios": [
            scenario_name for scenario_name, details in lowest_ranked if int(details["score"]) == bottom_score
        ],
    }


def _write_report(report: dict[str, Any], output_dir: Path) -> None:
    scenario_dir = output_dir / "scenarios"
    for scenario_name, details in report["scenarios"].items():
        (scenario_dir / f"{scenario_name}.json").write_text(
            json.dumps(details, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )
    (output_dir / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    markdown_lines = [
        f"# Eval Report: {report['suite']} / {report['target']}",
        "",
        f"- Mode: {report['mode']}",
        f"- Ablation: {report['ablation']}",
        f"- Transport: {report.get('transport', 'internal')}",
        f"- Average Score: {report['summary']['average_score']}",
        f"- Pass Rate: {report['summary']['pass_rate']}%",
    ]
    if report.get("runtime"):
        markdown_lines.append(f"- Runtime Status: {report['runtime'].get('status', 'unknown')}")
    if report.get("auth_status") is not None:
        markdown_lines.append(f"- Auth Status: {report['auth_status']}")
    if report.get("benchmark_complete") is not None:
        markdown_lines.append(f"- Benchmark Complete: {'yes' if report['benchmark_complete'] else 'no'}")
    fallback = report.get("evidence", {}).get("fallback")
    fallback_transport = fallback.get("transport") if isinstance(fallback, dict) else None
    if fallback_transport:
        markdown_lines.append(f"- Fallback Transport: {fallback_transport}")
    artifact_paths = report.get("artifact_paths") or []
    if artifact_paths:
        markdown_lines.append(f"- Artifact Paths: {', '.join(artifact_paths[:4])}")
    markdown_lines.extend(
        [
            "",
            "## Strengths",
        ]
    )
    for item in report["analysis"]["strengths"]:
        markdown_lines.append(
            f"- {item['scenario']}: score={item['score']} ready={'yes' if item['ready'] else 'no'} | {item['summary']}"
        )
    if not report["analysis"]["strengths"]:
        markdown_lines.append("- None")

    markdown_lines.extend(
        [
            "",
            "## Gaps",
        ]
    )
    for item in report["analysis"]["gaps"]:
        markdown_lines.append(
            f"- {item['scenario']}: score={item['score']} ready={'yes' if item['ready'] else 'no'} | {item['summary']}"
        )
    if not report["analysis"]["gaps"]:
        markdown_lines.append("- None")

    markdown_lines.extend(
        [
            "",
            "## Leaderboard",
            f"- Highest Scoring: {', '.join(report['analysis']['highest_scoring_scenarios']) or 'None'}",
            f"- Lowest Scoring: {', '.join(report['analysis']['lowest_scoring_scenarios']) or 'None'}",
            "",
            "## Scenarios",
        ]
    )
    for scenario_name, details in report["scenarios"].items():
        markdown_lines.append(
            f"- {scenario_name}: score={details['score']} ready={'yes' if details['ready'] else 'no'}"
        )
    (output_dir / "report.md").write_text("\n".join(markdown_lines).rstrip() + "\n", encoding="utf-8")


def run_eval_suite(
    *,
    suite: str,
    target: str,
    mode: str,
    ablation: str = "full",
    output_root: str | Path | None = None,
) -> dict[str, Any]:
    if suite not in _VALID_SUITES:
        raise ValueError(f"Unsupported suite: {suite}")
    if target not in _VALID_TARGETS:
        raise ValueError(f"Unsupported target: {target}")
    if mode not in _VALID_MODES:
        raise ValueError(f"Unsupported mode: {mode}")
    if ablation not in _VALID_ABLATIONS:
        raise ValueError(f"Unsupported ablation: {ablation}")

    output_dir = _resolve_output_dir(
        suite=suite,
        target=target,
        mode=mode,
        ablation=ablation,
        output_root=output_root,
    )
    if mode == "internal":
        if suite == "core_v1":
            scenario_bundle = _internal_scenario_report(ablation)
        elif suite == "continuity_v1":
            scenario_bundle = _continuity_scenario_report(output_dir)
        else:
            scenario_bundle = _skill_scenario_report(output_dir)
    else:
        if suite != "core_v1":
            raise ValueError(f"Bakeoff mode only supports core_v1, got: {suite}")
        scenario_bundle = run_runtime_bakeoff(target, output_dir=output_dir)
    report = {
        "suite": suite,
        "target": target,
        "mode": mode,
        "ablation": ablation,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "output_dir": str(output_dir),
        "transport": scenario_bundle.get("transport", "internal"),
        "runtime": scenario_bundle.get("runtime"),
        "auth_status": scenario_bundle.get("auth_status"),
        "fallback_used": scenario_bundle.get("fallback_used"),
        "benchmark_complete": scenario_bundle.get("benchmark_complete"),
        "artifact_paths": scenario_bundle.get("artifact_paths", []),
        "repo_root": scenario_bundle.get("repo_root"),
        "scenarios": scenario_bundle["scenarios"],
        "summary": _summarize_scores(scenario_bundle["scenarios"]),
        "analysis": _build_analysis(scenario_bundle["scenarios"]),
        "evidence": {key: value for key, value in scenario_bundle.items() if key != "scenarios"},
    }
    _write_report(report, output_dir)
    return report


def main(argv: list[str] | None = None, *, output_root: str | Path | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run unified Clawith eval suites.")
    parser.add_argument("--suite", required=True)
    parser.add_argument("--target", required=True)
    parser.add_argument("--mode", required=True)
    parser.add_argument("--ablation", default="full")
    args = parser.parse_args(argv)

    report = run_eval_suite(
        suite=args.suite,
        target=args.target,
        mode=args.mode,
        ablation=args.ablation,
        output_root=output_root,
    )
    print(
        f"[eval] suite={report['suite']} target={report['target']} mode={report['mode']} "
        f"ablation={report['ablation']} average_score={report['summary']['average_score']} "
        f"pass_rate={report['summary']['pass_rate']} output_dir={report['output_dir']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
