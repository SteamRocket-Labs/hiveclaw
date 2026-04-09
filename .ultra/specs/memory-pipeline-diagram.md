# Hive 记忆系统管线图 — 完整版

> 每一种 Agent 行为 → 产出什么数据 → 进入哪个文件 → 蒸馏到哪里
> 2026-04-05 v1

---

## 1. 全景管线图

```
═══════════════════════════════════════════════════════════════════════════════
                          AGENT 行为层
═══════════════════════════════════════════════════════════════════════════════

  ┌─────────────────────────────────────────────────────────────────────────┐
  │                                                                         │
  │  A. 对话行为        B. 自主行为         C. 工具行为        D. 系统行为     │
  │  ───────────       ───────────        ───────────       ───────────     │
  │  用户对话           心跳               文件读写           上下文压缩       │
  │  Agent 回复         触发器执行          搜索/爬取          工具结果驱逐     │
  │  用户反馈           自动做梦            发邮件/飞书         prompt 缓存     │
  │  Agent→Agent 委托   广场发帖            技能加载/创建       workspace 同步  │
  │                    HR 创建员工          记忆读写                          │
  │                                        触发器管理                        │
  │                                        MCP 工具调用                      │
  │                                                                         │
  └──────────────────────────────┬──────────────────────────────────────────┘
                                 │
                                 ▼
═══════════════════════════════════════════════════════════════════════════════
                          数据产出 → 归集层
═══════════════════════════════════════════════════════════════════════════════
```

---

## 2. 行为 → 数据 → 归集 详细映射

### A. 对话行为（用户交互）

```
┌──────────────────────────────────────────────────────────────────────┐
│ A1. 用户发送消息                                                      │
│     产出: ChatMessage (DB)                                           │
│     归集: chat_messages 表 (role=user)                               │
│     蒸馏: session-end → LLM 摘要 → ChatSession.summary (DB)          │
│           session-end → LLM 提取 facts → SQLite                     │
│           → auto-dream → memory.md (T3)                             │
│                                                                      │
│ A2. Agent 回复                                                       │
│     产出: ChatMessage (DB) + streaming chunks                        │
│     归集: chat_messages 表 (role=assistant)                           │
│     蒸馏: 同 A1 (合并进 session summary)                              │
│                                                                      │
│ A3. 用户显式纠正 ("不要这样做"/"应该用X")                               │
│     权重: ★★★★★ (1.0)                                               │
│     产出: ChatMessage (DB)                                           │
│     归集: session-end → SQLite fact (importance=0.9)                  │
│     蒸馏: → memory.md (T3, 高优先留存)                               │
│           → 可能升入 soul.md (T3, 3+次后)                             │
│                                                                      │
│ A4. 用户对话中的隐式洞察                                               │
│     权重: ★★★★☆ (0.8)                                               │
│     产出: ChatMessage (DB)                                           │
│     归集: session-end → SQLite fact (importance=0.7)                  │
│     蒸馏: → memory.md (T3)                                          │
│                                                                      │
│ A5. Agent→Agent 委托                                                  │
│     权重: ★★★☆☆ (0.5)                                               │
│     产出: 新 ChatSession (source=agent) + RuntimeTask (DB)            │
│     归集: worker 的 session-end 处理                                  │
│     ⚠️ 遗漏: 委托结果不回流到 coordinator 的记忆                       │
│                                                                      │
│ A6. 会话摘要生成                                                      │
│     产出: ChatSession.summary (DB 字段)                              │
│     归集: chat_sessions 表                                           │
│     注入: 最近 3 条 → Dynamic suffix (T2)                             │
│     蒸馏: auto-dream 读取 → 合并进 SQLite facts → memory.md          │
└──────────────────────────────────────────────────────────────────────┘
```

### B. 自主行为（Agent 自发）

