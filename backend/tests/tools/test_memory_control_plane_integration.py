from __future__ import annotations

import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest


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
async def test_save_memory_rejects_form_contract_violation(tmp_path: Path) -> None:
    from app.tools.handlers.memory import save_memory

    agent_id = uuid.uuid4()

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr("app.config.get_settings", lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)))
        result = await save_memory(
            agent_id,
            {
                "content": "He should handle this tomorrow.",
                "category": "strategy",
            },
        )

    assert result.startswith("[Rejected]")
    assert "Form Contract violation" in result
