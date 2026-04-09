# Phase 3: 压缩体系对齐

> **依赖**: 01-hooks (PRE/POST_COMPACTION)
> **可并行**: 与 Phase 2 (提取器) 并行
> **交付**: 5 差距修复 + 11-section 压缩提示词

---

## 1. 当前状态 (基于源码, 逐层分析)

### 1.1 Layer 1: 工具结果驱逐 (`engine.py:522-571`)

```python
_TOOL_RESULT_EVICTION_THRESHOLD = 50000  # chars (CC: 50K) ✅ 已对齐
_TOOL_RESULT_PREVIEW_LENGTH = 4000       # chars to keep inline
```

**逻辑** (`_maybe_evict_tool_result()`):
- 单个工具结果 > 50K chars → 写到 `workspace/tool_results/{tool_call_id}.txt`
- 保留 4K preview + `<persisted-output>` 标记
- 豁免工具: `list_files, read_file, load_skill, tool_search, discover_resources, list_triggers, list_tasks, get_task, get_current_time, check_async_task, list_async_tasks, web_search, firecrawl_fetch, xcrawl_scrape, read_document`

**状态**: ✅ 已对齐 Claude Code 的 `DEFAULT_MAX_RESULT_SIZE_CHARS = 50_000`

### 1.2 Layer 2: 轮次聚合预算 (`engine.py:1167-1168, 1249-1254`)

```python
_TOOL_RESULTS_AGGREGATE_BUDGET = 200000  # chars per round (CC: 200K) ✅
```

**逻辑**: 每轮工具执行后累加 `_round_tool_chars`，超 200K → 强制截断到 4K

**状态**: ✅ 已对齐 Claude Code 的 `MAX_TOOL_RESULTS_PER_MESSAGE_CHARS = 200_000`

### 1.3 Layer 3: 轮次微压缩 (`engine.py:1393-1423`)

```python
_MICROCOMPACT_ROUND_AGE = 20             # rounds old
_MICROCOMPACT_CLEARED_MARKER = "[Old tool result cleared to save context space]"
_MIDLOOP_COMPACT_CHECK_INTERVAL = 3      # check every 3 rounds
```

**逻辑** (完整代码, `engine.py:1393-1423`):
```python
if round_i >= _MICROCOMPACT_ROUND_AGE and (round_i + 1) % _MIDLOOP_COMPACT_CHECK_INTERVAL == 0:
    _cutoff_round = round_i - _MICROCOMPACT_ROUND_AGE
    for _mi, _msg in enumerate(api_messages):
        if (
            _msg.role == "tool"
            and _mi < _cutoff_round * 3          # rough: ~3 messages per round
            and _msg.content != _MICROCOMPACT_CLEARED_MARKER
            and len(_msg.content or "") > 500     # only clear substantial results
        ):
            # Check exemption by looking back up to 5 messages for the tool call
            _is_exempt = ...  # checks _EVICTION_EXEMPT_TOOLS
            if not _is_exempt:
                _msg.content = _MICROCOMPACT_CLEARED_MARKER
```

**⚠️ 差距**: Claude Code 用**时间制** (60min 空闲 + 保留最近 5)，Hive 用**轮次制** (20 轮龄)。
- Claude Code: `gapThresholdMinutes: 60`, `keepRecent: 5` (`timeBasedMCConfig`)
- 问题: 轮次制在快速多轮对话中过早清除有用结果

### 1.4 Mid-loop 压缩 (`engine.py:1425-1469`)

**触发条件** (`engine.py:1426`):
```python
if (round_i + 1) % _MIDLOOP_COMPACT_CHECK_INTERVAL == 0 and len(api_messages) > 6:
```

**阈值**: `_MIDLOOP_COMPACT_THRESHOLD = 0.85` (85% of context window)

**调用链**:
```
engine.py:1439 → maybe_compress_messages()
  → memory_service.py:238 → estimate_tokens() → 检查阈值
  → _safe_split() 安全分割 (不断开 tool_call/result 对)
  → _llm_summarize() 或 _extract_summary() fallback
```

**Post-compact 恢复** (`engine.py:1452-1468`):
- 调 `_build_restoration_context()` → 预算 60K chars
- 恢复优先级: soul → focus → recent files → tool outcomes → writes → skills → packs → refs → pending
- 注入位置: summary 之后, recent messages 之前

