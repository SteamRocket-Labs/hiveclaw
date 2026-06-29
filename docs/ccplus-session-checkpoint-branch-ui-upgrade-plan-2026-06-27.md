# CCPlus Session Checkpoint / Rewind / Branch UI 升级方案

日期：2026-06-27

状态：历史细节附录，不再作为 Session TUI 主方案。统一主方案是 `docs/ccplus-session-tui-unified-expression-plan-2026-06-28.md`；后续 checkpoint、rewind、branch、Agent Team、Dynamic Workflow、右侧 Runtime Tables、三栏布局等裁决必须写入统一 TUI 文档。本文只保留 2026-06-27 checkpoint/branch 方向的背景和局部细节，避免形成第二套方案。

关联文档：

- `docs/ccplus-session-control-command-alignment-2026-06-27.md`：session command 的语义、typed result、`/compact`、`/rewind`、`/branch`、`/clear` 的后端契约。
- `docs/ccplus-session-ux-contract-2026-06-26.md`：Session Workbench 的整体 UX 契约，尤其是“用户看到工作判断，不看工具流水账”。
- `docs/frontend-session-workbench-cc-codex-parity-gap-2026-06-23.md`：前端 Session Workbench 的大结构方向。
- `docs/ccplus-session-runtime-token-compaction-alignment-2026-06-27.md`：runtime/token/compaction 对齐口径。
- `docs/ccplus-unclosed-gap-register-2026-06-27.md`：当前 gap register；其中 C-1/C-2 记录本轮代码层闭环证据，剩余未闭环项仍以该文档为准，更完整的 checkpoint/branch UI 体验升级以本文为准。

## 1. 产品裁决

Session 对用户来说应像一个可恢复、可分叉、可压缩、可审计的工作仓库。

```text
Session = 一条可 resume 的工作线
Checkpoint = 一次用户输入形成的导航锚点
Rewind = 当前工作线的 active head 回到所选 checkpoint 对应 prompt 输入前
Branch = 从所选 checkpoint 对应 prompt 输入前派生一条新的工作线
Compact = 保留事实与结论，替换旧上下文窗口
Clear = 结束当前工作线，创建干净的新工作线
```

Codex 值得借鉴的是 checkpoint rail / timeline 的可感知交互：用户能看到每次 prompt 输入对应的小节点，能点击任意节点定位到对应 transcript 位置，并能看到回溯后哪些内容变成非当前 head。

CC / FreeCode 决定语义：`rewind` 和 `branch` 不能混淆。Codex 只作为 UI/工程体验参考。本文不定义 `clone` session command；现有 `/copy` 只是复制 assistant 输出内容，不是 session 克隆，也不是 checkpoint 分叉。若 UI 需要把被选中的 user prompt 放回 composer，这属于 CC rewind/branch 交互的 prefill 副作用，不是第三种 session mutation。

### 1.1 CC 源码核验结论（2026-06-28）

已迁入统一主方案：`docs/ccplus-session-tui-unified-expression-plan-2026-06-28.md` 的 `1.1.1 CC checkpoint / rewind / branch 源码核验结论`。本文不再复制该表，避免两份文档产生口径漂移。

## 2. 当前基线

当前代码已经具备以下基础能力：

| 能力 | 当前状态 | 代码入口 |
| --- | --- | --- |
| `/compact` 写 active projection | 已有 | `backend/app/services/session_command_runtime.py` |
| `/compact` projection 被下一轮 runtime 消费 | 已补齐 | `backend/app/services/web_chat_runtime.py::_apply_active_projection_to_history` |
| `/rewind` 写 active projection | 已有 | `backend/app/services/session_command_runtime.py` |
| `/rewind` projection 被下一轮 runtime 消费 | 已补齐 | `backend/app/services/web_chat_runtime.py::_rewind_projected_history` |
| typed `ui_action` 不再渲染 raw JSON | 已有 | `frontend/src/pages/agent-detail/sessionCommandResult.ts` |
| 基础 session command 面板 | 已补齐 | `frontend/src/pages/agent-detail/AgentChatSection.tsx::SessionCommandControlPanel` |
| 基础 checkpoint selector 点击回传 `/rewind` | 现状断点 | `frontend/src/pages/agent-detail/AgentChatSection.tsx::SessionCommandControlPanel` |

