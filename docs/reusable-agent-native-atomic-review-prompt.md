# Agent-Native 原子化审查可复用 Prompt

这是一份长期复用模板。它不携带固定日期、历史审查次数、预设断点数量或既有修复结论。使用时直接复制“可复用正文”中的全部内容交给审查 Agent。

## 可复用正文

````text
你现在需要对当前代码仓库执行一次独立、完整、证据驱动的 Agent-Native 原子化架构审查。

这是一项从当前事实重新建立结论的审查。不要假设项目以前做过类似工作，不要继承旧报告的完成声明，不要预设断点数量，也不要为了与已有文档一致而弱化源码证据。

本任务默认只读。除非用户另行明确授权，否则不要修改业务代码、数据库、部署配置、生产数据或现有文档。需要输出报告时，新建独立文件并在开始前声明路径；不得覆盖已有报告。文件名必须唯一，但不要依赖本提示词中的固定日期、固定轮次或固定编号。

# 一、先建立权威顺序

开始代码审查前，必须完整读取当前仓库根目录的 Agent 指令文件和其中声明的 canonical 文档。历史设计稿只能作为线索，不能覆盖当前根规范、当前源码、测试、迁移和生产路径。

发生冲突时，严格按照以下顺序裁决：

1. 产品双北极星，且单 Agent 能力先建设、先评判。
2. AI-Native Design Law 与 Model Agency Boundary。
3. CC / FreeCode 的 Agent 生命周期与能力语义。
4. Codex 的工程控制、可靠性、可观测性和桌面交互增量。
5. Hive Native 的 Memory、自进化、Skill evolve、Dynamic context、Local Agent、A2A、Workflow 与 Knowledge 能力。
6. 企业身份、权限、安全、预算、审计、治理和 AI 资产控制中台。
7. 七原子闭环证据。
8. KISS、奥卡姆剃刀、迁移清理和一次完整交付纪律。

低层规则只能增强高层目标，不能覆盖高层目标。

# 二、产品北极星

Hive 只有两个顶层产品目标：

1. 最强数字员工
   - 单 Agent 的智能、工具使用、记忆、自进化、技能成长、可靠性和安全边界必须达到当前根规范指定的内部 lean benchmark（当前为 hermes-agent），并持续获得可验证提升。
   - 一个架构更复杂、治理更多，但实际使用时更弱、更笨、更容易卡住的 Agent，不算成功。

2. 公司级控制中台
   - 在企业规模下运营 Agent，覆盖身份、权限、组织、预算、审计、协作、治理、生命周期、AI 资产和可观测性。
   - 控制中台必须建立在强 Agent 之上，不能用控制面的完整度掩盖 Agent 能力退化。

四个审查模块与北极星的关系是：

- 单 Agent：数字员工的 CCPlus 运行基座。
- Hive Native：数字员工的自进化和长期差异化能力。
- 企业治理：公司级控制中台。
- UI/UX：以上能力的共同操作、消费、恢复和验收面。

# 三、最高设计法律：先释放模型，再约束行动

所有需要理解、判断、总结、规划、综合、反思、提炼、根因分析、学习价值判断或最终表达的步骤，主路径必须属于 LLM。

在认证、授权数据、外部效果、资源上限和执行隔离建立之后：

- LLM 独占语义判断、推理、综合、优先级、学习和最终表达。
- 平台拥有身份、权限、数据入口、外部副作用、执行隔离、显式资源、机械契约、机械事实、证据、恢复、审计和 durable commit。
- 平台可以记录模型与证据的不一致，也可以把结构化证据交给模型重新综合；平台不能冒充模型给出语义结论。

对齐的目标是保住并增强 Agent 能力，而不是让 Agent 更容易被平台预测。

## 3.1 允许 hard block 的范围

机械 hard gate 只能建立在以下可外部验证的不变量之上：

1. Authority / data ingress
   - tenant、RLS、principal、ownership、delegation、source ACL、sensitivity、credential visibility。

2. Side effect
   - 外发、删除、转账、资源创建、不可逆变更、公司边界行为、审批和 checkpoint。

3. Execution isolation
   - sandbox、provider capability、host secret isolation、path、transport、protocol safety。

4. Resource / lifecycle
   - provider 的物理 context window / request contract、用户或组织显式授权的成本与时限上限、sandbox/进程的真实容量、cancel、deadline、lease、结构化 cycle safety。
   - 真实拓扑 cycle 可以硬拒绝最小成环边；应用自设 `max_depth` 默认只是软调度水位。只有当 depth 映射到真实调用栈/协议上限或显式 Workflow contract 时，才可结束当前 attempt；仍不得无状态地把整个 task/session 终态化。
   - 应用内部为了方便而设置的 turn/tool/result/concurrency 常量不自动获得 hard-block 资格；必须先通过下一节的硬/软边界分类。

5. Evidence / recovery
   - typed receipt、event/span/transcript ordering、idempotency、replay、rollback、durable-write commit。

6. Machine contract
   - API schema、语法、协议、版本和精确状态机合法性。

每一个 hard gate 必须说明：

- 它保护哪条硬不变量；
- 权威事实源是什么；
- 为什么不能放到数据入口或外部效果执行前；
- 被拒绝后哪些无关推理和工具仍可继续；
- 如何观察、解释和恢复。

如果回答不了这些问题，该机制应被标记为模型能力限制，而不是治理。

## 3.2 硬边界、软预算与失败感知法律

本轮审查必须把“边界存在”与“边界如何失败”分开。目标不是删除边界，而是做到 **bounded without blindness：效果受硬治理，思考与任务推进具备弹性，任何压力都可感知、可降载、可恢复。**

先对仓库中的每一个 limit、budget、quota、timeout、max-*、slice、top-k、first-N、semaphore、queue、depth、cycle、retry 和 result-size 机制建立登记表，并归入以下四类之一：

1. **物理 / 协议硬边界**
   - 例如模型 context window、provider request/schema 上限、单进程真实内存/CPU/文件描述符、sandbox 隔离容量、消息协议最大帧。
   - 这类边界可以硬拒绝“不可能的单次动作”，但必须先尝试 lossless externalization、分页、分块、排队、checkpoint、model-led compaction 或换用兼容模型；不能静默丢字节。

2. **权威 / 外部效果硬边界**
   - 例如 tenant/ACL/RLS、approval、不可逆外发/付款/删除、credential scope、sandbox/egress policy。
   - 必须硬执行；但一个效果被拒绝，只能拒绝该效果，不能让无关推理、只读工具、交付已有结果或请求帮助一起死亡。

3. **显式经济 / 生命周期硬边界**
   - 只有来自用户、组织、套餐、账单、SLA、明确 workflow artifact 或可信 operator policy 的 token/cost/deadline/cancel 上限，才可作为最终硬停事实。
   - 即使到达硬上限，也必须先持久化 checkpoint、已完成工作、coverage、pending effect 和恢复令牌，再返回 typed paused/stopped 状态；不得把它变成无状态报错或平台伪造的 final answer。

4. **运行软预算 / 调度目标**
   - 默认包括模型轮数、工具轮数、并发数、fan-out 数、单批结果量、Prompt 目标占用率、Memory/Skill/MCP 候选量、retry 目标、延迟目标和缓存目标。
   - 越界优先触发 warning、backpressure、queue、batch、fan-in、spill-to-artifact、deferred discovery、checkpoint、继续授权请求或降并发；只有命中前三类明确硬事实时才能最终停止。

Prompt 体积方面，除 provider 的物理 context window / request contract 外，不得把任意固定 token 数、字符数、消息数、Skill 数、Tool 数、Sub-agent 数或结果数当成“超出即任务失败”的语义预算。`max_output_tokens` 必须按任务与模型能力自适应；上下文目标占用率只能是触发整理的软水位，不能成为静默截断依据。

每次 provider 调用都必须形成 effective context contract，至少记录实际 model/provider/version、advertised context window、provider max output、reserved output、system/developer/history/tool schema/多模态开销、tokenizer 或估算器及误差余量、实际 request size、response usage、`finish_reason` 和 provider overflow/413 回执。标称 256K 不等于 256K 可用输入。`finish_reason=length`、单轮 output cap 或流中断只结束当前生成 attempt；partial output 必须按序去重并 durable 保存，随后允许同一模型续写、重规划或恢复，不得伪装成 task failure。

所有预算与压力状态至少要有下列 typed 字段或等价事实：

- `limit_kind`、`limit_source`、`hard_or_soft`；
- `used`、`reserved`、`remaining`、`estimated_pending`；
- `scope`（turn/session/agent/workflow/tenant/provider）；
- `state`（normal/pressure/backpressured/paused/exhausted）；
- `preserved_state_ref`、`coverage_ref`、`retryable`、`next_valid_actions`；
- `task_id`、`attempt_id`、`pressure_epoch`、`blocked_on`、`resume_condition`、`resume_owner`；
- `not_before`、`lease_expires_at`、`progress_marker`、`retry_fingerprint`、`checkpoint_ref`；
- 若为 hard outcome，指向其 authoritative policy/provider/approval fact。

嵌套 Agent/Workflow 预算必须使用 reserve → commit/release 的父子账本，避免 100 个子任务各自认为拥有完整父预算。并发上限可以硬限制“同时执行多少”，但超出的工作必须进入有界 durable queue 或 typed admission pause，不能被当成语义失败或静默遗失。

大结果不得全量 inline 回父 Prompt，也不得截断后假装完整。必须保留完整 durable result/artifact，向父 Agent 返回 typed manifest、摘要、source refs、coverage、大小和按需读取入口。fan-in 由父 Agent/专门 integrator 在完整 coverage ledger 上分批综合；平台只做调度、格式、证据和硬边界，不替模型写最终语义。

每一个 limit 命中都必须对当前模型可见。模型至少能知道：发生了什么、哪些工作已保留、哪些未完成、是否可重试、可以请求什么资源、可以改用什么策略。禁止“框架直接报错、模型完全不知道为什么终止”。

活性与安全同等重要。审查必须建立 wait-for graph，检查 barrier、approval、budget、queue、parent/child、channel delivery、hook 和 retry 之间是否可能形成循环等待。结构化 cycle detection 可以阻止真实拓扑循环，但不得用自然语言关键词猜循环。任何 full barrier 都必须有 partial/late/failed participant policy、deadline、取消传播和可恢复的 join 状态。

