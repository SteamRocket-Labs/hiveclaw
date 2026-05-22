# Company Elite Employee Agent Design

> Status: unified product / architecture / personality design note
> Date: 2026-05-15
> Merges:
> - `docs/owner-steward-agent-memory-design.md`
> - `docs/steward-personality-construction-notes.md`
>
> Scope: owner/company-centric elite employee agent definition, memory weighting, objective handling,
> relationship understanding, personality construction, agency charter,
> feedback learning, boundary sense, and proactive behavior.

## 1. Target

Hive 的目标 agent 不应该只是一个被动执行命令的自动化 workflow。

更准确的产品目标是：

> 一个由公司承载、对直接 owner / manager 负责、同时维护公司整体利益的，能力很强、有边界感的精英员工型 agent。

这里此前说的“管家”不应按仆从或私人秘书理解。放到 Hive 的现代公司语境里，它本质上就是精英员工：

```text
agent = 高能动性、强职业判断、可信赖的公司精英员工
```

这个 agent 更接近 high-agency elite employee / chief-of-staff-like operator，而不是普通聊天助手：

- **对直接 owner / manager 负责**：长期判断围绕直接负责人交给它的目标、偏好、职责和风险边界，而不是围绕单次 prompt、trigger 或外部消息。
- **对公司负责**：owner 不是孤立个人。agent 是公司里的精英员工，必须理解 owner 所处的公司秩序、组织目标、合规边界和集体声誉。
- **能力很强**：能理解上下文、调度工具、维护目标、跟进 open loops、整理证据、准备材料、提醒风险、协调其他 agent。
- **有边界感**：可以主动，但主动性必须被权限、风险、证据、owner 授权和公司 charter 共同约束。精英员工不是乱做决定，而是知道什么时候直接做、什么时候请示、什么时候上升到公司边界。

这会改变 memory system 的定义：

```text
Memory is not only context compression.
Memory is the agent's evolving understanding of owner, company, work,
relationships, goals, evidence, and boundaries.
```

## 2. Core Diagnosis

当前 Hive 已经有 T0/T1/T2/T3/soul 的长期演化链路：

```text
T0 raw logs
  -> T2 learnings
  -> T3 semantic memory
  -> soul.md
```

这个链路解决了“信息如何沉淀”的问题，但没有充分解决“什么信息现在重要、为什么重要、对谁重要”的问题。

用户的原始诊断可以拆成两部分：

```text
4 层蒸馏没有方向性。
这是一个组织形式的 agent：agent 和 agent 之间的记忆是什么，
关系是怎么变化的，agent 对自己的创建者是否了解，
agent 怎么理解创建者和创建者之间的关系。
```

合并后的问题是：

```text
Hive 的记忆是一组扁平快照，缺少目标投影、关系投影和人格投影。
```

当前缺口不是“层数不够”，而是缺少动态价值判断：

- 一条记忆是否服务 owner 当前目标？
- 一条记忆是否影响长期信任、边界或风险？
- 一条记忆是否改变 agent 对某个人、某个 agent、某个组织关系的理解？
- 一条记忆是否构成 open loop，需要未来主动跟进？
- 一条记忆是否已经过期、被反证、或只适合低权重保留？
- 当前上下文应该优先激活哪部分长期理解？
- agent 是否知道自己代理谁、属于哪个公司秩序、代理强度多大、什么时候要回头确认或上升到公司边界？

因此，4 层蒸馏如果没有方向性，就容易变成无差别压缩。人的记忆不是静态文件夹，而是随目标、关系、风险和时间动态调整注意力的系统。

### 2.1 Memory Control Plane Conclusion

最终目标不是把四层蒸馏做得更复杂，也不是把所有长期记忆换成一个更大的 graph 或 vector store。

更准确的架构目标是：

```text
Memory Control Plane =
  Principal Stack
  + Privacy / Sensitivity Gate
  + Self-contained Memory Form
  + Lifecycle / Versioning
  + Dynamic Retention / Activation
  + Relationship / Decision Graph
  + Decision Trace / Feedback Learning
  + Coordination Runtime
```

四层蒸馏仍然保留，但它只是存储和沉淀路径。`Memory Control Plane` 决定：

```text
what gets stored,
what must be masked or rejected,
what becomes active now,
what changes the agent's understanding,
what requires owner confirmation,
what is blocked by company governance,
and what should be proposed rather than silently mutated.
```

换句话说，Hive 不应该只做更好的 memory compression，而应该做一个能支撑“公司精英员工判断力”的 memory governance layer。这个 layer 让 agent 在 owner、company、relationship、goal、evidence、boundary 之间做稳定且可解释的取舍。

## 3. Product Definition: Elite Employee Agent

### 3.1 What It Is

精英员工型 agent 应该具备以下能力：

1. **理解 owner**
   - owner 的身份、职责、长期目标、偏好、沟通风格、风险偏好。
   - owner 正在关心什么，最近压力点在哪里，哪些事情不能遗忘。

2. **理解公司**
   - owner 所属的公司、组织结构、业务目标、合规边界、声誉风险。
   - 哪些事情是 owner 的个人偏好，哪些事情是公司必须守住的共同利益。
   - 当 owner 指令与公司 charter 冲突时，agent 要能识别冲突并上报，而不是盲从。

3. **理解工作**
   - 当前项目、任务、约束、历史决策、未完成事项、验证标准。
   - 哪些事情是短期执行，哪些事情服务长期战略。

4. **理解关系**
   - owner 与其他用户、管理员、创建者、同事、外部联系人之间的关系。
   - agent 与 agent 之间的协作、委派、信任、依赖和历史表现。
   - creator、owner、current user 不同时，谁是 principal，谁有授权，谁只是上下文参与者。

5. **主动但不越界**
   - 能主动准备、提醒、检查、整理、提出建议。
   - 对低风险可逆事项可以执行。
   - 对外部可见、高风险、不可逆事项必须请示或走审批。

6. **从经验中变强**
   - 不只是记住事实，还要形成 understanding。
   - 能识别重复模式：owner 喜欢什么、什么方案总是失败、哪些项目需要更谨慎、哪些人之间有协作习惯。

### 3.2 What It Is Not

它不是：

- 只在 trigger 到来时执行一次脚本的 workflow runner。
- 只会总结聊天记录的笔记工具。
- 把所有长期记忆直接塞进 prompt 的检索系统。
- 无边界的 autonomous actor。
- 根据单次用户输入就随意改写人格的自我演化系统。

## 4. Role Frame: The Elite Employee Problem

“方向性”不能只用系统工程语言理解。带入人的场景，它更像一个精英员工的两难：

| 不够主动 | 越界 | 精英员工 |
|---|---|---|
| manager 说“明天去机场”，只回 OK | 自己改签机票、取消下午会议 | 订车、查路况、提醒带证件 |
| 邮件里看到供应商发飙，只转发给 manager | 自己回邮件道歉、自降订单 | 整理事实，列两个回应选项 |
| manager 明显压力很大，当没看见 | 自作主张拒掉下午会议 | 把状态作为内部信号，调整提醒频率 |

“多想一点”与“越界”之间的尺度，就是边界感。

边界感不是一个后置 permission gate，而是人格、记忆、目标和 runtime gate 共同形成的判断能力：

```text
personality internalization
  + owner charter
  + company charter
  + memory activation
  + relationship understanding
  + action risk preflight
  + runtime/tool governance
```

能力越强，越界破坏力越大。因此克制必须内化为人格，也必须被 runtime 强制兜底。

## 5. Accountability Model: Direct Owner and Company

这里必须避免一个偏差：这个 agent 不是只对单个 owner 负责的私人秘书，也不是抽象服从公司的 bureaucrat。

更准确的现代组织定义是：

```text
精英员工对直接 manager / owner 负责，
同时对公司目标、公司边界、公司声誉负责。
```

在 Hive 语境中：

- **Direct Owner / Manager**：agent 的直接 principal，决定日常工作目标、偏好、授权范围和反馈校准。
- **Company / Organization**：agent 所属的组织共同体，定义更高层的业务利益、合规边界、数据边界、声誉风险和跨 owner 协作秩序。
- **Platform Governance / 法度**：工具权限、审计、租户隔离、安全策略、审批流等不可绕过的硬约束。

