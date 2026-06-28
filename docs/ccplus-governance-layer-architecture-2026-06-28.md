# CCPlus 治理分层架构：L0-L3

日期：2026-06-28

状态：治理层收口的架构决策草案

相关文档：

- `docs/cc-tooling-alignment-and-plugin-system.md`
- `docs/ccplus-session-permission-and-enterprise-hard-rules-2026-06-25.md`
- `docs/external-capability-trust-gate-plan-2026-06-26.md`
- `docs/ccplus-governance-code-repair-plan-2026-06-28.md`

## 决策摘要

Hive 不应该为默认工具保留一层独立的“L2 工具准入治理层”。

本文使用 **L0 / L1 / L2 / L3** 表达治理架构，避免和记忆系统里的原始会话证据术语混淆。

正确的治理模型是：

1. **L0 平台硬护栏**：平台不可配置的安全和正确性不变量。
2. **L1 公司硬规则**：公司管理员可以配置的 capability / enterprise policy。
3. **L2 扩展与组合面**：plugin / MCP / provider capability / 公司预装增值能力的安装、卸载、分配、凭据和来源治理。L2 只控制非 CORE 扩展的可见性和组合关系，不暴露默认 CORE 能力的开关。
4. **L3 Session Permission Mode**：当前会话内的执行授权，对齐 CC / Cloud 的 session permission 语义。

默认 CORE 工具是 runtime baseline。它们应该默认可见，并在调用时经过 L0、L1、L3 治理。Plugin 提供的工具通过 L2 安装和分配进入工具面，但每一次调用仍然必须经过同一套 L0、L1、L3。Hook 是 runtime 拦截点，可以阻断或收窄执行，但不能绕过 L0、L1、L3。

旧的“工具目录 / 工具开关”页面如果继续存在，必须改成扩展和组合面，而不是默认工具开关。把默认能力放在这里让用户开关，会制造这次 `agent.message.send` 事故里暴露出的混乱：公司后台看起来工具已经开启，但 runtime 实际上是在等待当前 session 内的授权决定。

## 核心判据：可关闭的是扩展，不是底座

治理边界要先区分两类能力。

### 基础能力 / 默认功能

基础能力是 Hive agent runtime 能成立的前提。它们不能在公司后台作为可关闭能力暴露，否则系统会出现“关掉基础部件后产品本身无法工作”的荒谬状态。

典型基础能力包括：

- 文件读写、搜索、命令 / 代码执行的基础工具。
- Session messaging、channel reply、channel file delivery。
- Agent-to-agent message、delegation、async task helpers。
- Work ledger、plan mode helpers、current time。
- Skill loading / discovery。
- 基础 web read / search。
- Subagent / workflow source capabilities。
- 如果 Office 被产品定义为在线办公底座，则 Office 也应归入基础能力，而不是可关闭扩展。

基础能力不做“可见性治理”或“安装准入治理”。它们默认存在，不能在 L2 里被关闭。

但基础能力仍然做“调用时治理”：

- L0 防止平台不安全或不可审计的执行。
- L1 约束公司硬规则，例如不能 share agent、不能 destructive delete、不能越过公司边界。
- L3 决定当前 session 是否允许这次具体 tool call。

换句话说，不能把 `send_message_to_agent` 这种基础能力关掉；但可以通过 L1 明确禁止某类跨边界分享或委派行为。不能把 Office 基础办公能力关掉；但可以治理外发、删除、覆盖、分享等具体风险动作。

### 扩展能力 / 组合能力

扩展能力不是 runtime 成立的前提，而是租户、行业、场景、供应商或公司增值配置带来的额外能力。

典型扩展能力包括：

- 公司自行安装的 plugin。
- 公司或 agent 分配的 MCP server。
- 外部 provider-backed tools，例如高级搜索、第三方抓取、行业数据源。
- 平台自带但可插拔的外部集成，例如飞书、Slack、Email 等企业连接器。
- PaaS 相关基础集成，例如部署、存储、数据、消息、监控、身份等外部服务连接器。
- 金融、法务、医疗、销售等行业能力包。
- 公司一开始预装的增值能力。
- Plugin skills、plugin subagents、plugin workflows、plugin hooks。

这些能力属于 L2。它们可以安装、卸载、分配、取消分配、配置凭据、查看 provenance，也可以在公司后台治理其可见性和启用状态。

一旦扩展能力进入可见工具面，它的每一次调用仍然必须经过 L0、L1、L3。

## L2 对外暴露范围