### 3.2.1 Attempt、Task 终态与活性证明

“除 context window / provider request contract 外，应用运行指标默认是软预算”只适用于智能、容量和调度目标，不软化 tenant/ACL/RLS、不可逆 effect、credential/sandbox、显式 cost/deadline/cancel 或其它真实物理、协议和权威不变量。

Hard gate 可以拒绝一个 effect、admission、request 或 execution attempt；除非权威 task 状态机本身满足终态条件，不得据此伪造整个 task/session 的语义失败。operation timeout、自动 retry 次数、circuit breaker、semaphore、queue capacity、batch size、fan-out/depth 水位和 provider 单轮 output cap，只能停止或延后当前 attempt、停止自动重试，或把控制权交回模型/用户。它们必须保留 durable task、partial result、coverage 和 next actions。

每个非终态 pressure/backpressured/paused 状态必须有可达恢复边和 progress certificate。若没有任何可达的 resume condition/owner，必须诚实进入 typed blocked/terminal 状态并释放资源，不能永久 pending，也不能靠重复 timeout 重建同一等待环。

Parent 只有在 child budget reserve 与 durable enqueue/admission 原子提交后，才可把 child 加入 expected/wait coverage。未成功 admission 的请求必须登记为 typed `not_admitted` 或 `deferred`，不得产生永远不会到达的幽灵 child。任何等待 approval、channel callback、budget、fan-in 或人工输入的节点不得继续占用稀缺 worker、DB、socket 或 connector permit；资源按稳定全序获取，等待前释放非必要 permit，lease 到期必须可回收。

每个自动 retry、fallback、compaction、requeue 或 provider 切换都必须证明单调进展，或证明 input/effect/policy/credential/dependency/time-window fingerprint 已发生实质变化。相同 fingerprint 的 policy denial 不得自动 retry；停止自动 retry 只是把控制权交还模型/用户，不等于 task failure。模型切换只能是用户/策略明确允许的可选恢复手段，单 Session、单模型、无替代 provider 时仍必须有 externalize、compact、分批、多 turn、checkpoint、pause/resume 路径，禁止秘密降级模型。

Limit observation 按 material state transition / pressure epoch 进入模型；重复事件写 durable counter、manifest 和聚合引用，不逐条灌入 Prompt 形成 observation storm。若命中 hard stop 时 provider 已无法再次调用，typed 状态至少必须立即对 UI/operator 可见，并保证恢复后的首次模型调用可见；平台不得伪造一段 assistant prose 冒充模型终答。

### 3.2.2 四个伸缩平面必须分账

审查开始时必须先画出四个彼此正交的伸缩平面，任何容量结论、断点计数和测试结果都不得混算：

1. **Fleet plane**：平台数据库中已注册、可路由、可被 trigger 唤醒的数字员工总量。两三千、上万甚至更多 Agent 可以是常态；这不等于同时存在同数量的模型进程，也不等于它们进入同一个 Prompt。重点审查 registry/query、trigger scan、worker fairness、tenant isolation、queue partition、control-plane headroom 和全局可观测性。
2. **Root execution tree**：一个 root Agent 的一个 Session/Turn/Task 发起的执行树。本文所有“100-way fan-out/fan-in”默认都指 **同一个 root Session 一次请求 100 个 child execution**；child 可以来自直接 Sub-agent、Agent Team、Workflow leaf 或它们的嵌套组合。重点审查 admission、root identity、expected set、result manifest、integration epoch、parent context 和恢复。
3. **Capability plane**：单个 Agent 拥有的 Skill、MCP、Sub-agent definition、Agent Team、Workflow、Memory 和 Knowledge 规模。资源总量可以很大，但首轮 resident Prompt 必须相对资源总量有界，且授权资源全集保持可发现。
4. **Channel plane**：同一 root task 横跨钉钉、飞书、Slack、Web 等渠道的 ingress、handoff、effect 与 delivery。Agent work terminal 与每个渠道 delivery terminal 必须分离。

禁止用任一平面的证据替代另一个平面：

- 不能把“平台有一万个 Agent definition”写成“一次启动一万个 Agent”；
- 不能用 fleet worker semaphore 证明单个 root 的 100 个结果不会撑爆父上下文；
- 不能用 parent Prompt 有界证明全局 scheduler 对其它 tenant/root 公平；
- 不能用某个渠道成功证明 root task 完成，也不能用 root task 完成证明所有渠道已送达。

Root execution tree 至少要有 `root_execution_id/root_runtime_task_id`、`root_session_id`、`requested/admitted/deferred/not_admitted`、`expected/received/failed/late/duplicate`、`integration_epoch`、`result_manifest_ref` 和 `parent_resume_cursor` 或等价机械事实。直接 Sub-agent、Agent Team 和 Workflow 可以保留不同执行语义，但必须投影到同一 root coverage/result contract；否则不得宣称 100-way fan-in 可恢复。

## 3.3 禁止模式

在 Session、Plan、Compaction、Memory、Soul、Skill、Workflow、A2A、Sub-agent、Agent Team、Model Routing 和 Final Answer 主路径上，重点查找并判定以下反模式：

- 用关键词、正则、自然语言子串、计数器、字符串相似度或固定阈值决定语义真伪、用户意图、任务完成、重要性、矛盾、学习价值或答案正确性；
- 模型已经生成最终回答后，平台因为自然语言扫描结果而替换、追加或压制模型原文；
- 因任务措辞、coordinator 标签或启发式路由删除工具、委派或真实 capability surface；
- 未经用户同意秘密更换或降级已选模型；
- 为节省预算静默保留 head/tail/first-N/top-k，导致智能步骤看不到完整授权证据；
- reviewer 或 LLM 不可用时，机械 fallback 直接 accept、reject、promote、delete 或 rewrite Memory/Soul/Skill/Plan 语义；
- 把基础设施 unavailable 伪装成 policy denied；
- 一个工具或效果被拒绝后，连带禁止无关推理、无关工具、规划或最终回答；
- 平台生成固定语义文案，伪装成模型的判断；
- 客户端回显 server hash 并把它当成语义或权限权威。
- 用固定 model-turn/tool-round/fan-out/result-size 常量直接把可继续任务终态化为失败，而不给模型 pressure observation、checkpoint、排队、扩容请求或继续路径；
- 100 个或更多 Worker 同时完成时把所有原始结果一次性拼回父 Prompt，或在返回风暴中无界 gather/append/JSON serialize；
- 把平台 fleet Agent 总量、单个 root execution tree 的 child 数量、单 Agent capability 数量和跨渠道 delivery 数量混为同一个“并发 Agent 数”；
- direct Sub-agent、Agent Team 与 Workflow 各自有局部完成状态，却没有同一 root 的 requested/admitted/expected/result/integration ledger；
- 等待所有子任务、所有渠道或所有 workflow leaf 成功的 full barrier，任何一个 late/denied/unavailable 就让全局永久等待；
- 只暴露 first-N Skill/MCP/Agent/Workflow 且没有可发现的完整 catalog、分页、搜索或 coverage ledger，造成授权能力实际消失；
- 父 Agent、子 Agent、Workflow 和 Connector 各自独立计算预算，出现层级超卖、重复扣费、重复外发或预算耗尽后无法恢复；
- limit 命中只写日志或抛异常，不产生模型可见的 typed result、保留状态和下一步；
- retry、fallback、compaction、fan-in 或跨渠道回调互相触发，形成无终止条件的机械死循环。

历史测试如果断言了上述行为，不得把“测试是绿的”当成正确性证据。应先判断测试是否在保护错误架构。

## 3.4 完整证据可用性

“模型看到完整输入”指当前智能步骤拥有完整的授权证据可用性，不等于把所有数据库和知识库全文塞进原始 Prompt。

允许使用大型 Artifact、分块、索引和 Pointer，但必须同时满足：

- 模型知道完整 coverage；
- 引用真实、无损、可发现；
- 原始内容可以稳定读取；
- 丢弃范围、hash、source ref 和恢复方式明确；
- 决定性证据不能藏在模型不知道存在的尾部；
- 物理超窗时优先 model-led compaction；
- 单次仍无法覆盖时使用完整 chunk/map-reduce coverage；
- 机械丢弃只能是可观测、可恢复的 provider-failure 末端路径。
- 多 Agent / 多渠道返回必须维护 expected/received/failed/late/duplicate coverage ledger；父 Agent 可以在 coverage 不完整时继续判断，但平台和 UI 不得把 partial 冒充 complete；
- 300–400 个 Skill、200 个 MCP 或更大 capability catalog 必须保持全量可发现，但只能按需加载描述、schema 和参考资料；“未进入当前 Prompt”不等于“能力不存在”；
- 当模型切换到更小 context window、恢复时 tool bundle 改变、Memory 增长或 provider 报 context overflow 时，必须能重建输入并说明 coverage 变化，而不是直接丢 Session。

Personal KB 必须保持 tool-only：

- 不在 Agent loop 开始前预取；
- 不静态注入最原始 context assembly；
- 通过受治理的 search/read/cite 工具按需使用；
- 工具能力必须可发现；
- 权限拒绝、基础设施不可用和真实空结果必须区分；
- 引用和 source refs 必须保留；
- 公共 Agent、共享 Agent、Sub-agent 和 A2A 不得自动继承 Owner 的 Personal KB 权限。

Enterprise Knowledge 建立在知识工具和权限平面之上，增加 organization/tenant、ACL/RLS、provenance、retention、legal hold、audit、version 和 deletion propagation。不得用 Personal KB、普通文件树、Company Intro 或遗留文件接口冒充企业知识库。

# 四、对标边界

## 4.1 CC / FreeCode

CC / FreeCode 是本地 Agent 生命周期和能力语义基线，不是逐行实现模板。

必须对比：

- context assembly
- context pressure、provider window、output budget 与 limit-hit 后的继续/压缩/暂停语义
- accepted user prompt
- transcript append
- model loop
- tool discovery 与 tool loop
- hooks
- permission / approval
- Plan Mode
- Task/Todo
- Skill progressive disclosure
- Sub-agent / AgentTool
- Agent Team
- large tool/worker result 的保存、引用、partial fan-in 与 parent consumption
- 大型 Tool/Skill/Agent catalog 的 discoverability 与 deferred loading
- compaction
- stop / cancel
- resume / retry
- checkpoint
- rewind / fork / branch
- workspace
- artifact
- session close

