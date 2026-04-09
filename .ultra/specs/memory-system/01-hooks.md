# Phase 0: Hooks 系统重构

> **依赖**: 无 (所有后续阶段的前置条件)
> **交付**: hooks.py 10→16 events + 关键 emit 接入到 kernel/invoker/websocket

---

## 1. 当前状态 (基于源码)

### 1.1 hooks.py (`runtime/hooks.py`, 143 行)

**10 个事件已定义:**
```python
class HookEvent(StrEnum):
    PRE_TOOL_USE = "pre_tool_use"       # ✅ 已接入 engine.py:274
    POST_TOOL_USE = "post_tool_use"     # ✅ 已接入 engine.py:302
    POST_TOOL_FAILURE = "post_tool_failure"  # ✅ 已接入 engine.py:292
    SESSION_START = "session_start"     # ❌ 未接入
    SESSION_END = "session_end"         # ❌ 未接入
    PRE_COMPACTION = "pre_compaction"   # ❌ 未接入
    POST_COMPACTION = "post_compaction" # ❌ 未接入
    DELEGATION_START = "delegation_start"  # ❌ 未接入
    DELEGATION_END = "delegation_end"   # ❌ 未接入
    MEMORY_EXTRACTED = "memory_extracted"  # ❌ 未接入
```

**HookContext 数据结构** (`hooks.py:36-45`):
```python
@dataclass(slots=True)
class HookContext:
    event: HookEvent
    agent_id: Any = None
    session_id: str | None = None
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_result: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
```

**HookResult** (`hooks.py:48-53`): 只支持 block + modified_args (仅 PRE_TOOL_USE)。

**emit 机制** (`hooks.py:81-124`): 顺序执行, PRE_TOOL_USE 可拦截/修改参数, 其他事件 fire-and-forget。

**全局单例**: `hook_registry = HookRegistry()` + `emit_hook()` 便捷函数。

### 1.2 已接入的 3 个 emit 点 (`kernel/engine.py`)

| emit 点 | 行号 | 位置 |
|---------|------|------|
| PRE_TOOL_USE | 273-279 | `_execute_tool_with_hooks()` 工具执行前 |
| POST_TOOL_FAILURE | 291-298 | 同上函数, except 分支 |
| POST_TOOL_USE | 301-307 | 同上函数, 成功后 |

### 1.3 未接入的调用位置

| 事件 | 应该 emit 的位置 | 当前状态 |
|------|----------------|---------|
| SESSION_START | `invoker.py:640` invoke_agent() 调用 kernel 前 | 无 emit |
| SESSION_END | `invoker.py:641` invoke_agent() 返回后 | 无 emit |
| PRE_COMPACTION | `engine.py:1425-1451` mid-loop compact 触发时 | 无 emit |
| POST_COMPACTION | `engine.py` compact 完成后 | 无 emit |
| DELEGATION_START | `agents/orchestrator.py` delegate 前 | 无 emit |
| DELEGATION_END | `agents/orchestrator.py` delegate 返回后 | 无 emit |

### 1.4 WebSocket 空闲逻辑 (`api/websocket.py:416-463`)

当前已有两阶段空闲处理:
- Phase 1: `_DREAM_IDLE_SECONDS=180` → `persist_runtime_memory()` (已有)
- Phase 2: `_idle_timeout=600` → 关闭连接

**问题**: Phase 1 调用 `persist_runtime_memory()` 不经过 hooks, 直接写 memory。需要改为 emit SESSION_IDLE。

---

## 2. Claude Code 对标 (基于源码)

### 2.1 事件定义 (`coreTypes.ts:25-53`)

**27 个事件** (详见总纲 §17), Hive 需要的核心映射:

| Claude Code | Hive 对应 | 说明 |
|------------|----------|------|
| Stop | **RESPONSE_COMPLETE** (新增) | 每轮响应后, extractMemories 主触发 |
| SessionStart | SESSION_START | source: startup/resume/clear/compact |
| SessionEnd | 拆分为 SESSION_IDLE + SESSION_CLOSE | 服务端无进程退出 |
| PreCompact | PRE_COMPACTION | 可注入 custom instructions |
| PostCompact | POST_COMPACTION | 接收 compact_summary |
| SubagentStart | DELEGATION_START | |
| SubagentStop | DELEGATION_END | |
| PreToolUse | PRE_TOOL_USE | ✅ 已对齐 |
| PostToolUse | POST_TOOL_USE | ✅ 已对齐 |
| PostToolUseFailure | POST_TOOL_FAILURE | ✅ 已对齐 |

