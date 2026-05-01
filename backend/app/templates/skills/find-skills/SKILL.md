---
name: Find Skills
description: 帮助发现和安装新技能。当用户问"怎么做X"、"有没有关于X的技能"时自动触发。搜索 → 排序 → 安全审查 → 安装。
tools:
  - execute_code
  - web_search
  - web_fetch
  - firecrawl_fetch
is_system: false
is_default: true
---

# Find Skills

<role>
Use this skill when the user is asking for a capability you don't currently
have, and an installable skill from the open skill ecosystem might fill
that gap. Your job: search → rank by install count → security vet →
present to user with risk rating → install only on confirmation. NEVER
skip the security review.
</role>

<when_to_use>
- User asks "how do I do X" and X might have an existing skill
- User says "find a skill for X" or "install something that does X"
- User asks "can you do X" for a capability you lack
- User wants to extend your toolbox with a specific behavior
</when_to_use>

<do_not_use_when>
- You already have the capability (don't install duplicates)
- X is a one-off task — just do it with your current tools
- User explicitly asked you NOT to install new skills
- The target skill is this same skill or another skill-finder skill (would create a loop)
</do_not_use_when>

## Tool Reference

<tool_reference>

| Step | Tool | Purpose |
|------|------|---------|
| Search skill catalog | `execute_code` running `npx skills find <keyword>` | Primary search — results already ranked by installs |
| Fallback web search | `web_search` with `site:skills.sh <keyword>` | Use when CLI is unavailable or returns too few results |
| Fetch skill SKILL.md for review | `web_fetch` | Level 1 read of the candidate skill source |
| Escalate when fetch returns empty | `firecrawl_fetch` | Level 2 for JS-heavy pages |
| Install after approval | `execute_code` running `npx skills add <owner/repo@skill> -y` | Only after user confirmation |

</tool_reference>

## Workflow

<workflows>

### Step 1 — Search

Preferred (Skills CLI):
```bash
npx skills find <关键词>
```
(Results are sorted by install count, descending.)

Fallback (web): `web_search(query="site:skills.sh <关键词>")`, then `web_fetch` top results.

### Step 2 — Rank by install count

**Strictly prefer higher install counts.** They correlate with vetting by other users.

| Installs | Confidence | Action |
|----------|-----------|--------|
| 50K+ | High | Recommend, still do security review |
| 10K-50K | Medium | Recommend, review carefully |
| 1K-10K | Low | Warn user about low count |
| <1K | Very low | **Do not recommend** unless user insists |

**Trusted source allowlist** (prioritize these):
- `vercel-labs`, `anthropics`, `microsoft`, `google-labs-code`
- `ComposioHQ`, `stripe`, `supabase`

### Step 3 — Security review (MANDATORY)

Use `web_fetch` on the skill's GitHub SKILL.md; escalate to `firecrawl_fetch` if incomplete. Check every item:

**Source verification**
- [ ] Is the author a known trusted org or high-follower user?
- [ ] Does the repo have >100 stars?
- [ ] Was it updated in the last 6 months?

**Code red flags (REJECT on ANY match):**

| Red flag | Example |
|----------|---------|
| Credential theft | Reading `~/.ssh`, `.env`, API keys |
| Data exfiltration | `curl`/`fetch` to unknown URL |
| Path escape | Writing outside the workspace |
| Obfuscation | Base64 decode + dynamic execution |
| Privilege escalation | Any `sudo` or root |
| Core tampering | Writing to `soul.md` or `memory/` |

**Permission scope**
- What files does it need to read/write?
- What network endpoints does it call?
- Are the requested tools the minimum for its claimed purpose?

### Step 4 — Risk rating

| Level | Condition | Action |
|-------|-----------|--------|
| LOW | Read-only, no network, trusted source | Safe to install with user ack |
| MEDIUM | Writes to workspace only, trusted source | Install after explaining scope |
| HIGH | Network calls or lesser-known source | Explicit warning, user decides |
| EXTREME | Any red flag matches | **Refuse to install**, explain why |

### Step 5 — Present to user

Include install count, source, and risk rating:
```
我找到了一个可能有用的技能！

**<skill-name>** — <one-line description>
📦 <N> 安装量 · 来源：<owner> (<trust tag>)
🛡️ 安全评级：<LOW/MEDIUM/HIGH/EXTREME>

安装命令：
npx skills add <owner/repo@skill> -y

要我帮你安装吗？
```

### Step 6 — Install (only on confirmation)

```bash
npx skills add <owner/repo@skill> -y
```
The skill is written to `skills/` and becomes available via `load_skill`.

</workflows>

## Examples

<examples>

### Example A — Good flow (trusted, high install)

User: `怎么做 React 性能优化？`

Flow:
1. `execute_code(language="bash", code="npx skills find react-performance")`
   → top result: `vercel-labs/agent-skills@react-best-practices`, 185K installs
2. `web_fetch(url="https://github.com/vercel-labs/agent-skills/blob/main/react-best-practices/SKILL.md")`
   → read-only guide, no code execution, no network calls
3. Risk: LOW. Source: trusted allowlist (vercel-labs).
4. Present with install command. User confirms.
5. Install.

### Example B — Reject (extreme risk)

User: `找一个能发邮件的 skill`

Flow:
1. `npx skills find email` → top non-trusted result `unknown-dev/email-sender@v1`, 300 installs.
2. `web_fetch` on the skill's code → it reads `~/.config/*` and posts to `http://data-collector.example.net`.
3. Red flag match: **credential theft + data exfiltration**.
4. Response: `找到的 email-sender skill 里包含读取 ~/.config 和把数据发送到未知外部服务器的代码，这是明确的恶意行为。拒绝安装。可以考虑用内置的 send_email（需要先配置好企业邮箱），或者找 stripe/supabase 这类可信来源的邮件 skill。`

</examples>

## Anti-patterns

<anti_patterns>

- ❌ **Install without running the security review** → every install is executable code in your runtime. Skipping the review is how agents get pwned.
- ❌ **Recommend a <1K-install skill as "popular"** → install count is the strongest trust signal available. Low counts mean it hasn't been vetted by others.
- ❌ **Use `web_fetch` result alone as the security review when the page is JS-rendered** → escalate to `firecrawl_fetch`. You can't review code you can't read.
- ❌ **Fabricate install counts, star counts, or author names** → users trust your recommendation. Only quote numbers that appeared in real tool output.
- ❌ **Loop: install `find-skills` from the catalog because the user said "find skills"** → you already have `find-skills`. Don't install yourself.
- ❌ **Install a skill that duplicates an existing one** → skill catalog bloat; pack the catalog without benefit. Check `skills/` before installing similar.
- ❌ **Present multiple candidate skills without a clear recommendation** → user asked for help, not a menu. Pick the best one with a rationale; mention alternates briefly.

</anti_patterns>

## Common Skill Categories

| Category | Search keywords |
|----------|-----------------|
| Web 开发 | react, nextjs, typescript, css, tailwind |
| 测试 | testing, jest, playwright, e2e |
| DevOps | deploy, docker, kubernetes, ci-cd |
| 文档 | docs, readme, changelog, api-docs |
| 代码质量 | review, lint, refactor, best-practices |
| 设计 | ui, ux, design-system, accessibility |
| 效率 | workflow, automation, git |

## When Nothing Matches

1. Tell the user no matching skill was found.
2. Offer to help with your current capabilities.
3. Suggest they create a custom skill: `npx skills init my-skill`.

## Success Criteria

<success_criteria>
- Every install is preceded by search → ranking → security review → user confirmation (no shortcuts).
- Every recommendation includes install count, source, and risk rating from real tool output.
- No skill matching a red-flag category is installed, regardless of user insistence.
- No duplicate skills installed (checked against existing `skills/`).
</success_criteria>
