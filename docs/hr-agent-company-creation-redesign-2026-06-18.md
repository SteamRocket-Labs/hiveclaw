# HR Agent 公司级创建流整改方案（2026-06-18，2026-06-20 修订）

> 状态：下一轮代码整改前的设计文档。本文只定义目标、边界、文件去留、实现路径和验收口径，不表示代码已经完成。
>
> 核心纠偏：HR Agent **不判断要不要创建员工**。用户进入 HR 创建入口时，创建意图已经成立。HR 的唯一工作是把这个意图塑造成更符合这家公司、更像真实职场人的数字员工。
>
> 2026-06-20 修订：当前 memory 基础设施已收敛为 T0 append-only session ledger -> T2 reviewed Segment Package -> T3 accepted four-file semantic layer。本文中的公司 Agent DNA 必须落在这条 governed pipeline 上，不能新增平行记忆系统，也不能新增 `memory/t3/company_agent_dna.md` 这类非 canonical T3 文件。

## 0. 结论

本轮整改应把 HR 创建体系收敛成：

```text
公司级 HR Agent
  + 一个 canonical create_employee skill
  + 极薄 soul.md 身份锚点
  + 后端 preview / confirmation / hash / session gate
  + Company Knowledge Lane 主导的公司语境
  + T0/T2/T3 governed memory 沉淀的 HR 历史建议
```

需要明确删除的错误方向：

1. HR 不做“是否应该创建员工”的判断。
2. HR 不做招聘审批、HC 管理、组织规划或员工编制建议。
3. HR 不把“是否扩展现有 agent”作为创建流前置决策。
4. HR 不把流程本身当成价值；流程只服务于更好地创建员工。
5. HR 不直接写新的 memory/t3 文件或旧 logs 目录；创建学习必须走现有 Memory Governance Layer。

HR 的目标是：

1. 接收用户已经表达的创建意图。
2. 用尽可能少的追问补齐必要信息。
3. 把模糊需求转成一个像真实公司成员的数字员工：精英实习生、职场 partner、职场搭档。
4. 让这个员工第一天就有清晰任务、质量标准、边界、汇报方式和可进化起点。
5. 从公司知识库、后台治理语料和过往创建语料中校准创建建议，让后续新员工越来越符合公司真实需求。

当前执行判断：

- `HR Guide` 与 `Create Employee` 仍然重复，必须合并。
- “五轮”不是产品目标；保留员工质量门禁，取消固定轮次心智。
- `memory/t3/company_agent_dna.md` 是旧基础设施下的候选表达，现在必须删除这个实现方向。
- 公司 Agent DNA 不能等同于 HR Agent 的创建记忆；HR 创建历史只是弱参考信号，公司的知识库、治理语料和后台配置才是公司需求的主来源。
- 公司 Agent DNA 的 HR 侧沉淀应作为 accepted T3 semantic blocks，分布在 `episodes.md`、`worker.md`、`capabilities.md`，必要时才写 `user.md`。
- 公司 Agent DNA 只能提出候选默认值，不能替代当前创建过程中的边界询问和用户确认。

## 1. 北极星对齐

Hive 的北极星不是“后台配置生成器”，而是：

1. 最强数字员工：单个 agent 有智能、记忆、自进化、技能、可靠性和安全边界。
2. 公司级控制中台：在企业规模下运营这些 agent，覆盖权限、预算、治理、协作和观测。

HR Agent 属于这两个目标的入口层。它创建出来的不是工具壳，而是公司级数字员工。

因此 HR 创建质量的判断标准不是“字段是否填齐”，而是：

- 新 agent 是否有清晰的职场身份。
- 新 agent 是否知道服务谁、产出什么、第一天做什么。
- 新 agent 是否像一个高潜实习生或靠谱职场搭档，而不是泛用聊天助手。
- 新 agent 是否有能进入记忆和自进化循环的初始 DNA。
- 新 agent 是否遵守公司、owner、权限、外部可见行为的边界。

## 2. 当前问题

当前 HR 模板中存在三类重复：

1. `soul.md` 写了较完整的创建流程。
2. `skills/hr-guide/SKILL.md` 写了一套招聘流程。
3. `skills/create-employee/SKILL.md` 又写了一套创建流程。

这些内容虽然已被临时改成一致，但结构上仍然有问题：