**⚠️ 差距**:
1. 没有减去 summary 输出预留 — Claude Code: `effective = context_window - min(maxOutputTokens, 20000)`
2. PRE/POST_COMPACTION hooks 未接入 — 提取器无法在压缩前保全上下文

### 1.5 PTL 重试 (`engine.py:962-1028`)

**完整逻辑** (`engine.py:963-1028`):
```python
if _is_prompt_too_long(exc) and ptl_retries < _PTL_MAX_RETRIES:  # _PTL_MAX_RETRIES = 2
    if len(api_messages) <= 4:
        pass  # skip — too few messages
    else:
        ptl_retries += 1
        compressed = await maybe_compress_messages(
            conv_dicts,
            compress_threshold=0.5,  # aggressive
        )
        _after_chars = sum(len(d.get("content", "") or "") for d in compressed)
        if _after_chars < _before_chars * 0.8:  # >20% reduction achieved
            api_messages = [system_msg] + compressed
            continue  # retry LLM call
```

**⚠️ 差距**:
1. 只有 2 次重试 (Claude Code: 3 次)
2. 策略是全量重压缩 (Claude Code: 按 API round 分组丢弃最老 group)
3. Claude Code 有 `truncateHeadForPTLRetry()` — 按 `groupMessagesByApiRound()` 分组, 丢弃 20% oldest groups
4. Claude Code 在失败时还能解析 provider 返回的 token gap

### 1.6 压缩提示词 (`conversation_summarizer.py:302-336`)

**完整提示词** (已读, 10-section):
```
CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.
...
<analysis> tags (scratchpad, stripped before persistence)
<summary> tags using EXACTLY this format:

**Task Ledger:** [goal + status]
**Decision Ledger:** [decisions, corrections, constraints]
**Artifact Ledger:** [file paths, URLs, IDs]
**Code Snapshot:** [key code changes, function signatures]
**Tool Ledger:** [tools called + key results]
**User Messages:** [ALL non-trivial user messages]
**Preference Ledger:** [stable user preferences]
**Error Ledger:** [errors, root causes, resolutions]
**Pending Ledger:** [incomplete items + direct quotes]
**Narrative Snapshot:** [1-2 line recap]
```

**LLM 调用参数** (`_llm_summarize`, `conversation_summarizer.py:339-391`):
- max_tokens: 2500
- temperature: 0.3
- 输入: 最近 40 条消息 (`conversation_text[-40:]`)
- 用户消息保留 800 chars, assistant 800 chars, tool_result 1500 chars

**无 LLM fallback** (`_extract_summary`, `conversation_summarizer.py:233-279`):
- Pattern-based 提取: task(最后用户消息) + decisions(最后 3-4 条) + reasoning(hints) + artifacts(regex) + tools(最近 15 交互) + preferences(hints) + pending(hints) + narrative(最后 assistant 200 chars)
- 8 section (比 LLM 版少 Code Snapshot 和 Error Ledger)

**Post-summary 处理** (`_extract_summary_from_response`, `conversation_summarizer.py:282-296`):
- 提取 `<summary>` 标签内容
- Fallback: 剥离 `<analysis>` 标签
- Final fallback: 返回全文

### 1.7 memory_service.py 的 maybe_compress_messages (`memory_service.py:238-322`)

**阈值** (`memory_service.py:260-280`):
```python
# 默认 82% (was 70%, 对 256K models 太激进)
threshold = compress_threshold or tenant_compress_threshold or 0.82
```

**safe_split** (`memory_service.py` `_safe_split`):
- 防止断开 tool_call/tool_result 对
- Case 1: recent 以 tool result 开头 → 移到 old
- Case 2-3: old 以 tool_calls 结尾但 result 不完整 → 移到 recent

**keep_recent**: 默认 10 条

---

## 2. Claude Code 对标 (基于源码)

### 2.1 有效窗口计算 (`autoCompact.ts`)

```typescript
const AUTOCOMPACT_BUFFER_TOKENS = 13_000
const MAX_OUTPUT_TOKENS_FOR_SUMMARY = 20_000

function getEffectiveContextWindowSize(model) {
  const reservedForSummary = Math.min(getMaxOutputTokensForModel(model), 20_000)
  return contextWindow - reservedForSummary
}
// threshold = effective - 13_000
// 200K model: 200K - 20K = 180K effective, 180K - 13K = 167K threshold (~92.8%)
```

