"""Model-authored semantics without model-authored authority or receipts."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AnalysisEntity(_Strict):
    key: str = Field(min_length=1, max_length=500)
    type_ref: str = Field(min_length=1, max_length=500)
    display_name: str = Field(min_length=1, max_length=500)
    properties: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[str] = Field(min_length=1)


class AnalysisAssertion(_Strict):
    key: str = Field(min_length=1, max_length=500)
    subject_key: str = Field(min_length=1, max_length=500)
    predicate_ref: str = Field(min_length=1, max_length=500)
    object_key: str | None = None
    typed_value: Any = None
    source_refs: list[str] = Field(min_length=1)


class AnalysisLink(_Strict):
    key: str = Field(min_length=1, max_length=500)
    type_ref: str = Field(min_length=1, max_length=500)
    from_key: str = Field(min_length=1, max_length=500)
    to_key: str = Field(min_length=1, max_length=500)
    properties: dict[str, Any] = Field(default_factory=dict)
    source_refs: list[str] = Field(min_length=1)


class AnalysisFinding(_Strict):
    summary: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    source_refs: list[str] = Field(min_length=1)
    publication_ids: list[str] = Field(default_factory=list)


class EvidenceAnalysisCandidate(_Strict):
    schema_version: Literal["hive.company_evidence_analysis.v1"]
    entities: list[AnalysisEntity]
    assertions: list[AnalysisAssertion]
    links: list[AnalysisLink]
    covered_source_refs: list[str] = Field(min_length=1)
    unresolved_conflicts: list[AnalysisFinding]
    resolved_conflicts: list[AnalysisFinding]
    duplicate_candidates: list[AnalysisFinding]
    unresolved_questions: list[str]


def validate_analysis_candidate(payload, *, source_refs, publication_ids=()):
    candidate = EvidenceAnalysisCandidate.model_validate(payload)
    allowed = set(source_refs)
    if set(candidate.covered_source_refs) != allowed:
        raise ValueError("company_analysis_incomplete_source_coverage")
    for item in (*candidate.unresolved_conflicts, *candidate.resolved_conflicts, *candidate.duplicate_candidates):
        if not set(item.source_refs) <= allowed or not set(item.publication_ids) <= set(publication_ids):
            raise ValueError("company_analysis_finding_source_invalid")
    keys = [entity.key for entity in candidate.entities]
    for items in (candidate.entities, candidate.assertions, candidate.links):
        item_keys = [item.key for item in items]
        if len(set(item_keys)) != len(item_keys):
            raise ValueError("company_analysis_duplicate_candidate_key")
    for item in [*candidate.entities, *candidate.assertions, *candidate.links]:
        if not set(item.source_refs).issubset(allowed):
            raise ValueError("company_analysis_source_binding_mismatch")
    for assertion in candidate.assertions:
        if assertion.subject_key not in keys or (assertion.object_key is not None and assertion.object_key not in keys):
            raise ValueError("company_analysis_assertion_binding_mismatch")
        if assertion.object_key is None and assertion.typed_value is None:
            raise ValueError("company_analysis_assertion_value_missing")
    for link in candidate.links:
        if link.from_key not in keys or link.to_key not in keys:
            raise ValueError("company_analysis_link_binding_mismatch")
    return candidate
