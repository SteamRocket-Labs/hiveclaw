from __future__ import annotations

from pathlib import Path

from app.skills.parser import SkillParser
from app.tools.packs import pack_for_name


REPO_ROOT = Path(__file__).resolve().parents[3]
PACK_GUIDE_PATHS = {
    "web_pack": REPO_ROOT / "backend" / "app" / "templates" / "system_skills" / "web-research" / "SKILL.md",
    "feishu_pack": REPO_ROOT / "backend" / "app" / "templates" / "system_skills" / "feishu-integration" / "SKILL.md",
    "plaza_pack": REPO_ROOT / "backend" / "app" / "templates" / "system_skills" / "plaza-guide" / "SKILL.md",
    "email_pack": REPO_ROOT / "backend" / "app" / "templates" / "system_skills" / "email-guide" / "SKILL.md",
    "mcp_admin_pack": REPO_ROOT / "backend" / "agent_template" / "skills" / "MCP_INSTALLER.md",
}


def test_every_static_pack_has_a_matching_skill_guide():
    expected_pack_names = {"web_pack", "feishu_pack", "plaza_pack", "email_pack", "mcp_admin_pack"}
    assert set(PACK_GUIDE_PATHS) == expected_pack_names

    missing: list[str] = []
    for pack_name, path in PACK_GUIDE_PATHS.items():
        pack = pack_for_name(pack_name)
        assert pack is not None
        if not path.exists():
            missing.append(pack_name)
            continue
        content = path.read_text(encoding="utf-8")
        for tool_name in pack.tools:
            assert tool_name in content, f"{pack_name} guide missing {tool_name}"

    assert not missing


def test_mcp_pack_guide_is_parseable_skill_markdown():
    skill_path = PACK_GUIDE_PATHS["mcp_admin_pack"]
    content = skill_path.read_text(encoding="utf-8")

    parsed = SkillParser().parse_content(
        content,
        path=skill_path,
        relative_path="skills/mcp-installer/SKILL.md",
        default_name="mcp-installer",
    )

    assert parsed.metadata.name == "MCP Tool Installer"
    assert parsed.metadata.description
    assert set(parsed.metadata.declared_tools) == {
        "discover_resources",
        "import_mcp_server",
        "list_mcp_resources",
        "read_mcp_resource",
        "call_mcp_tool",
    }