### 2.2 时间微压缩 (`microCompact.ts`)

```typescript
type TimeBasedMCConfig = {
  enabled: boolean
  gapThresholdMinutes: number  // Default: 60
  keepRecent: number            // Default: 5
}
// 触发: 最后一条 assistant 消息距今 > 60 min
// 动作: 清除旧 tool results, 保留最近 5 个 (按时间排序)
```

### 2.3 PTL round-group (`compact.ts + grouping.ts`)

```typescript
const MAX_PTL_RETRIES = 3
// groupMessagesByApiRound(): 按 assistant 响应分组
// truncateHeadForPTLRetry(): 丢弃最老 20% 的 round groups
// 解析 provider error 中的 token gap, 或 fallback 20%
```

### 2.4 Session Memory 压缩 (`sessionMemoryCompact.ts`)

```typescript
// 无 LLM 裁剪: 保留最近 N 条有文本的消息
const DEFAULT_SM_COMPACT_CONFIG = {
  minTokens: 10_000,
  minTextBlockMessages: 5,
  maxTokens: 40_000,
}
```

### 2.5 压缩提示词 (`prompt.ts`, 9-section)

```
1. Primary Request and Intent
2. Key Technical Concepts
3. Files and Code Sections
4. Errors and fixes
5. Problem Solving            ← Hive 缺少
6. All user messages
7. Pending Tasks
8. Current Work
9. Optional Next Step
```

---

## 3. 五个差距 + 改造方案

### G1: 微压缩 — 轮次制 → 时间制

**当前** (`engine.py:1393-1423`): `_MICROCOMPACT_ROUND_AGE = 20` 轮
**改为**: 时间制, 对齐 Claude Code `TimeBasedMCConfig`

```python
# 新常量
_MICROCOMPACT_GAP_MINUTES = 60        # 空闲超过 60min 触发
_MICROCOMPACT_KEEP_RECENT = 5          # 保留最近 5 个 tool results

# 新逻辑: 在每 3 轮检查点
if (round_i + 1) % _MIDLOOP_COMPACT_CHECK_INTERVAL == 0:
    _now = time.time()
    _tool_results_by_time = []  # (index, timestamp, msg)
    for _mi, _msg in enumerate(api_messages):
        if _msg.role == "tool" and len(_msg.content or "") > 500:
            _tool_results_by_time.append((_mi, _msg.timestamp, _msg))
    
    # 按时间排序, 保留最近 5 个
    _sorted = sorted(_tool_results_by_time, key=lambda x: x[1], reverse=True)
    _to_keep = set(x[0] for x in _sorted[:_MICROCOMPACT_KEEP_RECENT])
    
    for _mi, _ts, _msg in _tool_results_by_time:
        if _mi not in _to_keep and (_now - _ts) > _MICROCOMPACT_GAP_MINUTES * 60:
            if not _is_exempt(_msg, api_messages):
                _msg.content = _MICROCOMPACT_CLEARED_MARKER
```

**需要**: LLMMessage 增加 `timestamp` 字段 (创建时打戳)

### G2: 有效窗口 — 减去 summary 预留

**当前** (`memory_service.py`): 直接用 `context_limit`
**改为**:

```python
# memory_service.py, _get_input_context_limit() 返回后
_SUMMARY_OUTPUT_RESERVE = 20000  # tokens, 对齐 CC MAX_OUTPUT_TOKENS_FOR_SUMMARY
effective_limit = context_limit - _SUMMARY_OUTPUT_RESERVE
```

### G3: PTL — round-group 分组 + 3 次重试

**当前** (`engine.py:962-1028`): 全量重压缩 + 2 次
**改为**:

```python
_PTL_MAX_RETRIES = 3  # was 2

def _group_messages_by_api_round(messages: list[LLMMessage]) -> list[list[LLMMessage]]:
    """按 assistant 响应分组 — 每个 group 是一个完整的 API round。"""
    groups = []
    current_group = []
    for msg in messages:
        current_group.append(msg)
        if msg.role == "assistant" and not msg.tool_calls:
            groups.append(current_group)
            current_group = []
    if current_group:
        groups.append(current_group)
    return groups

def _truncate_head_for_ptl(messages, drop_ratio=0.2):
    """丢弃最老的 N% round groups."""
    groups = _group_messages_by_api_round(messages)
    drop_count = max(1, int(len(groups) * drop_ratio))
    kept_groups = groups[drop_count:]
    return [msg for group in kept_groups for msg in group]
```