```
┌──────────────────────────────────────────────────────────────────────┐
│ B1. 心跳执行 (成功, score ≥ 7)                                        │
│     权重: ★★★☆☆ (0.6)                                               │
│     产出: 反思 ChatSession + lineage 条目                             │
│     归集: lineage.md (T2) ← 追加结构化条目                            │
│           scorecard.md (T2) ← 更新计数器                              │
│     蒸馏: auto-dream → SQLite facts → memory.md (T3)                 │
│                                                                      │
│ B2. 心跳执行 (noop, 无动作)                                           │
│     权重: ★☆☆☆☆ (0.2)                                               │
│     产出: lineage 条目                                               │
│     归集: lineage.md (T2) ← 追加 "noop" 条目                         │
│           scorecard.md (T2) ← 更新 total_heartbeats                  │
│     蒸馏: auto-dream → SQLite (importance=0.1, 可能被淘汰)            │
│                                                                      │
│ B3. 心跳执行 (失败, score < 3)                                        │
│     权重: ★★★★☆ (0.7)                                               │
│     产出: lineage 条目                                               │
│     归集: lineage.md (T2) ← 追加 "failure" 条目                      │
│           scorecard.md (T2) ← 更新 failed_attempts                   │
│     蒸馏: auto-dream → SQLite facts (category=strategy) → memory.md   │
│                                                                      │
│ B4. 心跳 3 连败 → 自动封禁                                            │
│     权重: 自动 (绕过权重，直写 T3)                                     │
│     产出: blocklist 条目                                             │
│     归集: blocklist.md (T3) ← 直接追加                               │
│     蒸馏: auto-dream 提炼为 SQLite fact (category=blocked_pattern)    │
│           无需经过 T2                                                 │
│                                                                      │
│ B5. 触发器执行                                                        │
│     权重: ★★★☆☆ (0.5)                                               │
│     产出: 反思 ChatSession + lineage 条目 + trigger 状态更新           │
│     归集: lineage.md (T2) + AgentTrigger 表 (DB, last_fired_at)       │
│     蒸馏: 同 B1                                                      │
│                                                                      │
│ B6. 广场发帖/评论                                                     │
│     产出: plaza_posts / plaza_comments (DB)                          │
│     归集: DB 表 (外部系统)                                            │
│     蒸馏: ❌ 不进入 MD 记忆系统 (外部社交数据)                          │
│                                                                      │
│ B7. HR 创建数字员工                                                   │
│     产出: Agent 记录 + 全套 workspace 文件                            │
│     归集: agents 表 (DB) + 新 agent 的 soul.md/focus.md/etc.          │
│     蒸馏: ❌ 创建行为本身不进入创建者的记忆                              │
│     ⚠️ 可考虑: 创建者记住 "我创建了 X agent"                          │
│                                                                      │
│ B8. 空闲做梦 (idle dream)                                            │
│     产出: 反思 ChatSession                                           │
│     归集: 同心跳 (source_channel=heartbeat)                           │
│     蒸馏: 同 B1-B3                                                   │
└──────────────────────────────────────────────────────────────────────┘
```

### C. 工具行为（Agent 调用工具）

