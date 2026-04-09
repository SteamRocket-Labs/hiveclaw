# Phase 1: T0 原始日志层

> **依赖**: 01-hooks (SESSION_CLOSE/TRIGGER_END 触发写 T0)
> **交付**: workspace logs/ 目录 + T0 logger 模块 + 5 种行为格式

---

## 1. 当前状态 (基于源码)

### 1.1 Workspace 结构 (`tools/workspace.py:122-137`)

当前 `ensure_workspace()` 创建的目录:
```
{AGENT_DATA_DIR}/{agent_id}/
├── skills/
├── workspace/
│   └── knowledge_base/
├── memory/
│   ├── learnings/          # ← T2 已有
│   └── memory.md
├── evolution/
│   ├── lineage.md
│   ├── scorecard.md
│   └── blocklist.md
└── soul.md
```

**缺失**: 没有 `logs/` 目录。T0 原始日志层不存在。

### 1.2 对话记录当前存储位置

| 行为 | 当前记录位置 | MD 文件? |
|------|------------|---------|
| 用户对话 | PostgreSQL `ChatMessage` 表 | ❌ DB only |
| 触发器执行 | PostgreSQL `ChatMessage` + `AgentActivityLog` | ❌ DB only |
| 委托 | PostgreSQL `ChatMessage` (子 session) | ❌ DB only |
| 心跳 | PostgreSQL `ChatMessage` (heartbeat session) + evolution/ 文件 | ⚠️ 部分 MD |
| 梦境 | `auto_dream_state.json` + `memory/dream_backups/` | ⚠️ JSON, 不是 MD |

**核心问题**: 对话的原始内容只在 DB 中, 不在 MD 文件系统中。违反 "MD = Source of Truth" 原则。

### 1.3 AGENT_DATA_DIR (`config.py:67`)

```python
AGENT_DATA_DIR: str = _default_agent_data_dir()
# Docker: /app/agent_data
# Local: ~/.hive/agent_data
```

---

## 2. Claude Code 对标

### 2.1 Claude Code 日志路径

**KAIROS 模式 (泄漏源码分析):**
```
logs/YYYY/MM/YYYY-MM-DD.md   # Append-only daily log
```

**Standard 模式:**
- 对话记录在 transcript JSONL: `~/.claude/projects/{path}/{session_id}.jsonl`
- 无结构化 MD 日志 (KAIROS 特有)

**Dream 读取日志**: consolidation prompt Phase 2 "Check `logs/` or `sessions/` if present"

### 2.2 Hive 的 T0 设计 (比 Claude Code 更结构化)

Claude Code 用单个 daily log 文件。Hive 按行为类型拆分:
```
logs/YYYY-MM-DD/
├── chat-1430-a1b2.md         # 每个对话独立文件
├── trigger-0900-daily.md     # 每次触发独立文件
├── delegation-1100-pm.md     # 每次委托独立文件
├── heartbeat-1000.md         # 每次 tick 独立文件
└── dream-1400.md             # 每次梦境独立文件
```

优势: 每个行为可独立处理/搜索/清理。

---

## 3. 目标状态

### 3.1 Workspace 新增 logs/ 目录

```
{AGENT_DATA_DIR}/{agent_id}/
├── logs/                       # T0: 原始行为日志 (新增)
│   ├── 2026-04-05/
│   │   ├── chat-1430-a1b2.md
│   │   ├── trigger-0900-daily.md
│   │   └── ...
│   └── 2026-04-04/
│       └── ...
├── memory/
│   ├── learnings/              # T2
│   ├── INDEX.md                # T3 索引
│   ├── feedback.md             # T3
│   ├── knowledge.md            # T3
│   ├── strategies.md           # T3
│   ├── blocked.md              # T3
│   └── user.md                 # T3
├── evolution/
│   └── lineage.md              # 策展日志
├── skills/
├── workspace/
├── focus.md                    # T1
└── soul.md                     # T3 top
```

### 3.2 T0 Logger 模块

新建 `services/t0_logger.py`:

```python
"""T0 Raw Behavior Logger — writes per-behavior MD files to logs/ directory."""

async def write_t0_log(
    agent_id: uuid.UUID,
    *,
    behavior_type: str,       # "chat" | "trigger" | "delegation" | "heartbeat" | "dream"
    messages: list[dict],     # 对话消息
    metadata: dict,           # YAML frontmatter 数据
) -> Path:
    """Write a T0 raw log file. Returns the file path."""
    ...
```

