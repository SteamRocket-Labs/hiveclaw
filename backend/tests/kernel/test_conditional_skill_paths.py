from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4


def test_activate_conditional_skills_for_paths_tracks_matching_skill(tmp_path):
    from app.kernel.contracts import InvocationRequest
    from app.kernel.engine import _activate_conditional_skills_for_paths
    from app.runtime.session import SessionContext

    skill_dir = tmp_path / "skills" / "python"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        """---
name: Python
description: Python project work.
paths:
  - backend/**/*.py
---
# Python
Use Python conventions.
""",
        encoding="utf-8",
    )
    session = SessionContext(session_id="s-paths")
    request = InvocationRequest(
        model=SimpleNamespace(provider="openai", model="gpt-4.1"),
        messages=[],
        agent_name="Agent",
        role_description="",
        agent_id=uuid4(),
        session_context=session,
    )

    activated = _activate_conditional_skills_for_paths(
        request,
        ["backend/app/main.py"],
        workspace=tmp_path,
    )

    assert activated == ["Python"]
    assert session.active_skills == ["Python"]
    assert session.metadata["conditional_skill_activations"][0]["skill_name"] == "Python"
    assert session.metadata["conditional_skill_activations"][0]["matched_path"] == "backend/app/main.py"
