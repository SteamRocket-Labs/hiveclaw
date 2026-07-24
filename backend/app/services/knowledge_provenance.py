"""Typed provenance for governed Knowledge results.

The module reads only exact tool/result machine envelopes. It never infers
privacy or authority from natural-language content. The original tool result
remains the durable evidence; this projection carries the labels and lossless
references that downstream T0, T2, and outbound-effect boundaries need.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable, Mapping
from typing import Any

from app.services.privacy_layer import (
    SensitivityLevel,
    canonicalize_sensitivity,
    max_sensitivity,
    sensitivity_rank,
)


KNOWLEDGE_PROVENANCE_KEY = "knowledge_provenance"
KNOWLEDGE_CONTENT_SENSITIVITY_KEY = "content_sensitivity"
_KNOWLEDGE_TOOL_SCOPES = {
    "search_personal_kb": "personal",
    "read_personal_kb": "personal",
    "search_company_kb": "company",
    "read_company_kb": "company",
    "query_company_ontology": "company",
    "get_company_object": "company",
    "explain_company_fact": "company",
}
KNOWLEDGE_TOOL_NAMES = frozenset(_KNOWLEDGE_TOOL_SCOPES)
_ONTOLOGY_KNOWLEDGE_TOOLS = frozenset(
    {
        "query_company_ontology",
        "get_company_object",
        "explain_company_fact",
    }
)
_KNOWLEDGE_PROVENANCE_FORWARDING_TOOLS = {
    "spawn_subagent",
    "check_subagent",
}


def _mapping(value: Any) -> dict[str, Any] | None:
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, (str, bytes, bytearray)):
        try:
            parsed = json.loads(value)
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return dict(parsed) if isinstance(parsed, Mapping) else None
    return None


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)


def _sha256(value: Any) -> str:
    return hashlib.sha256(_canonical_json(value).encode("utf-8")).hexdigest()


def _source_value(row: Mapping[str, Any], key: str, fallback: Any = None) -> str | None:
    value = row.get(key, fallback)
    rendered = str(value or "").strip()
    return rendered or None


def _canonical_source_sensitivity(
    value: Any,
    *,
    warnings: list[str],
) -> tuple[SensitivityLevel, str | None]:
    try:
        return canonicalize_sensitivity(value), None
    except ValueError:
        declared = str(value or "").strip() or "<missing>"
        warning = f"invalid_sensitivity:{declared}"
        if warning not in warnings:
            warnings.append(warning)
        return SensitivityLevel.PL4_CREDENTIAL, declared


def ontology_result_source_rows(
    tool_name: str,
    payload: Mapping[str, Any],
) -> tuple[list[dict[str, Any]], bool] | None:
    """Flatten typed ontology results into evidence-bearing source records.

    The returned records contain identifiers, source refs, and declared
    sensitivity only; semantic object/fact/link content never enters replay
    projections. Missing lineage or a missing source ref makes coverage
    incomplete so downstream consumers fail closed.
    """

    normalized_tool_name = str(tool_name or "").strip()
    if normalized_tool_name not in _ONTOLOGY_KNOWLEDGE_TOOLS:
        return None

    rows: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()
    coverage_complete = True

    def append_item(
        raw_item: Any,
        *,
        result_kind: str,
        inherited: Mapping[str, Any] | None = None,
        source_refs: Any = None,
    ) -> None:
        nonlocal coverage_complete
        if not isinstance(raw_item, Mapping):
            coverage_complete = False
            rows.append(
                {
                    "result_kind": "invalid_source_record",
                    "sensitivity": None,
                }
            )
            return
        item = dict(raw_item)
        parent = dict(inherited or {})
        refs = source_refs if source_refs is not None else item.get("source_refs")
        normalized_refs = (
            [str(value).strip() for value in refs if str(value).strip()] if isinstance(refs, list | tuple) else []
        )
        if not normalized_refs:
            coverage_complete = False
            normalized_refs = [None]
        identifiers = {
            key: item.get(key, parent.get(key)) for key in ("release_id", "object_id", "assertion_id", "link_id")
        }
        stable_id = next(
            (
                str(identifiers[key])
                for key in ("assertion_id", "link_id", "object_id", "release_id")
                if identifiers.get(key)
            ),
            "",
        )
        for source_ref in normalized_refs:
            dedupe_key = (result_kind, stable_id, str(source_ref or ""))
            if dedupe_key in seen:
                continue
            seen.add(dedupe_key)
            rows.append(
                {
                    "result_kind": result_kind,
                    **{key: value for key, value in identifiers.items() if value is not None},
                    "source_ref": source_ref,
                    "sensitivity": item.get("sensitivity"),
                }
            )

    def append_object(raw_object: Any) -> None:
        nonlocal coverage_complete
        if not isinstance(raw_object, Mapping):
            append_item(raw_object, result_kind="invalid_source_record")
            return
        obj = dict(raw_object)
        append_item(obj, result_kind="ontology_object")
        for fact in obj.get("facts") or []:
            append_item(
                fact,
                result_kind="ontology_assertion",
                inherited={
                    "release_id": obj.get("release_id"),
                    "object_id": obj.get("object_id"),
                },
            )
        for link in obj.get("links") or []:
            append_item(
                link,
                result_kind="ontology_link",
                inherited={
                    "release_id": obj.get("release_id"),
                    "object_id": obj.get("object_id"),
                },
            )

    if normalized_tool_name == "query_company_ontology":
        raw_objects = payload.get("objects")
        if not isinstance(raw_objects, list):
            return ([], False)
        for raw_object in raw_objects:
            append_object(raw_object)
    elif normalized_tool_name == "get_company_object":
        raw_object = payload.get("object")
        if raw_object is None:
            return ([], True)
        append_object(raw_object)
    else:
        raw_fact = payload.get("fact")
        if raw_fact is None:
            return ([], True)
        if not isinstance(raw_fact, Mapping):
            append_item(raw_fact, result_kind="invalid_source_record")
        else:
            fact = dict(raw_fact)
            lineage = fact.get("lineage")
            lineage_map = dict(lineage) if isinstance(lineage, Mapping) else {}
            lineage_refs = lineage_map.get("source_refs")
            if not lineage_refs:
                raw_evidence = lineage_map.get("evidence")
                lineage_refs = (
                    [
                        row.get("evidence_ref")
                        for row in raw_evidence
                        if isinstance(row, Mapping) and row.get("evidence_ref")
                    ]
                    if isinstance(raw_evidence, list)
                    else []
                )
            append_item(
                fact,
                result_kind="ontology_assertion",
                source_refs=fact.get("source_refs") or lineage_refs,
            )
            lineage_coverage = lineage_map.get("coverage")
            if not isinstance(lineage_coverage, Mapping) or lineage_coverage.get("complete") is not True:
                coverage_complete = False

    return rows, coverage_complete


def build_knowledge_provenance(tool_name: str, raw_result: Any) -> dict[str, Any] | None:
    """Project one trusted Knowledge tool result into typed provenance.

    Empty/denied results carry no Knowledge bytes and therefore do not taint a
    later answer. Malformed sensitivity on a returned source fails closed as
    PL4 while preserving the declared value and a recovery warning.
    """

    normalized_tool_name = str(tool_name or "").strip()
    scope = _KNOWLEDGE_TOOL_SCOPES.get(normalized_tool_name)
    if scope is None:
        return None
    payload = _mapping(raw_result)
    if payload is None:
        return None

    ontology_rows = ontology_result_source_rows(normalized_tool_name, payload)
    if ontology_rows is not None:
        rows, coverage_complete = ontology_rows
    else:
        is_search = normalized_tool_name.startswith("search_")
        raw_rows = payload.get("results" if is_search else "segments")
        rows = list(raw_rows) if isinstance(raw_rows, list) else []
        coverage_complete = True
        if not rows and payload.get("credential_reference"):
            rows = [
                {
                    "result_kind": "credential_reference",
                    "document_id": payload.get("document_id"),
                    "credential_reference": payload.get("credential_reference"),
                    "sensitivity": payload.get("sensitivity") or SensitivityLevel.PL4_CREDENTIAL.value,
                    "source_ref": payload.get("source_ref"),
                }
            ]
    if not rows:
        return None

    default_document_id = _source_value(payload, "document_id")
    default_publication_id = _source_value(payload, "publication_id")
    default_source_ref = _source_value(payload, "source_ref")
    default_sensitivity = payload.get("sensitivity")
    warnings: list[str] = []
    sources: list[dict[str, Any]] = []
    sensitivities: list[SensitivityLevel] = []

    for index, raw_row in enumerate(rows):
        if not isinstance(raw_row, Mapping):
            coverage_complete = False
            sensitivities.append(SensitivityLevel.PL4_CREDENTIAL)
            sources.append(
                {
                    "source_index": index,
                    "result_kind": "invalid_source_record",
                    "sensitivity": SensitivityLevel.PL4_CREDENTIAL.value,
                    "declared_sensitivity": "<invalid-source-record>",
                }
            )
            warning = f"invalid_source_record:{index}"
            if warning not in warnings:
                warnings.append(warning)
            continue

        row = dict(raw_row)
        declared_sensitivity = row.get("sensitivity", default_sensitivity)
        sensitivity, invalid_declared = _canonical_source_sensitivity(
            declared_sensitivity,
            warnings=warnings,
        )
        sensitivities.append(sensitivity)
        document_id = _source_value(row, "document_id", default_document_id)
        publication_id = _source_value(row, "publication_id", default_publication_id)
        segment_id = _source_value(row, "segment_id")
        source_ref = _source_value(row, "source_ref", default_source_ref)
        if source_ref and segment_id and "#segment=" not in source_ref:
            source_ref = f"{source_ref}#segment={segment_id}"
        source: dict[str, Any] = {
            "source_index": index,
            "result_kind": _source_value(row, "result_kind")
            or ("knowledge_segment" if segment_id else "knowledge_document"),
            "publication_id": publication_id,
            "document_id": document_id,
            "segment_id": segment_id,
            "source_ref": source_ref,
            "sensitivity": sensitivity.value,
        }
        for identifier_key in ("release_id", "object_id", "assertion_id", "link_id"):
            identifier = _source_value(row, identifier_key)
            if identifier is not None:
                source[identifier_key] = identifier
        if invalid_declared is not None:
            source["declared_sensitivity"] = invalid_declared
            coverage_complete = False
        sources.append({key: value for key, value in source.items() if value is not None})

    if not sensitivities:
        return None
    effective_sensitivity = max_sensitivity(*sensitivities)
    semantic_memory_eligible = sensitivity_rank(effective_sensitivity) < sensitivity_rank(
        SensitivityLevel.PL3_SENSITIVE
    )
    authority = payload.get("authority") if isinstance(payload.get("authority"), Mapping) else None
    status = "held_invalid_sensitivity" if warnings else str(payload.get("status") or "ok")
    provenance: dict[str, Any] = {
        "schema": "hive.knowledge_provenance.v1",
        "scope": scope,
        "tool_name": normalized_tool_name,
        "status": status,
        "max_sensitivity": effective_sensitivity.value,
        "semantic_memory_eligible": semantic_memory_eligible,
        "authority": dict(authority) if authority is not None else None,
        "sources": sources,
        "coverage": {
            "result_count": len(rows),
            "source_count": len(sources),
            "complete": coverage_complete,
        },
        "result_sha256": _sha256(payload),
        "source_manifest_sha256": _sha256(sources),
        "warnings": warnings,
    }
    return provenance


def _tool_envelope(content: Any) -> dict[str, Any] | None:
    return _mapping(content)


def _tool_name_from_event(content: Any, metadata: Mapping[str, Any]) -> str:
    direct = str(metadata.get("tool_name") or "").strip()
    if direct:
        return direct
    tool_event = metadata.get("tool_event")
    if isinstance(tool_event, Mapping):
        nested = str(tool_event.get("name") or tool_event.get("tool_name") or "").strip()
        if nested:
            return nested
    envelope = _tool_envelope(content)
    if envelope is not None:
        return str(envelope.get("name") or envelope.get("tool_name") or "").strip()
    return ""


def _raw_result_from_event(content: Any) -> Any:
    envelope = _tool_envelope(content)
    if envelope is not None and "result" in envelope:
        return envelope.get("result")
    return content


def _forwarded_knowledge_provenance(tool_name: str, raw_result: Any) -> dict[str, Any] | None:
    """Read provenance only from trusted collaboration result envelopes."""

    if tool_name not in _KNOWLEDGE_PROVENANCE_FORWARDING_TOOLS:
        return None
    result = _mapping(raw_result)
    if result is None:
        return None
    raw_provenance = result.get(KNOWLEDGE_PROVENANCE_KEY)
    if not isinstance(raw_provenance, Mapping):
        return None
    provenance = dict(raw_provenance)
    if provenance.get("schema") != "hive.knowledge_provenance_aggregate.v1":
        return None
    try:
        sensitivity = canonicalize_sensitivity(provenance.get("max_sensitivity"))
    except ValueError:
        sensitivity = SensitivityLevel.PL4_CREDENTIAL
        provenance["status"] = "held_invalid_sensitivity"
        provenance["warnings"] = ["invalid_forwarded_sensitivity"]
    provenance["max_sensitivity"] = sensitivity.value
    provenance["semantic_memory_eligible"] = sensitivity_rank(sensitivity) < sensitivity_rank(
        SensitivityLevel.PL3_SENSITIVE
    )
    refs = provenance.get("source_event_refs")
    if not isinstance(refs, list) or not all(isinstance(ref, str) and ref.strip() for ref in refs):
        provenance["source_event_refs"] = []
        provenance["semantic_memory_eligible"] = False
        provenance["max_sensitivity"] = SensitivityLevel.PL4_CREDENTIAL.value
        provenance["status"] = "held_invalid_source_refs"
        provenance["warnings"] = ["invalid_forwarded_source_refs"]
    return provenance


def enrich_knowledge_event_metadata(
    *,
    event_type: str,
    content: Any,
    metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Attach Knowledge provenance to an exact ``tool_result`` event."""

    enriched = dict(metadata or {})
    if str(event_type or "") != "tool_result":
        return enriched
    tool_name = _tool_name_from_event(content, enriched)
    raw_result = _raw_result_from_event(content)
    provenance = build_knowledge_provenance(tool_name, raw_result)
    if provenance is None:
        provenance = _forwarded_knowledge_provenance(tool_name, raw_result)
    if provenance is None:
        return enriched
    enriched[KNOWLEDGE_PROVENANCE_KEY] = provenance
    enriched[KNOWLEDGE_CONTENT_SENSITIVITY_KEY] = provenance["max_sensitivity"]
    if not provenance["semantic_memory_eligible"]:
        enriched["semantic_memory_eligible"] = False
    elif "semantic_memory_eligible" not in enriched:
        enriched["semantic_memory_eligible"] = True
    return enriched