### 2.2 Claude Code 关键执行模式

**Stop hook** (`stopHooks.ts:142-152`):
- 每轮响应后 fire-and-forget
- 门控: `feature('EXTRACT_MEMORIES') && !agentId && isExtractModeActive()`
- 调用: `void extractMemoriesModule.executeExtractMemories(context, appendSystemMessage)`

**SessionEnd** (`gracefulShutdown.ts:469-480`):
- 在进程退出前同步执行
- 超时: 1.5s (可配)
- 失败静默忽略

**PreCompact** (`hooks.ts:3961-3984`):
- 在 REPL 外执行 (同步)
- 可返回 `newCustomInstructions` 注入压缩摘要
- 传入: trigger ('manual'|'auto') + custom_instructions

---

## 3. 目标状态

### 3.1 hooks.py 重构为 16 events

```python
class HookEvent(StrEnum):
    # ── 工具生命周期 (已有, 已接入) ──
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    POST_TOOL_FAILURE = "post_tool_failure"

    # ── Session 生命周期 (重构) ──
    SESSION_START = "session_start"
    RESPONSE_COMPLETE = "response_complete"  # 新增: 对齐 CC Stop hook
    SESSION_IDLE = "session_idle"            # 新增: 替代旧 SESSION_END (空闲超时)
    SESSION_CLOSE = "session_close"          # 新增: 替代旧 SESSION_END (断开/新对话)

    # ── 上下文压缩 (已有) ──
    PRE_COMPACTION = "pre_compaction"
    POST_COMPACTION = "post_compaction"

    # ── 委托 (已有) ──
    DELEGATION_START = "delegation_start"
    DELEGATION_END = "delegation_end"

    # ── Hive 独有 (新增) ──
    TRIGGER_END = "trigger_end"
    HEARTBEAT_TICK_END = "heartbeat_tick_end"
    DREAM_END = "dream_end"
    MEMORY_EXTRACTED = "memory_extracted"    # 已有
```

### 3.2 HookContext 扩展

```python
@dataclass(slots=True)
class HookContext:
    event: HookEvent
    agent_id: Any = None
    session_id: str | None = None
    tool_name: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_result: str | None = None
    error: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # 新增: 支持 RESPONSE_COMPLETE/SESSION_IDLE/SESSION_CLOSE
    messages: list[dict] | None = None      # 对话消息 (还在内存)
    source: str | None = None               # web/trigger/agent/heartbeat
```

### 3.3 关键 emit 接入点

| 事件 | 文件:行号 | 接入位置 | 传入数据 |
|------|---------|---------|---------|
| **RESPONSE_COMPLETE** | `engine.py:1108` | kernel handle() 返回 InvocationResult 前 | messages, last_response, turn_count, source |
| **SESSION_IDLE** | `websocket.py:431` | idle dream 触发处 (替代 persist_runtime_memory) | messages (conversation), idle_seconds |
| **SESSION_CLOSE** | `websocket.py:458` | idle timeout 关闭 + WebSocket 断开 | messages, reason |
| **PRE_COMPACTION** | `engine.py:1425` | mid-loop compact 检测通过后, 压缩前 | messages_to_compress, trigger='auto' |
| **POST_COMPACTION** | `engine.py` | 压缩完成后 (restoration 前) | compact_summary, trigger |
| **SESSION_START** | `invoker.py:640` | invoke_agent() 调 kernel.handle() 前 | source, model_name |
| **DELEGATION_END** | `orchestrator.py` | delegate_to_agent() 返回后 | from_agent, task, result, messages |
| **TRIGGER_END** | `trigger_daemon.py` | 触发器执行完成后 | trigger_name, type, result, messages |
| **HEARTBEAT_TICK_END** | `heartbeat.py` | _execute_heartbeat() 完成后 | tick_number, score, distilled_count |
| **DREAM_END** | `auto_dream.py` | run_dream() 完成后 | deduped, promoted_to_soul |