L2 面向企业后台暴露的是“可插拔增强能力”，不是 Agent 基础能力。

L2 不应该展示 Agent 基础能力清单，也不应该提供关闭 Agent 基础能力的开关。基础能力默认开放；如果某个基础能力的具体行为有风险，应该通过 L0 / L1 / L3 治理具体行为，而不是在 L2 把能力整体关掉。

企业后台真正需要暴露的是：

1. **高级搜索**
   - AnySearch vertical search。
   - Exa。
   - Tavily。
   - 行业/垂直数据搜索。
   - 公司自建或第三方 search provider。

2. **第三方抓取 / 提取**
   - Firecrawl。
   - XCrawl。
   - AnySearch extract。
   - 其他需要 provider、凭据或付费 quota 的 crawler / extractor。

3. **平台自带外部集成**
   - 飞书 / Lark。
   - Slack。
   - Email。
   - DingTalk / WeCom / Teams / Telegram / Discord 等已接入或未来接入的企业 channel。
   - 这些属于平台提供的默认增值项，可以默认开启，也可以由企业关闭或按 agent 分配。

4. **平台自带协作 / 社区增值项**
   - Plaza / 广场。
   - `plaza_get_new_posts`。
   - `plaza_create_post`。
   - `plaza_add_comment`。
   - 广场可以作为平台默认开放能力，也可以由企业整体关闭；它不是 Agent 基础能力。

5. **PaaS / 外部服务基础集成**
   - MCP server。
   - 数据库、对象存储、队列、部署平台、监控平台、身份系统等外部服务连接器。
   - 公司预装的 PaaS 能力包。
   - 租户自行安装的 PaaS plugin。

6. **公开扩展接口**
   - Plugin install / uninstall。
   - Per-agent assignment。
   - Credential setup。
   - Provenance / signature / source policy。
   - Hook allowlist 与 hook registration。
   - Capability / approval metadata。

### Web 能力判定

Web 能力必须拆开判断：

- **Agent 基础能力**：`web_fetch`，以及平台基础 `web_search`。这里的基础 `web_search` 应该代表平台默认搜索底座，例如自研 SearchRNG / SearXNG 路径。
- **L2 默认增值能力**：AnySearch、Exa、Tavily、Firecrawl、XCrawl，以及其他垂直或 provider-backed 搜索/抓取能力。

当前代码里 `web_search` 的描述仍然混合了 “AnySearch API first, then SearXNG fallback”。按本文治理口径，这里应该拆开：基础 `web_search` 不应该依赖 AnySearch 作为语义默认；AnySearch 应作为 L2 增强能力独立安装、开启、关闭或替换。

### Office 能力判定

Office 能力也必须拆开判断：

- **Agent 基础能力**：Office CLI / 文档生成、读取、转换、编辑所需的基础 agent 能力。如果它是 agent 完成交付物的基础能力，就不应该出现在 L2 可关闭开关里。
- **L2 默认增值能力**：Office Online / 在线协作编辑 / 浏览器工作台等平台默认增值项。它可以默认开启，也可以由企业关闭或按租户配置；关闭它不应破坏 agent 通过 Office CLI 生成和处理文档的能力。

当前已收口为两个治理对象：`read_document` / `office_document_*` 是 `agent_base` CORE runtime tools；`office_pack` 只保留 manifest/skill guide 语义，manifest 中这些工具标记为 `requires_core`；Office Online / 在线协作编辑 / 浏览器工作台由 L2 `office_browser` 承接。

## 分层定义

### L0：平台硬护栏

L0 由 Hive 平台拥有，不属于公司管理员，也不属于当前 session 用户。

L0 保护平台正确性、租户隔离、凭据安全、沙箱完整性、审计完整性，以及不能被绕过的 runtime 不变量。它回答的问题是：

> 即使公司管理员或当前 session 用户想执行这件事，平台是否允许安全地执行？

典型例子：

- 非安全工具缺少 tenant context，必须 fail closed。
- security zone 无法解析，必须 fail closed。
- capability gate 本身不可用，必须 fail closed。
- 工具参数包含 path escape、危险删除路径、托管凭据探测、secret exfiltration。
- code execution 必须走配置好的 sandbox provider；生产环境不能退回 raw host subprocess。
- RLS、tenant ownership、delegation token scope、audit write invariant。
- Hook 的 timeout、recursion、schema validation 不变量。

L0 的特征：

