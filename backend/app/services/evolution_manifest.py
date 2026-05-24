"""Thin manifest contract for self-evolution candidates."""

from __future__ import annotations

from typing import Any


EVOLUTION_MANIFEST_SCHEMA = "hive_evolution_manifest.v1"
VALID_RISK_LEVELS = {"low", "medium", "high", "critical"}


def _string_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _require_non_empty_string(manifest: dict[str, Any], field: str, errors: list[str]) -> None:
    if not str(manifest.get(field) or "").strip():
        errors.append(f"{field} is required")


def validate_evolution_manifest(manifest: Any) -> list[str]:
    """Return validation errors for a `hive_evolution_manifest.v1` payload."""

    if not isinstance(manifest, dict):
        return ["manifest must be an object"]

    errors: list[str] = []
    if manifest.get("schema") != EVOLUTION_MANIFEST_SCHEMA:
        errors.append(f"schema must be {EVOLUTION_MANIFEST_SCHEMA}")

    for field in ("change_type", "target_type", "target_id"):
        _require_non_empty_string(manifest, field, errors)

    if not _string_list(manifest.get("source_refs")):
        errors.append("source_refs must contain at least one reference")
    if not _string_list(manifest.get("trace_refs")):
        errors.append("trace_refs must contain at least one reference")
    if not isinstance(manifest.get("eval_refs"), list):
        errors.append("eval_refs must be present as a list")

    rollback_plan = manifest.get("rollback_plan")
    if not isinstance(rollback_plan, dict) or not str(rollback_plan.get("strategy") or "").strip():
        errors.append("rollback_plan.strategy is required")

    risk_level = str(manifest.get("risk_level") or "").strip().lower()
    if risk_level not in VALID_RISK_LEVELS:
        errors.append("risk_level must be one of: low, medium, high, critical")

    return errors


def build_evolution_manifest(
    *,
    change_type: str,
    target_type: str,
    target_id: str,
    source_refs: list[str],
    trace_refs: list[str],
    eval_refs: list[str] | None = None,
    rollback_strategy: str = "restore previous baseline before applying candidate",
    risk_level: str = "medium",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Build the smallest reviewable manifest needed before fast reflection writes candidates."""

    normalized_source_refs = _string_list(source_refs)
    normalized_trace_refs = _string_list(trace_refs)
    normalized_eval_refs = _string_list(eval_refs or [])
    manifest: dict[str, Any] = {
        "schema": EVOLUTION_MANIFEST_SCHEMA,
        "change_type": change_type.strip().lower() or "unknown",
        "target_type": target_type.strip(),
        "target_id": target_id.strip(),
        "source_refs": normalized_source_refs,
        "trace_refs": normalized_trace_refs,
        "eval_refs": normalized_eval_refs,
        "rollback_plan": {"strategy": rollback_strategy.strip() or "restore previous baseline"},
        "risk_level": risk_level.strip().lower() or "medium",
    }
    if metadata:
        manifest["metadata"] = metadata
    errors = validate_evolution_manifest(manifest)
    if errors:
        raise ValueError("; ".join(errors))
    return manifest
