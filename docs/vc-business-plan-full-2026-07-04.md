# Hiveclaw VC Business Plan 完整讨论稿

> 日期：2026-07-04
> 状态：完整讨论稿 v0.3，已吸收金融/咨询 ICP、独立第三方、渠道互补、企业部署、上下文控制和 AI 资产管理定位
> 用途：给 VC / Angel / strategic advisor 的 long-form business plan 底稿。后续可拆成 1 页 executive memo、10-12 页 pitch deck、customer discovery brief 和 financial model。
> 注意：本文中未由当前产品数据验证的内容会明确标为“假设”或“待补充”。不要把假设当作 traction 事实对外发送。

---

## 1. Executive Summary

### 1.1 一句话定义

Hiveclaw 是面向企业数字员工的 AI-native 组织中台：企业可以像管理真实员工一样，创建、授权、运行、观测和持续改进 AI 数字员工。

英文投资人版：

> Hiveclaw is the AI-native organization control plane for enterprise digital employees: companies can create, govern, run, observe, and continuously improve AI workers with identity, memory, tools, workflows, permissions, audit, and self-evolution.

### 1.2 公司使命

未来每家公司都会拥有一支由人类员工和 AI 数字员工共同组成的 workforce。模型公司会提供越来越强的智能，但企业仍需要一套中立、可控、可审计、可自部署的组织基础设施，来管理这些 AI worker 的身份、权限、工具、记忆、工作流、预算、审计和持续改进。

Hiveclaw 的使命是成为这层基础设施。

在 AI agent 时代，企业最重要的新型资产之一会是 agent 本身。一个成熟的数字员工不只是一个 prompt，而是一组持续积累的企业 AI 资产：身份、职责、权限、上下文、记忆、技能、工作流、工具配置、审计证据、历史产出和团队协作关系。Hiveclaw 因此既是产生这些 AI 资产的平台，也是管理这些 AI 资产的平台。

同时，Hiveclaw 必须被讲成一个独立第三方控制层：不绑定任何单一模型厂商、云平台、IM/OA 生态或办公套件。飞书、钉钉、Slack、Teams、企业微信、邮件和本地运行时都不是 Hiveclaw 的竞争对象，而是企业已有的工作入口和上下文渠道。Hiveclaw 的角色是在这些入口之上，提供统一的数字员工身份、权限、记忆、工具、审批、审计和持续改进能力。

这点对投资人很重要：未来企业不会只生活在某一个 AI 或 IM 生态里。真正有价值的基础设施，应该能进入客户已有环境，而不是要求客户迁移到另一个封闭工作流。

### 1.3 核心问题

企业已经在大规模试用 AI，但绝大多数 AI agent 仍停留在 pilot、demo、个人助手或局部 automation 阶段。真正进入生产时，企业会马上遇到一组“组织级问题”：

- 这个 AI agent 到底代表谁行动？
- 它能访问哪些工具、数据、文件和系统？
- 它能否跨会话记住经验，但又不泄露不该看的信息？
- 它执行任务失败、越权、重复操作或产生外部影响时，谁负责？
- 管理员如何审批、观测、回滚、审计和持续优化？
- 多个 agent 如何协作，而不是互相制造噪音？
- 当 agent 成为企业资产后，企业如何盘点、授权、复用、沉淀、评估和退役这些 AI 资产？

这些问题不是单个 chatbot、单个 prompt、单个 LangGraph workflow 或单个 RPA automation 能解决的。它们需要一个公司级 control plane。

### 1.4 解决方案

Hiveclaw 把 AI agent 从“一次性聊天窗口”提升为“稳定的组织行动者”：

1. Digital Employee Runtime
   - 每个 agent 都有身份、session、workspace、tools、memory、skills、runtime state。
   - Web chat、trigger、workflow、subagent、channel message 都进入同一条受治理 runtime。

2. Enterprise Control Plane
   - 管理权限、审批、预算、审计、组织结构、tool policy、agent lifecycle、observability。
   - 企业可以控制 agent 能做什么，而不是削弱 agent 怎么思考。

3. Self-Evolution / Learning Vault
   - 通过 T0/T2/T3/soul、Memory Gate、Platform Gate、skill candidates、verification、rollback，让数字员工随工作经验持续变强。
   - 记忆和技能演化可追溯、可审计、可回滚。

4. Neutral Enterprise Deployment Layer
   - Hiveclaw 以自部署 / 私有化 / 企业受控环境为核心产品方向，不把客户锁进某个模型、IM、云或办公生态。
   - 对企业 buyer 的承诺不是“再换一个协作工具”，而是“把可治理的 AI worker 带进你已经在用的协作、知识和业务系统”。

5. AI Asset Management Platform
   - Hiveclaw 不只是运行 agent，也负责把 agent 变成可管理、可复用、可改进的企业 AI 资产。
   - 企业可以创建数字员工、分配职责和权限、安装工具和技能、沉淀记忆和工作流、查看产出和审计、评估使用情况，并在需要时调整或退役。

### 1.5 Why Now

过去两年，LLM 的推理、工具调用、代码生成、长上下文和多步执行能力快速提升，agent demo 变得容易。但企业从 demo 进入 production 的主要瓶颈已经从“模型是否会回答”转向“组织是否敢让 AI 执行真实工作”。

公开市场研究也指向同一方向：

- Menlo Ventures 估计 2025 年企业 generative AI spend 达到 370 亿美元，其中 190 亿美元流向应用层，说明企业预算正在从底层模型转向直接生产力产品。
- McKinsey 2025 调研显示 88% 的组织已在至少一个业务函数中常规使用 AI，但多数仍未完成规模化。
- PwC 调研显示 88% 的企业计划因 agentic AI 增加 AI 预算，但组织变革、workflow integration、trust 仍是落地障碍。
- OpenAI 企业报告显示 AI 正在进入 repeatable multi-step workflows，Custom GPTs / Projects / API workflows 的使用强度快速提升。
- Forrester 相关报道指出，企业把 agent 当作 chatbot 而不是分布式系统，是 agentic AI 无法 operationalize 的关键原因之一。

这正是 Hiveclaw 的窗口：企业需要的不是更多 AI demo，而是一套可以把 AI workers 带入生产的组织级操作系统。

### 1.6 目标客户

建议第一阶段聚焦：

> 高判断密度、高信息密度、专业人才稀缺的知识服务组织，尤其是咨询、投研、券商、VC、PE、资产管理、B2B SaaS 战略/运营团队、研发型组织和跨境业务团队。

这类客户的共同点不是简单的成本替代，而是：核心产出高度依赖专家判断、上下文积累、资料整合、反复研究和可信交付。AI 如果只是一次性聊天工具，无法进入他们的专业流程；但如果被治理成可审计、可复用、能积累组织记忆的数字员工，就能成为专家团队的认知杠杆。

第一阶段仍建议从 30-500 人规模的 AI-forward 团队切入。这个规模足够感知专业脑力工作的瓶颈，也通常没有资源自建完整 agent control plane；他们更可能通过一个具体数字员工 workflow 购买，而不是先采购抽象平台。

### 1.7 初始产品切入

建议第一条 flagship workflow：

> Investment / Market Intelligence Digital Employee

理由：

- 任务频繁、知识密集、跨工具、跨资料源，天然适合咨询、投研、VC/PE、券商研究、市场战略团队。
- 对外部副作用风险低于交易执行、法务签署、生产运维，适合作为第一批生产化场景。
- 结果可验收，适合定义 ROI：节省信息整理时间、提升研究覆盖面、提高周报/月报一致性、减少重复背景解释。
- 能自然展示 Hiveclaw 的核心能力：身份、权限、企业知识库、工具、长期记忆、自动触发、审计、经验复用。

### 1.8 GTM 概要

建议第一阶段 GTM：

> Open-source trust + founder-led design partners + paid workflow pilots.

路径：

1. 用开源和技术内容建立可信度。
2. 找 5-10 个 design partners，每个只落一个高摩擦 workflow。
3. 以 4-8 周 paid pilot 进入客户。
4. 从一个 team / workflow / digital employee 扩展到多个数字员工、跨部门 workflow 和 enterprise governance。
5. 后续通过 AI consultants、automation agencies、SIs 作为 channel partners。

