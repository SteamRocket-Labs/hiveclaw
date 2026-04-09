"""Live bakeoff adapters for external agent runtimes."""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from shutil import which as _which
from time import monotonic
from typing import Any


_SCENARIOS = (
    "coding",
    "review",
    "research",
    "operations",
    "delegation",
    "memory_recall",
    "self_evolution",
    "long_context_after_compaction",
)
_BAKEOFF_REPO_ENV_KEYS = {
    "claude_code": "HIVE_BAKEOFF_CLAUDE_CODE_ROOT",
    "hermes_agent": "HIVE_BAKEOFF_HERMES_AGENT_ROOT",
}
_BAKEOFF_REPO_NAMES = {
    "claude_code": "claude-code",
    "hermes_agent": "hermes-agent",
}
_RUNTIME_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "status": {"type": "string"},
        "answer": {"type": "string"},
        "evidence": {"type": "array", "items": {"type": "string"}},
        "files_created": {"type": "array", "items": {"type": "string"}},
        "used_parallelism": {"type": "boolean"},
        "notes": {"type": "string"},
    },
    "required": ["status", "answer", "evidence", "files_created", "used_parallelism", "notes"],
    "additionalProperties": False,
}


@dataclass(slots=True)
class ProcessRunResult:
    command: list[str]
    cwd: str
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int


@dataclass(slots=True)
class ScenarioWorkspace:
    name: str
    workspace_dir: Path
    prompt: str
    rubric: str


@dataclass(frozen=True, slots=True)
class RuntimeProfile:
    max_turns: int
    timeout_seconds: int
    preflight_timeout_seconds: int = 20


def _ensure_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _runtime_profile(target: str) -> RuntimeProfile:
    if target == "claude_code":
        return RuntimeProfile(max_turns=4, timeout_seconds=60, preflight_timeout_seconds=20)
    if target == "hermes_agent":
        return RuntimeProfile(max_turns=6, timeout_seconds=90, preflight_timeout_seconds=20)
    raise ValueError(f"Unsupported bakeoff target: {target}")


def build_runtime_command(target: str, *, prompt: str, workspace_dir: Path, max_turns: int = 4) -> list[str]:
    if target == "claude_code":
        return [
            "claude",
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            json.dumps(_RUNTIME_JSON_SCHEMA, separators=(",", ":")),
            "--model",
            "sonnet",
            "--effort",
            "low",
            "--max-turns",
            str(max_turns),
            "--permission-mode",
            "bypassPermissions",
            "--dangerously-skip-permissions",
            "--add-dir",
            str(workspace_dir),
            prompt,
        ]
    if target == "hermes_agent":
        return [
            "hermes",
            "chat",
            "-q",
            prompt,
            "-Q",
            "--max-turns",
            str(max_turns),
            "--yolo",
        ]
    raise ValueError(f"Unsupported bakeoff target: {target}")


def _extract_first_json_object(text: str) -> dict[str, Any]:
    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char != "{":
            continue
        try:
            payload, _ = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            return payload
    raise ValueError("No JSON object found in runtime output.")


def extract_runtime_payload(target: str, raw_output: str) -> dict[str, Any]:
    outer = _extract_first_json_object(raw_output)
    if target == "claude_code" and isinstance(outer.get("structured_output"), dict):
        return outer["structured_output"]
    if target == "claude_code" and isinstance(outer.get("result"), str):
        nested = outer["result"].strip()
        if nested.startswith("{"):
            return _extract_first_json_object(nested)
    return outer


