#!/usr/bin/env python3
"""Mechanical Weekend RC manifest and production-evidence gate.

This tool validates exact structure and release facts. It deliberately does not
decide whether an Agent answer, UI, workflow, or artifact is semantically good.
Those verdicts remain with the accountable verifier who authors the evidence.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_MANIFEST = ROOT / "acceptance" / "weekend_production_journeys.v1.json"
DEFAULT_EVIDENCE_ROOT = ROOT / "docs" / "acceptance" / "2026-08-30-weekend-rc" / "evidence"
DOCUMENT_GROUP = ROOT / "docs" / "acceptance" / "2026-08-30-weekend-rc"

EXPECTED_CANDIDATES = [f"PJ-{index:02d}" for index in range(1, 36)]
EXPECTED_COMMAND_IDS = [f"P03-CMD{index:02d}" for index in range(1, 21)]
EXPECTED_PERSONAL_FORMAT_IDS = {"P10-PDF", "P10-DOCX", "P10-MD", "P10-TXT"}
EXPECTED_ARTIFACT_FORMAT_IDS = {"P30-PDF", "P30-DOCX", "P30-MD", "P30-TXT"}
EXPECTED_MODEL_LABELS = {"minimax", "glm", "deepseek"}
EXPECTED_REQUIRED_EVIDENCE = {
    "signed_in_pass_1",
    "signed_in_pass_2",
    "fault_recovery",
    "negative_authority",
    "cleanup_record",
}
EXPECTED_COVERAGE_WEIGHTS = {
    "live_wiring": 15,
    "automation_real_postgres": 20,
    "signed_in_double_pass": 30,
    "fault_recovery": 20,
    "negative_authority": 15,
}
REQUIRED_RESOLVED_FIELDS = {
    "id",
    "name",
    "candidate_id",
    "proof_order",
    "domain",
    "persona",
    "principal",
    "entry",
    "input",
    "data_version",
    "allowed_effects",
    "acceptance",
    "negative_authority",
    "fault_recovery",
    "expected_artifacts",
    "measurements",
    "evidence_path_template",
    "required_evidence",
    "cleanup",
    "scope_status",
}
PROHIBITED_SECRET_KEYS = {
    "password",
    "api_key",
    "access_token",
    "refresh_token",
    "client_secret",
    "bridge_token",
}
PASS = "PASS"


def load_manifest(path: Path = DEFAULT_MANIFEST) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8"))


def manifest_sha256(path: Path = DEFAULT_MANIFEST) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _list(value: object) -> list[Any]:
    return list(value) if isinstance(value, list) else []


def expand_manifest(manifest: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Resolve defaults/profile/family inheritance into scoreable journeys."""

    defaults = dict(manifest.get("defaults") or {})
    profiles = dict(manifest.get("profiles") or {})
    resolved: list[dict[str, Any]] = []

    for family in _list(manifest.get("journey_families")):
        if not isinstance(family, dict):
            continue
        profile = dict(profiles.get(family.get("profile")) or {})
        common = dict(family.get("common") or {})
        for variant in _list(family.get("variants")):
            if not isinstance(variant, dict):
                continue
            item: dict[str, Any] = {
                **defaults,
                **profile,
                **common,
                **{key: value for key, value in variant.items() if not key.endswith("_additions")},
                "candidate_id": family.get("candidate_id"),
                "proof_order": family.get("proof_order"),
                "domain": family.get("domain"),
            }
            for key in (
                "acceptance",
                "negative_authority",
                "fault_recovery",
                "expected_artifacts",
                "measurements",
                "allowed_effects",
                "cleanup",
            ):
                item[key] = [
                    *_list(defaults.get(key)),
                    *_list(profile.get(key)),
                    *_list(common.get(key)),
                    *_list(variant.get(f"{key}_additions")),
                ]
            resolved.append(item)
    return resolved


def _walk_keys(value: object) -> Iterable[str]:
    if isinstance(value, dict):
        for key, child in value.items():
            yield str(key)
            yield from _walk_keys(child)
    elif isinstance(value, list):
        for child in value:
            yield from _walk_keys(child)


