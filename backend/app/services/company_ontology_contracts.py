"""Pure Company Ontology package and model-authored candidate contracts.

Domain Packs are signed declarative data. They can describe types, mappings,
typed rules, named queries, governed action declarations, acceptance cases,
and migrations; they cannot execute code or acquire Hive authority.
"""

from __future__ import annotations

import base64
import hashlib
from importlib import resources
from importlib.resources.abc import Traversable
import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

from nacl.exceptions import BadSignatureError
from nacl.signing import VerifyKey
from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.services.privacy_layer import canonicalize_sensitivity


PACKAGE_CONTRACT_VERSION = "hive.company_ontology_package.v1"
CANDIDATE_CONTRACT_VERSION = "hive.company_ontology_candidate.v1"
BUILTIN_SIGNATURE_KEY_REF = "hive_builtin_ontology_2026_07"

# Public verification material only. The authoring private key is not shipped.
_TRUSTED_ED25519_KEYS = {
    BUILTIN_SIGNATURE_KEY_REF: "RtFn3zs8Qt7SllXdfjSJ9OkNgDgl6bVWIShkDZO/KOM=",
}
_SUPPORTED_ENGINE_CAPABILITIES = frozenset(
    {
        "validate_package",
        "validate_candidate",
        "materialize_release_projection",
        "query",
        "resolve_fact_lineage",
        "simulate_action",
        "rebuild_projection",
    }
)
_FORBIDDEN_DECLARATIVE_KEYS = frozenset(
    {
        "python_import",
        "python_module",
        "shell",
        "shell_command",
        "command",
        "executable",
        "entrypoint",
        "webhook",
        "database_url",
        "credential",
        "secret",
    }
)
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_REF_RE = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[A-Za-z][A-Za-z0-9_-]*)+$")
_VALUE_TYPES = frozenset({"string", "integer", "number", "boolean", "datetime", "uuid", "json", "object_ref"})
_EVALUATION_MODES = frozenset(
    {"deterministic_typed", "llm_semantic_candidate", "human_decision", "external_authoritative_result"}
)


class OntologyPackageRejected(ValueError):
    """A package is not admissible as declarative ontology input."""


class OntologyCandidateRejected(ValueError):
    """A model-authored candidate cannot enter review/release yet."""


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class OntologyPackageSignature(_StrictModel):
    algorithm: Literal["ed25519"]
    key_ref: str = Field(min_length=1, max_length=500)
    value: str = Field(min_length=1, max_length=1000)


class OntologyPackageDependency(_StrictModel):
    package_key: str = Field(min_length=1, max_length=240)
    version: str = Field(min_length=1, max_length=120)


class OntologyPackageManifest(_StrictModel):
    package_key: str = Field(min_length=1, max_length=240)
    display_name: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=4000)
    publisher: str = Field(min_length=1, max_length=300)
    version: str = Field(min_length=1, max_length=120)
    hive_contract_version: str = Field(min_length=1, max_length=120)
    engine_capabilities: list[str] = Field(min_length=1, max_length=30)
    namespaces: list[str] = Field(min_length=1, max_length=50)
    dependencies: list[OntologyPackageDependency] = Field(default_factory=list, max_length=50)
    conflicts: list[OntologyPackageDependency] = Field(default_factory=list, max_length=50)

    @field_validator("engine_capabilities", "namespaces")
    @classmethod
    def _unique_strings(cls, value: list[str]) -> list[str]:
        cleaned = [str(item).strip() for item in value]
        if not all(cleaned) or len(set(cleaned)) != len(cleaned):
            raise ValueError("values must be unique non-empty strings")
        return cleaned


class OntologyObjectTypeDefinition(_StrictModel):
    type_ref: str
    display_name: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=4000)
    required_property_refs: list[str] = Field(default_factory=list)
    source_refs: list[str] = Field(default_factory=list)
    sensitivity: str = "PL1_public"
    permission_resource_ref: str


