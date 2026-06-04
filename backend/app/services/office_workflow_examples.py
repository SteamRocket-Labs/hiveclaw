"""Built-in office workflow examples (§9 P13, §4 产品路径).

The office scene is the FIRST real caller of the workflow engine: these
definitions are plain structured data (§3.2) exercising the one mental model
一次性编排 → 可复用模板 → 定时/触发自动化. They compile through the same
compiler/admission as anything an agent submits — being built-in grants no
bypass. Office document tools ride on the leaf capability surface (worker
preset + capability gate), never on a private execution path.

Risk posture (§10 decision 3): read-only / workspace-write steps stay low
risk (lightweight preview); 外发/共享/删除 carry ``effects="external"`` and
are therefore HIGH risk — compiler enforces a preceding gate_step and the
launch needs a confirmed plan.
"""

from __future__ import annotations

OFFICE_LEAF_PARSER = "office-doc-parser"
OFFICE_LEAF_ANALYST = "office-doc-analyst"
OFFICE_LEAF_WRITER = "office-doc-writer"
OFFICE_LEAF_SENDER = "office-doc-sender"

#: 合同审阅: OCR/解析 → 条款提取 → 风险表 (workspace artifacts only — low risk)
CONTRACT_REVIEW_EXAMPLE: dict = {
    "name": "office-contract-review",
    "description": "Parse a contract, extract clauses, produce a risk table in the workspace.",
    "args_schema": {
        "doc_path": {"type": "string", "required": True},
        "risk_threshold": {"type": "number", "required": False},
    },
    "default_budget": {"max_total_tokens": 120_000},
    "steps": [
        {
            "id": "parse",
            "type": "agent_step",
            "leaf": {"name": OFFICE_LEAF_PARSER, "type": "worker"},
            "task": "Parse the contract at {{args.doc_path}} and emit structured sections.",
            "effects": "workspace_write",
        },
        {
            "id": "clauses",
            "type": "agent_step",
            "leaf": {"name": OFFICE_LEAF_ANALYST, "type": "worker"},
            "task": "Extract obligations, liabilities and deadlines from {{steps.parse.output}}.",
        },
        {
            "id": "risk-table",
            "type": "agent_step",
            "leaf": {"name": OFFICE_LEAF_WRITER, "type": "worker"},
            "task": "Write a risk table artifact for {{steps.clauses.output}} into the workspace.",
            "effects": "workspace_write",
        },
    ],
}

#: 周报汇总: bounded fanout over sources → compose (workspace artifact)
WEEKLY_REPORT_EXAMPLE: dict = {
    "name": "office-weekly-report",
    "description": "Summarize each source, then compose the weekly report document.",
    "args_schema": {
        "sources": {"type": "array", "required": True},
        "week": {"type": "string", "required": True},
    },
    "default_budget": {"max_total_tokens": 150_000},
    "steps": [
        {
            "id": "summarize",
            "type": "fanout_step",
            "leaf": {"name": OFFICE_LEAF_ANALYST, "type": "worker"},
            "items_from": "args.sources",
            "per_item_task": "Summarize {{item}} for week {{args.week}}.",
            "max_concurrency": 4,
        },
        {
            "id": "compose",
            "type": "agent_step",
            "leaf": {"name": OFFICE_LEAF_WRITER, "type": "worker"},
            "task": "Compose the weekly report for {{args.week}} from {{steps.summarize.output}}.",
            "effects": "workspace_write",
        },
    ],
}

#: 文档外发: draft (workspace) → 审批 gate → send (EXTERNAL — high risk by design)
DOCUMENT_DISTRIBUTION_EXAMPLE: dict = {
    "name": "office-document-distribution",
    "description": "Draft a document, get human approval, then distribute it externally.",
    "args_schema": {
        "doc_path": {"type": "string", "required": True},
        "recipients": {"type": "array", "required": True},
    },
    "default_budget": {"max_total_tokens": 80_000},
    "steps": [
        {
            "id": "draft",
            "type": "agent_step",
            "leaf": {"name": OFFICE_LEAF_WRITER, "type": "worker"},
            "task": "Prepare the outgoing version of {{args.doc_path}}.",
            "effects": "workspace_write",
        },
        {
            "id": "approve-send",
            "type": "gate_step",
            "reason": "document leaves the company: human approval required",
        },
        {
            "id": "send",
            "type": "agent_step",
            "leaf": {"name": OFFICE_LEAF_SENDER, "type": "worker"},
            "task": "Distribute {{steps.draft.output}} to the approved recipients.",
            "effects": "external",
        },
    ],
}

OFFICE_WORKFLOW_EXAMPLES: dict[str, dict] = {
    "office-contract-review": CONTRACT_REVIEW_EXAMPLE,
    "office-weekly-report": WEEKLY_REPORT_EXAMPLE,
    "office-document-distribution": DOCUMENT_DISTRIBUTION_EXAMPLE,
}

#: The office leaf catalog — what admission's leaf-capability binding checks
#: these examples against (the tenant's real catalog supersedes this in prod).
OFFICE_LEAF_CATALOG: frozenset[str] = frozenset(
    {OFFICE_LEAF_PARSER, OFFICE_LEAF_ANALYST, OFFICE_LEAF_WRITER, OFFICE_LEAF_SENDER}
)
