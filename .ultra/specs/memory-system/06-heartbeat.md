# Phase 5: 心跳重构 (KAIROS + T2→T3 策展)

> **依赖**: 03-extractor (T2 learnings 产出) + 05-prompts (HEARTBEAT.md 模板)
> **交付**: KAIROS 持续 session + HEARTBEAT.md 重写 + 增量 T2 读取 + 空转保护

---

## 1. 当前状态 (基于源码, 完整分析)

### 1.1 心跳执行 (`heartbeat.py:694-942`, ~250 行)

**_execute_heartbeat() 完整流程:**

1. **Lease 获取** (`heartbeat.py:700-705`):
   ```python
   lease_held = _try_acquire_heartbeat_lease(agent_id)
   ```

2. **Agent + Model 查询** (`heartbeat.py:717-743`):
   - 查 Agent (检查存在)
   - 设 execution identity (`set_agent_bot_identity`, source="heartbeat")
   - 查 LLMModel (primary_model_id fallback fallback_model_id)

3. **Evolution context 构建** (`heartbeat.py:748-777`):
   ```python
   recent_result = await db.execute(
       select(AgentActivityLog)
       .where(AgentActivityLog.agent_id == agent_id)
       .where(AgentActivityLog.action_type.in_(["chat_reply", "tool_call", ...]))
       .order_by(AgentActivityLog.created_at.desc())
       .limit(50)
   )
   evolution_context = await _build_evolution_context(agent_id, recent_activities)
   ```
   → 读 evolution/lineage.md, scorecard.md, blocklist.md + 最近 50 条 activity log

4. **Heartbeat 指令** (`heartbeat.py:779-782`):
   ```python
   heartbeat_instruction = _load_heartbeat_instruction(agent_id)
   if evolution_context:
       heartbeat_instruction += "\n\n" + evolution_context
   runtime_messages = [{"role": "user", "content": heartbeat_instruction}]
   ```
   → **每次只有 1 条 user message** (HEARTBEAT.md + evolution context)

5. **创建全新 Reflection Session** (`heartbeat.py:791-813`):
   ```python
   session = ChatSession(
       agent_id=agent_id,
       source_channel="heartbeat",
       title=f"💓 Heartbeat: {agent.name}"[:200],
   )
   ```
   → **每次心跳 = 全新 DB session + 全新 invocation**

6. **Invoke agent** (`heartbeat.py:845-873`):
   ```python
   result = await asyncio.wait_for(
       invoke_agent(AgentInvocationRequest(
           messages=runtime_messages,      # 只有 1 条 user message
           session_context=SessionContext(source="heartbeat"),
           core_tools_only=False,
           max_tool_rounds=25,
       )),
       timeout=300,  # 5 min hard limit
   )
   ```

7. **Post-heartbeat 处理** (`heartbeat.py:876-942`):
   - 保存 assistant reply 到 DB (`heartbeat.py:877-888`)
   - 解析 outcome_type + score (`_parse_heartbeat_outcome`, `heartbeat.py:891`)
   - 更新 last_heartbeat_at (`heartbeat.py:896-901`)
   - 写 activity log (`heartbeat.py:905-918`)
   - **服务端 evolution 文件写回** (`heartbeat.py:920-925`):
     ```python
     await asyncio.to_thread(_update_evolution_files, agent_id, outcome_type, heartbeat_score, summary)
     ```
   - Auto-dream gate check (`heartbeat.py:927-937`)

### 1.2 心跳 tick 调度 (`heartbeat.py:1056-1128`)

**_heartbeat_tick() 扫描逻辑:**
```python
# 扫描所有 heartbeat_enabled + status in (running, idle) 的 Agent
# 检查 active hours: _is_in_active_hours(agent.heartbeat_active_hours or "09:00-18:00", tz_name)
# 检查 interval: interval = timedelta(minutes=agent.heartbeat_interval_minutes or 120)
# 获取 lease: _try_acquire_heartbeat_lease(agent.id, now=now)
# 触发: asyncio.create_task(_execute_heartbeat(agent.id, lease_acquired=True))
```

### 1.3 HEARTBEAT.md (`templates/HEARTBEAT.md`, 80 行)

**当前 4 阶段:**
```
Phase 1: OBSERVE — 读 scorecard.md, blocklist.md, focus.md, ERRORS.md
Phase 2: ANALYZE — 思考最高优先级 (无工具调用)
Phase 3: ACT — 做 1 个动作 (8-12 tool calls)
Phase 4: EVOLVE — 评分 + 写 lineage.md + 更新 scorecard.md + blocklist check
```