def merge_knowledge_provenance(
    entries: Iterable[tuple[str, Mapping[str, Any]]],
) -> dict[str, Any] | None:
    """Merge per-event provenance without duplicating raw Knowledge bodies."""

    source_event_refs: list[str] = []
    seen_refs: set[str] = set()
    sensitivities: list[SensitivityLevel] = []
    scopes: list[str] = []
    tool_names: list[str] = []
    manifest: list[dict[str, Any]] = []
    coverage_complete = True

    def append_unique_strings(target: list[str], value: Any) -> None:
        values = value if isinstance(value, (list, tuple, set, frozenset)) else (value,)
        for item in values:
            rendered = str(item or "").strip()
            if rendered and rendered not in target:
                target.append(rendered)

    for raw_ref, raw_provenance in entries:
        provenance = dict(raw_provenance or {})
        if not provenance:
            continue
        ref = str(raw_ref or "").strip()
        if ref and ref not in seen_refs:
            seen_refs.add(ref)
            source_event_refs.append(ref)
        nested_refs = provenance.get("source_event_refs")
        if isinstance(nested_refs, list):
            for nested_ref in nested_refs:
                rendered_ref = str(nested_ref or "").strip()
                if rendered_ref and rendered_ref not in seen_refs:
                    seen_refs.add(rendered_ref)
                    source_event_refs.append(rendered_ref)
        declared = provenance.get("max_sensitivity")
        try:
            sensitivity = canonicalize_sensitivity(declared)
        except ValueError:
            sensitivity = SensitivityLevel.PL4_CREDENTIAL
            coverage_complete = False
        sensitivities.append(sensitivity)
        append_unique_strings(scopes, provenance.get("scope"))
        append_unique_strings(tool_names, provenance.get("tool_name"))
        append_unique_strings(tool_names, provenance.get("tool_names"))
        coverage = provenance.get("coverage")
        if isinstance(coverage, Mapping) and coverage.get("complete") is False:
            coverage_complete = False
        manifest.append(
            {
                "source_event_ref": ref or None,
                "scope": provenance.get("scope"),
                "tool_name": provenance.get("tool_name"),
                "tool_names": provenance.get("tool_names"),
                "max_sensitivity": sensitivity.value,
                "result_sha256": provenance.get("result_sha256"),
                "source_manifest_sha256": provenance.get("source_manifest_sha256"),
            }
        )

    if not sensitivities:
        return None
    effective_sensitivity = max_sensitivity(*sensitivities)
    semantic_memory_eligible = sensitivity_rank(effective_sensitivity) < sensitivity_rank(
        SensitivityLevel.PL3_SENSITIVE
    )
    return {
        "schema": "hive.knowledge_provenance_aggregate.v1",
        "scope": scopes,
        "tool_names": tool_names,
        "max_sensitivity": effective_sensitivity.value,
        "semantic_memory_eligible": semantic_memory_eligible,
        "source_event_refs": source_event_refs,
        "coverage": {
            "source_event_count": len(source_event_refs),
            "complete": coverage_complete,
        },
        "event_manifest_sha256": _sha256(manifest),
    }