```
┌──────────────────────────────────────────────────────────────────────┐
│ C1. 文件读取 (read_file, list_files, glob_search, grep_search)       │
│     产出: 文件内容 (ephemeral, 在 kernel context 中)                  │
│     归集: ❌ 不持久化 (只在当前 session context)                       │
│     kernel 追踪: session_context.recent_files (最近 3 个)             │
│                                                                      │
│ C2. 文件写入 (write_file, edit_file)                                 │
│     产出: 工作区文件                                                  │
│     归集: 取决于写入目标:                                             │
│       → focus.md → T1 (Working Memory)                               │
│       → learnings/*.md → T2 (Episodic)                               │
│       → soul.md → T3 (Identity) ⚠️ 建议禁止 Agent 直接写              │
│       → memory.md → T3 (Knowledge) ⚠️ 建议禁止 Agent 直接写           │
│       → skills/*.md → 技能文件 (不参与记忆蒸馏)                        │
│       → workspace/*.md → 工作文档 (不参与记忆蒸馏)                     │
│     kernel 追踪: session_context.recent_writes (最近 5 个)            │
│                                                                      │
│ C3. 文件删除 (delete_file)                                           │
│     产出: 文件消失 (metadata loss)                                    │
│     归集: ❌ 删除行为本身不被记录                                      │
│     ⚠️ 遗漏: Agent 删除重要文件时无审计日志                            │
│                                                                      │
│ C4. 代码执行 (execute_code, run_command)                              │
│     产出: stdout/stderr (ephemeral)                                  │
│     归集: kernel context 内，不持久化                                 │
│     异常时: Agent 可能写入 learnings/errors.md (T2)                   │
│                                                                      │
│ C5. 网络搜索 (web_search)                                            │
│     产出: 搜索结果 (ephemeral)                                       │
│     归集: kernel context，可能被驱逐 (>50K)                           │
│     kernel 追踪: session_context.recent_external_refs                │
│     蒸馏: ❌ 搜索结果不进入记忆 (除非 Agent 主动 save_memory)          │
│                                                                      │
│ C6. 网页抓取 (web_fetch, firecrawl_fetch, xcrawl_scrape)              │
│     产出: 页面内容 (ephemeral, 可能被驱逐)                             │
│     归集: 同 C5                                                      │
│     蒸馏: ❌ 同 C5                                                    │
│                                                                      │
│ C7. 文档解析 (read_document — PDF/Word/Excel)                        │
│     产出: 文本内容 (ephemeral)                                       │
│     归集: kernel context                                             │
│     蒸馏: ❌ 不自动进入记忆                                            │
│                                                                      │
│ C8. 发送邮件 (send_email, reply_email)                                │
│     产出: 邮件记录 (external IMAP/SMTP)                               │
│     归集: 外部系统                                                    │
│     蒸馏: ❌ 不进入记忆                                                │
│                                                                      │
│ C9. 飞书操作 (feishu_*: 发消息/读文档/操作表格/日历/任务)               │
│     产出: 飞书记录 (external)                                        │
│     归集: 外部飞书系统                                                │
│     蒸馏: ❌ 不进入记忆                                                │
│                                                                      │
│ C10. 保存记忆 (save_memory)                                          │
│      权重: Agent 自判 (通常 0.5-0.8)                                 │
│      产出: semantic fact                                             │
│      归集: SQLite (semantic_facts.json)                              │
│      蒸馏: → auto-dream → memory.md (T3)                            │
│                                                                      │
│ C11. 搜索记忆 (search_memory)                                        │
│      产出: ❌ 只读 (检索 SQLite + session summaries)                   │
│                                                                      │
│ C12. 技能加载 (load_skill)                                           │
│      产出: 技能内容加载到 context (ephemeral)                         │
│      归集: session_context.active_skills                             │
│      蒸馏: ❌ 不进入记忆                                               │
│                                                                      │
│ C13. 技能创建 (write_file → skills/*.md)                              │
│      产出: 技能文件                                                   │
│      归集: workspace/skills/ 目录                                    │
│      蒸馏: ❌ 技能文件独立于记忆系统                                    │
│      scorecard: skills_created 计数器 +1                             │
│                                                                      │
│ C14. 触发器管理 (set_trigger, update_trigger, cancel_trigger)         │
│      产出: AgentTrigger 记录 (DB)                                    │
│      归集: agent_triggers 表                                         │
│      蒸馏: ❌ 结构数据不进入记忆 (DB 是 source of truth)               │
│                                                                      │
│ C15. MCP 工具调用 (execute_mcp_tool)                                  │
│      产出: 外部 MCP 服务的返回值 (ephemeral)                           │
│      归集: kernel context                                            │
│      蒸馏: ❌ 不自动进入记忆                                            │
│                                                                      │
│ C16. 广场阅读 (plaza_get_new_posts)                                   │
│      产出: ❌ 只读                                                    │
│                                                                      │
│ C17. 图片上传 (upload_image)                                          │
│      产出: CDN URL                                                   │
│      归集: 外部 CDN                                                   │
│      蒸馏: ❌                                                         │
│                                                                      │
│ C18. 异步任务管理 (check_async_task, list_async_tasks)                 │
│      产出: 任务状态查询 (read-only)                                   │
│      归集: ❌                                                         │
└──────────────────────────────────────────────────────────────────────┘
```

### D. 系统行为（非 Agent 主动触发）

