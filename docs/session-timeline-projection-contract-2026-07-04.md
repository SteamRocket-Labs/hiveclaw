# Session Timeline Projection Contract

日期：2026-07-04

状态：实现前产品/工程契约。本文收敛 Session 中间区、完成态折叠、Thinking step、最终交付物、File Changes 与右侧 Workspace rail 的统一投影规则。后续实现必须先按本文补红灯测试，再改代码。

## 1. 背景

当前 Session 页面已经具备 durable web chat、transcript replay、run disclosure、artifact part、file_changes event、右侧 Runtime/Workspace rail 等底座，但投影语义仍不稳定：

1. 完成后的运行过程折叠仍像一个带标签的过程卡片，显示 `Processed / 已处理 + N steps + compact chips`，没有退成 Codex Desktop 式轻量过程边界。
2. Thinking 在 live 阶段挂在 `_streaming` assistant message 上；terminal tool card 到来时，现有前端逻辑可能把 dangling thinking placeholder 删除，导致最终页面只剩工具调用。
3. 后端已有“模型声明交付物 + 当前 turn 写入校验 + file_changes 侧通道”方向，但前端右栏只从 `messages[].artifacts` 聚合；当 final answer 只有 `DELIVERABLE: workspace/...` 文本而没有 artifact part 时，中间区不可点击，右侧 Workspace 也显示 0。
4. File Changes 与 Deliverables 的语义仍容易混在一起。全量写入是变更记录，不等于用户最终交付物。

本文的核心裁决：Session 中央 timeline 与右侧 Workspace rail 必须来自同一份 projection model。不能让正文、artifact cards、右栏各自独立猜测“当前交付物是什么”。

## 2. 目标体验

### 2.1 运行过程中

运行中状态默认展开，用户应看到连续的 work timeline：

```text
用户请求

处理中 0分18秒 v
  Thinking / 工作笔记
    我先检查当前 session artifact 与右栏 Workspace 的接线，因为问题可能在 projection 断链。
  读取文件
    AgentChatSection.tsx, timelineModel.ts
  Thinking / 工作笔记
    已确认中间区有 artifact 打开入口，下一步检查右栏 Workspace 聚合来源。
  搜索代码
    artifact, file_changes, workspaceDocuments
  文件变更
    如果本轮产生写入，显示 change set；无写入则不显示。
  交付物
    只有被模型声明且通过 provenance 校验的文件进入。
```

运行中必须满足：

1. Thinking 是 run step，不是最终 answer 上方的 tag，也不是只在 live 框里短暂存在的临时文本。
2. tool call / tool result / permission / plan / ask user / artifact delivery 都是同一 turn 的 step。
3. Thinking 与 tool call 必须按真实发生顺序交错显示，不能先把所有 thinking 堆一起、再把所有工具按 type 堆一起。
4. 每段 thinking 遇到后续 tool call / permission / plan / artifact event 时立即封存为独立 step；后续新的 thinking 必须另起新 step。
5. final answer 还没完成时，右侧 Runtime rail 显示 active run / waiting / worker / workflow 状态。
6. 如果已有当前 turn deliverable artifact，右侧 Workspace rail 可同步显示；但 scratch/intermediate writes 只进入 File Changes。

### 2.2 完成折叠后

完成后，**只折叠运行过程**，不能折叠最终回答、交付物或变更卡片。过程默认折叠为一行轻量边界：

```text
已处理 1分52秒 >

最终回答正文继续正常显示。
如果有最终交付物，artifact cards 继续显示。
如果本 turn 有文件改动，File Changes card 继续显示。
```

展开后恢复完整 step stream。完成只改变 `RunProcessBlock` 的 collapsed state，不改变 step 的分段、顺序或渲染模型：

```text
已处理 1分52秒 v
  Thinking / 工作笔记
    我先检查当前 session artifact 与右栏 Workspace 的接线，因为问题可能在 projection 断链。
  Tool call
    read_file AgentChatSection.tsx
  Tool result
    已读取 AgentChatSection.tsx。
  Thinking / 工作笔记
    已确认中间区有 artifact 打开入口，下一步检查右栏 Workspace 聚合来源。
  Tool call
    read_file timelineModel.ts
  文件变更
  交付 artifact
```

完成态必须满足：

1. 折叠只作用于 `run_process`，不作用于 `final_answer`、`deliverable_cards`、`file_changes_card`。
2. 折叠行只显示状态、耗时、chevron。不要显示大块过程卡片、`N steps` 强标签或 compact chips。
3. final answer 保持正文主叙事地位，仍按普通 assistant answer 的 markdown 格式完整显示。
4. 最终交付物出现在 final answer 下方，使用 artifact card，必须可点击打开 inspector 或下载。
5. File Changes 是独立 change-set surface：只有本 turn 真正修改文件时显示；无改动不显示。
6. 完成后展开必须仍按 `run_process.steps[]` 的 `sequence` 渲染，不能改成 thinking bucket + tool bucket。

## 3. Projection Contract

一个 assistant turn 必须被投影为以下稳定结构：

```text
TurnProjection
  user_message
  run_process
    collapsed: boolean
    collapsed_label: "已处理 1分52秒"
    steps[]
      - kind: thinking | tool_call | tool_result | permission | plan | file_changes | artifact_delivery | status
      - sequence: number
      - status: pending | running | completed | failed | cancelled
      - parent_step_id?
      - payload
  final_answer
  deliverable_cards[]
  file_changes_card?
  right_rail
    workspace_documents
    runtime_console
```

