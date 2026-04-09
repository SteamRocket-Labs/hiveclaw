# Phase 4: 提示词体系

> **依赖**: 03-extractor (Memory section 需要知道 T2 结构)
> **交付**: 系统提示词 section 化 + Memory section + System section + 蒸馏器提示词

---

## 1. 当前状态 (基于源码, 完整分析)

### 1.1 系统提示词组装 (`runtime/prompt_builder.py`, 323 行)

**三层架构**:
```
Frozen Prefix = agent_context + memory_snapshot + skill_catalog
── PROMPT_CACHE_BOUNDARY ──
Dynamic Suffix = active_packs + retrieval + system_prompt_suffix
```

**build_frozen_prompt_prefix()** (`prompt_builder.py:72-89`):
```python
def build_frozen_prompt_prefix(*, agent_context, memory_snapshot="", skill_catalog=""):
    parts = [agent_context]
    if memory_snapshot:
        parts.append(memory_snapshot)
    if skill_catalog:
        parts.append(skill_catalog)
    return "\n\n".join(parts)
```
问题: 简单拼接, 没有结构化 section header。

**build_dynamic_prompt_suffix()** (`prompt_builder.py:112-149`):
- `_render_active_packs()` → "## Active Capability Packs"
- `retrieval_context` → 直接追加
- `system_prompt_suffix` → 直接追加
- 如果有 `suggested_pack_names` → "## Likely Capability Packs"

**assemble_runtime_prompt()** (`prompt_builder.py:166-229`):
- 预算控制: `budget = compute_system_prompt_budget(context_window_tokens)`
- 超预算: 截断 frozen prefix (尾部截断), 保留 dynamic suffix
- CACHE_BOUNDARY 标记: `__PROMPT_DYNAMIC_BOUNDARY__`

**build_runtime_prompt()** (`prompt_builder.py:235-322`):
- 完整入口: context budget → build_agent_context → frozen prefix → knowledge → dynamic suffix → assemble
- 知识注入: `fetch_relevant_knowledge()` → 放入 dynamic suffix

### 1.2 Agent 上下文 (`agent_context.py:179-329`)

**build_agent_context()** 完整流程:

1. **Identity** (`agent_context.py:232-251`):
```python
_identity_by_mode = {
    "coordinator": f"You are {agent_name}, operating in coordinator mode...",
    "task": f"You are {agent_name}, executing an assigned task autonomously...",
    "heartbeat": f"You are {agent_name}, in self-evolution mode...",
}
# default: f"You are {agent_name}, an enterprise digital employee. You assist users..."
```

2. **Role description** (`agent_context.py:255-256`):
```python
if role_description:
    identity_parts.append(f"### Role\n{role_description}")
```

3. **Soul** (`agent_context.py:212`):
```python
soul = _read_file_safe(tool_ws / "soul.md", soul_budget) or _read_file_safe(data_ws / "soul.md", soul_budget)
# soul_budget = 16000 chars (or from budget_profile)
```

4. **Skills** (`agent_context.py:222-226`):
```python
skills_text = _load_skills_index(agent_id, budget_chars=max(skill_budget, 800))
```

5. **Relationships** (`agent_context.py:229`):
```python
relationships = _read_file_safe(data_ws / "relationships.md", relationships_budget)
```

6. **Channel integrations** (`agent_context.py:258-282`): 查 DB 获取已配置渠道

7. **Company info** (`agent_context.py:284-329`): 从 tenant_settings / system_settings 读 company_intro

8. **Runtime metadata** (`_build_runtime_metadata_sections`):
   - Active triggers (从 DB 查询)
   - Current conversation user name

**拼接方式** (`agent_context.py` 约 330-360):
```python
parts = ["\n".join(identity_parts)]
if soul:
    parts.append(soul)
for cp in context_parts:
    parts.append(cp)
return "\n\n".join(parts)
```

**完全没有 section headers** — soul.md 直接拼接在 identity 后面, 没有分隔标记。

### 1.3 缺失的 sections (对比 Claude Code)

