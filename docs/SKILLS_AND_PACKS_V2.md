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

Platform built-in Skill capsules are preinstalled into every Agent workspace through the global Skill registry and
`install_active_skill_package()`. This makes the Agent skills panel match the company skill surface while preserving
the governance boundary: loading a Skill gives the agent guidance and resources only; it does not unlock tool schemas,
credentials, MCP servers, Office tools, or external actions.

| Folder | Purpose |
|--------|---------|
| `skill-creator` | Create or update reusable Skills through the governed Skill creation path. |
| `web-research` | Advanced Web Research: escalation above core `web_search` / `web_fetch` for vertical search, crawler extraction, contradiction handling, and source-quality work. |
| `feishu-integration` | Feishu playbook for configured Feishu tools and channel boundaries. |
| `dingtalk-integration` | DingTalk inbound channel behavior and trigger/reply boundary. |
| `email-guide` | Email send/read/reply playbook using configured email tools. |
| `plaza-guide` | Agent Circle / plaza interaction playbook. |
| `mcp-installer` | MCP import/install administration. |
| `skill-marketplace` | Merged external skill discovery and vetting package. Replaces the old split discovery/vetting skills. |

### Capability Pack Skills

Office has exactly one pack skill entrypoint. The Skill capsule is preinstalled, while the Office pack activation
remains governed separately.

| Pack | Skill folder | Default? | Purpose |
|------|--------------|----------|---------|
| `office_pack` | `office-productivity` | Skill: Yes; pack activation: inactive | Governed entrypoint for DOCX, XLSX, PPTX, PDF, meeting minutes, weekly reports, pitch decks, and delivery workflows. |

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
cd backend && source .venv/bin/activate && pytest tests/services/test_skill_surface_refactor.py tests/services/test_skill_seeder.py tests/services/test_system_skill_retirement.py tests/tools/test_audit.py tests/architecture/test_active_skill_installation_paths.py tests/tools/test_hr_handler.py -q
# 50 passed, 4 warnings

cd backend && source .venv/bin/activate && ruff check app/services/skill_seeder.py app/tools/handlers/hr.py app/tools/workspace.py app/services/agent_seeder.py tests/services/test_skill_surface_refactor.py tests/architecture/test_active_skill_installation_paths.py tests/tools/test_hr_handler.py
# All checks passed!

cd frontend && npm test -- --run src/pages/agent-detail/AgentDetailSections.test.tsx
# 71 passed

cd frontend && npm run build
# tsc + vite build succeeded
```
