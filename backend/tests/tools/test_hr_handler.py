"""Tests for the HR tool handler — create_digital_employee registration and validation."""

from __future__ import annotations

from types import SimpleNamespace
from uuid import uuid4

import pytest


def test_create_digital_employee_is_registered():
    """The create_digital_employee tool must be collected by the tool collector."""
    from app.services.agent_tools import get_combined_openai_tools

    all_tools = get_combined_openai_tools()
    names = [t["function"]["name"] for t in all_tools]
    assert "create_digital_employee" in names
    assert "preview_agent_blueprint" in names


def test_create_digital_employee_schema_has_required_name():
    """The tool schema must require 'name' as the only required field."""
    from app.services.agent_tools import get_combined_openai_tools

    all_tools = get_combined_openai_tools()
    hr_tool = next(t for t in all_tools if t["function"]["name"] == "create_digital_employee")
    params = hr_tool["function"]["parameters"]

    assert params["required"] == ["name"]
    assert "name" in params["properties"]
    assert "role_description" in params["properties"]
    assert "personality" in params["properties"]
    assert "boundaries" in params["properties"]
    assert "primary_users" in params["properties"]
    assert "core_outputs" in params["properties"]
    assert "skill_names" in params["properties"]
    assert "external_skill_urls" in params["properties"]
    assert "permission_scope" in params["properties"]
    assert "company_charter" in params["properties"]
    assert "owner_agency_charter" in params["properties"]


def test_hr_tool_included_in_hr_tools_set():
    """_get_hr_tools should return the create_digital_employee tool."""
    from app.services.agent_tools import _get_hr_tools

    hr_tools = _get_hr_tools()
    names = [t["function"]["name"] for t in hr_tools]
    assert "create_digital_employee" in names
    assert "preview_agent_blueprint" in names
    assert "web_search" in names
    assert "firecrawl_fetch" in names
    assert "xcrawl_scrape" in names
    assert "execute_code" in names
    assert "discover_resources" in names
    assert "search_clawhub" in names
    assert len(hr_tools) == 8


def test_hr_tool_meta_has_correct_attributes():
    """The create_digital_employee tool must have correct category and adapter."""
    import importlib
    import app.tools.handlers.hr as hr_mod

    # Force re-registration in case a prior test called clear_registry()
    importlib.reload(hr_mod)

    from app.tools.decorator import get_all_registered_tools

    all_metas = get_all_registered_tools()
    meta, _fn = all_metas["create_digital_employee"]
    assert meta.governance == "sensitive"  # agent creation requires governance approval
    assert meta.category == "hr"
    assert meta.adapter == "request"
    assert meta.is_default is False

    preview_meta, _preview_fn = all_metas["preview_agent_blueprint"]
    assert preview_meta.governance == "safe"
    assert preview_meta.category == "hr"
    assert preview_meta.adapter == "request"
    assert preview_meta.read_only is True


def test_build_create_employee_result_is_structured_json():
    from app.tools.handlers.hr import _build_create_employee_result

    agent_id = "d20f09de-c0a8-4cc1-a033-0b982dd7a0a3"
    result = _build_create_employee_result(
        agent_id=agent_id,
        agent_name="Strategy Bot",
        features=["heartbeat=09:00-18:00 every 120min"],
        skills_dir="/tmp/agent/skills",
        creation_state="ready_with_warnings",
        warnings=["missing email config"],
        manual_steps=["Configure email before enabling triggers"],
    )

    assert '"status": "success"' in result
    assert f'"agent_id": "{agent_id}"' in result
    assert '"agent_name": "Strategy Bot"' in result
    assert '"creation_state": "ready_with_warnings"' in result
    assert '"warnings": ["missing email config"]' in result
    assert '"manual_steps": ["Configure email before enabling triggers"]' in result
    assert '"message": "Successfully created digital employee' in result


