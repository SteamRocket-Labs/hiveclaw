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


def test_semantic_memory_lanes_are_llm_primary_with_observable_fallbacks() -> None:
    extract_agent = _read("app/services/extract_agent.py")
    learning_brain = _read("app/services/fast_reflection_learning_brain.py")

    assert "LLM primary" in extract_agent
    assert "Pattern fallback" in extract_agent
    assert "mechanical_fallback" in _read("app/services/fast_reflection_service.py")
    assert "learning brain" in learning_brain.lower()
    assert "Do not write memory, files, skills, workflows" in learning_brain
