# Phase 6: 梦境重构 (T3→soul 归档)

> **依赖**: 06-heartbeat (T3 memory/*.md 由心跳策展产出)
> **交付**: DREAM.md 模板 + MD→MD 归档 + T3 精简 + soul 提炼 + 清理

---

## 1. 当前状态 (基于源码, 完整分析)

### 1.1 run_dream() (`auto_dream.py:212`, ~300 行)

**完整流程:**

1. **加载 facts** (`auto_dream.py:222`): `store.load_semantic_facts(agent_id)` → SQLite
2. **加载 summaries** (`auto_dream.py:228`): `_load_recent_summaries(agent_id, limit=10)` → DB ChatSession.summary
3. **备份** (`auto_dream.py:234`): `_backup_facts()` → `memory/dream_backups/`
4. **Importance 过滤** (`auto_dream.py:240-242`): high ≥0.5 送 LLM, low <0.5 保留
5. **Cluster-then-synthesize** (`auto_dream.py` ~250行): 按 category 分组 → keyword Jaccard 聚类 → 小 cluster 保留, 大 cluster LLM 合并
6. **安全门控** (`auto_dream.py` ~280行): >70% facts 丢失 → 拒绝
7. **Evolution 蒸馏** (`_distill_evolution_to_facts`): 读 evolution/ 文件 → LLM → typed facts
8. **Learnings 消化** (`_ingest_learnings`): 读 learnings/*.md → LLM → facts → **截断源文件**
9. **替换 facts** (`auto_dream.py` ~310行): `store.replace_semantic_facts(agent_id, new_facts)` → SQLite
10. **Sync memory.md** (`_sync_facts_to_memory_md`): facts → LLM 合成 → `memory/memory.md`
11. **Promote to soul** (`_promote_to_soul`): 3+ 次 feedback → LLM 改写第一人称 → soul.md `## Learned Behaviors`
12. **Cleanup** (`auto_dream.py` ~350行): focus.md 清理 + blocklist.md 过期

### 1.2 核心问题 (对照 v9 spec)

| 问题 | 当前 | v9 要求 |
|------|------|--------|
| Source of truth | SQLite (semantic_facts) | **MD 文件** (memory/*.md) |
| 输出格式 | JSON array | **MD→MD** |
| 读 T2 | 是 (`_ingest_learnings`) | **不读** — 心跳独占 T2→T3 |
| 读 evolution | 是 (`_distill_evolution_to_facts`) | 读 lineage (策展历史), 不读 scorecard/blocklist |
| 合并 + 提取 + 清理 | 全在一个函数 | **只做 T3 精简 + soul 提炼 + 清理** |
| 心跳 session 重置 | 无 | **梦境完成后重置** |

### 1.3 梦境提示词 (`auto_dream.py:45-49`)

```python
_AUTO_DREAM_SYSTEM_PROMPT = (
    "You consolidate an agent's long-term memory into a clean, deduplicated fact list.\n"
    "Do NOT preserve transient task state, temporary TODOs, or raw session transcripts.\n"
    "Keep durable reusable facts, durable strategy lessons, and blocked patterns.\n"
    "Return only a JSON array — no prose, no explanation."
)
```

**问题**: 要求 JSON array 输出, 弱模型 JSON 格式不稳定, 违反 MD→MD 原则。

### 1.4 门控逻辑 (`auto_dream.py:33-34, 144-152`)

```python
MIN_HOURS_BETWEEN_DREAMS = 4
MIN_SESSIONS_SINCE_DREAM = 3

def should_dream(agent_id):
    last, sessions = _load_dream_state(agent_id)
    if last and hours_since < 4:
        return False
    return sessions >= 3
```

**v9 要求**: 门控扩展为 `4h + (3 sessions OR 2 heartbeat ticks)`

---

## 2. Claude Code 对标

### 2.1 autoDream (`autoDream.ts`)

- 门控: `minHours=24, minSessions=5` (比 Hive 宽松得多)
- 锁: `.consolidate-lock` 文件 + PID + 1h stale
- 执行: `runForkedAgent` with `consolidationPrompt`
- UI: `DreamTask` 进度追踪

### 2.2 consolidation prompt (`consolidationPrompt.ts`)

**4 阶段:**
```
Phase 1: Orient — ls, read MEMORY.md, skim topics, check logs/
Phase 2: Gather — daily logs, drifted memories, transcript grep
Phase 3: Consolidate — merge, convert dates, delete contradicted
Phase 4: Prune — MEMORY.md < 200 lines/25KB, remove stale, shorten verbose
```

**关键**: 直接操作 MD 文件, 不要求 JSON。

---

## 3. 目标状态

### 3.1 改造后的 run_dream

```python
async def run_dream(agent_id: uuid.UUID, tenant_id: uuid.UUID) -> dict:
    """梦境归档: T3 精简 + soul 提炼 + 清理。
    
    ⚠️ 不读 T2 (心跳独占 T2→T3)
    ⚠️ 不读 SQLite facts (MD = source of truth)
    ⚠️ 不输出 JSON (MD→MD)
    """
    # Step 1: 读 T3 全量 (MD 文件)
    t3_files = _read_all_t3(agent_id)  # feedback.md, knowledge.md, ...
    
    # Step 2: T3 各文件内部精简
    for filename, content in t3_files.items():
        refined = await _consolidate_t3_file(agent_id, filename, content)
        _write_t3_file(agent_id, filename, refined)
    
    # Step 3: 跨文件去重
    await _cross_file_dedup(agent_id, t3_files)
    
    # Step 4: soul.md 提炼
    await _promote_to_soul(agent_id)
    
    # Step 5: INDEX.md 更新
    _update_index_md(agent_id)
    
    # Step 6: FTS5 索引重建 (从 MD 文件)
    _rebuild_fts5_from_md(agent_id)
    
    # Step 7: 清理
    _truncate_t2(agent_id, keep=10)        # learnings 截断到 10 条
    _cleanup_old_t0(agent_id, days=30)     # T0 >30d 删除
    _archive_lineage(agent_id, max=200)     # lineage >200 归档
    _cleanup_focus(agent_id)                # focus.md 清理 [x] + >7d
    
    # Step 8: 重置心跳 session
    from app.runtime.hooks import emit_hook, HookEvent
    await emit_hook(HookEvent.DREAM_END, agent_id=agent_id)
    
    # Step 9: 写 T0 梦境日志
    await write_t0_log(agent_id, behavior_type="dream", ...)
    
    # Step 10: 标记完成
    _mark_dreamed(agent_id)
```

### 3.2 DREAM.md 模板

详见总纲 §15.4.2 完整模板:
- Phase 1: ORIENT (读 INDEX.md + memory/*.md + lineage.md)
- Phase 2: CONSOLIDATE (各文件去重 + cap)
- Phase 3: PROMOTE (feedback 3+ → soul.md Learned Behaviors)
- Phase 4: INDEX + CLEANUP

### 3.3 降级路径

| 环节 | LLM 可用 | 零 LLM (地板) |
|------|---------|-------------|
| T3 精简 | LLM 重写精简 | SequenceMatcher >70% 去重 + cap 截断 |
| 跨文件去重 | LLM 判断保留哪个 | 保留更长的那个 |
| Soul 提炼 | LLM 改写第一人称 | 直接复制原文 |
| FTS5 重建 | N/A (纯程序) | 同 |
| 清理 | N/A (纯程序) | 同 |

### 3.4 门控扩展

```python
MIN_HOURS_BETWEEN_DREAMS = 4
MIN_SESSIONS_SINCE_DREAM = 3
MIN_HEARTBEAT_TICKS_SINCE_DREAM = 2  # 新增

def should_dream(agent_id):
    last, sessions = _load_dream_state(agent_id)
    if last and hours_since < MIN_HOURS_BETWEEN_DREAMS:
        return False
    # 扩展: sessions OR heartbeat ticks
    ticks = _heartbeat_ticks_since_dream.get(agent_id, 0)
    return sessions >= MIN_SESSIONS_SINCE_DREAM or ticks >= MIN_HEARTBEAT_TICKS_SINCE_DREAM
```

---

## 4. 实现步骤

### Step 1: 新建 `templates/DREAM.md`

### Step 2: 新增 T3 MD 读写函数
- `_read_all_t3(agent_id)` → `dict[str, str]`
- `_write_t3_file(agent_id, filename, content)`
- `_update_index_md(agent_id)`
- `_rebuild_fts5_from_md(agent_id)` — 解析 MD → 写入 SQLite FTS5

### Step 3: 改造 run_dream — 删除 SQLite-first 逻辑
- 不再调 `store.load_semantic_facts()` (MD = truth)
- 不再调 `_ingest_learnings()` (心跳独占)
- 不再调 `_distill_evolution_to_facts()` (evolution 由心跳管理)
- 不再要求 JSON 输出

### Step 4: 实现程序化精简 (零 LLM 地板)
- `_programmatic_dedup(lines)` — SequenceMatcher >70%
- `_cap_truncate(lines, max_items)` — 保留最新 N 条

### Step 5: 实现 soul 提炼 (MD→MD)
- 扫描 feedback.md 高频条目 (3+)
- LLM 改写或直接复制 → soul.md `## Learned Behaviors` (整体替换, max 20)

### Step 6: 门控扩展
- 新增 `_heartbeat_ticks_since_dream` 跟踪
- `should_dream()` 扩展为 `sessions >= 3 OR ticks >= 2`

### Step 7: 清理整合
- T2 截断: `_truncate_t2(agent_id, keep=10)`
- T0 清理: `_cleanup_old_t0(agent_id, days=30)` (调用 t0_logger.cleanup_old_logs)
- Lineage 归档: >200 条 → `evolution/lineage_archive/`
- Focus 清理: 删 `[x]` + >7d

### Step 8: DREAM_END hook emit
- 梦境完成 → emit DREAM_END → 心跳 session 重置 + T0 日志写入

---

## 5. 验收标准

| # | 验收项 | 方法 |
|---|--------|------|
| V1 | DREAM.md 存在 | `cat templates/DREAM.md` → 4 阶段协议 |
| V2 | run_dream 不读 SQLite | 梦境执行 → 无 `load_semantic_facts` 调用 |
| V3 | run_dream 不读 T2 | 梦境执行 → 无 `_ingest_learnings` 调用 |
| V4 | T3 精简有效 | feedback.md 有 3 条重复 → 梦境后合并为 1 条 |
| V5 | Soul 提炼 | feedback.md 有 "始终用 snake_case" 出现 3 次 → soul.md Learned Behaviors 新增 |
| V6 | FTS5 从 MD 重建 | 梦境后 → `recall("snake_case")` 能搜到 |
| V7 | T2 截断 | 梦境后 → learnings/*.md 各 ≤10 条 |
| V8 | T0 清理 | `logs/` 中 >30d 目录被删除 |
| V9 | 心跳重置 | 梦境后 → 下次心跳 = 首次 tick (full init) |
| V10 | 门控扩展 | 0 sessions + 2 heartbeat ticks → should_dream = True |
| V11 | 零 LLM 降级 | 断开 LLM → 梦境仍完成 (程序化去重 + 直接复制到 soul) |

---

## 6. 影响文件

| 文件 | 改动类型 | 改动范围 |
|------|---------|---------|
| `templates/DREAM.md` | 新建 | 4 阶段模板, ~80 行 |
| `services/auto_dream.py` | 重构 | 删除 SQLite-first + JSON 输出, 改为 MD→MD, ~200 行改动 |
| `memory/store.py` | 修改 | 新增 `rebuild_fts5_from_md()`, ~30 行 |
| `runtime/hooks_setup.py` | 修改 | DREAM_END handler, ~5 行 |
