# T0 追加式会话账本重构设计

日期：2026-06-18
范围：仅 T0 层
状态：实现设计草案

下一层契约：T0 -> T2 打包机制单独由 `docs/t0-to-t2-segment-package-redesign-2026-06-18.md` 定义。本文只定义 T0。

## 1. 执行摘要

T0 不是记忆智能。T0 是原始、可回放、可引用的证据地基。

当前 Hive 的 T0 层通过 idle / close hooks 写入事后 Markdown 快照。这不足以支撑 resume、replay、source refs，也不足以支撑后续任何证据校验。

T0 必须重建到 Claude Code transcript 和 Codex rollout 同等级的地基上：

- 追加式 session ledger
- event-first 持久化
- 显式 session identity
- 类型化 event boundary
- 可以直接从 ledger 恢复
- DB / index / summary 都是派生 read model，永远不是真相源

Hive 可以继续保持上层 MD-first 的表达方式。但底层语义必须是 append-only session ledger。Hive 最终形态是：Markdown 容器 + XML event blocks + 机械 sidecar 用于索引和完整性校验。

## 2. 设计法律

这一层遵循项目记忆系统的核心法律：

> LLM 负责判断、提炼、反思、归纳、候选生成；平台负责证据引用、权限、去重、回滚、审计、最终落盘。

对 T0 来说，这句话的含义是：

- 平台写入 raw events。
- 平台可以在 durable storage 前对禁止持久化的敏感材料做 mask 或 block。
- 平台不能总结、分类、打分、晋升或重写语义。
- LLM 智能从 raw evidence 层之后开始。

T0 允许机械化，因为它是记录器，不是裁判。

## 3. 对齐原则

### 3.1 Claude Code Transcript 原则

Claude Code 会把每个 session 存为项目作用域下的 JSONL transcript 文件。它真正值得对齐的是以下原则：

- 在模型 loop 前持久化已接受的用户消息，因此即使进程在 API 返回前被 kill，`/resume` 仍然可用。
- 存储 message UUID 和 parent-chain links，让对话可以被重建。
- 把 compaction 存为 boundary / replacement metadata，而不是破坏性地重写历史。
- 从 transcript resume，而不是从 summary resume。
- sidechain / subagent transcripts 是相关链路，但仍然可以独立重建。

### 3.2 Codex Rollout 原则

Codex 把 session history 存为类型化 rollout JSONL。它真正值得对齐的是以下原则：

- `SessionMeta` 用稳定 metadata 打开 session。
- `RolloutItem` variants 记录 session meta、response items、compacted items、turn context 和 event messages。
- append flush 完成后，DB / index metadata 才能被视为 current。
- resume 会把 rollout items 加载到 `InitialHistory::Resumed`。
- compaction、rollback、interruption 都是 history 里的 event，不是静默的破坏性编辑。

### 3.3 Hive 原则

Hive 不需要复制 JSONL 作为用户可见记忆格式。Hive 要复制的是它们的底层不变量。

目标表述：

> Claude Code transcript 和 Codex rollout 都是 append-only session ledger。Hive T0 也必须成为 append-only session ledger。Hive 的 durable semantic representation 可以继续使用 Markdown / XML，但持久化语义必须达到 transcript / rollout 级别。

## 4. 旧 Hive T0 机制

旧代码路径：

- `backend/app/services/t0_logger.py`
- `backend/app/runtime/hooks_setup.py`
- `backend/app/services/web_chat_runtime.py`
- `backend/app/models/chat_session.py`
- `backend/app/models/audit.py`

旧存储结构：

```text
<AGENT_DATA_DIR>/<agent_id>/logs/YYYY-MM-DD/
  behavior/
    chat-{HHmm}-{id}.md
    trigger-{HHmm}-{id}.md
    delegation-{HHmm}-{id}.md
  system/
    heartbeat-{HHmm}-{id}.md
    dream-{HHmm}-{id}.md
  artifacts/
    <tool-result-artifact>.json
```

旧 chat T0 文件形态：

- YAML frontmatter
- `## Turn N`
- `**User**`
- `**Agent**`
- `**Tools**`
- 大型 tool outputs 可带 artifact references

旧写入时机：

- `SESSION_IDLE` 写一次 incremental T0 snapshot。
- `SESSION_CLOSE` 再写一次 incremental T0 snapshot。
- cursor 是进程本地状态：`_t0_cursors["agent_id:session_id"] -> message index`。
- `RESPONSE_COMPLETE` 目前会另外触发上层 extraction，但这条路径不属于本文 T0 重构范围。

