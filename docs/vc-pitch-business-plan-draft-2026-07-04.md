# Hiveclaw VC Pitch / Business Plan 草稿

> 日期：2026-07-04
> 状态：research draft
> 目的：先形成给 VC 的商业计划书叙事骨架，不做 PPT 版式。

## 0. 当前建议一句话

Hiveclaw 不应该被讲成“又一个 AI agent framework”或“多 Agent 聊天产品”。更适合的 VC 叙事是：

> Hiveclaw is the AI-native organization control plane for enterprise digital employees: companies can create, govern, run, observe, and continuously improve AI workers with identity, memory, tools, workflows, permissions, audit, and self-evolution.

中文版本：

> Hiveclaw 是面向企业数字员工的 AI-native 组织中台：企业可以像管理真实员工一样，创建、授权、运行、观测和持续改进 AI 数字员工。

核心融资叙事应该围绕一个现实矛盾：

- 企业已经大量试用 AI，但真正进入生产的 agentic workflow 很少。
- 原因不是模型不够强，而是企业缺少“可控、可审计、可扩展”的 agent 运行与治理底座。
- Hiveclaw 的切入点是把 AI 从 isolated prompt window 变成公司级 digital workforce。

## 1. 成熟商业计划书 / VC Pitch 通常包含什么

综合 SBA、Sequoia、YC 和 AI startup pitch 资料，一个完整版本可以分成两层：

### 1.1 完整商业计划书结构

适合尽调和内部推演，通常包含：

1. Executive Summary
2. Company Description
3. Problem / Market Need
4. Market Analysis
5. Customer Segments / ICP
6. Product / Service Line
7. Technology / Defensibility
8. Competitive Landscape
9. Go-to-Market / Sales Strategy
10. Business Model / Pricing
11. Organization / Team
12. Financial Projections
13. Funding Request / Use of Funds
14. Risks and Mitigations
15. Appendix / Evidence

### 1.2 VC Pitch Deck 结构

适合第一次融资沟通，应更短、更有叙事张力：

1. One-line purpose
2. Problem
3. Why now
4. Solution
5. Product demo / workflow
6. Customer / ICP
7. Market size
8. Business model
9. GTM
10. Competition / why we win
11. Traction / evidence
12. Team
13. Ask / use of funds

### 1.3 AI 公司需要额外回答的问题

AI startup 不能只按传统 SaaS 写，还要额外回答：

- AI 是 feature、workflow、platform，还是 infrastructure？
- 谁是 sponsor，谁是 end user？
- 如何证明 retention，而不是 demo novelty？
- 单位经济模型如何覆盖模型 / sandbox / infra 成本？
- 如何处理数据权限、安全、审计、human-in-the-loop？
- 产品是否能从 point solution 扩展成 platform？
- 随模型进步，公司护城河是否增强，还是被模型吞掉？

## 2. 市场与“Why Now”

### 2.1 市场信号

外部资料给出的共同信号：

- Enterprise AI spend 正在快速增长，应用层已经成为主要消费方向之一。
- 企业 AI adoption 很高，但从 pilot 到 scaled production 仍然卡住。
- Agentic AI 的主要落地障碍集中在 orchestration、control、trust、governance、workflow redesign，而不只是模型能力。
- 企业开始从“用 AI 生成内容”转向“把复杂任务委派给 AI workflow / AI worker”。

### 2.2 Hiveclaw 的 Why Now

Hiveclaw 的 why now 可以这样写：

> 过去两年，模型能力和工具调用能力让 agent demo 变得容易；但企业真正需要的是可控的 agent workforce。ChatGPT、Claude、Copilot 让个人生产力爆发，却没有解决公司如何给 AI worker 分配身份、权限、记忆、工具、预算、审批、审计和持续改进的问题。随着企业从 AI pilot 进入 agentic workflow 阶段，agent control plane 成为新的基础设施层。

重点不是“AI 很热”，而是：

- 模型能力到了可执行多步任务的临界点。
- 企业从个人 copilots 进入组织级 agent deployment。
- 治理、审计、权限、身份、可恢复运行正在成为采购门槛。
- 开源 / self-hosted / private deployment 对中高敏感企业更有吸引力。

## 3. 用户画像

### 3.1 推荐先聚焦的 Beachhead ICP