### 1.9 商业模式

初始可采用三层收入结构：

1. Paid pilot package
   - 固定费用，覆盖一个 workflow 的设计、部署和成功指标验证。

2. Managed Cloud
   - 平台费 + 每个 digital employee seat / worker fee + usage / compute pass-through margin。

3. Self-hosted Enterprise
   - 年费 license + support + premium governance / connectors / deployment package。

### 1.10 护城河

Hiveclaw 的护城河不来自某个模型 API，而来自组织级运行与学习闭环：

- Runtime moat：durable session、RuntimeTask、tool governance、restart/resume、audit spans。
- Governance moat：tenant / RLS、principal-aware memory/action boundary、capability policy、approval/preflight。
- Learning moat：T0/T2/T3/soul、Memory Gate + Platform Gate、skill candidates、verification/rollback。
- Product moat：数字员工作为可积累、可复用、可治理的企业 AI 资产，而不是一次性 assistant。
- Asset moat：Hiveclaw 同时生成和管理企业 AI 资产；每个 agent 的身份、权限、记忆、技能、workflow、产出和审计证据都会形成组织级资产图谱。
- Neutrality moat：独立第三方、model-neutral、channel-complementary，不与单一模型厂商、IM/OA 或云生态绑定。
- Deployment moat：self-hosted、private deployment、enterprise-grade control plane；当前 README 已将 Hive 定位为可自部署组织中台，后续对外叙事应把“傻瓜式部署到企业环境”产品化成明确卖点。
- Data-control moat：在 self-hosted / private deployment 下，transcript、workspace files、memory vault、enterprise knowledge、policy、approval、audit 等控制面上下文可以留在企业控制的基础设施内；模型调用是否外发由客户选择的模型/provider 策略决定。

### 1.11 需要补充的关键事实

这份计划书要变成可对外版本，还必须补齐：

- 真实 traction：用户数、design partners、waitlist、GitHub stars、付费 pilot、部署数。
- 真实 ROI：一个 workflow 的人工 baseline、Hiveclaw 后节省时间、质量提升、错误减少。
- 真实 pricing evidence：目标客户愿意为 digital employee workflow 支付多少钱。
- 真实 financial model：推理成本、sandbox 成本、infra 成本、support 成本、毛利率。
- 团队与融资金额：创始人背景、招聘计划、融资目标和资金用途。

---

## 2. Problem

### 2.1 企业 AI adoption 高，但 production agent 少

企业已经不需要被说服“AI 很重要”。真正的问题是：AI adoption 很高，但可规模化、可审计、可治理的 agentic production 很少。

许多企业当前状态是：

- 员工用 ChatGPT / Claude / Copilot 做个人生产力。
- 团队用 prompt、Custom GPT、automation、RPA、LangGraph / CrewAI demo 做局部尝试。
- IT / AI platform team 尝试接入模型 API、内部知识库、工具调用。
- 但一旦进入真实业务流程，就卡在权限、审计、失败恢复、数据边界、成本、审批、责任归属上。

这导致 AI 很热，但实际组织生产力提升不稳定。

### 2.2 为什么现有方案不够

1. Chatbot / Copilot 解决的是个人交互，不是组织执行。
   - 它们擅长回答和辅助，但不天然承载公司级身份、权限、预算、审批、长期记忆和审计。

2. Agent framework 解决的是开发，不是运营。
   - LangGraph、CrewAI、AutoGen 等让开发者构建 agent flow，但企业还需要 lifecycle、policy、approval、observability、multi-tenant、memory governance。

3. RPA / workflow automation 解决的是确定流程，不是 LLM-native worker。
   - Zapier、Make、UiPath 适合明确触发器和确定步骤。AI 数字员工需要在不确定输入下判断、检索、计划、调用工具、复用经验。

4. Enterprise search 解决的是找信息，不是执行任务。
   - Glean 等产品强化知识访问，但 agentic work 还需要用知识行动，并记录每一步 evidence。

5. Vertical AI agent 解决单岗位单流程，但难以成为公司统一控制面。
   - 企业最终会有多个 AI workers，分布在销售、客服、运营、研发、财务、人事、研究等岗位，需要统一治理。

### 2.3 客户痛点

#### Economic buyer 的痛点

- AI 工具很多，但没有形成可衡量 ROI。
- 团队还在重复做研究、整理、跟进、文档、报告、内部协调。
- 增长需要更多 headcount，AI 没有真正变成 workforce multiplier。
- 担心 AI 出错、越权或泄露信息，不能放手让它执行。

#### Technical buyer 的痛点

- 内部 agent demo 越来越多，但缺少统一 runtime。
- 权限、审计、工具凭据、MCP、知识库、session、memory、日志各自为政。
- 难以复现 agent 失败过程，也难以证明某个 agent 安全可控。
- 既担心 vendor lock-in，又不想自己从零搭 control plane。

#### Functional sponsor 的痛点

- 具体业务团队有大量重复知识工作。
- 一次性 AI 回答有用，但不能稳定跟进长期工作。
- 同一个上下文要反复解释，经验无法沉淀。
- AI 输出质量无法持续改进，也无法形成团队级可复用流程。

#### End user 的痛点

- 又多了一个聊天窗口，而不是少了工作。
- 不知道 agent 做了什么、为什么这么做、是否能信任。
- 手动复制粘贴、上传文件、重讲背景、检查结果仍然耗时。

---

## 3. Solution

### 3.1 核心产品主张

Hiveclaw 把 agent 变成企业可运营的数字员工。

一个 Hiveclaw 数字员工不是 prompt template，而是完整组织对象：

- 有身份：代表谁、服务哪个团队、承担什么角色。
- 有权限：能访问哪些工具、知识、文件、渠道和外部动作。
- 有记忆：能积累公司、用户、角色、任务和反馈经验。
- 有技能：能通过验证过的 skill candidates 扩展能力。
- 有工作区：能产生、读取、维护文件和交付物。
- 有 session：每次工作都有 transcript、timeline、tool events、checkpoint。
- 有 runtime：任务可持久运行、恢复、取消、审计。
- 有治理：敏感动作需要 preflight / approval / policy。
- 有进化：从 T0 evidence 到 T2 review 到 T3 semantic memory，再到 skill / soul 改进。

### 3.2 产品不是“聊天”，而是组织执行系统

对外叙事要避免让投资人以为这是 chatbot UI。Hiveclaw 的价值在“组织执行闭环”：

```text
User / Trigger / Channel
        |
        v
Digital Employee Session
        |
        v
Context + Memory + Skills + Tools + Governance
        |
        v
Agent Runtime executes work
        |
        v
Artifacts + Audit + Evidence + Feedback
        |
        v
Memory / Skill / Governance Improvement
```

这套闭环让 agent 不只是回答，而是长期参与组织工作。

### 3.3 三层架构

#### Layer 1: Digital Employee Runtime

目标：让单个 agent 能稳定完成长期任务。

能力：

- ChatSession / RuntimeTask
- Durable runs
- Tool loop
- Workspace
- Files and artifacts
- Checkpoint / branch / rewind
- Plan Mode / confirmed autonomy
- Workflow / subagent / Agent Team
- Channel integration

投资人语言：

> Hiveclaw gives every AI worker a persistent runtime, not just a chat thread.

#### Layer 2: Enterprise Control Plane

目标：让企业敢把真实任务交给 agent。

能力：

- Tenant / organization / role
- Agent lifecycle management
- Capability policies
- Session permission profiles
- Approval workflows
- Action preflight
- MCP / connector governance
- RLS / principal-aware data access
- Audit logs / invocation spans
- Budget / quota / observability

投资人语言：

> Hiveclaw turns autonomous AI from a risk into a governed enterprise asset.

#### Layer 3: Self-Evolution / Learning Vault

目标：让 agent 越用越懂公司，越用越会做事。

能力：

- T0 raw evidence
- T2 reviewed segment packages
- T3 semantic memory
- `soul.md` identity and role contract
- Memory Gate + Platform Gate
- Skill candidate packages
- Verification and rollback
- Session feedback and lifecycle hygiene

