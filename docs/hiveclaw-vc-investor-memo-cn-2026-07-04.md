# Hiveclaw 投资人备忘录

> 日期：2026 年 7 月 4 日  
> 用途：正式投资人材料

---

## 1. 执行摘要

Hiveclaw 正在构建面向企业数字员工的 AI-native 组织控制平面。

在 AI agent 时代，agent 本身会成为企业最重要的新型资产之一。一个成熟的 AI worker 不只是 prompt 或 workflow，而是一组持续积累的企业 AI 资产：角色上下文、权限、工具、工作流、记忆、技能、工作产出、审计证据和协作历史。Hiveclaw 既是产生这些 AI 资产的平台，也是管理这些 AI 资产的系统。

Hiveclaw 让企业能够像管理真实员工一样，创建、授权、运行、观测和持续改进 AI 数字员工。每个数字员工都有身份、记忆、工具、工作流、权限、审批、审计和受治理的自进化路径。

Hiveclaw 的定位是独立第三方企业基础设施。它不绑定任何单一模型厂商、云平台、IM/OA 生态或办公套件。飞书、钉钉、Slack、Teams、企业微信、邮件、办公文件和知识系统是企业工作已经发生的入口。Hiveclaw 不替代这些入口，而是在其上提供统一的数字员工身份、权限、记忆、工具、审批、审计和生命周期管理。

Hiveclaw 的初始切入点是高信息密度、高判断密度的专业知识工作，包括咨询、投研、VC/PE、券商研究、资产管理和企业战略团队。这类组织已经在使用 AI，但难以把 AI agent 真正放入生产，因为身份、权限、记忆、上下文、审批、审计和数据边界仍未被系统性解决。

Hiveclaw 的第一条 flagship workflow 是 Investment / Market Intelligence Digital Employee：一个受治理的 AI 研究员工，持续跟踪公司、市场、客户、竞品、内部知识和周期性研究需求，生成可复用、可追溯、可审阅的研究交付物，同时保留人类专家的最终判断。

商业模式包括：

- 初始部署费用，用于企业环境部署、基础配置和首批数字员工上线。
- 持续维护 / 平台费用，对应 SaaS-style subscription、support、upgrade、monitoring 和治理能力。
- Token / 模型调用费用，通过 Hiveclaw 作为统一充值、计量、采购和消耗入口，并保留一定溢价空间。
- 定制化服务费用，包括定制 Digital Employee、定制 workflow、非标准知识库 / 数据库接入、Embedding 策略优化和独特第三方数据 API 集成。
- 授信插件市场收入，面向经过验证的插件、工具、行业能力包和数据连接器进行一次性收费、订阅收费或分成。

---

## 2. 市场时机

企业 AI adoption 已经越过认知阶段，但 production-grade agentic work 仍处在早期。

Menlo Ventures 估计 2025 年企业 generative AI spend 达到 370 亿美元，其中 190 亿美元流向应用层。这说明企业 AI 预算正在从模型访问转向直接产生 workflow value 的软件。McKinsey 2025 年 State of AI 调研显示，88% 的组织已经在至少一个业务函数中常规使用 AI，但多数企业仍处于 experimentation 或 pilot 阶段。PwC 2025 年 5 月 AI Agent Survey 显示，88% 的高管计划因为 agentic AI 增加 AI 相关预算。OpenAI 2025 年企业 AI 报告也指出，企业 AI 使用正在从简单提问转向 repeatable multi-step workflows。

这代表一个明确变化：企业 AI 的瓶颈已经不再是模型能不能回答问题，而是企业能不能安全地让 AI worker 执行真实工作。这个问题涉及身份、上下文、权限、记忆、工具、审批和审计。

Hiveclaw 对应的是一个新的企业软件品类：AI worker control plane。

---

## 3. 核心问题

企业正在部署大量 AI 工具，但绝大多数 AI 使用仍分散在个人 copilot、prompt collection、内部 demo 和局部 automation 里。

这造成四个结构性问题。

### 3.1 AI worker 缺少企业级身份

当 AI agent 执行动作时，企业必须知道它代表谁、服务哪个团队、拥有什么权限，以及哪个用户或 owner 对结果负责。大多数 AI 工具没有为非人类 worker 提供稳定的组织身份模型。

