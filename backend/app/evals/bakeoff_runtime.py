"""Live bakeoff adapters for external agent runtimes."""

from __future__ import annotations

import json
import ast
import hashlib
import ipaddress
import os
import re
import shutil
import stat
import subprocess
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from pathlib import PurePosixPath
from shutil import which as _which
from time import monotonic, sleep
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx

from app.services.subprocess_env import build_agent_subprocess_env


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

J4_ENVELOPE_SCHEMA = "hive.j4.same_envelope.v1"
J4_RECEIPT_SCHEMA = "hive.j4.runtime_receipt.v1"
J4_MODEL = "gpt-5.4"
J4_REASONING_EFFORT = "low"
J4_STATUSES = {
    "completed",
    "auth_required",
    "authority_denied",
    "cli_unavailable",
    "model_unavailable",
    "resource_unavailable",
    "sandbox_unavailable",
    "timeout",
    "cancelled",
    "needs_reconciliation",
    "failed",
    "invalid_output",
    "attestation_failed",
}
FREECODE_BINARY_ENV = "HIVE_J4_FREECODE_BINARY"
HERMES_BINARY = Path.home() / ".local" / "bin" / "hermes"
_J4_RUNTIME_ORDER = ("hive", "freecode", "hermes")
_J4_ALLOWED_TOOLS = {
    "hive": ("read_file", "write_file", "edit_file", "glob_search", "grep_search"),
    "freecode": ("Read", "Write", "Edit", "Glob", "Grep"),
    "hermes": ("read_file", "write_file", "patch", "search_files"),
}
_J4_OUTPUT_FILES = {
    "coding": ("calculator.py",),
    "review": ("review.md",),
    "research": ("research_summary.md",),
    "operations": ("ops_summary.md",),
    "delegation": ("delegation_plan.md",),
    "memory_recall": ("memory_answer.md",),
    "self_evolution": ("self_evolution.md",),
    "long_context_after_compaction": ("long_context_answer.md",),
}
_J4_CRITERIA = {
    "coding": ("coding.ast_add", "coding.execution_assertions"),
    "review": ("review.priority", "review.file_ref", "review.issue"),
    "research": ("research.winner", "research.date", "research.source_refs"),
    "operations": ("operations.root_cause", "operations.staging_verification"),
    "delegation": ("delegation.coverage", "delegation.mode", "delegation.fallback"),
    "memory_recall": ("memory.exact_bytes",),
    "self_evolution": ("self_evolution.repeated_evidence", "self_evolution.skill_decision"),
    "long_context_after_compaction": ("long_context.exact_bytes",),
}


@dataclass(frozen=True, slots=True)
class J4RuntimeConfig:
    """Explicit authority and resource inputs for one manual P08-J4 run."""

    hive_base_url: str | None = None
    hive_bearer: str | None = field(default=None, repr=False)
    hive_agent_id: str | None = None
    hive_revision: str | None = None
    hive_binary_sha256: str | None = None
    external_profile_authorized: bool = False
    require_same_credential_domain: bool = False
    max_tool_rounds: int = 6
    wall_clock_seconds: int = 120
    cancel_fence_seconds: int = 10
    max_budget_usd: float = 2.0
    max_files: int = 64
    max_bytes: int = 1_000_000
    http_client: Any | None = field(default=None, repr=False, compare=False)


@dataclass(slots=True)
class ProcessRunResult:
    command: list[str]
    cwd: str
    returncode: int
    stdout: str
    stderr: str
    duration_ms: int
    sandbox: dict[str, Any] | None = None


def _freecode_binary() -> Path | None:
    configured = str(os.environ.get(FREECODE_BINARY_ENV) or "").strip()
    discovered = configured or _which("freecode")
    return Path(discovered).expanduser().resolve() if discovered else None


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


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _sha256_json(value: Any) -> str:
    return _sha256_bytes(_canonical_json(value).encode("utf-8"))


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_relative_path(value: str) -> str:
    path = PurePosixPath(str(value).replace("\\", "/"))
    if not value or path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError(f"Unsafe workspace path: {value!r}")
    return path.as_posix()


