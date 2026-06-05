from __future__ import annotations

import json



def test_plan_fill_contains_runtime_native_contract_for_deep_research():
    from app.services.deep_research.plan_mode import build_deep_research_plan_fill
    from app.services.deep_research.plan_contract import (
        research_plan_from_contract,
        validate_runtime_contract,
    )
    from app.services.deep_research.schemas import ResearchRequest

    request = ResearchRequest(
        question="Evaluate the RWA launchpad opportunity.",
        depth="full",
        max_sources=30,
        output_format="xlsx",
        output_language="zh",
        worker_topics=["confirmed official lane", "confirmed market lane"],
    )
    preview = {
        "worker_topics": ["confirmed official lane", "confirmed market lane"],
        "clarifying_questions": ["确认受众和用途"],
        "plan": {
            "lanes": [
                {
                    "lane_id": "official",
                    "label": "Official evidence",
                    "goal": "Verify primary issuer claims.",
                    "queries": [{"query": "RWA launchpad official documentation"}],
                    "preferred_source_types": ["primary", "technical"],
                },
                {
                    "lane_id": "market",
                    "label": "Market evidence",
                    "goal": "Quantify adoption and market size.",
                    "queries": [{"query": "RWA launchpad market data"}],
                    "preferred_source_types": ["dataset", "secondary"],
                },
            ]
        },
    }

    fill = build_deep_research_plan_fill(request, preview)
    contract = fill["deep_research"]["runtime_contract"]

    validate_runtime_contract(contract)
    assert contract["schema"] == "deep_research_runtime_contract.v1"
    assert contract["output"]["requested_formats"] == ["xlsx"]
    assert contract["output"]["format_briefs"]["xlsx"]["purpose"] == "evidence workbook"
    assert contract["research"]["lanes"][0]["worker_topic"] == "confirmed official lane"

    plan = research_plan_from_contract(contract)
    assert [lane.lane_id for lane in plan.lanes] == ["official", "market"]
    assert plan.lanes[0].queries[0].query == "RWA launchpad official documentation"


def test_chinese_deep_research_plan_fill_is_user_facing_not_internal_tool_script():
    from app.services.deep_research.plan_mode import build_deep_research_plan_fill
    from app.services.deep_research.schemas import ResearchRequest

    request = ResearchRequest(
        question="使用 deepresearch做一个web3的全景报告",
        mode="industry_research",
        depth="full",
        max_sources=30,
        output_language="zh-CN",
    )
    preview = {
        "worker_topics": ["Official lane", "Market lane"],
        "clarifying_questions": ["确认报告用途"],
        "plan": {"lanes": [{"lane_id": "official", "label": "Official/project sources"}]},
    }

    fill = build_deep_research_plan_fill(request, preview)
    visible_text = "\n".join(
        [
            fill["title"],
            fill["objective"],
            fill["motivation"],
            "\n".join(step["description"] for step in fill["steps"]),
            "\n".join(fill["success_criteria"]),
            "\n".join(fill["stop_conditions"]),
            "\n".join(fill["required_capabilities"]),
        ]
    )

    assert fill["title"] == "Web3 全景深度研究报告"
    assert "生成一份" in fill["objective"]
    assert "确认研究范围" in visible_text
    assert "证据标准" in visible_text
    assert "report.md" in visible_text
    assert "来源账本" in visible_text
    assert "内部保留" in visible_text
    assert "load_skill" not in visible_text
    assert "deep_research_start" not in visible_text
    assert "web_search" not in visible_text
    assert "web_fetch" not in visible_text
    assert "firecrawl_fetch" not in visible_text
    assert "plan_confirmed" not in visible_text
    assert "mem_" not in visible_text
    assert "sources.jsonl" not in visible_text
    assert "claims.jsonl" not in visible_text
    assert "steps.jsonl" not in visible_text
    assert "final.json" not in visible_text
    assert "runtime_artifacts" not in visible_text
    assert fill["required_capabilities"] == ["Deep Research", "Web 来源核验"]
    assert fill["handoff"]["payload"]["plan_confirmed"] is True
    assert "runtime_contract" in fill["deep_research"]


def test_research_request_accepts_approved_plan_contract_from_tool_arguments():
    from app.services.deep_research.schemas import ResearchRequest

    contract = {
        "schema": "deep_research_runtime_contract.v1",
        "research": {"lanes": [{"id": "official", "worker_topic": "Official evidence"}]},
        "output": {"requested_formats": ["pptx"], "primary_format": "pptx"},
    }

    request = ResearchRequest.from_arguments(
        {
            "question": "Research RWA custody.",
            "plan_confirmed": True,
            "approved_plan": json.dumps(contract),
        }
    )

    assert request.approved_plan["schema"] == "deep_research_runtime_contract.v1"
    assert request.approved_plan["output"]["requested_formats"] == ["pptx"]
    assert request.output_format == "pptx"
    assert request.worker_topics == ["Official evidence"]


