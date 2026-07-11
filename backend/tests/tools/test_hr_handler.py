"""Tests for the HR tool handler — create_digital_employee registration and validation."""

from __future__ import annotations

import inspect
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


def test_create_digital_employee_schema_accepts_only_a_confirmed_canonical_draft_reference():
    """Create must consume the server-side canonical draft, never restate it."""
    from app.services.agent_tools import get_combined_openai_tools

    all_tools = get_combined_openai_tools()
    hr_tool = next(t for t in all_tools if t["function"]["name"] == "create_digital_employee")
    params = hr_tool["function"]["parameters"]

    assert params["required"] == ["blueprint_id"]
    assert set(params["properties"]) == {"blueprint_id"}


def test_preview_agent_blueprint_schema_exposes_role_description_prompt_guard():
    from app.services.agent_tools import get_combined_openai_tools
    from app.tools.handlers.hr import HR_LONG_TEXT_MAX_CHARS, ROLE_DESCRIPTION_MAX_CHARS

    all_tools = get_combined_openai_tools()
    preview_tool = next(t for t in all_tools if t["function"]["name"] == "preview_agent_blueprint")
    params = preview_tool["function"]["parameters"]

    assert params["properties"]["role_description"]["maxLength"] == ROLE_DESCRIPTION_MAX_CHARS
    assert params["properties"]["personality"]["maxLength"] == HR_LONG_TEXT_MAX_CHARS
    assert params["properties"]["boundaries"]["maxLength"] == HR_LONG_TEXT_MAX_CHARS
    assert params["properties"]["welcome_message"]["maxLength"] == HR_LONG_TEXT_MAX_CHARS
    assert params["properties"]["focus_content"]["maxLength"] == HR_LONG_TEXT_MAX_CHARS
    assert params["properties"]["heartbeat_topics"]["maxLength"] == HR_LONG_TEXT_MAX_CHARS
    assert "source_attributions" in params["properties"]
    assert "blueprint_id" in params["properties"]
    source_schema = params["properties"]["source_attributions"]["items"]["properties"]
    assert "supported_by_company_kb" not in source_schema["source_type"]["enum"]


def test_hr_role_description_prompt_guard_trims_to_tool_limit():
    from app.tools.handlers.hr import ROLE_DESCRIPTION_MAX_CHARS, _trim_role_description_for_prompt_guard

    result = _trim_role_description_for_prompt_guard("x" * (ROLE_DESCRIPTION_MAX_CHARS + 50))

    assert len(result) == ROLE_DESCRIPTION_MAX_CHARS


def test_hr_default_skill_count_includes_builtin_and_pack_skill_capsules():
    from app.services.skill_seeder import DEFAULT_BUILTIN_SKILL_FOLDERS, DEFAULT_PACK_SKILL_FOLDERS
    from app.tools.handlers.hr import _default_skill_count

    assert _default_skill_count() == len(DEFAULT_BUILTIN_SKILL_FOLDERS) + len(DEFAULT_PACK_SKILL_FOLDERS)


def test_build_blueprint_preview_payload_trims_role_description_to_prompt_guard():
    from app.tools.handlers.hr import ROLE_DESCRIPTION_MAX_CHARS, _build_blueprint_preview_payload

    payload = _build_blueprint_preview_payload(
        {
            "name": "研究助理",
            "role_description": "x" * (ROLE_DESCRIPTION_MAX_CHARS + 50),
            "primary_users": ["投资团队"],
            "core_outputs": ["日报"],
            "focus_content": "先做日报",
        }
    )

    assert len(payload["blueprint"]["role_description"]) == ROLE_DESCRIPTION_MAX_CHARS


def test_hr_tool_included_in_hr_tools_set():
    """_get_hr_tools should include the HR creation core without pinning dynamic search providers."""
    from app.services.agent_tools import _get_hr_tools

    hr_tools = _get_hr_tools()
    names = [t["function"]["name"] for t in hr_tools]
    required_names = {
        "create_digital_employee",
        "preview_agent_blueprint",
        "web_search",
        "exa_search",
        "tavily_search",
        "firecrawl_fetch",
        "xcrawl_scrape",
        "execute_code",
        "discover_resources",
        "search_clawhub",
    }
    assert required_names <= set(names)


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
    assert preview_meta.risk_class == "controlled_write"
    assert preview_meta.idempotency_scope == "session"


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