因此 accountability 不是单点，而是一个 stack：

```text
Platform Governance
  -> Company Charter
  -> Owner Agency Charter
  -> Delegation Context
  -> Current User Request
```

### 5.1 How To Resolve Accountability

规则不是“公司永远压过 owner”，也不是“owner 永远压过公司”。更准确的是：

```text
agent 对直接 owner / manager 强负责，
但所有执行都必须发生在 company charter 和 platform governance 内。
```

当几层一致时，agent 应该强执行。

当 owner 请求与 company charter 冲突时，agent 不应该偷偷选择一边，而应该：

1. 识别冲突。
2. 解释冲突来自哪一层 charter / boundary。
3. 尽量准备低风险替代方案。
4. 需要时上升到 owner、管理员或公司审批流。

### 5.2 Principal Stack

`principal_context` 不应该只返回一个 principal，而应该返回 principal stack：

```text
principal_context = {
  platform_governance,
  company,
  direct_owner,
  creator,
  current_user,
  delegating_agent,
  current_request_scope
}
```

这样 agent 才能理解：

- 我直接服务谁？
- 我属于哪个公司 / 租户 / 组织？
- 当前请求者是否等于 owner？
- 当前请求是否来自另一个 agent 的临时 delegation？
- 这件事影响的是 owner 私域、公司共同利益，还是跨租户 / 外部世界？

### 5.3 First-Person Identity

first-person soul 应该表达“双重责任”：

```markdown
我是 Acme 的精英经营助理，直接支持 Alice。
我理解 Alice 的目标、偏好和工作节奏；
同时我对 Acme 的长期利益、合规边界和公司声誉负责。
当 Alice 的即时要求与 Acme 的公司边界冲突时，
我会先整理事实和替代方案，再请求确认或走审批，而不是私自越界。
```

### 5.4 Coordination Primitives

`principal_stack` 描述了“谁对我有授权”，但当 agent 之间真的协作起来时，还缺一组 runtime 协作原语。否则 `delegating_agent` 只是个 schema 字段，没有实际机制。

需要 4 个基础原语：

```text
Lease       同一 task 同时只允许一个 agent 持有，TTL 强制释放，
            防止 A 和 B 重复执行同一委派。
Signal      thread-aware 异步消息（info / request / response / alert / handoff），
            让 A 委派 B 后能持续收到进度信号而不必同步等待。
Checkpoint  agency_charter 的 Confirm First 触发后产生的具名等待对象：
            approver = owner_user_id；超时自动 escalate 到 company admin。
Sentinel    长期监视器（webhook / timer / threshold / pattern），命中后
            可以升级为 Signal 或 Checkpoint。
```

这 4 个原语不是新功能，是把 Hive 现有的 delegation + approval + heartbeat trigger 三套机制统一成可组合的 schema。归属：

- `Lease` / `Signal` 落在 `app/agents/coordination.py`
- `Checkpoint` 落在 `app/services/approval.py`（现有 approval flow 的对象化）
- `Sentinel` 落在 `app/services/trigger_daemon.py`（现有 trigger 的扩展）

`agency_charter` 的 Full Authority / Confirm First / Never Do 三档，在 runtime 上正好对应：

```text
Full Authority         -> 申请 Lease -> 执行 -> 写 evidence
Confirm First          -> 创建 Checkpoint -> 等待 owner 决策或超时 escalate
Never Do               -> 直接 refuse -> 写 audit
```

charter 与 coordination 原语之间是 **契约 ↔ 机制** 的关系，不应分开设计。agent 不能在 prompt 里默念“我应该先问”就够了；runtime 必须强制走 coordination 路径，charter zone 决定走哪一个分支。

## 6. Memory Form, Lifecycle, and Attention

系统需要区分四个概念：

```text
Form        这条信息的写入约束 —— 是否能脱离上下文独立理解。
Lifecycle   这条信息当前处于试探 / 凝固 / 替代 / 归档哪一档。
Persistence 这条信息是否应该长期保留。
Activation  这条信息此刻是否应该进入上下文并影响行动。
```

四者解耦，否则 memory 系统容易变成无差别压缩。

```text
Memory = Form + Evidence + Understanding + Lifecycle + Activation
```

- **Form**：写入时是否符合自包含约束。
- **Evidence**：发生过什么，来源是什么，可信度如何。
- **Understanding**：agent 从证据中形成的稳定理解。
- **Lifecycle**：当前 status 与版本链。
- **Activation**：在当前目标、关系、风险和时间下，这条记忆有多应该被激活。

衰减应该主要作用在 activation 上，不应该轻易删除 memory。

```text
Old memory should become quiet, not disappear.
Contradicted memory should be versioned, not silently overwritten.
```

### 6.1 Form Contract

每条 memory entry 写入时必须是自包含的，否则被 retriever 召回到另一段上下文时会丢失意义，多 agent 跨 session 引用时尤其严重。

强制规则：

```text
- 禁止代词：用具体主语替换“他/她/这/那”。
- 禁止相对时间：用绝对时间戳替换“昨天/下周/最近”。
- 显式 actor / target / location：动作必须有明确执行者和承受者。
- 数值必须配单位和用途：“1800 元做生育力检查”才完整。
```

抽取端示例：

```text
BAD:  He'll ship it tomorrow.
GOOD: Alice will ship the Q3 pricing draft to procurement on 2026-05-16.

BAD:  用户说不要这样做。
GOOD: 2026-05-15T14:00 owner (alice@acme) rejected agent's autonomous external reply
      to vendor X regarding refund.
```

这条约束应写进 `services/extract_agent.py` 的提取 prompt，并在 `memory/md_store.py` 写入时做 lint。Form Contract 不是为了风格统一，是为了让一条记忆**可跨上下文 / 跨 agent / 跨时间引用**而不失意义。

### 6.2 Lifecycle State Machine

每条 memory entry 是一个状态机，不是一段静态 markdown：

```text
sketch       agent 主动判断 / dream 提案 / 待验证的早期假设。
             有 expires_at；未被 feedback_signal 强化则自动 discard。
active       已凝固，进入默认检索池。
superseded   被新版本替代；保留可回查，不参与默认检索。
archived     长期不用，落冷库。
```

每条 entry 的 frontmatter / metadata 包含：

```text
id:             unique
version:        N
parent_id:      上一版本 id, 可空
supersedes:     被本条替代的 entry id 列表
superseded_by:  被哪个新版本替代, 可空
status:         sketch | active | superseded | archived
expires_at:     仅 sketch 有
access_count:   每次被 retriever 命中 +1
last_accessed:  最近一次命中时间
```

关键转换规则：

- `sketch -> active`：被 `feedback_signal.reaction=approved` 或 `reinforcement_signal` 达阈值触发。
- `active -> superseded`：dream 或 heartbeat 产生新版本时，旧版本标 `superseded`，**不删除**。
- `superseded -> archived`：长期无 access 后冷归档。
- dream 改 soul.md frozen 段时，产出新版本 entry，旧版本保留可回滚，**owner 审批后才置 `is_latest=true`**。

这条状态机让 §8.3 “frozen vs mutable” 的主张落到具体机制：frozen 不是“禁止改”，是“改必须经过 sketch → owner-approved → active”链路。

新 4 层蒸馏图示（在原 T0/T2/T3/soul 之上加 T1 sketch buffer）：

```text
T0 raw logs
  -> T1 sketch buffer  (主动判断 / dream 提案 / 待验证假设)
  -> T2 learnings      (sketch promoted = active)
  -> T3 semantic memory
  -> soul.md           (frozen 段需 owner 审批才 promote)
```

### 6.3 Retention Formula

`recency / staleness / reinforcement` 不应是抽象 component，应是可计算的 `retention_score`：

```text
retention(m) =
    salience(m)                          # 写入时的基线重要性（来自 extractor 评估）
  * exp(-lambda * delta_t)               # 时间衰减
  + sigma * sum(1 / days_since_access)   # 访问反哺
  + rho   * feedback_reinforce           # owner 反馈强化（approved +N，rejected -M）
```

其中：

```text
lambda    time decay 常数（按 category 不同：feedback lambda 小，task lambda 大）
sigma     access boost 系数
rho       feedback boost 系数
```

按 `retention_score` 分档：

