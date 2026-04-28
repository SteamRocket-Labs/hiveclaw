from __future__ import annotations


def test_render_agent_soul_from_blueprint_includes_operating_contract_sections() -> None:
    from app.services.agent_manager import _render_agent_soul_from_blueprint

    soul = _render_agent_soul_from_blueprint(
        agent_name="研究助理",
        role_description="追踪市场与融资动态",
        creator_name="Rocky",
        created_at="2026-04-02",
        personality="严谨\n结论先行",
        boundaries="不捏造来源\n敏感操作先说明风险",
        blueprint={
            "primary_users": ["投资团队", "研究团队"],
            "core_outputs": ["行业日报", "投研简报"],
            "permission_scope": "company",
            "triggers": [{"name": "daily_report", "type": "cron"}],
            "skill_names": ["feishu-integration"],
            "mcp_server_ids": ["smithery/github"],
            "focus_content": "优先建立日报流程",
            "heartbeat_topics": "AI\n半导体",
        },
    )

    assert "## Identity & Mission" in soul
    assert "## What Good Looks Like" in soul
    assert "## Primary Users & Stakeholders" in soul
    assert "## Core Outputs" in soul
    assert "## Operating Style" in soul
    assert "## Boundaries & Red Lines" in soul
    assert "## Collaboration & Escalation" in soul
    assert "## How I Learn" in soul
    assert "投资团队" in soul
    assert "行业日报" in soul
    assert "严谨" in soul
    assert "feishu-integration" not in soul
    assert "smithery/github" not in soul
    assert "## Tool Preferences" not in soul
    assert "## Operating Cadence" not in soul
    assert "## Early Focus" not in soul


def test_render_focus_from_blueprint_includes_runtime_capability_sections() -> None:
    from app.services.agent_manager import _render_focus_from_blueprint

    focus = _render_focus_from_blueprint(
        focus_content="1. 建日报\n2. 补关键词",
        heartbeat_topics="AI\n半导体",
        primary_users=["投资团队"],
        core_outputs=["日报", "周报"],
        ready_now=["builtin tools + 12 default skills"],
        deferred_capabilities=["feishu-integration (only if the first live handoff proves builtin messaging is insufficient)"],
        manual_steps=["完成 Feishu CLI 或渠道认证"],
        triggers=[{"name": "daily_report", "type": "cron"}],
    )

    assert "# Focus" in focus
    assert "## Initial Mission" in focus
    assert "## Who This Agent Serves" in focus
    assert "## Expected Outputs" in focus
    assert "## Tasks" in focus
    assert "## Starting Capabilities Available Now" in focus
    assert "## Capability Gaps To Validate" in focus
    assert "## Human Setup Still Required" in focus
    assert "## Planned Wake Policies" in focus
    assert "## Heartbeat Exploration Topics" in focus
    assert "## First Success Check" in focus
    assert "- [ ] task_1 :: 建日报" in focus
    assert "- [ ] task_2 :: 补关键词" in focus
    assert "投资团队" in focus
    assert "日报" in focus
    assert "完成 Feishu CLI 或渠道认证" in focus
    assert "feishu-integration" in focus