当前不足：

- checkpoint rail 只是基础面板，不是完整 timeline。
- 当前基础面板里点击 checkpoint 会直接调用 `/rewind`；目标 UI 必须改成“点击只定位/选中，用户再选择 Rewind 或 Branch”。
- rewind 后的“当前 head / 被回溯 tail / 新 tail”没有视觉区分。
- branch family 虽已有 lineage，但没有和 checkpoint timeline 合并成可理解的分支图。
- workspace restore 仍没有完整 UI 和 snapshot contract。
- `/clear`、`/compact`、`/rewind` 的结果还没有统一进入右侧 Session Inspector 的状态区。
- mobile / narrow viewport 下 checkpoint selector、branch graph、inspector drawer 尚未验收。

## 3. 目标心智模型

目标 UI 要让用户明白三件事：

1. **我现在在哪条工作线上。**
   - 当前 `ChatSession.id` 是一条执行线。
   - `root_session_id` 是同一个 session family。
   - `parent_session_id` 和 branch metadata 决定分支关系。

2. **这条工作线当前 head 在哪里。**
   - 没有 rewind：head 是最新 transcript event。
   - 有 rewind projection：head 指向某个 checkpoint；后续旧内容仍在历史里，但不再进入模型上下文。
   - 有 compact projection：head 不变，但旧上下文被 summary replacement 替换。

3. **我可以从哪里继续或分叉。**
   - hover checkpoint：显示该 checkpoint 的缩略状态卡片。
   - 点击 checkpoint：只让 transcript / session timeline 位置回滚到该节点，不执行任何改变类命令。
   - 改变类操作只有两个：`Rewind here` 和 `Branch here`。
   - 查看当时上下文、查看文件变化属于只读详情，不是 checkpoint hover 卡片里的改变类动作。

## 4. Session 命令与语义

### 4.0 Checkpoint Git Line 硬规则

Checkpoint Git line 是导航图，不是动作按钮。

规则：

- Hover checkpoint 节点：只显示缩略状态卡片。
- 点击 checkpoint 节点：只滚动/定位到该节点对应的 session 对话位置，并把节点置为当前 focused checkpoint。
- 不允许点击节点后直接执行 `/rewind`。
- 不允许点击节点后改变 `active_projection`、`active head`、`ChatSession.id` 或 active run 状态。
- focused checkpoint 对话锚点或右侧 inspector 只提供两个改变类动作：`Rewind`、`Branch`。
- `Rewind` 和 `Branch` 都以“所选 checkpoint 对应用户 prompt 输入前”为语义边界。
- 不引入 `Clone`。如未来需要“复制 prompt 到输入框”，必须作为独立 composer prefill 设计，不得混入 session branch/rewind 语义。

### 4.1 Rewind

`rewind` 是当前 session 的 active projection 改变，不创建新的 `ChatSession.id`。

用户效果：

- 选择某个 checkpoint。
- 当前 session 的模型上下文回到该 checkpoint 对应 prompt 输入前。
- checkpoint 之后的旧消息仍显示在 UI 中，但标记为“已回溯 / 不在当前上下文”。
- 用户可以修改原 prompt 后继续，也可以直接继续发新消息。

后端契约：

- T0 / transcript append-only，不物理删除历史。
- `ChatSession.transcript_metadata_json.active_projection.projection_reason = "rewind"`。
- `checkpoint_event_id` 指向被选 checkpoint。
- 下一轮 runtime 必须使用 active projection 重建 provider conversation。
- 目标语义必须排除所选 checkpoint 对应的 user prompt 本身，即 transcript 边界应是 `sequence < checkpoint.sequence`。

UI 契约：

- 不能把 rewind 结果渲染成 JSON。
- 必须显示 active projection 状态。
- 必须能清楚区分：
  - 当前有效上下文 prefix。
  - 被回溯 tail。
  - rewind 后新写入的 tail。

真实断点：

- 当前 `backend/app/services/web_chat_runtime.py::_rewind_projected_history` 按 `sequence <= anchor.sequence` 取 prefix，会把所选 user prompt 本身带入下一轮上下文；这和本节“回到 prompt 输入前”的产品语义不一致，后续实现必须改为 before-checkpoint 边界。

