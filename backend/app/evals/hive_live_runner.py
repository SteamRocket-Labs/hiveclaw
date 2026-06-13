"""E2/G0/G1: Hive agent-core live behavior runner, ledger wiring, and grader.

Closes G0 (no Hive agent-core live runner) and G1 (live behavior results never
reached the ledger / promotion). Unlike ``bakeoff_runtime`` (which shells out to
external claude/hermes CLIs), this runner drives Hive's OWN ``invoke_agent`` on
the same behavior task workspaces and scores with the SAME external hard graders,
so "did Hive's agent behave correctly" is measured against
agent-improvement-proof, deterministic checks.

Honesty boundary (round2 §9): a result counts as a passing gate signal ONLY when
it is a COMPLETE live run on a TRUSTED transport. Diagnostic fallbacks
(repo_evidence, unavailable, partial, mixed) may be recorded but never pass the
gate — ``behavior_eval_passed`` / ``record_behavior_eval_run`` enforce this.

``invoke_agent`` needs DB + LLM + a workspace-bound tool_executor, so it cannot
run inside the network-isolated microVM (§1.3). The Hive live runner therefore
runs in the main backend; ``build_invoke_agent_runner`` is the real (injectable)
wiring, and E8's CI job supplies the eval tenant/agent + tool_executor to run it
live end-to-end.
"""

from __future__ import annotations

import argparse
import asyncio
import inspect
import json
import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from time import monotonic
from types import SimpleNamespace
from typing import Any

from app.evals.bakeoff_runtime import (
    ProcessRunResult,
    _extract_first_json_object,
    _scenario_workspace,
    _score_runtime_scenario,
)
from app.services.evolution_ledger import record_eval_run

# Behavior scenarios with deterministic external ground-truth (match the E1
# baseline suite). Softer scenarios (delegation, self_evolution) stay out of the
# hard gate and belong to the nightly observational layer.
DETERMINISTIC_BEHAVIOR_SCENARIOS: tuple[str, ...] = (
    "coding",
    "review",
    "research",
    "operations",
    "memory_recall",
    "long_context_after_compaction",
)

# A behavior result is gate-trusted ONLY on a complete live run. Everything else
# (diagnostic fallback, partial, unavailable, mixed) is observational, never a pass.
TRUSTED_LIVE_TRANSPORTS: frozenset[str] = frozenset({"hive_live", "live_cli"})

AgentRunner = Callable[[str, Path], Awaitable[dict[str, Any]]]

_EVAL_WORKSPACE_TOOL_NAMES: frozenset[str] = frozenset(
    {
        "list_files",
        "read_file",
        "write_file",
        "edit_file",
        "delete_file",
        "glob_search",
        "grep_search",
        "read_document",
        "execute_code",
        "run_command",
        "fs_read",
        "fs_write",
        "fs_list",
    }
)


def behavior_eval_passed(report: dict[str, Any]) -> bool:
    """A behavior report passes the gate only on a complete, trusted live run."""

    if report.get("fallback_used"):
        return False
    if not report.get("benchmark_complete"):
        return False
    if report.get("transport") not in TRUSTED_LIVE_TRANSPORTS:
        return False
    scenarios = report.get("scenarios") or {}
    if not scenarios:
        return False
    return all(bool(entry.get("ready")) for entry in scenarios.values())