渲染顺序必须固定为：

```text
User bubble
RunProcessBlock        # 可折叠；折叠时只剩 “已处理 1分52秒 >”
FinalAnswerMarkdown    # 不随 RunProcessBlock 折叠
DeliverableCards       # 不随 RunProcessBlock 折叠
FileChangesCard        # 不随 RunProcessBlock 折叠；无改动则不存在
```

禁止把整个 `TurnProjection` 放进一个可折叠容器。折叠交互只属于 `RunProcessBlock`。

### 3.1 Thinking Step

Thinking 的投影规则：

1. live `thinking` events 先累积到当前 open thinking step。
2. 一旦收到后续 tool call / permission / plan / artifact event，当前 open thinking step 必须封存为 completed step，并固定在该工具调用之前。
3. 后续如果再次收到 `thinking`，必须创建新的 thinking step，不能追加回前一个已封存 thinking step。
4. 如果随后出现 tool call，不得删除 thinking placeholder；必须把它固化为 `run_process.steps[]` 中的独立 step。
5. 如果 final assistant message 同时带 `thinking`/`reasoning parts` 和 content，则 projection 应拆成：
   - `run_process.steps[]` 中按序出现的 thinking step
   - `final_answer`
6. Thinking step 的默认文案应是用户可读工作笔记，不展示 provider raw reasoning payload。现阶段如果已有 thinking 文本，只按现有安全边界展示摘要/片段。

正确示例：

```text
Thinking
  我需要读取 AgentChatSection.tsx 来确认 artifact card 的打开路径。
Tool call
  read_file AgentChatSection.tsx
Tool result
  已读取文件。
Thinking
  已确认中间区有 openArtifact，下一步检查 timelineModel 的 Workspace 聚合。
Tool call
  read_file timelineModel.ts
```

错误示例：

```text
Thinking
  我需要读取 AgentChatSection.tsx...
  已确认中间区有 openArtifact...
Tool calls
  read_file AgentChatSection.tsx
  read_file timelineModel.ts
```

上面的错误示例把 thinking 按类型合并，也把 tool call 按类型分桶，破坏了 run process 的真实顺序。

### 3.2 Tool Step

工具过程默认是 step，不是主叙事正文：

1. 折叠态只参与 `已处理 1分52秒` 这条边界。
2. 展开态按照 `run_process.steps[]` 的 `sequence` 显示完整 step stream。
3. command output、tool args、raw JSON 只在 step detail 内显示。
4. Plan / AskUserQuestion / permission 等需要用户交互的工具，可以在 timeline 中以 inline card 出现，但仍属于当前 run process。
5. tool step 不得从 thinking step 中抽离后再按工具类型聚合；它必须保留在触发它的 thinking 后方。

### 3.3 Final Answer

Final answer 是模型交给用户的结论，不应混入所有工具流水账。

Final answer 不属于可折叠过程块。无论 `run_process` 当前是 collapsed 还是 expanded，final answer 都必须保持可见。

Final answer 可包含文本中的交付声明，例如：

```text
DELIVERABLE: workspace/report.md
交付物: workspace/report.md
```

但 UI 不应把这些路径当成纯文本链接猜测打开。它们必须先经过后端 provenance 校验并变成 artifact part，前端再渲染为 artifact card。

### 3.4 Deliverables

Deliverable 的唯一来源：

```text
模型 final answer 显式声明
AND 当前 turn 写入记录中存在
AND 后端 artifact delivery 创建了可打开的 artifact part/snapshot
```

不允许：

1. 把当前 turn 写入全集自动当成最终交付物。
2. 把历史 workspace 文件自动当成当前 session 交付物。
3. 只因为正文里出现 `workspace/...` 就渲染成可点击交付物。
4. 把 scratch、log、plan、中间稿和最终交付物并列。

### 3.5 File Changes

File Changes 是 change-set，不是 deliverables。

显示规则：

1. 本 turn 有真实写入时显示。
2. 展示改动路径、动作、diff summary（如果有）。
3. 如果某个 changed file 同时被模型声明为最终交付物并通过校验，则它同时出现在 Deliverables；否则只在 File Changes 中。
4. 本 turn 没有写入时，不显示 File Changes。
5. File Changes card 不随 `RunProcessBlock` 折叠；它是完成结果的一部分，用来说明“本 turn 改了什么”。

### 3.6 Session Contamination Guard

共享 workspace 可以存在，但 Session deliverable 不能被共享 workspace 污染。当前 session 的交付物必须同时满足语义选择和机械归属：

```text
LLM 在 final answer 中语义上选择/声明它是最终交付物
AND 平台能证明它属于当前 turn/session/run
OR 它是一个显式跨 agent / child session / workflow artifact ref
```

禁止：

1. 把其他 session 的历史 workspace 文件自动归入当前交付物。
2. 把本 turn 所有写入自动归入当前交付物。
3. 把 scratch、日志、计划、中间稿、work ledger 自动归入当前交付物。
4. 从 assistant 正文里 regex 出 `workspace/...` 后直接创建可点击 deliverable。
5. 在 A2A / subagent / workflow 异步任务里默认读取当前 agent workspace，而忽略 artifact 的 `owner_agent_id` / `source_agent_id` / `download_agent_id` / `delivery_agent_id`。

异步任务规则：

