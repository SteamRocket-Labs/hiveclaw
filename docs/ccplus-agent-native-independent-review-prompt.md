# CCPlus Agent-Native 独立原子化审查 Prompt

## 文档定位

这是一份可长期复用的独立审查提示词，用于从当前源码和运行证据重新评估 Agent-Native 系统。

它只固定四类内容：

1. 产品北极星和不可倒置的能力边界；
2. 证据驱动的原子化审查方法；
3. 最终报告必须回答的问题；
4. 向独立 Eval 阶段交接尚未证明的能力主张。

它不预设当前架构正确，不预设断点、规模、状态机、字段、修复方案或报告结论。具体工程答案由审查模型根据当前源码独立得出。

使用时复制下面“可复用正文”的全部内容。若仓库根规范没有声明 CC、Codex、Hive Connect 或内部 benchmark 的源码位置，再在任务开头补充这些路径；不要把日期、审查轮次、历史断点数或旧结论写入模板。

## 可复用正文

````text
你现在需要对当前代码仓库执行一次独立、完整、证据驱动的 CCPlus / Agent-Native 原子化架构审查。

本任务的目的不是验证已有设计是否“看起来合理”，而是从当前源码、测试、迁移、运行路径和真实消费者重新建立结论。不要继承任何既有完成声明，不要预设断点数量，不要因为仓库已有某种实现或文档就假定它是正确答案。

本任务默认只读。除非用户另行明确授权，否则不要修改业务代码、数据库、部署配置、生产数据或现有文档。若需要保存报告，新建独立文件并先声明路径，不要覆盖已有报告。

# 一、产品双北极星

Hive 只有两个顶层产品目标：

1. 最强可控数字员工
   - 单 Agent 的推理、工具使用、上下文利用、长期记忆、自进化、技能成长、可靠性和安全边界必须达到或超过仓库根规范指定的 lean benchmark。
   - 一个治理更复杂、组件更多，但实际更弱、更笨、更容易卡住的 Agent，不算成功。
   - 单 Agent 能力是基础，必须先建设、先评判。

2. 公司级 Agent 控制中台
   - 在企业规模下运营 Agent，覆盖组织、身份、权限、预算、安全、审计、协作、生命周期、AI 资产和可观测性。
   - 控制中台必须建立在强 Agent 之上，不能以控制面的完整度掩盖 Agent 能力退化。

任何设计只能增强这两个目标，不能用低层工程便利覆盖高层产品目的。

# 二、最高设计法则：先释放模型，再约束行动

在认证身份、授权数据范围、外部效果权限、显式资源边界和执行隔离建立之后：

- LLM 负责语义理解、判断、推理、综合、规划、优先级、学习、反思和最终表达；
- 平台负责身份、权限、数据入口、外部副作用、执行隔离、资源、精确机器契约、机械事实、证据、恢复、审计和持久化提交；
- 治理约束 Agent 可以读取什么、调用什么和产生什么外部效果，不替模型决定开放语义；
- 一个效果被拒绝，不应自动削弱无关推理、只读能力、其它合法工具或最终回答；
- 可靠性和确定性的目标是让权限、效果、状态、证据与恢复可预测，而不是让模型的思考和表达更容易被平台预测。

审查时必须区分：

1. 有外部权威事实源的真实硬边界；
2. 可以调整、排队、恢复或由模型重新规划的工程策略；
3. 用机械规则替代模型判断的能力限制。

不要在没有源码和事实源证据时预先判定某个机制属于哪一类。

# 三、CCPlus 的定义与裁决顺序

CCPlus 不是功能清单拼接，而是以下能力在同一个生产生命周期中的能力保持型合成：

```text
CC / FreeCode 原生能力与完整生命周期语义
+ Codex 的工程可靠性、控制机制与 UI/UX 表达增量
+ Hive Connect 的云端、本地、设备与渠道连接能力
+ Hive Native 的 Memory、自进化、Skill evolve、Dynamic context、A2A 与 Workflow
+ 企业身份、权限、安全、预算、审计、AI 资产与控制中台
= CCPlus / Hive Agent Infrastructure
```

## 3.1 CC / FreeCode：能力语义基线

CC / FreeCode 决定本地 Agent 原生能够做什么，以及完整生命周期应具有什么可观察语义。它是能力下限和行为基线，不是逐行复制模板。

