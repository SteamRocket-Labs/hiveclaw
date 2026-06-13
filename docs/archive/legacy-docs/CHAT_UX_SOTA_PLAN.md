# Chat UI/UX SOTA 全面重构计划 (v2.0)

> 状态: v2.0 全面重构计划，替代 v1.0 "交互可见性"草案
> 日期: 2026-05-28
> 范围: Hive 主 Chat 前端体验、交互状态、组件体系、事件契约、deep research 合流、运行态可观测性
> 对标: `/Users/rocky243/vc-saas/onyx` 的 chat timeline / packet processor / AgentTimeline / pacing / completed state
> 目标: 不是修一个 Thinking indicator，而是把 Hive Chat 从调试型界面重构成企业用户可放心使用的 agent 工作台

---

## 0. 这次重构的真实性质

当前 Hive Chat 的问题不是"样式不好看"，而是**产品交互模型不成立**。用户给 agent 发出任务后，中间区域长期处于黑箱状态：不知道 agent 是否在工作、在调用什么工具、是否卡住、是否等待权限、是否还会继续输出、deep research 到了哪一步。对于企业经理、运营、HR、市场这类非工程用户，这等于"把工作交给一个没有回执的系统"。

所以本计划不是局部 UI polish，而是一次大型、颠覆性、分层推进的 Chat UI/UX 重构：

```text
当前模式: 消息气泡 + 静态 Thinking + 隐藏 tool trace
目标模式: 事件驱动 Agent 工作台 + 统一 Timeline + 明确状态 + 可复盘执行过程
```

设计北极星：

1. **Every agent action is visible enough to trust.** agent 的工作必须在屏幕上留下可理解、可折叠、可复盘的轨迹。
2. **普通对话和 deep research 共用同一套 timeline。** deep research 不是另一个 panel，也不是 debug 附属视图。
3. **状态永不留白。** sent、waiting、thinking、tool running、streaming、reconnecting、continuing、blocked、done 每一态都必须有明确 UI。
4. **前端不猜文本。** UI 由类型化事件驱动，和 onyx 一样把 packet/event contract 当成渲染契约。
5. **企业用户优先。** 体验应接近 Notion/Slack 的清晰协作感，叠加 Vercel/Raycast 式精致和响应速度；不是 hacker/debug console。

---

## 1. 当前事实核实

### 1.1 已核实的 Hive 事件流

Hive 后端已经通过 web chat runtime 发送了关键运行事件：

| 事件 | 现有后端路径 | 现有前端状态 |
|---|---|---|
| `run_started` | `backend/app/services/web_chat_runtime.py` | 只用于内部 active run 状态 |
| `thinking` | `backend/app/services/chat_message_parts.py::build_thinking_event` | 进入 assistant 空消息或折叠 Thinking 卡 |
| `chunk` | `build_chunk_event` | 已可流式进入 assistant 气泡 |
| `tool_call` running | `backend/app/kernel/engine.py` 执行工具前发 | 已进入 React state，但默认隐藏 |
| `tool_call` done | `engine.py` 工具完成后发 | 已进入 React state，但默认隐藏 |
| `permission` | `build_permission_event` | 事件卡片，未进入统一工作流 |
| `session_compact` | `build_compaction_event` | 事件卡片，未进入统一工作流 |
| `pack_activation` | `build_active_packs_event` | 事件卡片，未进入统一工作流 |
| `done` | `build_done_event` | 收尾 assistant 气泡 |
| deep research SSE | `backend/app/api/deep_research.py` | 独立 panel，和主 timeline 分叉 |

关键结论：**黑箱主要是前端呈现层问题，不是后端完全没有进度事件。**

### 1.2 当前前端的根本问题

已核实代码：

- `frontend/src/pages/agent-detail/AgentChatSection.tsx:331` 默认 `showInternalTrace=false`
- `AgentChatSection.tsx:715-717` 对 `tool_call` 做默认隐藏
- `AgentDetail.tsx:685-688` 收到 `thinking/chunk/tool_call` 后立刻 `setIsWaiting(false)`，纯工具长轮次会出现静止空窗
- `DeepResearchStreamPanel.tsx` 只在 deep research tool result 内部挂载，且外层 tool card 又被 trace 开关影响
- `AgentChatSection.tsx` 与 `AgentDetail.tsx` 都是巨型文件，状态、渲染、会话列表、composer、runtime summary 混在一起