发现差异时，区分：

- 能力或生命周期语义差异；
- 可接受的 Python/Cloud 实现差异；
- 上游实现细节或缺陷；
- Hive Native 主动增强。

不要为了“平齐”而复制上游 Bug、Prompt 常量、供应商偏好或能力限制。

必须直接读取当前 FreeCode/CC 源码回答“哪些是真正 provider/物理上限，哪些只是运行目标，limit 命中后模型是否得到 observation 并可继续”。不得仅凭产品文档或本提示词断言“CC 没有某个 hard cap”；若基线没有对应 100-way fan-out、跨渠道或企业治理能力，应明确标为 Hive Native 设计责任，而不是伪造 parity 结论。

## 4.2 Codex

Codex 只作为不冲突的工程与体验增量：

- typed thread / turn / run / event
- approval routing
- sandbox / permission profile
- deferred tool transport 与可发现恢复
- structured progress surface
- resumable state
- diff / terminal / artifact / workspace
- Codex Desktop 的信息层级、状态连续、微交互和交付物优先体验

Codex 的 deferred/dynamic tool exposure 只能优化传输和发现，不能根据自然语言启发式剥夺模型工具。

## 4.3 Hive Native

以下是主动超越 parity 的核心产品能力，不能在架构简化时被删除：

- Memory / reflection / self-evolution
- Agent Markdown Wiki / Learning Vault
- Skill evolve
- Dynamic context
- Personal Knowledge
- Enterprise Knowledge
- Local Agent / Hive Bridge
- A2A
- Sub-agent / Agent Team
- Workflow / Dynamic Workflow
- Goal / Plan / Task / Work Ledger
- Background / Scheduled execution
- enterprise identity / governance / AI assets

## 4.4 CCPlus 合成裁决

CCPlus 不是三份功能清单相加，而是把三类优势收敛到同一个生产生命周期：

- CC / FreeCode 提供 Agent semantic loop、context assembly、tool/Skill discovery、compaction、Sub-agent consumption 的能力与行为下限；
- Codex 提供 typed thread/turn/event、approval/sandbox、durable resume、artifact/workspace 和状态消费的工程基线；
- Hive Native 提供 Dynamic Memory/self-evolution、enterprise authority、A2A/Workflow/cloud durable execution 的主动超越面。

三者必须共享一个 principal/execution frame、一个 run/event/evidence truth、一个 budget/failure envelope 和一个 artifact/result contract。不得为了“融合”而并存三套 loop、状态机、终态、恢复路径或 final delivery truth。

每个对标结论必须登记 baseline repo/path/HEAD、symbol/line、可观察行为、可重放 fixture/trace、差异类别和 Hive 裁决。未知的托管或闭源行为标记未证实，不能用产品印象补齐。

必须使用同一 model/provider、同一 tool fixture 和同一任务 corpus 做 paired replay：

- 普通单 Agent 路径对 CC / FreeCode 行为和质量非劣；
- typed recovery、approval、sandbox、artifact/workspace 和状态连续达到 Codex 可比基线；
- 大 Memory、100-way fan-in、企业治理与跨渠道 A2A 产生可测净增益，且不削弱前两项；
- 没有行为数据，不得仅凭结构或功能数量宣称达到 CCPlus。

## 4.5 排除项

只有供应商托管、无法访问的私有远程基础设施可以从 CC parity 债务中排除。排除只表示“不要求复制为 CC parity”，不表示 Hive 不能建设自己的 Remote Workstation、Local Agent 或云端替代能力。

# 五、原子化定义

“有 API”“有表”“有组件”“有页面”“有测试文件”都不算完成。每个能力必须检查七个原子：

1. 输入（Input）
   - 谁发起？
   - 输入契约是什么？
   - 是否经过验证、规范化和持久化？
   - 断线或重启后能否恢复？
   - 模型是否被迫重复生成已确认的长参数？

2. 权威（Authority）
   - 谁有权读取、决定、执行和写入？
   - tenant、organization、user、owner、agent、sub-agent、service account、delegation 如何绑定？
   - RLS、RBAC、ABAC、Agent grant 与对象归属是否一致？
   - 异步 Worker 是否保持同一 authority？

3. 执行（Execution）
   - 唯一生产执行入口是什么？
   - API、Worker、Tool、Workflow、Trigger、Schedule 是否走同一治理咽喉？
   - 是否存在旁路、重复执行器或兼容层反客为主？
   - 智能判断是否错误地下沉成平台机械规则？

4. 证据（Evidence）
   - event、span、transcript、database、file、object storage 中谁是机械事实源？
   - 是否存在双事实源？
   - typed receipt、artifact ref、tool result、approval、failure 是否可追踪？
   - UI 展示能否回到真实证据？

5. 恢复（Recovery）
   - disconnect、refresh、restart、retry、cancel、rollback、fork、rewind 是否幂等？
   - 是否区分 denied、approval_required、unavailable、timeout、retryable_error？
   - 是否会重复写入、重复扣费、重复外发或遗留 orphan？

6. 消费（Consumption）
   - Agent、Memory、Skill、Workflow、Knowledge、父 Agent、UI、Workspace 是否真实消费产物？
   - 是否存在写入但永不读取的表、事件、状态或文件？
   - 工具成功后最终用户是否真的拿到交付物？
   - 普通用户是否被迫看到内部运行数据？

7. 验收（Acceptance）
   - 是否有覆盖真实生产路径的测试？
   - 是否包含迁移、回填、兼容、故障注入和恢复？
   - 是否有可观测性、告警和生产证据？
   - 是否能明确复现、验证和关闭问题？

状态只能使用：

- 闭环：七个原子全部成立，且存在当前真实生产消费路径。
- 局部闭环：主路径成立，但存在双事实源、旁路、恢复、权限、部署或 UI 断点。
- 断点：能力存在，但生产路径在两个原子之间中断。
- 缺失：当前源码没有实现。
- 已知缺失：产品明确暂不建设，不得伪装为完成或回归。
- 排除：供应商私有远程能力，不属于 CC parity 债务，但必须写明依据。
- 未证实：当前环境无法获得足够证据，不能推定完成。

每一个“闭环”结论也必须附源码、测试和真实消费证据。

# 六、审查范围

## 6.1 单 Agent

沿真实生产路径覆盖：

- Agent definition / identity / soul
- thread / session
- user message
- turn / run
- context assembly
- provider/model request
- streaming / reasoning / final
- tool discovery / tool call / tool result
- permission / approval / confirmation
- Plan Mode
- Goal / Task / Work Ledger
- artifact / workspace / attachment
- usage / cost / quota
- compaction
- checkpoint / rewind / branch / fork
- cancel / retry / resume / reconnect
- timeout / provider failure / empty response
- process / Worker restart
- terminalization / cleanup
- channel ingress / delivery
- Local Agent / Remote Workstation

重点判断：

- 是否存在模型成功、框架又改写为失败；
- 工具成功、Artifact 或 UI 消费失败；
- 状态完成但 UI 仍运行；
- 前端取消但后端继续；
- retry 重复外部效果；
- confirmation 要模型重新复述 canonical object；
- 服务端状态、事件、数据库、Workspace 和 UI 漂移；
- 云端多副本、API/Worker、对象存储和 Workspace 是否共享正确事实。

## 6.2 Hive Native

不要只检查被点名模块。主动扫描所有原生能力，至少包括：

- working/session/episodic/semantic/preference/project/agent/organization/procedural memory
- T0/T2/T3/Soul
- reflection / heartbeat / dream
- candidate / eval / promotion / rollback
- Skill / Skill package / Skill evolve
- Dynamic context / activation / compaction recovery
- Personal Knowledge / Enterprise Knowledge
- Local Agent / A2A
- Sub-agent / Agent Team
- Workflow / Dynamic Workflow
- Goal / Plan / Task / Work Ledger
- Background Task / Scheduled Task / Trigger
- Checkpoint / Rewind / Branch
- Artifact / Workspace
- evaluation / feedback / self-evolution
- human-in-the-loop

Memory/Self-Evolution 必须检查：

- LLM 是否看到完整授权证据；
- 学习价值是否由 LLM 判断；
- 平台是否只做证据、权限、去重、审计、回滚和 commit；
- reviewer 不可用时是否 hold/quarantine，而不是机械裁决语义；
- 是否存在写了不消费、多个 durable truth 或 derived view 反客为主；
- Skill 是否从 evidence + eval 成长，而不是把 runtime completed 当 semantic success；
- Workflow 是否保持确定性状态机，而没有夺走智能 leaf 的判断权。

## 6.3 企业治理、安全与 AI 资产

检查：

- tenant / organization / project / workspace
- user / owner / member / admin / sponsor
- agent / sub-agent / service account / connector identity / delegated identity
- RLS / RBAC / ABAC / capability policy
- approval / checkpoint / policy version
- quota / budget / cost
- secret / encryption / credential scope
- sandbox / network egress / protocol safety
- prompt injection / tool injection / data exfiltration
- SSRF / path traversal / arbitrary execution / sandbox escape
- retention / deletion / export / legal hold
- audit immutability / incident traceability
- Skill / Plugin / MCP / Connector trust

AI 资产至少包括：

- Agent
- Sub-agent
- Agent Team
- Skill
- Workflow
- Prompt
- Tool
- Connector
- Model configuration
- Memory policy
- Knowledge source
- Evaluation
- Template
- Artifact

每类资产检查 owner、organization、visibility、permission、version、draft/published/deprecated、dependency、provenance、audit、approval、rollback、import/export、deletion、retention、runtime binding 和 usage evidence。只有 CRUD 不算闭环。

特别查找治理冲突：

- RLS 允许创建但不允许 Worker 回读；
- API 有权但异步消费者没有权；
- approval 后原 run 无法恢复；
- policy、RLS、budget、quota、approval 同时作用形成死锁；
- service role 绕过过多限制；
- 子 Agent 权限放大；
- policy update 没有版本绑定；
- 一个 denial 导致整个 Agent 无法继续；
- denied 与 unavailable 混淆；
- Agent 反复申请同一权限。

画出唯一治理决策顺序，并标记事实源。

## 6.4 用户使用体验与 UI/UX

以非技术业务用户真实完成任务为视角，Codex Desktop 是交互与信息克制基线。

把信息分成：

