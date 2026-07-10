"""Personal Knowledge Base retrieval scorecard.

This harness compares Hive Personal KB retrieval output with optional SAG trace
output. SAG is treated only as an evaluation trace, never as a runtime provider
or source of truth.
"""

from __future__ import annotations

from dataclasses import dataclass
from statistics import median
from typing import Any


@dataclass(frozen=True, slots=True)
class PersonalKBScorecardCase:
    id: str
    query: str
    expected_refs: tuple[str, ...]
    forbidden_refs: tuple[str, ...]
    k: int = 5


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    if isinstance(value, set):
        return list(value)
    return [value]


def _clean_ref(value: Any) -> str | None:
    text = str(value or "").strip()
    return text or None


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        deduped.append(value)
    return deduped


def _refs_from_source_ref_object(value: Any) -> list[str]:
    if not isinstance(value, dict):
        cleaned = _clean_ref(value)
        return [cleaned] if cleaned else []

    refs: list[str] = []
    # Segment id is the retrieval/citation unit for Personal KB scorecards.
    for key in ("segment_id", "segmentId", "segment_ref", "segmentRef"):
        cleaned = _clean_ref(value.get(key))
        if cleaned:
            refs.append(cleaned)
    for key in ("ref", "source_ref", "sourceRef", "uri"):
        cleaned = _clean_ref(value.get(key))
        if cleaned:
            refs.append(cleaned)
    return refs


def refs_from_result_item(item: dict[str, Any]) -> list[str]:
    """Return ordered refs from Hive/SAG result shapes.

    Hive Personal KB currently returns ``source_ref`` and may also expose
    ``source_refs`` dictionaries with ``segment_id``. Scorecard input fixtures
    may use simpler ``ref`` or ``refs`` fields.
    """

    refs: list[str] = []
    for key in ("source_ref", "ref", "uri"):
        cleaned = _clean_ref(item.get(key))
        if cleaned:
            refs.append(cleaned)
    for key in ("source_refs", "refs", "citation_refs", "citations"):
        for value in _as_list(item.get(key)):
            refs.extend(_refs_from_source_ref_object(value))
    return _dedupe(refs)


def _parse_case(payload: dict[str, Any]) -> PersonalKBScorecardCase:
    case_id = _clean_ref(payload.get("id") or payload.get("case_id"))
    if not case_id:
        raise ValueError("Personal KB scorecard case is missing id")
    expected_refs = tuple(
        ref
        for ref in (
            _clean_ref(value) for value in _as_list(payload.get("expected_refs") or payload.get("expected_source_refs"))
        )
        if ref
    )
    forbidden_refs = tuple(
        ref
        for ref in (
            _clean_ref(value) for value in _as_list(payload.get("forbidden_refs") or payload.get("denied_refs"))
        )
        if ref
    )
    k_raw = payload.get("k", 5)
    try:
        k = max(1, int(k_raw))
    except (TypeError, ValueError):
        k = 5
    return PersonalKBScorecardCase(
        id=case_id,
        query=str(payload.get("query") or ""),
        expected_refs=expected_refs,
        forbidden_refs=forbidden_refs,
        k=k,
    )


def _provider_kind(provider_name: str) -> str:
    normalized = provider_name.strip().lower()
    if normalized in {"sag", "sag_trace", "sag-trace"}:
        return "comparison_trace"
    return "hive_native" if normalized == "hive" else "retrieval_trace"


def _score_case(case: PersonalKBScorecardCase, result: dict[str, Any] | None) -> dict[str, Any]:
    result = result or {}
    items = [
        item
        for item in _as_list(result.get("items") or result.get("hits") or result.get("results"))
        if isinstance(item, dict)
    ]
    top_items = items[: case.k]
    item_refs = [refs_from_result_item(item) for item in top_items]
    retrieved_refs = _dedupe([ref for refs in item_refs for ref in refs])
    retrieved_set = set(retrieved_refs)
    expected_set = set(case.expected_refs)
    forbidden_set = set(case.forbidden_refs)
    found_expected_refs = sorted(expected_set.intersection(retrieved_set))
    leaked_refs = sorted(forbidden_set.intersection(retrieved_set))

    if expected_set:
        recall_at_k = len(found_expected_refs) / len(expected_set)
    else:
        recall_at_k = 1.0

    expected_item_hits = sum(1 for refs in item_refs if expected_set.intersection(refs))
    denominator = max(len(top_items), len(expected_set), 1)
    citation_accuracy = expected_item_hits / denominator

    return {
        "case_id": case.id,
        "query": case.query,
        "k": case.k,
        "expected_refs": list(case.expected_refs),
        "forbidden_refs": list(case.forbidden_refs),
        "retrieved_refs": retrieved_refs,
        "found_expected_refs": found_expected_refs,
        "leaked_refs": leaked_refs,
        "recall_at_k": round(recall_at_k, 6),
        "citation_accuracy": round(citation_accuracy, 6),
        "acl_leakage_count": len(leaked_refs),
        "latency_ms": int(result.get("latency_ms") or 0),
        "cost_usd": round(float(result.get("cost_usd") or 0.0), 6),
    }


def _score_provider(provider_name: str, cases: list[PersonalKBScorecardCase], results: list[Any]) -> dict[str, Any]:
    result_by_case: dict[str, dict[str, Any]] = {}
    for item in results:
        if not isinstance(item, dict):
            continue
        case_id = _clean_ref(item.get("case_id") or item.get("id"))
        if case_id:
            result_by_case[case_id] = item

    case_reports = {case.id: _score_case(case, result_by_case.get(case.id)) for case in cases}
    case_count = len(cases)
    recall = (
        sum(case_report["recall_at_k"] for case_report in case_reports.values()) / case_count if case_count else 0.0
    )
    citation_accuracy = (
        sum(case_report["citation_accuracy"] for case_report in case_reports.values()) / case_count
        if case_count
        else 0.0
    )
    acl_leakage_count = sum(case_report["acl_leakage_count"] for case_report in case_reports.values())
    latencies = [case_report["latency_ms"] for case_report in case_reports.values() if case_report["latency_ms"]]

    return {
        "provider_kind": _provider_kind(provider_name),
        "case_count": case_count,
        "recall_at_k": round(recall, 6),
        "citation_accuracy": round(citation_accuracy, 6),
        "acl_leakage_count": acl_leakage_count,
        "acl_leakage_zero": acl_leakage_count == 0,
        "latency_ms_avg": round(sum(latencies) / len(latencies), 3) if latencies else 0,
        "latency_ms_p50": int(median(latencies)) if latencies else 0,
        "cost_total_usd": round(sum(case_report["cost_usd"] for case_report in case_reports.values()), 6),
        "cases": case_reports,
    }


def score_personal_kb_benchmark(payload: dict[str, Any]) -> dict[str, Any]:
    """Score Hive Personal KB retrieval against optional SAG trace results."""

    cases_payload = [case for case in _as_list(payload.get("cases")) if isinstance(case, dict)]
    cases = [_parse_case(case) for case in cases_payload]
    providers_payload = payload.get("providers") or {}
    if not isinstance(providers_payload, dict):
        raise ValueError("Personal KB scorecard payload.providers must be an object")

    providers = {
        provider_name: _score_provider(str(provider_name), cases, _as_list(provider_results))
        for provider_name, provider_results in providers_payload.items()
    }
    return {
        "benchmark": "personal_kb_sag_scorecard",
        "version": 1,
        "case_count": len(cases),
        "providers": providers,
        "hard_gates": {
            "acl_leakage_zero": all(provider["acl_leakage_zero"] for provider in providers.values())
            if providers
            else True,
        },
    }