| Section | Claude Code | Hive 当前 |
|---------|-----------|----------|
| § Identity | 简洁 + 输出风格 | ✅ 有 (但太简单) |
| § System | 详细的工具/权限/hooks/压缩 | ❌ **完全缺失** |
| § Doing Tasks | 代码风格/安全/完成度 | ❌ **完全缺失** |
| § Executing Actions | 风险控制 | ❌ **完全缺失** |
| § Using Your Tools | 工具偏好/批量 | ❌ **完全缺失** |
| § Tone and Style | 风格/格式 | ❌ **完全缺失** |
| § Output Efficiency | 简洁 | ❌ **完全缺失** |
| § Memory | 4 类型+存取规则+示例 | ❌ **完全缺失** (memory 只通过 retriever 注入) |
| § Environment | 时间/平台/模型 | ⚠️ 部分 (current_user_name 有, 其他无) |
| § Skills | 技能目录 | ✅ 有 |
| § Relationships | 同事关系 | ✅ 有 |
| § Active Packs | 能力包 | ✅ 有 |
| § Knowledge | 检索结果 | ✅ 有 |

---

## 2. Claude Code 对标 (基于源码)

### 2.1 系统提示词结构 (`constants/prompts.ts`)

**20 个 section, 严格分层:**

```
STATIC (cacheable):
  1. SimpleIntro — identity + output style + cyber risk
  2. System — tool output, permissions, hooks, compression, tags
  3. DoingTasks — code style, security, completeness, verification
  4. ExecutingActions — risk control, blast radius, confirmation
  5. UsingTools — dedicated tools > bash, parallel calls, task tools
  6. ToneStyle — emoji, brevity, references, colons
  7. OutputEfficiency — concise, lead with answer

── SYSTEM_PROMPT_DYNAMIC_BOUNDARY ──

DYNAMIC (per-session, cached via registry):
  8. SessionGuidance — AskUser, Agent, Skill tools
  9. Memory — 4 types, when/how to save, MEMORY.md
  10. EnvironmentInfo — cwd, git, platform, model, cutoff
  11-20. Language, OutputStyle, MCP, Scratchpad, etc.
```

### 2.2 Memory section (`memdir.ts` → `loadMemoryPrompt()`)

核心内容:
- `# auto memory` title
- 内存文件目录位置
- 4 种类型 (user/feedback/project/reference) 各含: name, description, when_to_save, how_to_use, examples
- "What NOT to save" section
- "How to save memories" (两步: file + MEMORY.md index)
- "When to access memories"
- "Before recommending from memory" (验证存在性)
- "Memory and other persistence" (Plans/Tasks 区分)
- MEMORY.md 入口 (if exists, 加载前 200 行)

---

## 3. 目标状态

### 3.1 Prompt Section 模块化

新建 `runtime/prompt_sections/`:

```python
# runtime/prompt_sections/__init__.py
from .identity import build_identity_section
from .system import build_system_section
from .tasks import build_tasks_section
from .tools import build_tools_section
from .memory import build_memory_section
from .environment import build_environment_section
```

### 3.2 § System section (完整模板)

```markdown
## System

You run inside the Hive agent kernel — a multi-round LLM loop with governed tool execution.

### Execution Model
- Each conversation is an invocation. Your memory snapshot is frozen at entry and doesn't change within the session.
- You can call tools in each round. The kernel runs up to 50 rounds per invocation.
- When context reaches 85% capacity, older messages are automatically compressed. Important information is extracted before compression.

### Tool Governance
- All tool calls go through governance: security zone check → capability gate → approval flow.
- Some tools require explicit user approval before execution.
- Capability packs (web, feishu, email, etc.) activate on-demand when you load a skill.

### Memory Integration
- Your long-term memory is in memory/*.md files (read-only during session).
- New learnings from this conversation are automatically extracted after each response.
- The heartbeat process curates your learnings into memory every ~45 minutes.
- The dream process refines memory and promotes patterns to your soul every ~4 hours.
- You don't need to manually manage memory — focus on the task. Use save_memory only for critical corrections.

### Context Compression
- At 85% context usage, older messages are summarized by LLM.
- Key information (files, code, decisions, user preferences) is preserved in the summary.
- Tool results older than 60 minutes are automatically cleared to save space.
- Full session logs are available in logs/ for recovery if needed.
```