def _manifest(root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    manifest: list[dict[str, Any]] = []
    errors: list[str] = []
    if not root.exists():
        return manifest, ["workspace_missing"]
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            errors.append(f"symlink:{path.relative_to(root).as_posix()}")
            continue
        if not path.is_file():
            continue
        relative = _safe_relative_path(path.relative_to(root).as_posix())
        payload = path.read_bytes()
        manifest.append(
            {
                "path": relative,
                "sha256": _sha256_bytes(payload),
                "size": len(payload),
                "mode": f"{stat.S_IMODE(path.stat().st_mode):04o}",
            }
        )
    paths = [entry["path"] for entry in manifest]
    if len(paths) != len(set(paths)):
        errors.append("duplicate_path")
    return manifest, errors


def _workspace_diff(before: list[dict[str, Any]], after: list[dict[str, Any]]) -> list[dict[str, Any]]:
    before_by_path = {entry["path"]: entry for entry in before}
    after_by_path = {entry["path"]: entry for entry in after}
    changes: list[dict[str, Any]] = []
    for path in sorted(set(before_by_path) | set(after_by_path)):
        previous = before_by_path.get(path)
        current = after_by_path.get(path)
        if previous == current:
            continue
        changes.append(
            {
                "path": path,
                "change": "created" if previous is None else "deleted" if current is None else "modified",
                "before_sha256": previous.get("sha256") if previous else None,
                "after_sha256": current.get("sha256") if current else None,
            }
        )
    return changes


def _clone_seed(seed_root: Path, workspace_root: Path) -> tuple[list[dict[str, Any]], list[str]]:
    workspace_root.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(seed_root, workspace_root, copy_function=shutil.copy2)
    return _manifest(workspace_root)


def _scorer_source_sha256() -> str:
    return _sha256_file(Path(__file__).resolve())


def _runtime_workspace_path(output_dir: Path, runtime: str, envelope_id: str) -> Path:
    return output_dir / "j4_runtime" / runtime / envelope_id / "workspace"


def _build_same_envelope(
    scenario: ScenarioWorkspace,
    *,
    config: J4RuntimeConfig,
) -> tuple[dict[str, Any], str]:
    base_manifest, errors = _manifest(scenario.workspace_dir)
    if errors:
        raise ValueError(f"Invalid seed workspace: {', '.join(errors)}")
    scorer_sha256 = _scorer_source_sha256()
    identity_seed = {
        "schema": J4_ENVELOPE_SCHEMA,
        "scenario": scenario.name,
        "seed_sha256": _sha256_json(base_manifest),
        "scorer_sha256": scorer_sha256,
    }
    envelope_id = f"p08-j4-{scenario.name}-{_sha256_json(identity_seed)[:16]}"
    scenario.prompt = (
        f"The runtime-provided workspace root is logically named workspace/{envelope_id}.\n"
        "Treat the current workspace root as that directory and read TASK.md there.\n"
        "Treat that directory as the only writable evaluation workspace.\n"
        f"{scenario.prompt}"
    )
    (scenario.workspace_dir / "prompt.txt").write_text(scenario.prompt, encoding="utf-8")
    seed_manifest, errors = _manifest(scenario.workspace_dir)
    if errors:
        raise ValueError(f"Invalid seed workspace: {', '.join(errors)}")
    seed_sha256 = _sha256_json(seed_manifest)
    envelope = {
        "schema": J4_ENVELOPE_SCHEMA,
        "envelope_id": envelope_id,
        "task": {
            "suite": "P08-J4",
            "scenario": scenario.name,
            "prompt_sha256": _sha256_bytes(scenario.prompt.encode("utf-8")),
            "output_schema_sha256": _sha256_json(_RUNTIME_JSON_SCHEMA),
            "rubric_id": f"p08-j4.{scenario.name}.v1",
        },
        "workspace": {
            "logical_root": "workspace",
            "seed_manifest": seed_manifest,
            "seed_sha256": seed_sha256,
            "max_files": config.max_files,
            "max_bytes": config.max_bytes,
            "clone_policy": "immutable_seed_per_runtime",
        },
        "model": {
            "vendor": "openai",
            "model": J4_MODEL,
            "equivalence": "exact_vendor_model_id",
            "reasoning_effort": J4_REASONING_EFFORT,
            "fallback_allowed": False,
            "allowed_provider_routes": {
                "hive": ["openai-response"],
                "freecode": ["chatgpt-codex"],
                "hermes": ["openai-codex"],
            },
        },
        "resources": {
            "max_tool_rounds": config.max_tool_rounds,
            "wall_clock_seconds": config.wall_clock_seconds,
            "hard_common": ["max_tool_rounds", "wall_clock_seconds", "reasoning_effort"],
            "runtime_extra_guards": {"freecode": {"max_budget_usd": config.max_budget_usd}},
        },
        "authority": {
            "writable_scope": "evaluation_workspace_only",
            "allowed_tools": {runtime: list(_J4_ALLOWED_TOOLS[runtime]) for runtime in _J4_RUNTIME_ORDER},
            "network_tools_allowed": False,
            "proof_required": True,
        },
        "scorer": {
            "id": "hive.p08-j4.external",
            "version": "1",
            "source_sha256": scorer_sha256,
            "criteria_ids": list(_J4_CRITERIA[scenario.name]),
        },
    }
    return envelope, _sha256_json(envelope)


def _ensure_text(value: str | bytes | None) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _runtime_profile(target: str) -> RuntimeProfile:
    if target == "claude_code":
        return RuntimeProfile(max_turns=4, timeout_seconds=60, preflight_timeout_seconds=20)
    if target == "freecode":
        return RuntimeProfile(max_turns=6, timeout_seconds=120, preflight_timeout_seconds=20)
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
    if target == "freecode":
        freecode_binary = _freecode_binary()
        return [
            str(freecode_binary or "freecode"),
            "-p",
            "--output-format",
            "json",
            "--json-schema",
            _canonical_json(_RUNTIME_JSON_SCHEMA),
            "--model",
            J4_MODEL,
            "--effort",
            J4_REASONING_EFFORT,
            "--max-turns",
            str(max_turns),
            "--max-budget-usd",
            "2.0",
            "--tools",
            "Read,Write,Edit,Glob,Grep",
            "--permission-mode",
            "bypassPermissions",
            "--dangerously-skip-permissions",
            "--setting-sources",
            "",
            "--strict-mcp-config",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--no-chrome",
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


def _load_json_object(text: str) -> dict[str, Any]:
    try:
        payload = json.loads(text.strip())
    except json.JSONDecodeError as exc:
        raise ValueError("Runtime output must be exactly one JSON object.") from exc
    if not isinstance(payload, dict):
        raise ValueError("Runtime output must be a JSON object.")
    return payload


def extract_runtime_payload(target: str, raw_output: str) -> dict[str, Any]:
    outer = _load_json_object(raw_output)
    if target in {"claude_code", "freecode"} and isinstance(outer.get("structured_output"), dict):
        return outer["structured_output"]
    if target in {"claude_code", "freecode"} and isinstance(outer.get("result"), str):
        nested = outer["result"].strip()
        if nested.startswith("{"):
            return _load_json_object(nested)
    return outer


def _run_process(
    command: list[str],
    cwd: Path,
    timeout_seconds: int,
    *,
    env_overrides: dict[str, str] | None = None,
    require_workspace_sandbox: bool = False,
) -> ProcessRunResult:
    started = monotonic()
    env = build_agent_subprocess_env(home=Path.home())
    if command and command[0] == "claude":
        # Claude OAuth/keychain auth is unavailable in simple mode, so avoid
        # inheriting or forcing CLAUDE_CODE_SIMPLE during live bakeoffs.
        env.pop("CLAUDE_CODE_SIMPLE", None)
    if env_overrides:
        env.update(env_overrides)
    sandbox: dict[str, Any] | None = None
    cleanup_paths: list[Path] = []
    actual_command = command
    if require_workspace_sandbox:
        actual_command, cleanup_paths, sandbox = _build_workspace_sandbox_command(command, cwd, env)
        if actual_command is None:
            return ProcessRunResult(
                command=command,
                cwd=str(cwd),
                returncode=126,
                stdout="",
                stderr=str((sandbox or {}).get("reason") or "Workspace sandbox unavailable"),
                duration_ms=int((monotonic() - started) * 1000),
                sandbox=sandbox,
            )
    try:
        completed = subprocess.run(  # noqa: S603
            actual_command,
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
    except OSError as exc:
        returncode = 127
        stdout = ""
        stderr = str(exc)
    finally:
        for path in cleanup_paths:
            path.unlink(missing_ok=True)
    return ProcessRunResult(
        command=command,
        cwd=str(cwd),
        returncode=returncode,
        stdout=stdout,
        stderr=stderr,
        duration_ms=int((monotonic() - started) * 1000),
        sandbox=sandbox,
    )


def _build_workspace_sandbox_command(
    command: list[str],
    cwd: Path,
    env: dict[str, str],
) -> tuple[list[str] | None, list[Path], dict[str, Any]]:
    """Build a real workspace-write OS sandbox or fail closed.

    Provider transport still needs network access, while the model-visible tool
    surface is separately restricted to file tools by each adapter.
    """

    from app.runtime.ccplus_contracts import SandboxProfile
    from app.services.subprocess_sandbox import (
        SandboxBuildSpec,
        build_sandboxed_agent_command,
        probe_os_sandbox_capability,
    )

    probe = probe_os_sandbox_capability()
    if not probe.available:
        return None, [], {"status": "unavailable", "provider": probe.provider, "reason": probe.reason}
    built = build_sandboxed_agent_command(
        command,
        work_dir=cwd,
        env=env,
        spec=SandboxBuildSpec(
            profile=SandboxProfile.WORKSPACE_WRITE,
            network_access=True,
            writable_roots=(),
        ),
    )
    if built.command is None or built.command == command:
        return (
            None,
            list(built.cleanup_paths),
            {
                "status": "unavailable",
                "provider": probe.provider,
                "reason": built.error or "OS sandbox did not wrap the command",
            },
        )
    return (
        list(built.command),
        list(built.cleanup_paths),
        {
            "status": "enforced",
            "provider": probe.provider,
            "reason": probe.reason,
        },
    )


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
        return ScenarioWorkspace(
            scenario_name, workspace_dir, common_prompt, "coding task readiness and required tool surface"
        )
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
                "TASK.md": (
                    "Write research_summary.md answering which supplier ships first. Include exact lines "
                    "`Winner: <supplier>`, `Ship date: <YYYY-MM-DD>`, and "
                    "`Sources: source_alpha.md, source_beta.md`."
                ),
                "source_alpha.md": "Supplier Atlas ships the EU rollout on 2026-05-01.",
                "source_beta.md": "Supplier Beacon ships the US rollout on 2026-05-03.",
            },
        )
        return ScenarioWorkspace(scenario_name, workspace_dir, common_prompt, "research workflow maturity")
    if scenario_name == "operations":
        _write_files(
            workspace_dir,
            {
                "TASK.md": (
                    "Write ops_summary.md with the exact root-cause line `Root cause: DATABASE_URL missing` "
                    "and a `Staging verification:` line describing how to verify startup before restart."
                ),
                "service.log": "[fatal] DATABASE_URL missing at startup\n[info] service aborted before migrations\n",
                "runbook.md": "Safe remediation: set DATABASE_URL, verify with staging, then restart the service.",
            },
        )
        return ScenarioWorkspace(scenario_name, workspace_dir, common_prompt, "operations workflow maturity")
    if scenario_name == "delegation":
        _write_files(
            workspace_dir,
            {
                "TASK.md": (
                    "Create delegation_plan.md with exact lines `Tasks: task-a, task-b`, either "
                    "`Mode: parallel` or `Mode: serial`, and `Fallback: serial if delegation unavailable`."
                ),
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
                "TASK.md": (
                    "Write self_evolution.md with exact line `Evidence count: 6` and a `Decision:` line "
                    "choosing either `update existing skill` or `create new skill`."
                ),
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


def _score_runtime_scenario(
    scenario: ScenarioWorkspace, payload: dict[str, Any], run_result: ProcessRunResult
) -> dict[str, Any]:
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
        score += (
            30 if "repeat" in content or "repeated" in content or "promote" in content or "update" in content else 0
        )
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


def _score_timeout_runtime_scenario(
    target: str, scenario: ScenarioWorkspace, result: ProcessRunResult
) -> dict[str, Any]:
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


def _write_runtime_artifacts(
    output_dir: Path, scenario: ScenarioWorkspace, prompt: str, result: ProcessRunResult
) -> None:
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
    if target in {"claude_code", "freecode"} and "not logged in" in haystack:
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


def _unavailable_report(
    *,
    executable: str,
    status: str,
    preflight: ProcessRunResult | None = None,
    artifact_paths: list[str] | None = None,
) -> dict[str, Any]:
    """Honest no-run report (spec §2.4): when the target CLI cannot run there
    are NO scenario scores — synthesized repo-evidence scoring is retired."""
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
        "transport": "runtime_unavailable",
        "runtime": runtime_payload,
        "auth_status": status,
        "benchmark_complete": False,
        "artifact_paths": artifact_paths or [],
        "scenarios": {},
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


def run_runtime_bakeoff(
    target: str,
    *,
    output_dir: Path,
    external_profile_authorized: bool = False,
) -> dict[str, Any]:
    executable = (
        "claude"
        if target == "claude_code"
        else str(_freecode_binary() or "freecode")
        if target == "freecode"
        else "hermes"
        if target == "hermes_agent"
        else None
    )
    if executable is None:
        raise ValueError(f"Unsupported bakeoff target: {target}")
    if target == "freecode" and not external_profile_authorized:
        return _unavailable_report(executable=executable, status="authority_denied")
    profile = _runtime_profile(target)

    if not _which(executable):
        return _unavailable_report(executable=executable, status="cli_unavailable")

    preflight = _preflight_runtime(target, output_dir, profile)
    preflight_artifacts: list[str] = []
    if preflight is not None:
        preflight_artifacts = _write_preflight_artifacts(output_dir, preflight)
        preflight_haystack = f"{preflight.stdout}\n{preflight.stderr}".lower()
        if preflight.returncode != 0 or "not logged in" in preflight_haystack:
            return _unavailable_report(
                executable=executable,
                status=_runtime_failure_status(target, preflight),
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
        if target == "freecode":
            result = _run_process(
                command,
                scenario.workspace_dir,
                timeout_seconds=profile.timeout_seconds,
                env_overrides={"CLAUDE_CODE_USE_OPENAI": "1"},
            )
        else:
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
        if target in {"claude_code", "freecode"}:
            try:
                outer = _load_json_object(result.stdout)
            except ValueError:
                scenario_reports[scenario_name] = _failed_runtime_scenario(
                    scenario,
                    result,
                    reason="invalid_json",
                )
                continue
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

    # Spec §2.4: failed scenarios stay failed — no synthesized repo-evidence
    # scores backfill a live run. Incomplete coverage is reported, not papered.
    incomplete_scenarios = _collect_incomplete_scenarios(scenario_reports)
    transport = "live_cli_partial" if incomplete_scenarios else "live_cli"
    benchmark_complete = not incomplete_scenarios
    runtime_status = "partial" if incomplete_scenarios else "completed"

    return {
        "kind": "bakeoff",
        "transport": transport,
        "runtime": {"status": runtime_status, "executable": executable},
        "auth_status": "ok",
        "benchmark_complete": benchmark_complete,
        "incomplete_scenarios": incomplete_scenarios,
        "artifact_paths": _collect_runtime_artifact_paths(output_dir),
        "scenarios": scenario_reports,
    }


def _validate_runtime_payload(payload: Any) -> list[str]:
    if not isinstance(payload, dict):
        return ["payload_not_object"]
    required = set(_RUNTIME_JSON_SCHEMA["required"])
    errors: list[str] = []
    if set(payload) != required:
        errors.append("payload_keys")
    if payload.get("status") not in {"success", "partial", "failed"}:
        errors.append("status")
    for key in ("answer", "notes"):
        if not isinstance(payload.get(key), str):
            errors.append(key)
    for key in ("evidence", "files_created"):
        value = payload.get(key)
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            errors.append(key)
    if not isinstance(payload.get("used_parallelism"), bool):
        errors.append("used_parallelism")
    return sorted(set(errors))


def _normalize_model_id(value: Any) -> str:
    model = str(value or "").strip().lower()
    for prefix in ("openai/", "openai:"):
        if model.startswith(prefix):
            model = model[len(prefix) :]
    return model


def _usage_total(value: Any, names: set[str]) -> int:
    total = 0
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).lower().replace("_", "") in names and isinstance(item, (int, float)):
                total += int(item)
            elif isinstance(item, (dict, list)):
                total += _usage_total(item, names)
    elif isinstance(value, list):
        total += sum(_usage_total(item, names) for item in value)
    return total


def _artifact(path: Path, content: str) -> dict[str, Any]:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = content.encode("utf-8")
    path.write_bytes(payload)
    return {"path": str(path.resolve()), "bytes": len(payload), "sha256": _sha256_bytes(payload)}


def _write_j4_artifacts(
    output_dir: Path,
    *,
    runtime: str,
    envelope_id: str,
    stdout: str,
    stderr: str,
    transcript: str = "",
) -> dict[str, dict[str, Any]]:
    root = output_dir / "j4_artifacts" / runtime / envelope_id
    return {
        "stdout": _artifact(root / "stdout.txt", stdout),
        "stderr": _artifact(root / "stderr.txt", stderr),
        "transcript": _artifact(root / "transcript.txt", transcript),
    }


def _command_output(command: list[str], *, cwd: Path) -> str:
    try:
        result = subprocess.run(  # noqa: S603
            command,
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=10,
            env=build_agent_subprocess_env(home=Path.home()),
        )
    except (OSError, subprocess.TimeoutExpired):
        return ""
    return (result.stdout or result.stderr or "").strip() if result.returncode == 0 else ""


def _binary_identity(runtime: str, binary: Path | None) -> dict[str, Any]:
    if binary is None:
        return {"path": "", "version": "", "sha256": "", "revision": ""}
    resolved = binary.expanduser().resolve()
    version = _command_output([str(resolved), "--version"], cwd=resolved.parent) if resolved.is_file() else ""
    revision = ""
    if runtime == "freecode" and resolved.is_file():
        source = resolved.parent
        revision = _command_output(["git", "-C", str(source), "rev-parse", "HEAD"], cwd=source)
    elif runtime == "hermes":
        match = re.search(
            r"(?mi)^Hermes Agent v[^\n]*?upstream ([0-9a-f]+)\s*.*?\blocal ([0-9a-f]+)",
            version,
        )
        if match:
            revision = f"upstream:{match.group(1)};local:{match.group(2)}"
    return {
        "path": str(resolved),
        "version": version,
        "sha256": _sha256_file(resolved) if resolved.is_file() else "",
        "revision": revision,
    }


def _workspace_receipt(
    workspace_root: Path,
    before: list[dict[str, Any]],
    *,
    envelope: dict[str, Any],
    declared_paths: list[str] | None = None,
) -> dict[str, Any]:
    after, errors = _manifest(workspace_root)
    unsafe_declared: list[str] = []
    declared_seen: set[str] = set()
    duplicate_declared: list[str] = []
    for path in declared_paths or []:
        try:
            safe = _safe_relative_path(path)
        except ValueError:
            unsafe_declared.append(path)
            continue
        if safe in declared_seen:
            duplicate_declared.append(safe)
        declared_seen.add(safe)
    limits = envelope["workspace"]
    total_bytes = sum(int(entry["size"]) for entry in after)
    boundary_ok = (
        not errors
        and not unsafe_declared
        and not duplicate_declared
        and len(after) <= int(limits["max_files"])
        and total_bytes <= int(limits["max_bytes"])
    )
    return {
        "logical_root": limits["logical_root"],
        "local_path": str(workspace_root.resolve()),
        "before_manifest": before,
        "before_sha256": _sha256_json(before),
        "after_manifest": after,
        "after_sha256": _sha256_json(after),
        "diff": _workspace_diff(before, after),
        "boundary_ok": boundary_ok,
        "boundary_errors": [
            *errors,
            *[f"unsafe_declared_path:{path}" for path in unsafe_declared],
            *[f"duplicate_declared_path:{path}" for path in duplicate_declared],
        ],
        "file_count": len(after),
        "total_bytes": total_bytes,
    }


def _base_receipt(
    *,
    runtime: str,
    binary: dict[str, Any],
    envelope: dict[str, Any],
    envelope_sha256: str,
    status: str,
    argv: list[str],
    duration_ms: int,
    exit_code: int | None,
    workspace: dict[str, Any],
    artifacts: dict[str, Any],
    parsed_payload: dict[str, Any] | None = None,
    schema_errors: list[str] | None = None,
    turns: int | None = None,
    tokens: int | None = None,
    observed_cost: float | None = None,
    effective_model: str | None = None,
    effective_provider: str | None = None,
    fallback_observed: bool | None = None,
    attestation_source: str | None = None,
    effective_resources: dict[str, Any] | None = None,
    resource_sources: dict[str, str] | None = None,
    authority: dict[str, Any] | None = None,
    route_attestation: dict[str, Any] | None = None,
    execution: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if status not in J4_STATUSES:
        raise ValueError(f"Unsupported J4 status: {status}")
    return {
        "schema": J4_RECEIPT_SCHEMA,
        "runtime": runtime,
        "binary": binary,
        "envelope_sha256": envelope_sha256,
        "seed_sha256": envelope["workspace"]["seed_sha256"],
        "scorer_sha256": envelope["scorer"]["source_sha256"],
        "status": status,
        "argv": list(argv),
        "duration_ms": duration_ms,
        "exit_code": exit_code,
        "turns": turns,
        "tokens": tokens,
        "observed_cost": observed_cost,
        "requested_model": J4_MODEL,
        "effective_model": effective_model,
        "requested_provider": envelope["model"]["allowed_provider_routes"][runtime][0],
        "effective_provider": effective_provider,
        "fallback_observed": fallback_observed,
        "attestation_source": attestation_source,
        "resources": {
            "requested": {
                "max_tool_rounds": envelope["resources"]["max_tool_rounds"],
                "wall_clock_seconds": envelope["resources"]["wall_clock_seconds"],
                "reasoning_effort": envelope["model"]["reasoning_effort"],
            },
            "effective": effective_resources,
            "sources": resource_sources or {},
        },
        "authority": authority,
        "route_attestation": route_attestation
        or {
            "call_count": 0,
            "routes": [],
            "source": None,
        },
        "execution": execution or {},
        "workspace": workspace,
        "artifacts": artifacts,
        "parsed": {
            "payload": parsed_payload,
            "schema_valid": parsed_payload is not None and not (schema_errors or []),
            "schema_errors": schema_errors or [],
        },
        "score": None,
    }


def _expected_resources(envelope: dict[str, Any]) -> dict[str, Any]:
    return {
        "max_tool_rounds": envelope["resources"]["max_tool_rounds"],
        "wall_clock_seconds": envelope["resources"]["wall_clock_seconds"],
        "reasoning_effort": envelope["model"]["reasoning_effort"],
    }


def _argv_value(command: list[str], flag: str) -> str | None:
    positions = [index for index, value in enumerate(command) if value == flag]
    if len(positions) != 1:
        return None
    index = positions[0]
    return command[index + 1] if index + 1 < len(command) else None


def _cli_resource_attestation(
    runtime: str,
    command: list[str],
    envelope: dict[str, Any],
) -> tuple[dict[str, Any] | None, dict[str, str]]:
    expected = _expected_resources(envelope)
    max_turns = _argv_value(command, "--max-turns")
    reasoning_flag = "--effort" if runtime == "freecode" else "--reasoning"
    reasoning = _argv_value(command, reasoning_flag)
    run_budget = (
        str(envelope["resources"]["wall_clock_seconds"])
        if runtime == "freecode"
        else _argv_value(command, "--run-budget")
    )
    effective = {
        "max_tool_rounds": int(max_turns) if max_turns and max_turns.isdigit() else None,
        "wall_clock_seconds": int(run_budget) if run_budget and run_budget.isdigit() else None,
        "reasoning_effort": reasoning,
    }
    sources = {
        "max_tool_rounds": f"{runtime}.argv.--max-turns",
        "wall_clock_seconds": (
            "adapter.subprocess_timeout"
            if runtime == "freecode"
            else "hermes.argv.--run-budget+adapter.subprocess_timeout"
        ),
        "reasoning_effort": f"{runtime}.argv.{reasoning_flag}",
    }
    return (effective if effective == expected else None), sources


def _cli_authority_attestation(
    runtime: str,
    command: list[str],
    envelope: dict[str, Any],
    sandbox: dict[str, Any] | None,
) -> dict[str, Any] | None:
    expected_tools = list(envelope["authority"]["allowed_tools"][runtime])
    if not isinstance(sandbox, dict) or sandbox.get("status") != "enforced":
        return None
    if runtime == "freecode":
        configured = (_argv_value(command, "--tools") or "").split(",")
        tools_source = "freecode.argv.--tools"
        command_scope_ok = configured == expected_tools
    else:
        configured = list(_J4_ALLOWED_TOOLS["hermes"]) if _argv_value(command, "-t") == "file" else []
        tools_source = "hermes.argv.-t=file"
        command_scope_ok = "--safe-mode" in command and "--yolo" in command
    if configured != expected_tools or not command_scope_ok:
        return None
    requested = {
        "allowed_tools": expected_tools,
        "writable_scope": envelope["authority"]["writable_scope"],
    }
    return {
        "requested": requested,
        "effective": dict(requested),
        "sources": {
            "allowed_tools": tools_source,
            "writable_scope": f"os_sandbox:{sandbox.get('provider') or 'unknown'}",
        },
        "sandbox": dict(sandbox),
    }


def _hive_session_authority(
    session_payload: Any,
    *,
    remote_root: str,
    envelope: dict[str, Any],
) -> dict[str, Any] | None:
    if not isinstance(session_payload, dict):
        return None
    profile = session_payload.get("permission_profile")
    if not isinstance(profile, dict):
        return None
    expected_tools = list(envelope["authority"]["allowed_tools"]["hive"])
    mode = profile.get("mode")
    allowed_tools = profile.get("allowed_tools")
    writable_roots = profile.get("writable_roots")
    readable_roots = profile.get("readable_roots")
    capability_snapshot = profile.get("capability_policy_snapshot")
    if not isinstance(allowed_tools, list) or not all(isinstance(value, str) for value in allowed_tools):
        return None
    if not isinstance(writable_roots, list) or not all(isinstance(value, str) for value in writable_roots):
        return None
    if (
        mode != "bypassPermissions"
        or len(allowed_tools) != len(expected_tools)
        or set(allowed_tools) != set(expected_tools)
        or writable_roots != [remote_root]
        or readable_roots != [remote_root]
        or not isinstance(capability_snapshot, dict)
        or capability_snapshot.get("session_exact_scope") is not True
    ):
        return None
    requested = {
        "allowed_tools": expected_tools,
        "writable_scope": envelope["authority"]["writable_scope"],
    }
    return {
        "requested": requested,
        "effective": dict(requested),
        "sources": {
            "allowed_tools": "hive.session.permission_profile.allowed_tools",
            "writable_scope": "hive.session.permission_profile.writable_roots",
        },
        "sandbox": {
            "status": "enforced",
            "provider": "hive.session.permission_profile",
        },
    }


def _freecode_command(*, prompt: str, envelope: dict[str, Any], binary: Path | None = None) -> list[str]:
    return [
        str(binary or _freecode_binary() or "freecode"),
        "-p",
        "--output-format",
        "json",
        "--json-schema",
        _canonical_json(_RUNTIME_JSON_SCHEMA),
        "--model",
        J4_MODEL,
        "--effort",
        J4_REASONING_EFFORT,
        "--max-turns",
        str(envelope["resources"]["max_tool_rounds"]),
        "--max-budget-usd",
        str(envelope["resources"]["runtime_extra_guards"]["freecode"]["max_budget_usd"]),
        "--tools",
        "Read,Write,Edit,Glob,Grep",
        "--permission-mode",
        "bypassPermissions",
        "--dangerously-skip-permissions",
        "--setting-sources",
        "",
        "--strict-mcp-config",
        "--disable-slash-commands",
        "--no-session-persistence",
        "--no-chrome",
        prompt,
    ]


def _hermes_command(*, workspace_root: Path, envelope: dict[str, Any]) -> list[str]:
    return [
        str(HERMES_BINARY.resolve()),
        "chat",
        "--query-file",
        "prompt.txt",
        "--oneshot",
        "-Q",
        "--in",
        str(workspace_root.resolve()),
        "-m",
        J4_MODEL,
        "--provider",
        "openai-codex",
        "--reasoning",
        J4_REASONING_EFFORT,
        "-t",
        "file",
        "--max-turns",
        str(envelope["resources"]["max_tool_rounds"]),
        "--run-budget",
        str(envelope["resources"]["wall_clock_seconds"]),
        "--yolo",
        "--safe-mode",
        "--source",
        "p08-j4",
    ]


def _freecode_attestation(
    outer: dict[str, Any],
) -> tuple[str | None, str | None, bool | None, int | None, int | None, float | None]:
    usage = outer.get("modelUsage")
    if not isinstance(usage, dict) or not usage:
        return None, None, None, None, None, None
    models = {_normalize_model_id(key) for key in usage}
    effective_model = next(iter(models)) if len(models) == 1 else None
    provider_attested = all(str(key).strip().lower().startswith(("openai/", "openai:")) for key in usage)
    effective_provider = "chatgpt-codex" if provider_attested else None
    fallback_observed = len(models) != 1 or effective_model != J4_MODEL or effective_provider != "chatgpt-codex"
    turns_value = outer.get("num_turns")
    turns = turns_value if isinstance(turns_value, int) and not isinstance(turns_value, bool) else None
    tokens = _usage_total(
        usage,
        {"inputtokens", "outputtokens", "cachecreationinputtokens", "cachereadinputtokens"},
    )
    raw_cost = outer.get("total_cost_usd")
    observed_cost = float(raw_cost) if isinstance(raw_cost, (int, float)) else None
    return effective_model, effective_provider, fallback_observed, turns, tokens, observed_cost


def _cli_failure_status(result: ProcessRunResult, *, runtime: str) -> str:
    if result.returncode == 124:
        return "timeout"
    if result.returncode == 126:
        return "sandbox_unavailable"
    if result.returncode == 127:
        return "cli_unavailable"
    lines = [line.strip().lower() for line in f"{result.stdout}\n{result.stderr}".splitlines() if line.strip()]
    auth_markers = (
        "authentication required",
        "not logged in",
        "no credentials for openai-codex",
        "no valid credentials",
        "oauth authentication required",
    )
    if runtime in {"freecode", "hermes"} and any(marker in line for line in lines for marker in auth_markers):
        return "auth_required"
    return "failed"


def _run_freecode_j4(
    scenario: ScenarioWorkspace,
    envelope: dict[str, Any],
    envelope_sha256: str,
    *,
    output_dir: Path,
    config: J4RuntimeConfig,
) -> dict[str, Any]:
    workspace_root = _runtime_workspace_path(output_dir, "freecode", envelope["envelope_id"])
    before, clone_errors = _clone_seed(scenario.workspace_dir, workspace_root)
    binary_path = _freecode_binary()
    binary = _binary_identity("freecode", binary_path)
    command = _freecode_command(prompt=scenario.prompt, envelope=envelope, binary=binary_path)
    if clone_errors or not all(binary.values()):
        artifacts = _write_j4_artifacts(
            output_dir,
            runtime="freecode",
            envelope_id=envelope["envelope_id"],
            stdout="",
            stderr="",
        )
        workspace = _workspace_receipt(workspace_root, before, envelope=envelope)
        return _base_receipt(
            runtime="freecode",
            binary=binary,
            envelope=envelope,
            envelope_sha256=envelope_sha256,
            status="cli_unavailable" if binary_path is None or not binary_path.is_file() else "attestation_failed",
            argv=command,
            duration_ms=0,
            exit_code=None,
            workspace=workspace,
            artifacts=artifacts,
            schema_errors=[*clone_errors, "binary_identity"],
        )

    result = _run_process(
        command,
        workspace_root,
        timeout_seconds=envelope["resources"]["wall_clock_seconds"],
        env_overrides={"CLAUDE_CODE_USE_OPENAI": "1"},
        require_workspace_sandbox=True,
    )
    effective_resources, resource_sources = _cli_resource_attestation("freecode", command, envelope)
    authority = _cli_authority_attestation("freecode", command, envelope, result.sandbox)
    artifacts = _write_j4_artifacts(
        output_dir,
        runtime="freecode",
        envelope_id=envelope["envelope_id"],
        stdout=_ensure_text(result.stdout),
        stderr=_ensure_text(result.stderr),
    )
    payload: dict[str, Any] | None = None
    schema_errors: list[str] = []
    effective_model: str | None = None
    fallback_observed: bool | None = None
    turns: int | None = None
    tokens: int | None = None
    observed_cost: float | None = None
    status = _cli_failure_status(result, runtime="freecode") if result.returncode else "completed"
    if result.returncode == 0:
        try:
            outer = _load_json_object(_ensure_text(result.stdout))
        except ValueError:
            outer = {}
            schema_errors.append("whole_output_json")
        if outer.get("is_error") is not False or str(outer.get("subtype") or "") != "success":
            schema_errors.append("outer_success")
        payload = outer.get("structured_output") if isinstance(outer.get("structured_output"), dict) else None
        schema_errors.extend(_validate_runtime_payload(payload))
        (
            effective_model,
            effective_provider,
            fallback_observed,
            turns,
            tokens,
            observed_cost,
        ) = _freecode_attestation(outer)
        if (
            effective_model != J4_MODEL
            or effective_provider != "chatgpt-codex"
            or fallback_observed is not False
            or not isinstance(turns, int)
            or turns < 1
            or not isinstance(tokens, int)
            or tokens < 1
        ):
            schema_errors.append("model_usage_attestation")
        if effective_resources is None:
            schema_errors.append("resource_attestation")
        if authority is None:
            schema_errors.append("authority_attestation")
        if schema_errors:
            if "authority_attestation" in schema_errors:
                status = "sandbox_unavailable"
            elif "resource_attestation" in schema_errors:
                status = "resource_unavailable"
            else:
                status = "attestation_failed" if "model_usage_attestation" in schema_errors else "invalid_output"
    workspace = _workspace_receipt(
        workspace_root,
        before,
        envelope=envelope,
        declared_paths=payload.get("files_created") if isinstance(payload, dict) else None,
    )
    if status == "completed" and not workspace["boundary_ok"]:
        status = "sandbox_unavailable"
    return _base_receipt(
        runtime="freecode",
        binary=binary,
        envelope=envelope,
        envelope_sha256=envelope_sha256,
        status=status,
        argv=command,
        duration_ms=result.duration_ms,
        exit_code=result.returncode,
        workspace=workspace,
        artifacts=artifacts,
        parsed_payload=payload,
        schema_errors=schema_errors,
        turns=turns,
        tokens=tokens,
        observed_cost=observed_cost,
        effective_model=effective_model,
        effective_provider=effective_provider,
        fallback_observed=fallback_observed,
        attestation_source="freecode.stdout.modelUsage+invocation.CLAUDE_CODE_USE_OPENAI" if effective_model else None,
        effective_resources=effective_resources,
        resource_sources=resource_sources,
        authority=authority,
        route_attestation={
            "call_count": 1 if effective_model and isinstance(tokens, int) and tokens > 0 else 0,
            "count_semantics": "minimum_observed",
            "routes": (
                [{"model": effective_model, "provider": effective_provider}]
                if effective_model and effective_provider
                else []
            ),
            "source": "freecode.stdout.modelUsage+invocation.CLAUDE_CODE_USE_OPENAI",
        },
    )


def _hermes_session_id(stderr: str) -> str | None:
    matches = re.findall(r"(?mi)^session_id:\s*([a-z0-9][a-z0-9._:-]*)\s*$", stderr)
    return matches[0] if len(matches) == 1 else None


def _parse_jsonl(text: str) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for line in text.splitlines():
        if not line.strip():
            continue
        payload = _load_json_object(line)
        records.append(payload)
    if not records:
        raise ValueError("Hermes transcript export is empty.")
    return records


def _decoded_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    if isinstance(value, str):
        try:
            decoded = json.loads(value)
        except json.JSONDecodeError:
            return {}
        return decoded if isinstance(decoded, dict) else {}
    return {}


def _hermes_attestation(
    records: list[dict[str, Any]],
) -> tuple[str | None, str | None, bool | None, int, int, float | None, bool]:
    routes: set[tuple[str, str]] = set()
    fallback_observed = False
    attribution_complete = True
    seen_call_segments: set[str] = set()
    turns = 0
    tokens = 0
    costs: list[float] = []
    for record in records:
        sessions = record.get("segments") if isinstance(record.get("segments"), list) else [record]
        for session in sessions:
            if not isinstance(session, dict):
                attribution_complete = False
                continue
            model = _normalize_model_id(session.get("model"))
            provider = str(session.get("billing_provider") or "").strip().lower()
            model_config = _decoded_mapping(session.get("model_config"))
            model = model or _normalize_model_id(model_config.get("model"))
            provider = provider or str(model_config.get("provider") or "").strip().lower()
            fallback_observed = fallback_observed or any(
                bool(model_config.get(key))
                for key in ("fallback_model", "fallback_provider", "_fallback_activated", "fallback_activated")
            )
            api_calls = session.get("api_call_count")
            session_tokens = _usage_total(session, {"inputtokens", "outputtokens", "cachetokens"})
            tokens += session_tokens
            cost = session.get("actual_cost_usd")
            if not isinstance(cost, (int, float)):
                cost = session.get("estimated_cost_usd") or session.get("cost_usd")
            if isinstance(cost, (int, float)):
                costs.append(float(cost))
            valid_call_count = isinstance(api_calls, int) and not isinstance(api_calls, bool) and api_calls >= 0
            if valid_call_count and api_calls > 0:
                call_segment_sha256 = _sha256_json(session)
                if call_segment_sha256 in seen_call_segments:
                    attribution_complete = False
                else:
                    seen_call_segments.add(call_segment_sha256)
                    turns += api_calls
                    if model and provider:
                        routes.add((model, provider))
                    else:
                        attribution_complete = False
            elif (
                (not valid_call_count and api_calls is not None)
                or session_tokens > 0
                or (isinstance(cost, (int, float)) and float(cost) > 0)
            ):
                attribution_complete = False
    if len(routes) != 1:
        return None, None, True if routes else None, turns, tokens, sum(costs) if costs else None, False
    model, provider = next(iter(routes))
    fallback_observed = fallback_observed or model != J4_MODEL or provider != "openai-codex"
    return model, provider, fallback_observed, turns, tokens, sum(costs) if costs else None, attribution_complete


def _run_hermes_j4(
    scenario: ScenarioWorkspace,
    envelope: dict[str, Any],
    envelope_sha256: str,
    *,
    output_dir: Path,
    config: J4RuntimeConfig,
) -> dict[str, Any]:
    del config
    workspace_root = _runtime_workspace_path(output_dir, "hermes", envelope["envelope_id"])
    before, clone_errors = _clone_seed(scenario.workspace_dir, workspace_root)
    binary = _binary_identity("hermes", HERMES_BINARY)
    command = _hermes_command(workspace_root=workspace_root, envelope=envelope)
    if clone_errors or not all(binary.values()):
        artifacts = _write_j4_artifacts(
            output_dir,
            runtime="hermes",
            envelope_id=envelope["envelope_id"],
            stdout="",
            stderr="",
        )
        return _base_receipt(
            runtime="hermes",
            binary=binary,
            envelope=envelope,
            envelope_sha256=envelope_sha256,
            status="cli_unavailable" if not HERMES_BINARY.is_file() else "attestation_failed",
            argv=command,
            duration_ms=0,
            exit_code=None,
            workspace=_workspace_receipt(workspace_root, before, envelope=envelope),
            artifacts=artifacts,
            schema_errors=[*clone_errors, "binary_identity"],
        )

    result = _run_process(
        command,
        workspace_root,
        timeout_seconds=envelope["resources"]["wall_clock_seconds"],
        require_workspace_sandbox=True,
    )
    effective_resources, resource_sources = _cli_resource_attestation("hermes", command, envelope)
    authority = _cli_authority_attestation("hermes", command, envelope, result.sandbox)
    payload: dict[str, Any] | None = None
    schema_errors: list[str] = []
    effective_model: str | None = None
    effective_provider: str | None = None
    fallback_observed: bool | None = None
    turns: int | None = None
    tokens: int | None = None
    observed_cost: float | None = None
    transcript = ""
    status = _cli_failure_status(result, runtime="hermes") if result.returncode else "completed"
    if result.returncode == 0:
        try:
            payload = _load_json_object(_ensure_text(result.stdout))
        except ValueError:
            schema_errors.append("whole_output_json")
        schema_errors.extend(_validate_runtime_payload(payload))
        session_id = _hermes_session_id(_ensure_text(result.stderr))
        if session_id is None:
            schema_errors.append("session_id_attestation")
        else:
            export = _run_process(
                [
                    str(HERMES_BINARY.resolve()),
                    "sessions",
                    "export",
                    "-",
                    "--format",
                    "jsonl",
                    "--session-id",
                    session_id,
                    "--redact",
                ],
                workspace_root,
                timeout_seconds=20,
                require_workspace_sandbox=True,
            )
            transcript = _ensure_text(export.stdout)
            if export.returncode != 0:
                schema_errors.append("transcript_export")
            else:
                try:
                    records = _parse_jsonl(transcript)
                except ValueError:
                    records = []
                    schema_errors.append("transcript_jsonl")
                if records:
                    (
                        effective_model,
                        effective_provider,
                        fallback_observed,
                        turns,
                        tokens,
                        observed_cost,
                        attribution_complete,
                    ) = _hermes_attestation(records)
                    if (
                        effective_model != J4_MODEL
                        or effective_provider != "openai-codex"
                        or fallback_observed is not False
                        or turns < 1
                        or attribution_complete is not True
                    ):
                        schema_errors.append("model_route_attestation")
        if effective_resources is None:
            schema_errors.append("resource_attestation")
        if authority is None:
            schema_errors.append("authority_attestation")
        if schema_errors:
            if "authority_attestation" in schema_errors:
                status = "sandbox_unavailable"
            elif "resource_attestation" in schema_errors:
                status = "resource_unavailable"
            else:
                status = (
                    "attestation_failed"
                    if any("attestation" in error or "transcript" in error for error in schema_errors)
                    else "invalid_output"
                )
    artifacts = _write_j4_artifacts(
        output_dir,
        runtime="hermes",
        envelope_id=envelope["envelope_id"],
        stdout=_ensure_text(result.stdout),
        stderr=_ensure_text(result.stderr),
        transcript=transcript,
    )
    workspace = _workspace_receipt(
        workspace_root,
        before,
        envelope=envelope,
        declared_paths=payload.get("files_created") if isinstance(payload, dict) else None,
    )
    if status == "completed" and not workspace["boundary_ok"]:
        status = "sandbox_unavailable"
    return _base_receipt(
        runtime="hermes",
        binary=binary,
        envelope=envelope,
        envelope_sha256=envelope_sha256,
        status=status,
        argv=command,
        duration_ms=result.duration_ms,
        exit_code=result.returncode,
        workspace=workspace,
        artifacts=artifacts,
        parsed_payload=payload,
        schema_errors=schema_errors,
        turns=turns,
        tokens=tokens,
        observed_cost=observed_cost,
        effective_model=effective_model,
        effective_provider=effective_provider,
        fallback_observed=fallback_observed,
        attestation_source="hermes.sessions.export.jsonl" if effective_model else None,
        effective_resources=effective_resources,
        resource_sources=resource_sources,
        authority=authority,
        route_attestation={
            "call_count": turns if isinstance(turns, int) else 0,
            "count_semantics": "exact",
            "routes": ([{"model": effective_model, "provider": effective_provider}] if effective_model else []),
            "source": "hermes.sessions.export.jsonl",
        },
    )


def _http_error_status(status_code: int) -> str:
    if status_code == 401:
        return "auth_required"
    if status_code == 403:
        return "authority_denied"
    if status_code in {404, 410}:
        return "resource_unavailable"
    return "failed"


def _valid_hive_base_url(value: str) -> bool:
    parsed = urlparse(str(value or "").strip())
    if not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    if parsed.scheme == "https":
        return True
    if parsed.scheme != "http":
        return False
    if parsed.hostname.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(parsed.hostname).is_loopback
    except ValueError:
        return False


def _response_json(response: Any) -> Any:
    try:
        return response.json()
    except Exception:
        return None


def _hive_server_build_identity(health_payload: Any) -> dict[str, str] | None:
    if not isinstance(health_payload, dict):
        return None
    components = health_payload.get("components")
    identity = components.get("build_identity") if isinstance(components, dict) else None
    if not isinstance(identity, dict):
        return None
    revision = str(identity.get("revision") or "").strip()
    sha256 = str(identity.get("sha256") or "").strip().lower()
    if not revision or not re.fullmatch(r"[0-9a-f]{64}", sha256):
        return None
    return {"revision": revision, "sha256": sha256}


def _hive_request(
    client: Any,
    *,
    base_url: str,
    bearer: str,
    method: str,
    path: str,
    timeout: float,
    params: dict[str, Any] | None = None,
    payload: dict[str, Any] | None = None,
    extra_headers: dict[str, str] | None = None,
) -> Any:
    headers = {"Authorization": f"Bearer {bearer}", "Accept": "application/json"}
    if extra_headers:
        headers.update(extra_headers)
    return client.request(
        method,
        urljoin(base_url.rstrip("/") + "/", path.lstrip("/")),
        headers=headers,
        params=params,
        json=payload,
        timeout=timeout,
    )


def _hive_workspace_is_clean(
    client: Any,
    *,
    base_url: str,
    bearer: str,
    agent_id: str,
    root: str,
    timeout: float,
) -> tuple[bool, str | None]:
    response = _hive_request(
        client,
        base_url=base_url,
        bearer=bearer,
        method="GET",
        path=f"/api/v1/agents/{agent_id}/files/",
        params={"path": root},
        timeout=timeout,
    )
    if response.status_code == 404:
        return True, None
    if response.status_code >= 400:
        return False, _http_error_status(response.status_code)
    entries = _response_json(response)
    if not isinstance(entries, list):
        return False, "invalid_output"
    return (not entries, None if not entries else "sandbox_unavailable")


def _hive_active_run_fence(
    client: Any,
    *,
    base_url: str,
    bearer: str,
    agent_id: str,
    session_id: str,
    timeout: float,
) -> tuple[bool, dict[str, Any]]:
    response = _hive_request(
        client,
        base_url=base_url,
        bearer=bearer,
        method="GET",
        path=f"/api/v1/agents/{agent_id}/sessions/{session_id}/runs/active",
        timeout=timeout,
    )
    if response.status_code >= 400:
        return False, {"status": "unreconciled", "http_status": response.status_code, "active_run_id": None}
    active_payload = _response_json(response)
    if active_payload is None:
        return True, {"status": "settled", "http_status": response.status_code, "active_run_id": None}
    active_run_id = str(active_payload.get("run_id") or "") if isinstance(active_payload, dict) else ""
    return False, {
        "status": "unreconciled",
        "http_status": response.status_code,
        "active_run_id": active_run_id or None,
    }


def _hive_list_workspace(
    client: Any,
    *,
    base_url: str,
    bearer: str,
    agent_id: str,
    root: str,
    timeout: float,
) -> tuple[dict[str, str], str | None]:
    files: dict[str, str] = {}
    pending = [root]
    seen: set[str] = set()
    while pending:
        directory = pending.pop()
        if directory in seen:
            return {}, "duplicate_directory"
        seen.add(directory)
        response = _hive_request(
            client,
            base_url=base_url,
            bearer=bearer,
            method="GET",
            path=f"/api/v1/agents/{agent_id}/files/",
            params={"path": directory},
            timeout=timeout,
        )
        if response.status_code >= 400:
            return {}, _http_error_status(response.status_code)
        entries = _response_json(response)
        if not isinstance(entries, list):
            return {}, "invalid_output"
        for entry in entries:
            if not isinstance(entry, dict):
                return {}, "invalid_output"
            entry_path = str(entry.get("path") or "")
            try:
                relative = PurePosixPath(entry_path).relative_to(PurePosixPath(root)).as_posix()
                _safe_relative_path(relative)
            except (ValueError, TypeError):
                return {}, "sandbox_unavailable"
            if entry.get("is_dir") is True:
                pending.append(entry_path)
                continue
            if relative in files:
                return {}, "sandbox_unavailable"
            content_response = _hive_request(
                client,
                base_url=base_url,
                bearer=bearer,
                method="GET",
                path=f"/api/v1/agents/{agent_id}/files/content",
                params={"path": entry_path},
                timeout=timeout,
            )
            if content_response.status_code >= 400:
                return {}, _http_error_status(content_response.status_code)
            content_payload = _response_json(content_response)
            if not isinstance(content_payload, dict) or not isinstance(content_payload.get("content"), str):
                return {}, "invalid_output"
            files[relative] = content_payload["content"]
    return files, None


def _replace_local_workspace(root: Path, files: dict[str, str]) -> list[str]:
    expected = set(files)
    errors: list[str] = []
    for path in sorted(root.rglob("*"), reverse=True):
        if path.is_file() and path.relative_to(root).as_posix() not in expected:
            path.unlink()
        elif path.is_dir() and not any(path.iterdir()):
            path.rmdir()
    for relative, content in files.items():
        try:
            safe = _safe_relative_path(relative)
        except ValueError:
            errors.append(f"unsafe_remote_path:{relative}")
            continue
        target = root / safe
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return errors


def _event_run_id(event: dict[str, Any]) -> str:
    scope = event.get("scope") if isinstance(event.get("scope"), dict) else {}
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    return str(scope.get("run_id") or event.get("run_id") or metadata.get("runtime_task_id") or "")


def _event_marker(event: dict[str, Any]) -> tuple[str, str]:
    payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
    metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    kind = str(
        event.get("kind") or event.get("item_kind") or metadata.get("event_type") or event.get("event_type") or ""
    )
    lifecycle = str(event.get("lifecycle") or event.get("item_status") or "")
    return kind, lifecycle


def _hive_transcript_attestation(
    events: list[dict[str, Any]],
    *,
    run_id: str,
) -> dict[str, Any]:
    route: dict[str, Any] | None = None
    final_text: str | None = None
    provider_calls: list[dict[str, Any]] = []
    provider_call_ids: set[str] = set()
    provider_call_event_count = 0
    invalid_provider_call_count = 0
    tokens = 0
    costs: list[float] = []
    terminal_status: str | None = None
    for event in events:
        if _event_run_id(event) != run_id:
            continue
        kind, lifecycle = _event_marker(event)
        payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
        metadata = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
        if kind in {"model_route", "session_context.model_route"} or metadata.get("event_type") == "model_route":
            route = {**metadata, **{key: value for key, value in payload.items() if key != "metadata"}}
        if kind in {"provider_call_ledger", "session_context.provider_call_ledger"}:
            provider_call_event_count += 1
            ledger = _decoded_mapping(metadata.get("provider_prompt_ledger") or payload.get("provider_prompt_ledger"))
            cache_metrics = _decoded_mapping(metadata.get("cache_metrics") or payload.get("cache_metrics"))
            provider_call_id = str(ledger.get("provider_call_id") or "").strip()
            provider = str(ledger.get("provider") or "").strip().lower()
            model = _normalize_model_id(ledger.get("model"))
            tool_names = ledger.get("tool_names")
            tool_schema_sha256 = str(ledger.get("tool_schema_sha256") or "")
            tool_count = metadata.get("tool_count", payload.get("tool_count"))
            if (
                provider_call_id
                and provider
                and model
                and isinstance(tool_count, int)
                and not isinstance(tool_count, bool)
                and isinstance(tool_names, list)
                and tool_names
                and all(isinstance(name, str) and name for name in tool_names)
                and len(tool_names) == len(set(tool_names))
                and re.fullmatch(r"[0-9a-f]{64}", tool_schema_sha256)
                and provider_call_id not in provider_call_ids
            ):
                provider_call_ids.add(provider_call_id)
                provider_calls.append(
                    {
                        "provider_call_id": provider_call_id,
                        "provider": provider,
                        "model": model,
                        "tool_count": tool_count,
                        "tool_names": list(tool_names),
                        "tool_schema_sha256": tool_schema_sha256,
                    }
                )
            else:
                invalid_provider_call_count += 1
            tokens += _usage_total(cache_metrics, {"totalinputtokens", "outputtokens"})
            cost = payload.get("cost_usd") or metadata.get("cost_usd")
            if isinstance(cost, (int, float)):
                costs.append(float(cost))
        if kind in {"assistant_final", "assistant_text.completed"} and lifecycle in {"completed", ""}:
            content = payload.get("content")
            if isinstance(content, str):
                final_text = content
        run_lifecycle = lifecycle if event.get("item_kind") == "run" else ""
        if kind.startswith("run."):
            run_lifecycle = kind.split(".", 1)[1]
        if run_lifecycle in {"completed", "failed", "cancelled", "needs_reconciliation"}:
            terminal_status = run_lifecycle
    fallback_observed = bool(route and route.get("fallback_model"))
    return {
        "route": route,
        "final_text": final_text,
        "fallback_observed": fallback_observed,
        "turns": len(provider_calls),
        "tokens": tokens,
        "observed_cost": sum(costs) if costs else None,
        "terminal_status": terminal_status,
        "call_count": len(provider_calls),
        "provider_call_event_count": provider_call_event_count,
        "invalid_provider_call_count": invalid_provider_call_count,
        "provider_calls": provider_calls,
    }


def _hive_cancel_and_fence(
    client: Any,
    *,
    base_url: str,
    bearer: str,
    agent_id: str,
    session_id: str,
    run_id: str,
    attempt_id: str,
    fence_seconds: int,
) -> tuple[str, list[dict[str, Any]], dict[str, Any] | None, dict[str, Any]]:
    cancel_key = f"p08-j4-cancel:{attempt_id}:{run_id}"
    cancel_response = _hive_request(
        client,
        base_url=base_url,
        bearer=bearer,
        method="POST",
        path=f"/api/v1/agents/{agent_id}/sessions/{session_id}/runs/{run_id}/cancel",
        timeout=10,
        extra_headers={"Idempotency-Key": cancel_key},
    )
    cancel_payload = _response_json(cancel_response)
    cancellation = {
        "requested": True,
        "http_status": cancel_response.status_code,
        "idempotency_key_sha256": _sha256_bytes(cancel_key.encode("utf-8")),
        "receipt": (
            {
                key: cancel_payload[key]
                for key in ("command_id", "run_id", "status", "accepted")
                if isinstance(cancel_payload, dict) and key in cancel_payload
            }
        ),
        "fence": "pending",
    }
    valid_receipt = (
        isinstance(cancel_payload, dict)
        and cancel_payload.get("accepted") is True
        and str(cancel_payload.get("run_id") or "") == run_id
        and str(cancel_payload.get("status") or "") in {"accepted", "applying", "applied"}
        and bool(str(cancel_payload.get("command_id") or "").strip())
    )
    if cancel_response.status_code >= 400 or not valid_receipt:
        cancellation["fence"] = "unreconciled"
        return "needs_reconciliation", [], None, cancellation

    deadline = monotonic() + fence_seconds
    latest_events: list[dict[str, Any]] = []
    latest_attestation: dict[str, Any] | None = None
    while monotonic() < deadline:
        transcript_response = _hive_request(
            client,
            base_url=base_url,
            bearer=bearer,
            method="GET",
            path=f"/api/v1/agents/{agent_id}/sessions/{session_id}/transcript",
            params={"schema_version": 2, "limit": 1000},
            timeout=10,
        )
        active_response = _hive_request(
            client,
            base_url=base_url,
            bearer=bearer,
            method="GET",
            path=f"/api/v1/agents/{agent_id}/sessions/{session_id}/runs/active",
            timeout=10,
        )
        events = _response_json(transcript_response)
        if (
            transcript_response.status_code >= 400
            or active_response.status_code >= 400
            or not isinstance(events, list)
            or any(not isinstance(event, dict) for event in events)
        ):
            cancellation["fence"] = "unreconciled"
            return "needs_reconciliation", latest_events, latest_attestation, cancellation
        latest_events = events
        latest_attestation = _hive_transcript_attestation(events, run_id=run_id)
        active_payload = _response_json(active_response)
        terminal_status = latest_attestation["terminal_status"]
        if active_payload is None and terminal_status is not None:
            cancellation["fence"] = "settled"
            if terminal_status in {"cancelled", "failed", "needs_reconciliation"}:
                return terminal_status, latest_events, latest_attestation, cancellation
            return "timeout", latest_events, latest_attestation, cancellation
        sleep(0.25)
    cancellation["fence"] = "unreconciled"
    return "needs_reconciliation", latest_events, latest_attestation, cancellation


def _run_hive_j4(
    scenario: ScenarioWorkspace,
    envelope: dict[str, Any],
    envelope_sha256: str,
    *,
    output_dir: Path,
    config: J4RuntimeConfig,
) -> dict[str, Any]:
    workspace_root = _runtime_workspace_path(output_dir, "hive", envelope["envelope_id"])
    before, clone_errors = _clone_seed(scenario.workspace_dir, workspace_root)
    attempt_id = uuid.uuid4().hex
    remote_root = f"workspace/p08-j4/{attempt_id}/{envelope['envelope_id']}"
    base_url = str(config.hive_base_url or "").strip()
    binary = {
        "path": base_url,
        "version": "",
        "sha256": "",
        "revision": "",
    }
    argv = [
        "GET /api/v1/auth/me",
        f"GET /api/v1/agents/{config.hive_agent_id or '<agent>'}/files/?path=<attempt-root>",
        f"POST /api/v1/agents/{config.hive_agent_id or '<agent>'}/sessions",
        f"POST /api/v1/agents/{config.hive_agent_id or '<agent>'}/sessions/<session>/runs",
        "GET transcript?schema_version=2",
        "POST run/<run>/cancel + GET runs/active fence on timeout",
    ]
    setup_errors = [*clone_errors]
    if not _valid_hive_base_url(base_url):
        setup_errors.append("hive_base_url")
    if not config.hive_bearer:
        setup_errors.append("hive_bearer")
    if not config.hive_agent_id:
        setup_errors.append("hive_agent_id")
    if setup_errors:
        artifacts = _write_j4_artifacts(
            output_dir,
            runtime="hive",
            envelope_id=envelope["envelope_id"],
            stdout="",
            stderr="",
        )
        return _base_receipt(
            runtime="hive",
            binary=binary,
            envelope=envelope,
            envelope_sha256=envelope_sha256,
            status="resource_unavailable",
            argv=argv,
            duration_ms=0,
            exit_code=None,
            workspace=_workspace_receipt(workspace_root, before, envelope=envelope),
            artifacts=artifacts,
            schema_errors=setup_errors,
            execution={
                "attempt_id": attempt_id,
                "session_id": None,
                "run_id": None,
                "remote_root": remote_root,
                "terminal_status": None,
            },
        )

    owns_client = config.http_client is None
    client = config.http_client or httpx.Client()
    started = monotonic()
    transcript_events: list[dict[str, Any]] = []
    payload: dict[str, Any] | None = None
    schema_errors: list[str] = []
    status = "failed"
    exit_code: int | None = None
    effective_model: str | None = None
    effective_provider: str | None = None
    fallback_observed: bool | None = None
    turns: int | None = None
    tokens: int | None = None
    observed_cost: float | None = None
    run_id = ""
    session_id = ""
    terminal_status: str | None = None
    route_attestation: dict[str, Any] = {"call_count": 0, "routes": [], "source": None}
    effective_resources: dict[str, Any] | None = None
    resource_sources: dict[str, str] = {}
    authority: dict[str, Any] | None = None
    execution: dict[str, Any] = {
        "attempt_id": attempt_id,
        "session_id": None,
        "run_id": None,
        "remote_root": remote_root,
        "terminal_status": None,
    }

    def fence_unsettled_run() -> None:
        nonlocal status, transcript_events, turns, tokens, observed_cost, terminal_status
        try:
            status, fenced_events, fenced_attestation, cancellation = _hive_cancel_and_fence(
                client,
                base_url=base_url,
                bearer=config.hive_bearer or "",
                agent_id=config.hive_agent_id or "",
                session_id=session_id,
                run_id=run_id,
                attempt_id=attempt_id,
                fence_seconds=config.cancel_fence_seconds,
            )
        except (httpx.HTTPError, OSError):
            status = "needs_reconciliation"
            fenced_events = []
            fenced_attestation = None
            cancellation = {"requested": True, "fence": "unreconciled", "http_status": None}
        execution["cancel"] = cancellation
        if fenced_events:
            transcript_events = fenced_events
        if fenced_attestation:
            turns = fenced_attestation["turns"]
            tokens = fenced_attestation["tokens"]
            observed_cost = fenced_attestation["observed_cost"]
            terminal_status = fenced_attestation["terminal_status"]
        execution["terminal_status"] = terminal_status

    try:
        health = _hive_request(
            client,
            base_url=base_url,
            bearer=config.hive_bearer or "",
            method="GET",
            path="/api/health",
            timeout=10,
        )
        if health.status_code >= 400:
            status = _http_error_status(health.status_code)
            raise RuntimeError("typed_http_stop")
        health_payload = _response_json(health)
        if isinstance(health_payload, dict):
            binary["version"] = str(health_payload.get("version") or "")
        if not binary["version"]:
            status = "attestation_failed"
            schema_errors.append("hive_version")
            raise RuntimeError("typed_http_stop")
        server_identity = _hive_server_build_identity(health_payload)
        if server_identity is None:
            status = "resource_unavailable"
            schema_errors.append("hive_build_identity_unavailable")
            raise RuntimeError("typed_http_stop")
        binary.update(server_identity)
        if (config.hive_revision and config.hive_revision != binary["revision"]) or (
            config.hive_binary_sha256 and str(config.hive_binary_sha256).lower() != binary["sha256"]
        ):
            status = "attestation_failed"
            schema_errors.append("hive_build_identity_mismatch")
            raise RuntimeError("typed_http_stop")
        auth = _hive_request(
            client,
            base_url=base_url,
            bearer=config.hive_bearer or "",
            method="GET",
            path="/api/v1/auth/me",
            timeout=10,
        )
        if auth.status_code >= 400:
            status = _http_error_status(auth.status_code)
            raise RuntimeError("typed_http_stop")
        agent = _hive_request(
            client,
            base_url=base_url,
            bearer=config.hive_bearer or "",
            method="GET",
            path=f"/api/v1/agents/{config.hive_agent_id}",
            timeout=10,
        )
        if agent.status_code >= 400:
            status = _http_error_status(agent.status_code)
            raise RuntimeError("typed_http_stop")
        agent_payload = _response_json(agent)
        models = _hive_request(
            client,
            base_url=base_url,
            bearer=config.hive_bearer or "",
            method="GET",
            path="/api/v1/enterprise/llm-models",
            timeout=10,
        )
        if models.status_code >= 400:
            status = _http_error_status(models.status_code)
            raise RuntimeError("typed_http_stop")
        model_rows = _response_json(models)
        primary_id = str(agent_payload.get("primary_model_id") or "") if isinstance(agent_payload, dict) else ""
        primary = (
            next(
                (row for row in model_rows if isinstance(row, dict) and str(row.get("id") or "") == primary_id),
                None,
            )
            if isinstance(model_rows, list)
            else None
        )
        if (
            not isinstance(agent_payload, dict)
            or agent_payload.get("fallback_model_id") is not None
            or int(agent_payload.get("max_tool_rounds") or 0) != envelope["resources"]["max_tool_rounds"]
            or not isinstance(primary, dict)
            or primary.get("provider") != "openai-response"
            or _normalize_model_id(primary.get("model")) != J4_MODEL
            or str(primary.get("reasoning_effort") or "").lower() != J4_REASONING_EFFORT
            or primary.get("enabled") is not True
        ):
            status = "model_unavailable"
            raise RuntimeError("typed_http_stop")

        effective_resources = _expected_resources(envelope)
        resource_sources = {
            "max_tool_rounds": "hive.agent.max_tool_rounds",
            "wall_clock_seconds": "j4.adapter.deadline",
            "reasoning_effort": "hive.primary_model.reasoning_effort",
        }
        is_clean, clean_error = _hive_workspace_is_clean(
            client,
            base_url=base_url,
            bearer=config.hive_bearer or "",
            agent_id=config.hive_agent_id or "",
            root=remote_root,
            timeout=10,
        )
        if not is_clean:
            status = clean_error if clean_error in J4_STATUSES else "sandbox_unavailable"
            schema_errors.append(f"attempt_workspace:{clean_error or 'not_clean'}")
            raise RuntimeError("typed_http_stop")

        session = _hive_request(
            client,
            base_url=base_url,
            bearer=config.hive_bearer or "",
            method="POST",
            path=f"/api/v1/agents/{config.hive_agent_id}/sessions",
            payload={
                "title": f"P08-J4 {envelope['envelope_id']} {attempt_id}",
                "permission_mode": "bypassPermissions",
                "allowed_tools": list(_J4_ALLOWED_TOOLS["hive"]),
                "writable_roots": [remote_root],
            },
            timeout=10,
        )
        if session.status_code >= 400:
            status = _http_error_status(session.status_code)
            raise RuntimeError("typed_http_stop")
        session_payload = _response_json(session)
        session_id = str(session_payload.get("id") or "") if isinstance(session_payload, dict) else ""
        execution["session_id"] = session_id or None
        if not session_id:
            status = "invalid_output"
            schema_errors.append("session_id")
            raise RuntimeError("typed_http_stop")
        authority = _hive_session_authority(session_payload, remote_root=remote_root, envelope=envelope)
        if authority is None:
            status = "sandbox_unavailable"
            schema_errors.append("session_authority_attestation")
            raise RuntimeError("typed_http_stop")

        expected_seed_files: dict[str, str] = {}
        for entry in envelope["workspace"]["seed_manifest"]:
            relative = _safe_relative_path(str(entry["path"]))
            content = (scenario.workspace_dir / relative).read_text(encoding="utf-8")
            expected_seed_files[relative] = content
            write = _hive_request(
                client,
                base_url=base_url,
                bearer=config.hive_bearer or "",
                method="PUT",
                path=f"/api/v1/agents/{config.hive_agent_id}/files/content",
                params={"path": f"{remote_root}/{relative}"},
                payload={"content": content},
                timeout=10,
            )
            if write.status_code >= 400:
                status = _http_error_status(write.status_code)
                raise RuntimeError("typed_http_stop")
            readback = _hive_request(
                client,
                base_url=base_url,
                bearer=config.hive_bearer or "",
                method="GET",
                path=f"/api/v1/agents/{config.hive_agent_id}/files/content",
                params={"path": f"{remote_root}/{relative}"},
                timeout=10,
            )
            readback_payload = _response_json(readback)
            if (
                readback.status_code >= 400
                or not isinstance(readback_payload, dict)
                or readback_payload.get("content") != content
            ):
                status = "attestation_failed"
                schema_errors.append(f"seed_readback:{relative}")
                raise RuntimeError("typed_http_stop")
        seeded_files, seed_error = _hive_list_workspace(
            client,
            base_url=base_url,
            bearer=config.hive_bearer or "",
            agent_id=config.hive_agent_id or "",
            root=remote_root,
            timeout=10,
        )
        if seed_error or seeded_files != expected_seed_files:
            status = seed_error if seed_error in J4_STATUSES else "attestation_failed"
            schema_errors.append(f"seed_manifest:{seed_error or 'mismatch'}")
            raise RuntimeError("typed_http_stop")

        run = _hive_request(
            client,
            base_url=base_url,
            bearer=config.hive_bearer or "",
            method="POST",
            path=f"/api/v1/agents/{config.hive_agent_id}/sessions/{session_id}/runs",
            payload={
                "content": scenario.prompt,
                "display_content": scenario.prompt,
                "permission_mode": "bypassPermissions",
                "model_routing_locked": True,
                "idempotency_key": f"p08-j4:{envelope_sha256}:{attempt_id}",
            },
            timeout=10,
        )
        if run.status_code >= 400:
            status = _http_error_status(run.status_code)
            raise RuntimeError("typed_http_stop")
        run_payload = _response_json(run)
        run_id = str(run_payload.get("run_id") or "") if isinstance(run_payload, dict) else ""
        execution["run_id"] = run_id or None
        if not run_id:
            status = "invalid_output"
            schema_errors.append("run_id")
            raise RuntimeError("typed_http_stop")

        deadline = monotonic() + envelope["resources"]["wall_clock_seconds"]
        terminal = False
        final_text: str | None = None
        route: dict[str, Any] | None = None
        transcript_attestation: dict[str, Any] | None = None
        while monotonic() < deadline:
            transcript_response = _hive_request(
                client,
                base_url=base_url,
                bearer=config.hive_bearer or "",
                method="GET",
                path=f"/api/v1/agents/{config.hive_agent_id}/sessions/{session_id}/transcript",
                params={"schema_version": 2, "limit": 1000},
                timeout=10,
            )
            if transcript_response.status_code >= 400:
                status = _http_error_status(transcript_response.status_code)
                raise RuntimeError("typed_http_stop")
            events = _response_json(transcript_response)
            if not isinstance(events, list) or any(not isinstance(event, dict) for event in events):
                status = "invalid_output"
                raise RuntimeError("typed_http_stop")
            transcript_events = events
            transcript_attestation = _hive_transcript_attestation(events, run_id=run_id)
            route = transcript_attestation["route"]
            final_text = transcript_attestation["final_text"]
            fallback_observed = transcript_attestation["fallback_observed"]
            turns = transcript_attestation["turns"]
            tokens = transcript_attestation["tokens"]
            observed_cost = transcript_attestation["observed_cost"]
            terminal_status = transcript_attestation["terminal_status"]
            terminal = terminal_status is not None
            if terminal:
                execution["terminal_status"] = terminal_status
                break
            sleep(0.25)
        if not terminal:
            fence_unsettled_run()
            raise RuntimeError("typed_http_stop")
        if terminal_status in {"failed", "cancelled", "needs_reconciliation"}:
            status = terminal_status
            execution["terminal_status"] = terminal_status
            raise RuntimeError("typed_http_stop")
        try:
            active_settled, active_fence = _hive_active_run_fence(
                client,
                base_url=base_url,
                bearer=config.hive_bearer or "",
                agent_id=config.hive_agent_id or "",
                session_id=session_id,
                timeout=10,
            )
        except (httpx.HTTPError, OSError):
            active_settled = False
            active_fence = {"status": "unreconciled", "http_status": None, "active_run_id": None}
        execution["active_fence"] = active_fence
        if not active_settled:
            status = "needs_reconciliation"
            schema_errors.append("active_run_fence")
            raise RuntimeError("typed_http_stop")
        assert transcript_attestation is not None
        provider_calls = transcript_attestation["provider_calls"]
        actual_routes = [
            {"model": call["model"], "provider": call["provider"]} for call in provider_calls if isinstance(call, dict)
        ]
        route_attestation = {
            "call_count": transcript_attestation["call_count"],
            "count_semantics": "exact",
            "routes": actual_routes,
            "source": "hive.transcript.v2.provider_call_ledger",
            "tool_names": list(provider_calls[0].get("tool_names") or ()) if provider_calls else [],
            "tool_schema_sha256": provider_calls[0].get("tool_schema_sha256") if provider_calls else None,
        }
        if actual_routes:
            effective_model = actual_routes[0]["model"]
            effective_provider = actual_routes[0]["provider"]
        if (
            effective_model != J4_MODEL
            or effective_provider != "openai-response"
            or fallback_observed is not False
            or not isinstance(route, dict)
            or route.get("model_routing_locked") is not True
            or _normalize_model_id(route.get("selected_model")) != J4_MODEL
            or str(route.get("selected_provider") or "") != "openai-response"
            or not provider_calls
            or transcript_attestation["invalid_provider_call_count"] > 0
            or transcript_attestation["provider_call_event_count"] != len(provider_calls)
            or any(
                call.get("model") != J4_MODEL
                or call.get("provider") != "openai-response"
                or call.get("tool_count") != len(_J4_ALLOWED_TOOLS["hive"])
                or call.get("tool_names") != list(_J4_ALLOWED_TOOLS["hive"])
                for call in provider_calls
            )
            or len({call.get("tool_schema_sha256") for call in provider_calls}) != 1
        ):
            status = "attestation_failed"
            schema_errors.append("model_route_attestation")
            raise RuntimeError("typed_http_stop")
        try:
            payload = _load_json_object(final_text or "")
        except ValueError:
            schema_errors.append("whole_output_json")
        schema_errors.extend(_validate_runtime_payload(payload))
        if schema_errors:
            status = "invalid_output"
            raise RuntimeError("typed_http_stop")
        remote_files, remote_error = _hive_list_workspace(
            client,
            base_url=base_url,
            bearer=config.hive_bearer or "",
            agent_id=config.hive_agent_id or "",
            root=remote_root,
            timeout=10,
        )
        if remote_error:
            status = remote_error if remote_error in J4_STATUSES else "attestation_failed"
            schema_errors.append(f"workspace_readback:{remote_error}")
            raise RuntimeError("typed_http_stop")
        schema_errors.extend(_replace_local_workspace(workspace_root, remote_files))
        status = "completed" if not schema_errors else "sandbox_unavailable"
        terminal_status = "completed"
        execution["terminal_status"] = terminal_status
        exit_code = 0
    except (httpx.HTTPError, OSError):
        if run_id and terminal_status is None and "cancel" not in execution:
            fence_unsettled_run()
        else:
            status = "needs_reconciliation" if run_id and terminal_status is None else "resource_unavailable"
            execution["terminal_status"] = terminal_status
    except (KeyError, TypeError, ValueError):
        status = "invalid_output"
        if run_id and terminal_status is None and "cancel" not in execution:
            fence_unsettled_run()
    except RuntimeError as exc:
        if str(exc) != "typed_http_stop":
            status = "failed"
        if run_id and terminal_status is None and "cancel" not in execution:
            fence_unsettled_run()
    finally:
        if owns_client:
            client.close()

    transcript_text = "\n".join(_canonical_json(event) for event in transcript_events)
    artifacts = _write_j4_artifacts(
        output_dir,
        runtime="hive",
        envelope_id=envelope["envelope_id"],
        stdout=_canonical_json(payload) if payload is not None else "",
        stderr="",
        transcript=transcript_text,
    )
    workspace = _workspace_receipt(
        workspace_root,
        before,
        envelope=envelope,
        declared_paths=payload.get("files_created") if isinstance(payload, dict) else None,
    )
    if status == "completed" and not workspace["boundary_ok"]:
        status = "sandbox_unavailable"
    return _base_receipt(
        runtime="hive",
        binary=binary,
        envelope=envelope,
        envelope_sha256=envelope_sha256,
        status=status,
        argv=argv,
        duration_ms=int((monotonic() - started) * 1000),
        exit_code=exit_code,
        workspace=workspace,
        artifacts=artifacts,
        parsed_payload=payload,
        schema_errors=schema_errors,
        turns=turns,
        tokens=tokens,
        observed_cost=observed_cost,
        effective_model=effective_model,
        effective_provider=effective_provider,
        fallback_observed=fallback_observed,
        attestation_source="hive.transcript.v2.provider_call_ledger" if effective_model else None,
        effective_resources=effective_resources,
        resource_sources=resource_sources,
        authority=authority,
        route_attestation=route_attestation,
        execution=execution,
    )


def _eval_integer_expression(node: ast.expr, bindings: dict[str, int]) -> int:
    if isinstance(node, ast.Name) and node.id in bindings:
        return bindings[node.id]
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return _eval_integer_expression(node.left, bindings) + _eval_integer_expression(node.right, bindings)
    raise ValueError("unsupported expression")


def _external_score(scenario_name: str, workspace_root: Path) -> dict[str, Any]:
    checks: dict[str, bool]
    if scenario_name == "coding":
        try:
            tree = ast.parse(_safe_read(workspace_root / "calculator.py"))
        except (SyntaxError, ValueError):
            tree = ast.Module(body=[], type_ignores=[])
        valid_add = False
        add_expression: ast.expr | None = None
        for node in tree.body:
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) or node.name != "add":
                continue
            if [argument.arg for argument in node.args.args] != ["a", "b"] or len(node.body) != 1:
                continue
            statement = node.body[0]
            valid_add = bool(
                isinstance(statement, ast.Return)
                and isinstance(statement.value, ast.BinOp)
                and isinstance(statement.value.op, ast.Add)
                and isinstance(statement.value.left, ast.Name)
                and statement.value.left.id == "a"
                and isinstance(statement.value.right, ast.Name)
                and statement.value.right.id == "b"
            )
            if valid_add:
                add_expression = statement.value
        execution_assertions = bool(
            add_expression is not None
            and all(
                _eval_integer_expression(add_expression, {"a": a, "b": b}) == expected
                for a, b, expected in ((2, 3, 5), (-4, 1, -3), (0, 0, 0))
            )
        )
        checks = {
            "coding.ast_add": valid_add,
            "coding.execution_assertions": execution_assertions,
        }
    elif scenario_name == "review":
        content = _safe_read(workspace_root / "review.md")
        lowered = content.lower()
        finding_lines = [line for line in lowered.splitlines() if line.strip().startswith(("[p1]", "[p2]"))]
        checks = {
            "review.priority": bool(finding_lines),
            "review.file_ref": (workspace_root / "review_target.py").is_file()
            and any("review_target.py" in line for line in finding_lines),
            "review.issue": any(
                token in line for line in finding_lines for token in ("index", "range", "pop", "out of bounds")
            ),
        }
    elif scenario_name == "research":
        lines = {line.strip().lower() for line in _safe_read(workspace_root / "research_summary.md").splitlines()}
        checks = {
            "research.winner": "winner: atlas" in lines,
            "research.date": "ship date: 2026-05-01" in lines,
            "research.source_refs": "sources: source_alpha.md, source_beta.md" in lines,
        }
    elif scenario_name == "operations":
        lines = [line.strip().lower() for line in _safe_read(workspace_root / "ops_summary.md").splitlines()]
        staging_lines = [line for line in lines if line.startswith("staging verification:")]
        checks = {
            "operations.root_cause": "root cause: database_url missing" in lines,
            "operations.staging_verification": any(
                "database_url" in line and "startup" in line for line in staging_lines
            ),
        }
    elif scenario_name == "delegation":
        lines = {line.strip().lower() for line in _safe_read(workspace_root / "delegation_plan.md").splitlines()}
        checks = {
            "delegation.coverage": "tasks: task-a, task-b" in lines,
            "delegation.mode": bool({"mode: parallel", "mode: serial"} & lines),
            "delegation.fallback": "fallback: serial if delegation unavailable" in lines,
        }
    elif scenario_name == "memory_recall":
        checks = {"memory.exact_bytes": _safe_read(workspace_root / "memory_answer.md") == "cedar-lantern"}
    elif scenario_name == "self_evolution":
        lines = {line.strip().lower() for line in _safe_read(workspace_root / "self_evolution.md").splitlines()}
        checks = {
            "self_evolution.repeated_evidence": "evidence count: 6" in lines,
            "self_evolution.skill_decision": bool(
                {"decision: update existing skill", "decision: create new skill"} & lines
            ),
        }
    elif scenario_name == "long_context_after_compaction":
        checks = {
            "long_context.exact_bytes": _safe_read(workspace_root / "long_context_answer.md") == "delta-saffron-42"
        }
    else:
        raise ValueError(f"Unsupported external scoring scenario: {scenario_name}")
    passed = sum(1 for value in checks.values() if value)
    return {
        "score": round((passed / len(checks)) * 100) if checks else 0,
        "ready": bool(checks) and passed == len(checks),
        "criteria": checks,
        "source": "external_workspace_assertions",
    }


def _receipt_blockers(receipt: dict[str, Any], envelope: dict[str, Any], envelope_sha256: str) -> list[str]:
    runtime = str(receipt.get("runtime") or "")
    blockers: list[str] = []
    if receipt.get("schema") != J4_RECEIPT_SCHEMA:
        blockers.append("receipt_schema")
    if receipt.get("status") != "completed":
        blockers.append(f"status:{receipt.get('status') or 'missing'}")
    if receipt.get("envelope_sha256") != envelope_sha256:
        blockers.append("envelope_sha256")
    if receipt.get("seed_sha256") != envelope["workspace"]["seed_sha256"]:
        blockers.append("seed_sha256")
    if receipt.get("scorer_sha256") != envelope["scorer"]["source_sha256"]:
        blockers.append("scorer_sha256")
    if receipt.get("effective_model") != J4_MODEL:
        blockers.append("effective_model")
    allowed_routes = envelope["model"]["allowed_provider_routes"].get(runtime, [])
    if receipt.get("effective_provider") not in allowed_routes:
        blockers.append("effective_provider")
    if receipt.get("fallback_observed") is not False:
        blockers.append("fallback_observed")
    if not receipt.get("attestation_source"):
        blockers.append("attestation_source")
    if receipt.get("parsed", {}).get("schema_valid") is not True:
        blockers.append("parsed_schema")
    if receipt.get("workspace", {}).get("boundary_ok") is not True:
        blockers.append("workspace_boundary")
    if receipt.get("workspace", {}).get("before_sha256") != envelope["workspace"]["seed_sha256"]:
        blockers.append("workspace_seed")
    expected_resources = {
        "max_tool_rounds": envelope["resources"]["max_tool_rounds"],
        "wall_clock_seconds": envelope["resources"]["wall_clock_seconds"],
        "reasoning_effort": envelope["model"]["reasoning_effort"],
    }
    requested = receipt.get("resources", {}).get("requested")
    effective = receipt.get("resources", {}).get("effective")
    resource_sources = receipt.get("resources", {}).get("sources")
    if (
        requested != expected_resources
        or effective != expected_resources
        or not isinstance(resource_sources, dict)
        or any(not resource_sources.get(name) for name in expected_resources)
    ):
        blockers.append("hard_common_resources")
    expected_authority = {
        "allowed_tools": envelope["authority"]["allowed_tools"].get(runtime),
        "writable_scope": envelope["authority"]["writable_scope"],
    }
    authority = receipt.get("authority") if isinstance(receipt.get("authority"), dict) else {}
    authority_sources = authority.get("sources") if isinstance(authority.get("sources"), dict) else {}
    sandbox = authority.get("sandbox") if isinstance(authority.get("sandbox"), dict) else {}
    if (
        authority.get("requested") != expected_authority
        or authority.get("effective") != expected_authority
        or not authority_sources.get("allowed_tools")
        or not authority_sources.get("writable_scope")
        or sandbox.get("status") != "enforced"
    ):
        blockers.append("authority_envelope")
    route_attestation = receipt.get("route_attestation") if isinstance(receipt.get("route_attestation"), dict) else {}
    routes = route_attestation.get("routes") if isinstance(route_attestation.get("routes"), list) else []
    call_count = route_attestation.get("call_count")
    count_semantics = route_attestation.get("count_semantics", "exact")
    turns = receipt.get("turns")
    hive_tool_identity_valid = runtime != "hive" or (
        route_attestation.get("tool_names") == list(envelope["authority"]["allowed_tools"]["hive"])
        and bool(re.fullmatch(r"[0-9a-f]{64}", str(route_attestation.get("tool_schema_sha256") or "")))
    )
    if (
        not isinstance(call_count, int)
        or isinstance(call_count, bool)
        or call_count < 1
        or count_semantics not in {"exact", "minimum_observed"}
        or not isinstance(turns, int)
        or isinstance(turns, bool)
        or turns < 1
        or (count_semantics == "exact" and turns != call_count)
        or (count_semantics == "minimum_observed" and turns < call_count)
        or not route_attestation.get("source")
        or not hive_tool_identity_valid
        or not routes
        or any(
            not isinstance(route, dict) or route.get("model") != J4_MODEL or route.get("provider") not in allowed_routes
            for route in routes
        )
    ):
        blockers.append("provider_call_evidence")
    if runtime == "hive":
        execution = receipt.get("execution") if isinstance(receipt.get("execution"), dict) else {}
        active_fence = execution.get("active_fence") if isinstance(execution.get("active_fence"), dict) else {}
        attempt_id = str(execution.get("attempt_id") or "")
        expected_root_prefix = f"workspace/p08-j4/{attempt_id}/"
        if (
            not attempt_id
            or not execution.get("session_id")
            or not execution.get("run_id")
            or not str(execution.get("remote_root") or "").startswith(expected_root_prefix)
            or execution.get("terminal_status") != "completed"
            or active_fence.get("status") != "settled"
            or not isinstance(active_fence.get("http_status"), int)
            or not 200 <= active_fence["http_status"] < 300
            or active_fence.get("active_run_id") is not None
        ):
            blockers.append("execution_refs")
    binary = receipt.get("binary") if isinstance(receipt.get("binary"), dict) else {}
    if not all(binary.get(key) for key in ("path", "version", "revision")) or not re.fullmatch(
        r"[0-9a-f]{64}", str(binary.get("sha256") or "")
    ):
        blockers.append("binary_identity")
    artifacts = receipt.get("artifacts") if isinstance(receipt.get("artifacts"), dict) else {}
    for name in ("stdout", "stderr", "transcript"):
        artifact = artifacts.get(name) if isinstance(artifacts.get(name), dict) else {}
        if not artifact.get("path") or not re.fullmatch(r"[0-9a-f]{64}", str(artifact.get("sha256") or "")):
            blockers.append(f"artifact:{name}")
    return blockers


def _empty_j4_report(*, blockers: list[dict[str, Any]], receipts: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {
        "kind": "bakeoff",
        "schema": J4_ENVELOPE_SCHEMA,
        "transport": "runtime_unavailable",
        "runtime": {"status": "blocked"},
        "auth_status": "blocked",
        "benchmark_complete": False,
        "acceptance_ready": False,
        "comparison": {"status": "blocked", "scores": {}, "blockers": blockers},
        "scenario_scores": {},
        "receipts": receipts or [],
        "envelopes": [],
        "artifact_paths": [],
        "scenarios": {},
    }


def _j4_acceptance_decision(scenario_scores: dict[str, dict[str, Any]]) -> dict[str, Any]:
    comparisons: dict[str, dict[str, bool]] = {}
    all_hard_criteria_ready = bool(scenario_scores)
    hive_not_weaker = bool(scenario_scores)
    for scenario_name, runtime_scores in scenario_scores.items():
        criteria_names = set(_J4_CRITERIA.get(scenario_name, ()))
        exact_coverage = bool(criteria_names) and all(
            set(((runtime_scores.get(runtime) or {}).get("criteria") or {})) == criteria_names
            for runtime in _J4_RUNTIME_ORDER
        )
        all_hard_criteria_ready = (
            all_hard_criteria_ready
            and all(bool((runtime_scores.get(runtime) or {}).get("ready")) for runtime in _J4_RUNTIME_ORDER)
            and exact_coverage
        )
        if not exact_coverage:
            all_hard_criteria_ready = False
            hive_not_weaker = False
        for criterion in sorted(criteria_names):
            observed = {
                runtime: bool(((runtime_scores.get(runtime) or {}).get("criteria") or {}).get(criterion))
                for runtime in _J4_RUNTIME_ORDER
            }
            not_weaker = observed["hive"] or not (observed["freecode"] or observed["hermes"])
            hive_not_weaker = hive_not_weaker and not_weaker
            comparisons[f"{scenario_name}.{criterion}"] = {**observed, "hive_not_weaker": not_weaker}
    return {
        "acceptance_ready": all_hard_criteria_ready and hive_not_weaker,
        "all_hard_criteria_ready": all_hard_criteria_ready,
        "hive_not_weaker": hive_not_weaker,
        "comparisons": comparisons,
    }


def run_same_envelope_bakeoff(
    *,
    output_dir: Path,
    config: J4RuntimeConfig,
) -> dict[str, Any]:
    """Run the manual three-runtime P08-J4 comparison without any model preflight."""

    precondition_blockers: list[dict[str, Any]] = []
    if not config.external_profile_authorized:
        precondition_blockers.append(
            {"code": "external_profile_authority_required", "runtimes": ["freecode", "hermes"]}
        )
    hive_base_url = str(config.hive_base_url or "").strip()
    hive_configured = all(
        (
            _valid_hive_base_url(hive_base_url),
            config.hive_bearer,
            config.hive_agent_id,
        )
    )
    if not hive_configured:
        precondition_blockers.append({"code": "hive_session_authority_required", "runtime": "hive"})
    if (
        config.max_tool_rounds < 1
        or config.wall_clock_seconds < 1
        or config.cancel_fence_seconds < 1
        or config.max_budget_usd <= 0
        or config.max_files < 1
        or config.max_bytes < 1
    ):
        precondition_blockers.append({"code": "resource_configuration_invalid"})
    if config.require_same_credential_domain:
        precondition_blockers.append(
            {
                "code": "model_auth_unavailable",
                "detail": "The three allowed routes use different provider and credential domains.",
            }
        )
    if precondition_blockers:
        return _empty_j4_report(blockers=precondition_blockers)

    seed_root = output_dir / "j4_seed"
    seed_root.mkdir(parents=True, exist_ok=True)
    receipts: list[dict[str, Any]] = []
    envelopes: list[dict[str, Any]] = []
    envelope_hashes: dict[str, str] = {}
    stopped_runtimes: set[str] = set()
    adapters = {
        "hive": _run_hive_j4,
        "freecode": _run_freecode_j4,
        "hermes": _run_hermes_j4,
    }
    for scenario_name in _SCENARIOS:
        scenario = _scenario_workspace(seed_root, scenario_name)
        envelope, envelope_sha256 = _build_same_envelope(scenario, config=config)
        envelopes.append({"envelope": envelope, "sha256": envelope_sha256})
        envelope_hashes[scenario_name] = envelope_sha256
        for runtime in _J4_RUNTIME_ORDER:
            if runtime in stopped_runtimes:
                continue
            receipt = adapters[runtime](
                scenario,
                envelope,
                envelope_sha256,
                output_dir=output_dir,
                config=config,
            )
            receipts.append(receipt)
            if receipt.get("status") == "auth_required":
                stopped_runtimes.add(runtime)

    blockers: list[dict[str, Any]] = []
    by_scenario_runtime = {
        (str(envelope["task"]["scenario"]), runtime): next(
            (
                receipt
                for receipt in receipts
                if receipt.get("runtime") == runtime
                and receipt.get("envelope_sha256") == envelope_hashes[str(envelope["task"]["scenario"])]
            ),
            None,
        )
        for envelope_entry in envelopes
        for envelope in [envelope_entry["envelope"]]
        for runtime in _J4_RUNTIME_ORDER
    }
    for envelope_entry in envelopes:
        envelope = envelope_entry["envelope"]
        scenario_name = str(envelope["task"]["scenario"])
        envelope_sha256 = envelope_entry["sha256"]
        common_resources: set[str] = set()
        for runtime in _J4_RUNTIME_ORDER:
            receipt = by_scenario_runtime[(scenario_name, runtime)]
            if receipt is None:
                blockers.append({"scenario": scenario_name, "runtime": runtime, "code": "receipt_missing"})
                continue
            for code in _receipt_blockers(receipt, envelope, envelope_sha256):
                blockers.append({"scenario": scenario_name, "runtime": runtime, "code": code})
            common_resources.add(_canonical_json(receipt.get("resources", {}).get("effective")))
        if len(common_resources) != 1:
            blockers.append({"scenario": scenario_name, "code": "hard_common_mismatch"})

    if blockers:
        report = _empty_j4_report(blockers=blockers, receipts=receipts)
        report["envelopes"] = envelopes
        report["artifact_paths"] = [
            artifact["path"]
            for receipt in receipts
            for artifact in receipt.get("artifacts", {}).values()
            if isinstance(artifact, dict) and artifact.get("path")
        ]
        return report

    scenario_scores: dict[str, dict[str, Any]] = {}
    scenarios: dict[str, dict[str, Any]] = {}
    runtime_totals = {runtime: [] for runtime in _J4_RUNTIME_ORDER}
    for envelope_entry in envelopes:
        envelope = envelope_entry["envelope"]
        scenario_name = str(envelope["task"]["scenario"])
        scenario_scores[scenario_name] = {}
        for runtime in _J4_RUNTIME_ORDER:
            receipt = by_scenario_runtime[(scenario_name, runtime)]
            assert receipt is not None
            score = _external_score(scenario_name, Path(receipt["workspace"]["local_path"]))
            receipt["score"] = score
            scenario_scores[scenario_name][runtime] = score
            runtime_totals[runtime].append(int(score["score"]))
        scores = [int(details["score"]) for details in scenario_scores[scenario_name].values()]
        scenarios[scenario_name] = {
            "ready": all(details["ready"] for details in scenario_scores[scenario_name].values()),
            "score": round(sum(scores) / len(scores)),
            "transcript": "external_workspace_assertions",
            "rubric": f"P08-J4 external rubric {scenario_name}",
            "score_breakdown": scenario_scores[scenario_name],
        }
    comparison_scores = {
        runtime: round(sum(scores) / len(scores), 2) if scores else 0.0 for runtime, scores in runtime_totals.items()
    }
    acceptance = _j4_acceptance_decision(scenario_scores)
    return {
        "kind": "bakeoff",
        "schema": J4_ENVELOPE_SCHEMA,
        "transport": "same_envelope_live",
        "runtime": {"status": "completed"},
        "auth_status": "ok",
        "benchmark_complete": True,
        "acceptance_ready": acceptance["acceptance_ready"],
        "comparison": {
            "status": "completed",
            "scores": comparison_scores,
            "blockers": [],
            "acceptance": acceptance,
        },
        "scenario_scores": scenario_scores,
        "receipts": receipts,
        "envelopes": envelopes,
        "artifact_paths": [
            artifact["path"]
            for receipt in receipts
            for artifact in receipt.get("artifacts", {}).values()
            if isinstance(artifact, dict) and artifact.get("path")
        ],
        "scenarios": scenarios,
    }
