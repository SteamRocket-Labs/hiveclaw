from __future__ import annotations

import json

import pytest


@pytest.mark.asyncio
async def test_orchestrator_writes_source_claim_report_artifacts(tmp_path):
    from app.services.deep_research.orchestrator import DeepResearchOrchestrator
    from app.services.deep_research.schemas import ResearchRequest

    async def fake_tool(tool_name: str, arguments: dict) -> str:
        if tool_name == "web_search":
            return "https://issuer.example/rwa\nhttps://regulator.example/rwa"
        if tool_name == "web_fetch":
            url = arguments["url"]
            return (
                f"Title: {url}\n"
                "Tokenized treasury products are a visible RWA adoption lane in 2026. "
                "Issuers still face custody, liquidity, and regulatory disclosure risks."
            )
        raise AssertionError(f"unexpected tool: {tool_name}")

    result = await DeepResearchOrchestrator(fake_tool).run(
        ResearchRequest(
            question="Do a deep research brief on RWA adoption.",
            mode="topic_deep_dive",
            max_rounds=1,
            max_sources=2,
        ),
        artifact_dir=tmp_path,
    )

    assert result.status == "completed"
    assert (tmp_path / "sources.jsonl").is_file()
    assert (tmp_path / "claims.jsonl").is_file()
    assert (tmp_path / "steps.jsonl").is_file()
    assert (tmp_path / "report.md").is_file()
    assert (tmp_path / "final.json").is_file()

    final = json.loads((tmp_path / "final.json").read_text(encoding="utf-8"))
    assert final["quality_gates"]["attribution"] == "passed"
    assert final["source_count"] == 2
    assert final["claim_count"] >= 1
    assert "unsupported" not in {claim["status"] for claim in final["claims"]}


@pytest.mark.asyncio
async def test_orchestrator_returns_partial_report_and_gaps_when_sources_fail(tmp_path):
    from app.services.deep_research.orchestrator import DeepResearchOrchestrator
    from app.services.deep_research.schemas import ResearchRequest

    async def fake_tool(tool_name: str, arguments: dict) -> str:
        if tool_name == "web_search":
            return "https://blocked.example/rwa"
        return ""

    result = await DeepResearchOrchestrator(fake_tool).run(
        ResearchRequest(
            question="Research blocked RWA source.",
            mode="topic_deep_dive",
            max_rounds=1,
            max_sources=1,
        ),
        artifact_dir=tmp_path,
    )

    assert result.status == "failed"
    assert result.gaps
    assert (tmp_path / "report.md").read_text(encoding="utf-8").startswith("# Deep Research Report")
    assert json.loads((tmp_path / "final.json").read_text(encoding="utf-8"))["gaps"]