## 5. 当前断点

### 5.1 T0 写入滞后

已接受的用户消息会写入 `ChatMessage`，但 T0 自己要等 idle / close hooks。如果进程在 idle / close 前挂掉，T0 证据可能不存在。

Claude Code 的做法是在用户消息被接受后立即写 transcript。

### 5.2 游标不持久

`_t0_cursors` 在内存里。进程重启会丢失。

这会导致：

- T0 chunks 重复
- hook 失败后 chunks 缺失
- 无法证明哪些内容已经 sealed

### 5.3 文件名不是事件安全的

`chat-{HHmm}-{short_id}.md` 只有分钟级时间和短 id。同一个 session 在同一分钟内多次写入时，可能碰撞或变得歧义。

### 5.4 ChatSession 不是账本切片

`ChatSession` 是产品层对话容器。它可以持续几分钟、几天甚至几个月。可回放的 ledger segment 必须更小、可 seal、可恢复。

因此：

- ChatSession 不应该等于 T0 source packet。
- ChatSession 应该包含多个 T0 source packets。
- T0 source packets 应该可以独立 replay，也可以独立 seal。

### 5.5 恢复逻辑仍偏向 DB / 最新会话

WebSocket 当前接受可选 `session_id`。如果缺失或无效，会 fallback 到该 user + agent 的最新 session。这个 UX fallback 有用，但它不是可靠的 transcript / resume 地基。

恢复必须显式，并且由 ledger 支撑。

### 5.6 压缩不是 T0 的一等边界

T0 当前没有把 compaction、resume、interruption、cancellation、rollback 表达为一等 source event。

这会让后续重建和证据审计变得脆弱。

### 5.7 Summary 旁路不能成为真相源

`ChatSession.summary` 会在 idle 时生成，并被 retrieval paths 使用。它可以继续作为 read model，但不能成为语义真相。

append-only T0 ledger 才是 session reconstruction 的真相源。

## 6. 目标模型

### 6.1 术语

| 术语 | 定义 |
| --- | --- |
| Product ChatSession | 用户可见的对话容器。跨重连和显式 resume 时保持稳定。 |
| Session Ledger | 一个 ChatSession 下的追加式 T0 event ledger。 |
| T0 Source Packet / Segment | Session Ledger 中一个已封存、有边界的切片。这是可回放或可从中恢复的单位。 |
| T0 Event | 一个类型化原始事件，例如 user message、assistant message、tool call、tool result、runtime boundary 等。 |
| Artifact | event body 外部的大型 payload，带 hash 和 reference。 |
| Read Model | 从 ledger 派生出来的 DB rows、indexes、summaries、projections 和 UI views。它不是真相源。 |

### 6.2 关系

```text
ChatSession
  -> Session Ledger
      -> T0 Segment 001
          -> T0 Event...
      -> T0 Segment 002
          -> T0 Event...
      -> T0 Segment 003
          -> T0 Event...
```

### 6.3 Segment 边界

以下条件都可以 seal 一个 T0 segment：

- idle threshold 到达
- context / token threshold 到达
- 用户显式创建新对话
- 显式 resume 产生 resume boundary
- compaction boundary
- runtime task 完成
- cancellation / interruption
- system restart recovery
- 渠道级对话边界，例如飞书 thread / new command

第一条重要规则：

> Idle 只封存一个 segment，不一定关闭 ChatSession。

第二条重要规则：

> Segment boundary 是回放、预算和恢复边界，不是语义完成判断。

T0 可以因为 idle、token pressure、最大事件数、runtime 完成、取消、中断、重启恢复或渠道边界而 seal 一个 segment。但这些原因都不代表用户任务或语义事件已经完成。语义闭合只能由 T2 及以上的 Agent 判断：Summary Agent、Learning Brain、Memory Gate，以及当相邻 segment 可能属于同一个 episode 时介入的 Continuity / Episode Stitcher。

因此，T0 需要记录足够的边界 metadata，让上层可以判断连续性；但 T0 自己不能决定两个 segment 是否应该合并、晋升，或被视为同一条记忆：

```json
{
  "reason": "session_idle",
  "boundary_kind": "physical",
  "semantic_completion": "not_judged",
  "may_continue": true
}
```

如果用户在任务中途暂停，之后又回来继续，T0 可以在同一个 ChatSession 下创建新的 segment。如果外部渠道因为断连或线程规则创建了新的 ChatSession，但语义上仍是在继续同一件事，T0 仍然保持每条 ledger 的真实性；上层连续性判断层通过 source refs 链接这些 segment，不重写、不合并 T0。