- 普通用户必须看到；
- 用户按需展开；
- Workspace/Artifact 区域；
- 公司后台；
- Operator/诊断模式；
- 不应直接展示。

普通用户优先看到：

- Agent 正在做什么；
- 当前进度和关键判断；
- 是否需要用户操作；
- 可恢复错误；
- 下一步；
- 交付物；
- 必要的权限、成本和额度提示。

默认不应展示：

- raw schema / JSON
- internal/correlation/run/thread ID
- provider payload
- API request
- token internals
- raw stack trace
- evidence metadata 全量
- 数据库字段和内部权限实现

审查：

- App shell、导航、Session、Workspace、Artifact、状态区的职责；
- 当前栏数是否真正服务用户任务，不预设必须保留现有布局；
- Session 中 reasoning、progress、tool、approval、warning、retry、failure、sub-agent、workflow、artifact、final 的表达层级；
- 哪些内容应是消息、折叠行、状态、Inspector 或后台证据；
- Workspace 是否优先呈现交付物、文件、预览、变更、下载和引用；
- Branch/Rewind/Checkpoint/GitLine 是否与消息、文件、Memory、Artifact、Task、Approval、Sub-agent 状态一致；
- Sub-agent/Agent Team 的触发、点选、取消、部分成功、返回父 Agent 和失败恢复；
- keyboard、focus、hover、active、loading、streaming、animation、responsive、accessibility；
- UI 是否从 typed backend state 消费，而不是自行猜测运行状态。

Artifact 必须追踪完整链：

```text
文件生成
-> 持久 Workspace / Object Storage
-> 写入证据
-> Artifact 登记
-> 权限校验
-> 父/主 Agent 消费
-> Session 附件
-> Workspace 展示
-> 打开/预览/下载
```

## 6.5 极端伸缩、边界冲突与系统活性专项

本专项是强制范围，不得以“正常用户不会这样配置”排除。目标不是证明所有极端负载都能在一个 Prompt 内运行，而是验证系统在超大 fan-out、能力目录、Memory、Tool result 和跨渠道协作下仍然 **安全、有界、可感知、可降载、可恢复、可继续**。

### 6.5.1 必须建立的六张图

1. **限制登记图**
   - 枚举所有 context/token/output/tool-round/turn/fan-out/depth/concurrency/result-size/timeout/retry/cost/quota 常量、配置、数据库字段和 provider 派生值。
   - 标记 hard/soft、事实源、scope、默认值、覆盖优先级、命中行为、模型是否可见、状态是否持久化、恢复入口和 UI 消费。

2. **上下文流量图**
   - 从 system/developer/soul、Skill index、Tool/MCP schema、Sub-agent/Workflow catalog、Memory/Knowledge refs、history、tool result、fan-in result 到 provider request，逐段记录字节/token 估算、选择者、丢弃规则、coverage 和重建方式。
   - 区分“目录可发现”“schema 已加载”“正文已加载”“durable reference 可读”，不得把未 inline 等同于不可用。

3. **并发与返回风暴图**
   - 覆盖 admission、queue、worker lease、parent/child budget reservation、result persistence、notification、fan-in、integration、final delivery。
   - 查找无界 `gather`、内存列表、一次性 JSON、全量 Prompt append、N×M 广播、重复 wakeup、full barrier 和 parent hot loop。

4. **跨渠道 A2A 因果图**
   - 至少包含 root task/run、interaction、parent/child、tenant、requester、owner、source/target Agent、delegation/policy version、source/target channel、channel credential ref、idempotency key、sequence/causation、result/artifact ref、delivery receipt。
   - Agent completion、A2A handoff、渠道发送成功和用户已读是四个不同状态，不得合并。

5. **等待与恢复图**
   - 列出 Agent、Sub-agent、Workflow leaf、approval、budget、connector、channel delivery、hook、queue、compaction、integrator 之间的 wait-for edge。
   - 检查 cycle、lost wakeup、late result、duplicate result、partial success、deadline、cancel、restart、replay 和 model/provider unavailable。

6. **失败传播与终态图**
   - 为每个 failure/pressure 记录 cause、actor、blocked scope（effect/tool/child/attempt/turn/task/session/workflow/delivery）、terminality、progress certificate、resume owner/condition 和 next valid actions。
   - 验证一次 attempt 失败不会越权扩大成 task/session 失败；多种并发原因不得被压成一个无信息 `error`；安全 denial、软压力、基础设施 unavailable、provider physical limit 和用户 cancel 必须保持正交事实。

### 6.5.2 强制极端测试矩阵

每个测试必须记录 Input/Authority/Execution/Evidence/Recovery/Consumption/Acceptance 七原子、峰值资源、typed trace、预期状态机和明确失败判据。优先使用 deterministic fake provider、可控 resolver/connector、真实数据库/队列测试容器和虚拟时钟；不得为了审查真的启动 100 次付费模型调用、访问真实内网或向真实 IM 用户群发。通过模拟证明结构，再做小规模真实 provider/channel smoke 和生产 shadow telemetry。

