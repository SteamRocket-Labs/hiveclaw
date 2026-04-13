from __future__ import annotations

from pathlib import Path


def _python_sources() -> dict[str, str]:
    project_root = Path(__file__).resolve().parents[3]
    app_root = project_root / "backend/app"
    return {
        str(path.relative_to(project_root)): path.read_text(encoding="utf-8")
        for path in app_root.rglob("*.py")
    }


def test_prompt_memory_trunk_entrypoints_are_single_path() -> None:
    sources = _python_sources()

    build_agent_context_refs = sorted(path for path, source in sources.items() if "build_agent_context(" in source)
    build_agent_runtime_context_refs = sorted(
        path for path, source in sources.items() if "build_agent_runtime_context(" in source
    )
    build_memory_snapshot_refs = sorted(path for path, source in sources.items() if "build_memory_snapshot(" in source)
    build_runtime_prompt_refs = sorted(path for path, source in sources.items() if "build_runtime_prompt(" in source)
    on_conversation_start_refs = sorted(path for path, source in sources.items() if "on_conversation_start(" in source)
    on_conversation_end_refs = sorted(path for path, source in sources.items() if "on_conversation_end(" in source)

    assert build_agent_context_refs == [
        "backend/app/runtime/invoker.py",
        "backend/app/services/agent_context.py",
    ]
    assert build_agent_runtime_context_refs == [
        "backend/app/runtime/invoker.py",
        "backend/app/services/agent_context.py",
    ]
    assert build_memory_snapshot_refs == [
        "backend/app/runtime/invoker.py",
        "backend/app/services/memory_service.py",
    ]
    assert build_runtime_prompt_refs == []
    assert on_conversation_start_refs == []
    assert on_conversation_end_refs == []

    invoker_source = sources["backend/app/runtime/invoker.py"]
    websocket_source = sources["backend/app/api/websocket.py"]
    kernel_contracts_source = sources["backend/app/kernel/contracts.py"]
    agent_context_source = sources["backend/app/services/agent_context.py"]
    memory_api_source = sources["backend/app/api/memory.py"]
    memory_store_source = sources["backend/app/memory/store.py"]
    prompt_builder_source = sources["backend/app/runtime/prompt_builder.py"]

    assert "if request.memory_context:" not in invoker_source
    assert "memory_context=request.memory_context" not in invoker_source
    assert 'memory_context: str = ""' not in websocket_source
    assert "memory_context=memory_context" not in websocket_source
    assert 'memory_context: str = ""' not in kernel_contracts_source
    assert "include_memory_file" not in agent_context_source
    assert "include_focus" not in agent_context_source
    assert "include_memory_file=False" not in invoker_source
    assert "include_focus=False" not in invoker_source
    assert "def _compute_system_prompt_budget(" not in prompt_builder_source
    assert "def _render_active_packs(" not in prompt_builder_source
    assert '"memory.json"' not in memory_api_source
    assert "PersistentMemoryStore" in memory_api_source
    assert "def _write_legacy_json(" not in memory_store_source
