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

## 17. Runtime-Gated Implementation Order

Implementation order still follows the safety-first rule:

```text
Build the judgment substrate before making agents look more autonomous.
```

The earlier phase list was directionally right, but too module-oriented. A phase is **not complete** when a file or class exists. A phase is complete only when the production runtime uses it, tests cover the intended boundary, and the behavior has a practical smoke proving that it helps the agent act more like a company elite employee.

### 17.1 Phase Completion Contract

Every phase must close with an evidence packet:

```text
Phase Evidence Packet =
  Code paths changed
  + Runtime entrypoint wired
  + Red/green tests
  + Practical utility smoke
  + Safety/regression notes
  + Documentation update in this section
```

Required fields:

| Field | Meaning |
|---|---|
| `Runtime gate` | The production path that now must pass through this capability. |
| `Tests` | Exact test command and pass/fail output. |
| `Utility smoke` | A practical example showing the phase helps owner/company judgment, not just unit correctness. |
| `Evidence refs` | File paths, test names, deployment id if deployed, or log/query evidence if runtime behavior was checked. |
| `Residual risk` | What is still intentionally out of scope. |

If a phase changes behavior that can call an LLM, experiments may use the configured DeepSeek V4 Pro runtime **only through a temporary environment variable** such as `DEEPSEEK_API_KEY`. Raw API keys must never be committed, logged, written to docs, or stored in memory.

### Phase 1: Identity and Charter Substrate

Goal: define who the agent serves, which company context it belongs to, and which charter boundaries govern its judgment.

- Implement `services/agency_charter.py` with `CompanyCharter`, `OwnerAgencyCharter`, and a resolved `AgentAccountabilityContext`.
- Extend `PrincipalStack` usage so `direct_owner`, `company`, `creator`, `current_user`, and `delegating_agent` are explicit inputs to memory/action decisions.
- Define charter zones as executable data: `full_authority`, `confirm_first`, `never_do`.
- Provide a safe default charter for existing agents when no explicit charter exists.
- Runtime gate: memory activation and action preflight can request a resolved accountability context instead of inventing identity from prompt text.
- Utility smoke: given owner Alice and company Acme, the system can explain that the agent directly supports Alice while still escalating Acme charter conflicts.

### Phase 2: Memory Write Safety

Goal: every newly written memory is safe, attributable, self-contained, and inspectable before it enters any durable layer.

- Apply `privacy_layer.classify_and_mask()` before new T0/T1/T2/T3 writes.
- Reject PL4 credentials before persistence.
- Apply `form_lint` to agent-authored durable facts.
- Persist `sensitivity`, `evidence_refs`, `status`, `version`, `parent_id`, `supersedes`, `superseded_by`, `expires_at`, `access_count`, and `last_accessed` metadata for new entries.
- Add retention/access logging for activated entries.
- Runtime gate: `save_memory`, extract/write paths, and future T0/T2 writers must all use the same safety substrate.
- Utility smoke: an API key is rejected; a self-contained owner preference is stored with sensitivity/status/version metadata.

### Phase 3: Memory Read and Activation Runtime

Goal: prompt memory retrieval is owner-aware, company-aware, goal-aware, and sensitivity-aware by default.

- Build `ActivationContext` from the resolved accountability context, current objective/focus, company terms, owner terms, and current query.
- Production `memory_service.build_memory_context()` must pass `activation_context` into `MemoryRetriever.retrieve()`.
- Apply `sensitivity_strip` based on the current `PrincipalStack`.
- Include activation reasons in prompt assembly.
- Bump `access_count` and `last_accessed` only for activated entries.
- Runtime gate: every prompt memory context assembled for an agent turn goes through activation unless explicitly running in a legacy compatibility mode.
- Utility smoke: PL3 memory is omitted when current user is not the direct owner; the direct owner can activate it, and prompt output includes `why=...`.

### Phase 4: Decision Trace and Action Preflight Loop

Goal: every non-trivial action has a preflight judgment before execution and can later explain why it acted, asked, escalated, or refused.

- Wire `ActionPreflightService` into tool/external-action execution boundaries.
- Map the 5 boundary axes plus charter zone, company conflict, runtime permission, and sensitivity into `do | prepare_only | ask | refuse | escalate`.
- Emit `DecisionTrace` for confirm-first, never-do, external-visible, irreversible, sensitive, or company-conflict actions.
- Log preflight output into the trace.
- Runtime gate: high-risk tool/action calls cannot bypass preflight even if prompt text says they are allowed.
- Utility smoke: an external vendor reply becomes `ask`; a company charter conflict becomes `escalate`; a low-risk local summary becomes `do`.

### Phase 5: Feedback Learning and Dream Calibration

Goal: owner/company reactions update future judgment by linking feedback back to the decision that created it.

- Add `reaction`, `polarity`, `source`, and `rationale_from_owner` to feedback entries.
- Update `extract_agent.py` to classify feedback as `approved | rejected | questioned | corrected | unclear`.
- Use `reaction=unclear` when the signal is ambiguous; never infer approval from silence.
- Attach `refs=decision/<id>` when feedback maps to a prior decision.
- Extend `auto_dream.py` to consume decision-feedback chains and propose charter/memory calibration.
- Runtime gate: dream can propose boundary changes only from traced decisions plus explicit feedback.
- Utility smoke: repeated approval can propose moving an action toward `full_authority`; rejection proposes tightening to `confirm_first`.

### Phase 6: First-Person Soul and HR Creation

Goal: newly created agents visibly and operationally feel like company elite employees after the safety and activation substrate exists.

- Add archetype inference in HR soul refinement.
- Generate default company charter and owner agency charter.
- Expose charter fields to owner/admin before final creation.
- Render first-person soul with frozen Company Charter and Agency Charter sections.
- Keep capability packs separate from archetype/personality.
- Soul/charter mutation must go through sketch -> owner-approved -> active.
- Runtime gate: new agent creation cannot silently create an agent with no accountability context.
- Utility smoke: a new agent's first-person identity states the company, direct owner, allowed autonomy, confirm-first boundaries, and never-do boundaries.

### Phase 7: Coordination Runtime

Goal: `agency_charter` zones are enforced by runtime coordination primitives, not just prompt text.