审查必须先直接读取当前本地 CC / FreeCode 源码，建立完整能力基线，再评价 Hive。不要只依赖产品印象、二手文档、功能名称或旧对比结论。

至少从完整生命周期出发建立源码清单，包括但不限于：Agent 定义与身份、上下文组装、用户输入、transcript、模型循环、工具发现与执行、权限与审批、hooks、Plan/Task/Skill、compaction、stop/cancel、resume/retry、checkpoint、rewind/fork/branch、workspace/artifact、Sub-agent/Team、session close 以及用户消费面。

这只是检索起点，不是穷举清单。模型必须根据当前源码主动发现遗漏能力。

若一个能力属于 CC 本地进程、文件系统、workspace、session、transcript、tool、hook、sandbox 或终端状态语义，它原则上属于 CCPlus 能力范围。只有依赖供应商私有、不可访问的托管远程服务时，才可以从 CC parity 债务中排除，并说明证据。

## 3.2 Codex：非冲突的工程与产品表达增量

Codex 用于回答两类问题：

1. 如何让 CC 能力具备更清晰、可靠、可恢复、可观测的工程实现；
2. 如何让用户以更克制、更连续、更可操作的方式理解和消费 Agent 状态与交付物。

应直接读取当前 Codex 源码和桌面端行为证据，识别真正可复用的工程与交互优势。Codex 增量只有在不删除、不隐藏、不重定义 CC 能力语义时才成立。

Codex Desktop 是信息层级、状态连续、渐进披露、恢复操作和交付物优先体验的参考，不是要求复制某个固定布局、组件树或视觉样式。

## 3.3 Hive Connect：连接与执行连续性增量

Hive Connect 负责把云端 Agent 与本地环境、设备能力、用户工作区和外部渠道连接起来。

审查模型需要从当前源码独立判断其真实能力边界、身份与授权传递、任务连续性、断线恢复、结果返回、文件与 artifact 交付，以及它与主 Agent 生命周期的关系。不要预设它必须采用某种传输、状态机或拓扑。

Hive Connect 不应因为执行位置变化而制造能力缩水、隐式权限继承、第二套互相冲突的任务事实或无法回到主 Agent 的孤立结果。

## 3.4 Hive Native：主动超越基线的能力

Hive Native 是 Hive 的长期差异化能力，包括但不限于 Memory、reflection、自进化、Soul、Skill evolve、Dynamic context、Local Agent、A2A、Sub-agent、Agent Team、Workflow、Knowledge、长期任务与后台执行。

这些能力不能为了表面 parity、代码收敛或治理便利被删除。模型需要独立检查它们是否真正进入生产 Agent 生命周期，是否有真实消费者，是否形成学习与执行闭环，以及是否反过来削弱了单 Agent。

Personal Knowledge Base 是一个已经确定的产品边界：它通过受治理的工具按需发现、搜索、读取和引用，不预取或静态注入最原始上下文。Enterprise Knowledge 建立在知识工具与组织权限平面之上；两者必须分别审查，不能互相冒充。

## 3.5 企业治理：能力保持型控制中台

企业治理负责组织、身份、principal、tenant、ownership、delegation、ACL/RLS、approval、sandbox、credential、预算、审计、留存、合规和 AI 资产管理。

治理必须在最窄的权威边界约束未授权数据与外部效果。审查模型需要独立判断：

- 权威事实来自哪里；
- 是否存在绕过或双重判断；
- 权限、RLS、审批、预算和 sandbox 是否互相冲突；
- 拒绝是否只冻结受影响的效果；
- 合法 Agent 是否可能被治理机制机械卡死；
- Agent、Skill、Sub-agent、Team、Workflow、Knowledge、Connector 等 AI 资产是否可管理、可追溯、可撤销和可审计。

## 3.6 裁决顺序

发生冲突时，按照以下顺序判断：

1. 产品双北极星，且单 Agent 能力先建设、先评判；
2. 模型语义主权与能力释放；
3. CC / FreeCode 原生能力与生命周期语义；
4. Codex 非冲突的工程可靠性与 UI/UX 增量；
5. Hive Connect 和 Hive Native 主动增强；
6. 企业治理与控制中台；
7. 七原子闭环证据；
8. KISS、奥卡姆剃刀和可维护性。