### 4.2 Branch

`branch` 有两个 UI 入口，但后端必须清楚区分边界：

- `/branch` slash command：按 CC 当前 head fork 语义创建新的 `ChatSession.id`，复制当前有效 head。
- checkpoint 上的 `Branch here`：按 CC MessageSelector 的 before-boundary 语义创建新的 `ChatSession.id`，复制所选 user checkpoint 之前的 prefix。

用户效果：

- 从当前 session 某个 checkpoint 创建新分支。
- 新分支的上下文边界同样是该 checkpoint 对应 prompt 输入前。
- UI 切换到新分支。
- 新分支保留来源关系，可以在 branch graph 中回到原分支。

后端契约：

- 新 `ChatSession.id`。
- `root_session_id` 与来源 session family 一致。
- `parent_session_id` 指向来源 session。
- branch metadata 记录 `anchor_event_id`、`branch_mode`、`source_session_id`。

UI 契约：

- 用户命令统一叫 `/branch`，不要暴露 `/fork`。
- UI 可用“分支”概念；内部 legacy `fork` 字段只作为兼容，不进入用户语言。
- 分支图里每条分支是独立 session line，不要和 rewind 的 active projection 混淆。

真实断点：

- 当前 `/branch` command 总是通过 `create_conversation_branch(mode="branch")` 创建新 session；这对“当前 head fork”是合理的，但如果 UI 在 selected checkpoint 上复用该命令，会因为 `mode="branch"` 的 prefix 规则包含 anchor 而把所选 user prompt 复制进新分支。
- 当前 `POST /sessions/{session_id}/branches` 的公开 schema 不接受 `mode="branch"`，但接受 `mode="rewind"`；而 command 层内部使用 `mode="branch"`。后续必须统一：checkpoint `Branch here` 调 branch API 时用 `mode="rewind"`，普通 `/branch` 无 checkpoint 参数时保留 current-head fork 语义。
- 当前 `backend/tests/services/test_conversation_branch_service.py::test_rewind_branch_copies_prefix_before_user_checkpoint` 已经覆盖 `mode="rewind"` 的 before-boundary 分支能力，断点主要在 command/UI 调用路径，而不是 branch service 完全缺失。

### 4.3 Compact

`compact` 是当前 session 的上下文窗口替换，不创建新 session。

用户效果：

- UI 出现“上下文已压缩”状态。
- 旧消息仍可查看，但模型下一轮看到的是 summary replacement + compact 后 tail。
- Usage/Context 面板应显示压缩前后 token 变化和 active projection 摘要。

后端契约：

- 写 `active_projection.projection_reason = "compact"`。
- `replacement_messages` 是后续 runtime 的模型输入替代片段。
- append `session_compact` 事件。

UI 契约：

- timeline 中出现 compact marker。
- context panel 显示 compact summary，不默认展示 raw replacement JSON。
- 如果 compact 失败，必须显示失败原因，不伪造成功。

### 4.4 Clear

`clear` 是创建干净的新 session identity，不是删除旧 session。

用户效果：

- 当前会话切到新 session。
- 旧 session 保留在列表。
- 新 session 可显示“由 clear 从 X 创建”。

后端契约：

- 创建新 `ChatSession.id`。
- 保留 parent/root 关系用于审计。

UI 契约：

- `switch_session` 必须真实切换。
- 旧 session 不应被隐藏或误删。

### 4.5 已实现 Session 操作清单

结论：如果只看 checkpoint Git line 上的改变类动作，用户应只看到 `Rewind` 和 `Branch`。但如果看完整 session command / session API 表面，已经实现的 session 操作不止两个。

代码证据：

- `backend/app/services/session_command_runtime.py::SESSION_COMMAND_NAMES`
- `backend/app/services/command_registry.py::build_default_command_registry`
- `backend/app/api/chat_sessions.py::BranchSessionIn`
- `backend/app/api/chat_sessions.py` session routes

Session command 层已经实现：