### 3.3 5 种行为的 MD 格式 (YAML frontmatter + body)

详见总纲 §4.0 的格式定义。每种格式包含:
- YAML frontmatter: type, session_id/trigger_name/etc, timestamps, status
- MD body: Turn-by-turn 对话记录 (chat), 执行过程 (trigger/delegation), 蒸馏日志 (heartbeat/dream)

---

## 4. 实现步骤

### Step 1: workspace.py 新增 logs/ 目录

在 `ensure_workspace()` (`workspace.py:122`) 中添加:
```python
(ws / "logs").mkdir(exist_ok=True)
```

同时新增 T3 文件结构:
```python
(ws / "memory" / "INDEX.md").touch(exist_ok=True)
for f in ["feedback.md", "knowledge.md", "strategies.md", "blocked.md", "user.md"]:
    p = ws / "memory" / f
    if not p.exists():
        p.write_text(f"# {f.replace('.md', '').title()}\n\n", encoding="utf-8")
```

### Step 2: 新建 services/t0_logger.py

核心函数:
- `write_t0_log()` — 写单个 T0 日志文件
- `_format_chat_log()` — 格式化对话消息为 MD
- `_format_trigger_log()` — 格式化触发器执行
- `_format_delegation_log()` — 格式化委托执行
- `_format_heartbeat_log()` — 格式化心跳 tick
- `_format_dream_log()` — 格式化梦境执行
- `_generate_filename()` — 生成 `{type}-{HHmm}-{short_id}.md`
- `cleanup_old_logs()` — 删除 >30 天的日期目录

### Step 3: 注册到 hooks

在 `hooks_setup.py` 中注册:
```python
# SESSION_CLOSE → 写 T0 (对话)
hook_registry.register(HookEvent.SESSION_CLOSE, t0_session_close_handler)
# SESSION_IDLE → 写 T0 (对话, 空闲触发)
hook_registry.register(HookEvent.SESSION_IDLE, t0_session_idle_handler)
# TRIGGER_END → 写 T0 (触发器)
hook_registry.register(HookEvent.TRIGGER_END, t0_trigger_handler)
# DELEGATION_END → 写 T0 (委托)
hook_registry.register(HookEvent.DELEGATION_END, t0_delegation_handler)
# HEARTBEAT_TICK_END → 写 T0 (心跳)
hook_registry.register(HookEvent.HEARTBEAT_TICK_END, t0_heartbeat_handler)
# DREAM_END → 写 T0 (梦境)
hook_registry.register(HookEvent.DREAM_END, t0_dream_handler)
```

### Step 4: cleanup_old_logs() 集成

在 `auto_dream.py` 的 `run_dream()` 末尾调用:
```python
from app.services.t0_logger import cleanup_old_logs
cleanup_old_logs(agent_id, retention_days=30)
```

---

## 5. 验收标准

| # | 验收项 | 方法 |
|---|--------|------|
| V1 | ensure_workspace 创建 logs/ 目录 | 新 agent → `ls {data_dir}/{agent_id}/logs/` 存在 |
| V2 | 对话结束后写 T0 | 发消息 → `ls logs/YYYY-MM-DD/chat-*.md` 存在 |
| V3 | T0 文件有 YAML frontmatter | `head -5 logs/.../chat-*.md` 包含 `---` + `type: chat` |
| V4 | T0 文件包含完整对话 | 文件内容包含 User/Agent 轮次 |
| V5 | 30 天清理可用 | 手动调用 `cleanup_old_logs()` → 旧目录被删 |
| V6 | T3 文件结构初始化 | 新 agent → `ls memory/` 包含 5 个 category 文件 + INDEX.md |

---

## 6. 影响文件

| 文件 | 改动类型 | 改动范围 |
|------|---------|---------|
| `tools/workspace.py` | 修改 | ensure_workspace +3 行 (logs/ + T3 files) |
| `services/t0_logger.py` | 新建 | T0 写入 + 5 种格式化 + 清理, ~200 行 |
| `runtime/hooks_setup.py` | 修改 | 注册 6 个 T0 handler, ~30 行 |
| `services/auto_dream.py` | 修改 | run_dream 末尾调用 cleanup_old_logs, +3 行 |