def _run_process(command: list[str], cwd: Path, timeout_seconds: int) -> ProcessRunResult:
    started = monotonic()
    env = dict(os.environ)
    if command and command[0] == "claude":
        # Claude OAuth/keychain auth is unavailable in simple mode, so avoid
        # inheriting or forcing CLAUDE_CODE_SIMPLE during live bakeoffs.
        env.pop("CLAUDE_CODE_SIMPLE", None)
    try:
        completed = subprocess.run(  # noqa: S603
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=env,
        )
        returncode = completed.returncode
        stdout = completed.stdout
        stderr = completed.stderr
    except subprocess.TimeoutExpired as exc:
        returncode = 124
        stdout = _ensure_text(exc.stdout)
        stderr = _ensure_text(exc.stderr) + f"\nTimed out after {timeout_seconds} seconds."
    return ProcessRunResult(
        command=command,
        cwd=str(cwd),
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration_ms=int((monotonic() - started) * 1000),
    )


def _scan_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _resolve_bakeoff_repo_root(target: str) -> Path | None:
    repo_name = _BAKEOFF_REPO_NAMES.get(target)
    if repo_name is None:
        raise ValueError(f"Unsupported bakeoff target: {target}")

    env_override = os.environ.get(_BAKEOFF_REPO_ENV_KEYS[target])
    candidates: list[Path] = []
    if env_override:
        candidates.append(Path(env_override).expanduser())

    current_repo_root = Path(__file__).resolve().parents[3]
    candidates.append(current_repo_root.parent / repo_name)

    search_root = current_repo_root.parent.parent
    if search_root.exists():
        for child in search_root.iterdir():
            if child.name == repo_name:
                candidates.append(child)
                continue
            nested = child / repo_name
            if nested.exists():
                candidates.append(nested)

    seen: set[Path] = set()
    for candidate in candidates:
        resolved = candidate.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        if resolved.exists():
            return resolved
    return None


def _repo_evidence_report(target: str) -> dict[str, Any]:
    repo_root = _resolve_bakeoff_repo_root(target)
    exists = bool(repo_root and repo_root.exists())
    if not exists:
        scenarios = {
            name: {
                "ready": False,
                "score": 0,
                "transcript": "repository unavailable",
                "rubric": "source repository not found locally",
                "score_breakdown": {"repo_found": False},
            }
            for name in _SCENARIOS
        }
        return {"kind": "bakeoff", "transport": "repo_evidence", "repo_root": str(repo_root) if repo_root else "", "scenarios": scenarios}

    if target == "claude_code":
        compact_text = _scan_text(repo_root / "src/services/compact/prompt.ts")
        session_text = _scan_text(repo_root / "src/services/SessionMemory/prompts.ts")
        skill_text = _scan_text(repo_root / "src/tools/SkillTool/prompt.ts")
        scenarios = {
            "coding": {"ready": True, "score": 95, "transcript": "repo evidence", "rubric": "coding assistant maturity", "score_breakdown": {"repo_found": True}},
            "review": {"ready": True, "score": 92, "transcript": "repo evidence", "rubric": "review workflow maturity", "score_breakdown": {"repo_found": True}},
            "research": {"ready": True, "score": 88, "transcript": "repo evidence", "rubric": "research workflow maturity", "score_breakdown": {"repo_found": True}},
            "operations": {"ready": True, "score": 84, "transcript": "repo evidence", "rubric": "operations workflow maturity", "score_breakdown": {"repo_found": True}},
            "delegation": {"ready": False, "score": 60, "transcript": "repo evidence", "rubric": "delegation surface", "score_breakdown": {"parallel_agent_docs": "delegate" in skill_text.lower()}},
            "memory_recall": {"ready": "summary" in session_text.lower(), "score": 95 if "summary" in session_text.lower() else 65, "transcript": "repo evidence", "rubric": "session memory continuity", "score_breakdown": {"session_memory_prompt": "summary" in session_text.lower()}},
            "self_evolution": {"ready": "skill" in skill_text.lower(), "score": 90 if "skill" in skill_text.lower() else 50, "transcript": "repo evidence", "rubric": "skill routing/evolution", "score_breakdown": {"skill_prompt": "load" in skill_text.lower()}},
            "long_context_after_compaction": {"ready": "compact" in compact_text.lower(), "score": 96 if "compact" in compact_text.lower() else 55, "transcript": "repo evidence", "rubric": "compaction strategy", "score_breakdown": {"compact_prompt": "compact" in compact_text.lower()}},
        }
        return {"kind": "bakeoff", "transport": "repo_evidence", "repo_root": str(repo_root), "scenarios": scenarios}

    compressor_text = _scan_text(repo_root / "agent/context_compressor.py")
    skill_text = _scan_text(repo_root / "tools/skills_tool.py")
    scenarios = {
        "coding": {"ready": True, "score": 86, "transcript": "repo evidence", "rubric": "coding workflow maturity", "score_breakdown": {"repo_found": True}},
        "review": {"ready": True, "score": 82, "transcript": "repo evidence", "rubric": "review workflow maturity", "score_breakdown": {"repo_found": True}},
        "research": {"ready": True, "score": 85, "transcript": "repo evidence", "rubric": "research workflow maturity", "score_breakdown": {"repo_found": True}},
        "operations": {"ready": True, "score": 80, "transcript": "repo evidence", "rubric": "operations workflow maturity", "score_breakdown": {"repo_found": True}},
        "delegation": {"ready": False, "score": 58, "transcript": "repo evidence", "rubric": "delegation surface", "score_breakdown": {"repo_found": True}},
        "memory_recall": {"ready": "compress" in compressor_text.lower(), "score": 88 if "compress" in compressor_text.lower() else 55, "transcript": "repo evidence", "rubric": "memory/continuity surface", "score_breakdown": {"compressor": "compress" in compressor_text.lower()}},
        "self_evolution": {"ready": "skill" in skill_text.lower(), "score": 85 if "skill" in skill_text.lower() else 45, "transcript": "repo evidence", "rubric": "skill surface", "score_breakdown": {"skill_tool": "skill" in skill_text.lower()}},
        "long_context_after_compaction": {"ready": "compress" in compressor_text.lower(), "score": 90 if "compress" in compressor_text.lower() else 55, "transcript": "repo evidence", "rubric": "compaction strategy", "score_breakdown": {"compressor": "compress" in compressor_text.lower()}},
    }
    return {"kind": "bakeoff", "transport": "repo_evidence", "repo_root": str(repo_root), "scenarios": scenarios}