## 7. 目标存储形态

目标 durable T0 layout：

```text
<AGENT_DATA_DIR>/<agent_id>/memory/t0/sessions/<chat_session_id>/
  index.json
  segments/
    <segment_id>/
      source.md
```

决策：

- 实现采用 segmented layout。
- 每个 `source.md` 在 sealed 前只允许 append。
- seal 之后，除非有显式审计过的 repair record，否则不可变。
- `index.json` 记录 active segment、next sequence、sealed segments 和 legacy import idempotency。

`index.json` 是机械 sidecar，不是语义记忆真相。超大的 runtime artifacts 仍然由 runtime / workspace artifact 机制单独治理，除非未来增加 T0 artifact adapter。

## 8. 源 Markdown 格式

T0 保持 MD-first，但 source 文件内部所有语义 block 都使用 XML。

示例：

```markdown
# T0 Session Ledger
schema_version: t0.session-ledger.v1
agent_id: <agent_id>
session_id: <session_id>
segment_id: <segment_id>
created_at: 2026-06-18T00:00:00+00:00

<t0_event id="evt_x" seq="1" event_type="user_message" role="user" created_at="2026-06-18T00:00:01+00:00" message_id="<chat_message_id>" actor_id="<user_id>" tenant_id="<tenant_id>" runtime_task_id="<runtime_task_id>" source="web" sensitivity="PL1_public">
  <content>用户原文</content>
  <metadata>{"source":"web"}</metadata>
</t0_event>

<t0_event id="evt_y" seq="2" event_type="assistant_message" role="assistant" created_at="2026-06-18T00:00:10+00:00" message_id="<chat_message_id>" actor_id="<agent_id>" tenant_id="<tenant_id>" runtime_task_id="<runtime_task_id>" source="web" sensitivity="PL1_public">
  <content>助手回复</content>
  <metadata>{"source":"web"}</metadata>
</t0_event>

<t0_event id="evt_z" seq="3" event_type="segment_boundary" role="system" created_at="2026-06-18T00:30:00+00:00" message_id="" actor_id="" tenant_id="" runtime_task_id="" source="t0_ledger" sensitivity="PL1_public">
  <content>session_idle</content>
  <metadata>{"reason":"session_idle"}</metadata>
</t0_event>
```

## 9. T0 Event 类型

当前已实现的 event types：

| Event type | 用途 |
| --- | --- |
| `user_message` | runtime 已接受的用户原始输入。 |
| `assistant_message` | assistant 最终内容。 |
| `tool_result` | runtime 已接受的 tool call / result payload。 |
| `trigger_run` | 已完成 trigger run 的证据。 |
| `delegation_run` | 已完成 delegation run 的证据。 |
| `heartbeat_tick` | 已完成 heartbeat tick 的证据。 |
| `dream_run` | 已完成 dream run 的证据。 |
| `legacy_import` | 从 legacy `t0_logger` 文件导入的内容，按 path + digest 幂等。 |
| `segment_boundary` | idle / close / task-complete boundary；seal 当前 active segment。 |

未来可选 event types：

- `session_started`
- `session_resumed`
- `turn_started`
- `assistant_delta`
- `tool_call`
- `runtime_event`
- `compaction_boundary`
- `content_replacement`
- `error`
- `interrupt`
- `rollback_marker`
- `file_snapshot_ref`
- `attribution_snapshot_ref`
- `approval_request`
- `approval_result`
- `policy_preflight`

这些 event types 可以在核心 session ledger 稳定之后再增加。

## 10. 硬不变量

### 10.1 先追加写入，再执行

当用户消息被接受时：

1. 持久化 `ChatMessage`。
2. append `user_message` 到 T0。
3. flush T0。
4. 创建或继续 `RuntimeTask`。

如果 T0 append 失败，runtime 必须 fail closed 或把该 turn 标记为不可恢复。不能在没有 ledger entry 的情况下静默继续。

### 10.2 T0 必须早于派生读模型

DB summaries、session indexes、vector indexes 和 UI projections 不能领先于 T0。

Codex 的规则适用：

> Index 绝不能领先于 append-only source ledger。

### 10.3 显式恢复

恢复必须使用显式 `chat_session_id` 或已验证的 channel conversation mapping。latest-session fallback 只能作为 UX 便利存在，并且必须可以从 session ledger 或已验证的 DB-to-ledger reconciliation 中恢复。

### 10.4 压缩是边界，不是删除

