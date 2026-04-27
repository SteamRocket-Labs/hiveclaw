from __future__ import annotations

from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = BACKEND_ROOT / "app"


def test_memory_and_objective_layers_stay_separate() -> None:
    memory_service = (APP_ROOT / "services/memory_service.py").read_text(encoding="utf-8")
    objective_intake = (APP_ROOT / "services/objective_intake.py").read_text(encoding="utf-8")

    assert "AgentObjective" not in memory_service
    assert "objective_intake" not in memory_service
    assert "memory_service" not in objective_intake


def test_runtime_invoker_is_the_prompt_memory_ingress() -> None:
    invoker = (APP_ROOT / "runtime/invoker.py").read_text(encoding="utf-8")
    websocket = (APP_ROOT / "api/websocket.py").read_text(encoding="utf-8")
    kernel_contracts = (APP_ROOT / "kernel/contracts.py").read_text(encoding="utf-8")

    assert "build_memory_snapshot" in invoker
    assert "resolve_memory_context=" in invoker
    assert "memory_context=memory_context" not in websocket
    assert 'memory_context: str = ""' in kernel_contracts


def test_context_compaction_has_reference_tracking_fields() -> None:
    session = (APP_ROOT / "runtime/session.py").read_text(encoding="utf-8")

    assert "recent_files" in session
    assert "recent_writes" in session
    assert "recent_tool_outcomes" in session
    assert "pending_items" in session