### 3.2 上下文和记忆没有成为受治理资产

知识工作者不断向 chatbot 重复解释背景。内部文档、研究结论、反馈和历史工作很难自动沉淀为组织记忆。大量 AI 生成的上下文停留在个人聊天、局部文件或一次性 workflow 中，无法成为公司可复用资产。

### 3.3 自主执行带来治理风险

AI agent 可以搜索、写作、调用工具、更新系统、触发工作流和对外沟通。没有权限、preflight、审批、审计和 rollback 边界，企业无法放心让 agent 进入生产环境。

### 3.4 Agent 资产无法被统一管理

当企业创建越来越多 AI worker 后，需要知道：

- 公司有哪些 agent。
- 每个 agent 归谁所有。
- 它能访问哪些数据、工具和渠道。
- 它做过哪些工作。
- 它积累了哪些记忆和技能。
- 哪些 workflow 依赖它。
- 它应该被改进、限制、归档还是退役。

这不是聊天界面问题，而是企业 AI 资产管理问题。

---

## 4. 现有方案的缺口

| 类别 | 解决的问题 | 仍然缺失的部分 |
|---|---|---|
| Chatbot / Copilot | 个人生产力和交互式辅助 | 企业身份、持久运行时、团队记忆、审批、审计、资产生命周期 |
| Agent framework | 开发者构建 agent flow | 组织级运营、治理、观测、部署和持续改进 |
| RPA / automation | 确定性流程自动化 | LLM-native 判断、上下文组装、记忆、工具推理、受治理自主性 |
| Enterprise search | 查找内部知识 | 用知识执行任务、生成交付物、保存证据、改进 workflow |
| Vertical AI agent | 单岗位或单用例结果 | 多岗位 digital workforce 管理和公司级治理 |
| IM / OA / Office 生态 | 团队沟通、办公协作、文档流转 | 跨渠道 AI worker 身份、治理、记忆、生命周期和中立控制层 |
| 企业自建平台 | 完全定制 | 高工程成本、长上线周期和持续维护负担 |

Hiveclaw 位于这些类别之上，是企业 AI worker 的中立控制平面。

---

## 5. 产品

Hiveclaw 将 agent 变成受治理的组织行动者。

每个数字员工都具备：

- 身份：角色、owner、公司上下文、职责边界。
- 权限：工具、文件、知识、渠道和外部动作。
- 运行时：持久 session、task、checkpoint、workflow execution、restart / resume。
- 记忆：原始证据、reviewed memory、accepted semantic knowledge、role evolution。
- 技能：可通过证据和验证持续改进的 capability package。
- 工作区：文件、artifact、报告和交付物。
- 治理：policy、approval、preflight、quota、audit log。
- 协作：subagent、workflow、team delegation、channel integration。
- 生命周期：创建、分配、观测、改进、限制和退役。

### 5.1 AI 资产生成

Hiveclaw 通过受治理的 employee-creation flow 创建数字员工。用户不是简单填写一个 prompt，而是明确 role、authority、memory boundary、tool access、first workflow 和 accountability。最终产物是一个可管理的企业 AI 资产，而不是一次性 assistant。

### 5.2 AI 资产管理

Hiveclaw 为企业提供 AI asset registry。数字员工可以被搜索、筛选、分配、审阅、共享、监控和持续改进。每个 agent 都有自己的 workbench，包含 chat、memory、skills、workflows、workspace、approvals、activity、settings 和 collaboration surfaces。

### 5.3 企业控制平面

Hiveclaw 的 control plane 管理：

- 组织与 tenant context。
- 模型与预算。
- 工具与 capability policy。
- 记忆治理。
- 渠道与集成。
- 审批与审计。
- 团队委托。
- 本地与 hybrid agent runtime。

企业因此可以把 AI worker 当作组织基础设施管理，而不是让 AI 工具散落在个人账号和局部流程中。

### 5.4 Learning Vault 与自进化

Hiveclaw 将原始证据、已审阅记忆和可持久改进的技能分开管理。

学习路径如下：

```text
T0 raw session evidence
  -> reviewed memory segment packages
  -> accepted semantic memory
  -> role, skill, and workflow improvement
```