这导致四个用户可感知失败：

1. agent 做事时屏幕没有可信进度。
2. 工具调用和权限状态被当成 internal trace，而不是用户应该看到的工作过程。
3. deep research 拥有更丰富的事件，但被分叉成孤立 panel，无法成为主 Chat 体验的一部分。
4. Chat 组件不可维护，继续堆 inline 条件会让重构不可控。

### 1.3 已有可复用资产

不需要推倒全部重写。以下资产可复用：

- `chatRuntime.ts` 已有 stored message / runtime event normalization 基础。
- `toolResultEnvelope.ts` 已能识别 `deep_research_*`、HR preview、create employee 等结构化结果。
- `renderToolCall` 已有 tool card 雏形，只是默认不显示。
- `DeepResearchStreamPanel` 和 `useDeepResearchStream` 已有 SSE parsing / reducer 基础，可迁入统一 timeline。
- 后端 `RuntimeTask(task_type="web_chat_turn")` 已把 web chat run 从 WebSocket 生命周期中解耦，前端可以围绕 durable run 做 continuing/reconnect 状态。

---

## 2. Onyx 对标目标

### 2.1 要学什么

Onyx 的 SOTA 不只是样式，而是完整运行模型：

| Onyx 能力 | 关键文件 | Hive 目标 |
|---|---|---|
| 类型化 packet union | `web/src/app/app/services/streamingModels.ts` | 建立 Hive `ChatStreamEvent` / `TimelineEvent` union |
| 增量 packet processor | `timeline/hooks/packetProcessor.ts` | 建立纯函数 reducer，把 WS/SSE 事件折叠为 turn/step |
| 统一 AgentTimeline | `timeline/AgentTimeline.tsx` | 所有 runtime events 共用一个 timeline |
| 状态机 | `timeline/hooks/useTimelineUIState.ts` | 明确 EMPTY / STREAMING / STOPPED / COMPLETED |
| pacing | `timeline/hooks/usePacedTurnGroups.ts` | step 间节奏、最终答案延后展示、历史回放跳过 |
| 完成折叠头 | `timeline/headers/CompletedHeader.tsx` | "思考 18s · 4 步"，可展开复盘 |
| step 容器 | `timeline/StepContainer.tsx` | 统一 rail、icon、连接线、内容展开 |
| citation/source | `components/search/results/Citation.tsx` | 后续支持答案引用、来源 hover 卡 |

### 2.2 不能机械照搬什么

Hive 不是 onyx 的搜索产品。Hive 是企业数字员工控制平面，所以不能只复制搜索 timeline：

- Hive 必须显示 `permission`、`session_compact`、`pack_activation`、team memory、capability gate。
- Hive 有 durable run、session reconnect、active run continuation，这些比 onyx 普通 streaming 更复杂。
- Hive 有 agent-to-agent / read-only session，需要明确谁在说话、是否可回复、当前用户是否能操作。
- Hive 的工具域更多：office、Feishu、memory、create employee、deep research、files、workspace、MCP。

因此目标是：**借 onyx 的 timeline 架构和交互节奏，做 Hive 自己的 Agent Workbench Chat。**

---

## 3. 产品体验目标

### 3.1 用户画像

核心用户不是开发者，而是每天使用数字员工推进工作的企业成员：

- 管理者：查看 agent 是否可靠完成任务。
- HR / 运营 / 市场：让 agent 写文档、查资料、跟进流程、创建员工、连接渠道。
- 管理员：关注权限、预算、模型、工具和审计。

他们不应该看到"内部 trace"这个概念。他们需要看到的是：**这个 agent 像一个同事一样，正在做什么、为什么停住、接下来要我做什么。**

### 3.2 体验性格

沿用 `.impeccable.md` 的设计上下文：

- 智能、前沿、精致。
- Notion/Slack 的清晰协作感。
- Vercel/Raycast 的精确状态和细节质感。
- light mode 应该是一等体验，dark mode 是 polished alternative。

