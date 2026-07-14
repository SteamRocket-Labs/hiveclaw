"""Verification gates for self-evolution candidates."""

from __future__ import annotations

import re
import subprocess
import tempfile
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml

from app.services.evolution_ledger import record_eval_run


_LOCAL_MARKDOWN_LINK_RE = re.compile(r"\[[^\]]+\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_SKILL_RESOURCE_DIRS = frozenset({"references", "scripts", "templates", "assets", "evals"})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_result(
    *,
    check_type: str,
    passed: bool,
    message: str,
    evidence: dict[str, Any] | None = None,
    gating: bool = True,
    score: float | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        "type": check_type,
        "passed": passed,
        "message": message,
        "evidence": evidence or {},
    }
    # E4/D3: only gating checks decide pass/fail; non-gating checks are observational.
    if not gating:
        result["gating"] = False
    if score is not None:
        result["score"] = score
    return result


def _run_deterministic_command(workspace: Path, grader: dict[str, Any]) -> dict[str, Any]:
    command = grader.get("command")
    if not isinstance(command, list) or not command:
        return _check_result(
            check_type="deterministic_command",
            passed=False,
            message="deterministic command grader requires a non-empty command list",
        )
    timeout = int(grader.get("timeout_seconds") or 30)
    try:
        completed = subprocess.run(  # noqa: S603
            [str(part) for part in command],
            cwd=str(workspace),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        return _check_result(
            check_type="deterministic_command",
            passed=False,
            message=f"command timed out after {timeout}s",
            evidence={"stdout": exc.stdout or "", "stderr": exc.stderr or "", "timeout_seconds": timeout},
        )
    return _check_result(
        check_type="deterministic_command",
        passed=completed.returncode == 0,
        message="command passed" if completed.returncode == 0 else "command failed",
        evidence={
            "command": [str(part) for part in command],
            "returncode": completed.returncode,
            "stdout": completed.stdout,
            "stderr": completed.stderr,
        },
    )


def _run_state_check(workspace: Path, grader: dict[str, Any]) -> dict[str, Any]:
    relative_path = str(grader.get("path") or "").strip()
    if not relative_path:
        return _check_result(check_type="state_check", passed=False, message="state_check requires path")
    path = workspace / relative_path
    if not path.exists():
        return _check_result(
            check_type="state_check",
            passed=False,
            message="path does not exist",
            evidence={"path": relative_path},
        )
    expected = grader.get("contains")
    if expected is None:
        return _check_result(
            check_type="state_check",
            passed=True,
            message="path exists",
            evidence={"path": relative_path},
        )
    content = path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""
    passed = str(expected) in content
    return _check_result(
        check_type="state_check",
        passed=passed,
        message="expected content found" if passed else "expected content missing",
        evidence={"path": relative_path, "contains": str(expected)},
    )


def _run_tool_call_check(grader: dict[str, Any]) -> dict[str, Any]:
    tool_calls = [str(item) for item in grader.get("tool_calls") or []]
    required = [str(item) for item in grader.get("required_tools") or []]
    forbidden = [str(item) for item in grader.get("forbidden_tools") or []]
    missing = [tool for tool in required if tool not in tool_calls]
    forbidden_seen = [tool for tool in forbidden if tool in tool_calls]
    passed = not missing and not forbidden_seen
    return _check_result(
        check_type="tool_call_check",
        passed=passed,
        message="tool call contract passed" if passed else "tool call contract failed",
        evidence={"missing": missing, "forbidden_seen": forbidden_seen, "tool_calls": tool_calls},
    )


def _slugify_skill_name(name: str, *, fallback: str = "candidate-skill") -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", name.strip().lower()).strip("-._")
    return slug or fallback


def _extract_frontmatter(content: str) -> tuple[dict[str, Any], list[str]]:
    from app.skills.parser import SkillParser

    match = SkillParser.FRONTMATTER_PATTERN.match(content.strip())
    if not match:
        return {}, ["frontmatter"]

    try:
        loaded = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError:
        return {}, ["frontmatter_yaml"]
    if not isinstance(loaded, dict):
        return {}, ["frontmatter_mapping"]
    return loaded, []


def _normalize_skill_file_path(relative_path: str, *, skill_name: str) -> Path:
    raw_path = Path(relative_path.strip().lstrip("/") or "SKILL.md")
    if raw_path.is_absolute() or any(part == ".." for part in raw_path.parts):
        return Path("skills") / _slugify_skill_name(skill_name) / "SKILL.md"

    parts = raw_path.parts
    if parts and parts[0] == "skills":
        if raw_path.name in {"SKILL.md", "skill.md"} and len(parts) >= 3:
            return raw_path
        if raw_path.suffix == ".md" and len(parts) == 2:
            return raw_path

    slug = _slugify_skill_name(skill_name)
    return Path("skills") / slug / "SKILL.md"


def _local_skill_resource_links(content: str) -> list[str]:
    references: set[str] = set()
    for match in _LOCAL_MARKDOWN_LINK_RE.finditer(content):
        link = match.group(1).strip().strip("<>")
        if not link or link.startswith("#") or link.startswith("/"):
            continue
        if "://" in link or link.lower().startswith(("mailto:", "data:")):
            continue
        normalized = link.split("#", 1)[0].strip()
        if not normalized:
            continue
        top_level = normalized.split("/", 1)[0]
        if top_level in _SKILL_RESOURCE_DIRS:
            references.add(normalized)
    return sorted(references)


def _run_resource_check(workspace: Path, skill_file_path: Path, content: str) -> dict[str, list[str]]:
    referenced = _local_skill_resource_links(content)
    if not referenced:
        return {"referenced": [], "missing": []}

    skill_root = skill_file_path.parent if skill_file_path.name in {"SKILL.md", "skill.md"} else skill_file_path.parent
    missing: list[str] = []
    for resource_path in referenced:
        candidate = (workspace / skill_root / resource_path).resolve()
        root = (workspace / skill_root).resolve()
        try:
            candidate.relative_to(root)
        except ValueError:
            missing.append(resource_path)
            continue
        if not candidate.is_file():
            missing.append(resource_path)
    return {"referenced": referenced, "missing": missing}


@lru_cache(maxsize=1)
def _available_tool_names() -> frozenset[str]:
    from app.tools.collector import collect_tools

    return frozenset(
        tool["function"]["name"]
        for tool in collect_tools().openai_tools
        if isinstance(tool, dict) and isinstance(tool.get("function"), dict) and tool["function"].get("name")
    )


@lru_cache(maxsize=1)
def _available_pack_names() -> frozenset[str]:
    from app.tools.runtime_tool_groups import RUNTIME_TOOL_GROUPS

    return frozenset(pack.name for pack in RUNTIME_TOOL_GROUPS)


def _run_parse_smoke(content: str, *, skill_file_path: Path) -> tuple[Any | None, dict[str, Any]]:
    from app.skills.parser import SkillParser

    frontmatter, errors = _extract_frontmatter(content)
    for required_key in ("name", "description"):
        value = frontmatter.get(required_key)
        if not isinstance(value, str) or not value.strip():
            errors.append(required_key)

    parsed_skill = None
    try:
        parser = SkillParser()
        parsed_skill = parser.parse_content(
            content,
            path=skill_file_path,
            relative_path=skill_file_path.as_posix(),
            default_name=skill_file_path.parent.name,
        )
    except Exception as exc:  # pragma: no cover - defensive guard for malformed future parser changes.
        errors.append(f"parse_exception:{exc.__class__.__name__}")

    metadata = parsed_skill.metadata if parsed_skill is not None else None
    evidence = {
        "parsed": not errors and parsed_skill is not None,
        "name": metadata.name if metadata is not None else "",
        "description": metadata.description if metadata is not None else "",
        "declared_tools": list(metadata.declared_tools) if metadata is not None else [],
        "declared_packs": list(metadata.declared_packs) if metadata is not None else [],
        "errors": sorted(set(errors)),
    }
    return parsed_skill, evidence


def _run_tool_dry_run(parsed_skill: Any | None) -> dict[str, Any]:
    if parsed_skill is None:
        return {"declared_tools": [], "unknown_tools": [], "declared_packs": [], "unknown_packs": []}

    declared_tools = sorted(set(parsed_skill.metadata.declared_tools))
    declared_packs = sorted(set(parsed_skill.metadata.declared_packs))
    available_tools = _available_tool_names()
    available_packs = _available_pack_names()
    return {
        "declared_tools": declared_tools,
        "unknown_tools": [tool for tool in declared_tools if tool not in available_tools],
        "declared_packs": declared_packs,
        "unknown_packs": [pack for pack in declared_packs if pack not in available_packs],
    }


def _run_load_smoke(content: str, *, skill_file_path: Path) -> dict[str, Any]:
    from app.skills.loader import WorkspaceSkillLoader

    with tempfile.TemporaryDirectory(prefix="hive-skill-guard-") as temp_dir:
        temp_workspace = Path(temp_dir)
        target = temp_workspace / skill_file_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")

        try:
            loaded_skills = WorkspaceSkillLoader().load_from_workspace(temp_workspace)
        except Exception as exc:  # pragma: no cover - loader should not throw for invalid candidate text.
            return {
                "loaded": False,
                "skill_count": 0,
                "relative_path": skill_file_path.as_posix(),
                "errors": [f"load_exception:{exc.__class__.__name__}"],
            }

    loaded_paths = {skill.relative_path for skill in loaded_skills}
    loaded = skill_file_path.as_posix() in loaded_paths
    return {
        "loaded": loaded,
        "skill_count": len(loaded_skills),
        "relative_path": skill_file_path.as_posix(),
        "loaded_paths": sorted(loaded_paths),
        "errors": [] if loaded else ["skill_not_loaded"],
    }


def _run_skill_guard_check(workspace: Path, grader: dict[str, Any]) -> dict[str, Any]:
    content = grader.get("content")
    relative_path = str(grader.get("path") or "SKILL.md").strip() or "SKILL.md"
    if content is None:
        path = workspace / relative_path
        if not path.exists() or not path.is_file():
            return _check_result(
                check_type="skill_guard",
                passed=False,
                message="skill_guard requires content or an existing file path",
                evidence={"path": relative_path},
            )
        content = path.read_text(encoding="utf-8", errors="replace")

    from app.services.skill_guard import scan_skill_files

    content = str(content)
    frontmatter, _ = _extract_frontmatter(content)
    skill_name = str(frontmatter.get("name") or Path(relative_path).stem or "candidate-skill")
    skill_file_path = _normalize_skill_file_path(relative_path, skill_name=skill_name)
    parsed_skill, parse_smoke = _run_parse_smoke(content, skill_file_path=skill_file_path)
    load_smoke = _run_load_smoke(content, skill_file_path=skill_file_path)
    tool_dry_run = _run_tool_dry_run(parsed_skill)
    resource_check = _run_resource_check(workspace, skill_file_path, content)
    guard = scan_skill_files(
        [{"path": skill_file_path.as_posix(), "content": content}],
        source="evolution_verification",
    )
    passed = (
        guard.allowed
        and not guard.requires_review
        and parse_smoke["parsed"]
        and load_smoke["loaded"]
        and not tool_dry_run["unknown_tools"]
        and not tool_dry_run["unknown_packs"]
        and not resource_check["missing"]
    )
    return _check_result(
        check_type="skill_guard",
        passed=passed,
        message="skill guard passed" if passed else "skill guard blocked candidate",
        evidence={
            "guard": guard.to_dict(),
            "parse_smoke": parse_smoke,
            "load_smoke": load_smoke,
            "tool_dry_run": tool_dry_run,
            "resource_check": resource_check,
        },
    )


def _run_llm_rubric_check(grader: dict[str, Any]) -> dict[str, Any]:
    # E4/D3: an LLM rubric is an OBSERVATIONAL quality signal only — it never gates
    # pass/fail (round2 §2.3: self-eval drifts and reward-hacks). It contributes a
    # continuous score to verification_quality_score but is excluded from the hard
    # gate (gating=False), so a verification can never pass on a rubric alone.
    raw = grader.get("score")
    score: float | None = None
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        value = float(raw)
        score = max(0.0, min(1.0, value / 100.0 if value > 1.0 else value))
    return _check_result(
        check_type="llm_rubric",
        passed=True,
        message="llm_rubric is observational only (non-gating quality signal; never decides pass/fail)",
        evidence={"rubric": grader.get("rubric", ""), "raw_score": raw},
        gating=False,
        score=score,
    )


def _run_human_confirmation_check(grader: dict[str, Any]) -> dict[str, Any]:
    confirmed = bool(grader.get("confirmed"))
    return _check_result(
        check_type="human_confirmation",
        passed=confirmed,
        message="human confirmation present" if confirmed else "human confirmation missing",
        evidence={"reviewer": grader.get("reviewer", "")},
    )


def run_evolution_verification(
    *,
    workspace: Path,
    candidate: dict[str, Any],
    graders: list[dict[str, Any]],
) -> dict[str, Any]:
    checks: list[dict[str, Any]] = []
    for grader in graders:
        grader_type = str(grader.get("type") or "").strip()
        if grader_type == "deterministic_command":
            checks.append(_run_deterministic_command(workspace, grader))
        elif grader_type == "state_check":
            checks.append(_run_state_check(workspace, grader))
        elif grader_type == "tool_call_check":
            checks.append(_run_tool_call_check(grader))
        elif grader_type == "skill_guard":
            checks.append(_run_skill_guard_check(workspace, grader))
        elif grader_type == "llm_rubric":
            checks.append(_run_llm_rubric_check(grader))
        elif grader_type == "human_confirmation":
            checks.append(_run_human_confirmation_check(grader))
        else:
            checks.append(
                _check_result(
                    check_type=grader_type or "unknown",
                    passed=False,
                    message="unknown grader type",
                    evidence={"grader": grader},
                )
            )

    # E4/D3: only GATING checks decide pass/fail. Non-gating checks (llm_rubric)
    # are observational; a verification with no gating check fails closed.
    gating_checks = [check for check in checks if check.get("gating", True)]
    passed = bool(gating_checks) and all(bool(check.get("passed")) for check in gating_checks)
    report = {
        "schema": "evolution_verification_report.v1",
        "created_at": _now_iso(),
        "candidate_id": candidate.get("candidate_id"),
        "target_type": candidate.get("target_type"),
        "target_id": candidate.get("target_id"),
        "passed": passed,
        "checks": checks,
        "trace_refs": candidate.get("source_attempt_ids") or candidate.get("manifest", {}).get("trace_refs") or [],
    }
    report["quality_score"] = verification_quality_score(report)
    return report


def verification_quality_score(report: dict[str, Any]) -> float:
    """E4: continuous quality score (0-1) over all checks — rubric contributes its
    normalized score, hard checks contribute pass=1.0 / fail=0.0. Observational,
    NOT the gate (round2 §2.3 / D3)."""

    checks = report.get("checks") or []
    if not checks:
        return 0.0
    total = 0.0
    for check in checks:
        raw = check.get("score")
        if raw is not None:
            total += max(0.0, min(1.0, float(raw)))
        else:
            total += 1.0 if check.get("passed") else 0.0
    return round(total / len(checks), 4)


def record_verification_eval(
    workspace: Path,
    *,
    candidate: dict[str, Any],
    verification_report: dict[str, Any],
    dataset: str = "evolution_verification",
) -> dict[str, Any]:
    passed = bool(verification_report.get("passed"))
    # E4: gating checks that failed are the regression signal (rubric never counts).
    failed_checks = [
        check
        for check in verification_report.get("checks", [])
        if check.get("gating", True) and not bool(check.get("passed"))
    ]
    return record_eval_run(
        workspace,
        candidate_id=str(candidate.get("candidate_id") or ""),
        dataset=dataset,
        reward=verification_quality_score(verification_report),
        baseline_reward=0.0,
        passed=passed,
        traces=[str(ref) for ref in verification_report.get("trace_refs") or [] if str(ref).strip()],
        critical_regressions=len(failed_checks),
        metadata={"verification_report": verification_report},
    )


def execution_evidence(verification_report: dict[str, Any]) -> dict[str, bool]:
    """E3: summarize agent-behavior (execution) evidence inside a verification report."""

    checks = verification_report.get("checks") or []
    behavior_checks = [check for check in checks if check.get("type") == "agent_behavior_check"]
    return {
        "has_behavior_check": bool(behavior_checks),
        "execution_passed": bool(behavior_checks) and all(bool(check.get("passed")) for check in behavior_checks),
    }


def decide_verified_promotion(
    candidate: dict[str, Any],
    *,
    verification_report: dict[str, Any] | None,
    regression_report: dict[str, Any] | None = None,
) -> dict[str, str]:
    del candidate
    if verification_report is None:
        return {"decision": "hold", "reason": "verification evidence is required"}
    if not bool(verification_report.get("passed")):
        return {"decision": "reject", "reason": "verification failed"}
    # E3 no_regressions: a behavior regression vs the version-pinned baseline blocks promotion.
    if regression_report is not None and not bool(regression_report.get("passed")):
        regressed = (
            [r.get("scenario") for r in regression_report.get("regressions", []) if r.get("regressed")]
            or regression_report.get("missing_scenarios")
            or []
        )
        return {"decision": "hold", "reason": f"behavior regression vs baseline: {regressed}"}
    # E3 execution_passed: if behavior evidence is present it must pass. verification.passed
    # already requires this; we assert it explicitly so a caller that computes passed
    # differently cannot bypass the behavior gate.
    evidence = execution_evidence(verification_report)
    if evidence["has_behavior_check"] and not evidence["execution_passed"]:
        return {"decision": "reject", "reason": "agent behavior eval failed"}
    return {"decision": "promote", "reason": "verification passed"}


_ARTIFACT_GATE_REQUIRED_TARGET_TYPES = {
    "code",
    "script",
    "skill",
    "skill_patch",
    "skill_executable",
    "workflow",
    "workflow_definition",
}


def _candidate_requires_artifact_gate(candidate: dict[str, Any]) -> bool:
    target_type = str(candidate.get("target_type") or "").strip().lower()
    if target_type in _ARTIFACT_GATE_REQUIRED_TARGET_TYPES:
        return True
    metadata = candidate.get("metadata") if isinstance(candidate.get("metadata"), dict) else {}
    artifact_type = str(metadata.get("artifact_type") or "").strip().lower()
    return artifact_type in _ARTIFACT_GATE_REQUIRED_TARGET_TYPES or bool(metadata.get("requires_artifact_gate"))


def _artifact_gate_hard_gate(artifact_gate_report: dict[str, Any] | None) -> dict[str, str]:
    if not isinstance(artifact_gate_report, dict):
        return {"decision": "hold", "reason": "artifact execution gate report is required"}
    status = str(artifact_gate_report.get("status") or "").strip().lower()
    if status == "not_applicable":
        reason = str(artifact_gate_report.get("reason") or "").strip()
        if reason:
            return {"decision": "promote", "reason": f"artifact execution gate not applicable: {reason}"}
        return {"decision": "hold", "reason": "artifact execution gate not_applicable requires a reason"}
    if status == "passed" or bool(artifact_gate_report.get("passed")):
        return {"decision": "promote", "reason": "artifact execution gate passed"}
    reason = str(artifact_gate_report.get("reason") or artifact_gate_report.get("error") or "artifact gate failed")
    return {"decision": "hold", "reason": f"artifact execution gate failed: {reason}"}


def decide_provisional_promotion(
    candidate: dict[str, Any],
    *,
    verification_report: dict[str, Any] | None,
    regression_report: dict[str, Any] | None = None,
    artifact_gate_report: dict[str, Any] | None = None,
) -> dict[str, str]:
    """Hard floors passed -> provisional trial.

    The retired behavior-eval and synthetic baseline regression reports are no
    longer promotion prerequisites. Deterministic verification still fails
    closed, and executable artifacts still need the artifact execution gate.
    """

    del regression_report
    base = decide_verified_promotion(candidate, verification_report=verification_report)
    if base["decision"] != "promote":
        return base
    if _candidate_requires_artifact_gate(candidate):
        artifact_gate = _artifact_gate_hard_gate(artifact_gate_report)
        if artifact_gate["decision"] != "promote":
            return artifact_gate
    return {"decision": "provisional", "reason": "hard verification floors passed; enter provisional trial"}