def _write_files(workspace_dir: Path, files: dict[str, str]) -> None:
    for relative_path, content in files.items():
        target = workspace_dir / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")


def _scenario_workspace(base_dir: Path, scenario_name: str) -> ScenarioWorkspace:
    workspace_dir = base_dir / scenario_name
    workspace_dir.mkdir(parents=True, exist_ok=True)

    common_prompt = (
        "You are running inside a temporary evaluation workspace.\n"
        "Read TASK.md and complete the task using only local workspace files.\n"
        "Do not use the network.\n"
        "When finished, respond with exactly one JSON object matching this shape:\n"
        '{"status":"success|partial|failed","answer":"short answer","evidence":["..."],"files_created":["..."],"used_parallelism":false,"notes":"short note"}\n'
        "Do not wrap the JSON in markdown fences."
    )

    if scenario_name == "coding":
        _write_files(
            workspace_dir,
            {
                "TASK.md": "Fix calculator.py so add(a, b) returns the correct sum.",
                "calculator.py": "def add(a, b):\n    return a - b\n",
            },
        )
        return ScenarioWorkspace(scenario_name, workspace_dir, common_prompt, "coding task readiness and required tool surface")
    if scenario_name == "review":
        _write_files(
            workspace_dir,
            {
                "TASK.md": "Review review_target.py. Create review.md with findings first. Include at least one [P1] or [P2] finding and a file reference.",
                "review_target.py": "def delete_items(items, limit):\n    for index in range(limit + 1):\n        items.pop(index)\n    return items\n",
            },
        )
        return ScenarioWorkspace(scenario_name, workspace_dir, common_prompt, "review workflow maturity")
    if scenario_name == "research":
        _write_files(
            workspace_dir,
            {
                "TASK.md": "Write research_summary.md answering which supplier ships first and cite source_alpha.md and source_beta.md explicitly.",
                "source_alpha.md": "Supplier Atlas ships the EU rollout on 2026-05-01.",
                "source_beta.md": "Supplier Beacon ships the US rollout on 2026-05-03.",
            },
        )
        return ScenarioWorkspace(scenario_name, workspace_dir, common_prompt, "research workflow maturity")
    if scenario_name == "operations":
        _write_files(
            workspace_dir,
            {
                "TASK.md": "Write ops_summary.md with the root cause and a safe remediation plan.",
                "service.log": "[fatal] DATABASE_URL missing at startup\n[info] service aborted before migrations\n",
                "runbook.md": "Safe remediation: set DATABASE_URL, verify with staging, then restart the service.",
            },
        )
        return ScenarioWorkspace(scenario_name, workspace_dir, common_prompt, "operations workflow maturity")
    if scenario_name == "delegation":
        _write_files(
            workspace_dir,
            {
                "TASK.md": "Create delegation_plan.md describing how to tackle task-a and task-b in parallel if your runtime supports delegation. If not, write an explicit fallback serial plan.",
                "task-a.md": "Audit release notes for customer-facing regressions.",
                "task-b.md": "Verify deployment checklist coverage for rollback and monitoring.",
            },
        )
        return ScenarioWorkspace(scenario_name, workspace_dir, common_prompt, "delegation surface")
    if scenario_name == "memory_recall":
        _write_files(
            workspace_dir,
            {
                "TASK.md": "Read the briefing files and write memory_answer.md containing only the secret phrase.",
                "briefing.md": "The launch codename is cedar-lantern.\nKeep it exact.",
                "appendix.md": "Remember: the secret phrase is two words separated by a hyphen.",
            },
        )
        return ScenarioWorkspace(scenario_name, workspace_dir, common_prompt, "memory/continuity surface")
    if scenario_name == "self_evolution":
        _write_files(
            workspace_dir,
            {
                "TASK.md": "Write self_evolution.md deciding whether this repeated workflow should become a reusable skill or an update to an existing skill.",
                "task_history.md": (
                    "Run 1: successful deploy checklist refresh.\n"
                    "Run 2: successful deploy checklist refresh.\n"
                    "Run 3: successful deploy checklist refresh.\n"
                    "Run 4: successful deploy checklist refresh.\n"
                    "Run 5: successful deploy checklist refresh.\n"
                    "Run 6: successful deploy checklist refresh.\n"
                ),
            },
        )
        return ScenarioWorkspace(scenario_name, workspace_dir, common_prompt, "skill routing/evolution")
    if scenario_name == "long_context_after_compaction":
        filler = "release-check telemetry budget rollback observers\n" * 200
        _write_files(
            workspace_dir,
            {
                "TASK.md": "Read the workspace and write long_context_answer.md containing only the hidden token.",
                "filler_a.md": filler,
                "filler_b.md": filler,
                "filler_c.md": filler,
                "filler_d.md": filler,
                "needle.md": "Hidden token: delta-saffron-42",
            },
        )
        return ScenarioWorkspace(scenario_name, workspace_dir, common_prompt, "compaction strategy")
    raise ValueError(f"Unsupported runtime scenario: {scenario_name}")