---

## 4. 实现步骤

### Step 1: hooks.py 重构

1. 添加 6 个新 HookEvent 枚举值
2. HookContext 加 `messages` 和 `source` 字段
3. `__init__` 自动为新事件初始化 handler 列表
4. 保持 emit 逻辑不变 (顺序执行, fire-and-forget)

### Step 2: RESPONSE_COMPLETE emit (提取器主触发)

接入 `engine.py` handle() 方法:
- 位置: `engine.py:1108` — `return InvocationResult(...)` 之前
- 条件: `request.session_context.source != "heartbeat"` (心跳不触发提取)
- 模式: fire-and-forget (`asyncio.create_task`)
- 传入: `api_messages` (还在内存), `final_content`, `round_i`

### Step 3: PRE_COMPACTION + POST_COMPACTION emit

接入 `engine.py` mid-loop compact 段:
- PRE: `engine.py:1425` 检测通过后, `maybe_compress_messages()` 调用前
- POST: compress 返回后, restoration 注入前
- PRE 传入: 即将被压缩的 old messages
- POST 传入: compact_summary 文本

### Step 4: SESSION_IDLE + SESSION_CLOSE emit

改造 `websocket.py:416-463`:
- SESSION_IDLE: 替代 `persist_runtime_memory()` 直接调用 → 改为 `emit_hook(SESSION_IDLE, messages=conversation)`
- SESSION_CLOSE: 在 `websocket.close()` 前 emit

### Step 5: SESSION_START emit

接入 `invoker.py:640`:
- 在 `get_agent_kernel().handle(kernel_request)` 前 emit
- 传入: source, model_name

### Step 6: DELEGATION_END, TRIGGER_END, HEARTBEAT_TICK_END, DREAM_END

分别接入各自文件 (详见各阶段文档)。Phase 0 只做前 5 步, 这些在对应阶段实现。

### Step 7: hooks_setup.py handler 注册

新建 `runtime/hooks_setup.py`:
- 在 `main.py` lifespan 中调用 `register_memory_hooks()`
- Phase 0 先注册空 handler (logging only)
- Phase 2+ 注册真正的提取/记录 handler

---

## 5. 验收标准

| # | 验收项 | 方法 |
|---|--------|------|
| V1 | hooks.py 包含 16 个 HookEvent | `grep -c "=" runtime/hooks.py` = 16 |
| V2 | RESPONSE_COMPLETE 每轮响应后触发 | 发一条消息 → 日志出现 `[Hooks] response_complete` |
| V3 | PRE_COMPACTION 在压缩前触发 | 长对话触发压缩 → 日志出现 `[Hooks] pre_compaction` |
| V4 | SESSION_IDLE 在空闲超时触发 | 等 180s → 日志出现 `[Hooks] session_idle` |
| V5 | SESSION_CLOSE 在连接关闭触发 | 关闭浏览器 → 日志出现 `[Hooks] session_close` |
| V6 | 已有 3 个工具 hooks 不受影响 | 工具调用正常, 治理正常 |
| V7 | handler 注册机制可用 | `hook_registry.handler_count(RESPONSE_COMPLETE) >= 1` |

---

## 6. 影响文件

| 文件 | 改动类型 | 改动范围 |
|------|---------|---------|
| `runtime/hooks.py` | 重构 | +6 events, +2 fields, ~30 行 |
| `kernel/engine.py` | 接入 | +3 emit 点 (RESPONSE_COMPLETE, PRE/POST_COMPACTION), ~20 行 |
| `api/websocket.py` | 接入 | +2 emit 点 (SESSION_IDLE, SESSION_CLOSE), 改造 idle 逻辑, ~15 行 |
| `runtime/invoker.py` | 接入 | +1 emit 点 (SESSION_START), ~5 行 |
| `runtime/hooks_setup.py` | 新建 | handler 注册, ~50 行 |
| `main.py` | 接入 | lifespan 中调用 register_memory_hooks(), ~3 行 |