投资人语言：

> Hiveclaw makes AI workers compound organizational knowledge over time.

### 3.4 横向定位：独立第三方，不做封闭生态

三层架构之上，Hiveclaw 需要有一个清楚的商业定位：它不是某个模型公司的 agent dashboard，也不是某个 IM / OA 厂商的 AI 插件，而是独立第三方的企业 AI worker control plane。

对外可以这样讲：

> Hiveclaw is the neutral control plane that lets enterprises deploy, govern, and improve AI workers across their existing models, collaboration channels, knowledge systems, tools, and runtime environments.

这句话解决三个 investor question：

1. 为什么不是模型厂商直接做？
   - 模型厂商天然倾向于把客户留在自己的模型和生态里，而企业长期会采用多模型、多云、多工具和多渠道。

2. 为什么不是飞书、钉钉、Slack、Teams 直接做？
   - IM / OA 是工作发生的入口，Hiveclaw 是数字员工身份、权限、记忆、工具、审批、审计和学习的控制层。二者互补，不互斥。

3. 为什么不是企业自建？
   - 自建可以做到单个 demo，但要补齐 runtime、治理、记忆、审计、权限、部署、升级和可用性，成本会迅速上升。

### 3.5 企业内上下文控制与部署承诺

README 已将 Hive 定位为可自部署的 AI Native 组织中台。Pitch 里应把它升级成更清楚的企业承诺：

> Hiveclaw can be deployed into the enterprise environment so the organization's operational context stays under enterprise control.

这里要避免过度承诺。更准确的表达是：

- 在 self-hosted / private deployment 下，企业知识库、workspace 文件、session transcript、T0/T2/T3 memory、权限策略、审批记录、审计日志、组织结构和 agent 配置可以留在企业控制的基础设施内。
- 模型调用是否经过外部 provider，取决于客户选择的模型部署方式：可接企业批准的 API provider，也可接私有化 / 自托管模型能力。
- 因此 Hiveclaw 卖的不是“永不出网”的绝对承诺，而是企业可控的部署、上下文、治理和模型路由边界。

面向 VC 的产品化表达：

- One-click enterprise deployment：把当前 self-hosted / Docker / Railway 能力产品化成企业 IT 能快速部署、升级、回滚和观测的 package。
- Context stays under enterprise control：把 agent 运行需要的组织上下文、工作证据和治理数据留在客户控制面内。
- Bring AI workers to existing workflows：通过渠道和 connector 进入飞书、钉钉、Slack、Teams、企业微信、邮箱、文件和知识库，而不是要求客户迁移工作入口。

---

## 4. Product Shape

### 4.1 当前已有产品 surface

这部分必须基于 Hiveclaw 当前真实产品来讲，不应写成尚未存在的理想体验。当前可对外组织成以下产品路径：

1. Enterprise Control Plane
   - `/enterprise/dashboard` 是公司级操作台，已按 operating areas 暴露 Digital Employees、Models & Budget、Capabilities & Tools、Team & Delegation、Memory Governance、Channels & Integrations、Approval Center、Audit Log、Assets & Automation、Members & Roles、Organization Structure、Quotas、Invitation Codes、Local Agent Channel。
   - 这可以支撑“企业不是在管理一堆聊天窗口，而是在管理 AI workforce 的身份、权限、预算、工具、记忆、审批和审计”。
   - 投资人叙事上，这也是企业 AI 资产管理台：盘点有哪些数字员工、它们属于谁、能做什么、消耗多少预算、沉淀了什么能力、是否需要审批或干预。

2. Digital Employees Directory
   - `/enterprise/digital-employees` 已是数字员工目录，支持搜索、筛选 owned/shared/running/needs attention/local runtime，并能进入 Chat、Memory、Workflows、Team、Workspace/Local 和 Detail。
   - 这可以支撑“数字员工是公司资产，有生命周期、状态、owner、共享边界和运行入口”。
   - 对外可以讲成 AI asset registry：每个 agent 都是一个可查询、可授权、可运行、可复盘、可改进的企业资产，而不是散落在个人账号里的 prompt。

3. HR Agent Creation Path
   - `/agents/new` 当前不是普通表单，而是进入 HR Agent 创建路径：由 HR Agent 澄清 role、authority、capability packs、memory boundaries 和 first working session，再通过 governed backend 创建员工。
   - 这适合对 VC 讲成“员工创建本身就是一个治理流程”，而不是让用户随便堆 prompt。

4. Agent Detail / Workbench
   - Agent Detail 已按 workbench areas 组织：Status/Activity、Chat/Aware、Tools/Skills/Workflows、Knowledge/Evolution、Subagents/A2A、Workspace/Office、Approvals/Settings。
   - 这可以支撑“一个数字员工的工作、记忆、工具、技能、协作、文件和审批在同一个工作台里被管理”。
   - 这也是单个 AI 资产的详情页：企业可以看到它的能力配置、工作记录、知识沉淀、产出文件、风险动作和改进路径。

5. Workspace Feature Hub
   - 已有 Plans、Automations、Memory & Knowledge、Documents & Research、Approvals、A2A / Team 这些跨员工功能入口。
   - 这可以支撑“客户不是只进入单个 agent chat，也可以从公司工作流、记忆、审批、文档和团队协作角度管理工作”。

6. Enterprise Knowledge Base and Company Info
   - 当前有 tenant-scoped enterprise knowledge-base 文件列表、上传、读取、编辑、删除；管理员上传后可做文本抽取和 OpenViking indexing。
   - 这可以支撑投研/咨询类客户的核心诉求：把公司资料、研究资料、行业资料、项目背景放进受控知识面。

7. Approvals / Audit / Quotas / Tools
   - Enterprise API 已有 approval list/resolve、audit logs、security audit query/export、tenant stats、tenant quotas、tool/capability governance。
   - 这可以支撑“金融和咨询客户关心的不是 AI 能不能说，而是过程是否能解释、谁批准、谁触发、是否越权、是否可追溯”。

8. Local Agent Channel
   - `/local-agents` 已支持 Hive Connect 登录、direct local chat、local channel transcript、workspace file upload/read/download。
   - 这可以作为 “hybrid / local runtime path” 的证据，但不要把它讲成所有客户都会立即使用的主路径。

9. Channel Complement Model
   - 当前 README 和产品文案已经把 “User / Trigger / Channel / Agent” 放进同一 runtime loop；前端也有 Channels & Integrations、飞书/Slack/Teams/钉钉/企业微信等渠道配置与工具文案。
   - 对外不应说 Hiveclaw 要替代飞书、钉钉或 Slack，而应说 Hiveclaw 让数字员工进入这些既有工作入口，并在背后统一身份、权限、记忆、审批和审计。

### 4.2 第一屏产品体验应该怎么讲

对一个新客户，Hiveclaw 当前更适合展示为“控制台 + 员工目录 + 员工工作台”的组合：

1. 管理员进入 Control Plane
   - 先看到 Employees、Models & Budget、Capabilities & Tools、Memory Governance、Approvals、Audit、Knowledge/Integrations。

2. 通过 HR Agent 创建数字员工
   - 不是填一个 prompt，而是澄清岗位、职责、权限、记忆边界和第一条工作任务。

3. 在 Digital Employees 目录管理员工
   - 查看 running / needs attention / local runtime / shared / owned。
   - 进入某个员工的 Chat、Memory、Workflows、Team、Detail。

4. 在 Agent Workbench 执行和审阅工作
   - Chat 承接任务。
   - Knowledge / Evolution 承接记忆和改进。
   - Tools / Skills / Workflows 承接能力和自动化。
   - Workspace / Office 承接文件和交付物。
   - Approvals / Activity 承接治理和审计。

5. 在公司级 Hub 复盘和扩展
   - Plans 看待确认计划。
   - Automations 看工作流和触发器。
   - Memory & Knowledge 看学习资产。
   - Documents & Research 看文件证据。
   - Approvals 看敏感动作。
   - A2A / Team 看协作。

### 4.3 Flagship Demo：Investment / Market Intelligence Digital Employee

建议用于 VC demo 的完整流程，应尽量贴近当前已有 surface：

