from __future__ import annotations

from pathlib import Path


def test_is_seedable_skill_template_file_skips_pycache_and_pyc():
    from app.services.skill_seeder import _is_seedable_skill_template_file

    assert _is_seedable_skill_template_file(Path("scripts/render.py")) is True
    assert _is_seedable_skill_template_file(Path("scripts/__pycache__/render.cpython-313.pyc")) is False
    assert _is_seedable_skill_template_file(Path("scripts/render.cpython-313.pyc")) is False