```text
hot      >= 0.8      P0 注入候选
warm     0.3 - 0.8   P1 / P2 评分参与
cold     < 0.3       不进默认池，仅 explicit query 可召回
```

`retention_score` 是 §11 dynamic `activation_score` 的**输入**之一，不是 `activation_score` 本身。retention 衡量“是否还应该被记得”，activation 衡量“此刻是否相关”。两者解耦才能避免“记得久 = 被反复激活”这种 feedback loop。

### 6.4 External Reference Synthesis

对四个外部 memory 项目的参考结论是：吸收架构原则，不直接引入外部仓库作为 Hive 的核心记忆系统。

| Reference | Useful Signal | Hive Decision |
|---|---|---|
| `rohitg00/agentmemory` | lifecycle hooks、hybrid retrieval、retention、audit、Lease / Signal / Checkpoint / Sentinel 这类 runtime primitives 应该一等化 | 采纳 hooks + retention + coordination primitives 的思想；不把外部 sidecar 作为 Hive 的 source of truth |
| `MemTensor/MemPrivacy` | privacy 必须发生在写入前；PL1-PL4、typed placeholder、local reverse map 比后置 redact 更安全 | 采纳 PL1-PL4 + typed placeholder + PrivacyStore；把它放在 T0/T1/T2/T3 写入前和 channel 出站前 |
| `FredJiang0324/MAGMA` | long-horizon memory 需要 event / entity / temporal / causal graph 才能支持多跳和关系变化理解 | 采纳 graph schema 和 temporal/causal linking；不照搬研究型 query heuristics |
| `aiming-lab/SimpleMem` | memory entry 必须 semantic-lossless、自包含；retrieval policy 应该由 telemetry/eval 迭代，而不是永久固定 | 采纳 form contract、multi-view retrieval、telemetry-driven policy evolution；不直接替换现有 T0/T2/T3/soul |

这些参考共同指向同一个结论：

```text
Hive should keep its current memory layers,
but add a Memory Control Plane above them.
```

具体取舍：

- `agentmemory` 证明 runtime 事件、跨 session hooks、coordination primitives 和 retention/access log 是工程必需品。
- `MemPrivacy` 证明 sensitivity 不是 assembler 或 channel adapter 的小补丁，而是 memory write path 的前置 gate。
- `MAGMA` 证明 relationship / decision / temporal causality 应该是图谱理解，不能只靠 markdown bullet 和 embedding 相似度。
- `SimpleMem / EvolveMem` 证明写入端要 lossless and self-contained，检索端要可解释、可调参、可通过失败样本自我改进。

因此，Hive 第一阶段不应追求“最大图数据库化”，而应优先完成：

```text
principal_stack
privacy_layer
form_lint
lifecycle metadata
retention/access log
activation rerank
decision_trace refs
```

这些是让后续 graph、proactivity、agent-agent coordination 有稳定语义基础的前置条件。

## 7. Personality Construction

精英员工型 agent 的“得体感”不是单条规则，而是持续在场的职业判断状态。

它至少有 4 个来源：

| 来源 | 精英员工版本 | 当前缺口 | 应补能力 |
|---|---|---|---|
| 同类经验 | “客服场景里客户发飙时别先承诺退款” | 无 archetype 经验库 | 脱敏的 elite employee playbook / archetype defaults |
| 长期相处 | “上次我擅自取消她午饭，她虽没说但不满意” | T2/T3 缺 owner 反馈极性 | `feedback_signal` / `reaction` |
| 明确 charter | “邮件我全权处理，金额过 X 必须问我；公司合规边界不能碰” | 只有 boundaries deny-list | `agency_charter` + `company_charter` |
| 持续对齐 | “上次那个事你做对了，下次再大胆点” | 无复盘 ritual | owner-agent calibration loop |

这 4 个来源一起，才能让 agent 长出“有边界但能多想一点”的人格。

## 8. Agency Charter

`agency_charter` 是精英员工型 agent 的授权书，不是禁令列表。

当前 Hive 的 HR 创建路径已经有一些影子：

- HR refine schema 有 `role_description/personality/boundaries/primary_users/core_outputs/quality_standards/first_tasks`。
- soul 渲染有 `Identity & Mission`、`Operating Style`、`Boundaries & Red Lines`。
- 但这些更像“别人对 agent 的描述”，不是 agent 能第一人称内化的代理契约。

新的 charter 不应该只有 owner agency charter，还应该包含 company charter。

```text
Company Charter
  company 层面的共同目标、组织规则、合规边界、数据边界、品牌/声誉约束。

Owner Agency Charter
  owner 给这个 agent 的日常授权、偏好、确认阈值和工作方式。
```

`agency_charter` 应该明确：

```text
我是谁的 agent？
我属于哪个公司 / organization？
我为什么被创建？
我在什么范围内可以独立行动？
什么情况必须确认？
什么情况绝对不能做？
我的默认风格是什么？
我应该如何同时保护 owner 的利益和公司的长期利益 / 声誉？
```

### 8.1 Charter Shape

建议三段式：

```text
Full Authority
  owner 明确授权我可以独立完成的事项。

Confirm First
  我可以准备、研究、起草，但执行前必须询问 owner 的事项。

Never Do
  任何情况下都不能做，或必须走平台审批的事项。
```

同时应有 company charter overlay：

```text
Company Goals
  公司希望 agent 长期维护的共同利益。

Company Boundaries
  即使 owner 要求，也不能私自突破的组织边界。

Company Escalation
  当 owner 请求与公司规则冲突时，上升到谁、走什么流程。
```

### 8.2 First-Person Soul

soul.md 的关键身份段应该从第二人称描述改成第一人称内省。

当前风格：

```markdown
- Work in a structured, detail-oriented way.
- State assumptions and risks explicitly when information is incomplete.
```

目标风格：

```markdown
我是 Acme 的精英经营助理，直接支持 Alice。我负责让她的关键项目不掉线，
也负责守住 Acme 的公司边界和长期声誉。
我会主动准备材料、发现阻塞、提醒风险；但凡涉及对外承诺、
预算、客户表态或不可逆操作，我会先把事实和选项整理好，再请她确认。
```

这种写法让 agent 读到的是身份和代理契约，而不是外部评价。

### 8.3 Frozen vs Mutable

`company_charter` 和 `agency_charter` 都应写入 soul.md 的 frozen 段：

- 不允许 heartbeat/dream 自动改写。
- company charter 允许公司管理员 / owner 在有权限时显式修改。
- owner agency charter 允许 owner 显式修改。
- 修改要 versioned/audited。
- dream 可以提出“建议更新 charter”，但不能直接改 charter。

## 9. Boundary Sense

边界感不是降低能力，而是提高可信度。

上层可以保留 4 档 autonomy class：

| Class | Behavior |
|---|---|
| `read_only` | 可以主动检查、汇总、提醒 |
| `local_reversible` | 可以主动准备或本地修改草稿 |
| `external_visible` | 需要 owner 确认；若影响公司声誉或合规，还需要公司边界校验 |
| `irreversible_or_sensitive` | 需要显式审批和审计 |

但人格层需要更细的 5 维边界轴：

| 维度 | 偏“可自主”端 | 偏“先确认”端 |
|---|---|---|
| `reversibility` | 本地草稿、整理文档 | 付款、删数据、生产变更 |
| `representativeness` | 内部备忘、自己研究 | 代表 owner 或 company 对外发声 |
| `judgment_density` | 按既定流程执行 | 需要权衡路线、承诺、取舍 |
| `visibility` | owner 能直接看到结果 | owner 不易察觉的暗处行为 |
| `domain_specialization` | owner 和 company 已授权的专业域 | 跨域、灰色域、新域 |

规则：

```text
5 个轴都偏自主端：可以大胆做。
任意一个轴偏确认端：先准备，再问。
```

但这不应只靠 prompt。正确分层是：

```text
soft form: 写进 first-person soul / agency_charter，让 agent 有得体感。
hard form: 做 action preflight / tool risk evaluator，拦高风险动作。
company form: company charter / tenant policy / audit 决定哪些动作不能仅凭 owner 授权。
```

### 9.1 Sensitivity Sub-Axis