- Wire `Lease` into cross-agent delegation so duplicate work is serialized.
- Wire `Signal` into delegation progress/handoff instead of synchronous return only.
- Object-ify approval flows into `Checkpoint` with approver, deadline, escalation chain, and trace linkage.
- Generalize triggers into `Sentinel` primitives that can produce Signal or Checkpoint events.
- Map charter zone to runtime path: `full_authority -> Lease + Act`, `confirm_first -> Checkpoint`, `never_do -> Refuse + Audit`.
- Runtime gate: `delegate_to_agent`, approval, and trigger execution all use coordination primitives for governed work.
- Utility smoke: two agents cannot acquire the same task lease at once; confirm-first action creates a checkpoint and escalates on timeout.

### Phase 8: Proactive Employee Loop and Eval-Driven Policy Evolution

Goal: heartbeat evolves from curation-only into a controlled proactive employee loop, and memory policy improves from replay evidence rather than hand-tuned constants.

- Observe objectives, company charter, owner charter, open loops, recent feedback, relationship changes, Sentinel events, and Signal inbox.
- Interpret owner/company importance and prepare low-risk artifacts first.
- Run preflight before any proactive action.
- Act only in full-authority zones; ask via Checkpoint in confirm-first zones; refuse never-do zones.
- Add telemetry for zero-hit retrievals, over-broad retrievals, owner corrections, sensitivity strips, and activation reasons.
- Build replay sets from anonymized decision traces and feedback signals.
- Tune activation weights, retention constants, context budgets, and retrieval profiles through guarded experiments.
- Auto-revert policy changes when replay quality drops.
- Runtime gate: proactive behavior must always produce evidence and must not mutate charter/company rules automatically.
- Utility smoke: heartbeat can prepare a low-risk follow-up draft from an open loop, but external sending requires checkpoint approval.

### Phase 9: Channel Outbound Privacy Redact

Goal: PL3 / PL4 content cannot reach an external channel even when an
agent or proactive loop tries to emit it.

- Add `services/outbound_privacy.py` with `redact_outbound()` that classifies
  text via `PrivacyLayer` and rejects PL4, strips PL3 to `[REDACTED_PL3]`
  unless an owner-private channel + direct-owner principal is in scope, and
  replaces PL2 with typed placeholders.
- Wire `ChannelDeliveryService.send_text` through the redact gate so every
  unified channel send (Feishu / Telegram / WeCom / WeChat / Web) shares the
  same final-mile policy. Rejected sends return `status="denied"` with a
  structured `outbound_sensitivity` log field.
- Runtime gate: any agent-triggered or proactive-loop-triggered outbound
  text now passes through the redact gate; bypassing requires editing
  `ChannelDeliveryService` itself.
- Utility smoke: a credential-shaped string never leaves Hive (status
  `denied`, executor not called); a salary mention bound for a vendor becomes
  `[REDACTED_PL3]`; an owner-private web reply preserves PL3 content.

### Phase 10: T0 Privacy Frontline + Form Lint

Goal: every behavior log written to disk has credentials masked, carries
sensitivity, and surfaces form-contract warnings so downstream T2 extract
knows when to rewrite before promoting.

- Wire `PrivacyLayer.classify_and_mask()` into `services/t0_logger.write_t0_log`
  after the formatter so PL4 strings become `<Credential_N>` placeholders
  before the MD file is written.
- Inject `t0_sensitivity` into the YAML frontmatter for every behavior /
  system T0 file; add `t0_form_warnings` with `ambiguous_pronoun` and/or
  `relative_time` codes when `form_lint` flags the rendered body.
- Runtime gate: every T0 file written through `write_t0_log` now passes
  the privacy gate; the helper `_apply_t0_privacy_gate()` is the chokepoint.
- Utility smoke: a chat transcript with `api_key=sk-...` ends up with the
  literal key replaced by `<Credential_1>` and `t0_sensitivity: PL4_credential`
  in the frontmatter; a salary mention becomes `t0_sensitivity: PL3_sensitive`;
  Chinese "他昨天说要下周再聊" yields `t0_form_warnings: [ambiguous_pronoun,
  relative_time]`.

### Phase 11: Understanding Store + Relationship Memory

Goal: relationship-shaped knowledge becomes a first-class durable node
with evidence refs, confidence, and contradiction history, instead of
being scattered across T2/T3 free-form bullets.

- Add `memory/understanding_store.py` with `UnderstandingEntry` (subject,
  object, relation_type, current_understanding, evidence_refs, confidence,
  last_confirmed_at, contradiction_history, open_questions, boundaries).
- Persist as YAML-fronted `understandings.md` blocks under
  `workspace/{agent}/memory/` so the store survives process restarts and
  owners can read it.
- Support `record`, `get`, `query(subject/object/relation)`, `contradict`
  (creates a new entry, links both ways, halves the original confidence
  without deleting it), and `decayed_confidence(now)` (confidence stays
  flat inside a 30d window, then decays linearly).
- Runtime gate: the store is available to dream / retriever as a future
  read source; the substrate is durable today.
- Utility smoke: recording `agent_a -[collaborator]-> agent_b` then
  contradicting it preserves both entries with cross-links and lowers the
  older confidence; reloading the store from disk returns the same
  entries.

### Phase 12: HR Archetype Inference + Defaults

Goal: new agents always start with a non-blank Full Authority / Confirm
First / Never Do contract even when the owner declines to write a custom
charter. Archetype is the "elite employee playbook floor" referenced in §7.

- Add `services/archetype.py` with `Archetype` enum (chief_of_staff,
  research_analyst, customer_success, ops_admin, engineering_assistant,
  vendor_liaison, generalist), `infer_archetype()` keyword classifier,
  and `default_owner_charter` / `default_company_charter` defaults.
- Wire `apply_archetype_defaults(blueprint)` into the HR blueprint
  preview path so missing charter keys fall back to archetype defaults
  while explicit owner-supplied lists pass through unmodified.
- Expose `archetype` on the blueprint payload so the soul renderer and
  approval UI can show which playbook was assumed.
- Runtime gate: HR preview / `create_digital_employee` cannot emit a
  blueprint without a populated company + owner charter once this phase
  is wired.
- Utility smoke: an HR refine with `role_description="research analyst"`
  but no charter still ships with `confirm_first` containing "Publish any
  external memo"; an explicit charter from the owner is preserved
  verbatim; missing roles fall back to `generalist` with safe defaults.

