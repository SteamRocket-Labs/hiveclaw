"""Phase 16: persisted replay corpus tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.memory.activation import ActivationContext
from app.memory.policy_replay import ReplayCase
from app.memory.replay_corpus import (
    AnonymizationMap,
    append_case_jsonl,
    load_corpus,
)
from app.memory.types import MemoryItem, MemoryKind
from app.services.principal_context import (
    Principal,
    PrincipalRole,
    PrincipalStack,
)


def _make_case(case_id: str, owner_term: str = "alice") -> ReplayCase:
    owner = Principal(role=PrincipalRole.OWNER, id="owner-1", label=owner_term)
    stack = PrincipalStack(direct_owner=owner)
    context = ActivationContext(
        query="weekly research memo",
        principal_stack=stack,
        goal_terms=["research"],
        owner_terms=[owner_term],
        company_terms=["acme"],
    )
    candidates = [
        MemoryItem(
            kind=MemoryKind.SEMANTIC,
            content="alice@example.com prefers cited sources",
            score=0.6,
            source="memory/knowledge.md",
            metadata={"entry_id": "k1", "sensitivity": "PL1_public"},
        ),
        MemoryItem(
            kind=MemoryKind.SEMANTIC,
            content="acme requires legal review for external publications",
            score=0.4,
            source="memory/knowledge.md",
            metadata={"entry_id": "k2", "sensitivity": "PL1_public"},
        ),
    ]
    return ReplayCase(
        case_id=case_id,
        context=context,
        candidates=candidates,
        expected_entry_ids={"k1"},
    )


class TestAppendAndLoad:
    def test_append_then_load_roundtrips(self, tmp_path: Path) -> None:
        path = tmp_path / "corpus.jsonl"
        amap = AnonymizationMap()
        append_case_jsonl(path, _make_case("case-1"), amap=amap)
        append_case_jsonl(path, _make_case("case-2", owner_term="bob"), amap=amap)
        loaded = load_corpus(path)
        assert len(loaded) == 2
        ids = {case.case_id for case in loaded}
        assert ids == {"case-1", "case-2"}

    def test_owner_terms_are_anonymized_consistently(self, tmp_path: Path) -> None:
        path = tmp_path / "corpus.jsonl"
        amap = AnonymizationMap()
        append_case_jsonl(path, _make_case("case-1", owner_term="alice"), amap=amap)
        append_case_jsonl(path, _make_case("case-2", owner_term="alice"), amap=amap)
        loaded = load_corpus(path)
        a = loaded[0].context.owner_terms[0]
        b = loaded[1].context.owner_terms[0]
        assert a == b
        assert a != "alice"
        assert a.startswith("owner_term_")

    def test_pii_in_candidate_content_is_masked(self, tmp_path: Path) -> None:
        path = tmp_path / "corpus.jsonl"
        amap = AnonymizationMap()
        append_case_jsonl(path, _make_case("case-1"), amap=amap)
        loaded = load_corpus(path)
        candidate_texts = "\n".join(item.content for item in loaded[0].candidates)
        assert "alice@example.com" not in candidate_texts

    def test_expected_entry_ids_preserved(self, tmp_path: Path) -> None:
        path = tmp_path / "corpus.jsonl"
        append_case_jsonl(path, _make_case("case-1"), amap=AnonymizationMap())
        loaded = load_corpus(path)
        assert loaded[0].expected_entry_ids == {"k1"}


class TestEvaluatable:
    def test_loaded_corpus_works_with_policy_evaluator(self, tmp_path: Path) -> None:
        from app.memory.activation import ActivationPolicy
        from app.memory.policy_replay import evaluate_activation_policy

        path = tmp_path / "corpus.jsonl"
        amap = AnonymizationMap()
        append_case_jsonl(path, _make_case("case-1"), amap=amap)
        cases = load_corpus(path)
        report = evaluate_activation_policy(policy=ActivationPolicy(), cases=cases, top_k=2)
        assert report.total_cases == 1
        assert report.hit_rate >= 0.0


class TestMalformed:
    def test_load_skips_malformed_lines(self, tmp_path: Path) -> None:
        path = tmp_path / "corpus.jsonl"
        path.write_text("not json\n", encoding="utf-8")
        loaded = load_corpus(path)
        assert loaded == []

    def test_load_missing_file_returns_empty(self, tmp_path: Path) -> None:
        assert load_corpus(tmp_path / "missing.jsonl") == []


class TestMissingDeps:
    def test_anon_map_returns_consistent_id(self) -> None:
        amap = AnonymizationMap()
        a = amap.anonymize("owner_term", "alice")
        b = amap.anonymize("owner_term", "alice")
        c = amap.anonymize("owner_term", "bob")
        assert a == b
        assert a != c

    def test_principal_id_anonymized(self, tmp_path: Path) -> None:
        path = tmp_path / "corpus.jsonl"
        amap = AnonymizationMap()
        append_case_jsonl(path, _make_case("case-1"), amap=amap)
        loaded = load_corpus(path)
        owner = loaded[0].context.principal_stack.direct_owner
        assert owner is not None
        assert owner.id != "owner-1"
        assert owner.id.startswith("principal_")


@pytest.fixture(autouse=True)
def _quiet_warnings():
    yield