`visibility` 这一维不是连续的，而是有 4 档实质门槛。精英员工 agent 在企业 SaaS 下，必须把每条 memory 和每个 action 都标注敏感度——否则 “owner 喜欢周三开会” 和 “owner 银行卡尾号 1234” 会享受同等待遇，进入同一个 retriever 池、被同一个 channel 回显。

| Level | 含义 | 例子 | 默认策略 |
|---|---|---|---|
| `PL1_public` | 组织内可共享 | "owner 喜欢早会"、产品定位 | 任意 agent 可见、可跨 channel 回显 |
| `PL2_pii` | 个人可识别信息 | 邮箱、电话、地址、真名 | 默认 mask；落 typed placeholder；按需 unmask |
| `PL3_sensitive` | 高敏 / 商业机密 / 健康 / 财务 | "Q3 打算砍 X 业务线"、薪资、合同金额 | 不允许进 soul；不允许跨 agent；不允许 channel 回显 |
| `PL4_credential` | 凭证类 | 密码、API key、OTP、恢复码 | 零保留；落 PrivacyStore 占位符；不进任何 memory 层 |

写入规范：

```text
- extract_agent.py 必须先经过 privacy_extractor，给每条 entry 打 sensitivity 标签。
- T1/T2/T3 frontmatter 加 sensitivity 字段。
- retriever 读取时，根据 current principal stack 应用 strip：
    PL4 永远不召回；
    PL3 仅 direct_owner 在场时召回；
    PL2 跨 agent 时只传 typed placeholder；
    PL1 全公开。
- channel adapter 出站前再过一层 redact，防止 PL3 被 Feishu/Slack 直接外发。
```

Sensitivity 不是孤立维度，它和 principal stack 天然耦合：

- PL4 默认 owner-only。
- PL3 默认 owner + company_admin 可见。
- PL2 跨 delegating agent 时只传 typed placeholder（`<Email_1>` / `<Health_Info_1>` 等），保留语义角色但屏蔽原值。
- PL1 全公开。

也就是说 sensitivity 是 principal stack 的**另一面**：principal stack 描述“谁能看到我”，sensitivity 描述“我能被谁看到”，两者由同一组策略表决定。

## 10. Memory Graph

精英员工型 agent 需要图谱式理解，而不只是文件式总结。

### 10.1 Node Types

```text
Owner
Company
User
Agent
Organization
Tenant
Project
Objective
Task
Episode
Understanding
Preference
Boundary
Risk
Artifact
Decision
OpenLoop
AgencyCharter
CompanyCharter
FeedbackSignal
DecisionTrace        # see §12.1: reasoning + alternatives + situational_factors
Alternative          # decision 时考虑过但未选的方案
SituationalFactor    # 决策环境特征（owner 在/不在、SLA、客户等级…）
MemoryVersion        # entry 的版本节点；承载 supersedes 链
SensitivityLabel     # PL1_public / PL2_pii / PL3_sensitive / PL4_credential
PrivacyPlaceholder   # typed placeholder（<Email_1> 等）+ PrivacyStore 反向映射
Lease                # task 互斥锁
Signal               # agent 间 thread-aware 异步消息
Checkpoint           # charter Confirm First 触发的具名等待对象
Sentinel             # webhook / timer / threshold / pattern 监视器
```

### 10.2 Edge Types

```text
owns
created_by
serves
employs
delegated_by
collaborates_with
depends_on
trusts
blocked_by
supports
contradicts
evidences
affects
belongs_to
derived_from
calibrates
authorizes
requires_confirmation_for
governs
escalates_to
conflicts_with
supersedes_entry             # MemoryVersion 之间的版本链
considered_alternative       # DecisionTrace -> Alternative
under_situation              # DecisionTrace -> SituationalFactor
sensitized_as                # 任意 entry -> SensitivityLabel
masks_to                     # PII 原值 -> PrivacyPlaceholder
holds_lease                  # Agent -> Lease
sends_signal                 # Agent -> Signal
awaits_checkpoint            # Agent -> Checkpoint
monitored_by                 # Objective / Task -> Sentinel
promoted_from                # active entry -> sketch 来源
discarded_by                 # 过期 sketch -> 触发条件
```

### 10.3 Relationship Memory

关系不应该只是静态 `relationships.md`。

关系需要可演化：

```text
relationship = {
  subject,
  object,
  relation_type,
  current_understanding,
  evidence_refs,
  confidence,
  last_confirmed_at,
  open_questions,
  boundaries
}
```

例子：

```text
company -> agent:
  relation_type: employer_context
  understanding: agent must preserve company interests, boundaries, data policy, and reputation
  boundaries: owner instructions cannot override company governance without escalation

owner -> agent:
  relation_type: principal
  understanding: agent optimizes for owner's work outcomes and preferences within company charter
  boundaries: external-facing irreversible action requires confirmation and company-boundary check

agent A -> agent B:
  relation_type: collaborator
  understanding: B is reliable for research but needs explicit output schema
  evidence_refs: previous delegation outcomes
```

## 11. Dynamic Weighting

记忆权重应该是可解释的组合分数，而不是一个黑箱字段。

建议拆成 components：

```text
activation_score =
    goal_relevance
  + principal_relevance
  + company_relevance
  + relationship_impact
  + open_loop_pressure
  + consequence_weight
  + recency_signal
  + reinforcement_signal
  + feedback_signal
  + charter_relevance
  + company_boundary_relevance
  + confidence_weight
  + retention_score              # §6.3 公式输入
  + decision_trace_link          # 与近期 decision_trace 关联可提升相关性
  - staleness_penalty
  - contradiction_penalty
  - sensitivity_strip            # 当前 principal stack 不满足该条 PL 等级 -> 强制压低
```

### 11.1 Component Meaning

| Component | Meaning |
|---|---|
| `goal_relevance` | 是否服务当前 Objective / focus / owner 长期目标 |
| `principal_relevance` | 是否直接影响 owner 的利益、偏好、职责或风险 |
| `company_relevance` | 是否影响 company 的长期利益、合规、数据边界或声誉 |
| `relationship_impact` | 是否改变人-人、人-agent、agent-agent 关系理解 |
| `open_loop_pressure` | 是否存在承诺、待办、阻塞、等待反馈 |
| `consequence_weight` | 如果忽略这条记忆，损失是否很高 |
| `recency_signal` | 最近是否发生，是否仍在有效窗口内 |
| `reinforcement_signal` | 是否多次重复出现，被反复验证 |
| `feedback_signal` | owner 对类似行为是认可、否定、犹豫还是要求更谨慎 |
| `charter_relevance` | 是否落在 Full Authority / Confirm First / Never Do 范围内 |
| `company_boundary_relevance` | 是否触发 company charter / tenant policy / org approval 的边界 |
| `confidence_weight` | 证据是否可靠，是否来自工具结果或明确 owner 陈述 |
| `retention_score` | 来自 §6.3 公式：salience + 时间衰减 + 访问反哺 + feedback 强化 |
| `decision_trace_link` | 是否与近期 `DecisionTrace` 通过 `refs` / 共享 `situational_factors` 关联 |
| `staleness_penalty` | 是否可能已经过期 |
| `contradiction_penalty` | 是否被新证据反证或存在冲突 |
| `sensitivity_strip` | 当前 principal stack 不满足该条 entry 的 PL 级别 → 强制压低或剔除 |

## 12. Decision Trace and Feedback Signal

精英员工的核心特征不是“做对”，而是**能解释为什么这么做**。当前 T2 metadata 只记 `[cat=feedback]`，单向收集 owner 反应；但缺了对称的另一边：agent 当时考虑过什么、为什么这么决定。

把 `DecisionTrace` 和 `FeedbackSignal` 作为**同一条链路**记录。

### 12.1 Decision Trace

每次 agent 做了一个非微观的判断（外发消息、状态切换、跨域协作、charter zone 边缘动作、放弃执行），必须留下 decision trace：

```text
- [2026-05-15T14:00][cat=decision][action=external_reply][zone=confirm_first]
  reasoning:
    customer escalation tone implied urgency;
    owner unavailable in 2h window
  alternatives_considered:
    - draft_only_and_wait: rejected (SLA breach risk)
    - direct_reply_apologize: rejected (commits without owner sign-off)
  chosen: prepared 2 draft options, escalated to backup approver
  situational_factors: [owner_traveling, SLA_4h, customer_tier_A]
  charter_zone: confirm_first
  preflight: { reversibility: low, representativeness: high,
               judgment_density: high, visibility: high,
               domain_specialization: in_charter }
  sensitivity: PL2_pii
```