| 命令 | 是否改变 session 状态 | UI 归属 |
| --- | --- | --- |
| `rewind` | 是。当前 session 写 `active_projection`。 | checkpoint selected 节点动作 |
| `rollback` | 是。按 N 个 user checkpoint 写 `active_projection`，是 rewind 的批量/隐藏形态。 | 不放 checkpoint 主菜单 |
| `branch` | 是。创建新的 `ChatSession.id` 并 `switch_session`。 | checkpoint selected 节点动作 |
| `compact` | 是。当前 session 写 compact projection。 | Session Inspector / context 状态 |
| `clear` | 是。创建干净新 session，旧 session 保留。 | Session menu，不放 checkpoint 节点 |
| `btw` | 是。创建 one-turn side session。 | 侧问/后台通道，不放 checkpoint 节点 |
| `rename` | 是。改当前 session title。 | Session menu / inline title |
| `tag` | 是。改当前 session metadata tags。 | Session metadata，不放 checkpoint 节点 |
| `interrupt` | 是。取消当前 active run。 | run status/control |
| `turn_steer` / `steer` | 是。给当前 active turn 追加 steering input。 | active run control，不放 checkpoint 节点 |
| `resume` | 否。读取 durable transcript 状态并返回 resume 策略。 | Session recovery |
| `checkpoints` | 否。列出 user-turn checkpoints。 | timeline/read model |
| `export` | 否。导出 transcript/artifact/ledger evidence。 | inspector/raw/export |
| `copy` | 否。返回 assistant response 内容用于 clipboard/file copy。 | 交付/复制，不是 clone |

Branch API 还支持消息级分支模式：

| API mode | 作用 |
| --- | --- |
| `fork` | 从某条消息 anchor 复制前缀创建新 session，不自动运行。 |
| `edit` | 从 user message 前创建新 session，并用新内容启动 run。 |
| `insert_before` / `insert_after` / `reply` | 从 anchor 周边创建新 session，并追加用户内容启动 run。 |
| `regenerate` | 从 assistant message 前创建新 session，用上一条 user prompt 重新运行。 |
| `rewind` | 创建一个 before-user-checkpoint 的 branch session；注意这和 command `/rewind` 的 active projection 不是同一语义层。 |
| `side_question` | 创建 side session，等价于 command `btw` 的底层 branch 模式。 |

非 command 的 session API 也已经存在：

| API | 是否改变 session 状态 | UI 归属 |
| --- | --- | --- |
| `POST /agents/{agent_id}/sessions` | 是。创建普通新 session。 | 左侧 session 列表 / 新会话 |
| `PATCH /agents/{agent_id}/sessions/{session_id}` | 是。重命名 session。 | title 编辑 |
| `PATCH /agents/{agent_id}/sessions/{session_id}/permissions/profile` | 是。更新 session-local permission profile，并同步 active run context。 | 权限模式控件 |
| `POST /agents/{agent_id}/sessions/{session_id}/runs` | 是。启动当前 session 的 durable run。 | composer submit |
| `POST /agents/{agent_id}/sessions/{session_id}/turns/steer` | 是。给 active turn 追加 steering input。 | active run control |
| `POST /agents/{agent_id}/sessions/{session_id}/permissions/{permission_request_id}/resolve` | 是。resolve session-local permission request，并可能恢复 pending tool。 | 权限确认卡 |
| `POST /agents/{agent_id}/sessions/{session_id}/runs/{run_id}/cancel` | 是。取消 active run。 | run status/control |
| `DELETE /agents/{agent_id}/sessions/{session_id}` | 是。删除 session read model。 | session list 管理，不属于 checkpoint Git line |
| `GET /sessions/.../messages/transcript/index/workbench/lineage/export` | 否。读取 session read model 或导出。 | inspector / transcript / recovery |

因此本 UI 的分层裁决是：

- Checkpoint Git line 节点：只负责导航/选中；改变类动作只放 `Rewind` 和 `Branch`。
- Session Inspector / Session menu：承载 `compact`、`clear`、`rename`、`tag`、`export`、`copy`、permission、run control。
- Active run 区：承载 `interrupt`、`turn_steer`。
- `rollback`、`checkpoints`、`btw` 是已有能力，但不应该作为 checkpoint 节点的一等主动作暴露。