async def run_hive_behavior_eval(
    *,
    agent_runner: AgentRunner,
    output_dir: Path,
    scenarios: tuple[str, ...] = DETERMINISTIC_BEHAVIOR_SCENARIOS,
) -> dict[str, Any]:
    """Run Hive's agent-core on each behavior scenario; score with hard graders.

    ``agent_runner`` is the injection seam for ``invoke_agent`` (see
    ``build_invoke_agent_runner``). If it raises on any scenario the whole run is
    marked unavailable and fail-closed (never a passing transport).
    """

    runtime_root = output_dir / "hive_live_workspaces"
    runtime_root.mkdir(parents=True, exist_ok=True)
    scenario_reports: dict[str, Any] = {}
    unavailable = False
    for name in scenarios:
        scenario = _scenario_workspace(runtime_root, name)
        started = monotonic()
        try:
            payload = await agent_runner(scenario.prompt, scenario.workspace_dir)
        except Exception as exc:  # fail-closed: agent core could not run
            unavailable = True
            scenario_reports[name] = {
                "ready": False,
                "score": 0,
                "transcript": f"agent_runner failed: {exc}",
                "rubric": scenario.rubric,
                "score_breakdown": {"transport": "hive_live_unavailable", "reason": "agent_runner_error"},
            }
            continue
        duration_ms = int((monotonic() - started) * 1000)
        run_result = ProcessRunResult(
            command=["invoke_agent"],
            cwd=str(scenario.workspace_dir),
            returncode=0,
            stdout=json.dumps(payload, ensure_ascii=False),
            stderr="",
            duration_ms=duration_ms,
        )
        scenario_reports[name] = _score_runtime_scenario(scenario, payload, run_result)

    return {
        "kind": "behavior_eval",
        "transport": "hive_live_unavailable" if unavailable else "hive_live",
        "runtime": {"executable": "invoke_agent"},
        "benchmark_complete": not unavailable,
        "fallback_used": False,
        "scenarios": scenario_reports,
    }


def _behavior_reward(report: dict[str, Any]) -> float:
    scenarios = report.get("scenarios") or {}
    scores = [float(entry.get("score") or 0.0) for entry in scenarios.values()]
    if not scores:
        return 0.0
    return round(sum(scores) / len(scores) / 100.0, 4)


def record_behavior_eval_run(
    workspace: Path,
    *,
    candidate_id: str,
    report: dict[str, Any],
    dataset: str = "behavior_eval",
    baseline_reward: float = 0.0,
) -> dict[str, Any]:
    """Bridge a behavior report into the evolution ledger (G1).

    The ``passed`` flag is the gate signal: a fallback/partial/unavailable
    transport records the run but never passes (round2 §9 honesty rule).
    """

    passed = behavior_eval_passed(report)
    scenarios = report.get("scenarios") or {}
    not_ready = sum(1 for entry in scenarios.values() if not entry.get("ready"))
    return record_eval_run(
        workspace,
        candidate_id=candidate_id,
        dataset=dataset,
        reward=_behavior_reward(report),
        baseline_reward=baseline_reward,
        passed=passed,
        traces=[str(path) for path in report.get("artifact_paths") or []],
        critical_regressions=0 if passed else not_ready,
        metadata={
            "behavior_eval": True,
            "transport": report.get("transport"),
            "benchmark_complete": report.get("benchmark_complete"),
            "fallback_used": report.get("fallback_used"),
        },
    )