压缩绝不能销毁或重写之前的 raw T0 events。未来可以增加专用 `compaction_boundary` event，但当前 T0 完整性不能依赖压缩去重写 raw events。

### 10.5 Segment 是回放单位

sealed segment 是最小 durable replay 单位。

回放 / read-model derivation 不能消费：

- open segments
- unflushed events
- 被当作真相源的 ChatSession summaries
- 没有 import metadata 的 legacy behavior logs

### 10.6 T0 不做判断

T0 不能执行：

- 语义分类
- memory promotion
- scoring
- tag generation
- reflection
- summary

允许的机械操作：

- schema validation
- sequence assignment
- event id assignment
- privacy gate / PL4 blocking
- artifact spilling
- hashing
- flush / fsync
- ledger flush 之后更新 index

## 11. 实现影响

### 11.1 已实现模块

```text
backend/app/memory/t0/
  __init__.py
  ledger.py
```

职责：

| 模块 | 职责 |
| --- | --- |
| `ledger.py` | 类型化 append result / event records、XML event block append + fsync、segment open / seal、replay，以及幂等 legacy import。 |

### 11.2 已修改模块

| 当前模块 | 改动 |
| --- | --- |
| `services/web_chat_runtime.py` | 用户消息被接受后立即 append + flush T0；在 finalization points append assistant / tool / runtime events。 |
| `services/task_executor.py` | 把 one-off background task 的 user / tool / assistant events append 到 task reflection session ledger，并在完成时 seal。 |
| `runtime/hooks_setup.py` | 停止把 `_t0_cursors` 当作 source of truth。idle / close 应只 seal segment，不再创建 primary T0 evidence。 |
| `services/t0_logger.py` | 从 runtime T0 truth 退役；只保留 legacy import / manual compatibility。`backfill_recent_chat_logs()` 写入新的 ledger。 |
| `api/websocket.py` | 使用显式 resume / session contract；latest fallback 会发 source event，不能被视为 canonical resume。 |
| `services/memory_service.py` | `ChatSession.summary` 变成 read model / cache。 |

### 11.3 数据库考虑

最小 code-first 路径可以只使用文件和 sidecars，不新增表。

后续推荐增加 durable state table：

```text
t0_segments
  id
  tenant_id
  agent_id
  chat_session_id
  segment_id
  state
  source_path
  first_event_id
  last_event_id
  event_count
  opened_at
  sealed_at
  sha256
  prev_segment_id
  created_at
  updated_at
```

这张表是文件之上的 index / read model，不是语义真相源。

## 12. 证据引用

稳定 source refs 应该采用 URI 风格格式：

```text
t0://agent/<agent_id>/session/<chat_session_id>/segment/<segment_id>
t0://agent/<agent_id>/session/<chat_session_id>/segment/<segment_id>#event/<event_id>
t0://agent/<agent_id>/session/<chat_session_id>/segment/<segment_id>#message/<chat_message_id>
t0://agent/<agent_id>/session/<chat_session_id>/segment/<segment_id>#artifact/<artifact_id>
```

后续上层可以引用这些 refs。T0 实现完成的标准是：这些 refs 稳定、可解析、可 replay。

## 13. 迁移策略

### 13.1 保留旧日志

默认不要重写或删除现有 `logs/YYYY-MM-DD/behavior/*.md`。

Legacy logs 仍然是证据，但必须标记为 legacy source。

### 13.2 导入旧日志

增加 importer：

```text
legacy behavior T0 MD
  -> parse frontmatter/body
  -> create imported T0 segment
  -> preserve original_path
  -> preserve artifact refs
  -> write import boundary event
```

Imported segment frontmatter 应包含：

```yaml
imported_from: logs/YYYY-MM-DD/behavior/chat-....md
imported_at: <timestamp>
legacy_schema: t0_logger.chat_snapshot.v1
```

### 13.3 Backfill 游标修复

当前 backfill cursor 按 `session_id` 分组。当一个 ChatSession 有多个 T0 chunks 时，这是不安全的。

新的 cursor 必须基于 segment / event：

```json
{
  "schema": "hive.t0.backfill_cursor.v2",
  "processed_segments": {
    "<segment_id>": {
      "last_event_id": "<event_id>",
      "sha256": "<segment_hash>"
    }
  }
}
```

### 13.4 切换顺序

切换顺序：

1. 增加 T0 writer 和 replay tests。
2. 在创建 RuntimeTask 前接入 web chat user-message append。
3. 在现有 finalization points 接入 assistant / tool / runtime append。
4. 把 idle / close hooks 改成只 seal segment。
5. 增加 legacy importer。
6. 将旧 `t0_logger.write_t0_log` 路径标记为 legacy compatibility only。