## 5. 目标 UI 结构

### 5.1 Session Header

Header 应显示：

- session title。
- model。
- run status。
- permission mode。
- checkpoint count。
- branch count。
- compaction count。
- active projection 状态。

active projection 状态例子：

```text
上下文：已压缩
或
回溯：到第 6 次输入
```

### 5.2 Checkpoint Timeline Rail

这是最接近 Codex 动效的核心区域。

视觉结构：

```text
●──●──●──●──●──●──●
1  2  3  4  5  6  7
         ↑
       active head
```

节点定义：

- 每个 user prompt 形成一个 primary checkpoint node。
- assistant/tool 过程不作为主节点，但可以作为节点内部展开细节。
- compact / permission / plan approval 可以作为 marker 挂在节点之间。

状态：

- `current`：当前 active head。
- `past`：active head 前的有效上下文。
- `rewound_tail`：checkpoint 后但已被 rewind 排除的旧内容，视觉虚化。
- `branch_anchor`：产生过 branch 的节点。
- `compacted_scope`：被 compact summary 覆盖的范围。

Hover 节点后的行为：

- 节点旁显示缩略状态卡片。
- 缩略状态卡片显示输入摘要、运行状态、文件变化、branch 来源、compact/snapshot 标记。
- hover 卡片不承载 `Rewind here` / `Branch here` 这种改变类动作。

点击节点后的行为：

- transcript 滚动到该 checkpoint 对应的 user prompt。
- checkpoint 节点进入 focused 状态。
- 中间 session timeline 的位置回滚 / 定位到该 checkpoint 对话锚点。
- 对话锚点 action bar 或右侧 inspector 显示只读详情与改变类动作。
- 改变类动作只显示 `Rewind here` 和 `Branch here`，不放在 hover 卡片里。

### 5.3 Transcript Timeline

Transcript 应和 checkpoint rail 联动。

规则：

- 当前 active projection 之前：正常显示。
- rewind 后被排除的旧 tail：降低 opacity，左侧显示 “已回溯，不在当前上下文”。
- rewind 后新 tail：正常显示，并显示 “回溯后继续” marker。
- compact 覆盖范围：显示 compact marker；旧内容仍可展开，但默认折叠。

### 5.4 Branch Graph

Branch graph 是 session family 的可视化。

结构：

```text
main
  ├─ branch A
  │   └─ branch A.1
  └─ branch B
```

每条 branch 是一个 `ChatSession.id`。切换 branch 等价于切换 session。

需要显示：

- 当前 branch。
- parent branch。
- anchor checkpoint。
- branch title。
- latest status。
- branch 是否仍有 active run。

### 5.5 Session Inspector

右侧 inspector / drawer 应统一承载：

- Context：active projection、compact summary、token usage。
- Checkpoints：列表、搜索、筛选。
- Branches：branch family graph。
- Artifacts：交付物预览。
- Changes：文件变更摘要。
- Raw：JSON export、debug detail。

桌面端：右侧 rail。

窄屏：drawer，不挤压 composer。

## 6. 数据与 API 契约

### 6.1 Session Projection Read Model

需要一个前端可直接消费的 projection read model：

```ts
interface SessionProjectionState {
  projection_reason: 'none' | 'compact' | 'rewind';
  applied_at?: string;
  checkpoint_event_id?: string;
  replacement_summary?: string;
  effective_message_ids?: string[];
  excluded_message_ids?: string[];
  tail_message_ids?: string[];
}
```

目的：

- 前端不应自己推断哪些消息被 rewind 排除。
- 后端应给出 active projection 的解释型 read model。

### 6.2 Checkpoint Read Model

```ts
interface SessionCheckpointNode {
  id: string;
  sequence: number;
  event_id: string;
  message_id?: string;
  role: 'user';
  title: string;
  preview: string;
  created_at: string;
  branch_count: number;
  has_workspace_snapshot: boolean;
  is_active_head: boolean;
  is_excluded_by_projection: boolean;
  markers: Array<'compact' | 'plan' | 'permission' | 'artifact' | 'file_change'>;
}
```

### 6.3 Branch Family Read Model