低层规则可以加强高层目标，但不能覆盖或缩小高层目标。

# 四、四个审查模块

## 4.1 单 Agent

从用户请求进入系统开始，一直追踪到模型理解、工具使用、状态持久化、恢复、最终回答和交付物消费。

核心问题是：Hive 是否完整保留 CC 原生生命周期和能力语义，在此基础上吸收 Codex 工程与交互优势，并完成必要的云端适配；这些优化是否真正提高了 Agent，而不是增加旁路、双事实源或机械阻断。

## 4.2 Hive Native

检查所有超越 CC parity 的能力，而不只检查本提示词举出的名称。重点判断它们是否：

- 有真实生产入口和消费者；
- 与单 Agent 生命周期连接；
- 可以恢复、回滚和审计；
- 保留模型判断权；
- 形成 Memory、Skill、Knowledge、Workflow、A2A 和长期任务之间的闭环；
- 对 benchmark 任务产生可验证净增益。

## 4.3 企业治理与 AI 资产

检查企业身份、权限、安全、预算、合规、审计、组织关系、Agent 生命周期以及 Agent/Skill/Sub-agent/Team/Workflow/Knowledge/Connector 等资产管理。

不要只检查“是否有表或页面”。必须检查治理是否进入唯一执行路径、是否可绕过、是否与 RLS/approval/sandbox 冲突，以及用户或 Agent 如何理解拒绝、请求审批、恢复和继续。

## 4.4 用户功能与 UI/UX

从真实用户使用 Agent 的过程审查产品，而不是从后端对象数量推导 UI。

UI/UX 不是只在第四模块末尾检查的“展示层”，而是贯穿单 Agent、Hive Native 和企业治理的产品消费平面。对前三个模块发现的每项能力，都必须继续追到用户可见状态、所需动作、恢复入口、结果/交付物和正确受众；后端闭环但用户无法理解、介入或消费时，仍然是未闭环。

模型需要独立判断：

- 普通用户真正需要看到什么；
- 哪些事实只属于管理员、审计员或调试面；
- intent、progress、decision、required action、failure、recovery 和 deliverable 是否清晰；
- Plan、Goal、Task、Workflow、Background/Scheduled work、Branch/Rewind、Sub-agent、Team 和 A2A 是否有一致的触发、状态和消费逻辑；
- Workspace 应呈现哪些真实文件、artifact、版本和交付状态；
- live、reconnect、reload、history、resume 后是否表达同一事实；
- 原始 schema、内部 ID、provider payload 和 forensic evidence 是否被错误暴露给普通用户。

不要预设三栏或任何固定布局。先建立用户任务、信息层级和状态语义，再评价当前布局与 Codex Desktop 参考之间的差距。

前端审查至少区分三种受众：普通用户、Owner/Manager、Operator/Auditor。不要把管理面等同于调试面，也不要因为用户是 Agent owner 就默认暴露 raw schema、ID、payload、tool arguments、provider 细节、token 细账或 forensic evidence。

把当前 backend/transcript/artifact/runtime/governance truth 与首页、Agent 概览、Session、右侧面板、Workspace、通知和公司后台逐项对账。正文、状态、数量、归属、可下载性、下一步和恢复动作互相矛盾时，按 Evidence → Consumption 断点登记，而不是只作为视觉建议。

若本次审查证据显示前端产品化是主要风险，输出独立 Frontend Experience Handoff，交给 `ccplus-frontend-product-review-prompt.md` 在新 Session 中做真实浏览器、角色、视觉与交互审查；不要在总 Review 中临场指定布局或替设计 Reviewer 预写答案。

# 五、原子化闭环标准

“有 API”“有表”“有 Service”“有事件”“有组件”“有页面”都不等于能力完成。每个能力都必须检查七个原子：

1. 输入（Input）
   - 谁发起，输入结构是什么，是否经过验证、保存并可恢复。

2. 权威（Authority）
   - 谁有权读取、决定和写入；tenant、organization、user、agent、owner 与 delegation 如何绑定。

3. 执行（Execution）
   - 唯一真实执行入口是什么；是否存在旁路、重复执行器或绕过治理的路径。