def test_hr_blueprint_trigger_config_gets_plan_exemption_marker():
    from app.services import plan_mode_core
    from app.tools.handlers.hr import _stamp_hr_blueprint_trigger_exemption

    config = _stamp_hr_blueprint_trigger_exemption({"expr": "0 12 * * *", "metadata": {"source": "hr"}})

    assert config["expr"] == "0 12 * * *"
    assert config["metadata"]["source"] == "hr"
    assert config["metadata"]["plan_exempt_reason"] == plan_mode_core.PLAN_EXEMPT_CONFIRMED_HR_BLUEPRINT


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
            "source_attributions": [
                {
                    "field": "boundaries",
                    "value_summary": "不绕过合规审批",
                    "source_type": "supported_by_company_kb",
                    "source_refs": ["kb://policy/compliance"],
                },
                {
                    "field": "focus_content",
                    "value_summary": "先完成行业扫描",
                    "source_type": "suggested_by_history",
                    "source_refs": ["t3:memory/t3/episodes.md#case-1"],
                },
                {
                    "field": "core_outputs",
                    "value_summary": "日报/周报缺少公司知识库依据",
                    "source_type": "unknown_or_needs_company_source",
                },
            ],
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
    assert payload["blueprint_hash"]
    assert payload["blueprint"]["source_attributions"][0]["source_type"] == "unknown_or_needs_company_source"
    assert (
        payload["source_attribution_policy"]["company_knowledge_lane"] == "known_missing_not_available_for_attribution"
    )
    assert any("Company KB is not implemented" in warning for warning in payload["warnings"])
    assert payload["source_attribution_policy"]["history_suggestion_lane"] == "advisory"
    assert any(item["source_type"] == "unknown_or_needs_company_source" for item in payload["knowledge_debt"])
    assert "unknown_or_needs_company_source" in payload["confirmation_requirements"]["source_types_to_present"]
    assert "suggested_by_history" in payload["confirmation_requirements"]["source_types_to_present"]
    assert payload["creation_flow"]["mode"] == "dynamic_rounds_mandatory_gates"
    assert payload["creation_flow"]["gates"]["identity"]["status"] == "complete"
    assert payload["creation_flow"]["gates"]["governance"]["status"] == "complete"
    assert payload["creation_flow"]["gates"]["activation"]["status"] == "complete"
    assert payload["creation_flow"]["gates"]["capabilities"]["status"] == "complete"
    assert payload["creation_flow"]["gates"]["confirmation"]["status"] == "pending"


def test_blueprint_preview_blocks_names_that_create_would_reject():
    from app.tools.handlers.hr import _build_blueprint_preview_payload

    payload = _build_blueprint_preview_payload(
        {
            "name": "A",
            "role_description": "Research markets for the investment team.",
            "primary_users": ["Investment team"],
            "core_outputs": ["Weekly brief"],
            "focus_content": "Prepare the first weekly brief.",
        }
    )

    assert payload["creation_flow"]["gates"]["identity"]["status"] == "missing"
    assert "name" in payload["creation_flow"]["gates"]["identity"]["missing"]
    assert "identity" in payload["missing_gates"]


def test_build_blueprint_preview_payload_rejects_invalid_source_attribution_types() -> None:
    from app.tools.handlers.hr import _build_blueprint_preview_payload

    payload = _build_blueprint_preview_payload(
        {
            "name": "研究助理",
            "role_description": "服务投研团队的市场研究员。",
            "primary_users": ["投研团队"],
            "core_outputs": ["日报"],
            "focus_content": "先完成日报",
            "source_attributions": [
                {"field": "boundaries", "source_type": "memory_says_so", "value_summary": "不外发"}
            ],
        }
    )

    assert payload["blueprint"]["source_attributions"] == []
    assert any("invalid source_attributions ignored" in warning for warning in payload["warnings"])