不要一开始讲“所有企业”。建议初稿先选一个 beachhead：

> AI-forward SMB / mid-market knowledge-work companies, especially 30-500 人规模的 SaaS、专业服务、咨询、研发型团队、跨境运营团队。

原因：

- 他们有足够多重复知识工作，能感知 AI worker 价值。
- 他们没有大企业内部平台团队，愿意买现成 control plane。
- 销售周期比大型企业短。
- 对“一个数字员工先解决一个高摩擦工作流”更容易付费。

### 3.2 Buyer / Sponsor / End User

| 角色 | 典型人群 | 关心什么 | Hiveclaw 应该怎么说 |
|---|---|---|---|
| Economic buyer | Founder, CEO, COO, CFO | 降本增效、业务规模化、可控风险 | 用更少 headcount 承接更多知识工作，但所有 action 都可审计可治理 |
| Technical buyer | CTO, Head of AI, IT / Platform lead | 安全、部署、集成、权限、可维护性 | self-hosted / API / tool governance / audit / model-neutral runtime |
| Functional sponsor | Head of Ops, Sales Ops, Support, Research, HR | 具体工作流效率 | 数字员工能持续执行任务、积累经验、交接上下文 |
| End user | 普通业务员工 / 管理者 | 好不好用、是否可信、是否减少负担 | 像同事一样协作，而不是又多一个聊天窗口 |

### 3.3 第一批使用场景建议

初始商业计划书建议只讲 3 个高信号场景，不要铺太开：

1. Operations / Research Digital Employee
   - 市场调研、竞品监控、客户/行业资料整理、周期性报告。
   - 优点：结果容易验收，风险相对可控，能体现长期记忆和自动触发。

2. Internal AI Workbench for Product / Engineering Teams
   - 文档整理、issue triage、代码/产品上下文问答、release note、研发知识库。
   - 优点：技术 buyer 自己能感知价值，适合开源传播。

3. Enterprise Agent Governance Layer
   - 管理多个 AI workers 的身份、权限、工具、审批、审计和成本。
   - 优点：更接近高 ACV enterprise story，是中长期扩展方向。

## 4. 产品形态

### 4.1 三层产品架构

对 VC 不要讲太多内部模块名，建议压成三层：

1. Digital Employee Runtime
   - 每个 agent 有身份、session、workspace、tools、memory、skills、runtime state。
   - 价值：让 agent 成为稳定的组织工作者，而不是一次性 chat。

2. Enterprise Control Plane
   - 权限、审批、预算、审计、组织结构、tool policy、agent lifecycle、observability。
   - 价值：让企业敢把任务交给 agent。

3. Self-Evolution / Learning Vault
   - T0/T2/T3/soul、memory gate、skill candidates、verification、rollback。
   - 价值：agent 越用越懂公司、越懂角色、越会做事。

### 4.2 VC 版本的产品 Demo 应该怎么讲

不要 demo “聊天”。建议 demo 一个完整工作流：

1. 管理员创建一个 Research 数字员工。
2. 分配公司知识、工具权限、预算和审批边界。
3. 用户发起一个调研任务，agent 生成计划并执行多步工具调用。
4. 过程里产生 session timeline、tool events、approval request、artifact。
5. 任务完成后沉淀 T0 evidence、T2 reviewed segment、T3 memory / skill candidate。
6. 下次类似任务，agent 能复用经验，并且管理员能看到审计与成本。

这个 demo 证明的不是“AI 会回答问题”，而是：

- 任务可以执行。
- 过程可以治理。
- 结果可以审计。
- 经验可以积累。
- 企业可以规模化运营。

## 5. Go-to-Market Strategy

### 5.1 推荐 GTM 主线

第一阶段不要直接重 enterprise 大单。建议：

> Open-source trust + founder-led design partners + paid workflow pilots.

具体打法：

1. Open-source / developer credibility
   - 用 Apache 2.0 self-hosted repo 建信任。
   - 内容主题聚焦 agent governance、digital employee control plane、self-evolving agents。
   - 目标是吸引 CTO、AI platform engineers、technical founders。

2. 5-10 个 design partners
   - 每个客户只落一个具体数字员工工作流。
   - 周期 4-8 周。
   - 必须付费或至少签明确 success criteria。

