from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PACK_ROOTS = (
    ROOT / "packs" / "deep_research_pack",
    ROOT / "backend" / "packs" / "deep_research_pack",
)


def test_deep_research_skill_documents_v2_worker_artifacts_and_no_manual_web_fallback():
    for pack_root in PACK_ROOTS:
        text = (pack_root / "skills" / "deep-research" / "SKILL.md").read_text(encoding="utf-8")
        assert "worker_reports.jsonl" in text
        assert "source_notes.jsonl" in text
        assert "lane_summaries.jsonl" in text
        assert "orchestrator-worker" in text.lower()
        assert "synthesize_from_digests" in text
        assert "unknown `src_`" in text or "unknown source" in text.lower()
        assert "deep_research_start" in text
        assert "Do not request or use raw web tools" in text


def test_subskills_route_to_dedicated_deep_research_tools_not_raw_manual_workflows():
    expected_modes = {
        "topic-deep-dive": "topic_deep_dive",
        "industry-research": "industry_research",
        "source-ledger-audit": "source_ledger_audit",
    }
    forbidden_tool_names = {
        "web_search",
        "web_fetch",
        "firecrawl_fetch",
        "xcrawl_scrape",
        "delegate_to_agent",
        "write_file",
        "edit_file",
    }
    for pack_root in PACK_ROOTS:
        for skill_name, mode in expected_modes.items():
            text = (pack_root / "skills" / skill_name / "SKILL.md").read_text(encoding="utf-8")
            assert "deep_research_run" in text
            assert "deep_research_start" in text
            assert mode in text
            assert "worker_reports.jsonl" in text
            yaml_header = text.split("---", 2)[1]
            for tool_name in forbidden_tool_names:
                assert f"  - {tool_name}" not in yaml_header
