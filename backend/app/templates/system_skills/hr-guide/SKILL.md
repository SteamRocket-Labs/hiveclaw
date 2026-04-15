---
name: HR Guide
description: Creating new digital employees — clarifying the role, previewing blueprints, searching installable skills on ClawHub, and issuing the final create call.
tools:
  - search_clawhub
  - preview_agent_blueprint
  - create_digital_employee
is_system: true
---

# HR Guide

<role>
Use this skill when you are about to create a new digital employee. The
flow is: clarify the mission with the user → preview the blueprint →
optionally search ClawHub for installable domain skills → present plan
for confirmation → issue `create_digital_employee` only after explicit
"yes". Rushing any step produces an agent with vague identity, wrong
capabilities, or unnecessary pack installs. Never call
`create_digital_employee` speculatively.
</role>

<when_to_use>
- User asks to hire, create, or spawn a new digital employee / agent
- User describes a role and asks "can we build something that does X"
- You are the HR agent and a new-hire ticket routes to you
- An existing agent lacks persistent capability and the user explicitly wants a new colleague rather than extending current agent's skills
</when_to_use>

<do_not_use_when>
- The user only wants to install a skill into the current agent — use the Find Skills skill (`npx skills find`)
- The capability gap is a one-off task — just do it
- You are already inside a worker/delegation session (agent creation is an HR-only flow)
- The user is still in mission-discovery phase — clarify first, create later
</do_not_use_when>

## Tool Reference

<tool_reference>

### Tools

| Tool | Purpose |
|------|---------|
| `preview_agent_blueprint` | Render the structured soul.md + config preview for user confirmation without actually creating anything |
| `search_clawhub` | Search the ClawHub marketplace for installable domain skills; returns `slug` values you can pass to `create_digital_employee(clawhub_slugs=[...])` |
| `create_digital_employee` | **Sensitive, irreversible.** Actually spawn the new digital employee. Call ONLY after user confirms the blueprint |

### Supporting tools from other skills

- `search_memory` / `save_memory` — load **Memory Guide** — used to persist the user's hiring intent across sessions when the conversation spans days
- `read_file(relationships.md)` — check existing colleagues to avoid duplicating a role
- `set_trigger` — load **Trigger Management Guide** — used when the new agent's mission requires a cron/interval heartbeat schedule

</tool_reference>

## Hiring Workflow

<workflows>

### Stage 1 — Clarify the mission

Before any tool call, get the user's answer to all five:
- **Mission**: what this agent exists to do (2–3 sentences).
- **Primary users**: who it serves (names or roles).
- **Core outputs**: the deliverables produced daily / weekly.
- **Boundaries**: what it must never do.
- **First task**: the first concrete thing it should do after creation.

If any is missing, ask. Do not guess. A missing boundary or output is the #1 cause of bad agent identity.

### Stage 2 — Check existing colleagues

```
read_file(path="relationships.md")
```
If a colleague already has the same role, explain and ask whether user wants to extend that agent instead.

### Stage 3 — Search installable skills (optional)

Only when the mission clearly needs a domain-specific capability pack (research, coding, competitive analysis, etc.):

```
search_clawhub(query="market research")
  → returns top skills with slug values
```

Rules:
- **Search only when user confirms they want installed skills.** Many agents are better off without any — identity first, capability later.
- **Keywords, not sentences.** `"competitor analysis"` beats `"I need an agent that can analyze competitors in the SaaS space"`.
- **At most 2–3 skill slugs** per new agent. More = diluted identity, longer cold start.

### Stage 4 — Preview the blueprint

```
preview_agent_blueprint(
  name=...,
  role_description=...,
  primary_users=[...],
  core_outputs=[...],
  boundaries=[...],
  first_mission=...,
  clawhub_slugs=[...] if Stage 3 returned any,
)
```
Show the preview to the user verbatim. Ask: "以上蓝图可以创建吗？" Do not proceed without explicit "yes" / "可以" / "创建吧".

### Stage 5 — Create

Only after explicit confirmation:

```
create_digital_employee(
  name=...,
  role_description=...,
  personality=...,
  # …same fields as preview, verified identical
)
```

Then, if the first task requires scheduled execution, load Trigger Management Guide and set up the agent's first trigger.