Chat 区域的密度策略：

- 对话正文要呼吸，不做密集 dashboard。
- 运行轨迹要可扫读，不做大段 JSON/debug dump。
- 高级细节可展开，但主路径永远给清楚状态。

---

## 4. 目标信息架构

### 4.1 Chat 页面应拆成 5 个区域

```text
AgentChatShell
├─ SessionSidebar
│  ├─ scope tabs: 我的会话 / 全部会话
│  ├─ session search/filter (P2)
│  └─ session rows with unread/running/error indicators
├─ ChatHeader
│  ├─ agent identity
│  ├─ connection / active run / model / context state
│  └─ actions: new session, settings shortcut, trace/detail toggle if needed
├─ ConversationViewport
│  ├─ MessageTurn(user)
│  ├─ MessageTurn(assistant)
│  │  ├─ AgentTimeline
│  │  └─ AssistantAnswer
│  └─ scroll / unread / reconnect affordances
├─ RuntimeStatusBar
│  ├─ continuing run
│  ├─ reconnecting
│  ├─ permission blocked
│  └─ active tool summary
└─ ComposerBar
   ├─ attachment tray
   ├─ textarea
   ├─ send / stop
   └─ disabled-state reason
```

### 4.2 组件拆分目标

当前 `AgentChatSection.tsx` 不应继续承载全部逻辑。目标拆分：

| 新组件/模块 | 职责 |
|---|---|
| `AgentChatShell.tsx` | chat tab 总布局和区域组合 |
| `SessionSidebar.tsx` | 会话列表、scope、loading/empty/error |
| `ConversationViewport.tsx` | 消息滚动、scroll-to-bottom、read-only banner |
| `MessageTurn.tsx` | 单轮用户/助手消息容器 |
| `AgentTimeline.tsx` | 统一运行轨迹 UI |
| `TimelineStep.tsx` | 单个 step 的 icon、状态、内容 |
| `TimelineCompletedHeader.tsx` | 完成折叠态 |
| `TimelineStreamingHeader.tsx` | 当前活态状态文字和计时 |
| `ComposerBar.tsx` | 输入、附件、发送、停止、禁用原因 |
| `chatTimeline.ts` | 纯 reducer 和事件映射 |
| `chatTimelineLabels.ts` | 工具名/事件到用户可读文案 |
| `chatTimelinePacing.ts` | pacing hook 或纯调度逻辑 |

原则：渲染组件只读 `TimelineSnapshot`，不要直接理解 WebSocket 原始事件。

---

## 5. 目标事件契约

### 5.1 前端内部 union

先不要求后端一次性重做成 onyx packet。前端先建立内部稳定契约：

```ts
type ChatStreamEvent =
  | { type: 'run_started'; runId: string; status: string; timestamp: string }
  | { type: 'thinking_delta'; text: string; timestamp: string }
  | { type: 'message_delta'; text: string; timestamp: string }
  | { type: 'tool_started'; toolName: string; args: Record<string, unknown>; timestamp: string }
  | { type: 'tool_completed'; toolName: string; args: Record<string, unknown>; result: unknown; timestamp: string }
  | { type: 'runtime_event'; eventType: 'permission' | 'session_compact' | 'pack_activation' | 'team_memory'; payload: unknown; timestamp: string }
  | { type: 'deep_research_event'; event: DeepResearchStreamEvent; timestamp: string }
  | { type: 'round_start'; index: number; max: number; timestamp: string }
  | { type: 'round_pressure'; used: number; max: number; level: 'info' | 'warning' | 'critical'; timestamp: string }
  | { type: 'done'; content: string; thinking?: string; timestamp: string }
  | { type: 'error'; message: string; timestamp: string }
  | { type: 'cancelled'; timestamp: string };
```

### 5.2 Timeline snapshot

`chatTimeline.ts` 输出一个不依赖 React 的结构：