```
┌──────────────────────────────────────────────────────────────────────┐
│ D1. 上下文压缩 (kernel mid-loop compaction)                           │
│     产出: 压缩摘要 (ephemeral) + compaction_summary.md (workspace)    │
│     归集: workspace/compaction_summary.md (ephemeral)                │
│     蒸馏: Agent flush turn → learnings/*.md → auto-dream → memory.md │
│     ⚠️ 如果 Agent 未在 flush turn 保存，压缩内容永久丢失               │
│                                                                      │
│ D2. 工具结果驱逐 (>50K chars)                                         │
│     产出: workspace/tool_results/{tool_call_id}.txt                  │
│     归集: workspace 文件 (可被 read_file 重读)                        │
│     蒸馏: ❌ 不进入记忆 (已有文件引用即可)                               │
│                                                                      │
│ D3. Prompt 缓存管理                                                   │
│     产出: 缓存 metadata (memory hash, prefix fingerprint)             │
│     归集: session_context (ephemeral)                                │
│     蒸馏: ❌ 纯性能优化，不产出知识                                     │
│                                                                      │
│ D4. Workspace 同步 (workspace_sync.py)                                │
│     产出:                                                            │
│       → relationships.md (Mirror, 非蒸馏)                            │
│       → company_profile.md (Mirror, 非蒸馏)                          │
│       → org_structure.md (Mirror, 非蒸馏)                            │
│     归集: workspace 文件                                              │
│     蒸馏: ❌ DB 镜像，不参与蒸馏链                                     │
│                                                                      │
│ D5. Auto-dream (自动做梦)                                             │
│     产出: 更新后的 T3 文件                                            │
│       → memory.md (重建)                                             │
│       → soul.md (更新 Learned Behaviors)                             │
│       → blocklist.md (清理过期)                                      │
│     归集: T3 层所有文件                                               │
│     蒸馏: ❌ 这是蒸馏动作本身，不再向上蒸馏                              │
│                                                                      │
│ D6. Session-end 持久化                                                │
│     产出:                                                            │
│       → ChatSession.summary (DB)                                     │
│       → SQLite facts (提取自对话消息)                                  │
│     归集: DB + SQLite                                                │
│     蒸馏: → auto-dream → memory.md (T3)                              │
│                                                                      │
│ D7. 审计日志 (write_audit_log)                                        │
│     产出: activity_logs (DB)                                         │
│     归集: DB 表                                                      │
│     蒸馏: ❌ 运维数据，不进入 Agent 记忆                                │
└──────────────────────────────────────────────────────────────────────┘
```

---

## 3. 蒸馏链路全景

```
═══════════════════════════════════════════════════════════════════════════
                     T1: Working Memory
═══════════════════════════════════════════════════════════════════════════

  focus.md ← Agent write_file (C2)
  │
  │ 不向上蒸馏。Auto-dream 仅清理。
  │ 始终加载进 prompt (Dynamic suffix, P0)
  │
═══════════════════════════════════════════════════════════════════════════
                     T2: Episodic Memory
═══════════════════════════════════════════════════════════════════════════

  learnings/errors.md    ← Agent 工具出错时写入 (C4 异常, 权重 0.7)
  learnings/insights.md  ← Agent 发现更好方式时 (权重 0.8)
  learnings/requests.md  ← Agent 发现能力缺口时 (权重 0.3)
  lineage.md             ← 心跳/触发器结果 (B1-B5, 权重 0.2-0.7)
  scorecard.md           ← 心跳计数器 (B1-B3)
  session summaries      ← session-end 生成 (A6, DB)
  │
  │ 全部通过以下路径蒸馏到 T3:
  │
  │  ┌─ session-end (高权重 ≥0.7):
  │  │    对话消息 → LLM 提取 → SQLite facts ──────────────┐
  │  │                                                      │
  │  └─ auto-dream (全量, 4h+3s 门控):                      │
  │       learnings/*.md → _ingest_learnings() → SQLite ──┐ │
  │       lineage.md → _distill_evolution() → SQLite ────┐│ │
  │       session summaries → 读取合并 ─────────────────┐│││
  │                                                     ││││
  │                                                     ▼▼▼▼
  │                                                   SQLite
  │                                                  (引擎层)
  │                                                     │
  │  蒸馏后清理:                                         │
  │  learnings/*.md → 截断最后 10 条                     │
  │  lineage.md → >200 条归档                            │
  │                                                     │
═══════════════════════════════════════════════════════════════════════════
                     SQLite (内部引擎，非最终产物)
═══════════════════════════════════════════════════════════════════════════
  │
  │  聚类 → 去重 → 淘汰 (importance < 0.3 && age > 30d)
  │  总量 cap: 200 facts
  │  安全门: >70% 保留率
  │
  ├────→ LLM 合成 ────→ memory.md (T3 Knowledge)
  │                      按 category 分节的连贯文本
  │
  ├────→ 高频提炼 ────→ soul.md ## Learned Behaviors (T3 Identity)
  │                      feedback/constraint 3+次 → 人格特质
  │                      整体替换（不追加）
  │
  └────→ 清理 ────────→ blocklist.md 过期条目 >60d 删除 (T3)
  │
═══════════════════════════════════════════════════════════════════════════
                     T3: Semantic Memory
═══════════════════════════════════════════════════════════════════════════

  soul.md       ← auto-dream _promote_to_soul (仅此一条路径)
                   Frozen prefix, 始终加载 (P0)

  memory.md     ← auto-dream _sync_facts_to_memory_md
                   Dynamic suffix, 全文加载 (P1)

  blocklist.md  ← heartbeat 3连败追加 + auto-dream 清理
                   仅心跳模式加载 (P3)

  scorecard.md  ← heartbeat 计数器更新
                   仅心跳模式加载 (P3)

═══════════════════════════════════════════════════════════════════════════
                     Mirror (非蒸馏，DB 同步)
═══════════════════════════════════════════════════════════════════════════

  relationships.md   ← workspace_sync (org 变动时)
                       Frozen prefix (P2)

  company_profile.md ← workspace_sync (企业信息变动时)
                       可选加载

  org_structure.md   ← workspace_sync (部门/成员变动时)
                       可选加载
```