- 不由租户配置。
- 不会被 `bypassPermissions` 绕过。
- 不转换成 admin approval row。
- 不通过 session 内“允许”按钮解决。
- 失败通常是 hard block、fail closed，或平台错误。

L0 不是“公司策略”。L0 是 runtime 的安全地板。

### L1：公司硬规则

L1 由公司 / 租户控制面拥有。

L1 表达一个公司对数字员工能力的持久规则。它回答的问题是：

> 这个公司是否允许 agent 在公司策略下执行这个 capability？

典型例子：

- 员工可以创建 agent，但只有管理员可以删除 agent。
- 公司禁止 share agent / 跨 agent 委派。
- 公司拒绝 `workspace.command.destructive_delete`。
- 公司要求某个敏感 MCP tool 或外部 channel action 走审批。
- 出现 company-boundary conflict 时，升级到公司级审批，而不是 session-local consent。

L1 的特征：

- 由租户 / 公司管理员配置。
- 持久化并可审计。
- 早于 session permission。
- Deny 不能被 `auto`、`default`、`allow once`、`allow session` 或 `bypassPermissions` 绕过。
- 显式 approval policy 可以进入 enterprise approval。
- 缺少 policy 不等于需要企业审批；如果没有显式公司硬规则，请求继续下落到 L3 session mode。

L1 应该表达为 capability policy / enterprise policy，而不是泛化的工具可见性开关。L1 的重点不是“关掉默认功能”，而是定义默认功能不可越过的行为边界。

### L2：扩展与组合面

L2 不是默认 CORE 工具的硬治理层。

L2 是非 CORE 能力的产品组合面和 runtime 组合面。它回答的问题是：

> 这个租户安装、配置、分配了哪些扩展能力给这个 agent？

典型例子：

- Extension / plugin catalog。
- 高级搜索 / 第三方抓取 catalog。
- Plaza / 广场等平台协作增值项。
- Plugin install / uninstall。
- Per-agent plugin assignment。
- MCP server import / assignment。
- 需要凭据的 provider-backed optional tools。
- 平台自带外部集成，例如飞书、Slack、Email。
- PaaS / 外部服务 connector。
- 公司预装的行业或增值能力。
- Plugin skills、subagents、workflows、hooks。
- Credential setup 和 provenance visibility。

L2 的特征：

- 控制扩展能力的可见性和组合关系。
- 不决定 CORE 工具是否可执行。
- 不暴露 CORE 工具的开关。
- 不替代 L1 capability policy。
- 不替代 L3 session permission。
- 一个 extension tool 一旦可见，它的调用仍然必须经过 L0、L1、L3。

所以 L2 可以存在，但它不是“工具准入治理层”。更准确地说，L2 是 extensions / composition surface。

### L3：Session Permission Mode

L3 由当前 session 拥有。

L3 实现 CC / Cloud 风格的 runtime consent。它回答的问题是：

> 在平台安全和公司策略没有拒绝的前提下，当前 session 是否允许这次 tool call？

面向用户的模式：

- `default`：敏感动作先请求批准。
- `auto`：确定性的低风险动作自动允许；模糊或高风险动作请求批准。
- `bypassPermissions`：只绕过 session-local prompt。

面向用户的决定：

- 仅本次允许。
- 本 session 允许。
- 拒绝。

L3 的特征：

- 只在当前 session 内生效。
- 必须在 tool call 出现的同一个 session 中处理。
- Web 和 IM 渠道必须暴露同等语义。
- 决策必须写入 session transcript / raw evidence trail。
- 不能覆盖 L0 或 L1。

如果 `agent.message.send` 需要授权，正确答案是 L3 session prompt，不是企业后台审批。

## 为什么没有“L2 工具准入治理层”

我们应该让 L2 留在扩展组合边界之外，而不是把它放进硬治理链路。原因有三个。

### 1. CORE 工具是 runtime baseline

CC 对齐后，默认工具不是 package 选择出来的可选功能。它们是 runtime baseline：

- 文件读写工具。
- 命令 / 代码执行工具。
- Web read / search 工具。
- Work ledger。
- Session messaging。
- Subagent 和 workflow source capabilities。
- Skill loading 和 discovery。

有些 CORE 工具很敏感，但敏感不代表它不是 CORE。它仍然应该可见，只是在调用时受治理。

因此，公司后台的“工具已开启”开关不应该被理解成 CORE 工具是否可执行的硬权限真相。如果公司要硬拒绝某个能力，应该通过 L1 capability policy 表达。

### 2. Plugin 安装替代了旧 package 式工具准入