### Phase 13: Access-Count Writeback to Markdown

Goal: every retriever hit accumulates durable evidence on the entry it
came from, so the retention formula (§6.3) and dream/auto-tune can read
real usage signals instead of in-memory counters that vanish on restart.

- Add `memory/access_log.py` with `bump_access(data_root, agent_id,
  file_relpath, entry_id, now=None) -> bool` that rewrites the matching
  T3 markdown line's `[access_count=...]` and `[last_accessed=...]`
  fields atomically.
- Wire `_apply_activation()` in `memory/retriever.py` so every activated
  (non-suppressed) item with an `entry_id` and `source` triggers a
  writeback. Failures degrade to a logger.debug.
- Runtime gate: any memory item that survives `ActivationScorer.score`
  contributes to the persisted access log; suppressed (PL3 strip) items
  do not.
- Utility smoke: a T3 entry with `[access_count=0]` retrieved three
  times reaches `[access_count=3]` on disk with the latest UTC
  `last_accessed` timestamp; unknown `entry_id` returns False and leaves
  the file untouched; missing file returns False.

### Phase 14: Persistent Coordination + Approval Queue (PostgreSQL)

Goal: Lease, Signal, Checkpoint state survives process restart and is
visible across every Hive worker in the same tenant, so confirm-first
approvals are durable and duplicate cross-worker delegations cannot
race for the same task.

- Add `models/coordination.py` with `CoordinationLease`,
  `CoordinationSignal`, `CoordinationCheckpoint` SQLAlchemy models.
  Each table has a `tenant_id` foreign key to `tenants`, with
  `UNIQUE(tenant_id, task_key)` on the lease table.
- Add alembic migration `coordination_charter_0522` to create the
  tables + indexes.
- Add `agents/coordination_repository.py` with
  `CoordinationRepository(session, tenant_id)`. API mirrors the
  in-process `CoordinationRuntime`: `acquire_lease`, `send_signal`,
  `read_signals`, `create_checkpoint`, `get_checkpoint`,
  `escalate_expired_checkpoints`.
- Lease acquisition uses PostgreSQL `INSERT ... ON CONFLICT
  (tenant_id, task_key) DO UPDATE WHERE expires_at <= NOW()` so the
  expiry check and the insert happen in one statement, removing the
  TOCTOU race a two-statement sqlite shim left open.
- Sentinel state is intentionally not persisted — it is re-derived per
  proactive-loop tick.
- Runtime gate: code paths that need cross-worker correctness
  instantiate `CoordinationRepository(session, tenant_id)` inside the
  request scope where `get_db()` has already set the tenant GUC for RLS.
  The in-process `CoordinationRuntime` remains for single-process /
  test usage; the prior `SqliteCoordinationStore` shim is removed.
- Utility smoke: a single `AsyncSession` driving the repository can
  acquire a lease, see no duplicate acquire returning the existing id,
  send / read a signal, and observe a checkpoint escalating from
  `alice` to `company_admin` after deadline.

### Phase 15: Charter Calibration Approval Surface (PostgreSQL)

Goal: dream's `propose_charter_calibrations_from_feedback()` proposals
become durable artifacts the owner / company admin can review, approve,
or reject — instead of being recomputed and lost on every dream tick.

- Add `models/charter_proposal.py` with `CharterProposal` SQLAlchemy
  model + alembic migration to create the table (`tenant_id` foreign
  key, `agent_id`, `decision_id`, `proposal_kind`, status fields).
- Rewrite `services/charter_proposals.py` as `CharterProposalStore(
  session, tenant_id)` using AsyncSession + asyncpg, with the same
  `ProposalKind` / `ProposalStatus` enums and `CharterProposal`
  dataclass surface as before.
- API: `submit(...)`, `get(id)`, `list_pending(agent_id=None)`,
  `approve(id, by, decision_reason=None)`, `reject(id, by, decision_reason=None)`,
  `expire_stale(max_age_days=7)`.
- Decision metadata recorded: `status`, `decided_at`, `decided_by`,
  `decision_reason`. Double-decisioning raises `ProposalAlreadyDecided`
  so accidental re-approval cannot silently mutate the audit trail.
- Runtime gate: the store is the durable handoff between dream and the
  owner-approval flow. The actual charter mutation step is intentionally
  out of scope here — once a proposal is approved, downstream (Phase 6
  sketch->active path) will apply it.
- Utility smoke: dream submits two proposals (agent-1 to widen
  `consider_full_authority`, agent-2 to tighten to `confirm_first`);
  the owner approves agent-1 with a reason and rejects agent-2;
  pending list is empty afterwards; a 14-day-old proposal is moved to
  `expired` by `expire_stale(max_age_days=7)`; reopening the store
  preserves all of the above.

### Phase 16: Persisted Replay Corpus

Goal: `policy_replay.guard_activation_policy_experiment()` can evolve
production activation policy from real owner / company outcomes instead
of test-only fixtures — without ever surfacing the original PII or
sensitive content.

- Add `memory/replay_corpus.py` with `AnonymizationMap`,
  `append_case_jsonl(path, case, *, amap)`, and `load_corpus(path)`.
- Three durable safety properties:
  - PII in candidate content goes through `PrivacyLayer.classify_and_mask`
    so loaded content never contains owner email / phone / credentials.
  - Owner / company / goal terms are anonymized via a stable
    placeholder map (`owner_term_1_<digest>`, etc.) so two occurrences of
    the same name share the same placeholder and matching still works.
  - PrincipalStack ids are anonymized via the same map.
- Malformed lines are skipped at load time; a missing corpus file
  returns an empty list.
- Runtime gate: any callsite that previously fed in-test fixtures to
  `guard_activation_policy_experiment` can now point at a persisted
  jsonl corpus instead. The corpus is the production source for
  candidate-policy evaluation.
- Utility smoke: two cases appended with `owner_term="alice"` both
  surface the same placeholder on disk and reload; a candidate text
  `alice@example.com prefers cited sources` becomes
  `<Email_N> prefers cited sources`; `expected_entry_ids` round-trip
  unchanged; `evaluate_activation_policy()` reads the loaded corpus
  without modification.

### Phase 17: Production Wiring — CoordinationGateway