def apply_inherited_knowledge_provenance(
    metadata: Mapping[str, Any] | None,
    provenance: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Bind an aggregate to assistant/output metadata at a mechanical seam."""

    enriched = dict(metadata or {})
    if not provenance:
        return enriched
    normalized = dict(provenance)
    enriched[KNOWLEDGE_PROVENANCE_KEY] = normalized
    sensitivity = canonicalize_sensitivity(normalized.get("max_sensitivity"))
    enriched[KNOWLEDGE_CONTENT_SENSITIVITY_KEY] = sensitivity.value
    if sensitivity_rank(sensitivity) >= sensitivity_rank(SensitivityLevel.PL3_SENSITIVE):
        enriched["semantic_memory_eligible"] = False
    elif "semantic_memory_eligible" not in enriched:
        enriched["semantic_memory_eligible"] = True
    return enriched


def knowledge_content_sensitivity(metadata: Mapping[str, Any] | None) -> str | None:
    """Return canonical typed sensitivity from event/output metadata."""

    if not metadata:
        return None
    value = metadata.get(KNOWLEDGE_CONTENT_SENSITIVITY_KEY)
    if value is None:
        provenance = metadata.get(KNOWLEDGE_PROVENANCE_KEY)
        if isinstance(provenance, Mapping):
            value = provenance.get("max_sensitivity")
    if value is None:
        return None
    try:
        return canonicalize_sensitivity(value).value
    except ValueError:
        return SensitivityLevel.PL4_CREDENTIAL.value


async def load_transcript_knowledge_provenance(
    db: Any,
    *,
    tenant_id: Any,
    agent_id: Any,
    session_id: Any,
    run_id: Any | None = None,
    turn_id: str | None = None,
    before_sequence: int | None = None,
) -> dict[str, Any] | None:
    """Aggregate Knowledge source receipts for exactly one run or turn.

    ``run_id`` is the preferred durable boundary. Direct channel turns without
    a RuntimeTask may use their typed ``turn_id``. A caller that supplies
    neither gets no aggregate rather than accidentally inheriting an older
    session's Knowledge access.
    """

    if run_id is None and not str(turn_id or "").strip():
        return None

    from sqlalchemy import select

    from app.models.chat_transcript_event import ChatTranscriptEvent

    filters = [
        ChatTranscriptEvent.tenant_id == tenant_id,
        ChatTranscriptEvent.agent_id == agent_id,
        ChatTranscriptEvent.session_id == session_id,
        ChatTranscriptEvent.event_type.in_(("tool_result", "knowledge_provenance_repair")),
    ]
    if run_id is not None:
        filters.append(ChatTranscriptEvent.run_id == run_id)
    else:
        filters.append(ChatTranscriptEvent.turn_id == str(turn_id).strip())
    if before_sequence is not None:
        filters.append(ChatTranscriptEvent.sequence < int(before_sequence))

    rows = list(
        (await db.execute(select(ChatTranscriptEvent).where(*filters).order_by(ChatTranscriptEvent.sequence.asc())))
        .scalars()
        .all()
    )
    entries: list[tuple[str, Mapping[str, Any]]] = []
    for row in rows:
        metadata = getattr(row, "metadata_json", None)
        provenance = metadata.get(KNOWLEDGE_PROVENANCE_KEY) if isinstance(metadata, Mapping) else None
        if isinstance(provenance, Mapping):
            target_ref = str(metadata.get("target_event_ref") or "").strip() if isinstance(metadata, Mapping) else ""
            entries.append((target_ref or f"transcript://event/{row.id}", provenance))
    return merge_knowledge_provenance(entries)