1. Control Plane 准备公司环境
   - 在 Company Info / Enterprise Knowledge Base 放入公司介绍、投资 thesis、行业关注方向、历史 memo、竞品或标的清单。
   - 在 Models & Budget、Capabilities & Tools、Memory Governance、Approvals 中展示边界配置。

2. 通过 HR Agent 创建 “Investment Research Analyst” 或 “Market Intelligence Analyst”
   - 让 HR Agent 澄清：它服务哪个团队、研究范围是什么、可访问哪些资料、能调用哪些工具、哪些动作需要审批。

3. 在 Digital Employees 目录查看该员工
   - 展示员工状态、owned/shared、running/needs attention，并从目录进入 Chat / Memory / Workflows / Team。

4. 在 Agent Detail Chat 发起任务
   - 示例任务：“请基于我们现有 thesis，跟踪 AI agent infrastructure 方向 10 家公司，输出本周变化、潜在投资启发、需要人工继续判断的问题。”

5. Agent 执行研究并生成 artifact
   - 使用现有 chat/session runtime、工具治理、workspace/file artifact、knowledge context。
   - 如果涉及外部可见或敏感动作，通过 approval/preflight 展示治理边界。

6. 在 Workbench 复盘结果
   - Activity / Session timeline 展示执行过程。
   - Workspace / Documents & Research 展示报告或研究文件。
   - Knowledge / Memory 展示经验如何进入受治理记忆路径。
   - Workflows / Automations 展示如何把 recurring research 变成后续自动化资产。
   - Audit / Approval Center 展示谁触发、谁批准、做了什么。

这个 demo 显示的核心不是“AI 会写投研报告”，而是：

- 专业团队可以把一个长期研究职责交给一个可治理数字员工。
- 资料、工具、记忆、审批和审计在现有产品面内闭环。
- AI 的价值不是替代专家判断，而是持续压缩资料收集、初步分析、交叉整理和重复交付的时间。
- 人类专家保留最终判断，Hiveclaw 负责让 AI worker 的过程可控、可复用、可追溯。

### 4.4 产品边界

Hiveclaw 不应该在第一版商业叙事里承诺：

- 替代所有员工。
- 自动完成所有业务流程。
- 无需人工审批即可执行高风险动作。
- 自训练 base model。
- 成为所有行业的 vertical AI agent。

更清晰的边界是：

- Hiveclaw 是 agent workforce control plane。
- 初期从低风险、高频、知识密集 workflow 切入。
- 对高风险动作采用 governed autonomy：policy、preflight、approval、audit。
- 模型能力来自多 provider，Hiveclaw 的价值是 runtime、governance、memory、workflow 和 control plane。

---

## 5. Customer Profile / ICP

### 5.1 Beachhead ICP

建议第一阶段聚焦：

> AI-forward professional knowledge organizations, 30-500 employees, where expert attention, research synthesis, judgment workflows, and controlled knowledge reuse are daily operating constraints.

中文：

> 30-500 人规模、AI 接受度高、专业判断密集、信息处理密集，但没有能力自建完整 AI agent control plane 的团队。

更具体地说，第一阶段应该把“咨询 + 金融专业服务 + 高密度知识工作团队”作为主叙事，而不是泛泛的 SMB。表达上要避免落入简单成本替代叙事：我们解决的是专家注意力稀缺、研究上下文难复用、信息处理链路碎片化、交付过程难审计的问题。

### 5.2 推荐优先行业

| 行业 / 公司类型 | 为什么适合 | 初始 workflow |
|---|---|---|
| 咨询 / 专业服务 | 大量行业研究、客户背景、proposal、访谈纪要、交付物复用；专家注意力是核心瓶颈 | client research、proposal draft、行业监控、项目知识库 |
| VC / PE / 投资机构 | thesis、deal sourcing、company tracking、market map、memo 初稿高度依赖资料整合和判断框架 | market map、company watchlist、investment memo support、portfolio intelligence |
| 券商 / 研究 / 资管 | 高频信息跟踪、公告/新闻/研报整理、行业比较、组合观察，需要可追溯来源 | sector monitor、issuer/company tracker、weekly research brief、risk signal summary |
| B2B SaaS 战略/运营团队 | 重市场、重客户、重竞品、重产品情报，技术接受度高 | competitor monitoring、release research、customer insight summary |
| 跨境运营团队 | 工具多、信息源多、流程重复，需要多语言/多渠道资料整合 | supplier / market monitoring、content ops、customer follow-up |
| AI-first agencies / automation firms | 有客户需求，缺统一 runtime | 给客户交付 governed AI workers |
| 研发型团队 | 内部知识库、issue、docs、release、code context 多 | engineering research、issue triage、release assistant |

### 5.3 不建议第一阶段重攻的客户

| 客户类型 | 暂不优先原因 |
|---|---|
| 超大型企业总部 | 销售周期长、合规复杂、需要大量 enterprise checklist |
| 金融交易执行 / 投资决策自动化 | 第一阶段不应碰“自动下单/自动投资决策”；可以服务投研、资料整理、memo support 和 portfolio intelligence |
| 低 AI 接受度传统 SMB | 教育成本高，ROI 解释难 |
| 单纯个人 productivity 用户 | ACV 低，无法体现企业 control plane 价值 |

### 5.4 Buyer / Sponsor / End User

| 角色 | 典型 title | 购买动机 | 反对意见 | 应对 |
|---|---|---|---|---|
| Economic buyer | CEO, COO, Founder, CFO | 用 AI 扩大专业产能、提高运营速度、让团队沉淀可复用知识资产 | “ROI 不确定” | 用 paid pilot 绑定前后 baseline |
| Technical buyer | CTO, Head of AI, IT Lead | 统一 agent runtime、权限、审计、集成和部署 | “我们可以自己搭” | 强调 time-to-production、治理闭环、自部署、维护成本 |
| Functional sponsor | Head of Ops, Research Lead, Product Lead | 减少具体 workflow 的人工工作量 | “AI 输出不稳定” | 从低风险 workflow 开始，保留 review 和 audit |
| End user | Analyst, PM, Ops, Engineer | 少重复解释、少复制粘贴、少整理资料 | “又多一个工具” | 嵌入现有渠道、保留上下文、自动交付 artifact |

### 5.5 用户画像样例

#### Persona A: Investment / Research Lead

- 公司：VC / PE / 券商研究 / 资产管理 / corporate strategy 团队。
- 痛点：团队每天处理大量公司、行业、新闻、研报、会议纪要和内部观点；信息散在不同系统里，重复整理耗费专家注意力。
- 采购触发：已经试过 ChatGPT / Claude，但无法形成团队流程。
- 关心指标：资料覆盖面、来源可追溯、memo 初稿质量、每周节省多少分析准备时间、是否保留人类最终判断。
- Hiveclaw pitch：先部署一个 Investment / Market Intelligence 数字员工，4 周内证明 recurring research workflow 的 ROI。

#### Persona B: CTO / Head of AI

- 公司：专业服务、金融服务、B2B SaaS 或研发型公司。
- 痛点：内部有多个 agent demo，但权限、日志、部署、工具凭据和 memory 治理混乱。
- 采购触发：CEO 要求把 AI 从 demo 推到 production。
- 关心指标：安全、权限、self-hosted、model-neutral、observability、integration。
- Hiveclaw pitch：统一 agent runtime 和 governance，让团队少造基础设施，专注业务 workflow。

#### Persona C: AI-forward COO / Operations Lead

- 公司：150-500 人专业服务 / B2B SaaS / 跨境运营团队。
- 痛点：运营团队每周做大量客户、市场、竞品、内部资料整理，产出慢且不一致。
- 采购触发：AI 工具已被个人使用，但无法形成组织级稳定产出。
- 关心指标：每周节省多少小时、报告是否稳定、团队是否愿意用、是否可审计。
- Hiveclaw pitch：先部署一个 Market Intelligence / Research Ops 数字员工，把 recurring intelligence 工作从个人手工整理变成受治理的团队资产。

#### Persona D: AI automation agency founder