1. A2A / subagent / Agent Team / Dynamic Workflow 的最终交付物可以来自 child agent workspace。
2. 这种交付物必须通过 artifact part 携带 agent/session/run provenance。
3. 前端打开或下载时必须优先使用 `download_agent_id`，其次 `owner_agent_id`、`source_agent_id`，最后才 fallback 到当前 agent。
4. 右栏分组以 artifact provenance 为准，不以当前页面 agent 的 workspace 目录为准。

## 4. 右侧 Workspace Rail Contract

右侧 Workspace rail 不是 agent 全目录浏览器。它是当前 Session projection 的侧栏视图。

### 4.1 分组

至少分为：

1. Current session deliverables
   - 当前 session / run / turn 归属明确的最终交付物。
   - 默认展开。
   - 每一行必须可点击打开 inspector。
2. File changes
   - 本 turn / 当前 run 的 change set。
   - 没有变更则不显示。
3. Explicitly referenced
   - 用户明确引用，或 agent 在本 session 明确 read/open 的文件。
   - 默认可折叠。
4. Historical / unattributed
   - 同 agent 历史文件、无法归属文件、旧数据。
   - 默认折叠，不进入当前交付物区。

### 4.2 点击行为

1. Markdown/text/image/pdf 等 previewable artifact：打开右侧 inspector。
2. 不可 preview 类型：下载或新 tab 打开。
3. 有 artifact snapshot 时默认读取交付时快照。
4. legacy 无 snapshot 时必须标注 current-file fallback。
5. 如果 artifact id 缺失但 path 可读，只能作为 legacy fallback，不得标为 current session deliverable。

### 4.3 与中间区的关系

中间区 artifact card 和右侧 Workspace row 必须引用同一个 `ChatArtifactPart` / `WorkspaceDocumentModel`。

禁止两套逻辑：

```text
中间区：从 final answer 文本 regex 猜测路径
右侧栏：从 messages[].artifacts 聚合
```

正确逻辑：

```text
backend artifact parts
  -> chatRuntime normalize
  -> timelineModel projection
  -> 中间区 artifact card
  -> 右侧 Workspace rail
```

## 5. 文档预览与 Artifact Inspector Contract

文档交付不是正文里的路径文本，也不是一个只能居中缩小查看的静态缩略图。它必须是可打开、可预览、可下载、可追溯的 artifact 对象。

### 5.1 Artifact card 行为

中间区 final answer 下方的 artifact card 必须满足：

1. 点击主区域打开 preview modal / inspector。
2. `markdown`、`text`、`image`、`pdf` 优先走内嵌预览。
3. 非 previewable 类型保留文件卡片，但主动作改为下载或新 tab 打开。
4. card 上必须显示文件名、类型、大小（如果后端提供）、来源状态。
5. card 不应只显示 `workspace/...` 路径文本；路径只能作为 secondary metadata。

### 5.2 Preview modal / inspector

Preview 应是一个真正的 document inspector，而不是页面中央的缩小版截图。

建议默认形态：

```text
width: min(960px, 78vw)
height: min(820px, 78vh)
```

移动端或窄屏时：

```text
width: 100vw
height: 100dvh
border-radius: 0
```

Inspector 顶部工具栏至少包含：

1. 文件名 + 类型。
2. 下载。
3. 新 tab 打开 / 原始文件打开。
4. 复制路径或 artifact reference。
5. 关闭。

Preview body 规则：

1. Markdown 使用正文排版预览，不显示 raw JSON。
2. Text 使用等宽或阅读模式，长行可换行。
3. Image 适配容器，保留缩放/原始大小入口。
4. PDF 使用内嵌 viewer 或浏览器原生预览。
5. 加载失败时显示明确错误和 fallback action，不 silent fail。

### 5.3 Snapshot 与 fallback

Artifact inspector 的读取优先级：

```text
artifact snapshot at delivery time
  -> current workspace file fallback
  -> unavailable state
```

如果使用 fallback，UI 必须标注：

```text
当前文件 fallback，可能不同于交付时版本
```

不能把 legacy fallback 文件渲染成 current session deliverable。它可以出现在 `Explicitly referenced` 或 `Historical / unattributed` 分组。

### 5.4 中间区与右栏共用打开模型

中间区 artifact card 与右侧 Workspace row 必须调用同一个 open model：

```text
ArtifactOpenRequest {
  artifact_id?
  snapshot_id?
  workspace_path?
  preview_kind
  source_group
}
```

禁止出现：

```text
中间区点击：读 workspace path
右栏点击：读 artifact snapshot
```

同一个交付物无论从哪里点开，都必须进入同一个 inspector。

## 6. 右侧 Runtime Object Model

右侧栏下半部分不是一堆通用按钮，也不是把所有 runtime 对象混成一个列表。它应该是当前 session 的运行对象索引。

Tabs 可以保留，但每个 tab 的对象语义必须稳定：

```text
Team      -> Agent Team container + member sessions
Workers   -> ordinary sub-agent / background worker
Workflow  -> Dynamic Workflow root + step/leaf tree
Activity  -> notifications / raw run records / governance events
```

### 6.1 普通 Sub-agent / Background worker

Session 内表现：

1. 作为 run process step 或 worker marker 出现。
2. 展示 spawn/delegate、progress、result summary、failure/cancel 状态。
3. 如果有结果，结果进入当前 turn 的过程或最终 answer 的引用，不冒充主 assistant answer。

右栏 Workers tab 表现：

