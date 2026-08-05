# Hive 活对象与原生 Surface 架构规格

> 首版日期：2026-07-10
> 最近修订：2026-07-15（纳入 Codex-style 开放表达沙箱与云端 SaaS 安全边界）
> 状态：架构权威面候选；本文件定义完整目标，不代表相关实现已经落地
> 适用范围：Hive Web Chat、Session Workbench、Personal Knowledge、Company Knowledge、A2A 协作、Office 导入导出与第三方交互式 UI
> 当前对齐基线：Hive checkout `fb8c92b9f33429aed4e7851398a22d3687c178ff`；Codex Desktop `26.707.72221` / Visualize plugin `1.0.11`；外部依赖版本仍以各专项实施前重新实核为准
> 交付纪律：后续实现必须单轮闭环交付；不采用 MVP、分期欠债或默认关闭的半成品能力

---

## 0. 最终决策

Hive 不应把下一代内容呈现理解成“给 Markdown 增加更多 renderer”，也不应把 A2UI 理解成可以替代产品对象模型的完整框架。

Hive 要建设的是一套 **Living Object + Native Surface System（活对象与原生 Surface 系统）**：

1. **Living Object 是业务真相**
   数据集、数据库视图、Deck、Dashboard、知识图、文档等都是有身份、有版本、有权限、有关系、有恢复语义的长期对象。

2. **Surface 是对象的交互投影**
   同一个对象可以被投影为聊天内联卡片、右侧画布、全屏编辑器、Personal KB 条目、Company Knowledge 页面或外部 A2A/A2UI 界面。Surface 不是第二份内容真相。

3. **AG-UI 是 Hive 标准的 Agent → UI 投影协议**
   Hive 不再自造一套与 AG-UI 重叠的 wire protocol。RuntimeTask、ChatTranscriptEvent、ToolRuntimeService 和 Living Object 继续是内部权威；AG-UI 负责把运行、消息、工具、活动和 Surface 状态投影给前端。

4. **CopilotKit 是 Surface SDK，不是 Hive Runtime**
   直接采用 @copilotkit/a2ui-renderer 与 AG-UI SDK；选择性吸收 CopilotKit React hooks 和 renderer。不得让 CopilotKit Runtime、Built-in Agent、Threads/Intelligence 接管 Hive 的 Agent loop、持久线程、权限或证据真相。

5. **A2UI 是声明式 Surface 格式，不是内部存储格式**
   Hive 使用 CopilotKit A2UI renderer 与自有高阶 catalog；A2UI payload 是可重建投影，版本演进不能反向绑架对象存储、权限和恢复模型。

6. **核心体验使用受信任的 React 原生组件目录**
   表格、Board、Timeline、Deck、知识图等由 Hive 自己维护的高阶组件渲染。模型引用组件和对象，不生成几千个单元格，也不默认生成任意 HTML。

7. **Codex-style 开放表达只进入受约束的 Sandbox Surface**
   一次性图表、模拟器、地图、计算器、特殊数据探索器和第三方 MCP App 可以使用 HTML/CSS/JavaScript 表达，但只能运行在独立 origin、无 ambient credential、无直接网络和无直接 Tool 权限的云端沙箱中。开放的是表达能力，不是数据与执行权限；核心 Dataset、Deck、Dashboard 仍优先使用 Native/A2UI Surface。

8. **Office 退到兼容、编解码与只读预览层**
   OfficeCLI 负责导入、导出、校验、Agent 修改和静态 HTML 渲染；浏览器通过鉴权 preview endpoint 与 sandboxed iframe 查看 DOCX/XLSX/PPTX，不再依赖外部在线编辑器。Hive 的 Dataset 和 Deck 不以 Office 文件作为默认运行时。

9. **Markdown 保留，但不再承担所有类型的唯一真相**
   叙事知识仍以 Markdown 为主；Dataset、Deck、Dashboard 使用结构化 canonical model；Markdown 保存摘要、引用和可读投影。

10. **Personal KB 变成对象长期归档和关联入口，但保持 Tool-first 边界**
   保存到个人知识库不等于把对象自动注入每次 Agent prompt。Agent 仍需通过明确的知识工具查询、读取或请求授权。

11. **不新建传统 SaaS 式“应用中心”**
   对象从任务中出生，在对话、工作台和知识库中自然流动。用户面对的是任务成果和持续对象，而不是先选一个模块再填表单。

这不是 A2UI 的“升级版”，也不是 CopilotKit 的二次包装。它是以 Hive 权限、记忆、任务、证据和恢复体系为业务内核，以 AG-UI + CopilotKit A2UI renderer 为原生表现底座，同时吸收 Claude Artifacts、Agent Native、Codex inline visualization 与 MCP Apps 优点之后形成的 Hive-native 对象运行时。Codex-style Sandbox Surface 是长尾表达通道，不是 Living Object、A2UI catalog 或 Hive Runtime 的替代品。

---

## 1. 用户真正要解决的问题

当前 Hive 的主要输出路径仍然以文本和文件为中心：

- Agent 在聊天中返回 Markdown；
- 生成文件后，以 ChatArtifact 的形式投递；
- 前端根据文件后缀选择 Markdown、图片、PDF、Office 或下载预览；
- Office 文件需要额外的编辑和渲染运行时；
- Personal KB 的主要真相仍然是 Markdown 文档、Segment、Graph 和 Grant。

这条路径适合“回答”和“交付文件”，但不适合以下长期任务：

- 一个会持续补充、筛选、分组、计算和协作的数据集；
- 一个需要 Agent 与人共同修订、重排、演示和导出的 Deck；
- 一个随着研究过程持续变化的 Dashboard；
- 一个可交互的知识地图、证据浏览器或决策看板；
- 一个需要确认、审批、重试和恢复的任务控制面；
- 一个被多个 Agent、多个 Session 和多个知识域共同引用的长期成果。

根本问题不是 Markdown 渲染能力弱，而是当前主要交付物缺少统一的对象语义：

- 没有稳定 object identity；
- 没有按对象类型定义的 canonical truth；
- 没有统一 revision、conflict、rollback 和 export provenance；
- UI 交互很难回到 Agent 的受治理执行链；
- Chat、Workspace、Personal KB、Company Knowledge 和外部协议之间容易形成多份副本；
- 文件后缀决定 renderer，无法表达“这是一个仍在运行和演化的对象”。

因此目标不是“把回答渲染得更漂亮”，而是让 Agent 能创造、操作、解释和交付长期存在的交互对象。

---

## 2. 产品北极星

### 2.1 一句话定义

**Hive 的活对象，是由 Agent 与人共同创建和演化、受组织权限治理、可在多种 Surface 中原生呈现、可恢复并可追溯的长期工作成果。**

### 2.2 用户体验目标

一个典型任务应该可以自然发生：

1. 用户在聊天中说：“把这批公司做成一个可筛选的项目库，并按阶段和赛道分组。”
2. Agent 完成信息理解和结构设计，创建 Dataset。
3. 聊天中先出现一个轻量 DataExplorer Surface。
4. 用户点“展开”，同一对象进入右侧画布或全屏，不产生副本。
5. 用户拖动列、修改筛选器、纠正一行数据；这些交互形成受治理的对象 action。
6. Agent 能读取新的对象 revision，继续补充、解释或生成图表。
7. 用户把对象保存到 Personal KB；知识库保存对象引用、摘要和关系，而不是复制一份静态 Markdown。
8. 用户让另一个 Agent 基于该 Dataset 生成 Deck。
9. Deck 继续与 Dataset 建立 derived_from 关系；数据变化可以触发受控刷新，而不是静默覆盖。
10. 最后按需要导出 PPTX、PDF、CSV 或 Markdown 快照，并能追溯到对象 revision。

### 2.3 “不像上一代 SaaS”的具体含义

不是取消结构，也不是把所有 UI 交给模型临时生成，而是改变入口和组织方式：

- 入口以任务、对话和对象为中心，不以模块菜单为中心；
- UI 随对象和当前意图显现，不要求用户预先选择复杂功能；
- Agent 可以主动提出合适的 Surface，但平台决定权限和执行边界；
- 同一对象跨聊天、画布、知识库和外部协作复用；
- 用户始终可以看到对象来源、变更、责任主体和下一步动作；
- 高阶组件提供专业能力，Agent 不重新发明表格、幻灯片或权限系统。

---

## 3. 关键术语与不可混淆的边界

| 概念 | 负责什么 | 不负责什么 | Hive 中的位置 |
|---|---|---|---|
| Living Object | 内容真相、身份、版本、关系、权限、恢复 | 不直接决定每个宿主如何渲染 | 新增核心对象层 |
| Surface | 把对象投影成可交互界面 | 不成为对象内容的第二事实源 | 新增表现与交互层 |
| A2UI | 声明式组件树、数据绑定、增量 UI 消息 | 不提供业务对象、数据库、权限或 Agent transport | CopilotKit renderer 消费的 Surface 格式 |
| AG-UI | 前端与 Agent runtime 之间的双向事件、活动和状态同步 | 不定义 Hive 的对象真相 | 标准 Agent → UI 投影协议 |
| CopilotKit | AG-UI client、React Agent UI、A2UI renderer、HITL、MCP Apps | 不接管 Hive Kernel、线程真相、权限、Living Object | 受边界约束的 Surface SDK |
| A2A | Agent 之间的任务、消息、状态和 artifact 交换 | 不定义浏览器 UI，也不替代内部 workflow | 外部/跨 Agent transport |
| MCP Apps | Tool 返回可交互 HTML app，并在 iframe 中运行 | 不适合作为所有核心对象的默认 renderer | sandbox 逃生舱 |
| Claude Artifacts | 任务旁的持久画布、版本与发布体验 | 不是可直接复用的开放对象协议 | 产品体验参考 |
| Agent Native | Action、原生 widget、generative UI、持久页面和对象模板 | 当前并未宣称原生支持 A2UI | 实现参考 |
| ChatArtifact | Session transcript 中对文件或对象交付物的稳定引用 | 不拥有对象业务内容 | 现有投递层，后续扩展 |
| AIAssetRecord | Agent、Skill、Workflow、Subagent、External Capability 的控制面索引 | 不存储用户工作成果 | 当前能力资产控制面 |
| Personal Knowledge | 用户拥有的长期知识、关系、授权和对象引用 | 不自动注入所有 prompt | 现有 owner-owned 知识层 |
| Office 文件 | 互操作、交付和高保真编辑格式 | 不作为所有 Dataset/Deck 的默认 native truth | 兼容与导出层 |

### 3.1 A2UI、AG-UI、A2A 的正确叠层

它们不是同一个协议的三个版本：

~~~mermaid
flowchart TB
    U["用户 / Browser"]
    S["Hive Native Surface Runtime"]
    O["Living Object Runtime"]
    R["Hive Agent Runtime / Workflow / ToolRuntimeService"]
    X["外部 Agent 或 Host"]

    U <-->|"AG-UI / Hive WebSocket projection"| S
    S <-->|"对象读取与受治理 Action"| O
    O <-->|"Tool、Workflow、Approval、Transcript"| R

    X <-->|"A2A：任务、消息、Artifact"| R
    X <-->|"A2UI：声明式 UI 投影"| S
    X <-->|"AG-UI：标准双向运行时事件"| R
    X <-->|"MCP Apps：沙箱 HTML App"| S
~~~

### 3.2 对 Agent Native 的准确判断

Agent Native 提供了值得吸收的四类能力：

- 同一 typed Action 可以被 UI、Agent、HTTP、MCP、A2A 和 CLI 复用；
- native chat widget 使用已注册的 React renderer，而不是让模型直接执行代码；
- generative UI 使用受限的 Alpine/Tailwind mini-app，并区分临时 UI 与持久 extension；
- Content Database 和 Slides 都拥有真实 domain model、action、revision 和 editor，不只是消息 renderer。

但当前本地 Agent Native 文档也明确说明：

- 它的 native chat UI 是自己的 typed renderer registry；
- A2A artifact 可以被它的 UI 或外部 host 渲染；
- 它目前不宣称 A2UI support。

所以 Hive 可以吸收其架构思想和对象实现，但不能把 Agent Native 直接称为 A2UI 的完善版。

### 3.3 对 CopilotKit 的正式集成决策

CopilotKit 比 Agent Native 更接近 Hive 的 Surface 问题，但它不提供 Living Object 真相层。正式边界如下：

| CopilotKit 能力 | Hive 决策 | 原因 |
|---|---|---|
| @copilotkit/a2ui-renderer | 直接依赖并由 Hive wrapper 封装 | 提供 A2UI v0.9 processor、React renderer、custom catalog 与 action hook |
| @ag-ui/core / @ag-ui/client | 直接依赖并封装 | 用标准事件替代自造 Agent/UI wire protocol |
| @copilotkit/react-core | 选择性依赖 | 只使用不要求第二套 runtime authority 的 renderer/hook；不整体替换 Hive Chat |
| CopilotKit Runtime | 不进入生产权威链 | Node BFF、agent runner、tool mediation 会与 Hive FastAPI RuntimeTask/ToolRuntimeService 重叠 |
| CopilotKit Built-in Agent | 排除 | Hive Kernel/CCPlus runtime 才是模型和工具 loop 权威 |
| CopilotKit Intelligence / Threads | 排除 | Hive 已有 ChatTranscriptEvent、RuntimeTask、session recovery、RLS 和 production topology |
| CopilotKit Shared State | 只作 Surface read model | 不能保存 Dataset rows、Deck AST、Grant 或 revision truth |
| CopilotKit MCP Apps / Open Generative UI | 安全加固后接入 | 默认 sandbox/CSP/bridge 不能直接等同 Hive enterprise boundary |

采用的 package contract 固定为：

~~~text
required:
  @copilotkit/a2ui-renderer@1.62.3
  @ag-ui/core@0.0.57
  @ag-ui/client@0.0.57
  zod@3.25.76

conditional:
  @copilotkit/react-core@1.62.3

excluded from production authority:
  @copilotkit/runtime
  CopilotKit Built-in Agent
  CopilotKit Intelligence / managed Threads
~~~

版本必须精确 pin，并由 frontend adapter 隔离。CopilotKit 当前 A2UI renderer 明确依赖 @a2ui/web_core 0.9.0；A2UI v1.0 需要单独 conformance/migration，不允许自动漂移。

---

## 4. 当前真实状态与断点

### 4.1 Hive 当前状态

