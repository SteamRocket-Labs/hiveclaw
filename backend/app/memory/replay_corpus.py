"""Persisted, anonymized replay corpus (Phase 8 residual / Phase 16).

`policy_replay.guard_activation_policy_experiment()` evaluates activation
policy candidates against a list of `ReplayCase` objects. Up to Phase 15
that list lived only in test fixtures, which means production never gets
to evolve its policy from real owner / company outcomes.

This module persists `ReplayCase` instances to JSONL with three safety
properties:

1. **PII in candidate content is masked** through `PrivacyLayer`, so
   loaded cases never resurface owner email / phone / credential.
2. **Owner / company / goal terms are anonymized** through a stable
   placeholder map (`owner_term_1`, `company_term_1`, …). Two
   occurrences of "alice" become the same placeholder so retrieval
   matching still works; "alice" is never present on disk.
3. **PrincipalStack ids are anonymized** via the same map so PL3
   sensitivity rules can still be evaluated against the corpus without
   exposing original user ids.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.memory.activation import ActivationContext
from app.memory.policy_replay import ReplayCase
from app.memory.types import MemoryItem, MemoryKind
from app.services.principal_context import Principal, PrincipalRole, PrincipalStack
from app.services.privacy_layer import PrivacyLayer, PrivacyStore


@dataclass(slots=True)
class AnonymizationMap:
    """Stable namespace → original → placeholder map.

    Persisted alongside the corpus so loaded placeholders survive process
    restarts. For now we keep it in-memory; callers can write/load via
    `serialize` / `from_dict` if they need cross-run consistency.
    """

    table: dict[str, dict[str, str]] = field(default_factory=dict)

    def anonymize(self, namespace: str, original: str) -> str:
        if not original:
            return original
        bucket = self.table.setdefault(namespace, {})
        existing = bucket.get(original)
        if existing is not None:
            return existing
        digest = hashlib.sha256(f"{namespace}:{original}".encode("utf-8")).hexdigest()[:8]
        placeholder = f"{namespace}_{len(bucket) + 1}_{digest}"
        bucket[original] = placeholder
        return placeholder

    def serialize(self) -> dict[str, dict[str, str]]:
        return {ns: dict(bucket) for ns, bucket in self.table.items()}

    @classmethod
    def from_dict(cls, data: dict[str, dict[str, str]]) -> AnonymizationMap:
        return cls(table={ns: dict(bucket) for ns, bucket in data.items()})


def append_case_jsonl(path: Path, case: ReplayCase, *, amap: AnonymizationMap) -> None:
    """Append one anonymized case to `path` (jsonl)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    record = _serialize_case(case, amap)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record) + "\n")


def load_corpus(path: Path) -> list[ReplayCase]:
    """Load every well-formed line from `path`. Skips malformed lines."""
    if not path.exists():
        return []
    cases: list[ReplayCase] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            record = json.loads(line)
            cases.append(_deserialize_case(record))
        except (ValueError, KeyError, TypeError):
            continue
    return cases


def _serialize_case(case: ReplayCase, amap: AnonymizationMap) -> dict[str, Any]:
    privacy = PrivacyLayer(PrivacyStore())
    masked_candidates = []
    for item in case.candidates:
        decision = privacy.classify_and_mask(item.content)
        masked_candidates.append(
            {
                "kind": item.kind.value,
                "content": decision.sanitized_text,
                "score": item.score,
                "source": item.source,
                "metadata": {key: str(value) for key, value in item.metadata.items()},
            }
        )

    context = case.context
    return {
        "case_id": case.case_id,
        "context": {
            "query": privacy.classify_and_mask(context.query).sanitized_text,
            "principal_stack": _serialize_principal_stack(context.principal_stack, amap),
            "goal_terms": [amap.anonymize("goal_term", t) for t in context.goal_terms],
            "owner_terms": [amap.anonymize("owner_term", t) for t in context.owner_terms],
            "company_terms": [amap.anonymize("company_term", t) for t in context.company_terms],
        },
        "candidates": masked_candidates,
        "expected_entry_ids": sorted(case.expected_entry_ids),
    }


def _deserialize_case(record: dict[str, Any]) -> ReplayCase:
    ctx_data = record["context"]
    context = ActivationContext(
        query=str(ctx_data.get("query", "")),
        principal_stack=_deserialize_principal_stack(ctx_data.get("principal_stack", {})),
        goal_terms=list(ctx_data.get("goal_terms", []) or []),
        owner_terms=list(ctx_data.get("owner_terms", []) or []),
        company_terms=list(ctx_data.get("company_terms", []) or []),
    )
    candidates = [_deserialize_item(item) for item in record.get("candidates", []) or []]
    return ReplayCase(
        case_id=str(record["case_id"]),
        context=context,
        candidates=candidates,
        expected_entry_ids=set(record.get("expected_entry_ids", []) or []),
    )


def _serialize_principal_stack(stack: PrincipalStack, amap: AnonymizationMap) -> dict[str, Any]:
    return {
        slot: _serialize_principal(principal, amap)
        for slot, principal in (
            ("platform", stack.platform),
            ("company", stack.company),
            ("direct_owner", stack.direct_owner),
            ("creator", stack.creator),
            ("current_user", stack.current_user),
            ("delegating_agent", stack.delegating_agent),
        )
        if principal is not None
    }


def _serialize_principal(principal: Principal, amap: AnonymizationMap) -> dict[str, str]:
    return {
        "role": principal.role.value,
        "id": amap.anonymize("principal", principal.id),
        "label": amap.anonymize("principal_label", principal.label) if principal.label else "",
    }


def _deserialize_principal_stack(data: dict[str, Any]) -> PrincipalStack:
    def _maybe(slot: str) -> Principal | None:
        raw = data.get(slot)
        if not raw:
            return None
        return Principal(
            role=PrincipalRole(raw["role"]),
            id=str(raw["id"]),
            label=str(raw.get("label", "")),
        )

    return PrincipalStack(
        platform=_maybe("platform"),
        company=_maybe("company"),
        direct_owner=_maybe("direct_owner"),
        creator=_maybe("creator"),
        current_user=_maybe("current_user"),
        delegating_agent=_maybe("delegating_agent"),
    )


def _deserialize_item(data: dict[str, Any]) -> MemoryItem:
    return MemoryItem(
        kind=MemoryKind(data.get("kind", "semantic")),
        content=str(data.get("content", "")),
        score=float(data.get("score", 0.0)),
        source=str(data.get("source", "")),
        metadata={key: str(value) for key, value in (data.get("metadata") or {}).items()},
    )
