# Phase 2: 提取器 (T0→T2)

> **依赖**: 01-hooks (RESPONSE_COMPLETE/PRE_COMPACTION) + 02-t0-layer (T0 写入)
> **交付**: extract_agent.py LLM 提取 + pattern 降级 + hooks handler 注册

---

## 1. 当前状态 (基于源码)

### 1.1 现有提取机制 (`memory_service.py`)

当前 Hive 的记忆提取有两条路径:

**路径 1: LLM 提取** (`_extract_facts_with_llm`, `memory_service.py:772`)
- 输入: messages (对话消息)
- 构建 conversation_text (含工具结果和文件写入)
- 调 LLM → 要求返回 **JSON array** (事实列表)
- 每个 fact 有 category + content + importance
- 失败时 fallback 到路径 2

**路径 2: Pattern 提取** (`_extract_facts_simple`, `memory_service.py:876`)
- 正则匹配: 用户纠正/偏好/决策/指令/项目事实
- 5 种 pattern 类别
- 无 LLM 依赖

**触发时机**: `on_conversation_end()` (`memory_service.py:325`) → `persist_runtime_memory()` (`memory_service.py:361`)
- 在 WebSocket 对话结束时调用
- 不是通过 hooks, 而是直接调用

**关键问题**:
1. 提取结果写入 **SQLite** (PersistentMemoryStore), 不是 MD 文件
2. 要求 **JSON 输出**, 弱模型不稳定
3. **不是** fire-and-forget — 同步等待, 阻塞响应返回
4. 没有增量 cursor — 每次全量处理
5. 没有 coalescing — 无并发保护

### 1.2 提取结果存储

```python
# memory_service.py → _update_agent_memory → PersistentMemoryStore
# 写入: {agent_data}/{agent_id}/memory/{agent_id}.db (SQLite)
# 表: semantic_facts (id, content, category, importance, created_at, updated_at)
```

