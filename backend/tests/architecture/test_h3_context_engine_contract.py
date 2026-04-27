from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
APP_ROOT = PROJECT_ROOT / "app"


def test_context_engine_contract_exists() -> None:
    source = (APP_ROOT / "runtime" / "context_engine.py").read_text(encoding="utf-8")

    assert "class ContextEngine" in source
    assert "class MemoryProvider" in source
    assert "class DefaultContextEngine" in source
    assert "def inject(" in source
    assert "context_artifacts" in source


def test_invoker_routes_memory_and_knowledge_through_context_engine() -> None:
    source = (APP_ROOT / "runtime" / "invoker.py").read_text(encoding="utf-8")

    assert "DefaultContextEngine" in source
    assert "_context_engine().inject(" in source
    assert "memory_provider:snapshot" in source
    assert "memory_provider:recall" in source
    assert "knowledge_provider:relevant" in source
