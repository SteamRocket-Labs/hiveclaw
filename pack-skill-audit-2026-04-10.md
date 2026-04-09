# Pack Skill Audit (2026-04-10)

## Scope

Audit target:

- Static capability packs declared in `backend/app/tools/packs.py`
- Matching builtin/default skills shipped in:
  - `backend/app/templates/system_skills/`
  - `backend/agent_template/skills/`

This audit judges whether each pack has a usable skill guide and whether the guide is actually good enough for runtime use.

## Rubric

Each skill is scored on 4 dimensions:

1. Trigger quality: can the model tell when to load this skill?
2. Tool coverage: does the guide cover the real tool surface instead of a stale subset?
3. Operational guidance: does it give concrete decision rules and workflows?
4. Safety and anti-hallucination guardrails: does it tell the model what not to guess?

Score bands:

- 9.0-10.0: excellent
- 8.0-8.9: very good
- 7.0-7.9: good but still thin
- below 7.0: incomplete / risky

## Coverage Matrix

| Pack | Matching Skill | Coverage | Score | Judgment |
|------|----------------|----------|-------|----------|
| `web_pack` | `web-research/SKILL.md` | Full | 8.8 | Very good |
| `feishu_pack` | `feishu-integration/SKILL.md` | Full | 9.2 | Excellent |
| `plaza_pack` | `plaza-guide/SKILL.md` | Full | 7.8 | Good but thinner than the others |
| `email_pack` | `email-guide/SKILL.md` | Full | 8.7 | Very good |
| `mcp_admin_pack` | `MCP_INSTALLER.md` | Full | 8.5 | Very good |

## Detailed Judgment

### `web_pack` -> `web-research`

Strengths:

- Trigger intent is clear: current facts, public information, technical docs, market research
- Tool escalation path is explicit: `web_search` -> `web_fetch` -> `firecrawl_fetch` -> `xcrawl_scrape`
- Hallucination guardrails are strong

Weaknesses:

- It is more policy-heavy than workflow-heavy
- It lacks a short "known URL vs unknown URL" decision tree

Judgment:

- Already strong enough for production
- No immediate rewrite needed

### `feishu_pack` -> `feishu-integration`

Strengths:

- Now fully matches the current tool surface, including:
  - `feishu_base_app_create`
  - `feishu_base_record_delete`
  - `feishu_doc_delete`
  - `feishu_approval_create`
  - `feishu_approval_query`
  - `feishu_approval_get`
- Includes runtime prerequisites, identity guidance, explicit delete confirmation rules, and multi-step workflows
- Strong anti-guessing rules for tokens, IDs, approval codes, and Base schema

Weaknesses:

- Still dense because Feishu surface is large

Judgment:

- This is now an excellent skill
- The previous stale state is fixed

### `plaza_pack` -> `plaza-guide`

Strengths:

- Clear purpose and posting boundaries
- Good privacy guardrails
- Lightweight enough for quick loading

Weaknesses:

- The weakest of the pack guides in operational depth
- Missing examples of "good post" vs "bad post"
- Missing edge-case guidance for duplicate topics, follow-up comments, and when not to broadcast

Judgment:

- Good, but not yet an excellent skill
- Safe enough to keep
- Best candidate for the next non-blocking quality pass

### `email_pack` -> `email-guide`

Strengths:

- Strong decision boundary: when to use email vs Feishu/web/agent messaging
- Explicit reply-thread workflow using real `Message-ID`
- Strong anti-hallucination behavior

Weaknesses:

- Could use one concrete "compose, then attach, then send" example

Judgment:

- Very good and operationally reliable
- No urgent change needed

### `mcp_admin_pack` -> `MCP Tool Installer`

Strengths:

- Now parseable as a real skill with frontmatter
- Covers the full pack surface:
  - `discover_resources`
  - `import_mcp_server`
  - `list_mcp_resources`
  - `read_mcp_resource`
- Gives a clean search -> import -> verify -> read flow
- Keeps strong anti-fabrication rules

Weaknesses:

- Naming is not perfectly symmetric with the pack name
- It still leans toward installation/use-once flows more than long-term MCP operations

Judgment:

- Very good and now sufficient as the `mcp_admin_pack` guide
- If we later want perfect symmetry, we can split out a dedicated `mcp-admin-guide`, but it is not required

## Non-Pack Integration Skills

These skills exist but are not part of the static pack catalog in `backend/app/tools/packs.py`:

- `dingtalk-integration`
- `atlassian-rovo`

They should be judged separately from the pack system.

## Final Conclusion

After this round:

- All static packs now have a matching usable skill guide
- `feishu_pack` metadata and skill content are aligned with the real tool surface
- `mcp_admin_pack` no longer relies on a thin install-only note; it now has a full skill-grade guide

Current priority order for future skill-quality improvements:

1. `plaza-guide`
2. `web-research` decision tree polish
3. Optional `mcp_admin_pack` naming symmetry
