# Hive 记忆系统重构方案

> **状态**: 架构确认 v9 (2026-04-05)
> **置信度**: ~95%
> **核心认知**: 4 层金字塔 (T0→T2��T3→soul)，3 个蒸馏器各守一层：提取 (T0→T2)、心跳 (T2→T3, KAIROS 持续 session)、梦境 (T3→soul)。每个蒸馏器只负责一个层级转换，不跨层，无职责重叠。MD = source of truth。
> **核心原则**: **记忆是可选的。会话永远继续。每一层独立失败。**
> **降级哲学**: 通过架构设计消除失败场景，而不是给失败场景写 fallback。

---

## 1. 设计原则

| 原则 | 含义 |
|------|------|
| **产出与蒸馏分离** | 产出者 (对话/触发器/委托) 只写 T0+T2。蒸馏由 3 个蒸馏器执行: 提取 (T0→T2)、心跳 (T2→T3)、梦境 (T3→soul) |
| **MD = Source of Truth** | Memory content 只存在于 MD 文件中。Agent 和人类直接读写 MD |
| **DB = 辅助角色** | 数据库只做两件事：session journal (元数据) + search index (FTS5 影子索引) |
| **MD 输入 MD 输出** | 所有循环的起点和终点都是 MD 文件，不经过 JSON 格式转换 |
| **三蒸馏器单一职责** | 每个蒸馏器只守一个层级转换: 提取 (T0→T2), 心跳 (T2→T3), 梦境 (T3→soul)。不跨层，无重叠 |
| **权重差异化** | 不同行为的信号强度不同，决定蒸馏优先级 |
| **冻结快照** | Prompt 中的记忆在 invoke 入口冻结，会话内不变，保护 LLM prompt cache |
| **核心原则** | **记忆是可选的。会话永远继续。每一层独立失败。** 不可违反的最高约束 |
| **降级友好** | 核心路径零 LLM 依赖 (pattern-based)，LLM 是增强层。DB 挂了不影响记忆 |

---

## 2. 架构全景：产出者 + 蒸馏器

```
┌─────────────────────────────────────────────────────────────────┐
│  产出者 (只写 T0, 不做蒸馏)                                       │
│  ──────────────────────────                                      │
│  用户对话 (含外部渠道消息)  ──→ logs/YYYY-MM-DD/chat-*.md (T0)   │
│  触发器执行                ──→ logs/YYYY-MM-DD/trigger-*.md (T0) │
│  Agent 委托               ──→ logs/YYYY-MM-DD/delegation-*.md   │
│  任务状态变更              ──→ focus.md (T1)                     │
│                                                                  │
├─────────────────────────────────────────────────────────────────┤
│  3 个蒸馏器 (各守一层, 不跨层)                                    │
│  ─────────────────────────────                                   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ 提取器 — T0 → T2 (对齐 Claude Code extractMemories)  │   │
│  │ 触发: session-end + 上下文压缩前                            │   │
│  │ 机制: LLM 提取 agent (降级: pattern-based)                │   │
│  │ 读: 对话消息 (还在内存中, 不是读 T0 文件)                   │   │
│  │ 写: learnings/*.md (T2) + logs/*.md (T0)                  │   │
│  └──────────────────────────────────────────────────────────┘   │
│       ↓                                                          │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ 心跳 — T2 → T3                                │   │
│  │ 触发: ~45min tick (持续 session, KAIROS 模式)              │   │
│  │ 机制: LLM 判断 (持续 session 内增量)                       │   │
│  │ 读: T2 增量 + T3 参考 (防重复)                             │   │
│  │ 写: memory/*.md 追加 + lineage.md + T0 heartbeat-*.md     │   │
│  │ 附带: 可选自主动作 (共享 session, 概念上独立)               │   │
│  └──────────────────────────────────────────────────────────┘   │
│       ↓                                                          │
│  ┌──────────────────────────────────────────────────────────┐   │
│  │ 梦境 — T3 → T3 refined + soul                │   │
│  │ 触发: 4h + (3 sessions OR 2 curator ticks)                │   │
│  │ 机制: 程序化 (核心) + LLM (增强)                           │   │
│  │ 读: T3 全量 + lineage (蒸馏历史)                           │   │
│  │ 写: T3 精简 + soul.md + INDEX.md + FTS5                   │   │
│  │ 清理: T2 截断 + T0 >30d 删除 + lineage 归档               │   │
│  │ ⚠️ 不读 T0, 不读 T2 内容 (T2→T3 是 心跳 独占)          │   │
│  └──────────────────────────────────────────────────────────┘   │
│                                                                  │
│  蒸馏链: T0 →[Extract]→ T2 →[Curate]→ T3 →[Archive]→ soul     │
│  保留策略: T0 30天 | T2 10条 | T3 cap | soul 20条               │
└─────────────────────────────────────────────────────────────────┘
```

---

## 3. 数据库的两个辅助角色

### 角色 1: Session Journal（Claude Code 模式）

```
存什么: 会话元数据 — sessions, summaries, observations
谁写:  服务端 hooks (session-end, heartbeat-end, observation capture)
谁读:  - session start: 注入最近会话上下文 (~150 tokens)
       - mid-workflow recall: 注入过去的 test failures
       - 梦境蒸馏: 增强输入 (非必需, DB 不可用时跳过)
本质:  "发生了什么" 的日志，不是 "学到了什么" 的知识
⚠️ 两个蒸馏器的必需输入仅为 MD 文件。DB 是增强，不是依赖。
```

### 角色 2: Search Index（Hermes Holographic 模式）

```
存什么: memory/*.md 内容的 FTS5 全文索引
谁写:  梦境蒸馏结束后，从 MD 文件解析重建
谁读:  recall tool (Agent 主动搜索) + prefetch (可选增强)
本质:  MD 文件的只读影子索引。MD 更新 → 索引重建。索引丢了 → 从 MD 重建。
```

**关键保证**: DB 完全挂掉，Agent 的记忆仍然完整（MD 文件还在）。

---

## 4. MD 文件四层金字塔

### 4.0 T0: Raw Behavior Logs（原始行为日志）— 恢复源

**所有行为完成时产出对应的 MD 文件。按日期目录组织，30 天保留。**

```
{agent_id}/
└── logs/
    ├── 2026-04-05/
    │   ├── chat-1430-a1b2.md           # 用户对话
    │   ├── feishu-0900-e5f6.md         # 外部渠道消息
    │   ├── trigger-0900-daily.md       # 触发器执行
    │   ├── delegation-1100-research.md # Agent 委托
    │   ├── heartbeat-1000.md           # 心跳 tick
    │   ├── heartbeat-1045.md           # 心跳 tick
    │   └── dream-1400.md              # 梦境执行
    ├── 2026-04-04/
    │   └── ...
    └── ...  (>30d 自动清理整个日期目录)
```

| 属性 | 值 |
|------|-----|
| **谁写** | 所有行为完成时: invoker session-end, trigger_daemon, orchestrator, heartbeat, auto_dream |
| **写入方式** | pattern-based 格式化 (零 LLM 依赖) — 把执行结果直接格式化为 MD |
| **命名** | `{type}-{HHmm}-{short_id}.md` — 按行为类型前缀 + 时间排序 |
| **谁读** | 心跳蒸馏 (可选: 回看原始对话上下文) + 人工 debug/审计 |
| **不注入 prompt** | T0 不进入对话 prompt (太大太原始) |
| **保留** | 30 天 → 自动删除日期目录 |
| **核心价值** | 恢复源: T2 提取遗漏时可从 T0 重新提取; 审计: `ls logs/` 一目了然 |

#### T0 文件格式 (YAML frontmatter + MD body)

**用户对话 / 外部渠道消息:**
```markdown
---
type: chat
session_id: abc-123
source: web | feishu | slack | wecom | dingtalk | teams | discord
user: Rocky
started: 2026-04-05T14:30:00+08:00
turns: 8
tools: [web_search, write_file, read_file]
---

## Turn 1 (14:30)
**User**: [消息内容]
**Agent**: [响应内容]
**Tools**:
- `tool_name(args)` → [结果摘要]

## Turn 2 (14:31)
...
```

**触发器执行:**
```markdown
---
type: trigger
trigger_name: daily-standup
trigger_type: cron | interval | poll | event | on_message
executed: 2026-04-05T09:00:00+08:00
status: success | error
duration_ms: 45000
---

## Instruction
[触发器执行指令]

## Execution
[工具调用 + 过程]

## Result
[最终结果]
```

**Agent 委托:**
```markdown
---
type: delegation
from: PM-Agent
to: Research-Agent
task: [委托任务简述]
delegated: 2026-04-05T11:00:00+08:00
status: success | error
---

## Task
[委托指令原文]

## Execution
[执行过程]

## Result
[返回结果]
```

**心跳 tick:**
```markdown
---
type: heartbeat
tick: 3
session_started: 2026-04-05T09:15:00+08:00
executed: 2026-04-05T10:45:00+08:00
new_t2: 3
distilled: 2
score: 7
---

## New T2 Entries
- [类型] 内容描述

## Distillation
- "条目" → target_file ✅ | skipped

## Action
[自主动作 或 none]
```

**梦境:**
```markdown
---
type: dream
executed: 2026-04-05T14:00:00+08:00
t3_processed: 5
deduped: 8
promoted_to_soul: 2
---

## Dedup
[各文件去重摘要]

## Soul Promotion
[提炼到 soul.md 的条目]

## Cleanup
[T2 截断, lineage 归档, FTS5 重建]
```

### 4.1 T1: Working Memory（当前工作记忆）

```
{agent_id}/
└── focus.md        — Agent 当前任务清单 (volatile)
```

| 属性 | 值 |
|------|-----|
| **谁写** | Agent (write_file), 产出者行为 |
| **谁读** | 每次 invoke (Dynamic suffix, P0) + 心跳蒸馏 (参考) |
| **蒸馏** | 不向上蒸馏。梦境仅清理完成/过期项 |
| **上限** | 3000 chars |

### 4.2 T2: Episodic Memory（短期情景记忆）— 从 T0 提取

**从 T0 原始日志中 pattern-based 提取，等待蒸馏器处理。**

```
{agent_id}/
└── learnings/
    ├── errors.md       — 操作失败 (权重 0.7)
    ├── insights.md     — 纠正、洞察、决策 (权重 0.3-1.0)
    └── requests.md     — 能力缺口 (权重 0.3)
```