async def _maybe_await_tool_result(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


def _eval_initial_tools() -> list[dict[str, Any]]:
    from app.services.agent_tools import get_combined_openai_tools

    return [
        tool
        for tool in get_combined_openai_tools()
        if tool.get("function", {}).get("name") in _EVAL_WORKSPACE_TOOL_NAMES
    ]


def build_workspace_tool_executor(
    workspace_dir: Path, *, tenant_id: str | None = None
) -> Callable[..., Awaitable[str]]:
    """Build a scenario-local filesystem executor for live behavior eval.

    The evaluator only needs local workspace tools. Binding the executor to the
    scenario workspace makes the grader inspect the same files the agent wrote.
    """

    from app.tools.handlers import filesystem

    handlers: dict[str, Callable[..., Any]] = {
        "list_files": filesystem.list_files,
        "read_file": filesystem.read_file,
        "write_file": filesystem.write_file,
        "edit_file": filesystem.edit_file,
        "delete_file": filesystem.delete_file,
        "glob_search": filesystem.glob_search,
        "grep_search": filesystem.grep_search,
        "read_document": filesystem.read_document,
        "execute_code": filesystem.execute_code,
        "run_command": filesystem.run_command,
        "fs_read": filesystem.fs_read,
        "fs_write": filesystem.fs_write,
        "fs_list": filesystem.fs_list,
    }

    async def execute(tool_name: str, args: dict[str, Any] | None = None, **_: Any) -> str:
        handler = handlers.get(tool_name)
        if handler is None:
            return f"[eval tool unavailable] {tool_name}"
        result = await _maybe_await_tool_result(handler(workspace_dir, args or {}, tenant_id))
        return str(result)

    return execute


def build_invoke_agent_runner(
    *,
    model: Any,
    agent_name: str,
    role_description: str,
    agent_id: Any = None,
    user_id: Any = None,
    tool_executor: Any = None,
    invoke: Callable[[Any], Awaitable[Any]] | None = None,
) -> AgentRunner:
    """Real (injectable) wiring from a behavior task to Hive's ``invoke_agent``.

    Defaults to the production ``invoke_agent``. ``tool_executor`` (workspace
    bound), ``agent_id`` and ``user_id`` are supplied by the eval harness / E8 CI
    job so the agent's filesystem tools write into the scenario workspace the hard
    grader inspects. ``invoke`` is injected in tests to exercise request
    construction + payload extraction without DB/LLM.
    """

    async def agent_runner(prompt: str, workspace_dir: Path) -> dict[str, Any]:
        from app.runtime.invoker import AgentInvocationRequest

        scenario_tool_executor = tool_executor or build_workspace_tool_executor(workspace_dir)
        task_prompt = (
            f"{prompt}\n\nYour evaluation workspace is: {workspace_dir}\n"
            "Use only local workspace files; do not use the network."
        )
        request = AgentInvocationRequest(
            model=model,
            messages=[{"role": "user", "content": task_prompt}],
            agent_name=agent_name,
            role_description=role_description,
            agent_id=agent_id,
            user_id=user_id,
            tool_executor=scenario_tool_executor,
            initial_tools=_eval_initial_tools(),
            allowed_tool_names=tuple(sorted(_EVAL_WORKSPACE_TOOL_NAMES)),
            invocation_scope="eval",
            core_tools_only=True,
        )
        if invoke is not None:
            _invoke = invoke
        else:
            from app.runtime.invoker import invoke_agent as _invoke
        result = await _invoke(request)
        content = getattr(result, "content", None) or str(result)
        try:
            return _extract_first_json_object(content)
        except ValueError:
            return {
                "status": "unknown",
                "answer": content,
                "evidence": [],
                "files_created": [],
                "used_parallelism": False,
                "notes": "no json object in agent output",
            }

    return agent_runner


async def _run_cli(args: argparse.Namespace) -> int:
    api_key = os.environ.get(args.api_key_env, "")
    model = SimpleNamespace(
        provider=args.provider,
        model=args.model,
        api_key=api_key,
        base_url=args.base_url,
        max_output_tokens=args.max_output_tokens,
    )
    runner = build_invoke_agent_runner(
        model=model,
        agent_name=args.agent_name,
        role_description=args.role_description,
        agent_id=args.agent_id,
        user_id=args.user_id,
    )
    report = await run_hive_behavior_eval(
        agent_runner=runner,
        output_dir=args.output.parent,
        scenarios=tuple(args.scenario) if args.scenario else DETERMINISTIC_BEHAVIOR_SCENARIOS,
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
    print(json.dumps({"transport": report["transport"], "benchmark_complete": report["benchmark_complete"]}))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run Hive invoke_agent live behavior eval and write a JSON report.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--provider", default=os.environ.get("HIVE_EVAL_PROVIDER", "anthropic"))
    parser.add_argument("--api-key-env", default="HIVE_EVAL_LLM_API_KEY")
    parser.add_argument("--base-url", default=os.environ.get("HIVE_EVAL_BASE_URL") or None)
    parser.add_argument("--max-output-tokens", type=int, default=None)
    parser.add_argument("--agent-name", default="Hive behavior eval agent")
    parser.add_argument(
        "--role-description",
        default="Complete behavior-eval tasks by editing files in the provided local evaluation workspace.",
    )
    parser.add_argument("--agent-id", default=os.environ.get("HIVE_EVAL_AGENT_ID") or None)
    parser.add_argument("--user-id", default=os.environ.get("HIVE_EVAL_USER_ID") or None)
    parser.add_argument("--scenario", choices=DETERMINISTIC_BEHAVIOR_SCENARIOS, action="append")
    args = parser.parse_args(argv)
    return asyncio.run(_run_cli(args))


if __name__ == "__main__":
    raise SystemExit(main())
