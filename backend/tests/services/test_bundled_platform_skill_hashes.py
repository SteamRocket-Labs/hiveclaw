"""Bundled platform hashes reuse parsed package data within a process."""

import yaml

from app.services.skill_seeder import builtin_skill_definitions
from app.services.skill_source_receipts import _bundled_platform_skill_hashes


def test_bundled_platform_hashes_reuse_parsed_packs(monkeypatch, tmp_path):
    safe_load = yaml.safe_load
    parsed = 0

    def count_parse(stream):
        nonlocal parsed
        parsed += 1
        return safe_load(stream)

    monkeypatch.setattr(yaml, "safe_load", count_parse)
    path = "skills/office-productivity/SKILL.md"
    first = _bundled_platform_skill_hashes()
    first_count = parsed
    assert path in first
    assert _bundled_platform_skill_hashes() == first
    assert parsed == first_count
    assert _bundled_platform_skill_hashes(source_path=path) == {path: first[path]}
    assert parsed == first_count

    first.pop(path)
    assert path in _bundled_platform_skill_hashes()

    pack = tmp_path / "sample_pack"
    skill = pack / "skills" / "sample-skill"
    skill.mkdir(parents=True)
    (pack / "pack.yaml").write_text(
        'name: sample_pack\nversion: "1.0.0"\nskills: [skills/sample-skill]\n',
        encoding="utf-8",
    )
    (skill / "SKILL.md").write_text("---\nname: Sample Skill\n---\n", encoding="utf-8")
    definitions = builtin_skill_definitions(pack_dirs=(tmp_path,))
    assert parsed > first_count
    assert any(entry["folder_name"] == "sample-skill" for entry in definitions)