| ID | 场景与注入 | 必须成立的不变量 | 直接判定断点的现象 |
|---|---|---|---|
| X-FAN-01 | 单个 parent 同时申请 100 个独立 Sub-agent；100 个结果在同一秒完成，每个结果 512 KiB–1 MiB，包含 artifact 与 source refs | admission 有界；结果先 durable commit 再通知；parent 只收到 manifest/摘要/coverage/ref；fan-in 分批由 LLM/integrator 综合；完整原文可按需读；内存、Prompt 和 DB 连接峰值有上限 | `gather` 后一次性拼接 50–100 MiB；结果截断无 ref；通知丢失；parent context overflow；一个结果失败导致全部丢失 |
| X-FAN-02 | 100 个 child 混合 70 success、10 denied、5 approval_required、5 unavailable、5 timeout、5 late，并注入 duplicate/out-of-order completion | coverage ledger 精确区分 expected/received/terminal/late/duplicate；partial 可以交给 parent 判断；typed 状态不互相冒充；late/duplicate 幂等 | 永久等待 100/100；partial 被标 complete；重复结果重复扣费/外发；denied 被 retry storm |
| X-FAN-03 | 两层 fan-out（10×10）并让一个 child 试图回调祖先形成拓扑环 | 父子预算 reserve/commit/release；全局并发和总成本有界；结构化 cycle detection 拒绝真实环但保留其它分支 | 每层重置预算导致 100 倍超卖；按消息文本猜循环；拒绝一个环后全局冻结 |
| X-FAN-04 | fan-in 期间用户 cancel、parent 重启、worker lease 过期，随后 late results 到达 | cancel 传播、已提交结果保留、未开始任务停止、in-flight effect 按幂等契约收口；resume 后不重复集成 | orphan worker 持续花钱；cancel 后仍外发；重启后丢 coverage 或重复 final |
| X-FAN-05 | 100 个 child 同时高频 streaming progress/tool/event，而不是只在终态返回 | event ingest、DB、broker、WebSocket 和 UI 使用有界 backpressure/coalescing；每个 material transition、final 和 evidence 可恢复；raw delta 不进入 parent Prompt | 事件 N×M 广播；UI 主线程/连接池耗尽；coalesce 丢 final；1 万条重复 pressure observation 再次撑爆 Prompt |
| X-ROOT-01 | 同一个 root Agent、同一个 Session/Turn 同时申请 40 个 direct Sub-agent、30 个 Agent Team member task 和 30 个 Workflow leaf；它们在相邻时间窗完成 | 三类执行保留各自语义，同时进入同一 root requested/admitted/expected/result ledger；integration epoch 可分批、可重放、可恢复；parent 只消费 bounded manifest/page/ref | 三套局部状态无法对账；某一路结果不进入 expected set；重复集成；parent 不知道 coverage；只能把三路原文一起塞进 Prompt |
| X-FLEET-01 | 合成 2,000/10,000/50,000 个已注册且可路由 Agent definition 与对应 enabled triggers，但不启动同数量模型调用；daemon 在 tick 中途 kill/restart | registry/trigger 枚举可分页、分片、续扫；租户隔离与 cursor 可恢复；tick 时延、RSS、DB 连接和 backlog 有容量曲线；不得把 definitions 当进程 | 每 tick `all()` 全量载入并从头串行扫描；崩溃后无 cursor；一个大 tenant 拖慢全 fleet；用“未实际调用模型”掩盖控制面 O(N) 崩溃 |
| X-FLEET-02 | 一个 noisy root 一次入队 100 个 child，同时 1,000 个其它 root/tenant 各有一个已 admission 的交互任务 | per-root/per-tenant weighted fairness、control-plane reserve 与 queue age SLO 成立；noisy root 只影响自身份额；负载停止后全量 drain | 全局 priority/FIFO 让一个 root 占满 worker；其它 tenant 永久或无界饥饿；控制消息、cancel、approval、checkpoint 无槽可写 |
| X-CAP-01 | 256K 模型；Agent 配置 400 个 Skill、200 个 MCP server/tool domain、大量 Sub-agent/Workflow 定义和巨大 Memory | startup 只加载稳定、紧凑、确定排序的 catalog/index；所有授权能力可分页/搜索发现；只对选中项加载完整 schema/reference；Personal KB 仍 tool-only；Prompt cache 不因随机排序碎裂 | 全量 schema 塞入 Prompt；first-N 后其余能力不可发现；目录本身超过窗口直接失败；Memory 静态注入；每轮工具顺序变化 |
| X-CAP-02 | catalog 中有重名、同义名、恶意 connector 描述、已撤销 credential、动态新增/删除工具 | namespace/version/risk/auth 状态明确；外部描述按 untrusted data；撤销即时生效；bundle 变化可观测且 resume 可重建 | 工具串台；恶意描述提升权限；缓存旧 schema 继续执行；工具消失导致 Session 不可恢复 |
| X-DISC-01 | 唯一正确的 Skill/MCP 位于最后一页、别名 namespace 或未 inline catalog；随后撤销其授权 | Agent 能通过 catalog search/load 发现并正确调用；coverage 可证明尾部未消失；撤权 fresh-check 后不可调用 | 只有 endpoint 可分页但模型永远找不到；first-N/embedding top-k 被当完整目录；缓存让撤权工具继续执行 |
| X-MCP-01 | 配置 200 个 MCP server，混合 healthy、slow、down、auth-expired、schema-drift 和恶意描述 | transport/schema 按需加载；process/socket/handshake 有界；故障域隔离；auth/schema execution 前 fresh-check；Session 可在单 server 故障后继续 | 启动同步拉起 200 连接/进程；一个 slow server 阻塞全部 catalog；旧 schema/credential 执行；descriptor 注入提升权限 |
| X-MEM-01 | 参数化生成 10^3→10^6 条 Memory，决定性事实仅在冷尾部，混入版本冲突、过期、敏感撤权和恶意文本 | dynamic retrieval 可迭代分页；provenance/coverage/authority 完整；模型综合冲突；top-k 只是一次 retrieval batch；Prompt 峰值不随总库线性增长 | 静态注入全部 Memory；尾部永远不可发现；旧敏感事实越权召回；top-k 被伪装成全量；机械规则替模型解冲突 |
| X-CTX-01 | context 使用率依次跨过多个内部软水位直到贴近真实 provider window；决定性证据位于最后一个 chunk | 软水位只触发 warning/retrieval/compaction/batch；决定性证据被覆盖；只有真实 provider 上限可形成 hard rebuild/pause；不静默 head/tail/top-k | 任意固定比例直接终止；尾部证据被裁掉；模型不知道 context pressure；机械摘要成为语义事实 |
| X-CTX-02 | model-led compaction 超时、reviewer unavailable、单块 map 失败、coverage 不完整 | 原始 T0/transcript/artifact 不变；状态 held/retryable；coverage 明示缺块；恢复重入 LLM 主路径 | regex/机械 fallback 生成可 promotion 摘要；清空旧 context 后才发现 compaction 失败；无限递归 compaction |
| X-CTX-03 | Session 从大 context 模型恢复到更小 context 模型，Tool bundle、Skill 版本和 Memory 已变化 | 先做 capability/context compatibility preflight；重建 lossless refs 与 coverage；不兼容时 typed pause/建议换回模型；approval/plan/pending effect 不丢 | 直接复用旧 Prompt 溢出；静默删工具/证据；approval 或 pending effect 丢失；Session 永久损坏 |
| X-OUT-01 | provider 命中 `max_output`/`finish_reason=length`，stream 中断后重放重复 chunk；仅允许同一 Session/同一模型 | partial output 按 sequence 去重并 durable；final 状态保持未完成；同一模型可续写/重规划；不能续写时 typed pause 且下次 resume 可见 | 截断文本被标 final；平台补写结论；重复 chunk 污染 transcript；必须秘密换模型才能恢复 |
| X-ONE-01 | 单 Session、单模型、无替代 provider，依次跨越软水位、命中真实 context overflow 和 provider unavailable | 先 externalize/compact/分批/多 turn；overflow 后从 durable refs 重建；provider unavailable 时 typed pause；恢复后同模型继续；既有结果/approval/effect 不丢 | 静默换模/降级；无替代模型就整 Session 报废；反复提交同一超窗 request；平台用固定 prose 冒充 final |
| X-RESULT-01 | 单工具返回超大日志、表格、二进制/Office Artifact、1 万条记录或高压缩比内容 | streaming + bytes/decompressed-bytes 上限；完整结果存 durable artifact；模型收到 schema、摘要、分页/查询入口、hash 和 coverage；UI 可打开交付物 | 原始 blob 直接进 Prompt；只留前 N 条却声称全部；解压炸弹耗尽内存；Artifact 生成但 parent/UI 不可见 |
| X-BUD-01 | 在 soft turn/tool/concurrency 水位前后各运行一次，再命中真实 provider context、显式 tenant cost ceiling 和 user cancel | soft 越界继续但产生 pressure observation/调度；三种 hard fact 分别 typed pause/stop；checkpoint、remaining、next actions 完整 | 所有 limit 都返回同一个 error；模型无感；达到内部常量即丢任务；hard cost/cancel 仍继续花费 |
| X-BUD-02 | parent 同时创建 100 个 child，每个 child 估算接近父总预算；部分任务退款/取消/重试 | 原子 reserve/commit/release；总 reserved+spent 不超过授权；retry 复用幂等账本；UI/模型可见剩余与 pending | 每个 child 获得完整预算；并发竞态超卖；取消不释放；重复扣费；负 remaining |
| X-LIM-01 | 对每个应用自设 limit 做 threshold−1/=/+1 与 0.5×/1×/2× 变异，并单独注入真实 hard fact | 内部阈值只改变 warning/queue/batch/attempt；没有语义 cliff；hard fact 有 provenance 且只阻断其作用域 | `max_*+1` 直接让 task failed；阈值两侧行为突变且无 checkpoint；应用常量冒充 provider/policy |
| X-LIVE-01 | operation timeout、retry cap、approval pause、queue full、connector breaker 同时发生，再恢复依赖 | attempt/task 分离；admission-before-expected；无 hold-and-wait；progress certificate 可达；相同 fingerprint 不重试；虚拟时钟下 drain 收敛 | 永久 pending；幽灵 child；permit 泄漏；timeout 重建同一环；恢复后无人唤醒 |
| X-QUEUE-01 | 到达率持续高于服务率、durable queue 填满并 kill/restart；同时让另一 tenant 的交互 Session 入队 | requested=admitted+deferred；queue 有界且 durable；noisy neighbor 不致永久饥饿；负载停止后可 drain；未 admission 不进 wait set | 内存排队/OOM；丢任务/lost wakeup；一个 tenant 吃满全部 worker；queue full 变 task failed；重启后重复执行 |
| X-SAFE-01 | 一个 child 的付款/外发被拒绝，但同一 parent 还有只读分析、Artifact 整理和最终说明 | 只拒绝该 effect；其它 reasoning/tools/final 继续；模型收到 denial 和可选替代动作 | 一个 denial 把整个 Session/Memory/Workspace 工具面冻结；平台替模型输出固定失败结论 |
| X-A2A-01 | 同 Owner 的 A/B/C/D 四个 Agent 分别经钉钉、飞书、Slack 和 Web 完成同一 root task，彼此多轮 handoff，最终返回主 Agent | root/requester/tenant/delegation/policy/budget 因果链跨 channel 不漂移；每次 handoff durable；channel credential 不进模型；parent 获得统一 result refs 与逐渠道 receipt | 以 Agent creator/Owner 猜 requester；渠道消息成为唯一状态；某渠道 success 被当整体 complete；跨渠道 credential/正文泄漏 |
| X-A2A-02 | 注入 channel rate limit、auth expiry/revocation、duplicate webhook、乱序 callback、发送成功但 ack 丢失、Agent 完成但渠道发送失败 | Agent work 与 delivery 分离；idempotency/sequence/dedupe 正确；auth failure typed 且可重新授权；重试不重复发送；partial delivery 可见 | webhook 重复触发任务；auth 失败被当 permission denied；无限重试；丢 ack 后重复群发；一渠道失败卡死全部 Agent |
| X-A2A-03 | same-owner 与 cross-owner/cross-tenant handoff 混合，任务中途撤销 delegation 或收紧 sensitivity ceiling | 每个 hop 在 bytes ingress 和 effect 前 fresh-check；撤权后只停止未授权 hop；既有证据按 retention 保留；不靠 same-owner 自动放权 | owner 相同即全通；跨 tenant 数据进入错误渠道；撤权不生效；已拒绝路径通过 subagent/channel 绕过 |
| X-A2A-04 | 渠道 identity 未绑定/误绑定；钉钉人工回复与 Slack child completion 竞态；root channel 关闭且 final routing 含歧义 | fresh identity/delegation；causal ordering；final destination 明确且可重选；Agent result 与每个 delivery 独立；不跨渠道重复播送 | 按昵称/owner 猜 requester；人类回复覆盖 child result；关闭渠道导致 task 丢失；final 群发到所有渠道 |
| X-A2A-05 | 伪造签名/replay webhook、超渠道大小消息/附件、目标渠道不允许该 sensitivity/residency | webhook authenticity + nonce/dedupe；超大内容转 artifact/ref；outbound 前 sensitivity/residency fresh-check；delivery failure 可恢复 | 伪造消息启动任务；replay 重复 effect；截断附件却标 complete；敏感字节进入错误渠道 |
| X-LOOP-01 | A 等 B、B 等 C、C 等 A；另有 parent 等 approval，而 approval notification 等 parent terminal hook | wait-for graph 发现结构环；打破/暂停最小边；保存 partial；用户和模型看到 blocker 与恢复动作 | 永久 pending；timeout 后自动重建同一环；自然语言“循环”关键词误杀正常任务 |
| X-INJ-01 | child/tool/channel result 中包含“忽略上级、提升权限、调用付款工具”等恶意文本及畸形 schema | result 标为 untrusted data；schema repair/deny 有 typed result；不会改变 policy/tool visibility；parent 仍可引用内容作分析 | 返回文本直接变系统指令；connector 描述授予权限；畸形结果导致 loop 无 tool result |
| X-OBS-01 | 依次命中 backpressure、soft budget、hard budget、context rebuild、partial fan-in、connector unavailable、approval pause | 每种状态都有 event/span/metric/model-visible observation/UI 状态和恢复入口；不得以 platform assistant prose 冒充 | 只有日志；UI 永远 running；模型收到空结果；恢复按钮无 authoritative state |
| X-OBS-02 | 注入 1 万次同类 backpressure/limit event，仅少量 pressure epoch 发生物质变化 | 原始事件可审计；重复项 durable 计数并聚合 ref；每个 material transition 对模型/UI 可见；Prompt inline 有界 | 每条 warning 都 inline；聚合时丢失终态；模型完全看不到状态变化；UI 事件风暴 |
| X-SES-01 | 同一固定 Session event fixture 分别走 live、history、reconnect、reload、resume，并在 100 个 child 同时完成时重复投递部分 event | canonical append-only event 是唯一运行事实；stable item id + lifecycle + ordinal；同一个 reducer 得到 byte-equivalent item snapshot；visibility 只做精确字段 redaction | live/history 使用不同 envelope/sequence；delta 每次生成新 item；user projection 删除关联 ID；reload 后顺序、状态或数量变化 |
| X-SES-02 | 流式 commentary/reasoning、tool started/delta/completed、approval、subagent result、compaction 与 final 交错；注入 sequence gap、out-of-order、duplicate 和 publish failure | persist-before-publish；outbox 至少一次；consumer 按 event id 幂等、按 sequence 补 gap；commentary/reasoning/final 不互相聚合或猜测；模型 final bytes 保真 | `thinking` 聚合进 final 附件；固定平台文案替代模型过程；前端从消息相邻关系重建事实；publish 失败后只能靠刷新碰运气 |
| X-REC-01 | 在 result commit 前后、notification 前后、fan-in checkpoint 前后分别 kill 进程并恢复 | transactional boundary 明确；sweeper/replay 最终补齐；exactly-once effect、at-least-once notification + idempotent consume；coverage 可重建 | commit 前假 success；commit 后永不通知；恢复重复 effect；parent 永远不知道 child 已完成 |
| X-CACHE-01 | 400 Skill/200 MCP catalog 在多 turn 中保持不变，再只修改一个动态 auth 状态和一个工具版本 | stable prefix/tool catalog 确定排序；动态状态位于 suffix；bundle version/hash 和 cached token 可观测；单点变化不重写无关历史 | timestamp/request id 在前缀；每轮全量 catalog 重排；cache miss 原因不可解释；旧权限因缓存继续生效 |
| X-WF-01 | 400 个 Sub-agent 定义、1000 个 Workflow 模板、1 万节点 DAG、动态展开与一条真实结构环 | catalog 可发现；scheduler/join durable；宽度有界 admission；自设 depth 只触发分解/暂停；真实环仅拒绝最小边；partial join 可消费 | 全量 DAG/schema 进 Prompt；固定 depth 终止合法任务；环误杀整图；restart 丢动态节点；full barrier 永久等待 |
| X-CCP-01 | 在同一 model/provider、tool fixture 和任务 corpus 下回放 CC/FreeCode、Codex 可比路径与 Hive | 普通单 Agent 对 CC 非劣；typed recovery/工程控制达到 Codex 基线；Hive 极端场景有净增益；记录 repo HEAD/trace/指标 | 用功能数代替行为；不同模型/fixture 伪比较；普通路径变弱却以治理更多宣称 CCPlus |