**违反 v9 原则**: MD = Source of Truth。提取结果应该写 MD 文件 (learnings/*.md), 不是 SQLite。

---

## 2. Claude Code 对标 (基于源码)

### 2.1 extractMemories 架构 (`extractMemories.ts`)

**核心设计:**
- **Forked agent**: `runForkedAgent()` 共享 prompt cache, 独立执行
- **Cursor 增量**: `lastMemoryMessageUuid` — 只处理上次 cursor 以来的新消息
- **Coalescing**: `inProgress` + `pendingContext` — 并发安全, trailing run
- **Fire-and-forget**: `void extractMemoriesModule.executeExtractMemories()` — 不阻塞用户
- **节流**: `turnsSinceLastExtraction` — 可配每 N 轮提取一次

**闭包状态** (`extractMemories.ts:297-326`):
```typescript
let lastMemoryMessageUuid: string | undefined  // cursor
let inProgress = false                          // mutual exclusion
let turnsSinceLastExtraction = 0                // throttle
let pendingContext: {...} | undefined           // coalescing stash
const inFlightExtractions = new Set<Promise<void>>()  // drain tracking
```

**Mutual exclusion** (`extractMemories.ts:348-360`):
- 如果 main agent 已经直接写了 memory → skip extraction + advance cursor

### 2.2 提取提示词 (`prompts.ts`)

**opener() 函数:**
```
You are now acting as the memory extraction subagent.
Analyze the most recent ~{N} messages above and use them to update your persistent memory systems.

Available tools: Read, Grep, Glob, read-only Bash, Edit/Write for memory dir only.

Turn budget: turn 1 = parallel reads, turn 2 = parallel writes.

MUST only use content from the last ~{N} messages. No grepping source files.
```

**4 类型 taxonomy**: user, feedback, project, reference (导入自 `memdir/memoryTypes.ts`)
**What NOT to save**: code patterns, git history, debugging recipes, CLAUDE.md content
**存储**: 两步 — Step 1 写 topic file, Step 2 更新 MEMORY.md 索引

### 2.3 工具权限

```
ALLOWED:
- FILE_READ (unrestricted)
- GREP, GLOB (unrestricted)
- BASH (read-only: ls/find/cat/stat/wc/head/tail)
- FILE_EDIT / FILE_WRITE (memory directory only)

DENIED: All other tools, write-capable bash, MCP, Agent
```

---

## 3. 目标状态

### 3.1 新的提取器架构

```python
# services/extract_agent.py — 全新文件

class ExtractAgent:
    """LLM-driven memory extraction sub-agent (对齐 Claude Code extractMemories)."""
    
    # Closure-scoped state (per-agent)
    _cursors: dict[uuid.UUID, int] = {}          # agent_id → last processed msg index
    _in_progress: dict[uuid.UUID, bool] = {}     # mutual exclusion
    _pending: dict[uuid.UUID, dict] = {}          # coalescing stash
    
    async def extract(self, agent_id, messages, source, session_id):
        """Main entry point — fire-and-forget from RESPONSE_COMPLETE hook."""
        ...
    
    async def drain(self, agent_id, timeout_s=60):
        """Wait for pending extractions to complete (SESSION_CLOSE)."""
        ...
```

### 3.2 写入目标: T2 learnings/*.md (不是 SQLite)

```
输入: 对话消息 (还在内存)
输出: 追加到 learnings/errors.md, insights.md, requests.md
格式: - [YYYY-MM-DD] description (MD bullet)
```

### 3.3 提取提示词 (对齐 Claude Code, 适配 Hive)

```markdown
# Memory Extraction

You are the memory extraction sub-agent for {agent_name}.
Analyze the last ~{N} messages and extract anything worth remembering.

## Available Tools
- read_file (learnings/ directory only)
- write_file (learnings/ directory only — append mode)

## Extraction Types
| Type | Target File | Signal |
|------|-------------|--------|
| User correction/preference | learnings/insights.md | "don't", "always", "I prefer" |
| Agent insight/discovery | learnings/insights.md | "I found", "the reason is" |
| Execution error | learnings/errors.md | Tool failures, unexpected results |
| Capability gap | learnings/requests.md | "if only", "I wish", missing tool |

## Rules
1. Read existing learnings files first — don't duplicate
2. Only extract from the provided messages — don't grep source files
3. Format: `- [YYYY-MM-DD] description`
4. Extract MORE rather than less — heartbeat will curate quality later
5. Skip ephemeral task details (those belong in focus.md)
6. Maximum 3 turns total
```

### 3.4 降级路径

```
LLM 可用 → ExtractAgent.extract() 用 LLM → T2
LLM 不可用 → _extract_facts_simple() pattern 匹配 → T2
两个都失败 → log, T0 完整, 下次 heartbeat 可从 T0 补提取
```

---

## 4. 实现步骤

### Step 1: 新建 services/extract_agent.py

核心组件:
- `ExtractAgent` 类 — 管理 per-agent cursor, mutual exclusion, coalescing
- `EXTRACT_PROMPT` — 提取指令模板 (见 §3.3)
- `_run_extraction()` — 调用 LLM (invoke_agent with restricted tools)
- `_pattern_fallback()` — 重用 `_extract_facts_simple()` 逻辑但输出改为 MD 追加
- `_append_to_learnings()` — 追加条目到 T2 文件 (errors/insights/requests.md)

### Step 2: 注册到 RESPONSE_COMPLETE hook

```python
# hooks_setup.py
extract_agent = ExtractAgent()

async def _on_response_complete(ctx: HookContext):
    """Fire-and-forget: 提取 + T0 写入"""
    if ctx.source == "heartbeat":  # 心跳不触发提取
        return
    asyncio.create_task(
        extract_agent.extract(
            agent_id=ctx.agent_id,
            messages=ctx.messages,
            source=ctx.source,
            session_id=ctx.session_id,
        )
    )

hook_registry.register(HookEvent.RESPONSE_COMPLETE, _on_response_complete)
```

### Step 3: 注册到 PRE_COMPACTION hook

```python
async def _on_pre_compaction(ctx: HookContext):
    """压缩前: 同步提取即将丢失的上下文"""
    await extract_agent.extract(
        agent_id=ctx.agent_id,
        messages=ctx.metadata.get("messages_to_compress", []),
        source="compaction",
        session_id=ctx.session_id,
    )

hook_registry.register(HookEvent.PRE_COMPACTION, _on_pre_compaction)
```

### Step 4: SESSION_CLOSE drain

```python
async def _on_session_close(ctx: HookContext):
    """连接关闭: drain 所有 pending 提取"""
    await extract_agent.drain(ctx.agent_id, timeout_s=10)

hook_registry.register(HookEvent.SESSION_CLOSE, _on_session_close)
```

### Step 5: 迁移旧代码

- `memory_service.py` 的 `_update_agent_memory()` → 标记 deprecated
- `on_conversation_end()` → 改为 emit SESSION_CLOSE hook (不直接调用 persist)
- SQLite fact store → 保留作为 FTS5 搜索索引 (DB 辅助角色), 但不再是 source of truth

---

## 5. 验收标准

| # | 验收项 | 方法 |
|---|--------|------|
| V1 | extract_agent.py 存在 | `ls services/extract_agent.py` |
| V2 | RESPONSE_COMPLETE 触发提取 | 发消息含 "记住这个" → learnings/insights.md 有新条目 |
| V3 | T2 文件格式正确 | `head learnings/insights.md` → `- [2026-04-05] ...` 格式 |
| V4 | PRE_COMPACTION 触发提取 | 长对话压缩前 → learnings 有新条目 |
| V5 | LLM 不可用时 pattern 降级 | 断开 LLM → 用户说 "不要用 X" → insights.md 有条目 |
| V6 | Coalescing 正常 | 快速连发 3 条 → 不重复提取 |
| V7 | SESSION_CLOSE drain | 关浏览器 → 日志显示 drain completed |

---

## 6. 影响文件

| 文件 | 改动类型 | 改动范围 |
|------|---------|---------|
| `services/extract_agent.py` | 新建 | ExtractAgent + prompt + pattern fallback, ~250 行 |
| `runtime/hooks_setup.py` | 修改 | 注册 RESPONSE_COMPLETE + PRE_COMPACTION + SESSION_CLOSE handlers, ~30 行 |
| `services/memory_service.py` | 修改 | on_conversation_end → emit hook; deprecated old extract path |
| `api/websocket.py` | 修改 | persist_runtime_memory → emit SESSION_CLOSE, ~5 行 |
