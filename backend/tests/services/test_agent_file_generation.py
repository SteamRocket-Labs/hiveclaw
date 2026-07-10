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
            "company_name": "Acme Capital",
            "owner_name": "Rocky",
            "permission_scope": "company",
            "triggers": [{"name": "daily_report", "type": "cron"}],
            "skill_names": ["feishu-integration"],
            "mcp_server_ids": ["smithery/github"],
            "focus_content": "优先建立日报流程",
            "heartbeat_topics": "AI\n半导体",
        },
    )

    assert "schema: hive.soul.v2" in soul
    assert '<soul_identity frozen="true">' in soul
    assert '<soul_principle id="first-person-accountability" stability="seed" frozen="true">' in soul
    assert '<soul_redline id="frozen-company-charter" stability="seed" frozen="true">' in soul
    assert '<soul_redline id="frozen-owner-agency-charter" stability="seed" frozen="true">' in soul
    assert '<soul_quality_bar id="what-good-looks-like" stability="seed">' in soul
    assert '<soul_user_model id="primary-users-and-outputs" stability="seed">' in soul
    assert '<soul_principle id="operating-style" stability="seed">' in soul
    assert "直接支持 Rocky" in soul
    assert "Acme Capital" in soul
    assert "full_authority" in soul
    assert "confirm_first" in soul
    assert "never_do" in soul
    assert "投资团队" in soul
    assert "行业日报" in soul
    assert "严谨" in soul
    assert "feishu-integration" not in soul
    assert "smithery/github" not in soul
    assert "## Tool Preferences" not in soul
    assert "## Operating Cadence" not in soul
    assert "## Early Focus" not in soul