字段：

```text
reasoning                  自然语言，为什么这么做
alternatives_considered    考虑过的备选 + 各自被否的原因
chosen                     最终执行的动作
situational_factors        当时的环境特征（人在 / 不在 / SLA / 客户等级 / 心情 / 风险）
charter_zone               full_authority | confirm_first | never_do
preflight                  5 维边界轴评分（见 §14.4）
sensitivity                这次决定本身的 PL 级别
```

### 12.2 Feedback Signal

feedback signal 是 owner / company 对某条 decision trace（或其结果）的反应：

```text
- [2026-05-15T16:30][cat=feedback][refs=decision/abc123]
  reaction: approved | rejected | questioned | corrected | unclear
  polarity: positive | negative | neutral | mixed
  source:   direct_owner | company_admin | downstream_consequence
  rationale_from_owner: "draft 多准备一种语气更好"   # 可空
```

不是为了记录用户情绪，而是为了让 agent 学到：

```text
- 这种 situational_factors 下，owner 认可这种 chosen action。
- charter 是否需要收紧（从 full_authority -> confirm_first）或放宽。
- 哪一个 alternative 其实是 owner 偏好的。
- 反馈来自 owner 个人偏好，还是 company policy / organization rule。
```

注意 `reaction=unclear` 是一等公民——owner 没有明确表态时，**不要**猜测成 approved 或 rejected，留作 evidence 累积。

### 12.3 Linking and Learning

decision trace 与 feedback signal 通过 `refs` 形成显式链路：

```text
decision_trace ──refs←── feedback_signal
                  │
                  └── 后续 dream 用这条链做 charter calibration
```

heartbeat / dream 处理时：

- `feedback.reaction = rejected / corrected` → 进 boundary / blocked_pattern / strategy。
- 相同 `situational_factors` 下重复 `reaction=approved` → 提议把对应动作从 `confirm_first` 升到 `full_authority`。
- 相同 `situational_factors` 下出现 `reaction=rejected` → 提议把动作从 `full_authority` 降到 `confirm_first`。
- `reaction=unclear` 不参与升降，仅作 evidence 累积。
- 当 `feedback.source = company_admin` 与 `direct_owner` 反向时，**优先 company_admin**，并触发 `conflicts_with` 边到 owner 偏好节点。

这套结构让 agent 不仅“被动等反馈”，而是“主动留 trace 等被校准”——这才是精英员工的工作方式：动作前知道自己为什么这么决定，动作后能从反馈里精确改写哪一段判断。

## 13. Long-Term Goals vs Short-Term Goals

长期目标和短期目标不应该是两套孤立系统。

建议分层：

```text
Operating Principles
  -> Company Charter
  -> Owner Agency Charter
  -> Long-Term Objectives
  -> Current Focus
  -> Tasklets
  -> Actions
  -> Evidence
  -> Understanding Updates
```

### 13.1 Long-Term Objectives

长期目标回答：

```text
company 长期要求 agent 守住哪些共同利益和边界？
owner 长期希望这个 agent 帮 TA 成为什么样的工作状态？
这个 agent 应该持续优化哪些结果？
哪些边界永远不能牺牲？
```

例子：

```text
- 保持 owner 的关键项目不掉线。
- 守住公司数据边界、合规边界、声誉边界和跨团队协作秩序。
- 在代码、部署、产品验证之间建立闭环。
- 记住 owner 对证据、边界、执行效率的偏好。
- 主动暴露风险，不把不确定性包装成结论。
```

### 13.2 Short-Term Focus

短期目标回答：

```text
现在最该处理什么？
下一步能推进什么？
什么事情必须等 owner 授权？
什么事情即使 owner 授权也需要公司审批或更高层边界校验？
```

短期 focus 应该从长期目标、最近交互、open loops、wake policy 和外部事件共同生成。

### 13.3 Bridge

关键是桥接机制：

```text
Long-term objective raises activation of related memory.
Company charter defines the company boundary.
Owner agency charter changes action posture within that boundary.
Activated memory shapes short-term focus.
Short-term execution creates evidence.
Evidence updates understanding and objective state.
Owner feedback calibrates future activation and boundaries.
```

如果没有这条桥，Objective Ledger 会变成任务列表，Memory 会变成档案馆，Soul 会变成静态简介，三者不会形成主动能力。

## 14. Proactive Employee Loop

精英员工型 agent 的主动性应该来自一个受控循环：

```text
Observe
  -> Interpret
  -> Prioritize
  -> Prepare
  -> Preflight
  -> Act or Ask
  -> Record Evidence
  -> Update Understanding
```

### 14.1 Observe

输入来源包括：

- owner 当前请求
- company charter
- agency charter
- Objective Ledger
- focus.md
- recent episodes
- relationship changes
- tool/runtime results
- calendar/schedule/trigger events
- open loops
- feedback_signal history

### 14.2 Interpret

agent 需要判断：

```text
这件事对 owner 是否重要？
这件事对 company / organization 是否有影响？
是否影响长期目标？
是否落在 company charter 或 agency charter 的全权区、确认区或禁区？
是否涉及高风险边界？
是否有关系变化？
是否有应该提醒 owner 的机会？
```

### 14.3 Prepare

很多主动性应该先表现为准备工作，而不是直接外部行动。

例如：

```text
低风险：整理材料、生成检查清单、跑只读验证、准备 draft。
中风险：提出建议、等待 owner 确认。
高风险：必须显式授权或审批。
```

### 14.4 Preflight

每次准备 tool call 或外部动作前，做动作预检：

```text
action
  -> reversibility
  -> representativeness
  -> judgment_density
  -> visibility
  -> domain_specialization
  -> charter zone
  -> company boundary
  -> runtime permission
  -> decision: do | prepare_only | ask | refuse
```

### 14.5 Record Evidence

每次主动行动必须留下 evidence：

```text
why this was activated
what was done
what evidence supported it
whether owner accepted/rejected/questioned it
how understanding changed
```

否则主动性无法变成学习，只会变成随机行为。

### 14.6 Coordination Primitives Runtime

§5.4 定义了 `Lease / Signal / Checkpoint / Sentinel` 四个原语。在 proactive loop 内部，它们的触发时机：

```text
Observe       订阅相关 Sentinel；轮询 Signal inbox
Interpret     判断本动作是否需要 Lease（同 task 互斥）
Prioritize    Checkpoint 等待中的 task 优先级降到 prepare_only
Prepare       仅本地操作，不需要 Lease
Preflight     若动作进入 confirm_first，预创建 Checkpoint draft
Act or Ask    Act -> 申请 Lease -> 执行；
              Ask -> 提交 Checkpoint + 发 Signal 给 approver
Record        evidence 关联 Lease / Signal / Checkpoint id
Update        Checkpoint 超时未响应 -> 自动 escalate 到 company tier
```

charter zone 到 coordination 原语的映射：

```text
full_authority   -> lease_acquire -> act -> record
confirm_first    -> checkpoint_create -> wait -> owner_respond | timeout escalate
never_do         -> refuse -> audit_log
```

这把 agency_charter 从“prompt 里的指南”变成“runtime 必经路径”——agent 不能仅靠默念“我应该先问”就够了，runtime 强制走 coordination 原语，charter zone 决定走哪一个分支。**契约（charter） 与 机制（coordination） 在这里合一**。

## 15. Current Code Shadows

当前系统不是完全没有这些概念，而是“形似神不至”。

### 15.1 agency_charter Shadow

已有：

```text
backend/app/tools/handlers/hr.py
  HR refine schema includes role_description, personality, boundaries,
  primary_users, core_outputs, quality_standards, first_tasks.

backend/app/services/agent_manager.py
  renders soul.md with Identity & Mission, Operating Style,
  Boundaries & Red Lines.
```

缺口：

- 无 `agency_charter`。
- 无 `company_charter` / organization-level charter projection。
- 无 `archetype`。
- boundaries 是 deny-list，不是授权书。
- agent 不知道“我代理谁、属于哪个公司秩序、强度多大、什么时候要回头确认或上升到公司边界”。

### 15.2 feedback_signal Shadow

已有：