非默认能力应该通过 plugin system 进入：

- Installed plugin tools。
- MCP servers。
- Plugin skills。
- Plugin subagents。
- Plugin workflows。
- Plugin hooks。
- 需要凭据的 provider-backed optional tools。

Plugin system 负责 install / uninstall / assignment / credentials / provenance。这是组合治理和供应链治理，不是另一层 runtime permission。一旦 plugin tool 可见，每一次调用仍然走 L0、L1、L3。

### 3. 真正的安全边界必须在 call-time

Tool visibility 不是安全边界。

平台必须假设工具可能通过历史 transcript、alias、plugin manifest、MCP name 或未来的 deferred loading 被请求。真正的安全边界是调用时治理：

1. L0 平台硬护栏。
2. L1 公司硬规则。
3. L3 session permission mode。
4. Tool schema validation、preflight、sandbox、execution provider constraints。

这样才能保证 Web、IM、automation、workflow、subagent、plugin surface 的治理语义一致。

## L2 应该保留什么

L2 应该收窄为扩展和组合职责。

它可以保留为：

- Extension catalog。
- Plugin catalog。
- Advanced search / crawler catalog。
- Plaza / collaboration add-on assignment。
- Plugin installation / uninstall。
- Per-agent plugin assignment。
- Credential setup。
- MCP server assignment。
- 企业 channel / SaaS integration assignment。
- PaaS connector assignment。
- 公司预装增值能力配置。
- Operator visibility into extension capabilities。

它不应该被描述为：

- 默认 CORE 工具是否可执行的真相源。
- 默认 CORE 工具的展示和开关页面。
- Hard allow / deny layer。
- Capability policy 的替代品。
- Session permission mode 的替代品。

对于默认 CORE 工具，L2 UI 不应该提供开关。最多可以在只读诊断面说明它们是 runtime baseline，并且执行时受 L0、L1、L3 治理。

对于 extension / plugin tools，UI 可以展示 install / uninstall / assign controls，但也应该明确：

> Installation 控制可见性。执行仍然在调用时受治理。

## Runtime 顺序

Runtime 顺序应该是：

1. 解析 agent、tenant、session、runtime context。
2. 校验 L0 平台硬护栏。
3. 应用 L1 公司硬规则和显式 capability policy。
4. 运行适用于当前 tool call 的 CC-compatible hooks。
5. 当调用需要 consent 时，应用 L3 session permission mode。
6. 如果 hook 修改了参数，重新校验最终 tool input。
7. 运行 tool-specific preflight。
8. 通过受治理的 provider / sandbox 执行。
9. 写入 transcript、invocation spans、audit events，并完成 channel / session delivery。

越早的层级优先级越高。后面的层可以收窄或暂停执行，但不能扩大权限。

## Channel 合同

Session permission 必须是 channel-native：

- Web：在当前 chat session 内渲染 permission card。
- IM：向原始 channel 回发清晰的 permission prompt，并接受“仅本次允许 / 本 session 允许 / 拒绝”等文本回复。

普通 L3 permission prompt 不应该被送到企业后台。

企业后台审批只用于显式 L1 approval policy 或 company-boundary escalation。

## Hook 合同

Hook 不是第四个权限拥有者。

Hook 是受治理的 runtime interception point。它可以：

- 观察 lifecycle events。
- 阻断 tool call。
- 收窄或改写参数。
- 写 audit 或 guidance。

Hook 不能：

- 绕过 L0 平台硬护栏。
- 覆盖 L1 enterprise deny。
- 把已经被拒绝的调用变成允许。
- 跳过仍然适用的 L3 session permission。
- 在没有 plugin trust-gate validation 的情况下执行任意 tenant code。

如果 hook 修改了 tool input，最终 input 必须按需重新经过 schema validation、capability mapping、L1 policy、L3 permission decision 和 preflight。

## 产品影响

公司后台应该拆成三个产品面：

1. **Capability Policies**
   - 公司硬规则。
   - 按 capability 配置 deny / allow / require approval。
   - 示例：destructive delete、agent sharing、cross-agent delegation、external channel send。

2. **Extensions and Add-ons**
   - Extension catalog 和 composition surface。
   - 不暴露默认 CORE 能力的开关。
   - 高级搜索、第三方抓取、飞书 / Slack 等平台集成、Plaza / 广场、PaaS connector、Plugins / MCP / provider tools / 公司预装增值能力可以 install、assign、uninstall 或 configure。