def test_build_blueprint_preview_payload_summarizes_ready_install_and_manual_steps():
    from app.tools.handlers.hr import _build_blueprint_preview_payload

    payload = _build_blueprint_preview_payload(
        {
            "name": "研究助理",
            "role_description": "追踪投融资与行业动态",
            "primary_users": ["投资团队", "研究团队"],
            "core_outputs": ["日报", "周报"],
            "personality": "严谨\n结论先行",
            "boundaries": "不捏造来源",
            "company_charter": {"goals": ["保护公司声誉"], "boundaries": ["不绕过合规审批"]},
            "owner_agency_charter": {
                "full_authority": ["准备投研草稿"],
                "confirm_first": ["对外发送日报"],
                "never_do": ["分享凭证"],
            },
            "skill_names": ["feishu-integration", "feishu-integration"],
            "mcp_server_ids": ["smithery/github", "smithery/github"],
            "clawhub_slugs": ["market-research-agent", "market-research-agent"],
            "focus_content": "先完成行业扫描",
            "heartbeat_topics": "AI\n半导体",
            "triggers": [{"name": "daily_report", "type": "cron", "config": {"expr": "0 9 * * *"}, "reason": "日报"}],
        }
    )

    assert payload["status"] == "preview"
    assert payload["blueprint"]["name"] == "研究助理"
    assert payload["blueprint"]["primary_users"] == ["投资团队", "研究团队"]
    assert payload["blueprint"]["core_outputs"] == ["日报", "周报"]
    assert payload["blueprint"]["company_charter"]["goals"] == ["保护公司声誉"]
    assert payload["blueprint"]["owner_agency_charter"]["confirm_first"] == ["对外发送日报"]
    assert payload["blueprint"]["skill_names"] == ["feishu-integration"]
    assert payload["blueprint"]["deferred_skill_names"] == []
    assert payload["blueprint"]["mcp_server_ids"] == ["smithery/github"]
    assert payload["blueprint"]["clawhub_slugs"] == ["market-research-agent"]
    assert any("builtin tools +" in item and "default skills" in item for item in payload["ready_now"])
    assert "extra skill: feishu-integration" in payload["will_install"]
    assert "mcp: smithery/github" in payload["will_install"]
    assert "clawhub skill: market-research-agent" in payload["will_install"]
    assert any("Feishu" in step for step in payload["manual_steps"])
    assert payload["summary"]["primary_users"] == ["投资团队", "研究团队"]
    assert payload["summary"]["core_outputs"] == ["日报", "周报"]
    assert payload["summary"]["first_mission"] == "先完成行业扫描"


def test_build_blueprint_preview_payload_auto_recommends_platform_skills() -> None:
    from app.tools.handlers.hr import _build_blueprint_preview_payload

    payload = _build_blueprint_preview_payload(
        {
            "name": "投研运营助理",
            "role_description": "给投资团队发送飞书日报，并同步 Jira 项目进展。",
            "primary_users": ["投资团队"],
            "core_outputs": ["飞书日报", "Jira 周报"],
            "focus_content": "先建立飞书日报和 Jira 跟进节奏",
        }
    )

    assert payload["recommended_skill_names"] == ["feishu-integration", "atlassian-rovo"]
    assert payload["blueprint"]["skill_names"] == []
    assert payload["blueprint"]["effective_skill_names"] == []
    assert payload["blueprint"]["deferred_skill_names"] == ["feishu-integration", "atlassian-rovo"]
    assert payload["will_install"] == []
    assert any(
        "defer extra installs until a builtin/default dry run proves a real gap" in step
        for step in payload["manual_steps"]
    )
    assert any("builtin workspace + web research" in item for item in payload["capability_routing"]["builtin_paths"])


def test_build_blueprint_preview_payload_warns_when_external_installs_cover_builtin_office_flows() -> None:
    from app.tools.handlers.hr import _build_blueprint_preview_payload

    payload = _build_blueprint_preview_payload(
        {
            "name": "材料助理",
            "role_description": "生成 PDF 汇总和 PPT 汇报材料。",
            "core_outputs": ["PDF 汇总", "PPT 汇报"],
            "mcp_server_ids": ["smithery/random-office"],
            "clawhub_slugs": ["third-party-ppt-skill"],
        }
    )

    assert any("default productivity skills already cover" in warning for warning in payload["warnings"])
    assert any("PDF/DOCX/XLSX/PPTX" in item for item in payload["capability_routing"]["builtin_paths"])


def test_build_blueprint_preview_payload_keeps_external_skill_urls_separate_from_platform_skills() -> None:
    from app.tools.handlers.hr import _build_blueprint_preview_payload

    payload = _build_blueprint_preview_payload(
        {
            "name": "设计提示词助手",
            "role_description": "整理前端设计提示词与规范",
            "external_skill_urls": [
                "https://github.com/acme/design-skills/tree/main/frontend-design-pro",
                "https://github.com/acme/design-skills/tree/main/frontend-design-pro",
            ],
        }
    )

    assert payload["blueprint"]["skill_names"] == []
    assert payload["blueprint"]["external_skill_urls"] == [
        "https://github.com/acme/design-skills/tree/main/frontend-design-pro",
    ]
    assert (
        "external skill ref: https://github.com/acme/design-skills/tree/main/frontend-design-pro"
        in payload["will_install"]
    )