Goal: production callers (`orchestrator.delegate_async`, tool service
preflight) can opt into the PostgreSQL-backed `CoordinationRepository`
without breaking single-process / test paths that still rely on the
in-memory `CoordinationRuntime`.

- Add `agents/coordination_gateway.py` with a `@runtime_checkable`
  async `CoordinationGateway` Protocol and `InProcessCoordinationGateway`
  adapter that wraps the existing sync `CoordinationRuntime`. The
  Protocol's surface is the union of lease / signal / checkpoint
  operations actually used in production.
- `CoordinationRepository` (Phase 14 PostgreSQL repo) already satisfies
  the Protocol natively — no adapter needed.
- Add `agents/coordination_wiring.py` with `pick_gateway(session,
  tenant_id)` that reads `settings.COORDINATION_BACKEND` (`"memory"` /
  `"postgres"`) and returns the right gateway, plus the async factory
  `gateway_from_session()` for per-request scopes.
- Add `COORDINATION_BACKEND: str = "memory"` to `app/config.py`. Default
  preserves current behaviour for everyone.
- Wire `orchestrator.delegate_async`: optional
  `coordination_gateway: CoordinationGateway | None = None` parameter,
  defaults to `InProcessCoordinationGateway(coordination_runtime)`. All
  `coordination_runtime.acquire_lease/send_signal(...)` inside the
  function are now `await gateway.xxx(...)`.
- Wire `tools/service.py`: `ToolRuntimeService` dataclass gains
  `coordination_gateway: CoordinationGateway | None` and seeds it from
  the in-process runtime in `__post_init__`. `_preflight_tool_execution`
  uses `await self.coordination_gateway.create_checkpoint(...)`.
- Runtime gate: production deployments set
  `COORDINATION_BACKEND=postgres` and construct a `CoordinationRepository`
  inside the request scope (where `get_db()` has set the tenant GUC for
  RLS), passing it as `coordination_gateway` into the orchestrator / tool
  service. Default `memory` keeps existing single-process behaviour.
- Utility smoke: `pick_gateway()` returns `InProcessCoordinationGateway`
  by default; with `COORDINATION_BACKEND=postgres` and a `session +
  tenant_id` it returns a `CoordinationRepository`; with `postgres` but
  no session it warns and falls back. All three are valid
  `CoordinationGateway` instances. `delegate_async()` + tool preflight
  produce the same Lease / Signal / Checkpoint dataclasses through
  either gateway.

### Phase 18: Auto-Resolve Gateway Scope at Runtime

Goal: close the Phase 17 wiring residual. `orchestrator.delegate_async`
and `ToolRuntimeService` confirm-first preflight now resolve a
`CoordinationGateway` automatically per call — no deployment hook
required. Setting `COORDINATION_BACKEND=postgres` is the only thing a
deployment touches.

- Add `gateway_scope(explicit_gateway=None, *, tenant_id=None)` async
  context manager in `agents/coordination_wiring.py`. Decision order:
  explicit gateway wins; otherwise `COORDINATION_BACKEND=postgres` +
  tenant_id opens a fresh `async_session()` and yields a
  `CoordinationRepository` (commit on clean exit, rollback on error);
  else in-process gateway.
- `delegate_async()` gains `tenant_id: uuid.UUID | str | None = None`
  and wraps every gateway interaction in `async with gateway_scope(
  coordination_gateway, tenant_id=tenant_id) as gateway:`. The unused
  `coordination_runtime` import is removed because the gateway is the
  only entry point now.
- `_delegate_to_agent_async()` reads `source_agent.tenant_id` and
  forwards it to `delegate_async`, so a confirm-first delegation
  triggered from within an agent automatically lands in PostgreSQL when
  the deployment is configured for it.
- `ToolRuntimeService._preflight_tool_execution()` wraps `create_checkpoint`
  in `async with gateway_scope(self.coordination_gateway,
  tenant_id=runtime_context.tenant_id) as gateway:`. The dataclass field
  remains the fast-path override; absent that, runtime backend choice
  is made automatically.
- Runtime gate: nothing in the production code path now bypasses gateway
  selection. `COORDINATION_BACKEND` is the single toggle that decides
  whether coordination state lives in memory or PostgreSQL.
- Utility smoke: `gateway_scope()` returns in-process gateway by
  default; `gateway_scope(tenant_id=uuid)` under `postgres` opens a
  fresh session and yields a `CoordinationRepository`; invalid /
  missing tenant logs a warning and falls back; an `Exception` inside
  the scope triggers rollback. `delegate_async()` and the tool service
  checkpoint path both go through the scope and produce identical
  Lease / Signal / Checkpoint dataclasses regardless of backend.

### 17.2 Phase Evidence Ledger