PTL 重试改为:
```python
if _is_prompt_too_long(exc) and ptl_retries < 3:
    ptl_retries += 1
    # 第一次: 丢弃 20% oldest rounds
    # 第二次: 再丢弃 20%
    # 第三次: 全量压缩 (fallback)
    if ptl_retries <= 2:
        truncated = _truncate_head_for_ptl(api_messages[1:])
        api_messages = [api_messages[0]] + truncated
    else:
        compressed = await maybe_compress_messages(..., compress_threshold=0.5)
        api_messages = [api_messages[0]] + compressed
    continue
```

### G4: 无 LLM 快速裁剪

**当前**: LLM 失败 → `_extract_summary()` (pattern-based)
**新增**: 在 pattern-based 也失败时, 直接裁剪旧消息

```python
# memory_service.py, maybe_compress_messages 内
if not summary:
    # CR-03: pattern extraction also failed
    # Last resort: keep recent N messages, drop oldest
    if len(old_messages) > 0:
        marker = {"role": "system", "content": "[Older messages trimmed to fit context window]"}
        return [marker] + recent_messages
    return messages  # nothing to compress
```

### G5: PRE/POST_COMPACTION hooks (已在 Phase 0 覆盖)

接入位置: `engine.py:1425` (PRE) 和 `engine.py:1452` (POST)

---

## 4. 压缩提示词升级 (10→11 section)

**改造 `conversation_summarizer.py:302-336`**:

```python
_SUMMARIZE_SYSTEM_PROMPT = """\
CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.
- Do NOT use read_file, write_file, web_search, execute_code, or ANY other tool.
- You already have all the context you need in the conversation below.
- Tool calls will be REJECTED and will waste your only turn.
- Your entire response must be plain text: an <analysis> block followed by a <summary> block.
- Session summaries preserve working state so the next turn can continue safely.
- Do NOT rewrite this summary as long-term memory or policy.
- Stable preferences, lessons, and policies are automatically extracted to the memory system.

Your task is to create a detailed summary of the conversation, preserving critical context \
for continuing work without losing state.

First, wrap your detailed analysis in <analysis> tags:
1. Chronologically analyze each message — identify user requests, your approach, and outcomes
2. Note ALL file paths, code snippets, function signatures, and technical decisions
3. Pay special attention to user corrections and feedback
4. Identify errors encountered and how they were resolved
5. Track problem-solving approaches — what was tried, what worked, what didn't

Then provide your final summary in <summary> tags using EXACTLY this format:

**Primary Request and Intent:** [core goal + current status — be specific]
**Key Technical Decisions:** [architecture choices, constraints, tradeoffs decided]
**Files and Code Sections:** [file_path:line_number + key snippets for critical changes]
**Problem Solving:** [approaches tried, what worked, what didn't — prevent re-trying failed approaches]
**Errors and Fixes:** [errors encountered + root causes + resolutions]
**All User Messages:** [ALL non-trivial user messages summarized — critical for tracking changing intent]
**User Preferences:** [corrections, stated preferences, feedback — highest priority to preserve]
**Tool Outcomes:** [key tool calls and their results — focus on outcomes, not individual calls]
**Pending Tasks:** [incomplete items + where work left off — include direct quotes from recent messages]
**Current Work:** [what was actively being done when compression triggered]
**Recovery Context:** [raw session log available at logs/{date}/ for full detail if needed]

Be thorough in preserving technical details — code snippets and file paths are more valuable than prose.
Respond in the same language as the conversation.\
"""
```

**变更汇总**:
| # | 变更 | 原因 |
|---|------|------|
| 1 | 新增 "Problem Solving" section | 防止压缩后重新尝试已失败的方案 |
| 2 | 新增 "Current Work" section | 明确压缩触发时的即时状态 |
| 3 | 新增 "Recovery Context" section | 指向 T0 日志, 可按需恢复完整记录 |
| 4 | "Decision Ledger" → "Key Technical Decisions" | 对齐 Claude Code 命名 |
| 5 | "Task Ledger" → "Primary Request and Intent" | 对齐 Claude Code 命名 |
| 6 | "Tool Ledger" → "Tool Outcomes" | 更准确 |
| 7 | 删除 "Code Snapshot" 独立 section | 合并到 "Files and Code Sections" |
| 8 | 删除 "Narrative Snapshot" | 被 "Current Work" 替代 |
| 9 | analysis 第 5 步新增 problem-solving tracking | 对齐新 section |
| 10 | 添加 "automatically extracted to memory system" 说明 | 告知记忆提取自动发生 |