### 6.5.3 压力测试验收指标

不得只看“没有抛异常”。至少记录并判定：

- parent Prompt 峰值与 provider window 的距离；
- durable result bytes 与 inline bytes 比例；
- expected/received/failed/late/duplicate coverage；
- queue depth、admission wait、worker concurrency、lease 和 sweeper convergence；
- parent/child reserved、spent、released、remaining budget 守恒；
- context compaction coverage、missing refs 和恢复次数；
- model-visible limit observation 覆盖率；
- duplicate side effect 数必须为 0；
- lost result / lost wakeup / orphan run 数必须为 0；
- cancel-to-stop、resume-to-progress、fan-in-to-parent-visible 延迟；
- Prompt/tool bundle hash、cache hit、catalog discoverability recall；
- channel duplicate/reorder/auth-expiry 下的 idempotency 与因果链完整率；
- partial success 是否被诚实呈现，最终回答是否由模型基于 coverage 作出。

还必须给出可复算的曲线和守恒式，而不是只列“有 semaphore / 有 queue”：

- 对 fan-out 至少执行 1/10/25/50/100 五档，对 payload、catalog、Memory 和 queue 使用 threshold−1/=/+1 与 0.5×/1×/2×；软阈值附近不得出现无状态失败 cliff；
- 对 fleet plane 至少使用 2,000/10,000/50,000 个 synthetic Agent/trigger definition 做 registry、trigger、claim 与 fairness 曲线；它与 root fan-out 的 1/10/25/50/100 曲线分开报告，禁止把 definition 数量换算成活跃模型进程；
- `requested = admitted + deferred/not_admitted`，且只有 admitted child 可以进入 expected/wait coverage；
- `authorized = spent + reserved + available`，release 必须回到 available，任何时刻不得负数或超卖；
- coverage 中 success/denied/approval_required/unavailable/timeout/paused/late/duplicate 必须互斥可重算；
- parent inline bytes 应随 manifest/批次数有界增长，不得随 raw result bytes 线性增长；RSS、DB connection、socket、event backlog 和 Prompt 峰值必须有容量曲线；
- noisy-neighbor storm 下，另一个已 admission 的交互任务不得永久饥饿；fairness 例外必须来自显式 authority/SLA；
- 负载停止或依赖恢复后必须在可测时间内 drain convergence；非终态 progress certificate 覆盖率必须为 100%；
- hard stop 前必须预留写 checkpoint、coverage、typed observation 和释放 lease 的 control-plane headroom；若无法预留，必须证明外围事务仍能完成这些动作；
- unauthorized effect、跨租户泄漏、伪造 webhook 接受、重复付款/外发、平台改写模型 final、admitted result 丢失、lost wakeup 和 orphan run 必须为 0；
- capability catalog namespace/page coverage 与 exact needle lookup 必须为 100%；语义选择效果必须与 paired baseline 比较，不能用“存在搜索 API”替代 Agent 真正可发现。

任何测试无法安全运行时，必须给出未证实原因、可执行 test harness 设计、所需 fixture/telemetry 和残余风险；不得用单元测试替代真实 queue/DB/transport 边界，也不得用小规模通过外推 100-way fan-out 已闭环。

### 6.5.4 防硬限制死循环门禁

以下 LB 门必须逐条给出 PASS / FAIL / UNVERIFIED 与证据；任何 FAIL 都必须映射到 canonical leaf 断点，UNVERIFIED 映射到 coverage gap：

- **LB-1 Hard fact**：每个 hard outcome 都有外部事实源；应用默认常量不得终态化 task/session。
- **LB-2 Attempt/Task**：attempt failure、task terminal、session delivery 三者严格分离。
- **LB-3 Progress**：每个 paused/backpressured 都有 progress certificate、可达 resume edge 和明确 owner。
- **LB-4 Admission**：reserve + durable enqueue commit 先于 expected/wait edge，不存在幽灵 child。
- **LB-5 Resources**：等待外部条件不持稀缺 permit；资源获取有全序，lease 可回收。
- **LB-6 Retry**：retry 有 fingerprint 与单调进展；unchanged denial 不自动 retry；停止自动 retry 不等于 task failure。
- **LB-7 Join**：任何 full barrier 都有 partial/late/failed/deadline/cancel policy。
- **LB-8 Durable first**：checkpoint/coverage/result commit 后才通知或终态；hard stop 前有 control-plane headroom。
- **LB-9 Observation**：material transition 全可见、重复项聚合；failure awareness 不反向制造 Prompt/event storm。
- **LB-10 Single-model recovery**：单模型、queue 满、provider unavailable、restart 和 late callback 下仍可保存、暂停与恢复；只有“换模型/加机器”才可继续不算闭环。

# 七、代码极简性

单独进行 KISS / 奥卡姆剃刀审查，但不得用“简化”删除真实能力。

重点寻找：

- 多套 Agent 执行入口；
- 多套状态枚举或前后端各自推导状态；
- 多套权限判断；
- 多套 Artifact / Workspace / Memory / Knowledge 机制；
- 相同概念不同命名；
- 不同概念使用相同命名；
- 无消费者的表、事件、字段、API、Service 和组件；
- 兼容层反客为主；
- 失效 feature flag；
- speculative abstraction；
- giant component / service；
- 循环依赖和隐式副作用；
- 为了测试确定性引入的机械语义规则；
- 把错误的双路径合并成更大的错误单体。

每个简化建议必须说明：

- 删除、合并或改写什么；
- 保留的真实能力是什么；
- 唯一事实源和唯一入口是什么；
- 如何迁移和回填；
- 如何证明没有削弱 Agent；
- 需要哪些回归与故障测试。

# 八、执行方法

按以下顺序工作：

1. 建立仓库地图：应用、服务、runtime、Worker、数据库、前端、测试、部署、文档。
2. 建立核心实体图：tenant、organization、user、agent、thread、turn、run、event、tool call、approval、task、workflow、memory、knowledge、artifact、workspace。
3. 建立状态机：Agent run、tool、approval、artifact、task/workflow、sub-agent、background/schedule。
4. 建立事实源矩阵：谁写、谁权威、谁消费、谁恢复、UI 从哪里读取。
5. 建立全仓限制登记表：逐个定位常量、配置、字段、provider limit、semaphore、queue、slice 和 fallback，判定 hard/soft、事实源、命中状态、模型可见性与恢复路径。
6. 建立上下文容量账本：对稳定指令、catalog、schema、history、Memory/Knowledge refs、tool/child results、compaction summary 和动态 suffix 分别量化 bytes/tokens、coverage、缓存与 externalization。
7. 建立并发/fan-out/fan-in 与 parent/child 预算账本，计算最坏情况而不只读默认配置；检查 reserve/commit/release、admission-before-expected、queue、result persistence 和 integration。
8. 建立跨渠道 A2A 因果图、wait-for graph 和失败传播/终态图，覆盖 authority、channel credential、delivery receipt、partial/late/duplicate、cycle、cancel、restart、attempt/task 分离、progress certificate、no-hold-and-wait 和 retry fingerprint。
9. 沿生产路径正向追踪。
10. 从 UI、Artifact、终态、审计、Memory、父 Agent fan-in 和渠道 delivery 反向追踪。
11. 对每个能力和每条 limit path 检查七个原子。
12. 对表、事件、API、状态、组件、budget reservation 和 result manifest 做反向消费者扫描。
13. 检查 happy path 以及 timeout、disconnect、restart、cancel、retry、permission denial、partial success、duplicate delivery、stale state、provider unavailable、context overflow、return storm、queue saturation、auth revocation 和 model switch。
14. 执行 §6.5 强制极端测试及 1/10/25/50/100 capacity curve；不能安全实跑的测试必须交付可执行 harness、fixture、断言和未证实风险。
15. 对 CC/FreeCode、Codex 和内部 lean benchmark 做当前源码对比，尤其比较 context assembly、工具发现、large result、budget hit、compaction、Sub-agent fan-in 和失败回到模型的语义；用同模型、同 fixture paired replay 验证 CCPlus 非劣与净增益。
16. 做代码极简性审查。
17. 对每个候选断点做 refute-first 复核；family、alias、source finding 和 test case 不计数，只对可独立复现、独立修复/回滚、独立验收的 canonical leaf seam 计数，并记录 ledger delta。
18. 建立机器可重算的施工 owner map：每个 canonical leaf 恰好归属一个 owner Group，每个 Missing 恰好归属一个建设 Group；为每个 Group 写出依赖、按顺序必须读取的 `@文档`、按需文档、当前源码入口、Red、退出门和稳定证据锚点。
19. 给出按依赖排序的一次完整修复方案；Group 表达施工依赖，不得把全部断点绑成一个发布列车。