class OntologyPropertyTypeDefinition(_StrictModel):
    property_ref: str
    display_name: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=4000)
    value_type: str
    object_type_refs: list[str] = Field(min_length=1)
    cardinality: Literal["one", "many"] = "one"
    required: bool = False
    source_refs: list[str] = Field(default_factory=list)
    sensitivity: str = "PL1_public"
    permission_resource_ref: str


class OntologyLinkTypeDefinition(_StrictModel):
    link_type_ref: str
    display_name: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=4000)
    from_type_refs: list[str] = Field(min_length=1)
    to_type_refs: list[str] = Field(min_length=1)
    properties_schema: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[str] = Field(default_factory=list)
    sensitivity: str = "PL1_public"
    permission_resource_ref: str


class OntologyEventTypeDefinition(_StrictModel):
    event_type_ref: str
    display_name: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=4000)
    subject_type_refs: list[str] = Field(default_factory=list)
    payload_schema: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[str] = Field(default_factory=list)
    sensitivity: str = "PL1_public"
    permission_resource_ref: str


class OntologyRuleDefinitionContract(_StrictModel):
    rule_ref: str
    rule_kind: str = Field(min_length=1, max_length=80)
    owner_principal_ref: str = Field(min_length=1, max_length=500)
    version: int = Field(ge=1)
    scope: dict[str, Any]
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    valid_from: datetime
    valid_until: datetime | None = None
    source_refs: list[str] = Field(min_length=1)
    examples: list[dict[str, Any]] = Field(default_factory=list)
    counterexamples: list[dict[str, Any]] = Field(default_factory=list)
    risk: Literal["low", "normal", "high", "critical"]
    review_policy: dict[str, Any]
    conflict_precedence: dict[str, Any] = Field(default_factory=dict)
    evaluation_mode: str
    acceptance_refs: list[str] = Field(min_length=1)
    sensitivity: str = "PL1_public"
    permission_resource_ref: str


class OntologyNamedQueryDefinition(_StrictModel):
    query_ref: str
    display_name: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=4000)
    object_type_refs: list[str] = Field(default_factory=list)
    predicate_refs: list[str] = Field(default_factory=list)
    input_schema: dict[str, Any] = Field(default_factory=dict)
    result_schema: dict[str, Any]
    max_items: int = Field(default=50, ge=1, le=200)


class OntologyActionDefinition(_StrictModel):
    action_type_ref: str
    display_name: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, max_length=4000)
    input_schema: dict[str, Any]
    output_schema: dict[str, Any]
    required_capability: str = Field(min_length=1, max_length=300)
    tool_workflow_mapping: dict[str, Any]
    approval_policy: dict[str, Any]
    side_effect_classification: str = Field(min_length=1, max_length=80)
    simulation_contract: dict[str, Any]
    source_refs: list[str] = Field(default_factory=list)
    sensitivity: str = "PL1_public"
    permission_resource_ref: str


class OntologySchemaBundle(_StrictModel):
    object_types: list[OntologyObjectTypeDefinition] = Field(default_factory=list)
    property_types: list[OntologyPropertyTypeDefinition] = Field(default_factory=list)
    link_types: list[OntologyLinkTypeDefinition] = Field(default_factory=list)
    event_types: list[OntologyEventTypeDefinition] = Field(default_factory=list)


class OntologyGoldenQuery(_StrictModel):
    case_ref: str
    query_ref: str
    input: dict[str, Any] = Field(default_factory=dict)
    expected_contract: dict[str, Any]


class OntologyGoldenAction(_StrictModel):
    case_ref: str
    action_type_ref: str
    input: dict[str, Any]
    expected_contract: dict[str, Any]


class OntologyAclCase(_StrictModel):
    case_ref: str
    principal_ref: str
    resource_ref: str
    action: str
    expected: Literal["allow", "deny"]