def validate_manifest(manifest: Mapping[str, Any], *, manifest_path: Path = DEFAULT_MANIFEST) -> list[str]:
    errors: list[str] = []
    if manifest.get("schema") != "hive.weekend_production_journeys.v1":
        errors.append("schema must be hive.weekend_production_journeys.v1")
    if manifest.get("version") != 1:
        errors.append("version must be 1")
    if manifest.get("status") != "frozen":
        errors.append("status must be frozen before production scoring")
    if manifest.get("external_fakes_allowed") is not False:
        errors.append("production manifest must set external_fakes_allowed=false")
    if not re.fullmatch(r"[0-9a-f]{40}", str(manifest.get("freeze_basis_commit") or "")):
        errors.append("freeze_basis_commit must be a full Git SHA")

    scoring = dict(manifest.get("scoring") or {})
    weights = scoring.get("evidence_weights")
    if weights != EXPECTED_COVERAGE_WEIGHTS:
        errors.append(f"evidence_weights must equal {EXPECTED_COVERAGE_WEIGHTS}")
    if sum((weights or {}).values()) != 100:
        errors.append("evidence weights must total 100")
    if len(_list(scoring.get("required_guardrails"))) != 5:
        errors.append("exactly five non-averagable guardrails are required")
    if scoring.get("blocked_precondition_counts_as_failure") is not True:
        errors.append("BLOCKED_PRECONDITION must remain a denominator failure")

    families = _list(manifest.get("journey_families"))
    candidate_ids = [family.get("candidate_id") for family in families if isinstance(family, dict)]
    if candidate_ids != EXPECTED_CANDIDATES:
        errors.append("journey families must preserve PJ-01 through PJ-35 exactly once and in order")

    profiles = dict(manifest.get("profiles") or {})
    for family in families:
        if not isinstance(family, dict):
            errors.append("journey family must be an object")
            continue
        if family.get("profile") not in profiles:
            errors.append(f"{family.get('candidate_id')}: unknown profile {family.get('profile')!r}")
        if not _list(family.get("variants")):
            errors.append(f"{family.get('candidate_id')}: variants must not be empty")
        domain = DOCUMENT_GROUP / str(family.get("domain") or "")
        if not domain.is_file():
            errors.append(f"{family.get('candidate_id')}: domain document does not exist: {domain}")

    journeys = expand_manifest(manifest)
    declared = manifest.get("frozen_denominator")
    if declared != len(journeys):
        errors.append(f"frozen_denominator={declared!r} but expanded journey count is {len(journeys)}")

    ids = [str(journey.get("id") or "") for journey in journeys]
    if len(ids) != len(set(ids)):
        errors.append("expanded journey IDs must be unique")
    for journey in journeys:
        journey_id = str(journey.get("id") or "<missing>")
        if not re.fullmatch(r"P\d{2}(?:-[A-Z0-9]+)+", journey_id):
            errors.append(f"{journey_id}: invalid production journey ID")
        missing = REQUIRED_RESOLVED_FIELDS - journey.keys()
        if missing:
            errors.append(f"{journey_id}: missing resolved fields {sorted(missing)}")
        for key in (
            "allowed_effects",
            "acceptance",
            "negative_authority",
            "fault_recovery",
            "expected_artifacts",
            "measurements",
            "required_evidence",
            "cleanup",
        ):
            if not _list(journey.get(key)):
                errors.append(f"{journey_id}: {key} must be a non-empty list")
        if set(_list(journey.get("required_evidence"))) != EXPECTED_REQUIRED_EVIDENCE:
            errors.append(f"{journey_id}: required_evidence must match the frozen closure contract")
        if journey.get("scope_status") == "excluded" and not journey.get("exclusion_reason"):
            errors.append(f"{journey_id}: excluded journey requires an owner reason")
        evidence_template = str(journey.get("evidence_path_template") or "")
        for placeholder in ("{deployed_commit}", "{journey_id}", "{pass}"):
            if placeholder not in evidence_template:
                errors.append(f"{journey_id}: evidence_path_template lacks {placeholder}")

    if [journey_id for journey_id in ids if journey_id.startswith("P03-CMD")] != EXPECTED_COMMAND_IDS:
        errors.append("PJ-03 must expand to CMD-01 through CMD-20 exactly once")
    if {
        journey_id for journey_id in ids if journey_id.startswith("P10-")
    } & EXPECTED_PERSONAL_FORMAT_IDS != EXPECTED_PERSONAL_FORMAT_IDS:
        errors.append("PJ-10 must include PDF, DOCX, Markdown and TXT")
    if {journey_id for journey_id in ids if journey_id.startswith("P30-")} != EXPECTED_ARTIFACT_FORMAT_IDS:
        errors.append("PJ-30 must contain exactly PDF, DOCX, Markdown and TXT artifact variants")
    model_labels = {str(journey.get("model_label")) for journey in journeys if journey.get("candidate_id") == "PJ-33"}
    if model_labels != EXPECTED_MODEL_LABELS:
        errors.append(f"PJ-33 model labels must equal {sorted(EXPECTED_MODEL_LABELS)}")

    prohibited = sorted({key for key in _walk_keys(manifest) if key.casefold() in PROHIBITED_SECRET_KEYS})
    if prohibited:
        errors.append(f"manifest contains prohibited credential-bearing keys: {prohibited}")
    if manifest_path.name != "weekend_production_journeys.v1.json":
        errors.append("production manifest filename is not canonical")
    return errors