- 两个 skill 都在讲创建，模型可能不知道哪个是主流程。
- `soul.md` 太像 SOP，长期会和 skill 漂移。
- `hr-guide` 容易把 HR 拉向“判断是否创建 / 是否扩展现有员工”的方向。
- 真正重要的目标，即“创建符合公司语境的真实职场搭档”，没有成为唯一中心。

## 3. 目标职责边界

### 3.1 HR Agent 是什么

HR Agent 是每家公司一个的数字员工创建搭档。

它是公司级共享 agent，不是某个员工私有的创建助手。公司内所有用户创建新数字员工时，都应进入同一个 HR Agent，使 HR 能沉淀本公司的创建语料、默认风格和隐性偏好。

如果当前系统中 `tenant == company`，则实现上继续保持每个 tenant 一个 `__system_hr__`。这不是平台全局共享，也不能跨 tenant 学习。

### 3.2 HR Agent 不是什么

HR Agent 不是：

- 招聘审批人。
- 组织设计顾问。
- 编制/预算裁决器。
- “是否应该创建这个员工”的判断器。
- 防止用户创建员工的 gatekeeper。

用户进入创建入口后，HR 默认目标就是完成创建。HR 可以补齐信息、提出默认值、提示边界、展示 setup debt，但不应把“是否创建”变成问题。

### 3.3 HR Agent 的唯一任务

HR 的唯一任务是：

```text
把用户的创建意图，转成一个符合本公司语境、第一天可工作、后续可自进化的数字员工。
```

## 4. 文件去留判断

### 4.1 保留：`backend/hr_agent_template/soul.md`

`soul.md` 保留，但必须缩薄。

它只写身份和不可绕过的边界：

- 你是公司级 Digital Employee Creation Partner。
- 你不判断是否创建；用户来到这里就是要创建。
- 你创建的是精英实习生 / 职场 partner / 职场搭档，不是工具壳。
- 你必须优先参考公司知识库、后台治理语料和用户当前确认，再把 HR 历史创建经验作为建议来源，让新员工符合这家公司。
- 你必须走 `preview_agent_blueprint` 和 `create_digital_employee` 的后端确认链路。
- 你不能跨公司复用或泄露创建偏好。

它不再重复完整 SOP、字段表、示例流和 skill routing 细节。

### 4.2 保留并扩展：`backend/hr_agent_template/skills/create-employee/SKILL.md`

`create_employee` 成为唯一 canonical HR 创建 skill。

它负责完整 SOP：

- 创建目标：塑造真实职场搭档。
- 动态轮次：只追问缺失信息。
- 创建门禁：Identity / Work Contract / Governance / Capability & Setup / Preview & Confirmation。
- 公司适配：优先读取公司知识库/治理语料，再使用 HR 历史经验作为建议。
- blueprint preview。
- explicit confirmation。
- `confirmed_blueprint_hash` 创建。
- 创建后把本次创建案例写入可治理记忆候选。

### 4.3 退役：`backend/hr_agent_template/skills/hr-guide/SKILL.md`

`hr-guide` 应退役。

退役原因：

- 它和 `create_employee` 重复。
- 它容易把 HR 变成“是否创建”的判断器。
- 它让 skill catalog 出现两个创建入口，增加模型选择成本。

迁移方式：

1. 把仍然有价值的内容并入 `create_employee`。
2. 模板同步时删除或隔离旧 `skills/hr-guide/`。
3. 对已有 HR workspace 做可逆清理：移动到归档目录，而不是直接硬删。

建议归档路径：

```text
skills/.retired/hr-guide/<timestamp>/SKILL.md
```

## 5. 新创建流程

### 5.1 流程总览

```text
User wants to create a digital employee
  -> HR accepts creation as the goal
  -> Load company knowledge + history suggestions as source-attributed context
  -> Clarify only missing creation material
  -> Build workplace-partner blueprint with source-attributed suggestions clearly marked
  -> Preview with gates + setup debt
  -> User confirms all substantive blueprint content
  -> create_digital_employee with confirmed_blueprint_hash
  -> Persist creation case evidence for HR learning
```

### 5.2 不是“五轮”，而是动态补齐

轮次不是目标。

正确规则：