| 能力 | 当前事实 | 完成状态 |
|---|---|---|
| Chat durable run | ChatTranscriptEvent 和 RuntimeTask 提供运行真相、重连与恢复 | 已有基础闭环 |
| Chat artifact | ChatArtifact 保存 workspace 文件引用和 preview metadata | 局部闭环 |
| 前端 artifact preview | ArtifactPreviewPanel 支持 image、PDF、Markdown、text 与隔离的 Office HTML；Artifact 优先消费 delivery snapshot | 已闭环 |
| OfficeCLI | 支持受限 Agent 操作，并提供 hash/cache/text fallback/CSP 的 HTML preview | 已闭环 |
| Office Online | 专用 Agent Detail 标签、editor/callback/JWT/config/Compose 已退役；生产服务只在新链路部署验收后执行最终删除 | 退役收尾中 |
| Personal KB | Inbox、Library、Graph、Profile、Grants；Markdown 是主要 canonical knowledge | 已有知识闭环 |
| A2A | Agent Card 与内部跨 Agent ArtifactRef 存在 | 局部闭环 |
| 公共 A2A JSON-RPC task endpoint | build_a2a_agent_card 明确标记 not_exposed | 已知缺失 |
| A2UI | 当前依赖和 runtime 中不存在 | 缺失 |
| AG-UI | 当前依赖和 runtime 中不存在 | 缺失 |
| CopilotKit | 当前 frontend 未安装 @copilotkit/*；本修订只锁定集成边界 | 缺失，目标已确定 |
| MCP Apps | 当前没有通用 MCP Apps host | 缺失 |
| Codex-style Sandbox Surface | 当前只有 Office HTML 的隔离 iframe 预览，没有独立 sandbox origin、capability bridge、结构化 surface_ref 或通用 visualization profile | 断点，云端目标已确定 |
| Native Dataset / Deck object | 当前没有统一的 Hive native domain model | 缺失 |
| AIAssetRecord | 当前 checkout 已有能力资产控制索引 | 已落地相邻能力，职责不同 |

### 4.2 现有 artifact 链的核心断点

当前链路大致是：

~~~mermaid
flowchart LR
    A["Agent 生成文本或 workspace 文件"]
    B["ChatArtifact 记录文件引用"]
    C["按后缀推断 preview_kind"]
    D["前端选择 Markdown / Image / PDF / Office / Download"]

    A --> B --> C --> D
~~~

断点在于：

- preview_kind 是文件类型，不是对象类型；
- 前端交互通常不能形成对象级 action；
- 没有跨 Surface 的同一 object identity；
- 没有统一对象 revision 与冲突处理；
- 用户编辑 Office 文件后，Agent 读取的是文件变化，不是明确的领域事件；
- 保存到知识库倾向于重新摄入内容，而不是链接一个持续演化的对象；
- Dataset 与 Deck 只能退化成 HTML、Markdown 或二进制文件。

### 4.3 必须保留的现有真相

新架构不得破坏以下不变量：

- ChatTranscriptEvent 继续作为 cloud run ordering、resume、replay、fork、checkpoint 和 rollback 的事务真相；
- workspace file 继续是现有普通文件和 Markdown 文档的源真相；
- ToolRuntimeService.execute 继续是受治理工具执行入口；
- Personal Knowledge 保持 user-owned、Tool-first、grant-aware；
- Agent Memory 与 Personal/Company Knowledge 保持不同 authority；
- A2A artifact 继续使用引用和 provenance，不在跨 Agent 时静默复制；
- AIAssetRecord 继续只管理 capability/config asset，不被扩展成万能内容表。

---

## 5. 目标架构总图

~~~mermaid
flowchart TB
    subgraph Experience["体验面"]
        Chat["Chat Inline"]
        Canvas["Right Canvas"]
        Full["Fullscreen Workbench"]
        PKB["Personal Knowledge"]
        CKB["Company Knowledge"]
        External["External Host"]
    end

    subgraph Projection["AG-UI Projection Layer"]
        Projector["Hive AG-UI Projector"]
        Client["@ag-ui/client"]
        Activity["Messages / Tools / Activity / State"]
    end

    subgraph Surface["Surface Layer"]
        Host["Hive SurfaceHost"]
        Renderer["@copilotkit/a2ui-renderer"]
        Registry["Hive Versioned A2UI Catalog"]
        State["Rebuildable Surface State"]
        Intent["Governed Surface Action Intent"]
    end

    subgraph Sandbox["Cloud Sandbox Surface Layer"]
        SandboxHost["Dedicated-origin Sandbox Host"]
        Viz["Codex-style Visualization Profile"]
        MCPApp["Hardened MCP App Profile"]
        Bridge["Nonce-bound Capability Bridge"]
        Policy["CSP / Egress / Quota / Kill Switch"]
    end

    subgraph Object["Living Object Layer"]
        Index["LivingObjectRecord"]
        Revision["Revision / Event / Checkpoint"]
        Dataset["Dataset Runtime"]
        Deck["Deck Runtime"]
        Knowledge["Knowledge / Document Adapter"]
        Relation["Relation / Grant / Export"]
    end

    subgraph Execution["受治理执行层"]
        Tools["ToolRuntimeService"]
        Workflow["Workflow / RuntimeTask"]
        Approval["Approval / Checkpoint"]
        Transcript["ChatTranscriptEvent / InvocationSpan"]
    end

    subgraph Adapter["外部协议与格式适配"]
        A2A["A2A Artifact Adapter"]
        Office["OfficeCLI / Sandboxed Preview / Export"]
    end

    Chat --> Projector
    Canvas --> Projector
    Full --> Projector
    PKB --> Projector
    CKB --> Projector
    External --> Adapter

    Projector --> Client
    Client --> Activity
    Activity --> Host
    Host --> Renderer
    Renderer --> Registry
    Host --> State
    Host --> Intent
    Host --> SandboxHost
    SandboxHost --> Viz
    SandboxHost --> MCPApp
    Viz --> Bridge
    MCPApp --> Bridge
    Policy --> SandboxHost
    Bridge --> Intent

    State <--> Object
    Intent --> Tools
    Tools --> Approval
    Tools --> Workflow
    Tools --> Object
    Tools --> Transcript

    Transcript --> Projector
    Workflow --> Projector
    Object --> Projector
    Object <--> Adapter
    A2A --> Execution
    Office --> Object
~~~

### 5.1 分层原则

1. Experience 只决定对象出现在哪里。
2. AG-UI Projection 把 Hive 的运行真相投影成标准事件；不反向成为 run authority。
3. Native Surface 负责 A2UI processing、React rendering、catalog 与结构化交互采集。
4. Cloud Sandbox Surface 负责 catalog 外的长尾表达；它只接收最小投影并提交非权威 intent。
5. Living Object 决定内容、版本、关系和可恢复状态。
6. Execution 决定谁可以执行、是否需要确认、如何记录证据。
7. Adapter 负责兼容外部协议和文件格式，不成为业务事实源。

任何实现只要跨越这些边界，就必须显式说明权威变化；不得通过 CopilotKit frontend tool、A2UI callback、shared state、iframe message 或文件写入绕过治理。

---

## 6. 真相模型：禁止再造多份事实源

| 事实 | 唯一权威 | 可重建投影 |
|---|---|---|
| Agent run 顺序、消息、工具事件 | ChatTranscriptEvent | 聊天 UI、T0 Markdown 投影 |
| Agent/Skill/Workflow/Subagent 配置资产 | 各 native runtime + AIAssetRecord 控制索引 | 市场、列表、统计 |
| Living Object 身份与治理元数据 | LivingObjectRecord | 搜索索引、知识库卡片 |
| Dataset schema 与 row state | Dataset canonical store | Grid、Board、Chart、CSV |
| Deck 结构与 slide state | Deck revision store | Editor、Presenter、PPTX、PDF |
| Narrative document 内容 | workspace Markdown 或明确迁移后的 document store | HTML、PDF、Surface |
| Surface 状态 | Object revision + 可丢弃的 Surface session state | AG-UI state/activity、CopilotKit shared state、A2UI store、任意宿主渲染 |
| Personal Knowledge 内容与授权 | Personal Knowledge canonical Markdown/graph/grant | Library UI、搜索结果 |
| Company Knowledge 发布内容 | Company Knowledge publish truth | Portal、搜索、Surface |
| 导出文件 | Derived export + source revision provenance | 下载、ChatArtifact |

### 6.1 canonical_source_kind

Living Object 不强迫所有类型使用同一种物理存储，但每个对象必须只有一个当前 canonical source：

| object_type | canonical_source_kind | 原因 |
|---|---|---|
| narrative_document | workspace_markdown | 可读、可移植，与现有知识和 Agent workspace 对齐 |
| dataset | relational_dataset | 支持大规模行、类型、查询、并发和增量变更 |
| deck | structured_revision | 支持 slide AST、主题、notes、版本与稳定导出 |
| dashboard | structured_revision | 布局、query binding 和 widget 配置可版本化 |
| knowledge_map | knowledge_projection | 节点来自知识真相，布局和视图是可重建投影 |
| binary_office | workspace_file | 兼容对象；只有显式 import 后才转成 native object |
| sandbox_app | signed_app_resource | 特殊 app resource，不与核心对象混为一谈 |

字段 canonical_source_kind 不是自由选择项；它由 object_type contract 决定。迁移 canonical source 必须走显式 migration，而不是双写。

---

## 7. 核心领域模型

### 7.1 LivingObjectRecord

这是所有活对象的身份和治理根，不直接吞下所有对象内容。

建议字段：

~~~json
{
  "id": "uuid",
  "tenant_id": "uuid",
  "object_type": "dataset",
  "title": "潜在投资项目库",
  "status": "draft",
  "canonical_source_kind": "relational_dataset",
  "current_revision_id": "uuid",
  "owner_principal_type": "user",
  "owner_principal_id": "uuid",
  "created_by_principal_type": "agent",
  "created_by_principal_id": "uuid",
  "origin_session_id": "uuid",
  "origin_runtime_task_id": "uuid",
  "sensitivity": "internal",
  "visibility": "private",
  "schema_version": 1,
  "created_at": "timestamp",
  "updated_at": "timestamp",
  "archived_at": null
}
~~~

关键不变量：

- tenant_id 必须参与所有 RLS 和 API 查询；
- owner 与 creator 分开；Agent 可以创建，但不自动成为最终 owner；
- current_revision_id 只在 revision commit 成功后原子更新；
- status 只允许 draft、active、archived、deleted_pending；
- deleted_pending 需要恢复窗口，不能直接物理删除；
- sensitivity 控制 Surface、export 和外部协议暴露；
- object_type 创建后不可原地改成另一个类型，只能 derived_from。

### 7.2 LivingObjectRevision

所有可版本化对象都有不可变 revision 记录；不同类型的 payload 由各自 schema 校验。

~~~json
{
  "id": "uuid",
  "object_id": "uuid",
  "parent_revision_id": "uuid",
  "revision_number": 18,
  "schema_version": 3,
  "content_ref": "object-type-owned-location",
  "content_hash": "sha256",
  "change_summary": "补充 18 家公司的融资阶段并修正 2 个来源",
  "created_by_principal": {
    "type": "agent",
    "id": "uuid"
  },
  "source_refs": [
    {
      "kind": "chat_transcript_event",
      "id": "uuid"
    },
    {
      "kind": "web_source",
      "uri": "https://example.com"
    }
  ],
  "idempotency_key": "surface-action:uuid",
  "created_at": "timestamp"
}
~~~

关键不变量：

- revision 不可覆盖；
- parent_revision_id 支持 conflict detection、fork 和 rollback；
- content_hash 用于完整性和重复提交检测；
- source_refs 用于证据追溯，不允许只存自然语言“来源说明”；
- rollback 创建一个新的 revision 指向旧内容，不篡改历史；
- Agent 生成的 change_summary 是解释，不是机械事实源。

### 7.3 LivingObjectRelation

对象关系必须显式，而不是隐藏在 Markdown 文本或前端状态中。

建议关系：

- references：弱引用；
- embeds：在当前对象中嵌入另一个对象的 Surface；
- derived_from：派生成果；
- presents：Deck 展示某 Dataset/Dashboard；
- cites：内容证据引用；
- refreshes_from：允许受控刷新，但必须配置策略；
- supersedes：新对象替代旧对象；
- belongs_to_collection：进入 Personal/Company Knowledge collection。

关系字段至少包括 tenant_id、from_object_id、to_object_id、relation_type、relation_policy、created_by、source_revision_id 和时间戳。

禁止：

- 默认级联删除；
- 因 derived_from 就自动授予写权限；
- 上游对象变化后静默覆盖下游；
- 跨 tenant 建关系；
- 用一个万能 AssetRef 取代每个域自己的 authority。

### 7.4 LivingObjectGrant

建议能力粒度：

- view；
- comment；
- propose_change；
- edit；
- manage；
- share；
- export；
- publish；
- run_actions。

Grant principal 可以是 user、agent、role、department 或 company policy。Agent 的权限来自 owner/delegation/tenant policy，不从客户端传入的 agent_id 推导。

### 7.5 LivingObjectExport

每个导出物必须记录：

- object_id；
- source_revision_id；
- format；
- exporter_version；
- export_options；
- output_path 或 object storage ref；
- content_hash；
- requested_by；
- generated_by_runtime_task_id；
- status 与 failure reason；
- created_at、expires_at。

导出文件通过 ChatArtifact 交付时，ChatArtifact 只引用 LivingObjectExport，不复制一套 provenance。

### 7.6 SurfaceInstance 与 SurfaceActionReceipt

SurfaceInstance 是可恢复的当前交互投影，默认可以丢弃并从对象重建。

需要持久化的字段：

- surface_id；
- tenant_id；
- object_id 与 object_revision_id；
- session_id；
- host_kind；
- placement；
- lifetime；
- catalog_versions；
- last_sequence；
- created_by；
- status；
- expires_at。

SurfaceActionReceipt 用于：

- idempotency；
- action authority 审核；
- approval/checkpoint 关联；
- mutation result；
- revision result；
- error 和 retry；
- transcript/span evidence。

它不能代替 ChatTranscriptEvent，而是关联到 transcript event 和 invocation span。

---

## 8. 对象类型契约

### 8.1 Narrative Document

适用：

- 报告；
- 研究笔记；
- 方案；
- 知识文章；
- 会议纪要；
- 长文本备忘录。

canonical truth 默认仍是 workspace Markdown。

Native Surface 可以提供：

- outline；
- citation explorer；
- comment/proposal；
- compare revisions；
- embedded Dataset/Deck；
- publish preview。

不得为了统一对象模型把全部 Markdown 搬进 JSONB。LivingObjectRecord 可以引用 workspace path 和 content hash，revision 记录文件版本与 provenance。

### 8.2 Dataset / 多维表格

Dataset 不是一个超大 A2UI component tree，也不是 XLSX 文件的别名。

#### 8.2.1 领域结构

建议对象专属表：

- dataset_fields；
- dataset_rows；
- dataset_views；
- dataset_mutation_events；
- dataset_checkpoints。

Field 至少支持：

- text；
- rich_text；
- number；
- currency；
- percent；
- boolean；
- date；
- datetime；
- single_select；
- multi_select；
- person；
- agent；
- relation；
- attachment；
- url；
- email；
- formula；
- rollup；
- source_ref。

Field schema 包含：

- stable field_id；
- name；
- type；
- nullable；
- validation；
- options；
- default；
- formula AST；
- relation target；
- sensitivity；
- display hints；
- schema version。

Row 使用 typed values JSONB 与 row_version：

~~~json
{
  "row_id": "uuid",
  "dataset_id": "uuid",
  "row_version": 12,
  "values": {
    "company_name_field_id": "Acme",
    "stage_field_id": "Series A",
    "amount_field_id": {
      "currency": "USD",
      "value": "12000000"
    }
  },
  "source_refs": [
    {
      "kind": "web_source",
      "uri": "https://example.com/company"
    }
  ]
}
~~~

#### 8.2.2 View

View 是 Dataset 的已保存读取与交互配置，不复制 row truth。

第一轮完整实现必须同时支持：

- table；
- board；
- list；
- gallery；
- calendar；
- timeline；
- chart/pivot。

每个 view 可以声明：

- visible fields；
- sort；
- filter AST；
- group；
- aggregation；
- calculation；
- layout；
- color rules；
- row density；
- frozen columns；
- permissions on edit；
- query cursor policy。

#### 8.2.3 计算边界

以下由平台机械执行：

- sort、filter、group、pagination；
- field validation；
- formula evaluation；
- aggregation；
- optimistic concurrency；
- virtualization；
- deterministic export。

以下由 LLM 执行：

- 从任务理解合适 schema；
- 推荐字段和视图；
- 从非结构化材料抽取 row；
- 解释异常和趋势；
- 生成归类候选；
- 判断需要哪些证据；
- 提议修复冲突数据。

模型不能逐格生成 UI；平台也不能用规则替代语义抽取。

### 8.3 Deck / 原生演示文稿

Deck canonical truth 是结构化 revision，不是 raw HTML，也不是 PPTX 二进制。

#### 8.3.1 Deck AST

~~~json
{
  "deck": {
    "title": "2026 投资机会",
    "theme_ref": "hive.deck.editorial@1",
    "aspect_ratio": "16:9",
    "slides": [
      {
        "slide_id": "uuid",
        "layout": "data-story",
        "blocks": [
          {
            "block_id": "uuid",
            "type": "heading",
            "content": "市场窗口已经打开"
          },
          {
            "block_id": "uuid",
            "type": "dataset_chart",
            "object_ref": {
              "object_id": "uuid",
              "revision_policy": "pinned",
              "revision_id": "uuid"
            },
            "view_id": "uuid"
          }
        ],
        "speaker_notes": "解释数据口径与风险",
        "transition": {
          "kind": "fade",
          "duration_ms": 300
        }
      }
    ]
  }
}
~~~

#### 8.3.2 Block 类型

至少包含：

- heading；
- body；
- quote；
- image；
- video；
- metric；
- table；
- chart；
- timeline；
- comparison；
- process；
- code；
- citation；
- object_embed；
- freeform_group。

#### 8.3.3 编辑与生成

Agent 可以：

- 创建 narrative；
- 生成 slide outline；
- 选择模板与 layout；
- 生成 block 内容；
- 引用 Dataset 的固定 revision 或受控 latest；
- 提议重排、精简和视觉修订；
- 导出 PPTX、PDF、HTML presenter。

用户可以：

- 拖动和调整 block；
- 修改文本与主题；
- 接受或拒绝 Agent proposal；
- 锁定 slide 或 block；
- 切换 editor/presenter；
- 回滚 revision。

任何 HTML import 都必须被解析、消毒并转换为受支持 block；无法转换的部分进入 sandbox block，不能让整个 Deck 退化成任意 HTML。

### 8.4 Dashboard

Dashboard 是 query、metric、chart、filter 和 layout 的结构化对象。它引用 Dataset 或 Knowledge query，不复制底层数据。

必须支持：

- pinned revision 与 latest-with-approval 两种绑定；
- query failure 与 stale data 状态；
- filter propagation；
- role-aware widget visibility；
- snapshot export；
- data lineage；
- refresh receipt。

### 8.5 Knowledge Map

Knowledge Map 不是新的知识真相：

- node、edge 来自 Personal/Company Knowledge 的 canonical graph；
- layout、focus、filter、annotation 是 Surface 或 object view state；
- Agent 可以提出新关系，但写入知识真相仍走对应 proposal/review/publish；
- 保存 Knowledge Map 只保存视图和已确认 annotation，不复制知识语料。

### 8.6 Codex-style 开放表达 Sandbox Surface

#### 8.6.1 “开放表达”而不是“开放权限”

Codex Desktop 当前 inline visualization 的产品形态可概括为：Agent 生成线程内 HTML fragment，宿主用结构化 directive 定位文件，再通过独立 Electron webview partition、内层 `sandbox="allow-scripts"` iframe、严格 CSP 与窄 MessageChannel bridge 渲染。任意 Tool/MCP 调用默认拒绝；follow-up、下载和外链都受用户激活、确认或 capability 约束。

Hive 吸收的是以下原则，而不是复制其本地 Electron 实现：

- Agent 可以表达 catalog 尚未覆盖的 HTML/CSS/JavaScript；
- 生成内容始终被视为不可信代码和不可信数据；
- 宿主控制身份、输入、网络、资源、动作、证据与退出；
- 展示层状态可以丢弃，真实对象和执行结果不能依赖 iframe 内存；
- 任意写操作都回到 Hive 的服务端权威链。

因此推荐产品术语为 **Sandbox Surface / 开放表达沙箱**，不使用“开放应用”或“开放执行环境”。

#### 8.6.2 使用范围与升级规则

只在以下情况使用：

- 一次性 chart、map、simulation、calculator、timeline、relationship graph 或数据探索器；
- 新交互尚未进入 Hive native catalog，但任务确实需要即时可视化；
- 第三方 MCP Tool 提供官方 MCP App；
- 外部系统要求独立 HTML application；
- 历史 HTML artifact 需要只读兼容。

不得用于：

- Dataset、Deck、Dashboard、Knowledge Map 的默认 canonical form；
- 高频重复出现且已能稳定定义 schema/action 的核心产品交互；
- `restricted` 数据，或 policy 明确禁止截图/复制/外部脚本的内容；
- 需要在 iframe 内持有租户凭证、数据库连接或长期后台任务的场景。

同一种 Sandbox Surface 达到稳定复用、需要协作 revision、需要知识库长期消费，或出现明确领域动作后，必须升级为 Native/A2UI catalog component 或 Living Object type，而不是继续积累不可治理 HTML。

#### 8.6.3 云端 SaaS 拓扑

Hive 是浏览器访问的多租户 SaaS，不能依赖 Codex Desktop 的 Electron `webview partition`。云端采用两层 Web 隔离：

~~~mermaid
sequenceDiagram
    participant U as User Browser
    participant A as app.hive.example
    participant B as Hive Backend
    participant S as surface-sandbox.hive.example
    participant I as Inner Fragment Frame
    participant T as ToolRuntimeService

    U->>A: 打开 surface_ref
    A->>B: POST /surfaces/{id}/sandbox-sessions
    B->>B: 重建 principal / tenant / grant / sensitivity
    B-->>A: 短期 SandboxBootstrap + nonce + projection
    A->>S: 加载静态 sandbox shell，不携带登录凭证
    S-->>A: shell_ready(instance_nonce)
    A->>S: transfer MessagePort + signed bootstrap
    S->>I: 创建 sandbox="allow-scripts" 的内层 srcdoc/blob frame
    I-->>S: local interaction / typed intent
    S-->>A: schema-validated client message
    A->>B: 带正常用户会话提交 SurfaceActionIntent
    B->>B: 重新鉴权、校验 capability/idempotency/base revision
    B->>T: governed execution
    T-->>B: receipt / revision / transcript event
    B-->>A: action result + recovery cursor
    A-->>S: 最小 projection patch
~~~

域名与部署约束：

- 主应用使用 `app.<root-domain>`；开放表达壳使用独立 `surface-sandbox.<root-domain>`；生产禁止退化成主站同源路径；
- 独立 origin 可以由现有 frontend 服务按 Host header 提供静态 shell，不要求新增第四个生产服务；但 Cookie、缓存、CSP、日志和路由策略必须按 origin 分离；
- 登录 Cookie 必须是 app host-only，不能设置为会覆盖 sandbox 子域的宽域 Cookie；sandbox response 不设置身份 Cookie；
- sandbox shell 使用固定、可审计版本，不从对象内容动态拼接宿主脚本；真正不可信 fragment 只进入内层无 same-origin iframe；
- `frame-ancestors` 只允许 Hive 明确的 app origins；开发、preview、production 各自使用独立 allowlist；
- 主站 API CORS 不向 sandbox origin 开放，sandbox 不能直接 fetch Hive API。

#### 8.6.4 Sandbox resource 与 bootstrap contract

服务端保存或引用不可变的 `SandboxResourceRevision`：

~~~json
{
  "resource_id": "uuid",
  "revision_id": "uuid",
  "tenant_id": "server-bound",
  "source_kind": "agent_generated|mcp_app|historical_html",
  "profile": "visualization|mcp_app|read_only_html",
  "content_hash": "sha256",
  "mime_type": "text/html-fragment",
  "resource_manifest": [],
  "sensitivity": "internal",
  "created_by_runtime_task_id": "uuid",
  "scan_status": "accepted|quarantined|rejected"
}
~~~

浏览器拿到的 `SandboxBootstrap` 只包含：

- surface_id、resource_revision_id、profile 与 schema_version；
- server 计算的 principal binding hash，不包含可声明身份的 tenantId/userId；
- placement、locale、theme 和尺寸上限；
- 已授权的最小 projection，不是完整 Living Object；
- action handles 与 capability names，不包含 Hive JWT/API key；
- bootstrap nonce、短期过期时间和 replay state；
- content/resource hashes 与 fallback metadata。

bootstrap 不放在 URL query、fragment、Referer、HTML source 或持久 localStorage 中。主站完成认证后通过 `MessageChannel` 内存传递；后端的 nonce/capability/idempotency 状态使用 Redis 或数据库支持多 replica，不得只放单进程内存。

#### 8.6.5 Capability profile

开放表达沙箱按 profile 授权，不按任意 JavaScript 方法名授权：

| Capability | visualization 默认 | mcp_app 默认 | 权威边界 |
|---|---:|---:|---|
| local_select/filter/hover | 允许 | 允许 | 只改变 iframe 本地 read model |
| report_height/error | 允许 | 允许 | 只写运行状态/metric，不写领域真相 |
| send_follow_up | 用户激活 + 确认 | 用户激活 + 确认 | 创建新的用户可见 Agent turn，不代表 action approval |
| download/export | 单次 capability + policy | 单次 capability + policy | 服务端重新校验 sensitivity、大小与文件类型 |
| domain_action_intent | 显式 schema/action handle | 显式 tool capability | 主站重建 auth 后进入 SurfaceActionService |
| call_tool/call_mcp | 默认拒绝 | 仅经 MCP host allowlist | iframe 永远不直接拥有 ToolRuntimeService credential |
| network_fetch | 拒绝 | 默认拒绝，例外需 server proxy policy | 防 SSRF、数据外泄和隐式第三方追踪 |
| clipboard/screenshot | sensitivity policy | sensitivity policy | confidential/restricted 默认拒绝 |

动作消息必须是版本化 discriminated union；source window、MessagePort、surface_id、instance_nonce、schema、payload size、action handle 与 sequence 全部校验。未知方法、重复 sequence、过期 nonce、跨 surface handle、超限 payload 一律 fail closed，并只向用户返回 typed/recoverable error，不暴露 stack trace。

#### 8.6.6 网络、资源与执行限制

默认 inner-frame CSP：

~~~text
default-src 'none';
connect-src 'none';
img-src blob: data:;
font-src 'self' data:;
media-src blob: data:;
worker-src 'none';
frame-src 'none';
object-src 'none';
base-uri 'none';
form-action 'none';
~~~

实现时允许为受审计 profile 调整 `script-src`，但必须满足：

- 不允许公共 CDN；D3、Vega、ECharts、Three.js 等由 Hive 固定版本、自托管或构建时打包；
- 不允许任意 remote script、image beacon、WebSocket、EventSource、fetch/XHR；
- 不允许 `allow-same-origin`、`allow-forms`、top navigation、popups、presentation、camera、microphone、geolocation、USB、Bluetooth、serial、payment、clipboard-read；
- 不允许 sandbox 自行创建 Service Worker、持久 IndexedDB 或跨会话 localStorage；
- 资源导入先由服务端 fetch/proxy 层执行 DNS/IP/redirect/content-type/size 校验，阻止 localhost、metadata endpoint、private network 与 redirect SSRF；
- HTML/JS、内联数据、DOM 数、消息、输出文件、帧高度、CPU 长任务、内存、worker、动作频率和生命周期均有 quota；
- quota 超限触发可观察的 terminate/degrade/retry，不让 renderer 阻塞主聊天；
- 管理员可以按 tenant、profile、resource hash、MCP server 或全局 kill switch 禁用。

#### 8.6.7 真相、状态与恢复

- HTML fragment 是 `SandboxResourceRevision` 或 ChatArtifact delivery snapshot，不是 Living Object 的另一份 canonical truth；
- `surface_ref` 是服务端签发的结构化 message part；不通过解析模型自然语言中的任意 directive 获得文件或对象访问权；
- iframe 内 filter、selection、zoom、hover 默认是可丢弃 local state；如需要恢复，只允许 schema 化、限量的 `SurfaceViewState`；
- Dataset row、Deck block、Knowledge relation 等 durable mutation 必须返回 SurfaceActionReceipt 与 resulting revision；
- iframe crash、tab reload、部署切换或 Redis ephemeral state 丢失后，从 resource revision + object revision + transcript cursor 重建；
- sandbox close 不取消 RuntimeTask，RuntimeTask 完成也不删除 resource/object；
- resource 过期、quarantine 或 policy revoke 时返回 typed tombstone 与安全 fallback，不回退读取未经授权的当前 workspace 文件；
- 任何 view-state fallback 只能 abstain/reset/retry，不能代替 LLM 或用户创建语义真相。

---

## 9. AG-UI + CopilotKit Surface Contract

### 9.1 决策：标准协议优先，Hive extension 最小化

Hive 不再定义与 AG-UI 重叠的 hive.surface.v1 wire protocol。

标准边界是：

- **AG-UI**：run、message、reasoning、tool call、activity、state snapshot/delta 的传输和前端消费；
- **A2UI v0.9**：由 @copilotkit/a2ui-renderer 消费的声明式组件树与数据模型；
- **Hive extension**：只表达 AG-UI/A2UI 不拥有的 object identity、revision、authority hint、placement、lifetime 和 provenance；
- **Hive canonical truth**：ChatTranscriptEvent、RuntimeTask、Living Object、ToolRuntimeService、Grant、Revision、Receipt。

AG-UI event log 在 Hive 中是投影，不是第二份持久化 run ledger。若外部 AG-UI 客户端请求 replay，事件必须从 ChatTranscriptEvent、RuntimeTask 和对象 read model 确定性重建。

### 9.2 Hive → AG-UI 事件映射

| Hive 事实或事件 | AG-UI 投影 | 规则 |
|---|---|---|
| RuntimeTask claimed/running | RUN_STARTED | threadId=session_id；runId=runtime_task_id |
| RuntimeTask success | RUN_FINISHED | 只有 durable terminal commit 后发出 |
| RuntimeTask failure | RUN_ERROR | 保留 recoverable/error code，不泄露 secret |
| assistant streaming | TEXT_MESSAGE_START/CONTENT/END | messageId 绑定 ChatTranscriptEvent/message identity |
| reasoning/thinking | REASONING_* | 服从 provider visibility 与 thinking-signature policy |
| governed tool call | TOOL_CALL_START/ARGS/END | toolCallId 绑定 invocation span |
| governed tool result | TOOL_CALL_RESULT | 只能来自 ToolRuntimeService receipt |
| workflow/subagent/plan/progress | ACTIVITY_SNAPSHOT/DELTA | activityType 使用版本化 hive.* 名称 |
| UI read model | STATE_SNAPSHOT/DELTA | 只承载可重建 state；不承载对象真相 |
| transcript recovery | MESSAGES_SNAPSHOT + missing events | 按 committed sequence 恢复 |
| Living Object / Artifact ref | CUSTOM 或 ACTIVITY payload | 只发送 ID、revision、capability 与 fallback |

禁止把任意后端内部事件全部塞进 RAW。只有无法归一化的外部兼容事件才使用 RAW，并必须带 source。

所有来自 durable Hive event 的 AG-UI projection 使用 BaseEvent.rawEvent 附带最小来源游标：

~~~json
{
  "rawEvent": {
    "source": "hive",
    "transcriptEventId": "uuid",
    "hiveSequence": 42,
    "runtimeTaskId": "uuid"
  }
}
~~~

rawEvent 只用于 provenance/cursor，不携带 tenant secret、完整 tool arguments 或未脱敏对象内容。

### 9.3 Hive Surface Metadata Extension

Surface metadata 通过版本化 CUSTOM event 或 A2UI activity metadata 发送：

~~~json
{
  "type": "CUSTOM",
  "name": "hive.surface.meta.v1",
  "value": {
    "surfaceId": "uuid",
    "hiveSequence": 42,
    "sessionId": "uuid",
    "runtimeTaskId": "uuid",
    "objectRef": {
      "objectId": "uuid",
      "revisionId": "uuid",
      "objectType": "dataset"
    },
    "placement": "right_canvas",
    "lifetime": "durable",
    "catalog": {
      "id": "hive.data",
      "version": "1.0.0"
    },
    "authorityHints": [
      "dataset.view",
      "dataset.propose_change"
    ],
    "fallback": {
      "kind": "deep_link",
      "href": "/objects/uuid"
    },
    "provenance": {
      "transcriptEventId": "uuid",
      "invocationSpanId": "uuid"
    }
  }
}
~~~

authorityHints 只用于控制 UI 是否显示 action，不是 server-side authorization。服务端必须重新计算全部权限。

### 9.4 A2UI Surface

Hive 使用 @copilotkit/a2ui-renderer 的以下能力：

- A2UIProvider；
- A2UIRenderer；
- createCatalog；
- extractCatalogComponentSchemas；
- v0.9 MessageProcessor；
- custom onAction callback；
- loading/error/recovery fallback。

A2UI operation 只描述组件和数据绑定。大型对象必须传 objectId、viewId、query token 或分页 cursor，不能把全部 Dataset rows/Deck assets 塞进 A2UI JSONL。

典型 root component：

~~~json
{
  "version": "v0.9",
  "createSurface": {
    "surfaceId": "uuid",
    "catalogId": "hive.data@1"
  }
}
~~~

~~~json
{
  "version": "v0.9",
  "updateComponents": {
    "surfaceId": "uuid",
    "components": [
      {
        "id": "root",
        "component": "DataExplorer",
        "objectId": "uuid",
        "viewId": "uuid",
        "mode": "edit"
      }
    ]
  }
}
~~~

### 9.5 Surface Action

CopilotKit 当前默认 A2UI action 行为是把 userAction 放入 Agent properties 并重新运行 Agent。Hive 生产路径必须覆盖这个默认行为：

1. @copilotkit/a2ui-renderer onAction 捕获 A2UIClientEventMessage；
2. HiveActionBridge 校验 surfaceId、componentId、action name 和参数 schema；
3. 本地纯视图 action 可在客户端完成；
4. 所有对象 mutation、grant、export、publish、workflow 和外部 action 发送到 Hive Surface Action API；
5. 服务端从认证 session 重建 tenant/user/agent/delegation；
6. ToolRuntimeService/对象 runtime 执行并产生 receipt；
7. ChatTranscriptEvent 与 InvocationSpan 写入证据；
8. 新 revision 经 AG-UI STATE/ACTIVITY 与 A2UI data patch 回到前端；
9. CopilotKit 默认 forward 必须被抑制，不能同时再触发一次 Agent run。

### 9.6 Placement

- inline：聊天内轻量摘要和少量交互；
- right_canvas：任务旁持续编辑；
- fullscreen：专业工作台；
- knowledge_embed：Personal/Company Knowledge 页面；
- external_embed：外部 host；
- presenter：Deck 演示；
- print_export：只读导出投影。

Placement 属于 Hive metadata，不写入 Living Object 内容，也不改变对象权限。

### 9.7 Lifetime

- ephemeral：一次消息或一次 tool call；
- session：随 ChatSession 恢复；
- durable：绑定 Living Object；
- published：拥有显式发布版本和 public/company policy。

CopilotKit shared state 只管理 ephemeral/session read model。durable/published 必须从 Living Object 和知识 authority 恢复。

### 9.8 Recovery 与顺序

- ChatTranscriptEvent.sequence 是 durable committed order；
- AG-UI streaming order 是在线投影顺序；
- hiveSequence 将 AG-UI event 关联到 transcript sequence；
- A2UI surfaceId 关联可重建的 SurfaceInstance；
- 客户端发现 gap 时请求 Hive recover endpoint；
- recover 返回 MESSAGES_SNAPSHOT、必要 STATE_SNAPSHOT 和当前 A2UI surface snapshot；
- action 使用 idempotency_key 和 receipt，不以“是否收到前端响应”判断是否提交；
- ephemeral surface 可以过期，但不能承载唯一业务事实。

### 9.9 Catalog negotiation

Host 必须声明：

- 支持的 AG-UI SDK/version；
- 支持的 A2UI version；
- 支持的 catalog 和版本；
- 支持的 placement；
- 最大 payload；
- 是否支持 state/activity delta；
- 是否支持 editable action；
- 是否支持 hardened sandbox app；
- accessibility 和 locale capability。

遇到不支持的 component：

1. 优先降级为同 catalog 的 read-only summary；
2. 再降级为对象 deep link；
3. 最后降级为 Markdown/text；
4. 不得空白失败；
5. 不得自动执行外部 HTML。

---

## 10. 原生组件目录

### 10.1 设计原则

组件目录必须是高阶领域组件，不是让模型拼 DOM 的低阶积木。

实现上使用 CopilotKit createCatalog(definitions, renderers)：

- definitions 使用 Zod 描述平台无关 props contract；
- renderers 是 Hive React component；
- catalog schema 可通过 extractCatalogComponentSchemas 注入 Agent context；
- component name、props、action 全部版本化；
- Hive wrapper 负责 object reference、authority hint、fallback 和 telemetry；
- 不直接修改 CopilotKit renderer 内部 store。

坏例子：

- 模型为 10,000 行表格生成 10,000 个 Row 和数十万个 Cell；
- 模型生成每个像素位置；
- 模型在 action 中写任意 JavaScript；
- 模型通过自由字符串命名后端函数。

好例子：

- DataExplorer(objectId, viewId)；
- DeckEditor(deckId, revisionId)；
- KnowledgeMap(queryRef, viewState)；
- ApprovalPanel(checkpointId)；
- RunProgress(runtimeTaskId)。

### 10.2 hive.core

- Stack；
- SplitPane；
- Tabs；
- Card；
- EmptyState；
- ErrorState；
- LoadingState；
- MarkdownView；
- CitationList；
- ObjectLink；
- CommandBar；
- ActionBar；
- ConfirmAction；
- VersionBadge；
- Presence；
- CommentThread。

### 10.3 hive.data

- DataExplorer；
- DataGrid；
- BoardView；
- ListView；
- GalleryView；
- CalendarView；
- TimelineView；
- PivotView；
- ChartView；
- FilterBuilder；
- GroupBuilder；
- FormulaEditor；
- FieldInspector；
- RowDetail；
- ImportMapping；
- DataLineage。

### 10.4 hive.deck

- DeckEditor；
- DeckPresenter；
- SlideNavigator；
- SlideCanvas；
- BlockInspector；
- ThemePicker；
- SpeakerNotes；
- RevisionCompare；
- ExportPanel；
- DatasetBindingInspector。

### 10.5 hive.knowledge

- KnowledgeSearch；
- KnowledgeMap；
- CitationExplorer；
- EvidenceTimeline；
- SourceViewer；
- ProposalReview；
- GrantInspector；
- ObjectCollection；
- ProfileDiff。

### 10.6 hive.runtime

- RunProgress；
- TodoBoard；
- FindingTimeline；
- ToolCallInspector；
- ApprovalPanel；
- WorkflowGraph；
- SubagentFanout；
- RecoveryPanel；
- BudgetMeter；
- AuditTrail。

### 10.7 Catalog 治理

每个组件必须声明：

- version；
- props schema；
- data query contract；
- emitted intents；
- required capabilities；
- editable/read-only modes；
- fallback renderer；
- accessibility contract；
- maximum payload；
- security review status；
- visual test fixtures。

组件升级遵循 semantic version。Durable Surface 记录 catalog major version；minor/patch 可以兼容升级，major 需要 migration 或 legacy renderer。

CopilotKit package 与 Hive catalog 分别版本化：

- package version 决定 A2UI processor/renderer 行为；
- catalog version 决定业务组件 contract；
- Living Object schema version 决定对象内容；
- 三者不得共用一个 version 字段；
- dependency upgrade 必须跑 CopilotKit/A2UI/AG-UI conformance fixtures；
- 不允许使用 latest range 自动升级生产 renderer。

---

## 11. Surface Action 与 Agent 行动闭环

### 11.1 统一链路

~~~mermaid
sequenceDiagram
    participant U as User
    participant C as CopilotKit A2UI Renderer
    participant H as HiveActionBridge
    participant S as SurfaceActionService
    participant G as Authority/Approval
    participant T as ToolRuntimeService
    participant O as LivingObjectRuntime
    participant E as Transcript/Span
    participant P as AG-UI Projector

    U->>C: 编辑单元格 / 调整 slide / 点击执行
    C->>H: onAction(A2UIClientEventMessage)
    H->>H: schema validate + suppress default Agent forward
    H->>S: surface.action_intent + base revision + idempotency key
    S->>G: 解析可信 principal、grant、risk、delegation
    alt 需要确认
        G-->>C: checkpoint_required
        U->>C: confirm / reject
    end
    G->>T: governed typed action
    T->>O: validate + mutate
    O->>O: commit revision / event / checkpoint
    O->>E: evidence refs + receipt
    O-->>S: revision result
    S->>P: result + new object revision
    P-->>C: AG-UI state/activity + A2UI data patch
~~~

### 11.2 ActionIntent

~~~json
{
  "event": "surface.action_intent",
  "surface_id": "uuid",
  "sequence": 42,
  "action": "dataset.row.update",
  "object_id": "uuid",
  "base_revision_id": "uuid",
  "arguments": {
    "row_id": "uuid",
    "field_id": "uuid",
    "value": "Series B",
    "row_version": 7
  },
  "idempotency_key": "uuid",
  "client_context": {
    "placement": "right_canvas"
  }
}
~~~

client_context 只能提供显示上下文，不能提供 user_id、tenant_id、agent_id、grant 或 approval 结果。可信 principal 从 server-side session、Agent run 和 delegation context 推导。

HiveActionBridge 的强制规则：

- CopilotKit onAction interceptor 对所有 domain action 返回 handled/null，禁止默认 rerun Agent；
- 只有纯展示或导航 action 可以完全在浏览器处理；
- frontend tool 不得直接执行对象 mutation；
- shared state 变化不等于对象提交；
- action success 只由 server receipt 判定；
- Agent 如需基于交互继续推理，由服务端在 revision commit 后显式创建新的受治理 turn，而不是浏览器直接 runAgent。

### 11.3 Action 分类

| Action | 默认治理 |
|---|---|
| read/query/filter/sort | 只读鉴权，可重放 |
| local view state | 不进入对象 revision，可进入 Surface state |
| object content edit | grant + optimistic concurrency + revision |
| Agent propose change | 生成 proposal，不直接覆盖用户锁定内容 |
| export | export grant + sensitivity policy |
| publish/share | 高风险，显式 checkpoint |
| external side effect | ToolRuntimeService + action preflight + approval |
| schema destructive change | impact preview + checkpoint + recovery plan |

### 11.4 模型与平台的职责分工

模型负责：

- 理解意图；
- 设计结构；
- 生成内容；
- 提出语义变更；
- 解释冲突；
- 选择合适 Surface；
- 判断信息不足并提问。

平台负责：

- schema validation；
- typed action；
- permission；
- idempotency；
- revision；
- concurrency；
- persistence；
- rendering safety；
- audit；
- export；
- recovery。

这符合 Hive AI-Native Design Law：释放模型智能，但不允许模型绕过治理。

---

## 12. Chat、Canvas、Fullscreen 与知识库的对象生命周期

### 12.1 生命周期

~~~mermaid
stateDiagram-v2
    [*] --> EphemeralSurface: Agent 产生临时 UI
    EphemeralSurface --> SessionDraft: 创建可恢复对象草稿
    SessionDraft --> DurableObject: 用户保存或任务要求长期交付
    DurableObject --> PersonalKnowledge: owner 归档、关联、授权
    PersonalKnowledge --> CompanyProposal: 提议进入企业知识
    CompanyProposal --> CompanyPublished: review + publish
    DurableObject --> Archived: 显式归档
    Archived --> DurableObject: 恢复
~~~

### 12.2 Chat Inline

用于：

- 首次呈现；
- 摘要；
- 关键 action；
- progress；
- approval；
- 打开对象。

限制：

- 不承担完整复杂编辑；
- 不加载超大数据；
- 仅展示当前对象的轻量 query；
- 所有交互仍引用同一 object_id。

### 12.3 Right Canvas

用于任务过程中持续存在的对象：

- DataExplorer；
- DeckEditor；
- KnowledgeMap；
- Document + citation；
- Approval/Run inspector。

Canvas 与当前 Session 相关，但对象本身可以是 durable。切换消息不应销毁对象；切换 Session 时保留明确的当前对象集合。

### 12.4 Fullscreen Workbench

用于：

- 大型 Dataset；
- Deck 专业编辑；
- Dashboard layout；
- revision compare；
- complex import/export。

它不是单独的产品模块，只是同一 SurfaceHost 的高密度 placement。

### 12.5 Personal Knowledge

Personal KB 新增的是对象引用与 collection，不是把结构化对象全文复制成 Markdown。

一个 Personal Knowledge entry 可以引用：

~~~yaml
kind: living_object_ref
object_id: 00000000-0000-0000-0000-000000000000
pinned_revision_id: 00000000-0000-0000-0000-000000000000
title: 潜在投资项目库
summary: 由 2026 Q3 市场研究任务生成的公司与融资事件数据集
tags:
  - 投资研究
  - 项目库
relationship:
  - belongs_to_collection: VC Research
~~~

必须保持：

- Personal KB owner 是用户；
- Agent 需要 grant 才能读取；
- 保存对象不等于在所有 prompt 中自动注入；
- Agent 通过 tool_search/read/query 使用对象；
- 分享给 Agent 或 Company 必须显式授权或 proposal；
- Markdown 摘要是人类可读投影，不是 Dataset row truth。

### 12.6 Company Knowledge

Company Knowledge 的完整 authority、proposal、permission、publication 和 Tool-first 契约以 `docs/company-knowledge-base-spec-2026-07-07.md` 为准。Living Object 在其中是可发布的 canonical resource reference，不是绕过 Company proposal 的第二条写入路径。

Personal Object 进入 Company Knowledge 的路径：

1. 固定 `object_id + source_revision_id + content_hash`；
2. 获取 authenticated owner consent；
3. 用户或 Agent 创建 Company Knowledge proposal；
4. 执行 source ACL、sensitivity、conflict、retention 和对象权限检查；
5. 按 Company risk policy 完成 reviewer/multi-review；
6. 发布 immutable Company publication reference；
7. 公司侧只获得经过 Company permission decision 的 Surface/query；
8. 后续对象 revision 只能生成 update proposal，不静默跟随 private latest。

允许的 revision policy：

- `pinned`：publication 永远固定已审查 revision；
- `reviewed_follow`：private latest 变化后自动生成 update proposal，审核通过才切换 publication。

Company publication 不复制 Dataset rows、Deck blocks 或 Dashboard state；对象内部 truth 和 mutation 继续由 Living Object authority/ToolRuntime 管理。

---

## 13. Agent 创建与操作对象的 Tool Contract

### 13.1 不给模型暴露底层 CRUD 海洋

模型需要的是高层 typed tools：

- living_object_create；
- living_object_describe；
- living_object_query；
- living_object_propose_change；
- living_object_apply_change；
- living_object_compare_revisions；
- living_object_create_relation；
- living_object_export；
- surface_present；
- surface_update；
- dataset_define_schema；
- dataset_upsert_rows；
- dataset_create_view；
- deck_create_outline；
- deck_apply_patch；
- deck_bind_object；
- deck_render_preview。

所有工具仍通过 ToolRuntimeService.execute。

### 13.2 proposal 与 apply 分离

对于以下情况必须先 proposal：

- 用户已经编辑过或锁定的内容；
- destructive schema mutation；
- Company Knowledge published object；
- 多个 Agent 同时协作；
- derived object 自动刷新；
- 高敏感内容导出；
- 外部可见 publish。

proposal 包含：

- base revision；
- intended patch；
- semantic reason；
- evidence refs；
- affected relations；
- risk；
- preview。

平台负责校验和提交，模型不能声称“已经修改”但没有 receipt。

### 13.3 读取与上下文预算

Agent 不应默认把整个 Dataset 或 Deck 塞入 prompt。

应使用：

- schema/outline summary；
- query；
- pagination；
- selected rows/slides；
- evidence retrieval；
- deterministic aggregates；
- explicit object refs。

这不违反“模型输入可见性完整”：对当前智能判断所需的内容必须完整可访问；平台提供可查询对象接口，而不是机械裁剪后假装完整。

---

## 14. 内部 Transport 与外部协议

### 14.1 Hive 内部

第一权威路径继续使用：

- ChatTranscriptEvent；
- RuntimeTask；
- web_chat_broker / WebSocket；
- InvocationSpan；
- ToolRuntimeService；
- typed API。

AG-UI projector 从上述权威读取并发出标准事件。Surface event 可以通过现有 session event fanout 或独立 SSE endpoint 发送，但不能把所有高频 Grid scroll/view state 都写进 ChatTranscriptEvent。边界如下：

| 事件 | 进入 Transcript | 进入 Surface state | 进入 Object revision/event |
|---|---|---|---|
| 打开/关闭面板 | 可选摘要 | 是 | 否 |
| 本地列宽/滚动 | 否 | 是 | 否 |
| 保存 view layout | 是，记录动作 | 是 | 是 |
| 修改 cell | 是，记录 receipt | patch | 是 |
| Agent proposal | 是 | 是 | proposal |
| export/publish | 是 | status | 是 |
| approval | 是 | status | checkpoint |

### 14.2 CopilotKit A2UI Integration

@copilotkit/a2ui-renderer 的职责：

- process A2UI v0.9 operations；
- 使用 Hive custom catalog 渲染 React components；
- 管理 surface-local component/data model；
- 将 A2UI user action 交给 HiveActionBridge；
- 协商 unsupported component fallback；
- 不把 A2UI payload 持久化为 canonical object；
- 不直接拥有 tenant、grant、revision 或 durable recovery。

Hive native catalog 比 A2UI basic catalog 更高阶。无法一一映射的组件：

- 用 custom catalog；
- 或降级为 deep link/summary；
- 不拆成海量低阶 atom。

### 14.3 AG-UI Primary Projection

AG-UI 是 Hive Web 的标准 Agent → UI projection contract，不再只是外部 Agent adapter。

它覆盖：

- run lifecycle；
- assistant/reasoning stream；
- tool lifecycle；
- workflow/subagent/plan activity；
- rebuildable UI state；
- A2UI Surface activity；
- reconnect snapshot/delta。

它不替换 RuntimeTask、ChatTranscriptEvent、Hive WebSocket/SSE delivery、invocation spans 或 object revision。外部 AG-UI 事件进入 Hive 后必须先归一化并获得 tenant/principal/provenance，不能成为无主的直接 UI mutation。

Transport 允许：

- 现有 WebSocket 承载 AG-UI-encoded events；
- 新增标准 HTTP SSE AG-UI endpoint；
- 外部 host 使用 @ag-ui/client HttpAgent；
- 内部 React 使用 HiveAGUIClient wrapper。

同一 run 不允许同时由两个 transport 各自生成事实；它们只能订阅同一个 projector/outbox。

### 14.4 A2A

A2A 负责跨 Agent：

- task；
- message；
- status；
- artifact reference；
- deep link；
- capability discovery。

后续如公开 A2A JSON-RPC endpoint，需要在 Agent Card 中声明：

- 是否支持 A2UI extension；
- 支持的 catalogs；
- artifact type；
- auth；
- streaming；
- sensitivity；
- deep link fallback。

当前 Hive build_a2a_agent_card 明确不暴露公共 JSON-RPC task endpoint，因此文档不得假装 A2UI 已可通过 A2A 对外工作。

### 14.5 MCP Apps

MCP Tool 返回 app resource 时：

1. MCP authz 校验 server 与 tool；
2. host 解析 resource metadata；
3. sandbox policy 决定是否允许；
4. iframe 获得最小 bridge；
5. app action 转换为 governed intent；
6. 结果写 transcript/span；
7. 需要持久化时创建明确 Living Object 或 external app reference。

MCP App 本身不自动成为 Personal Knowledge 内容。

CopilotKit MCPAppsActivityRenderer/OpenGenerativeUIRenderer 只能作为参考实现或经 Hive wrapper 加固后使用。生产默认策略必须比其开源默认值更窄：

- 不允许 connect-src *；
- 不允许应用自行扩大 resourceDomains；
- scripts、same-origin、forms 分开授权；
- iframe 使用独立 origin；
- postMessage 校验 source、origin、method、schema 和 capability token；
- localApi 只暴露 allowlisted read/action intent；
- 所有写操作仍回到 ToolRuntimeService；
- 禁止 JWT、cookie、tenant secret 和宿主 DOM 泄漏。

### 14.6 Cloud Sandbox Surface Transport

Codex-style visualization 与 MCP Apps 可以复用同一个 Sandbox Host 基础设施，但必须使用不同 capability profile；不得让 visualization profile 因为底层复用了 MCP bridge 就自动获得 MCP/tool 能力。

传输分四段：

1. **Authenticated bootstrap**：主站向 Hive API 请求 sandbox session；服务端从登录态、ChatSession、Agent、SurfaceInstance 和对象 grant 重建权威。
2. **Isolated initialization**：主站加载独立 origin 的固定 shell，使用随机 instance nonce 建立 `MessageChannel`，不使用通配 `window.postMessage` 作为长期 RPC。
3. **Untrusted render**：shell 在内层无 same-origin frame 中加载签名 resource revision；fragment 只能看到 profile 允许的 shim。
4. **Governed effect**：client intent 回到主站后，主站调用受认证 API；服务端重新校验 CSRF/session、tenant、grant、capability、base revision、quota 与 idempotency，再进入 ToolRuntimeService。

云端多 replica 约束：

- bootstrap nonce、capability consumption、action idempotency 与 revoke state 使用 PostgreSQL/Redis 共享状态；
- WebSocket/SSE 只投影 committed event，不把某个 frontend replica 的内存当事实；
- sandbox shell 和静态 runtime 以 content hash/version 发布，旧 surface 可在兼容窗口内恢复；
- deploy 时 frontend shell、backend contract 与 catalog/profile version 必须经过 compatibility gate；
- production topology 保持 backend、backend-api、frontend 三服务，独立 origin 不等于新建第二 Runtime 服务。

### 14.7 不引入 CopilotKit 第二运行时

生产拓扑不新增 CopilotKit Node Runtime 或 Intelligence service。原因：

- Hive FastAPI 已拥有 durable RuntimeTask；
- ChatTranscriptEvent 已拥有 thread/run ordering；
- ToolRuntimeService 已拥有 tool authority；
- web_chat_broker/outbox 已拥有 delivery；
- RLS、owner、delegation 已拥有身份和权限；
- 新增 CopilotKit Runtime 会产生第二 agent runner、第二 thread lock、第二 persistence 和第二 auth gate。

如未来某个外部 host 强制要求 CopilotRuntime API，只能建设无状态 compatibility gateway；它不得持久化 thread、执行模型、执行工具或判定权限。

---

## 15. Office 的正确位置

### 15.1 目标

把 Office 从“默认在线编辑底座”降级为：

- import codec；
- export codec；
- validation；
- render；
- high-fidelity compatibility editor。

### 15.2 Native first

| 用户目标 | 默认对象 | Office 路径 |
|---|---|---|
| 多维项目库 | Dataset | 可导出 XLSX |
| 演示文稿 | Deck | 可导出 PPTX/PDF |
| 长报告 | Markdown Document | 可导出 DOCX/PDF |
| 已有复杂 PPTX 修改 | binary_office 或显式 import | OfficeCLI Agent 操作 + retained original |
| 已有复杂 XLSX 公式 | binary_office 或显式 import | OfficeCLI Agent 操作 + retained original |

### 15.3 导入

导入 Office 文件时必须生成：

- import preview；
- mapping report；
- unsupported feature report；
- source file hash；
- target object type；
- resulting revision；
- retained original file；
- rollback path。

PPTX import：

- 支持的文本、图片、表格、图表、notes 转为 Deck block；
- 无法转换的复杂元素保留为 image/sandbox fallback；
- 不删除原文件。

XLSX import：

- sheet 到 Dataset 或多个 Dataset；
- typed field inference 由 Agent 提议、用户确认；
- formula 映射到受支持 AST；
- unsupported macro 永不执行；
- merged cells、hidden sheets 和 external links 必须报告。

### 15.4 导出

导出必须：

- 固定 source revision；
- 记录 exporter version；
- 校验输出；
- 生成 preview；
- 将 warning 返回用户；
- 创建 LivingObjectExport；
- 通过 ChatArtifact 投递；
- 保留 object deep link。

### 15.5 Office Online 退役策略

Hive 的用户需求是查看 Office 内容与下载原文件，不再提供浏览器内 WYSIWYG、共同编辑、评论或审阅。因此当前权威路径是：

- Agent 继续通过 OfficeCLI create/view/query/apply/validate/dump 操作原始 Office 文件；
- Workspace preview 读取当前受权文件；ChatArtifact preview 优先读取 delivery-time snapshot；
- OfficeCLI HTML 只作为可重建派生物，通过鉴权 endpoint、严格 CSP 和无 `allow-same-origin` 的 sandboxed iframe 消费；
- HTML 失败进入明确、可观测的 text fallback；两种渲染均失败时仍保留原文件下载；
- 专用 Office 标签、在线 editor、callback、force-save、JWT、配置与本地部署脚手架全部删除；
- production 在线编辑服务只在三服务部署、三格式 smoke、变量清理和无网络依赖证据成立后最终删除。
- 迁移和回滚演练通过。

---

## 16. 权限、安全与多租户

### 16.1 Principal

受信任 principal 至少包含：

- tenant；
- authenticated user；
- current Agent；
- direct owner；
- creator；
- delegation chain；
- RuntimeTask；
- ChatSession；
- acting surface host。

前端只提交 intent，不提交可信身份结论。

### 16.2 RLS

所有 Living Object 相关表必须 tenant-scoped，并覆盖：

- object；
- revision；
- relation；
- grant；
- export；
- dataset field/row/view/event；
- surface instance；
- action receipt。

API 必须先 check_agent_access/check user access，再读取对象 grant。只凭 object_id 不得访问。

### 16.3 Sensitivity

建议等级：

- public；
- company；
- internal；
- confidential；
- restricted。

敏感度影响：

- host placement；
- Agent query；
- export；
- external A2A/A2UI；
- MCP App；
- Company Knowledge publish；
- screenshot/preview；
- telemetry payload。

### 16.4 外部 UI 安全

对 A2UI：

- 只允许已注册 catalog；
- props schema validation；
- action allowlist；
- payload limits；
- no executable code。

对 CopilotKit frontend tool / shared state：

- frontend tool 只允许 view-local、navigation、clipboard、download trigger 等浏览器上下文动作；
- 任何业务写入都必须调用受认证的 Hive API，并由服务端重新鉴权；
- shared state 只保存可重建 read model；
- 浏览器传入的 properties、tenantId、agentId、capability 或 action result 全部视为不可信；
- 默认 CopilotKit A2UI action forward 必须被 HiveActionBridge 抑制；
- 依赖中如启用匿名 telemetry，生产设置 COPILOTKIT_TELEMETRY_DISABLED=true，并用依赖测试防止升级后恢复外发。

对 MCP Apps / HTML：

- sandbox；
- CSP；
- origin isolation；
- no ambient credentials；
- bridge schema；
- time/memory/storage/network quota；
- audit；
- user-visible trust label。

### 16.5 云端 Sandbox Surface 安全边界

#### 16.5.1 身份与租户

- sandbox origin 不接收 Hive 登录 Cookie、Authorization header、CSRF token、refresh token 或 provider credential；
- principal、tenant、Agent、delegation、grant 与 sensitivity 全部由服务端从 authenticated request 重建，客户端同名字段只作 untrusted hint；
- 每次 bootstrap/action 都绑定 surface_id、resource_revision_id、session_id、principal_id、tenant_id、profile、expiry 与 policy version；
- object/resource 查询同时检查 tenant RLS、agent/user access 与 object grant，防止 IDOR；
- service-to-service credential 只存在于 backend，绝不进入 browser 或 resource payload。

#### 16.5.2 输入、输出与消息

- `surface_ref`、SandboxBootstrap、bridge message、ActionIntent、view state、download metadata 全部执行 syntactic + semantic schema validation；
- HTML fragment 不是可信模板，不能插入主站 DOM；所有可见渲染只发生在 sandbox frame；
- bridge 只接收固定 discriminated union，不接受动态 method dispatch、eval 字符串、函数源码或可执行 URL；
- file name、MIME、size、hash、resource manifest 与 export format 在服务端验证；
- error response 不包含 stack、SQL、内部路径、token、tenant detail 或原始敏感 payload；完整调查信息只进入受控日志。

#### 16.5.3 浏览器边界

- app 与 sandbox 使用不同 origin；生产 CORS/`frame-ancestors` 使用显式 allowlist，不使用 `*`；
- state-changing API 继续使用 Hive 已建立的 session/CSRF 或等价 authenticated request boundary，sandbox capability 不能替代用户认证；
- iframe 不允许 same-origin、forms、top navigation、popup、download、modals 或设备权限，例外只能按 profile 单独审批；
- 外链必须是 HTTPS、经过 URL parser 和 allow/deny policy，并在真实用户激活后显示目标域确认；
- download/export 由宿主调用服务端 endpoint，使用单次 capability 与 `Content-Disposition`，不让 iframe 任意导航到 data/blob 之外的下载源。

#### 16.5.4 数据披露与敏感度

- bootstrap 只发送当前视图所需字段、行/slide 分页和聚合，不发送完整对象、grant 表、隐藏列、speaker notes 或无关知识内容；
- confidential 默认禁止外链、clipboard、screenshot、public publish 和第三方 resource；restricted 默认禁止 Sandbox Surface；
- Personal Knowledge 仍是 Tool-only，不能因为打开 Sandbox Surface 就静态注入个人知识库；
- Company Knowledge 只投影当前 principal 已授权的 pinned/reviewed revision；
- telemetry 只记录 ID/hash/count/policy result，不记录 HTML、Dataset cell、Deck note、prompt 或 message body。

#### 16.5.5 网络与供应链

- sandbox 默认零 egress；公共 CDN、第三方字体、tracking pixel 与用户任意 URL 均禁止；
- 可视化库由 Hive 精确 pin、自托管、生成 SBOM，并在升级时进行 hash、license、CVE 与 sandbox conformance 验证；
- MCP App remote resource 在进入 shell 前完成 server/tool authz、资源快照、content-type/size/hash 校验与 policy decision；
- 服务端 remote resource fetch 必须防 DNS rebinding、redirect SSRF、localhost、RFC1918/link-local、cloud metadata 和非 HTTP(S) scheme；
- quarantined/revoked resource 不能从浏览器缓存或旧 signed URL 继续执行。

#### 16.5.6 滥用、配额与运营控制

- tenant/user/Agent/resource/profile 分层 rate limit；
- bootstrap、message、action、download、render crash 与 long-task 有独立 metric 和告警；
- action burst、oversized message、无限 resize、内存/CPU runaway、递归 worker 或大量 Blob URL 触发 terminate；
- CSP violation、bridge validation failure、cross-surface nonce、capability replay、denied egress 与 IDOR 进入安全事件流；
- 提供 tenant/profile/resource/global kill switch，以及不执行资源代码的静态 fallback；
- 所有 kill/revoke/restore 操作写审计，不通过客户端静默完成。

### 16.6 Prompt injection

Living Object 内容属于外部/用户数据，不自动成为 system instruction：

- Dataset cell；
- Deck note；
- imported Office content；
- sandbox app message；
- A2UI payload；
- A2A artifact。

进入 Agent context 时必须带来源和数据边界，不能提升为指令权威。

---

## 17. 并发、恢复与故障语义

### 17.1 Optimistic concurrency

所有内容 mutation 必须带：

- base_revision_id；
- object version 或 row_version；
- idempotency_key。

冲突时：

- 不 last-write-wins；
- 返回 current revision；
- 给出可机械合并与需语义合并的差异；
- Agent 可以生成 merge proposal；
- 用户编辑优先级由 policy 决定，但不能静默丢失任何提交。

### 17.2 Dataset

- row mutation 使用 row_version；
- schema mutation 使用 dataset revision；
- 大批量 import 使用 staged transaction；
- mutation event 可重放；
- 定期 checkpoint；
- formula/rollup rebuild 可恢复；
- retry 不得重复插入行。

### 17.3 Deck

- slide/block 使用 stable id；
- patch 声明 base revision；
- 不同 slide 可自动合并；
- 同 block 冲突生成 proposal；
- presenter 永远读取固定 revision；
- export 固定 revision。

### 17.4 Surface

- 断线后客户端提交 transcript/AG-UI cursor 与 last hiveSequence；
- server 返回缺失 AG-UI events 或完整 MESSAGES/STATE/ACTIVITY + A2UI recover snapshot；
- Surface cache 丢失时从对象重建；
- CopilotKit shared state/A2UI store 丢失时不得影响 Living Object；
- UI 未收到 action result 时用 idempotency_key 查询 receipt；
- Surface close 不取消 RuntimeTask；
- RuntimeTask 结束不删除 durable object。

对 Sandbox Surface 另外要求：

- reload/crash 使用同一 immutable resource_revision_id 重建，不读取最新同路径文件替代 delivery snapshot；
- bootstrap nonce 过期后必须重新走 authenticated bootstrap，不能在客户端延长；
- action response 丢失时使用 idempotency_key 查询同一 receipt，不重复执行；
- capability 已消费、revoke 或 policy 变化时返回 typed terminal/recoverable state；
- `SurfaceViewState` 只保存 schema 允许的展示状态，超过大小或版本不兼容时安全 reset；
- sandbox runtime 版本升级必须保留旧 profile 的明确兼容窗口或给出静态 fallback；
- 多 replica 重连依据 transcript sequence、resource revision、object revision 和 receipt，而不是命中原进程。

### 17.5 删除与归档

- 默认 archive；
- delete 进入 deleted_pending；
- 有引用关系时显示 impact；
- export 和 transcript provenance 不被级联抹除；
- 到期物理删除需要独立 retention job、审计和恢复窗口；
- Company published object 需要更严格 policy。

---

## 18. 与 Agent Memory、Personal Knowledge、Company Knowledge 的边界

### 18.1 Agent Memory

Agent Memory 记录 Agent 自身学习、owner/company context、open loop、capability evidence 等。

Living Object 可以成为 Memory 的 evidence ref，但：

- Dataset 不自动复制到 T3；
- Deck 不自动变成 Skill；
- Surface state 不进入 soul；
- Memory Gate 仍决定是否形成长期记忆；
- Platform Gate 仍负责 evidence、dedupe、rollback 和 commit。

### 18.2 Personal Knowledge

Personal Knowledge 保存：

- 用户所有的知识文档；
- source、segment、entity、assertion、link；
- profile；
- grant；
- Living Object reference、collection 与摘要投影。

Living Object 自身内容仍由 object type authority 管理。

### 18.3 Company Knowledge

Company Knowledge 保存已发布、已审查、公司有权消费的内容和 immutable object publication references。Company Knowledge 当前在 Hive checkout 中仍是 Missing；本文描述的是 Living Object 与未来 Company authority 的目标边界，不是已实现状态。

private Dataset 不能因为被一个 Agent 引用、出现在 A2A message 或被 Personal KB 收藏就自动变为 company-visible。发布必须固定 revision 或声明 `reviewed_follow`，并由 Company proposal/review/publish 产生独立 ACL/version/retention。

Agent 对 published reference 的读取必须走 `search_company_kb -> read_company_kb`；Company Knowledge 内容不进入初始 prompt。真正的 object query/mutation 仍需同时通过 Living Object grant 与 ToolRuntime/Workflow/Approval。

### 18.4 Skill 与 Workflow

- Skill 可以包含如何创建/使用某类 Living Object 的说明、模板、script 和 eval；
- Workflow 可以创建、查询、审批和导出对象；
- Living Object 不是 Skill；
- Living Object 不是 Workflow；
- Surface action 不直接执行 workflow definition，必须走 start_workflow 或 ToolRuntimeService；
- AIAssetRecord 继续索引 Skill/Workflow 等 capability，不索引每个用户 Dataset row 或 Deck slide。

---

## 19. 精确代码落点

本节区分“现有扩展点”和“建议新增文件”。文件名是后续单轮实施的 canonical touchpoint；实现前应再次核对最新 checkout。

### 19.1 Backend：新增领域层

建议新增：

- backend/app/models/living_object.py
  定义 LivingObjectRecord、LivingObjectRevision、LivingObjectRelation、LivingObjectGrant、LivingObjectExport。

- backend/app/models/living_surface.py
  定义 SurfaceInstance、SurfaceActionReceipt、SandboxResourceRevision、SandboxSessionCapability；仅保存恢复、授权消费和审计所需状态，不保存浏览器任意 local state。

- backend/app/models/living_dataset.py
  定义 DatasetField、DatasetRow、DatasetView、DatasetMutationEvent、DatasetCheckpoint。

- backend/app/services/living_objects.py
  对象创建、读取、revision commit、archive、relation、grant 与 conflict。

- backend/app/services/living_object_authority.py
  principal、owner、delegation、grant、sensitivity 和 RLS 前置判断。

- backend/app/services/living_object_schemas.py
  object type 与 revision payload schema registry。

- backend/app/services/living_datasets.py
  schema、row mutation、query、view、formula、bulk import、checkpoint。

- backend/app/services/living_decks.py
  Deck AST、patch、proposal、theme、binding、revision。

- backend/app/services/surface_runtime.py
  SurfaceInstance、placement/lifetime、A2UI metadata、recover snapshot 与 host capability negotiation；不自造 wire protocol。

- backend/app/services/surface_actions.py
  ActionIntent 到 ToolRuntimeService 的治理桥，以及 receipt/idempotency。

- backend/app/services/surface_catalogs.py
  catalog metadata、version、schema、fallback。

- backend/app/services/surface_sandbox.py
  创建 immutable SandboxResourceRevision、执行 scan/quarantine、生成 bootstrap、选择 capability profile、校验 resource hash 与构造静态 fallback；不执行资源代码。

- backend/app/services/surface_capabilities.py
  生成并消费短期 nonce/action handle，绑定 tenant/principal/surface/resource/profile/policy/version，使用数据库或 Redis 支持多 replica、revoke 与 replay detection。

- backend/app/services/surface_resource_fetch.py
  受控抓取 MCP App/外部 resource，执行 URL scheme、DNS/IP、redirect、content-type、size、hash、malware/policy 与 SSRF 校验；浏览器不直接抓取。

- backend/app/services/ag_ui_projection.py
  将 committed RuntimeTask、ChatTranscriptEvent、InvocationSpan、Living Object 和 workflow/subagent state 确定性映射为 AG-UI events。

- backend/app/services/ag_ui_recovery.py
  从 transcript sequence 与对象 read model 生成 MESSAGES_SNAPSHOT、STATE_SNAPSHOT、ACTIVITY_SNAPSHOT 和 A2UI recover payload。

- backend/app/services/ag_ui_stream.py
  统一 AG-UI event outbox、SSE/WebSocket encoding、subscriber cursor 与 backpressure；不得持久化第二份 run truth。

- backend/app/services/living_object_exports.py
  export job、provenance、validation、ChatArtifact delivery。

- backend/app/services/living_object_office_adapter.py
  DOCX/XLSX/PPTX 与 native object 的 import/export mapping。

- backend/app/api/living_objects.py
  object、revision、relation、grant、query、export API。

- backend/app/api/surfaces.py
  open、recover、sandbox-session、action、download/export capability 与 catalog/profile negotiation API；高频 patch 可走 WebSocket。所有 state-changing route 复用现有认证/CSRF/authority boundary。

- backend/app/api/ag_ui.py
  提供标准 RunAgentInput/AG-UI SSE compatibility endpoint，以及只读 recover/subscribe endpoint；实际 run 启动仍委托现有 RuntimeTask/web chat command。

- backend/app/tools/handlers/living_objects.py
  Agent 高阶 typed tools。

- backend/app/tools/handlers/living_datasets.py
  Dataset 专属 Agent tools。

- backend/app/tools/handlers/living_decks.py
  Deck 专属 Agent tools。

### 19.2 Backend：现有文件的精确扩展点

- backend/app/tools/service.py
  保持 ToolRuntimeService.execute 为唯一受治理入口；注册 surface action source、object authority context、receipt 与 post-tool revision evidence。不得从 surface handler 调用 _execute_without_governance。

- backend/app/services/chat_transcript.py
  append_session_event 继续写运行真相；新增 object_created、object_revision_committed、surface_presented、surface_action_result 等 typed event payload，供 AG-UI projector 消费；不写高频纯视图状态。

- backend/app/services/web_chat_runtime.py
  在 _append_artifact_delivery_event 相邻路径增加 Living Object delivery event；RuntimeTask 完成时不得销毁 durable object。

- backend/app/services/web_chat_run_orchestrator.py
  保持 durable run state machine 和 fencing authority；只向 AG-UI outbox 发布 committed lifecycle，不允许 CopilotKit client 直接决定 run terminal state。

- backend/app/services/chat_artifact_delivery.py
  _preview_kind_for_path 保留文件 fallback；新增显式 object_ref/surface preview、SandboxResourceRevision 和 delivery snapshot hash，不再仅靠 suffix 推断所有交付物；声明 snapshot 丢失时禁止回退读取同路径最新文件。

- backend/app/models/chat_artifact.py
  新增可空 living_object_id、living_object_revision_id、living_object_export_id；现有 path/name/mime 行为保持兼容。

- backend/app/services/chat_message_parts.py
  增加 living_object_ref 与服务端签发的 surface_ref part 序列化/校验；模型自然语言中的伪 directive 不得触发资源读取。

- backend/app/api/chat_sessions.py
  _serialize_transcript_event 识别新的 typed object/surface events；session history 可恢复对象交付。

- backend/app/services/web_chat_broker.py
  fanout 原有 session event 与 AG-UI projection；不成为 durable state。

- backend/app/services/personal_knowledge_service.py
  增加 living_object_ref ingest/link/query；不把 Dataset rows 复制到 Markdown，不改变 Tool-first 边界。

- backend/app/api/agent_knowledge.py
  增加 owner-scoped object reference 与 collection API。

- backend/app/services/interoperability.py
  build_a2a_agent_card 在公共 A2A task endpoint 真正落地后才声明 A2UI/A2A extension；未落地继续 not_exposed。

- backend/app/agents/orchestrator.py
  _project_a2a_artifact_refs_to_parent_session 扩展为 object reference projection，保留 workspace/tenant boundary 和 no-copy provenance。

- backend/app/services/office_document_service.py
  OfficeDocumentService 的 run_view/run_query/run_validate/run_apply 作为 binary Office compatibility path；native object 操作不绕经 OfficeCLI。

- backend/app/services/officecli_adapter.py
  保留 command allowlist；新增 import/export adapter 时禁止 macro 和任意 command。

- backend/app/main.py
  注册 living_objects 和 surfaces router。

- backend/app/models/__init__.py
  注册新增 model。

- backend/app/db_bootstrap.py
  仅处理启动期 schema guard；正式 schema 仍由 Alembic。

- backend/app/services/invocation_trace.py
  为 object/surface action 记录 canonical invocation spans 和 object revision refs。

- backend/app/config.py
  增加 app/sandbox origin allowlist、CSP profile、resource/message/quota、bootstrap TTL、self-hosted library manifest 与 kill-switch 配置；生产启动时拒绝 wildcard CORS、宽域 Cookie、公共 CDN 或 sandbox origin 缺失。

### 19.3 Migration

建议单一完整迁移：

- backend/alembic/versions/living_object_native_surface_0712.py

迁移必须一次包含：

- object/revision/relation/grant/export；
- dataset 专属表；
- surface instance/action receipt；
- sandbox resource revision/session capability/revoke state；
- ChatArtifact nullable refs；
- indexes；
- constraints；
- tenant RLS；
- downgrade；
- legacy backfill markers；
- no destructive data conversion。

这不是要求与当前未提交的 ai_asset_control_plane_0710.py 使用同一个 revision id；实际实施时必须先读取最新 Alembic heads 并生成正确 down_revision。

### 19.4 Frontend：新增 Surface runtime

建议新增：

- frontend/src/api/domains/livingObjects.ts；
- frontend/src/api/domains/surfaces.ts；
- frontend/src/lib/agent-ui/HiveAGUIClient.ts；
- frontend/src/lib/agent-ui/agUIEventProjector.ts；
- frontend/src/lib/agent-ui/copilotKitVersionGuard.ts；
- frontend/src/components/surfaces/SurfaceHost.tsx；
- frontend/src/components/surfaces/SurfaceBoundary.tsx；
- frontend/src/components/surfaces/SurfaceRecovery.tsx；
- frontend/src/components/surfaces/copilotkit/HiveA2UIHost.tsx；
- frontend/src/components/surfaces/copilotkit/HiveActionBridge.ts；
- frontend/src/components/surfaces/copilotkit/HiveA2UIRecovery.tsx；
- frontend/src/components/surfaces/catalogs/definitions.ts；
- frontend/src/components/surfaces/catalogs/registry.tsx；
- frontend/src/components/surfaces/actionClient.ts；
- frontend/src/components/surfaces/sandbox/SandboxSurfaceHost.tsx；
- frontend/src/components/surfaces/sandbox/SandboxBridge.ts；
- frontend/src/components/surfaces/sandbox/SandboxFrame.tsx；
- frontend/src/components/surfaces/sandbox/sandboxMessages.ts；
- frontend/src/components/surfaces/sandbox/sandboxProfiles.ts；
- frontend/src/components/surfaces/sandbox/StaticSandboxFallback.tsx；
- frontend/src/components/surfaces/catalogs/core/；
- frontend/src/components/surfaces/catalogs/data/；
- frontend/src/components/surfaces/catalogs/deck/；
- frontend/src/components/surfaces/catalogs/knowledge/；
- frontend/src/components/surfaces/catalogs/runtime/；
- frontend/src/pages/living-object/LivingObjectWorkbench.tsx。

frontend/package.json 精确 pin：

- @copilotkit/a2ui-renderer 1.62.3；
- @ag-ui/core 0.0.57；
- @ag-ui/client 0.0.57；
- zod 3.25.76。

不得使用 caret、tilde 或 latest。@copilotkit/react-core 只有在明确消费其开放 hook/renderer 且不引入 CopilotRuntime authority 时才能加入。

### 19.5 Frontend：现有精确扩展点

- frontend/src/pages/agent-detail/ArtifactSurface.tsx
  在现有 ArtifactPreviewPanel/ArtifactSurface 中新增 living_object_ref、AG-UI activity 与 A2UI/sandbox surface 路由。现有 image/PDF/Markdown/text fallback 保留；Office preview 与开放表达 sandbox 使用不同 profile 和资源契约。

- frontend/src/pages/agent-detail/AgentChatSection.tsx
  只负责把 session timeline 与选中对象交给 ArtifactSurface/SurfaceHost，不重新实现 A2UI store。

- frontend/src/pages/agent-detail/chatRuntime.ts
  扩展 ChatArtifactPart 或新增 LivingObjectPart/SurfacePart 类型，带 object_id、revision、surface_id、resource_revision_id、sandbox_profile、placement 和 fallback；AG-UI projection 不覆盖现有 transcript replay identity。

- frontend/src/pages/agent-detail/sessionSocketEventProjector.ts
  将现有 WebSocket payload 归一到同一 AG-UI event consumer；投影 surface_presented/resource_revoked/action_result/recovery cursor，防止 WebSocket 与 SSE 双投影重复消费。

- frontend/src/components/MarkdownRenderer.tsx
  保持 `skipHtml`、rehype sanitize 与 URL policy；不得为了支持 Sandbox Surface 在主站 Markdown DOM 中执行 HTML。Surface 只通过结构化 message part 进入 SurfaceHost。

- frontend/src/main.tsx 与部署静态入口
  主 app bundle 不包含执行任意 fragment 的代码路径；sandbox shell 使用独立 entry、独立 origin security headers、固定 runtime version 和最小 bridge bundle。

- frontend/src/pages/agent-detail/ArtifactSurface.tsx
  Office binary 通过鉴权 Blob 与 sandboxed iframe 在聊天旁 inspector 预览；native Dataset/Deck 进入 SurfaceHost，不复用 binary Office preview。

- frontend/src/pages/PersonalKnowledge.tsx
  LibraryPanel 增加 ObjectCollection 和 object ref；Profile、Graph、Grants 语义保持；不得把 Personal KB 改成通用 SaaS 文件中心。

- frontend/src/api/domains/knowledge.ts
  增加 LivingObjectRef 类型，不把对象完整内容塞进 knowledge DTO。

- frontend/src/App.tsx 或当前路由定义文件
  增加 /objects/:objectId 可分享的受权 deep link 与 fullscreen placement。

### 19.6 AIAssetRecord 边界

当前 checkout 中的：

- backend/app/models/ai_asset.py；
- backend/app/services/ai_assets.py；
- backend/app/services/ai_asset_adapters.py；
- backend/app/api/ai_assets.py；

继续用于 Agent/Skill/Workflow/Subagent/External Capability 的薄控制索引。

禁止：

- 给 AIAssetRecord 增加 Dataset rows；
- 把 Deck revision 塞进 asset metadata；
- 让 AIAsset lifecycle 代替 Living Object owner/grant；
- 为了“统一”而移除各 native runtime 的权威。

二者可以通过 relation 关联，例如“某个 Skill 创建了某个 Living Object”，但不是同一实体。

---

## 20. API Contract 草案

### 20.1 Object

- POST /api/living-objects
- GET /api/living-objects/{object_id}
- GET /api/living-objects/{object_id}/revisions
- GET /api/living-objects/{object_id}/revisions/{revision_id}
- POST /api/living-objects/{object_id}/proposals
- POST /api/living-objects/{object_id}/mutations
- POST /api/living-objects/{object_id}/rollback
- POST /api/living-objects/{object_id}/relations
- GET /api/living-objects/{object_id}/grants
- POST /api/living-objects/{object_id}/exports

### 20.2 Dataset

- GET /api/living-objects/{object_id}/dataset/schema
- POST /api/living-objects/{object_id}/dataset/schema/proposals
- POST /api/living-objects/{object_id}/dataset/query
- POST /api/living-objects/{object_id}/dataset/rows:batch
- PATCH /api/living-objects/{object_id}/dataset/rows/{row_id}
- GET /api/living-objects/{object_id}/dataset/views
- POST /api/living-objects/{object_id}/dataset/views
- PATCH /api/living-objects/{object_id}/dataset/views/{view_id}

### 20.3 Deck

- GET /api/living-objects/{object_id}/deck
- POST /api/living-objects/{object_id}/deck/proposals
- POST /api/living-objects/{object_id}/deck/patches
- POST /api/living-objects/{object_id}/deck/render

### 20.4 Surface

- POST /api/surfaces/open
- GET /api/surfaces/{surface_id}/recover?after_sequence=N
- POST /api/surfaces/{surface_id}/actions
- POST /api/surfaces/{surface_id}/close
- GET /api/surfaces/catalogs

Sandbox 扩展：

- POST /api/surfaces/{surface_id}/sandbox-sessions
- POST /api/surfaces/{surface_id}/sandbox-sessions/{session_id}/refresh
- POST /api/surfaces/{surface_id}/sandbox-actions
- POST /api/surfaces/{surface_id}/sandbox-downloads
- POST /api/surfaces/{surface_id}/sandbox-close
- GET /api/surfaces/sandbox-profiles

`sandbox-sessions` response 必须：

- `Cache-Control: no-store`；
- 不返回 JWT、Cookie、provider credential 或内部 signed storage URL；
- 返回短期 instance_nonce、resource/content hash、profile/schema/policy version、最小 projection 与 action handles；
- 将 capability 绑定 authenticated principal、tenant、surface、resource revision 和 expiry；
- 支持 revoke 与多 replica consume；
- 对 `restricted`、quarantined、revoked、unsupported profile 返回 typed denial/fallback。

`sandbox-actions` 不能相信 body 中的 tenant/user/agent；必须从 server session 重建 principal，并验证 CSRF/auth、surface grant、capability handle、action schema、base revision、sequence、idempotency 与 policy version。

所有 mutation response 必须返回：

- receipt_id；
- object_id；
- previous_revision_id；
- resulting_revision_id；
- idempotency_key；
- status；
- transcript_event_id；
- invocation_span_id；
- warnings；
- recoverable_error。

---

## 21. 迁移、回填与兼容

### 21.1 原则

- 现有 Markdown、Office、图片、PDF 和普通 workspace 文件全部可继续打开；
- 不批量猜测“哪个文件应该变成 Living Object”；
- backfill 只增加引用和分类，不修改文件内容；
- native import 必须 dry-run/preview；
- 旧 ChatArtifact 不要求重写历史；
- 新交付优先使用 explicit object ref；
- suffix renderer 作为兼容 fallback 保留到验收完成。

### 21.2 回填分类

1. 普通 Markdown
   保持 workspace file truth；可选择生成 LivingObjectRecord 引用，不移动文件。

2. CSV/TSV
   默认仍为文件；用户或 Agent 显式“转为数据集”时导入 Dataset。

3. XLSX
   默认 binary_office；显式 import 后生成 Dataset 和 mapping report。

4. PPTX
   默认 binary_office；显式 import 后生成 Deck 和 unsupported report。

5. 历史 HTML artifact
   默认 read-only sandbox 或静态 preview；不自动变成 trusted native Surface。

6. 历史 ChatArtifact
   保持 path-based delivery；新 nullable object refs 不回填伪造值。

### 21.3 兼容窗口结束条件

只有满足以下条件，才可清理旧逻辑：

- 新旧 artifact preview 回归通过；
- 历史 Session 可正常恢复；
- Office 文件可继续编辑和下载；
- Personal KB 文档无丢失；
- object ref 有 fallback；
- 生产 telemetry 显示无未处理类型；
- rollback 演练通过；
- 用户确认 destructive cleanup。

---

## 22. 可观测性与证据

### 22.1 事件

至少记录：

- living_object.created；
- living_object.revision_committed；
- living_object.conflict_detected；
- living_object.rollback_committed；
- living_object.grant_changed；
- living_object.export_requested/completed/failed；
- surface.opened/recovered/closed；
- surface.action_requested/pending/completed/failed；
- sandbox.resource_created/scanned/quarantined/revoked；
- sandbox.session_issued/expired/terminated；
- sandbox.bridge_rejected/capability_replayed；
- sandbox.egress_denied/quota_exceeded/csp_violated；
- dataset.import_started/completed/failed；
- deck.render_started/completed/failed；
- company_publish.proposed/approved/rejected。

### 22.2 Span attributes

InvocationSpan 增加：

- living_object_id；
- living_object_type；
- base_revision_id；
- result_revision_id；
- surface_id；
- surface_action；
- receipt_id；
- catalog；
- host_kind；
- approval_id；
- export_id；
- sandbox_profile；
- sandbox_resource_revision_id；
- sandbox_policy_version；
- sandbox_capability_id_hash；
- sandbox_denial_reason。

敏感内容不写 telemetry，使用 ID、hash、count 和 policy result。

### 22.3 指标

- object create success/failure；
- revision conflict rate；
- action idempotent replay count；
- surface recover rate；
- unsupported component fallback rate；
- sandbox denial/error；
- sandbox bootstrap/action latency；
- sandbox active frames by tenant/profile；
- sandbox bridge validation failure；
- sandbox capability replay/expiry/revoke；
- sandbox CSP violation/denied egress；
- sandbox CPU long-task/crash/terminate；
- sandbox payload/download/quota rejection；
- Dataset query latency；
- Deck render/export latency；
- Office import unsupported feature count；
- approval conversion；
- object -> Personal KB save；
- Personal -> Company publish；
- orphan object count；
- stale derived relation count。

---

## 23. 测试策略与 TDD 顺序

后续实现必须按 Red → Green → Refactor 执行。下列测试不是“以后补”，而是同一轮交付的一部分。

### 23.1 Backend unit/domain

建议新增：

- backend/tests/models/test_living_object.py；
- backend/tests/models/test_living_dataset.py；
- backend/tests/services/test_living_objects.py；
- backend/tests/services/test_living_object_authority.py；
- backend/tests/services/test_living_datasets.py；
- backend/tests/services/test_living_decks.py；
- backend/tests/services/test_surface_runtime.py；
- backend/tests/services/test_surface_actions.py；
- backend/tests/services/test_ag_ui_projection.py；
- backend/tests/services/test_ag_ui_recovery.py；
- backend/tests/services/test_ag_ui_stream.py；
- backend/tests/services/test_surface_sandbox.py；
- backend/tests/services/test_surface_capabilities.py；
- backend/tests/services/test_surface_resource_fetch.py；
- backend/tests/services/test_living_object_exports.py；
- backend/tests/services/test_living_object_office_adapter.py。

必须覆盖：

- tenant isolation；
- owner/creator 区分；
- revision immutability；
- optimistic conflict；
- idempotent replay；
- rollback；
- relation no-cascade；
- grant/sensitivity；
- Dataset typed validation；
- formula；
- batch transaction；
- Deck patch/merge；
- Surface sequence gap/recover；
- ChatTranscriptEvent → AG-UI event deterministic mapping；
- AG-UI replay 不产生第二 run truth；
- AG-UI state/activity 只能投影 committed state；
- CopilotKit/A2UI action 不绕过 ToolRuntimeService；
- catalog fallback；
- sandbox denial；
- sandbox bootstrap 不泄露 Cookie、JWT、provider credential、内部 signed URL 或超出 projection 的对象字段；
- capability 与 principal、tenant、surface、resource revision、profile、policy version、expiry 完整绑定；
- expired、revoked、replayed、cross-surface、cross-resource、cross-tenant capability 全部 fail closed；
- 多 backend replica 同时 consume one-time capability 时只有一个成功，并返回同一 idempotent receipt；
- remote resource fetch 拒绝 loopback、link-local、RFC1918、metadata endpoint、DNS rebinding、超限 redirect 与不允许的 MIME/size；
- quarantined/revoked resource 无法创建新 session，已有 session 被终止；
- `restricted` 数据不进入开放表达 sandbox，`confidential` 按 policy 禁止 clipboard/screenshot/export/external link；
- sandbox view-state reset 不改变 Living Object revision；
- export provenance。

### 23.2 API

建议新增：

- backend/tests/api/test_living_objects_api.py；
- backend/tests/api/test_living_datasets_api.py；
- backend/tests/api/test_living_decks_api.py；
- backend/tests/api/test_surfaces_api.py。
- backend/tests/api/test_ag_ui_api.py。
- backend/tests/api/test_surface_sandbox_api.py。

特别验证：

- 客户端伪造 tenant/user/agent 无效；
- 没有 grant 的 Agent 无法 query；
- read-only grant 不能 action；
- publish/export 高风险需要 checkpoint；
- duplicate idempotency_key 返回同 receipt；
- stale base revision 返回 structured conflict；
- RunAgentInput 不能伪造 tenant/user/agent；
- SSE 与 WebSocket 订阅相同 committed cursor；
- client disconnect 不取消 RuntimeTask。
- sandbox bootstrap/action/download endpoint 的 CORS、CSRF、auth、IDOR、rate limit 与 `Cache-Control: no-store`；
- body、query、header 中伪造 tenant/user/agent/resource/profile/policy version 均不能提升权限；
- response、URL、日志与错误体不含 credential、nonce 明文、内部路径或堆栈；
- sandbox origin 配置缺失、与 app origin 相同、通配 CORS、非 HTTPS 或 Cookie scope 过宽时 production startup fail closed。

### 23.3 Migration

建议新增：

- backend/tests/migrations/test_living_object_native_surface_migration.py。

覆盖：

- upgrade/downgrade；
- constraints；
- indexes；
- RLS；
- ChatArtifact nullable refs；
- 现有数据不变；
- 多 Alembic head 防护；
- 大表迁移锁风险。

### 23.4 Transcript 与 runtime

扩展：

- backend/tests/services/test_chat_transcript.py；
- backend/tests/services/test_chat_artifact_delivery.py；
- backend/tests/services/test_web_chat_runtime.py；
- backend/tests/services/test_interoperability.py；
- backend/tests/agents/test_orchestrator.py。

覆盖：

- object event 顺序；
- T0 projection；
- disconnect 不取消 run；
- surface cache 丢失可恢复；
- A2A ref no-copy；
- 公共 A2A 未落地时继续 not_exposed。

### 23.5 Frontend

建议新增：

- frontend/src/components/surfaces/SurfaceHost.test.tsx；
- frontend/src/components/surfaces/SurfaceRecovery.test.tsx；
- frontend/src/components/surfaces/actionClient.test.ts；
- frontend/src/components/surfaces/copilotkit/HiveA2UIHost.test.tsx；
- frontend/src/components/surfaces/copilotkit/HiveActionBridge.test.ts；
- frontend/src/components/surfaces/copilotkit/HiveA2UIRecovery.test.tsx；
- frontend/src/components/surfaces/sandbox/SandboxSurfaceHost.test.tsx；
- frontend/src/components/surfaces/sandbox/SandboxBridge.test.ts；
- frontend/src/components/surfaces/sandbox/SandboxFrame.test.tsx；
- frontend/src/components/surfaces/sandbox/sandboxMessages.test.ts；
- frontend/src/components/surfaces/sandbox/sandboxProfiles.test.ts；
- frontend/src/components/surfaces/sandbox/StaticSandboxFallback.test.tsx；
- frontend/src/components/surfaces/catalogs/registry.test.tsx；
- frontend/src/lib/agent-ui/HiveAGUIClient.test.ts；
- frontend/src/lib/agent-ui/agUIEventProjector.test.ts；
- frontend/src/lib/agent-ui/copilotKitVersionGuard.test.ts；
- frontend/src/components/surfaces/catalogs/data/DataExplorer.test.tsx；
- frontend/src/components/surfaces/catalogs/deck/DeckEditor.test.tsx；
- frontend/src/pages/living-object/LivingObjectWorkbench.test.tsx。

覆盖：

- unknown component fallback；
- sequence gap；
- reconnect；
- optimistic edit；
- conflict UI；
- read-only mode；
- keyboard/accessibility；
- large-list virtualization；
- same object across inline/canvas/fullscreen；
- historical artifact fallback；
- CopilotKit default runAgent action forward 被抑制；
- A2UI custom catalog props 使用 Zod 校验；
- AG-UI duplicate/out-of-order event 不重复提交；
- STATE_DELTA divergence 请求 fresh snapshot；
- unsupported A2UI/CopilotKit version fail closed；
- frontend tool 不能直接完成 domain mutation。
- raw HTML/JavaScript 永不进入主站 Markdown DOM，也不能访问 host DOM、Cookie、localStorage、Service Worker 或 app API；
- outer shell 与 inner frame 的 sandbox、CSP、Permissions-Policy、referrer policy 和 origin 校验完全匹配 profile；
- bridge 只接受 nonce-bound MessageChannel 的版本化 schema，拒绝 window broadcast、未知 message、超限 payload 与重复 sequence；
- visualization profile 默认零网络、零 tool、零 domain mutation，缺少 user activation 时拒绝 follow-up、external link、download 与 clipboard；
- capability refresh、resource revoke、policy update、frame crash、timeout、memory/CPU quota 触发 typed fallback/terminate，不产生静默白屏；
- StaticSandboxFallback 不执行原始 fragment，且能恢复到 object deep link、下载受权快照或重新生成。

### 23.6 E2E

必须有以下完整场景：

1. Chat 创建 Dataset → inline → canvas → 编辑 → Agent 继续读取 → reload 恢复 → Personal KB 保存。
2. Dataset 生成 Deck → 绑定 pinned revision → 用户修改 → 冲突 proposal → 导出 PPTX/PDF。
3. Agent 无 grant 读取 private object → fail closed → 用户授权 → 成功。
4. 两个 Agent 同时修改 → 不丢数据 → merge proposal。
5. Surface WebSocket 断线 → recover → action receipt 不重复。
6. XLSX/PPTX import dry-run → warning → commit → 原文件保留 → rollback。
7. Personal object 发布 Company Knowledge → review → sensitivity enforcement。
8. unsupported A2UI component → native fallback/deep link，不白屏。
9. malicious MCP App → 无 credential、无越权 action。
10. 历史 ChatArtifact、Markdown、Office 文件仍可访问。
11. CopilotKit A2UI button → HiveActionBridge → ToolRuntimeService → receipt/revision；没有额外 browser-triggered Agent run。
12. AG-UI SSE/WebSocket 断线重连 → transcript cursor replay → Surface 与 run state 一致。
13. CopilotKit shared state 被清空 → durable Dataset/Deck 仍从 Living Object 完整重建。
14. 伪造 AG-UI properties/tenantId/agentId → server fail closed。
15. CopilotKit package 或 A2UI major 不匹配 → version guard 阻止渲染并提供 deep-link fallback。
16. 恶意开放表达 fragment 尝试读取 parent DOM、Cookie、localStorage、app API、跨 origin storage、摄像头/麦克风/地理位置 → 全部失败，主应用仍可用。
17. fragment 尝试直连公网 CDN、内网 IP、cloud metadata、DNS rebinding host 或任意 WebSocket → 默认拒绝；受权资源只经 server fetch、扫描和 immutable revision 进入。
18. 两个 backend replica 并发消费同一 action capability → 只有一个 governed effect；重试返回同 receipt，不重复 mutation。
19. tenant A 复制 tenant B 的 surface/resource/session/action handle → bootstrap、action、download 和 recover 全部 fail closed，审计无敏感泄漏。
20. resource 被 quarantine/revoke、grant/sensitivity/policy 被收紧 → 已打开 frame 收到 terminate，刷新后不能恢复旧权限，静态 fallback 可解释且可审计。
21. sandbox frame 崩溃、超时、前端部署切换、Redis 短暂丢失 → 从 immutable resource revision 与 transcript/receipt 恢复；未确认 action 不被误报成功。

### 23.7 验证命令

后续实现完成时至少执行：

~~~bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate
pytest tests/models/test_living_object.py tests/models/test_living_dataset.py -q
pytest tests/services/test_living_objects.py tests/services/test_living_datasets.py tests/services/test_living_decks.py -q
pytest tests/services/test_surface_runtime.py tests/services/test_surface_actions.py -q
pytest tests/services/test_surface_sandbox.py tests/services/test_surface_capabilities.py tests/services/test_surface_resource_fetch.py -q
pytest tests/services/test_ag_ui_projection.py tests/services/test_ag_ui_recovery.py tests/services/test_ag_ui_stream.py -q
pytest tests/api/test_living_objects_api.py tests/api/test_surfaces_api.py -q
pytest tests/api/test_ag_ui_api.py -q
pytest tests/api/test_surface_sandbox_api.py -q
pytest tests/migrations/test_living_object_native_surface_migration.py -q
pytest tests -q

cd /Users/rocky243/vc-saas/hiveclaw-main/frontend
npm ls @copilotkit/a2ui-renderer @ag-ui/core @ag-ui/client zod
npm run test -- --run
npm run build
~~~

文档本轮不新增逻辑，因此不伪造上述测试结果。

---

## 24. 七原子闭环验收

| 能力 | 输入 | 权威 | 执行 | 证据 | 恢复 | 消费 | 验收 |
|---|---|---|---|---|---|---|---|
| Dataset | Chat/tool/import/UI intent | owner、grant、tenant、delegation | ToolRuntimeService + DatasetRuntime | revision、mutation event、transcript、span | row version、checkpoint、idempotency | Grid/Board/Chart/Agent query/KB ref | typed、并发、E2E、迁移 |
| Deck | Chat/tool/UI patch/import | owner、grant、lock、publish policy | ToolRuntimeService + DeckRuntime | revision、proposal、export receipt | block merge、rollback、fixed presenter revision | editor/presenter/export/KB | patch、冲突、render、PPTX/PDF |
| Surface | Agent present/host open/A2UI action | host capability + object grant | AG-UI projection → CopilotKit renderer → HiveActionBridge → governed action | transcript sequence、AG-UI event、receipt、span | replay、STATE/ACTIVITY snapshot、A2UI rebuild | Chat/Canvas/Fullscreen/Knowledge | reconnect、fallback、a11y、default-forward suppression |
| Codex-style Sandbox Surface | governed create/present + server-signed `surface_ref` | authenticated bootstrap、tenant/RLS、grant、sensitivity、profile policy | dedicated-origin shell → no-same-origin inner frame → nonce-bound MessageChannel intent → server re-auth → ToolRuntimeService | immutable resource revision/hash、sandbox event、transcript、span、receipt | resource/object revision、transcript cursor、shared nonce/idempotency state、typed static fallback | Chat inline/Canvas/Workbench；稳定能力升级到 Native/A2UI | CSP/egress/IDOR/CSRF/SSRF/replay/quota/multi-replica/crash E2E |
| Personal KB object ref | owner save/Agent proposal | user-owned grant | PersonalKnowledgeService | Markdown ref、grant、object relation | broken-ref detection、revision pin | search/library/knowledge tools | no auto-injection、tenant isolation |
| Company publish | user/Agent proposal | company reviewer/policy | publish workflow/checkpoint | proposal/review/publish event | reject/revoke/republish | Company Knowledge surfaces/search | sensitivity、review、rollback |
| AG-UI/A2UI/CopilotKit | Hive run/object projection 与 UI action | auth、tenant mapping、catalog policy、server-side recheck | projector/client/renderer/action bridge | external event + local transcript/span/receipt | cursor replay、dedupe、version guard、deep link | Hive Web 与兼容 host | AG-UI/A2UI conformance + authz + action no-bypass |
| A2A | remote task/artifact/object ref | auth、collaboration policy、tenant boundary | A2A adapter → Hive runtime | external message + local transcript/span | retry、dedupe、deep link fallback | external Agent/host | conformance + authz + no-copy provenance |
| Office import/export | file/tool/user request | file access、object grant、export policy | Office adapter/CLI | mapping report、hash、export record | original retained、dry-run、rollback | native object/file delivery | fidelity、unsupported report、security |

只有一行的七列全部有当前真实消费路径，能力才能标记为“闭环”。任何 API、表、组件或 demo 单独存在都只能算局部闭环或断点。

---

## 25. 单轮完整施工账本

以下不是阶段路线图，而是同一轮实现必须全部完成的工作包。任何一个工作包缺失都不能把该架构称为落地。

### A. Contract 与测试先行

- 固化 object schema、Hive AG-UI mapping、Surface metadata extension、ActionIntent、Receipt；
- 固化 Dataset 与 Deck schema；
- 先写 unit、API、migration、frontend、E2E failing tests；
- 建立 CopilotKit 1.62.3、A2UI v0.9、AG-UI 0.0.57、catalog schema 与 protocol conformance fixtures。

### B. 数据模型与迁移

- Living Object core tables；
- Dataset tables；
- Surface/receipt tables；
- ChatArtifact refs；
- RLS、constraints、indexes；
- non-destructive backfill；
- downgrade 与 migration tests。

### C. 权威与治理

- principal/delegation；
- grants；
- sensitivity；
- ToolRuntimeService wiring；
- approval/checkpoint；
- Sandbox bootstrap principal 与 tenant/RLS/grant/sensitivity 绑定；
- 短期 capability 只缩小 server-side authority，不创建新 authority；
- action/download/follow-up 每次重新认证、重新授权并校验 CSRF、base revision、policy version 与 idempotency；
- no-bypass architecture tests。

### D. Dataset 完整域

- schema；
- rows；
- views；
- query；
- filter/sort/group/formula/rollup；
- import/export；
- concurrency；
- checkpoint/rebuild；
- Agent tools；
- DataExplorer 全视图。

### E. Deck 完整域

- AST；
- theme/layout/block；
- patch/proposal/merge；
- Dataset binding；
- editor/presenter；
- render/export/import；
- Agent tools。

### F. Surface runtime

- AG-UI projector/outbox/SSE/WebSocket；
- @ag-ui/client wrapper；
- CopilotKit A2UI provider/renderer wrapper；
- Hive Zod catalog；
- transcript sequence/cursor/replay；
- state/activity/A2UI snapshot/recover；
- placement/lifetime；
- HiveActionBridge/action intent/receipt/default-forward suppression；
- fallback；
- accessibility；
- hardened sandbox；
- 独立 sandbox origin 与 host-only app Cookie；
- 固定 outer shell + 无 `allow-same-origin` inner frame；
- immutable SandboxResourceRevision、扫描/quarantine/revoke；
- visualization、MCP App、Office preview profile 分离；
- authenticated no-store bootstrap + nonce-bound MessageChannel；
- 默认零网络、依赖 self-host/pin、server-side resource fetch；
- CPU/memory/time/payload/download quota、watchdog 与 tenant/global kill switch；
- typed action handles、user activation、static fallback；
- package/version guard；
- telemetry disabled。

### G. Chat 与 Workbench

- typed transcript events；
- AG-UI message/tool/activity projection；
- ChatArtifact object refs；
- inline/canvas/fullscreen；
- session recovery；
- object deep links；
- historical compatibility。

### H. Knowledge 生命周期

- Personal KB object ref/collection；
- Tool-first query；
- grants；
- Company proposal/review/publish；
- object relations；
- no-copy/no-auto-injection tests。

### I. 协议与外部宿主

- AG-UI 作为标准 Hive Web projection；
- @copilotkit/a2ui-renderer 作为 A2UI host；
- CopilotKit custom catalog；
- A2A artifact/deep link extension；
- hardened MCP Apps host；
- sandbox profile negotiation 与 capability downgrade，禁止 profile escalation；
- resource supply-chain scan、hash pin、quarantine/revoke；
- server-side remote fetch 的 SSRF/DNS rebinding/redirect/MIME/size 防护；
- catalog negotiation；
- version pin/upgrade contract；
- authz/conformance/fallback/no-bypass tests。

CopilotKit Runtime、Built-in Agent、Intelligence/Threads 明确排除在生产 authority 外；这不是待办或技术债，而是架构边界。

### J. Office 兼容

- import preview；
- mapping/unsupported report；
- native conversion；
- export provenance；
- original retention；
- OfficeCLI compatibility、preview 与 retained-original path；
- security/fidelity tests。

### K. 观测、恢复与生产验收

- spans/events/metrics；
- conflict/reconnect/retry/fault injection；
- orphan/stale relation jobs；
- load tests；
- full backend/frontend suite；
- production deployment 与三服务成功验证；
- production CSP/CORS/Cookie/Permissions-Policy/frame/egress probe；
- multi-replica capability replay、Redis failure、resource revoke、frame crash 与 deploy-switch fault injection；
- rollback drill；
- 文档与 capability status 回填。

---

## 26. Definition of Done

架构实现只有在以下全部成立时才算完成：

1. Agent 能从真实聊天入口创建 Dataset 与 Deck，不是测试专用 endpoint。
2. 用户能在 inline、canvas、fullscreen 中操作同一 object_id。
3. Agent 能读取用户编辑后的 revision，并基于它继续工作。
4. 所有 mutation 经过 ToolRuntimeService 或明确受治理的对象 action runtime。
5. tenant、owner、delegation、grant、sensitivity 全部 fail closed。
6. object、revision、transcript、span、export provenance 可机械关联。
7. 断线、重试、重复提交、进程重启、冲突和 rollback 有测试证据。
8. Dataset 不是 HTML 或 XLSX wrapper；Deck 不是 HTML 或 PPTX wrapper。
9. Markdown、现有 Office、历史 ChatArtifact 和 Personal KB 继续工作。
10. Personal KB 保存对象后仍保持 Tool-first，不出现自动全量注入。
11. Company publish 有 proposal/review/revoke/revision policy。
12. A2UI unsupported component 有确定 fallback。
13. MCP Apps 在 sandbox 中运行且无 ambient credential。
14. Office import/export 有 warning、hash、revision 和 rollback。
15. frontend test/build 与 backend full suite 零失败。
16. migration upgrade/downgrade、RLS 和旧数据回归通过。
17. 生产三服务 deployment 为 SUCCESS，并完成健康与关键 E2E 验证。
18. 原有 suffix preview 只有在新链路覆盖和生产验证后才允许清理。
19. AIAssetRecord 与 LivingObjectRecord 权威不混合。
20. 七原子矩阵全部能指向当前 checkout 的真实消费路径。
21. RuntimeTask/Transcript/tool/activity 到 AG-UI 的映射是确定性的，并有 replay fixture。
22. @copilotkit/a2ui-renderer、@ag-ui/core、@ag-ui/client 与 zod 使用精确 pin，并通过 version guard。
23. 每个 CopilotKit A2UI domain action 都被 HiveActionBridge 拦截，默认 runAgent forward 不会发生。
24. 清空 CopilotKit shared state 或 Surface cache 后，durable object 能从 Hive truth 完整恢复。
25. 生产仍只有 Hive backend、backend-api、frontend 三服务；没有第二 CopilotKit agent/thread authority。
26. MCP Apps/Open Generative UI 的 CSP、origin、network、postMessage、localApi 和 credential isolation 全部通过安全测试。
27. CopilotKit anonymous telemetry 在生产被明确禁用并有配置回归测试。
28. 开放表达只运行在独立 sandbox origin 的双层 frame 中；原始 HTML/JavaScript 永不进入主站 Markdown DOM 或主 app bundle 的执行路径。
29. sandbox 默认拿不到 app Cookie/JWT、主站 API CORS、provider credential、内部 signed URL、ambient storage、Service Worker、默认公网 egress 或公共 CDN。
30. visualization、MCP App、Office preview 使用不同 capability profile；visualization profile 没有 tool、domain mutation 或任意 network 权限。
31. follow-up、download、external link 和 domain action 需要明确 user activation/confirmation；domain action 必须由 server 重建 principal 并重新验证 RLS/grant/policy/approval/idempotency 后进入 ToolRuntimeService。
32. 每份可执行 fragment 都有 immutable resource revision、content hash、runtime/profile/schema/policy version、扫描结果、provenance 和 revoke 状态。
33. capability 的 issue/consume/revoke/expiry/idempotency 在 PostgreSQL/Redis 共享状态中对多 replica 正确；URL、日志、localStorage、错误体和浏览器 Cookie 不含 capability secret。
34. 自动化测试与生产 probe 覆盖 CSP、CORS、Cookie scope、frame sandbox、Permissions-Policy、external link、download、SSRF、IDOR、CSRF、replay、quota、kill switch。
35. `restricted` 数据禁止进入开放表达 sandbox；`confidential` 的 clipboard/screenshot/export/external link 按明确 policy fail closed；telemetry 只记录 ID/hash/count/policy result。
36. frame crash、资源撤销、权限收紧、前端部署切换、Redis 短暂不可用均有 typed recovery/terminate/static fallback，且不会重复副作用或伪造成功 receipt。

---

## 27. 明确不做什么

- 不把 A2UI 当数据库或内容真相；
- 不把 AG-UI event stream 当成第二份 ChatTranscriptEvent；
- 不让 CopilotKit Runtime、Built-in Agent 或 Intelligence 接管 Hive runtime；
- 不把 CopilotKit shared state 当成 Living Object store；
- 不让 CopilotKit frontend tool 直接执行 domain mutation；
- 不使用 CopilotKit 默认 A2UI action forward 触发无治理 Agent run；
- 不使用 latest/caret 自动升级 CopilotKit/AG-UI/A2UI 依赖；
- 不让模型生成任意 React/JavaScript 进入主进程；
- 不把“开放表达”解释成开放网络、开放 app API、开放工具、开放 Cookie、开放同源或开放宿主 DOM；
- 不在主站 DOM、MarkdownRenderer 或普通 React tree 中执行不受信任 HTML/JavaScript；
- 不让 capability handle 代替 authentication、CSRF、RLS、grant、sensitivity、approval 或 idempotency；
- 不把 nonce/capability/JWT/credential 写入 URL、localStorage、日志、错误体或宽域浏览器 Cookie；
- 不从公共 CDN 加载 sandbox runtime，不允许 fragment 直连 tracking、第三方资源、内网或 metadata endpoint；
- 不用 iframe 承载所有核心功能；
- 不把 Markdown 废弃；
- 不把 Personal KB 变成自动 prompt dump；
- 不把 AIAssetRecord 变成万能 Asset 表；
- 不把 Dataset 等同 XLSX；
- 不把 Deck 等同 PPTX；
- 不为了协议“兼容”而公开尚未完成的 A2A capability；
- 不静默复制跨 Agent、跨知识域或跨 tenant 的对象；
- 不用默认关闭 feature flag 隐藏半完成路径；
- 不先交一个只有 Table 和 Deck demo 的 MVP；
- 不在缺少迁移、回填、恢复、消费和验收时声称能力落地。

---

## 28. 已锁定的架构决策

### ADR-01：Living Object 与 Surface 分离

对象是长期事实，Surface 是可替换投影。接受。

### ADR-02：AG-UI 标准投影协议，Hive extension 最小化

不自造 hive.surface.v1 wire protocol；AG-UI 负责 Agent/UI 事件，Hive 只扩展 object、revision、placement、authority hint 与 provenance。ChatTranscriptEvent/RuntimeTask 继续是内部权威。接受。

### ADR-03：CopilotKit A2UI renderer + Hive 高阶 React catalog

@copilotkit/a2ui-renderer 负责标准 processing/rendering；核心表格、Deck、知识和运行控制使用 Hive Zod definitions + 受信任 React renderers。接受。

### ADR-04：开放表达按 profile 隔离，不能混成一个万能 iframe

Codex-style visualization、MCP App 与 Office preview 分别使用不同输入、CSP、网络、action、下载和敏感度 profile；它们都不是默认对象模型或核心 renderer。CopilotKit 与 Codex Desktop 只提供交互和隔离参考，Hive Cloud 必须使用自己的 server-authoritative bootstrap、resource revision、capability bridge 与审计。接受。

### ADR-05：按对象类型选择唯一 canonical source

Markdown、relational dataset、structured revision、workspace binary 各有明确边界。接受。

### ADR-06：Office 是 codec 与兼容层

Native Dataset/Deck 不依赖外部在线 Office 编辑器才能运行。接受。

### ADR-07：Personal KB 保存引用，不复制结构化真相

继续保持 user-owned 与 Tool-first。接受。

### ADR-08：所有交互 mutation 进入受治理执行链

客户端和 iframe 只能提交 intent。接受。

### ADR-09：AIAsset 与 Living Object 分开

Capability asset 和用户工作成果不共享权威模型。接受。

### ADR-10：一次完整交付

迁移、旧数据、恢复、协议、Office、测试和生产验收均属于同一轮范围。接受。

### ADR-11：CopilotKit 是 Surface SDK，不是 Runtime authority

直接采用 @copilotkit/a2ui-renderer 与 AG-UI SDK；选择性采用 react-core。明确排除 CopilotKit Runtime、Built-in Agent、Intelligence/Threads 对 Hive Agent loop、持久线程、权限和证据的接管。接受。

### ADR-12：CopilotKit action 与 shared state 都是非权威输入

A2UI action 必须经 HiveActionBridge；shared state 只保存可重建 read model；任何业务写入仍由 Hive API、ToolRuntimeService、Living Object revision 和 receipt 确认。接受。

### ADR-13：Company Knowledge 发布固定 Living Object revision

Company Knowledge 只保存经过 proposal/review/publish 的 immutable object reference；`pinned` 固定已审核 revision，`reviewed_follow` 只自动生成 update proposal。Company tools 负责发现/读取 publication，Living Object runtime 继续负责对象内部 query/mutation，二者权限必须同时成立。接受。

### ADR-14：Cloud Sandbox 使用独立 origin、双层 frame 与零 ambient credential

主站 `app.<root-domain>` 只承载 trusted host；`surface-sandbox.<root-domain>` 只提供固定 shell。模型/Agent 生成的 fragment 进入无 `allow-same-origin` 的 inner frame，数据只通过 nonce-bound MessageChannel 的最小 projection 传入。sandbox 不持有 app Cookie/JWT，不直接访问 Hive API，默认零 egress。接受。

### ADR-15：Capability 是窄化的临时委托，不是身份或权威

capability handle 绑定 principal、tenant、surface、resource revision、profile、policy version、expiry 和用途；它只能缩小已存在的 server authority。每个真实副作用仍由 server 重建 authenticated principal，重新验证 CSRF/RLS/grant/sensitivity/approval/base revision/idempotency，再进入 ToolRuntimeService。接受。

---

## 29. 资料与当前代码依据

### 29.1 Hive 当前代码

- backend/app/models/chat_artifact.py
- backend/app/services/chat_message_parts.py
- backend/app/services/chat_artifact_delivery.py
- backend/app/services/chat_transcript.py
- backend/app/services/web_chat_runtime.py
- backend/app/tools/service.py
- backend/app/config.py
- backend/app/services/personal_knowledge_service.py
- backend/app/services/office_document_service.py
- backend/app/services/officecli_adapter.py
- backend/app/services/interoperability.py
- backend/app/agents/orchestrator.py
- frontend/src/components/MarkdownRenderer.tsx
- frontend/src/pages/agent-detail/ArtifactSurface.tsx
- frontend/src/pages/agent-detail/AgentChatSection.tsx
- frontend/src/pages/agent-detail/chatRuntime.ts
- frontend/src/pages/agent-detail/sessionSocketEventProjector.ts
- frontend/src/pages/agent-detail/OfficeWorkbenchSection.tsx
- frontend/src/pages/PersonalKnowledge.tsx
- frontend/src/api/domains/knowledge.ts

### 29.2 Hive 现有架构文档

- docs/personal-company-knowledge-tool-boundary-2026-07-10.md
- docs/chat-artifact-delivery-redesign-2026-06-20.md
- docs/a2a-workflow-orchestration-design-2026-06-24.md
- docs/org-agent-asset-rights-model.md
- docs/frontend-agent-workbench-redesign-2026-06-20.md
- docs/hive-agent-native-atomic-kiss-reaudit-2026-07-10.md
- docs/hive-sota-master-goal.md

### 29.3 Agent Native 本地依据

- /Users/rocky243/vc-saas/agent-native/packages/core/docs/content/agent-surfaces.mdx
- /Users/rocky243/vc-saas/agent-native/packages/core/docs/content/native-chat-ui.mdx
- /Users/rocky243/vc-saas/agent-native/packages/core/docs/content/generative-ui.mdx
- /Users/rocky243/vc-saas/agent-native/packages/core/src/client/chat/tool-render-registry.tsx
- /Users/rocky243/vc-saas/agent-native/packages/core/src/action-ui.ts
- /Users/rocky243/vc-saas/agent-native/templates/content/actions/update-content-database-view.ts
- /Users/rocky243/vc-saas/agent-native/templates/content/app/components/editor/database/
- /Users/rocky243/vc-saas/agent-native/templates/slides/actions/create-deck.ts
- /Users/rocky243/vc-saas/agent-native/templates/slides/actions/patch-deck.ts
- /Users/rocky243/vc-saas/agent-native/templates/slides/app/components/deck/SlideRenderer.tsx

### 29.4 CopilotKit 当前源码与官方资料

核对快照：

- GitHub main: 87db1b01e7c48ff43b34acef63f29fabf6290029
- @copilotkit/a2ui-renderer: 1.62.3
- @copilotkit/react-core: 1.62.3
- @copilotkit/runtime: 1.62.3
- @ag-ui/core / client: 0.0.57
- @a2ui/web_core dependency: 0.9.0

源码：

- https://github.com/CopilotKit/CopilotKit
- packages/a2ui-renderer/src/react-renderer/create-catalog.tsx
- packages/a2ui-renderer/src/react-renderer/core/A2UIProvider.tsx
- packages/a2ui-renderer/src/react-renderer/core/A2UIRenderer.tsx
- packages/react-core/src/v2/a2ui/A2UIMessageRenderer.tsx
- packages/react-core/src/v2/hooks/use-human-in-the-loop.tsx
- packages/react-core/src/v2/components/MCPAppsActivityRenderer.tsx
- packages/react-core/src/v2/components/OpenGenerativeUIRenderer.tsx
- packages/runtime/src/v2/runtime/core/runtime.ts
- sdk-python/copilotkit/integrations/fastapi.py

官方资料：

- CopilotKit architecture: https://docs.copilotkit.ai/concepts/architecture
- CopilotKit Generative UI: https://docs.copilotkit.ai/a2a/concepts/generative-ui-overview
- CopilotKit A2UI: https://docs.copilotkit.ai/generative-ui/a2ui
- CopilotKit Shared State: https://docs.copilotkit.ai/shared-state
- CopilotKit Authentication: https://docs.copilotkit.ai/auth
- CopilotKit Premium/Intelligence: https://docs.copilotkit.ai/premium/overview
- CopilotKit Threads: https://docs.copilotkit.ai/premium/threads-explained
- CopilotKit Telemetry: https://docs.copilotkit.ai/telemetry

### 29.5 外部规范与产品资料

- A2UI v0.9.1 specification: https://a2ui.org/specification/v0.9.1-a2ui/
- A2UI v1.0 candidate: https://a2ui.org/specification/v1.0-a2ui/
- A2UI component reference: https://a2ui.org/reference/components/
- A2UI ecosystem comparison: https://a2ui.org/introduction/agent-ui-ecosystem/
- AG-UI overview: https://docs.ag-ui.com/
- AG-UI architecture: https://docs.ag-ui.com/concepts/architecture
- AG-UI events: https://docs.ag-ui.com/concepts/events
- AG-UI state: https://docs.ag-ui.com/concepts/state
- A2A key concepts: https://a2a-protocol.org/latest/topics/key-concepts/
- MCP Apps overview: https://modelcontextprotocol.io/extensions/apps/overview
- Claude Artifacts help: https://support.claude.com/en/articles/9487310-what-are-artifacts-and-how-do-i-use-them

### 29.6 Codex Desktop 本地与官方依据

本机核对快照（2026-07-15）：

- `/Applications/ChatGPT.app`，Codex Desktop build `26.707.72221`；
- `/Users/rocky243/.codex/plugins/cache/openai-bundled/visualize/1.0.11/skills/visualize/SKILL.md`；
- `/Users/rocky243/.codex/plugins/cache/openai-bundled/visualize/1.0.11/skills/visualize/scripts/render.py`；
- `/Applications/ChatGPT.app/Contents/Resources/app.asar` 中的 inline visualization host、directive parser、Electron webview partition、inner iframe sandbox、CSP、MessageChannel 和 capability/action handling。

本地证据表明，Codex inline visualization 的关键不是“把 HTML 放进 Markdown”，而是：Agent 产出 thread-scoped HTML resource，host 解析结构化 `codex-inline-vis` 引用，在隔离 webview/iframe 中执行，并把 follow-up、download、external link 等能力收敛到窄 bridge；任意 `callTool`/`callMcp` 不是默认开放权限。这个机制适合作为开放表达的产品参考，但它依赖 Electron/webview 与本机进程边界，不能直接复制到 Hive Cloud 浏览器环境。

官方资料只支持更高层安全原则，不被本文用来声称未公开的内部实现：

- Codex App：系统级 sandbox、默认受限写入/网络与越界 approval：https://openai.com/index/introducing-the-codex-app/
- Codex 默认本地/云端 network-disabled sandbox：https://openai.com/index/introducing-upgrades-to-codex/
- Sandbox 与 approval 分工：https://openai.com/index/running-codex-safely/
- Windows capability-based sandbox 的执行边界说明：https://openai.com/index/building-codex-windows-sandbox/

Hive Cloud 的对应原则是：**开放表达，封闭权限；本地交互自由，真实副作用受治理。** 浏览器侧必须改用独立 origin、双层 iframe、零 ambient credential、server-authoritative bootstrap/capability、默认零 egress 与每次副作用重新授权，不能把 Electron 的 process partition 当成 Web 安全边界。

---

## 30. 结论

Hive 的下一代呈现层不应以“Markdown 还是 HTML”“A2UI 还是 Office”作为主问题。

真正的主问题是：

> **Agent 产生的成果，能否成为一个长期存在、可继续操作、可被不同 Surface 原生呈现、可被知识系统引用、可跨 Agent 协作、同时受权限和证据治理的对象？**

本规格的答案是：

- 用 Living Object 承担长期业务真相；
- 用 AG-UI 承担 Hive Agent → UI 的标准事件投影；
- 用 @copilotkit/a2ui-renderer + Hive Native Catalog 承担原生交互和声明式 Surface；
- 用独立 origin 的 Codex-style Sandbox Surface 承担尚未产品化的长尾开放表达，并以 capability profile、零默认 egress、immutable resource revision 和 server-side re-authorization 封闭权限；
- 用 A2A/MCP Apps 承担跨 Agent 与开放 UI 互操作；
- 用 ChatTranscriptEvent、ToolRuntimeService、Grant、Revision 和 Receipt 承担执行证据与恢复；
- 用 Personal/Company Knowledge 承担所有权、知识关联和发布；
- 用 OfficeCLI、隔离预览和 retained original 承担兼容，而不是产品核心。

这样做之后，Markdown 不会消失，而会回到它最擅长的叙事知识；Office 原文件与 Agent 操作能力继续保留，而额外在线编辑基础设施退出默认产品拓扑；A2UI 不会被神化，而会成为 CopilotKit 可渲染、Hive 可治理的声明式 Surface；AG-UI 避免 Hive 再造一套 Agent/UI 协议；Codex-style sandbox 只承担长尾表达，不承担身份、业务真相或执行权威；CopilotKit 则被严格限制在它最擅长的 Surface SDK 位置。Hive 最终获得的是自己的 Agent-native 对象运行时、云端受治理的开放表达层，以及一致的安全与恢复合同，而不是第二个 CopilotKit agent platform，也不是把任意 HTML 直接塞回主站。