</workflows>

## Examples

<examples>

### Example A — Good: identity-first, no installs

User: "帮我建一个每天早上给我汇总 AI 融资新闻的 agent"

Flow:
1. Clarify: only one user (the requester), output = daily brief to Feishu, boundary = no speculation without source, first task = today's brief.
2. `read_file("relationships.md")` — no existing AI-news agent.
3. Skip `search_clawhub` — web_pack + web-research skill already covers it. No install needed.
4. `preview_agent_blueprint(name="AI融资观察", role_description="每日扫描中英文 AI 创投新闻，提炼金额/轮次/赛道，用中文发 Feishu 日报", ...)`
5. User confirms.
6. `create_digital_employee(...)` — returns agent_id.
7. Load Trigger Management Guide → set cron `0 8 * * *` + focus item for daily brief.

### Example B — Good: install domain skill

User: "建一个能做 PDF 报告编辑和二次加工的 agent"

Flow:
1. Clarify mission, users, outputs.
2. `search_clawhub(query="pdf editing")` → returns slug `anthropic-samples/pdf-editor`, 12K installs.
3. `preview_agent_blueprint(..., clawhub_slugs=["anthropic-samples/pdf-editor"])`
4. User confirms.
5. `create_digital_employee(..., clawhub_slugs=["anthropic-samples/pdf-editor"])`

### Example C — Bad: rushed create

```python
# ❌ Bad
# User says "建一个做数据分析的 agent"
create_digital_employee(
  name="数据分析",
  role_description="数据分析",          # ← generic, not actionable
  personality="严谨专业",
  ...
)
```

No clarification, no preview, no confirmation. The new agent wakes up without concrete first task and no identity anchor. Do the 5-stage flow.

### Example D — Bad: over-installing

```python
# ❌ Bad
search_clawhub("research") → 8 results
preview_agent_blueprint(..., clawhub_slugs=[s1, s2, s3, s4, s5, s6])
```

6 installed skills = diluted catalog, conflicting decision guides, slow cold start. Cap at 2–3 domain skills per new agent. Prefer identity + built-in tools first.

</examples>

## Anti-patterns

<anti_patterns>

- ❌ **Call `create_digital_employee` without a preceding `preview_agent_blueprint` + user confirmation** → irreversible; user has no chance to correct misread requirements.
- ❌ **Fill `role_description` with a job title** (`"数据分析"`, `"项目经理"`) → the new agent's soul.md will inherit the ambiguity. Write 2–3 sentences about mission, users, and outputs.
- ❌ **Install every marketplace skill that returns from `search_clawhub`** → 2–3 cap. More dilutes identity, slows cold start, and multiplies governance surface.
- ❌ **Use `search_clawhub` to find skills for your own current agent** → wrong tool. Use the Find Skills skill (local install via `npx skills`). `search_clawhub` is for marketplace slugs that must be passed to `create_digital_employee`.
- ❌ **Skip `read_file("relationships.md")` before creating** → you may duplicate an existing agent role and confuse the team.
- ❌ **Search with long natural-language queries** (`"I need something that helps with competitive analysis for SaaS"`) → `search_clawhub` is keyword-based. Use `"competitive analysis SaaS"`.
- ❌ **Pass `clawhub_slugs` fabricated from memory** → only pass slugs that appeared in a real `search_clawhub` tool result. Invented slugs will fail silently at install time.
- ❌ **Create an agent without a first task** → the new employee wakes up with no trigger, no focus item, no user context, and defaults to no-op heartbeat forever. Always set up the first mission right after creation.

</anti_patterns>

## Success Criteria

<success_criteria>
- Every `create_digital_employee` call is preceded by: mission clarification with five questions, `relationships.md` check, `preview_agent_blueprint`, and explicit user confirmation.
- `search_clawhub` is only called when the mission clearly requires a domain-specific installable skill; queries are keyword form, not sentences.
- `clawhub_slugs` passed to `create_digital_employee` never exceed 3 items and always originate from a real `search_clawhub` tool result.
- After creation, the new agent has a concrete first task (either a trigger + focus entry or an explicit user hand-off) before the HR session ends.
</success_criteria>