- 如果用户已经给足信息，一轮即可 preview。
- 如果缺少关键信息，只问缺失部分。
- 如果用户让 HR 决定，HR 可以结合公司 Agent DNA 和合理默认值提出候选方案，但涉及边界、红线、权限、外部可见行为的内容必须明确展示并让用户确认。
- 不因为固定流程而拖慢创建。

### 5.3 门禁改名：从流程门禁到员工质量门禁

现有门禁可继续保留，但表达应更贴近“真实员工质量”：

| 门禁 | 目的 |
|---|---|
| Identity Gate | 这个员工是谁，服务谁，产出什么 |
| Work Contract Gate | 第一件工作、成功证据、汇报方式、工作节奏 |
| Governance Gate | 不能做什么，哪些行为需要确认，外部可见边界 |
| Capability & Setup Gate | day-one 能力、默认能力、必须安装的能力、setup debt |
| Preview & Confirmation Gate | 用户确认的是当前 blueprint，而不是模糊口头描述 |

注意：这些门禁不是用来判断“是否创建”，而是保证创建出来的员工质量足够高。

### 5.4 历史经验和记忆不能替代用户确认

HR 可以使用记忆减少重复解释，但不能因为记忆里有“公司通常这样做”或“我记得你偏好这样”就跳过当前用户确认。

硬规则：

- 非当前 session 直接确认的默认值只能作为建议，不能直接把任何 creation gate 标记为 complete。
- 任何由公司知识库、历史经验、通用知识推导出来的实质性 blueprint 内容，都必须展示给用户确认后才能进入 `preview_agent_blueprint` / `create_digital_employee` 参数。
- Governance Gate 的边界、红线、权限、外部可见行为、确认优先事项，必须来自当前创建 session 的用户明确回答或用户对 preview 中这些边界的显式确认。
- 如果 HR 根据公司知识库或历史经验推断出默认边界，必须说清楚来源：这是 `supported_by_company_kb` 还是 `suggested_by_history`，并要求用户确认或修改。
- 如果用户说“你来定”，HR 可以提出完整默认方案，但仍必须把边界项列出来并请求确认；不能静默写入 blueprint。
- Preview & Confirmation Gate 必须确认的是最终 blueprint 的实质内容，而不是泛泛确认“创建吧”。

“实质性 blueprint 内容”包括：

- 员工身份、使命、服务对象、核心产出。
- 工作合同：第一任务、成功证据、汇报方式、节奏。
- 边界、红线、权限范围、外部可见行为、确认优先事项。
- day-one 能力、需要安装的 skill / MCP / 外部集成。
- setup debt、需要用户后续完成的授权、key、渠道配置。

正确行为：

```text
基于公司知识库和过往创建经验，我建议这个员工默认不做外部发送、不承诺法律/财务结论、涉及客户可见内容先让你确认。
同时我建议它第一天先做一份带证据链接的竞品扫描报告，不先安装额外 MCP。
这些设置可以吗？边界、第一任务和能力配置有没有要改的？
```

错误行为：

```text
公司历史上都这样设置边界和第一任务，所以我直接创建。
```

## 6. 公司 Agent DNA

### 6.1 目的

公司 Agent DNA 不是 HR Agent 从创建历史中自行归纳出来的一组记忆。

它应该是：

```text
公司权威知识库 / 治理语料 / 后台配置
  + 当前创建 session 中用户明确确认的需求
  + HR 创建历史中可审计、可降权使用的偏好信号
  + 通用岗位知识和外部研究的低优先级补充
```

HR Agent 的历史创建记忆只能说明“过去和 HR 交流过的人倾向怎样创建 agent”，不能直接代表“公司真正需要什么”。如果最初几次创建由少数人带偏，HR 不能把这种偏差固化成公司 DNA。

它回答：

- 这家公司偏好什么样的数字员工命名。
- 常见岗位类型是什么。
- 默认工作质量标准是什么。
- 默认边界是什么。
- 常见第一任务是什么形态。
- 哪些集成通常 day-one 必须具备。
- 哪些能力通常先不装，等 agent 自己在工作中进化。
- 这家公司认为“靠谱员工”的隐含标准是什么。

它不回答：

- 要不要创建这个员工。
- 公司应该招什么岗位。
- 用户的需求是否合理。
- 当前员工最终 blueprint 是否已经确认。
- 当前用户是否已经接受某条身份设定、工作合同、边界、权限或能力配置。