- 公司：20 人 agency，服务多个 SMB 客户。
- 痛点：客户想要 AI employee，但每个项目都像 custom build，维护困难。
- 采购触发：需要可复用交付底座。
- 关心指标：部署速度、可配置性、white-label / self-hosted、客户权限隔离。
- Hiveclaw pitch：成为 agency 的 agent delivery platform。

---

## 6. Market

### 6.1 Top-down market view

市场可以从三层理解：

1. Enterprise AI Applications
   - 企业已经把预算从底层模型扩展到应用层。Menlo Ventures 估计 2025 年企业 generative AI spend 为 370 亿美元，其中 190 亿美元在应用层。

2. Agentic AI / Digital Workforce
   - MarketsandMarkets 估计 Agentic AI 市场从 2025 年 70.6 亿美元增长到 2032 年 932 亿美元，CAGR 44.6%。这个数字适合作为方向性参考，但正式材料需要再次核验来源和定义。

3. Enterprise Agent Control Plane
   - 这是 Hiveclaw 要定义的新子类：不是模型 API、不是单一 agent app，而是组织级 agent runtime、governance、memory 和 lifecycle control plane。

### 6.2 Bottom-up market sizing 假设

以下是讨论用假设，不是已验证市场模型：

#### Beachhead SAM 假设

- 目标公司：AI-forward professional knowledge organizations，30-500 人，重点覆盖咨询、VC/PE、券商研究、资管、B2B SaaS 战略/运营和高密度研究团队。
- 地区：先以美国 + 国际英语市场 + 中国出海团队为主。
- 可服务公司数假设：50,000-150,000 家。
- 初始可售 workflow：investment / market intelligence、client/project research、internal knowledge、research operations、engineering/product research。
- 年度 ACV 假设：
  - small team plan：$6k-$18k / year。
  - mid-market plan：$24k-$120k / year。
  - self-hosted enterprise：$50k-$250k+ / year。

粗略 SAM：

```text
50,000 companies * $12k ACV = $600M
150,000 companies * $50k ACV = $7.5B
```

这个区间只用于投资叙事初稿；正式版本需要按行业、员工数、AI spend、workflow budget 和地区拆分。

#### 3-year SOM 假设

假设 Hiveclaw 三年内做到：

- 150-300 付费客户。
- blended ACV：$20k-$60k。
- ARR：$3M-$18M。

这不是 forecast，只是说明该市场具备 venture-scale path。正式模型应由实际 pilot conversion、sales cycle、gross margin、churn / NDR 推导。

### 6.3 市场趋势对 Hiveclaw 的含义

1. AI budgets are rising
   - 有预算，但预算会流向能证明 ROI 和生产可控的产品。

2. Enterprises prefer buying over building
   - 对中型公司尤其重要。自建 agent control plane 成本高、人才稀缺、维护复杂。

3. Workflow redesign beats tool adoption
   - Hiveclaw 不应只卖“工具”，而要卖“数字员工工作流”。

4. Governance is becoming a buying criterion
   - 这对 Hiveclaw 是机会，因为公司核心设计正是 identity、permission、audit、memory gate、platform gate。

5. Model commoditization increases control-plane value
   - 模型越强，企业越需要一层 model-neutral governance 来决定这些模型能代表谁做什么。

---

## 7. Go-to-Market Strategy

### 7.1 GTM 总策略

建议采用：

> Open-source trust + founder-led design partners + paid workflow pilots + land-and-expand.

不要第一天就卖“公司全量 AI 操作系统”。第一天卖的是：

> We deploy one governed digital employee for one high-value research or intelligence workflow and prove measurable expert leverage in 4-8 weeks.

### 7.2 Phase 1: Design Partner Motion

目标：找到 5-10 个愿意一起定义产品的客户。

时间：0-6 个月。

客户选择标准：

- 已经在用 ChatGPT / Claude / Copilot。
- 有明确重复研究、资料整理、市场跟踪、客户/标的/行业分析或内部知识复用工作。
- 有 owner 愿意每周反馈。
- 有预算或至少愿意付 pilot fee。
- 数据敏感度适中，适合先落低风险 workflow；金融客户第一阶段只做研究支持和知识工作，不碰交易执行或自动投资决策。

Pilot 结构：

- 周期：4-8 周。
- 范围：1 个 digital employee + 1 个 workflow + 1 个团队。
- 成功指标：
  - 每周完成任务数。
  - 人工节省小时数。
  - 任务 cycle time。
  - 输出质量 review score。
  - 复用率 / repeat usage。
  - 是否扩展到第二个 workflow。

Pilot 定价假设：

- Early design partner：$5k-$15k。
- Standard paid pilot：$15k-$50k。
- Self-hosted / compliance-heavy pilot：$50k+。

### 7.3 Phase 2: Repeatable Sales Motion

目标：从 bespoke pilot 变成可重复销售。

时间：6-18 个月。

动作：

- 固化 2-3 个 packaged workflow：
  - Investment / Market Intelligence Digital Employee
  - Consulting / Client Research Digital Employee
  - Ops Coordinator Digital Employee
  - Engineering Knowledge Assistant
- 做标准 demo environment。
- 输出 ROI calculator。
- 输出 security / deployment one-pager，明确 self-hosted/private deployment、企业上下文控制边界、模型 provider 选择边界。
- 输出 one-click enterprise deployment package：让当前可自部署能力变成客户 IT 可以按文档快速部署、升级、回滚和监控的交付包。
- 输出 buyer-specific deck：
  - CEO / COO：ROI 和 workforce leverage。
  - CTO / Head of AI：architecture、security、self-hosted、model-neutral、ecosystem-neutral。
  - Functional lead：workflow、artifact、daily usage。
- 建立 customer success playbook。

### 7.4 Phase 3: Enterprise Expansion

目标：从单 workflow 扩展到 organization control plane。

时间：18-36 个月。

扩展路径：

1. More digital employees
   - 从 Investment / Market Intelligence 扩到 Client Research、Portfolio Intelligence、Ops、Product、Engineering、HR。

2. More governance
   - 加入企业级 approval、audit、policy、quota、connector ACL。

3. More integrations
   - Feishu / Slack / Teams / Google Drive / Notion / Jira / GitHub / CRM。

4. More deployment models
   - Managed cloud、VPC、self-hosted、hybrid local-agent。

5. More partner-led implementations
   - AI consultants / SIs 帮客户设计和维护 workflow。

### 7.5 渠道策略

| 渠道 | 目标 | 动作 |
|---|---|---|
| Founder-led outbound | 找 design partners | 直接触达 AI-forward COO / CTO / founder |
| Expert workflow outbound | 找金融/咨询 design partners | 触达 VC/PE partner、research lead、consulting partner、strategy lead |
| Open source / GitHub | 建技术可信度 | 文档、demo、reference architecture、社区 issue |
| Thought leadership | 建 category | 写 agent governance、digital workforce、self-evolution、AI control plane |
| IM / OA / cloud ecosystem partners | 进入客户既有工作入口 | 把飞书、钉钉、Slack、Teams、企业微信等讲成渠道和互补生态，不讲成竞争替代 |
| Partner agencies | 扩交付 | 给 AI consultants 一套可复用底座 |
| Product-led entry | 降摩擦 | local/self-hosted quickstart + sample employee templates |

### 7.6 核心 GTM 指标

早期不要只看注册数。应该看：

- Qualified discovery calls / month。
- Paid pilot conversion。
- Pilot time-to-value。
- Weekly completed agent tasks。
- Workflow repeat rate。
- Number of active digital employees per org。
- Expansion from 1 workflow to 2+ workflows。
- Human review score。
- Hours saved / month。
- Gross margin after inference and infra cost。
- Logo conversion from pilot to annual contract。

---

## 8. Business Model

### 8.1 收入结构

#### Paid Pilot

用途：降低客户决策门槛，同时避免免费咨询。

可能价格：

- Design partner：$5k-$15k。
- Standard pilot：$15k-$50k。
- Enterprise pilot：$50k-$100k+。

交付物：

- 1 个 digital employee。
- 1 个 workflow。
- 基础权限与审计。
- 成功指标 baseline / after measurement。
- 期末 expansion plan。

#### Managed Cloud Subscription

用途：标准 SaaS 收入。

可能结构：