```ts
interface TimelineSnapshot {
  runId: string | null;
  state: 'empty' | 'waiting' | 'streaming' | 'blocked' | 'completed' | 'cancelled' | 'failed';
  startedAt: string | null;
  completedAt: string | null;
  activeStepId: string | null;
  steps: TimelineStepSnapshot[];
  answer: {
    content: string;
    isStreaming: boolean;
  };
}

interface TimelineStepSnapshot {
  id: string;
  kind:
    | 'thinking'
    | 'tool'
    | 'permission'
    | 'compaction'
    | 'pack_activation'
    | 'deep_research'
    | 'round'
    | 'answer';
  status: 'pending' | 'running' | 'done' | 'blocked' | 'failed' | 'cancelled';
  title: string;
  subtitle?: string;
  startedAt?: string;
  completedAt?: string;
  durationMs?: number;
  args?: Record<string, unknown>;
  resultPreview?: string;
  children?: TimelineStepSnapshot[];
}
```

### 5.3 事件映射原则

- `tool_call running` 和 `tool_call done` 必须更新同一个 step，不能重复显示两张卡。
- `thinking` 不再作为孤立 assistant 空消息，而是 timeline step。
- `chunk` / `done` 进入 answer 区域，最终答案仍是主内容。
- `permission` 是用户可见状态，不是 internal trace。
- deep research SSE 事件进入 `children`，成为 `deep_research` step 的嵌套进度。
- 历史消息加载时必须能从 persisted messages 还原 timeline，至少还原 tool/event/done 级别。

---

## 6. 核心状态矩阵

| 状态 | 用户看到什么 | 主要触发 |
|---|---|---|
| Empty session | 清楚的起始提示 + composer 可用 | 无消息 |
| Sending | 用户消息立即出现，composer 进入 stop 模式 | 用户点击发送 |
| Waiting first event | avatar + "正在启动..." shimmer，不能空白 | `start_web_chat_run` 成功但还没 WS event |
| Thinking | "正在思考..." + 计时，可展开推理摘要 | `thinking` |
| Tool running | "正在搜索/读取/创建/调用..." + 参数摘要 + 计时 | `tool_call running` |
| Tool done | step 变完成，显示结果摘要，可展开详细结果 | `tool_call done` |
| Permission blocked | 明确说明需要审批/缺权限，给下一步入口 | `permission status=approval_required/blocked` |
| Streaming answer | timeline 可折叠，答案打字机输出 | `chunk` |
| Continuing run | 页面恢复后显示"此会话仍在执行" + 当前已知步骤 | active run poll |
| Reconnecting | 顶部/状态条显示正在重连，不清空消息 | WS close + retry |
| Cancelled | timeline 标记用户停止，保留已有步骤 | `run_cancelled` |
| Failed | 错误卡 + 可重试/可复制错误 | `error/quota_exceeded` |
| Completed | "思考 Xs · N 步" 折叠头 + 最终答案 | `done` |
| Read-only | 清楚说明来源和只读原因，隐藏 composer | 其他用户/agent channel session |

验收要求：任何状态都不能出现"用户不知道系统是否还活着"的空窗。

---

## 7. UI 设计规范

### 7.1 Timeline 视觉模型

目标不是 debug log，而是 agent 的工作轨迹：

```text
助手
  思考 18s · 4 步                         [展开/收起]
  ─────────────────────────────────────
  ✓ 搜索网页          "onyx chat UI"       2.1s
  ✓ 读取来源          6 个来源             4.8s
  ✓ 深度研究          3 lanes · 12 claims  9.4s
    ├─ 规划研究问题
    ├─ 收集来源
    └─ 生成中间结论
  ✓ 撰写答案                               1.7s

  最终答案正文...
```

### 7.2 主路径和高级细节

- 默认显示：step 标题、参数摘要、状态、计时、结果摘要。
- 展开后显示：完整 tool args、structured result、deep research artifacts、permission reason、compaction details。
- 不再使用"Show internal trace"作为主门控。可以保留"详细信息"开关，但它只影响 debug 级字段，不影响工作过程可见性。

### 7.3 文案标准

状态文字必须是业务用户能理解的动作：

| 工具/事件 | 文案 |
|---|---|
| `web_search` / `search` | 正在搜索网页 |
| `web_fetch` / `open_url` | 正在读取网页 |
| `read_file` | 正在读取文件 |
| `write_file` / `edit_file` | 正在修改文件 |
| `office_document_*` | 正在处理文档 |
| `send_feishu_message` | 正在准备飞书消息 |
| `save_memory` | 正在保存记忆 |
| `deep_research_start/run` | 正在深度研究 |
| `create_digital_employee` | 正在创建数字员工 |
| `permission` | 需要确认 |
| `session_compact` | 已整理上下文 |
| `pack_activation` | 已启用能力包 |