| 属性 | 值 |
|------|-----|
| **谁写** | session-end hook: 从 T0 日志中 pattern-based 提取 + LLM 增强 |
| **写入时机** | 每次行为完成时 (写 T0 的同时提取写 T2) |
| **格式** | `- [YYYY-MM-DD] 描述` (MD bullet) |
| **谁读** | 心跳蒸馏器 (主要消费者) + 梦境蒸馏器 (处理心跳遗漏的残留) |
| **不注入 prompt** | T2 不直接进入对话 prompt (蒸馏后的 T3 才进入) |
| **清理** | 梦境蒸馏后截断到 10 条 |
| **恢复** | T2 丢失时可从 T0 (30 天内) 重新提取 |
| **DB 参与** | ❌ |

### 4.3 T3: Semantic Memory（长期语义记忆）— 蒸馏产物

**只有蒸馏器 (心跳/梦境) 写入 T3。产出者不直接写 T3。**

```
{agent_id}/
├── soul.md             — Agent 身份 (金字塔顶端, 仅梦境写入)
└── memory/
    ├── INDEX.md        — 索引 + 路由表
    ├── feedback.md     — 用户纠正 + 规则 (category: feedback, constraint)
    ├── knowledge.md    — 项目/领域知识 (category: project, reference)
    ├── strategies.md   — 有效策略 (category: strategy)
    ├── blocked.md      — 失败方法 (category: blocked_pattern)
    └── user.md         — 用户画像 (category: user)
```

| 属性 | 值 |
|------|-----|
| **谁写** | 心跳 (T2→T3 增量追加) + 梦境 (T3 精简 + soul 提炼) |
| **产出者能否直写** | ❌ 不能。产出者只写 T0，经提取器→心跳到达 T3 |
| **例外** | 3 连败 → blocked.md (⚡安全通路) + Agent save_memory tool (显式工具调用) |
| **谁读** | 中循环开始 (frozen prompt) + 心跳 (策展时参考防重复) |
| **DB 参与** | 梦境结束后从 MD 重建 FTS5 索引 |

#### INDEX.md 格式

```markdown
# Memory Index
Updated: 2026-04-05 14:30

| File | Category | Items | Updated | Load |
|------|----------|-------|---------|------|
| feedback.md | feedback, constraint | 12 | 2026-04-05 | P0 始终 |
| knowledge.md | project, reference | 28 | 2026-04-04 | P1 按需 |
| strategies.md | strategy | 8 | 2026-04-03 | P1 按需 |
| blocked.md | blocked_pattern | 5 | 2026-04-05 | P0 始终 |
| user.md | user | 6 | 2026-04-02 | P2 可选 |
```

### 4.4 蒸馏日志

```
{agent_id}/
└── evolution/
    └── lineage.md      — 蒸馏器操作日志 (心跳写入, 记录每次蒸馏做了什么)
```

| 属性 | 值 |
|------|-----|
| **谁写** | 心跳蒸馏器 (Phase 4 LOG) |
| **本质** | 蒸馏器的操作日志，不是产出者的行为日志 |
| **格式** | `### HB-YYYY-MM-DD-HH:MM` + 蒸馏了什么 / 提炼了什么 / 自主动作 / 得分 |
| **谁读** | 心跳蒸馏 (尾部, 避免重复) + 梦境蒸馏 (输入) |
| **清理** | >200 条归档 |

### 4.5 非蒸馏文件（不参与蒸馏链）

| 文件 | 类型 | 来源 | Prompt 注入 |
|------|------|------|------------|
| scorecard.md | Dashboard | 心跳计数器 | 仅心跳模式 (P3) |
| relationships.md | DB Mirror | workspace_sync | Frozen prefix (P2) |
| company_profile.md | DB Mirror | workspace_sync | 可选 |
| org_structure.md | DB Mirror | workspace_sync | 可选 |
| HEARTBEAT.md | Template | /app/templates/ | 仅心跳模式 |

### 4.6 三层能力分层

```
┌──────────────────────────────────────────────────────────────────┐
│  Layer 3: Hive 独有 — 三蒸馏器管线自进化                           │
│  ──────────────────────────────────────                           │
│  提取器 (T0→T2, LLM agent, session-end + 压缩前)              │
│  心跳 (T2→T3, LLM, KAIROS 持续 session, ~45min tick)          │
│  梦境 (T3→soul, programmatic+LLM, ~4h)                      │
│  独有���力: 三级管线 + 持续 session 策展 + 跨 tick 模式累积          │
│  Claude Code: 1 级 (/dream) | Hermes: 0 级自动 (LLM self-manage)│
├──────────────────────────────────────────────────────────────────┤
│  Layer 2: Hermes 增强 — 结构化检索                                │
│  ──────────────────────────────                                  │
│  FTS5 搜索索引 + trust scoring + Provider 隔离                    │
│  Agent 工具 (save_memory/recall) + 冻结快照                       │
├──────────────────────────────────────────────────────────────────┤
│  Layer 1: Claude Code 基座 — MD + Hooks                          │
│  ──────────────────────────────                                  │
│  MD = Source of Truth + Session Journal (DB) + Pattern-based 提取 │
│  Fail Silently + Never Block + 所有核心机制最弱模型可用            │
└──────────────────────────────────────────────────────────────────┘
```

---

## 5. 产出者：行为 × 写入映射

### 5.1 产出者 1: 用户对话（含外部渠道消息）

Agent 和人类交互，产出**对象层知识**。产出者**只写 T0**，T2 由 提取器 提取。

| 行为 | 权重 | T0 文件 | 提取器 → T2 | 
|------|------|--------|----------------|
| 用户显式纠正 ("不要用 X") | 1.0 | chat-*.md | → insights.md |
| 用户对话中的洞察 | 0.8 | chat-*.md | → insights.md |
| 工具执行出错 | 0.7 | chat-*.md | → errors.md |
| 能力缺口 ("如果能XX就好了") | 0.3 | chat-*.md | → requests.md |
| 任务状态更新 | N/A | chat-*.md | → focus.md (T1) |

### 5.2 产出者 2: 触发器 / Agent 委托

Agent 执行自主任务，产出**行动层知识**。

| 行为 | 权重 | T0 文件 | 提取器 → T2 |
|------|------|--------|----------------|
| 触发器执行结果 | 0.5 | trigger-*.md | → insights.md |
| Agent 委托结果 | 0.5 | delegation-*.md | → insights.md |
| 执行中出错 | 0.7 | trigger/delegation-*.md | → errors.md |

### 5.3 产出者不直接写 T2/T3

