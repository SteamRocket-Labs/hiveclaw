from __future__ import annotations

import logging
from pathlib import Path

from app.skills.loader import WorkspaceSkillLoader
from app.skills.parser import SkillParser
from app.skills.registry import SkillRegistry


def test_parser_supports_yaml_frontmatter_and_flat_hive_metadata(tmp_path):
    skill_path = tmp_path / "SKILL.md"
    content = """---
name: Research Deep Dive
description: |
  Produce a source-attributed research report with evidence review,
  quality checks, risk assessment, contradiction handling, reusable artifacts,
  and explicit notes about uncertainty.
license: Proprietary
compatibility: Hive >= 1.8.0
allowed-tools: web_search web_fetch firecrawl_fetch
tools:
  - web_search
  - firecrawl_fetch
packs:
  - web_pack
metadata:
  hive.version: "1.0.0"
  hive.pack: web_pack
  hive.requires_skills: "industry-research,source-ledger-audit"
  hive.locale: cloud
  hive.invocation: both
  hive.cost_tier: high
  hive.estimated_runtime_minutes: "30"
  hive.output_artifacts: "reports/{topic}.md,reports/{topic}.json"
  hive.security_zone: restricted
is_system: false
---
# Research Deep Dive

Use primary sources first.
"""

    parsed = SkillParser().parse_content(
        content,
        path=skill_path,
        relative_path="skills/research/SKILL.md",
        default_name="research",
    )

    assert parsed.metadata.name == "Research Deep Dive"
    assert "source-attributed research report" in parsed.metadata.description
    assert len(parsed.metadata.description) > 120
    assert parsed.metadata.license == "Proprietary"
    assert parsed.metadata.compatibility == "Hive >= 1.8.0"
    assert parsed.metadata.allowed_tools == (
        "web_search",
        "web_fetch",
        "firecrawl_fetch",
    )
    assert parsed.metadata.declared_tools == ("web_search", "firecrawl_fetch")
    assert parsed.metadata.declared_packs == ("web_pack",)
    assert parsed.metadata.version == "1.0.0"
    assert parsed.metadata.pack == "web_pack"
    assert parsed.metadata.requires_skills == ("industry-research", "source-ledger-audit")
    assert parsed.metadata.locale == "cloud"
    assert parsed.metadata.invocation == "both"
    assert parsed.metadata.cost_tier == "high"
    assert parsed.metadata.estimated_runtime_minutes == 30
    assert parsed.metadata.output_artifacts == ("reports/{topic}.md", "reports/{topic}.json")
    assert parsed.metadata.security_zone == "restricted"
    assert parsed.body.startswith("# Research Deep Dive")


def test_parser_supports_nested_hive_metadata_fallback(tmp_path):
    content = """---
name: Source Audit
description: Audit source coverage.
metadata:
  hive:
    version: "0.2.0"
    pack: web_pack
    requires_skills:
      - industry-research
      - source-ledger-audit
    estimated_runtime_minutes: 15
---
# Source Audit
"""

    parsed = SkillParser().parse_content(
        content,
        path=tmp_path / "SKILL.md",
        relative_path="skills/source-audit/SKILL.md",
    )

    assert parsed.metadata.version == "0.2.0"
    assert parsed.metadata.pack == "web_pack"
    assert parsed.metadata.requires_skills == ("industry-research", "source-ledger-audit")
    assert parsed.metadata.estimated_runtime_minutes == 15


def test_parser_is_tolerant_of_invalid_yaml_frontmatter(tmp_path):
    content = """---
name: Broken
metadata:
  hive.version: [unterminated
---
# Broken

Body survives.
"""

    parsed = SkillParser().parse_content(
        content,
        path=tmp_path / "broken.md",
        relative_path="skills/broken.md",
        default_name="broken-skill",
    )

    assert parsed.metadata.name == "broken skill"
    assert parsed.body.startswith("# Broken")
    assert "Body survives." in parsed.body


def test_parser_logs_invalid_yaml_frontmatter_only_once_per_skill_path(tmp_path, caplog):
    content = """---
name: Broken
metadata:
  hive.version: [unterminated
---
# Broken
"""

    parser = SkillParser()

    with caplog.at_level(logging.WARNING):
        parser.parse_content(
            content,
            path=tmp_path / "duplicate-warning.md",
            relative_path="skills/duplicate-warning.md",
            default_name="duplicate-warning",
        )
        parser.parse_content(
            content,
            path=tmp_path / "duplicate-warning.md",
            relative_path="skills/duplicate-warning.md",
            default_name="duplicate-warning",
        )

    messages = [
        record.message
        for record in caplog.records
        if "Skill skills/duplicate-warning.md has invalid YAML frontmatter:" in record.message
    ]
    assert len(messages) == 1


def test_loader_lists_and_reads_folder_skill_resources(tmp_path):
    workspace = tmp_path / "agent"
    skill_dir = workspace / "skills" / "research"
    (skill_dir / "references").mkdir(parents=True)
    (skill_dir / "scripts").mkdir()
    (skill_dir / "templates").mkdir()
    (skill_dir / "evals").mkdir()
    (skill_dir / "SKILL.md").write_text(
        "---\nname: Research\ndescription: Research analysis\n---\n# Research\n",
        encoding="utf-8",
    )
    (skill_dir / "references" / "sources.md").write_text("Source notes", encoding="utf-8")
    (skill_dir / "scripts" / "compile.py").write_text("print('ok')\n", encoding="utf-8")
    (skill_dir / "templates" / "memo.md").write_text("# Memo\n", encoding="utf-8")
    (skill_dir / "evals" / "eval.yaml").write_text("cases: []\n", encoding="utf-8")

    loader = WorkspaceSkillLoader()

    resources = loader.list_resources(workspace, "Research")
    assert resources == (
        "evals/eval.yaml",
        "references/sources.md",
        "scripts/compile.py",
        "templates/memo.md",
    )
    assert loader.read_resource(workspace, "Research", "references/sources.md") == "Source notes"
    assert loader.read_resource(workspace, "Research", "templates/memo.md") == "# Memo\n"


def test_registry_loads_required_skill_bodies_before_primary(tmp_path):
    def parsed(name: str, body: str, requires: tuple[str, ...] = ()):
        content = "\n".join(
            [
                "---",
                f"name: {name}",
                "description: test",
                "metadata:",
                f"  hive.requires_skills: {','.join(requires)}",
                "---",
                body,
            ]
        )
        return SkillParser().parse_content(
            content,
            path=Path(f"{name}.md"),
            relative_path=f"skills/{name}.md",
            default_name=name,
        )

    registry = SkillRegistry()
    registry.register(parsed("industry-research", "# Industry\n"))
    registry.register(parsed("dcf-valuation", "# DCF\n"))
    registry.register(parsed("secondary-equity-deep-dive", "# Main\n", ("industry-research", "dcf-valuation")))

    body = registry.load_body_with_dependencies("secondary-equity-deep-dive")

    assert body.index("# Industry") < body.index("# DCF") < body.index("# Main")
    assert "Missing required skill" not in body