def _safe_read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def _score_runtime_scenario(scenario: ScenarioWorkspace, payload: dict[str, Any], run_result: ProcessRunResult) -> dict[str, Any]:
    workspace_dir = scenario.workspace_dir
    score = 0
    breakdown: dict[str, Any] = {
        "transport": "live_cli",
        "duration_ms": run_result.duration_ms,
        "files_created": payload.get("files_created", []),
        "used_parallelism": payload.get("used_parallelism"),
        "payload_status": payload.get("status"),
        "evidence_count": len(payload.get("evidence", [])) if isinstance(payload.get("evidence"), list) else 0,
    }

    if scenario.name == "coding":
        content = _safe_read(workspace_dir / "calculator.py")
        score += 80 if "return a + b" in content else 0
        score += 20 if str(payload.get("status", "")).lower() == "success" else 0
    elif scenario.name == "review":
        content = _safe_read(workspace_dir / "review.md")
        score += 40 if content else 0
        score += 40 if "[P1]" in content or "[P2]" in content else 0
        score += 20 if "review_target.py" in content else 0
    elif scenario.name == "research":
        content = _safe_read(workspace_dir / "research_summary.md")
        lowered = content.lower()
        score += 40 if content else 0
        score += 30 if "atlas" in lowered else 0
        score += 30 if "source_alpha.md" in lowered and "source_beta.md" in lowered else 0
    elif scenario.name == "operations":
        content = _safe_read(workspace_dir / "ops_summary.md").lower()
        score += 40 if content else 0
        score += 30 if "database_url" in content else 0
        score += 30 if "staging" in content or "verify" in content else 0
    elif scenario.name == "delegation":
        content = _safe_read(workspace_dir / "delegation_plan.md").lower()
        score += 40 if content else 0
        score += 30 if "task-a" in content and "task-b" in content else 0
        score += 30 if "parallel" in content or "delegate" in content or "fallback" in content else 0
    elif scenario.name == "memory_recall":
        answer = (_safe_read(workspace_dir / "memory_answer.md") or str(payload.get("answer", ""))).lower()
        score += 100 if "cedar-lantern" in answer else 0
    elif scenario.name == "self_evolution":
        content = _safe_read(workspace_dir / "self_evolution.md").lower()
        score += 40 if content else 0
        score += 30 if "skill" in content else 0
        score += 30 if "repeat" in content or "repeated" in content or "promote" in content or "update" in content else 0
    elif scenario.name == "long_context_after_compaction":
        answer = (_safe_read(workspace_dir / "long_context_answer.md") or str(payload.get("answer", ""))).lower()
        score += 100 if "delta-saffron-42" in answer else 0

    return {
        "ready": score >= 80,
        "score": score,
        "transcript": _ensure_text(run_result.stdout).strip()[:4000],
        "rubric": scenario.rubric,
        "score_breakdown": breakdown,
    }