严禁把工具名裸露给普通用户作为唯一标题；工具名可以放在展开详情里。

### 7.4 Composer 体验

Composer 必须成为可预测的任务入口：

- Enter 发送，Shift+Enter 换行。
- 上传中、agent 运行中、断线中都要有明确禁用原因。
- Send 和 Stop 不应造成布局跳动。
- 附件 tray 必须显示上传进度、失败、可移除状态。
- active run 时 composer 可以禁用，但必须给"停止当前任务"入口。

---

## 8. Deep Research 合流

### 8.1 当前问题

deep research 当前路径是：

```text
tool result kind=deep_research
  -> StructuredToolResultBody
    -> DeepResearchStreamPanel
      -> useDeepResearchStream
        -> GET /api/deep-research/stream/{agent_id}/{task_id}
```

这导致 deep research 体验与普通 chat 分叉。用户看到的是一个嵌在 tool result 里的小面板，而不是 agent 主工作流。

### 8.2 目标形态

保留 SSE 作为数据源，但 UI 必须合流：

```text
deep_research tool_started
  -> TimelineStep(kind='deep_research', status='running')
    -> SSE step/source_note/claim/lane_summary/reflection/controller_trace
      -> nested children / counters / report preview
```

### 8.3 分阶段迁移

P1:

- `DeepResearchStreamPanel` 仍可存在，但外层 tool step 默认可见。
- 不再被 `showInternalTrace` 阻断。

P2:

- 新建 `DeepResearchTimelineBridge`，把 `useDeepResearchStream` 输出转换成 `TimelineStepSnapshot.children`。
- `DeepResearchStreamPanel` 改造成 `DeepResearchStepDetails`，只作为 timeline step 的展开详情。

P3:

- 评估是否把 SSE 事件物理合并进 WebSocket。
- 默认不急着合并，因为 durable/reconnect 语义风险高。

---

## 9. 后端契约补齐

P0/P1 原则上不改后端。P2 需要补两个低风险 runtime events：

| 新事件 | 位置 | 用途 |
|---|---|---|
| `round_start { index, max }` | `backend/app/kernel/engine.py` 每个 tool round 开始 | timeline 显示第几轮，避免长循环无解释 |
| `round_pressure { used, max, level }` | 目前只注入 LLM system warning 的位置 | 用户知道 agent 接近轮次上限 |

后续 P3 可选：

| 新事件 | 用途 |
|---|---|
| `tool_args_delta` | 类似 onyx `tool_call_argument_delta`，让工具参数流式出现 |
| `source_delta` | 搜索/读取结果增量显示 |
| `citation_info` | 答案内联引用 |
| `placement` | 支持 parallel tabs / nested sub-agent layout |

后端边界：不要在没有专项评审前改 WS/SSE 传输层拓扑。

---

## 10. 分期实施路线

### P0 - 止血：让 Chat 立刻人类可用

周期：0.5-1 天  
风险：低  
后端：不动

改动：

1. 新增 `chatTimelineLabels.ts`，集中处理 tool/event 到用户文案的映射。
2. 修复 `AgentDetail.tsx` active run UI 状态：`tool_call/thinking` 不应让用户看到空窗。
3. `tool_call` 默认可见，不再被 `showInternalTrace=false` 整体隐藏。
4. waiting indicator 读取最近 running tool，显示具名状态和计时。
5. deep research tool card 至少默认可见。

