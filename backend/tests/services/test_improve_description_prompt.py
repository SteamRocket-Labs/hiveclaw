from __future__ import annotations

from pathlib import Path


def test_vendor_specific_description_rewriter_is_not_packaged() -> None:
    from app.services.skill_creator_content import get_skill_creator_files

    app_root = Path(__file__).resolve().parents[2] / "app"
    deleted_helper = app_root / "services/skill_creator_files/scripts__improve_description.py"

    assert not deleted_helper.exists()
    assert "scripts/improve_description.py" not in {item["path"] for item in get_skill_creator_files()}


def test_skill_creator_leaves_description_semantics_to_the_current_agent() -> None:
    from app.services.skill_creator_content import SKILL_CREATOR_MD

    assert "current Agent must inspect the complete" in SKILL_CREATOR_MD
    assert "then author the revised\ndescription itself" in SKILL_CREATOR_MD
    assert "must not route this semantic decision through a privileged\nvendor-specific model" in SKILL_CREATOR_MD