公司 Agent DNA 只能作为创建建议来源，不能成为 gate completion source。任何非当前 session 直接确认的内容都必须经过当前用户确认：

```text
Company Agent DNA -> propose default blueprint content
Current session user answer / explicit preview confirmation -> complete creation gate
```

### 6.2 证据层级与权重

HR 创建时必须按证据层级使用上下文：

| 层级 | 来源 | 用途 | 权威性 |
|---|---|---|---|
| P0 | 当前创建 session 的用户明确回答和 preview 确认 | 决定本次 agent 的最终 blueprint | 最高；本次创建的直接依据 |
| P1 | 公司权威知识库、公司章程、产品/业务文档、组织政策、权限/合规规则、后台 HR 创建策略 | 定义公司真实需求、边界、质量标准 | 公司 DNA 主来源 |
| P2 | 部门/团队知识库、岗位说明、项目资料、流程 SOP | 定义局部角色语境 | 需要按部门/权限范围使用 |
| P3 | HR Agent 的历史创建 session、T3 memory、explicit feedback | 识别过去创建偏好、常见表达、潜在模式 | 弱参考；不能直接当公司事实 |
| P4 | 模型通用岗位知识、web research、行业常识 | 在公司语料不足时补充角色理解 | 低优先级；必须标注为外部/通用建议 |

默认解释策略：

- P1/P2 与 P3 冲突时，以 P1/P2 为准；P3 只能作为“过去有人这样偏好”的证据。
- P3 只能生成 `suggested_by_history`，不能生成 `company_policy`。
- P4 只能生成 `suggested_by_general_knowledge`，不能生成公司语境结论。
- P0 是本次创建最终提交的唯一直接授权来源；即使 P1/P2/P3/P4 都支持，仍要让用户确认实质 blueprint 内容。

### 6.3 知识库不完善时的偏差控制

当前公司的知识库/语料库仍不完整，因此 HR 不能用历史创建经验填补所有空白。

知识库不足时的策略：

1. **降权历史经验**：历史创建经验只能作为候选建议，不能自动变成公司默认值。
2. **显式标注来源**：preview 中应区分：
   - `confirmed_by_user`
   - `supported_by_company_kb`
   - `suggested_by_history`
   - `suggested_by_general_knowledge`
   - `unknown_or_needs_company_source`
3. **暴露 knowledge debt**：如果某个关键设定缺少公司知识库支撑，应在 setup debt / knowledge debt 中显示，而不是伪装成公司 DNA。
4. **关键边界必须询问**：边界、权限、外部可见行为、质量红线、核心产出标准不能由历史经验自动填。
5. **避免早期用户偏差固化**：少数早期创建者的偏好不能被提升为公司标准；只有当它被公司知识库、管理员策略或多次明确反馈支持时，才可提升权重。

建议的上下文占比不是固定数学公式，而是 prompt/ranking 策略：

```text
公司权威知识库 / 后台策略：主导
当前用户确认：决定本次创建
HR 历史创建经验：弱参考，帮助提出建议
通用知识 / web research：兜底补充
```

在知识库不完善阶段，宁可多暴露“不确定/待公司语料补齐”，也不要让 HR history 伪装成公司真相。

### 6.4 HR 侧历史经验写入来源

HR 侧历史经验只从创建相关证据中学习。它学习的是“创建交互中观察到的偏好和模式”，不是直接学习“公司真相”：

- HR 与用户的创建对话。
- `preview_agent_blueprint` 的参数和结果。
- `create_digital_employee` 的成功结果。
- 创建后初始 `soul.md`、first tasks、triggers、setup debt。
- 用户对创建结果的明确反馈。

不能从无关聊天、其他 agent 私有工作内容或跨 tenant 数据中学习。

必须显式进入学习链路的创建事件：

- HR 用户消息和 HR 回复：由 web chat runtime 写入 T0 session ledger。
- `preview_agent_blueprint` tool call/result：作为同 session T0 证据和 ChatMessage tool_call 证据。
- `create_digital_employee` tool call/result：必须在成功创建后追加 typed T0 event，避免只靠普通聊天摘要猜测创建成功。
- 创建出的 agent 初始 identity artifacts：`soul.md`、first task、boot trigger、setup debt、installed/default skills。
- 用户后续明确反馈：例如“这个员工很符合/不符合我们公司风格”，进入普通 T0/T2/T3 或 explicit overlay。