3. Land and expand
   - Land：一个 team、一个 workflow、一个 digital employee。
   - Expand：多个 digital employees、跨部门 workflows、企业知识库、审批审计、更多工具集成。

4. Partner channel
   - AI consultants / automation agencies / SIs 可以成为 early channel。
   - 他们有客户关系，但缺一套可治理 agent runtime。

### 5.2 定价建议

初稿可以写成假设，不要过早锁死：

- Managed Cloud: platform fee + per digital employee + usage / compute pass-through margin。
- Self-hosted Enterprise: annual license + support + premium governance / connectors。
- Pilot Package: fixed fee for one workflow implementation, e.g. 4-8 week paid pilot。

关键指标：

- Gross margin after model / sandbox / infra cost。
- Active digital employees per org。
- Weekly completed tasks。
- Human hours saved / task cycle time reduction。
- Expansion from 1 workflow to N workflows。
- Approval / audit events proving enterprise governance value。
- Retention by workflow, not only login activity。

### 5.3 GTM 风险

| 风险 | 解释 | 应对 |
|---|---|---|
| “Agent platform” 太宽 | VC 和客户听过太多泛化 agent 平台 | 先绑定 digital employee control plane + 具体工作流 |
| Enterprise sales 太慢 | 大企业采购、合规、集成周期长 | 先 mid-market design partner，再 enterprise |
| Demo 容易、ROI 难 | AI demo 多，但生产价值难证 | 每个 pilot 预设 baseline、task count、cycle time、quality metric |
| 模型厂商下压 | OpenAI / Anthropic / Microsoft 可能做更多 agent 管理功能 | Hiveclaw 强调 model-neutral、self-hosted、enterprise governance、memory/evolution vault |

## 6. 竞争定位

### 6.1 不要只列竞品，要按类别定位

| 类别 | 代表 | Hiveclaw 的差异 |
|---|---|---|
| Copilot / Chatbot | ChatGPT Enterprise, Claude, Microsoft Copilot | 它们提升个人生产力；Hiveclaw 管理公司级 AI workers |
| Agent frameworks | LangGraph, CrewAI, AutoGen | 它们帮助开发 agent；Hiveclaw 运营、治理、审计、改进数字员工 |
| RPA / automation | UiPath, Zapier, Make | 它们偏 deterministic workflow；Hiveclaw 面向 LLM-native workers 和 memory/skill evolution |
| Enterprise AI search / knowledge | Glean 等 | 它们解决知识访问；Hiveclaw 让 agent 用知识执行任务并留下审计 |
| Vertical AI agents | legal / sales / support agents | 它们解决单垂直任务；Hiveclaw 是多岗位数字员工控制平面 |

### 6.2 护城河初稿

Hiveclaw 的护城河不应该写成“用了某个模型”。应写成：

- Runtime moat：durable session、RuntimeTask、tool governance、restart/resume、audit spans。
- Governance moat：tenant/RLS、principal-aware memory/action boundary、capability policy、approval/preflight。
- Learning moat：T0/T2/T3/soul、Memory Gate + Platform Gate、skill candidates、verification/rollback。
- Product moat：数字员工作为组织资产，而不是一次性 assistant。
- Deployment moat：self-hosted / model-neutral / enterprise-grade controls。

## 7. 建议的商业计划书目录

建议后续正式文档按下面写：

1. Executive Summary
   - Hiveclaw 是什么、为什么现在、面向谁、解决什么、融资用途。

2. Problem
   - 企业 AI adoption 高，但 agentic production 难。
   - 缺身份、权限、审计、记忆、工具治理、运行时恢复。

3. Solution
   - AI-native organization OS / digital employee control plane。

4. Product
   - Digital Employee Runtime
   - Enterprise Control Plane
   - Self-Evolution / Learning Vault
   - Demo workflow

5. Customer / ICP
   - Beachhead ICP
   - Buyer / sponsor / end user
   - Initial workflows

6. Market
   - Enterprise AI spend
   - Agentic AI growth
   - AI application layer shift
   - Bottom-up market sizing assumptions

7. Go-to-Market
   - Open-source trust
   - Paid design partners
   - Pilot package
   - Land-and-expand
   - Partner channel

8. Business Model
   - Managed cloud
   - Self-hosted enterprise
   - Per digital employee / usage / enterprise add-ons