### 无 LLM fallback 也需同步更新

`_extract_summary()` (`conversation_summarizer.py:233-279`) 新增:
- "Problem Solving" — 从 assistant 消息中提取 "tried/attempted/failed/succeeded" hints
- "Current Work" — 最后一条 assistant 消息的前 200 chars
- "Recovery Context" — 固定文本指向 logs/

---

## 5. Post-compact 恢复优先级调整

当前恢复优先级 (`memory_service.py:396-520`) 保持不变, 但新增 T3 记忆文件:

```
P0: 压缩摘要 (~2500 tokens)
P0: soul.md 核心段 (identity)
P0: focus.md (T1 working memory)
P0: memory/feedback.md + blocked.md (T3 高优)     ← 新增
P1: memory/knowledge.md + strategies.md (T3)       ← 新增
P2: 最近读过的文件 (up to 3)
P2: 最近工具结果 (last 5)
P2: memory/user.md (T3)                            ← 新增
P3: active skills / active packs
```

---

## 6. 实现步骤

### Step 1: G1 时间微压缩
- `engine.py`: LLMMessage 加 `timestamp` 字段 (或用 message index 映射)
- `engine.py:1393-1423`: 重写微压缩逻辑 → 时间制 + 保留最近 5

### Step 2: G2 有效窗口
- `memory_service.py`: `_get_input_context_limit()` 返回值减去 20K

### Step 3: G3 PTL round-group
- `engine.py`: 新增 `_group_messages_by_api_round()` + `_truncate_head_for_ptl()`
- `engine.py:962-1028`: PTL 重试改为 round-group 丢弃 + 3 次

### Step 4: G4 无 LLM 裁剪
- `memory_service.py`: `maybe_compress_messages` 新增 last-resort 路径

### Step 5: 压缩提示词升级
- `conversation_summarizer.py:302-336`: 替换为 11-section 版本
- `conversation_summarizer.py:233-279`: `_extract_summary()` 同步新增 3 个 section

### Step 6: 恢复优先级
- `memory_service.py:_build_restoration_context()`: 新增 T3 文件读取和注入

---

## 7. 验收标准

| # | 验收项 | 方法 |
|---|--------|------|
| V1 | 时间微压缩 | 长对话空闲 >1h → 旧 tool results 被 `[Old tool result cleared]` 替代, 最近 5 个保留 |
| V2 | 有效窗口 | 200K model → 日志显示 compact 在 ~167K tokens 触发 (不是 170K) |
| V3 | PTL round-group | 构造超长 prompt → 日志显示 "PTL round-group drop: N groups" |
| V4 | PTL 3 次 | 极端超长 → 日志显示 attempt 1→2→3 |
| V5 | 无 LLM 裁剪 | 断开 LLM + 超长对话 → 压缩成功, 摘要含 "[Older messages trimmed]" |
| V6 | 11-section 摘要 | 压缩触发 → 摘要包含 "Problem Solving" + "Current Work" + "Recovery Context" |
| V7 | T3 恢复注入 | 压缩后 → 恢复上下文包含 feedback.md + blocked.md 内容 |
| V8 | Hooks 触发 | 压缩前 → 日志 `[Hooks] pre_compaction`; 压缩后 → `[Hooks] post_compaction` |

---

## 8. 影响文件

| 文件 | 改动类型 | 改动范围 |
|------|---------|---------|
| `kernel/engine.py` | 修改 | 微压缩改时间制 (~30行) + PTL round-group (~50行) + LLMMessage timestamp |
| `services/memory_service.py` | 修改 | 有效窗口 (~5行) + 无 LLM 裁剪 (~10行) + 恢复优先级 (~20行) |
| `services/conversation_summarizer.py` | 修改 | 10→11 section (~40行提示词) + _extract_summary 3 新 section (~20行) |
| `services/llm_utils.py` | 修改 | LLMMessage 加 timestamp 字段 (如需) |