## 14. 红线测试

T0 是地基。第一次实现必须包含这些测试。

### 14.1 已接受用户消息必须在运行前进入 Ledger

给定一个新的 web user turn：

- `ChatMessage(role=user)` 已存储。
- T0 有 `user_message`。
- T0 在 `RuntimeTask(status=running)` 可见之前已经 flush。

失败预期：

- 如果 T0 append 失败，该 turn 不能静默继续。

### 14.2 进程重启不能重复写 T0

给定一个已有 T0 events 的 session：

- 重启会清空进程内存。
- 新 event append 从 durable ledger / index 的 sequence 继续。
- 不写重复 event。

### 14.3 Idle 只封存 Segment，不关闭 ChatSession

给定一个 long-running ChatSession：

- Idle threshold 写入 `segment_boundary` 并封存当前 active segment。
- 同一个 ChatSession 后续仍可以继续追加新 segment。
- 回放可以选择 sealed segment，也可以从下一个 segment 继续。

### 14.4 显式恢复必须进入 Ledger

给定一个带显式 `session_id` 的 request：

- 新的已接受 events 追加到已有 session ledger 下。
- 被回放的 messages 来自 T0 ledger，或来自已验证的 DB-to-ledger reconciliation。

### 14.5 最新会话回退必须被审计

给定没有显式 `session_id`，且 fallback 选择 latest session：

- 在追加新 T0 events 前，选中的 session id 必须已经显式确定。
- 在专门的 `session_selected_by_fallback` event 实现前，fallback selection 应通过 caller metadata 审计。

### 14.6 压缩不能销毁原始事件

给定一个 compacted session：

- raw user / assistant / tool events 仍然可读。
- T0 source events 不会被删除或重写。
- 回放可以选择 compacted context，但不能删除 source events。

### 14.7 工具产物完整性

给定一个超过 inline threshold 的 tool result：

- 当前 T0 记录已接受的 tool result payload。
- 超大的 runtime artifacts 继续由 runtime / workspace artifact 机制单独治理，除非未来 T0 artifact adapter 增加 `artifact_ref` 和 hashes。

### 14.8 未封存 Segment 不是稳定回放单元

给定一个 open segment：

- 回放恢复只能把它当作 live tail state 读取。
- 持久化恢复历史使用最后一个已 flushed event。
- `segment_boundary` seal 当前 segment 之后，该 segment 才成为不可变 replay history。

### 14.9 旧数据导入必须幂等

给定一个旧 `logs/YYYY-MM-DD/behavior/*.md` 文件：

- 第一次 import 创建一个 imported segment。
- 第二次 import 不创建重复内容。
- `original_path` 被保留。

### 14.10 PL4 不能进入持久化 T0

给定包含 credential material 的 raw input：

- Privacy gate 根据 policy mask 或 block。
- 持久化 T0 不包含原始 credential。
- Event 记录 sensitivity decision metadata。

## 15. 不属于 T0 范围的事项

T0 实现不要解决这些问题：

- 上层 summary prompt 结构
- 上层 tag taxonomy
- T3 convergence files
- Learning Brain tagging logic
- Memory Governance review agents
- Dream 写入 `soul.md`
- Skill / capability capsule generation
- Workflow design
- Vector / graph derived indexes

T0 只创建可信 source ledger，供这些上层消费。

## 16. 实现验收标准

T0 满足以下条件才算完成：

1. Web chat 已接受的 user turns 在 runtime execution 前 append 到 T0。
2. Assistant final messages、tool calls / results、runtime boundaries append 到 T0。
3. Idle / close 不再依赖 in-memory cursor 作为 primary evidence。
4. Segments 可以被 seal 和 replay。
5. 恢复可以从 T0 ledger 或已验证 reconciliation 重建 conversation。
6. Legacy logs 可以幂等 import。
7. 第 14 节红线测试通过。
8. 切换期间现有 `ChatMessage` 和 `RuntimeTask` 路径保持兼容。
9. 没有任何 T0 code path 执行 semantic summary、scoring、promotion 或 tag judgment。

## 17. 一句话目标

T0 必须成为 Hive 的 Claude Code transcript / Codex rollout 等价物：

> 一个 durable append-only session ledger，以 Hive 的 MD / XML 形态呈现。Session 可以从中 resume，证据可以从中引用，上层记忆可以基于它安全蒸馏。