| ❌ 不允许 | 替代 |
|----------|------|
| 产出者直接写 learnings/*.md | 产出者写 T0，提取器 提取到 T2 |
| 产出者直接写 memory/*.md | 写 T0 → 提取器 → T2 → 心跳 → T3 |
| 产出者直接写 soul.md | T0 → ... → T3 → 梦境 → soul |
| 例外: Agent 调 save_memory tool | 允许 — 显式工具调用是 Agent 的主动决策，直写 T3 |
| 例外: 3 连败 → blocked.md | 允许 — 安全通路，直写 T3 |

---

## 5.5 提取器 — T0 → T2

**提取器是第一级蒸馏器，用 LLM 从对话中提取关键发现。对齐 Claude Code 的 `extractMemories` 机制。**

### 5.5.1 核心设计 (对齐 Claude Code)

| 属性 | 值 |
|------|-----|
| **负责层级** | T0 → T2 (唯一) |
| **触发** | RESPONSE_COMPLETE (每轮, 主路径) + PRE_COMPACTION (压缩前) + SESSION_IDLE/CLOSE (兜底) |
| **机制** | **LLM 提取 agent** (对齐 Claude Code `runForkedAgent`) |
| **读** | 对话消息 (kernel 结束前还在内存中，不是读 T0 文件) |
| **写** | learnings/errors.md, insights.md, requests.md + T0 日志 |
| **工具权限** | 只读 + 只写 learnings/ 目录 |
| **最大轮次** | 3 轮 (防止跑偏，Claude Code 限 5 轮) |
| **不做** | 不读 T3, 不做 T3 去重, 不写 memory/*.md |

### 5.5.2 触发时机 (三层保障)

```
Layer 1: RESPONSE_COMPLETE — 每轮响应后 (主路径, 对齐 Claude Code Stop hook)
  ──→ Agent 回复完成，无后续工具调用
  ──→ fire-and-forget 触发提取 agent (不阻塞用户)
       读: 对话消息 (还在内存中)
       写: T2 learnings/*.md
  ──→ 可配节流: 每 N 轮提取一次 (默认每轮)

Layer 2: PRE_COMPACTION — 上下文压缩前
  ──→ Kernel 检测到 85% 上下文阈值
  ──→ 压缩前触发提取 (保全即将丢失的上下文)
       读: 即将被压缩的旧消息
       写: T2 learnings/*.md
  ──→ Kernel 执行压缩

Layer 3: SESSION_IDLE / SESSION_CLOSE — 兜底
  ──→ 空闲超时 (N 分钟无消息) 或 WebSocket 断开/新对话创建
  ──→ drain pending 提取 + 写 T0 完整日志
  ──→ 覆盖: 用户放着不管、直接关浏览器
```

### 5.5.3 LLM 提取 Agent 指令

```
你是记忆提取子 Agent。分析以下对话消息，提取值得保留的发现。

提取类型:
  - 用户纠正/偏好 (权重 1.0) → learnings/insights.md
  - Agent 洞察/发现 (权重 0.8) → learnings/insights.md
  - 执行出错/失败 (权重 0.7) → learnings/errors.md
  - 能力缺口/需求 (权重 0.3) → learnings/requests.md

规则:
  - 只从对话消息中提取，不 grep 代码
  - 检查现有 learnings 文件，避免重复追加
  - 每条提取用 `- [YYYY-MM-DD] 描述` 格式追加
  - 宁可多提取也不遗漏 (去重是心跳的工作)
  - 临时任务细节不提取 (那属于 focus.md)
```

### 5.5.4 降级 (LLM 不可用)

```
LLM 可用 (主路径):
  LLM 提取 agent → T2 (高质量，能抓隐式偏好和推理洞察)

LLM 不可用 (地板):
  Pattern-based regex → T2 (覆盖显式信号)
  规则:
    用户消息含 "不要/别/禁止/stop/don't" → insights.md
    用户消息含 "记住/以后/always/never"  → insights.md
    工具返回 error/failed/exception      → errors.md
    用户消息含 "如果能/要是有/需要"       → requests.md
```

### 5.5.5 与 Claude Code 的对齐和差异

| 维度 | Claude Code | Hive 提取器 |
|------|------------|------------|
| 触发 | 每轮对话后 (fire-and-forget) | RESPONSE_COMPLETE (每轮) + PRE_COMPACTION + SESSION_IDLE/CLOSE |
| 执行 | `runForkedAgent` 共享 prompt cache | 后台 LLM 调用 (无 fork 机制) |
| 读 | 内存中的 messages (cursor 增量) | 内存中的 messages (全量/压缩前) |
| 写 | memory/*.md 按 topic 分文件 | learnings/*.md 按类型分文件 |
| 节流 | 可配每 N 轮提取 | per-session (不节流) |
| 去重 | 提取 agent 先读现有 memory | 提取 agent 先读现有 learnings |
| Dream | 24h+5s → 4 阶段合并 | 心跳 45min T2→T3 + 梦境 4h T3→soul |

**关键差异**: Claude Code 只有 2 层 (提取→memory)，所以提取 agent 直接写最终 memory 文件。Hive 有 4 层 (T0→T2→T3→soul)，所以提取 agent 只写 T2，策展和归档交给心跳和梦境。

**为什么 Hive 不是每轮提取**: Claude Code 是单用户 CLI，prompt cache 共享让每轮提取几乎零成本。Hive 是服务端多 Agent，每轮提取 = 额外 LLM 调用 = 真实成本。Session-end 提取是合理的平衡点。

---

## 6. 心跳 — T2 → T3 策展

### 6.1 本质

**心跳 = 持续 session 内的知识策展器。**

它的唯一工作：**判断 T2 的每条记录是否值得进入 T3，如果是，放到正确的 category 文件里。** 类似图书管理员——新书进来，决定放到哪个书架。借鉴 KAIROS tick 机制，运行在持续对话 session 中。

### 6.2 持续 Session 执行模型 (KAIROS-inspired)

**核心变化**: 心跳不再每次创建新 session。每个 Agent 维护一个持续的心跳对话上下文。

```
┌─────────────────────────────────────────────────────────────────┐
│  心跳持续 Session (per-agent, 服务端内存)                         │
│                                                                  │
│  tick 1 (10:00):                                                │
│    [首次] 完整初始化: system prompt + T2 全量 + T3 参考           │
│    Agent 蒸馏 → 响应留在对话上下文中                              │
│                                                                  │
│  tick 2 (10:45):                                                │
│    <tick 10:45> + 仅新增 T2 条目 (增量)                          │
│    Agent 看到上次的思考 → 增量蒸馏 (不重复处理)                    │
│                                                                  │
│  tick 3 (11:30):                                                │
│    <tick 11:30> + 仅新增 T2 条目                                 │
│    Agent 积累了 1.5h 的模式观察 → 更高质量的蒸馏决策               │
│                                                                  │
│  ...                                                             │
│                                                                  │
│  Session 重置触发条件:                                            │
│    - 梦境 触发时 (T3 全量精简后, 重置 心跳 session)         │
│    - 日切 (00:00 UTC+agent_tz)                                   │
│    - 进程重启 (内存清空, 下次 tick 自动重新初始化)                  │
│    - 上下文达到 kernel 85% 压缩阈值 (kernel 自动 compact)         │
└─────────────────────────────────────────────────────────────────┘
```

**实现要点:**

```python
# 服务端内存维护持续对话上下文
_heartbeat_contexts: dict[uuid.UUID, list[dict]] = {}

# tick 逻辑:
if agent_id not in _heartbeat_contexts:
    # 首次 tick: 完整初始化 (读 T2 全量 + T3 参考)
    messages = [{"role": "user", "content": full_heartbeat_instruction}]
else:
    # 后续 tick: 注入 <tick> + 仅新增 T2 条目
    new_t2 = _read_incremental_t2(agent_id)  # mtime delta
    if not new_t2:
        # T2 无新内容 → 跳过蒸馏 (空转保护)
        lineage_append(agent_id, "noop - no new T2")
        return
    messages = _heartbeat_contexts[agent_id]
    messages.append({"role": "user", "content": f"<tick>{now}</tick>\n{new_t2}"})

# 调用 kernel (kernel 自带 85% 压缩)
result = await invoke_agent(InvocationRequest(messages=messages, ...))
messages.append({"role": "assistant", "content": result.content})
_heartbeat_contexts[agent_id] = messages
```

**优势 vs 当前 stateless 模式:**

| 维度 | stateless (当前) | 持续 session |
|------|-----------------|-------------|
| 思维连续性 | 无 — 每次从零推导 | Agent 看到自己之前的推理链 |
| 跨 tick 模式识别 | 不可能 | "这个错误连续 3 次 tick 出现" |
| T2 去重 | 靠 lineage 间接判断 | 对话中直接可见"上次已处理 X" |
| T2 读取量 | 全量 | 仅增量 (mtime delta) |
| API 输入成本 | 每次全量 context | 后续 tick 仅追加 tick msg |
| 空转保护 | 无 (120min 必执行) | T2 无新内容 → skip |

**降级保证:**

```
进程重启 → _heartbeat_contexts 清空 → 下次 tick = 首次 tick (全量初始化)
                                     → MD 文件完整，无数据丢失
                                     → 符合核心原则: "记忆是可选的, session 永远继续"
```

### 6.3 心跳 4 阶段协议 (HEARTBEAT.md)

**在持续 session 中，4 阶段是每次 tick 的执行框架。**

```
Phase 1: OBSERVE — 读取 T2 输入
  首次 tick:
    必读: learnings/*.md 全量 (T2) + focus.md (T1)
    参考: memory/*.md (T3, 防重复写入)
    日志: lineage.md 尾部
  后续 tick:
    仅读 <tick> 注入的新增 T2 条目 (增量)
    上次的策展决策已在对话历史中

Phase 2: CURATE — 策展 T2 → T3
  遍历 T2 新条目，判断:
    这条值不值得进入 T3？(噪声 vs 知识)
    属于哪个 category 文件？
    T3 里是否已有类似内容？(防重复)
  写入 T3:
    用户偏好/纠正   → memory/feedback.md 追加
    项目/领域知识   → memory/knowledge.md 追加
    有效策略        → memory/strategies.md 追加
    失败方法        → memory/blocked.md 追加
    用户画像        → memory/user.md 追加
  ⚠️ 心跳 不做 T3 内部去重 (那是 梦境 的工作)

Phase 3: ACT (可选) — 一个自主动作
  共享 session context 但概念上不是蒸馏:
    修复 errors.md 中的错误
    创建/改进技能
    研究 requests.md 中的能力缺口
    社交 / 发消息给同事 Agent
  如果无可行动项: 跳过

Phase 4: LOG — 记录策展日志
  写 lineage.md:
    ### CUR-YYYY-MM-DD-HH:MM
    - Curated: {从 T2 策展了几条到 T3, 哪些 category}
    - Skipped: {跳过了几条, 原因}
    - Action: {自主动作 或 skip}
    - Score: {0-10}
  写 T0: logs/YYYY-MM-DD/heartbeat-HHmm.md
  更新 scorecard.md
```

### 6.4 心跳 参数

| 参数 | 值 | 说明 |
|------|-----|------|
| tick 间隔 | **45 min** (agent 可配, 下限 30min) | 持续 session 模式下可更高频 (增量成本低) |
| 活跃时段 | 09:00-18:00 (可配) | 非活跃时段不 tick |
| 空转保护 | T2 无新条目 → skip | 避免无意义 API 调用 |
| Session 生命周期 | 日级 (00:00 重置 / 梦境 触发重置 / 进程重启重置) | |
| 最大工具轮次 | 25 | 预算分配: Phase 1 ~4, Phase 2 ~8, Phase 3 ~8, Phase 4 ~4 |
| lineage 上限 | 200 条 | 超过归档 |
| 3 连败封禁 | 自动 → blocked.md | ⚡安全通路 |
| Kernel 压缩 | 85% context 阈值 | 持续 session 过长时 kernel 自动 compact |

---

## 7. 梦境 — T3 → T3 refined + soul 归档

### 7.1 本质

**梦境 = 定时全量精简器。** 它关注 T3 内部的质量控制和 soul 提炼。**梦境 不读 T0、不读 T2 内容** — T2→T3 是 心跳 独占的工作。

### 7.2 处理流程

```
触发: ≥ 4 小时 AND (≥ 3 sessions OR ≥ 2 curator ticks)

Step 1: 读取 T3 全量
  memory/feedback.md + knowledge.md + strategies.md + blocked.md + user.md

Step 2: T3 各文件内部精简 (MD→MD)
  核心 (零 LLM): 文本相似度 >70% 去重 + 每文件 cap 截断
  增强 (LLM 够): LLM 重写精简每个文件

Step 3: 跨文件去重
  同一条知识出现在 feedback 和 knowledge 里 → 保留更合适的那个

Step 4: 高频模式 → soul.md
  feedback.md 中出现 3+ 次的模式 → soul.md Learned Behaviors
  核心: 直接复制原文
  增强: LLM 改写为第一人称
  规则: 整体替换 Learned Behaviors (不追加), 上限 20 条

Step 5: 更新 INDEX.md

Step 6: 从 memory/*.md 重建 FTS5 索引

Step 7: 清理 (非蒸馏, 纯维护)
  T2 learnings/*.md → 截断到 10 条 (⚠️ 只截断, 不读内容)
  T0 logs/ → 删除 >30 天的日期目录
  lineage.md → >200 条归档
  focus.md → 删 [x] + 删 >7d

Step 8: 重置 心跳 session
  通知 heartbeat 清空 _heartbeat_contexts (下次 tick 重新初始化)

Step 9: 写 T0 归档日志
  logs/YYYY-MM-DD/dream-HHmm.md
```

### 7.3 三蒸馏器对比

| | 提取器 | 心跳 | 梦境 |
|--|-------------------|-----------------|-------------------|
| **层级** | T0 → T2 | T2 → T3 | T3 → T3 refined + soul |
| **执行模型** | 后台 LLM agent (对齐 Claude Code) | 持续 session + tick (KAIROS) | 独立 invocation |
| **频率** | 每轮响应后 (主) + 压缩前 + 空闲/关闭 | ~45min tick (空转跳过) | ~4h + (3s OR 2 ticks) |
| **机制** | **LLM 提取 agent** (降级: pattern regex) | LLM 判断 (持续 session) | 程序化 (核心) + LLM (增强) |
| **输入** | 对话消息 (还在内存中) | T2 增量 + T3 参考 | T3 全量 + lineage |
| **输出** | T2 learnings + T0 原始日志 | T3 memory 追加 + lineage | T3 精简 + soul + INDEX + FTS5 |
| **LLM** | **必需** (降级: pattern 兜底) | 必需 | 核心可零 LLM |
| **跨层** | ❌ 不读 T3 | ❌ 不写 soul, 不做 T3 去重 | ❌ 不读 T0/T2 内容 |
| **副作用** | 同时写 T0 原始日志 | 可选自主动作 | 清理 T0/T2 + 重置心跳 |
| **类比** | 会议纪要员: 记录+提炼要点 | 图书管理员: 上架新书 | 年终大扫除: 去重→精华→身份 |

---

## 8. 嵌套循环架构

```
┌──────────────────────────────────────────────────────────────────┐
│  大循环 (梦境, ~4h + 3s/2ticks)                               │
│  ───────────────────────────────                                  │
│  读: T3 全量 + lineage (蒸馏历史)                                  │
│  写: T3 精简 + soul.md + INDEX.md + FTS5                         │
│  清理: T2 截断 + T0 >30d + lineage 归档 + 重置 心跳 session   │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  中循环 (Per-Session, ~分钟到小时)                          │    │
│  │  ──────────────────────────────                           │    │
│  │  起点: 读 T3 (soul + memory/*.md) + T1 (focus) → frozen   │    │
│  │  执行: 多轮对话 / 触发器 / 委托                             │    │
│  │  终点: 写 T2 (learnings/*.md) — 产出者行为                  │    │
│  │  DB: 写 session 元数据 (journal)                           │    │
│  │                                                           │    │
│  │  ┌───────────────────────────────────────────────────┐    │    │
│  │  │  小循环 (Per-Turn, ~秒)                             │    │    │
│  │  │  ─────────────────                                  │    │    │
│  │  │  读: focus.md                                       │    │    │
│  │  │  写: focus.md + learnings/*.md (如有)                │    │    │
│  │  │  DB: ❌                                             │    │    │
│  │  └───────────────────────────────────────────────────┘    │    │
│  └──────────────────────────────────────────────────────────┘    │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐    │
│  │  心跳 循环 (持续 session 策展, ~45min tick)              │    │
│  │  ──────────────────────────────────                        │    │
│  │  执行模型: 持续 session + <tick> 注入 (KAIROS 模式)        │    │
│  │  首次 tick: 读 T2 全量 + T3 参考 (完整初始化)              │    │
│  │  后续 tick: 仅增量 T2 (mtime delta) + 上下文连续           │    │
│  │  空转保护: T2 无新条目 → skip tick                        │    │
│  │  写: T3 memory/*.md (增量追加) + lineage.md (策展日志)      │    │
│  │  + 可选自主动作 (共享 session, 概念独立)                   │    │
│  │  重置: 梦境 触发 / 日切 / 进程重启                     │    │
│  └──────────────────────────────────────────────────────────┘    │
│                                                                  │
│  闭环: 梦境精简 T3 → 下一个中循环的 frozen prompt → 自我进化       │
└──────────────────────────────────────────────────────────────────┘
```

### 循环间的数据流 (4 层蒸馏链)

```
行为完成 → T0 原始日志 (logs/YYYY-MM-DD/*.md)
    ↓ 提取器: pattern-based 提取 (per-session, 零 LLM)
提取器 产出 → T2 learnings (errors/insights/requests.md)
    ↓ 心跳: 持续 session 策展 (~45min tick)
心跳 产出 → T3 memory/*.md (增量追加) + lineage (策展日志)
    ↓ 梦境: 全量精简 (~4h)
梦境 产出 → T3 精简 + soul.md + INDEX + FTS5
    ↓ 下一个中循环读取
中循环 frozen prompt (包含最新 T3 + soul)
    ↓ Agent 在新知识基础上做出更好的决策

保留策略:
  T0: 30 天 → 删除日期目录
  T2: 梦境后截断到 10 条
  T3: cap per file (待定)
  soul: 永久, max 20 条
```

---

## 9. 上下文组装

### 9.1 普通对话模式

```
FROZEN PREFIX:
  P0  soul.md              T3 Identity      ~3000 chars
  P2  relationships.md     Mirror           ~1000 chars
  -   skills catalog       Static           variable

── CACHE BOUNDARY ──

DYNAMIC SUFFIX:
  P0  focus.md             T1 Working       ~1000 chars
  P0  memory/feedback.md   T3 Knowledge     ~500 chars
  P0  memory/blocked.md    T3 Knowledge     ~300 chars
  P1  memory/knowledge.md  T3 Knowledge     ~800 chars
  P1  memory/strategies.md T3 Knowledge     ~400 chars
  P2  memory/user.md       T3 Knowledge     ~300 chars
  P2  session summaries    DB (journal)     ~500 chars

总预算: ~8000 chars (~2200 tokens)
```

### 9.2 心跳蒸馏模式

```
普通对话上下文 + 额外:
  P0  learnings/errors.md     T2 (蒸馏输入)   ~500 chars
  P0  learnings/insights.md   T2 (蒸馏输入)   ~500 chars
  P1  learnings/requests.md   T2 (蒸馏输入)   ~300 chars
  P3  lineage.md (尾部)       蒸馏日志         ~500 chars
  P3  scorecard.md            Dashboard       ~300 chars
  -   HEARTBEAT.md            Template        ~2000 chars
```

**关键变化**: 心跳现在读 memory/*.md (T3) 作为蒸馏上下文，不再只看 evolution 文件。

### 9.3 加载优先级

```
P0 不可裁: soul.md, focus.md, feedback.md, blocked.md
           + 心跳模式: errors.md, insights.md
P1 优先保留: knowledge.md, strategies.md
P2 可裁剪: user.md, relationships.md, session summaries
P3 按需: lineage, scorecard, requests.md (仅心跳)
```

---

## 10. 行为权重体系

| 行为 | 权重 | 产出者 | 写入 T2 | 心跳蒸馏到 T3 |
|------|------|--------|---------|-------------|
| 用户显式纠正 | 1.0 | 用户对话 | insights.md | → feedback.md |
| 用户对话洞察 | 0.8 | 用户对话 | insights.md | → knowledge.md |
| Agent 洞察 | 0.8 | 对话/触发器 | insights.md | → feedback/knowledge.md |
| 执行出错 | 0.7 | 所有产出者 | errors.md | → knowledge.md |
| 3 连败封禁 | 自动 | 心跳蒸馏 | — | → blocked.md (⚡安全通路) |
| 心跳成功策略 | 0.6 | 心跳蒸馏 | lineage.md (日志) | → strategies.md |
| 触发器结果 | 0.5 | 触发器 | insights.md | → knowledge/strategies.md |
| Agent 委托结果 | 0.5 | 委托 | insights.md | → knowledge.md |
| 能力缺口 | 0.3 | 对话 | requests.md | → knowledge.md (低优) |
| 心跳 noop | 0.2 | 心跳蒸馏 | lineage.md (日志) | 可能不提炼 |

---

## 11. 降级策略

### 11.1 核心哲学

**通过架构设计消除失败场景：**

| 已消除的失败场景 | 为什么消除了 |
|----------------|------------|
| JSON parse 失败 | 不用 JSON，全路径 MD→MD |
| SQLite 连接失败 → memory 崩溃 | MD 是 source of truth，DB 是辅助 |
| cursor 状态损坏 | 无状态，没有 cursor |
| inline promotion 重复写 soul | 删除了 inline promotion，只有梦境写 soul |
| 3 条写入路径冲突 | 产出者写 T2，蒸馏器写 T3，职责分离 |

### 11.2 LLM 能力降级

| 环节 | 满能力 (增强) | 地板 (零 LLM) |
|------|-------------|-------------|
| 提取器: T0→T2 | LLM 提取 agent → T2 (隐式偏好+推理洞察) | Pattern-based 正则 → T2 (仅显式信号) |
| 心跳: T2→T3 | LLM 判断什么值得策展 | 全部新条目按 category 机械分配到 T3 |
| 梦境: T3 精简 | LLM 重写 | 程序化: 文本去重 + cap 截断 |
| 梦境: soul 提炼 | LLM 改写第一人称 | 直接复制原文 |
| 搜索: recall | FTS5 + 权重 | 纯 FTS5 |

### 11.3 数据库降级

| DB 状态 | 影响 | 记忆完整? |
|---------|------|----------|
| 正常 | 全功能 | ✅ |
| 不可用 | 无 session 回顾, 无 recall | ✅ MD 完整 |
| 损坏 | 从 MD 重建 FTS5 | ✅ MD 是 truth |

### 11.4 每个循环的失败处理

```
T0 写入 (原始日志):
  写入失败 → log, session 继续, 原始日志丢失但不影响运行

提取 (T0→T2):
  提取失败 → log, T0 完整, 可从 T0 重提取

心跳 (T2→T3):
  读 T2 失败 → 跳过该文件, 其他继续
  写 T3 失败 → log, 本次蒸馏部分失败, 下次重试
  LLM 判断失败 → 全部新条目机械分配 (宁可冗余不可丢失)

梦境 (T3 精简 + soul):
  读 T3 失败 → 跳过该文件
  去重失败 → 保留原样 (宁可冗余)
  Soul 提炼失败 → soul 不变
  FTS5 重建失败 → log, 搜索降级但 MD 完整

恢复 (T0 → 重建上层):
  T2 损坏 → 从 T0 (30天内) 重新 pattern-based 提取
  T3 损坏 → 从 T2 重新蒸馏 (或从 T0 完整重建)
  soul 损坏 → 从 T3 重新 dream 提炼
```

---

## 12. 压缩机制

```
Step 1: 服务端 fallback (不依赖模型)
  → 压缩摘要自动追加到 learnings/insights.md (T2)
  → 等心跳蒸馏处理

Step 2: Agent flush turn (增强)
  → Agent 保存关键发现到 learnings/*.md (T2)

Step 3: Prompt 重建
  → 从 T3 MD 文件重新组装 frozen prompt

保证: 压缩丢失的上下文 → T2 → 心跳蒸馏 → T3 → 回到 prompt
```

---

## 13. 与当前系统的差异

| 维度 | 当前 Hive | v9 方案 |
|------|----------|--------|
| 原始日志 | 无 (只在 DB ChatMessage) | **T0: per-behavior MD 文件, 30d 保留** |
| 蒸馏层数 | 2 层 (事实→记忆) | **4 层: T0→T2→T3→soul** |
| 蒸馏器数量 | 1 (dream) | **3: 提取器 + 心跳 + 梦境** |
| 蒸馏器���责 | dream 做所有事 (提取+合并+提炼+清理) | **各守一层, 单一职责, 不跨层** |
| 心跳 (原心跳) | 自主行动者 (观察/分析/行动/进化) | **T2→T3 策展器** (KAIROS 持续 session) |
| 心跳 执行模型 | 每次全新 invocation (stateless) | **持续 session + tick** (上下文连续) |
| 心跳 频率 | 120min | **45min tick** (空转保护, 增量成本低) |
| 梦境 (原梦境) | 合并+提取+T2消化+清理 | **仅 T3 精简 + soul 提炼 + 清理** |
| 梦境 读 T2 | 是 (learnings ingestion) | **否** — T2→T3 是 心跳 独占 |
| 触发器结果去向 | 写 lineage (evolution 侧路) | 写 T0 → 提取器 → T2 → 心跳 → T3 |
| 委托结果 | 不回流 | 写 T0 → 提取器 → T2 → 心跳 → T3 |
| 产出者能否写 T2/T3 | 可以 (增强路径直写) | **不能** (产出者只写 T0) |
| T3 谁写 | 产出者 + dream 混合 | **只有 心跳 (追加) + 梦境 (精简)** |
| lineage 定位 | 所有自主行为的日志 | **心跳 的策展日志** |
| 恢复能力 | 无 (DB 损坏→丢失) | **T0 30 天内可重建 T2/T3** |
| Source of truth | SQLite | MD 文件 |

---

## 14. 待讨论

### 14.1 频率 ✅ 已解决

| 问题 | 决定 | 理由 |
|------|------|------|
| 心跳 tick 间隔 | **45min** (可配, 下限 30min) | 持续 session 增量成本低; 空转保护避免浪费 |
| 梦境门控 | **4h + (3 sessions OR 2 heartbeat ticks)** | 低活跃 Agent 靠心跳 tick 也能触发梦境 |
| 中循环写 T2 | **保持每次 session-end** | pattern-based 低成本，不降频 |
| 软梦境 | **v6→v7 需重写** | 不再以 facts 计数触发，改为 memory/*.md 总行数 |

### 14.2 KAIROS / Proactive Mode ✅ 已整合到 §6

心跳蒸馏器采用 KAIROS-inspired 持续 session 模式:
- 服务端内存维护 per-agent 对话上下文 (`_heartbeat_contexts`)
- `<tick>` 注入 + 增量 T2 替代全新 invocation
- 日级/梦境/重启时重置 session
- 详见 §6.2

### 14.3 细节

| 问题 | 选项 |
|------|------|
| memory/ 文件数量 | 5 个 vs 8 个 |
| 每个文件 cap | 30 条? 500 chars? |
| soul.md Learned Behaviors | 整体重写 (防膨胀) |

---

## 15. 提示词体系 (Prompt Engineering)

**所有提示词是记忆系统能否正确运转的关键。** 系统提示词决定 Agent 如何与记忆交互；蒸馏器提示词决定 T0→T2→T3→soul 每层蒸馏的质量。

### 15.0 当前问题诊断

| 问题 | 当前 Hive | Claude Code 对标 |
|------|----------|-----------------|
| 系统提示词无结构 | identity 1 行 + soul 全文拼接 | 20 个结构化 section + cache boundary |
| 无 System section | Agent 不知道自己在什么系统里运行 | 详细的工具/权限/hooks/压缩机制说明 |
| 无 Task guidance | 无代码风格/安全/完成度标准 | 明确的 KISS/安全/验证要求 |
| 无 Memory section | 记忆只通过 retriever 注入，Agent 不知道如何使用 | 4 类型定义 + 存取规则 + when/how 示例 |
| 提取器提示词 | 不存在 | `extractMemories/prompts.ts` 完整指令 |
| 心跳提示词过时 | OBSERVE/ANALYZE/ACT/EVOLVE，读 evolution 文件 | 需改为 OBSERVE/CURATE/ACT/LOG，读 T2/T3 |
| 梦境要求 JSON 输出 | `_AUTO_DREAM_SYSTEM_PROMPT` 要求 JSON array | 违反 MD→MD 原则 |

### 15.1 P0: 系统提示词重构

**目标**: 对齐 Claude Code 的 section 结构，让 Agent 理解自己的运行环境和记忆系统。

#### 15.1.1 目标结构 (对齐 Claude Code 20-section 架构)

```
FROZEN PREFIX (cacheable, session-stable):
  ┌─────────────────────────────────────────────────────────────┐
  │ § Identity — 你是谁                                         │
  │   Agent 名称 + 角色描述 + 身份 (soul.md 核心段落)            │
  │                                                             │
  │ § System — 你在什么系统里运行                                │
  │   kernel 多轮循环 / 工具治理 / capability packs / hooks      │
  │   上下文压缩机制 / 冻结快照 / 记忆系统概述                    │
  │                                                             │
  │ § Doing Tasks — 你如何完成任务                               │
  │   代码风格 / 安全防护 / 完成度验证 / 不过度工程               │
  │                                                             │
  │ § Executing Actions with Care — 风险控制                     │
  │   可逆性判断 / 确认高风险操作 / blast radius 评估             │
  │                                                             │
  │ § Using Your Tools — 工具使用偏好                            │
  │   优先用 read_file 不用 cat / 批量并行调用 / pack 按需激活    │
  │                                                             │
  │ § Tone and Style — 输出风格                                  │
  │   简洁 / 中英文切换 / 引用格式                               │
  │                                                             │
  │ § Skills Catalog — 技能目录                                  │
  │   名称 + 简介列表，load_skill 按需加载                       │
  │                                                             │
  │ § Relationships — 同事 / 组织关系                            │
  │   relationships.md / company_profile.md (DB mirror)          │
  └─────────────────────────────────────────────────────────────┘

── CACHE BOUNDARY (PROMPT_CACHE_BOUNDARY) ──

DYNAMIC SUFFIX (per-round, volatile):
  ┌─────────────────────────────────────────────────────────────┐
  │ § Memory — 你的记忆系统 ⭐                                   │
  │   4 层金字塔说明 (T0→T2→T3→soul)                            │
  │   T3 memory/*.md 内容 (frozen snapshot)                     │
  │   focus.md (T1 working memory)                              │
  │   session 摘要 (DB journal)                                  │
  │   使用指导: 何时读/何时存/如何与蒸馏器配合                    │
  │                                                             │
  │ § Active Packs — 当前激活的能力包                            │
  │   (已有, 保持不变)                                           │
  │                                                             │
  │ § Knowledge Retrieval — 外部知识                             │
  │   (已有, 保持不变)                                           │
  │                                                             │
  │ § Environment — 运行环境                                     │
  │   当前时间 / 对话用户 / 渠道来源                              │
  │                                                             │
  │ § Active Triggers — 已配置的触发器                           │
  │   (已有, 保持不变)                                           │
  └─────────────────────────────────────────────────────────────┘
```

#### 15.1.2 关键新增: § Memory section

**对齐 Claude Code `memdir.ts` 的 `loadMemoryPrompt()`**

当前 Hive 的记忆只通过 retriever 注入文本块，Agent 不知道：
- 记忆系统是什么结构
- 如何主动使用 save_memory / recall 工具
- 什么值得记住，什么不值得
- 记忆和 focus.md 的区别

需要新增的 Memory section 内容:

```markdown
## Your Memory System

You have a 4-layer memory pyramid. Higher layers are more refined and permanent.

### Layer Structure
- **T0 Raw Logs**: Complete session records (you don't directly read these)
- **T1 Working Memory**: focus.md — your current task list (volatile)
- **T2 Episodic**: learnings/*.md — recent observations waiting for curation
- **T3 Semantic**: memory/*.md — curated long-term knowledge
- **soul.md**: Your core identity and learned behaviors

### How Memory Flows
- Your conversations produce T0 logs and T2 extractions (automatic)
- The heartbeat curates T2 → T3 every ~45 minutes
- The dream refines T3 and promotes patterns to soul.md every ~4 hours

### Using Memory Tools
- `save_memory(category, content)`: Directly write to T3 (use sparingly — heartbeat handles most curation)
- `recall(query)`: Search T3 via FTS5 for relevant knowledge

### What's Worth Remembering
- User corrections and preferences (highest value)
- Project decisions and constraints
- Strategies that worked or failed
- NOT: code patterns, file paths, debugging steps (these are in the code)

### Current Memory State
{memory_snapshot — injected from T3 files}
```

#### 15.1.3 关键新增: § System section

```markdown
## System

You run inside the Hive agent kernel — a multi-round LLM loop with tool execution.

### Execution Model
- Each conversation is an invocation with frozen context at entry
- Tools go through governance: security zone → capability gate → approval flow
- Capability packs activate on-demand when skills are loaded
- Context compresses at 85% threshold — important info is extracted before compression

### Memory Integration
- Your memory is read-only during a session (frozen snapshot)
- New learnings are automatically extracted after each response
- The heartbeat and dream processes curate your memories in the background
- You don't need to manually manage memory — focus on the task
```

### 15.2 P1: 提取器提示词 (新建)

**对齐 Claude Code `extractMemories/prompts.ts`**

```
文件: 新建 services/extract_agent.py 内的 EXTRACT_PROMPT
触发: RESPONSE_COMPLETE hook (每轮响应后)
输入: 最近 N 条对话消息
输出: learnings/*.md 追加
```

#### 15.2.1 提取器 LLM 指令

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
| User correction/preference | learnings/insights.md | "don't", "always", "I prefer", explicit feedback |
| Agent insight/discovery | learnings/insights.md | "I found", "the reason is", non-obvious learning |
| Execution error | learnings/errors.md | Tool failures, unexpected results |
| Capability gap | learnings/requests.md | "if only", "I wish", missing tool/skill |

## Rules
1. Read existing learnings files first — don't duplicate
2. Only extract from the provided messages — don't grep source files
3. Format: `- [YYYY-MM-DD] description` (one line per extraction)
4. Extract MORE rather than less — the heartbeat will curate quality later
5. Skip ephemeral task details (those belong in focus.md)
6. Convert relative dates to absolute ("yesterday" → "2026-04-04")

## Efficiency
- Turn 1: Read all 3 learnings files (parallel)
- Turn 2: Write extractions to appropriate files
- Maximum 3 turns total
```

### 15.3 P2: 心跳提示词重写 (HEARTBEAT.md)

**从"自主行动者"改为"持续 session 策展器"**

#### 15.3.1 当前 vs 改造

| 阶段 | 当前 HEARTBEAT.md | 改造后 |
|------|------------------|-------|
| Phase 1 | OBSERVE: 读 evolution/scorecard, blocklist, focus | OBSERVE: 读 T2 (learnings) + T3 (memory) + focus |
| Phase 2 | ANALYZE: "最高优先级是什么?" (思考) | **CURATE**: 逐条判断 T2 → T3 (写入) |
| Phase 3 | ACT: 做一个自主动作 | ACT: 可选自主动作 (不变) |
| Phase 4 | EVOLVE: 写 lineage + scorecard | LOG: 写 lineage (策展日志) + T0 + scorecard |

#### 15.3.2 改造后的 HEARTBEAT.md 模板

```markdown
# Heartbeat — Knowledge Curation Protocol

You are in heartbeat mode with a persistent session.
Your primary job: **curate T2 learnings into T3 memory**.
Your secondary job: take one useful autonomous action if possible.

## Context
- This is tick #{tick_number} in your current session
- Your previous curation decisions are in the conversation history above
- You only see NEW T2 entries since last tick (injected below)

## Phase 1: OBSERVE (2-3 tool calls)

Read current state:
1. `read_file` focus.md — current priorities
2. If first tick: `read_file` memory/feedback.md, memory/strategies.md, memory/blocked.md
   If subsequent tick: skip (already in conversation context)

## Phase 2: CURATE (main job, 5-8 tool calls)

For each new T2 entry, decide:
- **Worth keeping?** Is this durable knowledge or noise?
- **Which category?** feedback / knowledge / strategies / blocked / user
- **Already in T3?** Don't duplicate existing entries

Write worthy entries to the appropriate T3 file:
- User corrections/preferences → memory/feedback.md
- Project/domain knowledge → memory/knowledge.md
- Effective strategies → memory/strategies.md
- Failed approaches → memory/blocked.md
- User profile info → memory/user.md

**Rules:**
- Append, don't rewrite (dedup is the dream's job)
- Format: `- [YYYY-MM-DD] description`
- Skip if T3 already has essentially the same content
- When in doubt, keep it (false negative worse than false positive)

## Phase 3: ACT (optional, 5-8 tool calls)

If T2 contains actionable items:
- Fix an error from learnings/errors.md
- Create/improve a skill
- Research a capability gap from learnings/requests.md
- Post to plaza or message a colleague

If nothing actionable: skip to Phase 4.

## Phase 4: LOG (2-3 tool calls)

1. Append to evolution/lineage.md:
   ```
   ### CUR-{YYYY-MM-DD-HH:MM}
   - Curated: {N entries from T2 → T3, categories}
   - Skipped: {N entries, reasons}
   - Action: {what or "skip"}
   - Score: {0-10}
   ```
2. Update evolution/scorecard.md counters
3. T0 log is written automatically by the system
```

#### 15.3.3 持续 Session 特有指令

```markdown
## Persistent Session Notes

You are running in a persistent session across ticks.
- Your previous tick's reasoning is in the conversation above
- You DON'T need to re-read files you read in previous ticks
- You CAN reference patterns: "This error appeared in tick #2 as well"
- On session reset (after dream or daily), you start fresh — read everything again
```

### 15.4 P3: 梦境提示词重写

**从 Python 内嵌 JSON 要求 → 结构化 MD 模板，对齐 MD→MD 原则**

#### 15.4.1 当前问题

```python
# 当前: auto_dream.py 内嵌字符串
_AUTO_DREAM_SYSTEM_PROMPT = "...Return only a JSON array..."
```

问题:
1. 要求 JSON 输出 → 弱模型 JSON 格式不稳定 → 失败
2. 没有 4 阶段结构
3. 读 facts (SQLite) 不是 MD 文件

#### 15.4.2 改造后的 DREAM.md 模板

```markdown
# Dream — Memory Consolidation Protocol

You are consolidating {agent_name}'s long-term memory.
Your job: refine T3 quality + promote patterns to soul.

## Phase 1: ORIENT (3-4 tool calls)

Read T3 current state:
1. `read_file` memory/INDEX.md — overview
2. `read_file` memory/feedback.md, knowledge.md, strategies.md, blocked.md, user.md
3. `read_file` evolution/lineage.md — recent curation history

## Phase 2: CONSOLIDATE (5-10 tool calls)

For each T3 file:
1. **Dedup**: Remove entries that say essentially the same thing (keep the more specific one)
2. **Merge**: Combine related entries into single comprehensive statements
3. **Prune**: Remove entries contradicted by newer ones
4. **Cap**: Keep max {cap} entries per file. If over, remove least important

Write the refined file back:
- `read_file` → edit in your response → `write_file` (full content)
- Keep format: `- [YYYY-MM-DD] description`

**What NOT to keep:**
- Ephemeral task details (belong in focus.md)
- Code patterns derivable from workspace
- Debugging solutions (fix is in the code)
- Exact tool sequences (only outcomes matter)

## Phase 3: PROMOTE to soul.md (2-3 tool calls)

Scan feedback.md for patterns appearing 3+ times:
- Promote to soul.md `## Learned Behaviors` section
- Rewrite as first-person trait: "I always..." / "I never..."
- **Replace** the entire Learned Behaviors section (don't append)
- Max 20 entries

## Phase 4: INDEX + CLEANUP (3-4 tool calls)

1. Update memory/INDEX.md with current file stats
2. Truncate learnings/*.md to 10 entries each (remove oldest)
3. Delete logs/ directories older than 30 days
4. Archive lineage.md entries older than 200

**All output is MD files. No JSON. No prose outside of files.**
```

#### 15.4.3 降级路径

```
LLM 可用:
  执行上述 4 阶段协议

LLM 不可用 (程序化降级):
  Phase 2: 文本相似度 >70% 去重 + cap 截断 (SequenceMatcher)
  Phase 3: 直接复制 feedback.md 高频条目原文到 soul.md
  Phase 4: 同上 (纯文件操作)
```

### 15.5 提示词文件位置

| 提示词 | 当前位置 | 改造后位置 |
|--------|---------|----------|
| 系统提示词 | `agent_context.py` (散落) | `runtime/prompt_builder.py` (结构化 section 组装) |
| Memory section | 不存在 | `runtime/prompt_sections/memory.py` 新建 |
| System section | 不存在 | `runtime/prompt_sections/system.py` 新建 |
| 提取器 | 不存在 | `services/extract_agent.py` EXTRACT_PROMPT |
| 心跳 | `templates/HEARTBEAT.md` | `templates/HEARTBEAT.md` 重写 |
| 梦境 | `auto_dream.py` 内嵌字符串 | `templates/DREAM.md` 新建 |

### 15.6 实现顺序

```
P0 系统提示词:
  Step 1: 创建 prompt_sections/ 目录，拆分 section 模块
  Step 2: 新建 § System section
  Step 3: 新建 § Memory section (对齐 Claude Code memdir.ts)
  Step 4: 重构 build_frozen_prompt_prefix 为 section 组装

P1 提取器:
  Step 5: 新建 extract_agent.py + EXTRACT_PROMPT
  Step 6: 注册到 RESPONSE_COMPLETE hook handler

P2 心跳:
  Step 7: 重写 HEARTBEAT.md (OBSERVE/CURATE/ACT/LOG)
  Step 8: 新增持续 session 指令块

P3 梦境:
  Step 9: 新建 templates/DREAM.md
  Step 10: auto_dream.py 从 Python 字符串改为读 DREAM.md 模板
  Step 11: 删除 JSON 输出要求，改为 MD→MD
```

---

## 16. 上下文压缩体系（对齐 Claude Code）

**压缩质量直接决定长对话中的记忆保全。** 当前 Hive 有基础压缩能力但与 Claude Code 有 5 个关键差距。

### 16.1 Claude Code 压缩体系（7 层机制）

```
Layer 1: 工具结果驱逐 — 单结果 >50K chars → 持久化到文件, 保留 preview
Layer 2: 轮次聚合预算 — 单轮总结果 >200K chars → 强制驱逐最大块
Layer 3: 时间微压缩 — 空闲 >60min → 清除旧工具结果, 保留最近 5 个
Layer 4: 缓存微压缩 — API cache_edits 原生删除 (不破坏缓存前缀)
Layer 5: Session Memory 压缩 — 无 LLM 裁剪 (保留最近 N 条有文本的消息)
Layer 6: 自动压缩 — ~93% 阈值 → LLM 摘要 (9 section 结构化总结)
Layer 7: PTL 重试 — 3 次重试, 按 API round 分组 → 丢弃最老的 round group
```

**Claude Code 压缩提示词 (9-section 结构化总结):**

```
<analysis>
[起草草稿 — 压缩后剥离, 不进入上下文]
</analysis>

<summary>
1. Primary Request and Intent — 核心目标和当前状态
2. Key Technical Concepts — 技术决策和架构要点
3. Files and Code Sections — 文件路径 + 行号 + 关键代码片段
4. Errors and fixes — 遇到的错误和修复方案
5. Problem Solving — 解决问题的思路和尝试过的方案
6. All user messages — 所有用户消息摘要 (理解变化的意图)
7. Pending Tasks — 未完成的工作项
8. Current Work — 当前正在做的事
9. Optional Next Step — 下一步建议
</summary>
```

**关键阈值:**
| 参数 | Claude Code | 说明 |
|------|-----------|------|
| 有效窗口 | context_window - 20K (summary 预留) | 200K → 180K effective |
| 自动压缩 | effective - 13K buffer = ~167K tokens | ~92.8% |
| 单工具驱逐 | 50K chars | 超过存文件 |
| 轮次总预算 | 200K chars | 防止 N 个并行工具撑爆 |
| 时间微压缩 | 60min 空闲 + 保留最近 5 | 适配长空闲 |
| PTL 重试 | 3 次, 每次丢弃 20% oldest rounds | 按 API round 原子分组 |
| 摘要输出预留 | 20K tokens | max_output_tokens 上限 |

### 16.2 Hive 当前状态

```
Layer 1: 工具结果驱逐 ✅ — 50K/4K preview (对齐)
Layer 2: 轮次聚合预算 ✅ — 200K/轮 (对齐)
Layer 3: 轮次微压缩 ⚠️ — 20 轮龄清除 (Claude Code 是时间制, 更合理)
Layer 4: 缓存微压缩 ❌ — 无 API cache_edits
Layer 5: Session Memory 压缩 ❌ — 无无 LLM 裁剪路径
Layer 6: 自动压缩 ⚠️ — 85% 阈值, 有 LLM 摘要但差异大
Layer 7: PTL 重试 ⚠️ — 2 次, 50% 激进压缩 (不是 round-group 丢弃)
```

**Hive 压缩提示词 (10-section, `conversation_summarizer.py`):**

```
<analysis>[起草草稿]</analysis>

<summary>
1. Task Ledger — 正在做什么
2. Decision Ledger — 做了什么决策
3. Artifact Ledger — 文件/URL/ID
4. Code Snapshot — 关键代码片段
5. Tool Ledger — 工具调用和结果
6. User Messages — 用户消息
7. Preference Ledger — 用户偏好
8. Error Ledger — 错误和修复
9. Pending Ledger — 未完成项
10. Narrative Snapshot — 1-2 行当前状态
</summary>
```

### 16.3 差距分析 + 改造方案

| # | 差距 | 当前 Hive | Claude Code | 改造方案 |
|---|------|----------|------------|---------|
| G1 | **微压缩策略** | 轮次制 (20 轮) | 时间制 (60min 空闲) | 改为时间制: 空闲 >60min → 清除旧工具结果, 保留最近 5 |
| G2 | **有效窗口计算** | 直接用 context_window | context_window - 20K summary 预留 | 减去摘要输出预留 (20K tokens) |
| G3 | **PTL 重试算法** | 50% 激进压缩 + 2 次 | round-group 丢弃 + 3 次 | 实现 API round 分组 → 丢弃最老 group; 增至 3 次 |
| G4 | **无 LLM 快速裁剪** | 无 | Session Memory compact | 新增: 当 LLM 不可用或延迟高时, 直接裁剪旧消息 (保留最近 N 条) |
| G5 | **压缩前 hooks** | 已定义, 未接入 | PreCompact/PostCompact | 接入 (已在 §16 Hooks 计划中) |

### 16.4 压缩提示词对齐

**当前 Hive 的 10-section 摘要结构本身已经不错**，但需要以下调整：

| 调整 | 原因 |
|------|------|
| **加入 "Problem Solving" section** | Claude Code 有, 记录尝试过的方案和思路, 避免压缩后重复尝试 |
| **"All user messages" 更强调** | Claude Code 专门说 "summarized — critical for understanding changing intent" |
| **加入 transcript 路径提示** | 压缩后告知 Agent 完整记录在哪 (T0 日志) |
| **加入 T2 提取确认** | 压缩摘要中确认: "关键发现已提取到 learnings/" |

**改造后的压缩提示词 (11-section):**

```markdown
CRITICAL: Respond with TEXT ONLY. Do NOT call any tools.

Session summaries preserve working state so the next turn can continue safely.
Stable preferences and lessons are automatically extracted to the memory system (T2 learnings).

First wrap your analysis in <analysis> tags (will be stripped):
1. Chronologically analyze each message
2. Note ALL file paths, code snippets, function signatures
3. Pay special attention to user corrections
4. Identify errors and resolutions

Then provide your summary in <summary> tags using EXACTLY this format:

1. **Primary Request and Intent**: What is the core goal? Current status?
2. **Key Technical Decisions**: Architecture choices, constraints, tradeoffs
3. **Files and Code Sections**: file_path:line_number + key snippets
4. **Problem Solving**: Approaches tried, what worked, what didn't
5. **Errors and Fixes**: Errors encountered + root causes + resolutions
6. **All User Messages**: ALL non-trivial user messages summarized (critical for intent tracking)
7. **User Preferences**: Corrections, stated preferences, feedback (highest priority to preserve)
8. **Tool Outcomes**: Key tool calls and their results
9. **Pending Tasks**: Incomplete items + where work left off (include quotes)
10. **Current Work**: What was actively being done when compression triggered
11. **Recovery Context**: T0 raw log available at logs/{date}/ for full detail

Be thorough in preserving technical details — code snippets and file paths are more valuable than prose.
Respond in the same language as the conversation.
```

### 16.5 Post-Compact 恢复机制

**Hive 的 post-compact 恢复机制 (60K budget) 比 Claude Code 更强。** Claude Code 依赖摘要的完整性; Hive 额外注入 soul + focus + 最近读过的文件 + 最近工具结果。**保持这个优势。**

但需要调整恢复优先级以配合 4 层金字塔:

```
恢复注入 (压缩后, 按优先级):
  P0: 压缩摘要本身 (~2500 tokens)
  P0: soul.md 核心段落 (identity)
  P0: focus.md (T1 working memory)
  P0: memory/feedback.md + blocked.md (T3 高优)
  P1: memory/knowledge.md + strategies.md (T3)
  P2: 最近读过的文件 (up to 3)
  P2: 最近工具结果 (last 5)
  P3: active skills / active packs
```

### 16.6 改造代码位置

| 改造 | 文件 | 改动 |
|------|------|------|
| G1 时间微压缩 | `kernel/engine.py` L3 microcompact | 从轮次制改为时间制 |
| G2 有效窗口 | `memory_service.py` context_limit | 减去 20K summary 预留 |
| G3 PTL round-group | `kernel/engine.py` PTL retry | 实现 round 分组 + 3 次重试 |
| G4 无 LLM 快速裁剪 | `memory_service.py` | 新增 fallback: 裁剪旧消息 |
| 压缩提示词 | `conversation_summarizer.py` | 从 10-section 改为 11-section |
| 恢复优先级 | `memory_service.py` _build_restoration_context | 对齐 4 层金字塔优先级 |

---

## 17. Hooks 系统（前置条件）

**没有 hooks 接入，记忆系统无法工作。** 这是实现的第一步。

### 15.1 Session 边界问题

**Hive 是服务端，不像 Claude Code CLI 有明确的"退出"信号。**

| | Claude Code CLI | Hive 服务端 |
|--|----------------|------------|
| Session 结束 | 明确 — Ctrl+C、/exit、关终端 | **不明确** — 用户可能直接关浏览器、切另一个对话、放着不管 |
| 退出信号 | 进程退出 = session end | **无退出信号** — 服务端持续运行 |
| 提取触发 | 每轮响应后 (Stop hook) | 不能只靠 session end — 可能永远不触发 |

**如果等 SESSION_END 才提取，用户永远不点"结束"→ 提取永远不触发 → 记忆永远丢失。**

Claude Code 的实际做法：**每轮响应后 fire-and-forget 触发 `extractMemories`**，SESSION_END 只做 drain。

### 15.2 解决方案：三层保障

```
Layer 1: RESPONSE_COMPLETE — 每轮响应后提取 (主路径, 对齐 Claude Code Stop hook)
  Agent 回复完成 → 后台 fire-and-forget 触发提取 LLM agent
  不依赖 session 是否"结束"
  可配节流: 每 N 轮提取一次 (Claude Code 也有 turnsSinceLastExtraction)

Layer 2: SESSION_IDLE — 空闲超时提取 (兜底)
  N 分钟无消息 → 视为 session 边界 → 提取 + 写 T0 完整日志
  对齐当前 _DREAM_IDLE_SECONDS=180s (websocket.py)
  覆盖: 用户放着不管、切到其他页面

Layer 3: SESSION_CLOSE — 明确关闭信号 (清理)
  WebSocket 断开 / 用户创建新对话 / 触发器/委托 invoke 返回
  drain 所有 pending 提取
  写 T0 完整日志 (如果 Layer 1/2 还没写过)
```

### 15.3 完整 Hooks 列表 (16 个)

**hooks.py 需要从 10 个事件重构为 16 个:**

```python
class HookEvent(StrEnum):
    # ── 工具生命周期 (已有, 已接入) ──
    PRE_TOOL_USE = "pre_tool_use"
    POST_TOOL_USE = "post_tool_use"
    POST_TOOL_FAILURE = "post_tool_failure"

    # ── Session 生命周期 (重构: 拆分旧 SESSION_END 为 3 个) ──
    SESSION_START = "session_start"          # invoke 开始, 注入 frozen prompt
    RESPONSE_COMPLETE = "response_complete"  # ⭐ 每轮响应后, 主提取触发点
    SESSION_IDLE = "session_idle"            # ⭐ 空闲超时, 兜底提取
    SESSION_CLOSE = "session_close"          # WebSocket 断开/新对话, drain

    # ── 上下文压缩 (已有, 未接入) ──
    PRE_COMPACTION = "pre_compaction"        # ⭐ 压缩前提取 (保全即将丢失的上下文)
    POST_COMPACTION = "post_compaction"      # 压缩摘要写 T2 兜底

    # ── 委托 (已有, 未接入) ──
    DELEGATION_START = "delegation_start"
    DELEGATION_END = "delegation_end"        # 委托结果 → T0 + 提取

    # ── Hive 独有 (新增) ──
    TRIGGER_END = "trigger_end"              # 触发器完成 → T0 + 提取
    HEARTBEAT_TICK_END = "heartbeat_tick_end"  # 心跳 tick 完成 → T0
    DREAM_END = "dream_end"                  # 梦境完成 → T0 + 重置心跳
    MEMORY_EXTRACTED = "memory_extracted"     # 提取完成通知 (已有, 未接入)
```

### 15.4 各 Hook 详细定义

#### ⭐ RESPONSE_COMPLETE — 提取器主触发点 (对齐 Claude Code Stop hook)

```python
# 接入位置: kernel/engine.py — 每轮 Agent 响应完成, 无后续工具调用
# 对齐 Claude Code: stopHooks.ts → extractMemories (fire-and-forget)

await emit_hook(
    HookEvent.RESPONSE_COMPLETE,
    agent_id=request.agent_id,
    session_id=session_id,
    metadata={
        "messages": messages,          # 当前对话消息 (还在内存)
        "last_response": result.content,
        "source": session_context.source,
        "turn_count": turn_count,      # 用于节流
    },
)

# Handler (fire-and-forget):
#   1. 节流检查: turn_count % N == 0? (可配, 默认每轮)
#   2. LLM 提取 agent → T2 learnings/*.md
#   3. 不阻塞 — Agent 响应已发送给用户
```

#### ⭐ SESSION_IDLE — 空闲超时兜底

```python
# 接入位置: api/websocket.py — 空闲超时检测 (当前 _DREAM_IDLE_SECONDS)
# 触发: N 分钟内无新消息

await emit_hook(
    HookEvent.SESSION_IDLE,
    agent_id=agent_id,
    session_id=session_id,
    metadata={
        "messages": conversation,      # 完整对话历史
        "idle_seconds": elapsed,
        "source": "websocket",
    },
)

# Handler:
#   1. 写 T0 完整日志 (如果 RESPONSE_COMPLETE 还没写过)
#   2. drain pending 提取
#   3. 记录 session 元数据到 DB (session journal)
```

#### SESSION_CLOSE — 明确关闭

```python
# 接入位置: api/websocket.py — WebSocket 断开
#           api/websocket.py — 用户创建新对话
#           runtime/invoker.py — 触发器/委托 invoke 返回

await emit_hook(
    HookEvent.SESSION_CLOSE,
    agent_id=agent_id,
    session_id=session_id,
    metadata={
        "reason": "ws_disconnect" | "new_session" | "invoke_return",
        "messages": messages,
    },
)

# Handler:
#   1. drain 所有 pending 提取
#   2. 写 T0 (如果还没写)
#   3. auto_dream gate check (record_session_end)
```

#### PRE_COMPACTION — 压缩前保全

```python
# 接入位置: kernel/engine.py — _maybe_compact() 压缩前
# 对齐 Claude Code: PreCompact hook

await emit_hook(
    HookEvent.PRE_COMPACTION,
    agent_id=request.agent_id,
    metadata={
        "messages_to_compress": old_messages,
        "trigger": "auto",
    },
)

# Handler: 提取器读 old_messages → T2 (保全即将被压缩摘要替代的细节)
```

#### POST_COMPACTION — 压缩摘要兜底

```python
# 接入位置: kernel/engine.py — 压缩完成后

await emit_hook(
    HookEvent.POST_COMPACTION,
    agent_id=request.agent_id,
    metadata={"summary": compact_summary, "trigger": "auto"},
)

# Handler: 摘要追加到 learnings/insights.md (T2)
```

#### 其余 hooks

| Hook | 接入位置 | Handler 动作 |
|------|---------|-------------|
| SESSION_START | `invoker.py` invoke 开始 | 确认 frozen prompt 组装 |
| DELEGATION_END | `orchestrator.py` 委托返回 | T0 + 提取 + session gate |
| TRIGGER_END | `trigger_daemon.py` 触发器完成 | T0 + 提取 + session gate |
| HEARTBEAT_TICK_END | `heartbeat.py` tick 完成 | T0 heartbeat log + session gate |
| DREAM_END | `auto_dream.py` 梦境完成 | T0 dream log + 重置心跳 session + FTS5 |
| MEMORY_EXTRACTED | `extract_agent.py` 提取完成 | 通知 (可选用于 debug/监控) |

### 15.5 与 Claude Code 的对齐

| Claude Code Hook | Hive 对应 | 说明 |
|-----------------|----------|------|
| **Stop** (每轮响应后) | **RESPONSE_COMPLETE** | ⭐ 提取主触发点, 直接对齐 |
| PreCompact | PRE_COMPACTION | 直接对齐 |
| PostCompact | POST_COMPACTION | 直接对齐 |
| SessionStart | SESSION_START | 直接对齐 |
| SessionEnd (CLI 退出) | SESSION_IDLE + SESSION_CLOSE | 服务端拆为超时+断开两个信号 |
| PreToolUse | PRE_TOOL_USE | ✅ 已对齐 |
| PostToolUse | POST_TOOL_USE | ✅ 已对齐 |
| PostToolUseFailure | POST_TOOL_FAILURE | ✅ 已对齐 |
| SubagentStop | DELEGATION_END | 直接对齐 |
| extractMemories | 提取器 (RESPONSE_COMPLETE handler) | 机制对齐, fire-and-forget |
| autoDream | 梦境 (DREAM_END) | ✅ 已有 (需重构) |
| 其余 14 个 CLI hooks | 不需要 | CLI 特有 (权限/worktree/通知/file watch 等) |

**核心差异**: Claude Code `SESSION_END` = 进程退出。Hive 没有"退出"，拆为 `SESSION_IDLE` (超时) + `SESSION_CLOSE` (明确信号)，提取主路径移到 `RESPONSE_COMPLETE` (每轮)。

### 15.6 实现顺序

```
Phase 0 — MVP (提取器开始工作):
  Step 1: hooks.py 重构 (10→16 events)
  Step 2: RESPONSE_COMPLETE → engine.py (提取主触发)
  Step 3: 实现提取器 LLM agent (extract_agent.py)
  Step 4: PRE_COMPACTION → engine.py (压缩前保全)

Phase 1 — Session 边界完善:
  Step 5: SESSION_IDLE → websocket.py (空闲超时)
  Step 6: SESSION_CLOSE → websocket.py (断开/新对话)
  Step 7: POST_COMPACTION → engine.py (摘要兜底)
  Step 8: SESSION_START → invoker.py (frozen prompt)

Phase 2 — 全链路接入:
  Step 9: DELEGATION_END → orchestrator.py
  Step 10: TRIGGER_END → trigger_daemon.py
  Step 11: HEARTBEAT_TICK_END → heartbeat.py
  Step 12: DREAM_END → auto_dream.py
  Step 13: hooks_setup.py 统一注册所有 handlers
```

**Phase 0 完成后记忆系统即可运转** — 每轮响应触发提取 + 压缩前保全。

---

## 18. 关键代码位置

| 功能 | 文件 | 需要改动 |
|------|------|---------|
| **Hooks 系统重构 (前置, 详见 §15)** | | |
| hooks.py 重构 (10→16 events) | `runtime/hooks.py` | ✅ 重构 |
| RESPONSE_COMPLETE emit | `kernel/engine.py` 每轮响应后 | ✅ 接入 ⭐ (提取主触发) |
| PRE_COMPACTION emit | `kernel/engine.py` _maybe_compact 前 | ✅ 接入 ⭐ |
| SESSION_IDLE emit | `api/websocket.py` 空闲超时 | ✅ 接入 |
| SESSION_CLOSE emit | `api/websocket.py` 断开/新对话 | ✅ 接入 |
| POST_COMPACTION emit | `kernel/engine.py` 压缩完成后 | ✅ 接入 |
| SESSION_START emit | `runtime/invoker.py` invoke 开始 | ✅ 接入 |
| DELEGATION_END emit | `agents/orchestrator.py` 委托返回后 | ✅ 接入 |
| TRIGGER_END (新增) | `trigger_daemon.py` 触发器完成后 | ✅ 新增 |
| HEARTBEAT_TICK_END (新增) | `heartbeat.py` tick 完成后 | ✅ 新增 |
| DREAM_END (新增) | `auto_dream.py` 梦境完成后 | ✅ 新增 |
| Hook handler 统一注册 | 新增 `hooks_setup.py` | ✅ 新增 |
| **提取器** | | |
| 提取器 LLM agent | 新增 `services/extract_agent.py` | ✅ 新增 |
| 提取器 handler (RESPONSE_COMPLETE) | `hooks_setup.py` 注册 | ✅ 新增 |
| 提取器 pattern 降级 | `services/extract_agent.py` fallback | ✅ 新增 |
| T0 日志写入 (所有行为) | `services/t0_logger.py` 新增 | ✅ 新增 |
| T0 清理 (>30d) | `auto_dream.py` 梦境清理步骤 | ✅ 新增 |
| **心跳 (策展器)** | | |
| 心跳 4 阶段协议 | `templates/HEARTBEAT.md` | ✅ 重写 |
| 心跳持续 session + tick | `heartbeat.py` `_execute_heartbeat` | ✅ 重构 (KAIROS) |
| 心跳上下文管理 | `heartbeat.py` `_heartbeat_contexts` | ✅ 新增 |
| 心跳增量 T2 读取 | `heartbeat.py` `_read_incremental_t2` | ✅ 新增 |
| 心跳空转保护 | `heartbeat.py` `_heartbeat_tick` | ✅ 改造 |
| **梦境 (归档器)** | | |
| 梦境 T3 精简 + soul (不读 T2) | `auto_dream.py` | ✅ 重构 |
| **基础设施** | | |
| Workspace 初始化 (加 logs/) | `tools/workspace.py` | ✅ 调整 |
| 上下文组装 | `agent_context.py` | 调整 |
| Prompt 构建 | `runtime/prompt_builder.py` | 调整 |

---

## 19. 置信度

| 组件 | 置信度 |
|------|--------|
| **Hooks 16 events (含 RESPONSE_COMPLETE 三层保障)** | **96%** |
| **三蒸馏器单一职责 (各守一层, 不跨层)** | **96%** |
| **4 层蒸馏链: T0→T2→T3→soul** | **95%** |
| T0 原始日志层 (per-behavior MD, 30d 保留) | **95%** |
| 提取器: T0→T2 (LLM agent, 对齐 Claude Code extractMemories) | **95%** |
| 心跳: T2→T3 (LLM, KAIROS 持续 session, ~45min) | **93%** |
| 梦境: T3→soul (programmatic+LLM, ~4h, MD→MD) | **94%** |
| **梦境 不读 T2 (心跳 独占 T2→T3)** | **95%** |
| 梦境 门控: 4h + (3s OR 2 heartbeat ticks) | **90%** |
| 产出者只写 T0, 不直接写 T2/T3 | **94%** |
| MD = Source of Truth | **95%** |
| 降级: 架构消除失败场景 + T0 恢复源 | **96%** |
| **P0 系统提示词 section 化 (对齐 Claude Code)** | **94%** |
| **P1 提取器提示词 (对齐 extractMemories)** | **95%** |
| **P2 心跳提示词 (OBSERVE/CURATE/ACT/LOG + 持续 session)** | **93%** |
| **P3 梦境提示词 (MD→MD, 4 阶段, 无 JSON)** | **94%** |
| **压缩体系 5 差距对齐 (时间微压缩/有效窗口/PTL round-group)** | **93%** |
| **压缩提示词 11-section (对齐 Claude Code + T2 确认)** | **94%** |
| **整体方案** | **~95%** |
