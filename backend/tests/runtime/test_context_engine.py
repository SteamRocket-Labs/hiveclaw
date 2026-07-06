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


def test_context_engine_records_prompt_manifest_selected_contexts_as_artifacts():
    from app.runtime.context_engine import record_prompt_manifest_context_artifacts

    session_context = SessionContext(session_id="s-ctx", source="test")
    prompt_manifest = {
        "selected_contexts": [
            {
                "id": "ctx:system:frozen_prefix",
                "kind": "system_prompt",
                "name": "frozen_prefix",
                "source_ref": "provider.system_prompt",
                "source_hash": "abc123",
                "chars": 42,
                "tokens": 12,
                "why_selected": "frozen_prefix_rendered",
                "cacheability": "prompt_cache_frozen",
            },
            {
                "id": "ctx:skill:skill_catalog",
                "kind": "skills",
                "name": "skill_catalog",
                "source_ref": "skills.catalog",
                "source_hash": "def456",
                "chars": 18,
                "tokens": 5,
                "why_selected": "skill_catalog_or_active_skills_present",
                "cacheability": "dynamic",
            },
        ],
        "suppressed_contexts": [
            {
                "id": "ctx:permissions:permissions_context",
                "name": "permissions_context",
                "source_ref": "runtime.context_engine.permissions",
            }
        ],
    }

    record_prompt_manifest_context_artifacts(session_context, prompt_manifest)

    artifacts = session_context.metadata["context_artifacts"]
    assert [item["kind"] for item in artifacts] == ["frozen_prefix", "skill_catalog"]
    assert artifacts[0]["source"] == "provider.system_prompt"
    assert artifacts[0]["content_hash"] == "abc123"
    assert artifacts[0]["candidate_id"] == "ctx:system:frozen_prefix"
    assert artifacts[0]["selection_reason"] == "frozen_prefix_rendered"
    assert artifacts[0]["token_count"] == 12
    assert "content" not in artifacts[0]