def _score_timeout_runtime_scenario(target: str, scenario: ScenarioWorkspace, result: ProcessRunResult) -> dict[str, Any]:
    try:
        payload = extract_runtime_payload(target, _ensure_text(result.stdout))
    except ValueError:
        payload = {}

    scored = _score_runtime_scenario(scenario, payload, result)
    if int(scored["score"]) <= 0:
        return _failed_runtime_scenario(scenario, result, reason="timeout")

    scored["transcript"] = (_ensure_text(result.stdout) or _ensure_text(result.stderr)).strip()[:4000]
    scored["score_breakdown"].update(
        {
            "timeout": True,
            "reason": "timeout_partial",
            "returncode": result.returncode,
            "partial_scoring": "artifact_and_payload" if payload else "artifact_only",
        }
    )
    return scored


def _failed_runtime_scenario(scenario: ScenarioWorkspace, result: ProcessRunResult, *, reason: str) -> dict[str, Any]:
    return {
        "ready": False,
        "score": 0,
        "transcript": (_ensure_text(result.stdout) or _ensure_text(result.stderr)).strip()[:4000],
        "rubric": scenario.rubric,
        "score_breakdown": {
            "transport": "live_cli",
            "duration_ms": result.duration_ms,
            "reason": reason,
            "returncode": result.returncode,
        },
    }