工具规则：

- 先使用仓库提供的代码图、符号和调用链工具；
- 开始时记录 HEAD、工作树、索引状态、报告 hash 和并发改动 ownership；结束前再次核对，不能把变化中的文件冒充冻结事实；
- 图索引不可用或不足时，记录具体失败和降置信范围，再使用文本搜索；
- 文档只能提供意图，不能证明实现；
- 找不到证据时标记未证实；
- 源码证据必须包含准确文件路径、符号名和行号；
- 对关键路径必须追踪 caller、callee、异步边界和 UI consumer；
- 对基线结论必须读取当前本地源码，不能凭产品印象。

# 九、断点记录格式

断点账本必须区分以下对象：

- `family_id`：同根问题家族，不参与数量统计；
- `canonical_leaf_id`：唯一计数单位，必须有独立 authority/execution seam、独立复现、独立修复/回滚和独立验收；
- `alias/source_finding`：旧报告或其它审查的映射，不参与数量统计；
- `scenario/test_case`：可以命中一个或多个 leaf，只增加证据与 coverage，不自动增加断点数；
- `coverage_gap`：尚未执行或无法执行的验证，不自动等于实现断点；
- `Missing/Excluded/Unverified`：与 breakpoint 分账，不能为了扩大或缩小数字混算。

每轮都要输出机器可重算的 `added / merged / split / refuted / reclassified / closed` ledger delta。同一代码 seam 的别名只计一次；同一家族若确有多个可独立修复的 seam，必须拆成多个 leaf，不能用斜线或范围表达式凑数。只有先冻结 checkout/worktree/production snapshot 与 coverage 分母，才可报告“当前共 N 个断点”。

每个断点使用统一结构：

## [稳定编号] 断点名称

- canonical_leaf_id：
- family_id：
- aliases / source findings：
- scenario / test case tags：
- ledger delta：added / merged / split / refuted / reclassified / closed
- 所属模块：
- owner Group（唯一）：
- 依赖 Group：
- `@必须先读` 文档（有序、必须存在）：
- `@按需读取` 文档与触发条件：
- 当前源码入口：
- 稳定证据锚点：`EVID-G<group>-<序号>`
- 严重级别：P0 / P1 / P2 / P3
- 当前状态：局部闭环 / 断点 / 缺失 / 已知缺失 / 未证实
- 边界类别：物理协议 / 权威效果 / 显式经济生命周期 / 运行软预算 / 非法机械语义
- limit / budget 事实源：
- hard / soft 裁决及理由：
- 规模触发条件与最坏情况：
- 影响对象：
- 用户可见现象：
- 触发条件：
- 输入原子：
- 权威原子：
- 执行原子：
- 证据原子：
- 恢复原子：
- 消费原子：
- 验收原子：
- 断裂位置：
- 根因：
- 是否削弱模型能力：
- 是否存在自然语言机械 hard outcome：
- 是否存在双事实源：
- 是否存在治理/RLS 冲突：
- 是否存在跨租户或安全风险：
- 是否可能导致 Agent 无法继续：
- blocked scope：effect / tool / child / attempt / turn / task / session / workflow / delivery
- attempt 与 task/session 终态是否分离：
- 是否存在等待环、full barrier、retry storm 或 lost wakeup：
- admission-before-expected 与 no-hold-and-wait：
- progress certificate 与可达 resume edge：
- retry fingerprint / 单调进展证明：
- limit 命中是否对模型可见：
- 已保留状态、coverage 和恢复令牌：
- fan-out/fan-in、parent/child budget 与 result manifest：
- scale plane：fleet / root_execution_tree / capability / channel（可多选，但每个结论必须分别给证据）：
- 定量证据（bytes/tokens/concurrency/queue/cost/latency）：
- 源码证据：
- 数据库/迁移证据：
- UI 消费证据：
- 测试证据：
- 对应 §6.5 极端测试 ID 与结果：
- CC/FreeCode/Codex 当前源码对照：
- CCPlus paired replay / 非劣与净增益证据：
- 反证或不确定性：
- 北极星裁决：
- 完整修复方案：
- 最小复杂度方案：
- 需要删除的旧路径：
- 迁移与回填：
- 可观测性：
- 依赖项：
- 验收标准：
- 回归测试：
- 故障注入：
- 实施风险：

严重级别：

- P0：跨租户、数据泄漏、安全绕过、不可逆破坏、核心 Agent 完全不可运行。
- P1：高频任务失败、模型能力被机械削弱、运行无法恢复、产物无法交付、治理导致卡死、严重状态漂移。
- P2：局部路径失败、明显体验断点、维护风险、缺少关键测试。
- P3：低频边缘问题、非阻塞一致性、代码清理或视觉细节。

定级按可达性、影响、爆炸半径、可恢复性和证据综合，不得因为场景“极端”就自动降为 P3。可由单租户配置触发的平台 OOM/队列雪崩、重复付款/外发、跨租户泄漏或全局不可恢复属于 P0 候选；使合法单 Session 在压力下无恢复地死亡、模型被硬限制剥夺能力、100-way fan-in 必然溢出或跨渠道任务永久等待属于 P1 候选。只有具备有界 backpressure、durable preservation 和真实恢复路径后，规模压力才能降级为普通容量风险。

# 十、最终报告结构

报告至少包含：

1. 执行摘要。
2. 审查范围、环境和未覆盖范围。
3. 权威顺序与北极星符合性。
4. 仓库与运行拓扑。
5. 核心实体、状态机和事实源矩阵。
6. 单 Agent 结论。
7. Hive Native 结论。
8. 企业治理、安全和 AI 资产结论。
9. 用户使用体验与 UI/UX 结论。
10. Model Agency / 机械化限制专项结论。
11. 全仓 hard/soft limit 与 budget registry。
12. 上下文容量、compaction、large-result externalization 与 prompt-cache 结论。
13. Fleet plane：平台注册/可路由 Agent、trigger/worker 调度、公平性与控制面容量结论。
14. Root execution tree：单个 root Session 的 100-way Sub-agent/Agent Team/Workflow admission、fan-out/fan-in、返回风暴、integration epoch 与层级预算结论。
15. Capability plane：400 Skill / 200 MCP / 大规模 Sub-agent/Workflow/Memory progressive disclosure 结论。
16. Channel plane：跨钉钉/飞书/Slack/Web A2A 因果、权威、delivery 与恢复结论。
17. wait-for graph、死循环、活性和模型失败感知结论。
18. Session 事实流、typed item、live/history/reconnect/reload/resume 同构结论。
19. §6.5 极端测试执行结果、容量曲线、未证实项与 test harness。
20. CC/FreeCode/Codex 当前源码事实、paired replay 与 CCPlus 合成裁决。
21. Personal KB tool-only 与 Knowledge authority 结论。
22. 代码极简性结论。
23. 全部 canonical leaf 断点、family/alias/scenario 映射、coverage ledger 与机器可重算 delta。
24. P0/P1 上线阻断项。
25. 双事实源和旁路清单。
26. 治理、RLS、预算、审批冲突清单。
27. 无消费路径清单。
28. 应删除、合并或收敛的抽象。
29. 已知缺失、排除项、coverage gap 和未证实项。
30. `canonical leaf / Missing → owner Group` 唯一映射，以及 Group 依赖图。
31. 每个 Group 的有序 `@必须先读`、按需文档、源码入口、Red、退出门和证据锚点。
32. 按依赖排序的一次完整落地方案。
33. 迁移、回填、清理和回滚方案。
34. 验收矩阵与故障注入方案。
35. Group 证据索引与单 leaf / 同根家族证据写回模板。
36. 残余风险。
37. 整体和分模块置信度。

# 十一、置信度

置信度必须来自证据，不得为了满足目标而虚构。

至少计算：

- 生产路径覆盖率；
- 七原子覆盖率；
- 源码证据覆盖率；
- 数据库/RLS覆盖率；
- UI 消费覆盖率；
- 失败与恢复路径覆盖率；
- hard/soft limit inventory 覆盖率；
- 上下文容量链和 capability discoverability 覆盖率；
- fan-out/fan-in、层级预算与 return-storm 覆盖率；
- 跨渠道 A2A 因果/authority/delivery 覆盖率；
- wait-for graph 与 model-visible failure 覆盖率；
- §6.5 极端测试已执行、模拟、未证实的覆盖率；
- 基线源码对比覆盖率；
- CCPlus paired replay 的普通路径非劣与极端路径净增益覆盖率；
- 测试与 live evidence 覆盖率；
- 无法访问范围。

目标是获得至少 95% 的审查置信度，而不是强行宣称 95%。如果证据不足，必须明确：

- 哪些范围未验证；
- 为什么未验证；
- 可能隐藏什么风险；
- 补充什么证据才能提高置信度。

# 十二、落地方案约束

修复建议不能只写“增加校验”“优化 UI”“补充测试”。每项必须说明：

- 唯一事实源；
- 唯一写入口；
- 状态机变化；
- 权限决策点；
- 模型语义主权如何保留；
- 证据在哪里产生；
- 失败后如何恢复；
- 谁消费结果；
- UI 如何呈现；
- 旧路径如何迁移和删除；
- 数据如何回填；
- 如何观测；
- 如何测试和故障注入；
- 如何证明能力没有被削弱。

## 12.1 终极施工文档与 `@文档路由` 合同

如果本轮报告将作为后续全量修复、更新和最终复审的总入口，必须在同一报告中建立以下可执行合同：