记忆和技能变更都受治理。模型负责产生洞察和改进候选，平台负责证据引用、权限、去重、回滚和审计。

### 5.5 中立部署与渠道策略

Hiveclaw 不绑定单一模型 provider、云平台、IM 系统或办公生态。

Hiveclaw 可以部署到客户控制的环境中，使 operational context、workspace files、transcripts、memory、approvals、policies 和 audit evidence 留在企业控制面内。模型路由由客户的 provider 策略决定，可以接企业批准的外部模型 API，也可以接私有化或自托管模型。

飞书、钉钉、Slack、Teams、企业微信、邮件、办公文件和知识系统是互补渠道。Hiveclaw 将受治理的 AI worker 带入这些现有工作入口，而不是要求客户迁移到新的封闭协作系统。

---

## 6. 初始切入场景

### Investment / Market Intelligence Digital Employee

Hiveclaw 的第一条 wedge 是面向专业研究和市场情报工作的受治理 AI worker。

目标用户包括：

- VC 和 PE 投资人。
- 券商研究和资产管理团队。
- 咨询和专业服务团队。
- 企业战略和 B2B SaaS 市场团队。
- AI-forward 的研究密集型组织。

该数字员工可以：

- 跟踪公司、市场、竞品、客户、产品和新闻。
- 持续维护 watchlist 和研究上下文。
- 使用内部知识和外部研究来源。
- 生成周期性 research brief、market map、memo support 和 intelligence summary。
- 保留来源证据和工作历史。
- 将敏感或外部可见动作交给审批流。
- 持续沉淀团队上下文。

其价值不是替代专家，而是放大专家判断。人类专业人士保留最终判断；Hiveclaw 将资料收集、信息综合、上下文回忆、初稿准备和周期性报告压缩为一个可复用、可审计、可治理的 AI workflow。

---

## 7. 目标客户

Hiveclaw 的 beachhead customer 是 30-500 人规模的 AI-forward professional knowledge organizations。

这些组织有三个共同点：

- 信息密度高。
- 判断密度高。
- 专家注意力价值高。

它们已经足够复杂，能感知 AI 治理和知识复用痛点；同时通常没有资源从零自建完整 agent control plane。

| 客户类型 | 核心痛点 | 第一条 workflow |
|---|---|---|
| 咨询 / 专业服务 | 重复客户研究、proposal、项目知识复用 | Client research、proposal support、industry monitoring |
| VC / PE | Deal tracking、thesis、company watchlist、market map | Company tracking、market intelligence、memo support |
| 券商研究 / 资产管理 | 高频信息监控和来源追溯 | Sector monitor、issuer tracker、weekly brief |
| B2B SaaS 战略 / 运营 | 竞品、客户、市场和产品情报 | Competitor monitoring、customer insight summary |
| AI agency / automation firm | 需要可复用的 AI worker 交付底座 | Governed AI employee deployment platform |
| 研发和产品团队 | 内部知识、issue、docs、release 和技术上下文 | Engineering research、issue triage、release assistant |

---

## 8. Go-to-Market

Hiveclaw 的 go-to-market 从企业部署和首个高价值数字员工场景开始，并扩展为完整的 AI asset management platform。

### 8.1 Land

第一笔购买包含基础部署费用、持续平台 / 维护费用，以及首个受治理数字员工场景。客户不需要在第一天采购完整公司级 AI operating system。第一个 deployment 通过具体业务场景证明可衡量价值。

Initial deployment package：

- 一个 digital employee。
- 一个团队。
- 一个可重复业务 workflow。
- 四到八周。
- 明确的 baseline 和 post-deployment measurement。

核心 pilot 指标：

- 节省的研究或 workflow 小时数。
- 完成的周期性任务数。
- 输出质量 review score。
- Cycle-time reduction。
- Source traceability。
- Repeat usage。
- 是否扩展到第二条 workflow。

### 8.2 Expand

第一个 workflow 证明价值后，Hiveclaw 可以扩展到：

- 更多 digital employees。
- 更多 governed workflows。
- 更多 teams。
- 更多 tools and channels。
- 更多 memory and knowledge assets。
- 更深的 approvals、audit 和 policy controls。

这将单一 workflow 采购扩展成企业 AI worker control plane。

