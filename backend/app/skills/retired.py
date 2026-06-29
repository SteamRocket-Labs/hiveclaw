"""Retired builtin skill folder names.

These folders are ignored at runtime even if an old workspace copy cannot be
deleted from the agent volume.
"""

RETIRED_BUILTIN_SKILL_FOLDERS = frozenset(
    {
        "web-research-guide",
        "complex-task-executor",
        "workspace-guide",
        "trigger-guide",
        "memory-guide",
        "messaging-guide",
        "delegation-guide",
        "find-skills",
        "skill-vetter",
        "data-analysis",
        "content-writing",
        "competitive-analysis",
        "meeting-notes",
        "content-research-writer",
        "deep-research",
        "topic-deep-dive",
        "industry-research",
        "atlassian-rovo",
        "source-ledger-audit",
        "docx-generator",
        "xlsx-processor",
        "pptx-generator",
        "pdf-generator",
        "weekly-report-generator",
        "meeting-minutes",
        "pitch-deck-generator",
        "finance-research",
        "secondary-equity-deep-dive",
        "dcf-valuation",
        "comps-valuation",
        "ipo-pipeline-monitor",
        "primary-market-due-diligence",
        "portfolio-risk-review",
        "ic-memo-generator",
    }
)