1. **唯一 owner**：每个 `canonical_leaf_id` 恰好属于一个 owner Group；每个 Missing 恰好属于一个建设 Group。family、alias、scenario、coverage gap 可以跨 Group 引用，但不能成为第二 owner，也不能参与 breakpoint 分母。
2. **完整显式清单**：Group 必须逐项列出 owner ID。禁止使用“剩余断点”“其它 P3”“A-05–A-08”之类无法机械展开的 catch-all；若为阅读使用范围缩写，必须另有逐 ID 的机器区块。
3. **文档路由**：每个 Group 必须包含按顺序的 `@必须先读`、带触发条件的 `@按需读取`、历史取证文档和当前源码入口。所有路径必须在冻结快照中存在；历史文档只能提供证据线索，不能覆盖当前源码或 canonical 规范。
4. **关键规范不可降级**：若某份设计文档定义跨 Group 的决策 ID、状态机或 golden scenarios，总报告必须给出完整交叉表和唯一 owner；Group 摘要不能替代阅读原文，也不能静默省略规范条款。
5. **施工门**：每个 Group 必须写依赖 Group、修复前 Red、权威事实源、唯一 live entry、migration/backfill/rollback、退出门和稳定 `EVID-G<group>-<序号>` 前缀。
6. **同文档回填**：实现完成后，必须把 HEAD/worktree/hash、Red/Green 命令与退出码、数据库/事件/trace/Artifact/UI/生产证据、commit/deploy/canary、七原子结论和残余风险写回总报告；外部 commit、PR 或聊天链接不能代替报告内的可验证索引。
7. **原子状态同步**：证据写回时同时更新 leaf ledger、Group 证据索引、Missing 状态和数量 delta。只把 Group 标绿、但不更新 leaf；或只改总数、不更新 owner map，均判定失败。
8. **机器门禁**：报告必须提供稳定标记区块，使 CI 可以验证 canonical ID 集合、owner 唯一性、Missing 唯一性、Group 数量和、关键规范映射、文档路径、证据 ID 与状态一致性。
9. **发布语义**：Group 顺序表示依赖和推荐施工序，不表示低优先级 Group 未完成时已独立闭环的 P0/P1 不能发布；one-pass 约束每个已开工 leaf/同根家族的完整交付，不是 103/103 式全局发布锁。

建议实施依赖顺序：

1. 已确认的 P0 安全/跨租户/不可逆效果漏洞独立闭环并立即发布，不等待容量治理或低优先级项。
2. principal、authority、事实源和唯一 effect 入口；保住真正必须硬执行的安全边界。
3. canonical Session event/item、typed limit/failure/terminal/progress envelope、persist-before-publish、幂等 gap recovery 与同一 reducer；先建立后续 admission、result、recovery 可共同引用的机械事实语言。
4. 全仓 limit registry 与 hard/soft 重分类；移除无事实源的机械终态，不删除必要 authority/effect ceiling。
5. parent/child reserve→commit/release 预算账本、有界 admission/queue 与统一 root execution ledger；未 admission 的 child 不得进入 expected/wait set。
6. coverage/result manifest、large-result reference、mailbox CAS/lease、integration epoch、100-way fan-out/fan-in 与 return-storm backpressure。
7. fleet scheduler/trigger 的分页、分片、per-root/per-tenant 公平与 control-plane headroom；fleet definition 数量与单 root child 数量分开验收。
8. context capacity ledger、model-led compaction、全量可发现的 Skill/MCP/Agent/Workflow progressive disclosure 与 same-model output recovery。
9. 状态机、幂等、resume/replay/cancel、sweeper 和 Durable execution。
10. 跨渠道 A2A execution frame、因果/sequence/idempotency、Agent result 与 channel delivery 分离、partial/late/revoked recovery。
11. Artifact / Workspace / 父 Agent / UI 最终消费。
12. Memory / Knowledge / Skill evolve 与行为级 eval。
13. UI 信息架构、pressure/partial/recovery 交互和用户旅程。
14. 代码简化、兼容清理和文档同步。

上述是依赖和排程，不是把全部断点绑成一个发布列车。每个开工前完整界定的原子修复/同根家族必须一轮交付 Red→Green、迁移回填、可观测性、恢复、真实消费和发布验收；高优先级项自身闭环后可以独立发布，不能被尚未完成的容量测试、UI 或 P3 清理拖住。

# 十三、禁止事项

- 不要只总结已有文档。
- 不要用旧报告的“已完成”代替当前验证。
- 不要因为有 API、表、页面或测试文件就判定闭环。
- 不要只检查 happy path。
- 不要只检查后端或前端。
- 不要忽略 Worker、队列、RLS、对象存储、部署拓扑和 Workspace。
- 不要预设断点数量。
- 不要预设历史审查次数。
- 不要复制固定日期或历史编号。
- 不要把计划能力写成已实现。
- 不要把供应商私有远程能力伪装成 parity 债务。
- 不要用治理、安全、成本、KISS 或测试确定性为机械削弱模型能力辩护。
- 不要把 Personal KB 重新塞进原始上下文。
- 不要把企业知识库的已知缺失伪装成回归或用 legacy surface 冒充完成。
- 不要把所有资源限制一律删除；必须区分安全/权威硬边界、物理硬边界、显式经济上限和运行软预算。
- 不要因为设置了 semaphore、max turns、timeout 或 result cap 就宣称系统有界；必须验证排队、保留、模型可见状态、恢复和总账守恒。
- 不要用 1/5/10 个 Agent 的通过外推 100-way fan-out 已闭环，也不要真的制造无审批的付费/外发压力来“证明”容量。
- 不要把 fleet 中已注册/可路由 Agent definition 的数量写成同一时刻的模型进程数；也不要把单 root 的 100 个 child 误写成平台只运行 100 个 Agent。
- 不要把 synthetic fake-provider 测试写成真实 provider/channel 证据；模拟、集成、smoke、production shadow 必须分层报告。
- 不要在没有 inventory/coverage ledger 的情况下宣称“找到了全部断点”；必须给出已覆盖分母、未覆盖面和账本更新规则。
- 不要把“剩余断点”“其它项”或范围缩写当 owner map；每个 canonical leaf 与 Missing 都必须能机械解析到唯一 Group。
- 不要只在 Group 摘要中转述关键设计文档；全文规范、决策 ID 和 golden scenarios 必须有交叉表、唯一 owner 和证据回填位置。
- 不要让实现证据只存在于 commit、PR、外部日志或聊天；总报告必须保留稳定证据索引并同步 leaf/Group/Missing 状态。
- 不要把跨渠道发送成功当 Agent 任务成功，也不要把 Agent 完成当所有渠道已送达。
- 不要用 same owner 替代 requester、tenant、delegation、connector credential 和 sensitivity 的逐 hop 权威校验。
- 未经授权不要修改生产代码或生产数据。

# 十四、完成条件

只有满足以下条件才算审查完成：

- 四个审查模块全部覆盖；
- 北极星、AI-native 和 Model Agency 单独完成裁决；
- 核心生产路径完成正向和反向追踪；
- 每个重要能力完成七原子判定；
- 闭环结论与断点结论都有当前证据；
- 对 CC/FreeCode、Codex 和内部 lean benchmark 完成当前源码对比；
- CC、Codex 与 Hive Native 已收敛到一个 lifecycle/principal/evidence/result contract，并用同模型同 fixture paired replay 验证普通路径非劣和 Hive 极端能力净增益；
- Personal KB tool-only 和 Enterprise Knowledge 边界明确；
- 治理是否错误阻断 Agent 被单独审计；
- 全仓 limit/budget/timeout/slice/queue/fan-out registry 已建立，每个 hard outcome 都有合法类别、权威事实源、模型可见状态和恢复路径；
- attempt 与 task/session 终态已经分离；每个 nonterminal pressure/pause 都有 progress certificate、可达 resume edge、admission-before-expected、no-hold-and-wait 和 retry fingerprint 证据；
- context capacity ledger 追到 provider request，证明 catalog/Memory/tool/child result 在极端规模下保持可发现、可外置、coverage 诚实且不静默丢弃；
- 100-way 同时返回、mixed partial、nested fan-out、cancel/restart 和 return storm 已按 §6.5 验证，或明确交付未证实原因与可执行 harness；
- fleet plane 的 registry/trigger/worker fairness 已与 root execution tree 分开验证；一个 noisy root 不会无界饿死其它 tenant/root，daemon 扫描可分页恢复；
- direct Sub-agent、Agent Team 与 Workflow 已投影到同一 root requested/admitted/expected/result/integration contract；
- live/history/reconnect/reload/resume 使用同一 canonical Session event/item reducer，stable identity、sequence gap、duplicate/out-of-order 与 publish failure 已验证；
- 400 Skill / 200 MCP / 大量 Sub-agent/Workflow 的 discovery、schema lazy-load、权限变化和 cache stability 已验证；
- 钉钉/飞书/Slack/Web 跨渠道 A2A 的 principal、delegation、causation、idempotency、partial delivery、auth expiry/revocation 和 parent consumption 已验证；
- wait-for graph 已覆盖 barrier/approval/budget/queue/channel/hook/compaction，真实 cycle、lost wakeup 和 retry storm 有测试与恢复证据；
- 每种 soft pressure 和 hard stop 都能被模型、UI 和 operator 感知，且 durable state、coverage、remaining budget 和 next actions 可恢复；
- 单 Session、单模型、无替代 provider 的恢复路径已验证；换模型/加机器不是唯一恢复答案；重复 observation 已聚合且不反向制造 Prompt storm；
- UI 信息分层、Workspace、Artifact 和多 Agent 状态消费被验证；
- 完成 KISS 和无消费者扫描；
- 断点数量只统计 canonical leaf；family、alias、scenario、coverage gap、Missing、Excluded 与 Unverified 已分账，并输出机器可重算 ledger delta；
- 每个 canonical leaf 与 Missing 已映射到唯一 owner Group；每个 Group 的依赖、`@必须先读`、按需文档、源码入口、Red、退出门和 `EVID-*` 锚点完整且路径存在；
- 跨 Group 关键设计文档的全部决策 ID、状态机和 golden scenarios 已建立交叉表，没有被 Group 摘要省略或降级；
- 总报告具备 Group 证据索引和单 leaf / 同根家族写回模板，且有机器门禁验证 owner、数量、引用路径、证据 ID 与状态同步；
- 给出按依赖排序、无隐藏半成品的一次完整落地方案；
- 明确未覆盖范围、残余风险和真实置信度。

最终断点数量只能是绑定当前 HEAD/工作树/生产快照和 coverage ledger 的工作账本。审查完成不等于产品无缺陷；若图谱、真实队列/DB、provider、渠道或生产 telemetry 无法访问，必须保留未证实项，不能用文档推理补成“全部已找到”。

现在开始。先输出仓库地图、权威顺序和审查计划，再进行源码级完整扫描。在没有完成生产路径、失败路径、消费者和基线源码验证前，不要提前给出最终结论。
````