def read_frontmatter(path: Path) -> dict[str, str]:
    lines = path.read_text(encoding="utf-8").splitlines()
    if not lines or lines[0] != "---":
        raise ValueError(f"{path} has no YAML frontmatter")
    try:
        end = lines.index("---", 1)
    except ValueError as exc:
        raise ValueError(f"{path} has unterminated YAML frontmatter") from exc
    result: dict[str, str] = {}
    for line in lines[1:end]:
        key, separator, value = line.partition(":")
        if separator and key.strip() and value.strip():
            result[key.strip()] = value.strip()
    return result


def _pass_evidence_errors(
    *,
    path: Path,
    journey_id: str,
    pass_number: int,
    deployed_commit: str,
    digest: str,
) -> list[str]:
    if not path.is_file():
        return [f"missing {path}"]
    try:
        metadata = read_frontmatter(path)
    except ValueError as exc:
        return [str(exc)]
    expected = {
        "journey_id": journey_id,
        "pass": str(pass_number),
        "environment": "production",
        "source_commit": deployed_commit,
        "deployed_commit": deployed_commit,
        "manifest_sha256": digest,
        "result": PASS,
    }
    errors = [f"{path}: {key} must be {value!r}" for key, value in expected.items() if metadata.get(key) != value]
    if pass_number == 2:
        for key in ("fault_recovery_result", "negative_authority_result", "cleanup_result"):
            if metadata.get(key) != PASS:
                errors.append(f"{path}: {key} must be PASS")
    return errors


def _release_gate_result(path: Path, deployed_commit: str, digest: str) -> tuple[dict[str, str], list[str]]:
    if not path.is_file():
        return {}, [f"missing {path}"]
    try:
        metadata = read_frontmatter(path)
    except ValueError as exc:
        return {}, [str(exc)]
    required = {
        "result": PASS,
        "source_commit": deployed_commit,
        "deployed_commit": deployed_commit,
        "manifest_sha256": digest,
        "backend_commit": deployed_commit,
        "backend_api_commit": deployed_commit,
        "frontend_commit": deployed_commit,
        "backend_status": "SUCCESS",
        "backend_api_status": "SUCCESS",
        "frontend_status": "SUCCESS",
        "zero_known_defects": PASS,
        "cleanup_result": PASS,
    }
    errors = [f"{path}: {key} must be {value!r}" for key, value in required.items() if metadata.get(key) != value]
    for guardrail in (
        "guardrail_single_agent",
        "guardrail_growth",
        "guardrail_benchmark",
        "guardrail_governance",
        "guardrail_experience",
    ):
        if metadata.get(guardrail) != PASS:
            errors.append(f"{path}: {guardrail} must be PASS")
    for coverage in EXPECTED_COVERAGE_WEIGHTS:
        if metadata.get(f"coverage_{coverage}") != PASS:
            errors.append(f"{path}: coverage_{coverage} must be PASS")
    return metadata, errors


