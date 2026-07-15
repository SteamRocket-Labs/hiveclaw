# Session Rendering S6 Completion Plan

日期：2026-07-04

状态：实施前工程计划。本文承接：

- `docs/session-rendering-streaming-cc-codex-gap-analysis-2026-07-03.md`
- `docs/session-rendering-overhaul-plan-2026-07-03.md`
- `docs/session-timeline-projection-contract-2026-07-04.md`

本文只处理剩余的 S6 / 超越项：完整 stable-tail、增量 markdown、真正虚拟化、worker/offscreen、富交互增强。已经上线或已本地完成的性能修复不在本文重复设计。

## 1. 当前基线

截至 2026-07-04 当前 checkout，核心性能修复已经进入可上线状态：

1. `AgentDetail` 已通过 `sessionMessageStore` / `useSyncExternalStore` 读取会话消息，流式 chunk 不再直接把父层 React state 当热路径。
2. `ChatMessageItem` 已提升到 `AgentChatSection.tsx` 模块顶层，并用 `React.memo` 包裹。
3. `AgentChatSection` 默认导出已用 `React.memo` 包裹。
4. `buildThreadTimelineCached` 已存在，并且 messages 引用变化时跳过 signature-only 大对象扫描。
5. `.session-tui-render-cell` 已启用 `content-visibility:auto` / containment；active run cell 已有 `translateZ(0)` 合成层 hint。
6. 后端 `web_chat_runtime._WebChatStreamMicroBatcher` 已做正文 / thinking 微批，稀疏 delta 也有 delayed flush。
7. Session timeline projection 已把 Thinking 作为 run step 保留，完成态只折叠过程，final answer / deliverables 不随过程折叠。

当前没有完成的项：

| 项 | 当前状态 |
|---|---|
| 真正虚拟化 | 未实现；无 `react-window` / `react-virtual` / virtualizer 依赖或入口。 |
| 增量 markdown parser | 未实现；`MarkdownRenderer` 仍对 committed content 做整段 `markdownToHtml(content)`。 |
| Worker / Offscreen | 未实现；无 `new Worker` / `OffscreenCanvas` 入口。 |
| 完整 stable-tail 架构 | 部分实现；已有 timeline cache + cell reuse，但还没有一等 `staticCells / activeTail / sideEffects` projection。 |
| 富交互增强包 | 未作为 S6 新包实现；现有 artifact/tool/runtime 交互保留，但没有系统性 diff viewer / rich inspector / keyboard layer。 |

## 2. 北极星

目标不是把每个旧组件再 memo 一遍，而是把 Session 中央 timeline 做成 Web 形态的 Codex stable-tail：

```text
SessionThreadProjection
  staticCells[]       # 已完成、引用稳定、可虚拟化
  activeTail?         # 当前 streaming / waiting / running 的唯一高频区
  terminalAnswer?     # 完成后的 final answer，独立于过程折叠
  sideEffects         # deliverables / file changes / right rail inputs
  anchors             # message/checkpoint/run ids -> virtual row index
```

任何 token / thinking delta 到来时，只允许：

1. 修改 `activeTail`。
2. 在一帧内合并更新。
3. 不重建 `staticCells`。
4. 不重算已完成 markdown block。
5. 不改变旧 row 的 key、height cache、expanded state。

做到这一步后，真正虚拟化和 worker 才安全。

## 3. 不先做虚拟化的原因

虚拟化不是第一刀。它依赖三个前置条件：

1. **稳定 cell identity**：同一条历史消息 / run process / final answer 必须有稳定 id。否则 virtualizer 会把 row cache 和展开态错配。
2. **稳定 height contract**：run process 展开 / 折叠、markdown block 解析、artifact preview 都会改变高度。没有 stable-tail 和 block cache，测量会不断失效。
3. **稳定 anchor map**：checkpoint jump、scroll-to-bottom、older messages prepend 都需要 `anchor -> row index -> measured offset` 的确定模型。

如果先做虚拟化，只会把滚动跳动、checkpoint 误定位和列表 remount 问题放大。

## 4. 实施顺序

### F1. 完整 Stable-Tail Projection

#### 目标

把 `buildThreadTimelineCached` 从“缓存整个 model”升级为“稳定 projection 分区”：

```ts
export interface SessionThreadProjection {
  staticCells: SessionThreadCell[];
  activeTail: SessionThreadCell | null;
  terminalAnswer: SessionThreadCell | null;
  sideEffects: SessionThreadSideEffects;
  anchors: SessionThreadAnchorMap;
}
```