4. 证据（Evidence）
   - event、span、transcript、文件和数据库中谁是机械事实源；顺序、因果和归属如何证明。

5. 恢复（Recovery）
   - 断线、重启、超时、重试、取消、回滚、fork、重复回调和部分成功是否幂等并可继续。

6. 消费（Consumption）
   - Memory、Skill、Workflow、Knowledge、父 Agent、UI、Workspace 和最终用户是否真实消费产物。

7. 验收（Acceptance）
   - 测试、迁移、回填、故障注入、可观测性和真实用户路径是否覆盖。

只使用以下状态：

- 闭环：七个原子均有当前真实消费路径；
- 局部闭环：主路径成立，但存在双事实源、旁路、恢复或 UI 断点；
- 断点：能力存在，但生产路径在两个原子之间断开；
- 缺失：当前源码没有实现；若明确暂不建设，标记为已知缺失；
- 排除：供应商私有远程能力，不计入 CC parity 债务，但必须给出排除依据；
- 未证实：当前证据不足，不能把猜测写成完成或断点。

# 六、证据和独立判断规则

## 6.1 事实优先级

原则上按以下顺序建立结论：

1. 当前 checkout 的真实源码、迁移和配置消费者；
2. 可复现的运行路径、数据库事实、event/span/transcript 与 UI 行为；
3. 当前测试及其断言的真实语义；
4. 当前 CC / FreeCode、Codex、Hive Connect 和 benchmark 源码；
5. canonical 产品与架构文档；
6. 历史报告、注释、README 和设计草稿。

文档说明意图，不能单独证明实现。测试是证据，但如果测试保护了错误的能力限制，也不能因为测试通过就判定设计正确。

## 6.2 CCPlus 基线账本

在评价 Hive 前，先从当前源码建立 CCPlus 基线账本。每条至少包含：

- 能力或生命周期节点；
- CC / FreeCode 源码证据与可观察语义；
- Hive 当前映射；
- Codex 可吸收的工程或 UI 增量；
- Hive Connect / Hive Native / 企业治理增量；
- 差异类别：缺失、语义退化、可接受实现差异、工程增强、主动超越或排除；
- 七原子状态与证据缺口。

如果基线源码不可访问，标记未证实并说明需要什么证据；不要用产品印象补齐。

## 6.3 把工程答案还给模型

以下内容必须由审查模型从证据中决定，不能因为本提示词提到某种概念就把它当作既定答案：

- 实际能力清单和系统拓扑；
- 事实源、状态机和执行入口；
- hard/soft boundary 的具体分类；
- 并发、预算、超时、重试和容量策略；
- Sub-agent、Team、Workflow、A2A 的触发与返回关系；
- Workspace、Artifact、Session 与 UI 的具体组织方式；
- 断点数量、严重程度和优先级；
- 最小复杂度目标架构；
- 具体字段、协议、队列、缓存、迁移和修复方案。

模型可以提出任何证据支持的方案，也可以推翻仓库中的既有设计。不得为了符合本提示词的示例而制造发现。

# 七、执行方法

1. 记录本次审查的仓库根、HEAD、工作树状态、可访问环境和证据时间点。
2. 完整读取当前根指令及其声明的 canonical 文档，但不把文档声明当成实现证据。
3. 确认并读取当前 CC / FreeCode、Codex、Hive Connect 和 benchmark 源码；从根规范获取其路径和优先级。
4. 建立 CC 原生能力与完整生命周期基线账本。
5. 建立 Hive 的真实入口、实体、状态、事实源、异步边界与消费者地图。
6. 按四个模块沿生产路径正向追踪，并从 UI、Workspace、Artifact、Memory、审计和最终交付反向追踪。
7. 对每个能力检查七个原子，以及 timeout、disconnect、restart、cancel、retry、denial、partial success、duplicate、stale state 和 dependency unavailable 等失败路径。
8. 对权限、RLS、approval、sandbox、预算和模型能力限制做冲突审查。
9. 对代码做第一性原理、KISS、奥卡姆剃刀和消费者反向扫描，寻找重复入口、双事实源、无消费者能力、兼容层堆积和不必要抽象。
10. 对每个候选发现先尝试反证；只有能够指出真实生产断裂、用户影响和当前证据时，才登记为断点。
11. 若可以安全运行测试或只读验证，执行与风险成比例的验证；若不能运行，明确标记未证实，不伪造结果。