- Platform fee：$500-$5,000 / month。
- Digital employee fee：$50-$500 / worker / month，按能力和使用量分层。
- AI asset management fee：按 active digital employees、governed workflows、managed memory/knowledge volume 或 audit retention 分层。
- Usage：模型、sandbox、web crawling、document processing 等成本加价或 pass-through。

#### Self-hosted Enterprise License

用途：满足数据敏感、合规、私有部署客户。

可能结构：

- Annual license：$50k-$250k+。
- Support / SLA：license 的 15%-25%。
- Premium connectors / governance package。
- One-click enterprise deployment package：Docker / private cloud / VPC / future Kubernetes package、升级、回滚、健康检查和基础运维手册。
- Professional services for deployment / integration。

这部分要成为金融、咨询和中大型专业服务客户的核心购买理由：他们不是只买一个 AI workflow，而是在买一套能进入企业环境、保留组织上下文控制权、并连接既有协作系统的数字员工基础设施。

### 8.2 Pricing Packaging 建议

#### Starter

目标：小团队，试用一个数字员工。

包含：

- 1-3 digital employees。
- 基础 tools / web research / workspace。
- 基础 memory。
- 基础 audit。

#### Team

目标：mid-market team。

包含：

- 5-20 digital employees。
- workflow / triggers。
- approval。
- team knowledge。
- standard connectors。
- usage analytics。

#### Enterprise

目标：有合规和治理要求的组织。

包含：

- unlimited / negotiated digital employees。
- AI asset registry and lifecycle governance。
- self-hosted / private deployment。
- RLS / advanced policy。
- advanced audit。
- SSO / SCIM。
- premium connectors。
- SLA / support。

### 8.3 Unit Economics 假设

需要重点验证：

1. Inference COGS
   - 每个 completed task 的 token cost。
   - 不同模型 tier 的成本。
   - prompt cache / context management 的节省。

2. Sandbox / execution COGS
   - code execution、browser、document processing、web crawl。

3. Support COGS
   - early product 需要多少人工 onboarding / workflow design。

4. Gross margin target
   - SaaS 长期目标应接近 70%+。
   - AI-heavy workflow 初期可能较低，需要通过 usage pricing、model routing、cache、packaging 改善。

5. Expansion economics
   - 如果一个客户从 1 个 workflow 扩到 5 个 workflow，边际交付成本是否下降。

### 8.4 Financial Model 初稿框架

正式表格应包括：

- Pipeline
  - discovery calls
  - pilots
  - pilot conversion
  - annual contracts

- Revenue
  - pilot revenue
  - subscription ARR
  - enterprise license
  - services

- COGS
  - inference
  - sandbox / infra
  - crawling / API
  - support

- Operating expense
  - engineering
  - product/design
  - GTM
  - support
  - cloud / tools / legal

- SaaS metrics
  - ARR
  - gross margin
  - CAC payback
  - logo retention
  - NDR
  - burn multiple
  - runway

---

## 9. Competition

### 9.1 Competitive Landscape

| 类别 | 代表 | 客户为什么买 | Hiveclaw 差异 |
|---|---|---|---|
| Chatbot / Copilot | ChatGPT Enterprise, Claude, Microsoft Copilot | 快速提升个人生产力 | Hiveclaw 管理公司级 digital employees，不只是个人助手 |
| Agent frameworks | LangGraph, CrewAI, AutoGen | 开发者构建 agent flow | Hiveclaw 运营、治理、审计、记忆和生命周期 |
| RPA / Automation | UiPath, Zapier, Make | 自动化确定流程 | Hiveclaw 面向 LLM-native judgment + governed autonomy |
| Enterprise search | Glean, Coveo | 找公司知识 | Hiveclaw 让 agent 用知识执行任务并留下证据 |
| Vertical agents | Sierra, Decagon, Harvey, Abridge 等 | 单行业 / 单岗位结果 | Hiveclaw 是多岗位 digital workforce control plane |
| IM / OA / Office ecosystems | 飞书、钉钉、Slack、Teams、企业微信、Microsoft 365、Google Workspace | 团队沟通、办公协作、文档流转 | Hiveclaw 不替代这些入口，而是让数字员工在这些入口中被统一治理、记忆、审批和审计 |
| Internal platform | 企业自建 | 完全定制和内部控制 | Hiveclaw 降低 time-to-production 和维护成本 |

### 9.2 为什么 Hiveclaw 可以赢

1. Category position
   - 不和 ChatGPT 比“谁更会回答”，而是定义 digital employee control plane。

2. Model-neutral
   - 企业不想把全部组织运行绑定到单一模型供应商。

3. Ecosystem-neutral
   - 企业也不想把 AI worker 的身份、记忆、权限和审计锁进单一 IM、OA、云或办公生态。Hiveclaw 的位置是横跨这些入口的第三方控制面。

4. Self-hosted / private deployment path
   - 对金融、咨询、投研和其他高敏感专业服务客户有吸引力，因为组织上下文、工作证据、记忆和审计可以留在企业控制的基础设施内。

5. Governance-first architecture
   - 从 identity、permission、audit、memory gate、platform gate 出发，不是后补安全层。

6. Self-evolution loop
   - 数字员工不是静态 prompt，而是能从工作经验中积累 memory 和 skill。

7. Open-source credibility
   - 有利于技术 buyer 试用、审计、贡献和信任建立。

### 9.3 竞争风险

| 风险 | 说明 | 应对 |
|---|---|---|
| Model vendors 下压 | OpenAI / Anthropic / Microsoft 会提供更多 agent 管理功能 | 强调 model-neutral、self-hosted、cross-model、enterprise control |
| IM / OA 生态上移 | 飞书、钉钉、Slack、Teams 可能内置更多 AI agent 能力 | 明确互补关系：它们是入口和渠道，Hiveclaw 是跨入口的数字员工治理、记忆、权限和审计层 |
| Framework 上移 | LangGraph 等可能增加部署和管理层 | 聚焦非开发者企业运营场景和 governance UX |
| Vertical agents 占据预算 | 客户可能直接买某个岗位 agent | Hiveclaw 从多岗位、可治理、可扩展 control plane 切入 |
| SI / agency 定制 | 客户找顾问自建 | 把 agency 变成 partner，用 Hiveclaw 做交付底座 |

---

## 10. Traction / Evidence

### 10.1 当前可讲的产品证据

基于当前 repo 文档，Hiveclaw 已经具备完整产品方向和技术底座：

- AI Native 组织中台定位。
- Digital Employee identity / memory / workspace / skill / runtime。
- ChatSession + RuntimeTask durable runtime。
- ToolRuntimeService 统一工具治理。
- Memory T0/T2/T3/soul 演化路径。
- Enterprise capability policy / approval / audit。
- Workflow、Subagent、Agent Team、channel integration。
- Self-hosted architecture。
- Channel-complementary positioning：README 已把渠道消息纳入统一 runtime loop，前端已有 Channels & Integrations 和多 IM 渠道配置文案；Pitch 应讲成“把数字员工带进既有协作入口”，而不是替代 IM。
- Enterprise context control path：self-hosted/private deployment 叙事可基于 README 的“可自部署组织中台”定位，但对外仍需区分控制面上下文留在企业内与模型 provider 调用边界。

### 10.2 不能伪造的 traction

对外材料目前不应声称，除非补证：

- 真实 ARR。
- 付费客户数量。
- design partner 数量。
- production deployment 数量。
- retention / NDR。
- ROI case study。
- GitHub star / community growth，除非实时查证。

### 10.3 应尽快补的 traction artifact

1. Product demo video
   - 5 分钟 Research Digital Employee。

2. Design partner pipeline
   - 20 个 target accounts。
   - 10 个 discovery calls。
   - 5 个 pilot proposals。
   - 2 个 paid pilots。

3. ROI case study
   - before：分析师/顾问每周花 10-20 小时整理行业变化、公司动态、会议纪要、竞品资料或项目背景。
   - after：数字员工持续维护 watchlist、生成 research brief / memo support，人类专家 review、补判断、做最终结论。
   - 结果：专家注意力从资料搬运转向判断、客户沟通和投资/业务决策支持。