def score_evidence(
    manifest: Mapping[str, Any],
    *,
    manifest_path: Path,
    evidence_root: Path,
    deployed_commit: str,
) -> dict[str, Any]:
    errors = validate_manifest(manifest, manifest_path=manifest_path)
    if not re.fullmatch(r"[0-9a-f]{40}", deployed_commit):
        errors.append("deployed_commit must be a full Git SHA")
    digest = manifest_sha256(manifest_path)
    release_dir = evidence_root / deployed_commit
    journeys = [journey for journey in expand_manifest(manifest) if journey.get("scope_status") != "excluded"]
    closed: list[str] = []
    journey_errors: dict[str, list[str]] = {}
    for journey in journeys:
        journey_id = str(journey["id"])
        current_errors: list[str] = []
        for pass_number in (1, 2):
            current_errors.extend(
                _pass_evidence_errors(
                    path=release_dir / f"{journey_id}-pass-{pass_number}.md",
                    journey_id=journey_id,
                    pass_number=pass_number,
                    deployed_commit=deployed_commit,
                    digest=digest,
                )
            )
        if current_errors:
            journey_errors[journey_id] = current_errors
        else:
            closed.append(journey_id)

    release_metadata, release_errors = _release_gate_result(release_dir / "release-gates.md", deployed_commit, digest)
    errors.extend(release_errors)
    denominator = len(journeys)
    nptcr = (len(closed) / denominator * 100) if denominator else 0.0
    coverage = sum(
        weight for key, weight in EXPECTED_COVERAGE_WEIGHTS.items() if release_metadata.get(f"coverage_{key}") == PASS
    )
    guardrails_pass = all(
        release_metadata.get(key) == PASS
        for key in (
            "guardrail_single_agent",
            "guardrail_growth",
            "guardrail_benchmark",
            "guardrail_governance",
            "guardrail_experience",
        )
    )
    mechanical_ready = (
        not errors
        and not journey_errors
        and nptcr == 100.0
        and coverage >= int((manifest.get("scoring") or {}).get("required_evidence_coverage_percent") or 95)
        and guardrails_pass
    )
    return {
        "schema": "hive.weekend_rc_mechanical_score.v1",
        "mechanical_ready": mechanical_ready,
        "semantic_verdict": "not_computed_by_tool",
        "manifest_sha256": digest,
        "deployed_commit": deployed_commit,
        "denominator": denominator,
        "closed": len(closed),
        "nptcr_percent": round(nptcr, 4),
        "evidence_coverage_percent": coverage,
        "guardrails_pass": guardrails_pass,
        "manifest_or_release_errors": errors,
        "journey_errors": journey_errors,
    }


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("validate", help="validate the frozen production manifest")
    score = subparsers.add_parser("score", help="mechanically score immutable production evidence")
    score.add_argument("--evidence-root", type=Path, default=DEFAULT_EVIDENCE_ROOT)
    score.add_argument("--deployed-commit", required=True)
    return parser


def main() -> int:
    args = _parser().parse_args()
    manifest = load_manifest(args.manifest)
    if args.command == "validate":
        errors = validate_manifest(manifest, manifest_path=args.manifest)
        output = {
            "schema": "hive.weekend_rc_manifest_validation.v1",
            "valid": not errors,
            "semantic_verdict": "not_computed_by_tool",
            "manifest_sha256": manifest_sha256(args.manifest),
            "denominator": len(expand_manifest(manifest)),
            "errors": errors,
        }
        print(json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True))
        return 0 if not errors else 1
    result = score_evidence(
        manifest,
        manifest_path=args.manifest,
        evidence_root=args.evidence_root,
        deployed_commit=args.deployed_commit,
    )
    print(json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True))
    return 0 if result["mechanical_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
