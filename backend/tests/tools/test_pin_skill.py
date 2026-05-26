from __future__ import annotations

from pathlib import Path


def test_pin_skill_pins_and_unpins(tmp_path: Path) -> None:
    from app.services.skill_curator import load_skill_usage, mark_skill_created
    from app.tools.handlers.skills import pin_skill

    mark_skill_created(tmp_path, "deploy-checklist", created_by="agent")

    out = pin_skill(workspace=tmp_path, arguments={"skill": "deploy-checklist", "pinned": True})
    assert "deploy-checklist" in out
    assert load_skill_usage(tmp_path)["deploy-checklist"]["pinned"] is True

    pin_skill(workspace=tmp_path, arguments={"skill": "deploy-checklist", "pinned": False})
    assert load_skill_usage(tmp_path)["deploy-checklist"]["pinned"] is False


def test_pin_skill_defaults_to_pinning(tmp_path: Path) -> None:
    from app.services.skill_curator import load_skill_usage
    from app.tools.handlers.skills import pin_skill

    pin_skill(workspace=tmp_path, arguments={"skill": "report-gen"})
    assert load_skill_usage(tmp_path)["report-gen"]["pinned"] is True


def test_pin_skill_requires_slug(tmp_path: Path) -> None:
    from app.tools.handlers.skills import pin_skill

    out = pin_skill(workspace=tmp_path, arguments={})
    assert "required" in out.lower()