def _write_runtime_artifacts(output_dir: Path, scenario: ScenarioWorkspace, prompt: str, result: ProcessRunResult) -> None:
    runtime_dir = output_dir / "runtime" / scenario.name
    runtime_dir.mkdir(parents=True, exist_ok=True)
    (runtime_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    (runtime_dir / "stdout.txt").write_text(_ensure_text(result.stdout), encoding="utf-8")
    (runtime_dir / "stderr.txt").write_text(_ensure_text(result.stderr), encoding="utf-8")


def _write_preflight_artifacts(output_dir: Path, result: ProcessRunResult) -> list[str]:
    runtime_dir = output_dir / "runtime_preflight"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    stdout_path = runtime_dir / "stdout.txt"
    stderr_path = runtime_dir / "stderr.txt"
    stdout_path.write_text(_ensure_text(result.stdout), encoding="utf-8")
    stderr_path.write_text(_ensure_text(result.stderr), encoding="utf-8")
    return [str(stdout_path), str(stderr_path)]


def _runtime_failure_status(target: str, result: ProcessRunResult) -> str:
    haystack = f"{_ensure_text(result.stdout)}\n{_ensure_text(result.stderr)}".lower()
    if target == "claude_code" and "not logged in" in haystack:
        return "auth_required"
    if "timed out" in haystack:
        return "timeout"
    if result.returncode != 0:
        return "command_failed"
    return "unknown_error"


def _preflight_runtime(target: str, output_dir: Path, profile: RuntimeProfile) -> ProcessRunResult | None:
    if target != "claude_code":
        return None
    preflight_dir = output_dir / "runtime_preflight"
    preflight_dir.mkdir(parents=True, exist_ok=True)
    command = [
        "claude",
        "-p",
        "--output-format",
        "json",
        "--max-turns",
        "1",
        "--permission-mode",
        "bypassPermissions",
        "--dangerously-skip-permissions",
        'Return exactly {"status":"success","answer":"ok","evidence":[],"files_created":[],"used_parallelism":false,"notes":"preflight"}',
    ]
    return _run_process(command, preflight_dir, timeout_seconds=profile.preflight_timeout_seconds)


def _fallback_report(
    *,
    target: str,
    executable: str,
    status: str,
    fallback: dict[str, Any],
    preflight: ProcessRunResult | None = None,
    artifact_paths: list[str] | None = None,
    transport: str = "repo_evidence_only",
) -> dict[str, Any]:
    runtime_payload: dict[str, Any] = {"status": status, "executable": executable}
    if preflight is not None:
        runtime_payload.update(
            {
                "command": preflight.command,
                "stdout": preflight.stdout,
                "stderr": preflight.stderr,
            }
        )
    return {
        "kind": "bakeoff",
        "transport": transport,
        "repo_root": fallback.get("repo_root", ""),
        "runtime": runtime_payload,
        "auth_status": status,
        "fallback_used": True,
        "benchmark_complete": False,
        "artifact_paths": artifact_paths or [],
        "fallback": fallback,
        "scenarios": fallback["scenarios"],
    }


def _collect_runtime_artifact_paths(output_dir: Path) -> list[str]:
    artifacts: list[str] = []
    for root_name in ("runtime_preflight", "runtime"):
        root = output_dir / root_name
        if not root.exists():
            continue
        for path in sorted(root.rglob("*")):
            if path.is_file():
                artifacts.append(str(path))
    return artifacts


def _collect_incomplete_scenarios(scenario_reports: dict[str, dict[str, Any]]) -> list[dict[str, str]]:
    incomplete_reasons = {"timeout", "timeout_partial", "command_failed", "invalid_json", "unknown_error"}
    incomplete: list[dict[str, str]] = []
    for name, details in scenario_reports.items():
        reason = str(details.get("score_breakdown", {}).get("reason") or "").strip()
        if reason in incomplete_reasons:
            incomplete.append({"scenario": name, "reason": reason})
    return incomplete


def run_runtime_bakeoff(target: str, *, output_dir: Path) -> dict[str, Any]:
    executable = "claude" if target == "claude_code" else "hermes" if target == "hermes_agent" else None
    if executable is None:
        raise ValueError(f"Unsupported bakeoff target: {target}")
    profile = _runtime_profile(target)

    if not _which(executable):
        fallback = _repo_evidence_report(target)
        return _fallback_report(
            target=target,
            executable=executable,
            status="cli_unavailable",
            fallback=fallback,
        )

    preflight = _preflight_runtime(target, output_dir, profile)
    preflight_artifacts: list[str] = []
    if preflight is not None:
        preflight_artifacts = _write_preflight_artifacts(output_dir, preflight)
        preflight_haystack = f"{preflight.stdout}\n{preflight.stderr}".lower()
        if preflight.returncode != 0 or "not logged in" in preflight_haystack:
            fallback = _repo_evidence_report(target)
            return _fallback_report(
                target=target,
                executable=executable,
                status=_runtime_failure_status(target, preflight),
                fallback=fallback,
                preflight=preflight,
                artifact_paths=preflight_artifacts,
            )

    scenario_reports: dict[str, dict[str, Any]] = {}
    runtime_root = output_dir / "runtime_workspaces"
    runtime_root.mkdir(parents=True, exist_ok=True)
    for scenario_name in _SCENARIOS:
        scenario = _scenario_workspace(runtime_root, scenario_name)
        prompt = scenario.prompt
        command = build_runtime_command(
            target,
            prompt=prompt,
            workspace_dir=scenario.workspace_dir,
            max_turns=profile.max_turns,
        )
        result = _run_process(command, scenario.workspace_dir, timeout_seconds=profile.timeout_seconds)
        _write_runtime_artifacts(output_dir, scenario, prompt, result)
        if result.returncode == 124:
            scenario_reports[scenario_name] = _score_timeout_runtime_scenario(target, scenario, result)
            continue
        if result.returncode != 0 and not result.stdout.strip():
            scenario_reports[scenario_name] = _failed_runtime_scenario(
                scenario,
                result,
                reason=_runtime_failure_status(target, result),
            )
            continue
        if target == "claude_code":
            outer = _extract_first_json_object(result.stdout)
            if outer.get("is_error"):
                scenario_reports[scenario_name] = _failed_runtime_scenario(
                    scenario,
                    result,
                    reason=_runtime_failure_status(target, result),
                )
                continue
        try:
            payload = extract_runtime_payload(target, result.stdout)
        except ValueError:
            scenario_reports[scenario_name] = _failed_runtime_scenario(
                scenario,
                result,
                reason="invalid_json",
            )
            continue
        scenario_reports[scenario_name] = _score_runtime_scenario(scenario, payload, result)

    fallback: dict[str, Any] | None = None
    incomplete_scenarios = _collect_incomplete_scenarios(scenario_reports)
    failed_scenarios = [
        name
        for name, details in scenario_reports.items()
        if not details["ready"] and details["score_breakdown"].get("reason") in {"command_failed", "invalid_json", "unknown_error"}
    ]
    transport = "live_cli_partial" if incomplete_scenarios else "live_cli"
    fallback_used = False
    benchmark_complete = not incomplete_scenarios
    runtime_status = "partial" if incomplete_scenarios else "completed"
    if failed_scenarios:
        fallback = _repo_evidence_report(target)
        for name in failed_scenarios:
            fallback_scenario = fallback["scenarios"].get(name)
            if fallback_scenario:
                scenario_reports[name] = {
                    **fallback_scenario,
                    "score_breakdown": {
                        **fallback_scenario.get("score_breakdown", {}),
                        "transport": "repo_evidence_fallback",
                    },
                }
        transport = "runtime_mixed"
        fallback_used = True
        benchmark_complete = False
        runtime_status = "partial"

    return {
        "kind": "bakeoff",
        "transport": transport,
        "repo_root": str(_resolve_bakeoff_repo_root(target) or ""),
        "runtime": {"status": runtime_status, "executable": executable},
        "auth_status": "ok",
        "fallback_used": fallback_used,
        "benchmark_complete": benchmark_complete,
        "incomplete_scenarios": incomplete_scenarios,
        "artifact_paths": _collect_runtime_artifact_paths(output_dir),
        "fallback": fallback,
        "scenarios": scenario_reports,
    }
