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
    assert build_runtime_prompt_refs == [
        "backend/app/runtime/prompt_builder.py",
    ]
    assert on_conversation_start_refs == []