9. Competition
   - Copilot, frameworks, RPA, search, vertical agents
   - Why Hiveclaw wins

10. Traction / Evidence
   - Current product surfaces
   - Runtime and governance maturity
   - Open-source / design partner / usage metrics once available

11. Team
   - Founder-market fit
   - AI infra / enterprise / product experience

12. Financial Plan
   - 24-month hiring plan
   - COGS assumptions
   - ACV assumptions
   - Pilot conversion
   - Runway

13. Fundraising Ask
   - Amount
   - Milestones
   - Use of funds

14. Risks and Mitigations
   - Model platform risk
   - Enterprise adoption risk
   - Infra cost risk
   - Security/governance risk

15. Appendix
   - Architecture
   - Source references
   - Product screenshots
   - Customer discovery notes
   - Detailed financial model

## 8. 当前最需要补的数据

正式 VC 版本还缺这些事实材料：

1. Traction
   - 是否已有真实用户 / design partners / waitlist / GitHub stars / deployments。
   - 如果没有，需要先设计 5-10 个 customer discovery interviews。

2. Pricing evidence
   - 目标客户愿意为一个 digital employee workflow 付多少钱。
   - 是否更接受 SaaS、self-hosted license、还是 implementation pilot。

3. ROI baseline
   - 一个 workflow 当前人工需要多少小时。
   - Hiveclaw 后减少多少时间、错误、handoff 成本。

4. Competitive benchmark
   - 与 ChatGPT Enterprise / Copilot / LangGraph / CrewAI / Glean / UiPath 的清晰差异。

5. Financial model
   - 模型成本、sandbox 成本、infra 成本、support 成本。
   - 毛利率、ACV、pilot conversion、payback period。

6. Product proof
   - 需要一条 VC 级 demo path，能在 5 分钟内证明“数字员工 + 控制中台 + 自进化”。

## 9. 下一步建议

建议按这个顺序推进：

1. 先确定 beachhead ICP：mid-market AI-forward knowledge-work companies。
2. 选 1 个 flagship workflow：Research / Ops digital employee 最适合第一版。
3. 写 1 页 executive narrative：问题、为什么现在、Hiveclaw 是什么、为什么赢。
4. 做 10 页 pitch deck 草稿。
5. 同步做一份 business plan long-form，补市场、GTM、商业模型、风险。
6. 设计 customer discovery 问卷，验证 buyer、预算、ROI、采购障碍。
7. 找 5-10 个 design partners，用 paid pilot 产出 traction。

## 10. 本轮参考资料

- U.S. Small Business Administration: Write your business plan
  - https://www.sba.gov/business-guide/plan-your-business/write-your-business-plan
- Sequoia Capital: Writing a Business Plan
  - https://sequoiacap.com/article/writing-a-business-plan/
- Y Combinator: How to build your seed round pitch deck
  - https://www.ycombinator.com/library/2u-how-to-build-your-seed-round-pitch-deck
- Headline: Series A Pitch Deck Template for AI Startups
  - https://headline.com/blog-latest/article-latest/series-a-pitch-deck-template
- Menlo Ventures: 2025 State of Generative AI in the Enterprise
  - https://menlovc.com/perspective/2025-the-state-of-generative-ai-in-the-enterprise/
- McKinsey: The State of AI Global Survey 2025
  - https://www.mckinsey.com/capabilities/quantumblack/our-insights/the-state-of-ai
- PwC: AI agent survey
  - https://www.pwc.com/us/en/tech-effect/ai-analytics/ai-agent-survey.html
- Bessemer Venture Partners: The State of AI 2025
  - https://www.bvp.com/atlas/the-state-of-ai-2025
- OpenAI: The State of Enterprise AI 2025
  - https://openai.com/business/guides-and-resources/the-state-of-enterprise-ai-2025-report/
- ITPro / Forrester summary: agentic AI operationalization gap
  - https://www.itpro.com/technology/artificial-intelligence/most-enterprises-are-still-unprepared-to-operationalize-it-it-leaders-are-bullish-on-agents-but-keeping-falling-at-the-final-hurdle-heres-why
- MarketsandMarkets: Agentic AI Market Report 2025-2032
  - https://www.marketsandmarkets.com/Market-Reports/agentic-ai-market-208190735.html