**问题:**
1. Phase 1 读 **evolution 文件** (lineage/scorecard/blocklist) — 不读 T2/T3
2. Phase 2 是 **ANALYZE (思考)** — 不是策展
3. Phase 4 写 **evolution 文件** — 不写 T3 memory
4. 整体定位: **自主行动者** — 不是蒸馏器
5. **每次全新 invocation** — 无思维连续性

### 1.4 Evolution context (`_build_evolution_context`, heartbeat.py:约350行)

读取:
- `evolution/lineage.md` — 最近的心跳记录
- `evolution/scorecard.md` — 计数器
- `evolution/blocklist.md` — 禁止列表
- AgentActivityLog (DB) — 最近 50 条活动

**不读**: learnings/*.md (T2), memory/*.md (T3)

### 1.5 Evolution 文件写回 (`_update_evolution_files`, heartbeat.py:约410行)

写:
- `evolution/lineage.md` — 追加 `### HB-YYYY-MM-DD-HH:MM` 条目
- `evolution/scorecard.md` — 更新 total/useful/failed 计数
- `evolution/blocklist.md` — 3 连败自动追加

使用 `fcntl.flock` 文件锁保护并发写入。

---

## 2. Claude Code 对标

Claude Code **没有心跳机制**。心跳是 Hive Layer 3 独有能力。

但 KAIROS 的 tick 机制 (`<tick>` 注入持续 session) 可以被借鉴:
- **Tick 注入**: 新消息追加到现有 session, 不创建新 session
- **SleepTool**: 空闲时不消耗 API 调用
- **持续上下文**: 所有之前的思考都在对话历史中

---

## 3. 目标状态

### 3.1 KAIROS 持续 Session

```python
# heartbeat.py 新增模块级变量
_heartbeat_contexts: dict[uuid.UUID, list[dict]] = {}
_heartbeat_session_ids: dict[uuid.UUID, uuid.UUID] = {}  # agent → DB session id
_heartbeat_tick_counts: dict[uuid.UUID, int] = {}

def _reset_heartbeat_session(agent_id: uuid.UUID) -> None:
    """重置心跳持续 session (梦境完成/日切/进程重启时调用)"""
    _heartbeat_contexts.pop(agent_id, None)
    _heartbeat_session_ids.pop(agent_id, None)
    _heartbeat_tick_counts.pop(agent_id, None)
```

### 3.2 改造后的 _execute_heartbeat

```python
async def _execute_heartbeat(agent_id: uuid.UUID, *, lease_acquired=False):
    # ... agent/model lookup 保持不变 ...
    
    tick_count = _heartbeat_tick_counts.get(agent_id, 0) + 1
    _heartbeat_tick_counts[agent_id] = tick_count
    
    if agent_id not in _heartbeat_contexts:
        # ═══ 首次 tick: 完整初始化 ═══
        heartbeat_instruction = _load_heartbeat_instruction(agent_id)
        # 新: 读 T2 learnings (全量)
        t2_content = _read_t2_full(agent_id)
        # 新: 读 T3 memory (参考, 防重复)
        t3_summary = _read_t3_summary(agent_id)
        
        full_init = f"{heartbeat_instruction}\n\n## Current T2 Learnings\n{t2_content}\n\n## Current T3 Memory (reference)\n{t3_summary}"
        messages = [{"role": "user", "content": full_init}]
        
        # 创建新 DB session (只在首次)
        session_id = await _create_heartbeat_session(db, agent_id, agent, agent_participant_id)
        _heartbeat_session_ids[agent_id] = session_id
    else:
        # ═══ 后续 tick: <tick> + 增量 T2 ═══
        new_t2 = _read_incremental_t2(agent_id)
        if not new_t2:
            # 空转保护: T2 无新内容 → skip
            logger.info("[Heartbeat] Skip tick %d for %s: no new T2 entries", tick_count, agent_id)
            _release_heartbeat_lease(agent_id)
            await _touch_last_heartbeat(agent_id)
            return
        
        messages = _heartbeat_contexts[agent_id]
        tick_msg = f"<tick>{datetime.now(timezone.utc).isoformat()} tick #{tick_count}</tick>\n\n## New T2 Entries\n{new_t2}"
        messages.append({"role": "user", "content": tick_msg})
        session_id = _heartbeat_session_ids[agent_id]
    
    # Invoke (复用 session)
    result = await asyncio.wait_for(
        invoke_agent(AgentInvocationRequest(
            messages=messages,
            session_context=SessionContext(source="heartbeat", session_id=str(session_id)),
            max_tool_rounds=25,
        )),
        timeout=300,
    )
    
    # 追加 assistant 响应到持续上下文
    messages.append({"role": "assistant", "content": result.content})
    _heartbeat_contexts[agent_id] = messages
    
    # ... post-heartbeat 处理 (activity log, dream gate, etc.) ...
```

### 3.3 增量 T2 读取

```python
_t2_mtimes: dict[uuid.UUID, dict[str, float]] = {}  # agent → {filename: mtime}

def _read_incremental_t2(agent_id: uuid.UUID) -> str:
    """只读 mtime 变化的 T2 文件中的新条目。"""
    data_dir = Path(settings.AGENT_DATA_DIR) / str(agent_id) / "memory" / "learnings"
    new_entries = []
    current_mtimes = _t2_mtimes.get(agent_id, {})
    
    for f in ["errors.md", "insights.md", "requests.md"]:
        fpath = data_dir / f
        if not fpath.exists():
            continue
        mtime = fpath.stat().st_mtime
        if f in current_mtimes and mtime <= current_mtimes[f]:
            continue  # 未变化
        # 读取内容, 找到自上次以来的新条目
        content = fpath.read_text(encoding="utf-8", errors="replace")
        lines = [l for l in content.strip().splitlines() if l.startswith("- [")]
        # 简单策略: 只取最近的未处理条目
        # (更精确的 cursor 可以用行数或时间戳)
        new_entries.extend(lines[-10:])  # 最近 10 条
        current_mtimes[f] = mtime
    
    _t2_mtimes[agent_id] = current_mtimes
    return "\n".join(new_entries) if new_entries else ""
```

### 3.4 Session 重置触发

```python
# 在 DREAM_END hook handler 中:
async def _on_dream_end(ctx: HookContext):
    from app.services.heartbeat import _reset_heartbeat_session
    _reset_heartbeat_session(ctx.agent_id)
    logger.info("[Heartbeat] Session reset for %s after dream", ctx.agent_id)
```

### 3.5 HEARTBEAT.md 重写 (完整模板)

```markdown
# Heartbeat — Knowledge Curation Protocol

You are in heartbeat mode with a persistent session.
Your primary job: **curate T2 learnings into T3 memory** (like a librarian shelving new books).
Your secondary job: take one useful autonomous action if possible.

## Context
- This is tick #{tick_number} in your current session
- Your previous curation decisions are in the conversation history above
- You only see NEW T2 entries since last tick (injected after <tick> tag)

## Phase 1: OBSERVE (2-3 tool calls)

Read current state:
1. `read_file` focus.md — current priorities
2. If first tick: `read_file` memory/feedback.md, memory/strategies.md, memory/blocked.md
   If subsequent tick: skip (already in conversation context from previous tick)

## Phase 2: CURATE (main job, 5-8 tool calls)

For each new T2 entry, decide:
- **Worth keeping?** Is this durable knowledge or noise/ephemeral detail?
- **Which category?** feedback / knowledge / strategies / blocked / user
- **Already in T3?** Check conversation context for what's already in memory files

Write worthy entries to the appropriate T3 file using `read_file` then `write_file`:
- User corrections/preferences → memory/feedback.md
- Project/domain knowledge → memory/knowledge.md
- Effective strategies → memory/strategies.md
- Failed approaches → memory/blocked.md
- User profile info → memory/user.md

**Rules:**
- Append new entries, don't rewrite the file (dedup is the dream's job)
- Format: `- [YYYY-MM-DD] description`
- Skip if T3 already has essentially the same content
- When in doubt, keep it (false negative worse than false positive for T3)

## Phase 3: ACT (optional, 5-8 tool calls)

If T2 contains actionable items:
- Fix an error from learnings/errors.md
- Create/improve a skill in skills/
- Research a capability gap from learnings/requests.md
- Post to plaza or message a colleague agent

If nothing actionable: skip to Phase 4. Do NOT waste rounds.

## Phase 4: LOG (2-3 tool calls)

1. Append to evolution/lineage.md:
```
### CUR-{YYYY-MM-DD-HH:MM}
- Curated: {N entries from T2 → T3, categories touched}
- Skipped: {N entries, brief reasons}
- Action: {what autonomous action was taken, or "skip"}
- Score: {0-10}
```
2. Update evolution/scorecard.md counters

## Persistent Session Notes

You are running in a persistent session across ticks:
- Your previous tick's reasoning is in the conversation above — use it
- You DON'T need to re-read files you read in previous ticks
- You CAN reference patterns: "This error appeared in tick #2 as well"
- If you see <tick> followed by "No new T2 entries", the system will skip you automatically
```

---

## 4. 实现步骤

### Step 1: 新增持续 session 状态变量
- `_heartbeat_contexts: dict[uuid.UUID, list[dict]]`
- `_heartbeat_session_ids: dict[uuid.UUID, uuid.UUID]`
- `_heartbeat_tick_counts: dict[uuid.UUID, int]`
- `_t2_mtimes: dict[uuid.UUID, dict[str, float]]`
- `_reset_heartbeat_session(agent_id)` 函数

### Step 2: 新增 T2/T3 读取函数
- `_read_t2_full(agent_id)` — 首次 tick 读 learnings/*.md 全量
- `_read_t3_summary(agent_id)` — 首次 tick 读 memory/*.md 摘要 (防重复写入)
- `_read_incremental_t2(agent_id)` — 后续 tick 读 mtime delta

### Step 3: 改造 _execute_heartbeat
- if/else 分支: 首次 tick (全量初始化) vs 后续 tick (tick + 增量)
- 空转保护: `_read_incremental_t2` 返回空 → skip
- 追加 assistant response 到 `_heartbeat_contexts`
- DB session 只在首次创建, 后续复用 `_heartbeat_session_ids`

### Step 4: 改造 tick 间隔
- `agent.heartbeat_interval_minutes or 120` → `agent.heartbeat_interval_minutes or 45`
- `tenant.min_heartbeat_interval_minutes` 默认值 120 → 30 (下限)

### Step 5: 重写 HEARTBEAT.md
- 替换 `templates/HEARTBEAT.md` 全文 (§3.5 模板)

### Step 6: 注册 DREAM_END 重置 handler
- `hooks_setup.py`: DREAM_END → `_reset_heartbeat_session(agent_id)`

### Step 7: 更新 evolution 文件写回
- `_update_evolution_files` 保留 (lineage/scorecard 仍需更新)
- lineage 条目前缀从 `HB-` 改为 `CUR-` (策展日志)
- 不再写 blocklist (3 连败封禁移到 T2→T3 策展逻辑中)

---

## 5. 验收标准

| # | 验收项 | 方法 |
|---|--------|------|
| V1 | 持续 session 跨 tick | tick 1 → tick 2 → 日志显示 "tick #2", Agent 引用上一次策展结果 |
| V2 | 首次 tick 全量初始化 | 第一次心跳 → 日志显示 "full init", messages 包含 T2+T3 |
| V3 | 增量 T2 | 在 insights.md 写新条目 → 下个 tick 只看到新条目 (不重复) |
| V4 | 空转保护 | T2 无变化 → 日志显示 "Skip tick: no new T2 entries" → 无 LLM 调用 |
| V5 | T2→T3 策展 | insights.md 有 "用户偏好 snake_case" → tick 后 feedback.md 新增对应条目 |
| V6 | Session 重置 | 梦境完成 → _heartbeat_contexts 清空 → 下次 tick 为首次 (full init) |
| V7 | Lineage 格式 | lineage.md 新条目前缀为 `CUR-` (不是 `HB-`) |
| V8 | 间隔调整 | 新 Agent 默认 45min (不是 120min) |
| V9 | HEARTBEAT.md 更新 | 文件包含 Phase 2: CURATE 和 "Persistent Session Notes" |
| V10 | DB Session 复用 | 同一天多次 tick → 只创建 1 个 ChatSession (不是每次新建) |

---

## 6. 影响文件

| 文件 | 改动类型 | 改动范围 |
|------|---------|---------|
| `services/heartbeat.py` | 重构 | 持续 session 状态 + T2/T3 读取 + 空转保护 + 改造 execute, ~120 行改动 |
| `templates/HEARTBEAT.md` | 重写 | 全文替换, ~80 行 |
| `runtime/hooks_setup.py` | 修改 | 注册 DREAM_END → reset handler, ~5 行 |
| `models/tenant.py` | 修改 | min_heartbeat_interval_minutes 默认值 120→30, 1 行 |
| `alembic/versions/` | 新增 | migration: default heartbeat interval 120→45 (如需) |