### 8.3 Distribution

主要获客渠道包括：

- Founder-led sales：面向 AI-forward operator、CTO、COO 和 research leader。
- Expert-workflow outbound：面向咨询、VC/PE、券商研究、资产管理和战略团队。
- Open-source trust 和技术内容：建立 developer 与 AI infrastructure credibility。
- AI consultants、automation agencies 和 SIs：作为 implementation partners。
- IM / OA / cloud ecosystem partners：作为互补的工作入口。

---

## 9. 商业模式

Hiveclaw 的收入由六类组成：部署费用、持续维护 / 平台费用、Token / 模型调用费用、定制化服务费用、插件市场收入和数据 API 集成 / 分发收入。

### 9.1 部署费用

企业部署阶段收取一次性基础部署费用。该费用覆盖环境部署、基础系统配置、企业组织 / 权限初始化、模型与预算配置、首批数字员工上线、基础知识库接入、审批与审计配置，以及管理员培训。

部署费用可以按部署复杂度分层：

- Standard deployment：适用于标准 cloud 或标准 self-hosted 环境。
- Enterprise deployment：适用于客户 VPC、私有云或更复杂权限 / 审计要求。
- Compliance-heavy deployment：适用于金融、咨询、大型企业等需要更强安全、合规和内部系统接入的客户。

### 9.2 持续维护 / 平台费用

部署后收取持续维护和平台费用，形态与 SaaS subscription 类似。该费用覆盖平台使用权、版本升级、基础运维、监控、权限治理、审计、知识库管理、数字员工资产管理和 support。

计费维度可以包括：

- Platform fee。
- Digital employee fee。
- AI asset management fee。
- Governed workflow fee。
- Memory / knowledge volume。
- Audit retention。
- Support / SLA tier。

### 9.3 Token / 模型调用费用

Token 和模型调用费用统一走 Hiveclaw。企业可以向 Hiveclaw 充值，由 Hiveclaw 作为统一采购、计量和结算入口。底层模型调用可以根据客户要求采用平台中转、企业授权直连或私有模型接入。

这一层收入包括：

- Token consumption pass-through。
- 模型路由和统一计量服务费。
- Bulk purchase / volume discount 的价差空间。
- 高级模型、私有模型、行业模型或专用推理资源的管理费用。

Hiveclaw 在这一层的价值不是单纯转售 token，而是为企业提供统一的模型消费入口、预算控制、权限策略、审计记录和成本可视化。

### 9.4 定制化服务费用

定制化服务包括定制 Digital Employee 和定制 workflow。

这里的“定制 workflow”指的是把某个可重复业务流程配置成可运行、可审计、可复用的数字员工工作包，而不是简单写一个 prompt。典型内容包括：

- 定义触发方式：手动、定时、渠道消息、文件上传、数据库变化或外部事件。
- 定义输入来源：企业知识库、文档、数据库、第三方数据 API、IM 渠道或内部系统。
- 定义执行步骤：检索、分析、工具调用、人工审批、报告生成、文件写入、消息发送。
- 定义权限边界：能看哪些资料、能调用哪些工具、哪些动作需要审批。
- 定义输出物：research brief、market map、memo support、客户报告、风险提醒、周报、工作单或内部知识更新。
- 定义审计与复盘：记录过程、来源、审批、产出、反馈和可复用经验。

定制 Digital Employee 则包括角色设定、职责边界、权限配置、工具包、记忆边界、工作台配置、初始知识注入和团队协作关系。

### 9.5 授信插件市场

Hiveclaw 可以上线授信插件市场，提供经过验证的工具、行业能力包、连接器、workflow template 和数字员工模板。

插件市场可以采用以下收费方式：

- 一次性购买。
- 订阅收费。
- Usage-based pricing。
- 与第三方开发者或数据提供方 revenue share。

插件市场的价值在于把 Hiveclaw 从单一平台扩展为企业 AI worker 的能力分发网络。企业可以按需购买经过验证的能力，而不是每次都做定制开发。

### 9.6 定制化部署服务

对复杂企业客户，Hiveclaw 可以围绕非标准数据和知识系统收取定制化部署服务费。

典型收费点包括：

