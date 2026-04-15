# Clawith 产品架构深度分析

> 视角：资深 AI SaaS 产品架构师（Notion / Linear / Figma 背景）
> 基于：AGENT_NATIVE_EXECUTION_PERMISSION_PROPOSAL.md 提案 + 代码实现审查
> 日期：2026-03-20

---

## 核心判断前置

| 判断 | 结论 | 置信度 |
|------|------|--------|
| Clawith 是否已经脱离聊天工具形态 | YES | 高 |
| 当前代码是否已具备 agent-native SaaS 的骨架 | YES | 高 |
| 是否需要立即做权限收口 | YES | 高 |
| 是否应先做 Local Connector | NO，先做 Hosted Workspace 深度 | 高 |
| 最近 PMF 场景是否明确 | 部分明确，需收窄 | 中 |
| 当前到企业可用的差距是否可弥合 | YES，但需要 2-3 个季度聚焦 | 中 |

---

## 问题一：Clawith 与 Claude Code / Cursor / Coze / Dify 的本质区别

### 判断：YES — Clawith 不是工具，是组织操作系统

**它们是什么：**

| 产品 | 本质 | 核心循环 |
|------|------|----------|
| Claude Code / Cursor | 开发者增强工具 | 人发指令 → AI 执行 → 人检查 |
| Coze / Dify | Bot 构建平台 | 人设计流程 → 部署 Bot → 用户触发 |
| Clawith | 数字员工运行平台 | 人分配角色 → Agent 持续存在 → 自主/协作执行 |

**关键区别在三个字：持续存在。**

Claude Code 的会话结束了，Agent 就不存在了。Coze 的 Bot 被调用才运行。但 Clawith 的 Agent：

1. **有身份** — `soul.md` 定义人格，`Agent` 模型有名字、角色、头像
2. **有记忆** — `memory.md` 跨会话持久，`conversation_summarizer.py` 提取长期上下文
3. **有工作空间** — `agent_data/<uuid>/` 下有独立文件系统
4. **有主动性** — `trigger_daemon.py` 支持 cron/interval/poll/webhook/on_message 六种触发器
5. **有人际关系** — `AgentRelationship` + `AgentAgentRelationship` 建模组织关系
6. **有自主边界** — `autonomy_policy` 定义 L1/L2/L3 分级

**Coze/Dify 是"build a bot"，Clawith 是"hire a digital employee"。**

这个区别不是市场定位的修辞，而是已经体现在代码里的架构选择。

**但风险是**：如果产品交互和营销没有让用户感知到这个区别，Clawith 会被误解为"又一个 Bot 平台"。当前的 Plaza 页面和 Agent 创建流程还不够突出"雇佣一个数字员工"的体验——它更像是"创建一个 Bot"。

### 建议

- 产品语言从"创建 Agent"迁移到"入职数字员工"
- 首次使用引导应该包含：取名字、定角色、设权限边界、分配第一个任务——模拟 HR onboarding
- Plaza 从"Bot 商店"转变为"数字人才市场"

---

## 问题二："数字员工"比喻的边界

### 判断：YES 像员工的部分 + NO 不像员工的部分——边界必须显式画出来

**像员工的地方（应该加强）：**

| 维度 | 当前实现 | 产品意义 |
|------|----------|----------|
| 身份 | `soul.md` + Agent model | 数字员工有名字、角色、职责描述 |
| 记忆 | `memory.md` + 对话摘要 | 不会每次从零开始 |
| 工作空间 | `agent_data/` 目录 | 有自己的"桌面" |
| 主动性 | trigger_daemon 六种触发 | 不只是被动响应 |
| 组织关系 | AgentRelationship | 知道谁是同事、谁是上级 |
| 工作时间 | heartbeat_active_hours | 有"上班时间" |
| 时区 | timezone 字段 | 尊重地理位置 |

**不像员工的地方（必须显式建模差异）：**