### 3.3 § Memory section (完整模板)

```markdown
## Your Memory System

You have a 4-layer memory pyramid. Higher layers are more refined and permanent.

### Layer Structure
| Layer | Files | Purpose | Lifecycle |
|-------|-------|---------|-----------|
| T0 Raw Logs | logs/YYYY-MM-DD/*.md | Complete session records | 30 days |
| T1 Working | focus.md | Current task list | Volatile |
| T2 Episodic | learnings/*.md | Recent observations | Curated by heartbeat |
| T3 Semantic | memory/*.md + soul.md | Long-term knowledge | Refined by dream |

### How Memory Flows
1. Your conversations automatically produce T0 logs and T2 extractions
2. The heartbeat curates T2 → T3 every ~45 minutes (quality filtering)
3. The dream refines T3 and promotes patterns to soul.md every ~4 hours

### Using Memory Tools
- `save_memory(category, content)` — Directly write to T3 (use sparingly, heartbeat handles most curation)
- `recall(query)` — Search T3 via FTS5 for relevant knowledge

### What's Worth Remembering
- User corrections and preferences (highest value — weight 1.0)
- Project decisions and constraints
- Strategies that worked or failed
- NOT: code patterns, file paths, debugging steps (these are in the workspace)
- NOT: ephemeral task details (those belong in focus.md)

### Current Memory State
{memory_snapshot — T3 files content injected here}
```

### 3.4 § Doing Tasks section (对齐 Claude Code)

```markdown
## Doing Tasks

- Read existing code before suggesting changes. Don't propose modifications to files you haven't read.
- Don't add features, refactor code, or make "improvements" beyond what was asked.
- Don't add error handling for scenarios that can't happen. Trust internal code and framework guarantees.
- Don't create helpers or abstractions for one-time operations.
- When given an unclear instruction, consider it in the context of your role and current work.
- Be careful not to introduce security vulnerabilities: command injection, XSS, SQL injection.
- If you encounter an obstacle, diagnose why before switching approaches — don't retry blindly.
```

### 3.5 § Using Your Tools section

```markdown
## Using Your Tools

- Use `read_file` instead of executing cat/head/tail. Use `write_file` instead of echo redirection.
- Use `web_search` for information lookup. Use `web_fetch` to read specific URLs.
- Call multiple tools in parallel when they are independent — don't serialize unnecessarily.
- Break complex tasks into focused tool calls. Verify outcomes before proceeding.
- Use `load_skill` to access full skill instructions when a task matches a skill name.
```

### 3.6 提取器提示词 (EXTRACT_PROMPT)

详见 03-extractor.md §3.3。

### 3.7 心跳提示词 (HEARTBEAT.md 重写)

详见总纲 §15.3.2 完整模板。关键变更:
- Phase 2: ANALYZE → CURATE (策展)
- 读 T2 learnings (不是 evolution 文件)
- 写 T3 memory (不是 evolution 文件)
- 持续 session 指令块

### 3.8 梦境提示词 (DREAM.md 新建)

详见总纲 §15.4.2 完整模板。关键变更:
- 4 阶段: ORIENT → CONSOLIDATE → PROMOTE → INDEX+CLEANUP
- 读 T3 (不是 SQLite facts)
- 写 T3 精简 + soul.md (不是 JSON array)
- MD→MD 全路径

---

## 4. 实现步骤

### Step 1: 创建 prompt_sections/ 目录

```
runtime/prompt_sections/
├── __init__.py
├── identity.py        # build_identity_section(agent_name, role, soul_excerpt)
├── system.py          # build_system_section() — 静态文本
├── tasks.py           # build_tasks_section() — 静态文本
├── tools.py           # build_tools_section() — 静态文本
├── memory.py          # build_memory_section(memory_snapshot, focus_content)
└── environment.py     # build_environment_section(user_name, channel, timestamp)
```

### Step 2: 编写各 section 模块