4. Technical proof
   - self-hosted deployment。
   - audit trail。
   - permission policy。
   - memory improvement example。

5. Community proof
   - GitHub stars / issues / forks。
   - developer signups。
   - content subscribers。

---

## 11. Team

### 11.1 团队叙事应该怎么写

VC 关心的不是 title，而是 founder-market fit：

- 为什么这个团队懂 agent runtime？
- 为什么这个团队懂 enterprise governance？
- 为什么这个团队能把复杂基础设施做成产品？
- 为什么这个团队能拿到 design partners？

### 11.2 待补充内容

这里需要创始团队提供真实材料：

- Founder / CEO 背景。
- CTO / engineering 背景。
- AI infra / agent / SaaS / enterprise / security 相关经历。
- 已完成产品 milestone。
- 顾问 / angel / design partner。
- 招聘计划。

### 11.3 招聘优先级建议

如果融资，建议前 12-18 个月优先招聘：

1. Founding product engineer
   - 前后端 + AI workflow + product sense。

2. Agent runtime / infra engineer
   - runtime、sandbox、observability、distributed systems。

3. Enterprise full-stack engineer
   - auth、RLS、admin surfaces、connectors、audit。

4. Design engineer / product designer
   - 把复杂 control plane 做成可用产品。

5. GTM founder / founding AE later
   - 在 design partner motion 验证后再规模化。

---

## 12. Fundraising Plan

### 12.1 融资阶段建议

如果当前还没有显著 ARR，建议定位为 pre-seed / seed：

- Pre-seed：卖 founder vision + working product + early design partner pipeline。
- Seed：需要更强 traction，如 paid pilots、repeat usage、early ARR、clear ICP。

### 12.2 融资金额假设

待创始团队确认。可讨论三个版本：

| 版本 | 金额 | Runway | 目标 |
|---|---:|---:|---|
| Lean pre-seed | $1M-$1.5M | 12-18 个月 | 完成 5-10 paid pilots，形成 repeatable demo 和 1-2 vertical workflows |
| Standard seed | $2M-$4M | 18-24 个月 | 达到 $500k-$1.5M ARR，证明 land-and-expand |
| Ambitious seed | $5M-$8M | 24 个月 | 组建完整 product + GTM，冲 enterprise self-hosted category |

### 12.3 Use of Funds

建议资金用途：

1. Product and engineering: 55%-65%
   - runtime stability
   - enterprise governance
   - deployment / self-hosted
   - connectors
   - product UX

2. GTM and customer success: 20%-30%
   - design partner pilots
   - sales materials
   - customer onboarding
   - partner enablement

3. Infrastructure and security: 10%-15%
   - hosting
   - observability
   - security review
   - compliance readiness

4. Operations / legal / admin: 5%-10%

### 12.4 Milestones to Next Round

下一轮融资前建议达成：

- 10+ paid pilots。
- 5+ converted annual customers。
- 2+ repeatable packaged workflows。
- $500k+ ARR 或明确接近该水平的 signed pipeline。
- 3+ customer case studies。
- Gross margin model clear。
- Self-hosted / enterprise security story credible。
- Usage metric shows workflow retention。

---

## 13. Risks and Mitigations

### 13.1 Product risk: platform too broad

风险：Hiveclaw 能力很多，客户不知道先买什么。

应对：

- 用一个 flagship workflow 切入。
- 对外只讲“一个数字员工解决一个高摩擦工作流”。
- 平台能力作为 expand 和 defensibility，而不是第一句话。

### 13.2 Market risk: enterprises remain in pilot mode

风险：客户愿意试用但不愿意付费或上线生产。

应对：

- paid pilot，避免免费 POC。
- 每个 pilot 绑定 ROI baseline。
- 选择低风险、高频、可验收 workflow。

### 13.3 Competitive risk: model vendors build control plane

风险：OpenAI、Anthropic、Microsoft 增加 agent 管理功能。

应对：

- model-neutral。
- self-hosted / private deployment。
- cross-tool / cross-model / cross-agent governance。
- enterprise memory and audit independence。

### 13.4 Technical risk: AI output quality unstable

风险：agent 结果不稳定，客户难以信任。

应对：

- 选择 review-friendly workflows。
- 保留 human-in-the-loop。
- 强化 evidence、citation、audit、verification。
- 用 memory / skill evolution 提升重复任务表现。

### 13.5 Unit economics risk

风险：推理、sandbox、web crawl、support 成本吃掉毛利。

应对：

- usage-based pricing。
- model routing。
- prompt cache。
- workflow packaging。
- paid onboarding。
- 限制低价值高成本任务。

### 13.6 Security / governance risk

风险：agent 越权、泄露、误操作会严重损害信任。

应对：

- permission-first architecture。
- ActionPreflight。
- approval。
- RLS。
- MCP authz。
- audit trail。
- fail-closed defaults。

---

## 14. Suggested Pitch Deck Outline

这份 long-form 后续可以拆成 12 页 deck：

1. Title
   - Hiveclaw: AI-native control plane for enterprise digital employees.

2. Problem
   - Enterprises have AI pilots, not governed AI workers or manageable AI assets.

3. Why Now
   - AI can act; agents are becoming enterprise assets; companies now need control, identity, audit, memory, and lifecycle management.

4. Solution
   - Digital Employee Runtime + Enterprise Control Plane + Self-Evolution + AI Asset Management.
   - Independent third-party control layer: model-neutral, channel-complementary, enterprise-deployable.

5. Product Demo
   - Research Digital Employee workflow.

6. Customer
   - AI-forward professional knowledge organizations: consulting, investment research, VC/PE, securities research, asset management, and strategy teams.

7. Market
   - Enterprise AI apps + agentic AI + new control plane category.

8. GTM
   - Open-source trust, design partners, paid pilots, one-click enterprise deployment package, land-and-expand.

9. Business Model
   - pilot + subscription + self-hosted enterprise.

10. Competition
   - why not chatbot/framework/RPA/search/vertical agent/IM ecosystem.

11. Traction / Roadmap
   - current product evidence, pilots, milestones. Traction facts must be filled with real data.

12. Team / Ask
   - team, financing, use of funds, next-round milestones.

---

## 15. Customer Discovery Plan

### 15.1 目标访谈对象

- 10 位 CEO / COO / founder。
- 10 位 CTO / Head of AI / IT lead。
- 10 位 professional workflow owners：VC/PE investor、券商/资管 research lead、consulting partner / engagement manager、corporate strategy lead。
- 10 位 functional leads：Ops、Research、Product、Engineering。
- 5 位 AI consultants / automation agency founders。

### 15.2 访谈问题

1. 你们现在在哪些工作里使用 AI？
2. 哪些 AI 尝试已经进入生产？哪些还停在 pilot？
3. 你最希望交给 AI worker 的重复研究、资料整理、跟踪或 memo support 工作是什么？
4. 这个工作现在每周消耗多少专家/分析师时间？
5. 如果 AI 完成 70% 初稿，人类 review，你是否愿意使用？
6. 你最担心 AI worker 做错什么？
7. 权限、审计、审批、数据隔离对你有多重要？
8. 你更愿意买 SaaS、self-hosted，还是一次 paid pilot？
9. 一个 workflow 如果每月节省 40-100 小时，你愿意支付多少钱？
10. 谁会批准采购？预算来自哪里？
11. 你会用什么指标判断 pilot 成功？
12. 如果成功，你会扩展到哪些下一个 workflow？
13. 对金融/投研客户：哪些环节可以接受 AI 支持，哪些环节必须明确保留人工最终判断？
14. 对咨询客户：哪些资料和项目知识最难复用，哪些交付物最适合作为 pilot？
15. 对 IT / 安全 / 合规 buyer：哪些上下文必须留在企业控制环境内？你们能接受企业批准的外部模型 API，还是必须接私有化 / 自托管模型？
16. 对现有 IM / OA 重度用户：你希望 AI worker 出现在飞书、钉钉、Slack、Teams、企业微信里，还是希望进入一个新的工作入口？为什么？

### 15.3 验证目标

必须验证：