def test_build_blueprint_preview_payload_defaults_missing_source_type_to_unknown_knowledge_debt() -> None:
    from app.tools.handlers.hr import _build_blueprint_preview_payload

    payload = _build_blueprint_preview_payload(
        {
            "name": "研究助理",
            "role_description": "服务投研团队的市场研究员。",
            "primary_users": ["投研团队"],
            "core_outputs": ["日报"],
            "focus_content": "先完成日报",
            "source_attributions": [{"field": "core_outputs", "value_summary": "日报需要进一步确认来源"}],
        }
    )

    assert payload["blueprint"]["source_attributions"] == [
        {
            "field": "core_outputs",
            "source_type": "unknown_or_needs_company_source",
            "value_summary": "日报需要进一步确认来源",
            "source_refs": [],
        }
    ]
    assert any("missing source_attributions source_type defaulted" in warning for warning in payload["warnings"])
    assert payload["knowledge_debt"][0]["field"] == "core_outputs"
    assert "unknown_or_needs_company_source" in payload["confirmation_requirements"]["source_types_to_present"]


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

    assert payload["recommended_skill_names"] == ["feishu-integration"]
    assert payload["blueprint"]["skill_names"] == []
    assert payload["blueprint"]["effective_skill_names"] == []
    assert payload["blueprint"]["deferred_skill_names"] == ["feishu-integration"]
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


def test_build_blueprint_preview_payload_requires_governance_for_finance_external_publishing() -> None:
    from app.tools.handlers.hr import _build_blueprint_preview_payload

    payload = _build_blueprint_preview_payload(
        {
            "name": "华尔街段子手",
            "role_description": "美股社区自媒体分析师，每日发布行情复盘和个股分析到 Telegram 和 X。",
            "primary_users": ["Telegram 美股社区", "X 关注者"],
            "core_outputs": ["每日美股内容"],
            "focus_content": "搭建追踪框架",
            "boundaries": "",
        }
    )

    assert payload["risk_class"] == "high"
    assert "governance" in payload["missing_gates"]
    assert payload["creation_flow"]["gates"]["governance"]["status"] == "missing"
    assert any("governance gate" in warning for warning in payload["warnings"])


def test_blueprint_hash_is_stable_for_semantically_identical_payloads() -> None:
    from app.tools.handlers.hr import _build_blueprint_preview_payload

    first = _build_blueprint_preview_payload(
        {
            "name": "研究员",
            "role_description": "服务投研团队的市场研究员。",
            "primary_users": ["投研团队"],
            "core_outputs": ["日报"],
            "boundaries": "不捏造来源",
            "focus_content": "先产出日报模板",
        }
    )
    second = _build_blueprint_preview_payload(
        {
            "focus_content": "先产出日报模板",
            "boundaries": "不捏造来源",
            "core_outputs": ["日报"],
            "primary_users": ["投研团队"],
            "role_description": "服务投研团队的市场研究员。",
            "name": "研究员",
        }
    )

    assert first["blueprint_hash"] == second["blueprint_hash"]


def test_blueprint_hash_is_stable_when_source_type_missing_then_defaulted() -> None:
    from app.tools.handlers.hr import _build_blueprint_preview_payload

    missing_source_type = _build_blueprint_preview_payload(
        {
            "name": "通用助理",
            "role_description": "按照用户指令完成对话、检索、整理和提醒等基础工作。",
            "primary_users": ["用户本人"],
            "core_outputs": ["按需对话回复"],
            "boundaries": "不伪造引用",
            "focus_content": "等待用户首次具体指派",
            "permission_scope": "self",
            "source_attributions": [{"field": "name", "value_summary": "通用助理"}],
        }
    )
    explicit_unknown = _build_blueprint_preview_payload(
        {
            "name": "通用助理",
            "role_description": "按照用户指令完成对话、检索、整理和提醒等基础工作。",
            "primary_users": ["用户本人"],
            "core_outputs": ["按需对话回复"],
            "boundaries": "不伪造引用",
            "focus_content": "等待用户首次具体指派",
            "permission_scope": "self",
            "source_attributions": [
                {
                    "field": "name",
                    "source_type": "unknown_or_needs_company_source",
                    "value_summary": "通用助理",
                }
            ],
        }
    )

    assert (
        missing_source_type["blueprint"]["source_attributions"] == explicit_unknown["blueprint"]["source_attributions"]
    )
    assert missing_source_type["blueprint_hash"] == explicit_unknown["blueprint_hash"]


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