公司知识库、产品语料、组织政策和后台治理配置不应被混入 HR 记忆文件里伪装成“历史经验”。它们应作为独立的 company knowledge retrieval lane 进入 HR 创建上下文。

### 6.5 记忆边界

HR 侧历史经验沉淀必须走现有 Memory Governance Layer：

```text
LLM 负责判断、提炼、归纳、候选生成；
平台负责 source_refs、权限、去重、审计、回滚、最终落盘。
```

实现上不应新增一套平行记忆系统。

当前 canonical memory layout 下，Accepted T3 只有四个可写语义文件：

```text
memory/t3/episodes.md
memory/t3/user.md
memory/t3/worker.md
memory/t3/capabilities.md
```

因此不得新增或写入：

```text
memory/t3/company_agent_dna.md
memory/company_agent_dna.md
logs/YYYY-MM-DD/**
memory/learnings/**
```

HR 侧历史经验沉淀的目标落点是 HR agent 自己 workspace 内的 accepted T3 semantic blocks：

| 目标文件 | 写入内容 |
|---|---|
| `memory/t3/episodes.md` | 单次创建案例、角色场景、成功/失败创建经验，作为后续相似角色 recall anchor |
| `memory/t3/worker.md` | 观察到的重复创建偏好、默认工作质量标准、红线、默认边界、汇报习惯；除非 source refs 包含公司知识库/后台策略，否则不得标记为 company policy |
| `memory/t3/capabilities.md` | 可复用创建方法、role archetype、能力路由习惯、setup debt 识别方法 |
| `memory/t3/user.md` | 只有当证据明确属于某个 user/owner 的偏好时才写；不得把公司默认值误写成个人偏好 |

### 6.6 Governed history-learning pipeline

HR 侧历史经验的写入链路必须是：

```text
HR creation session
  -> T0 append-only session ledger
  -> T2 Segment Package: summary.md / labels.md / review.md
  -> T3 Consolidator writes proposal
  -> Memory Gate reviews evidence, scope, conflict, safety
  -> Platform Gate validates target files, source_refs, rollback metadata
  -> accepted T3 blocks
  -> next HR creation retrieves relevant history suggestions
```

关键边界：

- T0 是原始证据层，平台只追加事件，不总结、不分类、不改写语义。
- T2 由 LLM summary / labels / review 生成候选，平台只校验结构、source refs、门禁。
- T3 只能由 accepted revised patch 进入四个 canonical files。
- HR tool handler 可以追加 typed T0 evidence，但不能直接写 T2/T3。
- 如果用户给出必须立即生效的明确偏好，使用 explicit overlay；后续仍由 T3 consolidation 吸收或拒绝。

### 6.7 Typed creation event

成功创建后，`create_digital_employee` 应追加一个 HR 专用 typed T0 event，例如：

```json
{
  "event_type": "hr_agent_created",
  "role": "tool",
  "content": "Created digital employee from confirmed HR blueprint.",
  "metadata": {
    "created_agent_id": "...",
    "created_agent_name": "...",
    "blueprint_hash": "...",
    "preview_session_id": "...",
    "requesting_user_id": "...",
    "tenant_id": "...",
    "permission_scope": "company",
    "risk_class": "standard|high",
    "archetype": "...",
    "primary_users": ["..."],
    "core_outputs": ["..."],
    "first_task_count": 1,
    "trigger_count": 0,
    "installed_skill_names": ["..."],
    "manual_setup_debt": ["..."]
  }
}
```

这个事件不是 memory candidate 本身；它只是 source-backed evidence，让 T2/T3 后续能稳定识别“本次创建了什么、为什么这样创建、哪些 setup debt 被暴露、哪些公司偏好可能值得沉淀”。

### 6.8 激活规则

HR 创建新员工时，应同时激活两个不同来源的上下文：

1. **Company Knowledge Lane**：公司知识库、产品/业务语料、组织政策、权限/合规规则、后台 HR 创建策略。
2. **History Suggestion Lane**：HR Agent 自己的创建历史、T3 memory、explicit feedback、相似创建案例。

激活内容应包括：

- 公司知识库中与当前角色相关的业务目标、流程、约束、质量标准。
- 后台策略中已配置的权限、合规、工具安装、审批边界。
- HR 历史中与当前角色类型相关的过往创建模式。
- 与当前角色类型相关的过往创建模式。
- 常见但未必权威的历史偏好、边界建议和质量标准。
- 常见 setup debt。