每个模块返回 `str`:
```python
# runtime/prompt_sections/system.py
_SYSTEM_SECTION = """## System
...
"""

def build_system_section() -> str:
    return _SYSTEM_SECTION
```

### Step 3: 重构 build_frozen_prompt_prefix

```python
def build_frozen_prompt_prefix(*, agent_context, memory_snapshot="", skill_catalog=""):
    # 旧: 简单拼接
    # 新: section 组装
    sections = [
        build_identity_section(agent_context),    # § Identity (含 soul 核心段)
        build_system_section(),                    # § System (新增)
        build_tasks_section(),                     # § Doing Tasks (新增)
        build_tools_section(),                     # § Using Your Tools (新增)
    ]
    if skill_catalog:
        sections.append(f"## Skills Catalog\n{skill_catalog}")
    return "\n\n".join(s for s in sections if s)
```

### Step 4: 重构 build_dynamic_prompt_suffix

```python
def build_dynamic_prompt_suffix(...):
    parts = []
    # § Memory (新增 — 含 T3 快照 + focus + 使用指导)
    parts.append(build_memory_section(memory_snapshot, focus_content))
    # § Active Packs (已有)
    parts.append(_render_active_packs(active_packs))
    # § Knowledge (已有)
    if retrieval_context:
        parts.append(retrieval_context)
    # § Environment (新增)
    parts.append(build_environment_section(user_name, channel, timestamp))
    return "\n\n".join(s for s in parts if s)
```

### Step 5: 精简 build_agent_context

- 移除 company_intro 注入 (移到 environment section 或 relationships)
- Soul 只提取核心段落 (identity section), 不全文注入
- Relationships 保留但移到 frozen prefix 的 dedicated section
- Channel integrations 移到 environment section

### Step 6: 重写 HEARTBEAT.md

替换 `templates/HEARTBEAT.md` 全文 (详见总纲 §15.3.2)

### Step 7: 新建 DREAM.md

新建 `templates/DREAM.md` (详见总纲 §15.4.2)
修改 `auto_dream.py`: 从内嵌 Python 字符串 → 读取 DREAM.md 模板

---

## 5. 验收标准

| # | 验收项 | 方法 |
|---|--------|------|
| V1 | prompt_sections/ 目录存在, 6 个模块 | `ls runtime/prompt_sections/*.py` |
| V2 | System prompt 含 § System | 日志打印 system prompt → 搜索 "## System" |
| V3 | System prompt 含 § Memory | 搜索 "## Your Memory System" |
| V4 | System prompt 含 § Doing Tasks | 搜索 "## Doing Tasks" |
| V5 | System prompt 含 § Using Your Tools | 搜索 "## Using Your Tools" |
| V6 | Memory section 含 T3 快照 | prompt 中出现 feedback.md 内容 |
| V7 | CACHE_BOUNDARY 位置正确 | Static sections 在 boundary 前, Memory 在后 |
| V8 | Agent 理解记忆系统 | 问 "你的记忆怎么工作的?" → 正确回答 4 层金字塔 |
| V9 | HEARTBEAT.md 已重写 | 文件包含 OBSERVE/CURATE/ACT/LOG |
| V10 | DREAM.md 已创建 | `cat templates/DREAM.md` → 4 阶段协议 |
| V11 | auto_dream.py 读 DREAM.md | 不再有内嵌 `_AUTO_DREAM_SYSTEM_PROMPT` |

---

## 6. 影响文件

| 文件 | 改动类型 | 改动范围 |
|------|---------|---------|
| `runtime/prompt_sections/` | 新建 | 6 个模块, ~400 行 |
| `runtime/prompt_builder.py` | 重构 | frozen/dynamic 改为 section 组装, ~50 行改动 |
| `services/agent_context.py` | 精简 | 移除冗余注入, soul 提取核心段, ~40 行减少 |
| `templates/HEARTBEAT.md` | 重写 | 全文替换, ~80 行 |
| `templates/DREAM.md` | 新建 | 4 阶段模板, ~80 行 |
| `services/auto_dream.py` | 修改 | 删除内嵌 prompt, 改读 DREAM.md, ~20 行 |