```ts
interface SessionBranchNode {
  session_id: string;
  root_session_id: string;
  parent_session_id?: string;
  anchor_event_id?: string;
  title: string;
  branch_mode: 'branch' | 'edit' | 'reply' | 'regenerate' | 'side_question';
  status: 'idle' | 'running' | 'completed' | 'failed';
  created_at: string;
}
```

### 6.4 Workspace Snapshot Contract

如果要支持 CC 式“同时回溯对话和代码”，必须补 workspace snapshot：

```ts
interface WorkspaceSnapshotRef {
  checkpoint_event_id: string;
  snapshot_id: string;
  files_changed: number;
  additions?: number;
  deletions?: number;
  restore_supported: boolean;
}
```

没有 snapshot 时，UI 必须明确：

```text
只能回溯对话上下文；当前没有可恢复的 workspace snapshot。
```

不能假装已经恢复文件。

## 7. 关键交互

### 7.1 点击 checkpoint

Hover checkpoint 节点只显示缩略状态卡片。

点击 checkpoint 节点本身只做 session 内位置回滚 / 导航：

1. transcript 跳转到该节点。
2. checkpoint rail / git line 高亮该节点。
3. 中间 session timeline 定位到该 checkpoint 对话锚点。
4. 不调用 `/rewind`、不调用 `/branch`、不创建新 session、不中断当前 run。

focused checkpoint 对话锚点或右侧 inspector 的改变类动作：

1. Rewind here
2. Branch here

如果存在 workspace snapshot，执行 `Rewind here` 时需要二级确认：

1. 只回溯对话
2. 只还原文件
3. 同时回溯对话和文件

如果没有 workspace snapshot，只显示：

- 回溯对话
- 从这里创建分支

### 7.2 执行 rewind

流程：

1. 用户先点击 checkpoint 选中节点。
2. 用户再点击 `Rewind here`。
3. 前端调用 `/rewind { checkpoint_event_id }`。
4. 后端写 active projection。
5. 前端刷新 session transcript + projection read model。
6. UI 标记 active head 和 excluded tail。
7. Composer 自动聚焦，允许用户继续输入。

验收：

- 不新增 `ChatSession.id`。
- 不显示 raw JSON。
- 下一轮模型输入使用 rewind projection。

### 7.3 执行 branch

流程：

1. 用户先点击 checkpoint 选中节点。
2. 用户再点击 `Branch here`。
3. 前端调用 branch API，并显式传 `mode="rewind"` 与 selected `anchor_event_id`，以获得 before-boundary branch。
4. 后端创建新 `ChatSession.id`。
5. 前端切换到新 session。
6. Branch graph 显示新 branch。

验收：

- 新 session 在 session list / branch graph 可见。
- 原 session 不被修改。
- branch anchor 清楚显示。
- 新 session 不复制 selected user prompt 本身；如需要让用户复用该 prompt，只能把它 prefill 到 composer，而不是写入 branch transcript。

### 7.4 执行 compact

流程：

1. 用户输入 `/compact` 或系统 auto compaction。
2. 后端生成 summary replacement。
3. 前端显示 compact marker。
4. context panel 显示 active projection。

验收：

- 下一轮模型输入使用 compact projection。
- UI 显示“已压缩”状态。
- 用户可展开查看 compact summary，但不默认看 raw JSON。

### 7.5 执行 clear

流程：

1. 用户输入 `/clear` 或点击 session menu 的 clear。
2. 后端创建新 session。
3. 前端切到新 session。
4. 旧 session 仍在列表中。

验收：

- 不删除旧 transcript。
- 新 session 是干净上下文。

## 8. 组件拆分建议

不要继续把所有逻辑堆进 `AgentChatSection`。建议拆为：

```text
frontend/src/pages/session-workbench/
  SessionTimelineRail.tsx
  SessionCheckpointMenu.tsx
  SessionProjectionBanner.tsx
  SessionBranchGraph.tsx
  SessionContextInspector.tsx
  sessionProjectionModel.ts
  sessionTimelineSelectors.ts
```

`AgentChatSection` 只保留组合职责：

- 加载 session。
- 传入 timeline/projection/branch data。
- 管理 composer。
- 处理 send / command / branch callbacks。

## 9. 实施顺序