不应激活：

- 其他 tenant 的创建偏好。
- 用户无权访问的私有信息。
- 其他 agent 工作过程中的敏感内容。

激活实现不应硬编码读取某个 `company_agent_dna.md`。正确方式是构造两个检索 profile：

Company Knowledge retrieval query/profile：

```text
company goals, products, policies, org workflow, compliance boundaries
role mission: <current role>
department/team context: <if provided>
quality bar and governance requirements for digital employees
```

History Suggestion retrieval query/profile：

```text
prior digital employee creation preferences
role archetype: <current role>
setup debt patterns
historical governance suggestions for new digital employees
```

Company Knowledge Lane 的来源必须是后台知识库/语料库/策略配置。History Suggestion Lane 的来源才是 explicit overlay + accepted T3 + episodic recall。两条 lane 都必须保持 tenant / principal / sensitivity stripping 约束。

激活后的使用规则：

- 激活上下文可以减少 HR 的探索成本，但不能减少用户确认义务。
- 公司知识库支持的内容标记为 `supported_by_company_kb`；历史经验支持的内容标记为 `suggested_by_history`；通用知识补充标记为 `suggested_by_general_knowledge`。
- 历史建议中的默认身份、工作合同、边界、能力和 setup debt 必须标记为建议默认值。
- 用户确认后才能标记为 `confirmed_by_user` 并进入 preview/create 参数。
- 如果用户没有确认非当前 session 直接确认的建议内容，HR 必须继续询问；不能自动填充并创建。
- 如果用户只确认“创建吧”，但 preview 没有明确列出这些实质内容，不视为有效确认。

## 7. `create_employee` skill 目标结构

建议重写后的 skill 结构：

```text
---
name: create_employee
description: Create a company-fit digital employee as a day-one-useful workplace partner.
tools:
  - preview_agent_blueprint
  - create_digital_employee
  - web_search
  - web_fetch
  - firecrawl_fetch
  - discover_resources
  - search_clawhub
---

# Create Digital Employee

1. Role
2. Core Principle: user already wants creation
3. Company Agent DNA usage
4. Dynamic clarification
5. Workplace Partner Blueprint
6. Gates
7. Capability routing
8. Preview
9. Confirmed create
10. Creation case learning
11. Anti-patterns
12. Examples
```

必须明确写入的反模式：

- 不问“你确定要创建吗”作为流程判断。
- 不把“是否扩展现有 agent”作为前置分支。
- 不把创建降级为配置字段收集。
- 不创建泛用助手。
- 不用固定轮次拖慢用户。
- 不把 history-derived 建议当成用户决定。
- 不静默把“我记得你喜欢 / 公司通常会”写入 blueprint。
- 不前置安装一堆不必要能力。
- 不隐藏 setup debt。

## 8. 后端硬约束保留

Prompt 不是最终边界，后端硬约束仍必须保留：

1. `preview_agent_blueprint` 必须生成 `blueprint_hash`。
2. `create_digital_employee` 必须要求 `confirmed_blueprint_hash`。
3. 创建必须能在同一 session 找到匹配 preview。
4. 高风险或外部可见角色必须有边界。
5. 重复创建失败要停止重试并报告日志/config 问题。
6. 成功创建必须追加 source-backed typed T0 creation event。
7. HR learning 不得绕过 T2/T3 Memory Gate / Platform Gate。
8. 非当前 session 直接确认的 blueprint fields 必须在 preview 中显式披露来源，并经过用户确认后才能 create。

这些约束不是 HR 判断“是否创建”，而是防止创建动作失真、绕过确认或产生低质量员工。

## 9. 实现计划

### 9.0 文档先行修订

1. 将本文升级为当前基础设施口径：
   - 删除 `memory/t3/company_agent_dna.md` 作为实现目标。
   - 明确公司 Agent DNA 不等于 HR memory；公司知识库/治理语料/后台策略是主来源。
   - 明确 HR 历史经验落在 accepted T3 four-file semantic layer，只能作为 History Suggestion Lane。
   - 明确 HR tool 只追加 typed T0 evidence，不直接写 T2/T3。
   - 明确下一次创建通过 Company Knowledge Lane + History Suggestion Lane 双路 retrieval 激活上下文，而不是读固定文件。