#### 代码入口

- `frontend/src/pages/session-workbench/timelineModel.ts`
- `frontend/src/pages/session-workbench/timelineModel.test.ts`
- `frontend/src/pages/agent-detail/AgentChatSection.tsx`
- `frontend/src/pages/agent-detail/RunDisclosureBlock.tsx`
- `frontend/src/pages/agent-detail/chatDisclosureReducer.ts`

#### 红灯测试

新增测试覆盖：

1. append streaming chunk 时，旧 `staticCells[i]` 引用保持不变。
2. thinking -> tool -> thinking -> answer 的 projection 顺序保持。
3. final answer 不进入可折叠 run process。
4. artifact / file_changes / right rail 输入来自同一个 projection，不各自猜测。
5. checkpoint anchor map 在 append / prepend 后仍能定位到同一 message id。

建议测试命令：

```bash
cd frontend
npm run test -- timelineModel.test.ts chatRuntime.test.ts RunDisclosureBlock.test.tsx
```

#### 实现要点

1. 新增 `buildSessionThreadProjection(input, cache)`，保留旧 `buildThreadTimelineCached` 作为兼容包装，直到 `AgentChatSection` 完成迁移。
2. `staticCells` 只接收 completed history；active streaming / waiting 只进入 `activeTail`。
3. `terminalAnswer` 从 run process 中拆出，保持正文主叙事地位。
4. `sideEffects` 聚合 deliverables、file changes、right rail document groups。
5. `anchors` 用 message id / transcript event id / checkpoint id 建索引。

#### 验收

1. `staticCells` 引用在流式 append 下稳定。
2. 老消息不随 token re-render。
3. Session projection 语义测试全绿。
4. React Profiler 中 old static row render count 不随 token 增长。

### F2. Incremental Markdown Block Cache

#### 目标

替换当前 `MarkdownRenderer` 的整段重解析模型，把 markdown 拆成稳定 block：

```ts
export interface MarkdownBlock {
  id: string;
  kind: 'paragraph' | 'heading' | 'list' | 'blockquote' | 'code' | 'table' | 'hr';
  source: string;
  html: string;
  complete: boolean;
  hash: string;
}
```

#### 代码入口

- `frontend/src/components/StreamingMarkdown.tsx`
- `frontend/src/components/MarkdownRenderer.tsx`
- 新增 `frontend/src/components/markdown/markdownBlocks.ts`
- 新增 `frontend/src/components/markdown/IncrementalMarkdownRenderer.tsx`
- 新增 `frontend/src/components/markdown/markdownBlocks.test.ts`

#### 红灯测试

1. 同一 completed block 的 `html` 在 tail token append 时不重新生成。
2. 未闭合 code fence 不提交为 completed block。
3. table 在 header / separator / row 不完整时 holdback。
4. streaming tail 只渲染 plain text 或当前 incomplete block。
5. final state 与旧 `MarkdownRenderer` 对常见 markdown 输出等价。

建议测试命令：

```bash
cd frontend
npm run test -- StreamingMarkdown.test.ts markdownBlocks.test.ts
```

#### 实现要点

1. 先保留当前正则 markdown 能力，不引入新 parser 依赖。
2. 以 block 为缓存单位，而不是全 document。
3. 对 code block / table / list 使用 holdback，避免显示半成品结构。
4. `IncrementalMarkdownRenderer` 只让 changed block 进入 React reconciliation。
5. 保留 `MarkdownRenderer` 作为 fallback，便于逐步迁移。

#### 验收

1. 长回答每新增一行时，只新增 / 更新 tail block。
2. 已完成 block 的 DOM 不重建。
3. Markdown snapshot 测试与现有渲染等价。
4. 没有 XSS 回退；仍走现有 escape / inline render 边界。

### F3. True Virtual Timeline

#### 目标

中央 Session timeline 使用真正 virtual list，而不是只靠 `content-visibility`：

```text
visible rows ~= viewport rows + overscan
total rows can be 500 / 2000 / 10000
DOM row count remains bounded
```

#### 依赖选择

优先选 `@tanstack/react-virtual`，理由：

1. 项目已经使用 TanStack React Query。
2. 支持 variable height measurement。
3. 对动态高度、scrollToIndex、overscan 控制足够直接。