测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm run test -- src/pages/agent-detail/chatRuntime.test.ts
npm run build
```

验收：

- 搜索/读文件/深度研究期间屏幕始终有具名进度。
- 纯工具长轮次不再出现静止空白。
- 用户不需要打开 internal trace 才知道 agent 在工作。

### P1 - Functional Core：建立 Timeline reducer

周期：1-2 天  
风险：中  
后端：不动

新增：

- `frontend/src/pages/agent-detail/chatTimeline.ts`
- `frontend/src/pages/agent-detail/chatTimeline.test.ts`
- `frontend/src/pages/agent-detail/chatTimelineLabels.ts`

实现：

1. 把 WS payload normalize 成 `ChatStreamEvent`。
2. 把事件序列 reduce 成 `TimelineSnapshot`。
3. 合并 running/done tool step。
4. thinking、permission、compact、pack activation 全部进入 step。
5. 从 persisted history 恢复基本 timeline。

测试必须先写：

```text
tool_call running -> 生成 running step
tool_call done -> 更新同一 step 为 done
thinking delta -> 生成/更新 thinking step
permission blocked -> timeline state=blocked
chunk/done -> answer content 正确聚合
stored tool_call/event messages -> 可恢复 snapshot
```

验收：

- reducer 是纯函数，无 React mock。
- 真实录制 WS 事件序列能生成稳定 snapshot。

### P2 - AgentTimeline 组件化

周期：2-3 天  
风险：中高  
后端：不动

新增/拆分：

- `AgentTimeline.tsx`
- `TimelineStep.tsx`
- `TimelineStreamingHeader.tsx`
- `TimelineCompletedHeader.tsx`
- `ConversationViewport.tsx`
- `MessageTurn.tsx`

实现：

1. `AgentChatSection` 不再直接渲染 tool/event/thinking 的细节。
2. assistant turn 渲染为：`AgentTimeline` + `AssistantAnswer`。
3. timeline 支持 streaming、completed collapsed、completed expanded、failed、cancelled。
4. 完成态默认折叠为"思考 Xs · N 步"。
5. 详细结果进 step 展开区。

测试：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm run test -- src/pages/agent-detail/chatTimeline.test.ts
npm run test -- src/pages/agent-detail/AgentTimeline.test.tsx
npm run build
```

验收：

- 普通 chat 与 tool-heavy chat 使用同一组件。
- 完成后不永久占屏，但可展开复盘。
- 没有 `showInternalTrace` 阻断主工作过程。

### P3 - Deep Research 合流 + pacing

周期：3-4 天  
风险：高  
后端：可选补事件

实现：

1. `DeepResearchTimelineBridge` 把 SSE state 映射为 nested timeline children。
2. `DeepResearchStreamPanel` 改为 step details，不再是独立主面板。
3. 引入 pacing：首 step 即时，其余 step 约 200ms 间隔展示。
4. 最终答案在工具 step pacing 完成前暂缓显示，历史回放跳过 pacing。
5. reasoning/thinking 最小展示时长，避免闪烁。

验收：

- long deep research 全程在主 timeline 可见。
- sources、claims、lanes、reflections 不再散落在独立 panel。
- 信息不会瞬间倾倒，用户能读懂 agent 的工作节奏。

### P4 - 全 Chat Shell 重构

周期：4-6 天  
风险：高  
目标：从"agent detail tab 里的聊天区"升级为真正工作台。

实现：

1. 抽出 `SessionSidebar`，优化会话列表 loading、running、error、read-only 状态。
2. 抽出 `ChatHeader`，显示 agent、channel、connection、model/context summary。
3. 抽出 `ComposerBar`，统一附件、禁用原因、stop/send 布局。
4. 加 session search/filter 和 running session 标识。
5. 移除大部分 inline style，沉淀 chat 局部 CSS class。

验收：

- `AgentChatSection.tsx` 显著瘦身，只做 composition。
- 状态逻辑不再散落在 1000+ 行组件里。
- light/dark mode 都可用，移动宽度下不丢关键功能。

### P5 - Onyx plus：source、citation、parallel、audit

周期：后续专项  
风险：中高

能力：

- citation/source hover card。
- parallel tool tabs。
- placement 坐标。
- tool result diff/preview。
- permission checkpoint 直接操作。
- audit trail 与 timeline step 关联。

---

## 11. 测试策略

### 11.1 TDD 规则

逻辑改动必须先写测试：

- reducer 先测再实现。
- component state 先测再实现。
- bug fix 先写复现用例。

文档改动不需要 TDD。

### 11.2 测试分层