2. 之后所有代码整改以本文修订版为准。

### 9.1 文档与模板

1. 新建或更新 prompt contract 测试，锁定：
   - `hr-guide` 不再存在于模板。
   - `create_employee` 是唯一创建 skill。
   - `soul.md` 不再复述完整 SOP。
   - `soul.md` 明确 HR 不判断是否创建。
   - `create_employee` 明确创建目标是 workplace partner / elite intern。
   - `create_employee` 明确公司知识库/治理语料优先，HR 历史经验只是弱参考建议。
2. 重写 `backend/hr_agent_template/soul.md`。
3. 合并 `hr-guide` 内容到 `create-employee/SKILL.md`，删除错误的判断职责。
4. 模板同步逻辑增加 retired skill cleanup。
5. 将 HR template version 升级到新版本，例如 `hr-flow-v3-company-dna-t3-2026-06-20`。

### 9.2 HR Agent 公司级语义

1. 保持每 tenant 一个 `__system_hr__`。
2. 明确 HR 是 company-scoped system agent，不是 first requester 的个人 agent。
3. 如果 DB 仍需要 `creator_id/sponsor_user_id`，将其视为 bootstrap 记录，不进入 HR identity prompt。
4. 保持 `AgentPermission(scope_type="company", access_level="use")`。

### 9.3 创建案例学习

1. 在 `create_digital_employee` 成功 commit 前后追加 typed T0 event：
   - `event_type="hr_agent_created"`
   - `session_id=request.context.session_id`
   - `tenant_id=effective_tenant_id`
   - `created_agent_id`
   - `blueprint_hash`
   - `risk_class`
   - `archetype`
   - `primary_users`
   - `core_outputs`
   - `manual_setup_debt`
   - `installed_skill_names`
2. event metadata 必须可追溯到同 session 的 `preview_agent_blueprint` 和 `create_digital_employee` tool calls。
3. T0 segment 在 session close / idle 后进入现有 T2 package build；不要新增专用 summary writer。
4. T2 labels prompt 增加 HR creation 场景识别能力：
   - `agent_creation_case`
   - `company_agent_dna_candidate`
   - `work_contract_pattern`
   - `setup_debt_pattern`
   - `governance_boundary_pattern`
5. T3 consolidation 将稳定偏好写入四个 canonical files：
   - creation cases -> `episodes.md`
   - company defaults / boundaries -> `worker.md`
   - reusable creation methods -> `capabilities.md`
   - user-specific explicit preferences -> `user.md`
6. 下次 HR 创建时通过 History Suggestion Lane 激活同 tenant、同 HR agent、同 role/archetype 相关的 accepted T3 / explicit / episodic evidence。

### 9.4 HR 创建上下文激活

1. 在 HR 创建场景构造双路 retrieval query/profile，而不是硬编码读文件。
2. Company Knowledge query 应包含：
   - 公司目标、产品、业务流程、组织政策、合规/权限边界。
   - 当前用户输入的 role / mission / primary users / core outputs。
   - 部门/团队上下文。
   - 公司对数字员工的质量标准和治理要求。
3. History Suggestion query 应包含：
   - 当前用户输入的 role / mission / primary users / core outputs。
   - 类似角色 archetype / prior creation cases。
   - setup debt 和治理边界习惯。
4. 激活结果进入 HR prompt 或 skill context，用于减少重复解释、提出更像本公司的默认值。
5. 不得激活其他 tenant 的 T3、explicit overlay 或知识库资料。
6. 不得把其他 agent 私有工作内容作为 HR 创建默认值，除非它已经通过 HR agent 自己的 governed T3 沉淀为 history suggestion，且仍不能作为 company policy。
7. 激活结果必须带 source attribution 进入创建流：`supported_by_company_kb`、`suggested_by_history`、`suggested_by_general_knowledge`、`unknown_or_needs_company_source`。
8. History Suggestion Lane 不得直接作为 confirmed blueprint input。
9. Preview payload 应能表达 source attribution 与 `confirmed_by_user` 的区别，至少在 prompt contract 中强制 HR 展示并确认。

## 10. 验收测试

### 10.1 Prompt / template tests

必须覆盖：