新增依赖前必须单独说明 package size 与替代方案。若 owner 不接受新依赖，可先实现极简内部 virtualizer，但不建议手写完整 variable-height virtualizer。

#### 代码入口

- 新增 `frontend/src/pages/agent-detail/SessionVirtualTimeline.tsx`
- 新增 `frontend/src/pages/agent-detail/SessionVirtualTimeline.test.tsx`
- `frontend/src/pages/agent-detail/AgentChatSection.tsx`
- `frontend/src/pages/session-workbench/timelineModel.ts`

#### 红灯测试

1. 1000 rows 输入时实际 render row 数低于固定上限。
2. `scrollToBottom` 能定位到最后一条 active tail。
3. `scrollToCheckpoint(checkpointId)` 能定位对应 anchor。
4. prepend older messages 后当前可视 anchor 不跳。
5. run process 展开 / 折叠后重新 measure，不破坏后续滚动。

建议测试命令：

```bash
cd frontend
npm run test -- SessionVirtualTimeline.test.tsx AgentDetailSections.test.tsx timelineModel.test.ts
```

#### 实现要点

1. 只虚拟化中央 timeline，不虚拟化 composer 和右栏。
2. active tail 可以常驻末尾，也可以作为 virtual row，但必须优先保持 streaming 稳定。
3. `cell.id` 是 virtual row key；禁止 index key。
4. 维护 `anchor -> row index` map，由 F1 projection 产出。
5. 对 older message prepend 使用 anchor retention：记录 prepend 前第一可见 anchor，prepend 后 scroll 回同一 anchor offset。
6. `content-visibility` 保留为低端设备的附加防线，但不再把它当主虚拟化机制。

#### 验收

1. 2000 条消息 DOM row 数保持在可控范围。
2. 快速 streaming 时输入框无明显卡顿。
3. checkpoint 跳转、branch/rewind 定位、scroll bottom 均稳定。
4. 浏览器 Performance trace 中 layout / paint 明显下降。

### F4. Worker Offload

#### 目标

把真正重的纯计算从主线程挪出去：

1. markdown block parsing
2. large diff summary
3. large tool output clipping / syntax highlighting

#### 非目标

OffscreenCanvas 暂不作为第一目标。当前瓶颈主要是 DOM / markdown / text processing，不是 canvas drawing。

#### 代码入口

- 新增 `frontend/src/workers/markdown.worker.ts`
- 新增 `frontend/src/components/markdown/markdownWorkerClient.ts`
- 后续新增 `frontend/src/workers/diff.worker.ts`

#### 红灯测试

1. worker client 在 worker 不可用时 fallback 到 main-thread parser。
2. out-of-order worker response 不覆盖更新的 tail。
3. cancel / session switch 后旧 parse result 被丢弃。
4. 大 markdown parse 不阻塞输入事件。

建议测试命令：

```bash
cd frontend
npm run test -- markdownWorkerClient.test.ts markdownBlocks.test.ts
```

#### 实现要点

1. 使用 request id / content hash 做响应匹配。
2. Worker payload 只传 plain data，不能传 React node。
3. 对 streaming tail 设置最大 pending queue，避免低端机器积压。
4. 出错时 fallback，并记录 debug event，不能让消息空白。

#### 验收

1. 超长 markdown / tool output 不产生明显 long task。
2. Worker failure 不影响消息显示。
3. Session switch 不出现旧内容回写。

### F5. Rich Interaction Package

#### 目标

在 projection 和 virtual timeline 稳定后，补 Web 相对 TUI 的富交互优势：

1. artifact preview drawer
2. file changes diff viewer
3. command output head / tail / expand
4. tool result structured tree
5. worker / subagent detail drawer
6. keyboard navigation
7. reduced-motion / aria-live support

#### 代码入口

- `frontend/src/pages/agent-detail/AgentChatSection.tsx`
- `frontend/src/pages/agent-detail/RunDisclosureBlock.tsx`
- `frontend/src/pages/agent-detail/*Artifact*`
- `frontend/src/pages/session-workbench/timelineModel.ts`
- `frontend/src/pages/agent-detail/SessionRuntimePanel` 相关区域

#### 红灯测试

1. artifact row keyboard open / download。
2. diff viewer 展开不影响 virtual row anchor。
3. command output expand/collapse 不重建整个 timeline。
4. worker inspect drawer 打开时不触发 session remount。
5. `prefers-reduced-motion` 下动画禁用或降级。

建议测试命令：