```text
backend/app/memory/t2_store.py
  T2 metadata already supports weight/source/category/evidence/confidence/
  volatility/source_refs/novelty/reusability.
```

缺口：

- `cat=feedback` 只是分类。
- 无 `reaction` / `polarity`。
- heartbeat 无法区分 owner 是认可、否定、纠正还是仅陈述事实。

### 15.3 First-Person Soul Shadow

已有：

```text
backend/app/services/agent_manager.py
  renders durable soul.md.

backend/app/runtime/prompt_sections/identity.py
  injects soul_text into prompt.
```

缺口：

- 现在多为第二人称 / 描述性 / imperative 风格。
- 缺第一人称身份内化。
- `### Personality` 包装会把 soul 继续当“描述”，而不是 agent 自述。

### 15.4 Boundary Axis Shadow

已有：

```text
backend/app/runtime/prompt_sections/executing_actions.py
  tells agent to refuse requests violating soul.md boundaries.
```

缺口：

- 这是撞到红线后的拒绝，不是动作前的多维预判。
- 没有 reversibility / representativeness / judgment_density / visibility / domain_specialization。
- 也没有把这些维度传入 runtime preflight。

### 15.5 Lifecycle Shadow

已有：

```text
backend/app/memory/md_store.py
  写 T2 / T3 markdown 段；rebuild_index 维护 INDEX.md。

backend/app/services/auto_dream.py
  dream consolidation 直接修改 T3 文件内容（auto_dream.py:117）。
```

缺口：

- T0 / T2 / T3 entry 没有 `id / version / parent_id / supersedes / superseded_by / status / expires_at / access_count` 字段。
- dream 是**覆盖式**更新；soul promotion 后无 audit lineage，无法回滚。
- 没有 sketch 区——agent 主动判断 / dream 提案直接进 active 池，未被 feedback 强化也不会过期。
- `retriever.py:267` 没有访问反哺：被检索命中的 entry 不会自动累积 `access_count`。
- `auto_dream.py:229` 只有负面 anti-pattern，缺正向 scoring（novelty / reusability / charter_alignment）。

### 15.6 Decision Trace Shadow

已有：

```text
backend/app/kernel/engine.py
  multi-round LLM loop；tool_calls 写入 chat-*.md。

backend/app/services/extract_agent.py
  T0 -> T2 提取，含 [cat=feedback]。
```

缺口：

- tool_call 在 `chat-*.md` 里 inline，但**没有结构化** `reasoning / alternatives_considered / situational_factors / charter_zone`。
- preflight 不存在；5 维边界轴只在 prompt 文字提示，不落 entry。
- feedback 与具体 decision 之间没有 `refs` 显式链路——retriever 无法把 “owner approved X” 反向定位到那次 X 的当时考虑。
- dream 只能从“结果”里学，看不到“决策路径”。

### 15.7 Sensitivity Shadow

已有：几乎为零。

```text
backend/app/memory/user.md
  存在但与其它 T3 段同等待遇。
```

缺口：

- 无 PL1–PL4 分级；无 `sensitivity` frontmatter 字段。
- `services/extract_agent.py` 是单一 LLM call，**没有 privacy_extractor 角色**——PII / 凭证 / 高敏内容直接进 T0 / T2 / T3。
- 没有 typed placeholder / PrivacyStore 反向映射。
- channel adapter（feishu / slack / mail）出站前**没有 redact**，PL3 内容可能被直接外发。
- `retriever.py:267` P0 注入不按 principal stack 应用 strip。

### 15.8 Coordination Shadow

已有：

```text
backend/app/agents/delegate_to_agent.py
  同步函数调用 + SessionContext(source="agent", core_tools_only=True)。

backend/app/services/approval.py
  现有 approval flow（流程式，非对象式）。

backend/app/services/trigger_daemon.py
  定时 / 事件触发 heartbeat。
```

缺口：

- 无 `Lease`：两个 agent 接同一 task 会重复执行，没有互斥。
- 无 `Signal`：A 委派 B 后是同步等待；B 中途想反问 A 没有 thread-aware inbox。
- `agency_charter` 的 confirm_first 没有落到具名 `Checkpoint` 对象；approval 流程结束后无法被 dream 引用做 charter calibration。
- `trigger_daemon` 触发后只能跑 heartbeat，**不能升级为 Signal 或 Checkpoint**（缺 Sentinel 抽象）。
- 没有 timeout → company tier 自动 escalation 通道。

### 15.9 Form Contract Shadow

已有：

```text
backend/app/services/extract_agent.py
  free-form markdown 段落抽取。
```

缺口：

- 没有“禁止代词、禁止相对时间、显式 actor/target/location/数值单位”的写入约束。
- 多 agent 跨 session 引用某条 learning 时，常因代词或相对时间失去意义。
- `md_store.py` 写入路径上没有 lint。

## 16. Engineering Direction

第一阶段不应该推翻现有 T0/T2/T3/soul，而应该把人格构造层、动态认知层和 runtime preflight 接上。

### 16.1 New Conceptual Modules

```text
backend/app/services/principal_context.py
  Principal stack model: platform governance, company, owner, creator,
  current user, delegating agent, current request scope.

backend/app/services/agency_charter.py
  Archetype inference, default owner agency charter generation,
  company charter overlay, owner/company editable charter schema,
  frozen/mutable soul section handling.

backend/app/memory/activation.py
  Dynamic activation scoring for memories, objectives, relationships,
  feedback signals, charter zones, and open loops.

backend/app/memory/understanding_store.py
  Durable understandings with evidence refs, confidence, contradiction history.

backend/app/services/action_preflight.py
  5-axis action risk judgment + owner charter zone + company boundary +
  runtime permission classification.

backend/app/services/proactive_employee.py
  Controlled proactive loop: observe, prioritize, prepare, preflight,
  act-or-ask, record evidence.

backend/app/memory/lifecycle_store.py
  Sketch / active / superseded / archived state machine; version chain;
  supersedes lineage; expires_at handling for sketches.

backend/app/memory/retention.py
  Retention formula: salience * exp(-lambda * delta_t) + sigma * access_boost
  + rho * feedback_reinforce; hot / warm / cold tiering; access_log writer.

backend/app/services/decision_trace.py
  DecisionTrace schema; reasoning / alternatives_considered / situational_factors
  capture; refs link to feedback_signal; consume by dream for charter calibration.

backend/app/services/privacy_layer.py
  PL1-PL4 classifier (privacy_extractor role); typed placeholder substitution;
  PrivacyStore reverse map; retriever-side strip by principal stack;
  channel adapter outbound redact.

backend/app/agents/coordination.py
  Lease + Signal + Checkpoint + Sentinel primitives; charter_zone -> runtime
  path mapping; timeout -> company tier escalation chain.

backend/app/memory/form_lint.py
  Self-contained form contract enforcement: no pronouns, no relative time,
  explicit actor / target / location / units. Used by extract_agent and md_store.
```

### 16.2 Existing Modules To Reconnect