- 非标准知识库、异步知识库、文档系统或数据库的接入。
- 数据清洗、权限映射、同步策略和索引策略。
- Embedding 向量化策略的定制优化。
- RAG / retrieval pipeline 的企业场景调优。
- 私有模型、私有向量库或私有云环境适配。
- 与客户内部审批、OA、CRM、研究系统、数据仓库或权限系统的集成。

### 9.7 独特数据 API 集成与分发

Hiveclaw 可以集成金融、行业、商业信息、企业数据库、市场数据等第三方数据 API，并通过集采或平台合作方式获得数据能力，再分发给具体企业或个人用户。

这一层可以形成数据增值收入：

- 数据 API access fee。
- 数据使用量加价。
- 行业数据包订阅。
- 特定 workflow 中的数据能力附加费。
- 与数据 provider 的 revenue share。

金融、投研、咨询和行业研究客户对高质量外部数据有明确需求。Hiveclaw 可以把这些数据 API 接入数字员工工作流，让客户直接在 AI worker 中使用，而不需要单独采购、集成和治理每个数据源。

### 9.8 Self-Hosted Enterprise License

对数据敏感和合规要求高的客户，Hiveclaw 可以以 self-hosted enterprise license 形式销售。

Enterprise package 包括：

- Annual platform license。
- One-click enterprise deployment package。
- Support and SLA。
- Premium connectors。
- Advanced governance and audit。
- AI asset registry and lifecycle controls。
- Deployment and integration services。
- Token / model gateway。
- Plugin marketplace access。
- Optional data API packages。

该 package 尤其适合金融服务、咨询、研究密集型组织，以及需要控制 transcript、workspace files、memory、audit logs、policies 和 operational context 的企业。

---

## 10. 竞争定位

Hiveclaw 的差异化不在于生成一个更好的 chatbot 回复，而在于提供企业运行 AI worker 所需的控制平面。

### 10.1 核心优势

1. AI asset management category
   - Hiveclaw 将 agent 视为受治理的企业资产，拥有 lifecycle、ownership、permissions、memory、workflows 和 audit evidence。

2. Model neutrality
   - 企业不需要把自己的 operating layer 绑定到单一模型厂商。

3. Channel complementarity
   - Hiveclaw 与现有工作渠道互补，而不是替代它们。

4. Self-hosted and private deployment path
   - 客户可以将 operational context、memory、artifacts、policies 和 audit evidence 保留在自己控制的环境中。

5. Governance-first architecture
   - 权限、审批、preflight、audit 和 tenant boundary 是核心架构，不是后补安全层。

6. Learning loop
   - 数字员工可以通过受治理的 memory 和 skill evolution 持续积累组织上下文。

7. Open-source credibility
   - 技术 buyer 可以更容易地 inspect、test、deploy 和 trust 系统。

### 10.2 Competitive Landscape

| 类别 | 代表 | Hiveclaw 差异 |
|---|---|---|
| Chatbot / Copilot | ChatGPT Enterprise、Claude、Microsoft Copilot | 公司级 digital employees 和 AI asset management，而不只是个人助手 |
| Agent frameworks | LangGraph、CrewAI、AutoGen | Operational control plane、governance、memory、audit、lifecycle |
| RPA / automation | UiPath、Zapier、Make | 面向不确定 workflow 的 LLM-native judgment 和 governed autonomy |
| Enterprise search | Glean、Coveo | Agent 使用知识执行任务，并保留 evidence |
| Vertical agents | Harvey、Sierra、Decagon、Abridge | 多岗位 digital workforce control plane，而不是单一 vertical worker |
| IM / OA / Office ecosystems | 飞书、钉钉、Slack、Teams、企业微信、Microsoft 365、Google Workspace | 跨渠道 governance、memory、approval 和 audit layer |
| Internal platforms | 企业自建 | 更快 time-to-production 和更低长期维护成本 |

---

## 11. 护城河

Hiveclaw 的护城河来自围绕 AI worker 的组织级学习与控制闭环。

