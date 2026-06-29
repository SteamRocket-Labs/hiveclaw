# Skills & Capability Packs V2 — Current Truth Surface

| Field | Value |
|------|-------|
| Status | Implemented truth surface |
| Date | 2026-06-28 |
| Owner | Hive Engineering |

This document records the current runtime shape after the CCPlus skill-surface cleanup. Older proposals that treated basic workspace, memory, messaging, trigger, delegation, planning, or single-purpose Office flows as default Skills are obsolete.

## Current Skill Model

Skill is a progressive-disclosure capability capsule. Loading a Skill adds instructions, references, templates, evals, or workflow guidance. Loading a Skill does not expose tool schemas by itself, does not bypass governance, and does not replace Core tool/runtime contracts.

Executable work still runs through its governed runtime:

- Core tools: always-on agent substrate such as workspace, memory, work ledger, messaging, trigger, search/fetch, and basic file/write behavior.
- Deferred tools/add-ons: discovered through `tool_search` and policy-gated before injection/execution.
- Workflow/subagent/code execution: executed through their own governed runtime entrypoints.
- Skill resources: read by progressive disclosure only when the task needs them.

## Current Default And Optional Skills

### Default

Only one default skill remains:

| Folder | Purpose |
|--------|---------|
| `skill-creator` | Create or update reusable Skills through the governed Skill creation path. |

### Optional Built-In / System Skills

These are not default. They are loaded only when the task needs their playbook.

| Folder | Current role |
|--------|--------------|
| `web-research` | Advanced Web Research: escalation above core `web_search` / `web_fetch` for vertical search, crawler extraction, contradiction handling, and source-quality work. |
| `feishu-integration` | Feishu playbook for configured Feishu tools and channel boundaries. |
| `dingtalk-integration` | DingTalk inbound channel behavior and trigger/reply boundary. |
| `email-guide` | Email send/read/reply playbook using configured email tools. |
| `plaza-guide` | Agent Circle / plaza interaction playbook. |
| `atlassian-rovo` | Atlassian/Rovo integration playbook. |
| `mcp-installer` | MCP import/install administration. |
| `skill-marketplace` | Merged external skill discovery and vetting package. Replaces the old split discovery/vetting skills. |

### Capability Pack Skills

Office now has exactly one pack skill entrypoint:

| Pack | Skill folder | Default? | Purpose |
|------|--------------|----------|---------|
| `office_pack` | `office-productivity` | No | Governed entrypoint for DOCX, XLSX, PPTX, PDF, meeting minutes, weekly reports, pitch decks, and delivery workflows. |

The single-purpose Office skill copies were removed from both app templates and pack skills. `office-productivity` is the only active Office Skill entrypoint.

## Retired Skill Slugs

These slugs are retired and may only appear in cleanup/retirement lists or historical docs:

- `complex-task-executor`
- `workspace-guide`
- `trigger-guide`
- `memory-guide`
- `messaging-guide`
- `delegation-guide`
- `find-skills`
- `skill-vetter`
- `docx-generator`
- `xlsx-processor`
- `pptx-generator`
- `pdf-generator`
- `weekly-report-generator`
- `meeting-minutes`
- `pitch-deck-generator`

## Memory Boundary

Memory is no longer delegated to `memory-guide`. The live runtime prompt and `save_memory` schema define the boundary directly:

- `save_memory` is explicit user-commanded only.
- Routine observations, tool output, code facts, file paths, debugging steps, and task-local state are not durable memory writes.
- Task-local state belongs in the Work Ledger or workspace artifacts.
- Accepted T3 changes go through Consolidator -> Memory Gate -> Platform Gate.

## Verification Evidence

Current verification commands:

```bash
cd backend && source .venv/bin/activate && pytest tests/services/test_skill_surface_refactor.py -q
# 7 passed

cd backend && source .venv/bin/activate && pytest tests/services/test_skill_surface_refactor.py tests/services/test_system_skill_retirement.py tests/services/test_skill_seeder.py tests/services/test_system_skill_templates.py tests/templates/test_skill_capability_alignment.py tests/services/test_prompt_contracts.py tests/templates/test_skill_package_structure.py tests/services/test_productivity_skill_templates.py tests/services/test_pack_skill_alignment.py tests/tools/test_audit.py -q
# 110 passed, 2 skipped

cd backend && source .venv/bin/activate && ruff check app/runtime/prompt_sections/memory.py app/services/agent_seeder.py app/services/skill_seeder.py app/skills/retired.py app/tools/handlers/memory.py tests/services/test_skill_surface_refactor.py tests/services/test_system_skill_retirement.py tests/services/test_productivity_skill_templates.py tests/services/test_prompt_contracts.py tests/services/test_system_skill_templates.py tests/templates/test_skill_capability_alignment.py tests/templates/test_skill_package_structure.py tests/templates/test_skill_creator_quality.py tests/services/test_skill_seeder.py tests/runtime/test_recovery_manifest_persistence.py tests/runtime/test_session_skill_lifecycle.py tests/runtime/test_prompt_eval.py tests/runtime/test_task_eval.py
# All checks passed!

cd backend && source .venv/bin/activate && pytest tests -q
# 5317 passed, 2 skipped, 4 warnings
```