| 维度 | 真实员工 | 数字员工应该 | 当前状态 |
|------|----------|-------------|----------|
| 权限继承 | 入职即继承部门权限 | 必须逐项授予 | 部分实现（autonomy_policy） |
| 外部身份 | 用自己的账号 | 明确声明用谁的身份 | 未显式建模 |
| 责任归属 | 自然人承担 | 必须追溯到委托人 | 审计日志有但不完整 |
| 离职 | 交接工作 | 停用时保留数据+工作交接 | expires_at 但无交接流程 |
| 晋升/调岗 | 权限范围变化 | capability grant 动态调整 | 无 |

**核心原则**：

> Agent 是独立的能力主体，不是员工账号的影子。

提案中这句话极为关键。产品上必须让管理员理解：给 Agent 开通某个能力 ≠ Agent 自动获得你的全部权限。这是企业用户最大的安全顾虑。

### 建议

- 创建 Agent 时，默认 autonomy_policy 全部 L3（需审批），由管理员逐步放开
- 在 Agent 详情页增加"权限清单"视图，像 iOS 应用权限那样直观
- "Agent 代表谁执行"必须在每个外部操作的审计记录中明确显示

---

## 问题三：三种执行模式的产品优先级

### 判断：Mode A 优先 > Mode B 有条件跟进 > Mode C 暂不做

| 模式 | 优先级 | 理由 |
|------|--------|------|
| Mode A: Hosted Workspace | P0 — 先做深 | 当前已有基础，SaaS 默认形态，权限边界最清晰 |
| Mode B: Local Connector | P2 — 验证需求后再做 | 有真实客户场景时启动，不要预研过早 |
| Mode C: Full Local Runtime | P3 — 暂不进入路线图 | 安全/运维复杂度太高，ROI 不明确 |

**为什么不急着做 Local Connector：**

1. **当前 Hosted Workspace 还不够深**。Agent 能读写文件，但不能结构化管理项目、不能跨 Agent 共享文档、不能版本化工作产物。把 Mode A 做到"足够好用"比开新战场更重要。

2. **没有验证过的客户场景**。提案中列举的本地文件/本地仓库场景，目前的目标用户（企业内部运营/客服/研究岗）不一定优先需要。如果最先打的客户是"用 Agent 做运营自动化"，那 Hosted Workspace + 渠道集成就够了。

3. **Local Connector 的安全模型会消耗大量工程资源**。connector 认证、能力面控制、本地目录授权——每一项都是大工程。在 PMF 前投入太多基础设施容易失焦。

### 建议

- Q2 2026：把 Hosted Workspace 做到"企业可用"水平（文件版本化、跨 Agent 共享、workspace template）
- Q3 2026：如果有 3 个以上付费客户提出本地操作需求，启动 Local Connector 最小方案
- Mode C 写进技术愿景文档但不排期

---

## 问题四：PMF 最近的场景

### 判断：企业内部"智能助理 + 渠道自动化"是最短路径

**分析方法**：从代码实现完成度反推产品准备度。

| 场景 | 代码完成度 | 客户需求强度 | 竞品壁垒 | PMF 距离 |
|------|-----------|-------------|---------|---------|
| 飞书/钉钉/企微 AI 助理 | 高（6 个渠道集成已有） | 高（中国企业刚需） | 中（Coze 已做但不够深） | 近 |
| Agent 间协作工作流 | 中（gateway 已有 agent-to-agent） | 中 | 高（竞品几乎没有） | 中 |
| 知识库 + 文档自动化 | 低（enterprise_info 存在但浅） | 高 | 低（RAG 同质化严重） | 远 |
| 本地代码仓库操作 | 无 | 低（开发者用 Cursor 更直接） | 低 | 远 |

**PMF 最短路径**：

> 企业通过 Clawith 创建一个"数字员工"，它驻留在飞书群里，有自己的身份和记忆，能主动提醒、回答问题、执行预设流程（如每日汇报、新员工引导、客户跟进），所有操作有审批边界和审计追溯。

这个场景的独特价值是：