def test_build_blueprint_preview_payload_reclassifies_skills_ref_out_of_platform_skill_names() -> None:
    from app.tools.handlers.hr import _build_blueprint_preview_payload

    payload = _build_blueprint_preview_payload(
        {
            "name": "设计提示词助手",
            "role_description": "整理前端设计提示词与规范",
            "skill_names": [
                "patricio0312rev/skills@design-to-component-translator",
                "feishu-integration",
            ],
        }
    )

    assert payload["blueprint"]["skill_names"] == ["feishu-integration"]
    assert payload["blueprint"]["deferred_skill_names"] == []
    assert payload["blueprint"]["external_skill_refs"] == ["patricio0312rev/skills@design-to-component-translator"]
    assert any(
        "external skill ref: patricio0312rev/skills@design-to-component-translator" in item
        for item in payload["will_install"]
    )


@pytest.mark.asyncio
async def test_install_external_skill_from_url_writes_skill_into_agent_workspace(tmp_path, monkeypatch) -> None:
    import app.tools.handlers.hr as hr_mod

    agent_id = uuid4()
    tenant_id = uuid4()

    monkeypatch.setattr(
        hr_mod,
        "get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )
    monkeypatch.setattr(
        hr_mod,
        "_parse_github_url",
        lambda _url: {
            "owner": "acme",
            "repo": "design-skills",
            "branch": "main",
            "path": "frontend-design-pro",
        },
    )

    async def fake_fetch(owner, repo, path, branch, token=""):
        assert (owner, repo, path, branch) == ("acme", "design-skills", "frontend-design-pro", "main")
        return [
            {"path": "SKILL.md", "content": "# Frontend Design Pro"},
            {"path": "notes.md", "content": "hello"},
        ]

    async def fake_token(_tenant_id):
        return "gh-token"

    async def fake_reuse(**_kwargs):
        return None

    monkeypatch.setattr(hr_mod, "_fetch_github_directory", fake_fetch)
    monkeypatch.setattr(hr_mod, "_get_github_token", fake_token)
    monkeypatch.setattr(hr_mod, "reuse_existing_skill_for_agent", fake_reuse)

    result = await hr_mod._install_external_skill_from_url(
        agent_id=agent_id,
        tenant_id=tenant_id,
        url="https://github.com/acme/design-skills/tree/main/frontend-design-pro",
    )

    assert result["status"] == "installed"
    assert result["folder_name"] == "frontend-design-pro"
    assert result["files_written"] == 2
    assert (tmp_path / str(agent_id) / "skills" / "frontend-design-pro" / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_install_external_skill_from_skills_ref_copies_cli_installed_skill(tmp_path, monkeypatch) -> None:
    import app.tools.handlers.hr as hr_mod

    agent_id = uuid4()

    monkeypatch.setattr(
        hr_mod,
        "get_settings",
        lambda: SimpleNamespace(AGENT_DATA_DIR=str(tmp_path)),
    )

    class _FakeProc:
        returncode = 0

        async def communicate(self):
            return (b"installed", b"")

        def kill(self):
            return None

    async def fake_create_subprocess_exec(*cmd, **kwargs):
        assert cmd[:3] == ("bash", "-lc", "npx skills add patricio0312rev/skills@design-to-component-translator -y")
        sandbox_skill_dir = tmp_path / "exec-home" / ".agents" / "skills" / "design-to-component-translator"
        sandbox_skill_dir.mkdir(parents=True, exist_ok=True)
        (sandbox_skill_dir / "SKILL.md").write_text("# Installed skill", encoding="utf-8")
        return _FakeProc()

    async def fake_wait_for(awaitable, timeout):
        return await awaitable

    monkeypatch.setattr(hr_mod.asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    monkeypatch.setattr(hr_mod.asyncio, "wait_for", fake_wait_for)
    monkeypatch.setattr(hr_mod.tempfile, "mkdtemp", lambda prefix: str(tmp_path / "exec-home"))

    result = await hr_mod._install_external_skill_from_skills_ref(
        agent_id=agent_id,
        ref="patricio0312rev/skills@design-to-component-translator",
    )

    assert result["status"] == "installed"
    assert result["folder_name"] == "design-to-component-translator"
    assert (tmp_path / str(agent_id) / "skills" / "design-to-component-translator" / "SKILL.md").exists()