```bash
cd frontend
npm run test -- AgentDetailSections.test.tsx RunDisclosureBlock.test.tsx timelineModel.test.ts
npm run test:e2e -- session-timeline-rich-interactions.spec.ts
```

#### 验收

1. 富交互只作用于对应 row / drawer。
2. 长会话下展开 diff / artifact preview 不造成主线程长卡顿。
3. 键盘和 screen reader 路径可用。

## 5. 总体验收矩阵

| 验收项 | 目标 |
|---|---|
| Render count | token streaming 不触发旧 static row render。 |
| DOM size | 2000 条消息下中央 timeline DOM row 数有上限。 |
| Scroll stability | scroll bottom、checkpoint jump、older prepend、run expand/collapse 稳定。 |
| Markdown cost | 已完成 markdown block 不随 tail token 重解析。 |
| Main thread | 超长 markdown / diff 不产生明显 long task。 |
| Projection correctness | Thinking -> tool -> Thinking -> answer 顺序保持；final answer 不被过程折叠。 |
| Artifact semantics | deliverables / file changes / workspace rail 来自同一 projection。 |
| Accessibility | reduced motion、keyboard open、aria labels 可用。 |

建议固定验证命令：

```bash
cd frontend
npm run test -- timelineModel.test.ts chatRuntime.test.ts RunDisclosureBlock.test.tsx sessionMessageStore.test.ts sessionRenderingPerformance.test.ts StreamingMarkdown.test.ts
npm run build
```

浏览器验收必须包含：

1. 500+ messages 的长会话。
2. 快速 streaming answer。
3. thinking / tool / artifact / file_changes 混合 turn。
4. checkpoint jump。
5. older messages prepend。
6. run process expand/collapse。
7. artifact preview / download。

## 6. 上线策略

每个 F 包都必须是完整闭环，不允许只落半套结构：

1. 红灯测试先行。
2. 实现。
3. focused tests。
4. `npm run build`。
5. 浏览器 trace / screenshot 验收。
6. Railway production archive-root 部署前，确认 working tree 没有其它 session 的无关改动；如有，必须使用 clean overlay/archive-root。

推荐 release order：

```text
Release A: F1 stable-tail projection
Release B: F2 incremental markdown block cache
Release C: F3 virtual timeline
Release D: F4 worker offload
Release E: F5 rich interaction package
```

Release A 和 B 可以连续开发，但不能绕过各自测试验收。Release C 之前必须确认 F1/F2 在生产长会话中稳定。

## 7. 风险与裁决

### 7.1 新依赖风险

`@tanstack/react-virtual` 是 F3 的推荐依赖，但不是无条件加入。加入前需要：

1. bundle impact 记录。
2. variable-height + scrollToIndex demo 测试。
3. 和手写 virtualizer 的维护成本对比。

### 7.2 Markdown parser 风险

不能在 F2 直接替换成大而全 markdown parser，除非证明：

1. 安全边界不退化。
2. bundle impact 可接受。
3. 当前 markdown feature parity 被测试覆盖。

优先做 block cache + 当前 parser 兼容层。

### 7.3 Worker 风险

Worker 是优化，不是 truth source。主线程 fallback 必须永远可用。Worker result 必须用 request id / content hash 防止旧结果覆盖新结果。

### 7.4 Virtualization 风险

虚拟化最容易破坏用户信任的地方不是性能，而是位置：

1. 滚动条跳。
2. checkpoint 定位错。
3. 展开后内容消失。
4. prepend older messages 后视野跳走。

这些必须由测试和浏览器 trace 双重验收。

## 8. Done Definition

S6 完整完成的定义：

1. Session timeline 有一等 stable-tail projection。
2. Markdown 已按 block 增量缓存，旧 block 不随 tail 更新重解析。
3. 中央 timeline 真正虚拟化，长会话 DOM row 有上限。
4. 大 markdown / diff / tool output 可通过 worker offload，且 fallback 正常。
5. 富交互增强在 virtual timeline 内稳定可用。
6. 所有 projection 语义仍满足 `session-timeline-projection-contract-2026-07-04.md`。
7. 浏览器实测长会话流式、滚动、checkpoint、artifact、展开折叠均通过。

只有满足以上条件，才能称为“完整 S6 超越项完成”。此前的 `content-visibility`、timeline cache、rAF/store 合帧属于性能修复达标与 S6 前置铺垫，不等同于完整 S6。
