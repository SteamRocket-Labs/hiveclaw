from __future__ import annotations

from uuid import uuid4

import pytest


@pytest.mark.asyncio
async def test_stage_mcp_prompt_as_skill_review_does_not_install_active_skill(tmp_path, monkeypatch):
    import app.services.mcp_prompt_trust as prompt_trust

    async def fake_stage_for_workspace(*, workspace, source_uri, folder_name, files, source_format):
        assert workspace == tmp_path
        assert source_uri.startswith("mcp_prompt:docs:review:agent:")
        assert folder_name == "mcp-docs-review"
        assert files[0]["path"] == "SKILL.md"
        assert source_format == "mcp_prompt"
        return {
            "status": "review_required",
            "folder_name": folder_name,
            "files_written": 0,
            "files": [],
            "review_id": "review-mcp-prompt",
            "skill_guard": {"allowed": True},
            "source_uri": source_uri,
        }

    monkeypatch.setattr(prompt_trust, "stage_external_skill_package_review_for_agent_workspace", fake_stage_for_workspace)

    result = await prompt_trust.stage_mcp_prompt_as_skill_review(
        workspace=tmp_path,
        agent_id=uuid4(),
        server_name="docs",
        prompt_name="review",
        prompt_text="Read docs carefully.",
    )

    assert result["status"] == "review_required"
    assert result["review_id"] == "review-mcp-prompt"
    assert not (tmp_path / "skills" / "docs-review" / "SKILL.md").exists()