| Phase | Status | Evidence packet |
|---|---|---|
| 1 Identity and Charter Substrate | Completed 2026-05-22 | Code paths: `backend/app/services/agency_charter.py`, `backend/tests/services/test_agency_charter.py`. Runtime gate available: downstream memory activation and action preflight can now consume `AgentAccountabilityContext` instead of reconstructing owner/company identity from prompt text. Tests: red run failed with `ModuleNotFoundError: No module named 'app.services.agency_charter'`; green run `pytest tests/services/test_agency_charter.py tests/services/test_principal_context.py tests/services/test_action_preflight.py tests/memory/test_activation_scoring.py tests/memory/test_retrieval_pipeline.py::test_activation_context_suppresses_pl3_when_current_user_is_not_owner tests/memory/test_retrieval_pipeline.py::test_activation_context_adds_reasons_and_updates_score` -> `15 passed in 0.25s`; lint `ruff check app/services/agency_charter.py tests/services/test_agency_charter.py && ruff format --check app/services/agency_charter.py tests/services/test_agency_charter.py` -> passed. Utility smoke: default context states the agent directly supports Alice within Acme's company charter, while an owner-authorized external refund commitment still exposes `company_boundary_conflict` and `company-admin` escalation. Residual risk: production memory/action runtime enforcement remains Phase 3/4. |
| 2 Memory Write Safety | Completed 2026-05-22 | Code paths: `backend/app/memory/write_gate.py`, `backend/app/memory/t2_store.py`, `backend/app/tools/handlers/memory.py`, `backend/tests/memory/test_write_gate.py`, `backend/tests/memory/test_t2_store.py`, `backend/tests/tools/test_memory_handler.py`. Runtime gate wired: agent `save_memory` T3 writes and extractor-backed T2 writes now both pass through `prepare_memory_write()` before durable persistence. Tests: red run failed with missing `app.memory.write_gate`, T2 writing 2 entries including a credential, and T3 missing `access_count`; green run `pytest tests/memory/test_write_gate.py tests/memory/test_t2_store.py tests/tools/test_memory_handler.py tests/tools/test_memory_control_plane_integration.py tests/services/test_privacy_layer.py tests/memory/test_form_contract.py tests/memory/test_md_store_metadata.py` -> `27 passed in 1.33s`; lint `ruff check ... && ruff format --check ...` -> passed. Utility smoke: `api_key=sk-...` is rejected before persistence, `alice@example.com` becomes `<Email_1>`, and persisted T2/T3 entries carry `entry_id`, `sensitivity`, `status`, `version`, `access_count`, `last_accessed`, and evidence refs where provided. Residual risk: raw T0 behavior logs and outbound channel adapters are not yet rewritten by this phase; outbound redaction remains an acceptance criterion for later action/channel gating. |
| 3 Memory Read and Activation Runtime | Completed 2026-05-22 | Code paths: `backend/app/services/memory_service.py`, `backend/app/runtime/invoker.py`, `backend/tests/services/test_memory_service.py`, `backend/tests/runtime/test_memory_query_routing.py`. Runtime gate wired: `build_memory_context()` and `build_memory_snapshot()` now resolve owner/company accountability into `ActivationContext` and pass it to `MemoryRetriever.retrieve()` when supported; runtime invoker passes `current_user_id/current_user_name` into memory retrieval. Tests: red run failed because `_resolve_accountability_context` did not exist and invoker passed no current user; green run `pytest tests/services/test_memory_service.py tests/runtime/test_memory_query_routing.py tests/memory/test_activation_scoring.py tests/memory/test_retrieval_pipeline.py tests/runtime/test_memory_section.py` -> `37 passed in 0.15s`; lint `ruff check ... && ruff format --check ...` -> passed. Utility smoke: real `build_memory_context()` over T3 markdown suppresses `PL3_sensitive` salary memory for non-owner Bob while retaining PL1 Acme policy memory with `why=` activation reasons. Residual risk: access-count mutation for activated entries is not yet persisted back to markdown/index; that remains part of the later telemetry/eval work. |
| 4 Decision Trace and Action Preflight Loop | Completed 2026-05-22 | Code paths: `backend/app/tools/service.py`, `backend/app/services/decision_trace.py`, `backend/tests/tools/test_service.py`. Runtime gate wired: `ToolRuntimeService.execute()` now runs action preflight after governance and before registry/backend execution; non-`do` decisions return a preflight block and do not call the tool executor. Tests: red run failed with `ToolRuntimeService.__init__() got an unexpected keyword argument 'decision_trace_store'`; green run `pytest tests/tools/test_service.py tests/services/test_action_preflight.py tests/services/test_decision_trace.py tests/services/test_agent_tools.py tests/services/test_agent_tools_executor_dispatch.py` -> `28 passed in 2.16s`; lint `ruff check ... && ruff format --check ...` -> passed. Utility smoke: `send_feishu_message` with an external vendor reply returns `[Preflight:ask]` and registry is not called; `write_file` containing `api_key=sk-...` returns `[Preflight:refuse]` with `pl4_zero_retention`, registry is not called, and `DecisionTraceStore` records chosen/preflight/sensitivity. Residual risk: checkpoint persistence/approval UX is not created yet; `ask` currently blocks with a preflight message and trace, while durable Checkpoint objects are Phase 7. |
| 5 Feedback Learning and Dream Calibration | Completed 2026-05-22 | Code paths: `backend/app/services/extract_agent.py`, `backend/app/memory/t2_store.py`, `backend/app/services/auto_dream.py`, `backend/tests/services/test_extract_agent.py`, `backend/tests/services/test_auto_dream.py`. Runtime gate wired: extractor feedback entries now carry `reaction`, `polarity`, `feedback_source`, `rationale_from_owner`, and `decision_ref` when available; T2 persistence keeps those metadata fields; dream can derive charter calibration proposals from explicit DecisionTrace feedback chains without mutating charter. Tests: red run failed because feedback metadata was absent from parse/T2 and `propose_charter_calibrations_from_feedback` did not exist; green run `pytest tests/services/test_extract_agent.py tests/services/test_auto_dream.py tests/services/test_decision_trace.py tests/memory/test_t2_store.py tests/services/test_prompt_contracts.py::test_extractor_prompt_emphasizes_weighted_curation_contract` -> `150 passed in 2.59s`; lint `ruff check ... && ruff format --check ...` -> passed. Utility smoke: `decision/dec-1` owner approval becomes `reaction=approved`, `polarity=positive`, `feedback_source=direct_owner`, and `decision_ref=decision/dec-1`; ambiguous "mentioned" feedback remains `unclear`; approved confirm-first decisions produce `consider_full_authority` proposals only. Residual risk: proposals are not yet surfaced in an owner approval UI; mutation remains intentionally blocked until Phase 6/7 approval flows. |
| 6 First-Person Soul and HR Creation | Completed 2026-05-22 | Code paths: `backend/app/services/agent_manager.py`, `backend/app/tools/handlers/hr.py`, `backend/tests/services/test_agent_file_generation.py`, `backend/tests/tools/test_hr_handler.py`. Runtime gate wired: HR schema and preview accept `company_charter` and `owner_agency_charter`; `create_digital_employee` passes company/owner identity and charter blueprint into `initialize_agent_files()`; soul rendering always emits `First-Person Accountability`, `Frozen Company Charter`, and `Frozen Owner Agency Charter` sections. Tests: red run failed because soul had no charter sections and HR schema/preview did not expose charter fields; green run `pytest tests/services/test_agent_file_generation.py tests/tools/test_hr_handler.py tests/tools/handlers/test_hr_soul_prompt.py` -> `31 passed in 2.69s`; lint `ruff check ... && ruff format --check ...` -> passed. Utility smoke: a new research assistant soul says it directly supports Rocky inside Acme Capital, lists Full Authority / Confirm First / Never Do, and marks charter sections frozen so dream/heartbeat can propose but not mutate. Residual risk: owner/admin approval UI for charter edits is not implemented yet; runtime mutation still remains blocked by convention and future checkpoint flows. |
| 7 Coordination Runtime | Completed 2026-05-22 | Code paths: `backend/app/agents/coordination.py`, `backend/app/agents/orchestrator.py`, `backend/app/tools/service.py`, `backend/tests/agents/test_coordination_primitives.py`, `backend/tests/agents/test_orchestrator.py`, `backend/tests/tools/test_service.py`. Runtime gate wired: `delegate_async()` now acquires a coordination `Lease` before spawning duplicate cross-agent work and sends a `delegation_started` `Signal`; confirm-first tool preflight now creates a `Checkpoint` with approver, deadline, escalation target, metadata, and decision-trace linkage before returning the ask block; `Sentinel` can map trigger-like open loops into either Signal or Checkpoint emissions. Tests: red run failed because `coordination_runtime` was not exported, preflight output lacked `checkpoint=`, and `register_sentinel()` did not exist; green targeted run `pytest tests/tools/test_service.py::test_tool_runtime_service_preflight_asks_before_external_visible_tool tests/agents/test_orchestrator.py::test_delegate_async_serializes_duplicate_work_with_coordination_lease` -> `2 passed, 3 warnings in 2.34s`; Sentinel red/green run `pytest tests/agents/test_coordination_primitives.py::test_sentinel_can_emit_signal_for_full_authority_followup tests/agents/test_coordination_primitives.py::test_sentinel_can_emit_checkpoint_for_confirm_first_action` -> red `AttributeError`, green `2 passed in 0.01s`; broader run `pytest tests/agents/test_coordination_primitives.py tests/agents/test_orchestrator.py tests/tools/test_service.py tests/services/test_action_preflight.py tests/services/test_decision_trace.py` -> `52 passed, 3 warnings in 2.81s`; lint `ruff check ... && ruff format --check ...` -> passed. Utility smoke: two identical async delegations return one running handle with `coordination_lease_id` and one `blocked_by_lease` handle with `blocked_by_lease_id`; external Feishu message preflight returns `[Preflight:ask] ... checkpoint=<id>` and the trace stores `checkpoint_id`; a full-authority Sentinel emits a Signal while a confirm-first Sentinel emits a Checkpoint. Residual risk: coordination state is still process-local and not a durable DB-backed approval queue; Phase 8 wires these primitives into the proactive heartbeat/eval loop. |
| 8 Proactive Employee Loop and Eval-Driven Policy Evolution | Completed 2026-05-22 | Code paths: `backend/app/services/proactive_employee_loop.py`, `backend/app/services/heartbeat.py`, `backend/app/memory/activation.py`, `backend/app/memory/policy_replay.py`, `backend/tests/services/test_proactive_employee_loop.py`, `backend/tests/services/test_heartbeat.py`, `backend/tests/memory/test_policy_replay.py`. Runtime gate wired: heartbeat evolution context now builds a proactive steward plan from open-loop activity using real creator/tenant accountability when available; low-risk full-authority work emits a Sentinel Signal for local preparation, external-visible work creates a Checkpoint, and never-do work is refused without emission. Memory activation weights are now parameterized by `ActivationPolicy`, and `guard_activation_policy_experiment()` compares candidate policy changes against replay cases before accepting or reverting. Tests: proactive/red run failed with missing `app.services.proactive_employee_loop`; heartbeat integration red run failed because `Proactive Steward Context` was absent; replay red run failed with missing `ActivationPolicy`; green run `pytest tests/services/test_proactive_employee_loop.py tests/memory/test_policy_replay.py tests/services/test_heartbeat.py::test_build_evolution_context_includes_proactive_steward_plan` -> `6 passed, 3 warnings in 1.47s`; broader run `pytest tests/services/test_proactive_employee_loop.py tests/memory/test_policy_replay.py tests/services/test_heartbeat.py tests/memory/test_activation_scoring.py tests/services/test_action_preflight.py tests/agents/test_coordination_primitives.py` -> `45 passed, 4 warnings in 1.59s`; lint `ruff check ... && ruff format --check ...` -> passed. Utility smoke: an open-loop investor memo follow-up becomes `Prepare local draft` plus a Signal; a ready-to-send vendor reply becomes `Checkpoint required` with owner approver and objective metadata; a credential-sharing action is refused; a replay candidate that improves expected memory hits is accepted, while a lower-quality candidate is reverted to the baseline policy. Residual risk: replay sets are currently in-code/test fixtures rather than a persisted anonymized production corpus; coordination objects remain process-local until durable approval storage is added. |
| 18 Auto-Resolve Gateway Scope at Runtime | Completed 2026-05-22 | Code paths: `backend/app/agents/coordination_wiring.py`, `backend/app/agents/orchestrator.py`, `backend/app/tools/service.py`, `backend/app/services/agent_tool_domains/messaging.py`, `backend/tests/agents/test_coordination_wiring.py`. Runtime gate wired: `delegate_async()` and `ToolRuntimeService._preflight_tool_execution()` both go through `async with gateway_scope(...) as gateway:` which honours an explicit gateway, then `COORDINATION_BACKEND=postgres` + tenant_id (opens `async_session()`, yields `CoordinationRepository`, commits on success / rolls back on error), then falls back to the in-process gateway. `_delegate_to_agent_async` forwards `source_agent.tenant_id` so an in-agent confirm-first delegation lands in PostgreSQL whenever the deployment is configured for it. Tests: red run failed because `gateway_scope`, the `tenant_id` parameter, and the in-callsite `async with` did not exist; green run `pytest tests/agents/ tests/services/test_charter_proposals.py tests/services/test_outbound_privacy.py tests/services/test_t0_privacy_gate.py tests/services/test_archetype.py tests/memory/test_access_log.py tests/memory/test_replay_corpus.py tests/memory/test_understanding_store.py tests/tools/test_service.py` -> `159 passed in 1.84s`; targeted `pytest tests/agents/test_coordination_wiring.py -v` -> `11 passed in 0.22s`; lint `ruff check ... && ruff format ...` -> passed. Utility smoke: with `COORDINATION_BACKEND=memory`, every `delegate_async` / checkpoint call yields the in-process gateway; with `postgres` + tenant_id supplied, `gateway_scope` opens a session and yields `CoordinationRepository`; with `postgres` but missing or malformed tenant_id, `gateway_scope` logs a warning and yields the in-process fallback. `_delegate_to_agent_async("agent-a -> agent-b")` propagates `source_agent.tenant_id` through. **Closes the Phase 17 production-wiring residual end-to-end.** |
| 17 Production Wiring — CoordinationGateway | Completed 2026-05-22 | Code paths: `backend/app/agents/coordination_gateway.py`, `backend/app/agents/coordination_wiring.py`, `backend/app/agents/orchestrator.py`, `backend/app/tools/service.py`, `backend/app/config.py`, `backend/tests/agents/test_coordination_gateway.py`, `backend/tests/agents/test_coordination_wiring.py`. Runtime gate wired: `orchestrator.delegate_async` and `ToolRuntimeService._preflight_tool_execution` now go through the `CoordinationGateway` Protocol; default `InProcessCoordinationGateway(coordination_runtime)` keeps single-process behaviour byte-identical, while `pick_gateway(session, tenant_id)` returns a `CoordinationRepository` when `COORDINATION_BACKEND=postgres`. Tests: red run failed because gateway / wiring modules and the optional `coordination_gateway` parameter did not exist; green run `pytest tests/agents/ tests/services/test_charter_proposals.py tests/tools/test_service.py` -> `153 passed in 1.92s`; lint `ruff check ... && ruff format ...` -> passed. Utility smoke: an in-process gateway acquires a lease, sends and reads a signal, creates / fetches / escalates a checkpoint with the same surface as the sync runtime; `pick_gateway(session, tenant_id)` returns a `CoordinationRepository` when `COORDINATION_BACKEND=postgres`, an `InProcessCoordinationGateway` (with a log warning) when postgres is requested but no session was supplied, and the in-process gateway by default. Residual risk: Sentinel state remains process-local (not part of the Protocol). The deployment-side lifespan / request-scope injection note from this phase is **closed by Phase 18**, which makes `gateway_scope` resolve the right backend automatically per call without any deployment hook. Closes the Phase 14 production-wiring residual. |
| 16 Persisted Replay Corpus | Completed 2026-05-22 | Code paths: `backend/app/memory/replay_corpus.py`, `backend/tests/memory/test_replay_corpus.py`. Runtime gate available: production replay material can now be appended to a jsonl corpus and reloaded by `guard_activation_policy_experiment`. Tests: red run failed with `ModuleNotFoundError: No module named 'app.memory.replay_corpus'`; green run `pytest tests/memory/test_replay_corpus.py` -> `9 passed in 0.02s`; lint `ruff check ... && ruff format ...` -> passed. Utility smoke: `alice@example.com` in candidate content becomes a typed PII placeholder; two cases with owner term `alice` produce the same anonymized `owner_term_1_<digest>`; a malformed line is silently skipped; `evaluate_activation_policy()` runs on a corpus reloaded from disk. Residual risk: the anonymization map is per-write today — long-running production should persist the map alongside the corpus so placeholders stay stable across restarts (left as a follow-on operational nicety). Closes the Phase 8 production-corpus residual. |
| 15 Charter Calibration Approval Surface (PostgreSQL) | Completed 2026-05-22 | Code paths: `backend/app/models/charter_proposal.py`, `backend/alembic/versions/coordination_charter_proposals_0522.py`, `backend/app/services/charter_proposals.py`, `backend/tests/services/test_charter_proposals.py`. Storage: PostgreSQL table `charter_proposals` with `tenant_id` foreign key, indexed by `(tenant_id, status)`. Replaces the local-sqlite shim from the first Phase 15 implementation so proposals live in Hive's single source of truth, participate in tenant isolation, and survive worker restarts. Tests: red run failed because the rewritten store now requires `AsyncSession` + `tenant_id`; green run `pytest tests/services/test_charter_proposals.py` -> `9 passed in 0.17s`; lint `ruff check ... && ruff format ...` -> passed. Utility smoke: `submit(...)` adds a pending row to PG; `approve(by="alice", decision_reason="LGTM")` updates the same row to `status=approved` with `decided_at` / `decided_by`; second `approve` raises `ProposalAlreadyDecided`; `expire_stale(max_age_days=7)` walks pending rows older than the cutoff and sets `status=expired`; unknown `proposal_id` raises `KeyError`. Residual risk: applying an approved proposal back to the agent's frozen charter still requires a downstream `Phase 6 sketch->active` step; the proposal store itself does not mutate any charter. Closes the Phase 5/6 missing-approval-surface residual. |
| 14 Persistent Coordination + Approval Queue (PostgreSQL) | Completed 2026-05-22 | Code paths: `backend/app/models/coordination.py`, `backend/alembic/versions/coordination_charter_proposals_0522.py`, `backend/app/agents/coordination_repository.py`, `backend/tests/agents/test_coordination_repository.py`. Storage: PostgreSQL tables `coordination_leases` (UNIQUE on `(tenant_id, task_key)`), `coordination_signals`, `coordination_checkpoints`, each `tenant_id`-scoped. Lease acquisition uses `INSERT ... ON CONFLICT (tenant_id, task_key) DO UPDATE WHERE expires_at <= NOW() RETURNING ...` so expiry check + replace happen atomically. The prior `SqliteCoordinationStore` shim is removed; Hive single-source-of-truth is preserved. Tests: red run failed when the sqlite tests were deleted and the new repo had no coverage; green run `pytest tests/agents/test_coordination_repository.py` -> `8 passed in 0.16s` with a `_FakeSession` mirroring `tests/services/test_objective_service.py`; broader `pytest tests/agents/ tests/services/test_charter_proposals.py tests/tools/test_service.py` -> `105 passed in 2.33s`; lint `ruff check ... && ruff format ...` -> passed. Utility smoke: `acquire_lease(task-1)` returns `acquired=True` and a Lease dataclass with the new id; a duplicate call against the same `(tenant_id, task_key)` triggers the ON CONFLICT path and returns `acquired=False` + the existing lease id; `send_signal` adds a row, `read_signals(agent, thread)` filters by tenant + thread; `escalate_expired_checkpoints` advances `current_approver_id` to the next escalation_chain entry and stamps `metadata.escalated_at`. Residual risk: production wiring (which call-sites — `orchestrator.delegate_async`, `tools/service` confirm_first preflight — should instantiate the repo in their request scope) is a follow-on integration; Sentinel state remains process-local because it is re-derived per proactive-loop tick. Closes the Phase 7 process-local residual. |
| 13 Access-Count Writeback to Markdown | Completed 2026-05-22 | Code paths: `backend/app/memory/access_log.py`, `backend/app/memory/retriever.py`, `backend/tests/memory/test_access_log.py`. Runtime gate wired: `MemoryRetriever._apply_activation` now calls `bump_access(...)` on every non-suppressed item that carries `entry_id` and a `source` relpath; suppressed (PL3 strip) items do not increment, so the access log truly reflects what reached the prompt. Tests: red run failed with `ModuleNotFoundError: No module named 'app.memory.access_log'`; green run `pytest tests/memory/test_access_log.py tests/memory/test_retrieval_pipeline.py tests/memory/test_activation_scoring.py` -> `20 passed in 0.19s`; lint `ruff check ... && ruff format ...` -> passed. Utility smoke: a T3 entry seeded with `[access_count=0]` reaches `[access_count=3]` after three bumps with the latest UTC `last_accessed` timestamp; unknown `entry_id` returns False and leaves the file untouched; missing file returns False. Residual risk: writeback is fire-and-forget synchronous IO inside the activation hot path; if a markdown file grows beyond ~100KB per agent the cost will need to be batched. Closes the Phase 3 access-count residual. |
| 12 HR Archetype Inference + Defaults | Completed 2026-05-22 | Code paths: `backend/app/services/archetype.py`, `backend/app/tools/handlers/hr.py`, `backend/tests/services/test_archetype.py`. Runtime gate wired: `_build_blueprint_preview_payload` now calls `apply_archetype_defaults(...)` before emitting the blueprint, so an HR preview / `create_digital_employee` payload always carries a non-empty `company_charter` and `owner_agency_charter`, plus an explicit `archetype` tag. Tests: red run failed with `ModuleNotFoundError: No module named 'app.services.archetype'`; green run `pytest tests/services/test_archetype.py tests/tools/test_hr_handler.py` -> `22 passed, 4 warnings in 1.38s`; lint `ruff check ... && ruff format --check ...` -> passed after auto-format. Utility smoke: an HR refine with `role_description="research analyst tracking emerging markets"` now ships with `confirm_first` containing "Publish any external memo"; an explicit owner charter passes through unmodified; missing role descriptions fall back to `generalist` with safe defaults. Residual risk: archetype is a keyword classifier today, not an LLM call; downstream soul rendering does not yet display the archetype label visibly to owners (purely metadata). |
| 11 Understanding Store + Relationship Memory | Completed 2026-05-22 | Code paths: `backend/app/memory/understanding_store.py`, `backend/tests/memory/test_understanding_store.py`. Runtime gate available: `UnderstandingStore(base_dir)` persists entries to `understandings.md` so dream / retriever can read relationship-shaped memory as a first-class node instead of free-form T3 bullets. Tests: red run failed with `ModuleNotFoundError: No module named 'app.memory.understanding_store'`; green run `pytest tests/memory/test_understanding_store.py` -> `10 passed in 0.03s`; lint `ruff check ... && ruff format --check ...` -> passed after auto-format. Utility smoke: recording `agent_a-[collaborator]->agent_b` and then contradicting it preserves both entries with cross-linked `contradiction_history` and halves the original confidence; reloading the store from disk yields the same entries; `decayed_confidence(now)` returns original confidence inside the 30-day window and decays linearly afterward. Residual risk: the store is not yet wired into retriever scoring or dream proposals; that integration is incremental and intentionally out of scope here. |
| 10 T0 Privacy Frontline + Form Lint | Completed 2026-05-22 | Code paths: `backend/app/services/t0_logger.py`, `backend/tests/services/test_t0_privacy_gate.py`. Runtime gate wired: `write_t0_log()` now calls `_apply_t0_privacy_gate(content)` immediately after the formatter and before `filepath.write_text(...)`, so PL4 credentials are masked, sensitivity is recorded in the frontmatter, and form-lint warnings (pronoun / relative time) are emitted. Tests: red run failed because the credential string still appeared in the file; green run `pytest tests/services/test_t0_privacy_gate.py tests/services/test_t0_logger.py` -> `77 passed in 0.22s`; lint `ruff check app/services/t0_logger.py tests/services/test_t0_privacy_gate.py && ruff format --check ...` -> passed after auto-format. Utility smoke: a chat transcript containing a synthetic `sk-` token persists only `<Credential_1>` with `t0_sensitivity: PL4_credential`; a salary discussion is preserved but tagged `t0_sensitivity: PL3_sensitive`; an everyday standup line stays `t0_sensitivity: PL1_public`; Chinese pronoun + relative-time content yields `t0_form_warnings: [ambiguous_pronoun, relative_time]`. Residual risk: outbound file attachments and inline rich-text blocks still bypass the gate; per-message rewrite-on-promotion remains Phase 11/12 work. |
| 9 Channel Outbound Privacy Redact | Completed 2026-05-22 | Code paths: `backend/app/services/outbound_privacy.py`, `backend/app/services/channel_delivery_service.py`, `backend/tests/services/test_outbound_privacy.py`. Runtime gate wired: `ChannelDeliveryService.send_text` now calls `redact_outbound()` before any per-channel send branch; PL4 returns `status="denied"`, PL3 is stripped for external channels, PL2 is replaced with typed placeholders. Tests: red run failed with `ModuleNotFoundError: No module named 'app.services.outbound_privacy'`; green run `pytest tests/services/test_outbound_privacy.py tests/services/test_channel_delivery_service.py tests/services/test_privacy_layer.py` -> `23 passed, 3 warnings in 1.22s`; lint `ruff check app/services/outbound_privacy.py app/services/channel_delivery_service.py tests/services/test_outbound_privacy.py && ruff format --check ...` -> passed after auto-format. Utility smoke: outbound Telegram text containing `api_key=sk-...` is blocked (`status=denied`, executor never called); a vendor reply about salary is rewritten to `[REDACTED_PL3]` before delivery; a web reply to the direct owner keeps the original sensitive content. Residual risk: file payloads (`send_file`) and inline rich-text blocks bypass the gate; T0 behavior writers (Phase 10) and outbound file attachments are intentionally left to follow-on phases. |

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