- 客户是否真的想要“数字员工”，还是只想要 ChatGPT prompt。
- 谁为 digital employee control plane 付费。
- 哪个 workflow 付费意愿最强。
- 客户是否接受 paid pilot。
- 客户对 self-hosted / cloud 的偏好。
- ROI 是否可量化。
- 金融/咨询客户是否认可“expert leverage / research operations”叙事，而不是误解为“替代专家”。
- 客户是否把 self-hosted/private deployment 和企业上下文控制当成购买门槛、加分项，还是只在后期 procurement 关心。
- 客户是否接受“Hiveclaw 与现有 IM / OA / Office 互补”的叙事，还是更想要一个完整替换式工作台。

---

## 16. Operating Plan

### 16.1 0-3 个月

目标：形成可讲、可演示、可销售的 wedge。

动作：

- 完成 Investment / Market Intelligence Digital Employee demo path，并基于当前真实 product surface 展示：Control Plane、HR Agent creation、Digital Employees directory、Agent Workbench、Enterprise KB、Approval/Audit。
- 做 1 页 executive narrative。
- 做 12 页 pitch deck。
- 做 security / deployment one-pager：说明 self-hosted/private deployment、企业上下文控制、模型 provider 策略、与飞书/钉钉/Slack/Teams 等渠道的互补关系。
- 做 customer discovery。
- 找 5 个 design partner。
- 定义 pilot contract 和 success metrics。

### 16.2 3-6 个月

目标：从 demo 到 paid pilot。

动作：

- 签 2-5 个 paid pilots。
- 每个 pilot 有 baseline / after metrics。
- 修 product onboarding。
- 固化 admin / audit / approval / memory proof。
- 固化 one-click enterprise deployment package 的第一版交付形态。
- 输出 1-2 个 case studies。

### 16.3 6-12 个月

目标：形成 repeatable motion。

动作：

- 10+ pilots。
- 5+ annual conversions。
- 2 packaged workflows：Investment / Market Intelligence、Consulting / Client Research。
- self-hosted / cloud packaging 清楚。
- partner channel 初步启动。

### 16.4 12-24 个月

目标：从 workflow product 扩成 control plane。

动作：

- 多 digital employee。
- 跨部门 governance。
- enterprise connectors。
- advanced policy / audit。
- workflow marketplace / templates。
- agency / SI partner motion。

---

## 17. Discussion Questions

下一轮建议重点讨论这些决策：

1. 我们第一版到底锁哪一个 beachhead ICP？
   - VC / PE / 投研团队？
   - 券商研究 / 资管研究？
   - 咨询 / 专业服务？
   - B2B SaaS 战略/运营团队？
   - AI automation agencies？

2. 第一条 flagship workflow 是否就是 Investment / Market Intelligence Digital Employee？
   - 如果不是，咨询类 Client Research / Proposal Support 是否更强？
   - 需要避免客户误解成“自动投资决策”还是“替代分析师”。

3. 我们融资阶段怎么定位？
   - pre-seed vision + product？
   - seed with early pilots？

4. 公开叙事用 Hive 还是 Hiveclaw？
   - repo / README 当前主要使用 Hive；对外品牌需要统一。

5. 开源策略怎么讲？
   - open-core？
   - self-hosted community + paid enterprise？
   - managed cloud？

6. 第一版 pricing 怎么设？
   - pilot fee？
   - per digital employee？
   - platform fee？
   - usage pass-through？

7. 我们有哪些真实 traction 可以写？
   - 用户、试用、部署、开源、社区、客户访谈、收入。

8. 需要补哪些 VC 级 demo 截图或视频？
   - Control Plane overview。
   - HR Agent 创建员工。
   - Digital Employees 目录。
   - Agent Workbench 的 Chat / Knowledge / Workflows / Workspace / Approvals / Activity。
   - Enterprise Knowledge Base 上传研究资料。
   - Approval / Audit 证明治理闭环。

9. 是否需要把产品从“平台”叙事压成“one killer workflow”？

10. 哪些能力必须先藏在 appendix，不要放主 deck？

11. “一键部署到企业环境”第一版到底承诺到什么形态？
   - Docker Compose？
   - Railway / managed private environment？
   - Customer VPC？
   - Kubernetes / Helm？
   - On-prem appliance？

12. “上下文数据留在企业内部”对外怎么精确定义？
   - transcript / workspace / memory / audit / policy 一定留在客户控制环境？
   - 模型调用由客户选择 provider？
   - 是否需要支持私有模型作为金融/咨询客户的 enterprise package？

13. 我们和飞书、钉钉、Slack、Teams、企业微信的关系是否统一表述为“channel / context surface”，而不是“竞争协作入口”？

---

## 18. Source Notes

本稿使用的外部资料方向：

- SBA business plan guidance：business plan 是融资与经营路线图，常见结构包括 executive summary、company description、market analysis、organization、product/service、marketing/sales、funding request、financial projections、appendix。
- Sequoia business plan guidance：VC pitch 应清楚回答 company purpose、problem、solution、why now、market potential、competition、business model、team、financials、vision。
- Headline AI startup pitch guidance：AI apps 需要强调 usage metrics、ICP、sponsor vs end user、ROI、GTM、partnership、community、ARR/NDR。
- Menlo Ventures 2025 enterprise generative AI report：企业 generative AI spend 快速增长，应用层占重要份额。
- McKinsey 2025 State of AI：AI 使用广泛，但从 pilot 到 scale 仍是主要挑战，workflow redesign 是 high performers 的关键。
- PwC AI agent survey：agentic AI 带来预算增长和生产力价值，但组织、workflow、trust 是核心问题。
- OpenAI State of Enterprise AI 2025：企业 AI 使用正在进入 repeatable multi-step workflows。
- Forrester / ITPro agentic AI operationalization coverage：agent 不能被当作 chatbot，需要 orchestration、control、trust、governance 和 nonhuman identity。
- MarketsandMarkets Agentic AI market report：agentic AI 市场高速增长，适合作为方向性市场参考，但正式融资材料应再次核验定义与数据口径。

---

## 19. Appendix: One-page Narrative Draft

Hiveclaw is building the AI-native organization control plane for enterprise digital employees.

AI adoption inside companies has exploded, but most enterprise AI remains trapped in personal copilots, demos, and isolated workflow experiments. The bottleneck is no longer whether models can answer questions. The bottleneck is whether companies can safely let AI workers execute real work: with identity, permissions, tools, memory, approvals, audit trails, and continuous improvement.

In the agent era, the most important new enterprise asset is the agent itself. A mature AI worker is not just a prompt; it is a compounding asset made of role context, permissions, tools, workflows, memory, skills, work products, audit evidence, and collaboration history. Hiveclaw is both the platform that creates these AI assets and the system of record that manages them.

Hiveclaw turns agents into governed organizational actors. Each digital employee has a durable runtime, private workspace, long-term memory, installed skills, tool permissions, session history, and audit evidence. Every action flows through an enterprise control plane: capability policy, approval, preflight, tenant isolation, and observability. Every completed task can feed a governed learning loop, so the worker becomes more useful over time without bypassing safety or compliance.

Hiveclaw is independent infrastructure, not a model-vendor dashboard or an IM replacement. It is model-neutral and channel-complementary: Feishu, DingTalk, Slack, Teams, WeCom, email, files, and knowledge systems are the places where work already happens. Hiveclaw brings governed AI workers into those existing environments instead of asking companies to migrate into a closed ecosystem.

For enterprise customers, deployment and context control are part of the product. Hiveclaw can be self-hosted or deployed into a customer-controlled environment so operational context, workspace files, transcripts, memory, approvals, policies, and audit evidence stay under enterprise control. Model routing remains configurable based on the customer's provider and private-model strategy.

We are starting with AI-forward professional knowledge organizations: consulting, investment research, VC/PE, securities research, asset management, and strategy teams that already use AI but cannot operationalize agentic workflows. Our initial wedge is an Investment / Market Intelligence Digital Employee: a governed AI worker that monitors markets, companies, competitors, customers, and internal knowledge, produces recurring research artifacts, and compounds organizational context week after week.

The long-term vision is a digital workforce operating system: every company can create, govern, coordinate, and improve AI workers the same way it manages human teams, but with software-native memory, audit, permissions, and automation.