1. 每个 worker 一行，展示 role/type、status、elapsed、last activity。
2. 可展示 tokens/tools/counts，但不抢主视觉。
3. 可点击打开 worker detail drawer/panel：输入、状态、日志摘要、结果、错误。
4. `Stop` 只在 running 时出现。
5. `Retry` 只在 failed/cancelled 且后端支持 replay 时出现。
6. 普通 one-shot Sub-agent worker 只显示 `Inspect` / detail 入口；不要显示 `Continue` 这种会暗示可继续对话的动作。
7. 后续对话 / follow-up 属于 Agent Team member session、主 session，或后端显式提供的 resumable workflow/subagent 控制面，不能混进普通 worker 行。

硬约束：

```text
普通 sub-agent 行不显示 Continue。
普通 sub-agent 没有 child_session_id 时，也不显示 Enter 按钮。
```

### 6.2 Agent Team

Agent Team 是协作容器，不是普通 worker 列表。

Session 内表现：

1. 父 session 只显示 team start/progress/consolidation/final handoff。
2. 成员的完整对话留在成员 session/window 内。
3. 父 session 可以显示成员产出摘要，但不把成员完整 transcript 灌进主线。

右栏 Team tab 表现：

1. 顶部是 team container summary：目标、状态、成员数、running/waiting/done。
2. 成员行必须展示身份标签：role、能力标签、来源、当前状态。
3. 成员有 `chat_session_id` 时，点击成员进入 child session/window。
4. 成员无 `chat_session_id` 时，只能打开 detail，不显示 Enter。
5. 支持对成员结果做 inspect、resume/follow-up、close/stop，但按钮必须按状态条件显示。
6. Team 级别可以有 consolidate / handoff / continue team action，但不能出现在普通 sub-agent 行上。

需要保留的标签能力：

```text
role tag
capability tag
status tag
source/provenance tag
```

这些标签是为了让用户看懂“谁在干什么”，不是装饰 chip。

### 6.3 Dynamic Workflow

Dynamic Workflow 是确定性工作流运行，不是 Agent Team，也不是普通 Sub-agent。

Session 内表现：

1. 作为 workflow start/progress/gate/final marker 出现。
2. 可以在主 timeline 中显示关键 gate、wait、resume、repair 事件。
3. 不把每个 leaf 的内部日志完整展开到主 session。

右栏 Workflow tab 表现：

1. 顶部是 workflow root summary：名称、状态、当前 phase、elapsed。
2. 中间是 step/leaf tree 或 phase list。
3. 选中 leaf 后显示 leaf detail：输入、输出摘要、状态、错误、artifact。
4. gate/wait/resume/repair 作为 workflow 控制动作显示在 root 或 step 上。
5. workflow root 可以切换到 Workflow Run Window。
6. leaf 只有存在明确 `child_session_id` 时才显示 Enter；否则只显示 View detail。

硬约束：

```text
Dynamic Workflow leaf 默认不是可进入的 ChatSession。
没有 child_session_id 时，不能给 leaf 放 Enter session 按钮。
```

### 6.4 Activity

Activity tab 是辅助审计面，不是主要操作面。

包含：

1. 最近 runtime run 记录。
2. 完成/失败通知。
3. policy/gate/permission 事件。
4. raw event count 和 debug 入口。

Activity 不能替代 Team / Workers / Workflow 的对象视图。

### 6.5 Session 内 vs 右栏表现

| 对象 | Session 内 | 右栏 |
| --- | --- | --- |
| 普通 sub-agent | 过程 step / marker / result summary | Workers 行 + Inspect/detail；不显示 Continue |
| Agent Team | team marker + parent consolidation | Team container + member rows；有 chat_session_id 的成员可进入 |
| Dynamic Workflow | workflow marker + gate/progress/final | Workflow root + step/leaf tree；leaf 默认只看 detail |
| Artifact | final answer 下方 artifact card | Workspace current deliverables row，共用 inspector |
| File changes | final answer 下方 change-set card | Workspace File changes group |

## 7. 当前断点判断

以 2026-07-04 当前代码为准，主要断点是：

1. `frontend/src/pages/agent-detail/chatRuntime.ts`
   - `applyRuntimeDoneEvent()` 能从 `event.parts` 提取 artifacts。
   - 但 terminal tool card 路径中仍存在删除 dangling thinking placeholder 的行为，需要改成固化为 reasoning step。
2. `frontend/src/pages/session-workbench/timelineModel.ts`
   - `buildWorkspaceDocumentsModel()` 只从 `messages[].artifacts` 聚合。
   - 如果后端/WS/replay 没把 final deliverables 转成 artifact part，右栏就是 0。
3. `frontend/src/pages/agent-detail/RunDisclosureBlock.tsx`
   - 完成态仍显示 header + step count + compact chips。
   - 需要改为 `已处理 1分52秒 >` 的轻量折叠行。
4. `frontend/src/pages/agent-detail/AgentChatSection.tsx`
   - 中间 artifact card 与右侧 Workspace row 已有点击入口，但依赖 artifact part 是否存在。
   - 需要确保 final answer 下方 deliverables 与右侧 rail 共用同一模型。
5. `backend/app/services/web_chat_runtime.py`
   - 已有 `_terminal_artifact_paths_for_turn()`、`_append_file_changes_event()`、`_finalize_web_chat_run_with_assistant()` 的方向。
   - 需要验证 production/current path 中 artifact parts 是否真正进入 final done payload 和 transcript replay。