def test_append_hr_creation_t0_event_records_source_attributed_creation_case(tmp_path) -> None:
    from app.memory.t0.ledger import replay_t0_session_events
    from app.tools.handlers.hr import _append_hr_creation_t0_event

    hr_agent_id = uuid4()
    created_agent_id = uuid4()
    session_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()

    result = _append_hr_creation_t0_event(
        hr_agent_id=hr_agent_id,
        created_agent_id=created_agent_id,
        created_agent_name="投研助理",
        session_id=session_id,
        tenant_id=tenant_id,
        user_id=user_id,
        blueprint_hash="bp_123",
        preview_payload={
            "risk_class": "standard",
            "blueprint": {
                "archetype": "research",
                "primary_users": ["投研团队"],
                "core_outputs": ["日报"],
                "skill_names": ["feishu-integration"],
                "source_attributions": [
                    {
                        "field": "boundaries",
                        "source_type": "unknown_or_needs_company_source",
                        "source_refs": ["kb://policy/compliance"],
                        "value_summary": "不绕过合规审批",
                    },
                    {
                        "field": "focus_content",
                        "source_type": "suggested_by_history",
                        "source_refs": ["t3:memory/t3/episodes.md#case-1"],
                        "value_summary": "先做日报",
                    },
                ],
            },
            "manual_steps": ["配置飞书授权"],
        },
        installed_skill_names=["feishu-integration"],
        trigger_count=1,
        data_root=tmp_path,
    )

    events = replay_t0_session_events(agent_id=hr_agent_id, session_id=session_id, data_root=tmp_path)

    assert result.event_id
    assert len(events) == 1
    assert events[0].event_type == "hr_agent_created"
    assert events[0].role == "tool"
    assert events[0].metadata["created_agent_id"] == str(created_agent_id)
    assert events[0].metadata["blueprint_hash"] == "bp_123"
    assert events[0].metadata["source_attributions"][0]["source_type"] == "unknown_or_needs_company_source"
    assert events[0].metadata["source_attributions"][1]["source_type"] == "suggested_by_history"
    assert events[0].metadata["manual_setup_debt"] == ["配置飞书授权"]


def test_hr_creation_t0_projection_is_idempotent_by_canonical_draft(tmp_path) -> None:
    from app.memory.t0.ledger import replay_t0_session_events
    from app.tools.handlers.hr import _append_hr_creation_t0_event

    hr_agent_id = uuid4()
    created_agent_id = uuid4()
    session_id = uuid4()
    tenant_id = uuid4()
    user_id = uuid4()
    draft_id = uuid4()
    kwargs = {
        "hr_agent_id": hr_agent_id,
        "created_agent_id": created_agent_id,
        "created_agent_name": "Research Bot",
        "session_id": session_id,
        "tenant_id": tenant_id,
        "user_id": user_id,
        "blueprint_hash": "bp_idempotent",
        "preview_payload": {"blueprint": {"name": "Research Bot"}, "manual_steps": []},
        "installed_skill_names": [],
        "trigger_count": 0,
        "creation_draft_id": draft_id,
        "data_root": tmp_path,
    }

    first = _append_hr_creation_t0_event(**kwargs)
    second = _append_hr_creation_t0_event(**kwargs)
    events = replay_t0_session_events(agent_id=hr_agent_id, session_id=session_id, data_root=tmp_path)

    assert first.event_id == second.event_id
    assert len(events) == 1
    assert events[0].message_id == draft_id.hex
    assert events[0].metadata["creation_draft_id"] == str(draft_id)