---

## 4. 已发现的遗漏 & 断点

| # | 遗漏 | 行为 | 影响 | 建议 |
|---|------|------|------|------|
| M1 | **委托结果不回流** | Agent A 委托 Agent B 做任务，B 的结果存在 B 的 session 里，A 无法回忆 | A 不记得委托过什么、结果如何 | 委托完成时，将 result summary 写入 A 的 session 消息流，走正常 session-end 提取 |
| M2 | **文件删除无记录** | Agent 删除 workspace 文件时没有审计 | 误删重要文件无法追溯 | delete_file 操作写一条 audit log 到 activity_logs |
| M3 | **外部文档读取遗忘** | Agent 读飞书文档/网页后，内容是 ephemeral | 下次问 Agent "你之前读了什么" 答不上来 | 追踪 recent_external_refs 到 session summary，走正常蒸馏 |
| M4 | **压缩未 flush 则丢失** | 如果 Agent 在 flush turn 没保存关键信息，压缩内容永久丢失 | 关键上下文可能丢失 | 压缩摘要自动作为一条 fact 写入 SQLite (importance=0.5) |
| M5 | **创建员工未记录** | HR Agent 创建新员工后，自己不记得创建了谁 | HR Agent 无法回忆自己的创建历史 | 创建完成时自动 save_memory("创建了 {name} agent", category="project") |
| M6 | **广场交互单向** | Agent 发帖后不记得发过什么 | Agent 重复发帖或忘记已分享的内容 | 低优先级 — Agent 可以重新 plaza_get_new_posts 查看 |
| M7 | **触发器配置不在记忆中** | Agent 设置了触发器但记忆系统里没有 | Agent 忘记自己设了哪些触发器 | 低优先级 — Agent 可以 list_triggers 查看 |
| M8 | **MCP 工具调用结果遗忘** | 调用外部 MCP 服务的返回值是 ephemeral | 下次无法引用之前的 MCP 结果 | 和 C5/C6 相同 — Agent 可以主动 save_memory |

---

## 5. 按权重排序的蒸馏路径表