6. Artifact preview
   - 中间区和右侧 Workspace 已有打开入口，但需要统一成同一套 inspector/open model。
   - preview 需要恢复下载、新 tab、复制 reference 等动作。
7. Workers / Sub-agent
   - 普通 sub-agent 不能默认给 Enter/Continue。
   - 只有存在 `child_session_id` 或 `chat_session_id` 时才是可进入对象。
8. Agent Team / Dynamic Workflow
   - Team 成员行与 Workflow leaf 需要区分。
   - Workflow leaf 默认是 detail 对象，不是 child session。

## 8. 实施顺序

### S0. 红灯测试

先补测试，不先改 UI：

1. `chatRuntime.test.ts`
   - `thinking -> tool_call -> done` 后，thinking 不被删除，最终进入 run step。
   - `thinking A -> tool_call A -> thinking B -> tool_call B` 投影为四个按序 step，不能合并为一个 thinking 块或一个 tool bucket。
   - `done` payload 顶层/parts 中的 artifact 都能进入 assistant message artifacts。
2. `timelineModel.test.ts`
   - 一个 turn 投影为 `user_turn + active_run + final_answer/deliverables`。
   - thinking/tool/file_changes/artifact/final answer 都属于同一 turn，但 run process 内必须按 `sequence` 渲染。
   - file_changes 不进入 deliverable cards。
3. `RunDisclosureBlock.test.tsx`
   - completed collapsed renders only `已处理/Processed + duration + chevron`。
   - expanded renders original `run_process.steps[]` sequence without merging thinking or bucketing tools。
4. `AgentDetailSections.test.tsx`
   - final answer 下方 artifact row 可点击。
   - right rail current session deliverables 与 middle artifact card 使用同一 artifact。
   - no artifacts 时右栏显示空态，但 final answer 文本路径不被假装 clickable。
5. `ArtifactPreview.test.tsx`
   - artifact card 和 Workspace row 打开同一个 inspector。
   - markdown/text/image/pdf 有 preview；非 previewable 走下载/新 tab fallback。
   - snapshot fallback 时显示 current-file fallback 标识。
6. `RuntimeRailModel.test.tsx`
   - 普通 sub-agent 无 `child_session_id` 时不显示 Enter/Continue。
   - Agent Team member 有 `chat_session_id` 时可进入 child session。
   - Dynamic Workflow leaf 无 `child_session_id` 时只显示 View detail。

### S1. Thinking 固化为 run step

改 `chatRuntime.ts`：

1. terminal tool card 不再直接删除 live thinking placeholder。
2. terminal tool card 到来时，当前 open thinking step 立即 seal，插入到 tool call 前方。
3. 后续 thinking 新建 step，不能追加到已 seal 的 thinking step。
4. live thinking placeholder 转换为 durable reasoning/work-note message 或 transcript step。
5. final `thinking`/`reasoning parts` 与 content 分离投影。

### S2. Timeline projection 统一

改 `timelineModel.ts`：

1. `buildThreadTimeline()` 输出同一 turn 的 process + answer + deliverables。
2. `assistantReasoningStepMessage()` 不再只是 assistant 附属切片，而是 run step 的稳定来源。
3. `file_changes` event 建成 change-set step，不生成 deliverable card。
4. `run_process.steps[]` 是唯一渲染来源，UI 不再分别遍历 `thinking_steps[]`、`tool_steps[]` 后拼接。

### S3. 完成态折叠

改 `RunDisclosureBlock.tsx` 与 CSS：

1. running/blocked/failed 继续默认展开。
2. completed 默认折叠为一行：

```text
已处理 1分52秒 >
```

3. 点击展开后显示原 `run_process.steps[]` sequence。
4. 展开态继续使用同一个 `run_process.steps[]` 顺序流，不能因为 completed 状态切换成分桶渲染。
5. 移除 completed collapsed 下的 compact chips。

### S4. Deliverables / Workspace rail 接线

改 `chatRuntime.ts`、`timelineModel.ts`、`AgentChatSection.tsx`：

1. final deliverable artifact part 是唯一可点击交付物来源。
2. 中间 artifact card 与右侧 Workspace row 共用 artifact part。
3. `file_changes` 独立显示 change set。
4. historical/unattributed 默认折叠。

### S5. 后端验证与必要修补

只在测试证明断链来自后端时修改后端：

1. 确认 `_finalize_web_chat_run_with_assistant()` 创建的 `artifact_parts` 同时进入：
   - ChatMessage artifact relation
   - assistant transcript event `parts`
   - WS done event payload
2. 确认 `file_changes` event 带 `file_change_paths`、`attached_artifact_paths`、`rejected_artifact_paths`。
3. 确认 legacy/no snapshot fallback 被显式标记。

### S6. Artifact preview 与右栏对象落地

改 `AgentChatSection.tsx`、Workspace rail、runtime rail 相关组件：

## 9. 2026-07-09 二次验收补充

本轮复查确认：前一轮“全面落地”主要收敛了 provider/token/tool evidence 链路，但 `Session Timeline Projection Contract` 的体验目标仍有两处没有完全闭环。

### 9.0 Run process / final answer projection

问题：

1. 前端已有 `RunDisclosureBlock` 的轻折叠测试，也有 thinking/tool 交错测试，但主投影模型仍把 final answer 挂在 `active_run.answer` 上。
2. `AgentChatSection` 随后在 active-run cell 内渲染这个 answer，导致结构上仍是“运行过程容器里包含最终回答”。
3. 这不满足 §2.2：完成后只能折叠 `RunProcessBlock`，不能把 final answer、deliverable cards、file changes card 放在同一个折叠容器语义下。