1. **不是 Bot，是员工** — 有记忆、有主动性（trigger daemon 已经支持）
2. **不是单点，是持续** — 不像 Coze bot 那样每次都是新会话
3. **不是无管控，是有边界** — autonomy policy 给企业安全感
4. **已有大量代码基础** — feishu/dingtalk/wecom/slack/discord/teams 六个渠道

### 建议

- 将"飞书驻场数字员工"作为 v1.0 的核心场景
- 打包成可交付方案：创建 Agent → 配置飞书渠道 → 设定自主边界 → 分配任务 → Agent 开始工作
- 用这个场景验证：trigger daemon 的稳定性、autonomy 审批流的体验、memory 的长期可用性

---

## 问题五：文档/代码/流程自动化——先做好哪个

### 判断：先做流程自动化（trigger + workspace），文档做轻量集成，代码不做

| 方向 | 优先级 | 理由 |
|------|--------|------|
| 流程自动化 | P0 | trigger_daemon 已有基础，直接绑定渠道和 workspace 即可形成闭环 |
| 文档 | P1（轻量） | 不做"文档编辑器"，做"文档作为 workspace 资源" |
| 代码 | P3 | 开发者场景竞品太强（Cursor/Claude Code），不是 Clawith 的主战场 |

**流程自动化的具体形态**：

当前 trigger_daemon 支持六种触发方式，加上 autonomy_policy 的审批机制，已经具备了一个轻量工作流引擎的雏形：

```
触发器（cron/webhook/on_message）
  → Agent 被唤醒
  → 读取 workspace 上下文
  → 调用工具执行动作
  → 经过 autonomy 审批判定
  → 执行/等待审批
  → 结果写入 workspace + 通知相关人
```

这就是一个完整的自动化流程。不需要画流程图，不需要低代码编辑器——Agent 本身就是流程执行器。

**文档的正确做法**：

提案中说得好——"文档应成为 workspace 能力，而不是孤立功能"。具体来说：

- 不做内嵌文档编辑器（Notion 做了十年才做好）
- 做文档的读取、摘要、搜索——作为 Agent workspace 的一种资源类型
- 通过飞书文档 API / Google Docs API 集成外部文档，而非自建
- `enterprise_info/` 目录已经是正确方向，扩展它

### 建议

- 将 trigger_daemon + workspace + autonomy 三者打通成"自动化引擎"
- 文档集成做到"Agent 能读飞书文档并基于它执行任务"，而非"在 Clawith 里编辑文档"
- 代码场景完全放弃，不分散资源

---

## 问题六：从当前状态到企业可用需要跨越什么

### 判断：需要跨越四道门槛

#### 门槛一：权限模型从"能不能用"到"能做什么"（工程难度：中，优先级：P0）

**当前状态**：
- `permissions.py` 解决"谁能访问这个 Agent"（use/manage）
- `autonomy_service.py` 解决"Agent 执行某动作需要什么审批级别"（L1/L2/L3）
- `policy.py` 有 RBAC/ABAC 框架但未接入 Agent 运行时

**差距**：
- 三套系统各自运行，没有统一判定入口
- Agent 运行时的 tool call 不经过 capability policy 判定
- 外部系统操作没有 execution identity 概念

**提案建议的四层模型（Binding / Access / Capability / Execution Identity）方向正确，但不要一次全做。建议**：

- Phase 1（2 周）：将 autonomy_service 升级为 capability 入口，每个 tool call 都经过它
- Phase 2（3 周）：引入 execution identity 概念，先只在飞书渠道实现
- Phase 3（长期）：Binding 和 Access 层的细化（当有多租户大客户时再做）

#### 门槛二：可观测性和审计（工程难度：低，优先级：P0）

**当前状态**：
- `policy.py` 有 `write_audit_event`，带 hash chain
- `AuditLog` 模型存在
- 但审计不完整——很多操作路径没有调用审计

**企业用户要的是**：
- 这个 Agent 今天做了什么？（Activity timeline）
- 谁批准了这个操作？（Approval trail）
- Agent 消耗了多少 token？（Cost visibility）
- 异常操作实时告警（Anomaly detection）