| 权重 | 行为 | 来源 | T2 归集 | 蒸馏触发 | T3 终点 | 留存 |
|------|------|------|---------|---------|---------|------|
| **1.0** | 用户显式纠正 | A3 | session-end → SQLite | 即时 (session-end) | memory.md → 可升 soul.md | 几乎不淘汰 |
| **0.8** | 用户对话洞察 | A4 | session-end → SQLite | 即时 (session-end) | memory.md | 高留存 |
| **0.8** | Agent 洞察记录 | C2→insights.md | learnings/insights.md | auto-dream | memory.md | 高留存 |
| **0.7** | Agent 执行出错 | C2→errors.md | learnings/errors.md | auto-dream | memory.md | 高留存 |
| **0.7** | 心跳失败 | B3 | lineage.md | auto-dream | memory.md | 高留存 |
| **自动** | 3 连败封禁 | B4 | blocklist.md (直写 T3) | — | blocklist.md | 60d 后过期 |
| **0.6** | 心跳成功 (score≥7) | B1 | lineage.md | auto-dream | memory.md | 中留存 |
| **0.5** | 触发器执行 | B5 | lineage.md | auto-dream | memory.md | 中留存 |
| **0.5** | Agent→Agent 委托 | A5 | session-end → SQLite | auto-dream | memory.md | 中留存 |
| **0.5** | Agent 主动 save_memory | C10 | SQLite (直写) | auto-dream | memory.md | 中留存 |
| **0.3** | 能力缺口记录 | C2→requests.md | learnings/requests.md | auto-dream | memory.md (低优) | 低留存 |
| **0.2** | 心跳 noop | B2 | lineage.md | auto-dream | 可能被淘汰 | 最低 |
| **N/A** | Agent 更新任务 | C2→focus.md | focus.md (T1) | 不蒸馏 | — | volatile |
| **❌** | 外部通信 (邮件/飞书/广场) | C8/C9/B6 | 外部系统 | 不进入 | — | 外部持有 |
| **❌** | 文件读取/搜索 | C1/C5/C6 | ephemeral | 不进入 | — | 会话内 |
| **❌** | 触发器/MCP 配置 | C14/C15 | DB 表 | 不进入 | — | DB 持有 |

---

## 6. 上下文组装索引

```
┌──────────────────────────────────────────────────────────────────┐
│                                                                  │
│  FROZEN PREFIX（invoke 时冻结，会话内不变）                         │
│                                                                  │
│  优先级  来源        层级    加载策略          Token 预算           │
│  ─────  ────        ────   ────────          ──────────          │
│  P0     soul.md     T3     全文              ~3000 chars         │
│  P2     relations   Mirror 全文              ~1000 chars         │
│  -      skills      Static 目录 + 概要       variable            │
│                                                                  │
│  ──── PROMPT CACHE BOUNDARY ────                                │
│                                                                  │
│  DYNAMIC SUFFIX（每次 invoke 重新读取）                            │
│                                                                  │
│  P0     focus.md    T1     全文              ~1000 chars         │
│  P1     memory.md   T3     全文/按 section   ~2000 chars         │
│  P2     summaries   T2     最近 3 条         ~500 chars          │
│                                                                  │
│  心跳模式额外:                                                    │
│  P3     lineage     T2     尾部 10 条        ~800 chars          │
│  P3     blocklist   T3     全文              ~500 chars          │
│  P3     scorecard   T2     全文              ~300 chars          │
│  -      HEARTBEAT   Template 全文            ~2000 chars         │
│                                                                  │
└──────────────────────────────────────────────────────────────────┘

加载优先级含义:
  P0 = 不可裁剪 (身份 + 工作状态)
  P1 = 优先保留 (核心知识)
  P2 = 可裁剪 (budget 紧张时)
  P3 = 按需加载 (仅特定模式)
```

---

## 7. 行为总量统计

| 类别 | 行为数 | 进入记忆系统 | 不进入 | 遗漏 |
|------|--------|------------|--------|------|
| A. 对话行为 | 6 | 5 | 0 | 1 (委托回流) |
| B. 自主行为 | 8 | 6 | 2 (广场/HR创建) | 0 |
| C. 工具行为 | 18 | 3 (save_memory + write_file→L0/L1 + 技能创建) | 15 (外部/ephemeral) | 0 |
| D. 系统行为 | 7 | 3 (session-end + auto-dream + 压缩) | 4 (cache/sync/audit) | 1 (压缩丢失) |
| **总计** | **39** | **17** | **21** | **2 关键遗漏** |

**17 个行为进入记忆蒸馏链, 21 个不进入 (合理 — 外部系统/ephemeral), 2 个关键遗漏待修复。**