修正规则：

```text
RunProcessBlock cell
FinalAnswer cell
DeliverableCards / FileChanges surfaces
```

`active_run` 只能持有 `sourceMessages` 和 `run_process.steps[]`。如果一个 assistant message 同时包含 thinking 和 content，必须拆成：

1. thinking-only message 进入 `active_run.sourceMessages`；
2. answer-only message 作为后续独立 `assistant_final` cell。

验收：

1. `thinking -> tool -> final answer` 投影为两个相邻 cell：`active_run` 后接 `assistant_final`。
2. `active_run` 的 `timeline.answerMessageId` 可以用于状态判断，但 UI 不得从 active-run cell 内渲染 answer。
3. 完成态折叠后，DOM 里仍可见 final answer；展开 run process 只恢复 step stream。

### 9.1 Final summary artifact delivery

问题：

1. 后端当前只把 final answer 中显式写成 `DELIVERABLE: workspace/...` 或 `交付物: workspace/...` 的路径转成 artifact delivery。
2. 如果最终总结说“我完成了文档/已编辑文件如下”，但没有使用上述 marker，或者文件只出现在引用区/已编辑文件列表，当前不会自动生成可打开 artifact part。
3. 结果是中间区和右栏都只能看到正文、`file_changes` 或外层“已编辑文件”，没有把最终文档和被提及文件 po 成当前 session 的可打开交付物。

修正规则：

```text
final answer 显式 DELIVERABLE/交付物 marker
OR final answer 中提到的 workspace 路径命中本 turn 写入
OR final summary fallback 选择本 turn 写入中的主要用户文档
AND 路径通过 current-turn/session provenance 校验
-> 创建 artifact_delivery part
```

约束：

1. fallback 只允许从本 turn 写入中选择用户可见文档，不包括 `.ultra/*`、日志、内部审计文件、scratch、临时输出和隐藏文件。
2. 如果 final answer 提到多个本 turn 文件，全部进入 artifact delivery；这些 artifact 同时作为中间区卡片和 Workspace rail 当前 session 行的唯一来源。
3. `file_changes` 仍是 change-set，不能代替 deliverables；但它可作为 artifact fallback 的证据输入。
4. 不能把历史 workspace 文件或其他 session 文件因为正文 regex 命中而提升为 current session deliverable。

验收：

1. final answer 为“已完成，见 `workspace/report.md`”且 `workspace/report.md` 是本 turn 写入时，必须生成 `artifact_delivery`。
2. final answer 没有路径，但本 turn 只写入一个用户文档时，该文档必须作为 final summary fallback delivery 出现。
3. final answer 提到 `workspace/old.md` 但本 turn 未写入时，只能进入 rejected list，不能成为可点击交付物。

### 9.2 Workspace rail current-session scope

问题：

1. 右侧 Workspace rail 的模型虽然已有 `currentSession / historical / unattributed` 分组，但 UI 仍把这些组作为“工作区文档”一起暴露。
2. 用户期望的不是 agent workspace 浏览器，而是当前 session projection 的侧栏；默认不应出现不同 session、历史文件、raw tool workspace writes 或无法归属的文件。

修正规则：

```text
Workspace rail default view = Current session deliverables + File changes
Historical / unattributed = diagnostic only, 默认隐藏，不进入主 Workspace 列表
```

约束：

1. `currentSession` 只接收 artifact delivery、snapshot artifact、terminal/explicit delivery ref，且 runtime task 必须属于当前 session run，或是显式 A2A/child-session delivery ref。
2. `workspace_write` raw tool artifact 不进入 current session deliverables，只进入 file changes / unattributed diagnostic。
3. 右栏默认计数、空态和列表只看 current session deliverables；historical/unattributed 不再让用户误以为当前 session 产生了别的文件。
4. 如果需要排查污染，可以保留 diagnostic 折叠面，但默认不展示在 Workspace 主列表。

验收：

1. 当前 session 有一个 delivery 和一个 historical artifact 时，右栏主列表只显示 delivery。
2. 当前 session 只有 raw tool writes、没有 artifact delivery 时，右栏交付物显示空态，同时 File changes 显示变更。
3. 中间区 artifact card 与右栏 current session row 必须引用同一个 `ChatArtifactPart`。

1. 中间 artifact card 与 Workspace row 共用 `ArtifactOpenRequest`。
2. inspector 恢复 preview、download、open、copy reference、close。
3. Workers tab 按普通 sub-agent / background worker 规则显示动作。
4. Team tab 按 Agent Team container + member rows 显示。
5. Workflow tab 按 workflow root + step/leaf tree 显示。
6. Activity tab 只保留审计/通知/raw run，不承载主要对象操作。

## 9. Source-Checked Durable Stream Step Contract

本节是 2026-07-09 对线上缺口的补充修正：用户在运行完成后只能看到 `已处理` 和工具卡，运行中出现过的 assistant 中间文本消失。这不是 UI 文案问题，而是 durable replay contract 漏项。

### 9.1 CC / FreeCode baseline

源代码核对：

