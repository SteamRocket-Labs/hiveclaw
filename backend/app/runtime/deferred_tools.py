"""Stable contract for deferred tool discovery candidates."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Iterable


@dataclass(frozen=True, slots=True)
class DeferredToolCandidate:
    name: str
    group: str
    reason: str
    selector: str
    schema_token_cost: int
    risk: str

    def to_manifest(self) -> dict[str, Any]:
        return asdict(self)


def make_deferred_tool_candidate(
    name: str,
    *,
    group: str = "deferred",
    reason: str = "available_via_tool_search",
    selector: str | None = None,
    schema_token_cost: int = 0,
    risk: str = "governed_runtime",
) -> DeferredToolCandidate | None:
    clean_name = str(name or "").strip()
    if not clean_name:
        return None
    clean_selector = str(selector or f"select:{clean_name}").strip()
    return DeferredToolCandidate(
        name=clean_name,
        group=str(group or "deferred").strip() or "deferred",
        reason=str(reason or "available_via_tool_search").strip() or "available_via_tool_search",
        selector=clean_selector,
        schema_token_cost=max(int(schema_token_cost or 0), 0),
        risk=str(risk or "governed_runtime").strip() or "governed_runtime",
    )


def coerce_deferred_tool_candidates(items: Iterable[Any] | None) -> list[DeferredToolCandidate]:
    candidates: list[DeferredToolCandidate] = []
    seen: set[str] = set()
    for item in items or ():
        if isinstance(item, DeferredToolCandidate):
            candidate = item
        elif isinstance(item, dict):
            candidate = make_deferred_tool_candidate(
                str(item.get("name") or ""),
                group=str(item.get("group") or "deferred"),
                reason=str(item.get("reason") or "available_via_tool_search"),
                selector=str(item.get("selector") or ""),
                schema_token_cost=int(item.get("schema_token_cost") or 0),
                risk=str(item.get("risk") or "governed_runtime"),
            )
        else:
            candidate = make_deferred_tool_candidate(str(item or ""))
        if candidate is None or candidate.name in seen:
            continue
        candidates.append(candidate)
        seen.add(candidate.name)
    return candidates


def deferred_tool_candidate_payload(items: Iterable[Any] | None) -> list[dict[str, Any]]:
    return [candidate.to_manifest() for candidate in coerce_deferred_tool_candidates(items)]
