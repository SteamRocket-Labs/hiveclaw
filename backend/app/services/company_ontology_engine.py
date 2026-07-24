"""Replaceable typed Company Ontology engine contract and Hive reference engine.

The engine receives already authorized typed data. It validates, filters,
projects, explains, and simulates; it never reads Hive authority tables,
publishes releases, changes permissions, or executes external effects.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Protocol

from app.services.company_ontology_contracts import (
    OntologyActionDefinition,
    OntologyCandidatePatch,
    OntologyPackageBundle,
    validate_ontology_candidate,
)


_CAPABILITIES = [
    "validate_package",
    "validate_candidate",
    "materialize_release_projection",
    "query",
    "resolve_fact_lineage",
    "simulate_action",
    "rebuild_projection",
]


class OntologyEngineUnavailable(RuntimeError):
    """A replaceable ontology provider could not answer safely."""


def _canonical_hash(value: Any) -> str:
    rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(rendered.encode("utf-8")).hexdigest()


def _json_type_matches(value: Any, expected: str) -> bool:
    if expected == "string":
        return isinstance(value, str)
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    if expected == "null":
        return value is None
    return False


def _ontology_value_matches(value: Any, value_type: str) -> bool:
    if value_type == "string":
        return isinstance(value, str)
    if value_type == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if value_type == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if value_type == "boolean":
        return isinstance(value, bool)
    if value_type == "datetime":
        if isinstance(value, datetime):
            return True
        if not isinstance(value, str):
            return False
        try:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return False
        return True
    if value_type == "uuid":
        try:
            uuid.UUID(str(value))
        except (TypeError, ValueError):
            return False
        return True
    if value_type == "object_ref":
        return isinstance(value, str) and bool(value.strip())
    if value_type == "json":
        return True
    return False


def validate_typed_payload(schema: dict[str, Any], payload: Any) -> tuple[str, ...]:
    """Small deterministic JSON contract validator for bundled typed schemas."""

    errors: list[str] = []
    expected_type = schema.get("type")
    if isinstance(expected_type, str) and not _json_type_matches(payload, expected_type):
        return (f"$ expected {expected_type}",)
    if expected_type == "object" and isinstance(payload, dict):
        required = {str(item) for item in schema.get("required", [])}
        missing = sorted(required - set(payload))
        errors.extend(f"$.{key} is required" for key in missing)
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for key, value in payload.items():
                child_schema = properties.get(key)
                if child_schema is None:
                    if schema.get("additionalProperties") is False:
                        errors.append(f"$.{key} is not allowed")
                    continue
                if isinstance(child_schema, dict):
                    errors.extend(
                        error.replace("$", f"$.{key}", 1) for error in validate_typed_payload(child_schema, value)
                    )
    if expected_type == "array" and isinstance(payload, list):
        item_schema = schema.get("items")
        if isinstance(item_schema, dict):
            for index, value in enumerate(payload):
                errors.extend(
                    error.replace("$", f"$[{index}]", 1) for error in validate_typed_payload(item_schema, value)
                )
    enum = schema.get("enum")
    if isinstance(enum, list) and payload not in enum:
        errors.append("$ is not one of the allowed enum values")
    return tuple(errors)


class OntologyEnginePlugin(Protocol):
    async def capability_status(self) -> dict[str, Any]: ...

    async def validate_package(self, package: OntologyPackageBundle) -> dict[str, Any]: ...

    async def validate_candidate(
        self,
        *,
        package: OntologyPackageBundle,
        candidate: OntologyCandidatePatch,
    ) -> dict[str, Any]: ...

    async def materialize_release_projection(self, request: dict[str, Any]) -> dict[str, Any]: ...

    async def query(self, request: dict[str, Any]) -> dict[str, Any]: ...

    async def resolve_fact_lineage(self, request: dict[str, Any]) -> dict[str, Any]: ...

    async def simulate_action(
        self,
        *,
        action_definition: OntologyActionDefinition,
        proposed_input: dict[str, Any],
    ) -> dict[str, Any]: ...

    async def rebuild_projection(self, request: dict[str, Any]) -> dict[str, Any]: ...


class ReferenceOntologyEngine:
    """Deterministic in-process reference implementation over typed snapshots."""

    async def capability_status(self) -> dict[str, Any]:
        return {
            "status": "available",
            "provider": "hive_reference",
            "capabilities": list(_CAPABILITIES),
            "authority": "hive_postgresql",
            "derived_projection": "rebuildable",
        }

    async def validate_package(self, package: OntologyPackageBundle) -> dict[str, Any]:
        return {
            "schema": "hive.company_ontology_engine_validation.v1",
            "passed": True,
            "provider": "hive_reference",
            "package_key": package.manifest.package_key,
            "version": package.manifest.version,
            "content_hash": package.content_hash,
            "checks": {
                "signature": package.verification_receipt["signature_valid"],
                "contract": package.verification_receipt["contract_compatible"],
                "declarative_only": package.verification_receipt["declarative_only"],
                "object_types": len(package.schema.object_types),
                "property_types": len(package.schema.property_types),
                "link_types": len(package.schema.link_types),
                "event_types": len(package.schema.event_types),
                "rules": len(package.rules),
                "queries": len(package.queries),
                "actions": len(package.actions),
            },
        }

    async def validate_candidate(
        self,
        *,
        package: OntologyPackageBundle,
        candidate: OntologyCandidatePatch,
    ) -> dict[str, Any]:
        candidate = validate_ontology_candidate(candidate)
        object_type_refs = {item.type_ref for item in package.schema.object_types}
        property_definitions = {item.property_ref: item for item in package.schema.property_types}
        property_refs = set(property_definitions)
        rule_refs = {item.rule_ref for item in package.rules}
        override_object_refs = {item.type_ref for item in candidate.definition_overrides.object_types}
        object_type_refs |= override_object_refs
        property_definitions.update({item.property_ref: item for item in candidate.definition_overrides.property_types})
        property_refs = set(property_definitions)

        errors: list[str] = []
        for item in candidate.definition_overrides.object_types:
            unknown_required = set(item.required_property_refs) - property_refs
            if unknown_required:
                errors.append(
                    f"object type {item.type_ref} has unknown required properties: {sorted(unknown_required)}"
                )
        for item in candidate.definition_overrides.property_types:
            unknown_owners = set(item.object_type_refs) - object_type_refs
            if unknown_owners:
                errors.append(f"property {item.property_ref} has unknown object types: {sorted(unknown_owners)}")
        for item in candidate.definition_overrides.link_types:
            unknown_endpoints = (set(item.from_type_refs) | set(item.to_type_refs)) - object_type_refs
            if unknown_endpoints:
                errors.append(f"link type {item.link_type_ref} has unknown object types: {sorted(unknown_endpoints)}")
        for item in candidate.definition_overrides.event_types:
            unknown_subjects = set(item.subject_type_refs) - object_type_refs
            if unknown_subjects:
                errors.append(f"event type {item.event_type_ref} has unknown object types: {sorted(unknown_subjects)}")

        object_type_definitions = {
            item.type_ref: item
            for item in (
                *package.schema.object_types,
                *candidate.definition_overrides.object_types,
            )
        }
        objects_by_key = {item.stable_object_key: item for item in candidate.objects}
        for item in candidate.objects:
            if item.object_type_ref not in object_type_refs:
                errors.append(f"unknown object type: {item.object_type_ref}")
            unknown_properties = set(item.properties) - property_refs
            if unknown_properties:
                errors.append(f"object {item.stable_object_key} has unknown properties: {sorted(unknown_properties)}")
            object_type = object_type_definitions.get(item.object_type_ref)
            if object_type is not None:
                missing_required = set(object_type.required_property_refs) - set(item.properties)
                if missing_required:
                    errors.append(
                        f"object {item.stable_object_key} is missing required properties: {sorted(missing_required)}"
                    )
            for property_ref, value in item.properties.items():
                definition = property_definitions.get(property_ref)
                if definition is None:
                    continue
                if item.object_type_ref not in set(definition.object_type_refs):
                    errors.append(f"property {property_ref} is not valid for object type {item.object_type_ref}")
                if not _ontology_value_matches(value, definition.value_type):
                    errors.append(
                        f"object {item.stable_object_key} property {property_ref} has invalid {definition.value_type} value"
                    )
        cardinality_counts: dict[tuple[str, str], int] = {}
        for item in candidate.assertions:
            definition = property_definitions.get(item.predicate_ref)
            if definition is None:
                errors.append(f"unknown assertion predicate: {item.predicate_ref}")
            else:
                subject = objects_by_key.get(item.subject_key)
                if subject is not None and subject.object_type_ref not in set(definition.object_type_refs):
                    errors.append(f"assertion {item.stable_assertion_key} predicate is invalid for subject type")
                if item.object_key is not None and definition.value_type != "object_ref":
                    errors.append(
                        f"assertion {item.stable_assertion_key} requires a typed {definition.value_type} value"
                    )
                if item.typed_value is not None and (
                    definition.value_type == "object_ref"
                    or not _ontology_value_matches(item.typed_value, definition.value_type)
                ):
                    errors.append(f"assertion {item.stable_assertion_key} has invalid {definition.value_type} value")
                cardinality_key = (item.subject_key, item.predicate_ref)
                cardinality_counts[cardinality_key] = cardinality_counts.get(cardinality_key, 0) + 1
                if definition.cardinality == "one" and cardinality_counts[cardinality_key] > 1:
                    errors.append(f"assertion {item.stable_assertion_key} violates one-value cardinality")
            if item.derived_by_rule_ref and item.derived_by_rule_ref not in rule_refs:
                errors.append(f"unknown derivation rule: {item.derived_by_rule_ref}")
        for item in candidate.links:
            definition = next(
                (
                    link_definition
                    for link_definition in (
                        *package.schema.link_types,
                        *candidate.definition_overrides.link_types,
                    )
                    if link_definition.link_type_ref == item.link_type_ref
                ),
                None,
            )
            if definition is None:
                errors.append(f"unknown link type: {item.link_type_ref}")
            else:
                from_object = objects_by_key.get(item.from_object_key)
                to_object = objects_by_key.get(item.to_object_key)
                if from_object is not None and from_object.object_type_ref not in set(definition.from_type_refs):
                    errors.append(f"link {item.stable_link_key} has invalid source object type")
                if to_object is not None and to_object.object_type_ref not in set(definition.to_type_refs):
                    errors.append(f"link {item.stable_link_key} has invalid target object type")
        for item in candidate.events:
            definition = next(
                (
                    event_definition
                    for event_definition in (
                        *package.schema.event_types,
                        *candidate.definition_overrides.event_types,
                    )
                    if event_definition.event_type_ref == item.event_type_ref
                ),
                None,
            )
            if definition is None:
                errors.append(f"unknown event type: {item.event_type_ref}")
            elif item.subject_object_key is not None:
                subject = objects_by_key.get(item.subject_object_key)
                if (
                    subject is not None
                    and definition.subject_type_refs
                    and subject.object_type_ref not in set(definition.subject_type_refs)
                ):
                    errors.append(f"event {item.stable_event_key} has invalid subject object type")
        return {
            "schema": "hive.company_ontology_candidate_validation.v1",
            "passed": not errors,
            "provider": "hive_reference",
            "candidate_hash": _canonical_hash(candidate.model_dump(mode="json")),
            "errors": errors,
            "coverage": candidate.coverage_ledger.model_dump(mode="json"),
            "conflicts": candidate.conflict_ledger.model_dump(mode="json"),
            "counts": {
                "objects": len(candidate.objects),
                "assertions": len(candidate.assertions),
                "links": len(candidate.links),
                "events": len(candidate.events),
            },
        }

    async def materialize_release_projection(self, request: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": "hive.company_ontology_projection_receipt.v1",
            "status": "materialized",
            "provider": "hive_reference",
            "release_id": str(request["release_id"]),
            "release_hash": str(request["release_hash"]),
            "projection_hash": _canonical_hash(request),
            "authority_changed": False,
        }

    async def query(self, request: dict[str, Any]) -> dict[str, Any]:
        objects = list(request.get("objects") or [])
        allowed_object_types = {str(value) for value in request.get("object_type_refs") or []}
        object_ids = {str(value) for value in request.get("object_ids") or []}
        if allowed_object_types:
            objects = [item for item in objects if str(item.get("object_type_ref")) in allowed_object_types]
        if object_ids:
            objects = [item for item in objects if str(item.get("object_id")) in object_ids]
        limit = max(1, min(int(request.get("limit") or 50), 200))
        bounded = objects[:limit]
        return {
            "schema": "hive.company_ontology_query_result.v1",
            "status": "ok" if bounded else "empty",
            "objects": bounded,
            "result_count": len(bounded),
            "truncated": len(objects) > limit,
            "provider": "hive_reference",
            "contract": {
                "typed_only": True,
                "natural_language_hard_gate": False,
                "relation_expansion_authorized_first": True,
            },
            "query_receipt": {
                "query_ref": request.get("query_ref"),
                "filters_hash": _canonical_hash(
                    {
                        "object_type_refs": sorted(allowed_object_types),
                        "object_ids": sorted(object_ids),
                        "time_window": request.get("time_window"),
                    }
                ),
            },
        }

    async def resolve_fact_lineage(self, request: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": "hive.company_ontology_fact_lineage.v1",
            "status": "ok",
            "assertion_id": str(request["assertion_id"]),
            "release_id": str(request["release_id"]),
            "source_refs": list(request.get("source_refs") or []),
            "evidence": list(request.get("evidence") or []),
            "coverage": dict(request.get("coverage") or {}),
            "provider": "hive_reference",
        }

    async def simulate_action(
        self,
        *,
        action_definition: OntologyActionDefinition,
        proposed_input: dict[str, Any],
    ) -> dict[str, Any]:
        errors = validate_typed_payload(action_definition.input_schema, proposed_input)
        return {
            "schema": "hive.company_ontology_action_simulation.v1",
            "status": "simulated" if not errors else "invalid_input",
            "simulation_id": str(uuid.uuid4()),
            "action_type_ref": action_definition.action_type_ref,
            "input_hash": _canonical_hash(proposed_input),
            "input_valid": not errors,
            "validation_errors": list(errors),
            "required_capability": action_definition.required_capability,
            "approval_policy": dict(action_definition.approval_policy),
            "tool_workflow_mapping": dict(action_definition.tool_workflow_mapping),
            "side_effect_classification": action_definition.side_effect_classification,
            "effect_committed": False,
            "external_side_effects": [],
            "provider": "hive_reference",
            "simulated_at": datetime.now(timezone.utc).isoformat(),
        }

    async def rebuild_projection(self, request: dict[str, Any]) -> dict[str, Any]:
        return {
            "schema": "hive.company_ontology_projection_rebuild.v1",
            "status": "rebuilt",
            "provider": "hive_reference",
            "release_id": str(request["release_id"]),
            "release_hash": str(request["release_hash"]),
            "projection_hash": _canonical_hash(request),
            "authority_changed": False,
        }


__all__ = [
    "OntologyEngineUnavailable",
    "OntologyEnginePlugin",
    "ReferenceOntologyEngine",
    "validate_typed_payload",
]