- `/Users/example-owner/vc-saas/free-code-main/src/QueryEngine.ts`
  - accepted user prompt 在进入 query loop 前写入 transcript。
  - query loop 对 `assistant` / `user` / `compact_boundary` 写 transcript。
  - assistant message fire-and-forget 写入，但仍进入同一个 ordered message chain。
- `/Users/example-owner/vc-saas/free-code-main/src/utils/messages.ts`
  - assistant message 的多个 content block 会拆成多个 normalized message，保持 `text` / `thinking` / `tool_use` 的相对顺序。
- `/Users/example-owner/vc-saas/free-code-main/src/components/messages/*`
  - assistant text、assistant thinking、assistant tool use 是独立渲染单元，而不是只显示工具调用。

结论：CC 的语义不是“工具调用记录 + 最终答案”，而是完整 assistant/tool/result 消息链。中间 assistant text 或 thinking 只要进入 provider message stream，就必须能被 transcript replay 还原。

### 9.2 Codex baseline

源代码核对：

- `/tmp/openai-codex/codex-rs/app-server-protocol/src/protocol/common.rs`
  - app-server protocol 明确定义 `item/agentMessage/delta`、`item/reasoning/textDelta`、`item/reasoning/summaryTextDelta`、`item/started`、`item/completed`。
- `/tmp/openai-codex/codex-rs/app-server-protocol/src/protocol/thread_history.rs`
  - persisted rollout replay 和 running thread rejoin 共用 reducer。
  - `AgentMessage` 进入 `ThreadItem::AgentMessage`。
  - `AgentReasoning` / `AgentReasoningRawContent` 进入 `ThreadItem::Reasoning`。
  - `ExecCommandBegin/End` 等工具事件进入对应 command/tool item，按 turn_id/upsert 保持原始 turn 归属。
- `/tmp/openai-codex/codex-rs/tui/src/thread_transcript.rs`
  - `ThreadItem::AgentMessage` 渲染为 `AgentMarkdownCell`。
  - `ThreadItem::Reasoning` 渲染为 `ReasoningSummaryCell`。
  - command/tool/file/search 等 item 走 fallback 或专用 history cell。
- `/tmp/openai-codex/codex-rs/tui/src/history_cell/mod.rs`
  - `HistoryCell` 是 committed transcript 和 streaming active cell 的共同显示单位。

结论：Codex 的精致化不是隐藏中间过程，而是把 provider/runtime event 降维成 typed turn items，再由 history cell 展示。live streaming 和 replay transcript 必须共享同一套 item 语义。

### 9.3 Hive current gap

当前 Hive 已有：

- `web_chat_runtime._WebChatStreamMicroBatcher` 把 `chunk` / `thinking` 合并后发 websocket。
- `chatRuntime.applyTranscriptEvent` 能消费 `thinking` / `chunk`。
- `chatDisclosureReducer` 能把带 `thinking` 的 assistant message 投影成 run process step。

缺口：

1. 后端 `send_stream_event()` 只 broadcast `chunk` / `thinking`，没有写 `chat_transcript_events`。
2. 工具调用和 file changes 已持久化，所以 completed replay 只剩工具卡。
3. 前端只会封存“空 content + thinking”的 streaming placeholder；普通 assistant text delta 在工具调用前不会转成 durable process step。
4. final answer 与 pre-tool assistant narration 缺少明确边界，导致运行中看见、完成后消失。

### 9.4 Required fix

后端：

1. `chunk` / `thinking` 必须在 micro-batcher flush 后写入 `chat_transcript_events`。
2. 这些事件必须：
   - `actor_type=assistant`
   - `role=assistant`
   - `event_type=chunk|thinking`
   - `materialize_chat_message=false`
   - `run_id/runtime_task_id` 绑定当前 run
   - `parts` 使用现有 `text_delta` / `reasoning` 结构
3. reset tombstone 只用于 live retry，不进入 durable transcript。
4. 持久化失败必须记录 warning，但不能让 websocket live stream 或 runtime run 失败。

前端：

1. transcript replay 遇到 `chunk` 时继续创建 streaming assistant placeholder。
2. 当下一个 tool/event step 到来时，如果末尾 assistant placeholder 有非空 `content`，必须把 `content` 转入 `thinking`/process-note，并移除 `_streaming`。
3. final `assistant_message` / `done` 到来时，末尾 streaming assistant 仍由 final answer 替换，避免把最终回答 delta 复制成过程 step。
4. `thinking -> tool -> chunk -> tool -> final answer` 必须投影为：
   - run process step: thinking/process-note
   - run process step: tool
   - run process step: process-note
   - run process step: tool
   - separate final answer cell

### 9.5 Non-goals

1. 不展示 provider raw chain-of-thought。
2. 不把最终回答 delta 复制成过程 step。
3. 不把 tool raw output 展开成默认正文。
4. 不改变 CC/FreeCode 消息链语义，只补 Hive replay/presentation 的断点。

### 9.6 Landing record

状态：已落地

文件：

- `backend/app/services/web_chat_runtime.py`
- `backend/tests/services/test_web_chat_runtime.py`
- `frontend/src/pages/agent-detail/chatRuntime.ts`
- `frontend/src/pages/agent-detail/chatRuntime.test.ts`

落地内容：

1. 后端新增 durable stream step 写入：`chunk` / `thinking` 在 micro-batcher flush 后写入 `chat_transcript_events`，并保持 `materialize_chat_message=false`。
2. websocket live event 绑定 transcript metadata：`transcript_event_id`、`sequence`、`parts`、`metadata`。
3. reset tombstone 不写 durable transcript。
4. 前端在 tool/runtime step 到来前封存 streaming assistant placeholder；普通 assistant text delta 会转成 `thinking`/process-note，不再在完成后消失。
5. final assistant message 仍替换末尾 streaming placeholder，避免最终答案 delta 被复制成过程 step。

