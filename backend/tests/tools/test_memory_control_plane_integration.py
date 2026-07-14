from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest


async def _safe_memory_threat_classifier(**_kwargs):
    from app.memory.write_gate import MemoryThreatAssessment

    return MemoryThreatAssessment(
        rejected=False,
        labels=[],
        method="llm_classifier",
        confidence=0.99,
        rationale="No memory-write threat detected.",
    )


@pytest.mark.asyncio
async def test_save_memory_rejects_pl4_credential(tmp_path: Path) -> None:
    from app.tools.handlers.memory import save_memory

    agent_id = uuid.uuid4()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
        result = await save_memory(
            agent_id,
            {
                "content": "Owner Alice shared api_key=sk-1234567890abcdefghijklmnop for setup.",
                "category": "reference",
            },
        )

    assert result.startswith("[Rejected]")
    assert "PL4_credential" in result
    assert not (tmp_path / str(agent_id) / "memory" / "knowledge.md").exists()


@pytest.mark.asyncio
async def test_save_memory_masks_pii_before_explicit_overlay_write(tmp_path: Path) -> None:
    from app.tools.handlers.memory import save_memory

    agent_id = uuid.uuid4()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
        mp.setattr(
            "app.memory.write_gate.classify_memory_write_threat_with_llm",
            _safe_memory_threat_classifier,
        )
        result = await save_memory(
            agent_id,
            {
                "content": "Owner Alice email is alice@example.com for vendor escalation.",
                "category": "user",
            },
        )

    user_path = tmp_path / str(agent_id) / "memory" / "user.md"
    overlay_path = tmp_path / str(agent_id) / "memory" / "explicit" / "MEMORY.md"
    body = overlay_path.read_text(encoding="utf-8")
    entry_body = next((tmp_path / str(agent_id) / "memory" / "explicit" / "entries").glob("*.md")).read_text(
        encoding="utf-8"
    )
    assert result.startswith("Saved to explicit memory overlay")
    assert not user_path.exists()
    assert "alice@example.com" not in body
    assert "alice@example.com" not in entry_body
    assert "&lt;Email_1&gt;" in entry_body
    assert "&lt;Email_1&gt;" in body


@pytest.mark.asyncio
async def test_save_memory_holds_form_signal_for_model_revision(tmp_path: Path) -> None:
    from app.memory.write_gate import MemoryThreatAssessment
    from app.tools.handlers.memory import save_memory

    agent_id = uuid.uuid4()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))

        async def safe_classifier(**_kwargs):
            return MemoryThreatAssessment(
                rejected=False,
                labels=[],
                method="llm_classifier",
                confidence=0.98,
                rationale="No threat content.",
            )

        mp.setattr("app.memory.write_gate.classify_memory_write_threat_with_llm", safe_classifier)
        result = await save_memory(
            agent_id,
            {
                "content": "He should handle this tomorrow.",
                "category": "strategy",
            },
        )

    assert result.startswith("[Held]")
    assert "form_review_required" in result
