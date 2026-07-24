"""Pure contracts for Company source, evidence, review, and lifecycle state."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.services.privacy_layer import canonicalize_sensitivity


_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_MANAGED_CREDENTIAL_PREFIXES = ("secret://", "credential://", "vault://")
_INGEST_MODES = frozenset({"manual", "snapshot", "incremental", "cdc", "webhook", "reference"})
_EVIDENCE_KINDS = frozenset(
    {"document", "structured_record", "event", "living_object_revision", "external_immutable_ref"}
)
_SAFE_ARTIFACT_SUFFIXES = frozenset({".md", ".txt", ".json", ".bin"})
_MATERIALIZATION_OPERATIONS = frozenset({"agent_proposed_update", "personal_promotion", "legacy_import"})


def _jsonable(value: Any) -> Any:
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc).isoformat()
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_jsonable(item) for item in value]
    return value


def _canonical_json(value: Any) -> str:
    return json.dumps(_jsonable(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _require_hash(value: str, *, field: str) -> str:
    clean = str(value or "").strip().lower()
    if not _HASH_RE.fullmatch(clean):
        raise ValueError(f"{field} must be a 64-character lowercase SHA-256 digest")
    return clean


def _namespace(value: str) -> str:
    clean = str(value or "").strip().strip("/")
    if not clean or clean.startswith(".") or "//" in clean or any(part in {"", ".", ".."} for part in clean.split("/")):
        raise ValueError("allowed_namespaces contains an invalid namespace")
    return clean


@dataclass(frozen=True, slots=True)
class SourceContractInput:
    source_kind: str
    provider_kind: str
    stable_source_id: str
    owner_principal_ref: str
    accountable_steward_ref: str
    connection_ref: str | None
    schema_ref: str | None
    schema_version: str | None
    identity_keys: tuple[str, ...]
    relation_keys: tuple[str, ...]
    ingest_mode: str
    cursor_kind: str | None
    cursor_policy: dict[str, Any]
    watermark_field: str | None
    temporal_mapping: dict[str, Any]
    source_acl_mapping_policy: dict[str, Any]
    default_sensitivity: str
    export_policy: dict[str, Any]
    retention_policy: dict[str, Any]
    legal_hold_policy: dict[str, Any]
    allowed_namespaces: tuple[str, ...]
    precedence_policy_ref: str | None
    acceptance_suite_ref: str | None
    idempotency_policy: dict[str, Any]


def validate_source_contract(contract: SourceContractInput) -> SourceContractInput:
    required = {
        "source_kind": contract.source_kind,
        "provider_kind": contract.provider_kind,
        "stable_source_id": contract.stable_source_id,
        "owner_principal_ref": contract.owner_principal_ref,
        "accountable_steward_ref": contract.accountable_steward_ref,
    }
    missing = [name for name, value in required.items() if not str(value or "").strip()]
    if missing:
        raise ValueError(f"source contract fields are required: {', '.join(sorted(missing))}")
    if contract.ingest_mode not in _INGEST_MODES:
        raise ValueError(f"unsupported ingest_mode: {contract.ingest_mode}")
    canonicalize_sensitivity(contract.default_sensitivity)
    if contract.connection_ref and not str(contract.connection_ref).startswith(_MANAGED_CREDENTIAL_PREFIXES):
        raise ValueError("connection_ref must be a managed credential reference")
    if not contract.allowed_namespaces:
        raise ValueError("allowed_namespaces must contain at least one namespace")
    for namespace in contract.allowed_namespaces:
        _namespace(namespace)
    if not dict(contract.source_acl_mapping_policy or {}):
        raise ValueError("source_acl_mapping_policy is required")
    if not dict(contract.idempotency_policy or {}):
        raise ValueError("idempotency_policy is required")
    return contract


def compute_source_contract_hash(contract: SourceContractInput) -> str:
    validate_source_contract(contract)
    return _sha256(asdict(contract))


@dataclass(frozen=True, slots=True)
class CanonicalEvidenceInput:
    evidence_id: uuid.UUID
    tenant_id: uuid.UUID
    source_contract_id: uuid.UUID
    source_contract_version: int
    evidence_kind: str
    source_item_id: str
    source_revision: str
    artifact_ref: str | None
    schema_ref: str | None
    typed_payload_ref: str | None
    typed_payload: dict[str, Any] | None
    content_hash: str
    source_acl_snapshot_hash: str
    source_acl_snapshot: dict[str, Any]
    occurred_at: datetime | None
    effective_from: datetime | None
    effective_until: datetime | None
    observed_at: datetime
    cursor: dict[str, Any]
    sequence: str | None
    idempotency_key: str
    coverage_ledger_ref: str
    coverage_ledger: dict[str, Any]
    ingestion_receipt_ref: str


def build_canonical_evidence_envelope(evidence: CanonicalEvidenceInput) -> dict[str, Any]:
    if evidence.evidence_kind not in _EVIDENCE_KINDS:
        raise ValueError(f"unsupported evidence_kind: {evidence.evidence_kind}")
    if evidence.source_contract_version < 1:
        raise ValueError("source_contract_version must be positive")
    if not str(evidence.source_item_id or "").strip() or not str(evidence.idempotency_key or "").strip():
        raise ValueError("source_item_id and idempotency_key are required")
    _require_hash(evidence.content_hash, field="content_hash")
    if not evidence.source_acl_snapshot_hash:
        raise ValueError("source ACL snapshot hash is required")
    _require_hash(evidence.source_acl_snapshot_hash, field="source_acl_snapshot_hash")
    if not dict(evidence.source_acl_snapshot or {}):
        raise ValueError("source ACL snapshot is required")
    coverage = dict(evidence.coverage_ledger or {})
    if coverage.get("complete") is not True:
        raise ValueError("complete coverage ledger is required")
    total_units = coverage.get("total_units")
    covered_units = coverage.get("covered_units")
    if (
        not isinstance(total_units, int)
        or not isinstance(covered_units, int)
        or total_units < 1
        or covered_units != total_units
        or list(coverage.get("missing_units") or [])
    ):
        raise ValueError("complete coverage ledger is required")
    if not str(evidence.coverage_ledger_ref or "").strip() or not str(evidence.ingestion_receipt_ref or "").strip():
        raise ValueError("coverage and ingestion receipt references are required")
    if evidence.evidence_kind == "document" and not evidence.artifact_ref:
        raise ValueError("document evidence requires an immutable artifact_ref")
    if evidence.evidence_kind in {"structured_record", "event"} and (
        not evidence.schema_ref or (evidence.typed_payload is None and not evidence.typed_payload_ref)
    ):
        raise ValueError("typed evidence requires schema and lossless payload")
    if evidence.evidence_kind in {"living_object_revision", "external_immutable_ref"} and not evidence.artifact_ref:
        raise ValueError("reference evidence requires an immutable artifact_ref")
    return {
        "schema": "hive.company_knowledge_evidence.v1",
        "evidence_id": str(evidence.evidence_id),
        "tenant_id": str(evidence.tenant_id),
        "source_contract_id": str(evidence.source_contract_id),
        "source_contract_version": evidence.source_contract_version,
        "evidence_kind": evidence.evidence_kind,
        "source_item_id": evidence.source_item_id,
        "source_revision": evidence.source_revision,
        "artifact_ref": evidence.artifact_ref,
        "schema_ref": evidence.schema_ref,
        "typed_payload_ref": evidence.typed_payload_ref,
        "typed_payload": _jsonable(evidence.typed_payload),
        "content_hash": evidence.content_hash,
        "source_acl_snapshot_hash": evidence.source_acl_snapshot_hash,
        "source_acl_snapshot": _jsonable(evidence.source_acl_snapshot),
        "occurred_at": _jsonable(evidence.occurred_at),
        "effective_from": _jsonable(evidence.effective_from),
        "effective_until": _jsonable(evidence.effective_until),
        "observed_at": _jsonable(evidence.observed_at),
        "cursor": _jsonable(evidence.cursor),
        "sequence": evidence.sequence,
        "idempotency_key": evidence.idempotency_key,
        "coverage_ledger_ref": evidence.coverage_ledger_ref,
        "coverage_ledger": _jsonable(coverage),
        "ingestion_receipt_ref": evidence.ingestion_receipt_ref,
    }


def company_knowledge_artifact_path(
    data_root: str | Path,
    *,
    tenant_id: uuid.UUID,
    evidence_id: uuid.UUID,
    content_hash: str,
    suffix: str,
) -> Path:
    digest = _require_hash(content_hash, field="content_hash")
    clean_suffix = str(suffix or "").lower()
    if clean_suffix not in _SAFE_ARTIFACT_SUFFIXES:
        raise ValueError("artifact suffix is not allowed")
    root = Path(data_root).expanduser().resolve()
    target = (
        root
        / "companies"
        / str(tenant_id)
        / "knowledge"
        / "evidence"
        / digest[:2]
        / f"{evidence_id}-{digest}{clean_suffix}"
    ).resolve(strict=False)
    target.relative_to(root)
    return target


_PROPOSAL_TRANSITIONS: dict[tuple[str, str], str] = {
    ("draft", "submit"): "submitted",
    ("draft", "withdraw"): "withdrawn",
    ("submitted", "begin_review"): "in_review",
    ("submitted", "withdraw"): "withdrawn",
    ("in_review", "request_changes"): "changes_requested",
    ("in_review", "approve"): "approved",
    ("in_review", "reject"): "rejected",
    ("changes_requested", "submit"): "submitted",
    ("approved", "begin_publish"): "publishing",
    ("publishing", "publish_succeeded"): "published",
    ("publishing", "publish_failed"): "publish_failed",
    ("publish_failed", "begin_publish"): "publishing",
}


def next_company_proposal_status(current: str, command: str) -> str:
    try:
        return _PROPOSAL_TRANSITIONS[(str(current), str(command))]
    except KeyError as exc:
        raise ValueError(f"invalid proposal transition: {current} -> {command}") from exc


def company_knowledge_proposal_requires_materialization(
    *,
    proposal_kind: str,
    proposed_patch: dict[str, Any],
) -> bool:
    operation = str(dict(proposed_patch or {}).get("operation") or "")
    return operation in _MATERIALIZATION_OPERATIONS or proposal_kind in {
        "personal_promotion",
        "legacy_import",
    }


def default_company_knowledge_review_policy(
    *,
    proposed_sensitivity: str,
    risk_level: str,
    created_by_type: str,
) -> dict[str, Any]:
    sensitivity = canonicalize_sensitivity(proposed_sensitivity).value
    if risk_level not in {"normal", "high", "critical"}:
        raise ValueError("unsupported_company_knowledge_risk_level")
    heightened = sensitivity in {"PL3_sensitive", "PL4_credential"} or risk_level in {
        "high",
        "critical",
    }
    return {
        "minimum_approvals": 2 if heightened else 1,
        "required_roles": ["org_admin"],
        "separation": heightened or created_by_type == "agent",
        "source": "server_policy_v1",
    }


def evaluate_company_review_set(
    reviews: list[dict[str, Any]],
    *,
    policy: dict[str, Any],
    created_by_type: str,
    created_by_id: uuid.UUID,
    risk_level: str,
) -> dict[str, Any]:
    approvals = [review for review in reviews if review.get("decision") == "approve"]
    reason_codes: list[str] = []
    if any(str(review.get("reviewer_role") or "") == "agent" for review in reviews):
        reason_codes.append("agent_cannot_review_or_approve")
    if any(review.get("decision") == "reject" for review in reviews):
        reason_codes.append("review_rejected")
    minimum_approvals = max(1, int(policy.get("minimum_approvals") or 1))
    if len(approvals) < minimum_approvals:
        reason_codes.append("minimum_approvals_not_met")
    required_roles = {str(value) for value in policy.get("required_roles", [])}
    approval_roles = {str(review.get("reviewer_role") or "") for review in approvals}
    if not required_roles.issubset(approval_roles):
        reason_codes.append("required_review_roles_missing")
    reviewer_ids = [str(review.get("reviewer_user_id") or "") for review in approvals]
    if (bool(policy.get("separation")) or risk_level in {"high", "critical"}) and len(set(reviewer_ids)) < len(
        reviewer_ids
    ):
        reason_codes.append("reviewer_separation_required")
    if (bool(policy.get("separation")) or risk_level in {"high", "critical"}) and str(created_by_id) in reviewer_ids:
        reason_codes.append("creator_reviewer_separation_required")
    if created_by_type == "agent" and any(str(review.get("reviewer_role") or "") == "agent" for review in approvals):
        if "agent_cannot_review_or_approve" not in reason_codes:
            reason_codes.append("agent_cannot_review_or_approve")
    reason_codes = list(dict.fromkeys(reason_codes))
    if reason_codes:
        return {"approved": False, "reason_codes": reason_codes, "review_set_hash": None}
    review_set = sorted(
        (
            {
                "reviewer_user_id": str(review["reviewer_user_id"]),
                "reviewer_role": str(review["reviewer_role"]),
                "decision": str(review["decision"]),
                "decision_hash": str(review["decision_hash"]),
            }
            for review in approvals
        ),
        key=lambda item: (item["reviewer_role"], item["reviewer_user_id"], item["decision_hash"]),
    )
    return {
        "approved": True,
        "reason_codes": [],
        "review_set_hash": _sha256(review_set),
    }


__all__ = [
    "CanonicalEvidenceInput",
    "SourceContractInput",
    "build_canonical_evidence_envelope",
    "company_knowledge_artifact_path",
    "company_knowledge_proposal_requires_materialization",
    "compute_source_contract_hash",
    "default_company_knowledge_review_policy",
    "evaluate_company_review_set",
    "next_company_proposal_status",
    "validate_source_contract",
]