| 层级 | 文件 | 覆盖 |
|---|---|---|
| pure reducer | `chatTimeline.test.ts` | event sequence -> snapshot |
| label mapping | `chatTimelineLabels.test.ts` | tool args -> 用户文案 |
| component | `AgentTimeline.test.tsx` | EMPTY/STREAMING/BLOCKED/DONE 渲染 |
| deep research bridge | `DeepResearchTimelineBridge.test.ts` | SSE events -> nested steps |
| integration | `AgentChatSection.test.tsx` | tool_call 默认可见、waiting 不空白 |
| existing regression | `chatRuntime.test.ts` | stored message normalization |

### 11.3 验证命令

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm run test -- src/pages/agent-detail/chatRuntime.test.ts
npm run test -- src/pages/agent-detail/chatTimeline.test.ts
npm run test -- src/pages/agent-detail/AgentTimeline.test.tsx
npm run build
```

后端补事件时追加：

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/services/test_web_chat_runtime.py tests/api/test_chat_session_runs.py tests/api/test_websocket_call_llm.py -q
ruff check app/kernel/engine.py app/services/web_chat_runtime.py app/services/chat_message_parts.py
```

### 11.4 手动验收脚本

必须覆盖：

1. 普通一句话问答。
2. web search -> fetch -> answer。
3. read file -> analyze -> answer。
4. deep research start -> streaming artifacts -> final report。
5. permission required / blocked。
6. context compaction。
7. 页面关闭后 30-60 秒重开同一 session，显示 continuing run。
8. WebSocket 断开重连。
9. 用户点击 Stop。
10. 只读 agent-to-agent session。

---

## 12. 成功标准

### 12.1 用户可感知标准

重构完成后，用户必须能回答：

- agent 现在是否还在工作？
- 它正在做哪一步？
- 它用了什么工具或来源？
- 它为什么停住？
- 我是否需要审批、重试、等待或停止？
- 完成后我能不能复盘它做过什么？

### 12.2 工程标准

- `AgentChatSection.tsx` 不再是巨型渲染泥潭。
- timeline reducer 是纯函数，有高覆盖单测。
- 新 runtime event 不会被前端静默吞掉。
- deep research 不再独立成另一套主 UI。
- 默认路径不再出现 internal trace 门控。
- build 和核心 vitest 通过。

### 12.3 对标标准

达到 onyx 的这些体验能力：

- 类型化事件驱动 UI。
- 统一 timeline。
- 活态状态头。
- completed collapsed summary。
- pacing。
- deep research 嵌套步骤。
- 工具/来源/引用逐步可视化。

同时超过 onyx 的 Hive 特有能力：

- permission / capability gate 可见。
- context compaction 可见。
- pack activation 可见。
- durable run continuation 可见。
- enterprise control-plane 状态可见。

---

## 13. 风险与边界

### 13.1 最大风险

1. **在巨型组件里继续小修小补。** 这会让 P0 看似快，P1/P2 崩掉。
2. **把 debug trace 当用户体验。** 用户要看工作过程，不要看 JSON dump。
3. **过早改后端传输层。** WS/SSE 合并会碰 durable/reconnect，必须后置。
4. **忽视历史消息恢复。** 如果刷新后 timeline 消失，信任感仍然断裂。
5. **只做 dark mode。** 企业用户的主体验应该保证 light mode 一等可用。

### 13.2 不做什么

- P0/P1 不改 WS/SSE 拓扑。
- 不引入新 UI framework。
- 不做可配置状态文案 DSL。
- 不把所有 tool raw result 默认展开。
- 不为了像 onyx 而移除 Hive 的权限/记忆/控制平面状态。

---

## 14. 建议立即执行的第一批改动

第一批不要直接做完整视觉重构，先做一个可验证的垂直切片：

1. 写 `chatTimeline.test.ts`，定义 `tool_call running/done`、`thinking`、`chunk/done` 的 snapshot。
2. 实现 `chatTimeline.ts` reducer。
3. 让 `AgentChatSection` 使用 snapshot 显示最小 timeline。
4. 修复 `isWaiting` 空窗。
5. 默认显示 tool step。
6. 跑 vitest + build。

这条切片能证明架构成立，也能立刻解决"人类不可用"的核心痛点。