源码证据应尽量包含准确文件路径、symbol/function/class、调用关系和行号。关键结论必须区分：已验证事实、证据支持的推断、尚未解决的不确定性。

# 八、报告要求

最终输出一份独立中文报告，至少回答：

1. 当前系统是否符合两个北极星，单 Agent 是否真的先于控制面成立；
2. 当前 CC 原生能力基线是什么，Hive 的逐项映射和差异是什么；
3. Codex 工程优化与 UI/UX 增量中，哪些已吸收、哪些缺失、哪些错误改变了 CC 能力；
4. Hive Connect、Hive Native 和企业治理是否形成同一个可恢复的生产生命周期；
5. 四个模块各自有哪些闭环、局部闭环、断点、缺失、排除和未证实项；
6. 哪些治理或工程机制正在不必要地限制模型；
7. 普通用户、企业管理员和审计/调试人员目前看到的信息是否合理；
8. 哪些代码、状态、事实源或产品概念可以删除、合并或收敛，同时保留全部真实能力；
9. 上线阻断项、完整修复方向、迁移/回填/清理要求和验收证据；
10. 当前证据覆盖、残余风险和真实置信度；
11. 哪些关键能力仍需独立 Eval 证明，以及对应的 Eval Handoff。
12. 哪些能力在后端成立但尚未形成可信的前端消费，以及是否需要独立 Frontend Experience Handoff。

## 8.1 建议的主报告结构

1. 执行摘要与上线判断；
2. 审查范围、当前环境与未覆盖面；
3. 双北极星与 Model Agency 裁决；
4. CCPlus 基线账本与源码对照；
5. 单 Agent 审查；
6. Hive Connect 与 Hive Native 审查；
7. 企业治理、安全与 AI 资产审查；
8. 用户功能与 UI/UX 审查；
9. 七原子矩阵与断点清单；
10. 代码极简性和目标架构建议；
11. Eval Handoff 与待证明能力；
12. Frontend Experience Handoff（若适用）；
13. 完整落地方向与验收矩阵；
14. 未证实项、残余风险与置信度。

这只是结果组织结构，不限制模型增加必要章节，也不要求为没有证据的主题制造内容。

## 8.2 每个断点的最小记录

每个真实断点至少记录：

- 稳定编号和所属模块；
- 当前状态与严重级别；
- 用户可见影响；
- 断裂的两个原子；
- 当前源码、运行、数据库、测试或 UI 证据；
- 根因及其权威事实源；
- 属于 CC 语义退化、Codex 增量缺失、Hive Connect/Hive Native 断裂还是治理冲突；
- 是否削弱模型能力或存在越权风险；
- 最小但完整的闭环方向；
- 迁移、回填、清理、恢复和验收要求；
- 反证和未解决的不确定性。

不要用字段数量制造“原子化”的假象。记录项只为证明真实断裂、影响和闭环条件服务。

# 九、代码极简性原则

KISS、第一性原理和奥卡姆剃刀用于删除偶然复杂度，不用于删除真实能力。

“架构保持”不是冻结当前实现。需要保持的是双北极星、CC 原生能力语义、Hive Native 主动优势、唯一权威边界和已经被真实消费者依赖的产品合同；重复实现、历史旁路、错误抽象和无消费者代码不因“保持架构”而获得保留资格。

审查模型应独立判断：

- 是否有多个含义相同的入口、状态、事实源或抽象；
- 是否有组件、表、事件、API、Service 或配置没有生产消费者；
- 是否把不同概念错误合并，或把同一概念重复实现；
- 是否为了测试确定性、治理便利或成本优化而牺牲模型能力；
- 是否存在更小、更清晰、可迁移且保持能力的设计；
- 模块依赖方向、ownership、public contract 和失败边界是否清楚；
- 新增一个 Tool、Skill、Provider、Connector、Agent 资产、Workflow 或治理策略时，是否需要修改不相关核心、复制状态机或增加条件分支；
- 大规模代码中是否存在高耦合热点、循环依赖、重复协议、巨型模块、隐式副作用、性能退化和测试盲区；
- 性能优化是否基于测量，并且没有通过静默裁剪上下文、能力、证据或模型输出换取指标。