@pytest.mark.asyncio
async def test_install_external_skill_from_url_stages_review_without_active_install(tmp_path, monkeypatch) -> None:
    import app.tools.handlers.hr as hr_mod

    agent_id = uuid4()
    expected_tenant_id = uuid4()

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

    async def fake_stage_for_tenant(*, tenant_id, created_by_user_id, source_uri, folder_name, files, source_format):
        assert tenant_id == expected_tenant_id
        assert created_by_user_id is None
        assert source_uri == "https://github.com/acme/design-skills/tree/main/frontend-design-pro"
        assert folder_name == "frontend-design-pro"
        assert files[0]["path"] == "SKILL.md"
        assert source_format == "external_skill_url"
        return {
            "status": "review_required",
            "folder_name": folder_name,
            "files_written": 0,
            "files": [],
            "review_id": "review-hr-url",
            "skill_guard": {"allowed": True},
            "source_uri": source_uri,
        }

    monkeypatch.setattr(hr_mod, "_fetch_github_directory", fake_fetch)
    monkeypatch.setattr(hr_mod, "_get_github_token", fake_token)
    monkeypatch.setattr(hr_mod, "reuse_existing_skill_for_agent", fake_reuse)
    monkeypatch.setattr(hr_mod, "stage_external_skill_package_review_for_tenant", fake_stage_for_tenant)

    result = await hr_mod._install_external_skill_from_url(
        agent_id=agent_id,
        tenant_id=expected_tenant_id,
        url="https://github.com/acme/design-skills/tree/main/frontend-design-pro",
    )

    assert result["status"] == "review_required"
    assert result["folder_name"] == "frontend-design-pro"
    assert result["files_written"] == 0
    assert result["review_id"] == "review-hr-url"
    assert not (tmp_path / str(agent_id) / "skills" / "frontend-design-pro" / "SKILL.md").exists()


@pytest.mark.asyncio
async def test_install_external_skill_from_skills_ref_stages_review_without_active_install(
    tmp_path, monkeypatch
) -> None:
    from pathlib import Path

    import app.tools.handlers.hr as hr_mod
    from app.services.code_execution.contracts import CodeExecutionResult

    agent_id = uuid4()

    def fake_mkdtemp(prefix):
        target = tmp_path / ("skill-work" if "work" in prefix else "exec-home")
        target.mkdir(parents=True, exist_ok=True)
        return str(target)

    monkeypatch.setattr(hr_mod.tempfile, "mkdtemp", fake_mkdtemp)

    calls = []

    async def fake_execute_agent_command(command, *, work_dir, env, timeout, runtime=None, network_policy=None):
        calls.append({"command": command, "env": env, "runtime": runtime, "network_policy": network_policy})
        # Simulate `npx skills add` producing a skill under $HOME/.agents/skills; the
        # execution provider syncs the remote HOME subtree back into exec_home.
        skill_dir = Path(env["HOME"]) / ".agents" / "skills" / "design-to-component-translator"
        skill_dir.mkdir(parents=True, exist_ok=True)
        (skill_dir / "SKILL.md").write_text("# Installed skill", encoding="utf-8")
        return CodeExecutionResult(stdout="installed", exit_code=0)

    monkeypatch.setattr(hr_mod, "execute_agent_command", fake_execute_agent_command)

    async def fake_stage_for_tenant(*, tenant_id, created_by_user_id, source_uri, folder_name, files, source_format):
        assert tenant_id is None
        assert created_by_user_id is None
        assert source_uri == "skills_ref:patricio0312rev/skills@design-to-component-translator"
        assert folder_name == "design-to-component-translator"
        assert files[0]["path"] == "SKILL.md"
        assert source_format == "skills_ref"
        return {
            "status": "review_required",
            "folder_name": folder_name,
            "files_written": 0,
            "files": [],
            "review_id": "review-hr-skills-ref",
            "skill_guard": {"allowed": True},
            "source_uri": source_uri,
        }

    monkeypatch.setattr(hr_mod, "stage_external_skill_package_review_for_tenant", fake_stage_for_tenant)

    result = await hr_mod._install_external_skill_from_skills_ref(
        agent_id=agent_id,
        ref="patricio0312rev/skills@design-to-component-translator",
    )

    assert result["status"] == "review_required"
    assert result["folder_name"] == "design-to-component-translator"
    assert result["review_id"] == "review-hr-skills-ref"
    assert not (tmp_path / str(agent_id) / "skills" / "design-to-component-translator" / "SKILL.md").exists()
    assert calls
    assert calls[0]["env"]["HOME"] == str(tmp_path / "exec-home")
    assert calls[0]["runtime"] == "node24"
    assert calls[0]["network_policy"] == "allow-all"


