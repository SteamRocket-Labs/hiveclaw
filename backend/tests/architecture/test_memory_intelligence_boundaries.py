from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def _read(rel: str) -> str:
    return (ROOT / rel).read_text(encoding="utf-8")


def test_evolution_directory_is_audit_not_semantic_memory_source() -> None:
    heartbeat_template = _read("app/templates/HEARTBEAT.md")
    dream_template = _read("app/templates/DREAM.md")
    heartbeat_service = _read("app/services/heartbeat.py")

    combined = "\n".join([heartbeat_template, dream_template, heartbeat_service])

    assert "evolution/lineage.md stores policy-level learning" not in combined
    assert "semantic memory body" in combined
    assert "direct writes are refused" in combined
    assert "Do not write `memory/explicit/**` directly" in combined


def test_no_model_reflection_is_reduced_to_scorecard_only() -> None:
    heartbeat_service = _read("app/services/heartbeat.py")

    assert "_route_heartbeat_reflection_learning" in heartbeat_service
    assert '"source": "heartbeat_reflection"' in heartbeat_service
    assert "_update_evolution_files" in heartbeat_service
    assert heartbeat_service.index("_route_heartbeat_reflection_learning") < heartbeat_service.rindex(
        "_update_evolution_files"
    )


def test_semantic_memory_lanes_hold_when_llm_semantics_are_unavailable() -> None:
    extract_agent = _read("app/services/extract_agent.py")
    learning_brain = _read("app/services/fast_reflection_learning_brain.py")
    fast_reflection = _read("app/services/fast_reflection_service.py")

    assert "LLM primary" in extract_agent
    assert "legacy compatibility" in extract_agent
    assert "mechanical_fallback" not in fast_reflection
    assert "degraded_session_only" not in fast_reflection
    assert "learning brain" in learning_brain.lower()
    assert "Do not write memory, files, skills, workflows" in learning_brain


def test_no_second_semantic_truth_sources_are_writable() -> None:
    understanding_store = _read("app/memory/understanding_store.py")
    extract_queue_replay = _read("app/services/extract_queue_replay.py")
    extract_agent = _read("app/services/extract_agent.py")
    heartbeat = _read("app/services/heartbeat.py")
    self_evolution_audit = _read("app/services/self_evolution_audit.py")
    retriever = _read("app/memory/retriever.py")

    assert "record() is disabled" in understanding_store
    assert "contradict()" in understanding_store
    assert "writes are disabled" in understanding_store
    assert "HIVE_ENABLE_LEGACY_EXTRACT_REPLAY" in extract_queue_replay
    assert "HIVE_ENABLE_LEGACY_T2_BACKFILL" in extract_agent
    assert "load_t2_entries" not in heartbeat
    assert "load_t2_entries" not in self_evolution_audit
    assert "include_derived_sources" in retriever
    assert "source_type\": \"understanding_store\"" not in retriever
