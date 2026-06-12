from __future__ import annotations

from app.runtime.session import SessionContext


def test_context_engine_fences_context_and_records_reference_artifact():
    from app.runtime.context_engine import DefaultContextEngine

    session_context = SessionContext(session_id="s-ctx", source="test")
    engine = DefaultContextEngine()

    fenced = engine.inject(
        session_context,
        kind="memory_context",
        source="memory_provider:context",
        content="- user prefers concise answers",
    )

    assert '<context_block kind="memory_context" source="memory_provider:context">' in fenced
    assert "- user prefers concise answers" in fenced
    artifacts = session_context.metadata["context_artifacts"]
    assert artifacts[0]["kind"] == "memory_context"
    assert artifacts[0]["source"] == "memory_provider:context"
    assert artifacts[0]["char_count"] == len("- user prefers concise answers")
    assert "content_hash" in artifacts[0]
    assert "content" not in artifacts[0]


def test_context_engine_ignores_blank_context_blocks():
    from app.runtime.context_engine import DefaultContextEngine

    session_context = SessionContext(session_id="s-ctx", source="test")
    engine = DefaultContextEngine()

    assert engine.inject(session_context, kind="memory_context", source="memory", content="  ") == ""
    assert session_context.metadata.get("context_artifacts") is None
