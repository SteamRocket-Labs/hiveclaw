"""Phase 10: T0 privacy frontline + form lint tests."""

from __future__ import annotations

import uuid
from pathlib import Path
from unittest.mock import patch

import pytest

from app.services.t0_logger import write_t0_log


@pytest.fixture
def agent_id() -> uuid.UUID:
    return uuid.uuid4()


@pytest.fixture
def tmp_agent_dir(tmp_path: Path, agent_id: uuid.UUID) -> Path:
    (tmp_path / str(agent_id) / "logs").mkdir(parents=True)
    return tmp_path


def _write(agent_id: uuid.UUID, tmp_agent_dir: Path, **kwargs) -> Path:
    with patch("app.services.t0_logger.get_settings") as mock_settings:
        mock_settings.return_value.AGENT_DATA_DIR = str(tmp_agent_dir)
        path = write_t0_log(agent_id, **kwargs)
    assert path is not None
    return path


class TestT0PrivacyGate:
    def test_pl4_credential_is_masked_in_t0_chat(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        secret = "sk-" + "A" * 24  # synthetic; assembled at runtime to dodge static scanners
        path = _write(
            agent_id,
            tmp_agent_dir,
            behavior_type="chat",
            messages=[
                {"role": "user", "content": f"rotate api_key={secret} tomorrow"},
                {"role": "assistant", "content": "noted"},
            ],
            metadata={"source": "web", "session_id": "s-1"},
        )
        content = path.read_text(encoding="utf-8")
        assert secret not in content
        assert "<Credential_" in content
        assert "t0_sensitivity: PL4_credential" in content

    def test_semantic_keyword_does_not_mechanically_mark_pl3(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        path = _write(
            agent_id,
            tmp_agent_dir,
            behavior_type="chat",
            messages=[
                {"role": "user", "content": "What's the planned salary band for Q3?"},
                {"role": "assistant", "content": "I will draft a memo."},
            ],
            metadata={"source": "web", "session_id": "s-2"},
        )
        content = path.read_text(encoding="utf-8")
        assert "t0_sensitivity: PL1_public" in content
        assert "salary" in content.lower()

    def test_pl1_chat_marks_public(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        path = _write(
            agent_id,
            tmp_agent_dir,
            behavior_type="chat",
            messages=[
                {"role": "user", "content": "standup at 1000"},
                {"role": "assistant", "content": "acknowledged"},
            ],
            metadata={"source": "web", "session_id": "s-3"},
        )
        content = path.read_text(encoding="utf-8")
        assert "t0_sensitivity: PL1_public" in content

    def test_form_lint_warnings_attached(self, agent_id: uuid.UUID, tmp_agent_dir: Path) -> None:
        path = _write(
            agent_id,
            tmp_agent_dir,
            behavior_type="chat",
            messages=[
                {"role": "user", "content": "他昨天说要下周再聊"},
                {"role": "assistant", "content": "ok"},
            ],
            metadata={"source": "web", "session_id": "s-4"},
        )
        content = path.read_text(encoding="utf-8")
        assert "t0_form_warnings:" in content
        assert "ambiguous_pronoun" in content
        assert "relative_time" in content