class OntologyAcceptanceContract(_StrictModel):
    golden_queries: list[OntologyGoldenQuery] = Field(min_length=1)
    golden_actions: list[OntologyGoldenAction] = Field(min_length=1)
    acl_cases: list[OntologyAclCase] = Field(min_length=1)
    conflict_cases: list[dict[str, Any]] = Field(min_length=1)
    temporal_cases: list[dict[str, Any]] = Field(min_length=1)


class OntologyMigrationContract(_StrictModel):
    from_versions: list[str] = Field(default_factory=list)
    upgrade_steps: list[dict[str, Any]] = Field(default_factory=list)
    downgrade_steps: list[dict[str, Any]] = Field(default_factory=list)
    backfill_plan: dict[str, Any] = Field(default_factory=dict)
    rollback_compatible: bool


class OntologyPackagePayload(_StrictModel):
    signature: OntologyPackageSignature
    manifest: OntologyPackageManifest
    ontology_schema: OntologySchemaBundle = Field(alias="schema")
    mappings: dict[str, Any] = Field(default_factory=dict)
    rules: list[OntologyRuleDefinitionContract] = Field(default_factory=list)
    queries: list[OntologyNamedQueryDefinition] = Field(default_factory=list)
    actions: list[OntologyActionDefinition] = Field(default_factory=list)
    permissions: dict[str, Any]
    acceptance: OntologyAcceptanceContract
    migrations: OntologyMigrationContract


@dataclass(frozen=True, slots=True)
class OntologyPackageBundle:
    signature: OntologyPackageSignature
    manifest: OntologyPackageManifest
    schema: OntologySchemaBundle
    mappings: dict[str, Any]
    rules: tuple[OntologyRuleDefinitionContract, ...]
    queries: tuple[OntologyNamedQueryDefinition, ...]
    actions: tuple[OntologyActionDefinition, ...]
    permissions: dict[str, Any]
    acceptance: OntologyAcceptanceContract
    migrations: OntologyMigrationContract
    content_hash: str
    verification_receipt: dict[str, Any]
    raw_payload: dict[str, Any]
    source_path: Path | None = None