- Runtime moat：durable sessions、RuntimeTask execution、tool governance、restart / resume、artifacts 和 audit spans。
- Governance moat：tenant isolation、principal-aware permissions、capability policy、approval、preflight 和 auditability。
- Learning moat：evidence-backed memory、semantic knowledge、skill candidates、verification 和 rollback。
- Asset moat：每个数字员工的 identity、permissions、tools、memory、skills、workflows、outputs 和 audit evidence 都会形成企业 AI asset graph。
- Neutrality moat：跨 models、clouds、IM / OA systems 和 office ecosystems 的独立第三方基础设施。
- Deployment moat：面向 context-control 客户的 self-hosted 和 private deployment path。
- Data-control moat：operational context、work evidence、policies、approvals、memory 和 audit records 可以留在客户控制的基础设施内。

---

## 12. 当前产品基础

Hiveclaw 已具备 AI-native organization control plane 的核心基础：

- 公司级控制台：管理数字员工、模型与预算、工具与能力、记忆治理、渠道与集成、审批、审计、quota、成员、角色和本地 agent channel。
- 数字员工目录：支持 owned、shared、running、attention-needed 和 local runtime 状态。
- HR-style 数字员工创建路径：澄清 role、authority、capability packs、memory boundaries 和 first working session。
- Agent workbench：覆盖 chat、awareness、tools、skills、workflows、knowledge、evolution、subagents、A2A、workspace、office、approvals、activity 和 settings。
- Enterprise knowledge base 和 company-info surfaces：用于受控组织上下文。
- Approval、audit、quota 和 capability governance。
- Channel integrations：将 workplace tools 作为进入受治理 runtime 的入口。
- Self-hosted architecture 和 local / hybrid agent runtime path。

---

## 13. 路线图

### 13.1 Near Term

- 打包 Investment / Market Intelligence Digital Employee workflow。
- 产品化 VC / consulting / research demo path。
- 强化数字员工创建、授权和审阅 onboarding。
- 输出 self-hosted 和 private deployment 的 security / deployment brief。
- 与 professional knowledge organizations 验证 paid deployments 和首批可重复数字员工场景。

### 13.2 Expansion

- 增加 consulting client research、portfolio intelligence、operations coordination 和 engineering knowledge work 等 packaged workflows。
- 扩展 managed cloud、customer VPC、self-hosted 和 hybrid local-agent deployment options。
- 深化 AI asset lifecycle management：registry、owner、permissions、usage、memory、audit、improvement history 和 retirement。
- 建设 AI consultants、automation agencies 和 SIs 的 partner enablement。

### 13.3 Long-Term Vision

Hiveclaw 将成为企业 digital workforce operating system。

每家公司都可以像管理人类团队一样创建、治理、协调和改进 AI workers，同时获得软件原生的 identity、memory、permissions、audit、workflows 和 automation。

---

## 14. 风险与应对

| 风险 | 应对 |
|---|---|
| 模型厂商提供更多 agent management 功能 | 保持 model-neutral、self-hosted、cross-channel 和 enterprise-control 定位 |
| IM / OA 生态内置更多 AI agent | 将其作为互补渠道，Hiveclaw 拥有跨渠道 governance、memory 和 lifecycle layer |
| 客户偏好 vertical agents | 以高价值 workflow land，再扩展到 multi-agent control plane |
| 企业选择自建 | 用 time-to-production、governance depth、self-hosted packaging 和维护成本取胜 |
| AI 输出质量波动 | 高判断 workflow 保留 human review，并用 evidence-backed memory 提高可重复性 |
| 推理和工具成本压缩毛利 | 使用 usage pricing、model routing、caching、workflow packaging 和 enterprise licensing |
| 安全或治理事件损害信任 | 强制 permission-first architecture、approval、action preflight、audit trail 和 fail-closed defaults |

---

## 15. 市场来源

- [Menlo Ventures, 2025 State of Generative AI in the Enterprise](https://menlovc.com/perspective/2025-the-state-of-generative-ai-in-the-enterprise/)。
- [McKinsey, The State of AI: Global Survey 2025](https://www.mckinsey.com/capabilities/quantumblack/our-insights/the-state-of-ai)。
- [PwC, 2025 AI predictions midyear update](https://www.pwc.com/us/en/tech-effect/ai-analytics/ai-predictions-update.html)。
- [OpenAI, The State of Enterprise AI 2025](https://openai.com/business/guides-and-resources/the-state-of-enterprise-ai-2025-report/)。
