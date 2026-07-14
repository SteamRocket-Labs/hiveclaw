from __future__ import annotations

import ast
from pathlib import Path


def test_final_answer_hot_path_has_no_posthoc_semantic_rewriter() -> None:
    """Tool evidence is observable fact, never a licence to rewrite model speech."""

    kernel_root = Path(__file__).resolve().parents[2] / "app" / "kernel"
    orchestrator_source = (kernel_root / "turn_orchestrator.py").read_text(encoding="utf-8")
    engine_source = (kernel_root / "engine.py").read_text(encoding="utf-8")

    assert "verify_final_answer_tool_evidence" not in orchestrator_source
    assert "verify_final_answer_tool_evidence" not in engine_source
    assert "本轮没有实际工具调用记录" not in orchestrator_source + engine_source
    assert not (kernel_root / "final_answer_evidence.py").exists()


def test_tool_evidence_ledger_is_not_imported_as_a_final_answer_gate() -> None:
    kernel_root = Path(__file__).resolve().parents[2] / "app" / "kernel"
    tree = ast.parse((kernel_root / "turn_orchestrator.py").read_text(encoding="utf-8"))

    final_answer_calls = [
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and "final_answer" in node.func.id
    ]

    assert final_answer_calls == []