class OntologyPackageCatalog:
    def __init__(self, bundles: list[OntologyPackageBundle]) -> None:
        self._bundles = {(bundle.manifest.package_key, bundle.manifest.version): bundle for bundle in bundles}

    @property
    def package_keys(self) -> tuple[str, ...]:
        return tuple(sorted({package_key for package_key, _version in self._bundles}))

    def versions(self, package_key: str) -> tuple[str, ...]:
        return tuple(sorted(version for key, version in self._bundles if key == package_key))

    def get(self, package_key: str, version: str) -> OntologyPackageBundle | None:
        return self._bundles.get((str(package_key), str(version)))

    def all(self) -> tuple[OntologyPackageBundle, ...]:
        return tuple(self._bundles[key] for key in sorted(self._bundles))


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def canonical_ontology_hash(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _signature_payload(raw_payload: dict[str, Any]) -> bytes:
    unsigned = {key: value for key, value in raw_payload.items() if key != "signature"}
    return _canonical_json(unsigned).encode("utf-8")


def _reject_executable_shape(value: Any, *, path: str = "$") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            clean_key = str(key).strip().lower()
            if clean_key in _FORBIDDEN_DECLARATIVE_KEYS:
                raise OntologyPackageRejected(f"Domain Pack declarative boundary rejected key {path}.{key}")
            _reject_executable_shape(child, path=f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_executable_shape(child, path=f"{path}[{index}]")


def _assert_unique_refs(label: str, values: list[str]) -> None:
    if len(values) != len(set(values)):
        raise OntologyPackageRejected(f"duplicate {label} refs")
    for value in values:
        if not _REF_RE.fullmatch(value):
            raise OntologyPackageRejected(f"invalid {label} ref: {value}")


def _validate_package_contract(payload: OntologyPackagePayload) -> None:
    manifest = payload.manifest
    if manifest.hive_contract_version != PACKAGE_CONTRACT_VERSION:
        raise OntologyPackageRejected("Domain Pack Hive contract is incompatible")
    unsupported = set(manifest.engine_capabilities) - _SUPPORTED_ENGINE_CAPABILITIES
    if unsupported:
        raise OntologyPackageRejected(f"unsupported engine capabilities: {sorted(unsupported)}")
    if set(manifest.engine_capabilities) != _SUPPORTED_ENGINE_CAPABILITIES:
        raise OntologyPackageRejected("Domain Pack must declare the complete reference engine contract")
    if not all(namespace and "/" not in namespace and ".." not in namespace for namespace in manifest.namespaces):
        raise OntologyPackageRejected("Domain Pack namespace is invalid")

    object_refs = [item.type_ref for item in payload.ontology_schema.object_types]
    property_refs = [item.property_ref for item in payload.ontology_schema.property_types]
    link_refs = [item.link_type_ref for item in payload.ontology_schema.link_types]
    event_refs = [item.event_type_ref for item in payload.ontology_schema.event_types]
    rule_refs = [item.rule_ref for item in payload.rules]
    query_refs = [item.query_ref for item in payload.queries]
    action_refs = [item.action_type_ref for item in payload.actions]
    for label, refs in (
        ("object type", object_refs),
        ("property", property_refs),
        ("link", link_refs),
        ("event", event_refs),
        ("rule", rule_refs),
        ("query", query_refs),
        ("action", action_refs),
    ):
        _assert_unique_refs(label, refs)

    object_ref_set = set(object_refs)
    property_ref_set = set(property_refs)
    for item in payload.ontology_schema.object_types:
        canonicalize_sensitivity(item.sensitivity)
        if not set(item.required_property_refs) <= property_ref_set:
            raise OntologyPackageRejected(f"object type {item.type_ref} references an unknown property")
    for item in payload.ontology_schema.property_types:
        canonicalize_sensitivity(item.sensitivity)
        if item.value_type not in _VALUE_TYPES:
            raise OntologyPackageRejected(f"unsupported value_type: {item.value_type}")
        if not set(item.object_type_refs) <= object_ref_set:
            raise OntologyPackageRejected(f"property {item.property_ref} references an unknown object type")
    for item in payload.ontology_schema.link_types:
        canonicalize_sensitivity(item.sensitivity)
        if not set(item.from_type_refs) <= object_ref_set or not set(item.to_type_refs) <= object_ref_set:
            raise OntologyPackageRejected(f"link type {item.link_type_ref} references an unknown object type")
    for item in payload.ontology_schema.event_types:
        canonicalize_sensitivity(item.sensitivity)
        if not set(item.subject_type_refs) <= object_ref_set:
            raise OntologyPackageRejected(f"event type {item.event_type_ref} references an unknown object type")
    for item in payload.rules:
        canonicalize_sensitivity(item.sensitivity)
        if item.evaluation_mode not in _EVALUATION_MODES:
            raise OntologyPackageRejected(f"unsupported rule evaluation mode: {item.evaluation_mode}")
    for item in payload.actions:
        canonicalize_sensitivity(item.sensitivity)
        if item.side_effect_classification == "direct_execution":
            raise OntologyPackageRejected("Domain Pack actions cannot directly execute effects")
        if item.simulation_contract.get("external_side_effects") not in (None, [], False):
            raise OntologyPackageRejected("Domain Pack simulation must be side-effect free")
    if {case.query_ref for case in payload.acceptance.golden_queries} - set(query_refs):
        raise OntologyPackageRejected("golden query references an unknown named query")
    if {case.action_type_ref for case in payload.acceptance.golden_actions} - set(action_refs):
        raise OntologyPackageRejected("golden action references an unknown action")


def verify_ontology_package_payload(
    raw_payload: dict[str, Any],
    *,
    source_path: Path | None = None,
) -> OntologyPackageBundle:
    if not isinstance(raw_payload, dict):
        raise OntologyPackageRejected("Domain Pack root must be an object")
    _reject_executable_shape(raw_payload)
    try:
        payload = OntologyPackagePayload.model_validate(raw_payload)
    except Exception as exc:
        raise OntologyPackageRejected(f"Domain Pack schema rejected: {exc}") from exc
    _validate_package_contract(payload)

    signature = payload.signature
    public_key = _TRUSTED_ED25519_KEYS.get(signature.key_ref)
    if not public_key:
        raise OntologyPackageRejected("Domain Pack signature key is not trusted")
    signed_bytes = _signature_payload(raw_payload)
    try:
        VerifyKey(base64.b64decode(public_key)).verify(signed_bytes, base64.b64decode(signature.value))
    except (BadSignatureError, ValueError, TypeError) as exc:
        raise OntologyPackageRejected("Domain Pack signature verification failed") from exc

    content_hash = hashlib.sha256(signed_bytes).hexdigest()
    receipt = {
        "schema": "hive.company_ontology_package_admission.v1",
        "package_key": payload.manifest.package_key,
        "version": payload.manifest.version,
        "content_hash": content_hash,
        "signature_valid": True,
        "signature_key_ref": signature.key_ref,
        "contract_compatible": True,
        "declarative_only": True,
        "requested_capabilities": sorted(payload.manifest.engine_capabilities),
    }
    return OntologyPackageBundle(
        signature=signature,
        manifest=payload.manifest,
        schema=payload.ontology_schema,
        mappings=dict(payload.mappings),
        rules=tuple(payload.rules),
        queries=tuple(payload.queries),
        actions=tuple(payload.actions),
        permissions=dict(payload.permissions),
        acceptance=payload.acceptance,
        migrations=payload.migrations,
        content_hash=content_hash,
        verification_receipt=receipt,
        raw_payload=json.loads(json.dumps(raw_payload)),
        source_path=source_path,
    )


def _builtin_pack_root() -> Traversable:
    return resources.files("app.ontology").joinpath("domain_packs")


def load_builtin_ontology_catalog(
    root: Path | Traversable | None = None,
) -> OntologyPackageCatalog:
    pack_root = root.resolve() if isinstance(root, Path) else (root or _builtin_pack_root())
    bundles: list[OntologyPackageBundle] = []
    if not pack_root.is_dir():
        return OntologyPackageCatalog([])
    paths = sorted(
        (
            version_file
            for package_dir in pack_root.iterdir()
            if package_dir.is_dir()
            for version_file in package_dir.iterdir()
            if version_file.name.endswith(".json")
        ),
        key=lambda item: str(item),
    )
    for path in paths:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise OntologyPackageRejected(f"Domain Pack could not be read: {path}") from exc
        bundles.append(
            verify_ontology_package_payload(
                payload,
                source_path=path if isinstance(path, Path) else None,
            )
        )
    return OntologyPackageCatalog(bundles)


class OntologySourceIdentityCandidate(_StrictModel):
    source_contract_id: uuid.UUID
    source_identity_key: str = Field(min_length=1, max_length=700)
    aliases: list[str] = Field(default_factory=list, max_length=100)


class OntologyObjectCandidate(_StrictModel):
    stable_object_key: str = Field(min_length=1, max_length=500)
    object_type_ref: str
    display_name: str = Field(min_length=1, max_length=500)
    properties: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[str] = Field(min_length=1)
    source_identities: list[OntologySourceIdentityCandidate] = Field(default_factory=list)
    sensitivity: str
    permission_resource_ref: str = Field(min_length=1, max_length=500)
    valid_from: datetime
    valid_until: datetime | None = None
    observed_at: datetime


class OntologyAssertionCandidate(_StrictModel):
    stable_assertion_key: str = Field(min_length=1, max_length=500)
    subject_key: str = Field(min_length=1, max_length=500)
    predicate_ref: str
    object_key: str | None = None
    typed_value: Any | None = None
    assertion_kind: Literal["sourced", "derived", "tenant_authored"]
    source_refs: list[str] = Field(min_length=1)
    derived_by_rule_ref: str | None = None
    sensitivity: str
    permission_resource_ref: str = Field(min_length=1, max_length=500)
    valid_from: datetime
    valid_until: datetime | None = None
    observed_at: datetime
    supersedes_assertion_id: uuid.UUID | None = None


class OntologyLinkCandidate(_StrictModel):
    stable_link_key: str = Field(min_length=1, max_length=500)
    link_type_ref: str
    from_object_key: str = Field(min_length=1, max_length=500)
    to_object_key: str = Field(min_length=1, max_length=500)
    properties: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[str] = Field(min_length=1)
    sensitivity: str
    permission_resource_ref: str = Field(min_length=1, max_length=500)
    valid_from: datetime
    valid_until: datetime | None = None
    observed_at: datetime
    supersedes_link_id: uuid.UUID | None = None


class OntologyEventCandidate(_StrictModel):
    stable_event_key: str = Field(min_length=1, max_length=500)
    event_type_ref: str
    subject_object_key: str | None = None
    payload: dict[str, Any]
    occurred_at: datetime
    effective_from: datetime | None = None
    effective_until: datetime | None = None
    observed_at: datetime
    sequence: str | None = None
    source_refs: list[str] = Field(min_length=1)
    sensitivity: str
    permission_resource_ref: str = Field(min_length=1, max_length=500)


class OntologyCoverageLedger(_StrictModel):
    complete: bool
    total_units: int = Field(ge=1)
    covered_units: int = Field(ge=0)
    missing_units: list[str] = Field(default_factory=list)


class OntologyConflictLedger(_StrictModel):
    unresolved: list[dict[str, Any]] = Field(default_factory=list)
    resolved: list[dict[str, Any]] = Field(default_factory=list)


class OntologyModelPromptReceipt(_StrictModel):
    model: str = Field(min_length=1, max_length=300)
    prompt_hash: str
    response_hash: str
    source_refs: list[str] = Field(min_length=1)
    receipt_source: Literal["tool_runtime"] | None = None
    agent_id: uuid.UUID | None = None
    turn_id: str | None = Field(default=None, min_length=1, max_length=300)
    runtime_task_id: uuid.UUID | None = None


class OntologyCandidatePatch(_StrictModel):
    schema_version: str
    snapshot_complete: bool
    objects: list[OntologyObjectCandidate] = Field(default_factory=list, max_length=5000)
    assertions: list[OntologyAssertionCandidate] = Field(default_factory=list, max_length=20_000)
    links: list[OntologyLinkCandidate] = Field(default_factory=list, max_length=20_000)
    events: list[OntologyEventCandidate] = Field(default_factory=list, max_length=20_000)
    definition_overrides: OntologySchemaBundle = Field(default_factory=OntologySchemaBundle)
    coverage_ledger: OntologyCoverageLedger
    conflict_ledger: OntologyConflictLedger
    unresolved_questions: list[str] = Field(default_factory=list)
    model_prompt_receipts: list[OntologyModelPromptReceipt] = Field(default_factory=list)


def _require_evidence_ref(value: str) -> uuid.UUID:
    rendered = str(value or "").strip()
    if not rendered.startswith("company-evidence://"):
        raise OntologyCandidateRejected("candidate source_refs must use company-evidence://")
    try:
        return uuid.UUID(rendered.removeprefix("company-evidence://").split("#", 1)[0])
    except ValueError as exc:
        raise OntologyCandidateRejected("candidate evidence reference is invalid") from exc


def validate_ontology_candidate(candidate: OntologyCandidatePatch) -> OntologyCandidatePatch:
    try:
        candidate = OntologyCandidatePatch.model_validate(candidate.model_dump(mode="python"))
    except Exception as exc:
        raise OntologyCandidateRejected(f"candidate schema rejected: {exc}") from exc
    if candidate.schema_version != CANDIDATE_CONTRACT_VERSION:
        raise OntologyCandidateRejected("candidate contract version is incompatible")
    if candidate.snapshot_complete is not True:
        raise OntologyCandidateRejected("candidate must be a complete release snapshot")
    coverage = candidate.coverage_ledger
    if coverage.complete is not True or coverage.covered_units != coverage.total_units or coverage.missing_units:
        raise OntologyCandidateRejected("candidate coverage is incomplete")
    if candidate.conflict_ledger.unresolved:
        raise OntologyCandidateRejected("candidate has unresolved conflicts")
    if candidate.unresolved_questions:
        raise OntologyCandidateRejected("candidate has unresolved questions")

    object_keys = [item.stable_object_key for item in candidate.objects]
    assertion_keys = [item.stable_assertion_key for item in candidate.assertions]
    link_keys = [item.stable_link_key for item in candidate.links]
    event_keys = [item.stable_event_key for item in candidate.events]
    for label, values in (
        ("object", object_keys),
        ("assertion", assertion_keys),
        ("link", link_keys),
        ("event", event_keys),
    ):
        if len(values) != len(set(values)):
            raise OntologyCandidateRejected(f"candidate has duplicate {label} keys")
    object_key_set = set(object_keys)
    all_refs: list[str] = []
    for item in candidate.objects:
        canonicalize_sensitivity(item.sensitivity)
        all_refs.extend(item.source_refs)
    for item in candidate.assertions:
        canonicalize_sensitivity(item.sensitivity)
        if item.subject_key not in object_key_set or (item.object_key and item.object_key not in object_key_set):
            raise OntologyCandidateRejected("assertion references an unknown object")
        if (item.object_key is None) == (item.typed_value is None):
            raise OntologyCandidateRejected("assertion requires exactly one object_key or typed_value")
        all_refs.extend(item.source_refs)
    for item in candidate.links:
        canonicalize_sensitivity(item.sensitivity)
        if item.from_object_key not in object_key_set or item.to_object_key not in object_key_set:
            raise OntologyCandidateRejected("link references an unknown object")
        all_refs.extend(item.source_refs)
    for item in candidate.events:
        canonicalize_sensitivity(item.sensitivity)
        if item.subject_object_key and item.subject_object_key not in object_key_set:
            raise OntologyCandidateRejected("event references an unknown object")
        all_refs.extend(item.source_refs)
    for item in (
        *candidate.definition_overrides.object_types,
        *candidate.definition_overrides.property_types,
        *candidate.definition_overrides.link_types,
        *candidate.definition_overrides.event_types,
    ):
        canonicalize_sensitivity(item.sensitivity)
        all_refs.extend(item.source_refs)
    for receipt in candidate.model_prompt_receipts:
        if not _HASH_RE.fullmatch(receipt.prompt_hash) or not _HASH_RE.fullmatch(receipt.response_hash):
            raise OntologyCandidateRejected("model/prompt receipt hashes must be SHA-256")
        all_refs.extend(receipt.source_refs)
    if not all_refs:
        raise OntologyCandidateRejected("candidate evidence references are required")
    for source_ref in all_refs:
        _require_evidence_ref(source_ref)
    return candidate


def bind_runtime_model_receipt(
    raw_candidate: dict[str, Any],
    *,
    runtime_receipt: dict[str, Any],
) -> OntologyCandidatePatch:
    """Replace model-authored audit claims with one runtime-owned receipt."""

    if not isinstance(raw_candidate, dict):
        raise OntologyCandidateRejected("candidate must be an object")
    receipt = dict(runtime_receipt or {})
    if receipt.get("schema") != "hive.company_ontology_model_execution.v1":
        raise OntologyCandidateRejected("runtime model receipt schema is invalid")
    if receipt.get("receipt_source") != "tool_runtime":
        raise OntologyCandidateRejected("runtime model receipt source is invalid")
    try:
        agent_id = uuid.UUID(str(receipt.get("agent_id")))
    except (TypeError, ValueError) as exc:
        raise OntologyCandidateRejected("runtime model receipt agent is invalid") from exc
    model = str(receipt.get("model") or "").strip()
    turn_id = str(receipt.get("turn_id") or "").strip()
    prompt_hash = str(receipt.get("prompt_hash") or "").strip()
    if not model or not turn_id or not _HASH_RE.fullmatch(prompt_hash):
        raise OntologyCandidateRejected("runtime model receipt is incomplete")
    runtime_task_raw = receipt.get("runtime_task_id")
    try:
        runtime_task_id = uuid.UUID(str(runtime_task_raw)) if runtime_task_raw else None
    except (TypeError, ValueError) as exc:
        raise OntologyCandidateRejected("runtime model receipt task is invalid") from exc

    semantic_payload = json.loads(json.dumps(raw_candidate))
    semantic_payload["model_prompt_receipts"] = []
    try:
        semantic_candidate = OntologyCandidatePatch.model_validate(semantic_payload)
    except Exception as exc:
        raise OntologyCandidateRejected(f"candidate schema rejected: {exc}") from exc
    source_refs = sorted(
        {
            source_ref
            for item in (
                *semantic_candidate.objects,
                *semantic_candidate.assertions,
                *semantic_candidate.links,
                *semantic_candidate.events,
                *semantic_candidate.definition_overrides.object_types,
                *semantic_candidate.definition_overrides.property_types,
                *semantic_candidate.definition_overrides.link_types,
                *semantic_candidate.definition_overrides.event_types,
            )
            for source_ref in item.source_refs
        }
    )
    if not source_refs:
        raise OntologyCandidateRejected("runtime model receipt requires candidate evidence")
    semantic_payload["model_prompt_receipts"] = [
        {
            "model": model,
            "prompt_hash": prompt_hash,
            "response_hash": ontology_semantic_candidate_hash(semantic_payload),
            "source_refs": source_refs,
            "receipt_source": "tool_runtime",
            "agent_id": str(agent_id),
            "turn_id": turn_id,
            "runtime_task_id": str(runtime_task_id) if runtime_task_id else None,
        }
    ]
    return validate_ontology_candidate(OntologyCandidatePatch.model_validate(semantic_payload))


def ontology_semantic_candidate_hash(candidate: OntologyCandidatePatch | dict[str, Any]) -> str:
    """Hash model-authored semantics without caller-controlled audit receipts."""

    if isinstance(candidate, OntologyCandidatePatch):
        payload = candidate.model_dump(mode="json")
    elif isinstance(candidate, dict):
        payload = json.loads(json.dumps(candidate, default=str))
    else:
        raise OntologyCandidateRejected("candidate must be an object")
    payload["model_prompt_receipts"] = []
    try:
        normalized = OntologyCandidatePatch.model_validate(payload)
    except Exception as exc:
        raise OntologyCandidateRejected(f"candidate schema rejected: {exc}") from exc
    return canonical_ontology_hash(normalized.model_dump(mode="json"))


def ontology_candidate_hash(candidate: OntologyCandidatePatch) -> str:
    validated = validate_ontology_candidate(candidate)
    return canonical_ontology_hash(validated.model_dump(mode="json"))


__all__ = [
    "BUILTIN_SIGNATURE_KEY_REF",
    "CANDIDATE_CONTRACT_VERSION",
    "PACKAGE_CONTRACT_VERSION",
    "OntologyAcceptanceContract",
    "OntologyActionDefinition",
    "OntologyCandidatePatch",
    "OntologyCandidateRejected",
    "OntologyEventTypeDefinition",
    "OntologyLinkTypeDefinition",
    "OntologyNamedQueryDefinition",
    "OntologyObjectTypeDefinition",
    "OntologyPackageBundle",
    "OntologyPackageCatalog",
    "OntologyPackageRejected",
    "OntologyPropertyTypeDefinition",
    "OntologyRuleDefinitionContract",
    "OntologySchemaBundle",
    "bind_runtime_model_receipt",
    "canonical_ontology_hash",
    "load_builtin_ontology_catalog",
    "ontology_candidate_hash",
    "ontology_semantic_candidate_hash",
    "validate_ontology_candidate",
    "verify_ontology_package_payload",
]