3. **Session Permission Mode**
   - 位于 chat / session UX，不属于 enterprise settings。
   - 只控制当前 run / 当前 session 的行为。
   - Web 和 IM 必须暴露等价语义。

这个分离可以避免“工具在公司后台显示 enabled，但 runtime 实际暂停等待 session-local decision”的误导状态。

## 当前代码现实

当前代码已经部分指向这个模型：

- `CORE_TOOL_NAMES` 和 `_ALWAYS_INCLUDE_CORE` 让默认工具不依赖 DB tool rows 也能进入 runtime tool surface。
- `run_tool_governance()` 是 call-time enforcement path。
- `CAPABILITY_MAP` 把 tool name 映射到公司可治理的 capability。
- `workspace.command.destructive_delete` 这类 synthetic capabilities 覆盖依赖参数才能判断的风险。
- Session permission mode 持久化在 session metadata 中，并通过当前 session resolve。
- Plugin / MCP installation 和 assignment 是非 CORE 能力未来正确的可见性控制面。

按当前代码，能力大致可以分成：

- **当前 CORE / always include**：文件读写、命令 / 代码执行、skill、memory、trigger、agent message / delegation、async task helpers、session/channel delivery、`tool_search`、基础 `web_fetch` / `web_search`、subagent / workflow source capabilities、work ledger。
- **当前 RuntimeToolGroup / 扩展候选**：高级 web provider tools、Feishu/Lark、Email、MCP admin/import/call、Plaza、Office、command-layer deferred tools。
- **明显应归入 L2 的能力**：AnySearch、Exa、Tavily、Firecrawl、XCrawl、MCP server、plugin tools、provider-backed optional tools、飞书 / Slack / Email 等外部 channel integrations、Plaza / 广场、PaaS connectors、行业/金融/法务等公司增值能力。
- **需要产品判定的能力**：Office 和 command-layer deferred tools。如果它们是 Hive 的基础体验，就应该从“可关闭 pack”语义里移出；如果它们是增值模块，才留在 L2。

目前的错位在产品表达：

- `/enterprise/tools` 看起来像 hard tool permission layer。
- Capability policy 后端已经有支撑，但需要更清晰的前端管理面。
- 默认 CORE tools 不应该在 L2 被当成 optional package toggles。
- `web_search` 现在仍把基础搜索和 AnySearch 路径混在一起，需要拆成基础搜索底座与 L2 增强搜索。
- Office 这类能力如果被产品定义为基础办公功能，也不应该在后台暴露成可关闭扩展。

## 收口验收标准

治理层收口完成，必须同时满足：

1. 产品和文档不再把 tool catalog 描述成 CORE tools 的硬治理层。
2. 公司硬规则通过 capability / enterprise policy 表达，而不是通过 generic tool visibility 表达。
3. CORE tools 默认可见，不在 L2 暴露关闭开关。
4. Plugin / extension tools 由 install / assignment / credential / provenance 控制可见性，然后在调用时受治理。
5. `bypassPermissions` 只绕过 L3 prompt，不能绕过 L0 或 L1。
6. 缺少 capability policy 时，下落到 L3 session permission；除非 L0 guardrail 失败。
7. Web 和 IM 都能在当前 session / channel 暴露 L3 prompt。
8. Hook 可以阻断或收窄，但不能扩大权限或绕过治理。
9. L2 UI 只暴露高级搜索、第三方抓取、平台自带外部集成、Plaza / 广场、PaaS connector、plugin/MCP、公开扩展接口和公司预装增值能力。
10. `web_search` 拆清：基础搜索底座留在 Agent 基础能力；AnySearch / provider-backed search 进入 L2。
11. Office、command-layer deferred tools 等边界能力完成产品归类：基础能力就移出可关闭扩展面；增值能力才留在 L2。
12. 测试覆盖代表性路径：
   - CORE sensitive tool 默认可见，但调用时受治理。
   - L1 hard deny 优先于 L3 bypass。
   - L3 prompt 能在 Web 和 IM delivery。
   - Plugin tool visibility 由 plugin assignment 控制。
   - Hook 修改后的 args 会在执行前重新校验。

## 非目标

本文不主张删除 tool catalog UI。

本文主张删除的是：把 tool catalog 当成默认 runtime capabilities 的独立治理层。

本文也不降低 plugin trust-gate 要求。Plugin source validation、credential isolation、tenant installation records、hook allowlists、provenance、sandbox materialization 仍然是必需的平台工作。它们属于 plugin supply-chain 和 runtime governance，不属于默认工具的独立准入层。