**建议**：
- 在 `agent_tools.py` 的每个 tool handler 里增加审计写入
- 前端增加 Agent 活动时间线页面
- token usage 的日/月统计已有字段，补上前端展示

#### 门槛三：多租户隔离的完整性（工程难度：中，优先级：P1）

**当前状态**：
- tenant_id 存在于所有主要模型
- `check_agent_access` 有 tenant 边界检查
- 但 `agent_data/` 文件系统没有 tenant 级隔离

**差距**：
- Agent 的文件 workspace 目前按 agent_id 组织，不按 tenant_id
- 如果两个租户的 Agent 都叫"小助手"，gateway 的 name-based 路由可能冲突
- `enterprise_info/` 共享目录的租户隔离不明确

**建议**：
- workspace 路径改为 `agent_data/<tenant_id>/<agent_id>/`
- gateway 的 agent 查找加 tenant 约束
- enterprise_info 明确按 tenant_id 隔离

#### 门槛四：稳定性和运维能力（工程难度：中，优先级：P1）

**当前状态**：
- trigger_daemon 是单进程后台任务
- 没有健康监控和自动恢复
- LLM 调用没有完整的 retry/fallback 链

**企业用户容忍度**：
- Agent 掉线超过 5 分钟 = 不可接受
- 触发器漏掉一次 = 不可接受
- LLM 超时导致 Agent 无响应 = 不可接受

**建议**：
- trigger_daemon 加入持久化状态（当前 `_last_invoke` 在内存中，重启丢失）
- LLM 调用加入 fallback_model 实际使用逻辑（字段存在但未看到调用链路）
- 增加 /health 端点的深度检查（DB + Redis + trigger daemon 心跳）

---

## 战略路线图建议

### Phase 1: "企业可用的飞书数字员工"（8 周）

| 周 | 重点 |
|----|------|
| W1-2 | 权限收口：tool call → capability policy → autonomy 审批 |
| W3-4 | 审计完整性：每个 tool call + 外部操作写审计 + 前端活动时间线 |
| W5-6 | 飞书场景深度：delegated identity 概念引入 + 飞书文档读取能力 |
| W7-8 | 稳定性：trigger daemon 持久化 + LLM fallback + 健康监控 |

### Phase 2: "多场景扩展"（8 周）

| 周 | 重点 |
|----|------|
| W9-10 | Agent 间协作：结构化消息协议 + 跨 Agent workspace 共享 |
| W11-12 | Workspace 深度：文件版本化 + 模板化 workspace + 企业知识库 |
| W13-14 | 多渠道加固：钉钉/企微渠道达到飞书同等深度 |
| W15-16 | 企业管理：团队仪表盘 + 成本分析 + Agent ROI 可视化 |

### Phase 3: "平台化"（时间待定，需求驱动）

- Local Connector（有 3+ 付费客户需求时启动）
- 第三方 Agent 市场
- 自定义 workflow 编排（不做低代码，做 Agent 编排）

---

## 与提案的分歧点

| 提案建议 | 我的判断 | 理由 |
|---------|---------|------|
| Phase 3 引入 Local Connector | 推迟到 PMF 验证后 | 当前最大风险不是技术能力不足，而是核心场景不够深 |
| 四层权限模型同时推进 | 分阶段：先 Capability，再 Execution Identity | 一次做四层会拖慢交付，而且 Binding/Access 层在单租户阶段价值有限 |
| 文档作为 workspace resource | 同意方向，但不建议自建抽象 | 先集成飞书文档/Google Docs API，不要发明自己的文档模型 |
| 默认外部执行身份定为 agent_bot | 完全同意 | 最安全、最易审计、最易向管理员解释 |

---

## 一句话总结

> Clawith 已经不是聊天 Bot 平台——它的代码骨架已经是"数字员工运行平台"。现在最重要的不是加功能或开新战场（Local Connector），而是把"飞书驻场数字员工"这一个场景做到企业客户愿意付费的深度：权限可控、行为可审计、运行稳定、价值可衡量。
