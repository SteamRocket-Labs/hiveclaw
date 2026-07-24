from __future__ import annotations

from pathlib import Path


INDEXER = Path(__file__).resolve().parents[2] / "app" / "services" / "company_knowledge_indexer.py"


def test_company_knowledge_outbox_consumer_recovers_ontology_projection_work() -> None:
    source = INDEXER.read_text(encoding="utf-8")

    assert "project_ontology_release" in source
    assert "tombstone_ontology_release" in source
    assert "CompanyOntologyRelease" in source
    assert "rebuild_projection" in source
    assert "release_hash" in source
