from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
PROMPT_CONTRACT_DOC = ROOT / "docs" / "cc-codex-prompt-unified-audit-2026-06-22.md"


def test_prompt_text_contract_covers_agent_lifecycle_surfaces() -> None:
    text = PROMPT_CONTRACT_DOC.read_text(encoding="utf-8")

    required_surfaces = [
        "Frozen Prefix",
        "Dynamic Suffix",
        "Tool Use",
        "Tool Search",
        "Skill",
        "System Skills",
        "Sub-agent",
        "Agent Delegation",
        "Workflow",
        "Hooks",
        "Plan Mode",
        "Runtime Reminders",
        "Auto Compaction",
        "Goal",
        "Command Parity",
        "Permissions",
        "Session Continuity",
        "Team",
        "Work Ledger",
        "Deep Research",
        "MCP / Extensions",
        "Compaction",
        "Compaction Trace / Resume",
        "Dream / T3 Background Prompts",
    ]

    missing = [surface for surface in required_surfaces if surface not in text]
    assert missing == []


def test_prompt_text_contract_keeps_dream_memory_as_next_stage_mechanism() -> None:
    text = PROMPT_CONTRACT_DOC.read_text(encoding="utf-8")

    required_boundaries = [
        "JSON/JSONL is the mechanical truth",
        "Markdown is the deterministic projection",
        "T0 session corpus",
        "T2 Segment Package",
        "T3 semantic layer",
        "`soul.md`",
        "Dream is a background consolidation job",
    ]

    missing = [boundary for boundary in required_boundaries if boundary not in text]
    assert missing == []


def test_prompt_text_contract_covers_auto_compaction_prompt_boundaries() -> None:
    text = PROMPT_CONTRACT_DOC.read_text(encoding="utf-8")

    required_boundaries = [
        "_SUMMARIZE_SYSTEM_PROMPT",
        "Initial context compaction",
        "Prompt-too-long reactive retry",
        "Mid-loop auto compaction",
        "Microcompact",
        "no-tools",
        "session-state preservation",
        "not long-term memory",
        "11 fields",
        "_SUMMARY_MAX_OUTPUT_TOKENS = 20_000",
        "PRE_COMPACTION",
        "POST_COMPACTION",
        "post-compact restoration",
        "compact summary = continuation handoff",
    ]

    missing = [boundary for boundary in required_boundaries if boundary not in text]
    assert missing == []