- `backend/hr_agent_template/skills/hr-guide/` 被退役。
- `create_employee` 是唯一 HR 创建 skill。
- `soul.md` 包含 “does not decide whether to create” 等价语义。
- `soul.md` 不包含完整字段清单、长 SOP、固定轮次。
- `create_employee` 包含 “workplace partner / elite intern / day-one useful” 等价语义。
- `create_employee` 不包含“ask whether to create / extend existing agent instead” 这类前置判断。
- `create_employee` 明确所有非当前 session 直接确认的建议都必须标注来源，并经过用户确认。
- `create_employee` 禁止把 history-derived 建议静默写入 create 参数。

### 10.2 Runtime tests

必须覆盖：

- GET `/agents/system/hr` 会同步新版模板。
- 旧 workspace 中的 `skills/hr-guide` 会被归档或移除。
- 旧 workspace 不会继续暴露两个创建 skill。
- `create_digital_employee` 仍要求同 session preview hash。
- HR 创建失败预算仍有效。
- 成功创建后会追加 `hr_agent_created` typed T0 event。
- history-derived / general-knowledge-derived blueprint 内容未展示给用户确认时，`create_digital_employee` 不应被调用。
- preview 文案必须展示建议来源/性质，并要求用户确认或修改。
- 公司知识库与 HR 历史经验冲突时，preview 必须以公司知识库为准，并把历史经验作为冲突/参考提示展示。

### 10.3 Memory / Knowledge tests

必须覆盖：

- 成功创建后 T0 ledger 包含 `hr_agent_created` event。
- T0 event 带 tenant/session/created_agent/blueprint/hash/source metadata。
- T2 package source_bundle 能看到该 typed event。
- T2 labels 能识别 `agent_creation_case` / `company_agent_dna_candidate`。
- T3 Platform Gate 拒绝 `memory/t3/company_agent_dna.md`。
- T3 accepted patch 只能写入 `episodes.md`、`worker.md`、`capabilities.md`、`user.md`。
- HR 下一次创建能同时调用 Company Knowledge Lane 与 History Suggestion Lane。
- HR 不激活其他 tenant 的创建偏好。
- HR 不从 legacy `logs/YYYY-MM-DD/**` 或 `memory/learnings/**` 读取公司 Agent DNA。
- HR 使用历史经验提出建议时，T0/T2 证据仍能区分 `suggested_by_history` 与 `confirmed_by_user`。
- HR 使用公司知识库提出建议时，preview 能标记 `supported_by_company_kb`。
- 公司知识库缺失时，preview 必须暴露 `unknown_or_needs_company_source` / knowledge debt，而不是把 HR 历史经验提升为公司标准。
- 公司知识库与历史经验冲突时，历史经验不得覆盖 `supported_by_company_kb`。

## 11. 非目标

本轮不做：

- 平台全局 HR Agent。
- 跨 tenant 创建偏好共享。
- 招聘审批流。
- 预算/编制判断。
- 组织架构规划。
- 让 HR 决定是否应该创建员工。
- 把公司 Agent DNA 做成新的平行记忆系统。
- 新增 `memory/t3/company_agent_dna.md`。
- 让 HR tool handler 直接写 accepted T3。
- 从旧 `logs/YYYY-MM-DD/**` 或 legacy `memory/learnings/**` 建立新的 prompt memory 依赖。
- 让 HR 基于记忆替用户决定身份、工作合同、边界、权限、能力或 setup debt。
- 让 HR 历史创建经验替代公司知识库、治理语料或后台策略。
- 在公司知识库缺失时，把少数早期用户偏好固化成公司标准。

## 12. 最终判断

这次整改的核心不是“再加流程”，而是把 HR 拉回产品目标：

```text
用户已经要创建员工；
HR 负责把这个员工塑造成符合公司语境、第一天能工作、以后能自进化的职场搭档。
```

因此：

- `HR Guide` 和 `Create Employee` 应合并。
- 只保留一个 `create_employee` skill。
- `soul.md` 缩成身份和边界。
- 公司级学习服务于“创建得更像这家公司”，不服务于“阻止创建”。
- 公司 Agent DNA 必须由公司知识库/治理语料/后台策略主导；HR 历史创建经验只能通过 T0/T2/T3 governed pipeline 沉淀为可追溯建议。
- 后端 gate 只保证创建一致性、安全性和审计性，不替代 HR 的塑形智能。