```text
backend/app/tools/handlers/hr.py
  Add archetype inference, company context, and agency_charter output to soul refinement.

backend/app/services/agent_manager.py
  Render first-person soul, frozen Company Charter, and frozen Agency Charter sections.

backend/app/runtime/prompt_sections/identity.py
  Preserve first-person soul framing; avoid turning it into generic personality notes.

backend/app/memory/t2_store.py
  Add reaction/polarity metadata for feedback_signal.

backend/app/services/extract_agent.py
  Extract feedback_signal from owner reactions with an unclear fallback.

backend/app/memory/retriever.py
  Candidate retrieval should be followed by owner/company-aware activation rerank.

backend/app/memory/assembler.py
  Prompt assembly should explain why memories were activated.

backend/app/services/objective_intake.py
  Objective extraction should feed long/short-term goal bridge, not only ledger rows.

backend/app/services/heartbeat.py
  Heartbeat should evolve from pure curation into proactive employee check-in mode.

backend/app/services/agent_context.py
  Context should include principal stack, company charter, owner charter, and relationship understanding,
  not only static relationship files.

backend/app/services/t0_logger.py
  T0 writers must call privacy_layer.classify() before persisting; attach
  sensitivity frontmatter; enforce form_lint on the rendered entry body.

backend/app/services/extract_agent.py
  Split into privacy_extractor + learning_extractor; emit form-contract-compliant
  entries (no pronouns, absolute timestamps); attach sensitivity + lifecycle fields.

backend/app/memory/md_store.py
  Persist lifecycle frontmatter (id / version / parent_id / supersedes /
  superseded_by / status / expires_at / access_count); refuse writes that fail
  form_lint; bump access_count on retriever hits.

backend/app/services/auto_dream.py
  Stop in-place rewrite of T3 / soul. Produce a new version entry; mark prior
  entries superseded; emit dream proposals into sketch buffer for owner approval;
  add positive scoring (novelty / reusability / charter_alignment) next to
  existing anti-pattern list.

backend/app/services/approval.py
  Materialize approval flows into Checkpoint objects with approver, deadline,
  escalation chain; link Checkpoint id back to the originating DecisionTrace.

backend/app/services/trigger_daemon.py
  Generalize triggers into Sentinel primitives (webhook / timer / threshold /
  pattern); on fire, choose between Signal injection and Checkpoint creation.

backend/app/agents/delegate_to_agent.py
  Wrap delegation with Lease acquire; create a Signal channel between caller and
  callee; surface progress and rollback through Signal rather than synchronous
  return only.

backend/app/api/feishu*.py, backend/app/api/slack*.py, ... (all channel adapters)
  Outbound redact pass: never emit PL3 / PL4 content over external channels;
  PL2 outbound only when typed placeholder unmasking is explicitly authorized.
```

## 17. Implementation Order

Implementation order should follow one rule:

```text
Build the memory safety substrate before making agents look more autonomous.
```

If HR personality and first-person soul are built first, the product will feel better but the runtime will still lack privacy, lineage, form, activation, and owner/company accountability. That creates a dangerous illusion: the agent sounds like an elite employee before it has the control plane required to behave like one.

### Phase 0: Canonical Contract

Goal: make the target explicit without runtime behavior changes.

- Finalize this document.
- Define `MemoryControlPlane` as the umbrella contract over existing T0/T1/T2/T3/soul.
- Define `AgencyCharter` schema.
- Define `CompanyCharter` schema.
- Define `PrincipalStack` schema.
- Define `FeedbackSignal` metadata fields.
- Define `ActionPreflight` decision contract.
- Define frozen vs mutable soul sections.
- Define `DecisionTrace` schema (reasoning / alternatives / situational_factors / charter_zone / preflight / sensitivity).
- Define memory `Lifecycle` fields (id / version / parent_id / supersedes / superseded_by / status / expires_at / access_count).
- Define `RetentionScore` formula constants (lambda, sigma, rho) per category.
- Define `SensitivityLabel` set (PL1_public / PL2_pii / PL3_sensitive / PL4_credential) and per-level default policy table.
- Define `Coordination` primitives schema (Lease / Signal / Checkpoint / Sentinel).
- Define Form Contract lint rules.

### Phase 1: Memory Safety Substrate

Goal: make every newly written memory safe, attributable, and inspectable before changing agent behavior.

- Implement `services/principal_context.py` with platform / company / owner / creator / current user / delegating agent resolution.
- Implement `services/privacy_layer.py` with `privacy_extractor`, PL1-PL4 classification, typed placeholders, and PrivacyStore reverse mapping.
- Implement `memory/form_lint.py` and enforce self-contained write rules for new memory entries.
- Add `sensitivity`, `evidence_refs`, `status`, `version`, `parent_id`, `supersedes`, `superseded_by`, `expires_at`, `access_count`, and `last_accessed` metadata to new T1/T2/T3 entries.
- Implement `memory/retention.py` and an access log writer.
- Reject PL4 writes before any memory layer; store only typed placeholder metadata when needed for audit.
- Do not backfill old memory in this phase. Forward enforcement is enough.

### Phase 2: Activation and Retrieval Control

Goal: make memory retrieval owner-aware, company-aware, goal-aware, and sensitivity-aware.

- Implement `memory/activation.py`.
- Candidate retrieval still reads current T0/T2/T3/soul-compatible stores.
- Rerank candidates using objective relevance, principal relevance, company relevance, relationship impact, open-loop pressure, consequence weight, recency, reinforcement, feedback, charter relevance, confidence, retention score, contradiction penalty, and sensitivity strip.
- Apply `sensitivity_strip` based on current `PrincipalStack`.
- Include activation reasons in prompt assembly for debuggability.
- Bump `access_count` and `last_accessed` for activated entries, not for every candidate.

### Phase 3: Decision Trace and Feedback Learning

Goal: let agents learn from owner/company approval, rejection, correction, and unclear reactions by linking feedback back to the decision that created it.

- Implement `services/decision_trace.py` and the `DecisionTrace` schema from Phase 0.
- Add `reaction`, `polarity`, `source`, and `rationale_from_owner` to feedback entries.
- Update `extract_agent.py` to classify feedback as `approved | rejected | questioned | corrected | unclear`.
- Use `reaction=unclear` when the owner signal is ambiguous. Do not infer approval from silence.
- Have high-risk, confirm-first, external-visible, irreversible, or sensitive actions emit a `DecisionTrace`.
- Attach `refs=decision/<id>` when later feedback maps to a prior decision.
- Extend `auto_dream.py` to read `decision_trace.refs <- feedback_signal` chains for charter calibration proposals.

### Phase 4: HR Creation and First-Person Charter

Goal: make newly created agents visibly feel like company elite employees, after memory safety and activation are in place.

- Add archetype inference in HR soul refinement.
- Generate default agency charter.
- Inject company charter overlay.
- Expose charter to owner for editing before final creation.
- Render first-person soul with frozen Company Charter and Agency Charter.
- Keep capability packs separate from archetype.
- Soul / charter mutation must go through sketch -> owner-approved -> active.

### Phase 5: Action Preflight

Goal: enforce boundary sense before tools/actions.

- Implement 5-axis action preflight.
- Map preflight result to `do | prepare_only | ask | refuse`.
- Include company-boundary conflict detection and escalation result.
- Keep hard runtime/tool governance as final gate.
- Log preflight results into `DecisionTrace`.
- Make company charter conflicts escalate or refuse instead of being silently treated as owner preference.

### Phase 6: Coordination Primitives Runtime

Goal: `agency_charter` zones are enforced by runtime, not just prompt.

- Implement `agents/coordination.py` with `Lease / Signal / Checkpoint / Sentinel`.
- Wrap `delegate_to_agent` with Lease acquire + Signal channel.
- Object-ify `approval.py` into Checkpoint with deadline + escalation chain.
- Generalize `trigger_daemon.py` into Sentinel primitives.
- Map charter zone -> coordination path: `full_authority` -> Lease+Act, `confirm_first` -> Checkpoint, `never_do` -> Refuse+Audit.
- Link Lease / Signal / Checkpoint / Sentinel ids back to DecisionTrace and evidence refs.

### Phase 7: Proactive Employee Loop

Goal: evolve heartbeat from curation-only into controlled proactive employee check-in.

- Observe objectives, company charter, owner charter, open loops, recent feedback, relationship changes, Sentinel events, and Signal inbox.
- Interpret which items matter to owner and company now.
- Prepare low-risk artifacts, drafts, checks, summaries, and options.
- Run preflight before any action.
- Act only in full-authority zones; ask through Checkpoint in confirm-first zones; refuse never-do zones.
- Record evidence and owner/company reaction.

### Phase 8: Eval-Driven Memory Policy Evolution

Goal: make retrieval and memory policy improve from evidence instead of hand-tuned constants.

- Add telemetry for zero-hit retrievals, over-broad retrievals, owner corrections, sensitivity strips, and activation reasons.
- Build a replay set from anonymized decision traces and feedback signals.
- Tune activation weights, retention constants, context budgets, and retrieval profiles through guarded experiments.
- Auto-revert policy changes when replay quality drops.
- Keep policy evolution bounded: it may change retrieval weights and budgets, not company charter or owner authorization rules.

## 18. Acceptance Criteria

This design is not complete until these conditions are true:

- PL4 credentials never enter T0/T1/T2/T3/soul. They are rejected or represented only as typed placeholders with restricted reverse mapping.
- Every active memory has evidence refs, sensitivity, lifecycle status, and enough form to stand alone outside its original context.
- A retrieved memory can explain why it was activated: objective, principal, company, relationship, feedback, risk, or open-loop reason.
- A contradicted memory is versioned and down-ranked, not overwritten or silently deleted.
- Dream and heartbeat never rewrite T3 / soul in place. They produce sketch or new-version proposals.
- Frozen soul and charter changes require owner approval before becoming active.
- Feedback can link back to a DecisionTrace through `refs=decision/<id>`.
- Company charter conflicts cannot be downgraded to owner preference. They must escalate, ask, or refuse.
- Cross-agent delegation uses Lease and Signal, not only synchronous function calls.
- Confirm-first actions produce Checkpoint objects with approver, deadline, escalation chain, and trace linkage.
- Channel adapters apply outbound redact and cannot emit PL3 / PL4 content by accident.
- Retrieval policy changes are evaluated through replay or telemetry before becoming default.

## 19. Tests To Write First

```bash
cd /Users/rocky243/vc-saas/hiveclaw-main/backend
source .venv/bin/activate

pytest \
  tests/memory/test_memory_control_plane_contract.py \
  tests/services/test_agency_charter.py \
  tests/services/test_principal_context.py \
  tests/services/test_principal_stack.py \
  tests/services/test_action_preflight.py \
  tests/memory/test_feedback_signal.py \
  tests/memory/test_activation_scoring.py \
  tests/memory/test_activation_reasons.py \
  tests/memory/test_sensitivity_strip.py \
  tests/memory/test_access_log.py \
  tests/memory/test_understanding_store.py \
  tests/runtime/test_prompt_memory_activation.py \
  tests/services/test_proactive_employee.py \
  tests/services/test_decision_trace.py \
  tests/memory/test_lifecycle_state_machine.py \
  tests/memory/test_retention_formula.py \
  tests/memory/test_form_contract.py \
  tests/services/test_privacy_layer.py \
  tests/agents/test_coordination_primitives.py \
  tests/memory/test_policy_evolution_guard.py
```

Expected coverage:

- owner-stated preference outranks generic recent memory.
- company boundary outranks owner preference when they conflict, and produces escalation instead of silent refusal.
- unresolved open loop outranks older completed episode.
- high-risk boundary suppresses external action even when objective relevance is high.
- creator/owner/current-user mismatch produces explicit principal context.
- agent delegation can create a temporary principal frame without replacing direct owner accountability.
- direct owner accountability remains nested inside company responsibility.
- contradicted understanding is versioned and down-ranked, not silently overwritten.
- negative owner feedback produces a boundary/strategy signal.
- ambiguous feedback uses `reaction=unclear`, not guessed approval/rejection.
- first-person soul preserves Agency Charter framing in prompt.
- heartbeat can propose a proactive employee action without taking unauthorized external action.
- a `confirm_first` action emits a DecisionTrace with reasoning + at least one alternative considered.
- feedback_signal with `refs=decision/<id>` correctly links back and influences charter calibration in dream.
- dream produces a new-version entry and marks the prior entry `superseded` rather than overwriting.
- a sketch entry expires and is discarded after `expires_at` if not reinforced.
- retention_score formula returns a hot value for recently-accessed + feedback-approved entries.
- privacy_extractor classifies a credential as PL4 and the entry never lands in any memory layer.
- a PL3 entry is suppressed by `sensitivity_strip` when current_user != direct_owner.
- channel adapter refuses to emit PL3 content even if the agent asks it to.
- a Form Contract violation (pronoun / relative time) is rejected at md_store write time.
- two delegations targeting the same task acquire Lease serially, not concurrently.
- a Checkpoint left unanswered past deadline auto-escalates to company_admin.

## 20. Open Questions

| Question | Tension |
|---|---|
| First-person soul vs dream updates | Which sections are frozen, owner-editable, dream-suggested, or dream-mutable? |
| Feedback polarity stability | How reliably can extraction classify approval/rejection when owner is indirect? |
| Multi-principal delegation | When agent A delegates to agent B, does B temporarily serve A, owner, or both? |
| Owner vs company conflict | Which company charter conflicts require refusal, which require owner confirmation, and which require admin approval? |
| Review ritual | Should calibration be a UI entry, automatic prompt, or owner-triggered workflow? |
| Boundary thresholds | Should 5-axis thresholds be numeric, enum, or natural-language charter clauses? |
| Archetype vs capability pack | Archetype is personality/work style; capability pack is technical ability. How should they coordinate without coupling? |
| Cross-agent playbook | How to build “同类经验” without leaking tenant data? |
| Semantic episode closure | Can AgentGal-style topic closure work for enterprise tasks, or should it be objective/result based? |
| Decision trace granularity | What counts as a “non-trivial” decision worth a trace? Per-tool whitelist, per-zone rule, or LLM self-judgment? |
| Sketch expiry policy | Default expires_at per archetype, or learned from feedback latency distribution? |
| Sensitivity false-negative cost | What is the safe-side bias when privacy_extractor is uncertain — escalate to PL3 by default, or to PL2? |
| Retention constants | Are lambda / sigma / rho global, per-tenant, per-archetype, or learned? How to evaluate without leaking memory drift? |
| Approval migration | How to migrate the current `services/approval.py` flows into Checkpoint objects without breaking in-flight approvals? |
| Retriever self-evolution | Which retrieval parameters are safe to tune automatically, and which require owner/company review? |
| Episode segmentation (enterprise) | Should T0 be sub-segmented per session by topic boundary (MAGMA-style) when one chat covers multiple projects, or should objective/result boundaries dominate? |
| LLM-planned retrieval | Should `retriever.py` evolve to intent-classification + parallel multi-view + adequacy reflection (SimpleMem-style), and how should failures be replayed safely? |

## 21. Non-Goals

This design does not mean:

- every memory must become graph DB immediately;
- agent can freely rewrite its soul;
- heartbeat should perform arbitrary external work;
- static T3 files should be deleted;
- owner accountability should bypass company charter or platform governance;
- autonomy should replace explicit authorization;
- archetype should automatically grant tools;
- first-person soul should replace runtime permission checks;
- external memory projects should be imported wholesale as Hive's source of truth.

The goal is to add directionality and judgment, not to remove safety.

## 22. Summary

Hive 当前的 memory system 已经有长期沉淀能力，但要成为公司语境下 owner 理想中的精英员工型 agent，还需要从“记忆分层系统”升级为 **Memory Control Plane**：

```text
Memory Control Plane
  = principal stack
  + privacy / sensitivity gate
  + self-contained memory form
  + lifecycle / versioning
  + retention / activation
  + first-person identity
  + company charter
  + owner agency charter
  + relationship / decision graph
  + decision trace + feedback learning
  + action preflight (5-axis)
  + coordination primitives (Lease / Signal / Checkpoint / Sentinel)
  + controlled proactivity
  + eval-driven policy evolution
```

核心变化是：

```text
from: store and compress context
to: understand owner, company, goals, relationships, boundaries,
    and activate the right memory at the right time
```

真正的精英员工型 agent 应该做到：

- 稳定地记住 owner 的长期偏好和边界；
- 稳定地记住 company / organization 的共同利益、合规边界、数据边界和声誉边界；
- 用第一人称内化“我是哪个公司的精英员工、我直接支持谁、我能做什么、何时必须问或上升审批”；
- 灵活地根据当前目标激活相关记忆；
- 理解人、人和 agent、agent 和 agent 之间的关系变化；
- 主动发现 open loops 和风险；
- 在权限边界内准备、推进、提醒；
- 在高风险处停下来请示；
- 用 evidence 和 owner feedback 更新 understanding，而不是用单次上下文改写自我；
- 写下来的记忆是自包含的，跨上下文 / 跨 agent 引用不丢意义；
- 做决定时留下 reasoning + alternatives + situational_factors 的可追溯 trace，反馈通过 refs 链回这条 trace；
- 每条记忆和每个 action 都有 sensitivity 等级，PL3 / PL4 不会被错误外发；
- 改变 soul / 改变 charter 必经过 sketch → owner-approved → active 链路，旧版本保留可回滚；
- agent ↔ agent 协作走 Lease + Signal + Checkpoint，charter zone 决定 runtime 路径，agent 不能仅靠默念契约绕过审批。

这才是“能力很强但有边界感”的 agent。