验证：

```bash
cd backend && source .venv/bin/activate && pytest \
  tests/services/test_web_chat_runtime.py::test_execute_web_chat_run_emits_first_class_phase_signal \
  tests/services/test_web_chat_runtime.py::test_execute_web_chat_run_persists_stream_steps_for_replay \
  tests/services/test_web_chat_runtime.py::test_web_chat_stream_micro_batcher_coalesces_chunk_bursts \
  tests/services/test_web_chat_runtime.py::test_web_chat_stream_micro_batcher_flushes_before_reset_and_preserves_order \
  -q
# 4 passed, 3 warnings

cd backend && source .venv/bin/activate && ruff check \
  app/services/web_chat_runtime.py \
  tests/services/test_web_chat_runtime.py
# All checks passed!

cd frontend && npm test -- --run \
  src/pages/agent-detail/chatRuntime.test.ts \
  src/pages/agent-detail/chatDisclosureReducer.test.ts
# 76 passed

cd frontend && npm run build
# tsc && vite build passed
```

## 10. 验收标准

### 10.1 运行中

1. 用户发起请求后，run process 默认展开。
2. Thinking 以 step 形式出现在最上方，并在 tool call 后仍留存。
3. 多段 Thinking 与工具调用按发生顺序交错留存：`Thinking A -> Tool A -> Thinking B -> Tool B`。
4. 工具调用按 step 出现，不挤占 final answer。
5. 右侧 Runtime rail 显示 active state。

### 10.2 完成后

1. run process 默认折叠为 `已处理 1分52秒 >`。
2. 展开后完整 `run_process.steps[]` sequence 仍在：`Thinking A -> Tool A -> Thinking B -> Tool B` 的顺序不能变化。
3. final answer 在 run process 折叠时仍然完整可见，不能被折叠进过程块。
4. artifact card 出现在 final answer 下方且可点击，并且不随 run process 折叠消失。
5. File Changes card 在有文件改动时显示在最终回答区域，不随 run process 折叠消失；无改动时不显示。
6. 右侧 Workspace 显示相同 current session deliverables。

### 10.3 交付物

1. final answer 里的 `DELIVERABLE:` 文本不会被前端 regex 伪装成链接。
2. 只有 artifact part 可点击。
3. 点击 markdown/text artifact 打开 inspector。
4. 点击非 previewable artifact 走下载/新 tab。
5. 历史文件不进入 current session deliverables。

### 10.4 文档预览

1. 中间 artifact card 与右栏 Workspace row 打开同一个 inspector。
2. inspector 有下载、新 tab 打开、复制 reference、关闭。
3. Markdown/text/image/pdf 有可读 preview。
4. snapshot 可用时打开交付快照。
5. current-file fallback 被明确标注。

### 10.5 右侧 Runtime 对象

1. 普通 sub-agent worker 只显示 Inspect/detail，不显示 Continue；无 `child_session_id` 时也不显示 Enter。
2. Agent Team member 有 `chat_session_id` 时可以进入 child session/window。
3. Dynamic Workflow root 可以进入 Workflow Run Window。
4. Dynamic Workflow leaf 无 `child_session_id` 时只显示 View detail。
5. Team / Workers / Workflow / Activity 的对象职责不混用。

### 10.6 回归命令

```bash
cd frontend
npm run test -- \
  chatRuntime.test.ts \
  timelineModel.test.ts \
  RunDisclosureBlock.test.tsx \
  AgentDetailSections.test.tsx \
  ArtifactPreview.test.tsx \
  RuntimeRailModel.test.tsx

npm run build
```

如 S5 修改后端，再补：

```bash
cd backend
source .venv/bin/activate
pytest tests/services/test_web_chat_runtime.py -q -k "artifact or file_changes or thinking"
```

## 11. 非目标

1. 本文不要求把 agent workspace 全目录浏览器塞进右侧 rail。
2. 本文不要求展示 provider raw chain-of-thought。
3. 本文不要求把所有文件写入都当成交付物。
4. 本文不改变 CC/FreeCode runtime 语义；它只定义 Hive Web Session 的 Codex-style projection。
5. 本文不处理性能优化；性能优化见 `docs/session-rendering-overhaul-plan-2026-07-03.md`。
6. 本文不把 Dynamic Workflow leaf 强行改造成 ChatSession；除非后端显式提供 `child_session_id`。

## 12. 相关文档

1. `docs/ccplus-session-ux-contract-2026-06-26.md`
   - Session UX 总契约：工作判断优先、工具折叠、artifact inspector。
2. `docs/agent-team-session-workbench-root-cause-and-repair-plan-2026-07-02.md`
   - Agent Team / Runtime Tables / Workspace Documents 分组要求。
3. `docs/session-tui-collaboration-provenance-root-cause-and-repair-plan-2026-07-02.md`
   - 交付物 provenance、模型声明交付物、file_changes 侧通道。
4. `docs/session-rendering-overhaul-plan-2026-07-03.md`
   - 性能渲染优化实施计划。
5. `docs/session-rendering-streaming-cc-codex-gap-analysis-2026-07-03.md`
   - streaming/rendering gap 分析。
