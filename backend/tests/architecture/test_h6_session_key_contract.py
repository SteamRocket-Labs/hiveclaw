from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = PROJECT_ROOT / "app"


def test_session_key_contract_exists() -> None:
    source = (APP_ROOT / "runtime" / "session_key.py").read_text(encoding="utf-8")

    assert "class SessionKey" in source
    assert "build_session_key" in source
    assert "ensure_session_key" in source
    assert "runtime_task_id" in source


def test_invoker_normalizes_session_key_for_all_entrypoints() -> None:
    facade = (APP_ROOT / "runtime" / "invoker.py").read_text(encoding="utf-8")
    source = (APP_ROOT / "runtime" / "invocation_orchestrator.py").read_text(encoding="utf-8")

    assert "run_agent_invocation" in facade
    assert "ensure_session_key" in facade
    assert "_normalize_invocation_session_context(request)" in source