这不是 MVP 分阶段，而是一个完整交付的内部工作顺序。每一步都必须带测试和验收，不允许留下“看起来有 UI 但不生效”的状态。

1. 后端 read model
   - 输出 projection state。
   - 输出 checkpoint nodes。
   - 输出 branch family nodes。

2. 前端 model 层
   - 建立 `sessionProjectionModel.ts`。
   - 把 transcript events、projection、checkpoint、branch family 合成一个 timeline model。

3. Checkpoint rail
   - 渲染节点。
   - 点击节点只选中和定位。
   - selected 节点打开详情与动作面板。
   - rewind / branch 操作接真实 command/API。

4. Transcript projection rendering
   - active head。
   - rewound tail 虚化。
   - compact marker。
   - branch anchor marker。

5. Inspector 升级
   - Context panel。
   - Branch graph。
   - Checkpoint list。
   - Export/raw debug。

6. Workspace snapshot UI
   - 如果后端没有 snapshot contract，只显示“对话回溯”。
   - 后端补齐 snapshot 后，再打开文件还原选项。

7. Browser 验收
   - desktop。
   - narrow viewport。
   - mobile。
   - long session。
   - active run 中禁用危险操作。

## 10. 测试要求

### 10.1 后端

必须覆盖：

- compact projection next-turn consumption。
- rewind projection next-turn consumption。
- rewind before-checkpoint 边界：所选 user prompt 本身不得进入下一轮模型上下文。
- checkpoint read model 排序。
- branch family read model。
- branch before-checkpoint 边界：从 selected checkpoint 分支时，新 session 不得复制所选 user prompt 本身。
- clear 创建新 session 且旧 session 保留。
- workspace snapshot not-supported 时不伪造成功。

### 10.2 前端

必须覆盖：

- checkpoint rail 渲染节点。
- 点击 checkpoint 只选中/定位，不调用 `/rewind` 或 `/branch`。
- 点击 `Rewind here` 才调用 `/rewind`。
- branch 操作创建新 session 并切换。
- rewind 后 excluded tail 虚化。
- compact marker 展示。
- inspector context panel 展示 active projection。
- raw JSON 不出现在 assistant message 中。

### 10.3 浏览器验收

必须手动或 Playwright 验证：

- `/compact` 后下一轮 agent 确实按压缩上下文回答。
- `/rewind` 后下一轮 agent 不再使用被排除 tail。
- `/branch` 创建新 session，原 session 保持不变。
- `/clear` 切到新 session。
- 点击 checkpoint 不执行改变类命令，也不造成 layout shift。
- 窄屏下 drawer 不遮挡 composer。

## 11. 风险

1. **语义混淆风险**
   - `rewind` 和 `branch` 最容易再次被混用。
   - 解决：所有 UI wording 和 command registry 必须统一：checkpoint click 只导航；rewind 不创建新 session；branch 创建新 session。

2. **视觉过载风险**
   - checkpoint rail、branch graph、inspector 同时出现可能变复杂。
   - 解决：默认只显示轻量 rail；详细 branch graph 放 inspector。

3. **workspace restore 风险**
   - 没有 snapshot 时不能承诺文件还原。
   - 解决：先明确 not-supported；支持后再打开三选项。

4. **旧 transcript 与 active projection 不一致风险**
   - 原始历史 append-only，UI 显示时必须解释“哪些不在当前上下文”。
   - 解决：后端提供 projection read model，不让前端猜。

## 12. 最终验收标准

完成后，用户应能做到：

1. 看见每次输入形成的 checkpoint。
2. 点击任意 checkpoint 只定位和选中该节点。
3. 在 selected checkpoint 上显式选择 Rewind，才回到该 checkpoint 对应 prompt 输入前。
4. 清楚看到被 rewind 排除的旧 tail。
5. 在 selected checkpoint 上显式选择 Branch，才创建 branch 并切换。
6. 看见 compact marker，并知道后续上下文已压缩。
7. 在 inspector 里查看 active projection、branch family、checkpoint list 和 artifacts。
8. 全程不看到裸 JSON，除非主动打开 raw/debug 面板。

这才算 Session UI 对 rewind / branch / compact / clear 的完整产品闭环。