@pytest.mark.asyncio
async def test_install_external_skill_from_skills_ref_fails_closed_without_sandbox(tmp_path, monkeypatch) -> None:
    import app.tools.handlers.hr as hr_mod
    from app.services.code_execution.contracts import CodeExecutionResult

    agent_id = uuid4()

    def fake_mkdtemp(prefix):
        target = tmp_path / ("skill-work" if "work" in prefix else "exec-home")
        target.mkdir(parents=True, exist_ok=True)
        return str(target)

    monkeypatch.setattr(hr_mod.tempfile, "mkdtemp", fake_mkdtemp)

    async def fake_execute_agent_command(*_args, **_kwargs):
        return CodeExecutionResult(error="sandbox unavailable")

    monkeypatch.setattr(hr_mod, "execute_agent_command", fake_execute_agent_command)

    with pytest.raises(RuntimeError, match="sandbox unavailable"):
        await hr_mod._install_external_skill_from_skills_ref(
            agent_id=agent_id,
            ref="patricio0312rev/skills@design-to-component-translator",
        )


class _ScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _ScalarsResult:
    def __init__(self, values):
        self._values = list(values)

    def scalars(self):
        return self

    def all(self):
        return self._values


class _QueuedDB:
    def __init__(self, results):
        self._results = list(results)
        self.executed = 0

    async def execute(self, _stmt):
        self.executed += 1
        if not self._results:
            raise AssertionError("Unexpected execute() call")
        return self._results.pop(0)


@pytest.mark.asyncio
async def test_hr_employee_creation_ignores_dangling_default_model_setting() -> None:
    from app.tools.handlers.hr import _resolve_employee_creation_model

    tenant_id = uuid4()
    dangling_model_id = uuid4()
    fallback_model = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, enabled=True)
    db = _QueuedDB(
        [
            _ScalarResult({"model_id": str(dangling_model_id)}),
            _ScalarResult(None),
            _ScalarResult(fallback_model),
        ]
    )

    model = await _resolve_employee_creation_model(db, tenant_id)

    assert model is fallback_model
    assert model.id != dangling_model_id


@pytest.mark.asyncio
async def test_hr_soul_refinement_model_falls_back_when_hr_model_is_unavailable() -> None:
    from app.tools.handlers.hr import _resolve_employee_refinement_model

    tenant_id = uuid4()
    creation_model = SimpleNamespace(id=uuid4(), tenant_id=tenant_id, enabled=True)
    db = _QueuedDB([_ScalarResult(None)])

    model, source = await _resolve_employee_refinement_model(
        db,
        tenant_id,
        preferred_model_id=uuid4(),
        creation_model=creation_model,
    )

    assert model is creation_model
    assert source == "tenant_default"


def test_create_digital_employee_uses_validated_model_resolution() -> None:
    from app.services.hr_provisioning_runner import run_hr_provisioning

    src = inspect.getsource(run_hr_provisioning)

    assert "_resolve_employee_creation_model" in src
    assert "_resolve_employee_refinement_model" in src
    assert "_claim_canonical_hr_blueprint" in src
    assert "confirmed_blueprint_hash" not in src


def test_create_digital_employee_uses_audited_identity_bootstrap_bypass() -> None:
    from app.services.hr_provisioning_runner import run_hr_provisioning

    src = inspect.getsource(run_hr_provisioning)

    assert "rls_bypass_reason=" in src
    assert "HR digital employee identity bootstrap" in src
    assert "rls_bypass_actor_id=str(user.id)" in src


def test_create_digital_employee_has_no_agent_row_equals_ready_recovery_bypass() -> None:
    from app.services.hr_provisioning_runner import run_hr_provisioning

    src = inspect.getsource(run_hr_provisioning)

    assert "ensure_hr_provisioning_steps" in src
    assert "derive_hr_provisioning_readiness" in src
    assert "required_blockers" in src
    assert src.index("derive_hr_provisioning_readiness") < src.index("mark_hr_creation_completed_record")
    assert "provisioning=dict(draft.provisioning_json or" not in src


def test_incomplete_hr_creation_result_is_not_reported_as_success() -> None:
    import json

    from app.tools.handlers.hr import _build_create_employee_result

    payload = json.loads(
        _build_create_employee_result(
            agent_id=str(uuid4()),
            agent_name="Research Bot",
            features=["workspace=complete"],
            skills_dir="/tmp/agent/skills",
            creation_state="provisioning_failed",
            warnings=["Required provisioning incomplete: capability:mcp:github"],
        )
    )

    assert payload["status"] == "incomplete"
    assert payload["creation_state"] == "provisioning_failed"
    assert "do not treat this employee as ready" in payload["message"]