每个简化建议都必须说明保留了什么能力、删除了什么复杂度、如何迁移，以及如何证明没有产生回归。

# 十、Eval Handoff：审查与评测的边界

本任务只负责审查，不负责同时设计完整考试、执行评测或作出发布裁决。审查者可以识别“还需要证明什么”，但不能为了完成报告而临场设计一套容易通过的测试，再用它证明自己的判断。

对于影响北极星、上线判断或关键能力声明、但仅靠本次源码和运行审查无法充分证明的事项，输出一个简洁的 Eval Handoff。每项至少说明：

- 待证明的能力主张及其所属模块；
- 当前已有证据、代表性 trace 或事实缺口；
- 为什么现有证据不足以完成判断；
- 应当比较的基线或历史稳定状态；
- 必须机械验证的硬不变量，以及需要开放质量判断的部分；
- 需要的环境、权限与安全边界；
- 若不验证，可能留下什么产品或发布风险。

不要在本报告中预写完整场景、固定阈值、grader 权重或终极大考答案。独立 Eval Designer 将根据 Handoff、真实生产分布和当前能力边界形成不可变的 Eval Manifest；执行者按 Manifest 取证；发布裁决者只消费证据。具体协作方式以仓库中的 `ccplus-review-eval-playbook.md` 为准。

若审查已经通过确定性事实完整证明某项主张，不要为了形式再制造 Eval。若需要 Eval，也不得据此建设第二套生产、常设合成场景平台或 Eval 专用前后端。

# 十一、置信度

目标是获得高置信度结论，而不是强行输出一个高数字。

审查置信度只描述“本报告对当前事实和断点的覆盖程度”，不等于系统已经可以发布。只有核心生产路径、七原子、CC/Codex 源码对照、权限/RLS、失败恢复、UI 消费和测试证据得到充分覆盖时，才能对审查结论声明 95% 或更高置信度。

系统级发布置信度必须另外消费独立 Eval 的真实 receipt；关键能力仍处于 UNVERIFIED 时，不得用审查置信度替代发布证据。

如果证据不足，必须诚实说明：

- 哪些范围未验证；
- 为什么未验证；
- 可能隐藏什么风险；
- 需要补充什么源码、运行、数据库、UI 或测试证据；
- 当前置信度因此受到什么限制。

# 十二、最终提醒

这份提示词提供的是目标、边界、审查方法和交付契约，不是架构答案。

你的职责是：

1. 从当前事实建立完整的 CC 原生能力基线；
2. 判断 Codex、Hive Connect、Hive Native 和企业治理如何在不削弱该基线的前提下形成 CCPlus；
3. 独立发现真实断点和不必要复杂度；
4. 给出证据支持、能力保持、可以完整验收的最终方向；
5. 把尚未证明的关键主张准确交接给独立 Eval 阶段，而不是在同一任务里自审、自测、自证。

不要为了迎合提示词而证明某个预设方案。若源码证据与产品假设冲突，以证据为基础明确指出冲突，再按北极星给出裁决。
````

## 设计说明

这份模板刻意采用“稳定入口 + 当前仓库事实 + 渐进披露”的方式：Prompt 规定目的、边界和证据合同，源码与 canonical 文档提供可发现上下文，工程结论由模型独立完成。

参考：

- [OpenAI — Harness engineering: leveraging Codex in an agent-first world](https://openai.com/index/harness-engineering/)
- [OpenAI — Codex Prompting Guide](https://developers.openai.com/cookbook/examples/gpt-5/codex_prompting_guide)
- [OpenAI — Best practices for prompt engineering](https://help.openai.com/en/articles/6654000-guidelines-for-prompting-large-language-models)
- [OpenAI — PaperBench](https://openai.com/index/paperbench/)
- [OpenAI — Predicting model behavior before release by simulating deployment](https://openai.com/index/deployment-simulation/)
- [OpenAI — Evaluate agent workflows](https://developers.openai.com/api/docs/guides/agent-evals)
- [OpenAI — Evaluation best practices](https://developers.openai.com/api/docs/guides/evaluation-best-practices)
- [OpenAI — Trace grading](https://developers.openai.com/api/docs/guides/trace-grading)
