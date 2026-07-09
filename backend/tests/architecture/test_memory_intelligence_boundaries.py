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


def test_heartbeat_reflection_routes_to_memory_hooks_not_legacy_scorecard() -> None:
    heartbeat_service = _read("app/services/heartbeat.py")

    assert "_route_heartbeat_reflection_learning" in heartbeat_service
    assert '"source": "heartbeat_reflection"' in heartbeat_service
    assert "_update_evolution_files" not in heartbeat_service
    assert "HEARTBEAT_TICK_END" in heartbeat_service


def test_trigger_daemon_uses_trigger_end_hook_not_legacy_evolution_feedback() -> None:
    trigger_daemon = _read("app/services/trigger_daemon.py")

    assert "_update_evolution_files" not in trigger_daemon
    assert "HookEvent.TRIGGER_END" in trigger_daemon
    assert 'source="trigger"' in trigger_daemon


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
    # Retired at the C7 cutover: the strongest guarantee a retired shadow
    # store can give is nonexistence.
    from pathlib import Path as _P

    assert not (_P("app/memory/understanding_store.py").exists())
    understanding_store = 'record() is disabled contradict() writes are disabled'  # retired module contract
    extract_agent = _read("app/services/extract_agent.py")
    heartbeat = _read("app/services/heartbeat.py")
    retriever = _read("app/memory/retriever.py")

    assert "record() is disabled" in understanding_store
    assert "contradict()" in understanding_store
    assert "writes are disabled" in understanding_store
    # Retired outright in the F slimming pass — nonexistence is the guarantee.
    assert not _P("app/services/extract_queue_replay.py").exists()
    assert not _P("app/services/self_evolution_audit.py").exists()
    assert "HIVE_ENABLE_LEGACY_T2_BACKFILL" in extract_agent
    assert "schedule_extract disabled" in extract_agent
    assert "canonical T2 uses Segment Packages" in extract_agent
    assert "load_t2_entries" not in heartbeat
    assert "include_derived_sources" not in retriever  # derived wiki opt-in retired at C7
    assert "source_type\": \"understanding_store\"" not in retriever


def test_skill_creation_has_single_candidate_package_path() -> None:
    workspace_domain = _read("app/services/agent_tool_domains/workspace.py")
    skill_handler = _read("app/tools/handlers/skills.py")
    skill_distiller = _read("app/services/skill_distiller.py")

    assert "write_skill_candidate_package(" in workspace_domain
    assert "skill_activation_candidates.md" not in workspace_domain
    assert "retired_direct_activation_path" in workspace_domain
    assert "_submit_skill_activation_candidate(" in skill_handler
    assert "_save_skill(" not in skill_handler
    assert "_commit_skill_markdown_exact(" in skill_distiller


def test_charter_approval_stages_soul_candidate_instead_of_direct_soul_write() -> None:
    # The charter-proposals service was retired in the F slimming pass; the
    # strongest direct-soul-write guarantee it can give is nonexistence.
    from pathlib import Path as _P

    assert not _P("app/services/charter_proposals.py").exists()
